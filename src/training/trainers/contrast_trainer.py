from src.training.trainers import base
from torch.utils.data import dataset
from src.training.callbacks import (
    checkpoints,
    devices,
    early_stopping,
    logistics
)
import numpy
from src.training.datasets import datasets
import pathlib
from src.training import exceptions
import os
import random
from tqdm import tqdm
import typing
from torch import nn
from torch.utils import data
from src.training.contrastive_learning import sampler
import torch
from torch import optim
from torch.optim import lr_scheduler
from torch import device


class ContrastiveTrainer(base.BaseTrainer):
    """
    Training pipeline for contrastive learning
    of multiple embedding generation networks:

        - S3D-backed video embedding encoder
        - DistilBERT-backed word embedding encoder
        - DistilBERT-backed audio embedding encoder

    Parameters:
    -----------
        networks: list of embedding generation networks for each modality.
        optimizers: list of optimizers for each embedding generator.
        lr_schedulers: list of LR schedulers for each embedding generator.
        contrastive_sampler: sampler for hard mining sample pairs.
        batch_size: int - size of the data batch, feed to networks at each iteration
        distributed: bool - enable distributed training.
    """
    def __init__(self,
        networks: typing.List[nn.Module],
        optimizers: typing.List[nn.Module],
        contrastive_sampler: sampler.BaseSampler,
        batch_size: int,
        loss_name: str,
        eval_metric_name: str,
        lr_schedulers: typing.List = [],
        distributed: bool = False,
        reproducible: bool = False
    ):
        super(ContrastiveTrainer, self).__init__()
        self.networks = networks
        self.optimizers = optimizers
        self.schedulers = lr_schedulers
        self.contrastive_sampler = contrastive_sampler
        self.batch_size = batch_size
        self.distributed = distributed 
        self.reproducible = reproducible
        self.loss_function = self.load_loss(loss_name)
        self.eval_metric = self.load_metric(eval_metric_name)
        self.stop = False # status code to urgently stop training
    
    def configure_optimizer(self, network: nn.Module, optimizer_config: typing.Dict):

        name = optimizer_config.get("name")
        learning_rate = optimizer_config.get("learning_rate")
        weight_decay = optimizer_config.get("weight_decay", None)
        use_nesterov = optimizer_config.get("nesterov", False)

        if name.lower() == 'adam':
            return optim.Adam(
                params=network.parameters(),
                lr=learning_rate,
                weight_decay=weight_decay
            )

        if name.lower() == 'adamax':
            return optim.Adamax(
                params=network.parameters(),
                lr=learning_rate,
                weight_decay=weight_decay
            )

        if name.lower() == 'adadelta':
            return optim.Adadelta()
        
        if name.lower() == 'rmsprop':
            return optim.RMSprop(
                params=network.parameters(),
                lr=learning_rate,
                weight_decay=weight_decay
            )
        
        if name.lower() == 'sgd':
            return optim.SGD(
                params=network.parameters(),
                weight_decay=weight_decay,
                learning_rate=learning_rate,
                nesterov=use_nesterov
            )
        else:
            raise NotImplemented()

    def configure_loader(self, 
        dataset: data.Dataset, 
        num_workers: int,
        batch_size: int, 
        distributed: bool = False,
        num_replicas: int = 1
    ):
        if distributed:
            return data.DataLoader(
                dataset=dataset,
                batch_size=batch_size,
                pin_memory=True,
                shuffle=False,
                num_workers=num_workers,
                sampler=data.DistributedSampler(
                    dataset=dataset, 
                    num_replicas=num_replicas
                ),
            )
        else:
            return data.DataLoader(
                dataset=dataset,
                batch_size=batch_size,
                shuffle=False,
                num_workers=num_workers
            )

    def configure_lr_scheduler(self, optimizer: nn.Module, lr_scheduler_config: typing.Dict):
        """
        Supports:
            'poly', 'step', 'multistep', 'exp';
        """
        name = lr_scheduler_config.get("name")
        verbose = lr_scheduler_config.get("verbose", False)
        gamma = lr_scheduler_config.get("gamma")
        total_iters = lr_scheduler_config.get("total_iters")

        if name == 'poly':
            return lr_scheduler.PolynomialLR(
                optimizer=optimizer,
                total_iters=total_iters,
                power=gamma,
                verbose=verbose
            )
        if name == 'step':
            step_size = lr_scheduler_config.get("step_size")
            return lr_scheduler.StepLR(
                optimizer=optimizer,
                step_size=step_size,
                gamma=gamma,
                verbose=verbose
            )

        if name == 'multistep':
            steps = lr_scheduler_config.get("steps")
            return lr_scheduler.MultiStepLR(
                optimizer=optimizer,
                milestones=steps,
                gamma=gamma,
                verbose=verbose
            )

        if name == 'exp':
            return lr_scheduler.ExponentialLR(
                optimizer=optimizer,
                gamma=gamma,
                verbose=verbose
            )

    def configure_device(self, device_name: str):
        return device(device_name)

    def configure_callbacks(self, base_log_dir: typing.Union[str, pathlib.Path]):

        report_log_dir = os.path.join(base_log_dir, "reports")
        cpu_log_dir = os.path.join(base_log_dir, "cpu")
        gpu_log_dir = os.path.join(base_log_dir, "gpu")

        snapshot_log_dir = os.path.join(base_log_dir, "snapshots")
        snapshot_ext = self.snapshot_config.get("snapshot_ext")
        save_every = self.snapshot_config.get("save_every")

        min_diff = self.early_stopping_config.get("min_diff")
        patience = self.early_stopping_config.get("patience")
        validation_dataset = self.early_stopping_config.get("validation_dataset")
        
        self.callbacks = [
            logistics.LogisticsCallback(log_dir=report_log_dir),
            devices.CPUInferenceCallback(log_dir=cpu_log_dir),
            devices.GPUInferenceCallback(log_dir=gpu_log_dir),
            checkpoints.SnapshotCallback(
                snapshot_ext=snapshot_ext, 
                save_every=save_every, 
                log_dir=snapshot_log_dir
            ),
            early_stopping.EarlyStoppingCallback(
                min_diff=min_diff,
                patience=patience,
                validation_dataset=validation_dataset
            )
        ]


    def configure_seed(self, input_seed: int):
        """
        Set network behaviour to be deterministic,
        including data loading, etc.
        Warning:
            do not use this method during training,
            it's main purpose lies in ability
            to provide an option for debugging tasks
            and may dramatically slow down training speed.
        """
        torch.manual_seed(seed=input_seed)
        random.seed(a=input_seed)
        torch.backends.cudnn.deterministic = True
        torch.backends.cudnn.benchmark = False

    def configure_loader(self, dataset: dataset.Dataset):

        if not self.distributed:
            return data.DataLoader(
                dataset=dataset,
                batch_size=self.batch_size,
                num_workers=self.num_workers,
                shuffle=True,
            )
        else:
            return data.DataLoader(
                dataset=dataset,
                batch_size=self.batch_size,
                num_workers=self.num_workers,
                pin_memory=True,
            )
    
    def predict_embs(self, 
        data_sample: typing.Tuple[
            torch.Tensor, 
            torch.Tensor, 
            torch.Tensor
        ]) -> torch.Tensor:
        embs = []
        for idx, modality in enumerate(data_sample):
            pred_emb = self.networks[idx].forward(modality)
            embs.append(pred_emb)
        return embs

    def train(self, train_dataset: dataset.Dataset):

        self.on_init_start()

        global_step = 0
        curr_loss = float('inf')

        self.network.train()
        loader = self.configure_loader(train_dataset)
        self.on_init_end()

        for epoch in range(self.max_epochs):
            self.on_train_batch_start()

            for videos, texts, audios, labels in tqdm(
                    loader, 
                    desc='epoch: %s; curr_loss: %s;' % (
            epoch, curr_loss)):
                
                samples = list(zip(videos, texts, audios))

                # finding hard pairs of (pos_sample, sample, neg_sample) for
                # contrastive learning training, using current batch
                hard_pairs = self.contrastive_sampler.hard_mining(
                    batch_data=samples, 
                    batch_labels=labels
                )
                
                for pos_pair, pair, neg_pair in hard_pairs:

                    pos_pair_v_emb, pos_pair_t_emb, pos_pair_a_emb = self.predict_embs(pos_pair)
                    pair_v_emb, pair_t_emb, pair_a_emb = self.predict_embs(pair)
                    neg_pair_v_emb, neg_pair_t_emb, neg_pair_a_emb = self.predict_embs(neg_pair)

                    video_loss = self.loss_function(pos_pair_v_emb, pair_v_emb, neg_pair_v_emb)
                    audio_loss = self.loss_function(pos_pair_a_emb, pair_a_emb, neg_pair_a_emb)
                    text_loss = self.loss_function(pos_pair_t_emb, pair_t_emb, neg_pair_t_emb)

                    for idx, loss in enumerate([video_loss, audio_loss, text_loss]):

                        loss.backward()
                        self.optimizers[idx].step()

                        if len(self.lr_schedulers) > 0:
                            self.lr_schedulers[idx].step()
                        # emptying the gradients, so they does not overlap
                        # with next ones, when training multiple networks
                        # on the same device.
                        self.optimizers[idx].zero_grad()

            # we pass argument 'trainer' to this event
            # in case early stopping callback want to say us, that training is done.
            # It will update flag 'stop' to True
            self.on_train_batch_end(trainer=self)
            
            # global step is simply used to track current epoch.
            self.on_validation_start(global_step=global_step)
            self.on_validation_end(global_step=global_step)
            self.on_train_epoch_end(global_step=global_step)

            if self.stop: break

        return curr_loss

    def evaluate(self, validation_dataset: dataset.Dataset):
        
        with torch.no_grad():
            loader = self.configure_loader(validation_dataset)

            video_metrics = []
            text_metrics = []
            audio_metrics = []

            for videos, texts, audios, _ in loader:

                sample = list(zip(videos, texts, audios))
                hard_pairs = self.contrastive_sampler.hard_mining(sample)
                
                for pos_sample, sample, neg_sample in hard_pairs:

                    pos_emb = self.predict_embs(pos_sample)
                    pred_emb = self.predict_embs(sample)
                    neg_emb = self.predict_embs(neg_sample)
                    
                    video_metric = self.eval_metric(
                        pred_emb[0], pos_emb[0], neg_emb[0])

                    audio_metric = self.eval_metric(
                        pred_emb[1], pos_emb[1], neg_emb[1])

                    text_metric = self.eval_metric(
                        pred_emb[2], pos_emb[2], neg_emb[2]
                    )
                video_metrics.append(video_metric)
                text_metrics.append(text_metrics)
                audio_metrics.append(audio_metrics)
    
            return (
                numpy.mean(video_metrics), 
                numpy.mean(text_metrics), 
                numpy.mean(audio_metrics)
            ) 

    def sliced_evaluate(self, validation_dataset: datasets.ContrastiveDataset):

        with torch.no_grad():
            metrics = {}
            labels = numpy.unique(validation_dataset.labels)

            video_embs_predictions = []
            text_embs_predictions = []
            audio_embs_predictions = []

            for label in labels:

                data_slice = [sample for sample in validation_dataset if sample[-1] == label]

                for video, text, audio, label in data_slice:
                    pred_v_emb, pred_t_emb, pred_a_emb = self.predict_embs(
                        data_sample=(video, text, audio)
                    )
                    video_embs_predictions.append(pred_v_emb)
                    text_embs_predictions.append(pred_t_emb)
                    audio_embs_predictions.append(pred_a_emb)

            # comparing vectors of each modalities together 

            video_embs_predictions = torch.as_tensor(video_embs_predictions)
            text_embs_predictions = torch.as_tensor(text_embs_predictions)
            audio_embs_predictions = torch.as_tensor(audio_embs_predictions)

            video_embs_predictions.requires_grad = False
            text_embs_predictions.requires_grad = False 
            audio_embs_predictions.requires_grad = False

            video_sim_metric = self.eval_metric(
                video_embs_predictions.unsqueeze(1), 
                video_embs_predictions, 
                dim=2
            )
            text_sim_metric = self.eval_metric(
                text_embs_predictions, 
                text_embs_predictions, 
                dim=2
            )
            audio_sim_metric = self.eval_metric(
                audio_embs_predictions, 
                audio_embs_predictions,
                dim=2
            )
            metrics[label] = (
                video_sim_metric, 
                text_sim_metric,
                audio_sim_metric
            )
        return metrics

