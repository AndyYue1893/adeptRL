# Copyright (C) 2018 Heron Systems, Inc.
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
from adept.expcaches._base import BaseExperience
from collections import namedtuple
import torch
import numpy as np


class ACRollout(dict, BaseExperience):
    def __init__(self, nb_rollout, reward_normalizer):
        super(ACRollout, self).__init__()
        assert type(nb_rollout == int)
        self['states'] = []
        self['rewards'] = []
        self['terminals'] = []
        self['values'] = []
        self['log_probs'] = []
        self['entropies'] = []
        self.nb_rollout = nb_rollout
        self.reward_normalizer = reward_normalizer

    def write_forward(self, actions, experience):
        for k, v in experience.items():
            if k not in self:
                raise KeyError(f'Incompatible rollout key: {k}')
            self[k].append(v)

    def write_env(self, obs, rewards, terminals, infos):
        rewards = self.reward_normalizer(rewards)
        self['states'].append(obs)
        self['rewards'].append(rewards)
        # TODO: rename as terminals_mask or don't mask here
        self['terminals'].append(terminals)

    def read(self):
        # returns rollout as a named tuple
        return namedtuple(self.__class__.__name__, self.keys())(**self)

    def clear(self):
        for k in self.keys():
            self[k] = []

    def is_ready(self):
        return len(self) == self.nb_rollout

    def __len__(self):
        return len(self['rewards'])
