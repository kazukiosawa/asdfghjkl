from typing import List, Union, Any, Tuple
from dataclasses import dataclass
import numpy as np

import torch
from torch import Tensor
import torch.nn as nn
import torch.nn.functional as F
import torch.distributed as dist

from .core import no_centered_cov
from .utils import skip_param_grad
from .grad_maker import GradientMaker
from .matrices import *
from .vector import ParamVector, reduce_vectors
from .mvp import power_method, stochastic_lanczos_quadrature, conjugate_gradient_method, quadratic_form

_COV_FULL = 'cov_full'
_CVP_FULL = 'cvp_full'

LOSS_CROSS_ENTROPY = 'cross_entropy'
LOSS_MSE = 'mse'

__all__ = [
    'LOSS_CROSS_ENTROPY',
    'LOSS_MSE',
    'FisherMakerConfig',
    'get_fisher_maker',
]

_supported_types = [FISHER_EXACT, FISHER_MC, FISHER_EMP]
_supported_shapes = [SHAPE_FULL, SHAPE_LAYER_WISE, SHAPE_KRON, SHAPE_UNIT_WISE, SHAPE_DIAG]
_supported_shapes_for_fvp = [SHAPE_FULL, SHAPE_LAYER_WISE]


@dataclass
class FisherMakerConfig:
    fisher_type: str
    fisher_shapes: List[Any]
    loss_type: str = None
    n_mc_samples: int = 1
    var: float = 1.
    seed: int = None
    fisher_attr: str = 'fisher'
    fvp_attr: str = 'fvp'
    fvp: bool = False
    ignore_modules: List[Any] = None
    is_distributed: bool = False
    all_reduce: bool = False
    is_master: bool = True


class FisherMaker(GradientMaker):
    def __init__(self, model, config):
        super().__init__(model)
        self.config: FisherMakerConfig = config

    def zero_fisher(self, fvp=False):
        attr = self.config.fvp_attr if fvp else self.config.fisher_attr
        for module in self.model.modules():
            if hasattr(module, attr):
                delattr(module, attr)

    @property
    def is_fisher_emp(self):
        return False

    def forward_and_backward(self,
                             scale=1.,
                             accumulate=False,
                             calc_emp_loss_grad=True,
                             vec: ParamVector = None) -> Union[Tuple[Any, Tensor], Any]:
        model = self.model
        fisher_shapes = self.config.fisher_shapes
        if isinstance(fisher_shapes, str):
            fisher_shapes = [fisher_shapes]
        ignore_modules = self.config.ignore_modules
        fvp = self.config.fvp
        seed = self.config.seed

        if not accumulate:
            # set Fisher/FVP zero
            self.zero_fisher(fvp=fvp)

        if seed:
            torch.random.manual_seed(seed)

        with no_centered_cov(model, fisher_shapes, ignore_modules=ignore_modules, cvp=fvp, vectors=vec) as cxt:
            self._forward()
            emp_loss = self._loss

            def closure(nll_expr, retain_graph=False):
                cxt.clear_batch_grads()
                with skip_param_grad(model, disable=calc_emp_loss_grad and self.is_fisher_emp):
                    nll_expr().backward(retain_graph=retain_graph or calc_emp_loss_grad)
                if fvp:
                    cxt.calc_full_cvp(model)
                else:
                    cxt.calc_full_cov(model)

            if self.is_fisher_emp:
                closure(lambda: emp_loss)
            else:
                self._fisher_loop(closure)
            self.accumulate(cxt, scale, fvp=fvp)

        if calc_emp_loss_grad and not self.is_fisher_emp:
            emp_loss.backward()

        if self._loss_fn is None:
            return self._model_output
        else:
            return self._model_output, self._loss

    def _fisher_loop(self, closure):
        raise NotImplementedError

    def accumulate(self, cxt, scale=1., target_module=None, target_module_name=None, fvp=False):
        model = self.model
        for name, module in model.named_modules():
            if target_module is not None and module != target_module:
                continue
            if target_module_name is not None and name != target_module_name:
                continue
            # accumulate layer-wise fisher/fvp
            if fvp:
                self._accumulate_fvp(module, cxt.cvp_paramvector(module, pop=True), scale)
            else:
                self._accumulate_fisher(module, cxt.cov_symmatrix(module, pop=True), scale)
            if target_module is not None:
                break
            if target_module_name is not None:
                target_module = module
                break

        if target_module is None or target_module == model:
            # accumulate full fisher/fvp
            if fvp:
                self._accumulate_fvp(model, cxt.full_cvp_paramvector(model, pop=True), scale)
            else:
                self._accumulate_fisher(model, cxt.full_cov_symmatrix(model, pop=True), scale)

    def _accumulate_fisher(self, module: nn.Module, new_fisher, scale=1., fvp=False):
        if new_fisher is None:
            return
        if scale != 1:
            new_fisher.mul_(scale)
        dst_attr = self.config.fvp_attr if fvp else self.config.fisher_attr
        dst_fisher = getattr(module, dst_attr, None)
        if dst_fisher is None:
            setattr(module, dst_attr, new_fisher)
        else:
            # this must be __iadd__ to preserve inv
            dst_fisher += new_fisher

    def _accumulate_fvp(self, module: nn.Module, new_fisher, scale=1.):
        self._accumulate_fisher(module, new_fisher, scale, fvp=True)

    def get_fisher_tensor(self, module: nn.Module, *keys) -> Union[torch.Tensor, None]:
        fisher = getattr(module, self.config.fisher_attr, None)
        if fisher is None:
            return None
        data = fisher
        for key in keys:
            data = getattr(data, key, None)
        if data is not None:
            assert isinstance(data, torch.Tensor)
        return data

    def reduce_scatter_fisher(self,
                              module_partitions: List[List[torch.nn.Module]],
                              *keys,
                              with_grad=False,
                              group: dist.ProcessGroup = None,
                              async_op=False):
        assert dist.is_initialized()
        assert torch.cuda.is_available()
        assert dist.get_backend(group) == dist.Backend.NCCL
        world_size = dist.get_world_size(group)
        assert len(module_partitions) == world_size
        assert all(len(module_partitions[0]) == len(module_partitions[i]) for i in range(1, world_size))
        tensor_partitions = []
        for module_list in module_partitions:
            tensor_list = []
            for module in module_list:
                tensor = self.get_fisher_tensor(module, *keys)
                if tensor is None:
                    continue
                assert tensor.is_cuda
                tensor_list.append(tensor)
                if with_grad:
                    for p in module.parameters():
                        if p.requires_grad and p.grad is not None:
                            tensor_list.append(p.grad)
            tensor_partitions.append(tensor_list)
        num_tensors_per_partition = len(tensor_partitions[0])
        assert all(len(tensor_partitions[i]) == num_tensors_per_partition for i in range(1, world_size))
        handles = []
        for i in range(num_tensors_per_partition):
            input_list = [tensor_list[i] for tensor_list in tensor_partitions]
            output = input_list[dist.get_rank(group)]
            handles.append(dist.reduce_scatter(output, input_list, group=group, async_op=async_op))
        return handles

    def reduce_fisher(self,
                      modules,
                      *keys,
                      all_reduce=True,
                      with_grad=False,
                      dst=0,
                      group: dist.ProcessGroup = None,
                      async_op=False):
        assert dist.is_initialized()
        tensor_list = []
        for module in modules:
            tensor = self.get_fisher_tensor(module, *keys)
            if tensor is None:
                continue
            tensor_list.append(tensor)
            if with_grad:
                for p in module.parameters():
                    if p.requires_grad and p.grad is not None:
                        tensor_list.append(p.grad)
        handles = []
        for tensor in tensor_list:
            if all_reduce:
                handles.append(dist.all_reduce(tensor, group=group, async_op=async_op))
            else:
                handles.append(dist.reduce(tensor, dst=dst, group=group, async_op=async_op))
        return handles

    def reduce_fvp(self, fisher_shape, is_master=True, all_reduce=False):
        v = self.load_fvp(fisher_shape)
        v = reduce_vectors(v, is_master, all_reduce)
        attr = self.config.fvp_attr
        if fisher_shape == SHAPE_FULL:
            setattr(self.model, attr, v)
        else:
            for module in self.model.modules():
                if hasattr(module, attr):
                    setattr(module, attr, v.get_vectors_by_module(module))

    def load_fvp(self, fisher_shape: str) -> ParamVector:
        if fisher_shape == SHAPE_FULL:
            v = getattr(self.model, self.config.fvp_attr, None)
            if v is None:
                return None
            return v.copy()
        else:
            rst = None
            for module in self.model.modules():
                if module == self.model:
                    continue
                v = getattr(module, self.config.fvp_attr, None)
                if v is not None:
                    if rst is None:
                        rst = v.copy()
                    else:
                        rst.extend(v.copy())
            return rst

    def _get_fvp_fn(self):
        def fvp_fn(vec: ParamVector) -> ParamVector:
            self.forward_and_backward(vec=vec)
            return self.load_fvp(self.config.fisher_shapes[0])
        return fvp_fn

    def fisher_eig(self,
                   top_n=1,
                   max_iters=100,
                   tol=1e-3,
                   is_distributed=False,
                   print_progress=False
                   ):
        # for making MC samplings at each iteration deterministic
        random_seed = torch.rand(1) * 100 if self.config.fisher_type == FISHER_MC else None

        eigvals, eigvecs = power_method(self._get_fvp_fn(),
                                        self.model,
                                        top_n=top_n,
                                        max_iters=max_iters,
                                        tol=tol,
                                        is_distributed=is_distributed,
                                        print_progress=print_progress,
                                        random_seed=random_seed
                                        )

        return eigvals, eigvecs

    def fisher_esd(self,
                   n_v=1,
                   num_iter=100,
                   num_bins=10000,
                   sigma_squared=1e-5,
                   overhead=None,
                   is_distributed=False
                   ):
        # for making MC samplings at each iteration deterministic
        random_seed = torch.rand(1) * 100 if self.config.fisher_type == FISHER_MC else None

        eigvals, weights = stochastic_lanczos_quadrature(self._get_fvp_fn(),
                                                         self.model,
                                                         n_v=n_v,
                                                         num_iter=num_iter,
                                                         is_distributed=is_distributed,
                                                         random_seed=random_seed
                                                         )
        # referenced from https://github.com/amirgholami/PyHessian/blob/master/density_plot.py
        eigvals = np.array(eigvals)
        weights = np.array(weights)

        lambda_max = np.mean(np.max(eigvals, axis=1), axis=0)
        lambda_min = np.mean(np.min(eigvals, axis=1), axis=0)

        sigma_squared = sigma_squared * max(1, (lambda_max - lambda_min))
        if overhead is None:
            overhead = np.sqrt(sigma_squared)

        range_max = lambda_max + overhead
        range_min = np.maximum(0., lambda_min - overhead)

        grids = np.linspace(range_min, range_max, num=num_bins)

        density_output = np.zeros((n_v, num_bins))

        for i in range(n_v):
            for j in range(num_bins):
                x = grids[j]
                tmp_result = np.exp(-(x - eigvals[i, :])**2 / (2.0 * sigma_squared)) / np.sqrt(2 * np.pi * sigma_squared)
                density_output[i, j] = np.sum(tmp_result * weights[i, :])
        density = np.mean(density_output, axis=0)
        normalization = np.sum(density) * (grids[1] - grids[0])
        density = density / normalization
        return density, grids

    def fisher_free(self,
                    b=None,
                    init_x=None,
                    damping=1e-3,
                    max_iters=None,
                    tol=1e-8,
                    preconditioner=None,
                    print_progress=False,
                    random_seed=None
                    ) -> ParamVector:
        if b is None:
            grads = {p: p.grad for p in self.model.parameters() if p.requires_grad}
            b = ParamVector(grads.keys(), grads.values())

        # for making MC samplings at each iteration deterministic
        if self.config.fisher_type == FISHER_MC and random_seed is None:
            random_seed = int(torch.rand(1) * 100)

        return conjugate_gradient_method(self._get_fvp_fn(),
                                         b,
                                         init_x=init_x,
                                         damping=damping,
                                         max_iters=max_iters,
                                         tol=tol,
                                         preconditioner=preconditioner,
                                         print_progress=print_progress,
                                         random_seed=random_seed)

    def fisher_quadratic_form(self, vec: ParamVector = None):
        if vec is None:
            grads = {p: p.grad for p in self.model.parameters() if p.requires_grad}
            vec = ParamVector(grads.keys(), grads.values())

        return quadratic_form(self._get_fvp_fn(), vec)


class FisherExactCrossEntropy(FisherMaker):
    def _fisher_loop(self, closure):
        logits = self._logits
        log_probs = F.log_softmax(logits, dim=-1)
        log_probs = log_probs.view(-1, log_probs.size(-1))
        n, n_classes = log_probs.shape
        with torch.no_grad():
            probs = F.softmax(logits, dim=-1)
            sqrt_probs = torch.sqrt(probs)
        for i in range(n_classes):
            targets = torch.tensor([i] * n, device=logits.device)

            def nll_expr():
                nll = F.nll_loss(log_probs, targets, reduction='none', ignore_index=-1)
                return nll.mul(sqrt_probs[:, i]).sum()
            closure(nll_expr, retain_graph=i < n_classes - 1)


class FisherMCCrossEntropy(FisherMaker):
    def _fisher_loop(self, closure):
        logits = self._logits
        log_probs = F.log_softmax(logits, dim=-1)
        n_mc_samples = self.config.n_mc_samples
        probs = F.softmax(logits, dim=-1)
        log_probs = F.log_softmax(logits, dim=-1)
        dist = torch.distributions.Categorical(probs)
        for i in range(n_mc_samples):
            with torch.no_grad():
                targets = dist.sample()
            closure(lambda: F.nll_loss(log_probs.view(-1, log_probs.size(-1)),
                                       targets.view(-1), reduction='sum', ignore_index=-1) / n_mc_samples,
                    retain_graph=i < n_mc_samples - 1)


class FisherExactMSE(FisherMaker):
    def _fisher_loop(self, closure):
        logits = self._logits
        n_dims = logits.size(-1)
        for i in range(n_dims):
            closure(lambda: logits[:, i].sum(), retain_graph=i < n_dims - 1)


class FisherMCMSE(FisherMaker):
    def _fisher_loop(self, closure):
        logits = self._logits
        n_mc_samples = self.config.n_mc_samples
        var = self.config.var
        dist = torch.distributions.normal.Normal(logits, scale=np.sqrt(var))
        for i in range(n_mc_samples):
            with torch.no_grad():
                targets = dist.sample()
            closure(lambda: 0.5 * F.mse_loss(logits, targets, reduction='sum') / n_mc_samples,
                    retain_graph=i < n_mc_samples - 1)


class FisherEmp(FisherMaker):
    @property
    def is_fisher_emp(self):
        return True


def get_fisher_maker(model: nn.Module, config: FisherMakerConfig):
    fisher_type = config.fisher_type
    loss_type = config.loss_type
    assert fisher_type in _supported_types
    if fisher_type == FISHER_EMP:
        return FisherEmp(model, config)
    assert loss_type in [LOSS_CROSS_ENTROPY, LOSS_MSE]
    if fisher_type == FISHER_EXACT:
        if loss_type == LOSS_CROSS_ENTROPY:
            return FisherExactCrossEntropy(model, config)
        else:
            return FisherExactMSE(model, config)
    else:
        if loss_type == LOSS_CROSS_ENTROPY:
            return FisherMCCrossEntropy(model, config)
        else:
            return FisherMCMSE(model, config)


