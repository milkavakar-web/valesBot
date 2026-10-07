"""Индикаторы без внешних библиотек.

Значение с индексом i зависит только от свечей 0..i, поэтому один и тот же
массив можно безопасно использовать и в бэктесте (без заглядывания в будущее).
"""
from typing import List, Optional


def ema(values: List[float], period: int) -> List[Optional[float]]:
    out: List[Optional[float]] = [None] * len(values)
    if period < 1 or len(values) < period:
        return out
    k = 2 / (period + 1)
    s = sum(values[:period]) / period
    out[period - 1] = s
    for i in range(period, len(values)):
        s = values[i] * k + s * (1 - k)
        out[i] = s
    return out


def _rsi_value(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)


def rsi(values: List[float], period: int) -> List[Optional[float]]:
    """RSI по Уайлдеру (как в TradingView)."""
    out: List[Optional[float]] = [None] * len(values)
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
