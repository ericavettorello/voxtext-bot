"""Ориентировочный расчёт кредитов и доли стоимости подписки ElevenLabs."""

from __future__ import annotations

import math
from decimal import ROUND_HALF_UP, Decimal


def estimate_credits(char_count: int, credit_multiplier: float = 1.0) -> int:
    """Оценка кредитов: символы × множитель голоса, округление вверх."""
    if char_count < 0:
        raise ValueError("char_count не может быть отрицательным.")
    return math.ceil(char_count * float(credit_multiplier))


def estimate_cost_usd(
    estimated_credits: int,
    plan_price_usd: Decimal | None,
    plan_credits: int | None,
) -> Decimal | None:
    """Ориентировочная доля стоимости подписки. None, если тариф не настроен."""
    if plan_price_usd is None or plan_credits is None:
        return None
    if plan_credits <= 0 or plan_price_usd <= 0:
        return None
    value = (Decimal(estimated_credits) / Decimal(plan_credits)) * plan_price_usd
    return value.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
