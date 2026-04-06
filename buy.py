# buy.py
# Find SR on selected TF (default M15) + place pending limit orders (configurable lot)
# + RR 1:1 TP (with psych buffer)
# + SR "zone" entry (ATR-based, with min width)
# + Replace pending when SR shifts a lot
# + monitor "not touched" (timeout/runaway)
# + optional micro pullback zone when price runs away

from dataclasses import dataclass
from typing import Optional, Tuple
import time
import MetaTrader5 as mt5

from exit import PsychConfig, is_psych_level, nudge_away_from_psych
from data import info, warn, error, trade, touch, set_spread



# -------------------- Order Config --------------------

@dataclass
class OrderConfig:
  magic: int = 260122
  lot: float = 0.01

  pending_expire_hours: int = 24
  min_distance_points: int = 150
  # extra buffer when auto-adjusting entry (avoid "too close" skips)
  min_distance_buffer_points: int = 10
  max_spread_points: int = 250
  deviation: int = 20

  # Entry TF settings
  entry_timeframe: int = mt5.TIMEFRAME_M15
  entry_lookback_bars: int = 96 # 96 bars M15 = 24 ชั่วโมง

  # Monitor not-touched cases (count in bars of entry timeframe)
  pending_timeout_bars: int = 8   # 8 bars M15 = 2 ชั่วโมง
  runaway_points: int = 8000    # ราคาหนีจาก pending ไกลเกิน -> cancel

  # SR Zone settings
  zone_atr_period: int = 14
  zone_atr_mult: float = 1.2
  zone_min_points: int = 500
  zone_entry_offset: float = 0.25  # 0.25 = วางลึกเข้าไปในโซน 25%

  # Replace pending when SR shifts
  replace_threshold_points: int = 800 # entry ใหม่ต่างจากเดิมเกินเท่านี้ -> replace

  # Micro pullback zone (ใกล้ราคา) สำหรับตลาดวิ่ง
  micro_lookback_bars: int = 16
  micro_zone_min_points: int = 250
  micro_zone_points: int = 600
  micro_entry_offset: float = 0.35
  micro_trigger_distance_points: int = 2500


  # ✅ SafeMode: enable/disable MICRO zone usage
  enable_micro: bool = True

  # Stacking (used by bot.py for scale-in logic)
  max_positions: int = 2
  stack_profit_points: int = 600

  # ✅ Safety: ไม่วาง pending ซ้อนระหว่างมี position (ลดการ over-exposure)
  allow_pending_when_in_position: bool = False

  # ✅ Volatility filter (ATR points). 0 = disable
  min_atr_points: int = 0
  max_atr_points: int = 0


  # TP cap by recent range (SR)
  tp_sr_buffer_points: int = 80
  # Optional: minimum TP distance after cap (points). 0 = disable
  min_tp_points: int = 0



# TP: SR-based (ไม่ตั้งจุดตัดขาดทุน)
# -------------------- Helpers --------------------

def _spread_points(symbol: str, point: float) -> Optional[float]:
  tick = mt5.symbol_info_tick(symbol)
  if tick is None:
    return None
  return (tick.ask - tick.bid) / point


def spread_ok(symbol: str, cfg: OrderConfig, point: float) -> bool:
  sp = _spread_points(symbol, point)
  if sp is None:
    set_spread("-")
    return False
  spv = round(sp, 1)
  set_spread(spv)
  return sp <= cfg.max_spread_points


def _normalize_price(symbol: str, price: float) -> float:
  info_ = mt5.symbol_info(symbol)
  if info_ is None or float(info_.trade_tick_size) <= 0:
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
def _pick_lot(symbol: str, cfg: OrderConfig) -> Optional[float]:
  """ใช้ cfg.lot แต่บังคับให้ตรงกับเงื่อนไขโบรก (min/max/step)"""
  info_ = mt5.symbol_info(symbol)
  if info_ is None:
    return None

  minv = float(info_.volume_min)
  maxv = float(info_.volume_max)
  step = float(info_.volume_step) if float(info_.volume_step) > 0 else minv

  lot = float(cfg.lot)
  lot = max(minv, min(lot, maxv))     # clamp
  lot = round(lot / step) * step      # snap to step
  lot = float(f"{lot:.2f}")        # กัน float แปลก ๆ
  return lot


# -------------------- SR / Zone --------------------

def find_sr_swing(symbol: str, timeframe: int, lookback_bars: int) -> Optional[Tuple[float, float]]:
  """หาแนวรับ/ต้านจาก swing ล่าสุด (ใกล้ราคาปัจจุบัน)"""
  tick = mt5.symbol_info_tick(symbol)
  if tick is None:
    return None

  rates = mt5.copy_rates_from_pos(symbol, timeframe, 1, lookback_bars)
  if rates is None or len(rates) < 12:
    return None

  highs = [float(r["high"]) for r in rates]
  lows = [float(r["low"]) for r in rates]

  swing_lows = []
  swing_highs = []
  for i in range(2, len(rates) - 2):
    if lows[i] < lows[i - 1] and lows[i] < lows[i + 1] and lows[i] < lows[i - 2] and lows[i] < lows[i + 2]:
      swing_lows.append(lows[i])
    if highs[i] > highs[i - 1] and highs[i] > highs[i + 1] and highs[i] > highs[i - 2] and highs[i] > highs[i + 2]:
      swing_highs.append(highs[i])

  mid = (tick.bid + tick.ask) / 2.0
  supports = [x for x in swing_lows if x < mid]
  resistances = [x for x in swing_highs if x > mid]

  support = max(supports) if supports else min(lows)
  resistance = min(resistances) if resistances else max(highs)
  return float(support), float(resistance)


def _calc_atr(symbol: str, timeframe: int, period: int) -> Optional[float]:
  """ATR แบบง่าย (True Range average) คืนค่าเป็นราคา"""
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
  return sum(trs[-period:]) / period


def _atr_points(symbol: str, timeframe: int, period: int, point: float) -> Optional[float]:
  """ATR เป็น points (ไว้ทำ volatility filter)."""
  if point <= 0:
    return None
  atr = _calc_atr(symbol, timeframe, period)
  if atr is None:
    return None
  return float(atr) / float(point)


def _sr_zone_width(symbol: str, cfg: OrderConfig, point: float) -> float:
  """zone width = max(ATR*mult, min_points) คืนค่าเป็นราคา"""
  atr = _calc_atr(symbol, cfg.entry_timeframe, cfg.zone_atr_period)
  atr_width = (atr * cfg.zone_atr_mult) if atr is not None else 0.0
  min_width = cfg.zone_min_points * point
  return max(atr_width, min_width)


def _pick_entry_in_zone(support: float, resistance: float, trend: int, zone_w: float, offset: float) -> float:
  """trend>0: support+zone_w*offset, trend<0: resistance-zone_w*offset"""
  return (support + zone_w * offset) if trend > 0 else (resistance - zone_w * offset)



def _cap_tp_by_recent_range(tp: float, entry: float, dir_: int,
              support: float, resistance: float,
              point: float, buffer_points: int) -> float:
  """Cap TP so it does not exceed the recent range extremes (support/resistance).

  - BUY (dir_>0): tp <= resistance - buffer
  - SELL (dir_<0): tp >= support + buffer
  Also ensures TP stays on the correct side of entry.
  """
  buf = float(buffer_points) * point

  if dir_ > 0:
    cap = resistance - buf
    tp2 = min(tp, cap)
    # Ensure TP is above entry at least 1 point
    return max(tp2, entry + 1.0 * point)

  cap = support + buf
  tp2 = max(tp, cap)
  # Ensure TP is below entry at least 1 point
  return min(tp2, entry - 1.0 * point)


def _tp_from_recent_sr(entry: float, dir_: int,
            support: float, resistance: float,
            point: float, buffer_points: int) -> float:
  """TP from recent swing range extremes (structure-based).

  - BUY (dir_>0): target = resistance - buffer
  - SELL (dir_<0): target = support + buffer
  Always keeps TP on the correct side of entry by at least 1 point.
  """
  buf = float(buffer_points) * point
  if dir_ > 0:
    return max((resistance - buf), entry + 1.0 * point)
  return min((support + buf), entry - 1.0 * point)




# -------------------- Micro Zone --------------------

def find_micro_pullback_zone(symbol: str, timeframe: int, lookback_bars: int) -> Optional[Tuple[float, float]]:
  """หา micro pullback zone ใกล้ราคา: ใช้ low/high ของ N แท่งล่าสุด

  ปรับให้รวมแท่งปัจจุบัน (shift=0) เพื่อให้ micro zone ตามราคาทันในช่วงที่ราคา "วิ่ง" แรง
  (ถ้าข้อมูลไม่พอจะ fallback เป็น shift=1)
  """
  rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, lookback_bars)
  if rates is None or len(rates) < 5:
    rates = mt5.copy_rates_from_pos(symbol, timeframe, 1, lookback_bars)
    if rates is None or len(rates) < 5:
      return None

  lows = [float(r["low"]) for r in rates]
  highs = [float(r["high"]) for r in rates]
  return float(min(lows)), float(max(highs))


def _micro_zone_entry(micro_support: float, micro_resistance: float, trend: int, point: float, cfg: OrderConfig) -> float:
  """สร้าง entry จาก micro zone ด้วยความกว้างที่คุมได้"""
  zone_w = max(cfg.micro_zone_min_points, cfg.micro_zone_points) * point
  if trend > 0:
    return micro_support + zone_w * cfg.micro_entry_offset
  return micro_resistance - zone_w * cfg.micro_entry_offset


# -------------------- Orders / Pending --------------------

def _orders_get_by_magic(symbol: str, magic: int):
  orders = mt5.orders_get(symbol=symbol)
  if not orders:
    return []
  return [o for o in orders if int(o.magic) == magic]



def _positions_get_by_magic(symbol: str, magic: int):
  positions = mt5.positions_get(symbol=symbol)
  if not positions:
    return []
  return [p for p in positions if int(p.magic) == int(magic)]

def _active_count_by_magic(symbol: str, magic: int) -> int:
  """Total active exposure = open positions + pending limit orders for this magic."""
  pos_n = len(_positions_get_by_magic(symbol, magic))
  ords = _orders_get_by_magic(symbol, magic)
  pend_n = 0
  for o in ords:
    if o.type in (mt5.ORDER_TYPE_BUY_LIMIT, mt5.ORDER_TYPE_SELL_LIMIT):
      pend_n += 1
  return pos_n + pend_n

def _cancel_order(ticket: int) -> bool:
  req = {"action": mt5.TRADE_ACTION_REMOVE, "order": ticket}
  res = mt5.order_send(req)
  return bool(res and res.retcode == mt5.TRADE_RETCODE_DONE)


def _cancel_all_pending_by_magic(symbol: str, magic: int) -> int:
  orders = _orders_get_by_magic(symbol, magic)
  if not orders:
    return 0
  n = 0
  for o in orders:
    if o.type in (mt5.ORDER_TYPE_BUY_LIMIT, mt5.ORDER_TYPE_SELL_LIMIT):
      if _cancel_order(int(o.ticket)):
        n += 1
  return n


def monitor_pending_not_touched(symbol: str, cfg: OrderConfig) -> bool:
  """
  cancel pending ถ้า:
  - รอนานเกิน pending_timeout_bars (นับเป็นแท่งของ entry_timeframe)
  - ราคาหนีไกลเกิน runaway_points
  """
  info_ = mt5.symbol_info(symbol)
  tick = mt5.symbol_info_tick(symbol)
  if info_ is None or tick is None:
    return False

  point = float(info_.point)
  now = time.time()

  tf_seconds_map = {
    mt5.TIMEFRAME_M1: 60,
    mt5.TIMEFRAME_M5: 300,
    mt5.TIMEFRAME_M15: 900,
    mt5.TIMEFRAME_M30: 1800,
    mt5.TIMEFRAME_H1: 3600,
    mt5.TIMEFRAME_H4: 14400,
    mt5.TIMEFRAME_D1: 86400,
  }
  sec_per_bar = tf_seconds_map.get(cfg.entry_timeframe, 900)

  canceled_any = False
  for o in _orders_get_by_magic(symbol, cfg.magic):
    if o.type not in (mt5.ORDER_TYPE_BUY_LIMIT, mt5.ORDER_TYPE_SELL_LIMIT):
      continue

    entry = float(o.price_open)

    # 1) timeout
    age_seconds = now - float(o.time_setup)
    age_bars = age_seconds / sec_per_bar
    if age_bars >= cfg.pending_timeout_bars:
      warn(f"Pending timeout ~{age_bars:.1f} bars -> cancel ticket={o.ticket}")
      if _cancel_order(int(o.ticket)):
        touch("Canceled pending (timeout)")
        canceled_any = True
      continue

    # 2) runaway
    mid = (tick.bid + tick.ask) / 2.0
    dist_points = abs(mid - entry) / point
    if dist_points >= cfg.runaway_points:
      warn(f"Price ran away {dist_points:.0f} pts -> cancel ticket={o.ticket}")
      if _cancel_order(int(o.ticket)):
        touch("Canceled pending (runaway)")
        canceled_any = True

  return canceled_any


# -------------------- Main Entry --------------------

def place_pending_by_trend(symbol: str, trend: int, cfg: OrderConfig, psych: PsychConfig) -> bool:
  touch("Evaluating entry (SR + pending)")

  info_ = mt5.symbol_info(symbol)
  tick = mt5.symbol_info_tick(symbol)
  if info_ is None or tick is None:
    error("No symbol info/tick")
    touch("No symbol info/tick")
    return False

  point = float(info_.point)

  if not spread_ok(symbol, cfg, point):
    warn("Skip: spread too high or no tick")
    touch("Skip: spread too high")
    return False


  # --- Safety: ถ้ามี position อยู่แล้ว (magic เดียวกัน) จะไม่วาง pending เพิ่ม (ลด over-exposure) ---
  if not bool(getattr(cfg, "allow_pending_when_in_position", False)):
    if _positions_get_by_magic(symbol, cfg.magic):
      info("Skip: already in position (pending disabled while holding)")
      touch("Skip: in position")
      return False

  # --- Volatility filter (ATR) ---
  atr_pts = _atr_points(symbol, cfg.entry_timeframe, cfg.zone_atr_period, point)
  if atr_pts is not None:
    min_atr = int(getattr(cfg, "min_atr_points", 0) or 0)
    max_atr = int(getattr(cfg, "max_atr_points", 0) or 0)
    if max_atr > 0 and atr_pts > float(max_atr):
      warn(f"Skip: ATR too high ({atr_pts:.0f}pts > {max_atr})")
      touch("Skip: ATR high")
      return False
    if min_atr > 0 and atr_pts < float(min_atr):
      warn(f"Skip: ATR too low ({atr_pts:.0f}pts < {min_atr})")
      touch("Skip: ATR low")
      return False


  # --- Max active trades guard (positions + pending) ---
  max_pos = int(getattr(cfg, "max_positions", 2))
  active_n = _active_count_by_magic(symbol, cfg.magic)
  if active_n >= max_pos:
    info(f"Skip: Max trades reached (active={active_n}/{max_pos})")
    touch("Skip: max trades reached")
    return False

  # --- Read SR (BASE zone) ---
  sr = find_sr_swing(symbol, cfg.entry_timeframe, cfg.entry_lookback_bars)
  if sr is None:
    warn("Skip: cannot read SR data")
    touch("Skip: no SR data")
    return False

  support, resistance = sr

  # --- BASE zone entry ---
  zone_w = _sr_zone_width(symbol, cfg, point)
  base_entry_raw = _pick_entry_in_zone(support, resistance, trend, zone_w, cfg.zone_entry_offset)
  base_entry_norm = _normalize_price(symbol, base_entry_raw)

  # --- Decide MICRO zone if price ran far from BASE entry ---
  mid = (tick.bid + tick.ask) / 2.0
  dist_from_base_pts = abs(mid - base_entry_norm) / point
  use_micro = bool(getattr(cfg, 'enable_micro', True)) and dist_from_base_pts >= cfg.micro_trigger_distance_points

  if use_micro:
    micro = find_micro_pullback_zone(symbol, cfg.entry_timeframe, cfg.micro_lookback_bars)
    if micro is not None:
      micro_support, micro_resistance = micro
      micro_entry_raw = _micro_zone_entry(micro_support, micro_resistance, trend, point, cfg)
      new_entry_norm = _normalize_price(symbol, micro_entry_raw)
      info(f"Using MICRO zone | dist_from_base={dist_from_base_pts:.0f}pts entry={new_entry_norm:.2f}")
      touch("Entry: MICRO zone")
    else:
      new_entry_norm = base_entry_norm
      info("MICRO zone unavailable -> fallback BASE zone")
      touch("Entry: BASE zone (fallback)")
  else:
    new_entry_norm = base_entry_norm
    touch("Entry: BASE zone")

  # --- Replace pending when entry shifts enough ---
  existing = _orders_get_by_magic(symbol, cfg.magic)
  if existing:
    pend = None
    for o in existing:
      if o.type in (mt5.ORDER_TYPE_BUY_LIMIT, mt5.ORDER_TYPE_SELL_LIMIT):
        pend = o
        break

    if pend is not None:
      old_entry = float(pend.price_open)
      diff_pts = abs(new_entry_norm - old_entry) / point

      if diff_pts >= cfg.replace_threshold_points:
        warn(f"Replace pending: old={old_entry} new={new_entry_norm} diff={diff_pts:.0f}pts")
        canceled = _cancel_all_pending_by_magic(symbol, cfg.magic)
        touch(f"Replaced pending (canceled {canceled})")
      else:
        info(f"Skip: pending exists (diff {diff_pts:.0f}pts < threshold)")
        touch("Skip: pending exists")
        return False

  # --- Lot / Expire ---
  lot = _pick_lot(symbol, cfg)
  if lot is None:
    error("Cannot read lot settings")
    touch("Error: cannot read lot settings")
    return False

  expire_ts = int(time.time() + cfg.pending_expire_hours * 3600)
  market_closed = getattr(mt5, "TRADE_RETCODE_MARKET_CLOSED", 10018)

  # ===================== BUY LIMIT =====================
  if trend > 0:
    entry = _normalize_price(symbol, nudge_away_from_psych(new_entry_norm, True, point, psych))

    # ถ้าใกล้ราคาเกิน -> ปรับ entry ให้ออกห่างตาม min_distance (+buffer) แทนการ skip
    if entry >= tick.bid - cfg.min_distance_points * point:
      buf = int(getattr(cfg, 'min_distance_buffer_points', 0) or 0)
      adj = float(tick.bid) - float(cfg.min_distance_points + buf) * point
      entry = _normalize_price(symbol, nudge_away_from_psych(adj, True, point, psych))
      info(f"Adjust: buy entry too close -> {entry:.2f}")
      touch("Adjust entry: buy min distance")
      if entry >= tick.bid - cfg.min_distance_points * point:
        info("Skip: buy limit still too close after adjust")
        touch("Skip: buy too close")
        return False
    if is_psych_level(entry, psych):
      info("Skip: entry is psych level")
      touch("Skip: entry psych")
      return False

    # ✅ SafeMode: ถ้า entry ไกลเกิน runaway_points ตั้งแต่แรก -> ไม่ต้องวาง (กันวางแล้วโดน cancel ทันที)
    mid2 = (tick.bid + tick.ask) / 2.0
    dist_pts2 = abs(mid2 - entry) / point
    if dist_pts2 >= cfg.runaway_points:
      warn(f"Skip: entry too far ({dist_pts2:.0f}pts >= runaway {cfg.runaway_points})")
      touch("Skip: entry too far")
      return False

    # --- TP (SR-based only; ) ---
    tp = _tp_from_recent_sr(entry, +1, support, resistance, point, cfg.tp_sr_buffer_points)
    tp = _normalize_price(symbol, float(nudge_away_from_psych(tp, True, point, psych)))

    # --- Broker stops level guard (avoid rejected orders) ---
    stops_pts = _stops_level_points(symbol)
    if stops_pts > 0:
      if (abs(tp - entry) / point) < stops_pts:
        warn(f"Skip: stops_level={stops_pts}pts (TP too close)")
        touch("Skip: stops_level")
        return False

    # --- Optional minimum TP distance guard ---
    if int(getattr(cfg, "min_tp_points", 0)) > 0:
      tp_dist_pts = (tp - entry) / point
      if tp_dist_pts < float(cfg.min_tp_points):
        warn(f"Skip: TP too close after cap ({tp_dist_pts:.0f}pts < {cfg.min_tp_points})")
        touch("Skip: TP too close")
        return False


    info(f"BUY plan | entry={entry:.2f} tp={tp:.2f} (SR) dist_from_price={(tick.bid-entry)/point:.0f}pts")

    req = {
      "action": mt5.TRADE_ACTION_PENDING,
      "symbol": symbol,
      "volume": lot,
      "type": mt5.ORDER_TYPE_BUY_LIMIT,
      "price": entry,
      "tp": _normalize_price(symbol, tp),
      "deviation": cfg.deviation,
      "magic": cfg.magic,
      "comment": f"TrendBuyLimit_SR_Zone_M15",
      "type_time": mt5.ORDER_TIME_SPECIFIED,
      "expiration": expire_ts,
      "type_filling": mt5.ORDER_FILLING_RETURN,
    }

    # --- Final guard: max trades reached (race-safe) ---
    max_pos = int(getattr(cfg, "max_positions", 2))
    active_n2 = _active_count_by_magic(symbol, cfg.magic)
    if active_n2 >= max_pos:
      warn(f"Skip: max trades reached (race {active_n2}/{max_pos})")
      touch("Skip: max trades reached")
      return False

    res = mt5.order_send(req)

    if res is None:
      le = mt5.last_error()
      error(f"Order failed (BUY LIMIT) | retcode=None last_error={le}")
      touch("Order failed BUY")
      return False

    if res and res.retcode == market_closed:
      warn("Skip: market closed (BUY LIMIT)")
      touch("Skip: market closed")
      return False

    if res and res.retcode == mt5.TRADE_RETCODE_DONE:
      trade(f"BUY LIMIT placed @ {entry} | TP={req['tp']} | lot={lot}")
      touch("Placed BUY LIMIT ✅")
      return True

    error(f"Order failed (BUY LIMIT) | retcode={res.retcode if res else 'None'}")
    touch("Order failed BUY")
    return False

  # ===================== SELL LIMIT =====================
  if trend < 0:
    entry = _normalize_price(symbol, nudge_away_from_psych(new_entry_norm, False, point, psych))

    # ถ้าใกล้ราคาเกิน -> ปรับ entry ให้ออกห่างตาม min_distance (+buffer) แทนการ skip
    if entry <= tick.ask + cfg.min_distance_points * point:
      buf = int(getattr(cfg, 'min_distance_buffer_points', 0) or 0)
      adj = float(tick.ask) + float(cfg.min_distance_points + buf) * point
      entry = _normalize_price(symbol, nudge_away_from_psych(adj, False, point, psych))
      info(f"Adjust: sell entry too close -> {entry:.2f}")
      touch("Adjust entry: sell min distance")
      if entry <= tick.ask + cfg.min_distance_points * point:
        info("Skip: sell limit still too close after adjust")
        touch("Skip: sell too close")
        return False
    if is_psych_level(entry, psych):
      info("Skip: entry is psych level")
      touch("Skip: entry psych")
      return False

    # ✅ SafeMode: ถ้า entry ไกลเกิน runaway_points ตั้งแต่แรก -> ไม่ต้องวาง (กันวางแล้วโดน cancel ทันที)
    mid2 = (tick.bid + tick.ask) / 2.0
    dist_pts2 = abs(mid2 - entry) / point
    if dist_pts2 >= cfg.runaway_points:
      warn(f"Skip: entry too far ({dist_pts2:.0f}pts >= runaway {cfg.runaway_points})")
      touch("Skip: entry too far")
      return False

    # --- TP (SR-based only; ) ---
    tp = _tp_from_recent_sr(entry, -1, support, resistance, point, cfg.tp_sr_buffer_points)
    tp = _normalize_price(symbol, float(nudge_away_from_psych(tp, False, point, psych)))

    # --- Broker stops level guard (avoid rejected orders) ---
    stops_pts = _stops_level_points(symbol)
    if stops_pts > 0:
      if (abs(tp - entry) / point) < stops_pts:
        warn(f"Skip: stops_level={stops_pts}pts (TP too close)")
        touch("Skip: stops_level")
        return False

    # --- Optional minimum TP distance guard ---
    if int(getattr(cfg, "min_tp_points", 0)) > 0:
      tp_dist_pts = (entry - tp) / point
      if tp_dist_pts < float(cfg.min_tp_points):
        warn(f"Skip: TP too close ({tp_dist_pts:.0f}pts < {cfg.min_tp_points})")
        touch("Skip: TP too close")
        return False

    info(f"SELL plan | entry={entry:.2f} tp={tp:.2f} (SR) dist_from_price={(entry-tick.ask)/point:.0f}pts")

    req = {
      "action": mt5.TRADE_ACTION_PENDING,
      "symbol": symbol,
      "volume": lot,
      "type": mt5.ORDER_TYPE_SELL_LIMIT,
      "price": entry,
      "tp": _normalize_price(symbol, tp),
      "deviation": cfg.deviation,
      "magic": cfg.magic,
      "comment": f"TrendSellLimit_SR_Zone_M15",
      "type_time": mt5.ORDER_TIME_SPECIFIED,
      "expiration": expire_ts,
      "type_filling": mt5.ORDER_FILLING_RETURN,
    }

    # --- Final guard: max trades reached (race-safe) ---
    max_pos = int(getattr(cfg, "max_positions", 2))
    active_n2 = _active_count_by_magic(symbol, cfg.magic)
    if active_n2 >= max_pos:
      warn(f"Skip: max trades reached (race {active_n2}/{max_pos})")
      touch("Skip: max trades reached")
      return False

    res = mt5.order_send(req)

    if res is None:
      le = mt5.last_error()
      error(f"Order failed (SELL LIMIT) | retcode=None last_error={le}")
      touch("Order failed SELL")
      return False

    if res and res.retcode == market_closed:
      warn("Skip: market closed (SELL LIMIT)")
      touch("Skip: market closed")
      return False

    if res and res.retcode == mt5.TRADE_RETCODE_DONE:
      trade(f"SELL LIMIT placed @ {entry} | TP={req['tp']} | lot={lot}")
      touch("Placed SELL LIMIT ✅")
      return True

    error(f"Order failed (SELL LIMIT) | retcode={res.retcode if res else 'None'}")
    touch("Order failed SELL")
    return False

  touch("No trend")
  return False
