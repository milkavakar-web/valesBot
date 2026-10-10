"""Веб-панель и API."""
import asyncio
import logging
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

import ccxt.async_support as ccxt
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from . import config, db, rules, scripts
from .backtest import MAX_CANDLES, chart_data, fetch_history, run_backtest
from .brokers import exchange_symbol, hub
from .engine import manager
from .notify import notifier
from .strategy import (DEFAULT_PARAMS, PRESETS, TF_MS, TIMEFRAMES, TUNED_BOTS, max_margin,
                       normalize_bot, prepare, warmup_candles)
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
    await notifier.start()
    await manager.startup()
    running = sum(manager.is_running(b["id"]) for b in db.list_bots())
    notifier.send(f"Программа бота запущена, работают ботов: {running}", silent=True)
    yield
    await manager.shutdown()
    notifier.send("Программа бота остановлена. Стопы на бирже остаются на месте.", silent=True)
    await notifier.stop()
    await hub.close_all()


app = FastAPI(title="Binance bot", lifespan=lifespan, dependencies=[Depends(auth)],
              docs_url=None, redoc_url=None)

def _hostname(netloc: str) -> Optional[str]:
    try:
        return urlsplit("//" + netloc).hostname
    except ValueError:
        return None


ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1", *config.PANEL_HOSTS}
if config.HOST not in ("0.0.0.0", "::"):
    ALLOWED_HOSTS.add(config.HOST.lower())


@app.middleware("http")
async def same_site_only(request: Request, call_next):
    """Не даёт другим сайтам, открытым в том же браузере, управлять ботами.

    Host: сайт злоумышленника может направить свой домен на 127.0.0.1 (DNS rebinding),
    тогда его запросы приходят с чужим именем в Host.
    Origin: любой сайт может отправить POST на 127.0.0.1:8000 (CSRF), и браузер
    подпишет такой запрос адресом этого сайта.
    """
    host = request.headers.get("host", "")
    if _hostname(host) not in ALLOWED_HOSTS:
        return JSONResponse({"detail": f"Панель открыта по адресу {host}. Откройте её через "
                                       f"http://127.0.0.1:{config.PORT} или добавьте адрес "
                                       f"в PANEL_HOSTS в .env"}, status_code=403)
    if request.method not in ("GET", "HEAD"):
        origin = request.headers.get("origin")
        if origin is not None and origin.lower().partition("://")[2] != host.lower():
            return JSONResponse({"detail": "Запрос с другого сайта отклонён"}, status_code=403)
    return await call_next(request)


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
            "tp": tr.tp_level(pos) if tr.has_tp(pos) else None,
            "sl": tr.sl_level(pos) if tr.has_sl(pos) else None,
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
        "indicators": rules.catalog_api(),  # из этого описания панель строит конструктор условий
        "ops": [{"id": k, "name": v} for k, v in rules.OPS.items()],
        "presets": PRESETS,
        "scripts": [s.info() for s in scripts.all_scripts()],
        "scripts_editable": scripts.editable(),
        "script_template": scripts.TEMPLATE,
        "live_allowed": config.ALLOW_LIVE,
        "has_live_keys": bool(config.BINANCE_API_KEY and config.BINANCE_API_SECRET),
        "has_demo_keys": bool(config.BINANCE_DEMO_API_KEY and config.BINANCE_DEMO_API_SECRET),
        "poll_seconds": config.POLL_SECONDS,
        "telegram": {"enabled": notifier.enabled, "has_token": bool(config.TELEGRAM_BOT_TOKEN),
                     "has_chat": bool(config.TELEGRAM_CHAT_ID), "chat": config.TELEGRAM_CHAT_ID,
                     "paper": config.TELEGRAM_PAPER},
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


# ---------- сигналы-скрипты ----------

def _scripts_editable() -> None:
    if not scripts.editable():
        raise HTTPException(403, "Скрипт выполняется со всеми правами программы, поэтому загружать "
                                 "его можно только из панели, открытой на этом компьютере. Чтобы "
                                 "разрешить из сети, поставьте ALLOW_SCRIPTS=1 в .env")


def _script_users(sid: str):
    return [b for b in db.list_bots()
            if b["params"].get("signal") == "script" and b["params"].get("script") == sid]


@app.get("/api/scripts")
async def list_scripts():
    return [{**s.info(), "bots": len(_script_users(s.id))} for s in scripts.all_scripts()]


@app.get("/api/scripts/{sid}")
async def get_script(sid: str):
    s = next((s for s in scripts.all_scripts() if s.id == sid), None)
    if not s:
        raise HTTPException(404, "Сигнал не найден")
    return s.info(with_code=True)


@app.post("/api/scripts")
async def save_script(data: Dict[str, Any]):
    """Новый сигнал (id не задан) или правка существующего. Работающие боты подхватят
    новый код на следующей свече — перезапускать ничего не нужно."""
    _scripts_editable()
    code, sid = data.get("code"), data.get("id") or None
    if not isinstance(code, str) or not code.strip():
        raise ValueError("Вставьте код сигнала")
    if sid is None:
        mod_name = scripts.slug(scripts.check(code).NAME)
        if any(s.id == mod_name for s in scripts.all_scripts()):
            raise ValueError("Сигнал с таким названием уже есть: поменяйте NAME или откройте "
                             "тот сигнал и правьте его")
    return await asyncio.to_thread(scripts.save, code, sid)


@app.delete("/api/scripts/{sid}")
async def delete_script(sid: str):
    _scripts_editable()
    users = _script_users(sid)
    if users:
        raise ValueError("Сигнал используют боты: " + ", ".join(b["name"] for b in users) +
                         ". Сначала удалите их или переключите на другой сигнал")
    scripts.delete(sid)
    return {"ok": True}


@app.post("/api/bots/tuned")
async def create_tuned_bots():
    """Бумажные боты с настройками, подобранными по истории (strategy.TUNED_BOTS).
    Бот с таким же именем уже есть — пропускаем, чтобы повторное нажатие не плодило копии."""
    have = {b["name"] for b in db.list_bots()}
    created = []
    for t in TUNED_BOTS:
        if t["name"] in have:
            continue
        bot_id = db.create_bot(normalize_bot({**t, "mode": "paper"}))
        created.append(_view(db.get_bot(bot_id)))
    return {"created": created, "skipped": len(TUNED_BOTS) - len(created)}


@app.put("/api/bots/{bot_id}")
async def update_bot(bot_id: int, data: Dict[str, Any]):
    old = _bot_or_404(bot_id)
    if manager.is_running(bot_id):
        raise ValueError("Сначала остановите бота, потом меняйте настройки")
    bot = normalize_bot(data)
    st = old["state"]
    if (st.get("position") or st.get("pending")) and any(
            old[k] != bot[k] for k in ("market", "symbol", "mode")):
        raise ValueError("Пока открыта позиция, нельзя менять рынок, пару или режим")
    db.update_bot(bot_id, bot)
    if st.get("dust") and any(old[k] != bot[k] for k in ("market", "symbol")):
        st["dust"] = None  # остаток монет относится к старой паре
        db.save_state(bot_id, st)
    return _view(db.get_bot(bot_id))


@app.delete("/api/bots/{bot_id}")
async def delete_bot(bot_id: int):
    bot = _bot_or_404(bot_id)
    if manager.is_running(bot_id):
        raise ValueError("Сначала остановите бота")
    if bot["state"].get("position") or bot["state"].get("pending"):
        raise ValueError("У бота открыта позиция или неподтверждённый ордер: "
                         "закройте позицию или уберите её из памяти")
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


@app.post("/api/telegram/test")
async def telegram_test():
    """Пробное сообщение в Telegram; без chat_id — подсказка, какой chat_id вписать."""
    return await notifier.test()


# ---------- проверка на истории и графики ----------

def _thousands(n: int) -> str:
    return f"{n:,}".replace(",", "\u00a0")


@app.post("/api/backtest")
async def backtest(data: Dict[str, Any]):
    """Прогон настроек бота по истории за период from–to (мс)."""
    bot = normalize_bot(data)
    p, tf = bot["params"], bot["timeframe"]
    tf_ms = TF_MS[tf]
    now = int(time.time() * 1000)
    try:
        until = min(int(data.get("to") or now), now)
        since = int(data.get("from") or until - 90 * 86_400_000)
    except (TypeError, ValueError):
        raise ValueError("Период задан неверно")
    if since >= until:
        raise ValueError("Начало периода должно быть раньше конца")
    n = (until - since) // tf_ms
    if n > MAX_CANDLES:
        raise ValueError(f"За этот период набирается {_thousands(n)} свечей {tf}, а за раз можно "
                         f"проверить не больше {_thousands(MAX_CANDLES)}. "
                         f"Возьмите период короче или свечи крупнее")
    # свечи до начала периода нужны, чтобы индикатор успел «разогреться»
    candles = await fetch_history(bot["market"], bot["symbol"], tf,
                                  since - warmup_candles(p) * tf_ms, until)
    start = next((i for i, c in enumerate(candles) if c[0] >= since), len(candles))
    return await run_backtest(bot, candles, start)


@app.get("/api/bots/{bot_id}/chart")
async def bot_chart(bot_id: int):
    """Последние свечи пары, индикатор бота, его сделки и уровни открытой позиции."""
    bot = _bot_or_404(bot_id)
    p, tf_ms = bot["params"], TF_MS[bot["timeframe"]]
    ex = await hub.get(bot["market"], "public")
    sym = exchange_symbol(bot["market"], bot["symbol"])
    if sym not in ex.markets:
        raise ValueError(f"Пары {bot['symbol']} нет на Binance")
    ohlcv = await ex.fetch_ohlcv(sym, bot["timeframe"], limit=1000)
    if len(ohlcv) < 2:
        raise RuntimeError("Биржа вернула слишком мало свечей")
    start = max(0, min(warmup_candles(p), len(ohlcv) - 300))
    ind = await asyncio.to_thread(prepare, ohlcv, p)
    first = ohlcv[start][0]
    # сделка случилась внутри свечи — на графике ставим её на время открытия этой свечи
    events = [{"ts": t["ts"] // tf_ms * tf_ms, "price": t["price"], "action": t["action"],
               "side": t["side"], "reason": t["reason"], "pnl": t["pnl"], "pnl_pct": None}
              for t in reversed(db.list_trades(bot_id, 1000)) if t["ts"] >= first]
    return {"chart": chart_data(ohlcv, ind, p, start, events), "levels": _view(bot)["levels"]}
