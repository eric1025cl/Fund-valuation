import unittest
from pathlib import Path


class StaticPageTests(unittest.TestCase):
    def test_explains_coverage_and_confidence_rules(self):
        html = Path("web/index.html").read_text(encoding="utf-8")

        self.assertIn("覆盖率 = 参与估值的持仓权重合计", html)
        self.assertIn("置信度不是准确率概率", html)
        self.assertIn("低覆盖率估值仅代表方向参考", html)
        self.assertIn("指数拟合", html)
        self.assertIn("风格漂移", html)
        self.assertIn("两者接近且 R² 高", html)
        self.assertIn("两者分歧大且 R² 高", html)
        self.assertNotIn("会用基金历史净值涨跌拟合宽基/行业指数暴露", html)
        self.assertIn("估值风险", html)
        self.assertIn("未覆盖仓位", html)

    def test_explains_trading_refresh_and_snapshot_date(self):
        html = Path("web/index.html").read_text(encoding="utf-8")
        js = Path("web/app.js").read_text(encoding="utf-8")

        self.assertIn("交易日 9:00-15:00 页面打开时每 10 分钟自动刷新", html)
        self.assertIn("非交易日优先使用上一交易日 15:00 估值快照，没有快照则重新计算", html)
        self.assertIn("每个交易日 15:05 自动保存", html)
        self.assertNotIn("每个交易日 15:00 自动保存", html)
        self.assertIn("估值交易日", html)
        self.assertIn("快照日期", html)
        self.assertIn("实际净值", js)
        self.assertIn("净值涨跌幅", js)
        self.assertIn("估值日", js)
        self.assertIn("本地交易日", js)
        self.assertNotIn("实际净值日", js)
        self.assertNotIn("基准净值日", js)
        self.assertIn("function estimateRiskLabel", js)
        self.assertIn("估值风险", js)
        self.assertIn("指数拟合", js)
        self.assertIn("风格漂移", js)
        self.assertIn("actual_growth_pct", js)
        self.assertIn("const AUTO_REFRESH_INTERVAL_MS = 10 * 60 * 1000", js)
        self.assertIn("function isTradingRefreshWindow", js)
        self.assertIn("/api/trading-status", js)
        self.assertIn("document.hidden", js)

    def test_shows_reconciliation_controls_and_records(self):
        html = Path("web/index.html").read_text(encoding="utf-8")
        js = Path("web/app.js").read_text(encoding="utf-8")

        self.assertIn("reconcileButton", html)
        self.assertIn("reconciliationList", html)
        self.assertIn("reconciliationStatus", html)
        self.assertIn("/api/reconciliations", js)
        self.assertIn("function loadReconciliations", js)
        self.assertIn("function renderReconciliations", js)

    def test_shows_estimate_notes_in_dedicated_column(self):
        js = Path("web/app.js").read_text(encoding="utf-8")
        css = Path("web/styles.css").read_text(encoding="utf-8")

        self.assertIn("<span>说明</span>", js)
        self.assertIn("function estimateExplanationItems", js)
        self.assertIn("fund-explanation", js)
        self.assertIn(".fund-explanation", css)
        self.assertIn("white-space: normal", css)

    def test_fund_table_fits_page_without_horizontal_scroll(self):
        css = Path("web/styles.css").read_text(encoding="utf-8")

        self.assertNotIn("overflow-x: auto;", css[css.index(".fund-card") : css.index(".fund-card.snapshot-fund-card")])
        self.assertNotIn("min-width: 1240px", css)
        self.assertNotIn("min-width: 1340px", css)
        self.assertIn(".fund-row {\n    grid-template-columns: 1fr", css)
        self.assertIn(".fund-head {", css)


if __name__ == "__main__":
    unittest.main()
