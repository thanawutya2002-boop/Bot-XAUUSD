# law.py
from dataclasses import dataclass
import MetaTrader5 as mt5

from data import info, warn, touch, set_trend_detail


@dataclass
class TrendConfig:
    # D1: compare close now vs N days back
    lookback_days_d: int = 10
    # H1: compare close now vs N bars back
    lookback_bars_h1: int = 48
    # M15: compare close now vs N bars back
    lookback_bars_m15: int = 96


def _get_close(symbol: str, timeframe: int, shift: int) -> float | None:
    rates = mt5.copy_rates_from_pos(symbol, timeframe, shift, 1)
    if rates is None or len(rates) == 0:
        return None
    return float(rates[0]["close"])


def trend_by_close(symbol: str, timeframe: int, back_bars: int) -> int:
    c0 = _get_close(symbol, timeframe, 0)
    cN = _get_close(symbol, timeframe, back_bars)
    if c0 is None or cN is None:
        return 0
    if c0 > cN:
        return 1
    if c0 < cN:
        return -1
    return 0


def get_major_trend(symbol: str, cfg: TrendConfig) -> int:
    """Major trend using 2-of-3 vote from (D1, H1, M15)."""
    touch("Checking trend (D/H1/M15)")

    t_d = trend_by_close(symbol, mt5.TIMEFRAME_D1, cfg.lookback_days_d)
    t_h1 = trend_by_close(symbol, mt5.TIMEFRAME_H1, cfg.lookback_bars_h1)
    t_m15 = trend_by_close(symbol, mt5.TIMEFRAME_M15, cfg.lookback_bars_m15)

    info(f"Trend check | D={t_d} H1={t_h1} M15={t_m15}")
    try:
        # stored for dashboard (D, H1, M15)
        set_trend_detail(t_d, t_h1, t_m15)
    except Exception:
        pass

    votes = [t_d, t_h1, t_m15]
    pos = sum(1 for x in votes if x == 1)
    neg = sum(1 for x in votes if x == -1)

    if pos >= 2:
        info("Trend CONFIRMED UP (2/3)")
        return 1
    if neg >= 2:
        info("Trend CONFIRMED DOWN (2/3)")
        return -1

    warn("Trend not aligned (need 2/3: D/H1/M15)")
    return 0
