# --------------------------------------------------------
# Tensorflow Faster R-CNN
# Licensed under The MIT License [see LICENSE for details]
# Written by Jiasen Lu, Jianwei Yang, based on code from Ross Girshick
# --------------------------------------------------------
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import _init_paths
import os
import sys
import numpy as np
import argparse
import pprint
import pdb
import time
import cv2
import torch
from torch.autograd import Variable
import torch.nn as nn
import torch.optim as optim
import pickle
from roi_data_layer.roidb import combined_roidb
from roi_data_layer.roibatchLoader import roibatchLoader
from model.utils.config import cfg, cfg_from_file, cfg_from_list, get_output_dir
from model.rpn.bbox_transform import clip_boxes
from model.nms.nms_wrapper import nms
from model.rpn.bbox_transform import bbox_transform_inv
from model.utils.net_utils import save_net, load_net, vis_detections
from model.faster_rcnn.vgg16 import vgg16
from model.faster_rcnn.resnet import resnet

import pdb

try:
    xrange          # Python 2
except NameError:
    xrange = range  # Python 3


def parse_args():
  """
  Parse input arguments
  """
  parser = argparse.ArgumentParser(description='Train a Fast R-CNN network')
  parser.add_argument('--dataset', dest='dataset',
                      help='training dataset',
                      default='pascal_voc', type=str)
  parser.add_argument('--cfg', dest='cfg_file',
                      help='optional config file',
                      default='cfgs/vgg16.yml', type=str)
  parser.add_argument('--net', dest='net',
                      help='vgg16, res50, res101, res152',
                      default='res101', type=str)
  parser.add_argument('--set', dest='set_cfgs',
                      help='set config keys', default=None,
                      nargs=argparse.REMAINDER)
  parser.add_argument('--load_dir', dest='load_dir',
                      help='directory to load models', default="/srv/share/jyang375/models",
                      type=str)
  parser.add_argument('--cuda', dest='cuda',
                      help='whether use CUDA',
                      action='store_true')
  parser.add_argument('--ls', dest='large_scale',
                      help='whether use large imag scale',
                      action='store_true')
  parser.add_argument('--mGPUs', dest='mGPUs',
                      help='whether use multiple GPUs',
                      action='store_true')
  parser.add_argument('--cag', dest='class_agnostic',
                      help='whether perform class_agnostic bbox regression',
                      action='store_true')
  parser.add_argument('--parallel_type', dest='parallel_type',
                      help='which part of model to parallel, 0: all, 1: model before roi pooling',
                      default=0, type=int)
  parser.add_argument('--checksession', dest='checksession',
                      help='checksession to load model',
                      default=1, type=int)
  parser.add_argument('--checkepoch', dest='checkepoch',
                      help='checkepoch to load network',
                      default=1, type=int)
  parser.add_argument('--checkpoint', dest='checkpoint',
                      help='checkpoint to load network',
                      default=10021, type=int)
  parser.add_argument('--vis', dest='vis',
                      help='visualization mode',
                      action='store_true')
  args = parser.parse_args()
  return args

# =========== 处理完参数 ============

# 3个超参数: 学习率、动量、权重衰减
lr = cfg.TRAIN.LEARNING_RATE
momentum = cfg.TRAIN.MOMENTUM
weight_decay = cfg.TRAIN.WEIGHT_DECAY

# =========== main: ============

if __name__ == '__main__':

  args = parse_args() # 处理参数

  print('Called with args:')
  print(args)

  if torch.cuda.is_available() and not args.cuda:
    print("WARNING: You have a CUDA device, so you should probably run with --cuda")

  np.random.seed(cfg.RNG_SEED) # ??
  # 决定使用的数据集
  if args.dataset == "pascal_voc":
	  args.imdb_name = "voc_2007_trainval"
	  args.imdbval_name = "voc_2007_test"
	  args.set_cfgs = ['ANCHOR_SCALES', '[8, 16, 32]', 'ANCHOR_RATIOS', '[0.5,1,2]']
  elif args.dataset == "pascal_voc_0712":
      args.imdb_name = "voc_2007_trainval+voc_2012_trainval"
      args.imdbval_name = "voc_2007_test"
      args.set_cfgs = ['ANCHOR_SCALES', '[8, 16, 32]', 'ANCHOR_RATIOS', '[0.5,1,2]']
  elif args.dataset == "coco":
      args.imdb_name = "coco_2014_train+coco_2014_valminusminival"
      args.imdbval_name = "coco_2014_minival"
      args.set_cfgs = ['ANCHOR_SCALES', '[4, 8, 16, 32]', 'ANCHOR_RATIOS', '[0.5,1,2]']
  elif args.dataset == "imagenet":
      args.imdb_name = "imagenet_train"
      args.imdbval_name = "imagenet_val"
      args.set_cfgs = ['ANCHOR_SCALES', '[8, 16, 32]', 'ANCHOR_RATIOS', '[0.5,1,2]']
  elif args.dataset == "vg":
      args.imdb_name = "vg_150-50-50_minitrain"
      args.imdbval_name = "vg_150-50-50_minival"
      args.set_cfgs = ['ANCHOR_SCALES', '[4, 8, 16, 32]', 'ANCHOR_RATIOS', '[0.5,1,2]']

  # .yml: yet another markup language 其中的冒号表示键值对
  args.cfg_file = "cfgs/{}_ls.yml".format(args.net) if args.large_scale else "cfgs/{}.yml".format(args.net)

  if args.cfg_file is not None:
    cfg_from_file(args.cfg_file)
  if args.set_cfgs is not None:
    cfg_from_list(args.set_cfgs)

  print('Using config:') # 输出配置情况
  pprint.pprint(cfg)

  # 使用图像翻转来增加训练集 这里不使用
  cfg.TRAIN.USE_FLIPPED = False
  # ratio_list, ratio_index: 计算roi-database中所有roi的宽高比并确定序号
  # === roidb由imdbval_name决定
  # combine_roidb(给定一个数据集名)函数: lib/roi_data_layer/roidb.py
  imdb, roidb, ratio_list, ratio_index = combined_roidb(args.imdbval_name, False)
  imdb.competition_mode(on=True) # ??

  print('{:d} roidb entries'.format(len(roidb)))

  input_dir = args.load_dir + "/" + args.net + "/" + args.dataset
  if not os.path.exists(input_dir):
    raise Exception('There is no input directory for loading network from ' + input_dir)
  # 确定保存了之前没训练完数据的文件
  load_name = os.path.join(input_dir,
    'faster_rcnn_{}_{}_{}.pth'.format(args.checksession, args.checkepoch, args.checkpoint))

  # initialize the network here.
  # 确定faster-rcnn使用的网络模型
  if args.net == 'vgg16':
    fasterRCNN = vgg16(imdb.classes, pretrained=False, class_agnostic=args.class_agnostic)
  elif args.net == 'res101':
    fasterRCNN = resnet(imdb.classes, 101, pretrained=False, class_agnostic=args.class_agnostic)
  elif args.net == 'res50':
    fasterRCNN = resnet(imdb.classes, 50, pretrained=False, class_agnostic=args.class_agnostic)
  elif args.net == 'res152':
    fasterRCNN = resnet(imdb.classes, 152, pretrained=False, class_agnostic=args.class_agnostic)
  else:
    print("network is not defined")
    pdb.set_trace()

  fasterRCNN.create_architecture()  # 建立整个模型

  print("load checkpoint %s" % (load_name))
  # 加载之前的配置情况:
  checkpoint = torch.load(load_name)
  fasterRCNN.load_state_dict(checkpoint['model'])
  if 'pooling_mode' in checkpoint.keys():
    cfg.POOLING_MODE = checkpoint['pooling_mode']

  print('load model successfully!')
  # initilize the tensor holder here.
  # 下面4个变量分别表示:
  # 图片 图片的尺寸 box数量 ground-truth的box
  im_data = torch.FloatTensor(1)
  im_info = torch.FloatTensor(1)
  num_boxes = torch.LongTensor(1)
  gt_boxes = torch.FloatTensor(1)

  # ship to cuda
  if args.cuda:
    im_data = im_data.cuda()
    im_info = im_info.cuda()
    num_boxes = num_boxes.cuda()
    gt_boxes = gt_boxes.cuda()

  # make variable
  # 读取到数据后将数据从Tensor转换成Variable格式
  im_data = Variable(im_data, volatile=True)
  im_info = Variable(im_info, volatile=True)
  num_boxes = Variable(num_boxes, volatile=True)
  gt_boxes = Variable(gt_boxes, volatile=True)

  if args.cuda:
    cfg.CUDA = True
  if args.cuda:
    fasterRCNN.cuda()

  start = time.time()

  max_per_image = 100 # ??
  vis = args.vis # ??

  if vis:
    thresh = 0.05
  else:
    thresh = 0.0

  save_name = 'faster_rcnn_10'
  num_images = len(imdb.image_index)
  # 每个类有若干image 每image有若干box
  # xrange: 和range相同 但返回一个生成器
  all_boxes = [ [[] for _ in xrange(num_images)] for _ in xrange(imdb.num_classes) ]

  output_dir = get_output_dir(imdb, save_name)
  # 固定的两步: 加载测试数据集
  # 构造函数 见lib/roi_data_layer/roibatchLoader.py
  # === 后面的data[0]-data[4]的内容由roidb决定
  dataset = roibatchLoader(roidb, ratio_list, ratio_index, 1, \
                        imdb.num_classes, training=False, normalize=False)
  dataloader = torch.utils.data.DataLoader(dataset, batch_size=1, # === 注意batch size = 1
                            shuffle=False, num_workers=0, pin_memory=True)
  # 这里没有像trainval文件里那样用sampler类
  data_iter = iter(dataloader)

  _t = {'im_detect': time.time(), 'misc': time.time()}
  det_file = os.path.join(output_dir, 'detections.pkl')

  fasterRCNN.eval() # ??
  # 输出是[]
  empty_array = np.transpose(np.array([[], [], [], [], []]), (1, 0))
  for i in range(num_images): # 对于测试集中每张图片
  """
  class-aware detector: 给一张图像 输出一个bbox的集合和相应的分类label
  class-agnostic detector: 检测出了所有bbox 但不知道属于哪个类
  only detect "foreground" objects
  it's a set that contains all classes we want to find in an image
  i.e. foreground = {cat, dog, car, airplane, ...}
  """

      # self.sample_iter = iter(self.batch_sampler)
      # 得到的self.sample_iter可以通过next(self.sample_iter)来获取batch size个数据的index
      data = next(data_iter)
      im_data.data.resize_(data[0].size()).copy_(data[0])
      im_info.data.resize_(data[1].size()).copy_(data[1])
      gt_boxes.data.resize_(data[2].size()).copy_(data[2])
      num_boxes.data.resize_(data[3].size()).copy_(data[3])

      det_tic = time.time()
      # 把4条数据输入模型之后 得到对应的7个输出
      rois, cls_prob, bbox_pred, \
      rpn_loss_cls, rpn_loss_box, \
      RCNN_loss_cls, RCNN_loss_bbox, \
      rois_label = fasterRCNN(im_data, im_info, gt_boxes, num_boxes)

      scores = cls_prob.data # 属于各个类的概率/评分
      boxes = rois.data[:, :, 1:5] # ??

      if cfg.TEST.BBOX_REG: # BBOX_REG: Train bounding-box regressors 训练bbox的回归器
          # Apply bounding-box regression deltas
          box_deltas = bbox_pred.data # 预测值
          # Optionally normalize targets by a precomputed mean and stdev ??
          # stdev: 标准差
          if cfg.TRAIN.BBOX_NORMALIZE_TARGETS_PRECOMPUTED:
          	# 只有在训练了回归器、标准差、方差的前提下才能在测试图片上标注
          	# class_agnostic: whether perform class_agnostic bbox regression
            if args.class_agnostic: # 不用标注具体类别 只识别物体
            	# 乘上标准差再加上方差 得到较准确的bbox
                box_deltas = box_deltas.view(-1, 4) * torch.FloatTensor(cfg.TRAIN.BBOX_NORMALIZE_STDS).cuda() \
                           + torch.FloatTensor(cfg.TRAIN.BBOX_NORMALIZE_MEANS).cuda()
                box_deltas = box_deltas.view(1, -1, 4)
            else: # 不仅要识别物体 还要标注类别
            	# 乘上标准差再加上方差 得到较准确的bbox
                box_deltas = box_deltas.view(-1, 4) * torch.FloatTensor(cfg.TRAIN.BBOX_NORMALIZE_STDS).cuda() \
                           + torch.FloatTensor(cfg.TRAIN.BBOX_NORMALIZE_MEANS).cuda()
                # *4的原因是bbox有4个回归系数
                # 下面的一步可能是对于一个bbox 对每个class都计算4个回归系数 取score最大的或者用mask过滤
                # 可能组成一个高度为bbox数量 宽度为4*class数量的矩阵
                box_deltas = box_deltas.view(1, -1, 4 * len(imdb.classes))

          pred_boxes = bbox_transform_inv(boxes, box_deltas, 1) # ??
          pred_boxes = clip_boxes(pred_boxes, im_info.data, 1) # 根据图片的尺寸信息(im_info)裁剪图片外的box
      else: # 没有训练回归器的情形
          # Simply repeat the boxes, once for each class
          # tile()是沿某个维度复制数组的元素 这里是对于每个类都复制一次 ??
          pred_boxes = np.tile(boxes, (1, scores.shape[1]))

      pred_boxes /= data[1][0][2]

      # 把shape中为1的维度去掉
      scores = scores.squeeze()
      pred_boxes = pred_boxes.squeeze()
      det_toc = time.time()
      detect_time = det_toc - det_tic
      misc_tic = time.time()
      if vis:
          im = cv2.imread(imdb.image_path_at(i)) # opencv读入图片
          im2show = np.copy(im) # 图片转为numpy中的数组
      for j in xrange(1, imdb.num_classes): # 遍历每个class
          inds = torch.nonzero(scores[:, j] > thresh).view(-1) # 取得分高于阈值的类的索引
          # if there is det
          if inds.numel() > 0: # numel(): 返回张量中所有元素的个数
            cls_scores = scores[:, j][inds] # 取出这些高于阈值的类的得分
            _, order = torch.sort(cls_scores, 0, True) # 把得分排序
            if args.class_agnostic: # 要在测试图片上标注每一个类
              cls_boxes = pred_boxes[inds, :] # 在预测的boxes中取出所有得分高于阈值的box的位置信息
            else: # 只识别不标注
              cls_boxes = pred_boxes[inds][:, j * 4:(j + 1) * 4] # ??
            
            cls_dets = torch.cat((cls_boxes, cls_scores.unsqueeze(1)), 1) # 把box和对应的得分组合起来
            # cls_dets = torch.cat((cls_boxes, cls_scores), 1)
            cls_dets = cls_dets[order] # 按照order排序
            keep = nms(cls_dets, cfg.TEST.NMS) # 非极大值抑制
            cls_dets = cls_dets[keep.view(-1).long()] # 经过NMS筛选
            if vis:
              im2show = vis_detections(im2show, imdb.classes[j], cls_dets.cpu().numpy(), 0.3) # ??
            all_boxes[j][i] = cls_dets.cpu().numpy()
          else: # 没有得分高于阈值的
            all_boxes[j][i] = empty_array

      # Limit to max_per_image detections *over all classes*
      # 可能是每张图片最多识别出的box的数量
      # (下面没有细看)
      if max_per_image > 0:
          image_scores = np.hstack([all_boxes[j][i][:, -1]
                                    for j in xrange(1, imdb.num_classes)])
          if len(image_scores) > max_per_image: # 如果超过了
              image_thresh = np.sort(image_scores)[-max_per_image] # 就重新定一个阈值
              for j in xrange(1, imdb.num_classes):
                  keep = np.where(all_boxes[j][i][:, -1] >= image_thresh)[0]
                  all_boxes[j][i] = all_boxes[j][i][keep, :]

      misc_toc = time.time()
      nms_time = misc_toc - misc_tic

      sys.stdout.write('im_detect: {:d}/{:d} {:.3f}s {:.3f}s   \r' \
          .format(i + 1, num_images, detect_time, nms_time))
      sys.stdout.flush()

      if vis:
          cv2.imwrite('result.png', im2show)
          pdb.set_trace() # 调试
          #cv2.imshow('test', im2show)
          #cv2.waitKey(0)

  with open(det_file, 'wb') as f:
      pickle.dump(all_boxes, f, pickle.HIGHEST_PROTOCOL) # 把检测到的box标注在图片上

  print('Evaluating detections')
  imdb.evaluate_detections(all_boxes, output_dir) # ??

  end = time.time()
  print("test time: %0.4fs" % (end - start))
