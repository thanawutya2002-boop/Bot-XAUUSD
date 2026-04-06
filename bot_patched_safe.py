# bot.py
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
import MetaTrader5 as mt5
import os

from law import TrendConfig, get_major_trend
from buy import OrderConfig, place_pending_by_trend, monitor_pending_not_touched
from exit import PsychConfig, is_psych_level, nudge_away_from_psych, close_position_ticket, cancel_pending_by_magic
from data import info, warn, error, dashboard, set_trend, touch, stop, set_initial_balance, get_initial_balance, set_symbol, set_guard_limit, set_account_snapshot, set_exposure, set_module, set_spread


SYMBOL = "XAUUSD"


@dataclass
class RiskConfig:
    # ✅ ปิดเฉพาะ “ไม้ที่ขาดทุนหนัก”
    # กฎเดียว: ถ้าไม้ใดขาดทุน (profit ติดลบ) เกิน % ของพอร์ต (อิงจาก balance ตอนเริ่มรันบอท)
    # -> ปิด "ไม้นั้น" เท่านั้น (ไม่ปิดทั้งพอร์ต, ไม่ pause)
    per_position_loss_percent_of_port: float = 40.0




@dataclass
class SafeModeConfig:
    enabled: bool = True

    # เข้มขึ้นในช่วงข่าวแรง (เข้าให้น้อยลงแต่คุณภาพสูงขึ้น)
    max_spread_points: int = 120
    max_atr_points: int = 2000

    # ถ้าเจอสภาพตลาดแรง/สเปรดบานต่อเนื่อง -> pause การวางออเดอร์ใหม่
    trigger_count: int = 3          # ต้องเจอ bad condition กี่ครั้งติดกัน
    pause_minutes: int = 20         # pause นานกี่นาที

    # ATR check
    atr_timeframe: int = mt5.TIMEFRAME_M15
    atr_period: int = 14

def get_last_bar_time(symbol: str, timeframe: int):
    rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, 1)
    if rates is None or len(rates) == 0:
        return None
    return rates[0]["time"]


def _today_key() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _next_day_start_ts(buffer_minutes: int = 5) -> float:
    """Timestamp ของวันถัดไป 00:00 + buffer นาที (กัน time skew)"""
    now = datetime.now()
    nxt = (now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)) + timedelta(minutes=int(buffer_minutes))
    return nxt.timestamp()



def _pick_lot(symbol: str, desired_lot: float) -> float | None:
    """ปรับ lot ให้เข้ากับ min/max/step ของโบรก"""
    info_ = mt5.symbol_info(symbol)
    if info_ is None:
        return None
    minv = float(info_.volume_min)
    maxv = float(info_.volume_max)
    step = float(info_.volume_step) if float(info_.volume_step) > 0 else minv

    lot = float(desired_lot)
    lot = max(minv, min(lot, maxv))
    lot = round(lot / step) * step
    return float(f"{lot:.2f}")


def _normalize_price(symbol: str, price: float) -> float:
    """Round price to broker tick size."""
    info_ = mt5.symbol_info(symbol)
    if info_ is None or float(getattr(info_, "trade_tick_size", 0.0) or 0.0) <= 0:
        return price
    tick_size = float(info_.trade_tick_size)
    return round(price / tick_size) * tick_size


def _stops_level_points(symbol: str) -> int:
    """Broker minimum stops level in points (0 if unknown)."""
    info_ = mt5.symbol_info(symbol)
    if info_ is None:
        return 0
    try:
        return int(getattr(info_, "trade_stops_level", 0) or 0)
    except Exception:
        return 0


def _spread_points(symbol: str) -> float | None:
    """คำนวณ spread เป็น points"""
    info_ = mt5.symbol_info(symbol)
    tick = mt5.symbol_info_tick(symbol)
    if info_ is None or tick is None:
        return None
    point = float(info_.point)
    if point <= 0:
        return None
    return (float(tick.ask) - float(tick.bid)) / point


def _spread_ok_for_scalein(symbol: str, cfg: OrderConfig) -> bool:
    sp = _spread_points(symbol)
    if sp is None:
        return False
    return sp <= float(cfg.max_spread_points)



def _calc_atr_points(symbol: str, timeframe: int, period: int) -> float | None:
    """ATR แบบง่าย คืนค่าเป็น points (ไว้ทำ SafeMode pause)"""
    info_ = mt5.symbol_info(symbol)
    if info_ is None:
        return None
    point = float(getattr(info_, "point", 0.0) or 0.0)
    if point <= 0:
        return None

    rates = mt5.copy_rates_from_pos(symbol, timeframe, 1, period + 2)
    if rates is None or len(rates) < period + 2:
        return None

    trs = []
    prev_close = float(rates[0]["close"])
    for i in range(1, len(rates)):
        h = float(rates[i]["high"])
        l = float(rates[i]["low"])
        tr = max(h - l, abs(h - prev_close), abs(l - prev_close))
        trs.append(tr)
        prev_close = float(rates[i]["close"])

    if len(trs) < period:
        return None
    atr = sum(trs[-period:]) / float(period)
    return float(atr) / point

def _count_my_positions(symbol: str, magic: int) -> int:
    positions = mt5.positions_get(symbol=symbol)
    if not positions:
        return 0
    return sum(1 for p in positions if int(p.magic) == int(magic))

def _count_my_pending(symbol: str, magic: int) -> int:
    orders = mt5.orders_get(symbol=symbol)
    if not orders:
        return 0
    n = 0
    for o in orders:
        if int(o.magic) != int(magic):
            continue
        if o.type in (mt5.ORDER_TYPE_BUY_LIMIT, mt5.ORDER_TYPE_SELL_LIMIT):
            n += 1
    return n

def _active_count(symbol: str, magic: int) -> int:
    """positions + pending"""
    return _count_my_positions(symbol, magic) + _count_my_pending(symbol, magic)

def scale_in_if_profit(symbol: str, cfg: OrderConfig) -> bool:
    """
    ซ้อนไม้ (Market) เมื่อ:
    - มี position ของบอทอยู่แล้ว (magic ตรง)
    - กำไรของไม้นั้น >= stack_profit_points (เช่น 600)
    - รวมแล้วไม่เกิน max_positions (เช่น 2)
    - ✅ เช็ค spread ก่อนยิง
    - ✅ market closed (10018) -> skip เฉย ๆ
    - ✅ ไม้ซ้อนใช้ TP เดียวกับไม้แรก + หลบ psych สำหรับราคาที่ส่งคำสั่ง
    """
    info_ = mt5.symbol_info(symbol)
    tick = mt5.symbol_info_tick(symbol)
    if info_ is None or tick is None:
        return False

    point = float(info_.point)
    if point <= 0:
        return False

    # ✅ เช็ค spread ก่อน scale-in (เหมือน pending)
    spread_pts = (float(tick.ask) - float(tick.bid)) / point
    if spread_pts > float(cfg.max_spread_points):
        touch("Skip scale-in: spread too high")
        return False

    max_pos = int(getattr(cfg, "max_positions", 2))
    stack_pts = float(getattr(cfg, "stack_profit_points", 600))

    positions = mt5.positions_get(symbol=symbol)
    if not positions:
        return False

    my_pos = [p for p in positions if int(p.magic) == int(cfg.magic)]
    if not my_pos:
        return False

    # เรียงให้ไม้เก่าที่สุดมาก่อน (ใช้เป็นไม้แรก)
    my_pos.sort(key=lambda p: (getattr(p, 'time', 0) or getattr(p, 'time_msc', 0) or 0, int(getattr(p, 'ticket', 0) or 0)))


    # ✅ กันไม่ให้เกินจำนวนรวม (positions + pending)
    max_pos = int(getattr(cfg, "max_positions", 2))
    active_n = _active_count(symbol, int(cfg.magic))
    if active_n >= max_pos:
        touch("Skip scale-in: max trades reached")
        return False

    # จำกัดจำนวนไม้
    if len(my_pos) >= max_pos:
        return False

    # ไม่ซ้อนถ้ามีทั้ง BUY และ SELL ปนกัน
    types = {int(p.type) for p in my_pos}
    if len(types) != 1:
        touch("Skip scale-in: mixed directions")
        return False

    pos_type = next(iter(types))

    # ใช้ไม้แรกเป็นตัววัดกำไร + ใช้ TP ของไม้แรก
    p0 = my_pos[0]
    entry0 = float(p0.price_open)
    first_tp = float(getattr(p0, "tp", 0.0) or 0.0)

    # ถ้าไม้แรกไม่มี TP (0) -> ไม่ซ้อน (กันพฤติกรรมไม่ชัด)
    if first_tp <= 0:
        touch("Skip scale-in: first TP not set")
        return False

    # คำนวณกำไรเป็น points
    if pos_type == mt5.POSITION_TYPE_BUY:
        cur = float(tick.bid)
        profit_pts = (cur - entry0) / point
        order_type = mt5.ORDER_TYPE_BUY
        price = float(tick.ask)  # BUY ใช้ ASK
        is_buy_level = True
    else:
        cur = float(tick.ask)
        profit_pts = (entry0 - cur) / point
        order_type = mt5.ORDER_TYPE_SELL
        price = float(tick.bid)  # SELL ใช้ BID
        is_buy_level = False

    if profit_pts < stack_pts:
        return False

    # ✅ หลบ psych สำหรับ "ราคาที่ส่งคำสั่ง" (market order)
    psych = PsychConfig()  # ใช้ค่า default เดียวกับระบบเดิม
    price = nudge_away_from_psych(price, is_buy_level, point, psych)
    price = _normalize_price(symbol, float(price))
    if is_psych_level(price, psych):
        touch("Skip scale-in: entry psych")
        return False

    lot = _pick_lot(symbol, float(cfg.lot))
    if lot is None:
        warn("Scale-in: cannot read lot constraints")
        return False

    # --- Broker stops level guard (avoid rejected orders) ---
    stops_pts = _stops_level_points(symbol)
    if stops_pts > 0:
        tp_dist = (float(first_tp) - float(price)) / point if order_type == mt5.ORDER_TYPE_BUY else (float(price) - float(first_tp)) / point
        if tp_dist < float(stops_pts):
            touch(f"Skip scale-in: stops_level={stops_pts}pts (TP too close)")
            return False

    req = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": lot,
        "type": order_type,
        "price": _normalize_price(symbol, float(price)),
        "tp": first_tp,  # ✅ TP เดียวกับไม้แรก
        "deviation": int(cfg.deviation),
        "magic": int(cfg.magic),
        "comment": f"SCALE_IN_{int(stack_pts)}PTS_TP=FIRST",
        "type_filling": mt5.ORDER_FILLING_RETURN,
    }

    # --- race-safe scale-in guard ---
    max_pos = int(getattr(cfg, "max_positions", 2))
    active_n2 = _active_count(symbol, int(cfg.magic))
    if active_n2 >= max_pos:
        touch(f"Skip scale-in: max trades reached (race {active_n2}/{max_pos})")
        return False

    res = mt5.order_send(req)

    # ✅ market closed -> skip เฉย ๆ
    market_closed = getattr(mt5, "TRADE_RETCODE_MARKET_CLOSED", 10018)
    if res and res.retcode == market_closed:
        touch("Scale-in: market closed")
        return False

    if res and res.retcode == mt5.TRADE_RETCODE_DONE:
        info(f"Scale-in ✅ profit={profit_pts:.0f}pts lot={lot} tp(first)={first_tp:.2f}")
        touch("Scale-in ✅")
        return True

    warn(f"Scale-in failed ret={res.retcode if res else 'None'}")
    return False





def _my_positions(symbol: str, magic: int):
    pos = mt5.positions_get(symbol=symbol)
    if not pos:
        return []
    return [p for p in pos if int(getattr(p, "magic", 0) or 0) == int(magic)]


def _floating_pnl(symbol: str, magic: int) -> float:
    """รวมกำไร/ขาดทุนลอยตัวของโพซิชันบอท (หน่วยสกุลเงินบัญชี)"""
    ps = _my_positions(symbol, magic)
    return float(sum(float(getattr(p, "profit", 0.0) or 0.0) for p in ps))


def _apply_time_stop(symbol: str, magic: int, risk: RiskConfig) -> int:
    """ปิดโพซิชันที่ค้างนานเกิน (เฉพาะที่ติดลบตาม config)"""
    now_ts = time.time()
    closed = 0
    for p in _my_positions(symbol, magic):
        opened = float(getattr(p, "time", 0) or 0)
        if opened <= 0:
            continue
        age_h = (now_ts - opened) / 3600.0
        if age_h < float(risk.max_hold_hours):
            continue
        profit = float(getattr(p, "profit", 0.0) or 0.0)
        if risk.close_only_if_negative and profit >= 0:
            continue
        # ปิดทีละไม้ (จะไม่ยกเลิก pending ตรงนี้)
        ok = close_position_ticket(symbol, int(getattr(p, 'ticket', 0) or 0), reason=f"TIME_STOP_{risk.max_hold_hours:.0f}H")
        if ok:
            closed += 1
        break
    return closed




def _apply_pos_loss_guard(symbol: str, magic: int, risk: RiskConfig) -> int:
    """ถ้า 'ไม้ใดไม้หนึ่ง' ขาดทุนเกิน % ของพอร์ต -> ปิดไม้เดียว แล้วจบ (ไม่ pause)"""
    acc = mt5.account_info()
    if acc is None:
        return 0

    # ใช้ balance ตอนเริ่มรันบอทเป็นฐาน (port)
    ib = float(get_initial_balance() or 0.0)
    base = ib if ib > 0 else float(getattr(acc, "balance", 0.0) or 0.0)
    if base <= 0:
        return 0

    thr = base * float(risk.per_position_loss_percent_of_port) / 100.0

    for p in _my_positions(symbol, magic):
        profit = float(getattr(p, "profit", 0.0) or 0.0)
        if profit < 0 and abs(profit) >= thr:
            ticket = int(getattr(p, "ticket", 0) or 0)
            warn(f"POS_LOSS_GUARD: ticket={ticket} loss={profit:.2f} <= -{thr:.2f} ({risk.per_position_loss_percent_of_port:.0f}% of port) -> closing this position")
            close_position_ticket(symbol, ticket, reason=f"POS_LOSS_{risk.per_position_loss_percent_of_port:.0f}PCT")
            return 1
    return 0


def main():
    if not mt5.initialize():
        error("MT5 initialize failed")
        return

    if not mt5.symbol_select(SYMBOL, True):
        error(f"symbol_select failed: {SYMBOL}")
        mt5.shutdown()
        return

    set_symbol(SYMBOL, "M15")
    set_module("TrendFilter", "ON")
    set_module("EntryPending", "ON")
    set_module("PendingMonitor", "ON")
    set_module("ScaleIn", "OFF")
    set_module("SafeMode", "ON")
    set_module("ExitManager", "OFF")
    set_module("AccountGuard", "OFF")
    set_module("PosLossGuard", "ON")
    set_module("DailyGuard", "OFF")
    set_module("FloatingGuard", "OFF")
    set_module("TimeStop", "OFF")

    acc = mt5.account_info()
    if acc is None:
        error("Cannot read account_info()")
        mt5.shutdown()
        return

    set_initial_balance(float(acc.balance))
    set_account_snapshot(float(acc.balance), float(acc.equity))
    info(f"Initial balance set: {acc.balance}")

    dashboard(interval=2, clear=True)
    set_guard_limit(40.0)
    info("BOT STARTED (M15 mode)")
    touch("Initialized")

    trend_cfg = TrendConfig(
        lookback_days_d=10,
        lookback_bars_h1=48,
        lookback_bars_m15=96,
    )

    order_cfg = OrderConfig(
        magic=260122,
        lot=0.01,

        entry_timeframe=mt5.TIMEFRAME_M15,
        entry_lookback_bars=96,
        pending_timeout_bars=8,
        runaway_points=3000,

        pending_expire_hours=24,
        min_distance_points=120,
        max_spread_points=120,  # ✅ SafeMode: เข้ม spread
        deviation=20,

        max_positions=1,  # ✅ SafeMode: จำกัดไม้รวมให้น้อยลง
        stack_profit_points=600,
        tp_sr_buffer_points=100,
        min_tp_points=200,
        replace_threshold_points=400,
        allow_pending_when_in_position=False,
        min_atr_points=100,
        max_atr_points=1800,  # ✅ SafeMode: เข้ม volatility (ATR)
            enable_micro=False,   # ✅ SafeMode: ปิด MICRO zone (ลดการไล่ราคา)
    )

    psych_cfg = PsychConfig(
        psych_suffixes=(0, 350, 500, 550, 750, 450, 950),
        tp_buffer_points=200,
        entry_psych_buffer_points=30
    )
    risk_cfg = RiskConfig(
        per_position_loss_percent_of_port=40.0,
    )

    safe_cfg = SafeModeConfig(
        enabled=True,
        atr_timeframe=mt5.TIMEFRAME_M15,
        atr_period=14,
    )
    # ✅ Sync OrderConfig thresholds with SafeMode (so editing SafeModeConfig is enough)
    try:
        order_cfg.max_spread_points = int(safe_cfg.max_spread_points)
        order_cfg.max_atr_points = int(safe_cfg.max_atr_points)
    except Exception:
        pass

    info(
        f"RUNNING: {os.path.abspath(__file__)} | "
        f"SafeMode spread<={safe_cfg.max_spread_points} ATR<={safe_cfg.max_atr_points} "
        f"trigger={safe_cfg.trigger_count} pause={safe_cfg.pause_minutes}m | "
        f"OrderConfig spread<={getattr(order_cfg,'max_spread_points','-')} ATR<={getattr(order_cfg,'max_atr_points','-')}"
    )

    pause_until_ts = 0.0
    bad_vol_count = 0
    last_bad_reason = "-"

    set_guard_limit(float(risk_cfg.per_position_loss_percent_of_port))

    last_m15_time = None

    try:
        while True:

            # --- dashboard snapshots ---
            try:
                acc2 = mt5.account_info()
                if acc2 is not None:
                    set_account_snapshot(float(acc2.balance), float(acc2.equity))
            except Exception:
                pass
            # --- Per-position loss guard (ปิดเฉพาะไม้ที่ขาดทุนหนัก) ---
            try:
                _apply_pos_loss_guard(SYMBOL, int(order_cfg.magic), risk_cfg)
            except Exception:
                pass


            try:
                sp = _spread_points(SYMBOL)
                if sp is not None:
                    set_spread(round(float(sp), 1))
            except Exception:
                pass

            try:
                pos_n = _count_my_positions(SYMBOL, int(order_cfg.magic))
                pend_n = _count_my_pending(SYMBOL, int(order_cfg.magic))
                set_exposure(pos_n, pend_n, pos_n + pend_n, max_pos=int(getattr(order_cfg, "max_positions", 2)))
            except Exception:
                pass

            # --- SafeMode: Volatility pause (spread/ATR) ---
            if safe_cfg.enabled:
                now_ts = time.time()
                sp_now = _spread_points(SYMBOL)  # points
                atr_pts = _calc_atr_points(SYMBOL, int(safe_cfg.atr_timeframe), int(safe_cfg.atr_period))

                bad_reasons = []
                if sp_now is not None and sp_now > float(safe_cfg.max_spread_points):
                    bad_reasons.append(f"spread {sp_now:.0f}>{safe_cfg.max_spread_points}")
                if atr_pts is not None and atr_pts > float(safe_cfg.max_atr_points):
                    bad_reasons.append(f"ATR {atr_pts:.0f}>{safe_cfg.max_atr_points}")

                if bad_reasons:
                    bad_vol_count += 1
                    last_bad_reason = ", ".join(bad_reasons)
                else:
                    bad_vol_count = 0

                # If currently paused, update dashboard status
                if now_ts < pause_until_ts:
                    mins_left = int((pause_until_ts - now_ts) / 60) + 1
                    set_module("SafeMode", f"PAUSED {mins_left}m")
                else:
                    set_module("SafeMode", "ON")
                    if bad_vol_count >= int(safe_cfg.trigger_count):
                        pause_until_ts = now_ts + int(safe_cfg.pause_minutes) * 60
                        bad_vol_count = 0
                        warn(f"SAFE MODE PAUSE {safe_cfg.pause_minutes}m | reason: {last_bad_reason}")
                        touch("SafeMode: paused")
                        # Cancel pending to avoid news-spike fills
                        try:
                            cancel_pending_by_magic(SYMBOL, int(order_cfg.magic))
                        except Exception:
                            pass

            # --- Max trades gate (positions + pending) ---
            max_pos = int(getattr(order_cfg, "max_positions", 2))
            active_n = _active_count(SYMBOL, int(order_cfg.magic))
            max_reached = active_n >= max_pos
            if max_reached:
                touch(f"Max trades reached ({active_n}/{max_pos})")

            # ✅ SafeMode: ปิด Scale-in (ข่าวแรง/ผันผวนสูง)
            # if not max_reached:
            #     if scale_in_if_profit(SYMBOL, order_cfg):
            #         time.sleep(1)

            canceled = monitor_pending_not_touched(SYMBOL, order_cfg)
            if canceled:
                touch("Pending refreshed (canceled)")

            cur_m15_time = get_last_bar_time(SYMBOL, mt5.TIMEFRAME_M15)
            if cur_m15_time is None:
                touch("Waiting M15 data")
                time.sleep(2)
                continue

            if last_m15_time != cur_m15_time:
                last_m15_time = cur_m15_time

                trend = get_major_trend(SYMBOL, trend_cfg)
                set_trend(trend)

                if trend == 0:
                    touch("Waiting trend confirmation")
                else:
                    if not max_reached:
                        if safe_cfg.enabled and time.time() < pause_until_ts:
                            touch("SafeMode: paused (no new orders)")
                        else:
                            ok = place_pending_by_trend(SYMBOL, trend, order_cfg, psych_cfg)
                            touch("Placed pending ✅" if ok else "No entry / skipped")
                    else:
                        touch("No entry: max trades reached")

            time.sleep(2)

    except KeyboardInterrupt:
        info("Stopped by user (Ctrl+C)")
    finally:
        stop()
        mt5.shutdown()
        print("\n")


if __name__ == "__main__":
    main()
