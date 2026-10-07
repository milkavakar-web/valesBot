"""Логика сделки: вход, усреднения, тейк-профит, стоп-лосс, выход.

Один и тот же класс работает и в живом боте, и в бэктесте — поэтому
результаты проверки на истории соответствуют тому, что бот делает вживую.
"""
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Optional

from .strategy import indicator_snapshot, signal_at, so_deviation_pct


@dataclass
class Fill:
    amount: float   # в базовой валюте (BTC и т.п.), уже за вычетом комиссии
    price: float    # средняя цена исполнения
    fee: float      # комиссия в USDT


class Trader:
    def __init__(self, bot: Dict[str, Any], broker, state: Dict[str, Any],
                 record_trade: Callable[[Dict[str, Any]], Awaitable[None]],
                 log: Callable[[str], None]):
        self.bot = bot
        self.p = bot["params"]
        self.market = bot["market"]
        self.broker = broker
        self.state = state
        self.record_trade = record_trade
        self.log = log

    # ---------- вспомогательное ----------

    @property
    def pos(self) -> Optional[Dict[str, Any]]:
        return self.state.get("position")

    def _allowed(self, side: str) -> bool:
        d = self.p["direction"]
        return d == "both" or d == side

    def _size(self, k: int):
        """Маржа и объём позиции (USDT) для ордера k: 0 — первый, 1.. — усреднения."""
        margin = self.p["order_size"] * self.p["so_volume_mult"] ** k
        lev = self.p["leverage"] if self.market == "futures" else 1
        return margin * lev, margin

    def so_level(self, pos: Dict[str, Any], k: int) -> float:
        dev = so_deviation_pct(self.p, k) / 100
        base = pos["first_price"]
        return base * (1 - dev) if pos["side"] == "long" else base * (1 + dev)

    def tp_level(self, pos: Dict[str, Any]) -> float:
        tp = self.p["take_profit_pct"] / 100
        return pos["avg"] * (1 + tp) if pos["side"] == "long" else pos["avg"] * (1 - tp)

    def sl_level(self, pos: Dict[str, Any]) -> float:
        sl = self.p["stop_loss_pct"] / 100
        return pos["avg"] * (1 - sl) if pos["side"] == "long" else pos["avg"] * (1 + sl)

    @staticmethod
    def unrealized(pos: Optional[Dict[str, Any]], price: float) -> float:
        if not pos:
            return 0.0
        value = pos["qty"] * price
        gross = value - pos["cost"] if pos["side"] == "long" else pos["cost"] - value
        return gross - pos["fees"]

    # ---------- события ----------

    async def on_candle(self, ind: Dict[str, list], i: int, price: float, ts: int) -> None:
        """Вызывается один раз на каждую закрытую свечу."""
        sig = signal_at(ind, i, self.p)
        self.state["last_signal"] = sig
        self.state["indicator"] = indicator_snapshot(ind, i, self.p)
        pos = self.pos
        if pos and sig and sig != pos["side"] and self.p["exit_on_opposite"]:
            await self.close(price, "противоположный сигнал", ts)
            pos = None
        if not pos and sig and self._allowed(sig):
            await self.open(sig, price, ts)

    async def on_price(self, price: float, ts: int, sim: bool = False) -> None:
        """Проверка стопа, усреднений и тейка. sim=True — исполнение по уровню (бэктест)."""
        pos = self.pos
        if not pos:
            return
        long = pos["side"] == "long"

        if self.p["stop_loss_pct"] > 0:
            lvl = self.sl_level(pos)
            if price <= lvl if long else price >= lvl:
                await self.close(lvl if sim else price, "стоп-лосс", ts)
                return

        while pos["so_filled"] < self.p["safety_orders"]:
            k = pos["so_filled"] + 1
            lvl = self.so_level(pos, k)
            if not (price <= lvl if long else price >= lvl):
                break
            await self.add(k, lvl if sim else price, ts)

        if self.p["take_profit_pct"] > 0:
            lvl = self.tp_level(pos)
            if price >= lvl if long else price <= lvl:
                await self.close(lvl if sim else price, "тейк-профит", ts)

    # ---------- действия ----------

    async def open(self, side: str, price: float, ts: int) -> None:
        notional, margin = self._size(0)
        fill: Fill = await self.broker.open(side, notional, price)
        self.state["position"] = {
            "side": side, "qty": fill.amount, "cost": fill.amount * fill.price,
            "avg": fill.price, "first_price": fill.price, "so_filled": 0,
            "fees": fill.fee, "margin": margin, "opened_ts": ts,
        }
        await self.record_trade({"ts": ts, "action": "open", "side": side, "price": fill.price,
                                 "amount": fill.amount, "fee": fill.fee, "pnl": None,
                                 "reason": "сигнал"})
        self.log(f"Вход в {'лонг' if side == 'long' else 'шорт'}: "
                 f"{fill.amount:.6g} по {fill.price:.6g}")

    async def add(self, k: int, price: float, ts: int) -> None:
        pos = self.pos
        notional, margin = self._size(k)
        # Номер усреднения сохраняем до ордера: при сбое бот не купит его повторно.
        pos["so_filled"] = k
        fill: Fill = await self.broker.open(pos["side"], notional, price)
        pos["qty"] += fill.amount
        pos["cost"] += fill.amount * fill.price
        pos["avg"] = pos["cost"] / pos["qty"]
        pos["fees"] += fill.fee
        pos["margin"] += margin
        await self.record_trade({"ts": ts, "action": "safety", "side": pos["side"],
                                 "price": fill.price, "amount": fill.amount, "fee": fill.fee,
                                 "pnl": None, "reason": f"усреднение {k}"})
        self.log(f"Усреднение {k}: {fill.amount:.6g} по {fill.price:.6g}, "
                 f"средняя {pos['avg']:.6g}")

    async def close(self, price: float, reason: str, ts: int) -> None:
        pos = self.pos
        fill: Fill = await self.broker.close(pos["side"], pos["qty"], price)
        proceeds = fill.amount * fill.price
        gross = proceeds - pos["cost"] if pos["side"] == "long" else pos["cost"] - proceeds
        pnl = gross - pos["fees"] - fill.fee
        self.state["position"] = None
        stats = self.state.setdefault("stats", {"closed": 0, "wins": 0, "pnl": 0.0, "fees": 0.0})
        stats["closed"] += 1
        stats["wins"] += 1 if pnl > 0 else 0
        stats["pnl"] += pnl
        stats["fees"] += pos["fees"] + fill.fee
        await self.record_trade({"ts": ts, "action": "close", "side": pos["side"],
                                 "price": fill.price, "amount": fill.amount, "fee": fill.fee,
                                 "pnl": pnl, "reason": reason,
                                 "pnl_pct": pnl / pos["margin"] * 100 if pos["margin"] else None,
                                 "opened_ts": pos["opened_ts"], "so_used": pos["so_filled"]})
        self.log(f"Выход ({reason}): {fill.amount:.6g} по {fill.price:.6g}, "
                 f"результат {pnl:+.2f} USDT")
