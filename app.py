from __future__ import annotations

import asyncio
from contextlib import suppress
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from fundval.providers import AkshareProvider
from fundval.service import FundValuationService
from fundval.store import WatchlistStore


ROOT_DIR = Path(__file__).resolve().parent
WEB_DIR = ROOT_DIR / "web"
DATA_DIR = ROOT_DIR / "data"


class FundRequest(BaseModel):
    code: str
    alias: str | None = None


class SnapshotRequest(BaseModel):
    snapshot_key: str | None = None


def create_app(
    service: FundValuationService | None = None,
    enable_scheduler: bool | None = None,
) -> FastAPI:
    valuation_service = service or _default_service()
    should_schedule = (service is None) if enable_scheduler is None else enable_scheduler
    api = FastAPI(title="Fund Valuation", version="0.1.0")

    if WEB_DIR.exists():
        api.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

    @api.get("/", include_in_schema=False)
    def index():
        return FileResponse(WEB_DIR / "index.html")

    @api.get("/api/health")
    def health():
        return valuation_service.health()

    @api.get("/api/trading-status")
    def trading_status():
        return valuation_service.trading_status()

    @api.get("/api/funds")
    def list_funds():
        return valuation_service.list_funds()

    @api.post("/api/funds")
    def add_fund(payload: FundRequest):
        try:
            fund = valuation_service.add_fund(payload.code, payload.alias)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return fund.to_dict()

    @api.delete("/api/funds/{code}")
    def delete_fund(code: str):
        try:
            deleted = valuation_service.delete_fund(code)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"deleted": deleted}

    @api.get("/api/valuations")
    def estimate_watchlist(refresh: bool = False):
        return valuation_service.estimate_watchlist_cached(force_refresh=refresh)

    @api.get("/api/valuation/{code}")
    def estimate_fund(code: str):
        try:
            return valuation_service.estimate_fund(code)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @api.get("/api/snapshots")
    def list_snapshots():
        return valuation_service.list_snapshots()

    @api.post("/api/snapshots")
    def create_snapshot(payload: SnapshotRequest):
        return valuation_service.create_snapshot(snapshot_key=payload.snapshot_key)

    @api.get("/api/snapshots/{snapshot_key}")
    def get_snapshot(snapshot_key: str):
        return valuation_service.get_snapshot(snapshot_key)

    @api.delete("/api/snapshots/{snapshot_key}")
    def delete_snapshot(snapshot_key: str):
        deleted = valuation_service.delete_snapshot(snapshot_key)
        if not deleted:
            raise HTTPException(status_code=404, detail="snapshot not found")
        return {"snapshot_key": snapshot_key, "deleted": deleted}

    @api.get("/api/reconciliations")
    def list_reconciliations(limit: int = 50):
        return valuation_service.list_reconciliations(limit=limit)

    @api.post("/api/reconciliations")
    def reconcile_snapshots():
        return valuation_service.reconcile_snapshots()

    if should_schedule:
        @api.on_event("startup")
        async def start_snapshot_scheduler():
            api.state.snapshot_task = asyncio.create_task(_snapshot_scheduler(valuation_service))

        @api.on_event("shutdown")
        async def stop_snapshot_scheduler():
            task = getattr(api.state, "snapshot_task", None)
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    return api


def _default_service() -> FundValuationService:
    store = WatchlistStore(DATA_DIR / "funds.db")
    provider = AkshareProvider()
    return FundValuationService(store=store, provider=provider)


app = create_app()


async def _snapshot_scheduler(service: FundValuationService) -> None:
    while True:
        await asyncio.to_thread(service.create_due_snapshot)
        await asyncio.to_thread(service.reconcile_due_snapshots)
        await asyncio.sleep(30)
