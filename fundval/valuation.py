from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Optional


@dataclass(frozen=True)
class Holding:
    name: str
    code: str
    weight_pct: float


@dataclass(frozen=True)
class Quote:
    code: str
    name: str
    change_pct: float


@dataclass(frozen=True)
class LatestNav:
    nav: float
    date: Optional[str] = None


@dataclass(frozen=True)
class OfficialEstimate:
    nav: float
    growth_pct: float
    estimate_time: Optional[str] = None


@dataclass(frozen=True)
class HoldingContribution:
    code: str
    name: str
    weight_pct: float
    change_pct: float
    contribution_pct: float


@dataclass(frozen=True)
class ValuationResult:
    status: str
    source: str
    estimate_nav: Optional[float]
    estimate_growth_pct: Optional[float]
    coverage_pct: float
    confidence: float
    reason: Optional[str] = None
    latest_nav: Optional[float] = None
    latest_nav_date: Optional[str] = None
    contributions: tuple[HoldingContribution, ...] = ()

    def to_dict(self) -> dict:
        data = asdict(self)
        data["contributions"] = [asdict(item) for item in self.contributions]
        return data


def calculate_holding_estimate(
    latest_nav: Optional[LatestNav],
    holdings: list[Holding],
    quotes: dict[str, Quote],
    min_coverage_pct: float = 35.0,
) -> ValuationResult:
    if latest_nav is None or latest_nav.nav <= 0:
        return _unavailable("no_nav")
    if not holdings:
        return _unavailable("no_holdings", latest_nav=latest_nav)
    if not quotes:
        return _unavailable("no_quotes", latest_nav=latest_nav)

    quote_by_code = {_normalize_stock_code(code): quote for code, quote in quotes.items()}
    covered_weight = 0.0
    weighted_growth_pct = 0.0
    contributions: list[HoldingContribution] = []

    for holding in holdings:
        weight_pct = max(float(holding.weight_pct or 0), 0.0)
        if weight_pct <= 0:
            continue
        quote = quote_by_code.get(_normalize_stock_code(holding.code))
        if quote is None:
            continue
        contribution_pct = weight_pct * quote.change_pct / 100.0
        covered_weight += weight_pct
        weighted_growth_pct += contribution_pct
        contributions.append(
            HoldingContribution(
                code=holding.code,
                name=holding.name or quote.name,
                weight_pct=round(weight_pct, 4),
                change_pct=round(quote.change_pct, 4),
                contribution_pct=round(contribution_pct, 4),
            )
        )

    if covered_weight <= 0:
        return _unavailable("no_covered_quotes", latest_nav=latest_nav)
    if covered_weight < min_coverage_pct:
        return ValuationResult(
            status="unavailable",
            source="holding",
            estimate_nav=None,
            estimate_growth_pct=None,
            coverage_pct=round(covered_weight, 4),
            confidence=_confidence(covered_weight),
            reason="low_coverage",
            latest_nav=latest_nav.nav,
            latest_nav_date=latest_nav.date,
            contributions=tuple(contributions),
        )

    estimate_growth_pct = weighted_growth_pct / (covered_weight / 100.0)
    estimate_nav = latest_nav.nav * (1 + estimate_growth_pct / 100.0)

    return ValuationResult(
        status="estimated",
        source="holding",
        estimate_nav=round(estimate_nav, 6),
        estimate_growth_pct=round(estimate_growth_pct, 4),
        coverage_pct=round(covered_weight, 4),
        confidence=_confidence(covered_weight),
        latest_nav=latest_nav.nav,
        latest_nav_date=latest_nav.date,
        contributions=tuple(contributions),
    )


def _unavailable(reason: str, latest_nav: Optional[LatestNav] = None) -> ValuationResult:
    return ValuationResult(
        status="unavailable",
        source="holding",
        estimate_nav=None,
        estimate_growth_pct=None,
        coverage_pct=0.0,
        confidence=0.0,
        reason=reason,
        latest_nav=latest_nav.nav if latest_nav else None,
        latest_nav_date=latest_nav.date if latest_nav else None,
    )


def _confidence(coverage_pct: float) -> float:
    if coverage_pct <= 0:
        return 0.0
    return round(min(95.0, max(20.0, coverage_pct)), 1)


def build_reconciliation(
    snapshot: dict,
    actual_nav: float,
    actual_nav_date: str,
    reconciled_at: str,
) -> dict:
    estimate_nav = _float_or_none(snapshot.get("estimate_nav"))
    actual = _float_or_none(actual_nav)
    if estimate_nav is None or estimate_nav <= 0:
        raise ValueError("snapshot estimate_nav is required")
    if actual is None or actual <= 0:
        raise ValueError("actual_nav must be positive")

    latest_nav = _float_or_none(snapshot.get("latest_nav"))
    estimate_growth = _float_or_none(snapshot.get("estimate_growth_pct"))
    actual_growth = None
    growth_error = None
    if latest_nav is not None and latest_nav > 0:
        actual_growth = (actual / latest_nav - 1) * 100
        if estimate_growth is not None:
            growth_error = estimate_growth - actual_growth

    nav_error = (estimate_nav - actual) / actual * 100
    return {
        "snapshot_key": str(snapshot.get("snapshot_key") or ""),
        "snapshot_date": str(snapshot.get("snapshot_date") or _date_from_text(snapshot.get("snapshot_key"))),
        "code": str(snapshot.get("code") or ""),
        "source": str(snapshot.get("source") or ""),
        "estimate_nav": _round_or_none(estimate_nav),
        "estimate_growth_pct": _round_or_none(estimate_growth),
        "latest_nav": _round_or_none(latest_nav),
        "latest_nav_date": snapshot.get("latest_nav_date"),
        "actual_nav": _round_or_none(actual),
        "actual_nav_date": str(actual_nav_date or ""),
        "actual_growth_pct": _round_or_none(actual_growth),
        "nav_error_pct": _round_or_none(nav_error),
        "abs_nav_error_pct": _round_or_none(abs(nav_error)),
        "growth_error_pct": _round_or_none(growth_error),
        "abs_growth_error_pct": _round_or_none(abs(growth_error) if growth_error is not None else None),
        "reconciled_at": str(reconciled_at or ""),
    }


def calibrate_confidence(base_confidence: float, profile: Optional[dict], min_samples: int = 5) -> float:
    base = min(95.0, max(0.0, float(base_confidence or 0.0)))
    if not profile or int(profile.get("sample_count") or 0) < min_samples:
        return round(base, 1)

    sample_count = int(profile.get("sample_count") or 0)
    mean_abs_nav_error = max(float(profile.get("mean_abs_nav_error_pct") or 0.0), 0.0)
    error_score = min(95.0, max(20.0, 95.0 - mean_abs_nav_error * 25.0))
    direction_accuracy = profile.get("direction_accuracy_pct")
    if direction_accuracy is not None:
        error_score = (error_score + min(100.0, max(0.0, float(direction_accuracy)))) / 2.0

    history_weight = min(0.6, sample_count / 50.0)
    calibrated = base * (1 - history_weight) + error_score * history_weight
    return round(min(95.0, max(0.0, calibrated)), 1)


def _float_or_none(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _round_or_none(value: float | None, digits: int = 6) -> float | None:
    return round(value, digits) if value is not None else None


def _date_from_text(value) -> str:
    text = str(value or "").strip()
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        return text[:10]
    return ""


def _normalize_stock_code(code: str) -> str:
    digits = "".join(ch for ch in str(code or "") if ch.isdigit())
    return digits[-6:] if len(digits) >= 6 else digits
