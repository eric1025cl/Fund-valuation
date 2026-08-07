from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from fundval.providers import AkshareProvider
from fundval.service import FundValuationService
from fundval.store import WatchlistStore


DATA_DIR = ROOT_DIR / "data"


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit valuation coverage for the local watchlist.")
    parser.add_argument(
        "--db",
        default=str(DATA_DIR / "funds.db"),
        help="Path to the watchlist sqlite database.",
    )
    parser.add_argument(
        "--fast",
        action="store_true",
        help="Skip official estimates and factor monitoring for a faster live-coverage check.",
    )
    args = parser.parse_args()

    service = FundValuationService(
        store=WatchlistStore(Path(args.db)),
        provider=AkshareProvider(),
    )
    rows = service.estimate_watchlist(
        use_snapshot_cache=False,
        include_official_estimate=not args.fast,
        include_factor_monitoring=not args.fast,
    )
    items = [_audit_row(row) for row in rows]
    summary = {
        "total": len(items),
        "estimated": sum(1 for item in items if item["status"] == "estimated"),
        "unavailable": sum(1 for item in items if item["status"] != "estimated"),
        "sources": _count_by(items, "source"),
        "fund_types": _count_by(items, "fund_type"),
        "items": items,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 1 if summary["unavailable"] else 0


def _audit_row(row: dict) -> dict:
    return {
        "code": row.get("code"),
        "name": row.get("name"),
        "status": row.get("status"),
        "source": row.get("source"),
        "fund_type": row.get("fund_type"),
        "reason": row.get("reason"),
        "coverage_pct": row.get("coverage_pct"),
        "confidence": row.get("confidence"),
        "trade_date": row.get("trade_date"),
        "latest_nav_date": row.get("latest_nav_date"),
    }


def _count_by(items: list[dict], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        value = str(item.get(key) or "unknown")
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


if __name__ == "__main__":
    raise SystemExit(main())
