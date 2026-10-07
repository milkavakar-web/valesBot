"""Параметры бота, проверка настроек, пресеты и сигналы на вход."""
import re
from typing import Any, Dict, Optional

from .indicators import ema, rsi

TIMEFRAMES = ["1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "1d"]
SIGNALS = ["rsi", "ema_cross", "none"]
MARKETS = ["spot", "futures"]
MODES = ["paper", "demo", "live"]
DIRECTIONS = ["long", "short", "both"]

DEFAULT_PARAMS: Dict[str, Any] = {
    "signal": "rsi",          # rsi | ema_cross | none (входить сразу)
    "rsi_period": 14,
    "rsi_low": 30,            # RSI ниже → сигнал в лонг
    "rsi_high": 70,           # RSI выше → сигнал в шорт
    "ema_fast": 9,
    "ema_slow": 21,
    "direction": "long",      # long | short | both (short/both только для фьючерсов)
    "order_size": 20.0,       # USDT на первый ордер (на фьючерсах это маржа)
    "leverage": 1,            # только фьючерсы
    "take_profit_pct": 1.5,   # от средней цены, 0 = выключен
    "stop_loss_pct": 0.0,     # от средней цены, 0 = выключен
    "safety_orders": 0,       # число усреднений
    "so_step_pct": 2.0,       # шаг первого усреднения, %
    "so_step_scale": 1.0,     # во сколько раз растёт каждый следующий шаг
    "so_volume_mult": 1.5,    # во сколько раз растёт объём каждого усреднения
    "exit_on_opposite": False,  # закрывать позицию по противоположному сигналу
}

PRESETS = [
    {
        "id": "dca",
        "title": "Усреднение по RSI (похоже на Veles)",
        "about": "Покупает на просадке, когда RSI ниже порога, докупает при падении и "
                 "закрывает всё с небольшой прибылью. Много мелких плюсовых сделок, "
                 "но при затяжном падении зависает в позиции.",
        "timeframe": "15m",
        "params": {"signal": "rsi", "rsi_period": 14, "rsi_low": 35, "rsi_high": 70,
                   "direction": "long", "take_profit_pct": 1.2, "stop_loss_pct": 0,
                   "safety_orders": 5, "so_step_pct": 1.5, "so_step_scale": 1.2,
                   "so_volume_mult": 1.5, "exit_on_opposite": False, "leverage": 1},
    },
    {
        "id": "trend",
        "title": "Тренд: пересечение EMA",
        "about": "Входит, когда быстрая средняя пересекает медленную, и держит позицию "
                 "до обратного пересечения. Ловит большие движения, но в боковике даёт "
                 "серии мелких убытков.",
        "timeframe": "1h",
        "params": {"signal": "ema_cross", "ema_fast": 9, "ema_slow": 21,
                   "direction": "long", "take_profit_pct": 0, "stop_loss_pct": 3,
                   "safety_orders": 0, "exit_on_opposite": True, "leverage": 1},
    },
    {
        "id": "rsi",
        "title": "Отскок по RSI со стопом",
        "about": "Покупает перепроданность (RSI ниже 30), выходит по тейку, стопу или "
                 "когда RSI поднимется выше 70. Без усреднений, риск каждой сделки ограничен.",
        "timeframe": "1h",
        "params": {"signal": "rsi", "rsi_period": 14, "rsi_low": 30, "rsi_high": 70,
                   "direction": "long", "take_profit_pct": 2.0, "stop_loss_pct": 4.0,
                   "safety_orders": 0, "exit_on_opposite": True, "leverage": 1},
    },
]

SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,15}/[A-Z0-9]{2,10}$")


def _num(d: dict, key: str, lo: float, hi: float, as_int: bool = False):
    raw = d.get(key, DEFAULT_PARAMS[key])
    try:
        v = int(float(raw)) if as_int else float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"Поле «{key}» должно быть числом")
    if not lo <= v <= hi:
        raise ValueError(f"Поле «{key}» должно быть от {lo:g} до {hi:g}")
    return v


def normalize_bot(data: Dict[str, Any]) -> Dict[str, Any]:
    """Проверяет настройки бота и возвращает очищенную копию. Ошибки — ValueError."""
    name = str(data.get("name") or "").strip()[:60]
    market = data.get("market", "spot")
    mode = data.get("mode", "paper")
    timeframe = data.get("timeframe", "15m")
    symbol = str(data.get("symbol") or "").strip().upper().replace("-", "/")
    if symbol and "/" not in symbol and symbol.endswith("USDT"):
        symbol = symbol[:-4] + "/USDT"

    if market not in MARKETS:
        raise ValueError("Рынок должен быть spot или futures")
    if mode not in MODES:
        raise ValueError("Режим должен быть paper, demo или live")
    if timeframe not in TIMEFRAMES:
        raise ValueError("Неизвестный таймфрейм")
    if not SYMBOL_RE.match(symbol):
        raise ValueError("Пара пишется так: BTC/USDT")

    src = dict(DEFAULT_PARAMS)
    src.update(data.get("params") or {})
    p: Dict[str, Any] = {}

    p["signal"] = src["signal"]
    if p["signal"] not in SIGNALS:
        raise ValueError("Неизвестный тип сигнала")
    p["direction"] = src["direction"]
    if p["direction"] not in DIRECTIONS:
        raise ValueError("Направление: long, short или both")
    if market == "spot" and p["direction"] != "long":
        raise ValueError("На споте можно торговать только в лонг")

    p["rsi_period"] = _num(src, "rsi_period", 2, 100, True)
    p["rsi_low"] = _num(src, "rsi_low", 1, 99)
    p["rsi_high"] = _num(src, "rsi_high", 1, 99)
    if p["rsi_low"] >= p["rsi_high"]:
        raise ValueError("Нижний порог RSI должен быть меньше верхнего")
    p["ema_fast"] = _num(src, "ema_fast", 2, 200, True)
    p["ema_slow"] = _num(src, "ema_slow", 3, 400, True)
    if p["ema_fast"] >= p["ema_slow"]:
        raise ValueError("Быстрая EMA должна быть короче медленной")

    p["order_size"] = _num(src, "order_size", 1, 1_000_000)
    p["leverage"] = _num(src, "leverage", 1, 20, True) if market == "futures" else 1
    p["take_profit_pct"] = _num(src, "take_profit_pct", 0, 100)
    p["stop_loss_pct"] = _num(src, "stop_loss_pct", 0, 95)
    p["safety_orders"] = _num(src, "safety_orders", 0, 15, True)
    p["so_step_pct"] = _num(src, "so_step_pct", 0.1, 50)
    p["so_step_scale"] = _num(src, "so_step_scale", 0.5, 3)
    p["so_volume_mult"] = _num(src, "so_volume_mult", 0.5, 5)
    p["exit_on_opposite"] = bool(src.get("exit_on_opposite"))

    if p["signal"] == "none" and p["direction"] == "both":
        raise ValueError("Без сигнала выберите одно направление: лонг или шорт")
    can_exit = p["take_profit_pct"] > 0 or p["stop_loss_pct"] > 0 or (
        p["exit_on_opposite"] and p["signal"] != "none")
    if not can_exit:
        raise ValueError("Боту нужен способ выйти: тейк-профит, стоп-лосс "
                         "или выход по противоположному сигналу")

    deepest = so_deviation_pct(p, p["safety_orders"])
    if p["direction"] in ("long", "both") and deepest >= 95:
        raise ValueError("Усреднения уходят ниже −95% — уменьшите шаг или их число")

    if not name:
        name = f"{symbol} {('спот' if market == 'spot' else 'фьючерсы')}"
    return {"name": name, "market": market, "symbol": symbol,
            "timeframe": timeframe, "mode": mode, "params": p}


def so_deviation_pct(p: Dict[str, Any], k: int) -> float:
    """Отклонение k-го усреднения от цены входа, в процентах."""
    return sum(p["so_step_pct"] * p["so_step_scale"] ** j for j in range(k))


def max_margin(p: Dict[str, Any]) -> float:
    """Сколько USDT (маржи) бот может вложить в одну сделку со всеми усреднениями."""
    return p["order_size"] * sum(p["so_volume_mult"] ** k for k in range(p["safety_orders"] + 1))


# ---------- сигналы ----------

def prepare(closes, p: Dict[str, Any]) -> Dict[str, list]:
    if p["signal"] == "rsi":
        return {"rsi": rsi(closes, p["rsi_period"])}
    if p["signal"] == "ema_cross":
        return {"fast": ema(closes, p["ema_fast"]), "slow": ema(closes, p["ema_slow"])}
    return {}


def signal_at(ind: Dict[str, list], i: int, p: Dict[str, Any]) -> Optional[str]:
    """Сигнал по закрытой свече i: 'long', 'short' или None."""
    s = p["signal"]
    if s == "rsi":
        v = ind["rsi"][i]
        if v is None:
            return None
        if v < p["rsi_low"]:
            return "long"
        if v > p["rsi_high"]:
            return "short"
        return None
    if s == "ema_cross":
        if i < 1:
            return None
        f0, s0 = ind["fast"][i - 1], ind["slow"][i - 1]
        f1, s1 = ind["fast"][i], ind["slow"][i]
        if None in (f0, s0, f1, s1):
            return None
        if f0 <= s0 and f1 > s1:
            return "long"
        if f0 >= s0 and f1 < s1:
            return "short"
        return None
    # "none": входим сразу после закрытия предыдущей сделки
    return "short" if p["direction"] == "short" else "long"


def indicator_snapshot(ind: Dict[str, list], i: int, p: Dict[str, Any]) -> Dict[str, Any]:
    if p["signal"] == "rsi":
        return {"rsi": ind["rsi"][i]}
    if p["signal"] == "ema_cross":
        return {"ema_fast": ind["fast"][i], "ema_slow": ind["slow"][i]}
    return {}
