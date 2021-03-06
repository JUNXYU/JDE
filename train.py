# -*- coding: utf-8 -*-
# file: train.py
# brief: JDE implementation based on PyTorch
# author: Zeng Zhiwei
# date: 2020/4/20

import os
import torch
import random
import argparse
import numpy as np
import torch.utils.data
from progressbar import *
import multiprocessing as mp
from functools import partial
from collections import defaultdict

import utils
import darknet
import dataset as ds
import shufflenetv2

def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('--in-size', type=int, default=[416,416],
        nargs='+', help='network input size (width, height)')
    parser.add_argument('--num-classes', type=int, default=1,
        help='number of classes')
    parser.add_argument('--resume', help='resume training',
        action='store_true')
    parser.add_argument('--checkpoint', type=str, default='',
        help='checkpoint model file')
    parser.add_argument('--anchors', type=str,
        help='path to the anchor parameters file')
    parser.add_argument('--batch-size', type=int, default=8,
        help='training batch size')
    parser.add_argument('--accumulated-batches', type=int, default=1,
        help='update weights every accumulated batches')
    parser.add_argument('--scale-step', type=int, default=[320,160,2,576,288],
        nargs='+', help='scale step for multi-scale training')
    parser.add_argument('--rescale-freq', type=int, default=80,
        help='image rescaling frequency')
    parser.add_argument('--epochs', type=int, default=50,
        help='number of total epochs to run')
    parser.add_argument('--warmup', type=int, default=1000,
        help='warmup iterations')
    parser.add_argument('--workers', type=int, default=4,
        help='number of data loading workers')
    parser.add_argument('--optim', type=str, default='sgd',
        help='optimization algorithms, adam or sgd')
    parser.add_argument('--lr', type=float, default=0.0001,
        help='initial learning rate')
    parser.add_argument('--milestones', type=int, default=[-1,-1],
        nargs='+', help='list of batch indices, must be in increasing order')
    parser.add_argument('--lr-gamma', type=float, default=0.1,
        help='factor of decrease learning rate')
    parser.add_argument('--momentum', type=float, default=0.9,
        help='momentum')
    parser.add_argument('--weight-decay', type=float, default=0.0005,
        help='weight decay')
    parser.add_argument('--savename', type=str, default='jde',
        help='filename of trained model')
    parser.add_argument('--eval-epoch', type=int, default=10,
        help='epoch beginning evaluate')
    parser.add_argument('--sparsity', help='enable sparsity training',
        action='store_true')
    parser.add_argument('--lamb', type=float, default=0.01,
        help='sparsity factor')
    parser.add_argument('--pin', help='use pin_memory',
        action='store_true')
    parser.add_argument('--workspace', type=str, default='workspace',
        help='workspace path')
    parser.add_argument('--print-interval', type=int, default=40,
        help='log printing interval [40]')
    parser.add_argument('--seed', type=int, default=0,
        help='seed number')
    parser.add_argument('--freeze-bn', help='freeze batch norm',
        action='store_true')
    parser.add_argument('--backbone', type=str, default='darknet',
        help='backbone architecture, shufflenetv2 (default) or darknet')
    parser.add_argument('--thin', type=str, default='2.0x',
        help='shufflenetv2 thin, it can be 0.5x (default), 1.0x, 1.5x, or 2.0x')
    parser.add_argument('--dataset-root', type=str,
        help='dataset root directory')
    parser.add_argument('--lr-coeff', type=float, default=[1,1,50],
        nargs='+', help='lr coeff [1,1,50] for backbone, detection, and identity')
    parser.add_argument('--box-loss', type=str, default='smoothl1loss',
        help='box regression loss, it can be smoothl1loss (default) or diouloss')
    parser.add_argument('--cls-loss', type=str, default='crossentropyloss',
        help='object classification loss, crossentropyloss or softmaxfocalloss')
    args = parser.parse_args()
    return args

def init_seeds(seed=0):
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)

def grouping_model_params(model):
    detection_branch_keynames = [
        'shbk6',  'conv7',
        'shbk11', 'conv12',
        'shbk16', 'conv17']

    identity_branch_keynames = [
        'shbk8',  'conv9',
        'shbk13', 'conv14',
        'shbk18', 'conv19']

    detection_branch_params = []
    identity_branch_params = []
    backbone_neck_params = []

    for name, param in model.named_parameters():
        if not param.requires_grad:
            continue
        keyname = name.split('.')[0]
        if keyname in detection_branch_keynames:
            detection_branch_params.append(param)
        elif keyname in identity_branch_keynames:
            identity_branch_params.append(param)
        else:
            backbone_neck_params.append(param)
    return backbone_neck_params, detection_branch_params, identity_branch_params

def train(args):    
    utils.make_workspace_dirs(args.workspace)
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    anchors = np.loadtxt(args.anchors) if args.anchors else None
    # Shared size between collate_fn and scale sampler.
    shared_size = torch.IntTensor(args.in_size).share_memory_()
    scale_sampler = utils.TrainScaleSampler(shared_size, args.scale_step,
        args.rescale_freq)
    logger = utils.get_logger(path=os.path.join(args.workspace, 'log.txt'))
    
    torch.backends.cudnn.benchmark = True
    
    dataset = ds.HotchpotchDataset(args.dataset_root, cfg='./data/train.txt',
        backbone=args.backbone, augment=True)
    collate_fn = partial(ds.collate_fn, in_size=shared_size)
    data_loader = torch.utils.data.DataLoader(dataset, args.batch_size,
        True, num_workers=args.workers, collate_fn=collate_fn,
        pin_memory=args.pin, drop_last=True)

    num_ids = int(dataset.max_id + 1)
    if args.backbone == 'darknet':
        model = darknet.DarkNet(anchors, num_classes=args.num_classes,
            num_ids=num_ids).to(device)
    elif args.backbone == 'shufflenetv2':
        model = shufflenetv2.ShuffleNetV2(anchors, num_classes=args.num_classes,
            num_ids=num_ids, model_size=args.thin, box_loss=args.box_loss,
            cls_loss=args.cls_loss).to(device)
    elif args.backbone == 'sosnet':
        model = sosmot.SOSMOT(anchors, num_classes=args.num_classes,
            num_ids=num_ids, box_loss=args.box_loss,
            cls_loss=args.cls_loss).to(device)
    else:
        print('unknown backbone architecture!')
        sys.exit(0)
    if args.checkpoint:
        model.load_state_dict(torch.load(args.checkpoint))    
    
    params = [p for p in model.parameters() if p.requires_grad]
    backbone_neck_params, detection_params, identity_params = grouping_model_params(model)
    if args.optim == 'sgd':
        # optimizer = torch.optim.SGD(params, lr=args.lr,
        #     momentum=args.momentum, weight_decay=args.weight_decay)
        optimizer = torch.optim.SGD([
            {'params': backbone_neck_params},
            {'params': detection_params, 'lr': args.lr * args.lr_coeff[1]},
            {'params': identity_params, 'lr': args.lr * args.lr_coeff[2]}
        ], lr=args.lr, momentum=args.momentum, weight_decay=args.weight_decay)
    else:
        optimizer = torch.optim.Adam(params, lr=args.lr, weight_decay=args.weight_decay)

    if args.freeze_bn:
        for name, param in model.named_parameters():
            if 'norm' in name:
                param.requires_grad = False
                logger.info('freeze {}'.format(name))
            else:
                param.requires_grad = True

    trainer = f'{args.workspace}/checkpoint/trainer-ckpt.pth'
    if args.resume:
        trainer_state = torch.load(trainer)
        optimizer.load_state_dict(trainer_state['optimizer'])

    if -1 in args.milestones:
        args.milestones = [int(args.epochs * 0.5), int(args.epochs * 0.75)]
    lr_scheduler = torch.optim.lr_scheduler.MultiStepLR(optimizer,
        milestones=args.milestones, gamma=args.lr_gamma)
    
    start_epoch = 0
    if args.resume:
        start_epoch = trainer_state['epoch'] + 1
        lr_scheduler.load_state_dict(trainer_state['lr_scheduler'])

    logger.info(args)
    logger.info('Start training from epoch {}'.format(start_epoch))
    model_path = f'{args.workspace}/checkpoint/{args.savename}-ckpt-%03d.pth'
    
    for epoch in range(start_epoch, args.epochs):
        model.train()
        logger.info(('%8s%10s%10s' + '%10s' * 8) % (
            'Epoch', 'Batch', 'SIZE', 'LBOX', 'LCLS', 'LIDE', 'LOSS', 'SBOX', 'SCLS', 'SIDE', 'LR'))

        rmetrics = defaultdict(float)
        optimizer.zero_grad()
        for batch, (images, targets) in enumerate(data_loader):
            warmup = min(args.warmup, len(data_loader))
            if epoch == 0 and batch <= warmup:
                lr = args.lr * (batch / warmup) ** 4
                for i, g in enumerate(optimizer.param_groups):
                    g['lr'] = lr * args.lr_coeff[i]
        
            loss, metrics = model(images.to(device), targets.to(device), images.shape[2:])
            loss.backward()
            
            if args.sparsity:
                model.correct_bn_grad(args.lamb)
            
            num_batches = epoch * len(data_loader) + batch + 1
            if ((batch + 1) % args.accumulated_batches == 0) or (batch == len(data_loader) - 1):
                optimizer.step()
                optimizer.zero_grad()

            for k, v in metrics.items():
                rmetrics[k] = (rmetrics[k] * batch + metrics[k]) / (batch + 1)
            
            fmt = tuple([('%g/%g') % (epoch, args.epochs), ('%g/%g') % (batch,
                len(data_loader)), ('%gx%g') % (shared_size[0].item(), shared_size[1].item())] + \
                list(rmetrics.values()) + [optimizer.param_groups[0]['lr']])
            if batch % args.print_interval == 0:
                logger.info(('%8s%10s%10s' + '%10.3g' * (len(rmetrics.values()) + 1)) % fmt)

            scale_sampler(num_batches)
      
        torch.save(model.state_dict(), f"{model_path}" % epoch)
        torch.save({'epoch' : epoch,
            'optimizer' : optimizer.state_dict(),
            'lr_scheduler' : lr_scheduler.state_dict()}, trainer)
        
        if epoch >= args.eval_epoch:
            pass
        lr_scheduler.step()

if __name__ == '__main__':
    args = parse_args()
    init_seeds(args.seed)
    train(args)