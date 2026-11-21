/**
 * Kernel-timeline categories and their colours. Every colour drawn on the timeline is one of the legend entries, and the
 * legend colours are pairwise distinguishable (hue, or lightness within a similar hue); `kernelColors.test.ts` enforces
 * that with a CIE76 colour-distance floor so a new category can't silently reintroduce look-alike greens.
 */

export type OpCategory =
  | 'matmul' | 'conv' | 'attention' | 'norm' | 'activation' | 'loss' | 'optimizer' | 'autograd'
  | 'reduction' | 'shape' | 'transfer' | 'memory' | 'elementwise' | 'overhead' | 'other'

export const LEGEND: readonly { key: OpCategory; color: string; label: string }[] = [
  { key: 'matmul', color: '#2563eb', label: 'matmul' },
  { key: 'conv', color: '#15803d', label: 'conv' },
  { key: 'attention', color: '#facc15', label: 'attention' },
  { key: 'norm', color: '#a855f7', label: 'norm' },
  { key: 'activation', color: '#0d9488', label: 'activation' },
  { key: 'loss', color: '#f472b6', label: 'loss' },
  { key: 'optimizer', color: '#dc2626', label: 'optimizer' },
  { key: 'autograd', color: '#a5b4fc', label: 'autograd' },
  { key: 'reduction', color: '#bef264', label: 'reduction' },
  { key: 'shape', color: '#22d3ee', label: 'shape/layout/indexing' },
  { key: 'transfer', color: '#f97316', label: 'transfer' },
  { key: 'memory', color: '#92400e', label: 'memory/fill' },
  { key: 'elementwise', color: '#7c8798', label: 'elementwise' },
  { key: 'overhead', color: '#e2e8f0', label: 'cuda overhead' },
  { key: 'other', color: '#3b4252', label: 'other' },
]

export const CATEGORY_COLOR: Record<OpCategory, string> = Object.fromEntries(
  LEGEND.map((l) => [l.key, l.color]),
) as Record<OpCategory, string>

/** Classify a profiler event by name (and the SDK's coarse `category`) into a timeline category. */
export function opCategory(name: string, category: string): OpCategory {
  if (category === 'memory_alloc' || category === 'memory_free') return 'memory'
  const n = name.toLowerCase()

  // Optimizer: Optimizer.step#*, Optimizer.zero_grad#*, _foreach_* (Adam/SGD multi-tensor ops)
  if (/optimizer\.(step|zero_grad)/.test(n)) return 'optimizer'
  if (/_foreach_/.test(n)) return 'optimizer'

  // CUDA API overhead and profiler artifacts
  if (/^cuda(launchkernel|memcpyasync|memsetasync|streamsynchronize|devicesynchronize|occupancy|streamiscapturing)/.test(n)) return 'overhead'
  if (/activity buffer|lazy function loading/.test(n)) return 'overhead'

  // Autograd engine: evaluate_function wrappers, named backward nodes, detach ops
  if (/autograd::engine|accumulategrad/.test(n)) return 'autograd'
  if (/backward0/.test(n)) return 'autograd'
  if (/^detach|::detach/.test(n)) return 'autograd'

  // GEMM / matmul: addmm is nn.Linear; bmm/mm are direct; sgemm/hgemm are raw CUDA kernels
  if (/(addmm|baddbmm|matmul|sgemm|hgemm|dgemm|cublas|cutlass)/.test(n)) return 'matmul'
  if (/(?:^|::)(mm|bmm)(?:$|_)/.test(n)) return 'matmul'
  if (/(?:^|::)linear(?:$|_)/.test(n)) return 'matmul'

  if (/conv/.test(n)) return 'conv'

  // Data transfer
  if (/(copy_|(?:^|::)to(?:$|\s)|^memcpy|cudamemcpy)/.test(n)) return 'transfer'

  // Normalization: layer_norm, batch_norm, linalg_vector_norm (gradient clipping), etc.
  if (/(layer_norm|batch_norm|group_norm|instance_norm|rms_norm|linalg_vector_norm|frobenius_norm)/.test(n)) return 'norm'

  // Attention / softmax / log-softmax
  if (/(attention|attn|softmax|scaled_dot|flash_attn|log_softmax|softmax_warp)/.test(n)) return 'attention'

  // Embedding lookups are gathers
  if (/embedding/.test(n)) return 'shape'

  // Activations + dropout (both are element-wise non-linear ops)
  if (/(relu|gelu|silu|sigmoid|tanh|hardswish|leaky_relu|threshold_backward|fused_dropout|native_dropout|dropout)/.test(n)) return 'activation'

  // Loss functions
  if (/(cross_entropy|nll_loss|mse_loss|bce|kl_div|hinge|margin_loss)/.test(n)) return 'loss'

  // Raw CUDA kernel name heuristics (long mangled names from the cuda_kernel category)
  if (/reduce_kernel/.test(n)) return 'reduction'
  if (/multi_tensor_apply_kernel/.test(n)) return 'optimizer'
  if (/vectorized_elementwise_kernel/.test(n)) return 'elementwise'
  if (/nll_loss.*kernel/.test(n)) return 'loss'
  if (/dropout_kernel|masked_scale/.test(n)) return 'activation'

  // Shape / layout ops, including aten::t (the .T transpose shorthand)
  if (/(view|reshape|transpose|permute|contiguous|flatten|squeeze|unsqueeze|expand|repeat|chunk|split|narrow|select|as_strided|(?:^|::)cat(?:$|_)|(?:^|::)stack(?:$|_))/.test(n)) return 'shape'
  if (/(?:^|::)t(?:$|_)/.test(n)) return 'shape'

  // Allocations, fill, memset: empty, zeros, fill_, zero_, Memset (Device)
  if (/^memset|(?:^|::)(empty|zeros|ones|full|fill_|zero_|empty_like|zeros_like|ones_like|empty_strided)(?:_|$|::)/.test(n)) return 'memory'

  // Reductions
  if (/(?:^|::)(sum|mean|amax|amin|max|min|var|std|norm|prod|cumsum|cumprod|logsumexp|argmax|argmin)(?:_|$|::)/.test(n)) return 'reduction'

  // Elementwise arithmetic
  if (/(?:^|::)(add|mul|sub|div|pow|exp|log|sqrt|abs|neg|clamp|where|sign|ceil|floor|round|lerp|addcmul|addcdiv|clamp_min|clamp_max)(?:_|$|::)/.test(n)) return 'elementwise'

  // Indexing
  if (/(index|gather|scatter|masked_fill|masked_select|nonzero)/.test(n)) return 'shape'

  return 'other'
}

export function opColor(name: string, category: string): string {
  return CATEGORY_COLOR[opCategory(name, category)]
}

function srgbToLab(hex: string): [number, number, number] {
  const c = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255)
    .map((v) => (v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4))
  const x = (0.4124 * c[0] + 0.3576 * c[1] + 0.1805 * c[2]) / 0.95047
  const y = 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]
  const z = (0.0193 * c[0] + 0.1192 * c[1] + 0.9505 * c[2]) / 1.08883
  const f = (t: number) => (t > 0.008856 ? Math.cbrt(t) : 7.787 * t + 16 / 116)
  return [116 * f(y) - 16, 500 * (f(x) - f(y)), 200 * (f(y) - f(z))]
}

/** CIE76 colour distance between two `#rrggbb` colours (about 2.3 is a just-noticeable difference). */
export function colorDistance(a: string, b: string): number {
  const [l1, a1, b1] = srgbToLab(a)
  const [l2, a2, b2] = srgbToLab(b)
  return Math.hypot(l1 - l2, a1 - a2, b1 - b2)
}
