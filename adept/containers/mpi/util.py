import time
import numpy as np
from enum import IntEnum


class MpiMessages(IntEnum):
    STOP = 1
    STOPPED = 2
    NAMES_AND_SHAPES = 3
    SEND = 20
    SEND_ACK = 25


class ArrayFlattener:
    def __init__(self, shapes, src_dtype=np.float32, dest_dtype=np.float32):
        self.shapes = shapes
        self.sizes = [np.prod(x) for x in shapes]
        self.total_size = np.sum([np.prod(x) for x in self.shapes])
        self.src_dtype = src_dtype
        self.dest_dtype = dest_dtype

    def flatten(self, values, buffer=None):
        if buffer is None:
            buffer = np.empty(self.total_size, dtype=self.dest_dtype)
        curr_start = 0
        for ind, s in enumerate(self.shapes):
            val = values[ind]
            # reshape into buffer
            buffer[curr_start:curr_start+self.sizes[ind]] = val.ravel()
            curr_start += self.sizes[ind]
        return buffer

    def unflatten(self, value):
        values = []
        curr_start = 0
        for ind, (shape, size) in enumerate(zip(self.shapes, self.sizes)):
            val = np.empty(size, dtype=self.src_dtype)
            val[0:] = value[curr_start:curr_start+size]
            values.append(val.reshape(shape))
            curr_start += size
        return values


class VariableRecieverSingle:
    def __init__(self, comm, variable_buffer, max_fail=5, root=0, warning_time=0.1):
        self.mpi_comm = comm
        self.variable_buffer = variable_buffer
        self.root = root
        self.comms = comm.Ibcast(self.variable_buffer, root=self.root)
        self.max_fail = max_fail
        self.curr_fail = 0
        self.warning_time = warning_time

    def recieve(self):
        wait = False
        if self.curr_fail >= self.max_fail:
            wait = True

        # get the latest variables for all
        st = time.time()
        got_something = self.get_latest(self.variable_buffer, wait)
        et = time.time()
        if et - st > self.warning_time:
            print('WARNING {}: Long wait for variable updates. Waited {}'.format(
                self.mpi_comm.Get_rank(),
                et - st
            ))
        if not got_something:
            self.curr_fail += 1
            return None
        self.curr_fail = 0
        return self.variable_buffer

    def get_latest(self, v, wait=False):
        if not wait:
            done = self.comms.Test()
            if not done:
                return False
        else:
            self.comms.Wait()

        # so got something loop through queue
        number_of_recvs = 0
        while True:
            self.comms = self.mpi_comm.Ibcast(v, root=self.root)
            has_data = self.comms.Test()
            if not has_data:
                if number_of_recvs >= 5:
                    print('WARNING {}: Worker had {} variable updates waiting. Worker is slow.'.format(
                        self.mpi_comm.Get_rank(),
                        number_of_recvs
                    ))

                return True  # we did get something
            else:
                number_of_recvs += 1
