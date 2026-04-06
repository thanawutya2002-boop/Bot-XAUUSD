from __future__ import annotations

from dataclasses import dataclass
import math
import time
from typing import Optional, Iterable

import MetaTrader5 as mt5

from data import info, warn, error, trade, touch, get_initial_balance

def _pick_filling_mode(symbol: str) -> int:
    """เลือก type_filling ให้เข้ากับโบรก/สัญลักษณ์ (กัน ret=10030 INVALID_FILL)"""
    info_ = mt5.symbol_info(symbol)
    if info_ is None:
        return mt5.ORDER_FILLING_RETURN

    fm = int(getattr(info_, "filling_mode", 0) or 0)

    # Prefer explicit SYMBOL_FILLING_* constants if available
    if hasattr(mt5, "SYMBOL_FILLING_FOK") and hasattr(mt5, "SYMBOL_FILLING_IOC") and hasattr(mt5, "SYMBOL_FILLING_RETURN"):
        if fm == mt5.SYMBOL_FILLING_FOK:
            return mt5.ORDER_FILLING_FOK
        if fm == mt5.SYMBOL_FILLING_IOC:
            return mt5.ORDER_FILLING_IOC
        if fm == mt5.SYMBOL_FILLING_RETURN:
            return mt5.ORDER_FILLING_RETURN

    # Fallback mapping (common in many builds): 0=FOK, 1=IOC, 2=RETURN
    if fm == 1:
        return mt5.ORDER_FILLING_IOC
    if fm == 2:
        return mt5.ORDER_FILLING_RETURN
    return mt5.ORDER_FILLING_FOK


# =======================
# Psych-level utilities
# =======================

@dataclass
class PsychConfig:
    # ระดับจิตวิทยา (ตัวท้ายของเลขเต็ม 1000)
    psych_suffixes: tuple[int, ...] = (0, 350, 500, 550, 750, 450, 950)
    # TP ต้องตั้ง "ก่อนถึง" psych ถัดไป กี่ points
    tp_buffer_points: int = 200
    # ถ้า entry ชน psych ให้ขยับหนี กี่ points
    entry_psych_buffer_points: int = 30


def is_psych_level(price: float, cfg: PsychConfig) -> bool:
    ip = math.floor(price)
    mod = ip % 1000
    return mod in cfg.psych_suffixes


def next_psych(price: float, dir_: int, cfg: PsychConfig) -> float | None:
    """หา psych ถัดไปในทิศทาง dir_ (+1 ขึ้น, -1 ลง)"""
    base = math.floor(price / 1000.0) * 1000
    best = None
    best_dist = 1e18

    for k in (-1, 0, 1, 2):
        block = base + k * 1000
        for suf in cfg.psych_suffixes:
            lvl = float(block + suf)
            if dir_ > 0 and lvl <= price:
                continue
            if dir_ < 0 and lvl >= price:
                continue
            d = abs(lvl - price)
            if d < best_dist:
                best_dist = d
                best = lvl
    return best


def calc_tp(entry: float, dir_: int, point: float, cfg: PsychConfig) -> float | None:
    """TP แบบอิง psych ถัดไป (ตั้งก่อนถึง psych ด้วย buffer)"""
    psy = next_psych(entry, dir_, cfg)
    if psy is None:
        return None
    if dir_ > 0:
        return psy - cfg.tp_buffer_points * point
    return psy + cfg.tp_buffer_points * point


def nudge_away_from_psych(level: float, is_buy_level: bool, point: float, cfg: PsychConfig) -> float:
    """ถ้าราคาไปชน psych ให้ขยับหนีเล็กน้อย"""
    if not is_psych_level(level, cfg):
        return level
    shift = cfg.entry_psych_buffer_points * point
    return (level - shift) if is_buy_level else (level + shift)


# =======================
# Trade safety helpers
# (ไม่ตั้ง SL ที่โบรก แต่มี Emergency close แบบบอทจัดการเอง)
# =======================

def _order_send_with_retry(req: dict, retries: int = 3, sleep_s: float = 0.15):
    """ส่งออเดอร์แบบ retry เฉพาะ error ชั่วคราว (requote/price changed/off quotes/timeout)."""
    # retcodes vary by build/broker, so use getattr fallback
    transient = set([
        getattr(mt5, "TRADE_RETCODE_REQUOTE", 10004),
        getattr(mt5, "TRADE_RETCODE_PRICE_CHANGED", 10020),
        getattr(mt5, "TRADE_RETCODE_OFF_QUOTES", 10021),
        getattr(mt5, "TRADE_RETCODE_TIMEOUT", 10012),
        getattr(mt5, "TRADE_RETCODE_CONNECTION", 10006),
    ])

    last = None
    for i in range(max(1, int(retries))):
        last = mt5.order_send(req)
        if last is None:
            # Sometimes last_error gives clue; treat as transient once.
            if i < retries - 1:
                time.sleep(sleep_s)
                continue
            return None
        if last.retcode == mt5.TRADE_RETCODE_DONE:
            return last
        if last.retcode in transient and i < retries - 1:
            time.sleep(sleep_s)
            continue
        return last
    return last


def _iter_positions(symbol: str, magic: int | None):
    positions = mt5.positions_get(symbol=symbol)
    if not positions:
        return []
    if magic is None:
        return list(positions)
    return [p for p in positions if int(getattr(p, "magic", 0) or 0) == int(magic)]


def _iter_orders(symbol: str, magic: int | None):
    orders = mt5.orders_get(symbol=symbol)
    if not orders:
        return []
    if magic is None:
        return list(orders)
    return [o for o in orders if int(getattr(o, "magic", 0) or 0) == int(magic)]


def cancel_pending_by_magic(symbol: str, magic: int | None) -> int:
    """ยกเลิก pending (BUY_LIMIT/SELL_LIMIT) ของบอท"""
    n = 0
    for o in _iter_orders(symbol, magic):
        if int(o.type) not in (mt5.ORDER_TYPE_BUY_LIMIT, mt5.ORDER_TYPE_SELL_LIMIT):
            continue
        req = {"action": mt5.TRADE_ACTION_REMOVE, "order": int(o.ticket)}
        res = _order_send_with_retry(req, retries=2)
        if res and res.retcode == mt5.TRADE_RETCODE_DONE:
            n += 1
    if n:
        touch(f"Canceled pending {n}")
    return n


def _close_position_market(symbol: str, position, deviation: int = 60) -> bool:
    """ปิด position ด้วย market order ฝั่งตรงข้าม"""
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return False

    volume = float(position.volume)
    pos_type = int(position.type)

    # BUY -> close by SELL at bid
    if pos_type == mt5.POSITION_TYPE_BUY:
        order_type = mt5.ORDER_TYPE_SELL
        price = float(tick.bid)
    else:
        order_type = mt5.ORDER_TYPE_BUY
        price = float(tick.ask)

    req = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": volume,
        "type": order_type,
        "position": int(position.ticket),
        "price": price,
        "deviation": int(deviation),
        "magic": int(position.magic),
        "comment": "BOT_EMERGENCY_CLOSE",
        "type_filling": _pick_filling_mode(symbol),
    }
    res = _order_send_with_retry(req, retries=3)
    return bool(res and res.retcode == mt5.TRADE_RETCODE_DONE)


def close_positions_by_magic(symbol: str, magic: int | None, reason: str = "BOT_CLOSE") -> int:
    """ปิดโพซิชันของบอท (กรองด้วย magic ถ้ามี)"""
    n = 0
    for p in _iter_positions(symbol, magic):
        ok = _close_position_market(symbol, p)
        if ok:
            n += 1
            trade(f"Closed position ticket={p.ticket} ({reason})")
        else:
            error(f"Failed to close ticket={p.ticket} ({reason})")
    if n:
        touch(f"Closed {n} positions ({reason})")
    return n


def close_all_and_cancel_pending(symbol: str, magic: int | None, reason: str) -> None:
    cancel_pending_by_magic(symbol, magic)
    close_positions_by_magic(symbol, magic, reason=reason)


# =======================
# Account Guard (ไม่ตั้ง SL ที่โบรก)
# =======================

def account_guard_close_if_loss_over(symbol: str, loss_percent: float = 30.0, magic: int | None = None) -> bool:
    """ถ้า Equity ลดลงเกิน loss_percent% จาก balance ตอนเริ่มรันบอท -> ปิดโพซิชัน + ยกเลิก pending"""
    acc = mt5.account_info()
    if acc is None:
        return False

    initial = get_initial_balance()
    if initial is None or initial <= 0:
        return False

    equity = float(acc.equity)
    loss_pct = (initial - equity) / initial * 100.0

    if loss_pct < loss_percent:
        return False

    warn(f"ACCOUNT GUARD TRIGGERED: loss={loss_pct:.2f}% >= {loss_percent:.2f}% -> closing bot exposure")
    touch("Account guard triggered")

    close_all_and_cancel_pending(symbol, magic, reason="ACCOUNT_GUARD")
    return True


def close_position_ticket(symbol: str, ticket: int, reason: str = "BOT_CLOSE") -> bool:
    """ปิดโพซิชันตาม ticket (ตัวเดียว)"""
    positions = mt5.positions_get(symbol=symbol)
    if not positions:
        return False
    target = None
    for p in positions:
        if int(getattr(p, "ticket", 0) or 0) == int(ticket):
            target = p
            break
    if target is None:
        return False
    ok = _close_position_market(symbol, target)
    if ok:
        trade(f"Closed position ticket={ticket} ({reason})")
        touch(f"Closed {ticket} ({reason})")
    else:
        error(f"Failed to close ticket={ticket} ({reason})")
    return ok
