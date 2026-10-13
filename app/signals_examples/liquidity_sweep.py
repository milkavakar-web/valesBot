"""Liquidity Sweep + EMA 200 + ATR — снятие ликвидности по тренду.

По описанию «Crypto Institutional Liquidity Sweep Strategy» (TradingView, февраль 2026).
Исходный код на странице не опубликован, поэтому длины и множители — наши.

Лонг (шорт — зеркально, только на фьючерсах):
1. Тренд вверх: закрытие выше EMA(ema_len), с запасом ema_buffer_pct процентов.
2. Ликвидность — последний подтверждённый минимум свинга (pivot low: ниже swing свечей
   слева и справа), не старше lookback свечей. Под ним стоят стопы.
3. Снятие: свеча проколола этот минимум (low ниже) и закрылась обратно выше него.
4. Разворотная свеча: зелёная и закрылась в верхних close_zone процентах своего диапазона.
5. Стоп — под фитилём снятия на atr_pad ATR, тейк — rr стопов (по умолчанию 1:2).
   Уровни фиксируются при входе и не двигаются.
Каждый минимум используется один раз.
"""
from app import indicators as ta

NAME = "Liquidity Sweep + EMA 200 + ATR"
ABOUT = ("Цена прокалывает недавний минимум свинга, собирает стопы и закрывается обратно выше — "
         "вход по тренду EMA 200. Стоп за фитилём на долю ATR, тейк 1:2. Лучше на 1h–4h.")

PARAMS = {
    "ema_len": 200,         # EMA тренда
    "ema_buffer_pct": 0.0,  # запас от EMA, %
    "swing": 5,             # свечей слева и справа у минимума/максимума свинга
    "lookback": 50,         # насколько старый свинг ещё считается ликвидностью, свечей
    "close_zone": 40,       # закрытие в верхних (для шорта — нижних) N% диапазона свечи
    "atr_len": 14,          # период ATR
    "atr_pad": 0.5,         # стоп за фитилём на столько ATR
    "rr": 2.0,              # тейк в стопах (риск к прибыли)
}
WARMUP = 220
LEVELS = True


def signals(candles, p):
    o = [c.open for c in candles]
    h = [c.high for c in candles]
    l = [c.low for c in candles]
    c = [c.close for c in candles]
    n = len(c)
    ema = ta.ema(c, int(p["ema_len"]))
    atr = ta.atr(h, l, c, int(p["atr_len"]))
    sw = int(p["swing"])
    lows, highs = ta.pivots(l, sw, sw, high=False), ta.pivots(h, sw, sw, high=True)
    buf = p["ema_buffer_pct"] / 100
    zone = p["close_zone"] / 100
    out = [None] * n
    last_low = last_high = None  # (индекс, цена) последнего подтверждённого свинга
    for i in range(n):
        # свинг, подтверждённый на свече i, становится ликвидностью со следующей свечи
        if ema[i] is not None and atr[i] is not None and i > 0:
            rng = h[i] - l[i]
            if last_low and rng > 0 and i - last_low[0] <= p["lookback"]:
                lvl = last_low[1]
                if (c[i] > ema[i] * (1 + buf) and l[i] < lvl < c[i] and c[i] > o[i]
                        and c[i] >= h[i] - zone * rng):
                    stop = l[i] - p["atr_pad"] * atr[i]
                    out[i] = {"side": "long", "stop": stop, "take": c[i] + p["rr"] * (c[i] - stop),
                              "why": f"снял минимум {lvl:g} и вернулся выше, EMA {ema[i]:.6g}"}
                    last_low = None  # уровень отработан
            if last_high and rng > 0 and i - last_high[0] <= p["lookback"] and not out[i]:
                lvl = last_high[1]
                if (c[i] < ema[i] * (1 - buf) and h[i] > lvl > c[i] and c[i] < o[i]
                        and c[i] <= l[i] + zone * rng):
                    stop = h[i] + p["atr_pad"] * atr[i]
                    out[i] = {"side": "short", "stop": stop, "take": c[i] - p["rr"] * (stop - c[i]),
                              "why": f"снял максимум {lvl:g} и вернулся ниже, EMA {ema[i]:.6g}"}
                    last_high = None
        # закрылись за уровнем — это пробой, а не снятие: уровень больше не ликвидность
        if last_low and c[i] < last_low[1]:
            last_low = None
        if last_high and c[i] > last_high[1]:
            last_high = None
        if lows[i]:
            last_low = lows[i]
        if highs[i]:
            last_high = highs[i]
    return out
