import os
import numpy as np
import yaml
import shutil
from tqdm import tqdm
from datetime import datetime
import torch
from torch.utils.data import DataLoader
from torch.utils.tensorboard import SummaryWriter

from datasets import MultiHeadDataset, ConcatMultiHeadDataset, SingleDataset
import data_transforms as transforms
from models import CerberusSegmentationModelMultiHead, CerberusSegmentationModelSingle
from utils import (
    mIoU,
    MinNormSolver,
    AverageMeter,
    AFFORDANCE_PALETTE,
    NYU40_PALETTE,
    save_image,
    save_colorful_image,
)


class CerberusMultiHeadTrain:
    def __init__(self, yaml_path):
        config = yaml.safe_load(open(yaml_path, "r"))

        self.mode = config["mode"]
        self.save_dir = os.path.join(
            config["save_dir"], self.mode, datetime.now().strftime("%Y%m%d_%H%M%S")
        )
        os.makedirs(self.save_dir, exist_ok=True)

        self.task_root_list = config["task"].keys()
        self.task_list = [v["category"] for v in config["task"].values()]

        self.model = CerberusSegmentationModelMultiHead(config["task"])

        if self.mode == "train":
            self.epochs = config["epochs"]

            self.writer = SummaryWriter(log_dir=os.path.join(self.save_dir, "log"))

            self.model = self.model.cuda()
            self.criterion = torch.nn.CrossEntropyLoss(ignore_index=255).cuda()

            train_tf = []

            if config["random_rotate"] > 0:
                train_tf.append(
                    transforms.RandomRotateMultiHead(config["random_rotate"])
                )
            if config["random_scale"] > 0:
                train_tf.append(transforms.RandomScaleMultiHead(config["random_scale"]))

            train_tf.extend(
                [
                    transforms.RandomCropMultiHead(config["random_crop"]),
                    transforms.RandomHorizontalFlipMultiHead(),
                    transforms.ToTensorMultiHead(),
                    transforms.Normalize(
                        mean=config["data_mean"], std=config["data_std"]
                    ),
                ]
            )

            train_tf = transforms.Compose(train_tf)

            dataset_at_train = MultiHeadDataset(
                config["data_dir"], "train_attribute", train_tf
            )
            dataset_af_train = MultiHeadDataset(
                config["data_dir"], "train_affordance", train_tf
            )
            dataset_seg_train = MultiHeadDataset(config["data_dir"], "train", train_tf)

            self.train_loader = DataLoader(
                ConcatMultiHeadDataset(
                    dataset_at_train, dataset_af_train, dataset_seg_train
                ),
                batch_size=config["batch_size"],
                shuffle=True,
                num_workers=config["workers"],
                pin_memory=True,
                drop_last=True,
            )

            val_tf = transforms.Compose(
                [
                    transforms.RandomCropMultiHead(config["random_crop"]),
                    transforms.ToTensorMultiHead(),
                    transforms.Normalize(
                        mean=config["data_mean"], std=config["data_std"]
                    ),
                ]
            )

            dataset_at_val = MultiHeadDataset(
                config["data_dir"], "val_attribute", val_tf
            )
            dataset_af_val = MultiHeadDataset(
                config["data_dir"], "val_affordance", val_tf
            )
            dataset_seg_val = MultiHeadDataset(config["data_dir"], "val", val_tf)

            self.val_loader = DataLoader(
                ConcatMultiHeadDataset(dataset_at_val, dataset_af_val, dataset_seg_val),
                batch_size=1,
                shuffle=False,
                num_workers=config["workers"],
                pin_memory=True,
                drop_last=True,
            )

            self.optimizer = torch.optim.SGD(
                [
                    {"params": self.model.pretrained.parameters()},
                    {"params": self.model.scratch.parameters()},
                ],
                lr=config["lr"],
                momentum=config["momentum"],
                weight_decay=config["weight_decay"],
            )

            if config["lr_mode"] == "step":
                lambda_func = lambda e: 0.1 ** (e // config["step"])

            elif config["lr_mode"] == "poly":
                lambda_func = lambda e: (1 - e / self.epochs) ** 0.9
            else:
                raise ValueError(f'Unknown lr mode {config["lr_mode"]}')

            self.scheduler = torch.optim.lr_scheduler.LambdaLR(
                self.optimizer, lr_lambda=lambda_func
            )

            torch.backends.cudnn.benchmark = True

        elif self.mode == "test":
            self.ms_scales = config["ms_scales"]
            self.save_vis = config["save_vis"]
            self.have_gt = config["have_gt"]

            test_tf = transforms.Compose(
                [
                    transforms.ToTensorMultiHead(),
                    transforms.Normalize(
                        mean=config["data_mean"], std=config["data_std"]
                    ),
                ]
            )

            dataset_at_test = MultiHeadDataset(
                config["data_dir"],
                "val_attribute",
                test_tf,
                ms_scale=self.ms_scales,
                out_name=True,
            )
            dataset_af_test = MultiHeadDataset(
                config["data_dir"],
                "val_affordance",
                test_tf,
                ms_scale=self.ms_scales,
                out_name=True,
            )
            dataset_seg_test = MultiHeadDataset(
                config["data_dir"],
                "val",
                test_tf,
                ms_scale=self.ms_scales,
                out_name=True,
            )

            self.test_loader = DataLoader(
                ConcatMultiHeadDataset(
                    dataset_at_test, dataset_af_test, dataset_seg_test
                ),
                batch_size=config["batch_size"],
                shuffle=False,
                num_workers=config["workers"],
            )
        else:
            raise ValueError(f"Unknown exec mode {self.mode}")

        self.best_score = 0
        self.start_epoch = 0

        if os.path.isfile(config["resume"]):
            checkpoint = torch.load(config["resume"])
            self.start_epoch = checkpoint["epoch"]
            self.best_score = checkpoint["best_score"]
            self.model.load_state_dict(
                {
                    k.replace("module.", ""): v
                    for k, v in checkpoint["state_dict"].items()
                }
            )
            print(
                f"Loaded checkpoint:"
                f"Epoch: {checkpoint['epoch']} Score: {checkpoint['best_score']}"
            )
        else:
            print(f"No checkpoint found")

    def exec(self):
        if self.mode == "train":
            for epoch in range(self.start_epoch, self.epochs):
                self.train(epoch)
                score = self.validate(epoch)
                self.scheduler.step()

                is_best = score > self.best_score
                self.best_score = max(score, self.best_score)
                state = {
                    "epoch": epoch + 1,
                    "state_dict": self.model.state_dict(),
                    "best_score": self.best_score,
                }
                save_dir = os.path.join(self.save_dir, "model")
                self.save_checkpoint(state, is_best, save_dir)
        elif self.mode == "test":
            save_dir = os.path.join(self.save_dir, "test_pred_img")
            self.test(save_dir, save_vis=self.save_vis, have_gt=self.have_gt)
        else:
            raise ValueError(f"Unknown exec mode {self.mode}")

    def train(self, epoch):
        self.model.train()

        loss_list = []
        loss_per_task_list = []
        score_list = []

        for i in range(len(self.task_root_list)):
            loss_list.append(AverageMeter())
            loss_per_task_list.append(
                [AverageMeter() for _ in range(len(self.task_list[i]))]
            )
            score_list.append(AverageMeter())

        for task_data_pair in tqdm(
            self.train_loader, desc=f"[Train] Epoch {epoch+1:04d}", ncols=80
        ):
            grads = [[] for _ in range(len(self.task_root_list))]
            for task_i, (input, target) in enumerate(task_data_pair):
                input = input.cuda()
                for i in range(len(target)):
                    target[i] = target[i].cuda()
                output = self.model(input, task_i)

                loss = []
                for idx in range(len(output)):
                    loss_single = self.criterion(output[idx], target[idx])
                    loss.append(loss_single)
                    loss_per_task_list[task_i][idx].update(
                        loss_single.item(), input.shape[0]
                    )
                loss = sum(loss)

                self.optimizer.zero_grad(set_to_none=True)

                loss.backward()
                loss_list[task_i].update(loss.item(), input.shape[0])

                for cnt in self.model.pretrained.parameters():
                    if cnt.grad is not None:
                        grads[task_i].append(cnt.grad.data.clone())
                grads[task_i].append(
                    self.model.scratch.layer1_rn.weight.grad.data.clone()
                )
                grads[task_i].append(
                    self.model.scratch.layer2_rn.weight.grad.data.clone()
                )
                grads[task_i].append(
                    self.model.scratch.layer3_rn.weight.grad.data.clone()
                )
                grads[task_i].append(
                    self.model.scratch.layer4_rn.weight.grad.data.clone()
                )

                score = []
                if task_i < 2:
                    for idx in range(len(output)):
                        ious = mIoU(output[idx], target[idx])
                        score.append(ious[1])
                elif task_i == 2:
                    for idx in range(len(output)):
                        ious = mIoU(output[idx], target[idx])
                        score.append(np.nanmean(ious))
                else:
                    raise ValueError(f"Not support task_i: {task_i}")

                score_list[task_i].update(np.nanmean(score), input.shape[0])

            sol, _ = MinNormSolver.find_min_norm_element(grads)

            task_loss = []
            for task_i, (input, target) in enumerate(task_data_pair):
                input = input.cuda()
                for i in range(len(target)):
                    target[i] = target[i].cuda()
                output = self.model(input, task_i)

                loss = []
                for idx in range(len(output)):
                    loss.append(self.criterion(output[idx], target[idx]))

                loss = sum(loss)
                task_loss.append(loss)
            loss = [sol[i] * task_loss[i] for i in range(len(self.task_root_list))]
            loss = sum(loss)

            self.optimizer.zero_grad()

            loss.backward()

            self.optimizer.step()

        for i, it in enumerate(self.task_root_list):
            self.writer.add_scalar(
                f"train_epoch_{it}_loss_avg", loss_list[i].avg, global_step=epoch
            )
            self.writer.add_scalar(
                f"train_epoch_{it}_score_avg", score_list[i].avg, global_step=epoch
            )
            for j, it in enumerate(self.task_list[i]):
                self.writer.add_scalar(
                    f"train_epoch_task_{it}_loss_avg",
                    loss_per_task_list[i][j].avg,
                    global_step=epoch,
                )

    @torch.no_grad()
    def validate(self, epoch):
        self.model.eval()

        loss_list = []
        loss_per_task_list = []
        score_list = []

        for i in range(len(self.task_root_list)):
            loss_list.append(AverageMeter())
            loss_per_task_list.append(
                [AverageMeter() for _ in range(len(self.task_list[i]))]
            )
            score_list.append(AverageMeter())

        for task_data_pair in tqdm(
            self.val_loader, desc=f"[Val] Epoch {epoch+1:04d}", ncols=80
        ):
            for task_i, (input, target) in enumerate(task_data_pair):
                input = input.cuda()
                for i in range(len(target)):
                    target[i] = target[i].cuda()

                output = self.model(input, task_i)

                loss = []
                for idx in range(len(output)):
                    loss_single = self.criterion(output[idx], target[idx])
                    loss.append(loss_single)
                    loss_per_task_list[task_i][idx].update(
                        loss_single.item(), input.shape[0]
                    )
                loss = sum(loss)
                loss_list[task_i].update(loss.item(), input.shape[0])

                score = []
                if task_i < 2:
                    for idx in range(len(output)):
                        ious = mIoU(output[idx], target[idx])
                        score.append(ious[1])
                elif task_i == 2:
                    for idx in range(len(output)):
                        ious = mIoU(output[idx], target[idx])
                        score.append(np.nanmean(ious))
                else:
                    raise ValueError(f"Not support task_i: {task_i}")

                score_list[task_i].update(np.nanmean(score), input.shape[0])

        for i, it in enumerate(self.task_root_list):
            self.writer.add_scalar(
                f"val_epoch_{it}_loss_avg", loss_list[i].avg, global_step=epoch
            )
            self.writer.add_scalar(
                f"val_epoch_{it}_score_avg", score_list[i].avg, global_step=epoch
            )
            for j, it in enumerate(self.task_list[i]):
                self.writer.add_scalar(
                    f"val_epoch_task_{it}_loss_avg",
                    loss_per_task_list[i][j].avg,
                    global_step=epoch,
                )

        score = np.nanmean([score_list[i].avg for i in range(len(self.task_root_list))])
        self.writer.add_scalar("val_score_avg", score, global_step=epoch)

        return score

    @torch.no_grad()
    def test(self, save_dir, save_vis, have_gt):
        self.model.eval()

        for task_data_pair in self.test_loader:
            for task_i, data in enumerate(task_data_pair):
                if task_i < 2:
                    PALETTE = AFFORDANCE_PALETTE
                elif task_i == 2:
                    PALETTE = NYU40_PALETTE
                else:
                    raise ValueError(f"Not support task_i: {task_i}")

                name = data[-1][0]
                print(f"File name: {name}")
                name = os.path.basename(name)

                base_input = data[0]
                ms_input = data[-2]

                if have_gt:
                    target = data[1]

                ms_output = []

                for input in ms_input:
                    output = self.model(input, task_i)
                    for idx in range(len(output)):
                        output[idx] = torch.nn.functional.interpolate(
                            output[idx],
                            base_input.shape[2:],
                            mode="bilinear",
                            align_corners=True,
                        ).numpy()
                    ms_output.append(output)

                ms_output = torch.as_tensor(np.array(ms_output))
                output = ms_output.sum(dim=0)

                if save_vis:
                    assert ".png" in name or ".jpg" in name or ".bmp" in name
                    for idx in range(len(output)):
                        file_name = (
                            f"{task_i}/{self.task_list[task_i][idx]}/{name[:-4]}.png"
                        )
                        pred = output[idx].argmax(dim=1)
                        save_image(pred, file_name, save_dir)
                        save_colorful_image(
                            pred, file_name, f"{save_dir}_color", PALETTE
                        )

                        if task_i == 2:
                            gt_name = f"{task_i}/{self.task_list[task_i][idx]}/{name[:-4]}_gt.png"
                            label_mask = target[idx] == 255
                            save_colorful_image(
                                target[idx] - label_mask * 255,
                                gt_name,
                                save_dir + "_color",
                                PALETTE,
                            )

                if have_gt:
                    score = []
                    if task_i < 2:
                        for idx in range(len(output)):
                            ious = mIoU(output[idx], target[idx])
                            score.append(ious[1])
                    elif task_i == 2:
                        for idx in range(len(output)):
                            ious = mIoU(output[idx], target[idx])
                            score.append(np.nanmean(ious))
                    else:
                        raise ValueError(f"Not support task_i: {task_i}")

                    score = np.nanmean(score)
                    print(f"Task {task_i} score: {score:.2f}")

    def save_checkpoint(self, state, is_best, save_dir, backup_freq=10):
        os.makedirs(save_dir, exist_ok=True)

        checkpoint_path = os.path.join(save_dir, "checkpoint_latest.pth")
        torch.save(state, checkpoint_path)

        if is_best:
            best_path = os.path.join(save_dir, "model_best.pth")
            shutil.copyfile(checkpoint_path, best_path)

        if state["epoch"] % backup_freq == 0:
            history_path = os.path.join(
                save_dir, f"checkpoint_{state['epoch']:04d}.pth"
            )
            shutil.copyfile(checkpoint_path, history_path)


class CerberusSingleTrain:
    def __init__(self, yaml_path):
        config = yaml.safe_load(open(yaml_path, "r"))

        self.mode = config["mode"]
        self.save_dir = os.path.join(
            config["save_dir"], self.mode, datetime.now().strftime("%Y%m%d_%H%M%S")
        )
        os.makedirs(self.save_dir, exist_ok=True)

        self.task_root_list = list(config["task"].keys())
        assert len(self.task_root_list) == 1
        self.task_list = [v["category"] for v in config["task"].values()]

        self.model = CerberusSegmentationModelSingle(config["task"])

        if self.mode == "train":
            self.epochs = config["epochs"]

            self.writer = SummaryWriter(log_dir=os.path.join(self.save_dir, "log"))

            self.model = self.model.cuda()
            self.criterion = torch.nn.CrossEntropyLoss(ignore_index=255).cuda()

            train_tf = []

            if config["random_rotate"] > 0:
                train_tf.append(
                    transforms.RandomRotateMultiHead(config["random_rotate"])
                )
            if config["random_scale"] > 0:
                train_tf.append(transforms.RandomScaleMultiHead(config["random_scale"]))

            train_tf.extend(
                [
                    transforms.RandomCropMultiHead(config["random_crop"]),
                    transforms.RandomHorizontalFlipMultiHead(),
                    transforms.ToTensorMultiHead(),
                    transforms.Normalize(
                        mean=config["data_mean"], std=config["data_std"]
                    ),
                ]
            )

            train_tf = transforms.Compose(train_tf)

            dataset_train = SingleDataset(
                config["data_dir"], f"train_{self.task_root_list[0]}", train_tf
            )

            self.train_loader = DataLoader(
                dataset_train,
                batch_size=config["batch_size"],
                shuffle=True,
                num_workers=config["workers"],
                pin_memory=True,
                drop_last=True,
            )

            val_tf = transforms.Compose(
                [
                    transforms.RandomCropMultiHead(config["random_crop"]),
                    transforms.ToTensorMultiHead(),
                    transforms.Normalize(
                        mean=config["data_mean"], std=config["data_std"]
                    ),
                ]
            )

            dataset_val = SingleDataset(
                config["data_dir"], f"val_{self.task_root_list[0]}", val_tf
            )

            self.val_loader = DataLoader(
                dataset_val,
                batch_size=1,
                shuffle=False,
                num_workers=config["workers"],
                pin_memory=True,
                drop_last=True,
            )

            self.optimizer = torch.optim.SGD(
                [
                    {"params": self.model.pretrained.parameters()},
                    {"params": self.model.scratch.parameters()},
                ],
                lr=config["lr"],
                momentum=config["momentum"],
                weight_decay=config["weight_decay"],
            )

            if config["lr_mode"] == "step":
                lambda_func = lambda e: 0.1 ** (e // config["step"])

            elif config["lr_mode"] == "poly":
                lambda_func = lambda e: (1 - e / self.epochs) ** 0.9
            else:
                raise ValueError(f'Unknown lr mode {config["lr_mode"]}')

            self.scheduler = torch.optim.lr_scheduler.LambdaLR(
                self.optimizer, lr_lambda=lambda_func
            )

            torch.backends.cudnn.benchmark = True

        elif self.mode == "test":
            self.save_vis = config["save_vis"]
            self.have_gt = config["have_gt"]

            test_tf = transforms.Compose(
                [
                    transforms.ToTensorMultiHead(),
                    transforms.Normalize(
                        mean=config["data_mean"], std=config["data_std"]
                    ),
                ]
            )

            dataset_test = SingleDataset(
                config["data_dir"],
                "val_affordance",
                test_tf,
                out_name=True,
            )

            self.test_loader = DataLoader(
                dataset_test,
                batch_size=config["batch_size"],
                shuffle=False,
                num_workers=config["workers"],
            )
        else:
            raise ValueError(f"Unknown exec mode {self.mode}")

        self.best_score = 0
        self.start_epoch = 0

        if os.path.isfile(config["resume"]):
            checkpoint = torch.load(config["resume"])
            self.start_epoch = checkpoint["epoch"]
            self.best_score = checkpoint["best_score"]
            self.model.load_state_dict(
                {
                    k.replace("module.", ""): v
                    for k, v in checkpoint["state_dict"].items()
                }
            )
            print(
                f"Loaded checkpoint:"
                f"Epoch: {checkpoint['epoch']} Score: {checkpoint['best_score']}"
            )
        else:
            print(f"No checkpoint found")

    def exec(self):
        if self.mode == "train":
            for epoch in range(self.start_epoch, self.epochs):
                self.train(epoch)
                score = self.validate(epoch)
                self.scheduler.step()
 
                is_best = score > self.best_score
                self.best_score = max(score, self.best_score)
                state = {
                    "epoch": epoch + 1,
                    "state_dict": self.model.state_dict(),
                    "best_score": self.best_score,
                }
                save_dir = os.path.join(self.save_dir, "model")
                self.save_checkpoint(state, is_best, save_dir)
        elif self.mode == "test":
            save_dir = os.path.join(self.save_dir, "test_pred_img")
            self.test(save_dir, save_vis=self.save_vis, have_gt=self.have_gt)
        else:
            raise ValueError(f"Unknown exec mode {self.mode}")

    def train(self, epoch):
        self.model.train()

        loss_list = AverageMeter()
        loss_per_task_list = [AverageMeter() for _ in range(len(self.task_list[0]))]
        score_list = AverageMeter()

        for input, target in tqdm(
            self.train_loader, desc=f"[Train] Epoch {epoch+1:04d}", ncols=80
        ):
            input = input.cuda()
            for i in range(len(target)):
                target[i] = target[i].cuda()
            output = self.model(input)

            score = []
            for idx in range(len(output)):
                ious = mIoU(output[idx], target[idx])
                ious = np.nanmean(ious)
                score.append(ious)
            score_list.update(np.nanmean(score), input.shape[0])

            loss = []
            for idx in range(len(output)):
                loss_single = self.criterion(output[idx], target[idx])
                loss.append(self.criterion(output[idx], target[idx]))
                loss_per_task_list[idx].update(
                    loss_single.item(), input.shape[0]
                )
            loss = sum(loss)
            loss_list.update(loss.item(), input.shape[0])

            self.optimizer.zero_grad()
            loss.backward()
            self.optimizer.step()

        self.writer.add_scalar(
            f"train_epoch_{self.task_root_list[0]}_loss_avg", loss_list.avg, global_step=epoch
        )
        self.writer.add_scalar(
            f"train_epoch_{self.task_root_list[0]}_score_avg", score_list.avg, global_step=epoch
        )
        for i, it in enumerate(self.task_list[0]):
            self.writer.add_scalar(
                f"train_epoch_task_{it}_loss_avg",
                loss_per_task_list[i].avg,
                global_step=epoch,
            )

    @torch.no_grad()
    def validate(self, epoch):
        self.model.eval()

        loss_list = AverageMeter()
        # loss_per_task_list = [AverageMeter() for _ in range(len(self.task_list[0]))]
        score_list = AverageMeter()
        score_per_task_list = [AverageMeter() for _ in range(len(self.task_list[0]))]

        for input, target in tqdm(
            self.val_loader, desc=f"[Val] Epoch {epoch+1:04d}", ncols=80
        ):
            input = input.cuda()
            for i in range(len(target)):
                target[i] = target[i].cuda()
            output = self.model(input)

            loss = []
            for idx in range(len(output)):
                loss_single = self.criterion(output[idx], target[idx])
                loss.append(loss_single)
                # loss_per_task_list[idx].update(
                #     loss_single.item(), input.shape[0]
                # )
            loss = sum(loss)
            loss_list.update(loss.item(), input.shape[0])

            score = []
            for idx in range(len(output)):
                ious = mIoU(output[idx], target[idx])
                ious = np.nanmean(ious)
                score.append(ious)
                score_per_task_list[idx].update(ious, input.shape[0])
            score_list.update(np.nanmean(score), input.shape[0])

        self.writer.add_scalar(
            f"val_epoch_{self.task_root_list[0]}_loss_avg", loss_list.avg, global_step=epoch
        )
        self.writer.add_scalar(
            f"val_epoch_{self.task_root_list[0]}_score_avg", score_list.avg, global_step=epoch
        )
        for i, it in enumerate(self.task_list[0]):
            # self.writer.add_scalar(
            #     f"val_epoch_task_{it}_loss_avg",
            #     loss_per_task_list[i].avg,
            #     global_step=epoch,
            # )
            self.writer.add_scalar(
                f"val_epoch_task_{it}_score_avg",
                score_per_task_list[i].avg,
                global_step=epoch,
            )

        score = score_list.avg
        self.writer.add_scalar("val_score_avg", score, global_step=epoch)

        return score

    @torch.no_grad()
    def test(self, save_dir, save_vis, have_gt):
        self.model.eval()

        for data_i, data in enumerate(self.test_loader):
            PALETTE = AFFORDANCE_PALETTE

            name = data[-1][0]
            print(f"File name: {name}")
            name = os.path.basename(name)

            input = data[0]
            if have_gt:
                target = data[1]

            output = self.model(input)

            if save_vis:
                assert ".png" in name or ".jpg" in name or ".bmp" in name
                for idx in range(len(output)):
                    file_name = (
                        f"{data_i}/{self.task_list[0][idx]}/{name[:-4]}.png"
                    )
                    pred = output[idx].argmax(dim=1)
                    # save_image(pred, file_name, save_dir)
                    save_colorful_image(
                        pred, file_name, f"{save_dir}_color", PALETTE
                    )

            if have_gt:
                for idx in range(len(target)):
                    file_name = (
                        f"{data_i}/{self.task_list[0][idx]}/{name[:-4]}_gt.png"
                    )
                    pred = target[idx].argmax(dim=0)
                    # save_image(pred, file_name, save_dir)
                    save_colorful_image(
                        pred, file_name, f"{save_dir}_color", PALETTE
                    )
                score = []
                for idx in range(len(output)):
                    ious = mIoU(output[idx], target[idx])
                    score.append(ious[1])
                score = np.nanmean(score)
                print(f"Task {self.task_root_list[0]} score: {score:.2f}")

    def save_checkpoint(self, state, is_best, save_dir, backup_freq=10):
        os.makedirs(save_dir, exist_ok=True)

        checkpoint_path = os.path.join(save_dir, "checkpoint_latest.pth")
        torch.save(state, checkpoint_path)

        if is_best:
            best_path = os.path.join(save_dir, "model_best.pth")
            shutil.copyfile(checkpoint_path, best_path)

        if state["epoch"] % backup_freq == 0:
            history_path = os.path.join(
                save_dir, f"checkpoint_{state['epoch']:04d}.pth"
            )
            shutil.copyfile(checkpoint_path, history_path)


if __name__ == "__main__":
    # os.environ["CUDA_VISIBLE_DEVICES"] = "1"

    # cerberus = CerberusMultiHeadTrain("train_multihead_nyud2.yaml")
    # cerberus.exec()

    cerberus = CerberusSingleTrain("train_weak_cad120.yaml")
    cerberus.exec()
    # cerberus = CerberusSingleTrain("test_weak_cad120.yaml")
    # cerberus.exec()
    
    # cerberus = CerberusSingleTrain("train_cad120.yaml")
    # cerberus.exec()