# Copyright (C) 2019 Heron Systems, Inc.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
import numpy as np


from adept.utils import listd_to_dlist
from adept.networks import NetworkModule
from adept.networks.net3d.four_conv import FourConv
from adept.networks.net3d.convlstm import ConvLSTM

import torch
from torch import nn
from torch.nn import ConvTranspose2d, BatchNorm2d, init, functional as F
from torch.nn import UpsamplingBilinear2d


def flatten(tensor):
    return tensor.view(*tensor.shape[0:-3], -1)


class Embedder(NetworkModule):
    args = {
        'autoencoder': True,
        'reward_pred': True
    }

    def __init__(self, args, obs_space, output_space):
        super().__init__()
        self._autoencoder = args.autoencoder
        self._reward_pred = args.reward_pred
        self._nb_action = int(output_space['Discrete'][0] / 51)

        # encode state with recurrence captured
        self._obs_key = list(obs_space.keys())[0]
        self.conv_stack = FourConv(obs_space[self._obs_key], 'fourconv', True)
        self.lstm = ConvLSTM(self.conv_stack.output_shape(), 'lstm', True, 32, 3)
        lstm_out_shape = np.prod(self.lstm.output_shape())

        # policy output
        self.pol_outputs = nn.ModuleDict(
            {k: nn.Linear(lstm_out_shape, v[0]) for k, v in output_space.items()}
        )

        if self._autoencoder:
            self.ae_upsample = PixelShuffleFourConv(self.lstm.output_shape()[0])

        if self._reward_pred:
            self.reward_pred = nn.Sequential(
                nn.Linear(lstm_out_shape+self._nb_action, 128),
                nn.BatchNorm1d(128),
                nn.Linear(128, 1)
            )

        # imagined next embedding
        # self.imag_embed_encoder = ResEmbed(self.lstm.output_shape()[0]+self._nb_action, 32)
        # self.imag_encoder = nn.Linear(np.prod(self.lstm.output_shape()) + 1, 128, bias=True)
        # reward prediciton from lstm + action one hot
        # self.reward_pred = nn.Linear(lstm_out_shape+self._nb_action, 1)
        # distil policy from imagination
        # self.distil_pol_outputs = nn.Linear(lstm_out_shape, output_space['Discrete'][0])

    @classmethod
    def from_args(
        cls,
        args,
        observation_space,
        output_space,
        net_reg
    ):
        """
        Construct a Embedder from arguments.

        ArgName = str
        ObsKey = str
        OutputKey = str
        Shape = Tuple[*int]

        :param args: Dict[ArgName, Any]
        :param observation_space: Dict[ObsKey, Shape]
        :param output_space: Dict[OutputKey, Shape]
        :param net_reg: NetworkRegistry
        :return: Embedder
        """
        return Embedder(args, observation_space, output_space)

    def new_internals(self, device):
        """
        Define any initial hidden states here, move them to device if necessary.

        InternalKey=str

        :return: Dict[InternalKey, torch.Tensor (ND)]
        """
        return {
            **self.lstm.new_internals(device),
        }

    def _encode_observation(self, observation, internals):
        conv_out, _ = self.conv_stack(observation, {})
        hx, lstm_internals = self.lstm(conv_out, internals)
        return hx, lstm_internals

    def forward(self, observation, internals, ret_imag=False):
        """
        Compute forward pass.

        ObsKey = str
        InternalKey = str

        :param observation: Dict[ObsKey, torch.Tensor (1D | 2D | 3D | 4D)]
        :param internals: Dict[InternalKey, torch.Tensor (ND)]
        :return: Dict[str, torch.Tensor (ND)]
        """
        obs = observation[self._obs_key]
        encoded_obs, lstm_internals = self._encode_observation(obs, internals)
        encoded_obs_flat = flatten(encoded_obs)

        pol_outs = {k: self.pol_outputs[k](encoded_obs_flat) for k in self.pol_outputs.keys()}

        # return cached stuff for training
        if ret_imag:
            if self._autoencoder:
                # upsample back to pixels
                ae_state_pred = self.ae_upsample(encoded_obs)
                pol_outs['ae_state_pred'] = ae_state_pred
            if self._reward_pred:
                pol_outs['encoded_obs'] = encoded_obs

        return pol_outs, lstm_internals

    def predict_reward(self, encoded_obs, action_taken):
        # reward prediction
        return self.reward_pred(torch.cat([flatten(encoded_obs), action_taken], dim=1))


def pixel_norm(xs):
    return xs * torch.rsqrt(torch.mean(xs ** 2, dim=1, keepdim=True) + 1e-8)


class ResEmbed(nn.Module):
    def __init__(self, in_channel, batch_norm=False):
        super().__init__()
        self._out_shape = None
        self.conv1 = nn.Conv2d(in_channel, 32, 3, padding=1, bias=False)
        self.conv2 = nn.Conv2d(32, 64, 3, padding=1, bias=False)
        self.conv3 = nn.Conv2d(64, 32, 3, padding=1, bias=False)

        if not batch_norm:
            self.n1 = pixel_norm
            self.n2 = pixel_norm
        else:
            self.n1 = BatchNorm2d(32)
            self.n2 = BatchNorm2d(64)

        relu_gain = init.calculate_gain('relu')
        self.conv1.weight.data.mul_(relu_gain)
        self.conv2.weight.data.mul_(relu_gain)

    def forward(self, xs):
        xs = F.relu(self.n1(self.conv1(xs)))
        xs = F.relu(self.n2(self.conv2(xs)))
        xs = self.conv3(xs)
        return xs


class PixelShuffleFourConv(nn.Module):
    def __init__(self, in_channel, batch_norm=False):
        super().__init__()
        self._out_shape = None
        self.conv1 = ConvTranspose2d(in_channel, 32*4, 7, bias=False)
        self.conv2 = ConvTranspose2d(32, 32*4, 4, bias=False)
        self.conv3 = ConvTranspose2d(32, 32*4, 3, padding=1, bias=False)
        self.conv4 = ConvTranspose2d(32, 1, 3, padding=1, bias=True)

        if not batch_norm:
            self.n1 = pixel_norm
            self.n2 = pixel_norm
            self.n3 = pixel_norm
        else:
            self.n1 = BatchNorm2d(32*4)
            self.n2 = BatchNorm2d(32*4)
            self.n3 = BatchNorm2d(32*4)

        relu_gain = init.calculate_gain('relu')
        self.conv1.weight.data.mul_(relu_gain)
        self.conv2.weight.data.mul_(relu_gain)
        self.conv3.weight.data.mul_(relu_gain)

    def forward(self, xs):
        xs = F.relu(F.pixel_shuffle(self.n1(self.conv1(xs)), 2))
        xs = F.relu(F.pixel_shuffle(self.n2(self.conv2(xs)), 2))
        xs = F.relu(F.pixel_shuffle(self.n3(self.conv3(xs)), 2))
        xs = F.leaky_relu(self.conv4(xs))
        return xs

