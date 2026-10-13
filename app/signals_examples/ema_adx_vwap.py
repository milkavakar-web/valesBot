"""EMA + ADX + VWAP + объём — вход по тренду, только когда тренд действительно есть.

Лонг (шорт — зеркально, только на фьючерсах), все условия на закрытой свече:
1. Направление: EMA(fast) выше EMA(slow).
2. Сила: ADX(adx_len) выше adx_min и +DI выше −DI — слабое движение отсекается.
3. Цена: закрытие выше VWAP с начала суток (UTC) — покупаем дороже «средней цены дня»
   только когда покупатели сильнее.
4. Объём: больше среднего за vol_len свечей в vol_mult раз.
Сигнал — на свече, где все условия впервые выполнились вместе (на предыдущей — нет).
Стоп — atr_mult ATR от цены, тейк — rr стопов.
"""
from app import indicators as ta

NAME = "EMA + ADX + VWAP + объём"
ABOUT = ("Вход по тренду: EMA быстрая выше медленной, ADX подтверждает силу, цена выше VWAP дня, "
         "объём выше среднего. Стоп по ATR, тейк 1:2. 15m–1h.")

PARAMS = {
    "fast": 20,        # быстрая EMA
    "slow": 50,        # медленная EMA
    "adx_len": 14,     # период ADX
    "adx_min": 25,     # ADX выше — тренд есть
    "vol_len": 20,     # средний объём за N свечей
    "vol_mult": 1.2,   # объём больше среднего в N раз
    "atr_len": 14,     # период ATR
    "atr_mult": 2.0,   # стоп на столько ATR от цены
    "rr": 2.0,         # тейк в стопах
}
WARMUP = 160
LEVELS = True


def signals(candles, p):
    t = [x.time for x in candles]
    h = [x.high for x in candles]
    l = [x.low for x in candles]
    c = [x.close for x in candles]
    v = [x.volume for x in candles]
    fast, slow = ta.ema(c, int(p["fast"])), ta.ema(c, int(p["slow"]))
    adx, pdi, mdi = ta.adx(h, l, c, int(p["adx_len"]))
    vw = ta.vwap(t, h, l, c, v)
    vr = ta.volume_ratio(v, int(p["vol_len"]))
    atr = ta.atr(h, l, c, int(p["atr_len"]))

    def side(i):
        if None in (fast[i], slow[i], adx[i], pdi[i], mdi[i], vr[i], atr[i]):
            return None
        if adx[i] < p["adx_min"] or vr[i] < p["vol_mult"]:
            return None
        if fast[i] > slow[i] and pdi[i] > mdi[i] and c[i] > vw[i]:
            return "long"
        if fast[i] < slow[i] and mdi[i] > pdi[i] and c[i] < vw[i]:
            return "short"
        return None

    out = [None] * len(c)
    prev = None
    for i in range(len(c)):
        s = side(i)
        if s and s != prev:
            risk = p["atr_mult"] * atr[i]
            sign = 1 if s == "long" else -1
            out[i] = {"side": s, "stop": c[i] - sign * risk, "take": c[i] + sign * p["rr"] * risk,
                      "why": f"ADX {adx[i]:.0f}, EMA{int(p['fast'])} {'выше' if s == 'long' else 'ниже'} "
                             f"EMA{int(p['slow'])}, VWAP {vw[i]:.6g}, объём ×{vr[i]:.1f}"}
        prev = s
    return out
