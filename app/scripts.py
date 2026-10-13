"""Сигналы-скрипты: свой сигнал на входе в виде небольшого файла на Python.

Файл лежит в data/signals/<id>.py. Добавить его можно из панели («Сигналы») или просто
положить в эту папку: программа подхватывает новые и изменённые файлы без перезапуска.

Что должно быть в файле (полный пример — TEMPLATE ниже):

    NAME = "Название"                 # обязательно
    ABOUT = "Что делает"              # необязательно
    PARAMS = {"range_pct": 0.5}       # настройки, которые видны в редакторе бота (числа)
    WARMUP = 50                       # сколько свечей нужно до первого сигнала
    LEVELS = True                     # сигнал сам задаёт стоп и тейк (см. ниже)

    def signal(candles, p):
        ...

signal вызывается на закрытии каждой свечи. candles — закрытые свечи, последняя
только что закрылась: candles[-1].close, candles[-2].low и т. д. (есть time, open,
high, low, close, volume; можно и candles[-1]["close"]). p — PARAMS с настройками бота.
Вернуть нужно None (сигнала нет), "long", "short" или словарь
{"side": "long", "stop": цена, "take": цена, "why": "почему"}. stop и take необязательны:
если они есть, позиция закрывается по ним, а не по процентам из настроек бота.
Пока позиция открыта, сигнал может ей управлять — словарь без side: {"exit": True} закрывает
позицию (или {"exit": "long"} — только лонг), {"stop": цена, "take": цена} переставляет уровни.
Вход — по цене открытия следующей свечи, как и у сигналов из конструктора.

Вместо signal можно написать signals(candles, p) — она вызывается один раз на все свечи и
возвращает список такой же длины: в элементе i — ответ для свечи i. Так удобнее и в сотни
раз быстрее для индикаторов вроде EMA или ADX: ряд считается один раз, а не на каждой свече.
Элемент i должен зависеть только от свечей 0..i — иначе проверка на истории «подсмотрит»
будущее и покажет то, чего вживую не будет. Готовые индикаторы: from app import indicators.

Скрипт выполняется внутри программы со всеми её правами — добавляйте только свой код
или тот, который прочитали. Поэтому загрузка из панели работает, только когда панель
открыта на этом компьютере (HOST=127.0.0.1), или если явно разрешить ALLOW_SCRIPTS=1.
"""
import keyword
import math
import os
import random
import re
import shutil
import threading
import traceback
import types
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional

from . import config

EXAMPLES = Path(__file__).parent / "signals_examples"
MAX_CODE = 100_000
LONG_PREFIX = "\\\\?\\"  # \\?\ — длинные пути Windows
EXTS = (".py", ".pine")  # сигнал на Python или Pine Script с TradingView (выполняет app/pine.py)
TIMEOUT = 30  # секунд на прогон скрипта по всем свечам

TEMPLATE = '''"""Шаблон сигнала. Поменяйте NAME, PARAMS и тело signal — остальное можно не трогать."""

NAME = "Мой сигнал"
ABOUT = "Лонг, когда свеча закрылась на N% выше открытия."

# Настройки, которые появятся в редакторе бота. Только числа.
PARAMS = {
    "min_body_pct": 1.0,   # тело свечи не меньше, %
}

WARMUP = 2      # сколько свечей нужно функции signal
LEVELS = False  # True — signal сам возвращает stop и take для каждой сделки


def signal(candles, p):
    """candles[-1] — свеча, которая только что закрылась. Вернуть None, "long", "short"
    или {"side": "long", "stop": цена, "take": цена, "why": "текст для журнала"}."""
    c = candles[-1]
    body = (c.close - c.open) / c.open * 100
    if body >= p["min_body_pct"]:
        return {"side": "long", "why": f"тело свечи {body:.2f}%"}
    return None
'''


class Candle(NamedTuple):
    time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    buy_volume: Optional[float] = None  # объём рыночных покупок (taker buy); продажи — volume − buy_volume

    def __getitem__(self, key):  # candle["close"] — как в pandas, candle[4] — как в ccxt
        return getattr(self, key) if isinstance(key, str) else tuple.__getitem__(self, key)


class Candles:
    """Свечи с первой по end — без копирования: скрипт вызывается на каждой свече истории."""

    def __init__(self, data: List[Candle], end: int):
        self._d, self._n = data, end

    def __len__(self):
        return self._n

    def __getitem__(self, i):
        if isinstance(i, slice):
            return self._d[:self._n][i]
        if i < 0:
            i += self._n
        if not 0 <= i < self._n:
            raise IndexError("нет такой свечи")
        return self._d[i]

    def __iter__(self):
        return iter(self._d[:self._n])


class Script:
    def __init__(self, sid: str, path: Path, mtime: float):
        self.id, self.path, self.mtime = sid, path, mtime
        self.code = path.read_text(encoding="utf-8")
        self.error: Optional[str] = None
        self.mod: Optional[types.ModuleType] = None
        try:
            self.mod = _pine().load(self.code) if path.suffix == ".pine" else _load(sid, self.code, str(path))
        except ValueError as e:
            self.error = str(e)

    @property
    def name(self) -> str:
        return getattr(self.mod, "NAME", self.id) if self.mod else self.id

    @property
    def params(self) -> Dict[str, float]:
        return dict(getattr(self.mod, "PARAMS", {})) if self.mod else {}

    @property
    def warmup(self) -> int:
        return int(getattr(self.mod, "WARMUP", 50)) if self.mod else 50

    @property
    def levels(self) -> bool:
        return bool(getattr(self.mod, "LEVELS", False)) if self.mod else False

    @property
    def labels(self) -> Dict[str, str]:
        """Подписи настроек для панели — из комментариев в PARAMS: "range_pct": 0.5,  # размах, %"""
        if getattr(self.mod, "LABELS", None):  # Pine: подписи из input.*(…, title)
            return dict(self.mod.LABELS)
        found = re.findall(r"""^\s*["'](\w+)["']\s*:[^#\n]*#\s*(.+?)\s*$""", self.code, re.M)
        return {k: v for k, v in found if k in self.params}

    def info(self, with_code: bool = False) -> Dict[str, Any]:
        d = {"id": self.id, "name": self.name, "about": getattr(self.mod, "ABOUT", "") if self.mod else "",
             "params": self.params, "labels": self.labels, "warmup": self.warmup, "levels": self.levels,
             "error": self.error, "file": self.path.name, "pine": self.path.suffix == ".pine"}
        if with_code:
            d["code"] = self.code
        return d


def folder() -> Path:
    """data/signals рядом с базой. Туда кладутся примеры — каждый один раз: в .examples
    записано, какие уже предлагались, так что удалённый пример не вернётся, а новый
    из обновлённой версии программы появится."""
    d = Path(config.DB_PATH).parent / "signals"
    if os.name == "nt" and not str(d).startswith(LONG_PREFIX):
        # Windows: путь длиннее 260 символов (глубокая папка + длинное имя сигнала) иначе не открыть
        d = Path(LONG_PREFIX + str(d.resolve()))
    d.mkdir(parents=True, exist_ok=True)
    seen_file = d / ".examples"
    try:
        seen = set(seen_file.read_text(encoding="utf-8").split())
    except OSError:  # папка от версии без .examples: примеры, что там уже лежат, — предложены
        seen = {f.name for f in d.glob("*.py")}
    new = [f for f in sorted(EXAMPLES.glob("*.py")) if f.name not in seen]
    for f in new:
        if not (d / f.name).exists():
            shutil.copyfile(f, d / f.name)
    if new or not seen_file.exists():
        seen_file.write_text("\n".join(sorted(seen | {f.name for f in new})) + "\n", encoding="utf-8")
    return d


_cache: Dict[str, Script] = {}
_lock = threading.Lock()


def all_scripts() -> List[Script]:
    """Все скрипты из папки; изменённые файлы перечитываются, удалённые пропадают."""
    with _lock:
        seen = set()
        for path in sorted(p for p in folder().iterdir() if p.suffix in EXTS):
            sid = path.stem
            seen.add(sid)
            mtime = path.stat().st_mtime
            if sid not in _cache or _cache[sid].mtime != mtime:
                _cache[sid] = Script(sid, path, mtime)
        for sid in set(_cache) - seen:
            del _cache[sid]
        return sorted(_cache.values(), key=lambda s: s.name.lower())


def get(sid: str) -> Script:
    s = next((s for s in all_scripts() if s.id == sid), None)
    if s is None:
        raise ValueError(f"Сигнал «{sid}» не найден: файла data/signals/{sid}.py нет")
    if s.error:
        raise ValueError(f"Сигнал «{s.name}» с ошибкой: {s.error}")
    return s


def editable() -> bool:
    return config.HOST in ("127.0.0.1", "localhost", "::1") or config.ALLOW_SCRIPTS


# ---------- загрузка и проверка ----------

def _load(sid: str, code: str, filename: str) -> types.ModuleType:
    """Выполняет код файла и проверяет, что в нём есть всё нужное. Ошибки — ValueError."""
    if len(code) > MAX_CODE:
        raise ValueError("Файл слишком большой")
    mod = types.ModuleType(f"valesbot_signal_{sid}")
    mod.__file__ = filename
    try:
        exec(compile(code, filename, "exec"), mod.__dict__)
    except SyntaxError as e:
        raise ValueError(f"Ошибка в коде, строка {e.lineno}: {e.msg}")
    except ModuleNotFoundError as e:
        raise ValueError(f"Нет библиотеки {e.name}. В программе есть стандартный Python и ccxt; "
                         f"pandas и numpy не входят — работайте со списком свечей")
    except Exception as e:
        raise ValueError(f"Файл не выполнился: {_where(e, filename)}")
    if not callable(getattr(mod, "signal", None)) and not callable(getattr(mod, "signals", None)):
        raise ValueError("В файле нет функции signal(candles, p) или signals(candles, p)")
    if not isinstance(getattr(mod, "NAME", None), str) or not mod.NAME.strip():
        raise ValueError("Задайте название сигнала: NAME = \"...\"")
    params = getattr(mod, "PARAMS", {})
    if not isinstance(params, dict):
        raise ValueError("PARAMS должен быть словарём: {\"имя\": число}")
    for k, v in params.items():
        if not isinstance(k, str) or not k.isidentifier() or keyword.iskeyword(k):
            raise ValueError(f"Имя настройки «{k}» должно быть словом латиницей")
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            raise ValueError(f"Настройка «{k}» должна быть числом")
    w = getattr(mod, "WARMUP", 50)
    if isinstance(w, bool) or not isinstance(w, int) or not 1 <= w <= 1000:
        raise ValueError("WARMUP — целое число от 1 до 1000")
    return mod


def _where(e: BaseException, filename: str) -> str:
    """«строка 12: ZeroDivisionError: division by zero» — место ошибки в файле скрипта."""
    frames = [f for f in traceback.extract_tb(e.__traceback__) if f.filename == filename]
    line = f"строка {frames[-1].lineno}: " if frames else ""
    return f"{line}{type(e).__name__}: {e}"


def slug(name: str) -> str:
    tr = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
                  "a b v g d e e zh z i y k l m n o p r s t u f h ts ch sh sch - y - e yu ya".split()))
    s = "".join(tr.get(ch, ch) for ch in name.lower())
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    return (s or "signal")[:40]


def _pine():
    from . import pine  # разборщик Pine грузится, только когда он нужен
    return pine


def warmup_pine() -> None:
    try:
        _pine().warmup()
    except Exception:  # прогрев — не обязательная часть, ошибка в нём не должна мешать запуску
        pass


def has_pine() -> bool:
    return any(p.suffix == ".pine" for p in folder().iterdir())


def check(code: str):
    """Загружает код из панели (Python или Pine Script) и проверяет его. Ошибки — ValueError."""
    code = code.replace("\r\n", "\n")
    if _pine().is_pine(code):
        return _pine().load(code)
    return _load("check", code, "<проверка>")


def save(code: str, sid: Optional[str] = None) -> Dict[str, Any]:
    """Проверяет код, прогоняет на пробных свечах и сохраняет в data/signals/<id>.py."""
    code = code.replace("\r\n", "\n")
    mod = check(code)
    sid = sid or slug(mod.NAME)
    if not re.fullmatch(r"[a-z0-9_]{1,40}", sid):
        raise ValueError("Имя файла — латиница, цифры и _")
    test = dry_run(mod)
    ext = ".pine" if getattr(mod, "PINE", False) else ".py"
    for other in EXTS:  # тот же сигнал мог раньше лежать на другом языке
        if other != ext and (folder() / f"{sid}{other}").exists():
            (folder() / f"{sid}{other}").unlink()
    path = folder() / f"{sid}{ext}"
    path.write_text(code, encoding="utf-8")
    return {**next(s for s in all_scripts() if s.id == sid).info(), "test": test}


def delete(sid: str) -> None:
    paths = [folder() / f"{sid}{ext}" for ext in EXTS if (folder() / f"{sid}{ext}").exists()]
    if not paths:
        raise ValueError("Такого сигнала нет")
    for path in paths:
        path.unlink()
    all_scripts()


def dry_run(mod) -> str:
    """Прогон по 2000 случайным свечам: ловит ошибки, которые проявляются не на первой свече."""
    rnd = random.Random(1)
    price, rows = 100.0, []
    for k in range(2000):
        o = price
        price *= math.exp(rnd.gauss(0, 0.004))
        h, l = max(o, price) * (1 + abs(rnd.gauss(0, 0.002))), min(o, price) * (1 - abs(rnd.gauss(0, 0.002)))
        rows.append([k * 60_000, o, h, l, price, rnd.uniform(1, 100)])
    params = dict(getattr(mod, "PARAMS", {}))
    if hasattr(mod, "LENIENT"):  # Pine: таймфрейм в request.security проверится на свечах бота
        mod.LENIENT = True
    try:
        res = run(mod, rows, params, "<проверка>")
    finally:
        if hasattr(mod, "LENIENT"):
            mod.LENIENT = False
    n = sum(1 for r in res if r)
    if callable(getattr(mod, "signals", None)) and not getattr(mod, "CAUSAL", False):
        # signals видит все свечи сразу и может нечаянно заглянуть вперёд. Проверяем: ответ
        # для свечи i не должен меняться, если свечей после неё ещё нет — как вживую.
        hits = [i for i, r in enumerate(res) if r][:30]
        for i in sorted(set(hits + rnd.sample(range(len(rows)), 20))):
            alone = run(mod, rows[:i + 1], params, "<проверка>")[i]
            if not _same(alone, res[i]):
                raise ValueError(f"Сигнал заглядывает в будущее: на свече {i + 1} ответ «{_brief(res[i])}», а если "
                                 f"следующих свечей ещё нет — «{_brief(alone)}». Вживую будет второй вариант, "
                                 f"поэтому проверка на истории врала бы. Элемент i должен зависеть только от свечей 0..i")
    return f"На 2000 пробных свечах сигнал сработал {n} раз, ошибок нет"


def _same(a: Optional[dict], b: Optional[dict]) -> bool:
    if not a or not b:
        return not a and not b
    return a["side"] == b["side"] and a.get("exit") == b.get("exit") and all(
        (a.get(k) is None) == (b.get(k) is None) and (a.get(k) is None or math.isclose(a[k], b[k], rel_tol=1e-9))
        for k in ("stop", "take"))


def _brief(r: Optional[dict]) -> str:
    if not r:
        return "нет сигнала"
    head = r["side"] or ("выход" if r.get("exit") else "уровни")
    return head + "".join(f", {k} {r[k]:g}" for k in ("stop", "take") if k in r)


# ---------- прогон ----------

def run(mod, ohlcv: List[list], params: Dict[str, float], filename: Optional[str] = None) -> List[Optional[dict]]:
    """Сигнал на каждой свече: None или {"side", "stop", "take", "why"}. Ошибки — ValueError."""
    filename = filename or getattr(mod, "__file__", "")
    data = [Candle(int(c[0]), float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[5]),
                   float(c[6]) if len(c) > 6 and c[6] is not None else None) for c in ohlcv]
    warm = int(getattr(mod, "WARMUP", 50))
    out: List[Optional[dict]] = [None] * len(data)
    if callable(getattr(mod, "signals", None)):  # весь ряд за один вызов
        try:
            res = mod.signals(Candles(data, len(data)), params)
            res = list(res)
        except Exception as e:
            if type(e).__name__ == "PineError":  # у Pine своё понятное сообщение с номером строки
                raise ValueError(f"Pine Script, {e}")
            raise ValueError(f"Сигнал упал — {_where(e, filename)}")
        if len(res) != len(data):
            raise ValueError(f"signals вернула {len(res)} ответов на {len(data)} свечей — нужно по одному на свечу")
        for i in range(warm - 1, len(data)):
            if res[i]:
                try:
                    out[i] = _result(res[i], data[i].close)
                except ValueError as e:
                    raise ValueError(f"Свеча {i + 1}: {e}")
        return out
    fn = mod.signal
    view = Candles(data, 0)
    for i in range(warm - 1, len(data)):
        view._n = i + 1
        try:
            r = fn(view, params)
        except Exception as e:
            raise ValueError(f"Сигнал упал на свече {i + 1} из {len(data)} — {_where(e, filename)}")
        if r:
            out[i] = _result(r, data[i].close)
    return out


def _result(r: Any, close: float) -> Optional[dict]:
    """Ответ сигнала в общем виде. Вход: side (+ stop, take). Без side — управление открытой
    позицией: exit (закрыть: True, "long" или "short") и/или новые stop и take."""
    if isinstance(r, str):
        r = {"side": r}
    if not isinstance(r, dict) or r.get("side") not in (None, "long", "short") or \
            r.get("exit") not in (None, False, True, "all", "long", "short"):
        raise ValueError(f"signal вернул {r!r}, а нужно None, \"long\", \"short\" или словарь "
                         f"с side (или с exit, stop, take для открытой позиции)")
    side = r.get("side")
    res: Dict[str, Any] = {"side": side, "why": str(r.get("why") or "")[:200]}
    if r.get("exit"):
        res["exit"] = True if r["exit"] in (True, "all") else r["exit"]
    long = side == "long"
    for key in ("stop", "take"):
        v = r.get(key)
        if v is None:
            continue
        try:
            v = float(v)
        except (TypeError, ValueError):
            raise ValueError(f"{key} должен быть ценой, а не {v!r}")
        if v <= 0 or not math.isfinite(v):
            raise ValueError(f"{key} должен быть положительной ценой, а не {v:g}")
        if side is None:  # новый уровень для открытой позиции: сторону знает бот
            res[key] = v
            continue
        below = key == "stop" if long else key == "take"
        if (below and v >= close) or (not below and v <= close):
            side = "лонга" if long else "шорта"
            where = "ниже" if below else "выше"
            raise ValueError(f"{key} {v:g} для {side} должен быть {where} цены закрытия {close:g}")
        res[key] = v
    if side is None and not res.get("exit") and "stop" not in res and "take" not in res:
        return None
    return res


def compute(sid: str, ohlcv: List[list], params: Dict[str, float]) -> List[Optional[dict]]:
    """Прогон сигнала с ограничением по времени: зависший скрипт не должен вешать программу."""
    s = get(sid)
    box: Dict[str, Any] = {}

    def work():
        try:
            box["res"] = run(s.mod, ohlcv, params, str(s.path))
        except BaseException as e:  # noqa: BLE001 — передаём в основной поток
            box["err"] = e

    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(TIMEOUT)
    if t.is_alive():
        raise ValueError(f"Сигнал «{s.name}» считает дольше {TIMEOUT} с — похоже, в нём бесконечный цикл")
    if "err" in box:
        raise box["err"] if isinstance(box["err"], ValueError) else ValueError(str(box["err"]))
    return box["res"]


def clean_params(s: Script, raw: Any) -> Dict[str, float]:
    """Настройки сигнала для бота: значения по умолчанию из PARAMS, поверх — заданные."""
    raw = raw if isinstance(raw, dict) else {}
    out = {}
    for k, default in s.params.items():
        v = raw.get(k, default)
        try:
            v = float(v)
        except (TypeError, ValueError):
            raise ValueError(f"Настройка сигнала «{k}» должна быть числом")
        if not math.isfinite(v):
            raise ValueError(f"Настройка сигнала «{k}» должна быть числом")
        out[k] = int(v) if isinstance(default, int) and v.is_integer() else v
    return out

