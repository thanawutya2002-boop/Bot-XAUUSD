# data.py
from __future__ import annotations

from datetime import datetime
from enum import Enum
import threading
import time
import sys
import os
import shutil
from collections import deque
from typing import Optional, Tuple, Dict

LOG_FILE = "bot_status.log"


class StatusLevel(Enum):
    INFO = "INFO"
    WARN = "WARN"
    ERROR = "ERROR"
    TRADE = "TRADE"


# =========================
# Runtime state (dashboard)
# =========================
_lock = threading.Lock()
_running = True
_dashboard_mode = False

_start_ts = time.time()
_last_action_time = time.time()
_last_message = "Starting..."

_symbol = "-"
_tf = "M15"

_current_trend = "NONE"
_trend_detail: Tuple[int, int, int] | None = None  # (D, H1, M15)

_current_spread = "-"

# exposure
_pos_n = 0
_pend_n = 0
_active_n = 0
_max_pos: Optional[int] = None

# account
_initial_balance: Optional[float] = None
_balance: Optional[float] = None
_equity: Optional[float] = None
_dd_pct: Optional[float] = None
_guard_loss_pct: Optional[float] = None

_modules: Dict[str, str] = {}  # name -> status text

_recent_logs = deque(maxlen=10)


def _now_hms() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _fmt_uptime(sec: int) -> str:
    h = sec // 3600
    m = (sec % 3600) // 60
    s = sec % 60
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def _write_log(text: str) -> None:
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(text + "\n")
    except Exception:
        pass


def log(level: StatusLevel, msg: str) -> None:
    global _last_message, _last_action_time
    with _lock:
        _last_message = msg
        _last_action_time = time.time()

    text = f"[{_now_hms()}] [{level.value}] {msg}"
    _write_log(text)

    # ถ้าเปิด dashboard mode จะไม่ spam console แต่จะโชว์ "Recent events" แทน
    if _dashboard_mode:
        with _lock:
            _recent_logs.append(text)
        return

    print(text)


def info(msg: str):  log(StatusLevel.INFO, msg)
def warn(msg: str):  log(StatusLevel.WARN, msg)
def error(msg: str): log(StatusLevel.ERROR, msg)
def trade(msg: str): log(StatusLevel.TRADE, msg)


def touch(msg: str | None = None) -> None:
    global _last_action_time, _last_message
    with _lock:
        _last_action_time = time.time()
        if msg is not None:
            _last_message = msg


# ---------------- setters for dashboard ----------------

def set_symbol(symbol: str, tf: str = "M15") -> None:
    global _symbol, _tf
    with _lock:
        _symbol = symbol
        _tf = tf


def set_trend(trend: int) -> None:
    global _current_trend
    with _lock:
        if trend > 0:
            _current_trend = "UP"
        elif trend < 0:
            _current_trend = "DOWN"
        else:
            _current_trend = "NONE"


def set_trend_detail(d: int, h1: int, m15: int) -> None:
    global _trend_detail
    with _lock:
        _trend_detail = (int(d), int(h1), int(m15))


def set_spread(spread_points) -> None:
    global _current_spread
    with _lock:
        _current_spread = spread_points


def set_initial_balance(balance: float) -> None:
    global _initial_balance
    with _lock:
        _initial_balance = float(balance)


def get_initial_balance():
    with _lock:
        return _initial_balance


def set_account_snapshot(balance: float | None, equity: float | None) -> None:
    global _balance, _equity, _dd_pct
    with _lock:
        _balance = None if balance is None else float(balance)
        _equity = None if equity is None else float(equity)
        if _initial_balance and _initial_balance > 0 and _equity is not None:
            _dd_pct = max(0.0, (_initial_balance - _equity) / _initial_balance * 100.0)
        else:
            _dd_pct = None


def set_exposure(pos_n: int, pend_n: int, active_n: int, max_pos: int | None = None) -> None:
    global _pos_n, _pend_n, _active_n, _max_pos
    with _lock:
        _pos_n = int(pos_n)
        _pend_n = int(pend_n)
        _active_n = int(active_n)
        _max_pos = None if max_pos is None else int(max_pos)


def set_guard_limit(loss_percent: float) -> None:
    global _guard_loss_pct
    with _lock:
        _guard_loss_pct = float(loss_percent)


def set_module(name: str, status: str) -> None:
    with _lock:
        _modules[str(name)] = str(status)


# ---------------- dashboard renderer ----------------

def _arrow(v: int) -> str:
    return "↑" if v > 0 else ("↓" if v < 0 else "·")


def _term_width(default: int = 100) -> int:
    try:
        return shutil.get_terminal_size((default, 24)).columns
    except Exception:
        return default


def _box(lines: list[str], width: int) -> str:
    w = max(72, min(140, width))

    def cap(s: str) -> str:
        s = s.replace("\t", " ")
        if len(s) > w - 2:
            return s[: w - 5] + "..."
        return s

    def pad(s: str) -> str:
        s = cap(s)
        return s + " " * max(0, (w - 2 - len(s)))

    top = "╔" + "═" * (w - 2) + "╗"
    bot = "╚" + "═" * (w - 2) + "╝"
    mid = ["║" + pad(ln) + "║" for ln in lines]
    return "\n".join([top] + mid + [bot])


def _render_snapshot() -> str:
    with _lock:
        now = _now_hms()
        uptime = _fmt_uptime(int(time.time() - _start_ts))
        idle = int(time.time() - _last_action_time)

        symbol = _symbol
        tf = _tf
        trend = _current_trend
        td = _trend_detail
        spread = _current_spread

        pos_n = _pos_n
        pend_n = _pend_n
        active_n = _active_n
        max_pos = _max_pos

        bal = _balance
        eq = _equity
        dd = _dd_pct
        guard = _guard_loss_pct

        modules = dict(_modules)
        recent = list(_recent_logs)[-8:]
        last_msg = _last_message

    # ---- lines ----
    lines: list[str] = []
    lines.append(f" TRADE BOT DASHBOARD  |  {now}  |  Uptime {uptime}  |  Idle {idle}s ")
    lines.append("─" * 60)

    # Core
    if td:
        d, h1, m15 = td
        votes = [d, h1, m15]
        pos = sum(1 for x in votes if x == 1)
        neg = sum(1 for x in votes if x == -1)
        aligned = 'YES' if (pos >= 2 or neg >= 2) else 'NO'
        lines.append(
            f" Symbol: {symbol:<8}  TF: {tf:<4}  Trend: {trend:<4}  "
            f"D/H1/M15: {_arrow(d)}{d:+d} / {_arrow(h1)}{h1:+d} / {_arrow(m15)}{m15:+d}  2of3: {aligned}"
        )
    else:
        lines.append(f" Symbol: {symbol:<8}  TF: {tf:<4}  Trend: {trend:<4}")

    lines.append(f" Spread: {spread} pts")

    # Exposure
    mx = f"{active_n}/{max_pos}" if max_pos is not None else f"{active_n}/-"
    lines.append(f" Exposure: Positions {pos_n}  Pending {pend_n}  Active {mx}")

    # Account
    def fmt_num(x):
        return "-" if x is None else f"{x:.2f}"
    dd_txt = "-" if dd is None else f"{dd:.2f}%"
    guard_txt = "-" if guard is None else f"{guard:.0f}%"
    lines.append(f" Account: Balance {fmt_num(bal)}  Equity {fmt_num(eq)}  Drawdown {dd_txt}  Guard {guard_txt}")

    lines.append("─" * 60)

    # Modules
    if modules:
        items = list(modules.items())
        for i in range(0, len(items), 2):
            left = items[i]
            right = items[i + 1] if i + 1 < len(items) else None
            if right:
                lines.append(f" Modules: {left[0]}={left[1]:<10} | {right[0]}={right[1]}")
            else:
                lines.append(f" Modules: {left[0]}={left[1]}")
    else:
        lines.append(" Modules: (no module status)")

    lines.append("─" * 60)

    # Recent
    lines.append(" Recent events:")
    if recent:
        for ln in recent[-6:]:
            lines.append("  " + ln)
    else:
        lines.append("  (none)")

    lines.append("─" * 60)
    lines.append(f" Last: {last_msg}")

    return _box(lines, _term_width())


def dashboard(interval: int = 2, clear: bool = True) -> None:
    """Dashboard แบบหน้าจอเดียว อ่านง่าย"""
    global _dashboard_mode
    _dashboard_mode = True

    def _run():
        while _running:
            if clear and os.name == "nt":
                os.system("cls")
            elif clear:
                os.system("clear")
            sys.stdout.write(_render_snapshot() + "\n")
            sys.stdout.flush()
            time.sleep(max(1, int(interval)))

    t = threading.Thread(target=_run, daemon=True)
    t.start()


# -------- backward compatibility (old one-line status) --------
def status_line(interval: int = 10):
    def _run():
        while _running:
            with _lock:
                idle = int(time.time() - _last_action_time)
                line = (
                    f"[{_now_hms()}] BOT RUNNING | "
                    f"Trend={_current_trend} | "
                    f"Spread={_current_spread} | "
                    f"Last={_last_message} | "
                    f"Idle={idle}s"
                )
            sys.stdout.write("\r" + line + " " * 12)
            sys.stdout.flush()
            time.sleep(interval)

    t = threading.Thread(target=_run, daemon=True)
    t.start()


def stop():
    global _running
    _running = False
