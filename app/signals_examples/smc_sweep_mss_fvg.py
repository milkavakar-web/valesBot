"""SMC: снятие ликвидности → слом структуры (MSS) → возврат в имбаланс (FVG).

По описанию «ICT Algo: Sweep + MSS + High Prob FVG/IFVG» (TradingView, 2026): та же
последовательность, но ликвидность — свинги на рабочем таймфрейме (а не уровни прошлого
дня/недели), без киллзон и IFVG. Исходный код на странице не опубликован.

Лонг (шорт — зеркально, только на фьючерсах):
1. Ликвидность — последний подтверждённый минимум свинга (liq_swing свечей слева и справа).
2. Снятие: цена ушла ниже этого минимума. Запоминаем самый низкий фитиль — это минимум снятия.
3. Слом структуры (MSS): не позже чем через sweep_bars свечей свеча закрывается выше
   последнего максимума свинга (mss_swing) и у неё сильное тело — не меньше disp_body% диапазона.
4. FVG: между снятием и сломом есть «бычий» разрыв — минимум свечи выше максимума свечи
   двумя раньше. Зона — между ними.
5. Вход: не позже чем через entry_bars свечей цена возвращается в зону и закрывается выше
   её нижней границы. Закрылась ниже зоны или ниже минимума снятия — сетап отменён.
6. Стоп — под минимумом снятия на atr_pad ATR, тейк — rr стопов.
"""
from app import indicators as ta

NAME = "SMC: Sweep → MSS → FVG"
ABOUT = ("Снятие ликвидности под минимумом свинга, слом структуры сильной свечой и вход на "
         "возврате в оставленный имбаланс (FVG). Стоп за минимумом снятия, тейк 1:2. 15m–4h.")

PARAMS = {
    "liq_swing": 10,   # свечей слева и справа у минимума свинга — ликвидности
    "mss_swing": 5,    # свечей слева и справа у максимума, пробой которого — слом структуры
    "sweep_bars": 20,  # слом структуры не позже стольких свечей после снятия
    "disp_body": 50,   # тело сломившей свечи не меньше N% её диапазона
    "entry_bars": 20,  # возврат в FVG не позже стольких свечей после слома
    "atr_len": 14,     # период ATR
    "atr_pad": 0.25,   # стоп за минимумом снятия на столько ATR
    "rr": 2.0,         # тейк в стопах
}
WARMUP = 60
LEVELS = True


def _long_setups(o, h, l, c, atr, p):
    """Сигналы лонга. Для шорта вызывается на перевёрнутых ценах (−цена)."""
    n = len(c)
    liq = ta.pivots(l, int(p["liq_swing"]), int(p["liq_swing"]), high=False)
    mss = ta.pivots(h, int(p["mss_swing"]), int(p["mss_swing"]), high=True)
    out = [None] * n
    level = None        # (индекс, цена) ликвидности
    swing_high = None   # последний подтверждённый максимум
    sweep = None        # {"bar", "low", "mss"} — снятие, ждём слом
    setup = None        # {"bar", "top", "bottom", "low"} — слом был, ждём возврат в FVG
    for i in range(n):
        if setup:
            if i - setup["bar"] > p["entry_bars"] or c[i] < setup["bottom"] or l[i] < setup["low"]:
                setup = None
            elif l[i] <= setup["top"] and atr[i] is not None:
                stop = setup["low"] - p["atr_pad"] * atr[i]
                if stop < c[i]:
                    out[i] = (stop, c[i] + p["rr"] * (c[i] - stop),
                              f"возврат в FVG {setup['bottom']:g}–{setup['top']:g} после снятия {setup['low']:g}")
                setup = None
        if sweep:
            sweep["low"] = min(sweep["low"], l[i])
            rng = h[i] - l[i]
            if i - sweep["bar"] > p["sweep_bars"]:
                sweep = None
            elif (sweep["mss"] is not None and c[i] > sweep["mss"] and rng > 0
                  and abs(c[i] - o[i]) >= p["disp_body"] / 100 * rng):
                gap = next(((l[k], h[k - 2]) for k in range(i, max(sweep["bar"], 2) - 1, -1)
                            if l[k] > h[k - 2]), None)
                if gap:
                    setup = {"bar": i, "top": gap[0], "bottom": gap[1], "low": sweep["low"]}
                sweep = None
        elif level and l[i] < level[1] and not setup:
            sweep = {"bar": i, "low": l[i], "mss": swing_high[1] if swing_high else None}
            level = None
        if liq[i]:
            level = liq[i]
        if mss[i]:
            swing_high = mss[i]
    return out


def signals(candles, p):
    o = [x.open for x in candles]
    h = [x.high for x in candles]
    l = [x.low for x in candles]
    c = [x.close for x in candles]
    atr = ta.atr(h, l, c, int(p["atr_len"]))
    longs = _long_setups(o, h, l, c, atr, p)
    # шорт — та же логика на перевёрнутом графике: максимумы становятся минимумами
    shorts = _long_setups([-v for v in o], [-v for v in l], [-v for v in h], [-v for v in c], atr, p)
    out = [None] * len(c)
    for i in range(len(c)):
        if longs[i]:
            stop, take, why = longs[i]
            out[i] = {"side": "long", "stop": stop, "take": take, "why": why}
        elif shorts[i]:
            stop, take, why = shorts[i]
            out[i] = {"side": "short", "stop": -stop, "take": -take,
                      "why": why.replace("-", "").replace("снятия", "снятия максимума")}
    return out
