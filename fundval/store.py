from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class WatchFund:
    code: str
    alias: Optional[str] = None
    name: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


def normalize_fund_code(code: str) -> str:
    normalized = str(code or "").strip()
    if not normalized or not normalized.isdigit() or len(normalized) > 6:
        raise ValueError("fund code must contain 1 to 6 digits")
    return normalized.zfill(6)


class WatchlistStore:
    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def add_fund(
        self,
        code: str,
        alias: Optional[str] = None,
        name: Optional[str] = None,
    ) -> WatchFund:
        fund_code = normalize_fund_code(code)
        now = _now()
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO watch_funds(code, alias, name, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(code) DO UPDATE SET
                    alias = excluded.alias,
                    name = COALESCE(excluded.name, watch_funds.name),
                    updated_at = excluded.updated_at
                """,
                (fund_code, _clean(alias), _clean(name), now, now),
            )
            conn.commit()
        return self.get_fund(fund_code) or WatchFund(fund_code, alias, name, now, now)

    def get_fund(self, code: str) -> Optional[WatchFund]:
        fund_code = normalize_fund_code(code)
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT code, alias, name, created_at, updated_at
                FROM watch_funds
                WHERE code = ?
                """,
                (fund_code,),
            ).fetchone()
        return _row_to_fund(row) if row else None

    def list_funds(self) -> list[WatchFund]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT code, alias, name, created_at, updated_at
                FROM watch_funds
                ORDER BY created_at ASC, code ASC
                """
            ).fetchall()
        return [_row_to_fund(row) for row in rows]

    def delete_fund(self, code: str) -> bool:
        fund_code = normalize_fund_code(code)
        with closing(self._connect()) as conn:
            cursor = conn.execute("DELETE FROM watch_funds WHERE code = ?", (fund_code,))
            conn.commit()
        return cursor.rowcount > 0

    def save_snapshot(
        self,
        snapshot_key: str,
        captured_at: str,
        valuations: list[dict],
    ) -> dict:
        clean_key = str(snapshot_key or "").strip()
        if not clean_key:
            raise ValueError("snapshot key is required")
        snapshot_date = _valuation_snapshot_date(valuations) or _snapshot_date(clean_key, captured_at)
        with closing(self._connect()) as conn:
            conn.execute("DELETE FROM valuation_snapshots WHERE snapshot_key = ?", (clean_key,))
            for item in valuations:
                conn.execute(
                    """
                    INSERT INTO valuation_snapshots (
                        snapshot_key, captured_at, code, name, status, source,
                        estimate_nav, estimate_growth_pct, coverage_pct, confidence,
                        reason, latest_nav, latest_nav_date, payload_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        clean_key,
                        captured_at,
                        item.get("code"),
                        item.get("name"),
                        item.get("status"),
                        item.get("source"),
                        item.get("estimate_nav"),
                        item.get("estimate_growth_pct"),
                        item.get("coverage_pct"),
                        item.get("confidence"),
                        item.get("reason"),
                        item.get("latest_nav"),
                        item.get("latest_nav_date"),
                        json.dumps(item, ensure_ascii=False),
                    ),
                )
            conn.commit()
        return {
            "snapshot_key": clean_key,
            "snapshot_date": snapshot_date,
            "captured_at": captured_at,
            "count": len(valuations),
        }

    def list_unreconciled_valuations(self) -> list[dict]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT vs.snapshot_key, vs.captured_at, vs.payload_json
                FROM valuation_snapshots vs
                LEFT JOIN valuation_reconciliations vr
                    ON vr.snapshot_key = vs.snapshot_key
                   AND vr.code = vs.code
                WHERE vr.id IS NULL
                  AND vs.status = 'estimated'
                  AND vs.estimate_nav IS NOT NULL
                ORDER BY vs.snapshot_key ASC, vs.code ASC
                """
            ).fetchall()
        result = []
        for row in rows:
            item = json.loads(row["payload_json"])
            item["snapshot_key"] = row["snapshot_key"]
            item["captured_at"] = row["captured_at"]
            item["snapshot_date"] = _item_snapshot_date(item, row["snapshot_key"], row["captured_at"])
            result.append(item)
        return result

    def save_reconciliation(self, reconciliation: dict) -> dict:
        snapshot_key = str(reconciliation.get("snapshot_key") or "").strip()
        code = normalize_fund_code(str(reconciliation.get("code") or ""))
        if not snapshot_key:
            raise ValueError("snapshot key is required")
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO valuation_reconciliations (
                    snapshot_key, snapshot_date, code, source,
                    estimate_nav, estimate_growth_pct, latest_nav, latest_nav_date,
                    actual_nav, actual_nav_date, actual_growth_pct,
                    nav_error_pct, abs_nav_error_pct,
                    growth_error_pct, abs_growth_error_pct,
                    reconciled_at, payload_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(snapshot_key, code) DO UPDATE SET
                    snapshot_date = excluded.snapshot_date,
                    source = excluded.source,
                    estimate_nav = excluded.estimate_nav,
                    estimate_growth_pct = excluded.estimate_growth_pct,
                    latest_nav = excluded.latest_nav,
                    latest_nav_date = excluded.latest_nav_date,
                    actual_nav = excluded.actual_nav,
                    actual_nav_date = excluded.actual_nav_date,
                    actual_growth_pct = excluded.actual_growth_pct,
                    nav_error_pct = excluded.nav_error_pct,
                    abs_nav_error_pct = excluded.abs_nav_error_pct,
                    growth_error_pct = excluded.growth_error_pct,
                    abs_growth_error_pct = excluded.abs_growth_error_pct,
                    reconciled_at = excluded.reconciled_at,
                    payload_json = excluded.payload_json
                """,
                (
                    snapshot_key,
                    reconciliation.get("snapshot_date"),
                    code,
                    reconciliation.get("source"),
                    reconciliation.get("estimate_nav"),
                    reconciliation.get("estimate_growth_pct"),
                    reconciliation.get("latest_nav"),
                    reconciliation.get("latest_nav_date"),
                    reconciliation.get("actual_nav"),
                    reconciliation.get("actual_nav_date"),
                    reconciliation.get("actual_growth_pct"),
                    reconciliation.get("nav_error_pct"),
                    reconciliation.get("abs_nav_error_pct"),
                    reconciliation.get("growth_error_pct"),
                    reconciliation.get("abs_growth_error_pct"),
                    reconciliation.get("reconciled_at"),
                    json.dumps({**reconciliation, "code": code}, ensure_ascii=False),
                ),
            )
            self._backfill_snapshot_reconciliation(conn, snapshot_key, code, reconciliation)
            conn.commit()
        return {**reconciliation, "code": code}

    def get_reconciliation_profile(
        self,
        code: str,
        source: str | None = None,
        limit: int = 60,
    ) -> dict:
        fund_code = normalize_fund_code(code)
        row_limit = max(1, int(limit or 60))
        params: list = [fund_code]
        source_filter = ""
        if source:
            source_filter = "AND source = ?"
            params.append(source)
        params.append(row_limit)
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"""
                SELECT nav_error_pct, abs_nav_error_pct,
                       growth_error_pct, abs_growth_error_pct,
                       estimate_growth_pct, actual_growth_pct
                FROM (
                    SELECT nav_error_pct, abs_nav_error_pct,
                           growth_error_pct, abs_growth_error_pct,
                           estimate_growth_pct, actual_growth_pct,
                           snapshot_date, snapshot_key
                    FROM valuation_reconciliations
                    WHERE code = ?
                    {source_filter}
                    ORDER BY snapshot_date DESC, snapshot_key DESC
                    LIMIT ?
                )
                """,
                params,
            ).fetchall()
        nav_biases = [_as_float(row["nav_error_pct"]) for row in rows]
        nav_errors = [_as_float(row["abs_nav_error_pct"]) for row in rows]
        growth_biases = [_as_float(row["growth_error_pct"]) for row in rows]
        growth_errors = [_as_float(row["abs_growth_error_pct"]) for row in rows]
        direction_pairs = [
            (_as_float(row["estimate_growth_pct"]), _as_float(row["actual_growth_pct"]))
            for row in rows
        ]
        direction_pairs = [
            (estimate, actual)
            for estimate, actual in direction_pairs
            if estimate is not None and actual is not None
        ]
        direction_matches = sum(
            1 for estimate, actual in direction_pairs if _direction(estimate) == _direction(actual)
        )
        return {
            "sample_count": len(rows),
            "mean_nav_error_pct": _avg(nav_biases),
            "mean_abs_nav_error_pct": _avg(nav_errors),
            "mean_growth_error_pct": _avg(growth_biases),
            "mean_abs_growth_error_pct": _avg(growth_errors),
            "direction_accuracy_pct": (
                round(direction_matches / len(direction_pairs) * 100, 4)
                if direction_pairs
                else None
            ),
        }

    def list_snapshots(self) -> list[dict]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT snapshot_key, MAX(captured_at) AS captured_at, COUNT(*) AS count,
                       MIN(payload_json) AS payload_json
                FROM valuation_snapshots
                GROUP BY snapshot_key
                ORDER BY snapshot_key DESC
                """
            ).fetchall()
        return [
            {
                "snapshot_key": row["snapshot_key"],
                "snapshot_date": _snapshot_list_date(row),
                "captured_at": row["captured_at"],
                "count": row["count"],
            }
            for row in rows
        ]

    def get_snapshot(self, snapshot_key: str) -> list[dict]:
        clean_key = str(snapshot_key or "").strip()
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT payload_json
                FROM valuation_snapshots
                WHERE snapshot_key = ?
                ORDER BY code ASC
                """,
                (clean_key,),
            ).fetchall()
        result = []
        for row in rows:
            item = json.loads(row["payload_json"])
            item["snapshot_date"] = _item_snapshot_date(item, clean_key, item.get("captured_at", ""))
            result.append(item)
        return result

    def has_snapshot(self, snapshot_key: str) -> bool:
        clean_key = str(snapshot_key or "").strip()
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT 1 FROM valuation_snapshots WHERE snapshot_key = ? LIMIT 1",
                (clean_key,),
            ).fetchone()
        return row is not None

    def _init_schema(self) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS watch_funds (
                    code TEXT PRIMARY KEY,
                    alias TEXT,
                    name TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS valuation_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    snapshot_key TEXT NOT NULL,
                    captured_at TEXT NOT NULL,
                    code TEXT NOT NULL,
                    name TEXT,
                    status TEXT,
                    source TEXT,
                    estimate_nav REAL,
                    estimate_growth_pct REAL,
                    coverage_pct REAL,
                    confidence REAL,
                    reason TEXT,
                    latest_nav REAL,
                    latest_nav_date TEXT,
                    payload_json TEXT NOT NULL,
                    UNIQUE(snapshot_key, code)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS valuation_reconciliations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    snapshot_key TEXT NOT NULL,
                    snapshot_date TEXT,
                    code TEXT NOT NULL,
                    source TEXT,
                    estimate_nav REAL,
                    estimate_growth_pct REAL,
                    latest_nav REAL,
                    latest_nav_date TEXT,
                    actual_nav REAL,
                    actual_nav_date TEXT,
                    actual_growth_pct REAL,
                    nav_error_pct REAL,
                    abs_nav_error_pct REAL,
                    growth_error_pct REAL,
                    abs_growth_error_pct REAL,
                    reconciled_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    UNIQUE(snapshot_key, code)
                )
                """
            )
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _backfill_snapshot_reconciliation(
        conn: sqlite3.Connection,
        snapshot_key: str,
        code: str,
        reconciliation: dict,
    ) -> None:
        row = conn.execute(
            """
            SELECT payload_json
            FROM valuation_snapshots
            WHERE snapshot_key = ? AND code = ?
            """,
            (snapshot_key, code),
        ).fetchone()
        if row is None:
            return
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except (TypeError, ValueError):
            payload = {}
        payload.update(_snapshot_reconciliation_patch(reconciliation))
        conn.execute(
            """
            UPDATE valuation_snapshots
            SET payload_json = ?
            WHERE snapshot_key = ? AND code = ?
            """,
            (json.dumps(payload, ensure_ascii=False), snapshot_key, code),
        )


def _row_to_fund(row: sqlite3.Row) -> WatchFund:
    return WatchFund(
        code=row["code"],
        alias=row["alias"],
        name=row["name"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _clean(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None


def _as_float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _avg(values: list[float | None]) -> float | None:
    clean = [value for value in values if value is not None]
    if not clean:
        return None
    return round(sum(clean) / len(clean), 4)


def _direction(value: float) -> int:
    if value > 0:
        return 1
    if value < 0:
        return -1
    return 0


def _snapshot_reconciliation_patch(reconciliation: dict) -> dict:
    fields = (
        "actual_nav",
        "actual_nav_date",
        "actual_growth_pct",
        "nav_error_pct",
        "abs_nav_error_pct",
        "growth_error_pct",
        "abs_growth_error_pct",
        "reconciled_at",
    )
    return {field: reconciliation.get(field) for field in fields if field in reconciliation}


def _snapshot_date(snapshot_key: str, captured_at: str) -> str:
    for value in (snapshot_key, captured_at):
        text = str(value or "").strip()
        if len(text) >= 10 and text[4] == "-" and text[7] == "-":
            return text[:10]
    return ""


def _item_snapshot_date(item: dict, snapshot_key: str, captured_at: str) -> str:
    return _date_key(item.get("trade_date")) or _snapshot_date(snapshot_key, captured_at)


def _snapshot_list_date(row: sqlite3.Row) -> str:
    try:
        item = json.loads(row["payload_json"] or "{}")
    except (TypeError, ValueError):
        item = {}
    return _item_snapshot_date(item, row["snapshot_key"], row["captured_at"])


def _valuation_snapshot_date(valuations: list[dict]) -> str:
    dates = {_date_key(item.get("trade_date")) for item in valuations}
    dates.discard("")
    return dates.pop() if len(dates) == 1 else ""


def _date_key(value) -> str:
    text = str(value or "").strip()
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        return text[:10]
    return ""
