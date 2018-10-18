"""
Copyright (C) 2018 Heron Systems, Inc.

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""
from ._base import BaseExperience
import numpy as np
import torch
from threading import Thread
from adept.utils import listd_to_dlist
from collections import namedtuple


class ExperienceReplay(dict, BaseExperience):
    def __init__(self, nb_env, batch_size, rollout_len, max_len, reward_normalizer, keys):
        super().__init__()
        # TODO: it's best to give the observation_space here along with any other data that will be
        # stored
        self['rewards'] = np.empty((max_len, nb_env), dtype=np.float32)
        self['terminals'] = np.empty((max_len, nb_env), dtype=np.float32)
        self.nb_env = nb_env
        self.max_len = max_len // nb_env
        self.batch_size = batch_size
        self.rollout_len = rollout_len
        self.min_length = 100
        self.reward_normalizer = reward_normalizer
        self._cached_rollout = []
        self._cache_thread = Thread(target=self._cache_loop)
        self.max_cache = 5
        self._current_index = 0
        self._num_inserted = 0
        self._cache_thread.start()

    def write_forward(self, **kwargs):
        # TODO: support internals
        for k, v in kwargs.items():
            v = np.asarray(v)
            if k not in self:
                # get size and create empty array
                print(k, v.shape)
                # assumes nb_env is first dim
                self[k] = np.empty((self.max_len, self.nb_env, ) + v.shape[1:], dtype=v.dtype)
            # insert
            self[k][self._current_index] = v

    def write_env(self, obs, rewards, terminals, infos):
        for k in obs.keys():
            self_key = 'obs-{}'.format(k)
            v = obs[k].cpu().numpy()
            if self_key not in self:
                # get size and create empty array
                print(k, v.shape)
                # assumes nb_env is first dim
                self[self_key] = np.empty((self.max_len, self.nb_env, ) + v.shape[1:], dtype=v.dtype)
                self[self_key] = np.empty((self.max_len, self.nb_env, ) + v.shape[1:], dtype=v.dtype)
            # insert
            self[self_key][self._current_index] = v
        self['rewards'][self._current_index] = np.asarray(rewards)
        self['terminals'][self._current_index] = 1 - np.asarray(terminals)

        self._current_index = (self._current_index + 1) % self.max_len
        self._num_inserted += 1

    def clear(self):
        """ 
        Called after read, deletes any over size
        """
        pass
    
    def _cache_loop(self):
        import time
        while not self.is_ready():
            time.sleep(1)

        # exp ready
        while True:
            if len(self._cached_rollout) < self.max_cache:
                self._cached_rollout.append(self._read())
            else:
                time.sleep(0.1)

    def read(self):
        if len(self._cached_rollout) > 1:
            return self._cached_rollout.pop(0)
        else:
            print('Cache miss')
            return self._read()

    def _read(self):
        # returns torch tensors (or dict of tensors) of shape [batch, seq, ...]
        max_ind = len(self) - 2 - self.rollout_len
        start_indexes = np.random.randint(0, max_ind, size=self.batch_size)
        end_indexes = start_indexes + self.rollout_len
        flat_indexes = np.array([np.arange(s_ind, e_ind) for s_ind, e_ind in
                             zip(start_indexes, end_indexes)]).ravel()
        env_ind = torch.from_numpy(np.random.randint(0, self.nb_env, size=self.batch_size))

        rollout = {k: self.take(self[k], flat_indexes, env_ind) for k, v in self.items()}
        rollout['last_obs'] = self.take_single(self['obs'], end_indexes + 1, env_ind)
        rollout['last_internals'] = self.take_single(self['internals'], end_indexes + 1, env_ind)
        # returns rollout as a named tuple
        rollout = namedtuple(self.__class__.__name__, rollout.keys())(**rollout)
        return rollout

    def is_ready(self):
        return self._num_inserted > self.min_length
        
    def __len__(self):
        return min(self._num_inserted, self.max_len)

    def take(self, values, flat_indexes, worker_inds):
        print('values', values.shape, values.dtype)
        sliced_v = [values[i] for i in flat_indexes]
        if isinstance(sliced_v[0], dict):
            slice_dict = listd_to_dlist(sliced_v)
            new_dict = {}
            for k, v in slice_dict.items():
                # can be list/array or tensor
                if isinstance(v[0], torch.Tensor):
                    arr_v = torch.stack(v)
                # array/list
                else:
                    arr_v = torch.from_numpy(np.asarray(v))
                orig_shape = arr_v.shape
                arr_v = arr_v.reshape(self.batch_size, self.rollout_len, self.nb_env, -1)
                arr_v = arr_v[np.arange(self.batch_size), :, worker_inds]
                new_dict[k] = arr_v.reshape((self.batch_size, self.rollout_len,) + orig_shape[2:])
            return new_dict
        # list of tensors
        elif isinstance(sliced_v[0], torch.Tensor):
            tensor = torch.stack(sliced_v)
            orig_shape = tensor.shape
            tensor = tensor.reshape(self.batch_size, self.rollout_len, self.nb_env, -1)
            return tensor[np.arange(self.batch_size), :, worker_inds]
        # list of lists or numpy arrays
        else: 
            array = torch.from_numpy(np.asarray(sliced_v))
            orig_shape = array.shape
            array = array.reshape(self.batch_size, self.rollout_len, self.nb_env, -1)
            array = array[np.arange(self.batch_size), :, worker_inds]
            return array.reshape((self.batch_size, self.rollout_len,) + orig_shape[2:])

    def take_single(self, values, inds, worker_inds):
        sliced_v = [values[i] for i in inds]
        if isinstance(sliced_v[0], dict):
            slice_dict = listd_to_dlist(sliced_v)
            new_dict = {}
            for k, v in slice_dict.items():
                # can be list/array or tensor
                if isinstance(v[0], torch.Tensor):
                    arr_v = torch.stack(v)
                # array/list
                else:
                    arr_v = torch.from_numpy(np.asarray(v))
                new_dict[k] = arr_v[np.arange(self.batch_size), worker_inds]
            return new_dict
        # list of tensors
        elif isinstance(sliced_v[0], torch.Tensor):
            tensor = torch.stack(sliced_v)
            return tensor[np.arange(self.batch_size), worker_inds]
        # list of lists or numpy arrays
        else: 
            array = torch.from_numpy(np.asarray(sliced_v))
            return array[np.arange(self.batch_size), worker_inds]

class PrioritizedExperienceReplay(BaseExperience):

    def write_forward(self, items):
        pass

    def write_env(self, obs, rewards, terminals, infos):
        pass

    def read(self):
        pass

    def is_ready(self):
        pass
