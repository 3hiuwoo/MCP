import torch
import re
import numpy as np
from torch import nn
from model.base import CNN3, Res20


def load_network(network_name):
    '''
    return backbone network class
    '''
    if network_name == 'cnn3':
        return CNN3
    elif network_name == 'res20':
        return Res20
    else:
        raise ValueError(f'Unknown network {network_name}')


def load_model(model_name, task, in_channels=1, embeddim=256):
    '''
    load model
    '''
    network = load_network(model_name)
    
    if task in ['cmsc', 'simclr']:
        return ContrastModel(network=network, in_channels=in_channels, embeddim=embeddim)
    elif task == 'comet':
        return COMETModel(network=network, in_channels=in_channels, embeddim=embeddim)
    elif task == 'moco':
        return MoCoModel(network=network, in_channels=in_channels, embeddim=embeddim)
    elif task == 'mcp':
        return MCPModel(network=network, in_channels=in_channels, embeddim=embeddim)
    elif task == 'supervised':
        return SupervisedModel(network=network, in_channels=in_channels, embeddim=embeddim)
    else:
        raise ValueError(f'Unknown task {task}')
    
    
class SupervisedModel(nn.Module):
    '''
    supervised model
    '''
    def __init__(self, network, in_channels=1, num_classes=4, embeddim=256):
        super(SupervisedModel, self).__init__()
        self.embeddim = embeddim
        self.encoder = network(in_channels, embeddim)
        # dim = self.encoder.fc.weight.shape[1]
        # self.encoder.fc = nn.Linear(dim, num_classes)
        self.fc = nn.Linear(embeddim, num_classes)
        
    
    def forward(self, x):
        x = self.encoder(x)
        x = self.fc(x)
        return x        

class ContrastModel(nn.Module):
    '''
    contrastive model used for CMSC, SimCLR
    '''
    def __init__(self, network, in_channels=1, embeddim=256):
        super(ContrastModel, self).__init__()
        self.embeddim = embeddim
        self.encoder = network(in_channels, embeddim, )
        
        
    def forward(self, x):
        """
        Args:
            x (torch.Tensor): inputs with N views (BxNxCxS)
        Returns:
            h (torch.Tensor): latent embedding for each of the N views (NxBxH)
        """
        nviews = x.shape[1]
        x = x.permute(1, 0, 2, 3)
        h = [self.encoder(x[n, ...]) for n in range(nviews)]
        return torch.stack(h, dim=0)


class COMETModel(nn.Module):
    '''
    COMET model
    '''
    def __init__(self, network, in_channels=12, embeddim=256):
        super(COMETModel, self).__init__()
        self.embeddim = embeddim
        self.encoder = network(in_channels, embeddim, keep_dim=True)
        
    
    def forward(self, x):
        """
        Args:
            x (torch.Tensor): inputs with L levels, each with N views (BxLxNxCxS)
        Returns:
            h (torch.Tensor): latent embedding for each of the N views (NxBxH)
        """
        nlevels = x.shape[1]
        nviews = x.shape[2]
        
        x = x.permute(1, 2, 0, 3, 4)
        ls = []
        for l in range(nlevels):
            h = torch.stack([self.encoder(x[l, n, ...]) for n in range(nviews)], dim=0)
            ls.append(h)
            
        return torch.stack(ls, dim=0)
  
class MoCoModel(nn.Module):
    '''
    MoCo model
    '''
    def __init__(self, network, in_channels=1, embeddim=256, queue_size=16384, momentum=0.999):
        super(MoCoModel, self).__init__()
        self.embeddim = embeddim
        self.encoder_q = network(in_channels, embeddim)
        self.encoder_k = network(in_channels, embeddim)
        self.queue_size = queue_size
        self.momentum = momentum
        self.register_buffer("queue", torch.randn(embeddim, queue_size))
        self.queue = nn.functional.normalize(self.queue, dim=0)
        self.register_buffer("queue_ptr", torch.zeros(1, dtype=torch.long))

        for param_q, param_k in zip(self.encoder_q.parameters(), self.encoder_k.parameters()):
            param_k.data.copy_(param_q.data)
            param_k.requires_grad = False


    @torch.no_grad()
    def _update_key_encoder(self):
        """
        Momentum update of the key encoder
        """
        for param_q, param_k in zip(
            self.encoder_q.parameters(), self.encoder_k.parameters()
        ):
            param_k.data = param_k.data * self.momentum + param_q.data * (1.0 - self.momentum)


    @torch.no_grad()
    def _update_queue(self, keys):
        batch_size = keys.shape[0]

        ptr = int(self.queue_ptr)
        assert self.queue_size % batch_size == 0  # for simplicity

        # replace the keys at ptr (dequeue and enqueue)
        self.queue[:, ptr : ptr + batch_size] = keys.T
        ptr = (ptr + batch_size) % self.queue_size  # move pointer

        self.queue_ptr[0] = ptr
        

    def forward(self, x):
        """
        Input:
            x: input with 2 views (Bx2xCxS)
        Output:
            logits
        """
        x = x.permute(1, 0, 2, 3)
        
        # compute query features
        q = self.encoder_q(x[0])  # queries: BxH
        q = nn.functional.normalize(q, dim=1)

        # compute key features
        with torch.no_grad():  # no gradient to keys
            self._update_key_encoder()  # update the key encoder

            # shuffle for making use of BN
            idx = torch.randperm(x[1].size(0), device=x.device)
            k = self.encoder_k(x[1, idx, ...])  # keys: BxH
            
            # undo shuffle
            k = k[torch.argsort(idx)]
            k = nn.functional.normalize(k, dim=1)

        # positive logits: Nx1
        pos = torch.einsum("nq,nq->n", [q, k]).unsqueeze(-1)
        # negative logits: NxK
        neg = torch.einsum("nq,qk->nk", [q, self.queue.clone().detach()])

        # logits: Nx(1+K)
        logits = torch.cat([pos, neg], dim=1)

        # dequeue and enqueue
        self._update_queue(k)

        return logits


class MCPModel(nn.Module):
    '''
    MoCo model patient specific variant
    '''
    def __init__(self, network, in_channels=1, embeddim=256, queue_size=16384, momentum=0.999):
        super(MCPModel, self).__init__()
        self.embeddim = embeddim
        self.encoder_q = network(in_channels, embeddim)
        self.encoder_k = network(in_channels, embeddim)
        self.queue_size = queue_size
        self.momentum = momentum
        self.register_buffer("queue", torch.randn(embeddim, queue_size))
        self.queue = nn.functional.normalize(self.queue, dim=0)
        self.register_buffer("queue_ptr", torch.zeros(1, dtype=torch.long))
        
        for param_q, param_k in zip(self.encoder_q.parameters(), self.encoder_k.parameters()):
            param_k.data.copy_(param_q.data)
            param_k.requires_grad = False


    @torch.no_grad()
    def _update_key_encoder(self):
        """
        Momentum update of the key encoder
        """
        for param_q, param_k in zip(
            self.encoder_q.parameters(), self.encoder_k.parameters()
        ):
            param_k.data = param_k.data * self.momentum + param_q.data * (1.0 - self.momentum)


    @torch.no_grad()
    def _update_queue(self, keys):
        batch_size = keys.shape[0]

        ptr = int(self.queue_ptr)
        assert self.queue_size % batch_size == 0  # for simplicity

        # replace the keys at ptr (dequeue and enqueue)
        self.queue[:, ptr : ptr + batch_size] = keys.T
        ptr = (ptr + batch_size) % self.queue_size  # move pointer

        self.queue_ptr[0] = ptr
        
                    
    def forward(self, x):
        """
        Input:
            x: input with 2 views (Bx2xCxS)
            queue_heads: patient id queue passed in from the training loop
        Output:
            logits
        """
        x = x.permute(1, 0, 2, 3)
        
        # compute query features
        q = self.encoder_q(x[0])  # queries: BxH
        q = nn.functional.normalize(q, dim=1)

        # compute key features
        with torch.no_grad():  # no gradient to keys
            self._update_key_encoder()  # update the key encoder

            # shuffle for making use of BN
            idx = torch.randperm(x[1].size(0), device=x.device)
            k = self.encoder_k(x[1, idx, ...])  # keys: BxH
            
            # undo shuffle
            k = k[torch.argsort(idx)]
            k = nn.functional.normalize(k, dim=1)

        query_key = torch.matmul(q, k.T) # BxB
        query_queue = torch.matmul(q, self.queue.clone().detach()) # BxK

        # dequeue and enqueue
        self._update_queue(k)

        return query_key, query_queue