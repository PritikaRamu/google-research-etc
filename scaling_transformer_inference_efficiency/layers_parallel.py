# coding=utf-8
# Copyright 2022 The Google Research Authors.
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

"""One Transformer layer, in hard xmap."""

from typing import Sequence, Tuple, Enum

import jax
from jax import lax
import jax.numpy as jnp
import jax.scipy
import numpy as np

from scaling_transformer_inference_efficiency import attention
from scaling_transformer_inference_efficiency import checkpoint
from scaling_transformer_inference_efficiency import collectives
from scaling_transformer_inference_efficiency import inference
from scaling_transformer_inference_efficiency import special2
from scaling_transformer_inference_efficiency.inference import Layer

HParams = checkpoint.HParams
CheckpointSpec = checkpoint.CheckpointSpec

ATTN_3D_SHARDING_THRESHOLD_PER_CHIP = 2

# pylint: disable = invalid-name
# pylint: disable = protected-access


class AttnAllToAll(Enum):
  """How much of an alltoall to use for attention."""
  NONE = 0  # [batch.B, heads.YZX]
  AXIS_Z = 1  # [batch.ZB, heads.YX]
  AXES_YZ = 2  # [batch.YZB, heads.X]
  AXES_YZX = 3  # [batch.YZXB, heads]


def assert_equal(x, y):
  assert x == y, f'{x} != {y}'


# pylint: disable = g-doc-return-or-yield
# pylint: disable = g-doc-args
# TODO(sholto): Update to new, tested parsing collectives.
def transformer_layer_weight_stationary(
    hparams,
    layer,
    params,
    sin,
    cos,
    kv_caches,
    x,
    x_axis,
    y_axis,
    z_axis,
    attn_all_to_all,
    latency_collectives,
    shard_seqlen_vs_batch = False,
):
  """Forward pass through a single layer, returning output, K, V.

  The 'x' physical axis plays multiple roles:
  * for d_model sharding, we shard 100% over 'x', which we call '.x' in our
  notation
  * for heads-sharding (on reducescatter), we break the 'x' physical axis up
  into a product axis:
    * ".X" in our notation represents head-sharding
    * ".B" in our notation represents batch-sharding. We use this if we run out
    of head-sharding. It is the "inner-most" part of this product axis.
  In the extreme with X=1 and B=8, this lets us scale up to a pf_8x8x8 slice
  before this partitioning runs out of steam. We need a custom reducescatter
  implementation that supports scattering over two different axes X and B. We do
  that as follows:
  * when B=1, all our implementations work
  * when B=2, X>1, reducescatter_bidirectional_latency works, and we still get
    compute/communication overlap
  * (theoretically) when B>2, we won't get any compute/communication overlap. We
  do a lax.psum_scatter over B and a lax.psum_scatter over X.
  For slice sizes <= pf_8x8x8, we only care about the B=1 or B=2 cases.

  This implementation is for pmap, with 'x'=d_model sharding,
  ('y', 'z')=d_ff sharding.
  * params are assumed already sharded this way, i.e. dmodel.x and heads.YZ
  * sin and cos are sharded by batch.YZx (or batch.YZ or batch.Y as necessary)
  * kv_cache is sharded by batch.YZx (or batch.YZ or batch.Y as necessary)
  * x: [batch.Z, maxlen, dmodel.xY]
  """
  if latency_collectives:
    matmul_reducescatter = collectives.matmul_reducescatter_bidirectional_latency
    reducescatter = collectives.reducescatter_bidirectional_latency
    matmul_allgather = collectives.async_matmul_allgather_latency
  else:
    if len(jax.local_devices()) <= 32:
      matmul_reducescatter = collectives.matmul_reducescatter_oneway
      reducescatter = collectives.reducescatter_oneway
      matmul_allgather = collectives.async_matmul_allgather_one_way
    else:
      matmul_reducescatter = collectives.matmul_reducescatter_bidirectional_throughput
      reducescatter = collectives.reducescatter_bidirectional_throughput
      matmul_allgather = collectives.async_matmul_allgather_throughput

  heads_yz = hparams.heads // (y_axis * z_axis)
  if heads_yz >= x_axis:
    B = 1
    X = x_axis
  else:
    B = x_axis // heads_yz
    X = heads_yz

  if B >= 2:
    # There are X many b_groups, each of size B
    # b_groups = [list(a) for a in np.reshape(np.arange(x_axis), (X, B))]
    # There are B many x_groups, each of size X
    x_groups = [list(a) for a in np.reshape(np.arange(x_axis), (X, B)).T]
  else:
    x_groups = None
    # b_groups = None

  b_index = lax.axis_index('x') % B

  def my_layer(t, axis=0):
    """Gets the parameters corresponding to a given layer."""
    return lax.dynamic_index_in_dim(t, layer, axis=axis, keepdims=False)

  # Compare
  # flaxformer/architectures/t5/parallel_fused_decoder.py
  # flaxformer/components/attention/dense_attention.py;l=1147;
  # flaxformer/components/attention/dense_attention.py;l=252;

  # prefix_batch = sin.shape[0]

  batch_z, max_len, _ = x.shape
  if shard_seqlen_vs_batch:
    max_len *= z_axis
    batch = batch_z
    batch_xyz = batch // (x_axis * y_axis * z_axis)
  else:
    batch = batch_z * z_axis
    batch_xyz = batch // (x_axis * y_axis * z_axis)
    batch_yz = batch // (y_axis * z_axis)
    batch_yzb = batch_yz // B
    batch_zb = batch // (z_axis * B)
    # beam = batch // prefix_batch # TODO(reinerp): Do we need this

  if batch == 1 and max_len == 1:
    raise ValueError('sharded batch-1 matmul is broken on VLC, b/246436629')

  # einsum(xnorm, q_wi):
  # [batch, maxlen, dmodel.x] @ [heads.YZ, dmodel.x, q_wi_per_head]
  # -> (matmul)
  # -> [batch, maxlen, heads.YZ, q_wi_per_head]{x unreduced}
  # -> (reducescatter over x into X heads, B batches)
  # -> [batch.B, maxlen, heads.YZX, q_wi_per_head]
  # TODO(reinerp): For chips>64, need to reducescatter over batch instead.
  with jax.named_scope('allgather_layernorm'):
    # allgather xnorm: [batch.Z, maxlen, dmodel.XY]
    # -> [batch.Z, maxlen, dmodel.X]    (xnorm_z)
    # -> [batch, maxlen, dmodel.X]
    xgather = x
    xgather = lax.all_gather(xgather, 'y', axis=2, tiled=True)

    epsilon = 1e-6
    xgather = jnp.float32(xgather)
    mean2 = lax.pmean(
        jnp.mean(lax.square(xgather), axis=-1, keepdims=True), axis_name='x')
    xnorm_z = jnp.bfloat16(xgather * lax.rsqrt(mean2 + epsilon))
    # when attention_all_to_all is None we can partition over sequence len not
    # batch
    if shard_seqlen_vs_batch:
      xnorm = lax.all_gather(xnorm_z, 'z', axis=1, tiled=True)
    else:
      xnorm = lax.all_gather(xnorm_z, 'z', axis=0, tiled=True)

  with jax.named_scope('q_wi'):
    if B == 1:
      q_wi = matmul_reducescatter(
          'bte,hed->bthd',
          xnorm,
          params.q_wi,
          scatter_dimension=(0, 2),
          axis_name='x',
          layer=layer,
          subsplit_axis=0)
    else:
      q_wi_unreduced = jnp.einsum('bte,hed->bthd', xnorm, my_layer(params.q_wi))
      # Combine batch into heads, reducescatter over heads, split batch back out
      assert_equal(q_wi_unreduced.shape,
                   (batch, max_len, heads_yz, hparams.q_wi_per_head))
      q_wi_unreduced = jnp.reshape(
          q_wi_unreduced,
          (B, batch // B, max_len, heads_yz, hparams.q_wi_per_head))
      q_wi_unreduced = jnp.transpose(q_wi_unreduced, (1, 2, 0, 3, 4))
      q_wi_unreduced = jnp.reshape(
          q_wi_unreduced,
          (batch // B, max_len, B * heads_yz, hparams.q_wi_per_head))
      q_wi = collectives.reducescatter_bidirectional_latency(
          q_wi_unreduced, scatter_dimension=2, axis_name='x')

    if shard_seqlen_vs_batch:
      assert_equal(q_wi.shape,
                   (batch, max_len, heads_yz // X, hparams.q_wi_per_head))
    else:
      assert_equal(q_wi.shape,
                   (batch // B, max_len, heads_yz // X, hparams.q_wi_per_head))

    if isinstance(params, inference.QuantizedLayer):
      prev_shape = q_wi.shape
      q_wi = jnp.bfloat16(q_wi * jnp.squeeze(my_layer(params.q_wi_scale)))
      assert_equal(prev_shape, q_wi.shape)

    # unlike in https://arxiv.org/pdf/2002.05202.pdf, PaLM implements
    # swiGLU with full d_ff dimension, rather than 2/3 scaled
    wi0 = q_wi[:, :, :, hparams.qkv:hparams.qkv + (hparams.ff // hparams.heads)]
    wi1 = q_wi[:, :, :, hparams.qkv + (hparams.ff // hparams.heads):]

  # einsum(xnorm, kv):
  #
  # if attn>=AXES_YZ:
  #   xnorm_z: [batch.Z, maxlen, dmodel.x]
  #     -> [batch.YZ, maxlen, dmodel.x]  (slice down)
  #
  # Then:
  #
  # [batch.Y? Z, maxlen, dmodel.x] @ [dmodel.x, 1, 2*qkv]
  # -> (matmul)
  # -> [batch.Y? Z, maxlen, 1, 2*qkv]{x unreduced}
  # -> (reducescatter over x into batch)
  #         *NOT* collective matmul, because it's batch
  # -> { Attn.NONE:      [batch.B, maxlen,  1, 2*qkv]
  #    { Attn.AXIS_Z:    [batch.ZB, maxlen, 1, 2*qkv]
  #    { Attn.AXES_YZ:   [batch.YZB, maxlen, 1, 2*qkv]
  #    { Attn.AXES_YZX:  [batch.YZXB, maxlen, 1, 2*qkv]
  with jax.named_scope('kv'):
    y_index = lax.axis_index('y')
    # TODO(reinerp): Consider using xnorm instead of xnorm_z in NONE case?
    # I don't know yet if that's better.
    if attn_all_to_all.value >= AttnAllToAll.AXES_YZ.value:
      xnorm_sliced = lax.dynamic_slice_in_dim(
          xnorm_z, y_index * batch_yz, batch_yz, axis=0)
    else:
      xnorm_sliced = xnorm_z
    kv_unreduced = jnp.einsum('bte,ezd->btzd', xnorm_sliced,
                              my_layer(params.kv))

    if attn_all_to_all == AttnAllToAll.NONE:
      if shard_seqlen_vs_batch:
        # [batch, maxlen.Z, 1, 2*qkv]{x_unreduced}
        # -> [batch.B, maxlen, 1, 2*qkv]
        assert B == 1
        kv = lax.psum(kv_unreduced, 'x')
        kv = lax.all_gather(kv, 'z', axis=1, tiled=True)
      else:
        # [batch.Z, maxlen, 1, 2*qkv]{x_unreduced}
        # --ARx-->   [batch.Z, maxlen, 1, 2*qkv]
        # --slice--> [batch.ZB, maxlen, 1, 2*qkv]
        # --AGZ-->   [batch.B, maxlen, 1, 2*qkv]
        kv = lax.psum(kv_unreduced, 'x')
        kv = lax.dynamic_slice_in_dim(kv, b_index * batch_zb, batch_zb, axis=0)
        kv = lax.all_gather(kv, 'z', axis=0, tiled=True)
    elif attn_all_to_all == AttnAllToAll.AXIS_Z:
      # [batch.Z, maxlen, 1, 2*qkv]{x_unreduced}
      # --ARx-->   [batch.Z, maxlen, 1, 2*qkv]
      # --slice--> [batch.ZB, maxlen, 1, 2*qkv]
      kv = lax.psum(kv_unreduced, 'x')
      kv = lax.dynamic_slice_in_dim(kv, b_index * batch_zb, batch_zb, axis=0)
    elif attn_all_to_all == AttnAllToAll.AXES_YZ:
      # [batch.YZ, maxlen, 1, 2*qkv]{x_unreduced}
      # --ARx-->   [batch.YZ, maxlen, 1, 2*qkv]
      # --slice--> [batch.YZB, maxlen, 1, 2*qkv]
      kv = lax.psum(kv_unreduced, 'x')
      kv = lax.dynamic_slice_in_dim(kv, b_index * batch_yzb, batch_yzb, axis=0)
    elif attn_all_to_all == AttnAllToAll.AXES_YZX:
      # [batch.YZ, maxlen, 1, 2*qkv]{x_unreduced}
      # --RSx-->   [batch.YZXB, maxlen, 1, 2*qkv]
      assert batch_xyz >= 1, ('Batch size too small for AXES_XYZ and this chip '
                              'count')
      kv = lax.psum_scatter(kv_unreduced, 'x', scatter_dimension=0, tiled=True)

    if isinstance(params, inference.QuantizedLayer):
      prev_shape = kv.shape
      kv = jnp.bfloat16(kv * jnp.squeeze(my_layer(params.kv_scale)))
      assert_equal(prev_shape, kv.shape)

    k = kv[:, :, 0, :hparams.qkv]
    v = kv[:, :, 0, hparams.qkv:]

  with jax.named_scope('attn'):
    k = inference._rope(sin, cos, k)

    # print(f'batch_yzb: {batch_yzb}')
    # q: [batch.B, maxlen, heads.YZX, qkv]
    # -> { NONE:                   [batch.B, maxlen, heads.YZX, qkv]
    #    { AXIS_Z:                 [batch.ZB, maxlen, heads.YX, qkv]
    #    { AXES_YZ:                [batch.YZB, maxlen, heads.X, qkv]
    #    { AXES_YZX:               [batch.YZXB, maxlen, heads, qkv]
    q = q_wi[:, :, :, :hparams.qkv]
    if attn_all_to_all == AttnAllToAll.NONE:
      pass
    elif attn_all_to_all == AttnAllToAll.AXIS_Z:
      q = lax.all_to_all(
          q, axis_name='z', split_axis=0, concat_axis=2, tiled=True)
    elif attn_all_to_all == AttnAllToAll.AXES_YZ:
      q = lax.all_to_all(
          q, axis_name=('y', 'z'), split_axis=0, concat_axis=2, tiled=True)
    elif attn_all_to_all == AttnAllToAll.AXES_YZX:
      q = lax.all_to_all(
          q,
          axis_name='x',
          split_axis=0,
          concat_axis=2,
          tiled=True,
          axis_index_groups=x_groups)
      q = lax.all_to_all(
          q, axis_name=('y', 'z'), split_axis=0, concat_axis=2, tiled=True)

    q = inference._rope(sin, cos, q)

    y_att = jnp.bfloat16(attention.attend(q, k, v, kv_caches, layer))
    # y_att:
    #    { NONE:                   [batch.B, maxlen, heads.YZX, qkv]
    #    { AXIS_Z:                 [batch.ZB, maxlen, heads.YX, qkv]
    #    { AXES_YZ:                [batch.YZB, maxlen, heads.X, qkv]
    #    { AXES_YZX:               [batch.YZXB, maxlen, heads, qkv]
    # -> [batch.B, maxlen, heads.YZX, qkv]
    if attn_all_to_all == AttnAllToAll.NONE:
      pass
    elif attn_all_to_all == AttnAllToAll.AXIS_Z:
      y_att = lax.all_to_all(
          y_att, axis_name='z', split_axis=2, concat_axis=0, tiled=True)
    elif attn_all_to_all == AttnAllToAll.AXES_YZ:
      y_att = lax.all_to_all(
          y_att, axis_name=('y', 'z'), split_axis=2, concat_axis=0, tiled=True)
    elif attn_all_to_all == AttnAllToAll.AXES_YZX:
      y_att = lax.all_to_all(
          y_att, axis_name=('y', 'z'), split_axis=2, concat_axis=0, tiled=True)
      y_att = lax.all_to_all(
          y_att,
          axis_name='x',
          split_axis=2,
          concat_axis=0,
          tiled=True,
          axis_index_groups=x_groups)

  with jax.named_scope('SwiGLU'):
    y_mlp = special2.swish2(wi0) * wi1

  # einsum(y_fused, o_wo):
  # [batch, maxlen, heads.YZ, o_wo_per_head] @
  #       [heads.YZ, o_wo_per_head, dmodel.x]
  # -> (matmul)
  # -> [batch, maxlen, dmodel.x]{YZ unreduced}
  # -> (fused reducescatter)
  # -> [batch, maxlen, dmodel.xY]
  # -> (non-fused reducescatter)
  # -> [batch.Z, maxlen, dmodel.xY]
  with jax.named_scope('o_wo'):
    y_fused = jnp.concatenate([y_att, y_mlp], axis=-1)

    # do the second half of the mlp and the self-attn projection in parallel
    # allgather y_fused: [batch.B, maxlen, heads.YZX, o_wo_per_head]
    #       -> [batch, maxlen, heads.YZ, o_wo_per_head]
    if True and B == 1:
      # We don't have a B=2 collective allgather/matmul implementation yet, so
      # we use the collective matmul/reducescatter instead
      # print(f'o_wo: {params.o_wo.shape}')
      y_out = matmul_allgather(
          'bthd,hde->bte',
          y_fused,
          params.o_wo,
          gather_dimension=(0, None),
          axis_name='x',
          layer=layer,
          subsplit_axis=2)
      y_out = reducescatter(
          y_out, scatter_dimension=2, axis_name='y', subsplit_axis=2)
    else:
      # y_fused: [batch.B, maxlen, heads.YZX, o_wo_per_head]
      # -> (allgather)
      # -> [batch.B, maxlen, B * heads.YZ, o_wo_per_head]
      # -> (if B>1, reshape)
      # -> [batch, maxlen, heads.YZ, o_wo_per_head]
      y_fused = lax.all_gather(y_fused, axis_name='x', axis=2, tiled=True)
      if B > 1:
        assert_equal(y_fused.shape,
                     (batch // B, max_len, heads_yz * B, hparams.o_wo_per_head))
        y_fused = jnp.reshape(
            y_fused, (batch // B, max_len, B, heads_yz, hparams.o_wo_per_head))
        y_fused = jnp.swapaxes(y_fused, 1, 2)
        y_fused = jnp.reshape(y_fused,
                              (batch, max_len, heads_yz, hparams.o_wo_per_head))

      assert_equal(y_fused.shape,
                   (batch, max_len, heads_yz, hparams.o_wo_per_head))

      y_out = matmul_reducescatter(
          'bthd,hde->bte',
          y_fused,
          params.o_wo,
          scatter_dimension=(2, 2),
          axis_name='y',
          layer=layer,
          subsplit_axis=2)

    if shard_seqlen_vs_batch:
      y_out = reducescatter(
          y_out, scatter_dimension=1, axis_name='z', subsplit_axis=0)
    else:
      y_out = reducescatter(
          y_out, scatter_dimension=0, axis_name='z', subsplit_axis=0)

    if isinstance(params, inference.QuantizedLayer):
      prev_shape = y_out.shape
      y_out = jnp.bfloat16(y_out * jnp.squeeze(my_layer(params.o_wo_scale)))
      assert_equal(y_out.shape, prev_shape)

  with jax.named_scope('residual'):
    z = jnp.bfloat16(y_out + x)

  # if shard_seqlen_vs_batch:
  #   return z, k, v
  # else:
  return z, k[:batch_xyz], v[:batch_xyz]


def transformer_layer_weight_gathered(
    hparams, layer, params, sin,
    cos, kv_caches, x,
    x_axis, y_axis,
    z_axis):
  """Weight gathered parallel layer. Typically prefill."""

  # x: [batch.XYZ, t, e]

  with jax.named_scope('allgather_layernorm'):
    # No need to communicate across batch, so everything is local
    x_prec = jnp.float32(x)
    epsilon = 1e-6
    mean2 = jnp.mean(lax.square(x_prec), axis=-1, keepdims=True)
    xnorm = jnp.bfloat16(x * lax.rsqrt(mean2 + epsilon))

  def my_layer(t, axis=0):
    """Gets the parameters corresponding to a given layer."""
    return lax.dynamic_index_in_dim(t, layer, axis=axis, keepdims=False)

  batch, _, _ = x.shape
  batch_xyz = batch // (x_axis * y_axis * z_axis)

  # [batch.XYZ, t, e] @ [heads.YZ, e.X, q_wi_per_head]
  with jax.named_scope('q_wi'):
    # if False:
    #   gathered_weights = jax.lax.all_gather(
    #       my_layer(params.q_wi), 'x', axis=1, tiled=True)
    #   gathered_weights = jax.lax.all_gather(
    #       gathered_weights, ('y', 'z'), axis=0, tiled=True)
    #   q_wi = jnp.einsum('bte,hed->bthd', xnorm, gathered_weights)
    # else:
    q_wi = collectives.matmul_collective_weights_gather_q_wi(
        'bte,hed->bthd',
        xnorm,
        my_layer(
            params.q_wi
        ),  # in this case it makes sense to do this here because its once
        scatter_dimension=(2, None),  # TBD
        axis_name='x',  # TBD
        layer=layer,
        subsplit_axis=None)  #   -> [batch.XYZ, t, h, q_wi_per_head]

    if isinstance(params, inference.QuantizedLayer):
      prev_shape = q_wi.shape
      q_wi = jnp.bfloat16(q_wi * jnp.squeeze(my_layer(params.q_wi_scale)))
      assert_equal(prev_shape, q_wi.shape)

    # unlike in https://arxiv.org/pdf/2002.05202.pdf, PaLM implements
    # swiGLU with full d_ff dimension, rather than 2/3 scaled
    wi0 = q_wi[:, :, :, hparams.qkv:hparams.qkv + (hparams.ff // hparams.heads)]
    wi1 = q_wi[:, :, :, hparams.qkv + (hparams.ff // hparams.heads):]

    # kv is only batch sharded

    with jax.named_scope('kv'):
      # [batch.XYZ, t, e] @ [e, 1, 2*qkv] -> [batch.XYZ, t, 1, 2*qkv]
      # Two options here:
      # a) Split along x, and then all reduce along x
      # b) We fully replicate kv
      kv = jnp.einsum('bte,ezd->btzd', xnorm, my_layer(params.kv))

      if isinstance(params, inference.QuantizedLayer):
        prev_shape = kv.shape
        kv = jnp.bfloat16(kv * jnp.squeeze(my_layer(params.kv_scale)))
        assert_equal(prev_shape, kv.shape)

      k = kv[:, :, 0, :hparams.qkv]  # [batch.XYZ, t, qkv]
      v = kv[:, :, 0, hparams.qkv:]  # [batch.XYZ, t, qkv]

    with jax.named_scope('attn'):
      k = inference._rope(sin, cos, k)  # [batch.XYZ, t, qkv]
      q = q_wi[:, :, :, :hparams.qkv]
      q = inference._rope(sin, cos, q)  # [batch.XYZ, t, h, qkv]

      # [batch.XYZ, t, h, qkv]
      y_att = jnp.bfloat16(attention.attend(q, k, v, kv_caches, layer))

    with jax.named_scope('SwiGLU'):
      y_mlp = special2.swish2(wi0) * wi1  # [batch.XYZ, t, h, ff_per_head]

    # [bach.XYZ, t , h, d] @ [h.YZ, d, e.X] -> [batch.XYZ, t, e.X]
    with jax.named_scope('o_wo'):
      y_fused = jnp.concatenate([y_att, y_mlp], axis=-1)

      # previously concat yz, contracting over x - reconstructing heads dim
      # here, we contract over yz, concat over x to reconstruct embed dim
      # if False:
      #   gathered_weights = jax.lax.all_gather(
      #       my_layer(params.o_wo), 'x', axis=2, tiled=True)
      #   gathered_weights = jax.lax.all_gather(
      #       gathered_weights, ('y', 'z'), axis=0, tiled=True)
      #   y_out = jnp.einsum('bthd,hde->bte', y_fused, gathered_weights)

      # else:

      y_out = collectives.matmul_collective_weights_gather_o_wo(
          'bthd,hde->bte',
          y_fused,
          my_layer(params.o_wo),
          scatter_dimension=(2, None),  # TODO(sholto): Rename
          axis_name=None,  # both X and Y
          subsplit_axis=None,
          layer=layer)  # -> [batch.XYZ, t, e]

    if isinstance(params, inference.QuantizedLayer):
      prev_shape = y_out.shape
      y_out = jnp.bfloat16(y_out * jnp.squeeze(my_layer(params.o_wo_scale)))
      assert_equal(y_out.shape, prev_shape)

    with jax.named_scope('residual'):
      z = jnp.bfloat16(y_out + x)

    return z, k[:batch_xyz], v[:batch_xyz]


# pylint: disable = unused-argument
def embed_unembed_topp(h, x, embed,
                       sample, rng, x_axis, y_axis,
                       z_axis):
  """Runs non-layer stack components."""
  # x: int32[batch, maxlen]
  # embed: bfloat16[dmodel.X, vocab.YZ]
  print(x.shape, embed.shape, rng.shape)
  _, vocab_yz = embed.shape
  yz_index = lax.axis_index('y') * z_axis + lax.axis_index('z')
  vocab_start = yz_index * vocab_yz

  # Initial embedding lookup:
  with jax.named_scope('embed'):

    def embed_one(one_x):
      one_x -= vocab_start
      result = lax.dynamic_index_in_dim(embed, one_x, axis=1, keepdims=False)
      return jnp.where((one_x >= 0) & (one_x < vocab_yz), result, 0)

    x = jax.vmap(jax.vmap(embed_one))(x)
    x = lax.psum(x, axis_name=('y', 'z'))

  # x: bfloat16[batch, maxlen, dmodel.X]

  ## Transformer stack would go here ##

  # x: bfloat16[batch, maxlen, dmodel.X]

  # Final layernorm after transformer stack
  with jax.named_scope('layernorm'):
    epsilon = 1e-6
    mean2 = lax.pmean(
        jnp.mean(lax.square(jnp.float32(x)), axis=-1, keepdims=True),
        axis_name='x')
    x = jnp.bfloat16(x * lax.rsqrt(mean2 + epsilon))

  # x: bfloat16[batch, maxlen, dmodel.X]

  with jax.named_scope('unembed'):
    logits_unreduced = jnp.einsum('bte,ev->btv', jnp.float32(x),
                                  jnp.float32(embed))
    logits = lax.psum_scatter(
        logits_unreduced, 'x', scatter_dimension=0, tiled=True)
    # logits: float32[batch.X, maxlen, vocab.YZ]

  if not sample:
    return logits

  with jax.named_scope('sample'):
    # logits:
    # float32[batch.X, maxlen, vocab.YZ]
    #   -> float32[batch.XYZ, maxlen, vocab]
    batch_x, _, vocab_yz = logits.shape
    padded_batch_x = max(batch_x, y_axis * z_axis)
    if padded_batch_x > batch_x:
      logits = jnp.pad(
          logits,
          pad_width=((0, padded_batch_x - batch_x), (0, 0), (0, 0)),
          mode='constant')
    logits = lax.all_to_all(
        logits, ('y', 'z'), split_axis=0, concat_axis=2, tiled=True)
    # logits = binary_search.topp_mask(logits, 0.9, -1e10)
    # TODO(reinerp): Do we still need t5x binary search?
    sample = jax.random.categorical(rng, logits).astype(jnp.int32)
    # sample: int32[batch.XYZ, maxlen]
    sample = lax.all_gather(sample, ('x', 'y', 'z'), axis=0, tiled=True)
    return sample
