import unittest
import threading
import time
from datetime import datetime, timedelta

import pandas as pd

from fundval.factor_fit import MarketFactor
from fundval.providers import AkshareProvider, _tencent_symbol


class FakeAk:
    def __init__(self):
        self.calls = []

    def fund_portfolio_hold_em(self, symbol, date):
        self.calls.append(date)
        target_year = str(datetime.now().year - 2)
        if date == target_year:
            return pd.DataFrame(
                [
                    {
                        "股票代码": "600519",
                        "股票名称": "贵州茅台",
                        "占净值比例": "30.0",
                    }
                ]
            )
        return pd.DataFrame()


class FakeLatestAk:
    def __init__(self):
        self.calls = []

    def fund_portfolio_hold_em(self, symbol, date):
        self.calls.append(date)
        if date == "":
            return pd.DataFrame(
                [
                    {
                        "股票代码": "000858",
                        "股票名称": "五粮液",
                        "占净值比例": 20.0,
                    }
                ]
            )
        return pd.DataFrame()


class FailingHoldingsAk:
    def fund_portfolio_hold_em(self, symbol, date):
        raise ValueError("upstream parser failed")


class EmptyLatestHoldingsAk:
    def __init__(self):
        self.calls = []

    def fund_portfolio_hold_em(self, symbol, date):
        self.calls.append(date)
        return pd.DataFrame()


class FakeQuoteAk:
    def stock_zh_a_spot_em(self):
        return pd.DataFrame(
            [
                {"代码": "600519", "名称": "贵州茅台", "涨跌幅": 2.0},
            ]
        )

    def stock_hk_spot_em(self):
        return pd.DataFrame(
            [
                {"代码": "00700", "名称": "腾讯控股", "涨跌幅": 1.5},
            ]
        )


class FakeEstimateAk:
    def fund_value_estimation_em(self, symbol):
        return pd.DataFrame(
            [
                {
                    "基金代码": "161725",
                    "交易日-估算数据-估算值": "1.2345",
                    "交易日-估算数据-估算增长率": "1.23%",
                    "估算时间": "2026/07/31 15:00:00",
                }
            ]
        )


class FakeFactorAk:
    def fund_open_fund_info_em(self, symbol, indicator):
        return pd.DataFrame(
            [
                {"净值日期": "2026-07-29", "单位净值": 1.0},
                {"净值日期": "2026-07-30", "单位净值": 1.01},
                {"净值日期": "2026-07-31", "单位净值": 1.0201},
            ]
        )

    def stock_zh_index_daily(self, symbol):
        return pd.DataFrame(
            [
                {"date": "2026-07-29", "close": 100.0},
                {"date": "2026-07-30", "close": 101.0},
                {"date": "2026-07-31", "close": 102.01},
            ]
        )

    def stock_zh_index_spot_em(self):
        return pd.DataFrame(
            [
                {"代码": "000300", "名称": "沪深300", "涨跌幅": 1.5},
            ]
        )


class FakeTrackingIndexAk:
    def stock_zh_index_spot_em(self):
        return pd.DataFrame(columns=["代码", "名称", "涨跌幅"])

    def stock_zh_index_daily(self, symbol):
        if symbol == "sz399986":
            return pd.DataFrame(
                [
                    {"date": "2026-08-03", "close": 100.0},
                    {"date": "2026-08-04", "close": 97.44},
                ]
            )
        if symbol == "sh931151":
            return pd.DataFrame(
                [
                    {"date": "2026-08-03", "close": 100.0},
                    {"date": "2026-08-04", "close": 103.0},
                ]
            )
        else:
            raise AssertionError(f"unexpected tracking index symbol {symbol}")


class FakeQdiiBenchmarkAk:
    def stock_us_daily(self, symbol):
        if symbol != "QQQ":
            raise AssertionError(f"unexpected US symbol {symbol}")
        return pd.DataFrame(
            [
                {"date": "2026-07-31", "close": 100.0},
                {"date": "2026-08-03", "close": 102.0},
            ]
        )

    def forex_hist_em(self, symbol):
        if symbol != "USDCNYC":
            raise AssertionError(f"unexpected FX symbol {symbol}")
        return pd.DataFrame(
            [
                {"日期": "2026-07-31", "最新价": 7.0},
                {"日期": "2026-08-03", "最新价": 7.035},
            ]
        )


class FakeHistoricalQuoteAk:
    def stock_us_daily(self, symbol):
        if symbol != "CIEN":
            raise AssertionError(f"unexpected US symbol {symbol}")
        return pd.DataFrame(
            [
                {"date": "2026-08-04", "close": 100.0},
                {"date": "2026-08-05", "close": 102.0},
            ]
        )

    def stock_hk_daily(self, symbol):
        if symbol != "01888":
            raise AssertionError(f"unexpected HK symbol {symbol}")
        return pd.DataFrame(
            [
                {"date": "2026-08-04", "close": 20.0},
                {"date": "2026-08-05", "close": 19.0},
            ]
        )

    def stock_zh_a_hist(self, symbol, period, adjust):
        if symbol != "300750":
            raise AssertionError(f"unexpected A-share symbol {symbol}")
        return pd.DataFrame(
            [
                {"日期": "2026-08-04", "收盘": 200.0},
                {"日期": "2026-08-05", "收盘": 210.0},
            ]
        )


class SlowFactorHistoryAk:
    def __init__(self):
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()

    def stock_zh_index_daily(self, symbol):
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(0.05)
            return pd.DataFrame(
                [
                    {"date": "2026-07-29", "close": 100.0},
                    {"date": "2026-07-30", "close": 101.0},
                    {"date": "2026-07-31", "close": 102.01},
                ]
            )
        finally:
            with self.lock:
                self.active -= 1


class FailingNavAk:
    def fund_open_fund_info_em(self, symbol, indicator):
        raise AssertionError("full fund history should not be needed")


class CountingFundAk:
    def __init__(self):
        self.nav_calls = []
        self.holding_calls = []

    def fund_open_fund_info_em(self, symbol, indicator):
        self.nav_calls.append((symbol, indicator))
        return pd.DataFrame(
            [
                {"净值日期": "2026-07-29", "单位净值": 1.0},
                {"净值日期": "2026-07-30", "单位净值": 1.01},
                {"净值日期": "2026-07-31", "单位净值": 1.0201},
            ]
        )

    def fund_portfolio_hold_em(self, symbol, date):
        self.holding_calls.append((symbol, date))
        if date == "":
            return pd.DataFrame(
                [
                    {
                        "股票代码": "600519",
                        "股票名称": "贵州茅台",
                        "占净值比例": "30.0",
                    }
                ]
            )
        return pd.DataFrame()


class TencentQuoteProvider(AkshareProvider):
    def _fetch_tencent_quote_text(self, query):
        return """
        v_bj920368="62~闆锋嫙鐢熷懡~920368~24.44~24.58~24.65~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~20260804135306~-0.14~-0.57~";
        v_sh600519="1~贵州茅台~600519~1342.30~1361.76~1330.03~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~20260731134123~-19.46~-1.43~";
        v_hk00700="100~腾讯控股~00700~473.800~471.800~470.000~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~2026/07/31 13:26:21~2.000~0.42~";
        v_usSNDK="200~闪迪~SNDK.OQ~1279.96~1015.89~1135.01~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~~2026-07-30 16:00:01~264.07~25.99~";
        """

    def _ak(self):
        raise AssertionError("akshare fallback should not be needed")


class TargetEtfTencentProvider(AkshareProvider):
    def __init__(self):
        super().__init__()
        self.queries = []

    def _fetch_tencent_quote_text(self, query):
        self.queries.append(query)
        return """
        v_sh515290="1~银行ETF天弘~515290~1.432~1.440~1.441~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~20260806095719~-0.008~-0.56~";
        v_sh512680="1~军工ETF广发~512680~1.164~1.161~1.156~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~20260806095718~0.003~0.26~";
        v_sh516150="1~稀土ETF嘉实~516150~1.778~1.749~1.761~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~0~20260806100737~0.029~1.66~";
        """

    def _ak(self):
        raise AssertionError("target ETF quote should use Tencent realtime data")


class TestableProvider(AkshareProvider):
    def __init__(self, fake_ak):
        super().__init__()
        self.fake_ak = fake_ak

    def _ak(self):
        return self.fake_ak

    def _fetch_tencent_quote_text(self, query):
        raise RuntimeError("network disabled in tests")

    def _fetch_eastmoney_nav_text(self, code):
        raise RuntimeError("network disabled in tests")


class EastmoneyFallbackProvider(TestableProvider):
    def _fetch_eastmoney_holdings_html(self, code):
        return """
        var apidata={ content:"<table><thead><tr>
        <th>序号</th><th>股票代码</th><th>股票名称</th><th>最新价</th><th>涨跌幅</th><th>相关资讯</th><th>占净值<br />比例</th><th>持股数</th><th>持仓市值</th>
        </tr></thead><tbody>
        <tr><td>1</td><td><a href='//quote.eastmoney.com/unify/r/105.SNDK'>SNDK</a></td><td class='tol'><a>闪迪</a></td><td></td><td>25.99%</td><td></td><td class='tor'>10.10%</td><td></td><td></td></tr>
        <tr><td>2</td><td><a>600519</a></td><td class='tol'><a>贵州茅台</a></td><td></td><td>1.23%</td><td></td><td class='tor'>9.90%</td><td></td><td></td></tr>
        <tr><td>2</td><td><a>000858</a></td><td class='tol'><a>五粮液</a></td><td></td><td>-0.20%</td><td></td><td class='tor'>9.63%</td><td></td><td></td></tr>
        </tbody></table>",arryear:[2026],curyear:2026};
        """


class FastEastmoneyProvider(EastmoneyFallbackProvider):
    pass


class EmptyEastmoneyProvider(TestableProvider):
    def _fetch_eastmoney_holdings_html(self, code):
        return "var apidata={ content:\"<table><tbody></tbody></table>\",arryear:[],curyear:2026};"


class NoEastmoneyProvider(TestableProvider):
    def _fetch_eastmoney_holdings_html(self, code):
        raise RuntimeError("eastmoney disabled in this test")


class FastEastmoneyNavProvider(TestableProvider):
    def _fetch_eastmoney_nav_text(self, code):
        return """
        var Data_netWorthTrend = [
          {"x":1785254400000,"y":1.0100,"equityReturn":1.0,"unitMoney":""},
          {"x":1785427200000,"y":1.0201,"equityReturn":1.0,"unitMoney":""}
        ];
        """


class AkshareProviderTests(unittest.TestCase):
    def test_current_day_cache_reuses_static_value_until_date_changes(self):
        provider = AkshareProvider()
        calls = 0

        def loader():
            nonlocal calls
            calls += 1
            return {"calls": calls}

        first = provider._cached("daily_static", loader, timedelta(days=1), current_day_only=True)
        same_day_old = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        provider._cache["daily_static"] = (same_day_old, first)

        same_day = provider._cached("daily_static", loader, timedelta(days=1), current_day_only=True)

        self.assertEqual(same_day, first)
        self.assertEqual(calls, 1)

        previous_day = datetime.now() - timedelta(days=1)
        provider._cache["daily_static"] = (previous_day, first)
        next_day = provider._cached("daily_static", loader, timedelta(days=1), current_day_only=True)

        self.assertEqual(next_day, {"calls": 2})

    def test_holdings_uses_latest_available_date_before_guessing_years(self):
        fake_ak = FakeLatestAk()
        provider = TestableProvider(fake_ak)

        holdings = provider.get_holdings("161725")

        self.assertEqual(len(holdings), 1)
        self.assertEqual(holdings[0].code, "000858")
        self.assertEqual(fake_ak.calls[0], "")

    def test_holdings_falls_back_across_recent_years(self):
        fake_ak = FakeAk()
        provider = NoEastmoneyProvider(fake_ak)

        holdings = provider.get_holdings("161725")

        self.assertEqual(len(holdings), 1)
        self.assertEqual(holdings[0].code, "600519")
        self.assertIn(str(datetime.now().year - 2), fake_ak.calls)

    def test_holdings_use_eastmoney_before_scanning_historical_years(self):
        fake_ak = EmptyLatestHoldingsAk()
        provider = FastEastmoneyProvider(fake_ak)

        holdings = provider.get_holdings("005827")

        self.assertEqual(len(holdings), 3)
        self.assertEqual(fake_ak.calls, [""])

    def test_holdings_do_not_scan_years_when_eastmoney_returns_empty(self):
        fake_ak = EmptyLatestHoldingsAk()
        provider = EmptyEastmoneyProvider(fake_ak)

        holdings = provider.get_holdings("005827")

        self.assertEqual(holdings, [])
        self.assertEqual(fake_ak.calls, [""])

    def test_holdings_falls_back_to_eastmoney_f10_table(self):
        provider = EastmoneyFallbackProvider(FailingHoldingsAk())

        holdings = provider.get_holdings("005827")

        self.assertEqual(len(holdings), 3)
        self.assertEqual(holdings[0].code, "SNDK")
        self.assertEqual(holdings[0].name, "闪迪")
        self.assertAlmostEqual(holdings[0].weight_pct, 10.1)

    def test_quotes_include_a_share_and_hk_holdings(self):
        provider = TestableProvider(FakeQuoteAk())

        quotes = provider.get_quotes(["600519", "00700"])

        self.assertEqual(quotes["600519"].name, "贵州茅台")
        self.assertEqual(quotes["00700"].name, "腾讯控股")
        self.assertAlmostEqual(quotes["00700"].change_pct, 1.5)

    def test_quotes_use_targeted_tencent_query_before_full_market_fallback(self):
        provider = TencentQuoteProvider()

        quotes = provider.get_quotes(["600519", "00700", "SNDK", "920368"])

        self.assertEqual(quotes["600519"].name, "贵州茅台")
        self.assertAlmostEqual(quotes["600519"].change_pct, -1.43)
        self.assertEqual(quotes["600519"].trade_date, "2026-07-31")
        self.assertEqual(quotes["600519"].quote_time, "2026-07-31 13:41:23")
        self.assertEqual(quotes["00700"].name, "腾讯控股")
        self.assertAlmostEqual(quotes["00700"].change_pct, 0.42)
        self.assertEqual(quotes["SNDK"].name, "闪迪")
        self.assertAlmostEqual(quotes["SNDK"].change_pct, 25.99)
        self.assertAlmostEqual(quotes["920368"].change_pct, -0.57)

    def test_historical_quotes_calculate_interval_returns_by_market(self):
        provider = TestableProvider(FakeHistoricalQuoteAk())

        quotes = provider.get_historical_quotes(
            ["CIEN", "01888", "300750"],
            from_date="2026-08-04",
            to_date="2026-08-05",
        )

        self.assertAlmostEqual(quotes["CIEN"].change_pct, 2.0)
        self.assertEqual(quotes["CIEN"].trade_date, "2026-08-05")
        self.assertAlmostEqual(quotes["01888"].change_pct, -5.0)
        self.assertAlmostEqual(quotes["300750"].change_pct, 5.0)

    def test_tencent_symbol_supports_beijing_exchange_codes(self):
        self.assertEqual(_tencent_symbol("920368"), "bj920368")

    def test_official_estimate_includes_source_trade_date(self):
        provider = TestableProvider(FakeEstimateAk())

        estimate = provider.get_official_estimate("161725")

        self.assertIsNotNone(estimate)
        self.assertAlmostEqual(estimate.nav, 1.2345)
        self.assertAlmostEqual(estimate.growth_pct, 1.23)
        self.assertEqual(estimate.estimate_time, "2026-07-31 15:00:00")
        self.assertEqual(estimate.trade_date, "2026-07-31")

    def test_latest_nav_uses_eastmoney_trend_before_full_history(self):
        provider = FastEastmoneyNavProvider(FailingNavAk())

        latest = provider.get_latest_nav("161725")
        history = provider.get_nav_history("161725")
        by_date = provider.get_nav_by_date("161725", "2026-07-31")

        self.assertIsNotNone(latest)
        self.assertAlmostEqual(latest.nav, 1.0201)
        self.assertEqual(latest.date, "2026-07-31")
        self.assertEqual(history[-1].date, "2026-07-31")
        self.assertIsNotNone(by_date)
        self.assertAlmostEqual(by_date.nav, 1.0201)

    def test_latest_nav_does_not_use_full_history_when_trend_is_missing(self):
        provider = TestableProvider(FailingNavAk())

        latest = provider.get_latest_nav("161725")

        self.assertIsNone(latest)

    def test_factor_history_and_quotes_are_normalized(self):
        provider = TestableProvider(FakeFactorAk())
        provider.factor_universe = [MarketFactor(code="sh000300", name="CSI 300")]

        nav_history = provider.get_nav_history("161725")
        factor_histories = provider.get_factor_histories()
        factor_quotes = provider.get_factor_quotes(["sh000300"])

        self.assertEqual(nav_history[-1].date, "2026-07-31")
        self.assertAlmostEqual(nav_history[-1].value, 1.0201)
        self.assertIn("sh000300", factor_histories)
        self.assertEqual(factor_histories["sh000300"][-1].date, "2026-07-31")
        self.assertAlmostEqual(factor_quotes["sh000300"].change_pct, 1.5)

    def test_tracking_index_quote_uses_fund_name_mapping(self):
        provider = TestableProvider(FakeTrackingIndexAk())

        quote = provider.get_tracking_index_quote("001595", fund_name="天弘中证银行ETF联接C")

        self.assertIsNotNone(quote)
        self.assertEqual(quote.code, "sz399986")
        self.assertEqual(quote.name, "中证银行")
        self.assertAlmostEqual(quote.change_pct, -2.56)
        self.assertIsNone(quote.quote_time)
        self.assertEqual(quote.trade_date, "2026-08-04")

    def test_tracking_index_quote_prefers_target_etf_realtime_quote(self):
        provider = TargetEtfTencentProvider()

        bank_quote = provider.get_tracking_index_quote("001595", fund_name="天弘中证银行ETF联接C")
        defense_quote = provider.get_tracking_index_quote("005693", fund_name="广发中证军工ETF联接C")
        rare_earth_quote = provider.get_tracking_index_quote("011036", fund_name="嘉实中证稀土产业ETF联接C")

        self.assertIsNotNone(bank_quote)
        self.assertEqual(bank_quote.code, "515290")
        self.assertEqual(bank_quote.name, "银行ETF天弘")
        self.assertAlmostEqual(bank_quote.change_pct, -0.56)
        self.assertEqual(bank_quote.trade_date, "2026-08-06")
        self.assertIsNotNone(defense_quote)
        self.assertEqual(defense_quote.code, "512680")
        self.assertEqual(defense_quote.name, "军工ETF广发")
        self.assertAlmostEqual(defense_quote.change_pct, 0.26)
        self.assertEqual(defense_quote.trade_date, "2026-08-06")
        self.assertIsNotNone(rare_earth_quote)
        self.assertEqual(rare_earth_quote.code, "516150")
        self.assertEqual(rare_earth_quote.name, "稀土ETF嘉实")
        self.assertAlmostEqual(rare_earth_quote.change_pct, 1.66)
        self.assertEqual(rare_earth_quote.trade_date, "2026-08-06")
        self.assertIn("sh515290", provider.queries[0])
        self.assertIn("sh512680", provider.queries[1])
        self.assertIn("sh516150", provider.queries[2])

    def test_tracking_index_quote_maps_photovoltaic_index_fund(self):
        provider = TestableProvider(FakeTrackingIndexAk())

        quote = provider.get_tracking_index_quote("011103", fund_name="天弘中证光伏产业指数C")

        self.assertIsNotNone(quote)
        self.assertEqual(quote.code, "sh931151")
        self.assertEqual(quote.name, "中证光伏产业")
        self.assertAlmostEqual(quote.change_pct, 3.0)
        self.assertEqual(quote.trade_date, "2026-08-04")

    def test_qdii_benchmark_quote_combines_nasdaq_proxy_and_fx(self):
        provider = TestableProvider(FakeQdiiBenchmarkAk())

        quote = provider.get_qdii_benchmark_quote(
            "539001",
            fund_name="建信纳斯达克100指数(QDII)A人民币",
            from_date="2026-07-31",
            to_date="2026-08-03",
        )

        self.assertIsNotNone(quote)
        self.assertEqual(quote["benchmark_symbol"], "QQQ")
        self.assertEqual(quote["fx_symbol"], "USDCNYC")
        self.assertAlmostEqual(quote["benchmark_growth_pct"], 2.0)
        self.assertAlmostEqual(quote["fx_growth_pct"], 0.5)
        self.assertAlmostEqual(quote["change_pct"], 2.51)
        self.assertEqual(quote["benchmark_start_date"], "2026-07-31")
        self.assertEqual(quote["benchmark_end_date"], "2026-08-03")

    def test_factor_histories_are_loaded_concurrently(self):
        fake_ak = SlowFactorHistoryAk()
        provider = TestableProvider(fake_ak)
        provider.factor_universe = [
            MarketFactor(code="sh000300", name="CSI 300"),
            MarketFactor(code="sh000905", name="CSI 500"),
            MarketFactor(code="sh000852", name="CSI 1000"),
        ]

        histories = provider.get_factor_histories()

        self.assertEqual(set(histories), {"sh000300", "sh000905", "sh000852"})
        self.assertGreater(fake_ak.max_active, 1)

    def test_fund_nav_and_holdings_are_cached_for_refreshes(self):
        fake_ak = CountingFundAk()
        provider = TestableProvider(fake_ak)

        provider.get_latest_nav("161725")
        provider.get_nav_history("161725")
        nav_calls_after_first_refresh = len(fake_ak.nav_calls)
        provider.get_latest_nav("161725")
        provider.get_nav_history("161725")
        provider.get_holdings("161725")
        holding_calls_after_first_refresh = len(fake_ak.holding_calls)
        provider.get_holdings("161725")

        self.assertEqual(nav_calls_after_first_refresh, 1)
        self.assertEqual(len(fake_ak.nav_calls), nav_calls_after_first_refresh)
        self.assertEqual(len(fake_ak.holding_calls), holding_calls_after_first_refresh)


if __name__ == "__main__":
    unittest.main()
