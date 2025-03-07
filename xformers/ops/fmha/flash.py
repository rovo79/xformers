# Copyright (c) Facebook, Inc. and its affiliates. All rights reserved.
#
# This source code is licensed under the BSD license found in the
# LICENSE file in the root directory of this source tree.


from dataclasses import replace
from itertools import zip_longest
from typing import Any, List, Optional, Set, Tuple, Union

import torch

from ..common import _get_storage_base, get_operator, register_operator
from .attn_bias import (
    AttentionBias,
    BlockDiagonalCausalFromBottomRightMask,
    BlockDiagonalCausalMask,
    BlockDiagonalMask,
    LowerTriangularMask,
)
from .common import (
    AttentionBwOpBase,
    AttentionFwOpBase,
    Context,
    Gradients,
    Inputs,
    check_lastdim_alignment_stride1,
)

FLASH_VERSION = "0.0.0"
try:
    try:
        from ... import _C_flashattention  # type: ignore[attr-defined]
        from ..._cpp_lib import _build_metadata

        if _build_metadata is not None:
            FLASH_VERSION = _build_metadata.flash_version
    except ImportError:
        import flash_attn
        from flash_attn.flash_attn_interface import flash_attn_cuda as _C_flashattention

        FLASH_VERSION = flash_attn.__version__

    # create library so that flash-attn goes through the PyTorch Dispatcher
    _flash_lib = torch.library.Library("xformers_flash", "DEF")

    _flash_lib.define(
        "flash_fwd(Tensor query, Tensor key, Tensor value, "
        "Tensor cu_seqlens_q, Tensor cu_seqlens_k, "
        "int max_seqlen_q, int max_seqlen_k, "
        "float p, float softmax_scale, "
        "bool is_causal, bool return_softmax) -> (Tensor, Tensor, Tensor)"
    )

    _flash_lib.define(
        "flash_bwd(Tensor dout, Tensor query, Tensor key, Tensor value, "
        "Tensor out, Tensor softmax_lse_, Tensor dq, Tensor dk, Tensor dv, "
        "Tensor cu_seqlens_q, Tensor cu_seqlens_k, "
        "int max_seqlen_q, int max_seqlen_k, "
        "float p, float softmax_scale, bool is_causal, Tensor rng_state) -> Tensor"
    )

    def _flash_fwd(
        query,
        key,
        value,
        cu_seq_lens_q,
        cu_seq_lens_k,
        max_seq_len_q,
        max_seq_len_k,
        p,
        softmax_scale,
        causal,
        return_softmax,
    ):
        out = query.new_empty(query.shape[0], query.shape[1], value.shape[2])
        (
            out,
            q_padded,
            k_padded,
            v_padded,
            out_padded,
            softmax_lse,
            p,
            rng_state,
        ) = _C_flashattention.varlen_fwd(
            query,
            key,
            value,
            out,
            cu_seq_lens_q,
            cu_seq_lens_k,
            max_seq_len_q,
            max_seq_len_k,
            p,
            softmax_scale,
            False,
            causal,
            return_softmax,
            None,
        )
        return out, softmax_lse, rng_state

    def _flash_bwd(
        grad,
        query,
        key,
        value,
        out,
        lse,
        dq,
        dk,
        dv,
        cu_seq_lens_q,
        cu_seq_lens_k,
        max_seq_len_q,
        max_seq_len_k,
        p,
        softmax_scale,
        causal,
        rng_state,
    ):
        _C_flashattention.varlen_bwd(
            grad,
            query,
            key,
            value,
            out,
            lse,
            dq,
            dk,
            dv,
            cu_seq_lens_q,
            cu_seq_lens_k,
            max_seq_len_q,
            max_seq_len_k,
            p,
            softmax_scale,
            False,  # zero_tensors
            causal,
            None,
            rng_state,
        )
        return dq

    _flash_lib.impl("flash_fwd", _flash_fwd, "CUDA")
    _flash_lib.impl("flash_bwd", _flash_bwd, "CUDA")
except ImportError:
    pass


def _convert_input_format(
    inp: Inputs,
) -> Tuple[Inputs, float, torch.Tensor, int, torch.Tensor, int]:
    query, key, value = inp.query, inp.key, inp.value
    batch = query.shape[0]
    seqlen_q = query.shape[1]
    seqlen_kv = key.shape[1]
    head_dim_q = query.shape[-1]
    head_dim_v = value.shape[-1]

    attn_bias = inp.attn_bias
    if isinstance(attn_bias, BlockDiagonalMask):
        attn_bias.k_seqinfo.seqstart = attn_bias.k_seqinfo.seqstart.to(
            inp.query.device, non_blocking=True
        )
        attn_bias.q_seqinfo.seqstart = attn_bias.q_seqinfo.seqstart.to(
            inp.query.device, non_blocking=True
        )

        cu_seqlen_k = attn_bias.k_seqinfo.seqstart
        cu_seqlen_q = attn_bias.q_seqinfo.seqstart
        max_seqlen_q = attn_bias.q_seqinfo.max_seqlen
        max_seqlen_k = attn_bias.k_seqinfo.max_seqlen
    else:
        cu_seqlen_k = torch.arange(
            0,
            (batch + 1) * seqlen_kv,
            step=seqlen_kv,
            dtype=torch.int32,
            device=query.device,
        )
        if seqlen_q == seqlen_kv:
            cu_seqlen_q = cu_seqlen_k
        else:
            cu_seqlen_q = torch.arange(
                0,
                (batch + 1) * seqlen_q,
                step=seqlen_q,
                dtype=torch.int32,
                device=query.device,
            )
        max_seqlen_q = seqlen_q
        max_seqlen_k = seqlen_kv

    if query.ndim == 5:  # QGA
        # Fold the group/head_in_group dimensions together
        def fold(x):
            # Either the head is replicated
            if x.stride(3) == 0:
                return x[:, :, :, 0]
            # Or we reshape
            return x.reshape(
                [
                    x.shape[0],
                    x.shape[1],
                    -1,
                    x.shape[4],
                ]
            )

        query = fold(query)
        key = fold(key)
        value = fold(value)
    # Initially we have `query.shape = [batch, seqlen, head_dim_q]`
    # We want format `[batch * seqlen, num_heads, head_dim_q]`
    new_inp = replace(
        inp,
        query=query.reshape([batch * seqlen_q, -1, head_dim_q]),
        key=key.reshape([batch * seqlen_kv, -1, head_dim_q]),
        value=value.reshape([batch * seqlen_kv, -1, head_dim_v]),
    )
    softmax_scale = inp.query.shape[-1] ** (-0.5) if inp.scale is None else inp.scale
    return new_inp, softmax_scale, cu_seqlen_q, max_seqlen_q, cu_seqlen_k, max_seqlen_k


def _is_causal(attn_bias: Optional[Union[torch.Tensor, AttentionBias]]) -> bool:
    return isinstance(
        attn_bias,
        (
            LowerTriangularMask,
            BlockDiagonalCausalMask,
            BlockDiagonalCausalFromBottomRightMask,
        ),
    )


def _check_needs_no_topleft(d: Inputs, reasons: List[str]) -> None:
    # Flash does not support TopLeft, so only allow causal masks with TopLeft
    # if each batch element has equal number of queries and keys.
    if isinstance(d.attn_bias, BlockDiagonalCausalMask):
        # Flash does not support TopLeft, so only allow BlockDiagonalCausalMask
        # if each batch element has equal number of queries and keys.
        for k_start, q_start in zip_longest(
            d.attn_bias.k_seqinfo.seqstart_py, d.attn_bias.q_seqinfo.seqstart_py
        ):
            if k_start != q_start:
                reasons.append(
                    "Only support BlockDiagonalCausalMask if equal"
                    " numbers of keys and queries"
                )
                break
    elif isinstance(d.attn_bias, LowerTriangularMask):
        if d.query.shape[1] != d.key.shape[1]:
            reasons.append(
                "Only support LowerTriangularMask if equal number of" "keys and queries"
            )


def _check_strides_for_bmghk(x: torch.Tensor, name: str, reasons: List[str]) -> None:
    """
    We want to be able to collapse the G/H dimensions together
    """
    if x.ndim == 5:
        stride_g, stride_h = x.stride(2), x.stride(3)
        if x.shape[2] == 1:
            return
        if x.shape[3] == 1 or stride_h == 0:
            return
        if stride_g != stride_h * x.shape[-2]:
            reasons.append(
                f"GQA is only supported when the G/H dimensions are contiguous"
                f"    {name}.stride:  {x.stride()}"
                f"    {name}.shape :  {list(x.shape)}"
            )


@register_operator
class FwOp(AttentionFwOpBase):
    """Operator that computes memory-efficient attention using \
        `Flash-Attention <https://github.com/HazyResearch/flash-attention>`_ \
        implementation.
    """

    OPERATOR = get_operator("xformers_flash", "flash_fwd")
    SUPPORTED_DEVICES: Set[str] = {"cuda"}
    CUDA_MINIMUM_COMPUTE_CAPABILITY = (8, 0)
    SUPPORTED_DTYPES: Set[torch.dtype] = {torch.half, torch.bfloat16}
    SUPPORTED_MAX_K = 256
    SUPPORTED_ATTN_BIAS_TYPES: Set[Any] = {
        type(None),
        LowerTriangularMask,
        BlockDiagonalMask,
        BlockDiagonalCausalMask,
        BlockDiagonalCausalFromBottomRightMask,
    }
    SUPPORTS_DROPOUT = True
    SUPPORTS_CUSTOM_SCALE = True
    SUPPORTS_DIFFERENT_VALUE_EMBED = False
    SUPPORTS_BMGHK = True
    NAME = f"flshattF@{FLASH_VERSION}"
    VERSION = FLASH_VERSION

    @classmethod
    def not_supported_reasons(cls, d: Inputs) -> List[str]:
        reasons = super(FwOp, cls).not_supported_reasons(d)
        check_lastdim_alignment_stride1(reasons, "query", d.query, 8)
        _check_needs_no_topleft(d, reasons)
        _check_strides_for_bmghk(d.query, "query", reasons)
        _check_strides_for_bmghk(d.key, "key", reasons)
        _check_strides_for_bmghk(d.value, "value", reasons)
        return reasons

    @classmethod
    def apply(
        cls, inp: Inputs, needs_gradient: bool
    ) -> Tuple[torch.Tensor, Optional[Context]]:
        return_softmax = False
        out_shape = [
            *inp.query.shape[:-1],
            inp.value.shape[-1],
        ]
        (
            inp,
            softmax_scale,
            cu_seqlens_q,
            max_seqlen_q,
            cu_seqlens_k,
            max_seqlen_k,
        ) = _convert_input_format(inp)
        out, softmax_lse, rng_state = cls.OPERATOR(
            inp.query,
            inp.key,
            inp.value,
            cu_seqlens_q,
            cu_seqlens_k,
            max_seqlen_q,
            max_seqlen_k,
            inp.p,
            softmax_scale,
            _is_causal(inp.attn_bias),
            return_softmax,
        )

        out = out.reshape(out_shape)
        ctx = Context(out=out, lse=softmax_lse)
        if inp.p != 0.0:
            ctx.op_bw = BwOp
            ctx.rng_state = rng_state
        return (out, ctx)

    @classmethod
    # type: ignore
    def operator_flop(
        cls,
        query,
        key,
        value,
        cu_seq_lens_q,
        cu_seq_lens_k,
        max_seq_len_q,
        max_seq_len_k,
        p,
        softmax_scale,
        causal,
        return_softmax,
    ) -> int:
        return cls.attn_operator_flop(
            query.unsqueeze(0),
            key.unsqueeze(0),
            value.unsqueeze(0),
            causal=causal,
            seqstart_k=cu_seq_lens_k,
            seqstart_q=cu_seq_lens_q,
        )


@register_operator
class BwOp(AttentionBwOpBase):
    __doc__ = FwOp.__doc__

    OPERATOR = get_operator("xformers_flash", "flash_bwd")
    SUPPORTED_DEVICES = FwOp.SUPPORTED_DEVICES
    CUDA_MINIMUM_COMPUTE_CAPABILITY = FwOp.CUDA_MINIMUM_COMPUTE_CAPABILITY
    SUPPORTED_DTYPES = FwOp.SUPPORTED_DTYPES
    SUPPORTED_MAX_K = FwOp.SUPPORTED_MAX_K
    SUPPORTED_ATTN_BIAS_TYPES = FwOp.SUPPORTED_ATTN_BIAS_TYPES
    SUPPORTS_DROPOUT = FwOp.SUPPORTS_DROPOUT
    SUPPORTS_CUSTOM_SCALE = FwOp.SUPPORTS_CUSTOM_SCALE
    SUPPORTS_DIFFERENT_VALUE_EMBED = FwOp.SUPPORTS_DIFFERENT_VALUE_EMBED
    IS_DETERMINISTIC = False
    SUPPORTS_BMGHK = False  # NOTE: Don't forget to update fmha doc when changing this!
    NAME = f"flshattB@{FLASH_VERSION}"
    VERSION = FLASH_VERSION

    MAX_HEADDIM_SM8x = 192

    @classmethod
    def shape_not_supported_reasons(
        cls, Mq: int, Mkv: int, K: int, Kv: int
    ) -> List[str]:
        reasons = super().shape_not_supported_reasons(Mq, Mkv, K, Kv)

        # In fbcode in mode/dev-nosan, we get nans from flash v2.1 if there
        # is a strange embedding dimension.
        if K not in {8, 16, 32, 64, 128, 256}:
            reasons.append(f"Embed dim {K} not supported")

        return reasons

    @classmethod
    def not_supported_reasons(cls, d: Inputs) -> List[str]:
        reasons = super(BwOp, cls).not_supported_reasons(d)
        check_lastdim_alignment_stride1(reasons, "query", d.query, 8)
        _check_needs_no_topleft(d, reasons)
        if d.device.type == "cuda":
            # Due to limited shared-memory, some GPUs are limited in head dimension
            device_capability = torch.cuda.get_device_capability(d.device)
            is_sm80_or_sm90 = device_capability in [(8, 0), (9, 0)]
            if (
                max(d.key.shape[-1], d.query.shape[-1]) > cls.MAX_HEADDIM_SM8x
                and not is_sm80_or_sm90
            ):
                reasons.append(
                    "requires a GPU with compute capability 8.0 "
                    f"(A100) or 9.0 (H100) for 'query.shape[-1] > {cls.MAX_HEADDIM_SM8x}'"
                )
        return reasons

    @classmethod
    def apply(cls, ctx: Context, inp: Inputs, grad: torch.Tensor) -> Gradients:
        dq_shape, dk_shape, dv_shape = inp.query.shape, inp.key.shape, inp.value.shape
        (
            inp,
            softmax_scale,
            cu_seqlens_q,
            max_seqlen_q,
            cu_seqlens_k,
            max_seqlen_k,
        ) = _convert_input_format(inp)
        assert ctx.lse.is_contiguous
        ctx_lse = ctx.lse
        assert ctx_lse.shape[2] >= max_seqlen_q
        if max_seqlen_q != ctx_lse.shape[2]:
            ctx_lse = ctx_lse[:, :, :max_seqlen_q].contiguous()
        kernel_out_shape = [
            inp.query.shape[0],
            inp.query.shape[1],
            inp.value.shape[2],
        ]

        # Create dq,dk,dv
        # If Q/K/V come from a single QKV tensor, let's put the gradient in the
        # right strides, so we can avoid a `cat`
        if (
            inp.query.shape[0] == inp.key.shape[0]
            and inp.query.shape[2] == inp.value.shape[2]
            and _get_storage_base(inp.query) == _get_storage_base(inp.key)
            and _get_storage_base(inp.query) == _get_storage_base(inp.value)
        ):
            # Create one big contiguous chunk
            # This is because q, k and v usually come from a single
            # output of a linear layer that is chunked.
            # Creating the gradients with the right layout saves us
            # a `torch.cat` call in the backward pass
            chunk = torch.empty(
                (inp.query.shape[0], 3, inp.query.shape[1], inp.query.shape[2]),
                dtype=inp.query.dtype,
                device=inp.device,
            )
            grads = Gradients(
                dq=chunk.select(1, 0),
                dk=chunk.select(1, 1),
                dv=chunk.select(1, 2),
            )
        else:
            grads = Gradients(
                dq=torch.empty_like(inp.query),
                dk=torch.empty_like(inp.key),
                dv=torch.empty_like(inp.value),
            )

        assert grad.dtype in cls.SUPPORTED_DTYPES
        cls.OPERATOR(
            grad.reshape(kernel_out_shape).contiguous(),
            inp.query,
            inp.key,
            inp.value,
            ctx.out.reshape(kernel_out_shape),
            ctx_lse,
            grads.dq,
            grads.dk,
            grads.dv,
            cu_seqlens_q,
            cu_seqlens_k,
            max_seqlen_q,
            max_seqlen_k,
            inp.p,
            softmax_scale,
            _is_causal(inp.attn_bias),
            ctx.rng_state,
        )
        grads.dq = grads.dq.reshape(dq_shape)
        grads.dk = grads.dk.reshape(dk_shape)
        grads.dv = grads.dv.reshape(dv_shape)
        return grads

    @classmethod
    # type: ignore
    def operator_flop(
        cls,
        grad,
        query,
        key,
        value,
        out,
        lse,
        dq,
        dk,
        dv,
        cu_seq_lens_q,
        cu_seq_lens_k,
        max_seq_len_q,
        max_seq_len_k,
        p,
        softmax_scale,
        causal,
    ) -> int:
        return cls.attn_operator_flop(
            query.unsqueeze(0),
            key.unsqueeze(0),
            value.unsqueeze(0),
            causal=causal,
            seqstart_k=cu_seq_lens_k,
            seqstart_q=cu_seq_lens_q,
        )
