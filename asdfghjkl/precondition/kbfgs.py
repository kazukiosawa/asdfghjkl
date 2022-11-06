from typing import Any, List
from dataclasses import dataclass
import math

import torch
import torch.nn as nn
from torch import Tensor

from ..core import extend, module_wise_assignments
from ..operations import OP_MEAN_INPUTS, OP_SPATIAL_MEAN_OUTPUTS, OP_SPATIAL_MEAN_OUTGRADS,\
    OP_OUT_SPATIAL_SIZE, OP_COV_KRON, OP_BFGS_KRON_S_AS, OperationContext
from ..utils import cholesky_inv
from ..symmatrix import SymMatrix
from .prec_grad_maker import PreconditionedGradientMaker, PreconditionedGradientConfig


_supported_modules = (nn.Linear, nn.Conv2d)


__all__ = ['KronBfgsGradientConfig', 'KronBfgsGradientMaker']


@dataclass
class KronBfgsGradientConfig(PreconditionedGradientConfig):
    data_size: int = 1
    damping: float = 1.e-5
    ema_decay: float = 0.1
    mu1: float = 0.2
    ignore_modules: List[Any] = None
    minibatch_hessian_action: bool = False
    bfgs_attr: str = 'bfgs'
    mean_outputs_attr: str = 'mean_outputs'
    mean_outgrads_attr: str = 'mean_outgrads'


class KronBfgsGradientMaker(PreconditionedGradientMaker):
    def __init__(self, model: nn.Module, config: KronBfgsGradientConfig):
        super().__init__(model, config)
        self.config: KronBfgsGradientConfig = config
        self.modules = [m for m in module_wise_assignments(model, ignore_modules=config.ignore_modules)
                        if isinstance(m, _supported_modules)]
        self._last_model_args = ()
        self._last_model_kwargs = dict()
        self._curr_model_args = ()
        self._curr_model_kwargs = dict()
        self._A_inv_exists = False

    def do_forward_and_backward(self, step=None):
        return not self.do_update_preconditioner(step)

    def _startup(self):
        step = self.state['step']
        if step > 0 and self.do_update_preconditioner(step - 1):
            self._post_preconditioner_update()

    def update_preconditioner(self):
        model = self.model
        config = self.config
        if config.minibatch_hessian_action and self._A_inv_exists:
            op_names = (OP_BFGS_KRON_S_AS, OP_SPATIAL_MEAN_OUTPUTS, OP_OUT_SPATIAL_SIZE)
        else:
            op_names = (OP_COV_KRON, OP_MEAN_INPUTS, OP_SPATIAL_MEAN_OUTPUTS, OP_OUT_SPATIAL_SIZE)
        op_names += (OP_SPATIAL_MEAN_OUTGRADS,)
        with extend(model, *op_names, ignore_modules=config.ignore_modules) as cxt:
            rst = self.forward()
            self._update_A_inv(cxt)
            self._store_mean(cxt, is_forward=True)
            self._loss.backward()
            self._store_mean(cxt, is_forward=False)
        self._record_model_args_kwargs()
        return rst

    def _post_preconditioner_update(self):
        self._restore_last_model_args_kwargs()
        # another forward and backward using the previous model_args, kwargs
        op_names = (OP_SPATIAL_MEAN_OUTPUTS, OP_SPATIAL_MEAN_OUTGRADS, OP_OUT_SPATIAL_SIZE)
        kwargs = dict(ignore_modules=self.config.ignore_modules)
        with extend(self.model, *op_names, **kwargs) as cxt:
            self.forward()
            self._loss.backward()
            self._update_B_inv(cxt)
        self._restore_curr_model_args_kwargs()

    def precondition(self, vec_weight: Tensor = None, vec_bias: Tensor = None):
        config = self.config
        for module in self.modules:
            matrix: SymMatrix = getattr(module, config.bfgs_attr)
            if vec_weight is None and module.weight.requires_grad:
                vec_weight = module.weight.grad
            assert vec_weight is not None, 'gradient has not been calculated.'
            if module.bias is not None and module.bias.requires_grad:
                vec_bias = module.bias.grad
                assert vec_bias is not None, 'gradient has not been calculated.'
            matrix.kron.mvp(vec_weight=vec_weight, vec_bias=vec_bias, use_inv=True, inplace=True)

    def _record_model_args_kwargs(self):
        self._last_model_args = self._model_args
        self._last_model_kwargs = self._model_kwargs.copy()

    def _restore_last_model_args_kwargs(self):
        self._curr_model_args = self._model_args
        self._curr_model_kwargs = self._model_kwargs.copy()
        self._model_args = self._last_model_args
        self._model_kwargs = self._last_model_kwargs.copy()

    def _restore_curr_model_args_kwargs(self):
        self._model_args = self._curr_model_args
        self._model_kwargs = self._curr_model_kwargs.copy()

    def _update_A_inv(self, cxt: OperationContext):
        config = self.config
        for module in self.modules:
            damping = self._get_damping(cxt, module, is_A=True)
            bfgs = getattr(module, config.bfgs_attr, None)
            if config.minibatch_hessian_action and self._A_inv_exists:
                s, As = cxt.bfgs_kron_s_As(module)
                y = As + damping * s
            else:
                new_bfgs = cxt.cov_symmatrix(module, pop=True).mul_(1/config.data_size)
                if bfgs is None:
                    setattr(module, config.bfgs_attr, new_bfgs)
                    bfgs = new_bfgs
                else:
                    # update the exponential moving average (EMA) of A
                    new_bfgs.mul_(config.ema_decay)
                    bfgs.mul_(1 - config.ema_decay)
                    bfgs += new_bfgs  # this must be __iadd__ to preserve inv
                A = bfgs.kron.A
                if bfgs.kron.A_inv is None:
                    bfgs.kron.A_inv = cholesky_inv(A, damping)
                mean_in_data = cxt.mean_in_data(module)
                s = torch.mv(bfgs.kron.A_inv, mean_in_data)
                y = torch.mv(A, s) + damping * s
            assert bfgs is not None, f'Matrix for {module} is not calculated yet.'
            H = bfgs.kron.A_inv
            bfgs_inv_update_(H, s, y)
        self._A_inv_exists = True

    def _store_mean(self, cxt: OperationContext, is_forward=True):
        config = self.config
        for module in self.modules:
            if is_forward:
                setattr(module, config.mean_outputs_attr, cxt.spatial_mean_out_data(module))
            else:
                setattr(module, config.mean_outgrads_attr, cxt.spatial_mean_out_grads(module))

    def _update_B_inv(self, cxt: OperationContext):
        config = self.config
        for module in self.modules:
            damping = self._get_damping(cxt, module, is_A=False)
            bfgs = getattr(module, config.bfgs_attr)
            s = cxt.spatial_mean_out_data(module) - getattr(module, config.mean_outputs_attr)
            y = cxt.spatial_mean_out_grads(module) - getattr(module, config.mean_outgrads_attr)
            if bfgs.kron.B_inv is None:
                bfgs.kron.B_inv = torch.eye(s.shape[0], device=s.device)
            H = bfgs.kron.B_inv
            if isinstance(module, nn.Conv2d):
                s = s.mean(dim=0)
                y = y.mean(dim=0)
            powell_lm_damping_(H, s, y, mu1=config.mu1, mu2=damping)
            bfgs_inv_update_(H, s, y)

    def _get_damping(self, cxt: OperationContext, module: nn.Module, is_A=True):
        damping = self.config.damping
        sqrt_damping = math.sqrt(damping)
        if isinstance(module, nn.Conv2d):
            spatial_size = cxt.out_spatial_size(module)
            sqrt_spatial_size = math.sqrt(spatial_size)
            if is_A:
                # for A
                return sqrt_damping * sqrt_spatial_size
            else:
                # for B
                return sqrt_damping / sqrt_spatial_size
        else:
            return sqrt_damping


def powell_lm_damping_(H: Tensor, s: Tensor, y: Tensor, mu1: float, mu2: float):
    assert 0 < mu1 < 1
    assert mu2 > 0
    Hy = torch.mv(H, y)
    ytHy = torch.dot(y, Hy)
    sty = torch.dot(s, y)
    if sty < mu1 * ytHy:
        theta = (1 - mu1) * ytHy / (ytHy - sty)
    else:
        theta = 1
    s.mul_(theta).sub_(Hy, alpha=1 - theta)  # Powell's damping on H
    y.add_(s, alpha=mu2)  # Levenberg-Marquardt damping on H^{-1}


def bfgs_inv_update_(H: Tensor, s: Tensor, y: Tensor):
    """
    The update of H=B^{-1} in BFGS by using the Sherman-Morrison formula explained in
    https://en.wikipedia.org/wiki/Broyden%E2%80%93Fletcher%E2%80%93Goldfarb%E2%80%93Shanno_algorithm
    """
    msg = f'H has to be a {Tensor} containing a symmetric matrix.'
    assert H.ndim == 2 and torch.all(H.T == H), msg
    d1, d2 = H.shape
    assert d1 == d2, msg
    msg = f' has to be a {Tensor} containing a vector of same dimension as H.'
    assert s.ndim == 1 and s.shape[0] == d1, 's' + msg
    assert y.ndim == 1 and y.shape[0] == d1, 'y' + msg

    sty = torch.dot(s, y)  # s^ty
    Hy = torch.mv(H, y)  # Hy
    Hyst = torch.outer(Hy, s)  # Hys^t
    ytHy = torch.dot(y, Hy)  # y^tHy
    sst = torch.outer(s, s)  # ss^t
    H.add_((sty + ytHy) @ sst / (sty ** 2))
    H.sub_((Hyst + Hyst.T) / sty)
