#  Copyright (c) 2017-2020 Wenyi Tang.
#  Author: Wenyi Tang
#  Email: wenyitang@outlook.com
#  Update: 2020 - 8 - 5

import logging

import numpy as np
import torch
import torch.nn.functional as F
from torch import nn

from .Model import SuperResolution
from .Ops.Blocks import EasyConv2d
from .Ops.Scale import SpaceToDepth
from ..Framework.Summary import get_writer
from ..Util import Metrics
from ..Util.Utility import upsample

_logger = logging.getLogger("VSR.RLSP")
_logger.info("LICENSE: RLSP is proposed by D. Fuoli, et. al. "
             "implemented by LoSeall. "
             "@loseall https://github.com/loseall/VideoSuperResolution")


class RLSPCell(nn.Module):
  def __init__(self, in_channels, out_channels, hidden_channels, layers):
    super(RLSPCell, self).__init__()
    cell = [EasyConv2d(in_channels, hidden_channels, 3, activation='relu')]
    for i in range(1, layers - 1):
      cell.append(
          EasyConv2d(hidden_channels, hidden_channels, 3, activation='relu'))
    self.cell = nn.Sequential(*cell)
    self.hidden = EasyConv2d(hidden_channels, hidden_channels, 3,
                             activation='relu')
    self.exit = EasyConv2d(hidden_channels, out_channels, 3)

  def forward(self, lr_frames, feedback, hidden_state):
    lr = torch.cat(lr_frames, dim=1)
    inputs = torch.cat((lr, hidden_state, feedback), dim=1)
    x = self.cell(inputs)
    next_hidden_state = self.hidden(x)
    residual = self.exit(x)
    return residual, next_hidden_state


class RlspNet(nn.Module):
  def __init__(self, scale, channel, depth=3, layers=7, filters=64):
    super(RlspNet, self).__init__()
    in_channels = channel * depth + filters + channel * scale ** 2
    self.rlspcell = RLSPCell(in_channels, channel * scale ** 2, filters, layers)
    self.shuffle = SpaceToDepth(scale)
    self.f = filters
    self.d = depth
    self.s = scale

  def forward(self, lr, sr, hidden):
    if hidden is None:
      shape = list(lr[0].shape)
      shape[1] = self.f
      hidden = torch.zeros(*shape, device=lr[0].device)
    center = F.interpolate(lr[self.d // 2], scale_factor=self.s)
    feedback = self.shuffle(sr).detach()
    res, next = self.rlspcell(lr, feedback, hidden)
    out = center + F.pixel_shuffle(res, self.s)
    return out, next


class RLSP(SuperResolution):
  """
  Args:
    clips: how many adjacent LR frames to use
    layers: number of convolution layers in RLSP cell
    filters: number of convolution filters

  Note:
    `depth` represents total sequences to train and evaluate
  """

  def __init__(self, scale, channel, clips=3, layers=7, filters=64, **kwargs):
    super(RLSP, self).__init__(scale=scale, channel=channel)
    self.rlsp = RlspNet(scale, channel, clips, layers, filters)
    self.adam = torch.optim.Adam(self.trainable_variables(), 1e-4)
    self.clips = clips

  def train(self, inputs, labels, learning_rate=None):
    frames = [x.squeeze(1) for x in inputs[0].split(1, dim=1)]
    labels = [x.squeeze(1) for x in labels[0].split(1, dim=1)]
    if learning_rate:
      for param_group in self.adam.param_groups:
        param_group["lr"] = learning_rate
    total_loss = 0
    image_loss = 0
    last_hidden = None
    last_sr = upsample(frames[0], self.scale)
    last_sr = torch.zeros_like(last_sr)
    for i in range(self.clips // 2, len(frames) - self.clips // 2):
      lr_group = [frames[i - self.clips // 2 + j] for j in range(self.clips)]
      sr, hidden = self.rlsp(lr_group, last_sr, last_hidden)
      last_hidden = hidden
      last_sr = sr.detach()
      l2_image = F.mse_loss(sr, labels[i])
      loss = l2_image
      total_loss += loss
      image_loss += l2_image.detach()
    self.adam.zero_grad()
    total_loss.backward()
    self.adam.step()
    return {
      'total_loss': total_loss.detach().cpu().numpy() / len(frames),
      'image_loss': image_loss.cpu().numpy() / len(frames),
    }

  def eval(self, inputs, labels=None, **kwargs):
    metrics = {}
    frames = [x.squeeze(1) for x in inputs[0].split(1, dim=1)]
    if labels is not None:
      labels = [x.squeeze(1) for x in labels[0].split(1, dim=1)]
    psnr = []
    predicts = []
    last_sr = upsample(frames[0], self.scale)
    last_sr = torch.zeros_like(last_sr)
    last_hidden = None
    i = 0
    for i in range(self.clips // 2, len(frames) - self.clips // 2):
      lr_group = [frames[i - self.clips // 2 + j] for j in range(self.clips)]
      sr, hidden = self.rlsp(lr_group, last_sr, last_hidden)
      last_sr = sr.detach()
      predicts.append(sr.cpu().detach().numpy())
      if labels is not None:
        psnr.append(Metrics.psnr(sr, labels[i]))
    metrics['psnr'] = np.mean(psnr)
    writer = get_writer(self.name)
    if writer is not None:
      step = kwargs['epoch']
      writer.image('clean', last_sr.clamp(0, 1), step=step)
      writer.image('label', labels[i], step=step)
    return predicts, metrics