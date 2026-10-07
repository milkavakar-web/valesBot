"""Веб-панель и API."""
import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, Optional

import ccxt.async_support as ccxt
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from . import config, db
from .backtest import fetch_history, run_backtest
from .brokers import hub
from .engine import manager
from .strategy import (DEFAULT_PARAMS, PRESETS, TIMEFRAMES, max_margin, normalize_bot,
                       so_deviation_pct)
from .trader import Trader

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

STATIC = Path(__file__).parent / "static"
security = HTTPBasic(auto_error=False)


def auth(creds: Optional[HTTPBasicCredentials] = Depends(security)) -> None:
    if not config.PANEL_PASSWORD:
        return
    ok = creds is not None and secrets.compare_digest(creds.username, config.PANEL_USER) \
        and secrets.compare_digest(creds.password, config.PANEL_PASSWORD)
    if not ok:
        raise HTTPException(401, "Нужен логин и пароль", headers={"WWW-Authenticate": "Basic"})


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    await manager.startup()
    yield
    await manager.shutdown()
    await hub.close_all()


app = FastAPI(title="Binance bot", lifespan=lifespan, dependencies=[Depends(auth)],
              docs_url=None, redoc_url=None)


@app.exception_handler(ValueError)
async def value_error(_: Request, e: ValueError):
    return JSONResponse({"detail": str(e)}, status_code=400)


@app.exception_handler(RuntimeError)
async def runtime_error(_: Request, e: RuntimeError):
    return JSONResponse({"detail": str(e)}, status_code=400)


@app.exception_handler(ccxt.BaseError)
async def exchange_error(_: Request, e: ccxt.BaseError):
    return JSONResponse({"detail": f"Ответ биржи: {e}"[:500]}, status_code=502)


def _bot_or_404(bot_id: int) -> Dict[str, Any]:
    bot = db.get_bot(bot_id)
    if not bot:
        raise HTTPException(404, "Бот не найден")
    return bot


def _view(bot: Dict[str, Any]) -> Dict[str, Any]:
    """Бот + вычисленные поля для интерфейса."""
    p, st = bot["params"], bot["state"]
    pos = st.get("position")
    price = st.get("last_price")
    levels = None
    if pos:
        tr = Trader(bot, None, st, None, None)
        levels = {
            "avg": pos["avg"],
            "tp": tr.tp_level(pos) if p["take_profit_pct"] > 0 else None,
            "sl": tr.sl_level(pos) if p["stop_loss_pct"] > 0 else None,
            "so": [{"price": tr.so_level(pos, k), "filled": k <= pos["so_filled"]}
                   for k in range(1, p["safety_orders"] + 1)],
        }
    return {
        **bot,
        "running": manager.is_running(bot["id"]),
        "unrealized": Trader.unrealized(pos, price) if pos and price else None,
        "levels": levels,
        "max_margin": max_margin(p),
    }


# ---------- страницы ----------

@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/meta")
async def meta():
    return {
        "timeframes": TIMEFRAMES,
        "defaults": DEFAULT_PARAMS,
        "presets": PRESETS,
        "live_allowed": config.ALLOW_LIVE,
        "has_live_keys": bool(config.BINANCE_API_KEY and config.BINANCE_API_SECRET),
        "has_demo_keys": bool(config.BINANCE_DEMO_API_KEY and config.BINANCE_DEMO_API_SECRET),
        "poll_seconds": config.POLL_SECONDS,
    }


# ---------- боты ----------

@app.get("/api/bots")
async def bots():
    return [_view(b) for b in db.list_bots()]


@app.post("/api/bots")
async def create_bot(data: Dict[str, Any]):
    bot = normalize_bot(data)
    bot_id = db.create_bot(bot)
    return _view(db.get_bot(bot_id))


@app.put("/api/bots/{bot_id}")
async def update_bot(bot_id: int, data: Dict[str, Any]):
    old = _bot_or_404(bot_id)
    if manager.is_running(bot_id):
        raise ValueError("Сначала остановите бота, потом меняйте настройки")
    bot = normalize_bot(data)
    if old["state"].get("position") and any(
            old[k] != bot[k] for k in ("market", "symbol", "mode")):
        raise ValueError("Пока открыта позиция, нельзя менять рынок, пару или режим")
    db.update_bot(bot_id, bot)
    return _view(db.get_bot(bot_id))


@app.delete("/api/bots/{bot_id}")
async def delete_bot(bot_id: int):
    bot = _bot_or_404(bot_id)
    if manager.is_running(bot_id):
        raise ValueError("Сначала остановите бота")
    if bot["state"].get("position"):
        raise ValueError("У бота открыта позиция: закройте её или уберите из памяти")
    db.delete_bot(bot_id)
    return {"ok": True}


@app.post("/api/bots/{bot_id}/start")
async def start(bot_id: int):
    _bot_or_404(bot_id)
    await manager.start(bot_id)
    return _view(db.get_bot(bot_id))


@app.post("/api/bots/{bot_id}/stop")
async def stop(bot_id: int):
    _bot_or_404(bot_id)
    await manager.stop(bot_id)
    return _view(db.get_bot(bot_id))


@app.post("/api/bots/{bot_id}/close")
async def close_position(bot_id: int):
    _bot_or_404(bot_id)
    await manager.close_position(bot_id)
    return _view(db.get_bot(bot_id))


@app.post("/api/bots/{bot_id}/forget")
async def forget_position(bot_id: int):
    _bot_or_404(bot_id)
    await manager.forget_position(bot_id)
    return _view(db.get_bot(bot_id))


@app.get("/api/bots/{bot_id}/logs")
async def logs(bot_id: int):
    return list(manager.logs.get(bot_id, []))


@app.get("/api/trades")
async def trades(bot_id: Optional[int] = None, limit: int = 200):
    return db.list_trades(bot_id, min(max(limit, 1), 1000))


# ---------- проверка на истории ----------

@app.post("/api/backtest")
async def backtest(data: Dict[str, Any]):
    bot = normalize_bot(data)
    n = int(data.get("candles") or 2000)
    n = min(max(n, 200), 10000)
    candles = await fetch_history(bot["market"], bot["symbol"], bot["timeframe"], n)
    result = await run_backtest(bot, candles)
    result["deepest_so_pct"] = so_deviation_pct(bot["params"], bot["params"]["safety_orders"])
    return result
