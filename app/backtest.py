"""Проверка стратегии на истории.

Внутри свечи цена условно проходит путь open → low → high → close
(для падающей свечи open → high → low → close). Тейк, стоп и усреднения
исполняются по своему уровню, а если свеча открылась уже за уровнем — по цене
открытия. Ликвидация на фьючерсах не моделируется.
"""
import asyncio
from collections import OrderedDict
from typing import Any, Dict, List, Optional

from .brokers import PaperBroker, exchange_symbol, hub
from . import rules
from .strategy import TF_MS, active_sets, entry_condition, max_margin, prepare
from .trader import Trader

MAX_CANDLES = 40_000  # за одну проверку; больше — долго качать и тяжело рисовать

# Последние загруженные истории: повторная проверка с другими настройками
# на том же периоде не качает свечи заново.
_cache: "OrderedDict[tuple, List[list]]" = OrderedDict()


async def fetch_history(market: str, symbol: str, timeframe: str,
                        since: int, until: int) -> List[list]:
    """Закрытые свечи, открывшиеся в промежутке [since, until) (мс)."""
    ex = await hub.get(market, "public")
    sym = exchange_symbol(market, symbol)
    if sym not in ex.markets:
        raise ValueError(f"Пары {symbol} нет на Binance")
    tf_ms = TF_MS[timeframe]
    since = since // tf_ms * tf_ms
    until = min(until, ex.milliseconds() // tf_ms * tf_ms)  # текущая свеча ещё не закрыта
    key = (market, sym, timeframe, since, until)
    if key in _cache:
        _cache.move_to_end(key)
        return _cache[key]
    out: Dict[int, list] = {}
    cursor = since
    while cursor < until:
        batch = await ex.fetch_ohlcv(sym, timeframe, since=cursor, limit=1000)
        if not batch:
            break
        for c in batch:
            out[c[0]] = c
        nxt = batch[-1][0] + tf_ms
        if nxt <= cursor:
            break
        cursor = nxt
    candles = [out[k] for k in sorted(out) if since <= k < until]
    _cache[key] = candles
    while len(_cache) > 6:
        _cache.popitem(last=False)
    return candles


def chart_data(candles: List[list], ind: Dict[str, list], p: Dict[str, Any], start: int,
               events: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Свечи, индикаторы из условий и сделки для графика, начиная со свечи start."""
    series, thresholds = rules.chart_series(p["rules"], ind, active_sets(p), start)
    return {
        "candles": [[c[0], float(c[1]), float(c[2]), float(c[3]), float(c[4])]
                    for c in candles[start:]],
        "series": series,          # линии: pane = main (поверх свечей) или своя панель
        "thresholds": thresholds,  # числа из условий: горизонтальные линии на панелях
        "events": events,
    }


def _max_drawdown(values: List[float]) -> float:
    peak, dd = 0.0, 0.0
    for v in values:
        peak = max(peak, v)
        dd = max(dd, peak - v)
    return dd


def _round_trips(trades: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Склеивает ордера в сделки: вход → усреднения → выход."""
    rounds: List[Dict[str, Any]] = []
    cur: Optional[Dict[str, Any]] = None
    for t in trades:
        if t["action"] == "open":
            cur = {"side": t["side"], "open_ts": t["ts"], "open_price": t["price"],
                   "entry_reason": t["reason"], "adds": [], "fees": t["fee"]}
        elif t["action"] == "safety":
            cur["adds"].append({"ts": t["ts"], "price": t["price"]})
            cur["fees"] += t["fee"]
        else:
            cur.update(close_ts=t["ts"], close_price=t["price"], exit_reason=t["reason"],
                       avg=t["avg"], pnl=t["pnl"], pnl_pct=t["pnl_pct"])
            cur["fees"] += t["fee"]
            rounds.append(cur)
            cur = None
    if cur:
        rounds.append(cur)  # открыта на конец периода, дополним снаружи
    return rounds


async def run_backtest(bot: Dict[str, Any], candles: List[list], start: int = 1) -> Dict[str, Any]:
    """Прогоняет бота по свечам candles[start:]. Свечи до start нужны только
    индикаторам — чтобы сигналы с первого дня периода были такими же, как вживую."""
    start = max(1, start)
    if len(candles) - start < 50:
        raise ValueError("Слишком мало истории для проверки: нужно хотя бы 50 свечей")
    p = bot["params"]
    state: Dict[str, Any] = {}
    trades: List[Dict[str, Any]] = []

    async def record(t: Dict[str, Any]) -> None:
        trades.append(t)

    trader = Trader(bot, PaperBroker(bot["market"]), state, record, lambda m: None)
    closes = [float(c[4]) for c in candles]
    ind = await asyncio.to_thread(prepare, candles, p)  # сигнал-скрипт может считать долго

    equity: List[float] = []
    signal_bars = 0   # свечей, на которых выполнялось условие входа
    max_used = 0.0    # сколько маржи реально было в сделке на пике
    for j in range(start, len(candles)):
        ts, o, h, l, c = candles[j][0], *map(float, candles[j][1:5])
        # сигнал по закрытой свече j-1, вход по открытию свечи j
        await trader.on_candle(ind, j - 1, o, ts)
        sig = state.get("last_signal")
        if sig and p["direction"] in ("both", sig):
            signal_bars += 1
        path = (o, l, h, c) if c >= o else (o, h, l, c)
        for n, px in enumerate(path):
            # на открытии свечи цена могла перепрыгнуть уровень — тогда ордер
            # исполнится по цене открытия, а не по уровню
            await trader.on_price(px, ts, sim=n > 0)
        pos = state.get("position")
        if pos:
            max_used = max(max_used, pos["margin"])
        realized = state.get("stats", {}).get("pnl", 0.0)
        equity.append(round(realized + Trader.unrealized(pos, c), 4))

    stats = state.get("stats", {"closed": 0, "wins": 0, "pnl": 0.0, "fees": 0.0})
    capital = max_margin(p)
    pos = state.get("position")
    last_price = closes[-1]
    first_price = float(candles[start][1])

    rounds = _round_trips(trades)
    if pos:
        rounds[-1].update(open_at_end=True, avg=pos["avg"],
                          pnl=Trader.unrealized(pos, last_price),
                          pnl_pct=Trader.unrealized(pos, last_price) / pos["margin"] * 100)
    closed = [r for r in rounds if not r.get("open_at_end")]
    pnls = [r["pnl"] for r in closed]

    reasons: Dict[str, Dict[str, Any]] = {}
    for r in closed:
        kind = r["exit_reason"].split(":")[0]  # «обратный сигнал: RSI 72 выше 70» → «обратный сигнал»
        g = reasons.setdefault(kind, {"reason": kind, "count": 0, "pnl": 0.0})
        g["count"] += 1
        g["pnl"] += r["pnl"]
    by_reason = sorted(reasons.values(), key=lambda g: -g["count"])
    if pos:
        by_reason.append({"reason": "ещё открыта", "count": 1, "pnl": rounds[-1]["pnl"]})

    events = [{k: t.get(k) for k in ("ts", "price", "action", "side", "reason", "pnl", "pnl_pct")}
              for t in trades]

    return {
        "from": candles[start][0],
        "to": candles[-1][0],
        "candles": len(candles) - start,
        "timeframe": bot["timeframe"],
        "price_change_pct": (last_price / first_price - 1) * 100,
        "summary": {
            "trades": stats["closed"],
            "wins": stats["wins"],
            "losses": stats["closed"] - stats["wins"],
            "winrate": stats["wins"] / stats["closed"] * 100 if stats["closed"] else None,
            "pnl": stats["pnl"],
            "pnl_pct": stats["pnl"] / capital * 100 if capital else None,
            "fees": stats["fees"],
            "capital": capital,
            "max_used": max_used,
            "max_drawdown": _max_drawdown(equity),
            "avg_trade": sum(pnls) / len(pnls) if pnls else None,
            "best": max(pnls, default=None),
            "worst": min(pnls, default=None),
        },
        "signals": {"condition": entry_condition(p), "bars": signal_bars,
                    "entries": len(rounds)},
        "by_reason": by_reason,
        "trades": rounds,
        "chart": {**chart_data(candles, ind, p, start, events), "equity": equity},
    }
