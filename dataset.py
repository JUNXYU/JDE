import os
import cv2
import glob
import numpy as np
from collections import OrderedDict
import torch.nn.functional as F
import sys
import torch
import utils
import transforms as T
from xxx import LoadImagesAndLabels

def letterbox_image(im, insize=(320,576,3), border=128):
    '''生成letterbox图像
    
    Args:
        im (ndarray): RGB/BGR格式图像
        insize (tuple, optional): 神经网络输入大小, insize=(height, width,
            channels)
        border (float, optional): 图像边沿填充颜色
    Returns:
        lb_im (ndarray): letterbox图像
        s (float): 图像缩放系数
        dx (int): 非填充区域的水平偏移量
        dy (int): 非填充区域的垂直偏移量
    '''
    h, w = im.shape[:2]
    s = min(insize[0] / h, insize[1] / w)
    nh = round(s * h)
    nw = round(s * w)
    dx = (insize[1] - nw) / 2
    dy = (insize[0] - nh) / 2
    left  = round(dx - 0.1)
    right = round(dx + 0.1)
    above = round(dy - 0.1)
    below = round(dy + 0.1)
    lb_im = np.full(insize, border, dtype=np.uint8)
    lb_im[above:above+nh, left:left+nw, :] = cv2.resize(im, (nw,nh), interpolation=
        cv2.INTER_AREA)
    return lb_im, s, dx, dy

class ImagesLoader(object):
    '''图像迭代器
    
    Args:
        path (str): 图像路径
        insize (tuple): 神经网络输入大小, insize=(height, width)
        formats (list of str): 需要解码的图像格式列表
    '''
    def __init__(self, path, insize, formats=['*.jpg'], backbone='shufflenetv2'):
        if os.path.isdir(path):
            self.files = []
            for format in formats:
                self.files += sorted(glob.glob(os.path.join(path, format)))
        elif os.path.isfile(path):
            self.files = [path]
        self.insize = insize
        self.count = 0
        self.backbone = backbone

    def __iter__(self):
        self.count = -1
        return self
    
    def __next__(self):
        self.count += 1
        if self.count == len(self.files):
            raise StopIteration
        path = self.files[self.count]
        im = cv2.imread(path)
        assert im is not None, 'cv2.imread{} fail'.format(path)
        lb_im, s, dx, dy = letterbox_image(im, insize=self.insize)
        if self.backbone is 'darknet':
            lb_im = lb_im[...,::-1].transpose(2, 0, 1)
            lb_im = np.ascontiguousarray(lb_im, dtype=np.float32)
            lb_im /= 255.0
        else:
            lb_im = lb_im.transpose(2, 0, 1)
            lb_im = np.ascontiguousarray(lb_im, dtype=np.float32)
        return path, im, lb_im

class VideoLoader(object):
    '''Video frame iterator.
    
    Params
    ------
    path    : The file path for video.
    insize  : The input size of neural network.
    backbone: The backbone architecture. It can be 'darknet' or 'shufflenetv2'.
    '''
    def __init__(self, path, insize, backbone='shufflenetv2'):
        if not os.path.isfile(path):
            raise FileExistsError
        self.vcap = cv2.VideoCapture(path)
        self.frames = int(self.vcap.get(cv2.CAP_PROP_FRAME_COUNT))
        self.insize = insize
        self.backbone = backbone
        self.count = 0
    
    def __iter__(self):
        self.count = -1
        return self
    
    def __next__(self):
        self.count += 1
        if self.count == len(self):
            raise StopIteration
        retval, im = self.vcap.read()
        assert im is not None, 'VideoCapture.read() fail'
        lb_im, s, dx, dy = letterbox_image(im, insize=self.insize)
        if self.backbone is 'darknet':
            lb_im = lb_im[...,::-1].transpose(2, 0, 1)
            lb_im = np.ascontiguousarray(lb_im, dtype=np.float32)
            lb_im /= 255.0
        else:
            lb_im = lb_im.transpose(2, 0, 1)
            lb_im = np.ascontiguousarray(lb_im, dtype=np.float32)
        # Keep consistency with ImagesLoader with fake path.
        path = '%06d.jpg' % self.count
        return path, im, lb_im
    
    def __len__(self):
        return self.frames

def get_transform(train, net_w=416, net_h=416):
    transforms = []
    transforms.append(T.ToTensor())
    if train == True:
        transforms.append(T.RandomSpatialJitter(jitter=0.3,net_w=net_w,net_h=net_h))
        transforms.append(T.RandomColorJitter(hue=0.1,saturation=1.5,exposure=1.5))
        transforms.append(T.RandomHorizontalFlip(prob=0.5))
    else:
        transforms.append(T.MakeLetterBoxImage(width=net_w,height=net_h))
    return T.Compose(transforms)

def collate_fn(batch, in_size=torch.IntTensor([320,576])):
    images, targets = [], []
    for i,b in enumerate(batch):
        image, target = b[0], b[1]
        target[:,0] = i
        images.append(image)
        targets.append(target)
    images = torch.stack(tensors=images, dim=0)
    images = F.interpolate(input=images, size=in_size.numpy().tolist(), mode='area')
    targets = torch.cat(tensors=targets, dim=0)
    return images, targets

class HotchpotchDataset(object):
    '''Hotchpotch dataset for Caltech, Citypersons, CUHK-SYSU, ETHZ, PRW, MOT, and so on.
    '''
    def __init__(self, root, cfg='train.txt', backbone='shufflenetv2', augment=True):
        '''Class initialization.
        
        Args:
            root     : Datasets root directory.
            cfg      : Datasets configuration file. The content of cfg file like this:
                       -----------------------------
                       | ./data/caltech.train      |
                       | ./data/citypersons.train  |
                       | ./data/cuhksysu.train     |
                       | ./data/eth.train          |
                       | ./data/mot17.train        |
                       | ./data/prw.train          |
                       -----------------------------
            backbone : Nerual network backbone architecture, 'darknet' or 'shufflenetv2'.
        '''
        
        self.root = root
        self.cfg = cfg
        self.backbone = backbone
        
        # Read dataset files from configuration file.
        self.datasets = open(self.cfg, 'r').readlines()
        self.datasets = [ds.strip() for ds in self.datasets]
        self.datasets = list(filter(lambda x: len(x) > 0, self.datasets))

        # Read image paths from dataset files.
        image_paths = OrderedDict()
        label_paths = OrderedDict()
        for ds in self.datasets:
            ds_name = os.path.basename(ds)  # With suffix
            ds_name = os.path.splitext(ds_name)[0]
            with open(ds, 'r') as file:
                image_paths[ds_name] = file.readlines()
                image_paths[ds_name] = [path.strip() for path in image_paths[ds_name]]
                image_paths[ds_name] = [os.path.join(root, path) for path in image_paths[ds_name]]
                image_paths[ds_name] = list(filter(lambda x: len(x) > 0, image_paths[ds_name]))
            # Inference label paths from image paths
            label_paths[ds_name] = []
            for path in image_paths[ds_name]:
                label_path = path.replace('images', 'labels_with_ids')
                label_path = label_path.replace('.png', '.txt')
                label_path = label_path.replace('.jpg', '.txt')
                label_paths[ds_name].append(label_path)
        self.image_paths = image_paths
        self.label_paths = label_paths
        
        # Count the number of training samples for each dataset.
        self.num_ims = [len(paths) for paths in image_paths.values()]
        # Accumulate total number of training samples by each dataset.
        self.acc_ims = [sum(self.num_ims[:i]) for i in range(len(self.num_ims))]
        self.total_ims = sum(self.num_ims)
        
        # Find the number of identifiers for each dataset.
        # The label format: class identifier centerx centery width height
        self.num_ids = OrderedDict()
        for ds_name, label_paths in self.label_paths.items():
            ds_max_id = -1
            for path in label_paths:
                labels = np.loadtxt(path)
                # Empty label file.
                if len(labels) < 1:
                    continue
                # Find the maximum identifier in current label file
                if len(labels.shape) == 2:
                    file_max_id = np.max(labels[:, 1])
                else:   # Only one label in this file.
                    file_max_id = labels[1]
                if file_max_id > ds_max_id:
                    ds_max_id = file_max_id
            # The valid identifier is begin with 0.
            self.num_ids[ds_name] = ds_max_id + 1

        # Calculate identifier shift for each dataset.
        # We will calculate global identifier based on the shift.
        last_id = 0
        self.id_shifts = OrderedDict()
        for ds_name, num_id in self.num_ids.items():
            self.id_shifts[ds_name] = last_id
            last_id += num_id
        
        self._max_id = last_id - 1
        
        if self.backbone is 'darknet':
            self.loader = LoadImagesAndLabels(augment=augment)
        else:
            self.loader = LoadImagesAndLabels(augment=augment, transforms=None)

    def __getitem__(self, index):
        # Transform global index to local index in dataset.
        lid = index
        ds_name = ''
        for i, acc_im in enumerate(self.acc_ims):
            if index >= acc_im:
                ds_name = list(self.label_paths.keys())[i]
                lid = index - acc_im
        
        if not ds_name:
            print('ERROR: index {} {}'.format(index, self.acc_ims))
        image_path = self.image_paths[ds_name][lid]
        label_path = self.label_paths[ds_name][lid]
        
        # TODO: Load and augment image and labels.
        image = None
        targets = None        
        ###################################################################
        # Temporary solution        
        image, labels, _, _ = self.loader.get_data(image_path, label_path)
        ###################################################################
        
        # Transform local identifier in dataset to global identifier.
        targets = []
        for c, i, x, y, w, h in labels:
            if i > -1:
                targets.append([0, c, i + self.id_shifts[ds_name], x, y, w, h])
            else:       # Only have bounding box annotations.
                targets.append([0, c, i, x, y, w, h])
        
        targets = torch.as_tensor(targets, dtype=torch.float32, device=torch.device('cpu'))
        if targets.size(0) == 0:
            targets = torch.FloatTensor(0, 7)        
        
        return image, targets
    
    def __len__(self):
        return self.total_ims
    
    @property
    def max_id(self):
        return self._max_id

if __name__ == '__main__':
    dataset = HotchpotchDataset('/data/tseng/dataset/jde', './data/train.txt')
    acc_ims = [0, 26738, 29238, 40444, 42500, dataset.__len__()]
    for i in range(len(acc_ims) - 1):
        image, targets = dataset.__getitem__(np.random.randint(acc_ims[i], acc_ims[i + 1]))
        print(targets)