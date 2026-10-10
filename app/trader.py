"""Логика сделки: вход, усреднения, тейк-профит, стоп-лосс, выход.

Один и тот же класс работает и в живом боте, и в бэктесте — поэтому
результаты проверки на истории соответствуют тому, что бот делает вживую.
"""
import math
import secrets
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Optional

from .strategy import (describe_signal, exit_reason, indicator_snapshot, signal_at,
                       signal_levels, so_deviation_pct)


@dataclass
class Fill:
    amount: float   # в базовой валюте (BTC и т.п.), уже за вычетом комиссии
    price: float    # средняя цена исполнения
    fee: float      # комиссия в USDT
    unsold: float = 0.0  # продажа на споте: монеты, оставшиеся на счёте (меньше шага лота)


class OrderStatusUnknown(RuntimeError):
    """Ордер ушёл на биржу, но ответа нет (таймаут, обрыв связи): он мог исполниться."""


class StopRejected(RuntimeError):
    """Биржа не приняла защитный стоп. immediate — цена уже за уровнем стопа."""

    def __init__(self, msg: str, immediate: bool = False):
        super().__init__(msg)
        self.immediate = immediate


STOP_RETRY_MS = 5 * 60_000  # не поставился стоп — следующая попытка через 5 минут


def merge_fills(a: Fill, b: Fill) -> Fill:
    """Одна продажа из двух частей: часть продал стоп на бирже, остаток — бот по рынку."""
    amount = a.amount + b.amount
    return Fill(amount=amount, price=(a.amount * a.price + b.amount * b.price) / amount,
                fee=a.fee + b.fee, unsold=b.unsold)


class Trader:
    def __init__(self, bot: Dict[str, Any], broker, state: Dict[str, Any],
                 record_trade: Callable[[Dict[str, Any]], Awaitable[None]],
                 log: Callable[[str], None],
                 persist: Callable[[], None] = lambda: None,
                 alert: Callable[[str], None] = lambda msg: None):
        self.bot = bot
        self.p = bot["params"]
        self.market = bot["market"]
        self.broker = broker
        self.state = state
        self.record_trade = record_trade
        self.log = log
        self.persist = persist  # сохранить state в базу
        self.alert = alert      # тревога: в журнал и в Telegram

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

    # Сигнал-скрипт может задать стоп и тейк ценой для конкретной сделки (pos["sl_price"],
    # pos["tp_price"]) — тогда они главнее процентов из настроек.

    def has_tp(self, pos: Dict[str, Any]) -> bool:
        return self.p["take_profit_pct"] > 0 or bool(pos.get("tp_price"))

    def has_sl(self, pos: Dict[str, Any]) -> bool:
        return self.p["stop_loss_pct"] > 0 or bool(pos.get("sl_price"))

    def tp_level(self, pos: Dict[str, Any]) -> float:
        if pos.get("tp_price"):
            return pos["tp_price"]
        tp = self.p["take_profit_pct"] / 100
        return pos["avg"] * (1 + tp) if pos["side"] == "long" else pos["avg"] * (1 - tp)

    def sl_level(self, pos: Dict[str, Any]) -> float:
        if pos.get("sl_price"):
            return pos["sl_price"]
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
        why = describe_signal(ind, i, self.p, sig) if sig else ""
        pos = self.pos
        if pos:
            side = pos["side"]
            reason = exit_reason(ind, i, self.p, side)
            if not reason and sig and sig != side and self.p["exit_on_opposite"]:
                reason = f"обратный сигнал: {why}"
            if reason:
                await self.close(price, reason, ts)
                pos = None
                if sig == side:  # условия входа и выхода совпали — не заходим снова на той же свече
                    return
        if not pos and sig and self._allowed(sig):
            await self.open(sig, price, ts, why, signal_levels(ind, i, self.p))

    async def on_price(self, price: float, ts: int, sim: bool = False) -> None:
        """Проверка стопа, усреднений и тейка. sim=True — исполнение по уровню (бэктест)."""
        pos = self.pos
        if not pos:
            return
        long = pos["side"] == "long"

        if self.has_sl(pos):
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

        if self.has_tp(pos):
            lvl = self.tp_level(pos)
            if price >= lvl if long else price <= lvl:
                await self.close(lvl if sim else price, "тейк-профит", ts)

    # ---------- действия ----------

    async def open(self, side: str, price: float, ts: int, reason: str = "сигнал",
                   levels: Optional[Dict[str, float]] = None) -> None:
        notional, margin = self._size(0)
        order = {"action": "open", "side": side, "margin": margin, "ts": ts, "reason": reason}
        if levels:
            order["levels"] = levels  # стоп и тейк от сигнала-скрипта
        fill = await self._send(order, lambda cid: self.broker.open(side, notional, price, cid))
        await self._apply(order, fill)

    async def add(self, k: int, price: float, ts: int) -> None:
        pos = self.pos
        notional, margin = self._size(k)
        # Номер усреднения сохраняем до ордера: если биржа его отклонит,
        # бот не будет пытаться купить это усреднение на каждой проверке.
        pos["so_filled"] = k
        order = {"action": "safety", "side": pos["side"], "k": k, "margin": margin, "ts": ts,
                 "reason": f"усреднение {k}"}
        fill = await self._send(order, lambda cid: self.broker.open(pos["side"], notional, price, cid))
        await self._apply(order, fill)

    def _with_dust(self, pos: Dict[str, Any]):
        """Объём и стоимость позиции вместе с остатком монет от прошлых продаж."""
        dust = self.state.get("dust") or {"qty": 0.0, "cost": 0.0}
        return pos["qty"] + dust["qty"], pos["cost"] + dust["cost"]

    async def close(self, price: float, reason: str, ts: int, part: Optional[Fill] = None) -> None:
        """Закрыть позицию по рынку. part — то, что уже продал стоп на бирже."""
        pos = self.pos
        if pos.get("stop"):
            # Стоп на бирже держит монеты и может сработать одновременно с нами — снимаем его первым.
            released = await self._release_stop(price, ts)
            if released is False:
                return  # позицию уже закрыл стоп на бирже, сделка записана
            part = released or part
        qty, _ = self._with_dust(pos)
        if part:
            qty = max(qty - part.amount, 0.0)
        order = {"action": "close", "side": pos["side"], "ts": ts, "reason": reason}
        try:
            fill = await self._send(order, lambda cid: self.broker.close(pos["side"], qty, price, cid))
        except ValueError:
            if not part:
                raise
            fill = None  # остаток меньше минимального ордера — останется на счёте
        if part:
            fill = merge_fills(part, fill) if fill else Fill(part.amount, part.price, part.fee, unsold=qty)
        await self._apply(order, fill)

    # ---------- стоп на бирже ----------
    # Стоп-лосс внутри программы работает, только пока она запущена и на связи. Поэтому на
    # демо-счёте и реальных деньгах тот же уровень ставится ещё и стоп-ордером на Binance:
    # он сработает, даже если компьютер выключен. Уровень считается от средней цены, так что
    # после каждого усреднения стоп переставляется на новый уровень и на весь объём.

    def _wants_exchange_stop(self, pos: Dict[str, Any]) -> bool:
        return (self.has_sl(pos) and self.p.get("exchange_stop", True)
                and getattr(self.broker, "exchange_stops", False))

    async def sync_stop(self, price: float, now: int) -> None:
        """Держит стоп на бирже в соответствии с позицией и настройками."""
        pos = self.pos
        if not pos or self.state.get("pending"):
            return
        st = pos.get("stop")
        if not self._wants_exchange_stop(pos):
            if st:
                await self._drop_stop(price, now, "выключен в настройках")
            return
        level = self.sl_level(pos)
        qty, _ = self._with_dust(pos)
        if st and math.isclose(st["level"], level, rel_tol=1e-9) and math.isclose(st["want"], qty, rel_tol=1e-9):
            return
        if st and not await self._drop_stop(price, now, "переставляю на новый уровень"):
            return
        if now < pos.get("stop_retry_at", 0):
            return
        cid = "bs" + secrets.token_hex(10)  # свой ID: если ответ потеряется, стоп найдётся по нему
        try:
            ref = await self.broker.place_stop(pos["side"], qty, level, cid)
        except OrderStatusUnknown:
            pos["stop"] = {"id": None, "cid": cid, "price": level, "qty": qty, "level": level, "want": qty}
            self.persist()
            return
        except StopRejected as e:
            pos["stop_retry_at"] = now + STOP_RETRY_MS
            if not e.immediate and pos.get("stop_error") != str(e):
                self.alert(f"Не удалось поставить стоп на бирже: {e}. "
                           f"Пока бот запущен, стоп работает внутри программы.")
            pos["stop_error"] = str(e)
            self.persist()
            return
        pos["stop"] = {**ref, "level": level, "want": qty}
        pos.pop("stop_error", None)
        pos.pop("stop_retry_at", None)
        self.persist()
        self.log(f"Стоп на бирже: {ref['qty']:.6g} по {ref['price']:.6g}")

    async def _drop_stop(self, price: float, now: int, why: str) -> bool:
        """Снять стоп с биржи. False — стоп за это время успел сработать, позиция закрыта."""
        released = await self._release_stop(price, now)
        if released is False:
            return False
        if released:  # стоп-лимит успел продать часть — продаём остальное
            await self.close(price, "стоп-лосс на бирже", now, part=released)
            return False
        self.log(f"Стоп на бирже снят: {why}")
        return True

    async def _release_stop(self, price: float, ts: int):
        """Снимает стоп перед закрытием позиции. Возвращает None — стоп снят; Fill — стоп-лимит
        успел продать часть; False — стоп уже сработал, и сделка записана."""
        pos, st = self.pos, self.pos["stop"]
        try:
            part = await self.broker.cancel_stop(st)
        except Exception:
            status, fill = await self.broker.check_stop(st, price)
            if status == "filled":
                await self._stop_filled(fill, ts)
                return False
            if status != "gone":  # стоп ещё на бирже или исполняется — продавать нельзя
                raise
            part = fill
        pos["stop"] = None
        self.persist()
        return part

    async def check_stop(self, price: float, ts: int) -> None:
        """Проверка стопа на бирже на каждом шаге: сработал — записываем сделку,
        пропал — поставим заново, стоп-лимит застрял — продаём остаток по рынку."""
        pos = self.pos
        st = pos.get("stop") if pos else None
        if not st:
            return
        status, fill = await self.broker.check_stop(st, price)
        if status == "filled":
            await self._stop_filled(fill, ts)
        elif status == "stuck":
            await self.close(price, "стоп-лосс на бирже", ts)
        elif status == "gone":
            pos["stop"] = None
            self.persist()
            if fill:
                await self.close(price, "стоп-лосс на бирже", ts, part=fill)
            else:
                self.log("Стоп на бирже снят или истёк — поставлю заново")

    async def _stop_filled(self, fill: Fill, ts: int) -> None:
        pos = self.pos
        if self.market == "spot":
            qty, _ = self._with_dust(pos)
            fill.unsold = max(qty - fill.amount, 0.0)
        pos["stop"] = None
        order = {"action": "close", "side": pos["side"], "ts": ts, "reason": "стоп-лосс на бирже"}
        self.state["pending"] = None
        await self._apply(order, fill)

    # ---------- ордер «в пути» ----------

    async def _send(self, order: Dict[str, Any],
                    place: Callable[[str], Awaitable[Fill]]) -> Fill:
        """Записывает ордер в базу как «в пути» и только потом отправляет на биржу.

        Если ответ биржи потерялся или программа упала посреди отправки, запись
        остаётся, и resolve_pending() находит ордер на бирже по его ID. Так бот не
        откроет сделку второй раз и не забудет о купленном.
        """
        order["cid"] = "bb" + secrets.token_hex(10)  # Binance принимает до 36 символов
        self.state["pending"] = order
        self.persist()
        try:
            return await place(order["cid"])
        except OrderStatusUnknown:
            raise  # запись остаётся, ордер проверим на следующем шаге
        except Exception:
            self.state["pending"] = None  # биржа ордер точно не приняла
            raise

    async def resolve_pending(self) -> None:
        """Доводит ордер, ответ на который не дошёл. Пока биржа не ответила,
        выбрасывает исключение, и бот ничего больше не делает."""
        order = self.state.get("pending")
        if not order:
            return
        fill = await self.broker.find(order["cid"])
        if fill is None:
            self.state["pending"] = None
            self.persist()
            self.log(f"Ордер «{order['reason']}» на бирже не найден — он не прошёл")
            return
        self.log(f"Ордер «{order['reason']}» найден на бирже, учитываю сделку")
        await self._apply(order, fill)

    async def _apply(self, order: Dict[str, Any], fill: Fill) -> None:
        side, ts, reason = order["side"], order["ts"], order["reason"]
        trade = {"ts": ts, "action": order["action"], "side": side, "price": fill.price,
                 "amount": fill.amount, "fee": fill.fee, "pnl": None, "reason": reason}
        if order["action"] == "open":
            self.state["position"] = {
                "side": side, "qty": fill.amount, "cost": fill.amount * fill.price,
                "avg": fill.price, "first_price": fill.price, "so_filled": 0,
                "fees": fill.fee, "margin": order["margin"], "opened_ts": ts,
            }
            lv = order.get("levels") or {}
            if lv.get("stop"):
                self.state["position"]["sl_price"] = lv["stop"]
            if lv.get("take"):
                self.state["position"]["tp_price"] = lv["take"]
            msg = (f"Вход в {'лонг' if side == 'long' else 'шорт'}: "
                   f"{fill.amount:.6g} по {fill.price:.6g}")
        elif order["action"] == "safety":
            pos = self.pos
            pos["qty"] += fill.amount
            pos["cost"] += fill.amount * fill.price
            pos["avg"] = pos["cost"] / pos["qty"]
            pos["fees"] += fill.fee
            pos["margin"] += order["margin"]
            msg = (f"Усреднение {order['k']}: {fill.amount:.6g} по {fill.price:.6g}, "
                   f"средняя {pos['avg']:.6g}")
        else:
            pos = self.pos
            qty, cost = self._with_dust(pos)
            # Непроданный остаток уносит свою долю стоимости в следующую сделку,
            # вместе с которой он и продастся. Иначе каждая продажа на споте
            # записывалась бы в минус на стоимость этого остатка.
            left = min(fill.unsold, qty)
            left_cost = cost * left / qty if qty else 0.0
            self.state["dust"] = {"qty": left, "cost": left_cost} if left > 0 else None
            proceeds = fill.amount * fill.price
            charged = cost - left_cost
            gross = proceeds - charged if side == "long" else charged - proceeds
            pnl = gross - pos["fees"] - fill.fee
            self.state["position"] = None
            stats = self.state.setdefault("stats", {"closed": 0, "wins": 0, "pnl": 0.0, "fees": 0.0})
            stats["closed"] += 1
            stats["wins"] += 1 if pnl > 0 else 0
            stats["pnl"] += pnl
            stats["fees"] += pos["fees"] + fill.fee
            trade.update(pnl=pnl, pnl_pct=pnl / pos["margin"] * 100 if pos["margin"] else None,
                         opened_ts=pos["opened_ts"], so_used=pos["so_filled"], avg=pos["avg"])
            msg = (f"Выход ({reason}): {fill.amount:.6g} по {fill.price:.6g}, "
                   f"результат {pnl:+.2f} USDT")
            if left > 0:
                msg += (f". На счёте осталось {left:.6g} — меньше шага лота, "
                        f"продам вместе со следующей сделкой")
        self.state["pending"] = None
        await self.record_trade(trade)  # сохраняет и state
        self.log(msg)
