from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict

import numpy as np


# ============================================================
# Constants
# ============================================================

HOURS_PER_MONTH = 730  # industry-standard billing assumption
SECONDS_PER_MONTH = 30 * 24 * 3600  # 2,592,000

# Bytes per parameter at each quantization level
BYTES_PER_PARAM = {"fp16": 2, "int8": 1, "int4": 0.5}

# Realistic efficiency factor applied to theoretical peak
# hardware throughput.  Accounts for kernel overhead, memory
# controller contention, tensor-parallel communication, etc.
HW_EFFICIENCY = 0.65


# ============================================================
# GPU Instance
# ============================================================


@dataclass(slots=True)
class GPUInstance:
    """
    An AWS EC2 GPU instance type.

    Pricing is on-demand, us-east-1 unless noted.
    Hardware specs are used by the throughput estimator.
    """

    name: str
    gpu_model: str
    gpu_count: int
    gpu_memory_gb: float  # per-GPU VRAM
    hourly_cost: float  # USD, on-demand

    # ── hardware specs (per-GPU) ──
    memory_bandwidth_gbs: float = 0.0  # GB/s per GPU
    fp16_tflops: float = 0.0  # FP16 tensor-core TFLOPS per GPU

    @property
    def total_gpu_memory_gb(self) -> float:
        return self.gpu_count * self.gpu_memory_gb

    @property
    def aggregate_bandwidth_gbs(self) -> float:
        """Total memory bandwidth across all GPUs (GB/s)."""
        return self.gpu_count * self.memory_bandwidth_gbs

    @property
    def aggregate_fp16_tflops(self) -> float:
        """Total FP16 tensor-core TFLOPS across all GPUs."""
        return self.gpu_count * self.fp16_tflops

    @property
    def monthly_cost(self) -> float:
        return self.hourly_cost * HOURS_PER_MONTH

    def __repr__(self) -> str:
        return (
            f"GPUInstance({self.name}, "
            f"{self.gpu_count}× {self.gpu_model} "
            f"[{self.total_gpu_memory_gb:.0f} GB], "
            f"${self.hourly_cost:.2f}/hr → "
            f"${self.monthly_cost:,.0f}/mo)"
        )


# ============================================================
# Open-Source Model
# ============================================================


@dataclass(slots=True)
class OpenSourceModel:
    """
    An open-source model's hardware-relevant specs.

    Architecture params (num_layers, num_kv_heads, head_dim) are
    needed for KV-cache memory estimation, which determines the
    maximum concurrent batch size on a given GPU instance.
    """

    name: str
    params_billions: float
    architecture: str  # "dense" or "moe"
    active_params_billions: float
    fp16_memory_gb: float  # approximate VRAM for weights in FP16

    # ── architecture params for KV-cache estimation ──
    num_layers: int = 0
    num_kv_heads: int = 0  # GQA heads (< num_attention_heads)
    head_dim: int = 0

    @property
    def int8_memory_gb(self) -> float:
        return self.fp16_memory_gb / 2

    @property
    def int4_memory_gb(self) -> float:
        return self.fp16_memory_gb / 4

    def weight_memory_gb(self, quantization: str) -> float:
        """Model weight memory at a given quantization level."""
        bytes_per = BYTES_PER_PARAM[quantization]
        return self.params_billions * 1e9 * bytes_per / (1024**3)

    def kv_cache_per_token_bytes(self) -> float:
        """
        KV-cache memory per token across all layers (FP16).

        Per layer: 2 (K + V) × num_kv_heads × head_dim × 2 bytes
        """
        per_layer = 2 * self.num_kv_heads * self.head_dim * 2  # bytes
        return per_layer * self.num_layers

    def kv_cache_per_sequence_gb(self, context_length: int) -> float:
        """Total KV-cache memory for one sequence at a given context length."""
        return self.kv_cache_per_token_bytes() * context_length / (1024**3)

    def __repr__(self) -> str:
        arch = self.architecture.upper()
        active = (
            f" ({self.active_params_billions:.0f}B active)"
            if self.architecture == "moe"
            else ""
        )
        return (
            f"{self.name} — {self.params_billions:.0f}B {arch}{active}, "
            f"FP16≈{self.fp16_memory_gb:.0f} GB"
        )


# ============================================================
# Self-Hosted Inference Configuration
# ============================================================


@dataclass(slots=True)
class SelfHostedConfig:
    """
    A concrete deployment configuration: one model on one
    instance type at a specific quantization level, with
    estimated throughput numbers.

    Throughput figures are *per-instance, batched* estimates
    drawn from published vLLM / TGI benchmarks for realistic
    online-serving workloads.

    The key throughput bottleneck for long-output generation
    is decode throughput (output tokens/sec).  Prefill
    throughput is tracked for latency estimation but does not
    drive the capacity model.
    """

    label: str
    instance: GPUInstance
    model: OpenSourceModel
    quantization: str  # "fp16", "int8", "int4"

    # ── per-instance, batched throughput estimates ──
    output_tokens_per_second: float  # aggregate decode tok/s
    prefill_tokens_per_second: float  # aggregate prefill tok/s

    # ── helpers ──

    @property
    def instance_monthly_cost(self) -> float:
        return self.instance.monthly_cost

    def _capacity_appeals_per_second(
        self,
        output_tokens_per_appeal: int,
    ) -> float:
        """Max appeals/sec one instance can sustain (decode-bound)."""
        return self.output_tokens_per_second / output_tokens_per_appeal

    # ── scalar methods ──

    def required_instances(
        self,
        appeals_per_month: int | float,
        output_tokens_per_appeal: int,
        headroom: float = 0.30,
    ) -> int:
        """Instances needed to serve the given monthly volume."""
        demand = appeals_per_month / SECONDS_PER_MONTH
        capacity = self._capacity_appeals_per_second(output_tokens_per_appeal)
        raw = demand / capacity
        return max(1, math.ceil(raw * (1 + headroom)))

    def monthly_cost(
        self,
        appeals_per_month: int | float,
        output_tokens_per_appeal: int,
        headroom: float = 0.30,
    ) -> float:
        n = self.required_instances(
            appeals_per_month,
            output_tokens_per_appeal,
            headroom,
        )
        return n * self.instance_monthly_cost

    def cost_per_appeal(
        self,
        appeals_per_month: int | float,
        output_tokens_per_appeal: int,
        headroom: float = 0.30,
    ) -> float:
        if appeals_per_month <= 0:
            return float("inf")
        return (
            self.monthly_cost(
                appeals_per_month,
                output_tokens_per_appeal,
                headroom,
            )
            / appeals_per_month
        )

    # ── vectorized methods (for plotting) ──

    def monthly_cost_curve(
        self,
        appeals_array: np.ndarray,
        output_tokens_per_appeal: int,
        headroom: float = 0.30,
    ) -> np.ndarray:
        """Monthly costs across an array of volumes.  Returns step-function."""
        demand = appeals_array / SECONDS_PER_MONTH
        capacity = self._capacity_appeals_per_second(output_tokens_per_appeal)
        raw = demand / capacity
        instances = np.maximum(1, np.ceil(raw * (1 + headroom)))
        return instances * self.instance_monthly_cost

    def cost_per_appeal_curve(
        self,
        appeals_array: np.ndarray,
        output_tokens_per_appeal: int,
        headroom: float = 0.30,
    ) -> np.ndarray:
        """Per-appeal cost across an array of volumes."""
        monthly = self.monthly_cost_curve(
            appeals_array,
            output_tokens_per_appeal,
            headroom,
        )
        with np.errstate(divide="ignore", invalid="ignore"):
            cpa = np.where(appeals_array > 0, monthly / appeals_array, np.inf)
        return cpa

    def instance_curve(
        self,
        appeals_array: np.ndarray,
        output_tokens_per_appeal: int,
        headroom: float = 0.30,
    ) -> np.ndarray:
        """Number of instances across an array of volumes."""
        demand = appeals_array / SECONDS_PER_MONTH
        capacity = self._capacity_appeals_per_second(output_tokens_per_appeal)
        raw = demand / capacity
        return np.maximum(1, np.ceil(raw * (1 + headroom))).astype(int)

    def latency_per_appeal_seconds(
        self,
        input_tokens_per_appeal: int,
        output_tokens_per_appeal: int,
    ) -> float:
        """
        Rough single-request latency estimate (seconds).

        Assumes the request gets full instance throughput divided
        by a typical concurrent batch size.  This is a simplified
        model — real latency depends heavily on load and batching.
        """
        prefill_time = input_tokens_per_appeal / self.prefill_tokens_per_second
        decode_time = output_tokens_per_appeal / (self.output_tokens_per_second / 8)
        return prefill_time + decode_time

    def __repr__(self) -> str:
        return (
            f"SelfHostedConfig(\n"
            f"  label='{self.label}',\n"
            f"  instance={self.instance.name},\n"
            f"  model={self.model.name},\n"
            f"  quantization={self.quantization},\n"
            f"  output_tok/s={self.output_tokens_per_second:,.0f},\n"
            f"  monthly_cost/instance=${self.instance_monthly_cost:,.0f}\n"
            f")"
        )


# ============================================================
# Pre-Defined AWS GPU Instances
# ============================================================

# Pricing: on-demand, us-east-1 (as of mid-2025, approximate)
# Bandwidth & TFLOPS from official NVIDIA data sheets.

G6E_48XLARGE = GPUInstance(
    name="g6e.48xlarge",
    gpu_model="NVIDIA L40S",
    gpu_count=8,
    gpu_memory_gb=48,
    hourly_cost=21.65,
    memory_bandwidth_gbs=864,
    fp16_tflops=362,
)

P4DE_24XLARGE = GPUInstance(
    name="p4de.24xlarge",
    gpu_model="NVIDIA A100 80 GB",
    gpu_count=8,
    gpu_memory_gb=80,
    hourly_cost=40.97,
    memory_bandwidth_gbs=2_039,
    fp16_tflops=312,
)

P5_48XLARGE = GPUInstance(
    name="p5.48xlarge",
    gpu_model="NVIDIA H100 80 GB",
    gpu_count=8,
    gpu_memory_gb=80,
    hourly_cost=98.32,
    memory_bandwidth_gbs=3_350,
    fp16_tflops=990,
)


# ============================================================
# Pre-Defined Open-Source Models
# ============================================================

LLAMA_3_3_70B = OpenSourceModel(
    name="Llama 3.3 70B",
    params_billions=70,
    architecture="dense",
    active_params_billions=70,
    fp16_memory_gb=140,
    num_layers=80,
    num_kv_heads=8,  # GQA: 8 KV heads (64 Q heads)
    head_dim=128,
)

QWEN_3_235B = OpenSourceModel(
    name="Qwen 3 235B-A22B",
    params_billions=235,
    architecture="moe",
    active_params_billions=22,
    fp16_memory_gb=470,  # total weights; INT4 ≈ 118 GB
    num_layers=94,
    num_kv_heads=4,  # GQA: 4 KV heads
    head_dim=192,
)


# ============================================================
# Pre-Built Inference Configurations
# ============================================================
#
# Throughput estimates are conservative, based on published
# vLLM benchmarks for batched online serving with ~60K context.
# Real-world numbers vary — the notebook exposes sliders to
# adjust these assumptions interactively.
#
# Decode throughput = aggregate output tokens/sec across all
# concurrent requests on one instance.

CONFIGS: dict[str, SelfHostedConfig] = {
    "L40S — Llama 70B INT8": SelfHostedConfig(
        label="8×L40S — Llama 3.3 70B (INT8)",
        instance=G6E_48XLARGE,
        model=LLAMA_3_3_70B,
        quantization="int8",
        output_tokens_per_second=1_500,
        prefill_tokens_per_second=15_000,
    ),
    "A100 — Llama 70B FP16": SelfHostedConfig(
        label="8×A100 80 GB — Llama 3.3 70B (FP16)",
        instance=P4DE_24XLARGE,
        model=LLAMA_3_3_70B,
        quantization="fp16",
        output_tokens_per_second=2_500,
        prefill_tokens_per_second=25_000,
    ),
    "H100 — Llama 70B FP16": SelfHostedConfig(
        label="8×H100 — Llama 3.3 70B (FP16)",
        instance=P5_48XLARGE,
        model=LLAMA_3_3_70B,
        quantization="fp16",
        output_tokens_per_second=4_500,
        prefill_tokens_per_second=45_000,
    ),
}


# ============================================================
# Throughput Estimator
# ============================================================


def estimate_throughput(
    instance: GPUInstance,
    model: OpenSourceModel,
    quantization: str,
    context_length: int,
    efficiency: float = HW_EFFICIENCY,
) -> Dict[str, float]:
    """
    Estimate decode and prefill throughput from first principles
    using a simplified roofline model.

    Decode (autoregressive generation) is **memory-bandwidth bound**:
      - Each generated token requires reading all model weights once.
      - With batching, B tokens are produced per weight-read pass.
      - throughput = batch_size × bandwidth / model_weight_bytes

    Prefill (prompt processing) is **compute bound**:
      - All input tokens are processed in parallel via large matmuls.
      - throughput ≈ FLOPS / (2 × active_params)

    The maximum concurrent batch size is constrained by VRAM:
      - available = total_gpu_memory − model_weights
      - max_batch = available / kv_cache_per_sequence

    Returns a dict with all intermediate values so the notebook
    can display exactly how each number was derived.
    """
    weight_bytes = BYTES_PER_PARAM[quantization]
    model_memory_gb = model.params_billions * 1e9 * weight_bytes / (1024**3)

    available_vram_gb = instance.total_gpu_memory_gb - model_memory_gb

    kv_per_seq_gb = model.kv_cache_per_sequence_gb(context_length)
    if kv_per_seq_gb > 0:
        max_batch_size = int(available_vram_gb / kv_per_seq_gb)
    else:
        max_batch_size = 64  # fallback if model arch not specified
    max_batch_size = max(1, max_batch_size)

    # ── Decode throughput (memory-bandwidth bound) ──
    effective_bw = instance.aggregate_bandwidth_gbs * efficiency
    model_weight_gb = model_memory_gb  # read all weights per decode step
    single_stream_tok_s = effective_bw / model_weight_gb
    decode_tok_s = max_batch_size * single_stream_tok_s

    # ── Prefill throughput (compute bound) ──
    effective_tflops = instance.aggregate_fp16_tflops * efficiency
    flops_per_token = 2 * model.active_params_billions * 1e9  # ~2 FLOPs/param
    prefill_tok_s = (effective_tflops * 1e12) / flops_per_token

    return {
        "model_memory_gb": model_memory_gb,
        "available_vram_gb": available_vram_gb,
        "kv_per_seq_gb": kv_per_seq_gb,
        "max_batch_size": max_batch_size,
        "single_stream_decode_tok_s": single_stream_tok_s,
        "batched_decode_tok_s": decode_tok_s,
        "prefill_tok_s": prefill_tok_s,
    }


# ============================================================
# Convenience: color palette for consistent plotting
# ============================================================

PALETTE = {
    # API models
    "claude-haiku-4.5": "#2ecc71",
    "claude-sonnet-4.5": "#3498db",
    # Self-hosted configs
    "L40S — Llama 70B INT8": "#e67e22",
    "A100 — Llama 70B FP16": "#9b59b6",
    "H100 — Llama 70B FP16": "#e74c3c",
}
