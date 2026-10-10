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
Вход — по цене открытия следующей свечи, как и у сигналов из конструктора.

Скрипт выполняется внутри программы со всеми её правами — добавляйте только свой код
или тот, который прочитали. Поэтому загрузка из панели работает, только когда панель
открыта на этом компьютере (HOST=127.0.0.1), или если явно разрешить ALLOW_SCRIPTS=1.
"""
import keyword
import math
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
            self.mod = _load(sid, self.code, str(path))
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
        found = re.findall(r"""^\s*["'](\w+)["']\s*:[^#\n]*#\s*(.+?)\s*$""", self.code, re.M)
        return {k: v for k, v in found if k in self.params}

    def info(self, with_code: bool = False) -> Dict[str, Any]:
        d = {"id": self.id, "name": self.name, "about": getattr(self.mod, "ABOUT", "") if self.mod else "",
             "params": self.params, "labels": self.labels, "warmup": self.warmup, "levels": self.levels,
             "error": self.error}
        if with_code:
            d["code"] = self.code
        return d


def folder() -> Path:
    """data/signals рядом с базой. При первом запуске туда кладутся примеры."""
    d = Path(config.DB_PATH).parent / "signals"
    if not d.exists():
        d.mkdir(parents=True)
        for f in EXAMPLES.glob("*.py"):
            shutil.copyfile(f, d / f.name)
    return d


_cache: Dict[str, Script] = {}
_lock = threading.Lock()


def all_scripts() -> List[Script]:
    """Все скрипты из папки; изменённые файлы перечитываются, удалённые пропадают."""
    with _lock:
        seen = set()
        for path in sorted(folder().glob("*.py")):
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
    if not callable(getattr(mod, "signal", None)):
        raise ValueError("В файле нет функции signal(candles, p)")
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


def check(code: str) -> types.ModuleType:
    """Загружает код из панели и проверяет, что в нём всё на месте. Ошибки — ValueError."""
    return _load("check", code.replace("\r\n", "\n"), "<проверка>")


def save(code: str, sid: Optional[str] = None) -> Dict[str, Any]:
    """Проверяет код, прогоняет на пробных свечах и сохраняет в data/signals/<id>.py."""
    code = code.replace("\r\n", "\n")
    mod = check(code)
    sid = sid or slug(mod.NAME)
    if not re.fullmatch(r"[a-z0-9_]{1,40}", sid):
        raise ValueError("Имя файла — латиница, цифры и _")
    test = dry_run(mod)
    path = folder() / f"{sid}.py"
    path.write_text(code, encoding="utf-8")
    return {**next(s for s in all_scripts() if s.id == sid).info(), "test": test}


def delete(sid: str) -> None:
    path = folder() / f"{sid}.py"
    if not path.exists():
        raise ValueError("Такого сигнала нет")
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
    res = run(mod, rows, dict(getattr(mod, "PARAMS", {})), "<проверка>")
    n = sum(1 for r in res if r)
    return f"На 2000 пробных свечах сигнал сработал {n} раз, ошибок нет"


# ---------- прогон ----------

def run(mod, ohlcv: List[list], params: Dict[str, float], filename: Optional[str] = None) -> List[Optional[dict]]:
    """Сигнал на каждой свече: None или {"side", "stop", "take", "why"}. Ошибки — ValueError."""
    filename = filename or getattr(mod, "__file__", "")
    data = [Candle(int(c[0]), float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[5])) for c in ohlcv]
    warm = int(getattr(mod, "WARMUP", 50))
    fn = mod.signal
    out: List[Optional[dict]] = [None] * len(data)
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


def _result(r: Any, close: float) -> dict:
    if isinstance(r, str):
        r = {"side": r}
    if not isinstance(r, dict) or r.get("side") not in ("long", "short"):
        raise ValueError(f"signal вернул {r!r}, а нужно None, \"long\", \"short\" или словарь с side")
    long = r["side"] == "long"
    res = {"side": r["side"], "why": str(r.get("why") or "")[:200]}
    for key in ("stop", "take"):
        v = r.get(key)
        if v is None:
            continue
        try:
            v = float(v)
        except (TypeError, ValueError):
            raise ValueError(f"{key} должен быть ценой, а не {v!r}")
        below = key == "stop" if long else key == "take"
        if v <= 0 or (below and v >= close) or (not below and v <= close):
            side = "лонга" if long else "шорта"
            where = "ниже" if below else "выше"
            raise ValueError(f"{key} {v:g} для {side} должен быть {where} цены закрытия {close:g}")
        res[key] = v
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

