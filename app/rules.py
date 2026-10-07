"""Конструктор условий.

Каждый индикатор описан здесь один раз. Из этого описания панель строит выпадающие
списки, а живой бот, бэктест и график считают значения одним и тем же кодом.

Условие — строка «индикатор, условие, значение»: {"left": операнд, "op": "below",
"right": операнд}. Операнд — индикатор {"ind": "rsi", "out": "value", "period": 14}
или число {"value": 30}. Набор условий выполняется, когда выполнены все строки («и»).
"""
from typing import Any, Dict, List, Optional, Tuple

from . import indicators as ta

def _o(name: str, nom: str, gen: Optional[str] = None, acc: Optional[str] = None) -> Dict[str, str]:
    """Линия индикатора: название в списке и подпись в тексте условия в трёх падежах —
    «верхняя полоса пересекает…», «выше верхней полосы», «пересекает верхнюю полосу»."""
    return {"name": name, "label": nom, "gen": gen or nom, "acc": acc or nom}


# Параметр: (ключ, подпись, по умолчанию, минимум, максимум, целое ли).
# pane — где линия на графике: main — поверх свечей (одна шкала с ценой),
# иначе отдельная панель с этой подписью. value — число по умолчанию справа в условии.
CATALOG: Dict[str, Dict[str, Any]] = {
    "price": {"title": "Цена", "params": [],
              "outputs": {"close": _o("цена закрытия", "цена", "цены", "цену")}, "pane": "main", "value": 0},
    "rsi": {"title": "RSI", "params": [("period", "период", 14, 2, 100, True)],
            "outputs": {"value": _o("RSI", "RSI({period})")}, "pane": "RSI({period})", "value": 30},
    "ema": {"title": "EMA", "params": [("period", "период", 20, 2, 500, True)],
            "outputs": {"value": _o("EMA", "EMA({period})")}, "pane": "main", "value": 0},
    "sma": {"title": "SMA", "params": [("period", "период", 20, 2, 500, True)],
            "outputs": {"value": _o("SMA", "SMA({period})")}, "pane": "main", "value": 0},
    "bb": {"title": "Полосы Боллинджера",
           "params": [("period", "период", 20, 2, 200, True), ("mult", "ширина, σ", 2, 0.5, 5, False)],
           "outputs": {"upper": _o("верхняя полоса", "верхняя полоса Боллинджера({period}, {mult})",
                                   "верхней полосы Боллинджера({period}, {mult})",
                                   "верхнюю полосу Боллинджера({period}, {mult})"),
                       "middle": _o("средняя линия", "средняя линия Боллинджера({period}, {mult})",
                                    "средней линии Боллинджера({period}, {mult})",
                                    "среднюю линию Боллинджера({period}, {mult})"),
                       "lower": _o("нижняя полоса", "нижняя полоса Боллинджера({period}, {mult})",
                                   "нижней полосы Боллинджера({period}, {mult})",
                                   "нижнюю полосу Боллинджера({period}, {mult})")},
           "pane": "main", "value": 0},
    "macd": {"title": "MACD",
             "params": [("fast", "быстрая", 12, 2, 100, True), ("slow", "медленная", 26, 3, 200, True),
                        ("signal", "сигнальная", 9, 2, 50, True)],
             "outputs": {"macd": _o("линия MACD", "MACD({fast}, {slow}, {signal})"),
                         "signal": _o("сигнальная линия", "сигнальная MACD({fast}, {slow}, {signal})",
                                      "сигнальной MACD({fast}, {slow}, {signal})",
                                      "сигнальную MACD({fast}, {slow}, {signal})"),
                         "hist": _o("гистограмма", "гистограмма MACD({fast}, {slow}, {signal})",
                                    "гистограммы MACD({fast}, {slow}, {signal})",
                                    "гистограмму MACD({fast}, {slow}, {signal})")},
             "pane": "MACD({fast}, {slow}, {signal})", "value": 0},
    "stoch": {"title": "Стохастик",
              "params": [("k", "%K", 14, 2, 100, True), ("d", "%D", 3, 1, 50, True),
                         ("smooth", "сглаживание", 3, 1, 50, True)],
              "outputs": {"k": _o("%K", "%K стохастика({k}, {d}, {smooth})"),
                          "d": _o("%D", "%D стохастика({k}, {d}, {smooth})")},
              "pane": "Стохастик({k}, {d}, {smooth})", "value": 20},
    "change": {"title": "Изменение цены за N свечей, %", "params": [("n", "свечей", 10, 1, 500, True)],
               "outputs": {"value": _o("изменение, %", "изменение цены за {n} свечей, %",
                                       "изменения цены за {n} свечей, %")},
               "pane": "Изменение за {n} свечей, %", "value": 0},
    "volume": {"title": "Объём", "params": [],
               "outputs": {"value": _o("объём", "объём", "объёма")}, "pane": "Объём", "value": 0},
    "vol_ratio": {"title": "Объём к среднему, ×", "params": [("n", "свечей", 20, 2, 500, True)],
                  "outputs": {"value": _o("объём к среднему", "объём к среднему за {n} свечей",
                                          "объёма к среднему за {n} свечей")},
                  "pane": "Объём к среднему({n}), ×", "value": 1.5},
}

OPS = {"above": "выше", "below": "ниже",
       "cross_up": "пересекает снизу вверх", "cross_down": "пересекает сверху вниз"}

SETS = ("long", "short", "exit_long", "exit_short")
MAX_ROWS = 8


def catalog_api() -> List[Dict[str, Any]]:
    """Описание индикаторов для панели."""
    return [{"id": ind, "title": s["title"], "value": s["value"],
             "params": [{"key": k, "label": lab, "default": d, "min": lo, "max": hi, "int": is_int}
                        for k, lab, d, lo, hi, is_int in s["params"]],
             "outputs": [{"key": k, **out} for k, out in s["outputs"].items()]}
            for ind, s in CATALOG.items()]


# ---------- проверка настроек ----------

def _operand(o: Any, allow_value: bool) -> Dict[str, Any]:
    if not isinstance(o, dict):
        raise ValueError("Условие заполнено не полностью")
    if "value" in o:
        if not allow_value:
            raise ValueError("Слева в условии должен быть индикатор, а не число")
        try:
            return {"value": float(o["value"])}
        except (TypeError, ValueError):
            raise ValueError("В условии вместо числа что-то другое")
    spec = CATALOG.get(o.get("ind"))
    if not spec:
        raise ValueError("Неизвестный индикатор в условии")
    out = o.get("out") or next(iter(spec["outputs"]))
    if out not in spec["outputs"]:
        raise ValueError(f"У индикатора {spec['title']} нет линии «{out}»")
    res: Dict[str, Any] = {"ind": o["ind"], "out": out}
    for key, label, default, lo, hi, is_int in spec["params"]:
        try:
            v = float(o.get(key, default))
        except (TypeError, ValueError):
            raise ValueError(f"{spec['title']}: «{label}» должно быть числом")
        v = int(v) if is_int else v
        if not lo <= v <= hi:
            raise ValueError(f"{spec['title']}: «{label}» должно быть от {_num(lo)} до {_num(hi)}")
        res[key] = v
    if res["ind"] == "macd" and res["fast"] >= res["slow"]:
        raise ValueError("MACD: быстрая EMA должна быть короче медленной")
    return res


def normalize(raw: Any) -> Dict[str, List[Dict[str, Any]]]:
    """Проверяет наборы условий и возвращает очищенную копию. Ошибки — ValueError."""
    raw = raw if isinstance(raw, dict) else {}
    out: Dict[str, List[Dict[str, Any]]] = {}
    for name in SETS:
        rows = raw.get(name) or []
        if not isinstance(rows, list):
            raise ValueError("Условия заданы неверно")
        if len(rows) > MAX_ROWS:
            raise ValueError(f"В одном наборе не больше {MAX_ROWS} условий")
        clean = []
        for c in rows:
            if not isinstance(c, dict) or c.get("op") not in OPS:
                raise ValueError("Неизвестное условие: выберите «выше», «ниже» или пересечение")
            clean.append({"left": _operand(c.get("left"), False), "op": c["op"],
                          "right": _operand(c.get("right"), True)})
        out[name] = clean
    return out


# ---------- расчёт ----------

def _key(o: Dict[str, Any]) -> str:
    params = ",".join(f"{k}={o[k]}" for k, *_ in CATALOG[o["ind"]]["params"])
    return f"{o['ind']}.{o['out']}({params})"


def operands(rules: Dict[str, list], sets=SETS) -> List[Dict[str, Any]]:
    return [o for name in sets for c in rules.get(name, []) for o in (c["left"], c["right"])
            if "value" not in o]


def _calc(o: Dict[str, Any], cols: Dict[str, List[float]]) -> Dict[str, list]:
    ind, close = o["ind"], cols["close"]
    if ind == "price":
        return {"close": close}
    if ind == "rsi":
        return {"value": ta.rsi(close, o["period"])}
    if ind == "ema":
        return {"value": ta.ema(close, o["period"])}
    if ind == "sma":
        return {"value": ta.sma(close, o["period"])}
    if ind == "bb":
        up, mid, low = ta.bollinger(close, o["period"], o["mult"])
        return {"upper": up, "middle": mid, "lower": low}
    if ind == "macd":
        line, sig, hist = ta.macd(close, o["fast"], o["slow"], o["signal"])
        return {"macd": line, "signal": sig, "hist": hist}
    if ind == "stoch":
        k, d = ta.stochastic(cols["high"], cols["low"], close, o["k"], o["d"], o["smooth"])
        return {"k": k, "d": d}
    if ind == "change":
        return {"value": ta.change(close, o["n"])}
    if ind == "volume":
        return {"value": cols["volume"]}
    return {"value": ta.volume_ratio(cols["volume"], o["n"])}


def compute(ohlcv: List[list], rules: Dict[str, list]) -> Dict[str, list]:
    """Значения всех индикаторов из условий: ключ операнда → ряд по свечам."""
    cols = {name: [float(c[i]) for c in ohlcv]
            for i, name in ((1, "open"), (2, "high"), (3, "low"), (4, "close"), (5, "volume"))}
    series: Dict[str, list] = {}
    done: Dict[str, Dict[str, list]] = {}  # индикатор с параметрами → все его линии
    for o in operands(rules):
        k = _key(o)
        if k in series:
            continue
        inst = k.split(".", 1)[0] + k[k.index("("):]
        if inst not in done:
            done[inst] = _calc(o, cols)
        series[k] = done[inst][o["out"]]
    return series


def warmup(rules: Dict[str, list]) -> int:
    """Сколько свечей нужно до первого сигнала, чтобы индикаторы «разогрелись»."""
    need = 1
    for o in operands(rules):
        ind = o["ind"]
        n = {"rsi": lambda: 10 * o["period"], "ema": lambda: 3 * o["period"],
             "sma": lambda: o["period"], "bb": lambda: o["period"],
             "macd": lambda: 3 * o["slow"] + 3 * o["signal"],
             "stoch": lambda: o["k"] + o["d"] + o["smooth"],
             "change": lambda: o["n"], "vol_ratio": lambda: o["n"]}.get(ind, lambda: 0)()
        need = max(need, n + 1)  # +1 — предыдущая свеча для пересечений
    return min(need, 1000)


def _val(series: Dict[str, list], o: Dict[str, Any], i: int) -> Optional[float]:
    return o["value"] if "value" in o else series[_key(o)][i]


def check(series: Dict[str, list], c: Dict[str, Any], i: int) -> bool:
    a, b = _val(series, c["left"], i), _val(series, c["right"], i)
    if a is None or b is None:
        return False
    if c["op"] == "above":
        return a > b
    if c["op"] == "below":
        return a < b
    if i < 1:
        return False
    a0, b0 = _val(series, c["left"], i - 1), _val(series, c["right"], i - 1)
    if a0 is None or b0 is None:
        return False
    if c["op"] == "cross_up":
        return a0 <= b0 and a > b
    return a0 >= b0 and a < b


def check_set(series: Dict[str, list], conds: List[Dict[str, Any]], i: int) -> bool:
    return bool(conds) and all(check(series, c, i) for c in conds)


# ---------- описание словами ----------

def _num(v: float) -> str:
    a = abs(v)
    if a >= 1000:
        s = f"{v:,.0f}".replace(",", " ")
    elif a >= 10:
        s = f"{v:.1f}"
    elif a >= 1:
        s = f"{v:.2f}"
    else:
        s = f"{v:.6f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s.replace(".", ",").replace("-", "\u2212")


def label(o: Dict[str, Any], case: str = "label") -> str:
    """«RSI(14)», «нижняя полоса Боллинджера(20, 2)», «30». case: label | gen | acc."""
    if "value" in o:
        return _num(o["value"])
    spec = CATALOG[o["ind"]]
    return spec["outputs"][o["out"]][case].format(**{k: _num(o[k]) for k, *_ in spec["params"]})


def _case(op: str) -> str:
    return "gen" if op in ("above", "below") else "acc"  # «выше чего», «пересекает что»


def text(conds: List[Dict[str, Any]]) -> str:
    """Набор условий словами: «RSI(14) ниже 30 и цена выше EMA(200)»."""
    return " и ".join(f"{label(c['left'])} {OPS[c['op']]} {label(c['right'], _case(c['op']))}"
                      for c in conds)


def explain(series: Dict[str, list], conds: List[Dict[str, Any]], i: int) -> str:
    """Почему набор сработал на свече i — со значениями индикаторов."""
    parts = []
    for c in conds:
        left, right = c["left"], c["right"]
        a = _val(series, left, i)
        r = label(right, _case(c["op"]))
        if "value" not in right:
            r += f" {_num(_val(series, right, i))}"
        if c["op"] in ("above", "below"):
            parts.append(f"{label(left)} {_num(a)} {OPS[c['op']]} {r}")
        else:
            parts.append(f"{label(left)} {OPS[c['op']]} {r}")
    return " и ".join(parts)


def snapshot(series: Dict[str, list], rules: Dict[str, list], sets, i: int) -> Dict[str, float]:
    """Текущие значения индикаторов из условий — для карточки бота."""
    out: Dict[str, float] = {}
    for o in operands(rules, sets):
        if o["ind"] != "price":
            v = series[_key(o)][i]
            if v is not None:
                out[label(o)] = round(v, 6)
    return out


def chart_series(rules: Dict[str, list], series: Dict[str, list], sets, start: int):
    """Линии индикаторов и пороги для графика, начиная со свечи start."""
    lines, seen, thresholds = [], set(), []

    def pane(o):
        spec = CATALOG[o["ind"]]
        return spec["pane"] if spec["pane"] == "main" else \
            spec["pane"].format(**{k: _num(o[k]) for k, *_ in spec["params"]})

    for name in sets:
        for c in rules.get(name, []):
            for o in (c["left"], c["right"]):
                if "value" in o or o["ind"] == "price" or _key(o) in seen:
                    continue
                seen.add(_key(o))
                style = "volume" if o["ind"] == "volume" else "hist" if o["out"] == "hist" else "line"
                lines.append({"label": label(o), "pane": pane(o), "style": style,
                              "values": series[_key(o)][start:]})
            if "value" in c["right"]:
                t = {"pane": pane(c["left"]), "value": c["right"]["value"]}
                if t not in thresholds:
                    thresholds.append(t)
    return lines, thresholds


# ---------- старые боты ----------

def from_legacy(p: Dict[str, Any]) -> Dict[str, list]:
    """Сигналы старого формата (rsi / ema_cross) в виде наборов условий."""
    empty = {name: [] for name in SETS}
    if p.get("signal") == "rsi":
        r = {"ind": "rsi", "out": "value", "period": int(p.get("rsi_period", 14))}
        return {**empty,
                "long": [{"left": r, "op": "below", "right": {"value": float(p.get("rsi_low", 30))}}],
                "short": [{"left": r, "op": "above", "right": {"value": float(p.get("rsi_high", 70))}}]}
    if p.get("signal") == "ema_cross":
        f = {"ind": "ema", "out": "value", "period": int(p.get("ema_fast", 9))}
        s = {"ind": "ema", "out": "value", "period": int(p.get("ema_slow", 21))}
        return {**empty, "long": [{"left": f, "op": "cross_up", "right": s}],
                "short": [{"left": f, "op": "cross_down", "right": s}]}
    return empty
