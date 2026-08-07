import unittest

from fundval.valuation import (
    Holding,
    LatestNav,
    Quote,
    build_reconciliation,
    calculate_holding_estimate,
    calibrate_confidence,
)


class HoldingEstimateTests(unittest.TestCase):
    def test_normalizes_covered_holdings_to_estimate_growth(self):
        result = calculate_holding_estimate(
            latest_nav=LatestNav(nav=1.2, date="2026-07-30"),
            holdings=[
                Holding(name="贵州茅台", code="600519", weight_pct=30.0),
                Holding(name="五粮液", code="000858", weight_pct=20.0),
            ],
            quotes={
                "600519": Quote(code="600519", name="贵州茅台", change_pct=2.0),
                "000858": Quote(code="000858", name="五粮液", change_pct=-1.0),
            },
            min_coverage_pct=40.0,
        )

        self.assertEqual(result.status, "estimated")
        self.assertEqual(result.source, "holding")
        self.assertAlmostEqual(result.coverage_pct, 50.0)
        self.assertAlmostEqual(result.estimate_growth_pct, 0.8)
        self.assertAlmostEqual(result.estimate_nav, 1.2096)

    def test_preserves_alphabetic_holding_codes(self):
        result = calculate_holding_estimate(
            latest_nav=LatestNav(nav=1.0, date="2026-08-04"),
            holdings=[
                Holding(name="Ciena", code="CIEN", weight_pct=50.0),
                Holding(name="TSMC", code="TSM", weight_pct=50.0),
            ],
            quotes={
                "CIEN": Quote(code="CIEN", name="Ciena", change_pct=2.0),
                "TSM": Quote(code="TSM", name="TSMC", change_pct=-1.0),
            },
        )

        self.assertEqual(result.status, "estimated")
        self.assertAlmostEqual(result.estimate_growth_pct, 0.5)
        self.assertEqual([item.code for item in result.contributions], ["CIEN", "TSM"])
        self.assertEqual([item.change_pct for item in result.contributions], [2.0, -1.0])

    def test_returns_unavailable_when_quote_coverage_is_too_low(self):
        result = calculate_holding_estimate(
            latest_nav=LatestNav(nav=1.0, date="2026-07-30"),
            holdings=[
                Holding(name="贵州茅台", code="600519", weight_pct=20.0),
                Holding(name="宁德时代", code="300750", weight_pct=20.0),
            ],
            quotes={
                "600519": Quote(code="600519", name="贵州茅台", change_pct=1.0),
            },
            min_coverage_pct=30.0,
        )

        self.assertEqual(result.status, "unavailable")
        self.assertEqual(result.reason, "low_coverage")
        self.assertAlmostEqual(result.coverage_pct, 20.0)
        self.assertIsNone(result.estimate_nav)

    def test_builds_reconciliation_error_metrics(self):
        reconciliation = build_reconciliation(
            {
                "snapshot_key": "2026-07-31 15:00",
                "snapshot_date": "2026-07-31",
                "code": "161725",
                "source": "holding",
                "estimate_nav": 1.01,
                "estimate_growth_pct": 1.0,
                "latest_nav": 1.0,
                "latest_nav_date": "2026-07-30",
            },
            actual_nav=1.009,
            actual_nav_date="2026-07-31",
            reconciled_at="2026-08-01 08:00:00",
        )

        self.assertEqual(reconciliation["code"], "161725")
        self.assertAlmostEqual(reconciliation["actual_growth_pct"], 0.9)
        self.assertAlmostEqual(reconciliation["growth_error_pct"], 0.1)
        self.assertAlmostEqual(reconciliation["abs_growth_error_pct"], 0.1)
        self.assertAlmostEqual(reconciliation["nav_error_pct"], 0.099108)
        self.assertAlmostEqual(reconciliation["abs_nav_error_pct"], 0.099108)

    def test_calibrates_confidence_with_enough_history(self):
        strong_profile = {
            "sample_count": 30,
            "mean_abs_nav_error_pct": 0.2,
            "direction_accuracy_pct": 90.0,
        }
        weak_profile = {
            "sample_count": 30,
            "mean_abs_nav_error_pct": 3.0,
            "direction_accuracy_pct": 30.0,
        }
        sparse_profile = {
            "sample_count": 4,
            "mean_abs_nav_error_pct": 0.1,
            "direction_accuracy_pct": 100.0,
        }

        self.assertAlmostEqual(calibrate_confidence(50.0, strong_profile), 74.0)
        self.assertAlmostEqual(calibrate_confidence(90.0, weak_profile), 51.0)
        self.assertAlmostEqual(calibrate_confidence(50.0, sparse_profile), 50.0)


if __name__ == "__main__":
    unittest.main()
