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
"""
An actor observes the environment and decides actions. It also outputs extra
info necessary for model updates (learning) to occur.
"""
import abc

from adept.utils import listd_to_dlist
from adept.utils.requires_args import RequiresArgsMixin


class ActorMixin:
    """
    Mixin used for inheritance by an Agent.
    """

    @property
    @abc.abstractmethod
    def network(self):
        raise NotImplementedError

    @property
    @abc.abstractmethod
    def device(self):
        raise NotImplementedError

    @property
    @abc.abstractmethod
    def internals(self):
        raise NotImplementedError

    @internals.setter
    @abc.abstractmethod
    def internals(self, new):
        raise NotImplementedError

    @property
    @abc.abstractmethod
    def is_train(self):
        raise NotImplementedError

    @property
    @abc.abstractmethod
    def gpu_preprocessor(self):
        raise NotImplementedError

    @property
    @abc.abstractmethod
    def action_space(self):
        raise NotImplementedError

    @staticmethod
    @abc.abstractmethod
    def output_space(action_space):
        raise NotImplementedError

    @abc.abstractmethod
    def process_predictions(self, preds, available_actions):
        """
        B = Batch Size

        :param preds: Dict[str, torch.Tensor]
        :return:
            actions: Dict[ActionKey, LongTensor (B)]
            experience: Dict[str, Tensor (B, X)]
        """
        raise NotImplementedError

    def act(self, obs):
        """
        :param obs: Dict[str, Tensor]
        :return:
            actions: Dict[ActionKey, LongTensor (B)]
            experience: Dict[str, Tensor (B, X)]
        """
        if self.is_train:
            self.network.train()
        else:
            self.network.eval()
        predictions, internals = self.network(
            self.gpu_preprocessor(obs, self.device),
            self.internals
        )
        if 'available_actions' in obs:
            av_actions = obs['available_actions']
        else:
            av_actions = None
        actions, experience = self.process_predictions(predictions, av_actions)
        self.internals = internals
        return actions, experience

    @property
    def action_keys(self):
        return list(sorted(self.action_space.keys()))


class ActorModule(ActorMixin, RequiresArgsMixin, metaclass=abc.ABCMeta):

    def __init__(self, network, device, gpu_preprocessor, nb_env, action_space):
        self._network = network.to(device)
        self._internals = listd_to_dlist(
            [self.network.new_internals(device) for _ in range(nb_env)]
        )
        self._device = device
        self._gpu_preprocessor = gpu_preprocessor
        self._action_space = action_space

    @property
    def device(self):
        return self._device

    @property
    def network(self):
        return self._network

    @property
    def internals(self):
        return self._internals

    @internals.setter
    def internals(self, new):
        self._internals = new

    @property
    def action_space(self):
        return self._action_space
