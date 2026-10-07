"""Параметры бота, проверка настроек, шаблоны и сигналы на вход и выход."""
import re
from typing import Any, Dict, List, Optional

from . import rules

TIMEFRAMES = ["1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "1d"]
TF_MS = {"1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
         "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000, "1d": 86_400_000}
SIGNALS = ["rules", "none"]
MARKETS = ["spot", "futures"]
MODES = ["paper", "demo", "live"]
DIRECTIONS = ["long", "short", "both"]


# ---------- короткая запись условий для шаблонов ----------

def _i(ind: str, out: str = "", **params) -> Dict[str, Any]:
    spec = rules.CATALOG[ind]
    o = {"ind": ind, "out": out or next(iter(spec["outputs"]))}
    o.update({k: params.get(k, d) for k, _, d, *_ in spec["params"]})
    return o


def _c(left, op, right) -> Dict[str, Any]:
    return {"left": left, "op": op, "right": right if isinstance(right, dict) else {"value": right}}


def _sets(**kw) -> Dict[str, list]:
    return {name: kw.get(name, []) for name in rules.SETS}


PRICE = _i("price")
RSI14 = _i("rsi", period=14)

DEFAULT_PARAMS: Dict[str, Any] = {
    "signal": "rules",        # rules — по условиям, none — входить сразу
    "rules": _sets(long=[_c(RSI14, "below", 30)], short=[_c(RSI14, "above", 70)]),
    "direction": "long",      # long | short | both (short/both только для фьючерсов)
    "order_size": 20.0,       # USDT на первый ордер (на фьючерсах это маржа)
    "leverage": 1,            # только фьючерсы
    "take_profit_pct": 1.5,   # от средней цены, 0 = выключен
    "stop_loss_pct": 0.0,     # от средней цены, 0 = выключен
    "safety_orders": 0,       # число усреднений
    "so_step_pct": 2.0,       # шаг первого усреднения, %
    "so_step_scale": 1.0,     # во сколько раз растёт каждый следующий шаг
    "so_volume_mult": 1.5,    # во сколько раз растёт объём каждого усреднения
    "exit_on_opposite": False,  # «Оба»: закрыть позицию и развернуться по сигналу в другую сторону
}

_DCA = {"safety_orders": 5, "so_step_pct": 1.5, "so_step_scale": 1.2, "so_volume_mult": 1.5,
        "take_profit_pct": 1.2, "stop_loss_pct": 0}
_NO_DCA = {"safety_orders": 0}

PRESETS = [
    {
        "id": "dca", "title": "DCA: усреднение по RSI", "timeframe": "15m", "market": "spot",
        "about": "Покупает на просадке, докупает при падении и закрывает всё с небольшой прибылью. "
                 "Очень много плюсовых сделок, но прибыль мелкая, а риск скрытый: при долгом падении "
                 "в позиции застревает весь депозит. На споте без плеча в худшем случае вы просто "
                 "держите монету; на фьючерсах с плечом так можно дойти до ликвидации.",
        "params": {**_DCA, "direction": "long",
                   "rules": _sets(long=[_c(RSI14, "below", 35)])},
    },
    {
        "id": "dca_trend", "title": "DCA только в восходящем тренде", "timeframe": "15m", "market": "spot",
        "about": "То же усреднение, но покупает, только пока цена выше EMA(200). Меньше шансов "
                 "начать сделку в начале затяжного падения, зато и сделок меньше.",
        "params": {**_DCA, "direction": "long",
                   "rules": _sets(long=[_c(RSI14, "below", 35), _c(PRICE, "above", _i("ema", period=200))])},
    },
    {
        "id": "trend_ema", "title": "Тренд: пересечение EMA 20 и 50", "timeframe": "1h", "market": "spot",
        "about": "Входит, когда быстрая средняя пересекает медленную снизу вверх, и держит позицию до "
                 "обратного пересечения. Половина сделок и больше — в минус, но редкие большие движения "
                 "их перекрывают. Психологически тяжело: бывают длинные серии убытков.",
        "params": {**_NO_DCA, "direction": "long", "take_profit_pct": 0, "stop_loss_pct": 5,
                   "rules": _sets(long=[_c(_i("ema", period=20), "cross_up", _i("ema", period=50))],
                                  exit_long=[_c(_i("ema", period=20), "cross_down", _i("ema", period=50))])},
    },
    {
        "id": "breakout", "title": "Тренд: пробой Боллинджера на объёме", "timeframe": "1h", "market": "spot",
        "about": "Входит, когда цена выходит выше верхней полосы Боллинджера при объёме в полтора раза "
                 "больше обычного, выходит при возврате к средней линии. Ловит начало сильных движений; "
                 "ложные пробои дают серии мелких стопов.",
        "params": {**_NO_DCA, "direction": "long", "take_profit_pct": 0, "stop_loss_pct": 3,
                   "rules": _sets(long=[_c(PRICE, "cross_up", _i("bb", "upper")),
                                        _c(_i("vol_ratio", n=20), "above", 1.5)],
                                  exit_long=[_c(PRICE, "cross_down", _i("bb", "middle"))])},
    },
    {
        "id": "rsi_trend", "title": "Отскок RSI по тренду", "timeframe": "1h", "market": "spot",
        "about": "Покупает перепроданность (RSI ниже 30), но только когда цена выше EMA(200), то есть "
                 "в восходящем тренде. Выходит, когда RSI поднимется выше 60, по тейку или по стопу.",
        "params": {**_NO_DCA, "direction": "long", "take_profit_pct": 3, "stop_loss_pct": 4,
                   "rules": _sets(long=[_c(RSI14, "below", 30), _c(PRICE, "above", _i("ema", period=200))],
                                  exit_long=[_c(RSI14, "above", 60)])},
    },
    {
        "id": "range_bb", "title": "Боковик: от нижней полосы к средней", "timeframe": "15m", "market": "spot",
        "about": "Работает как сетка, но проще: покупает, когда цена возвращается в канал снизу через "
                 "нижнюю полосу Боллинджера, и продаёт у средней линии. Хорошо в боковике, проигрывает, "
                 "когда цена уходит из диапазона, поэтому со стопом.",
        "params": {**_NO_DCA, "direction": "long", "take_profit_pct": 0, "stop_loss_pct": 3,
                   "rules": _sets(long=[_c(PRICE, "cross_up", _i("bb", "lower"))],
                                  exit_long=[_c(PRICE, "cross_up", _i("bb", "middle"))])},
    },
    {
        "id": "macd", "title": "MACD: разворот ниже нуля", "timeframe": "4h", "market": "spot",
        "about": "Входит, когда линия MACD пересекает сигнальную снизу вверх, пока обе ниже нуля "
                 "(разворот после падения). Выходит при обратном пересечении.",
        "params": {**_NO_DCA, "direction": "long", "take_profit_pct": 0, "stop_loss_pct": 6,
                   "rules": _sets(long=[_c(_i("macd", "macd"), "cross_up", _i("macd", "signal")),
                                        _c(_i("macd", "macd"), "below", 0)],
                                  exit_long=[_c(_i("macd", "macd"), "cross_down", _i("macd", "signal"))])},
    },
    {
        "id": "stoch", "title": "Стохастик из перепроданности", "timeframe": "1h", "market": "spot",
        "about": "Покупает, когда %K пересекает %D снизу вверх в зоне ниже 20, продаёт, когда %K "
                 "поднимется выше 80. Много коротких сделок, в сильном падении стоп срабатывает часто.",
        "params": {**_NO_DCA, "direction": "long", "take_profit_pct": 0, "stop_loss_pct": 3,
                   "rules": _sets(long=[_c(_i("stoch", "k"), "cross_up", _i("stoch", "d")),
                                        _c(_i("stoch", "k"), "below", 20)],
                                  exit_long=[_c(_i("stoch", "k"), "above", 80)])},
    },
    {
        "id": "trend_both", "title": "Тренд в обе стороны (фьючерсы)", "timeframe": "4h", "market": "futures",
        "about": "Пересечение EMA 20 и 50 на фьючерсах: вверх — лонг, вниз — шорт, при обратном сигнале "
                 "бот закрывает позицию и сразу разворачивается. Всегда в рынке, поэтому плечо 1 и стоп.",
        "params": {**_NO_DCA, "direction": "both", "take_profit_pct": 0, "stop_loss_pct": 6,
                   "leverage": 1, "exit_on_opposite": True,
                   "rules": _sets(long=[_c(_i("ema", period=20), "cross_up", _i("ema", period=50))],
                                  short=[_c(_i("ema", period=20), "cross_down", _i("ema", period=50))])},
    },
]

SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,15}/[A-Z0-9]{2,10}$")


def upgrade_params(p: Dict[str, Any]) -> Dict[str, Any]:
    """Переводит сигналы старого формата (RSI, пересечение EMA) в наборы условий.
    Поведение бота не меняется."""
    if p.get("signal") not in ("rsi", "ema_cross"):
        return p if "rules" in p else {**p, "rules": _sets()}  # старый бот «без сигнала»
    p = dict(p)
    r = rules.from_legacy(p)
    if p.get("exit_on_opposite") and p.get("direction") != "both":
        # раньше «выход по противоположному сигналу» в одну сторону = условие выхода
        r["exit_long"], r["exit_short"] = list(r["short"]), list(r["long"])
        p["exit_on_opposite"] = False
    p["signal"], p["rules"] = "rules", r
    return p


def _num(d: dict, key: str, lo: float, hi: float, as_int: bool = False):
    raw = d.get(key, DEFAULT_PARAMS[key])
    try:
        v = int(float(raw)) if as_int else float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"Поле «{key}» должно быть числом")
    if not lo <= v <= hi:
        raise ValueError(f"Поле «{key}» должно быть от {lo:g} до {hi:g}")
    return v


def _sides(p: Dict[str, Any]) -> List[str]:
    return {"long": ["long"], "short": ["short"], "both": ["long", "short"]}[p["direction"]]


def active_sets(p: Dict[str, Any]) -> List[str]:
    """Наборы условий, которые работают при выбранном направлении."""
    sides = _sides(p)
    entry = sides if p["signal"] == "rules" else []
    return entry + [f"exit_{s}" for s in sides]


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
    src = upgrade_params(src)
    p: Dict[str, Any] = {}

    p["signal"] = src["signal"]
    if p["signal"] not in SIGNALS:
        raise ValueError("Неизвестный тип сигнала")
    p["direction"] = src["direction"]
    if p["direction"] not in DIRECTIONS:
        raise ValueError("Направление: long, short или both")
    if market == "spot" and p["direction"] != "long":
        raise ValueError("На споте можно торговать только в лонг")
    p["rules"] = rules.normalize(src.get("rules"))

    p["order_size"] = _num(src, "order_size", 1, 1_000_000)
    p["leverage"] = _num(src, "leverage", 1, 20, True) if market == "futures" else 1
    p["take_profit_pct"] = _num(src, "take_profit_pct", 0, 100)
    p["stop_loss_pct"] = _num(src, "stop_loss_pct", 0, 95)
    p["safety_orders"] = _num(src, "safety_orders", 0, 15, True)
    p["so_step_pct"] = _num(src, "so_step_pct", 0.1, 50)
    p["so_step_scale"] = _num(src, "so_step_scale", 0.5, 3)
    p["so_volume_mult"] = _num(src, "so_volume_mult", 0.5, 5)
    p["exit_on_opposite"] = bool(src.get("exit_on_opposite")) and p["direction"] == "both"

    if p["signal"] == "none" and p["direction"] == "both":
        raise ValueError("Без условий выберите одно направление: лонг или шорт")
    names = {"long": "входа в лонг", "short": "входа в шорт"}
    if p["signal"] == "rules":
        for side in _sides(p):
            if not p["rules"][side]:
                raise ValueError(f"Добавьте хотя бы одно условие {names[side]}")
    can_exit = p["take_profit_pct"] > 0 or p["stop_loss_pct"] > 0 or p["exit_on_opposite"] or \
        all(p["rules"][f"exit_{s}"] for s in _sides(p))
    if not can_exit:
        raise ValueError("Боту нужен способ выйти: тейк-профит, стоп-лосс или условия выхода")

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


def warmup_candles(p: Dict[str, Any]) -> int:
    """Сколько свечей до начала периода нужно индикаторам, чтобы их значения
    перестали зависеть от того, с какой свечи начали считать."""
    return rules.warmup({k: p["rules"][k] for k in active_sets(p)})


# ---------- сигналы ----------

def prepare(ohlcv: List[list], p: Dict[str, Any]) -> Dict[str, list]:
    """Индикаторы из условий по свечам [время, open, high, low, close, volume]."""
    return rules.compute(ohlcv, {k: p["rules"][k] for k in active_sets(p)})


def signal_at(ind: Dict[str, list], i: int, p: Dict[str, Any]) -> Optional[str]:
    """Сигнал на вход по закрытой свече i: 'long', 'short' или None."""
    if p["signal"] == "none":  # входим сразу после закрытия предыдущей сделки
        return "short" if p["direction"] == "short" else "long"
    hits = [s for s in _sides(p) if rules.check_set(ind, p["rules"][s], i)]
    return hits[0] if len(hits) == 1 else None


def exit_reason(ind: Dict[str, list], i: int, p: Dict[str, Any], side: str) -> Optional[str]:
    """Причина выхода, если на свече i выполнены условия выхода из позиции side."""
    conds = p["rules"][f"exit_{side}"]
    if rules.check_set(ind, conds, i):
        return "условие выхода: " + rules.explain(ind, conds, i)
    return None


def describe_signal(ind: Dict[str, list], i: int, p: Dict[str, Any], sig: str) -> str:
    """Почему сработал сигнал на свече i — для журнала, сделок и бэктеста."""
    if p["signal"] == "none":
        return "вход без условий"
    return rules.explain(ind, p["rules"][sig], i)


def entry_condition(p: Dict[str, Any]) -> Optional[str]:
    """Условия входа словами: «RSI(14) ниже 30 и цена выше EMA(200)»."""
    if p["signal"] == "none":
        return None
    sides = _sides(p)
    if len(sides) == 1:
        return rules.text(p["rules"][sides[0]])
    return f"лонг — {rules.text(p['rules']['long'])}; шорт — {rules.text(p['rules']['short'])}"


def indicator_snapshot(ind: Dict[str, list], i: int, p: Dict[str, Any]) -> Dict[str, Any]:
    return rules.snapshot(ind, p["rules"], active_sets(p), i)
