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
from copy import deepcopy
import torch


class ObsPreprocessor:
    def __init__(self, ops, observation_space, observation_dtypes=None):
        """
        :param ops: List[Operation]
        :param observation_space: Dict[ObsKey, Shape]
        :param observation_dtypes: Dict[ObsKey, dtype_str]
        """
        updated_obs_space = deepcopy(observation_space)
        updated_obs_dtypes = deepcopy(observation_dtypes)

        rank_to_names = {1: [], 2: [], 3: [], 4: []}
        for name, shape in updated_obs_space.items():
            rank_to_names[len(shape)].append(name)

        for op in ops:
            for rank, names in rank_to_names.items():
                for name in names:
                    if op.filter(name, rank):
                        updated_obs_space[name] = op.update_shape(
                            updated_obs_space[name]
                        )
                        if updated_obs_dtypes:
                            updated_obs_dtypes[name] = op.update_dtype(
                                updated_obs_dtypes[name]
                            )

        self.ops = ops
        self.observation_space = updated_obs_space
        self.observation_dtypes = updated_obs_dtypes
        self.rank_to_names = rank_to_names

    def __call__(self, obs, device=None):
        processed_obs = deepcopy(obs)
        for op in self.ops:
            for rank, names in self.rank_to_names.items():
                for name in names:
                    if device is not None:
                        processed_obs[name] = processed_obs[name].to(device)

                    if op.filter(name, rank):
                        processed_obs[name] = op.update_obs(processed_obs[name])
        return processed_obs

    def reset(self):
        for o in self.ops:
            o.reset()

    def __repr__(self):
        return f"ObsPreprocessor(" \
            f"ops={self.ops}," \
            f"observation_space={self.observation_space}, " \
            f"observation_dtypes={self.observation_dtypes}," \
            f"rank_to_names={self.rank_to_names})"

    def __getstate__(self):
        odict = self.__dict__.copy()
        new_obs_dtypes = {}
        for key, dtype in self.observation_dtypes.items():
            if dtype == torch.float:
                new_obs_dtypes[key] = 'float'
        odict['observation_dtypes'] = new_obs_dtypes
        return odict

    def __setstate__(self, state):
        self.__dict__ == state
        new_obs_dtypes = {}
        dtypes = state['observation_dtypes']
        for obs_key, obs_value in dtypes.items():
            if obs_key == 'float':
                new_obs_dtypes[obs_key] = torch.float32
        self.observation_dtypes = new_obs_dtypes
