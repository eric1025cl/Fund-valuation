import unittest

from fundval.factor_fit import FactorPoint, fit_factor_model
from fundval.valuation import LatestNav, Quote


def points_from_returns(start_date, start_value, returns):
    year, month, day = start_date
    points = [FactorPoint(date=f"{year:04d}-{month:02d}-{day:02d}", value=start_value)]
    value = start_value
    for index, growth_pct in enumerate(returns, start=1):
        value *= 1 + growth_pct / 100.0
        points.append(FactorPoint(date=f"{year:04d}-{month:02d}-{day + index:02d}", value=value))
    return points


class FactorFitTests(unittest.TestCase):
    def test_fits_recent_factor_exposure_to_estimate_current_growth(self):
        fund_history = points_from_returns((2026, 7, 1), 1.0, [1.0, 1.0, 1.0, 1.0, 1.0])
        factor_histories = {
            "sh000300": points_from_returns((2026, 7, 1), 100.0, [1.0, 1.0, 1.0, 1.0, 1.0]),
            "sh000905": points_from_returns((2026, 7, 1), 100.0, [-0.5, -0.4, -0.3, -0.2, -0.1]),
        }

        result = fit_factor_model(
            latest_nav=LatestNav(nav=1.05, date="2026-07-06"),
            fund_history=fund_history,
            factor_histories=factor_histories,
            factor_quotes={
                "sh000300": Quote(code="sh000300", name="CSI 300", change_pct=2.0),
                "sh000905": Quote(code="sh000905", name="CSI 500", change_pct=-1.0),
            },
            min_observations=4,
        )

        self.assertEqual(result.status, "estimated")
        self.assertEqual(result.source, "factor_fit")
        self.assertAlmostEqual(result.estimate_growth_pct, 2.0, places=3)
        self.assertAlmostEqual(result.estimate_nav, 1.071, places=3)
        self.assertGreater(result.fit_r2, 0.95)
        self.assertLess(result.fit_residual_pct, 0.01)
        self.assertEqual(result.factor_exposures[0].code, "sh000300")

    def test_scores_style_drift_when_recent_exposure_changes(self):
        old_returns = [1.0] * 8
        recent_returns = [0.4] * 8
        fund_history = points_from_returns((2026, 7, 1), 1.0, old_returns + recent_returns)
        factor_histories = {
            "old_style": points_from_returns((2026, 7, 1), 100.0, old_returns + [0.0] * 8),
            "new_style": points_from_returns((2026, 7, 1), 100.0, [0.0] * 8 + recent_returns),
        }

        result = fit_factor_model(
            latest_nav=LatestNav(nav=1.12, date="2026-07-17"),
            fund_history=fund_history,
            factor_histories=factor_histories,
            factor_quotes={
                "old_style": Quote(code="old_style", name="Old Style", change_pct=0.0),
                "new_style": Quote(code="new_style", name="New Style", change_pct=1.5),
            },
            min_observations=6,
            recent_window=6,
        )

        self.assertEqual(result.status, "estimated")
        self.assertGreaterEqual(result.style_drift_score, 40.0)
        self.assertIn(result.style_drift_level, {"medium", "high"})
        self.assertEqual(result.recent_factor_exposures[0].code, "new_style")
        self.assertEqual(result.baseline_factor_exposures[0].code, "old_style")


if __name__ == "__main__":
    unittest.main()
