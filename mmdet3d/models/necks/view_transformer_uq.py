# Copyright (c) OpenMMLab. All rights reserved.
import torch
import torch.nn as nn
import torch.nn.functional as F
from mmcv.cnn import build_conv_layer
from mmcv.runner import BaseModule, force_fp32
from torch.cuda.amp.autocast_mode import autocast
from torch.utils.checkpoint import checkpoint

from mmdet3d.ops.bev_pool_v2.bev_pool import bev_pool_v2
from mmdet.models.backbones.resnet import BasicBlock
from ..builder import NECKS


@NECKS.register_module()
class LSSViewTransformerUQ(BaseModule):
    r"""Lift-Splat-Shoot view transformer with BEVPoolv2 implementation.

    Please refer to the `paper <https://arxiv.org/abs/2008.05711>`_ and
        `paper <https://arxiv.org/abs/2211.17111>`

    Args:
        grid_config (dict): Config of grid alone each axis in format of
            (lower_bound, upper_bound, interval). axis in {x,y,z,depth}.
        input_size (tuple(int)): Size of input images in format of (height,
            width).
        downsample (int): Down sample factor from the input size to the feature
            size.
        in_channels (int): Channels of input feature.
        out_channels (int): Channels of transformed feature.
        accelerate (bool): Whether the view transformation is conducted with
            acceleration. Note: the intrinsic and extrinsic of cameras should
            be constant when 'accelerate' is set true.
        sid (bool): Whether to use Spacing Increasing Discretization (SID)
            depth distribution as `STS: Surround-view Temporal Stereo for
            Multi-view 3D Detection`.
        collapse_z (bool): Whether to collapse in z direction.
    """

    def __init__(
        self,
        grid_config,
        input_size,
        downsample=16,
        in_channels=512,
        out_channels=64,
        accelerate=False,
        sid=False,
        collapse_z=True,
    ):
        super(LSSViewTransformerUQ, self).__init__()
        self.grid_config = grid_config
        self.downsample = downsample
        self.create_grid_infos(**grid_config)
        self.sid = sid
        self.frustum = self.create_frustum(grid_config['depth'],
                                           input_size, downsample)
        self.out_channels = out_channels
        self.in_channels = in_channels
        self.depth_net = nn.Conv2d(
            in_channels, self.D + self.out_channels, kernel_size=1, padding=0)
        self.accelerate = accelerate
        self.initial_flag = True
        self.collapse_z = collapse_z

    def create_grid_infos(self, x, y, z, **kwargs):
        """Generate the grid information including the lower bound, interval,
        and size.

        Args:
            x (tuple(float)): Config of grid alone x axis in format of
                (lower_bound, upper_bound, interval).
            y (tuple(float)): Config of grid alone y axis in format of
                (lower_bound, upper_bound, interval).
            z (tuple(float)): Config of grid alone z axis in format of
                (lower_bound, upper_bound, interval).
            **kwargs: Container for other potential parameters
        """
        self.grid_lower_bound = torch.Tensor([cfg[0] for cfg in [x, y, z]])
        self.grid_interval = torch.Tensor([cfg[2] for cfg in [x, y, z]])
        self.grid_size = torch.Tensor([(cfg[1] - cfg[0]) / cfg[2]
                                       for cfg in [x, y, z]])

    def create_frustum(self, depth_cfg, input_size, downsample):
        """Generate the frustum template for each image.

        Args:
            depth_cfg (tuple(float)): Config of grid alone depth axis in format
                of (lower_bound, upper_bound, interval).
            input_size (tuple(int)): Size of input images in format of (height,
                width).
            downsample (int): Down sample scale factor from the input size to
                the feature size.
        """
        H_in, W_in = input_size
        H_feat, W_feat = H_in // downsample, W_in // downsample
        d = torch.arange(*depth_cfg, dtype=torch.float)\
            .view(-1, 1, 1).expand(-1, H_feat, W_feat)
        self.D = d.shape[0]
        if self.sid:
            d_sid = torch.arange(self.D).float()
            depth_cfg_t = torch.tensor(depth_cfg).float()
            d_sid = torch.exp(torch.log(depth_cfg_t[0]) + d_sid / (self.D-1) *
                              torch.log((depth_cfg_t[1]-1) / depth_cfg_t[0]))
            d = d_sid.view(-1, 1, 1).expand(-1, H_feat, W_feat)
        x = torch.linspace(0, W_in - 1, W_feat,  dtype=torch.float)\
            .view(1, 1, W_feat).expand(self.D, H_feat, W_feat)
        y = torch.linspace(0, H_in - 1, H_feat,  dtype=torch.float)\
            .view(1, H_feat, 1).expand(self.D, H_feat, W_feat)

        # D x H x W x 3
        return torch.stack((x, y, d), -1)

    def get_lidar_coor(self, sensor2ego, ego2global, cam2imgs, post_rots, post_trans,
                       bda):
        """Calculate the locations of the frustum points in the lidar
        coordinate system.

        Args:
            rots (torch.Tensor): Rotation from camera coordinate system to
                lidar coordinate system in shape (B, N_cams, 3, 3).
            trans (torch.Tensor): Translation from camera coordinate system to
                lidar coordinate system in shape (B, N_cams, 3).
            cam2imgs (torch.Tensor): Camera intrinsic matrixes in shape
                (B, N_cams, 3, 3).
            post_rots (torch.Tensor): Rotation in camera coordinate system in
                shape (B, N_cams, 3, 3). It is derived from the image view
                augmentation.
            post_trans (torch.Tensor): Translation in camera coordinate system
                derived from image view augmentation in shape (B, N_cams, 3).

        Returns:
            torch.tensor: Point coordinates in shape
                (B, N_cams, D, ownsample, 3)
        """
        B, N, _, _ = sensor2ego.shape

        # post-transformation
        # B x N x D x H x W x 3
        points = self.frustum.to(sensor2ego) - post_trans.view(B, N, 1, 1, 1, 3)
        points = torch.inverse(post_rots).view(B, N, 1, 1, 1, 3, 3)\
            .matmul(points.unsqueeze(-1))
        # import ipdb;ipdb.set_trace()

        # cam_to_ego
        points = torch.cat(
            (points[..., :2, :] * points[..., 2:3, :], points[..., 2:3, :]), 5)
        combine = sensor2ego[:,:,:3,:3].matmul(torch.inverse(cam2imgs))
        points = combine.view(B, N, 1, 1, 1, 3, 3).matmul(points).squeeze(-1)
        points += sensor2ego[:,:,:3, 3].view(B, N, 1, 1, 1, 3)
        points = bda.view(B, 1, 1, 1, 1, 3,
                          3).matmul(points.unsqueeze(-1)).squeeze(-1)
        return points

    def init_acceleration_v2(self, coor):
        """Pre-compute the necessary information in acceleration including the
        index of points in the final feature.

        Args:
            coor (torch.tensor): Coordinate of points in lidar space in shape
                (B, N_cams, D, H, W, 3).
            x (torch.tensor): Feature of points in shape
                (B, N_cams, D, H, W, C).
        """

        ranks_bev, ranks_depth, ranks_feat, \
            interval_starts, interval_lengths = \
            self.voxel_pooling_prepare_v2(coor)

        self.ranks_bev = ranks_bev.int().contiguous()
        self.ranks_feat = ranks_feat.int().contiguous()
        self.ranks_depth = ranks_depth.int().contiguous()
        self.interval_starts = interval_starts.int().contiguous()
        self.interval_lengths = interval_lengths.int().contiguous()

    def voxel_pooling_v2(self, coor, depth, feat):
        ranks_bev, ranks_depth, ranks_feat, \
            interval_starts, interval_lengths = \
            self.voxel_pooling_prepare_v2(coor)
        if ranks_feat is None:
            print('warning ---> no points within the predefined '
                  'bev receptive field')
            dummy = torch.zeros(size=[
                feat.shape[0], feat.shape[2],
                int(self.grid_size[2]),
                int(self.grid_size[0]),
                int(self.grid_size[1])
            ]).to(feat)
            dummy = torch.cat(dummy.unbind(dim=2), 1)
            return dummy
        feat = feat.permute(0, 1, 3, 4, 2)
        bev_feat_shape = (depth.shape[0], int(self.grid_size[2]),
                          int(self.grid_size[1]), int(self.grid_size[0]),
                          feat.shape[-1])  # (B, Z, Y, X, C)
        bev_feat = bev_pool_v2(depth, feat, ranks_depth, ranks_feat, ranks_bev,
                               bev_feat_shape, interval_starts,
                               interval_lengths)
        # collapse Z
        if self.collapse_z:
            bev_feat = torch.cat(bev_feat.unbind(dim=2), 1)
        return bev_feat

    def voxel_pooling_prepare_v2(self, coor):
        """Data preparation for voxel pooling.

        Args:
            coor (torch.tensor): Coordinate of points in the lidar space in
                shape (B, N, D, H, W, 3).

        Returns:
            tuple[torch.tensor]: Rank of the voxel that a point is belong to
                in shape (N_Points); Reserved index of points in the depth
                space in shape (N_Points). Reserved index of points in the
                feature space in shape (N_Points).
        """
        B, N, D, H, W, _ = coor.shape
        num_points = B * N * D * H * W
        # record the index of selected points for acceleration purpose
        ranks_depth = torch.range(
            0, num_points - 1, dtype=torch.int, device=coor.device)
        ranks_feat = torch.range(
            0, num_points // D - 1, dtype=torch.int, device=coor.device)
        ranks_feat = ranks_feat.reshape(B, N, 1, H, W)
        ranks_feat = ranks_feat.expand(B, N, D, H, W).flatten()
        # convert coordinate into the voxel space
        coor = ((coor - self.grid_lower_bound.to(coor)) /
                self.grid_interval.to(coor))
        coor = coor.long().view(num_points, 3)
        batch_idx = torch.range(0, B - 1).reshape(B, 1). \
            expand(B, num_points // B).reshape(num_points, 1).to(coor)
        coor = torch.cat((coor, batch_idx), 1)

        # filter out points that are outside box
        kept = (coor[:, 0] >= 0) & (coor[:, 0] < self.grid_size[0]) & \
               (coor[:, 1] >= 0) & (coor[:, 1] < self.grid_size[1]) & \
               (coor[:, 2] >= 0) & (coor[:, 2] < self.grid_size[2])
        if len(kept) == 0:
            return None, None, None, None, None
        coor, ranks_depth, ranks_feat = \
            coor[kept], ranks_depth[kept], ranks_feat[kept]
        # get tensors from the same voxel next to each other
        ranks_bev = coor[:, 3] * (
            self.grid_size[2] * self.grid_size[1] * self.grid_size[0])
        ranks_bev += coor[:, 2] * (self.grid_size[1] * self.grid_size[0])
        ranks_bev += coor[:, 1] * self.grid_size[0] + coor[:, 0]
        order = ranks_bev.argsort()
        ranks_bev, ranks_depth, ranks_feat = \
            ranks_bev[order], ranks_depth[order], ranks_feat[order]

        kept = torch.ones(
            ranks_bev.shape[0], device=ranks_bev.device, dtype=torch.bool)
        kept[1:] = ranks_bev[1:] != ranks_bev[:-1]
        interval_starts = torch.where(kept)[0].int()
        if len(interval_starts) == 0:
            return None, None, None, None, None
        interval_lengths = torch.zeros_like(interval_starts)
        interval_lengths[:-1] = interval_starts[1:] - interval_starts[:-1]
        interval_lengths[-1] = ranks_bev.shape[0] - interval_starts[-1]
        return ranks_bev.int().contiguous(), ranks_depth.int().contiguous(
        ), ranks_feat.int().contiguous(), interval_starts.int().contiguous(
        ), interval_lengths.int().contiguous()

    def pre_compute(self, input):
        if self.initial_flag:
            coor = self.get_lidar_coor(*input[1:7])
            self.init_acceleration_v2(coor)
            self.initial_flag = False

    def view_transform_core(self, input, depth, tran_feat):
        B, N, C, H, W = input[0].shape
        
        # Lift-Splat
        if self.accelerate:
            feat = tran_feat.view(B, N, self.out_channels, H, W)
            feat = feat.permute(0, 1, 3, 4, 2)
            depth = depth.view(B, N, self.D, H, W)
            bev_feat_shape = (depth.shape[0], int(self.grid_size[2]),
                              int(self.grid_size[1]), int(self.grid_size[0]),
                              feat.shape[-1])  # (B, Z, Y, X, C)
            bev_feat = bev_pool_v2(depth, feat, self.ranks_depth,
                                   self.ranks_feat, self.ranks_bev,
                                   bev_feat_shape, self.interval_starts,
                                   self.interval_lengths)

            bev_feat = bev_feat.squeeze(2)
        else:
            coor = self.get_lidar_coor(*input[1:7])
            bev_feat = self.voxel_pooling_v2(
                coor, depth.view(B, N, self.D, H, W),
                tran_feat.view(B, N, self.out_channels, H, W))
        return bev_feat, depth

    def view_transform(self, input, depth, tran_feat):
        if self.accelerate:
            self.pre_compute(input)
        return self.view_transform_core(input, depth, tran_feat)

    def forward(self, input):
        """Transform image-view feature into bird-eye-view feature.

        Args:
            input (list(torch.tensor)): of (image-view feature, rots, trans,
                intrins, post_rots, post_trans)

        Returns:
            torch.tensor: Bird-eye-view feature in shape (B, C, H_BEV, W_BEV)
        """
        x = input[0]
        B, N, C, H, W = x.shape
        x = x.view(B * N, C, H, W)
        x = self.depth_net(x)

        depth_digit = x[:, :self.D, ...]
        tran_feat = x[:, self.D:self.D + self.out_channels, ...]
        depth = depth_digit.softmax(dim=1)
        # print("hkj")
        return self.view_transform(input, depth, tran_feat)

    def get_mlp_input(self, rot, tran, intrin, post_rot, post_tran, bda):
        return None


class _ASPPModule(nn.Module):

    def __init__(self, inplanes, planes, kernel_size, padding, dilation,
                 BatchNorm):
        super(_ASPPModule, self).__init__()
        self.atrous_conv = nn.Conv2d(
            inplanes,
            planes,
            kernel_size=kernel_size,
            stride=1,
            padding=padding,
            dilation=dilation,
            bias=False)
        self.bn = BatchNorm(planes)
        self.relu = nn.ReLU()

        self._init_weight()

    def forward(self, x):
        x = self.atrous_conv(x)
        x = self.bn(x)

        return self.relu(x)

    def _init_weight(self):
        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                torch.nn.init.kaiming_normal_(m.weight)
            elif isinstance(m, nn.BatchNorm2d):
                m.weight.data.fill_(1)
                m.bias.data.zero_()


class ASPP(nn.Module):

    def __init__(self, inplanes, mid_channels=256, BatchNorm=nn.BatchNorm2d):
        super(ASPP, self).__init__()

        dilations = [1, 6, 12, 18]

        self.aspp1 = _ASPPModule(
            inplanes,
            mid_channels,
            1,
            padding=0,
            dilation=dilations[0],
            BatchNorm=BatchNorm)
        self.aspp2 = _ASPPModule(
            inplanes,
            mid_channels,
            3,
            padding=dilations[1],
            dilation=dilations[1],
            BatchNorm=BatchNorm)
        self.aspp3 = _ASPPModule(
            inplanes,
            mid_channels,
            3,
            padding=dilations[2],
            dilation=dilations[2],
            BatchNorm=BatchNorm)
        self.aspp4 = _ASPPModule(
            inplanes,
            mid_channels,
            3,
            padding=dilations[3],
            dilation=dilations[3],
            BatchNorm=BatchNorm)

        self.global_avg_pool = nn.Sequential(
            nn.AdaptiveAvgPool2d((1, 1)),
            nn.Conv2d(inplanes, mid_channels, 1, stride=1, bias=False),
            BatchNorm(mid_channels),
            nn.ReLU(),
        )
        self.conv1 = nn.Conv2d(
            int(mid_channels * 5), inplanes, 1, bias=False)
        self.bn1 = BatchNorm(inplanes)
        self.relu = nn.ReLU()
        self.dropout = nn.Dropout(0.5)
        self._init_weight()

    def forward(self, x):
        x1 = self.aspp1(x)
        x2 = self.aspp2(x)
        x3 = self.aspp3(x)
        x4 = self.aspp4(x)
        x5 = self.global_avg_pool(x)
        x5 = F.interpolate(
            x5, size=x4.size()[2:], mode='bilinear', align_corners=True)
        x = torch.cat((x1, x2, x3, x4, x5), dim=1)

        x = self.conv1(x)
        x = self.bn1(x)
        x = self.relu(x)

        return self.dropout(x)

    def _init_weight(self):
        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                torch.nn.init.kaiming_normal_(m.weight)
            elif isinstance(m, nn.BatchNorm2d):
                m.weight.data.fill_(1)
                m.bias.data.zero_()


class Mlp(nn.Module):

    def __init__(self,
                 in_features,
                 hidden_features=None,
                 out_features=None,
                 act_layer=nn.ReLU,
                 drop=0.0):
        super().__init__()
        out_features = out_features or in_features
        hidden_features = hidden_features or in_features
        self.fc1 = nn.Linear(in_features, hidden_features)
        self.act = act_layer()
        self.drop1 = nn.Dropout(drop)
        self.fc2 = nn.Linear(hidden_features, out_features)
        self.drop2 = nn.Dropout(drop)

    def forward(self, x):
        x = self.fc1(x)
        x = self.act(x)
        x = self.drop1(x)
        x = self.fc2(x)
        x = self.drop2(x)
        return x


class SELayer(nn.Module):

    def __init__(self, channels, act_layer=nn.ReLU, gate_layer=nn.Sigmoid):
        super().__init__()
        self.conv_reduce = nn.Conv2d(channels, channels, 1, bias=True)
        self.act1 = act_layer()
        self.conv_expand = nn.Conv2d(channels, channels, 1, bias=True)
        self.gate = gate_layer()

    def forward(self, x, x_se):
        x_se = self.conv_reduce(x_se)
        x_se = self.act1(x_se)
        x_se = self.conv_expand(x_se)
        return x * self.gate(x_se)


class DepthNet(nn.Module):

    def __init__(self,
                 in_channels,
                 mid_channels,
                 context_channels,
                 depth_channels,
                 use_dcn=True,
                 use_aspp=True,
                 with_cp=False,
                 stereo=False,
                 bias=0.0,
                 aspp_mid_channels=-1,
                 D = 100):
        super(DepthNet, self).__init__()
        
        self.reduce_conv = nn.Sequential(
            nn.Conv2d(
                in_channels, mid_channels, kernel_size=3, stride=1, padding=1),
            nn.BatchNorm2d(mid_channels),
            nn.ReLU(inplace=True),
        )
        self.context_conv = nn.Conv2d(
            mid_channels, context_channels, kernel_size=1, stride=1, padding=0)
        self.bn = nn.BatchNorm1d(27)
        self.depth_mlp = Mlp(27, mid_channels, mid_channels)
        self.depth_se = SELayer(mid_channels)  # NOTE: add camera-aware
        self.context_mlp = Mlp(27, mid_channels, mid_channels)
        self.context_se = SELayer(mid_channels)  # NOTE: add camera-aware
        depth_conv_input_channels = mid_channels


        self.depth_adapt = nn.Conv2d(
            depth_channels, 1, kernel_size=1, stride=1, padding=0)
        self.uq_adapt = nn.Conv2d(
            depth_channels, 1, kernel_size=1, stride=1, padding=0)

        downsample = None

        if stereo:
            depth_conv_input_channels += depth_channels
            downsample = nn.Conv2d(depth_conv_input_channels,
                                    mid_channels, 1, 1, 0)
            cost_volumn_net = []
            for stage in range(int(2)):
                cost_volumn_net.extend([
                    nn.Conv2d(depth_channels, depth_channels, kernel_size=3,
                              stride=2, padding=1),
                    nn.BatchNorm2d(depth_channels)])
            self.cost_volumn_net = nn.Sequential(*cost_volumn_net)
            self.bias = bias
        depth_conv_list = [BasicBlock(depth_conv_input_channels, mid_channels,
                                      downsample=downsample),
                           BasicBlock(mid_channels, mid_channels),
                           BasicBlock(mid_channels, mid_channels)]
        if use_aspp:
            if aspp_mid_channels<0:
                aspp_mid_channels = mid_channels
            depth_conv_list.append(ASPP(mid_channels, aspp_mid_channels))
        if use_dcn:
            depth_conv_list.append(
                build_conv_layer(
                    cfg=dict(
                        type='DCN',
                        in_channels=mid_channels,
                        out_channels=mid_channels,
                        kernel_size=3,
                        padding=1,
                        groups=4,
                        im2col_step=128,
                    )))
        depth_conv_list.append(
            nn.Conv2d(
                mid_channels,
                depth_channels,
                kernel_size=1,
                stride=1,
                padding=0))
        self.depth_conv = nn.Sequential(*depth_conv_list)
        self.with_cp = with_cp
        self.depth_channels = depth_channels

        # 加入UQ分支的MLP和SE层
        self.uq_mlp = Mlp(27, mid_channels, mid_channels)
        self.uq_se = SELayer(mid_channels)
        
        # UQ预测分支
        uq_conv_list = [
            BasicBlock(mid_channels, mid_channels),
            BasicBlock(mid_channels, mid_channels),
            BasicBlock(mid_channels, mid_channels)
        ]
        if use_aspp:
            uq_conv_list.append(ASPP(mid_channels, aspp_mid_channels))
        if use_dcn:
            uq_conv_list.append(
                build_conv_layer(
                    cfg=dict(
                        type='DCN',
                        in_channels=mid_channels,
                        out_channels=mid_channels,
                        kernel_size=3,
                        padding=1,
                        groups=4,
                        im2col_step=128,
                    )))
        uq_conv_list.append(
            nn.Conv2d(
                mid_channels,
                depth_channels,  # 输出通道和depth一样
                kernel_size=1,
                stride=1,
                padding=0))
        self.uq_conv = nn.Sequential(*uq_conv_list)

    def gen_grid(self, metas, B, N, D, H, W, hi, wi):
        frustum = metas['frustum']
        points = frustum - metas['post_trans'].view(B, N, 1, 1, 1, 3)
        points = torch.inverse(metas['post_rots']).view(B, N, 1, 1, 1, 3, 3) \
            .matmul(points.unsqueeze(-1))
        points = torch.cat(
            (points[..., :2, :] * points[..., 2:3, :], points[..., 2:3, :]), 5)

        rots = metas['k2s_sensor'][:, :, :3, :3].contiguous()
        trans = metas['k2s_sensor'][:, :, :3, 3].contiguous()
        combine = rots.matmul(torch.inverse(metas['intrins']))

        points = combine.view(B, N, 1, 1, 1, 3, 3).matmul(points)
        points += trans.view(B, N, 1, 1, 1, 3, 1)
        neg_mask = points[..., 2, 0] < 1e-3
        points = metas['intrins'].view(B, N, 1, 1, 1, 3, 3).matmul(points)
        points = points[..., :2, :] / points[..., 2:3, :]

        points = metas['post_rots'][...,:2,:2].view(B, N, 1, 1, 1, 2, 2).matmul(
            points).squeeze(-1)
        points += metas['post_trans'][...,:2].view(B, N, 1, 1, 1, 2)

        px = points[..., 0] / (wi - 1.0) * 2.0 - 1.0
        py = points[..., 1] / (hi - 1.0) * 2.0 - 1.0
        px[neg_mask] = -2
        py[neg_mask] = -2
        grid = torch.stack([px, py], dim=-1)
        grid = grid.view(B * N, D * H, W, 2)
        return grid

    def calculate_cost_volumn(self, metas):
        prev, curr = metas['cv_feat_list']
        group_size = 4
        _, c, hf, wf = curr.shape
        hi, wi = hf * 4, wf * 4
        B, N, _ = metas['post_trans'].shape
        D, H, W, _ = metas['frustum'].shape
        grid = self.gen_grid(metas, B, N, D, H, W, hi, wi).to(curr.dtype)

        prev = prev.view(B * N, -1, H, W)
        curr = curr.view(B * N, -1, H, W)
        cost_volumn = 0
        # process in group wise to save memory
        for fid in range(curr.shape[1] // group_size):
            prev_curr = prev[:, fid * group_size:(fid + 1) * group_size, ...]
            wrap_prev = F.grid_sample(prev_curr, grid,
                                      align_corners=True,
                                      padding_mode='zeros')
            curr_tmp = curr[:, fid * group_size:(fid + 1) * group_size, ...]
            cost_volumn_tmp = curr_tmp.unsqueeze(2) - \
                              wrap_prev.view(B * N, -1, D, H, W)
            cost_volumn_tmp = cost_volumn_tmp.abs().sum(dim=1)
            cost_volumn += cost_volumn_tmp
        if not self.bias == 0:
            invalid = wrap_prev[:, 0, ...].view(B * N, D, H, W) == 0
            cost_volumn[invalid] = cost_volumn[invalid] + self.bias
        cost_volumn = - cost_volumn
        cost_volumn = cost_volumn.softmax(dim=1)
        return cost_volumn

    def forward(self, x, mlp_input, stereo_metas=None):
        mlp_input = self.bn(mlp_input.reshape(-1, mlp_input.shape[-1]))
        x = self.reduce_conv(x)
        context_se = self.context_mlp(mlp_input)[..., None, None]
        context = self.context_se(x, context_se)
        context = self.context_conv(context)
        depth_se = self.depth_mlp(mlp_input)[..., None, None]

        # import ipdb;ipdb.set_trace()

        depth_input = x

        depth = self.depth_se(depth_input, depth_se)      # TODO merge !!


        # UQ分支
        uq_se = self.uq_mlp(mlp_input)[..., None, None]
        uq_input = x
        uq = self.uq_se(uq_input, uq_se)



        if not stereo_metas is None:
            if stereo_metas['cv_feat_list'][0] is None:
                BN, _, H, W = x.shape
                scale_factor = float(stereo_metas['downsample'])/\
                               stereo_metas['cv_downsample']
                cost_volumn = \
                    torch.zeros((BN, self.depth_channels,
                                 int(H*scale_factor),
                                 int(W*scale_factor))).to(x)
            else:
                with torch.no_grad():
                    cost_volumn = self.calculate_cost_volumn(stereo_metas)
            cost_volumn = self.cost_volumn_net(cost_volumn)
            depth = torch.cat([depth, cost_volumn], dim=1)

       # 分别通过各自的卷积网络
        if self.with_cp and False:
            depth = checkpoint(self.depth_conv, depth)
            uq = checkpoint(self.uq_conv, uq)
        else:
            depth = self.depth_conv(depth)
            uq = self.uq_conv(uq)

        depth = self.depth_adapt(depth)
        uq = self.uq_adapt(uq)

        # 返回 depth预测 + uncertainty预测 + context特征
        return torch.cat([depth, uq, context], dim=1)
        # if self.with_cp:
        #     depth = checkpoint(self.depth_conv, depth)
        # else:
        #     depth = self.depth_conv(depth)
        # return torch.cat([depth, context], dim=1)


class DepthAggregation(nn.Module):
    """pixel cloud feature extraction."""

    def __init__(self, in_channels, mid_channels, out_channels):
        super(DepthAggregation, self).__init__()

        self.reduce_conv = nn.Sequential(
            nn.Conv2d(
                in_channels,
                mid_channels,
                kernel_size=3,
                stride=1,
                padding=1,
                bias=False),
            nn.BatchNorm2d(mid_channels),
            nn.ReLU(inplace=True),
        )

        self.conv = nn.Sequential(
            nn.Conv2d(
                mid_channels,
                mid_channels,
                kernel_size=3,
                stride=1,
                padding=1,
                bias=False),
            nn.BatchNorm2d(mid_channels),
            nn.ReLU(inplace=True),
            nn.Conv2d(
                mid_channels,
                mid_channels,
                kernel_size=3,
                stride=1,
                padding=1,
                bias=False),
            nn.BatchNorm2d(mid_channels),
            nn.ReLU(inplace=True),
        )

        self.out_conv = nn.Sequential(
            nn.Conv2d(
                mid_channels,
                out_channels,
                kernel_size=3,
                stride=1,
                padding=1,
                bias=True),
            # nn.BatchNorm3d(out_channels),
            # nn.ReLU(inplace=True),
        )

    @autocast(False)
    def forward(self, x):
        x = checkpoint(self.reduce_conv, x)
        short_cut = x
        x = checkpoint(self.conv, x)
        x = short_cut + x
        x = self.out_conv(x)
        return x


class LSSViewTransformerBEVDepthUQ(LSSViewTransformerUQ):

    def __init__(self, loss_depth_weight=3.0, depthnet_cfg=dict(), 
                **kwargs):
        super(LSSViewTransformerBEVDepthUQ, self).__init__(**kwargs)
        self.loss_depth_weight = loss_depth_weight
        self.depth_net = DepthNet(self.in_channels, self.in_channels,
                                  self.out_channels, self.D, **depthnet_cfg, 
                                )

        self.trans_feat_net=nn.Conv2d(self.out_channels*2, self.out_channels, 1, 1, 0)


        self.convnet=nn.Sequential(
            nn.Conv2d(in_channels=2, out_channels=512, kernel_size=3, padding=1),
            nn.BatchNorm2d(512),
            nn.ReLU(),
            nn.Conv2d(in_channels=512, out_channels=128, kernel_size=3, padding=1),
            nn.BatchNorm2d(128),
            nn.ReLU(),
            nn.Conv2d(in_channels=128, out_channels=32, kernel_size=3, padding=1),
            nn.BatchNorm2d(32),
            nn.ReLU(),
            nn.Conv2d(in_channels=32, out_channels=32, kernel_size=3, padding=1),
            # nn.Sigmoid()
        )   


    


    def get_mlp_input(self, sensor2ego, ego2global, intrin, post_rot, post_tran, bda):
        B, N, _, _ = sensor2ego.shape
        bda = bda.view(B, 1, 3, 3).repeat(1, N, 1, 1)
        mlp_input = torch.stack([
            intrin[:, :, 0, 0],
            intrin[:, :, 1, 1],
            intrin[:, :, 0, 2],
            intrin[:, :, 1, 2],
            post_rot[:, :, 0, 0],
            post_rot[:, :, 0, 1],
            post_tran[:, :, 0],
            post_rot[:, :, 1, 0],
            post_rot[:, :, 1, 1],
            post_tran[:, :, 1],
            bda[:, :, 0, 0],
            bda[:, :, 0, 1],
            bda[:, :, 1, 0],
            bda[:, :, 1, 1],
            bda[:, :, 2, 2],], dim=-1)
        sensor2ego = sensor2ego[:,:,:3,:].reshape(B, N, -1)
        mlp_input = torch.cat([mlp_input, sensor2ego], dim=-1)
        return mlp_input

    # def get_downsampled_gt_depth(self, gt_depths):
    #     """
    #     Input:
    #         gt_depths: [B, N, H, W]
    #     Output:
    #         gt_depths: [B*N*h*w, d]
    #     """
    #     B, N, H, W = gt_depths.shape
    #     gt_depths = gt_depths.view(B * N, H // self.downsample,
    #                                self.downsample, W // self.downsample,
    #                                self.downsample, 1)
    #     gt_depths = gt_depths.permute(0, 1, 3, 5, 2, 4).contiguous()
    #     gt_depths = gt_depths.view(-1, self.downsample * self.downsample)
    #     gt_depths_tmp = torch.where(gt_depths == 0.0,
    #                                 1e5 * torch.ones_like(gt_depths),
    #                                 gt_depths)
    #     gt_depths = torch.min(gt_depths_tmp, dim=-1).values
    #     gt_depths = gt_depths.view(B * N, H // self.downsample,
    #                                W // self.downsample)

    #     if not self.sid:
    #         gt_depths = (gt_depths - (self.grid_config['depth'][0] -
    #                                   self.grid_config['depth'][2])) / \
    #                     self.grid_config['depth'][2]
    #     else:
    #         gt_depths = torch.log(gt_depths) - torch.log(
    #             torch.tensor(self.grid_config['depth'][0]).float())
    #         gt_depths = gt_depths * (self.D - 1) / torch.log(
    #             torch.tensor(self.grid_config['depth'][1] - 1.).float() /
    #             self.grid_config['depth'][0])
    #         gt_depths = gt_depths + 1.
    #     gt_depths = torch.where((gt_depths < self.D + 1) & (gt_depths >= 0.0),
    #                             gt_depths, torch.zeros_like(gt_depths))
    #     gt_depths = F.one_hot(
    #         gt_depths.long(), num_classes=self.D + 1).view(-1, self.D + 1)[:,
    #                                                                        1:]
    #     return gt_depths.float()
    

    def get_downsampled_gt_depth(self, gt_depths, for_uncertainty=False):
        """
        Input:
            gt_depths: [B, N, H, W]
            for_uncertainty: bool, 是否用于uncertainty学习
        Output:
            gt_depths: 
                if for_uncertainty: [B*N*h*w] 标准化的真实深度值
                else: [B*N*h*w, d] one-hot形式
        """
        B, N, H, W = gt_depths.shape
        gt_depths = gt_depths.view(B * N, H // self.downsample,
                                self.downsample, W // self.downsample,
                                self.downsample, 1)
        gt_depths = gt_depths.permute(0, 1, 3, 5, 2, 4).contiguous()
        gt_depths = gt_depths.view(-1, self.downsample * self.downsample)
        gt_depths_tmp = torch.where(gt_depths == 0.0,
                                    1e5 * torch.ones_like(gt_depths),
                                    gt_depths)
        gt_depths = torch.min(gt_depths_tmp, dim=-1).values
        gt_depths = gt_depths.view(B * N, H // self.downsample,
                                W // self.downsample)

        # 标准化深度值
        if not self.sid:
            gt_depths = (gt_depths - (self.grid_config['depth'][0] -
                                    self.grid_config['depth'][2])) / \
                        self.grid_config['depth'][2]
        else:
            gt_depths = torch.log(gt_depths) - torch.log(
                torch.tensor(self.grid_config['depth'][0]).float())
            gt_depths = gt_depths * (self.D - 1) / torch.log(
                torch.tensor(self.grid_config['depth'][1] - 1.).float() /
                self.grid_config['depth'][0])
            gt_depths = gt_depths + 1.

        gt_depths = torch.where((gt_depths < self.D) & (gt_depths >= 0.0),
                                gt_depths, torch.zeros_like(gt_depths))
        gt_depths=gt_depths/torch.tensor(self.D).to(device=gt_depths.device, dtype=gt_depths.dtype)

        if not for_uncertainty:
            # 用于原始depth学习，转one-hot
            gt_depths = F.one_hot(
                gt_depths.long(), num_classes=self.D + 1).view(-1, self.D + 1)[:,1:]
            return gt_depths.float()
        else:
            # 用于uncertainty学习，返回标准化的真实值
            return gt_depths.reshape(-1)

    def recover_regression_from_pred(self, pred, grid_config, sid=False):
        """
        Input:
            pred: [B, D, H, W] - B批次，D个深度类别的概率，H高，W宽
        Output:
            depth: [B, H, W] - 还原的深度图
        """
        # 生成深度值序列 [D] 
        depth_indices = torch.arange(pred.shape[1], device=pred.device)
        
        # 把深度索引扩展为 [1,D,1,1] 以便做广播乘法
        depth_indices = depth_indices.view(1, -1, 1, 1)
        
        # 加权求和得到预测深度指数 [B,H,W]
        pred_depth = torch.sum(pred * depth_indices, dim=1)  
        
        # 还原真实深度值
        if not sid:
            depth = pred_depth * grid_config['depth'][2] + (grid_config['depth'][0] - grid_config['depth'][2])
        else:
            depth = torch.exp(
                (pred_depth - 1) * torch.log(torch.tensor(grid_config['depth'][1] - 1.).float() / grid_config['depth'][0]) / (D - 1)
            ) * grid_config['depth'][0]
            
        return depth  # [B,H,W]

    # # @force_fp32()
    # def get_depth_loss(self, depth_labels, depth_preds):
    #     depth_labels = self.get_downsampled_gt_depth(depth_labels) #33792, 88 = 2, 6, 512, 1408
    #     depth_preds = depth_preds.permute(0, 2, 3,
    #                                       1).contiguous().view(-1, self.D)
    #     fg_mask = torch.max(depth_labels, dim=1).values > 0.0
    #     depth_labels = depth_labels[fg_mask]
    #     depth_preds = depth_preds[fg_mask]

    #     depth_loss = F.binary_cross_entropy(
    #         depth_preds,
    #         depth_labels,
    #         reduction='none',
    #     )
    #     depth_loss = depth_loss.sum() / max(1.0, fg_mask.sum())
    #     return self.loss_depth_weight * depth_loss

    @force_fp32()
    def get_depth_loss(self, depth_labels, depth_preds):
        depth = depth_preds[0]    
        logvar = depth_preds[1]   
        
        # 获取标准化的真实深度值
        gt_depth = self.get_downsampled_gt_depth(depth_labels, for_uncertainty=True)  
        
        # reshape预测值
        depth = depth.reshape(-1)   
        logvar = logvar.reshape(-1)
        
        # 获取前景mask
        fg_mask = gt_depth > 0.0
        
        # 应用mask
        self_D=torch.tensor(self.D).to(device=logvar.device, dtype=logvar.dtype)
        # depth = depth[fg_mask]
        depth = depth[fg_mask]*self_D
        # logvar = logvar[fg_mask]-torch.log(self_D*self_D)
        logvar = logvar[fg_mask]
        logvar = logvar.clamp(min=-10)  
        # gt_depth = gt_depth[fg_mask]
        gt_depth = gt_depth[fg_mask]*self_D
        
        # 计算KL散度loss
        # loss = ((gt_depth - depth)**2 + logvar/2).mean()
        smooth_value=F.smooth_l1_loss(depth, gt_depth, beta=1.0, reduction='none')
        loss = ((   smooth_value    * torch.exp(-logvar))/2 + logvar/2).mean()
        
        return self.loss_depth_weight * loss



    def generate_depth_distribution(self, depth_mean, logvar, num_depth_bins=88, eps=1e-6):
        """
        输入:
            depth_mean: [B,H,W] tensor, 回归得到的深度均值(μ)
            logvar: [B,H,W] tensor, 回归得到的log variance(v=2logσ)
            num_depth_bins: int, 深度离散化的bins数量
        输出:
            depth_prob: [B,D,H,W] tensor, 每个位置上的深度概率分布
        """
        B, H, W = depth_mean.shape
        
        # 生成深度值序列 [0,1,2,...,D-1]
        depth_bins = torch.arange(num_depth_bins, device=depth_mean.device, dtype=depth_mean.dtype)
        
        # 将深度值序列扩展为 [1,D,1,1] 以便broadcasting
        depth_bins = depth_bins.view(1, -1, 1, 1)
        
        # 将mean和logvar扩展为 [B,1,H,W] 以便broadcasting
        depth_mean = depth_mean.unsqueeze(1)*torch.tensor(num_depth_bins).to(depth_mean.device)  # [B,1,H,W]
        logvar = logvar.unsqueeze(1)          # [B,1,H,W]
        
        # 计算正态分布概率
        # v = 2logσ, 所以 σ^2 = exp(v)
        variance = torch.exp(logvar)  # 从v转回variance
        
        # 把2*pi转成tensor
        pi = torch.tensor(torch.pi, device=depth_mean.device, dtype=depth_mean.dtype)
        
        # 使用log-space计算避免数值问题
        log_prob = -0.5 * (torch.log(2 * pi) + logvar) - \
                0.5 * (depth_bins - depth_mean).pow(2) / (variance + eps)
        
        # 转换回probability space并归一化
        prob = torch.exp(log_prob)
        depth_prob = prob / (torch.sum(prob, dim=1, keepdim=True) + eps)
        
        return depth_prob  # [B,D,H,W]




    def forward(self, input, stereo_metas=None, depth_gt=None):
        (x, rots, trans, intrins, post_rots, post_trans, bda,
         mlp_input) = input[:8]

        B, N, C, H, W = x.shape
        x = x.view(B * N, C, H, W)

        

        x = self.depth_net(x, mlp_input, stereo_metas) #torch.Size([12, 120, 32, 88])=  torch.Size([12, 512, 32, 88]) torch.Size([2, 6, 27])
        depth_digit = x[:, :1, ...]  #torch.Size([12, 120, 32, 88])
        uq_digit = x[:, 1:2, ...] #torch.Size([12, 120, 32, 88])
        tran_feat = x[:, 2:2 + self.out_channels, ...]
        # depth = depth_digit.softmax(dim=1) #torch.Size([12, 88, 32, 88])
        
        # raise NotImplementedError()

        # depth=self.recover_regression_from_pred(depth, self.grid_config)#[12, 26, 44]
        # uq=uq_digit.sum(dim=1)
        uq=uq_digit.squeeze(dim=1)

        depth = depth_digit.squeeze(dim=1).sigmoid()  # [12,26,44]
        uq = uq.clamp(min=-10)  # [12,26,44] torch tensor用clamp        # [12,26,44]
        depth_distribution = self.generate_depth_distribution(depth, uq, num_depth_bins=self.D)  # [12,88,26,44]

        a=0
        # depth_digit = x[:, :1, ...]
        # uq_digit = x[:, 1:2, ...]
        # tran_feat = x[:, 2:2 + self.out_channels, ...]
        # depth = depth_digit.softmax(dim=1)
        # depth = depth_digit.sigmoid()*88
        
        # tran_feat depth  uq

        depth_uq_feat = torch.cat([depth.unsqueeze(dim=1), uq.unsqueeze(dim=1)], dim=1).detach()
        depth_uq_feat = self.convnet(depth_uq_feat)
        trans_feat = torch.cat([tran_feat, 
                                depth_uq_feat
                                ], dim=1)
        
        tran_feat = self.trans_feat_net(trans_feat)


        # bev_feat, depth = self.view_transform(input, depth, tran_feat) #torch.Size([2, 32, 16, 200, 200]) torch.Size([12, 88, 32, 88])
        bev_feat, depth_ret = self.view_transform(input, depth_distribution, tran_feat) #torch.Size([2, 32, 16, 200, 200]) torch.Size([12, 88, 32, 88])
        return bev_feat, [depth, uq]  # depth_digit.sigmoid()


@NECKS.register_module()
class LSSViewTransformerBEVStereoUQ(LSSViewTransformerBEVDepthUQ):
    def __init__(self,  **kwargs):
        super(LSSViewTransformerBEVStereoUQ, self).__init__(**kwargs)
        self.cv_frustum = self.create_frustum(kwargs['grid_config']['depth'],
                                              kwargs['input_size'],
                                              downsample=4)