"""Проверка стратегии на истории.

Внутри свечи цена условно проходит путь open → low → high → close
(для падающей свечи open → high → low → close). Тейк, стоп и усреднения
исполняются ровно по своему уровню. Ликвидация на фьючерсах не моделируется.
"""
from typing import Any, Dict, List

from .brokers import PaperBroker, exchange_symbol, hub
from .strategy import max_margin, prepare
from .trader import Trader


async def fetch_history(market: str, symbol: str, timeframe: str, n: int) -> List[list]:
    ex = await hub.get(market, "public")
    sym = exchange_symbol(market, symbol)
    if sym not in ex.markets:
        raise ValueError(f"Пары {symbol} нет на Binance")
    tf_ms = ex.parse_timeframe(timeframe) * 1000
    now = ex.milliseconds()
    since = now - (n + 1) * tf_ms
    out: Dict[int, list] = {}
    while since < now:
        batch = await ex.fetch_ohlcv(sym, timeframe, since=since, limit=1000)
        if not batch:
            break
        for c in batch:
            out[c[0]] = c
        nxt = batch[-1][0] + tf_ms
        if nxt <= since:
            break
        since = nxt
    candles = [out[k] for k in sorted(out) if k + tf_ms <= now]  # только закрытые
    return candles[-n:]


def _max_drawdown(values: List[float]) -> float:
    peak, dd = 0.0, 0.0
    for v in values:
        peak = max(peak, v)
        dd = max(dd, peak - v)
    return dd


async def run_backtest(bot: Dict[str, Any], candles: List[list]) -> Dict[str, Any]:
    if len(candles) < 50:
        raise ValueError("Слишком мало истории для проверки")
    p = bot["params"]
    state: Dict[str, Any] = {}
    trades: List[Dict[str, Any]] = []

    async def record(t: Dict[str, Any]) -> None:
        trades.append(t)

    trader = Trader(bot, PaperBroker(bot["market"]), state, record, lambda m: None)
    closes = [float(c[4]) for c in candles]
    ind = prepare(closes, p)

    equity: List[float] = []
    times: List[int] = []
    for j in range(1, len(candles)):
        ts, o, h, l, c = candles[j][0], *map(float, candles[j][1:5])
        # сигнал по закрытой свече j-1, вход по открытию свечи j
        await trader.on_candle(ind, j - 1, o, ts)
        path = (o, l, h, c) if c >= o else (o, h, l, c)
        for px in path:
            await trader.on_price(px, ts, sim=True)
        realized = state.get("stats", {}).get("pnl", 0.0)
        equity.append(realized + Trader.unrealized(state.get("position"), c))
        times.append(ts)

    closed = [t for t in trades if t["action"] == "close"]
    stats = state.get("stats", {"closed": 0, "wins": 0, "pnl": 0.0, "fees": 0.0})
    capital = max_margin(p)
    pos = state.get("position")
    last_price = closes[-1]

    durations = [t["ts"] - t["opened_ts"] for t in closed]
    step = max(1, len(equity) // 300)
    curve = [{"t": times[i], "v": round(equity[i], 4)} for i in range(0, len(equity), step)]
    if equity and curve[-1]["t"] != times[-1]:
        curve.append({"t": times[-1], "v": round(equity[-1], 4)})

    return {
        "from": candles[0][0],
        "to": candles[-1][0],
        "candles": len(candles),
        "price_change_pct": (last_price / closes[0] - 1) * 100,
        "trades": stats["closed"],
        "wins": stats["wins"],
        "winrate": stats["wins"] / stats["closed"] * 100 if stats["closed"] else None,
        "pnl": stats["pnl"],
        "fees": stats["fees"],
        "capital": capital,
        "pnl_pct": stats["pnl"] / capital * 100 if capital else None,
        "max_drawdown": _max_drawdown(equity),
        "max_so_used": max((t.get("so_used", 0) for t in closed), default=0),
        "avg_duration_ms": sum(durations) / len(durations) if durations else None,
        "open_position": None if not pos else {
            "side": pos["side"], "avg": pos["avg"], "so_filled": pos["so_filled"],
            "unrealized": Trader.unrealized(pos, last_price)},
        "equity": curve,
        "last_trades": [
            {k: t.get(k) for k in ("ts", "side", "price", "pnl", "pnl_pct", "reason", "so_used")}
            for t in closed[-30:]][::-1],
    }
