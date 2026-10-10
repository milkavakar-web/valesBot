"""Micro-Rebound Hunter — «охотник за микровозвратами».

Ищет резкую свечу, которая закрылась у самого края своего диапазона (у минимума — после
пролива, у максимума — после выноса вверх), и ставит на откат в обратную сторону.
Индикаторов нет, только форма свечи.

1. Размах свечи (high − low) не меньше range_pct процентов.
2. Закрытие в нижних zone_pct процентах размаха — лонг, в верхних — шорт.
3. Если confirm = 1, ждём ещё одну свечу: она не должна обновить минимум (для лонга)
   или максимум (для шорта). Это «подтверждение слабости» из описания стратегии.
4. Тейк — take_pct процентов от цены, стоп — за экстремумом резкой свечи
   (плюс отступ stop_pad_pct).

Шорт возможен только на фьючерсах: на споте бот торгует только в лонг.
"""

NAME = "Micro-Rebound Hunter"
ABOUT = ("Откат после резкой свечи, закрывшейся у края диапазона. Стоп за экстремумом "
         "этой свечи, тейк фиксированный. Рассчитан на младшие таймфреймы (1m–5m).")

PARAMS = {
    "range_pct": 0.5,     # минимальный размах свечи, % от минимума
    "zone_pct": 20,       # закрытие в крайних N% размаха
    "confirm": 1,         # 1 — ждать подтверждения следующей свечой, 0 — входить сразу
    "take_pct": 0.25,     # тейк от цены сигнала, %
    "stop_pad_pct": 0.0,  # отступ стопа за экстремум, %
}

WARMUP = 3
LEVELS = True  # стоп и тейк задаёт сам сигнал


def signal(candles, p):
    confirm = p["confirm"] >= 1
    bar = candles[-2] if confirm else candles[-1]  # резкая свеча
    now = candles[-1]                               # последняя закрытая: по ней входим
    rng = bar.high - bar.low
    if rng <= 0:
        return None
    rng_pct = rng / bar.low * 100
    if rng_pct < p["range_pct"]:
        return None
    zone = rng * p["zone_pct"] / 100
    pad = p["stop_pad_pct"] / 100
    take = p["take_pct"] / 100

    if bar.close <= bar.low + zone:  # закрылась у минимума — ждём отскок вверх
        if confirm and now.low < bar.low:
            return None  # следующая свеча продавила минимум — слабость не подтвердилась
        stop = bar.low * (1 - pad)
        if stop >= now.close:
            return None
        return {"side": "long", "stop": stop, "take": now.close * (1 + take),
                "why": f"свеча {rng_pct:.2f}% закрылась у минимума {bar.low:g}"}

    if bar.close >= bar.high - zone:  # закрылась у максимума — ждём откат вниз
        if confirm and now.high > bar.high:
            return None
        stop = bar.high * (1 + pad)
        if stop <= now.close:
            return None
        return {"side": "short", "stop": stop, "take": now.close * (1 - take),
                "why": f"свеча {rng_pct:.2f}% закрылась у максимума {bar.high:g}"}
    return None
