import torch.distributed as dist
import torch.nn as nn
import distributed.process_group_manager as pgm

from parallel.base_parallel import BaseParallel

class DataParallel(BaseParallel):
    def __init__(self, model, config):
        #TODO: Add Zero1
        #TODO: Interleave all_reduce
        super().__init__(model, config)