"""Индикаторы без внешних библиотек.

Значение с индексом i зависит только от свечей 0..i, поэтому один и тот же
массив можно безопасно использовать и в бэктесте (без заглядывания в будущее).
Там, где значения ещё нет (мало свечей), стоит None. Ряды с None в начале
(например, линия MACD) можно передавать на вход другим индикаторам.
"""
from typing import List, Optional, Tuple

Series = List[Optional[float]]


def _first(values: Series) -> int:
    return next((i for i, v in enumerate(values) if v is not None), len(values))


def sma(values: Series, period: int) -> Series:
    out: Series = [None] * len(values)
    start = _first(values)
    s = 0.0
    for i in range(start, len(values)):
        s += values[i]
        if i - start >= period:
            s -= values[i - period]
        if i - start >= period - 1:
            out[i] = s / period
    return out


def ema(values: Series, period: int) -> Series:
    out: Series = [None] * len(values)
    start = _first(values)
    if period < 1 or len(values) - start < period:
        return out
    k = 2 / (period + 1)
    s = sum(values[start:start + period]) / period
    out[start + period - 1] = s
    for i in range(start + period, len(values)):
        s = values[i] * k + s * (1 - k)
        out[i] = s
    return out


def _rsi_value(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)


def rsi(values: List[float], period: int) -> Series:
    """RSI по Уайлдеру (как в TradingView)."""
    out: Series = [None] * len(values)
    if period < 1 or len(values) <= period:
        return out
    gains = losses = 0.0
    for i in range(1, period + 1):
        d = values[i] - values[i - 1]
        if d > 0:
            gains += d
        else:
            losses -= d
    avg_gain, avg_loss = gains / period, losses / period
    out[period] = _rsi_value(avg_gain, avg_loss)
    for i in range(period + 1, len(values)):
        d = values[i] - values[i - 1]
        gain = d if d > 0 else 0.0
        loss = -d if d < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        out[i] = _rsi_value(avg_gain, avg_loss)
    return out


def bollinger(values: List[float], period: int, mult: float) -> Tuple[Series, Series, Series]:
    """Полосы Боллинджера: средняя ± mult стандартных отклонений (как в TradingView)."""
    mid = sma(values, period)
    up: Series = [None] * len(values)
    low: Series = [None] * len(values)
    for i in range(period - 1, len(values)):
        m = mid[i]
        # считаем по окну напрямую: через сумму квадратов на ценах вроде 80 000 теряется точность
        sd = (sum((v - m) ** 2 for v in values[i - period + 1:i + 1]) / period) ** 0.5
        up[i], low[i] = m + mult * sd, m - mult * sd
    return up, mid, low


def macd(values: List[float], fast: int, slow: int, signal: int) -> Tuple[Series, Series, Series]:
    """Линия MACD, сигнальная линия и гистограмма."""
    f, s = ema(values, fast), ema(values, slow)
    line: Series = [None if a is None or b is None else a - b for a, b in zip(f, s)]
    sig = ema(line, signal)
    hist: Series = [None if a is None or b is None else a - b for a, b in zip(line, sig)]
    return line, sig, hist


def stochastic(high: List[float], low: List[float], close: List[float],
               k: int, d: int, smooth: int) -> Tuple[Series, Series]:
    """Стохастик: %K (сглаженный) и %D, как ta.stoch в TradingView."""
    raw: Series = [None] * len(close)
    for i in range(k - 1, len(close)):
        hh, ll = max(high[i - k + 1:i + 1]), min(low[i - k + 1:i + 1])
        raw[i] = 100 * (close[i] - ll) / (hh - ll) if hh > ll else 50.0
    k_line = sma(raw, smooth)
    return k_line, sma(k_line, d)


def change(values: List[float], n: int) -> Series:
    """Изменение цены за n свечей, %."""
    return [None if i < n or not values[i - n] else (values[i] / values[i - n] - 1) * 100
            for i in range(len(values))]


def volume_ratio(volume: List[float], n: int) -> Series:
    """Объём свечи, делённый на средний объём n предыдущих свечей."""
    out: Series = [None] * len(volume)
    s = sum(volume[:n])
    for i in range(n, len(volume)):
        if s > 0:
            out[i] = volume[i] * n / s
        s += volume[i] - volume[i - n]
    return out
