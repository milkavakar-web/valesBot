"""Multi-Regime — своя тактика для каждого режима рынка.

На каждой свече определяем режим:
- Буря: ATR в процентах от цены больше своей медианы за vol_window свечей в vol_max раз —
  рынок дёргается, не торгуем.
- Тренд: ADX не ниже trend_adx. Торгуем пробой: закрытие выше максимума прошлых break_len
  свечей и выше EMA(ema_len) — лонг (ниже минимума и ниже EMA — шорт). Стоп trend_atr ATR,
  тейк trend_rr стопов.
- Боковик: ADX не выше range_adx. Торгуем возврат к средней: закрытие вернулось в полосы
  Боллинджера снизу — лонг (сверху — шорт). Тейк — средняя линия, стоп range_atr ATR.
- Между range_adx и trend_adx режим неясен — не торгуем.
Шорт — только на фьючерсах.
"""
from app import indicators as ta

NAME = "Multi-Regime: пробой в тренде, возврат в боковике"
ABOUT = ("Определяет режим по ADX и волатильности: в тренде торгует пробой по EMA 200, в боковике — "
         "возврат от полос Боллинджера к средней, в бурю не торгует. 1h–4h.")

PARAMS = {
    "adx_len": 14,      # период ADX
    "trend_adx": 25,    # ADX не ниже — тренд
    "range_adx": 20,    # ADX не выше — боковик
    "vol_window": 100,  # окно медианы волатильности, свечей
    "vol_max": 2.0,     # ATR больше медианы в N раз — буря, не торгуем
    "ema_len": 200,     # EMA направления тренда
    "break_len": 20,    # пробой максимума/минимума за N свечей
    "trend_atr": 2.0,   # стоп в тренде, ATR
    "trend_rr": 2.0,    # тейк в тренде, стопов
    "bb_len": 20,       # период Боллинджера
    "bb_mult": 2.0,     # ширина Боллинджера, σ
    "range_atr": 1.5,   # стоп в боковике, ATR
    "atr_len": 14,      # период ATR
}
WARMUP = 220
LEVELS = True


def signals(candles, p):
    h = [x.high for x in candles]
    l = [x.low for x in candles]
    c = [x.close for x in candles]
    n = len(c)
    adx, _, _ = ta.adx(h, l, c, int(p["adx_len"]))
    atr = ta.atr(h, l, c, int(p["atr_len"]))
    ema = ta.ema(c, int(p["ema_len"]))
    up, mid, low = ta.bollinger(c, int(p["bb_len"]), p["bb_mult"])
    bl = int(p["break_len"])
    win = int(p["vol_window"])
    atr_pct = [None if a is None else a / x * 100 for a, x in zip(atr, c)]
    out = [None] * n
    for i in range(max(bl, win), n):
        if None in (adx[i], atr[i], ema[i], up[i], up[i - 1], atr_pct[i - win + 1]):
            continue
        hist = sorted(atr_pct[i - win + 1:i + 1])
        if atr_pct[i] > hist[len(hist) // 2] * p["vol_max"]:
            continue  # буря
        a = atr[i]
        if adx[i] >= p["trend_adx"]:
            hi, lo = max(h[i - bl:i]), min(l[i - bl:i])
            if c[i] > hi and c[i] > ema[i]:
                risk = p["trend_atr"] * a
                out[i] = {"side": "long", "stop": c[i] - risk, "take": c[i] + p["trend_rr"] * risk,
                          "why": f"тренд (ADX {adx[i]:.0f}): пробой максимума {hi:g}"}
            elif c[i] < lo and c[i] < ema[i]:
                risk = p["trend_atr"] * a
                out[i] = {"side": "short", "stop": c[i] + risk, "take": c[i] - p["trend_rr"] * risk,
                          "why": f"тренд (ADX {adx[i]:.0f}): пробой минимума {lo:g}"}
        elif adx[i] <= p["range_adx"]:
            if c[i - 1] < low[i - 1] and low[i] < c[i] < mid[i]:
                out[i] = {"side": "long", "stop": c[i] - p["range_atr"] * a, "take": mid[i],
                          "why": f"боковик (ADX {adx[i]:.0f}): вернулась в полосы снизу"}
            elif c[i - 1] > up[i - 1] and mid[i] < c[i] < up[i]:
                out[i] = {"side": "short", "stop": c[i] + p["range_atr"] * a, "take": mid[i],
                          "why": f"боковик (ADX {adx[i]:.0f}): вернулась в полосы сверху"}
    return out
