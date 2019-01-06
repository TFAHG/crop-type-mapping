import torch
from utils.classmetric import ClassMetric
from utils.logger import Printer, VisdomLogger, Logger
import os

CLASSIFICATION_PHASE_NAME="classification"
EARLINESS_PHASE_NAME="earliness"

class Trainer():

    def __init__(self,
                 model,
                 traindataloader,
                 validdataloader,
                 epochs=4,
                 switch_epoch=2,
                 learning_rate=0.1,
                 earliness_factor=0.7,
                 entropy_factor=0.3,
                 store="/tmp",
                 test_every_n_epochs=1,
                 visdomenv=None,
                 show_n_samples=1,
                 loss_mode="twophase_linear_loss", # early_reward, twophase_early_reward, twophase_linear_loss, or twophase_early_simple
                 overwrite=True,
                 **kwargs):

        self.epochs = epochs
        self.earliness_factor = earliness_factor
        self.switch_epoch = switch_epoch
        self.batch_size = validdataloader.batch_size
        self.traindataloader = traindataloader
        self.validdataloader = validdataloader
        self.nclasses=traindataloader.dataset.nclasses
        self.entropy_factor = entropy_factor
        self.store = store
        self.test_every_n_epochs = test_every_n_epochs
        self.logger = Logger(columns=["accuracy"], modes=["train", "test"], rootpath=self.store)
        self.show_n_samples = show_n_samples
        self.lossmode = loss_mode
        self.model = model
        self.optimizer = torch.optim.Adam(model.parameters(), lr=learning_rate)

        if visdomenv is not None:
            self.visdom = VisdomLogger(env=visdomenv)

        self.epoch = 0

        if os.path.exists(self.get_classification_model_name()) and not overwrite:
            print("Resuming from snapshot {}.".format(self.get_classification_model_name()))
            self.resume(self.get_classification_model_name())

    def resume(self, filename):
        snapshot = self.model.load(filename)
        if torch.cuda.is_available():
            self.model = self.model.cuda()
        self.epoch = snapshot["epoch"]
        self.optimizer.load_state_dict(snapshot["optimizer_state_dict"])
        self.logger.resume(snapshot["logged_data"])

    def snapshot(self, filename):
        self.model.save(
        filename,
        optimizer_state_dict=self.optimizer.state_dict(),
        epoch=self.epoch,
        logged_data=self.logger.get_data())

    def loss_criterion(self, inputs, targets, epoch, earliness_factor, entropy_factor):
        """a wrapper around several possible loss functions for experiments"""
        if epoch is None:
            return self.model.loss_cross_entropy(inputs, targets)

        ## try to optimize for earliness only when classification is correct
        if self.lossmode=="early_reward":
            return self.model.early_loss(inputs,targets,earliness_factor)

        elif self.lossmode=="loss_cross_entropy":
            return self.model.loss_cross_entropy(inputs,targets)

        # first cross entropy then early reward loss
        elif self.lossmode == "twophase_early_reward":
            if self.get_phase() == CLASSIFICATION_PHASE_NAME:
                return self.model.loss_cross_entropy(inputs, targets)
            elif self.get_phase() == EARLINESS_PHASE_NAME:
                return self.model.early_loss_simple(inputs, targets, alpha=earliness_factor)

        # first cross-entropy loss then linear classification loss and simple t/T regularization
        elif self.lossmode=="twophase_linear_loss":
            if self.get_phase() == CLASSIFICATION_PHASE_NAME:
                return self.model.loss_cross_entropy(inputs, targets)
            elif self.get_phase() == EARLINESS_PHASE_NAME:
                return self.model.early_loss_linear(inputs, targets, alpha=earliness_factor, entropy_factor=entropy_factor)

        # first cross entropy on all dates, then cross entropy plus simple t/T regularization
        elif self.lossmode == "twophase_cross_entropy":
            if self.get_phase() == CLASSIFICATION_PHASE_NAME:
                return self.model.loss_cross_entropy(inputs, targets)
            elif self.get_phase() == EARLINESS_PHASE_NAME:
                return self.model.early_loss_cross_entropy(inputs, targets, alpha=earliness_factor, entropy_factor=entropy_factor)

        else:
            raise ValueError("wrong loss_mode please choose either 'early_reward',  "
                             "'twophase_early_reward', 'twophase_linear_loss', or 'twophase_cross_entropy'")

    def fit(self):
        printer = Printer()

        while self.epoch < self.epochs:
            self.new_epoch() # increments self.epoch

            self.logger.set_mode("train")
            stats = self.train_epoch(self.epoch)
            printer.print(stats, self.epoch, prefix="\ntrain: ")

            if self.epoch % self.test_every_n_epochs == 0:
                self.logger.set_mode("test")
                stats = self.test_epoch(self.epoch)
                self.logger.log(stats, self.epoch)
                printer.print(stats, self.epoch, prefix="\nvalid: ")

            self.visdom.confusion_matrix(stats["confusion_matrix"])

            legend = ["class {}".format(c) for c in range(self.nclasses)]

            targets = stats["targets"]

            # either user-specified value or all available values
            n_samples = self.show_n_samples if self.show_n_samples < targets.shape[0] else targets.shape[0]

            for i in range(n_samples):
                classid = targets[i, 0]

                if len(stats["probas"].shape)==3:
                    self.visdom.plot(stats["probas"][:, i, :], name="sample {} P(y) (class={})".format(i, classid), fillarea=True,
                                 showlegend=True, legend=legend)
                self.visdom.plot(stats["inputs"][i, :, 0], name="sample {} x (class={})".format(i, classid))
                self.visdom.bar(stats["weights"][i, :], name="sample {} P(t) (class={})".format(i, classid))

            self.visdom.plot_epochs(self.logger.get_data())

        self.check_events()
        return self.logger.data

    def new_epoch(self):
        self.check_events()
        self.epoch += 1

    def get_phase(self):
        if self.epoch < self.switch_epoch:
            return CLASSIFICATION_PHASE_NAME
        else:
            return EARLINESS_PHASE_NAME

    def check_events(self):
        if self.epoch == 0:
            self.starting_phase_classification_event()
        elif self.epoch == self.switch_epoch:
            self.ending_phase_classification_event()
            self.starting_phase_earliness_event()
        elif self.epoch == self.epochs:
            self.ending_phase_earliness_event()

    def get_classification_model_name(self):
        return os.path.join(self.store, "model_{}.pth".format(CLASSIFICATION_PHASE_NAME))

    def get_earliness_model_name(self):
        return os.path.join(self.store, "model_{}.pth".format(EARLINESS_PHASE_NAME))

    def starting_phase_classification_event(self):
        print("starting training phase classification")

    def ending_phase_classification_event(self):
        print("ending training phase classification")
        self.snapshot(self.get_classification_model_name())

    def starting_phase_earliness_event(self):
        print("starting training phase earliness")

    def ending_phase_earliness_event(self):
        print("ending training phase earliness")
        self.snapshot(self.get_earliness_model_name())

    def train_epoch(self, epoch):
        # sets the model to train mode: dropout is applied
        self.model.train()

        # builds a confusion matrix
        metric = ClassMetric(num_classes=self.nclasses)

        for iteration, data in enumerate(self.traindataloader):
            self.optimizer.zero_grad()

            inputs, targets = data

            if torch.cuda.is_available():
                inputs = inputs.cuda()
                targets = targets.cuda()

            loss, logprobabilities, weights, stats = self.loss_criterion(inputs, targets, epoch, self.earliness_factor, self.entropy_factor)

            prediction = self.model.predict(logprobabilities, weights)

            loss.backward()
            self.optimizer.step()

            stats = metric.add(stats)
            stats["accuracy"] = metric.update_confmat(targets.mode(1)[0].detach().cpu().numpy(), prediction.detach().cpu().numpy())

        return stats

    def test_epoch(self, epoch):
        # sets the model to train mode: no dropout is applied
        self.model.eval()

        # builds a confusion matrix
        #metric_maxvoted = ClassMetric(num_classes=self.nclasses)
        metric = ClassMetric(num_classes=self.nclasses)
        #metric_all_t = ClassMetric(num_classes=self.nclasses)

        with torch.no_grad():
            for iteration, data in enumerate(self.validdataloader):

                inputs, targets = data

                if torch.cuda.is_available():
                    inputs = inputs.cuda()
                    targets = targets.cuda()

                loss, logprobabilities, weights, stats = self.loss_criterion(inputs, targets, epoch, self.earliness_factor, self.entropy_factor)

                prediction = self.model.predict(logprobabilities, weights)

                stats = metric.add(stats)
                stats["accuracy"] = metric.update_confmat(targets.mode(1)[0].detach().cpu().numpy(),
                                                          prediction.detach().cpu().numpy())

        stats["confusion_matrix"] = metric.hist
        stats["targets"] = targets.cpu().numpy()
        stats["inputs"] = inputs.cpu().numpy()
        stats["weights"] = weights.cpu().numpy()

        probas = logprobabilities.exp().transpose(0, 1)
        stats["probas"] = probas.cpu().numpy()

        return stats
