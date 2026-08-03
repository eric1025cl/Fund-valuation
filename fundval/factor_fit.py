from __future__ import annotations

from dataclasses import asdict, dataclass
from math import sqrt
from typing import Optional

from .valuation import LatestNav, Quote


@dataclass(frozen=True)
class MarketFactor:
    code: str
    name: str


@dataclass(frozen=True)
class FactorPoint:
    date: str
    value: float


@dataclass(frozen=True)
class FactorExposure:
    code: str
    name: str
    weight_pct: float
    correlation: Optional[float] = None


@dataclass(frozen=True)
class FactorFitResult:
    status: str
    source: str
    estimate_nav: Optional[float]
    estimate_growth_pct: Optional[float]
    coverage_pct: float
    confidence: float
    reason: Optional[str] = None
    latest_nav: Optional[float] = None
    latest_nav_date: Optional[str] = None
    fit_r2: Optional[float] = None
    fit_residual_pct: Optional[float] = None
    sample_count: int = 0
    factor_exposures: tuple[FactorExposure, ...] = ()
    baseline_factor_exposures: tuple[FactorExposure, ...] = ()
    recent_factor_exposures: tuple[FactorExposure, ...] = ()
    style_drift_score: float = 0.0
    style_drift_level: str = "low"
    style_drift_reason: Optional[str] = None
    trade_date: Optional[str] = None

    def to_dict(self) -> dict:
        data = asdict(self)
        data["factor_exposures"] = [asdict(item) for item in self.factor_exposures]
        data["baseline_factor_exposures"] = [
            asdict(item) for item in self.baseline_factor_exposures
        ]
        data["recent_factor_exposures"] = [asdict(item) for item in self.recent_factor_exposures]
        return data


DEFAULT_FACTOR_UNIVERSE = (
    MarketFactor("sh000300", "沪深300"),
    MarketFactor("sh000905", "中证500"),
    MarketFactor("sh000852", "中证1000"),
    MarketFactor("sz399006", "创业板指"),
    MarketFactor("sh000688", "科创50"),
    MarketFactor("sh000016", "上证50"),
    MarketFactor("sh000932", "中证消费"),
    MarketFactor("sh000933", "中证医药"),
    MarketFactor("sh000928", "中证能源"),
    MarketFactor("sh000929", "中证材料"),
    MarketFactor("sh000930", "中证工业"),
    MarketFactor("sh000931", "中证可选消费"),
)


def fit_factor_model(
    latest_nav: Optional[LatestNav],
    fund_history: list[FactorPoint],
    factor_histories: dict[str, list[FactorPoint]],
    factor_quotes: dict[str, Quote],
    min_observations: int = 20,
    recent_window: int = 20,
) -> FactorFitResult:
    if latest_nav is None or latest_nav.nav <= 0:
        return _unavailable("no_nav", latest_nav)

    fund_returns = _returns_by_date(fund_history)
    if len(fund_returns) < min_observations:
        return _unavailable("no_nav_history", latest_nav, sample_count=len(fund_returns))

    quote_codes = set(factor_quotes)
    factor_returns = {
        code: _returns_by_date(points)
        for code, points in factor_histories.items()
        if code in quote_codes
    }
    factor_returns = {code: returns for code, returns in factor_returns.items() if returns}
    if not factor_returns:
        return _unavailable("no_factor_history", latest_nav, sample_count=len(fund_returns))
    if not factor_quotes:
        return _unavailable("no_factor_quotes", latest_nav, sample_count=len(fund_returns))

    full_exposures = _fit_exposures(
        fund_returns,
        factor_returns,
        factor_quotes,
        min_observations=min_observations,
    )
    if not full_exposures:
        return _unavailable("weak_factor_relationship", latest_nav, sample_count=len(fund_returns))

    older_returns, recent_returns = _split_returns(fund_returns, recent_window, min_observations)
    baseline_exposures = _fit_exposures(
        older_returns,
        factor_returns,
        factor_quotes,
        min_observations=min_observations,
    ) or full_exposures
    recent_exposures = _fit_exposures(
        recent_returns,
        factor_returns,
        factor_quotes,
        min_observations=min_observations,
    ) or full_exposures

    exposures = recent_exposures
    evaluation_returns = recent_returns if len(recent_returns) >= min_observations else fund_returns
    fit_r2, residual = _fit_quality(evaluation_returns, factor_returns, exposures)
    estimate_growth = _current_growth(exposures, factor_quotes)
    estimate_nav = latest_nav.nav * (1 + estimate_growth / 100.0)
    drift_score = _style_drift_score(baseline_exposures, recent_exposures)
    drift_level = _style_drift_level(drift_score)

    return FactorFitResult(
        status="estimated",
        source="factor_fit",
        estimate_nav=round(estimate_nav, 6),
        estimate_growth_pct=round(estimate_growth, 4),
        coverage_pct=0.0,
        confidence=_confidence(fit_r2, residual, len(evaluation_returns), drift_score),
        latest_nav=round(float(latest_nav.nav), 6),
        latest_nav_date=latest_nav.date,
        fit_r2=round(fit_r2, 4),
        fit_residual_pct=round(residual, 4),
        sample_count=len(evaluation_returns),
        factor_exposures=tuple(exposures),
        baseline_factor_exposures=tuple(baseline_exposures),
        recent_factor_exposures=tuple(recent_exposures),
        style_drift_score=round(drift_score, 1),
        style_drift_level=drift_level,
        style_drift_reason=_style_drift_reason(drift_score, baseline_exposures, recent_exposures),
        trade_date=_quote_trade_date(factor_quotes),
    )


def _unavailable(
    reason: str,
    latest_nav: Optional[LatestNav],
    sample_count: int = 0,
) -> FactorFitResult:
    return FactorFitResult(
        status="unavailable",
        source="factor_fit",
        estimate_nav=None,
        estimate_growth_pct=None,
        coverage_pct=0.0,
        confidence=0.0,
        reason=reason,
        latest_nav=round(float(latest_nav.nav), 6) if latest_nav is not None else None,
        latest_nav_date=latest_nav.date if latest_nav is not None else None,
        sample_count=sample_count,
    )


def _returns_by_date(points: list[FactorPoint]) -> dict[str, float]:
    rows = sorted((_date_key(point.date), _as_float(point.value)) for point in points)
    rows = [(date, value) for date, value in rows if date and value is not None and value > 0]
    result: dict[str, float] = {}
    previous_value: float | None = None
    for date, value in rows:
        if previous_value is not None and previous_value > 0:
            result[date] = (value / previous_value - 1) * 100.0
        previous_value = value
    return result


def _split_returns(
    fund_returns: dict[str, float],
    recent_window: int,
    min_observations: int,
) -> tuple[dict[str, float], dict[str, float]]:
    dates = sorted(fund_returns)
    window = max(1, min(int(recent_window or 1), len(dates)))
    recent_dates = set(dates[-window:])
    older = {date: value for date, value in fund_returns.items() if date not in recent_dates}
    recent = {date: value for date, value in fund_returns.items() if date in recent_dates}
    if len(older) < min_observations:
        older = dict(fund_returns)
    if len(recent) < min_observations:
        recent = dict(fund_returns)
    return older, recent


def _fit_exposures(
    fund_returns: dict[str, float],
    factor_returns: dict[str, dict[str, float]],
    factor_quotes: dict[str, Quote],
    min_observations: int,
    max_factors: int = 5,
) -> list[FactorExposure]:
    scored: list[tuple[str, float, float | None]] = []
    for code, returns in factor_returns.items():
        pairs = _aligned_pairs(fund_returns, returns)
        if len(pairs) < min_observations:
            continue
        score, correlation = _fit_score(pairs)
        if score <= 0:
            continue
        scored.append((code, score, correlation))

    scored.sort(key=lambda item: item[1], reverse=True)
    scored = scored[:max_factors]
    total_score = sum(score for _, score, _ in scored)
    if total_score <= 0:
        return []

    exposures = []
    for code, score, correlation in scored:
        quote = factor_quotes.get(code)
        exposures.append(
            FactorExposure(
                code=code,
                name=(quote.name if quote is not None and quote.name else code),
                weight_pct=round(score / total_score * 100.0, 4),
                correlation=round(correlation, 4) if correlation is not None else None,
            )
        )
    return exposures


def _fit_score(pairs: list[tuple[float, float]]) -> tuple[float, float | None]:
    mean_abs_error = sum(abs(fund - factor) for fund, factor in pairs) / len(pairs)
    if mean_abs_error < 0.000001:
        return 1.0, 1.0
    correlation = _correlation(pairs)
    if correlation is None:
        return 0.0, None
    return max(0.0, correlation) ** 2, correlation


def _aligned_pairs(
    fund_returns: dict[str, float],
    factor_returns: dict[str, float],
) -> list[tuple[float, float]]:
    return [
        (fund_returns[date], factor_returns[date])
        for date in sorted(fund_returns)
        if date in factor_returns
    ]


def _correlation(pairs: list[tuple[float, float]]) -> float | None:
    if len(pairs) < 2:
        return None
    left = [item[0] for item in pairs]
    right = [item[1] for item in pairs]
    left_mean = sum(left) / len(left)
    right_mean = sum(right) / len(right)
    numerator = sum((x - left_mean) * (y - right_mean) for x, y in zip(left, right))
    left_var = sum((x - left_mean) ** 2 for x in left)
    right_var = sum((y - right_mean) ** 2 for y in right)
    if left_var <= 0 or right_var <= 0:
        return None
    return numerator / sqrt(left_var * right_var)


def _fit_quality(
    fund_returns: dict[str, float],
    factor_returns: dict[str, dict[str, float]],
    exposures: list[FactorExposure],
) -> tuple[float, float]:
    fitted_pairs: list[tuple[float, float]] = []
    exposure_codes = [exposure.code for exposure in exposures]
    for date, fund_return in sorted(fund_returns.items()):
        if any(date not in factor_returns.get(code, {}) for code in exposure_codes):
            continue
        fitted = sum(
            exposure.weight_pct / 100.0 * factor_returns[exposure.code][date]
            for exposure in exposures
        )
        fitted_pairs.append((fund_return, fitted))
    if not fitted_pairs:
        return 0.0, 0.0

    residual = sum(abs(fund - fitted) for fund, fitted in fitted_pairs) / len(fitted_pairs)
    mean_return = sum(fund for fund, _ in fitted_pairs) / len(fitted_pairs)
    sse = sum((fund - fitted) ** 2 for fund, fitted in fitted_pairs)
    sst = sum((fund - mean_return) ** 2 for fund, _ in fitted_pairs)
    if sst <= 0:
        r2 = 1.0 if sse <= 0.000001 else 0.0
    else:
        r2 = 1.0 - sse / sst
    return max(0.0, min(1.0, r2)), residual


def _current_growth(exposures: list[FactorExposure], factor_quotes: dict[str, Quote]) -> float:
    return sum(
        exposure.weight_pct / 100.0 * factor_quotes[exposure.code].change_pct
        for exposure in exposures
        if exposure.code in factor_quotes
    )


def _style_drift_score(
    baseline_exposures: list[FactorExposure],
    recent_exposures: list[FactorExposure],
) -> float:
    codes = {exposure.code for exposure in baseline_exposures} | {
        exposure.code for exposure in recent_exposures
    }
    baseline = {exposure.code: exposure.weight_pct for exposure in baseline_exposures}
    recent = {exposure.code: exposure.weight_pct for exposure in recent_exposures}
    return min(100.0, sum(abs(recent.get(code, 0.0) - baseline.get(code, 0.0)) for code in codes) / 2.0)


def _style_drift_level(score: float) -> str:
    if score >= 45.0:
        return "high"
    if score >= 25.0:
        return "medium"
    return "low"


def _style_drift_reason(
    score: float,
    baseline_exposures: list[FactorExposure],
    recent_exposures: list[FactorExposure],
) -> str | None:
    if score < 25.0 or not baseline_exposures or not recent_exposures:
        return None
    baseline_top = baseline_exposures[0].name
    recent_top = recent_exposures[0].name
    if baseline_top == recent_top:
        return f"风格权重变化 {score:.1f}"
    return f"主导风格由 {baseline_top} 转向 {recent_top}"


def _confidence(r2: float, residual: float, sample_count: int, drift_score: float) -> float:
    sample_score = min(15.0, max(0.0, sample_count / 120.0 * 15.0))
    residual_penalty = min(25.0, max(0.0, residual * 5.0))
    drift_penalty = min(15.0, max(0.0, drift_score / 100.0 * 15.0))
    confidence = 25.0 + r2 * 50.0 + sample_score - residual_penalty - drift_penalty
    return round(min(85.0, max(20.0, confidence)), 1)


def _quote_trade_date(factor_quotes: dict[str, Quote]) -> str | None:
    counts: dict[str, int] = {}
    for quote in factor_quotes.values():
        date = _date_key(quote.trade_date) or _date_key(quote.quote_time)
        if date:
            counts[date] = counts.get(date, 0) + 1
    if not counts:
        return None
    return max(counts.items(), key=lambda item: (item[1], item[0]))[0]


def _as_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _date_key(value) -> str:
    text = str(value or "").strip()
    if len(text) >= 10 and text[4] in "-/" and text[7] in "-/":
        return f"{text[:4]}-{text[5:7]}-{text[8:10]}"
    return ""
