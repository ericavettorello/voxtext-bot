"""Тесты ориентировочного расчёта кредитов и стоимости."""

from __future__ import annotations

import unittest
from decimal import Decimal

from services.usage_estimator import estimate_cost_usd, estimate_credits


class UsageEstimatorTests(unittest.TestCase):
    def test_credits_round_up(self) -> None:
        self.assertEqual(estimate_credits(10, 1.0), 10)
        self.assertEqual(estimate_credits(10, 1.5), 15)
        self.assertEqual(estimate_credits(3, 1.5), 5)

    def test_cost_uses_decimal(self) -> None:
        cost = estimate_cost_usd(15, Decimal("5"), 100)
        self.assertIsInstance(cost, Decimal)
        self.assertEqual(cost, Decimal("0.750000"))

    def test_cost_is_none_without_plan(self) -> None:
        self.assertIsNone(estimate_cost_usd(15, None, None))
        self.assertIsNone(estimate_cost_usd(15, Decimal("5"), None))
        self.assertIsNone(estimate_cost_usd(15, None, 100))
        self.assertIsNone(estimate_cost_usd(15, Decimal("0"), 100))
        self.assertIsNone(estimate_cost_usd(15, Decimal("5"), 0))


if __name__ == "__main__":
    unittest.main()
