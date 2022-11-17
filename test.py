import torch
import numpy as np
from tqdm import tqdm
from utils.util import IoU, AverageMeter
from models.model import DPTAffordanceModel
import utils.transform as TF
from datasets.dataset import make_dataloader

torch.set_grad_enabled(False)

affordance = ["openable", "cuttable", "pourable", "containable", "supportable", "holdable"]
split_mode = "object"
mode = "val"
print(split_mode)
print(mode)
resume_path = "outputs/20221116_201243/first_stage/model/model_best.pth"
print(resume_path)

num_objects = 12
model = DPTAffordanceModel(num_objects, len(affordance), use_hf=True).cuda()
ckpt = torch.load(resume_path, map_location=lambda storage, loc: storage)
model.load_state_dict(
    {
        k.replace("module.", ""): v
        for k, v in ckpt["state_dict"].items()
    },
    strict=False,
)
model.eval()

print(f"Score: {ckpt['score']}")

if split_mode == "object":
    mean = [132.2723, 106.8666, 112.8962]
    std = [67.4025, 70.7446, 72.1553]
elif split_mode == "actor":
    mean = [136.5133, 108.5417, 113.0168]
    std = [67.4025, 70.7446, 72.1553]
else:
    raise Exception(f"split_mode: {split_mode}")

tf = TF.Compose(
    [
        TF.PILToTensor(),
        TF.ImageNormalizeTensor(mean=mean, std=std),
    ]
)

loader = make_dataloader(
    f"../dataset/cad120/{split_mode}",
    f"{mode}_affordance",
    tf,
    label_level=["dense"],
    batch_size=1,
    shuffle=False,
    num_workers=10,
    pin_memory=True,
    drop_last=False,
)

score_meter = AverageMeter()
score_per_class_meter = [AverageMeter() for _ in range(len(affordance))]

for data in tqdm(loader):
    input = data["image"].cuda(non_blocking=True)
    target = data["dense_label"]
    for i in range(len(affordance)):
        target[i] = target[i].cuda(non_blocking=True)
    output = model(input)
    pred = []
    for i in range(len(affordance)):
        pred.append((output[i].detach() > 0).int())

    score = []
    for i in range(len(affordance)):
        score_per_class = IoU(
            pred[i],
            target[i],
            num_class=2,
            ignore_index=255,
        )
        if not np.isnan(score_per_class):
            score_per_class_meter[i].update(score_per_class, input.shape[0])
            score.append(score_per_class)

    if len(score) > 0:
        score = np.mean(score)
        score_meter.update(score, input.shape[0])

for i, it in enumerate(score_per_class_meter):
    iou = score_per_class_meter[i].get()
    print(f"{affordance[i]}: {iou:.2f}")
print(f"mIoU: {score_meter.get():.2f}")