from __future__ import annotations

from dataclasses import dataclass
from typing import Dict


# ============================================================
# Token Usage
# ============================================================


@dataclass(slots=True)
class TokenUsage:
    """
    Represents token usage for a single request, batch, customer,
    or arbitrary aggregation window.

    All token counts are raw integer token counts.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        """
        Total tokens across all categories.
        """
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
        )

    def to_millions(self) -> Dict[str, float]:
        """
        Returns all token categories normalized to millions of tokens.
        Useful because most providers bill per 1M tokens.
        """
        return {
            "input_tokens_m": self.input_tokens / 1_000_000,
            "output_tokens_m": self.output_tokens / 1_000_000,
            "cache_read_tokens_m": self.cache_read_tokens / 1_000_000,
            "cache_write_tokens_m": self.cache_write_tokens / 1_000_000,
            "total_tokens_m": self.total_tokens / 1_000_000,
        }

    def __add__(self, other: "TokenUsage") -> "TokenUsage":
        """
        Allows aggregation of usage objects.

        Example:
            monthly_usage = week1 + week2 + week3
        """
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
        )

    def __repr__(self) -> str:
        return (
            "TokenUsage(\n"
            f"  input_tokens={self.input_tokens:,},\n"
            f"  output_tokens={self.output_tokens:,},\n"
            f"  cache_read_tokens={self.cache_read_tokens:,},\n"
            f"  cache_write_tokens={self.cache_write_tokens:,},\n"
            f"  total_tokens={self.total_tokens:,}\n"
            ")"
        )


# ============================================================
# Model Pricing
# ============================================================


@dataclass(slots=True)
class ModelPricing:
    """
    Represents provider pricing for an LLM.

    All pricing is specified in:
        USD per 1M tokens
    """

    model_name: str

    input_cost_per_million: float
    output_cost_per_million: float

    cache_read_cost_per_million: float = 0.0
    cache_write_cost_per_million: float = 0.0

    provider: str = "Unknown"

    def calculate_cost(self, usage: TokenUsage) -> "CostBreakdown":
        """
        Calculates cost for a given TokenUsage object.
        """

        input_cost = (usage.input_tokens / 1_000_000) * self.input_cost_per_million

        output_cost = (usage.output_tokens / 1_000_000) * self.output_cost_per_million

        cache_read_cost = (
            usage.cache_read_tokens / 1_000_000
        ) * self.cache_read_cost_per_million

        cache_write_cost = (
            usage.cache_write_tokens / 1_000_000
        ) * self.cache_write_cost_per_million

        return CostBreakdown(
            model_name=self.model_name,
            usage=usage,
            input_cost=input_cost,
            output_cost=output_cost,
            cache_read_cost=cache_read_cost,
            cache_write_cost=cache_write_cost,
        )

    def __repr__(self) -> str:
        return (
            "ModelPricing(\n"
            f"  provider='{self.provider}',\n"
            f"  model_name='{self.model_name}',\n"
            f"  input_cost_per_million=${self.input_cost_per_million:.2f},\n"
            f"  output_cost_per_million=${self.output_cost_per_million:.2f},\n"
            f"  cache_read_cost_per_million=${self.cache_read_cost_per_million:.2f},\n"
            f"  cache_write_cost_per_million=${self.cache_write_cost_per_million:.2f}\n"
            ")"
        )


# ============================================================
# Cost Breakdown
# ============================================================


@dataclass(slots=True)
class CostBreakdown:
    """
    Represents calculated cost results for a usage object
    against a particular pricing model.
    """

    model_name: str
    usage: TokenUsage

    input_cost: float
    output_cost: float
    cache_read_cost: float
    cache_write_cost: float

    @property
    def total_cost(self) -> float:
        return (
            self.input_cost
            + self.output_cost
            + self.cache_read_cost
            + self.cache_write_cost
        )

    def category_breakdown(self) -> Dict[str, float]:
        """
        Returns structured cost breakdown.
        """

        return {
            "input_cost": self.input_cost,
            "output_cost": self.output_cost,
            "cache_read_cost": self.cache_read_cost,
            "cache_write_cost": self.cache_write_cost,
            "total_cost": self.total_cost,
        }

    def pretty_print(self) -> None:
        """
        Human-readable console output.
        """

        print("=" * 60)
        print(f"Model: {self.model_name}")
        print("-" * 60)

        print(f"{'Input Cost:':25s} ${self.input_cost:,.4f}")
        print(f"{'Output Cost:':25s} ${self.output_cost:,.4f}")
        print(f"{'Cache Read Cost:':25s} ${self.cache_read_cost:,.4f}")
        print(f"{'Cache Write Cost:':25s} ${self.cache_write_cost:,.4f}")

        print("-" * 60)

        print(f"{'TOTAL COST:':25s} ${self.total_cost:,.4f}")

        print("=" * 60)

    def __repr__(self) -> str:
        return (
            "CostBreakdown(\n"
            f"  model_name='{self.model_name}',\n"
            f"  input_cost=${self.input_cost:.4f},\n"
            f"  output_cost=${self.output_cost:.4f},\n"
            f"  cache_read_cost=${self.cache_read_cost:.4f},\n"
            f"  cache_write_cost=${self.cache_write_cost:.4f},\n"
            f"  total_cost=${self.total_cost:.4f}\n"
            ")"
        )


# ============================================================
# Claude Pricing Definitions
# ============================================================

# NOTE:
# These are examples based on commonly published pricing.
# Verify exact Bedrock regional pricing before production usage.


CLAUDE_HAIKU_45 = ModelPricing(
    provider="Anthropic / AWS Bedrock",
    model_name="claude-haiku-4.5",
    input_cost_per_million=1.00,
    output_cost_per_million=5.00,
    cache_read_cost_per_million=0.10,
    cache_write_cost_per_million=1.25,
)

CLAUDE_SONNET_45 = ModelPricing(
    provider="Anthropic / AWS Bedrock",
    model_name="claude-sonnet-4.5",
    input_cost_per_million=3.00,
    output_cost_per_million=15.00,
    cache_read_cost_per_million=0.30,
    cache_write_cost_per_million=3.75,
)

CLAUDE_OPUS_4 = ModelPricing(
    provider="Anthropic / AWS Bedrock",
    model_name="claude-opus-4",
    input_cost_per_million=15.00,
    output_cost_per_million=75.00,
    cache_read_cost_per_million=1.50,
    cache_write_cost_per_million=18.75,
)


# ============================================================
# Example Usage
# ============================================================

if __name__ == "__main__":
    usage = TokenUsage(
        input_tokens=58_510,
        output_tokens=8_525,
        cache_read_tokens=157_106,
        cache_write_tokens=48_119,
    )

    print(usage)
    print()

    print(CLAUDE_HAIKU_45)
    print()

    cost = CLAUDE_HAIKU_45.calculate_cost(usage)

    print(cost)
    print()

    cost.pretty_print()
