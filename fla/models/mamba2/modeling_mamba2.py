# Copyright 2024 state-spaces/mamba2 org and HuggingFace Inc. team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""PyTorch MAMBA2 model."""

import math
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple, Union

import torch
import torch.utils.checkpoint
from fla.models.mamba2.configuration_mamba2 import Mamba2Config
from fla.modules import FusedCrossEntropyLoss, FusedRMSNormSwishGate, RMSNorm
from torch import nn
from transformers.activations import ACT2FN
from transformers.modeling_utils import PreTrainedModel
from transformers.utils import ModelOutput, logging

logger = logging.get_logger(__name__)

try:
    from mamba_ssm.ops.triton.selective_state_update import selective_state_update
    from mamba_ssm.ops.triton.ssd_combined import (
        mamba_chunk_scan_combined,
        mamba_split_conv1d_scan_combined,
    )
except ImportError:
    (
        selective_state_update,
        mamba_chunk_scan_combined,
        mamba_split_conv1d_scan_combined,
    ) = (None, None, None)

try:
    from causal_conv1d import causal_conv1d_fn, causal_conv1d_update
except ImportError:
    causal_conv1d_update, causal_conv1d_fn = None, None

is_fast_path_available = all(
    (selective_state_update, causal_conv1d_fn, causal_conv1d_update)
)


def pad_by_size(x, pad_size):
    """
    Padding x tensor with `pad_size` on the seq_len dim (dim=1)

    Assumes that we only have tensors of either size 4 or 3
    """
    assert 2 < len(x.shape) < 5

    pad_shape = (
        (0, 0, 0, 0, 0, pad_size, 0, 0)
        if len(x.shape) == 4
        else (0, 0, 0, pad_size, 0, 0)
    )

    return torch.nn.functional.pad(x, pad_shape, mode="constant", value=0)


def reshape_into_chunks(x, pad_size, chunk_size):
    """
    Padding x tensor with `pad_size` on the seq_len dim (dim=1) and
    simultaneously splitting it into chunk sequences.

    Assumes that we only have tensors of either size 4 or 3
    """
    # [bsz, seq_len, ...] -> [bsz, seq_len multiple of chunk_size, ...]
    x = pad_by_size(x, pad_size)

    if len(x.shape) == 3:
        # b (l c) h -> b l c h with c=chunk_size
        # [bsz, seq_len multiple of chunk_size, num_heads] -> [bsz, -1, chunk_size, num_heads]
        return x.reshape(x.shape[0], -1, chunk_size, x.shape[2])
    else:
        # b (l c) h p -> b l c h p with c=chunk_size
        # [bsz, seq_len multiple of chunk_size, num_heads, head_dim or state_size] -> [bsz, -1, chunk_size, num_heads, head_dim or state_size]
        return x.reshape(x.shape[0], -1, chunk_size, x.shape[2], x.shape[3])


def segsum(x):
    """
    More stable segment sum calculation
    """
    T = x.size(-1)
    # [..., chunk_size] -> [..., chunk_size, chunk_size]
    x = x.unsqueeze(-1).expand(*x.size(), T)
    mask = torch.tril(torch.ones(T, T, device=x.device, dtype=torch.bool), diagonal=-1)
    x = x.masked_fill(~mask, 0)
    x_segsum = torch.cumsum(x, dim=-2)
    mask = torch.tril(torch.ones(T, T, device=x.device, dtype=torch.bool), diagonal=0)
    x_segsum = x_segsum.masked_fill(~mask, -torch.inf)
    return x_segsum


class Mamba2Cache:
    """
    Arguments:
        config: Mamba2Config
        batch_size: int
        dtype: torch.dtype
        device: torch.device

    Attributes:
        seqlen_offset: int
        dtype: torch.dtype
        conv_states: Dict[int, torch.Tensor] # layer_idx -> [batch_size, intermediate_size, conv_kernel_size]
        ssm_states: Dict[int, torch.Tensor] # layer_idx -> [batch_size, intermediate_size, ssm_state_size]
    """

    def __init__(
        self,
        config: Mamba2Config,
        batch_size: int,
        dtype: torch.dtype = torch.float16,
        device: Optional[str] = None,
    ):
        self.seqlen_offset = 0
        self.dtype = dtype
        self.conv_kernel_size = config.conv_kernel
        self.intermediate_size = int(config.expand * config.hidden_size)

        self.conv_states = {
            i: torch.zeros(
                batch_size,
                self.intermediate_size + 2 * config.n_groups * config.state_size,
                self.conv_kernel_size,
                device=device,
                dtype=dtype,
            )
            for i in range(config.num_hidden_layers)
        }
        self.ssm_states = {
            i: torch.zeros(
                batch_size,
                config.num_heads,
                config.head_dim,
                config.state_size,
                device=device,
                dtype=dtype,
            )
            for i in range(config.num_hidden_layers)
        }
        self.activation = config.hidden_act
        self.act = ACT2FN[config.hidden_act]

    def update_conv_state(
        self,
        layer_idx: int,
        new_conv_state: torch.Tensor,
        cache_position: torch.LongTensor,
    ) -> torch.Tensor:
        conv_state = self.conv_states[layer_idx]
        cache_position = cache_position.clamp(0, self.conv_kernel_size - 1)

        conv_state = conv_state.roll(shifts=-1, dims=-1)
        conv_state[:, :, cache_position] = new_conv_state.to(conv_state.device)
        self.conv_states[layer_idx].zero_()
        self.conv_states[layer_idx] += conv_state
        return self.conv_states[layer_idx]

    def reset(self):
        self.conv_states.zero_()
        self.ssm_states.zero_()


class Mamba2Mixer(nn.Module):
    """
    Compute ∆, A, B, C, and D the state space parameters and compute the `contextualized_states`.
    A, D are input independent (see Mamba paper [1] Section 3.5.2 "Interpretation of A" for why A isn't selective)
    ∆, B, C are input-dependent (this is a key difference between Mamba and the linear time invariant S4,
    and is why Mamba is called **selective** state spaces)
    """

    def __init__(self, config: Mamba2Config, layer_idx: int):
        super().__init__()
        self.num_heads = config.num_heads
        self.hidden_size = config.hidden_size
        self.ssm_state_size = config.state_size
        self.conv_kernel_size = config.conv_kernel
        self.intermediate_size = int(config.expand * self.hidden_size)
        self.time_step_rank = int(config.time_step_rank)
        self.layer_idx = layer_idx
        self.use_conv_bias = config.use_conv_bias
        self.activation = config.hidden_act
        self.act = ACT2FN[config.hidden_act]

        self.norm_before_gate = config.norm_before_gate
        self.layer_norm_epsilon = config.layer_norm_epsilon
        self.rms_norm = config.rms_norm

        self.n_groups = config.n_groups
        self.head_dim = config.head_dim
        self.chunk_size = config.chunk_size

        self.time_step_limit = config.time_step_limit
        self.time_step_min = config.time_step_min
        self.time_step_max = config.time_step_max

        self.conv_dim = self.intermediate_size + 2 * self.n_groups * self.ssm_state_size
        self.conv1d = nn.Conv1d(
            in_channels=self.conv_dim,
            out_channels=self.conv_dim,
            bias=config.use_conv_bias,
            kernel_size=config.conv_kernel,
            groups=self.conv_dim,
            padding=config.conv_kernel - 1,
        )

        # projection of the input hidden states
        self.in_proj = nn.Linear(
            self.hidden_size,
            2 * self.intermediate_size
            + 2 * self.n_groups * self.ssm_state_size
            + self.num_heads,
            bias=config.use_bias,
        )
        # selective projection used to make dt, B and C input dependant

        # time step projection (discretization)
        # instantiate once and copy inv_dt in init_weights of PretrainedModel
        self.dt_bias = nn.Parameter(torch.ones(self.num_heads))

        # S4D real initialization. These are not discretized!
        # The core is to load them, compute the discrete states, then write the updated state. Keeps the memory bounded
        A = torch.arange(1, self.num_heads + 1)
        self.A_log = nn.Parameter(torch.log(A))
        self.A_log._no_weight_decay = True
        self.norm = FusedRMSNormSwishGate(
            self.intermediate_size, eps=self.layer_norm_epsilon
        )

        self.D_has_hdim = False
        self.D = nn.Parameter(
            torch.ones(self.ssm_state_size if self.D_has_hdim else self.num_heads)
        )
        self.D._no_weight_decay = True

        self.out_proj = nn.Linear(
            self.intermediate_size, self.hidden_size, bias=config.use_bias
        )
        self.use_bias = config.use_bias

        if not is_fast_path_available:
            logger.warning_once(
                "The fast path is not available because on of `(selective_state_update, causal_conv1d_fn, causal_conv1d_update)`"
                " is None. Falling back to the naive implementation. To install follow https://github.com/state-spaces/mamba/#installation and"
                " https://github.com/Dao-AILab/causal-conv1d"
            )

    def cuda_kernels_forward(
        self,
        hidden_states: torch.Tensor,
        cache_params: Optional[Mamba2Cache] = None,
        cache_position: Optional[torch.LongTensor] = None,
        attention_mask: Optional[torch.Tensor] = None,
    ):
        seq_len = hidden_states.shape[1]
        # getting projected states from cache if it exists
        if cache_params is not None and cache_params.seqlen_offset > 0:
            batch_size = hidden_states.shape[0]
            zxbcdt = self.in_proj(hidden_states.squeeze(1))  # (B 2D)
            d_mlp = (
                zxbcdt.shape[-1]
                - 2 * self.intermediate_size
                - 2 * self.n_groups * self.ssm_state_size
                - self.num_heads
            ) // 2

            z0, x0, gate, xBC, dt = torch.split(
                zxbcdt,
                [
                    d_mlp,
                    d_mlp,
                    self.intermediate_size,
                    self.intermediate_size + 2 * self.n_groups * self.ssm_state_size,
                    self.num_heads,
                ],
                dim=-1,
            )

            xBC = causal_conv1d_update(
                xBC,
                cache_params.conv_states[self.layer_idx],
                self.conv1d.weight.squeeze(1),
                self.conv1d.bias,
                self.activation,
            )

            hidden_states, B, C = torch.split(
                xBC,
                [
                    self.intermediate_size,
                    self.n_groups * self.ssm_state_size,
                    self.n_groups * self.ssm_state_size,
                ],
                dim=-1,
            )
            A = -torch.exp(self.A_log.float())  # (nheads,)

            A = (
                A.unsqueeze(1)
                .unsqueeze(2)
                .expand(-1, self.head_dim, self.ssm_state_size)
                .to(dtype=torch.float32)
            )
            dt = dt.unsqueeze(2).expand(-1, -1, self.head_dim)
            dt_bias = self.dt_bias.unsqueeze(1).expand(-1, self.head_dim)
            D = self.D.unsqueeze(1).expand(
                -1, self.head_dim
            )  # repeat(self.D, "h -> h p", p=self.head_dim)
            B = B.view(B.shape[0], self.n_groups, B.shape[1] // self.n_groups)
            C = C.view(C.shape[0], self.n_groups, C.shape[1] // self.n_groups)
            hidden_states_reshaped = hidden_states.view(
                batch_size, self.num_heads, self.head_dim
            )
            if not self.rms_norm:
                gate = gate.view(batch_size, self.intermediate_size, self.head_dim)
                # gate = rearrange(gate, "b (h p) -> b h p", p=self.head_dim)
            hidden_states = selective_state_update(
                cache_params.ssm_states[self.layer_idx],
                hidden_states_reshaped,
                dt,
                A,
                B,
                C,
                D,
                z=gate if not self.rms_norm else None,
                dt_bias=dt_bias,
                dt_softplus=True,
            )
            hidden_states = hidden_states.view(
                batch_size, self.num_heads * self.head_dim
            )
            if self.rms_norm:
                hidden_states = self.norm(hidden_states, o=gate)
            if d_mlp > 0:
                hidden_states = torch.cat(
                    [torch.nn.functional.silu(z0) * x0, hidden_states], dim=-1
                )

            out = self.out_proj(hidden_states).unsqueeze(1)
        # if no cache is found, calling the kernel
        else:
            # 1. Gated MLP's linear projection
            projected_states = self.in_proj(hidden_states)
            A = -torch.exp(
                self.A_log.float()
            )  # (num_heads) or (intermediate_size, state_size)
            dt_limit_kwargs = (
                {}
                if self.time_step_limit == (0.0, float("inf"))
                else {"dt_limit": self.time_step_limit}
            )

            if self.training and cache_params is None:
                out, ssm_state = mamba_split_conv1d_scan_combined(
                    projected_states,
                    self.conv1d.weight.squeeze(1),
                    self.conv1d.bias,
                    self.dt_bias,
                    A,
                    D=self.D.view(-1, self.head_dim) if self.D_has_hdim else self.D,
                    chunk_size=self.chunk_size,
                    seq_idx=None,  # was seq_idx
                    activation=self.activation,
                    rmsnorm_weight=self.norm.weight,
                    rmsnorm_eps=self.norm.eps,
                    outproj_weight=self.out_proj.weight,
                    outproj_bias=self.out_proj.bias,
                    headdim=None if self.D_has_hdim else self.head_dim,
                    ngroups=self.n_groups,
                    norm_before_gate=self.norm_before_gate,
                    return_final_states=True,
                    **dt_limit_kwargs,
                )

            else:
                gate, xBC, time_step = torch.split(
                    projected_states,
                    [self.intermediate_size, self.conv_dim, self.num_heads],
                    dim=-1,
                )
                if (
                    attention_mask is not None
                    and attention_mask.shape[1] > 1
                    and attention_mask.shape[0] > 1
                ):
                    # tune out hidden states for pad tokens, see https://github.com/state-spaces/mamba/issues/66
                    # bug in generate tests?
                    dtype = hidden_states.dtype
                    hidden_states = (hidden_states * attention_mask.unsqueeze(2)).to(
                        dtype
                    )
                time_step = nn.functional.softplus(time_step + self.dt_bias)
                # 1D Convolution
                if causal_conv1d_fn is None or self.activation not in ["silu", "swish"]:
                    xBC = self.act(
                        self.conv1d(xBC.transpose(1, 2)).transpose(1, 2)[:, :seq_len]
                    )  # (B, L, self.d_inner + 2 * ngroups * d_state)
                else:
                    xBC = causal_conv1d_fn(
                        x=xBC.transpose(1, 2),
                        weight=self.conv1d.weight.squeeze(1),
                        bias=self.conv1d.bias,
                        activation=self.activation,
                    ).transpose(1, 2)[:, :seq_len]
                hidden_states, B, C = torch.split(
                    xBC,
                    [
                        self.intermediate_size,
                        self.n_groups * self.ssm_state_size,
                        self.n_groups * self.ssm_state_size,
                    ],
                    dim=-1,
                )

                if (
                    attention_mask is not None
                    and attention_mask.shape[1] > 1
                    and attention_mask.shape[0] > 1
                ):
                    # tune out hidden states for pad tokens, see https://github.com/state-spaces/mamba/issues/66
                    dtype = hidden_states.dtype
                    hidden_states = (hidden_states * attention_mask.unsqueeze(2)).to(
                        dtype
                    )
                scan_output, ssm_state = mamba_chunk_scan_combined(
                    hidden_states.view(
                        hidden_states.shape[0],
                        hidden_states.shape[1],
                        -1,
                        self.head_dim,
                    ),
                    time_step,
                    A,
                    B.view(B.shape[0], B.shape[1], self.n_groups, -1),
                    C.view(B.shape[0], C.shape[1], self.n_groups, -1),
                    chunk_size=self.chunk_size,
                    D=self.D,
                    z=None,
                    seq_idx=None,
                    return_final_states=True,
                    **dt_limit_kwargs,
                )
                if ssm_state is not None and cache_params is not None:
                    cache_params.ssm_states[self.layer_idx].copy_(ssm_state)
                scan_output = scan_output.view(
                    scan_output.shape[0], scan_output.shape[1], -1
                )
                # Multiply "gate" branch and apply extra normalization layer
                scan_output = self.norm(scan_output, o=gate)
                out = self.out_proj(scan_output)
        return out

    # fmt: off
    def torch_forward(self, input_states, cache_params: Optional[Mamba2Cache] = None,
                       cache_position: Optional[torch.LongTensor] = None,
                       attention_mask: Optional[torch.Tensor] = None):
        batch_size, seq_len, _ = input_states.shape
        dtype = input_states.dtype
        # Gated MLP's linear projection
        projected_states = self.in_proj(input_states.squeeze(1))
        d_mlp = (projected_states.shape[-1] - 2 * self.intermediate_size - 2
                  * self.n_groups * self.ssm_state_size - self.num_heads) // 2
        # z0 and x0 are empty tensors
        z0, x0, gate, hidden_states, dt = projected_states.split(
            [d_mlp, d_mlp, self.intermediate_size, self.conv_dim, self.num_heads], dim=-1
        )

        # Convolution sequence transformation
        if cache_params is not None:
            ssm_state = cache_params.ssm_states[self.layer_idx].clone()
            ssm_state = ssm_state.to(x0.device)
            if cache_params.seqlen_offset > 0:
                conv_state = cache_params.conv_states[self.layer_idx]  # [batch, intermediate_size, conv_kernel_size]
                conv_state = torch.roll(conv_state, shifts=-1, dims=-1)
                # handle batched generation - states are copied through
                conv_state[:, :, -1] = hidden_states[:, 0, :] if hidden_states.ndim == 3 else hidden_states
                cache_params.conv_states[self.layer_idx].copy_(conv_state)
                hidden_states = torch.sum(conv_state.to(projected_states.device)
                                           * self.conv1d.weight[:, 0, :], dim=-1)
                if self.use_conv_bias:
                    hidden_states += self.conv1d.bias
                hidden_states = self.act(hidden_states).to(dtype).unsqueeze(1)  # [batch, 1, intermediate_size] : decoding
            else:
                hidden_states = hidden_states.transpose(1, 2)
                conv_state = nn.functional.pad(
                    hidden_states,
                    (self.conv_kernel_size - hidden_states.shape[-1], 0)
                )
                cache_params.conv_states[self.layer_idx].copy_(conv_state)
                hidden_states = self.act(self.conv1d(
                    hidden_states).transpose(1, 2))[:, :seq_len, :]  # [batch, intermediate_size, seq_len]
        else:
            ssm_state = torch.zeros(
                (batch_size, self.num_heads, self.head_dim, self.ssm_state_size),
                device=hidden_states.device, dtype=dtype
            )
            hidden_states = self.act(self.conv1d(hidden_states.transpose(1, 2))[..., :seq_len].transpose(1, 2))
        hidden_states, B, C = torch.split(hidden_states, [self.intermediate_size, self.n_groups * self.ssm_state_size,
                                                           self.n_groups * self.ssm_state_size], dim=-1)
        A = -torch.exp(self.A_log.float())                            # [num_heads]
        if cache_params is not None and cache_params.seqlen_offset > 0:
            assert attention_mask.shape[-1] == 1
            # Note: there is no need to pad parameter matrices here, as there is just one new token
            # for batched generation
            dt = dt.unsqueeze(1) if dt.ndim == 2 else dt[:, 0, :].unsqueeze(1)
            dt = dt.transpose(1, 2).expand(dt.shape[0], dt.shape[-1], self.head_dim)
            # [num_heads] -> [num_heads, head_dim]
            dt_bias = self.dt_bias.unsqueeze(-1).expand(self.dt_bias.shape[0], self.head_dim)

            dt = torch.nn.functional.softplus(dt + dt_bias.to(dt.dtype))
            dt = torch.clamp(dt, self.time_step_min)  # , self.time_step_max)
            A = A[..., None, None].expand(A.shape[0], self.head_dim,
                                          self.ssm_state_size).to(dtype=torch.float32)
            # [bsz, num_heads, head_dim, state_size]
            dA = torch.exp(dt.unsqueeze(-1) * A)

            # Discretize B
            # [bsz, n_groups * state_size] -> [bsz, n_groups, 1, state_size] ->
            # -> [bsz, n_groups, group to head repetition factor, state_size] -> [bsz, num_heads, state_size]
            B = B.reshape(B.shape[0], self.n_groups, -1).unsqueeze(-2)
            B = B.expand(B.shape[0], B.shape[1], self.num_heads
                         // self.n_groups, B.shape[-1]).contiguous()
            B = B.reshape(B.shape[0], -1, B.shape[-1])
            # [bsz, num_heads, head_dim, state_size]
            dB = dt.unsqueeze(-1) * B.unsqueeze(-2)

            # Discretize x into dB
            # [bsz, intermediate_size] -> [bsz, num_heads, head_dim]
            hidden_states = hidden_states.reshape(hidden_states.shape[0], -1, self.head_dim)
            dBx = dB * hidden_states.unsqueeze(-1)

            # State calculation
            cache_params.ssm_states[self.layer_idx].copy_(
                cache_params.ssm_states[self.layer_idx] * dA + dBx
            )

            # Subsequent output
            # [bsz, n_groups * state_size] -> [bsz, num_heads, state_size]
            C = C.reshape(C.shape[0], self.n_groups, -1).unsqueeze(-2)
            C = C.expand(C.shape[0], C.shape[1], self.num_heads // self.n_groups, C.shape[-1]).contiguous()
            C = C.reshape(C.shape[0], -1, C.shape[-1])
            # [bsz, num_heads, head_dim]

            ssm_states = cache_params.ssm_states[self.layer_idx].to(C.dtype)  # Shape: [b, h, d, n]
            # Reshape ssm_states to merge the first two dimensions
            ssm_states_reshaped = ssm_states.view(batch_size * self.num_heads, self.head_dim, self.ssm_state_size)  # Shape: [b*h, d, n]
            C_reshaped = C.view(batch_size * self.num_heads, self.ssm_state_size, 1)  # Shape: [b*h, n, 1]
            y = torch.bmm(ssm_states_reshaped, C_reshaped)
            y = y.view(batch_size, self.num_heads, self.head_dim)

            # D skip connection
            # [num_heads] -> [num_heads, head_dim]
            D = self.D.unsqueeze(-1).expand(self.D.shape[0], self.head_dim)
            y = (y + hidden_states * D).to(y.dtype)

            # [bsz, num_heads, head_dim] -> [bsz, 1, intermediate_size]
            y = y.reshape(y.shape[0], -1).unsqueeze(1)
        else:
            # begin ssd naive implementation without einsums
            dt = nn.functional.softplus(dt + self.dt_bias)
            dt = torch.clamp(dt, self.time_step_min)  # , self.time_step_max)
            hidden_states = hidden_states.reshape(hidden_states.shape[0], hidden_states.shape[1], -1, self.head_dim).float()
            B = B.reshape(batch_size, seq_len, -1, self.ssm_state_size).float()
            C = C.reshape(batch_size, seq_len, -1, self.ssm_state_size).float()
            B = B.repeat(1, 1, self.num_heads // self.n_groups, 1)
            C = C.repeat(1, 1, self.num_heads // self.n_groups, 1)
            seq_len = hidden_states.shape[1]
            pad_size = self.chunk_size - (seq_len % self.chunk_size)

            D_residual = self.D.unsqueeze(-1) * pad_by_size(hidden_states, pad_size)

            # Discretize x and A
            hidden_states = hidden_states * dt.unsqueeze(-1)
            A = A.to(hidden_states.dtype) * dt

            # Rearrange into blocks/chunks
            hidden_states, A, B, C = (reshape_into_chunks(t, pad_size, self.chunk_size) for t in (hidden_states, A, B, C))

            # [bsz, -1, chunk_size, num_heads] -> [bsz, num_heads, -1, chunk_size]
            A = A.permute(0, 3, 1, 2)
            A_cumsum = torch.cumsum(A, dim=-1)

            # 1. Compute the output for each intra-chunk (diagonal blocks)
            # This is the analog of a causal mask
            L = torch.exp(segsum(A))

            # First, contraction of C and B to get G (attention-weights like)
            G_intermediate = C[:, :, :, None, :, :] * B[:, :, None, :, :, :]  # shape: (b, c, l, s, h, n)
            G = G_intermediate.sum(dim=-1)  # shape: (b, c, l, s, h)

            # Step 2: Compute M, equivalent to applying attention mask to weights
            M_intermediate = G[..., None] * L.permute(0, 2, 3, 4, 1)[..., None]
            M = M_intermediate.sum(dim=-1)

            # Step 3: Compute Y_diag (apply to values)
            # Y_diag = ((M.unsqueeze(-1) * hidden_states.unsqueeze(1)).sum(dim=3))
            # Y_diag_einsum = torch.einsum("bclsh,bcshp->bclhp", M, hidden_states)
            # Y_diag_alt = (M.unsqueeze(-1) * hidden_states.unsqueeze(2)).sum(3)
            # diff_ = ((Y_diag_alt - Y_diag_einsum) / (Y_diag_einsum + 1e-9)).min()

            # Y_diag_intermediate = M[..., None] * hidden_states[:, None, ...]
            # Y_diag = Y_diag_intermediate.sum(dim=3)

            Y_diag = (M.unsqueeze(-1) * hidden_states.unsqueeze(2)).sum(3)

            # (right term of low-rank factorization of off-diagonal blocks; B terms)

            decay_states = torch.exp(A_cumsum[:, :, :, -1:] - A_cumsum)
            B_decay_contraction = B * decay_states.permute(0, 2, 3, 1)[..., None]
            # permute back B * decay states
            states = (B_decay_contraction.permute(0, 1, 3, 2, 4)[..., None]
                      * hidden_states.permute(0, 1, 3, 2, 4)[..., None, :]).sum(dim=3).permute(0, 1, 2, 4, 3)
            if cache_params is not None and cache_params.seqlen_offset > 0:
                previous_states = cache_params.ssm_states[self.layer_idx].unsqueeze(1)
            else:
                previous_states = torch.zeros_like(states[:, :1])
            states = torch.cat([previous_states, states], dim=1)
            decay_chunk = torch.exp(segsum(nn.functional.pad(A_cumsum[:, :, :, -1], (1, 0))))

            states_permuted = states.permute(0, 2, 1, 3, 4)
            result = (decay_chunk[..., None, None] * states_permuted[:, :, None, ...]).sum(dim=2)
            new_states = result.permute(0, 2, 1, 3, 4)
            states, ssm_state = new_states[:, :-1], new_states[:, -1]

            # Compute state -> output conversion per chunk
            # (left term of low-rank factorization of off-diagonal blocks; C terms)
            state_decay_out = torch.exp(A_cumsum)
            # compute Yoff
            C_times_states = (C[..., None, :] * states[:, :, None, ...])
            state_decay_out_permuted = state_decay_out.permute(0, 2, 3, 1)
            Y_off = (C_times_states.sum(-1) * state_decay_out_permuted[..., None])
            # Add output of intra-chunk and inter-chunk terms (diagonal and off-diagonal blocks)

            y = Y_diag + Y_off
            # [bsz, -1, self.chunk_size, num_heads, head_dim] -> [bsz, (padded) seq_len, num_heads, head_dim]
            y = y.reshape(y.shape[0], -1, self.num_heads, self.head_dim)

            y = y + D_residual
            # Cutting off padded chunks
            if pad_size > 0:
                y = y[:, :seq_len, :, :]

            # move reshape to naive method
            y = y.reshape(y.shape[0], y.shape[1], -1)
            if ssm_state is not None and cache_params is not None:
                cache_params.ssm_states[self.layer_idx].copy_(ssm_state)

        scan_output = self.norm(y, o=gate)

        # end ssd naive
        if d_mlp > 0:
            y0 = nn.functional.silu(z0) * x0
            scan_output = torch.cat([y0, scan_output], dim=-1)

        # 4. Final linear projection
        contextualized_states = self.out_proj(scan_output.to(dtype))  # [batch, seq_len, hidden_size]
        return contextualized_states
    # fmt: on

    def forward(
        self,
        hidden_states,
        cache_params: Optional[Mamba2Cache] = None,
        cache_position: Optional[torch.LongTensor] = None,
        attention_mask: Optional[torch.Tensor] = None,
    ):
        if is_fast_path_available and "cuda" in self.in_proj.weight.device.type:
            return self.cuda_kernels_forward(
                hidden_states, cache_params, cache_position, attention_mask
            )
        # if cache_params is not None and attention_mask is not None and attention_mask.shape[1] > 1 and attention_mask.shape[0] > 1:
        dtype = hidden_states.dtype
        if (
            attention_mask is not None
            and attention_mask.shape[1] > 1
            and attention_mask.shape[0] > 1
        ):
            # tune out hidden states for pad tokens, see https://github.com/state-spaces/mamba/issues/66
            hidden_states = (hidden_states * attention_mask.unsqueeze(2)).to(dtype)

        return self.torch_forward(
            hidden_states, cache_params, cache_position, attention_mask
        )


class Mamba2Block(nn.Module):
    def __init__(self, config, layer_idx):
        super().__init__()
        self.config = config
        self.layer_idx = layer_idx
        self.residual_in_fp32 = config.residual_in_fp32
        self.norm = RMSNorm(config.hidden_size, eps=config.layer_norm_epsilon)
        self.mixer = Mamba2Mixer(config, layer_idx=layer_idx)

    def forward(
        self,
        hidden_states,
        cache_params: Optional[Mamba2Cache] = None,
        cache_position: Optional[torch.LongTensor] = None,
        attention_mask: Optional[torch.Tensor] = None,
    ):
        residual = hidden_states
        hidden_states = self.norm(hidden_states.to(dtype=self.norm.weight.dtype))
        if self.residual_in_fp32:
            residual = residual.to(torch.float32)

        hidden_states = self.mixer(
            hidden_states,
            cache_params=cache_params,
            cache_position=cache_position,
            attention_mask=attention_mask,
        )
        hidden_states = residual + hidden_states
        return hidden_states


class Mamba2PreTrainedModel(PreTrainedModel):
    """
    An abstract class to handle weights initialization and a simple interface for downloading and loading pretrained
    models.
    """

    config_class = Mamba2Config
    base_model_prefix = "backbone"
    _no_split_modules = ["Mamba2Block"]
    supports_gradient_checkpointing = True
    _is_stateful = True

    def _init_weights(self, module):
        """Initialize the weights."""
        if isinstance(module, Mamba2Mixer):
            module.A_log._no_weight_decay = True
            module.D._no_weight_decay = True

            dt = torch.exp(
                torch.rand(self.config.num_heads)
                * (
                    math.log(self.config.time_step_max)
                    - math.log(self.config.time_step_min)
                )
                + math.log(self.config.time_step_min)
            ).clamp(min=self.config.time_step_floor)

            # # Inverse of softplus: https://github.com/pytorch/pytorch/issues/72759
            inv_dt = dt + torch.log(-torch.expm1(-dt))
            with torch.no_grad():
                module.dt_bias.copy_(inv_dt)
            module.dt_bias._no_reinit = True

        if isinstance(module, nn.Linear):
            if module.bias is not None:
                if not getattr(module.bias, "_no_reinit", False):
                    nn.init.zeros_(module.bias)
        elif isinstance(module, nn.Embedding):
            nn.init.normal_(module.weight, std=self.config.initializer_range)

        if self.config.rescale_prenorm_residual:
            # Reinitialize selected weights subject to the OpenAI GPT-2 Paper Scheme:
            #   > A modified initialization which accounts for the accumulation on the residual path with model depth. Scale
            #   > the weights of residual layers at initialization by a factor of 1/√N where N is the # of residual layers.
            #   >   -- GPT-2 :: https://openai.com/blog/better-language-models/
            #
            # Reference (Megatron-LM): https://github.com/NVIDIA/Megatron-LM/blob/main/megatron/model/gpt_model.py
            for name, p in module.named_parameters():
                if name in ["out_proj.weight"]:
                    # Special Scaled Initialization --> There are 2 Layer Norms per Transformer Block
                    # Following Pytorch init, except scale by 1/sqrt(2 * n_layer)
                    # We need to reinit p since this code could be called multiple times
                    # Having just p *= scale would repeatedly scale it down
                    nn.init.kaiming_uniform_(p, a=math.sqrt(5))
                    with torch.no_grad():
                        p /= math.sqrt(self.config.num_hidden_layers)


@dataclass
# Copied from transformers.models.mamba.modeling_mamba.MambaOutput with MAMBA->MAMBA2,Mamba->Mamba2
class Mamba2Output(ModelOutput):
    """
    Class for the MAMBA2 model outputs.

    Args:
        last_hidden_state (`torch.FloatTensor` of shape `(batch_size, sequence_length, hidden_size)`):
            Sequence of hidden-states at the output of the last layer of the model.
        cache_params (`Mamba2Cache`):
            The state of the model at the last time step. Can be used in a forward method with the next `input_ids` to
            avoid providing the old `input_ids`.

            Includes both the State space model state matrices after the selective scan, and the Convolutional states
        hidden_states (`tuple(torch.FloatTensor)`, *optional*, returned when `output_hidden_states=True` is passed or when `config.output_hidden_states=True`):
            Tuple of `torch.FloatTensor` (one for the output of the embeddings, if the model has an embedding layer, +
            one for the output of each layer) of shape `(batch_size, sequence_length, hidden_size)`.

            Hidden-states of the model at the output of each layer plus the optional initial embedding outputs.
    """

    last_hidden_state: Optional[torch.FloatTensor] = None
    cache_params: Optional[Mamba2Cache] = None
    hidden_states: Optional[Tuple[torch.FloatTensor]] = None


@dataclass
# Copied from transformers.models.mamba.modeling_mamba.MambaCausalLMOutput with Mamba->Mamba2
class Mamba2CausalLMOutput(ModelOutput):
    """
    Base class for causal language model (or autoregressive) outputs.

    Args:
        loss (`torch.FloatTensor` of shape `(1,)`, *optional*, returned when `labels` is provided):
            Language modeling loss (for next-token prediction).
        logits (`torch.FloatTensor` of shape `(batch_size, sequence_length, config.vocab_size)`):
            Prediction scores of the language modeling head (scores for each vocabulary token before SoftMax).
        cache_params (`Mamba2Cache`):
            The state of the model at the last time step. Can be used in a forward method with the next `input_ids` to
            avoid providing the old `input_ids`.

            Includes both the State space model state matrices after the selective scan, and the Convolutional states
        hidden_states (`tuple(torch.FloatTensor)`, *optional*, returned when `output_hidden_states=True` is passed or when `config.output_hidden_states=True`):
            Tuple of `torch.FloatTensor` (one for the output of the embeddings, if the model has an embedding layer, +
            one for the output of each layer) of shape `(batch_size, sequence_length, hidden_size)`.

            Hidden-states of the model at the output of each layer plus the optional initial embedding outputs.
    """

    loss: Optional[torch.FloatTensor] = None
    logits: Optional[torch.FloatTensor] = None
    cache_params: Optional[Mamba2Cache] = None
    hidden_states: Optional[Tuple[torch.FloatTensor]] = None


class Mamba2Model(Mamba2PreTrainedModel):
    def __init__(self, config):
        super().__init__(config)

        self.embeddings = nn.Embedding(config.vocab_size, config.hidden_size)
        self.layers = nn.ModuleList(
            [
                Mamba2Block(config, layer_idx=idx)
                for idx in range(config.num_hidden_layers)
            ]
        )

        self.gradient_checkpointing = False
        self.norm_f = RMSNorm(config.hidden_size, eps=config.layer_norm_epsilon)
        # Initialize weights and apply final processing
        self._register_load_state_dict_pre_hook(self.load_hook)
        self.post_init()

    def load_hook(self, state_dict, prefix, *args):
        for k in state_dict:
            if "embedding." in k:
                state_dict[k.replace("embedding.", "embeddings.")] = state_dict.pop(k)
                break

    def get_input_embeddings(self):
        return self.embeddings

    def set_input_embeddings(self, new_embeddings):
        self.embeddings = new_embeddings

    def forward(
        self,
        input_ids: Optional[torch.LongTensor] = None,
        inputs_embeds: Optional[torch.LongTensor] = None,
        cache_params: Optional[Mamba2Cache] = None,
        use_cache: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        return_dict: Optional[bool] = None,
        cache_position: Optional[torch.LongTensor] = None,
        attention_mask: Optional[torch.Tensor] = None,
        **kwargs,
    ) -> Union[Tuple, Mamba2Output]:
        output_hidden_states = (
            output_hidden_states
            if output_hidden_states is not None
            else self.config.output_hidden_states
        )
        use_cache = (
            use_cache
            if use_cache is not None
            else (self.config.use_cache if not self.training else False)
        )
        return_dict = (
            return_dict if return_dict is not None else self.config.use_return_dict
        )

        if (input_ids is None) ^ (inputs_embeds is not None):  # ^ is python for xor
            raise ValueError(
                "You cannot specify both input_ids and inputs_embeds at the same time, and must specify either one"
            )

        if inputs_embeds is None:
            inputs_embeds = self.embeddings(input_ids)

        if self.gradient_checkpointing and self.training and use_cache:
            use_cache = False

        if use_cache:
            if cache_params is None:
                cache_params = Mamba2Cache(
                    self.config,
                    inputs_embeds.size(0),
                    device=inputs_embeds.device,
                    dtype=inputs_embeds.dtype,
                )
                cache_position = torch.arange(
                    0, self.config.conv_kernel, device=inputs_embeds.device
                )
            elif cache_position is None:
                # cases when we do manual forward instead of using `model.generate` which will initiate
                # `cache_position` and makes sure it is not None, throw error here instead of doing some
                # hack to conjecture the current cache position
                raise ValueError(
                    "You have to specify the `cache_position` manually when `use_cache=True` and `cache_params` is passed, "
                    "you don't have to pass a `cache_params` if you are in prefilling stage because in that case it will "
                    "be initialized for you automatically"
                )
        else:
            cache_params = None

        hidden_states = inputs_embeds
        all_hidden_states = () if output_hidden_states else None
        for mixer_block in self.layers:
            if self.gradient_checkpointing and self.training:
                hidden_states = self._gradient_checkpointing_func(
                    mixer_block.__call__,
                    hidden_states,
                    cache_params,
                    cache_position,
                    attention_mask,
                )
            else:
                hidden_states = mixer_block(
                    hidden_states,
                    cache_params=cache_params,
                    cache_position=cache_position,
                    attention_mask=attention_mask,
                )

            if output_hidden_states:
                all_hidden_states = all_hidden_states + (hidden_states,)

        if use_cache:
            cache_params.seqlen_offset += inputs_embeds.shape[1]

        hidden_states = self.norm_f(hidden_states)

        if output_hidden_states:
            all_hidden_states = all_hidden_states + (hidden_states,)

        if not return_dict:
            return tuple(
                v
                for v in [hidden_states, cache_params, all_hidden_states]
                if v is not None
            )

        return Mamba2Output(
            last_hidden_state=hidden_states,
            cache_params=cache_params if use_cache else None,
            hidden_states=all_hidden_states,
        )


class Mamba2ForCausalLM(Mamba2PreTrainedModel):
    _tied_weights_keys = []

    def __init__(self, config):
        super().__init__(config)
        self.backbone = Mamba2Model(config)
        self.lm_head = nn.Linear(config.hidden_size, config.vocab_size, bias=False)
        # Initialize weights and apply final processing
        self.post_init()

    def get_output_embeddings(self):
        return self.lm_head

    def set_output_embeddings(self, new_embeddings):
        self.lm_head = new_embeddings

    def get_input_embeddings(self):
        return self.backbone.get_input_embeddings()

    def set_input_embeddings(self, new_embeddings):
        return self.backbone.set_input_embeddings(new_embeddings)

    def _update_model_kwargs_for_generation(
        self,
        outputs: ModelOutput,
        model_kwargs: Dict[str, Any],
        num_new_tokens: int = 1,
        **kwargs,
    ) -> Dict[str, Any]:
        model_kwargs["cache_params"] = outputs.get("cache_params", None)
        if (
            model_kwargs.get("use_cache", True)
            and "cache_position" in model_kwargs
            and model_kwargs["cache_position"] is not None
        ):
            model_kwargs["cache_position"] = (
                model_kwargs["cache_position"][-1:] + num_new_tokens
            )

        return model_kwargs

    def prepare_inputs_for_generation(
        self,
        input_ids,
        inputs_embeds=None,
        use_cache=None,
        cache_params: Optional[Mamba2Cache] = None,
        cache_position: Optional[torch.LongTensor] = None,
        attention_mask: Optional[torch.Tensor] = None,
        **kwargs,
    ):
        if use_cache:
            # `cache_position` should have been initialized in `generate`
            if cache_position is None:
                raise ValueError(
                    "`cache_position` should not be None as it should have been initialized in "
                    "`model.generate`, you are responsible for passing in a valid `cache_position` if "
                    "you are calling `prepare_inputs_for_generation` directly with `use_cache=True`"
                )
            # how do we detect that we are in decoding without cache?
            if cache_position[0] > 0:
                input_ids = input_ids[:, -1].unsqueeze(-1)
                attention_mask = attention_mask[:, -1].unsqueeze(-1)
            else:
                # we initialize the `cache_position` to full size of `conv_states` at prefill stage
                # considering padding will be applied when input length is shorter, and truncation
                # will be applied when it is longer, so it will be equivalent to always have it match
                # the length of `cache_params.conv_states`, which is `config.conv_kernel`
                cache_position = torch.arange(
                    0, input_ids.shape[1], device=input_ids.device
                )
                # if the cache is not used, we also do have to extend the attention mask here
                # TODO there is likely a cleverer way to do this
                extended_mask = torch.ones(
                    attention_mask.size(0),
                    input_ids.shape[1] - attention_mask.shape[1],
                    device=attention_mask.device,
                )
                attention_mask = torch.cat([attention_mask, extended_mask], dim=1)
            cache_params = None
            if attention_mask.shape[1] < input_ids.shape[1]:
                # we have to update manually the attention mask if
                # we are in decoding without cache
                # and we don't have position_ids here
                # TODO but we should be able to use cache_position though at a later time
                extended_mask = torch.ones(
                    attention_mask.size(0),
                    input_ids.shape[1] - attention_mask.shape[1],
                    device=attention_mask.device,
                )
                attention_mask = torch.cat([attention_mask, extended_mask], dim=1)
        if inputs_embeds is not None and cache_params is None:
            model_inputs = {"inputs_embeds": inputs_embeds}
        else:
            model_inputs = {"input_ids": input_ids}

        model_inputs.update(
            {
                "attention_mask": attention_mask,
                "cache_params": cache_params,
                "use_cache": use_cache,
                "cache_position": cache_position,
            }
        )
        return model_inputs

    def forward(
        self,
        input_ids: Optional[torch.LongTensor] = None,
        inputs_embeds: Optional[torch.FloatTensor] = None,
        cache_params: Optional[Mamba2Cache] = None,
        labels: Optional[torch.LongTensor] = None,
        output_hidden_states: Optional[bool] = None,
        return_dict: Optional[bool] = None,
        use_cache: Optional[bool] = None,
        cache_position: Optional[torch.Tensor] = None,
        attention_mask: Optional[torch.Tensor] = None,
        **kwargs,  # for now we need this for generation
    ) -> Union[Tuple, Mamba2CausalLMOutput]:
        r"""
        labels (`torch.LongTensor` of shape `(batch_size, sequence_length)`, *optional*):
            Labels for language modeling. Note that the labels **are shifted** inside the model, i.e. you can set
            `labels = input_ids` Indices are selected in `[-100, 0, ..., config.vocab_size]` All labels set to `-100`
            are ignored (masked), the loss is only computed for labels in `[0, ..., config.vocab_size]`
        """
        return_dict = (
            return_dict if return_dict is not None else self.config.use_return_dict
        )

        mamba2_outputs = self.backbone(
            input_ids,
            cache_params=cache_params,
            inputs_embeds=inputs_embeds,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
            use_cache=use_cache,
            cache_position=cache_position,
            attention_mask=attention_mask,
        )
        hidden_states = mamba2_outputs[0]

        logits = self.lm_head(hidden_states.to(self.lm_head.weight.dtype)).float()

        loss = None
        if labels is not None:
            # move labels to correct device to enable model parallelism
            labels = labels.to(logits.device)
            # Shift so that tokens < n predict n
            shift_logits = logits[..., :-1, :].contiguous()
            shift_labels = labels[..., 1:].contiguous()
            # Flatten the tokens
            if self.config.fuse_cross_entropy:
                loss_fct = FusedCrossEntropyLoss(inplace_backward=True)
            else:
                loss_fct = nn.CrossEntropyLoss()
            loss = loss_fct(
                shift_logits.view(-1, shift_logits.size(-1)), shift_labels.view(-1)
            )

        if not return_dict:
            output = (logits,) + mamba2_outputs[1:]
            return ((loss,) + output) if loss is not None else output

        return Mamba2CausalLMOutput(
            loss=loss,
            logits=logits,
            cache_params=mamba2_outputs.cache_params,
            hidden_states=mamba2_outputs.hidden_states,
        )