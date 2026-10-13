"""Pine Script (TradingView) прямо в боте — без перевода на Python.

Код с TradingView разбирается библиотекой pynescript и выполняется здесь свеча за свечой,
как в TradingView: у каждой переменной есть история (x[1]), var сохраняет значение между
свечами, у каждого вызова ta.* своё состояние.

Что становится сигналом:
- strategy(...) — strategy.entry: вход; strategy.exit (stop/limit/loss/profit): стоп и тейк;
  strategy.close / close_all: выход. Позиция внутри скрипта ведётся так же, как в TradingView
  (вход по открытию следующей свечи, стоп и тейк — внутри свечи), поэтому
  strategy.position_size и strategy.position_avg_price работают.
- indicator(...) — alertcondition: условие с «buy/long/покупка» в названии — лонг,
  с «sell/short/продажа» — шорт. Выход тогда — по тейку и стопу из настроек бота.
- input.int / float / bool — настройки сигнала в редакторе бота.

Чего нет: рисование (plot, label, line, box, table — пропускается), данные другого таймфрейма
и символа (request.security на другой таймфрейм), свои типы, map, matrix, библиотеки,
лимитные и стоп-входы, трейлинг-стоп. О таком программа скажет с номером строки.
"""
import math
import re
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional, Tuple

from pynescript import ast as P

PINE_RE = re.compile(r"^\s*//\s*@version\s*=|^\s*(strategy|indicator|study)\s*\(\s*['\"]", re.M)
TF_NAMES = {60_000: "1", 180_000: "3", 300_000: "5", 900_000: "15", 1_800_000: "30", 3_600_000: "60",
            7_200_000: "120", 14_400_000: "240", 86_400_000: "D", 604_800_000: "W"}
# пространства имён «для рисования и оформления»: их константы и функции ничего не меняют в сигнале
VISUAL = {"color", "shape", "location", "size", "plot", "line", "label", "box", "table", "linefill",
          "polyline", "extend", "xloc", "yloc", "text", "font", "display", "format", "position",
          "hline", "alert", "order", "scale", "currency", "barmerge", "adjustment", "session",
          "splits", "dividends", "earnings", "chart", "log", "dayofweek_names"}
NOOP = {"plot", "plotshape", "plotchar", "plotarrow", "plotcandle", "plotbar", "hline", "fill",
        "bgcolor", "barcolor", "alert", "max_bars_back", "strategy", "indicator", "study", "library"}


def is_pine(code: str) -> bool:
    return bool(PINE_RE.search(code))


class PineError(ValueError):
    pass


def _err(node, msg: str) -> PineError:
    line = getattr(node, "lineno", None)
    return PineError((f"строка {line}: " if line else "") + msg)


class _Break(Exception):
    pass


class _Continue(Exception):
    pass


def dotted(node) -> Optional[str]:
    if isinstance(node, P.Name):
        return node.id
    if isinstance(node, P.Attribute):
        base = dotted(node.value)
        return f"{base}.{node.attr}" if base else None
    if isinstance(node, P.Specialize):
        return dotted(node.value)
    return None


def _walk(node):
    yield node
    for name in getattr(node, "_fields", ()):
        v = getattr(node, name, None)
        if isinstance(v, list):
            for x in v:
                if isinstance(x, P.AST):
                    yield from _walk(x)
        elif isinstance(v, P.AST):
            yield from _walk(v)


def _literal(node):
    if isinstance(node, P.Constant):
        return node.value
    if isinstance(node, P.UnaryOp) and isinstance(node.op, (P.USub, P.UAdd)) and isinstance(node.operand, P.Constant):
        v = node.operand.value
        return -v if isinstance(node.op, P.USub) else v
    return None


# ---------- разбор: что за скрипт, какие у него настройки ----------

class Compiled:
    """Разобранный скрипт: тело, функции, настройки (input.*), название."""

    def __init__(self, code: str):
        try:
            tree = P.parse(code)
        except Exception as e:  # pynescript бросает разные ошибки разбора
            raise PineError(f"Не удалось разобрать Pine Script: {str(e).strip()[:300]}")
        self.body = tree.body
        self.funcs: Dict[str, P.FunctionDef] = {}
        self.kind = None
        self.title = None
        self.inputs: Dict[int, dict] = {}  # id(узла input.*) → описание
        self.params: Dict[str, float] = {}
        self.labels: Dict[str, str] = {}
        self.max_len = 1
        problems = []
        for st in self.body:
            if isinstance(st, P.FunctionDef):
                self.funcs[st.name] = st
        assigned: Dict[int, str] = {}
        for node in _walk(tree):
            if isinstance(node, (P.Assign, P.ReAssign)) and isinstance(node.target, P.Name) \
                    and isinstance(node.value, P.Call):
                assigned[id(node.value)] = node.target.id
            if isinstance(node, (P.TypeDef, P.EnumDef)):
                problems.append(_err(node, "свои типы (type, enum) не поддерживаются"))
            elif isinstance(node, P.Import):
                problems.append(_err(node, "библиотеки (import) не поддерживаются"))
        for node in _walk(tree):
            if not isinstance(node, P.Call):
                continue
            fn = dotted(node.func) or ""
            if fn in ("strategy", "indicator", "study") and self.kind is None:
                self.kind = "strategy" if fn == "strategy" else "indicator"
                t = _arg(node, 0, "title")
                self.title = _literal(t) if t is not None else None
            elif fn.startswith("request.") and fn != "request.security":
                problems.append(_err(node, f"{fn} не поддерживается: в боте есть только свечи этой пары и таймфрейма"))
            elif fn.split(".")[0] in ("map", "matrix"):
                problems.append(_err(node, f"{fn.split('.')[0]} не поддерживается"))
            elif fn == "input" or fn.startswith("input."):
                self._input(node, fn, assigned.get(id(node)))
            elif fn.startswith("ta.") or fn.startswith("math."):
                for a in node.args:
                    v = _literal(a.value)
                    if isinstance(v, int) and not isinstance(v, bool) and v < 2000:
                        self.max_len = max(self.max_len, v)
        if self.kind is None:
            problems.append(PineError("Нет строки strategy(\"…\") или indicator(\"…\") — это точно скрипт TradingView?"))
        if problems:
            raise PineError("; ".join(str(p) for p in problems[:5]))
        if self.kind == "indicator" and not any(isinstance(n, P.Call) and dotted(n.func) == "alertcondition"
                                                for n in _walk(tree)):
            raise PineError("Это индикатор без alertcondition: в нём нет сигналов на вход. Нужна стратегия "
                            "(strategy.entry) или индикатор с alertcondition(…, \"Buy\"/\"Sell\")")

    def _input(self, node, fn: str, var_name: Optional[str]):
        kind = fn.split(".")[1] if "." in fn else None
        default_node = _arg(node, 0, "defval")
        default = _literal(default_node) if default_node is not None else None
        if kind is None:  # Pine v4: input(14, …) — тип по значению
            kind = "bool" if isinstance(default, bool) else "int" if isinstance(default, int) else \
                "float" if isinstance(default, float) else "source" if isinstance(default_node, P.Name) else "string"
        title_node = _arg(node, 1, "title")
        title = _literal(title_node) if title_node is not None else None
        key = var_name or f"input{len(self.inputs) + 1}"
        info = {"kind": kind, "default": default, "default_node": default_node, "key": None}
        if kind in ("int", "float", "bool", "price") and isinstance(default, (int, float)):
            key = re.sub(r"\W", "_", key)
            while key in self.params:
                key += "_"
            info["key"] = key
            self.params[key] = int(default) if isinstance(default, bool) else default
            self.labels[key] = (str(title) if title else key) + (" (1 — да, 0 — нет)" if kind == "bool" else "")
            if kind == "int" and not isinstance(default, bool) and default < 2000:
                self.max_len = max(self.max_len, int(default))
        self.inputs[id(node)] = info


def _arg(call, pos: int, name: str):
    for a in call.args:
        if a.name == name:
            return a.value
    positional = [a for a in call.args if a.name is None]
    return positional[pos].value if pos < len(positional) else None


# ---------- значения ----------

class Var:
    """Переменная с историей: vals[b] — значение на конец свечи b."""
    __slots__ = ("vals", "persist", "inited")

    def __init__(self, persist: bool):
        self.vals: List[Any] = []
        self.persist = persist
        self.inited = False

    def fill(self, i: int):
        v = self.vals
        while len(v) <= i:
            v.append(v[-1] if self.persist and v else None)

    def get(self, i: int):
        if i < 0:
            return None
        self.fill(i)
        return self.vals[i]

    def set(self, i: int, value):
        self.fill(i)
        self.vals[i] = value


def _num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _truthy(x) -> bool:
    if x is None:
        return False
    if isinstance(x, float) and math.isnan(x):
        return False
    return bool(x)


def _binop(op, a, b, node):
    if isinstance(op, P.Add):
        if isinstance(a, str) or isinstance(b, str):
            return ("" if a is None else str(a)) + ("" if b is None else str(b))
        return None if a is None or b is None else a + b
    if a is None or b is None:
        return None
    try:
        if isinstance(op, P.Sub):
            return a - b
        if isinstance(op, P.Mult):
            return a * b
        if isinstance(op, P.Div):
            if b == 0:
                return None
            r = a / b
            return int(r) if isinstance(a, int) and isinstance(b, int) and not isinstance(a, bool) and r.is_integer() else r
        if isinstance(op, P.Mod):
            return None if b == 0 else math.fmod(a, b) if isinstance(a, float) or isinstance(b, float) else a % b
    except TypeError:
        raise _err(node, f"нельзя выполнить действие над {a!r} и {b!r}")
    raise _err(node, f"неизвестное действие {type(op).__name__}")


def _compare(op, a, b) -> bool:
    if isinstance(op, (P.Eq, P.NotEq)):
        eq = a == b
        return eq if isinstance(op, P.Eq) else not eq
    if a is None or b is None:
        return False
    if isinstance(op, P.Lt):
        return a < b
    if isinstance(op, P.LtE):
        return a <= b
    if isinstance(op, P.Gt):
        return a > b
    return a >= b


# ---------- встроенные функции ----------

BUILTINS: Dict[str, Tuple[Callable, List[str], bool]] = {}


def bi(names, params, state=False):
    def deco(f):
        for n in names.split():
            BUILTINS[n] = (f, params, state)
        return f
    return deco


def _ma_seed(st, src, length, alpha):
    """EMA/RMA как в TradingView: старт — простая средняя первых length значений."""
    if src is None:
        return None
    if st.get("v") is None:
        seed = st.setdefault("seed", [])
        seed.append(src)
        if len(seed) < length:
            return None
        st["v"] = sum(seed[-length:]) / length
        return st["v"]
    st["v"] = alpha * src + (1 - alpha) * st["v"]
    return st["v"]


def _window(st, key, value, length):
    w = st.get(key)
    if w is None or w.maxlen != length:
        w = st[key] = deque(w or (), maxlen=length)
    w.append(value)
    return w


@bi("ta.sma", ["source", "length"], True)
def _sma(rt, st, src, length):
    w = _window(st, "w", src, int(length))
    return None if len(w) < w.maxlen or None in w else sum(w) / w.maxlen


@bi("ta.ema", ["source", "length"], True)
def _ema(rt, st, src, length):
    return _ma_seed(st, src, int(length), 2 / (int(length) + 1))


@bi("ta.rma", ["source", "length"], True)
def _rma(rt, st, src, length):
    return _ma_seed(st, src, int(length), 1 / int(length))


@bi("ta.wma", ["source", "length"], True)
def _wma(rt, st, src, length):
    n = int(length)
    w = _window(st, "w", src, n)
    if len(w) < n or None in w:
        return None
    return sum(v * (k + 1) for k, v in enumerate(w)) / (n * (n + 1) / 2)


@bi("ta.hma", ["source", "length"], True)
def _hma(rt, st, src, length):
    n = int(length)
    a = _wma(rt, st.setdefault("a", {}), src, max(1, n // 2))
    b = _wma(rt, st.setdefault("b", {}), src, n)
    raw = None if a is None or b is None else 2 * a - b
    return _wma(rt, st.setdefault("c", {}), raw, max(1, int(math.sqrt(n))))


@bi("ta.vwma", ["source", "length"], True)
def _vwma(rt, st, src, length):
    v = rt.v[rt.i]
    a = _sma(rt, st.setdefault("a", {}), None if src is None else src * v, length)
    b = _sma(rt, st.setdefault("b", {}), v, length)
    return None if a is None or not b else a / b


def _tr(rt, i, handle_na=True):
    if i == 0:
        return rt.h[0] - rt.l[0] if handle_na else None
    pc = rt.c[i - 1]
    return max(rt.h[i] - rt.l[i], abs(rt.h[i] - pc), abs(rt.l[i] - pc))


@bi("ta.tr", ["handle_na"], False)
def _tr_fn(rt, handle_na=False):
    return _tr(rt, rt.i, bool(handle_na))


@bi("ta.atr", ["length"], True)
def _atr(rt, st, length):
    return _ma_seed(st, _tr(rt, rt.i, True), int(length), 1 / int(length))


@bi("ta.rsi", ["source", "length"], True)
def _rsi(rt, st, src, length):
    prev = st.get("p")
    st["p"] = src
    if prev is None or src is None:
        return None
    n = int(length)
    up = _ma_seed(st.setdefault("u", {}), max(src - prev, 0), n, 1 / n)
    dn = _ma_seed(st.setdefault("d", {}), max(prev - src, 0), n, 1 / n)
    if up is None or dn is None:
        return None
    return 100.0 if dn == 0 else 100 - 100 / (1 + up / dn)


@bi("ta.stdev", ["source", "length", "biased"], True)
def _stdev(rt, st, src, length, biased=True):
    w = _window(st, "w", src, int(length))
    if len(w) < w.maxlen or None in w:
        return None
    m = sum(w) / len(w)
    return math.sqrt(sum((x - m) ** 2 for x in w) / (len(w) if biased in (None, True) else len(w) - 1))


def _hl(rt, st, args, high: bool, bars=False):
    if len(args) == 1:
        src, length = (rt.h if high else rt.l)[rt.i], args[0]
    else:
        src, length = args[0], args[1]
    w = _window(st, "w", src, int(length))
    if len(w) < w.maxlen or None in w:
        return None
    best = max(w) if high else min(w)
    if not bars:
        return best
    idx = len(w) - 1 - (max if high else min)(range(len(w)), key=lambda k: (w[k], k if high else -k))
    return -idx


@bi("ta.highest", ["source", "length"], True)
def _highest(rt, st, *args):
    return _hl(rt, st, [a for a in args if a is not None], True)


@bi("ta.lowest", ["source", "length"], True)
def _lowest(rt, st, *args):
    return _hl(rt, st, [a for a in args if a is not None], False)


@bi("ta.highestbars", ["source", "length"], True)
def _highestbars(rt, st, *args):
    return _hl(rt, st, [a for a in args if a is not None], True, bars=True)


@bi("ta.lowestbars", ["source", "length"], True)
def _lowestbars(rt, st, *args):
    return _hl(rt, st, [a for a in args if a is not None], False, bars=True)


def _cross(st, a, b):
    pa, pb = st.get("a"), st.get("b")
    st["a"], st["b"] = a, b
    return None in (a, b, pa, pb), pa, pb


@bi("ta.crossover", ["source1", "source2"], True)
def _crossover(rt, st, a, b):
    na, pa, pb = _cross(st, a, b)
    return False if na else a > b and pa <= pb


@bi("ta.crossunder", ["source1", "source2"], True)
def _crossunder(rt, st, a, b):
    na, pa, pb = _cross(st, a, b)
    return False if na else a < b and pa >= pb


@bi("ta.cross", ["source1", "source2"], True)
def _crossany(rt, st, a, b):
    na, pa, pb = _cross(st, a, b)
    return False if na else (a > b and pa <= pb) or (a < b and pa >= pb)


def _back(st, src, length):
    w = _window(st, "h", src, int(length) + 1)
    return None if len(w) < w.maxlen else w[0]


@bi("ta.change", ["source", "length"], True)
def _change(rt, st, src, length=1):
    old = _back(st, src, length or 1)
    if isinstance(src, bool):
        return old is not None and src != old
    return None if old is None or src is None else src - old


@bi("ta.mom", ["source", "length"], True)
def _mom(rt, st, src, length):
    old = _back(st, src, length)
    return None if old is None or src is None else src - old


@bi("ta.roc", ["source", "length"], True)
def _roc(rt, st, src, length):
    old = _back(st, src, length)
    return None if not old or src is None else 100 * (src - old) / old


@bi("ta.rising", ["source", "length"], True)
def _rising(rt, st, src, length):
    w = _window(st, "h", src, int(length) + 1)
    return len(w) == w.maxlen and None not in w and all(src > x for x in list(w)[:-1])


@bi("ta.falling", ["source", "length"], True)
def _falling(rt, st, src, length):
    w = _window(st, "h", src, int(length) + 1)
    return len(w) == w.maxlen and None not in w and all(src < x for x in list(w)[:-1])


@bi("ta.macd", ["source", "fastlen", "slowlen", "siglen"], True)
def _macd(rt, st, src, fast, slow, sig):
    f = _ema(rt, st.setdefault("f", {}), src, fast)
    s = _ema(rt, st.setdefault("s", {}), src, slow)
    line = None if f is None or s is None else f - s
    signal = _ema(rt, st.setdefault("g", {}), line, sig)
    return [line, signal, None if line is None or signal is None else line - signal]


@bi("ta.bb", ["series", "length", "mult"], True)
def _bb(rt, st, src, length, mult):
    basis = _sma(rt, st.setdefault("m", {}), src, length)
    dev = _stdev(rt, st.setdefault("d", {}), src, length)
    if basis is None or dev is None:
        return [None, None, None]
    return [basis, basis + mult * dev, basis - mult * dev]


@bi("ta.bbw", ["series", "length", "mult"], True)
def _bbw(rt, st, src, length, mult):
    b, u, l = _bb(rt, st, src, length, mult)
    return None if not b else (u - l) / b * 100


@bi("ta.stoch", ["source", "high", "low", "length"], True)
def _stoch(rt, st, src, high, low, length):
    hh = _hl(rt, st.setdefault("h", {}), [high, length], True)
    ll = _hl(rt, st.setdefault("l", {}), [low, length], False)
    if hh is None or ll is None or src is None:
        return None
    return 50.0 if hh == ll else 100 * (src - ll) / (hh - ll)


@bi("ta.cci", ["source", "length"], True)
def _cci(rt, st, src, length):
    w = _window(st, "w", src, int(length))
    if len(w) < w.maxlen or None in w:
        return None
    m = sum(w) / len(w)
    dev = sum(abs(x - m) for x in w) / len(w)
    return 0.0 if dev == 0 else (src - m) / (0.015 * dev)


@bi("ta.vwap", ["source", "anchor", "stdev_mult"], True)
def _vwap(rt, st, src=None, anchor=None, stdev_mult=None):
    src = rt.hlc3(rt.i) if src is None else src
    day = rt.t[rt.i] // 86_400_000
    if st.get("day") != day or (anchor is True):
        st.update(day=day, pv=0.0, vol=0.0)
    if src is None:
        return None
    st["pv"] += src * rt.v[rt.i]
    st["vol"] += rt.v[rt.i]
    return st["pv"] / st["vol"] if st["vol"] else src


def _pivot(rt, st, args, high: bool):
    args = [a for a in args if a is not None]
    if len(args) == 2:
        src, left, right = (rt.h if high else rt.l)[rt.i], args[0], args[1]
    else:
        src, left, right = args
    left, right = int(left), int(right)
    w = _window(st, "w", src, left + right + 1)
    if len(w) < w.maxlen or None in w:
        return None
    c = w[left]
    others = [w[k] for k in range(len(w)) if k != left]
    return c if all((c > x) if high else (c < x) for x in others) else None


@bi("ta.pivothigh", ["source", "leftbars", "rightbars"], True)
def _pivothigh(rt, st, *args):
    return _pivot(rt, st, args, True)


@bi("ta.pivotlow", ["source", "leftbars", "rightbars"], True)
def _pivotlow(rt, st, *args):
    return _pivot(rt, st, args, False)


@bi("ta.valuewhen", ["condition", "source", "occurrence"], True)
def _valuewhen(rt, st, cond, src, occurrence=0):
    vals = st.setdefault("v", [])
    if _truthy(cond):
        vals.append(src)
        del vals[:-100]
    k = int(occurrence or 0)
    return vals[-1 - k] if len(vals) > k else None


@bi("ta.barssince", ["condition"], True)
def _barssince(rt, st, cond):
    if _truthy(cond):
        st["last"] = rt.i
    return None if st.get("last") is None else rt.i - st["last"]


@bi("ta.cum", ["source"], True)
def _cum(rt, st, src):
    st["s"] = st.get("s", 0.0) + (src or 0.0)
    return st["s"]


@bi("ta.dmi", ["diLength", "adxSmoothing"], True)
def _dmi(rt, st, di_len, adx_len):
    i = rt.i
    if i == 0:
        return [None, None, None]
    up, down = rt.h[i] - rt.h[i - 1], rt.l[i - 1] - rt.l[i]
    pdm = up if up > down and up > 0 else 0.0
    mdm = down if down > up and down > 0 else 0.0
    n = int(di_len)
    tr = _ma_seed(st.setdefault("t", {}), _tr(rt, i), n, 1 / n)
    p = _ma_seed(st.setdefault("p", {}), pdm, n, 1 / n)
    m = _ma_seed(st.setdefault("m", {}), mdm, n, 1 / n)
    if not tr or p is None or m is None:
        return [None, None, None]
    pdi, mdi = 100 * p / tr, 100 * m / tr
    s = pdi + mdi
    k = int(adx_len)
    adx = _ma_seed(st.setdefault("a", {}), 100 * abs(pdi - mdi) / s if s else 0.0, k, 1 / k)
    return [pdi, mdi, adx]


@bi("ta.supertrend", ["factor", "atrPeriod"], True)
def _supertrend(rt, st, factor, period):
    i = rt.i
    atr = _atr(rt, st.setdefault("atr", {}), period)
    src = (rt.h[i] + rt.l[i]) / 2
    if atr is None:
        st["prev_atr"] = None
        return [None, None]
    up, lo = src + factor * atr, src - factor * atr
    pu, pl = st.get("u") or 0.0, st.get("l") or 0.0
    pc = rt.c[i - 1] if i else rt.c[i]
    lo = lo if lo > pl or pc < pl else pl
    up = up if up < pu or pc > pu else pu
    if st.get("prev_atr") is None:
        d = 1
    elif st.get("st") == pu:
        d = -1 if rt.c[i] > up else 1
    else:
        d = 1 if rt.c[i] < lo else -1
    stv = lo if d == -1 else up
    st.update(u=up, l=lo, st=stv, prev_atr=atr)
    return [stv, d]


@bi("ta.linreg", ["source", "length", "offset"], True)
def _linreg(rt, st, src, length, offset=0):
    n = int(length)
    w = _window(st, "w", src, n)
    if len(w) < n or None in w:
        return None
    xs = range(n)
    mx, my = (n - 1) / 2, sum(w) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, w)) / sxx if sxx else 0.0
    return my + slope * (n - 1 - int(offset or 0) - mx)


@bi("math.sum", ["source", "length"], True)
def _msum(rt, st, src, length):
    w = _window(st, "w", src, int(length))
    return None if len(w) < w.maxlen or None in w else sum(w)


@bi("fixnan", ["source"], True)
def _fixnan(rt, st, src):
    if src is not None:
        st["v"] = src
    return st.get("v")


@bi("na", ["x"])
def _na(rt, x=None):
    return x is None or (isinstance(x, float) and math.isnan(x))


@bi("nz", ["source", "replacement"])
def _nz(rt, x, rep=0):
    return (0 if rep is None else rep) if _na(rt, x) else x


def _nums(args):
    return [a for a in args if a is not None]


BUILTINS.update({
    "math.abs": (lambda rt, x: None if x is None else abs(x), ["number"], False),
    "math.max": (lambda rt, *a: max(_nums(a)) if _nums(a) and len(_nums(a)) == len(a) else None, ["a", "b"], False),
    "math.min": (lambda rt, *a: min(_nums(a)) if _nums(a) and len(_nums(a)) == len(a) else None, ["a", "b"], False),
    "math.avg": (lambda rt, *a: sum(a) / len(a) if a and None not in a else None, ["a", "b"], False),
    "math.round": (lambda rt, x, p=None: None if x is None else (round(x, int(p)) if p else int(math.floor(x + 0.5))), ["number", "precision"], False),
    "math.floor": (lambda rt, x: None if x is None else math.floor(x), ["number"], False),
    "math.ceil": (lambda rt, x: None if x is None else math.ceil(x), ["number"], False),
    "math.sqrt": (lambda rt, x: None if x is None or x < 0 else math.sqrt(x), ["number"], False),
    "math.pow": (lambda rt, x, y: None if x is None or y is None else x ** y, ["base", "exponent"], False),
    "math.log": (lambda rt, x: None if not x or x <= 0 else math.log(x), ["number"], False),
    "math.log10": (lambda rt, x: None if not x or x <= 0 else math.log10(x), ["number"], False),
    "math.exp": (lambda rt, x: None if x is None else math.exp(x), ["number"], False),
    "math.sign": (lambda rt, x: None if x is None else (x > 0) - (x < 0), ["number"], False),
    "math.round_to_mintick": (lambda rt, x: None if x is None else round(x / rt.mintick) * rt.mintick, ["number"], False),
    "math.todegrees": (lambda rt, x: None if x is None else math.degrees(x), ["radians"], False),
    "math.toradians": (lambda rt, x: None if x is None else math.radians(x), ["degrees"], False),
    "int": (lambda rt, x: None if x is None else int(x), ["x"], False),
    "float": (lambda rt, x: None if x is None else float(x), ["x"], False),
    "bool": (lambda rt, x: _truthy(x), ["x"], False),
    "str.tostring": (lambda rt, x, f=None: "NaN" if x is None else str(x), ["value", "format"], False),
    "str.format": (lambda rt, f, *a: str(f), ["formatString"], False),
    "str.contains": (lambda rt, s, sub: sub in s if s and sub else False, ["source", "str"], False),
    "str.length": (lambda rt, s: len(s or ""), ["string"], False),
})


def _time_part(attr):
    def f(rt, t=None, tz=None):
        ms = rt.t[rt.i] if t is None else t
        d = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
        return {"year": d.year, "month": d.month, "dayofmonth": d.day, "hour": d.hour, "minute": d.minute,
                "second": d.second, "weekofyear": d.isocalendar()[1],
                "dayofweek": (d.weekday() + 1) % 7 + 1}[attr]
    return f


for _a in ("year", "month", "dayofmonth", "hour", "minute", "second", "weekofyear", "dayofweek"):
    BUILTINS[_a] = (_time_part(_a), ["time", "timezone"], False)


def _timestamp(rt, *a):
    a = [x for x in a if x is not None]
    if a and isinstance(a[0], str) and len(a) == 1:
        try:
            return int(datetime.fromisoformat(a[0].replace("Z", "+00:00")).replace(tzinfo=timezone.utc).timestamp() * 1000)
        except ValueError:
            return None
    if a and isinstance(a[0], str):
        a = a[1:]  # часовой пояс — у Binance всё в UTC
    y, mo, d, h, mi, s = (list(map(int, a)) + [0, 0, 0])[:6] if len(a) >= 3 else (1970, 1, 1, 0, 0, 0)
    return int(datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc).timestamp() * 1000)


BUILTINS["timestamp"] = (_timestamp, ["year", "month", "day", "hour", "minute", "second"], False)


def _array(name):
    def f(rt, *a):
        arr = a[0] if a else None
        if name.startswith("new") or name == "from":
            if name == "from":
                return list(a)
            size = int(a[0] or 0) if a and a[0] is not None else 0
            return [a[1] if len(a) > 1 else None] * size
        if arr is None:
            return None
        if name == "push":
            arr.append(a[1])
        elif name == "unshift":
            arr.insert(0, a[1])
        elif name == "pop":
            return arr.pop() if arr else None
        elif name == "shift":
            return arr.pop(0) if arr else None
        elif name == "get":
            k = int(a[1])
            return arr[k] if -len(arr) <= k < len(arr) else None
        elif name == "set":
            arr[int(a[1])] = a[2]
        elif name == "size":
            return len(arr)
        elif name == "clear":
            arr.clear()
        elif name == "insert":
            arr.insert(int(a[1]), a[2])
        elif name == "remove":
            return arr.pop(int(a[1]))
        elif name in ("sum", "avg", "min", "max"):
            xs = _nums(arr)
            if not xs:
                return None
            return {"sum": sum, "min": min, "max": max, "avg": lambda v: sum(v) / len(v)}[name](xs)
        elif name == "includes":
            return a[1] in arr
        elif name == "indexof":
            return arr.index(a[1]) if a[1] in arr else -1
        elif name == "first":
            return arr[0] if arr else None
        elif name == "last":
            return arr[-1] if arr else None
        elif name == "copy":
            return list(arr)
        elif name == "slice":
            return arr[int(a[1]):int(a[2])]
        elif name == "reverse":
            arr.reverse()
        elif name == "sort":
            arr.sort(key=lambda v: (v is None, v), reverse=len(a) > 1 and str(a[1]).endswith("descending"))
        return None
    return f


ARRAY_FUNCS = "new_float new_int new_bool new_string new from push unshift pop shift get set size clear insert " \
              "remove sum avg min max includes indexof first last copy slice reverse sort".split()
for _a in ARRAY_FUNCS:
    BUILTINS[f"array.{_a}"] = (_array(_a), [], False)


# ---------- исполнение ----------

class Runtime:
    def __init__(self, comp: Compiled, candles, params: Dict[str, float], symbol: str = "BTC/USDT",
                 lenient: bool = False):
        self.s = comp
        self.lenient = lenient  # пробный прогон на случайных свечах: таймфрейм в request.security не проверяем
        self.o = [c.open for c in candles]
        self.h = [c.high for c in candles]
        self.l = [c.low for c in candles]
        self.c = [c.close for c in candles]
        self.v = [c.volume for c in candles]
        self.t = [c.time for c in candles]
        self.n = len(candles)
        diffs = [b - a for a, b in zip(self.t[:20], self.t[1:21]) if b > a]
        self.tf_ms = min(diffs) if diffs else 60_000
        self.tf = TF_NAMES.get(self.tf_ms, str(self.tf_ms // 60_000))
        self.symbol = symbol
        self.mintick = self._mintick()
        self.params = params
        self.i = 0
        self.globals: Dict[str, Var] = {}
        self.frames: Dict[tuple, Dict[str, Var]] = {(): self.globals}
        self.state: Dict[tuple, dict] = {}
        self.hist: Dict[tuple, Var] = {}
        # позиция внутри скрипта — как у strategy в TradingView
        self.pos = 0
        self.avg: Optional[float] = None
        self.entry_id: Optional[str] = None
        self.trades = 0
        self.pending: Optional[dict] = None
        self.exits: Dict[str, dict] = {}
        self.dispatch = {P.Name: self.ev_name, P.Constant: lambda n, fk: n.value, P.BinOp: self.ev_binop,
                         P.UnaryOp: self.ev_unary, P.BoolOp: self.ev_boolop, P.Compare: self.ev_compare,
                         P.Conditional: self.ev_cond, P.Call: self.ev_call, P.Attribute: self.ev_attr,
                         P.Subscript: self.ev_subscript, P.Tuple: lambda n, fk: [self.ev(e, fk) for e in n.elts],
                         P.If: self.ev_if, P.ForTo: self.ev_forto, P.ForIn: self.ev_forin, P.While: self.ev_while,
                         P.Switch: self.ev_switch, P.Qualify: lambda n, fk: self.ev(n.value, fk),
                         P.Specialize: lambda n, fk: self.ev(n.value, fk)}

    def _mintick(self) -> float:
        dec = 0
        for x in self.c[:300]:
            s = repr(float(x))
            if "e" not in s and "." in s:
                dec = max(dec, len(s.split(".")[1].rstrip("0")))
        return 10 ** -min(dec, 10)

    def hlc3(self, i):
        return (self.h[i] + self.l[i] + self.c[i]) / 3

    # ---- главный цикл ----

    def run(self) -> List[Optional[dict]]:
        out: List[Optional[dict]] = [None] * self.n
        for i in range(self.n):
            self.i = i
            self._fill(i)
            self.orders: List[tuple] = []
            self.closes: set = set()
            self.close_all = False
            self.alerts: List[Tuple[str, str]] = []
            self.exec_block(self.s.body, ())
            out[i] = self._result(i)
        return out

    # ---- позиция стратегии ----

    def _levels(self, ref: Optional[float], side: int) -> Dict[str, float]:
        res: Dict[str, float] = {}
        for e in self.exits.values():
            if e["from"] not in ("", None, self.entry_id) and self.pos:
                continue
            stop, take = e["stop"], e["limit"]
            if stop is None and e["loss"] is not None and ref:
                stop = ref - side * e["loss"] * self.mintick
            if take is None and e["profit"] is not None and ref:
                take = ref + side * e["profit"] * self.mintick
            if stop is not None:
                res["stop"] = stop
            if take is not None:
                res["take"] = take
        return res

    def _flat(self):
        self.pos, self.avg, self.entry_id = 0, None, None
        self.exits = {k: e for k, e in self.exits.items() if e["from"] in ("", None)}

    def _fill(self, i: int):
        """Начало свечи i: исполняем заявки прошлой свечи по открытию, потом стоп и тейк внутри свечи."""
        p, self.pending = self.pending, None
        if p:
            if p.get("close") and self.pos:
                self._flat()
            if p.get("entry"):
                eid, side = p["entry"]
                s = 1 if side == "long" else -1
                if self.pos != s:
                    self.pos, self.avg, self.entry_id = s, self.o[i], eid
                    self.trades += 1
        if not self.pos:
            return
        lv = self._levels(self.avg, self.pos)
        o, h, l, c = self.o[i], self.h[i], self.l[i], self.c[i]
        path = (o, l, h, c) if c >= o else (o, h, l, c)
        for px in path:
            st, tk = lv.get("stop"), lv.get("take")
            if (st is not None and (px <= st if self.pos > 0 else px >= st)) or \
                    (tk is not None and (px >= tk if self.pos > 0 else px <= tk)):
                self._flat()
                return

    def _result(self, i: int) -> Optional[dict]:
        r: Dict[str, Any] = {}
        entry = None
        for kind, eid, side, comment in self.orders:
            if kind == "entry":
                entry = (eid, side, comment)
        closing = self.pos and (self.close_all or self.entry_id in self.closes or
                                (entry and (1 if entry[1] == "long" else -1) == -self.pos))
        if self.s.kind == "indicator":
            for side, title in self.alerts:
                r = {"side": side, "why": f"alertcondition «{title}»"}
            return r or None
        if closing:
            r["exit"] = True
            r["why"] = "strategy.close" if not entry else f"разворот: {entry[0]}"
            self.pending = {"close": True}
        if entry and (1 if entry[1] == "long" else -1) != (0 if closing else self.pos):
            eid, side, comment = entry
            s = 1 if side == "long" else -1
            r["side"] = side
            r["why"] = f"strategy.entry «{eid}»" + (f": {comment}" if comment else "")
            self.pending = {**(self.pending or {}), "entry": (eid, side)}
            ref = self.c[i]
            for k, v in self._levels(ref, s).items():  # уровни для входа — только по правильную сторону
                if v and ((k == "stop") == (s > 0)) == (v < ref):
                    r[k] = v
        elif self.pos and not closing:
            r.update(self._levels(self.avg, self.pos))
        return r or None

    # ---- переменные ----

    def frame(self, fk) -> Dict[str, Var]:
        return self.frames.setdefault(fk, {})

    def lookup(self, name: str, fk) -> Optional[Var]:
        f = self.frames.get(fk)
        if f is not None and name in f:
            return f[name]
        return self.globals.get(name)

    def declare(self, name: str, value, fk, persist=False):
        f = self.frame(fk)
        var = f.get(name)
        if var is None:
            var = f[name] = Var(persist)
        var.set(self.i, value)
        var.inited = True

    def store(self, target, value, fk):
        if not isinstance(target, P.Name):
            raise _err(target, "присваивать можно только переменной")
        var = self.lookup(target.id, fk)
        if var is None:
            self.declare(target.id, value, fk)
        else:
            var.set(self.i, value)

    # ---- операторы ----

    def exec_block(self, stmts, fk):
        last = None
        for st in stmts:
            last = self.exec_stmt(st, fk)
        return last

    def exec_stmt(self, st, fk):
        t = type(st)
        if t is P.Expr:
            return self.ev(st.value, fk)
        if t is P.Assign:
            persist = isinstance(st.mode, (P.Var, P.VarIp))
            if isinstance(st.target, P.Tuple):
                val = self.ev(st.value, fk)
                vals = list(val) if isinstance(val, (list, tuple)) else [val] * len(st.target.elts)
                for el, v in zip(st.target.elts, vals):
                    self.declare(el.id, v, fk)
                return val
            if persist:
                var = self.frame(fk).get(st.target.id)
                if var is not None and var.inited:
                    return var.get(self.i)
            val = self.ev(st.value, fk) if st.value is not None else None
            self.declare(st.target.id, val, fk, persist)
            return val
        if t is P.ReAssign:
            val = self.ev(st.value, fk)
            self.store(st.target, val, fk)
            return val
        if t is P.AugAssign:
            val = _binop(st.op, self.ev(st.target, fk), self.ev(st.value, fk), st)
            self.store(st.target, val, fk)
            return val
        if t is P.FunctionDef:
            return None
        if t is P.Break:
            raise _Break
        if t is P.Continue:
            raise _Continue
        raise _err(st, f"конструкция {t.__name__} не поддерживается")

    # ---- выражения ----

    def ev(self, node, fk):
        f = self.dispatch.get(type(node))
        if f is None:
            raise _err(node, f"конструкция {type(node).__name__} не поддерживается")
        return f(node, fk)

    SERIES = {"open": "o", "high": "h", "low": "l", "close": "c", "volume": "v", "time": "t"}

    def series_at(self, name: str, j: int):
        if j < 0:
            return None
        if name in self.SERIES:
            return getattr(self, self.SERIES[name])[j]
        h, l, c, o = self.h[j], self.l[j], self.c[j], self.o[j]
        return {"hl2": (h + l) / 2, "hlc3": (h + l + c) / 3, "ohlc4": (o + h + l + c) / 4,
                "hlcc4": (h + l + 2 * c) / 4, "bar_index": j, "time_close": self.t[j] + self.tf_ms,
                "time_tradingday": self.t[j]}.get(name, KeyError)

    def ev_name(self, node, fk):
        name = node.id
        var = self.lookup(name, fk)
        if var is not None:
            return var.get(self.i)
        v = self.series_at(name, self.i)
        if v is not KeyError:
            return v
        if name == "na":
            return None
        if name in ("true", "false"):
            return name == "true"
        if name == "last_bar_index":
            return self.n - 1
        if name in ("year", "month", "dayofmonth", "hour", "minute", "second", "weekofyear", "dayofweek"):
            return BUILTINS[name][0](self)
        raise _err(node, f"неизвестное имя «{name}»")

    def ev_binop(self, node, fk):
        return _binop(node.op, self.ev(node.left, fk), self.ev(node.right, fk), node)

    def ev_unary(self, node, fk):
        v = self.ev(node.operand, fk)
        if isinstance(node.op, P.Not):
            return not _truthy(v)
        if v is None:
            return None
        return -v if isinstance(node.op, P.USub) else v

    def ev_boolop(self, node, fk):
        if isinstance(node.op, P.And):
            return all(_truthy(self.ev(v, fk)) for v in node.values)
        return any(_truthy(self.ev(v, fk)) for v in node.values)

    def ev_compare(self, node, fk):
        left = self.ev(node.left, fk)
        for op, comp in zip(node.ops, node.comparators):
            right = self.ev(comp, fk)
            if not _compare(op, left, right):
                return False
            left = right
        return True

    def ev_cond(self, node, fk):
        return self.ev(node.body, fk) if _truthy(self.ev(node.test, fk)) else self.ev(node.orelse, fk)

    def ev_if(self, node, fk):
        if _truthy(self.ev(node.test, fk)):
            return self.exec_block(node.body, fk)
        return self.exec_block(node.orelse, fk) if node.orelse else None

    def ev_forto(self, node, fk):
        start, end = self.ev(node.start, fk), self.ev(node.end, fk)
        if start is None or end is None:
            return None
        step = self.ev(node.step, fk) if node.step is not None else (1 if end >= start else -1)
        if not step:
            return None
        last, k, guard = None, start, 0
        while (k <= end) if step > 0 else (k >= end):
            self.declare(node.target.id, k, fk)
            try:
                last = self.exec_block(node.body, fk)
            except _Break:
                break
            except _Continue:
                pass
            k += step
            guard += 1
            if guard > 100_000:
                raise _err(node, "цикл выполняется больше 100 000 раз")
        return last

    def ev_forin(self, node, fk):
        items = self.ev(node.iter, fk) or []
        last = None
        for k, item in enumerate(list(items)):
            if isinstance(node.target, P.Tuple):
                self.declare(node.target.elts[0].id, k, fk)
                self.declare(node.target.elts[1].id, item, fk)
            else:
                self.declare(node.target.id, item, fk)
            try:
                last = self.exec_block(node.body, fk)
            except _Break:
                break
            except _Continue:
                pass
        return last

    def ev_while(self, node, fk):
        last, guard = None, 0
        while _truthy(self.ev(node.test, fk)):
            try:
                last = self.exec_block(node.body, fk)
            except _Break:
                break
            except _Continue:
                pass
            guard += 1
            if guard > 100_000:
                raise _err(node, "цикл while выполняется больше 100 000 раз")
        return last

    def ev_switch(self, node, fk):
        subject = self.ev(node.subject, fk) if node.subject is not None else None
        for case in node.cases:
            if case.pattern is None:
                return self.exec_block(case.body, fk)
            v = self.ev(case.pattern, fk)
            if (node.subject is not None and v == subject) or (node.subject is None and _truthy(v)):
                return self.exec_block(case.body, fk)
        return None

    def ev_subscript(self, node, fk):
        n = self.ev(node.slice, fk)
        if n is None:
            return None
        j = self.i - int(n)
        tgt = node.value
        if isinstance(tgt, P.Name):
            var = self.lookup(tgt.id, fk)
            if var is not None:
                return var.get(j) if j >= 0 else None
            v = self.series_at(tgt.id, j) if j >= 0 else None
            if v is not KeyError:
                return v
        val = self.ev(tgt, fk)  # любое выражение: считаем на каждой свече и храним историю
        key = (fk, id(node))
        h = self.hist.get(key)
        if h is None:
            h = self.hist[key] = Var(False)
        h.set(self.i, val)
        return h.get(j) if 0 <= j < self.i else (val if j == self.i else None)

    CONST = {"strategy.long": "long", "strategy.short": "short", "math.pi": math.pi, "math.e": math.e,
             "math.phi": (1 + 5 ** 0.5) / 2, "barstate.isconfirmed": True, "barstate.isnew": True,
             "barstate.isrealtime": False, "barstate.ishistory": True, "syminfo.pointvalue": 1,
             "syminfo.type": "crypto", "syminfo.timezone": "UTC", "syminfo.session": "24x7",
             "timeframe.isseconds": False, "strategy.cash": "cash", "strategy.fixed": "fixed",
             "strategy.percent_of_equity": "percent_of_equity", "strategy.initial_capital": 1000,
             "dayofweek.sunday": 1, "dayofweek.monday": 2, "dayofweek.tuesday": 3, "dayofweek.wednesday": 4,
             "dayofweek.thursday": 5, "dayofweek.friday": 6, "dayofweek.saturday": 7}

    def attr_value(self, name: str, node):
        if name in self.CONST:
            return self.CONST[name]
        i = self.i
        dyn = {
            "barstate.isfirst": lambda: i == 0, "barstate.islast": lambda: i == self.n - 1,
            "barstate.islastconfirmedhistory": lambda: i == self.n - 1,
            "syminfo.mintick": lambda: self.mintick, "syminfo.ticker": lambda: self.symbol.replace("/", ""),
            "syminfo.tickerid": lambda: "BINANCE:" + self.symbol.replace("/", ""),
            "syminfo.basecurrency": lambda: self.symbol.split("/")[0],
            "syminfo.currency": lambda: self.symbol.split("/")[-1],
            "timeframe.period": lambda: self.tf, "timeframe.main_period": lambda: self.tf,
            "timeframe.multiplier": lambda: int(self.tf) if self.tf.isdigit() else 1,
            "timeframe.isintraday": lambda: self.tf_ms < 86_400_000,
            "timeframe.isminutes": lambda: self.tf_ms < 86_400_000, "timeframe.isdaily": lambda: self.tf == "D",
            "timeframe.isweekly": lambda: self.tf == "W", "timeframe.ismonthly": lambda: False,
            "strategy.position_size": lambda: self.pos, "strategy.position_avg_price": lambda: self.avg,
            "strategy.opentrades": lambda: 1 if self.pos else 0,
            "strategy.closedtrades": lambda: self.trades - (1 if self.pos else 0),
            "strategy.equity": lambda: 1000.0, "strategy.netprofit": lambda: 0.0,
            "strategy.position_entry_name": lambda: self.entry_id or "",
            "ta.tr": lambda: _tr(self, i, False),
            "ta.vwap": lambda: self.call_builtin("ta.vwap", node, [], {}, ()),
            "ta.obv": lambda: self._obv(node), "ta.accdist": lambda: None,
        }
        if name in dyn:
            return dyn[name]()
        ns = name.split(".")[0]
        if ns in VISUAL or ns == "strategy" and ("." in name and name.split(".")[1] in ("oca", "commission", "direction")):
            return name
        raise _err(node, f"неизвестно: «{name}»")

    def _obv(self, node):
        st = self.state.setdefault(((), id(node)), {})
        if st.get("bar") != self.i:
            i = self.i
            d = 0 if i == 0 else (self.c[i] > self.c[i - 1]) - (self.c[i] < self.c[i - 1])
            st["v"] = st.get("v", 0.0) + d * self.v[i]
            st["bar"] = i
        return st["v"]

    def ev_attr(self, node, fk):
        name = dotted(node)
        if name is None:
            raise _err(node, "сложное обращение к полю не поддерживается")
        head = name.split(".")[0]
        if self.lookup(head, fk) is not None:
            raise _err(node, f"поля объектов ({name}) не поддерживаются")
        return self.attr_value(name, node)

    # ---- вызовы ----

    def args(self, node, fk) -> Tuple[list, dict]:
        pos, kw = [], {}
        for a in node.args:
            v = self.ev(a.value, fk)
            if a.name:
                kw[a.name] = v
            else:
                pos.append(v)
        return pos, kw

    def ev_call(self, node, fk):
        fn = dotted(node.func)
        if fn is None:
            raise _err(node, "такой вызов не поддерживается")
        if fn in self.s.funcs:
            return self.call_user(self.s.funcs[fn], node, fk)
        head, _, rest = fn.partition(".")
        if rest and self.lookup(head, fk) is not None:  # метод массива: arr.push(x)
            obj = self.lookup(head, fk).get(self.i)
            pos, kw = self.args(node, fk)
            impl = BUILTINS.get(f"array.{rest}")
            if impl is None:
                raise _err(node, f"метод {rest} не поддерживается")
            return impl[0](self, obj, *pos, *kw.values())
        if fn == "input" or fn.startswith("input."):
            return self.input_value(node, fk)
        if fn == "request.security":
            return self.security(node, fk)
        if fn == "alertcondition":
            return self.alertcondition(node, fk)
        if fn.startswith("strategy."):
            return self.strategy_call(fn, node, fk)
        if fn in NOOP or head in VISUAL:
            return None
        if fn == "runtime.error":
            pos, kw = self.args(node, fk)
            raise _err(node, f"runtime.error: {pos[0] if pos else ''}")
        pos, kw = self.args(node, fk)
        return self.call_builtin(fn, node, pos, kw, fk)

    def call_builtin(self, fn, node, pos, kw, fk):
        impl = BUILTINS.get(fn)
        if impl is None:
            raise _err(node, f"функция {fn} не поддерживается")
        f, params, stateful = impl
        args = list(pos)
        for k, v in kw.items():
            if k in params:
                idx = params.index(k)
                while len(args) <= idx:
                    args.append(None)
                args[idx] = v
        if not stateful:
            return f(self, *args)
        key = (fk, id(node))
        st = self.state.get(key)
        if st is None:
            st = self.state[key] = {}
        if st.get("_bar") == self.i:  # одна точка вызова — одно значение на свечу, как в TradingView
            return st["_val"]
        val = f(self, st, *args)
        st["_bar"], st["_val"] = self.i, val
        return val

    def call_user(self, fdef, node, fk):
        pos, kw = self.args(node, fk)
        sub = fk + (id(node),)
        frame = self.frame(sub)
        for k, p in enumerate(fdef.args):
            if k < len(pos):
                v = pos[k]
            elif p.name in kw:
                v = kw[p.name]
            else:
                v = self.ev(p.default, fk) if p.default is not None else None
            var = frame.get(p.name)
            if var is None:
                var = frame[p.name] = Var(False)
            var.set(self.i, v)
        return self.exec_block(fdef.body, sub)

    def input_value(self, node, fk):
        info = self.s.inputs.get(id(node))
        if info is None:
            return None
        if info["key"] is not None:
            v = self.params.get(info["key"], self.s.params[info["key"]])
            if info["kind"] == "bool":
                return bool(v)
            return int(v) if info["kind"] == "int" else v
        if info["kind"] == "source" and info["default_node"] is not None:
            return self.ev(info["default_node"], fk)
        return info["default"]

    def security(self, node, fk):
        tf_node, expr = _arg(node, 1, "timeframe"), _arg(node, 2, "expression")
        tf = self.ev(tf_node, fk) if tf_node is not None else ""
        if tf not in ("", None, self.tf) and not (tf == "D" and self.tf == "1D") and not self.lenient:
            human = {v: k for k, v in {"1m": "1", "3m": "3", "5m": "5", "15m": "15", "30m": "30", "1h": "60",
                                        "2h": "120", "4h": "240", "1d": "D", "1w": "W"}.items()}
            raise _err(node, f"request.security на другой таймфрейм («{tf}», а у бота свечи "
                             f"{human.get(self.tf, self.tf)}) не поддерживается — в боте есть свечи только "
                             f"своего таймфрейма")
        return self.ev(expr, fk)

    def alertcondition(self, node, fk):
        cond = self.ev(_arg(node, 0, "condition"), fk)
        title_node, msg_node = _arg(node, 1, "title"), _arg(node, 2, "message")
        text = " ".join(str(_literal(x) or "") for x in (title_node, msg_node) if x is not None)
        if not _truthy(cond):
            return None
        low = text.lower()
        side = "long" if re.search(r"buy|long|bull|покуп|лонг|вверх", low) else \
            "short" if re.search(r"sell|short|bear|прода|шорт|вниз", low) else None
        if side:
            self.alerts.append((side, text.strip() or side))
        return None

    def strategy_call(self, fn, node, fk):
        name = fn.split(".", 1)[1]
        pos, kw = self.args(node, fk)

        def a(k, idx):
            return kw.get(k, pos[idx] if idx < len(pos) else None)

        if "when" in kw and not _truthy(kw["when"]):
            return None
        if name in ("entry", "order"):
            if a("limit", 3) is not None or a("stop", 4) is not None:
                raise _err(node, f"strategy.{name} с limit или stop (отложенный вход) не поддерживается — "
                                 f"бот входит по рынку на открытии следующей свечи")
            direction = a("direction", 1)
            if direction not in ("long", "short"):
                raise _err(node, "strategy.entry: направление должно быть strategy.long или strategy.short")
            self.orders.append(("entry", str(a("id", 0)), direction, kw.get("comment")))
        elif name == "exit":
            if any(kw.get(k) is not None for k in ("trail_price", "trail_points", "trail_offset")):
                raise _err(node, "трейлинг-стоп в strategy.exit не поддерживается")
            eid = str(a("id", 0))
            self.exits[eid] = {"from": a("from_entry", 1) or "", "profit": kw.get("profit"),
                               "limit": kw.get("limit"), "loss": kw.get("loss"), "stop": kw.get("stop")}
        elif name == "close":
            self.closes.add(str(a("id", 0)))
        elif name == "close_all":
            self.close_all = True
        elif name in ("cancel", "cancel_all"):
            self.orders = [o for o in self.orders if name == "cancel" and o[1] != str(a("id", 0))]
        elif name.startswith("risk."):
            return None
        else:
            raise _err(node, f"strategy.{name} не поддерживается")
        return None


# ---------- обёртка для scripts.py: ведёт себя как модуль с signals() ----------

_compiled: Dict[str, Compiled] = {}


def compile_cached(code: str) -> Compiled:
    """Разбор с кэшем: один и тот же код при сохранении проверяется несколько раз."""
    comp = _compiled.get(code)
    if comp is None:
        comp = Compiled(code)
        if len(_compiled) > 50:
            _compiled.clear()
        _compiled[code] = comp
    return comp


def load(code: str) -> SimpleNamespace:
    """Разбирает Pine Script и возвращает «модуль» сигнала: NAME, PARAMS, signals(candles, p)…"""
    comp = compile_cached(code)
    name = str(comp.title or "Pine Script").strip()[:80]
    has_exits = any(isinstance(n, P.Call) and dotted(n.func) in ("strategy.exit", "strategy.close", "strategy.close_all")
                    for st in comp.body for n in _walk(st))

    def signals(candles, p):
        return Runtime(comp, list(candles), dict(p), lenient=ns.LENIENT).run()

    kind = "стратегия" if comp.kind == "strategy" else "индикатор (сигналы — из alertcondition)"
    ns = SimpleNamespace(
        NAME=name, ABOUT=f"Pine Script с TradingView, {kind}. Выполняется в боте как есть.",
        PARAMS=dict(comp.params), LABELS=dict(comp.labels), WARMUP=min(1000, max(50, 3 * comp.max_len)),
        LEVELS=comp.kind == "strategy" and has_exits, CAUSAL=True, PINE=True, LENIENT=False,
        signals=signals, __file__="<pine>")
    return ns


def warmup() -> None:
    """Первый разбор Pine после запуска долгий (~10–20 с): парсер (ANTLR) при первой встрече каждой
    конструкции языка строит свои таблицы. Прогреваем на скрипте в стиле TradingView, чтобы
    вставленный потом код разбирался за доли секунды."""
    P.parse((Path(__file__).parent / "pine_warmup.pine").read_text(encoding="utf-8"))
