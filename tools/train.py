import os
import torch
import argparse
from mot.utils import config
from mot.models import build_tracker

def parse_args():
    parser = argparse.ArgumentParser(
        description='Training multiple object tracker.')
    parser.add_argument('--config', type=str, default='',
        help='training configuration file path')
    parser.add_argument('opts', default=None, nargs=argparse.REMAINDER,
        help='modify configuration in command line')
    return parser.parse_args()

def main():
    args = parse_args()
    if os.path.isfile(args.config):
        config.merge_from_file(args.config)
    config.merge_from_list(args.opts)
    config.freeze()
    print(config)
    
    model = build_tracker(config.MODEL)
    print(model)
    
    input = torch.rand(64, 3, 320, 576)
    target = torch.rand(1000, 7)
    target[:, 0] = torch.randint(0, 64, (1000,))     # Image index
    target[:, 1] = 0                        # Class index
    target[:, 2] = torch.randint(0, 100, (1000,))    # Trajectory index
    loss, metrics = model(input, target, [320, 576])
    print('loss: {}'.format(loss))
    print('metrics:')
    for k, v in metrics.items():
        print('{}: {}'.format(k, v))
    
if __name__ == '__main__':
    main()