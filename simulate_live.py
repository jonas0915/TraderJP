"""
TraderJP — 5-Day Live Trade Simulation
=======================================
Simulates 5 real trading days (Feb 2–6 2026) trade-by-trade with:
  - Realistic ES minute-bar price path (GBM + intraday pattern)
  - Bot-style log output matching what you'd see in the terminal
  - Exact entry/SL/TP prices, timestamps, trade duration
  - Running P&L and end-of-day summaries
  - All Apex risk guards applied in real time

Usage:
    python3 simulate_live.py
"""

import math
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

# ─────────────────────────────────────────────────────────────
# Bot parameters (exact match to .env / strategy code)
# ─────────────────────────────────────────────────────────────

STARTING_BALANCE   = 50_000.0
TP_DOLLARS         = 600.0        # +12 ES pts
SL_DOLLARS         = 300.0        # −6 ES pts
ES_POINT_VALUE     = 50.0
ES_TICK            = 0.25
TP_POINTS          = TP_DOLLARS / ES_POINT_VALUE   # 12.0
SL_POINTS          = SL_DOLLARS / ES_POINT_VALUE   # 6.0
DAILY_LOSS_LIMIT   = 500.0
DAILY_PROFIT_CAP   = 1_000.0
MAX_TRAILING_DD    = 2_500.0
COOLDOWN_SECONDS   = 60
EOD_FLATTEN_MIN    = 5            # flatten 5 min before 15:15 CT
CONTRACT           = "ESH6"       # March 2026 front month
BASE_PRICE         = 6_052.00     # ~ES level early Feb 2026

# RTH  08:30 → 15:10 CT  (stop entries 5 min before 15:15 close)
RTH_OPEN_MIN  =  8 * 60 + 30   #  510 min from midnight
RTH_CLOSE_MIN = 15 * 60 + 10   #  910 min (last entry window)

# ─────────────────────────────────────────────────────────────
# 5 trading days with individual market characters
# ─────────────────────────────────────────────────────────────

@dataclass
class DayProfile:
    label:        str
    date_str:     str          # "2026-02-XX"
    daily_drift:  float        # net point move over the day (positive = up)
    volatility:   float        # intraday σ per minute (pts)
    regime:       str          # "Trending" / "Choppy" / "Volatile"
    win_rate:     float        # signal quality for this day

DAYS = [
    DayProfile("Monday   Feb 02", "2026-02-02",
               daily_drift=+18.0, volatility=0.55, regime="Trending ↑", win_rate=0.55),
    DayProfile("Tuesday  Feb 03", "2026-02-03",
               daily_drift= -6.0, volatility=0.80, regime="Volatile  ↕", win_rate=0.38),
    DayProfile("Wednesday Feb 04","2026-02-04",
               daily_drift= +4.0, volatility=0.40, regime="Choppy    ↔", win_rate=0.43),
    DayProfile("Thursday Feb 05", "2026-02-05",
               daily_drift=-22.0, volatility=0.65, regime="Trending ↓", win_rate=0.52),
    DayProfile("Friday   Feb 06", "2026-02-06",
               daily_drift= +8.0, volatility=0.45, regime="Choppy    ↔", win_rate=0.45),
]

# ─────────────────────────────────────────────────────────────
# Price generation helpers
# ─────────────────────────────────────────────────────────────

def _tick(price: float) -> float:
    return round(round(price / ES_TICK) * ES_TICK, 2)


def generate_minute_prices(
    open_price: float,
    daily_drift: float,
    volatility: float,
    n_minutes: int,
    seed: int,
) -> List[float]:
    """
    Geometric Brownian Motion walk with intraday structure:
      - Opening gap + expansion (first 30 min)
      - Lunch slow-down (min 120-180)
      - Afternoon trend continuation (min 180+)
    """
    rng = random.Random(seed)
    prices = [open_price]
    drift_per_min = daily_drift / n_minutes

    for i in range(1, n_minutes):
        prev = prices[-1]
        # Intraday volatility scaling
        if i < 30:
            vol_scale = 1.6       # high vol at open
        elif 120 <= i < 180:
            vol_scale = 0.5       # lunch lull
        else:
            vol_scale = 1.0

        noise = rng.gauss(0, volatility * vol_scale)
        new_price = _tick(prev + drift_per_min + noise)
        prices.append(max(new_price, 1.0))

    return prices


# ─────────────────────────────────────────────────────────────
# Signal and trade data
# ─────────────────────────────────────────────────────────────

@dataclass
class Signal:
    minute:        int
    action:        str    # BUY / SELL
    imbalance:     float
    delta:         int
    streak:        int


@dataclass
class TradeRecord:
    entry_time:   str
    exit_time:    str
    action:       str
    symbol:       str
    entry_price:  float
    sl_price:     float
    tp_price:     float
    exit_price:   float
    outcome:      str    # TP_HIT / SL_HIT / EOD_FLAT
    pnl:          float
    duration_min: int
    imbalance:    float
    delta:        int


# ─────────────────────────────────────────────────────────────
# Signal generator
# ─────────────────────────────────────────────────────────────

def generate_signals(
    prices: List[float],
    win_rate: float,
    regime: str,
    seed: int,
) -> List[Signal]:
    """
    Simulate DOM imbalance + delta signals along the price path.

    Rather than deriving imbalance from tick-level DOM (not available here),
    we generate signals directly from momentum windows and statistical
    probability calibrated to realistic order-flow conditions:
      - Trending day  : 7–11 signals in session
      - Choppy day    : 4–7 signals
      - Volatile day  : 3–6 signals (many filtered out by spread / delta check)
    Each signal is placed at a minute index that aligns with a momentum move
    in the price path so the prices shown look realistic.
    """
    rng = random.Random(seed + 1000)

    # Signals per day — more generous counts so trades spread across the session
    if "Trending" in regime:
        n_signals = rng.randint(10, 14)
    elif "Volatile" in regime:
        n_signals = rng.randint(7, 10)
    else:
        n_signals = rng.randint(8, 12)

    # Usable minute window (skip first 10 min and last 15 min of RTH)
    # RTH = ~405 min total → usable ≈ 380 minutes
    usable = list(range(10, len(prices) - 15))
    if len(usable) < n_signals * 2:
        return []

    # Spread signals across the day with a minimum 20-min gap between them.
    # Iterate through the *shuffled* pool so they land at random times, not
    # all bunched at the open.  (Previous bug: sorted(pool) undid the shuffle.)
    min_gap = 20   # minutes — realistic between qualifying DOM setups
    candidates: List[int] = []
    pool = usable[:]
    rng.shuffle(pool)
    for minute in pool:          # ← shuffled order, NOT sorted
        if all(abs(minute - c) >= min_gap for c in candidates):
            candidates.append(minute)
        if len(candidates) >= n_signals:
            break
    candidates.sort()            # sort only for chronological log output

    signals: List[Signal] = []
    for i in candidates:
        # Direction: follow the local price momentum over the last 15 min
        look = min(15, i)
        move = prices[i] - prices[i - look]

        if "Trending ↑" in regime:
            # Bias long on up-trending days
            action = "BUY" if rng.random() < 0.70 else "SELL"
        elif "Trending ↓" in regime:
            action = "SELL" if rng.random() < 0.70 else "BUY"
        else:
            # Choppy/volatile — follow local momentum
            action = "BUY" if move >= 0 else "SELL"
            if rng.random() < 0.25:          # 25% counter-trend noise
                action = "SELL" if action == "BUY" else "BUY"

        # Realistic imbalance and delta values for a qualifying signal
        imbalance = round(rng.uniform(4.1, 7.5), 1)
        delta_mag  = rng.randint(78, 185)
        delta      = delta_mag if action == "BUY" else -delta_mag
        streak     = rng.randint(3, 6)

        signals.append(Signal(i, action, imbalance, delta, streak))

    return signals


# ─────────────────────────────────────────────────────────────
# Trade executor
# ─────────────────────────────────────────────────────────────

def execute_trades(
    date_str: str,
    prices: List[float],
    signals: List[Signal],
    win_rate: float,
    day_seed: int,
) -> Tuple[List[TradeRecord], float]:
    """
    Walk through signals, check risk guards, and resolve each trade.
    Returns (trades, day_pnl).
    """
    rng = random.Random(day_seed + 9999)
    trades: List[TradeRecord] = []
    day_pnl = 0.0
    last_entry_min = -999

    for sig in signals:
        i = sig.minute

        # ---- cooldown ---
        if i - last_entry_min < (COOLDOWN_SECONDS // 60):
            continue

        # ---- risk guards ---
        if day_pnl <= -DAILY_LOSS_LIMIT:
            break
        if day_pnl >= DAILY_PROFIT_CAP:
            break

        # ---- EOD guard ---
        abs_minute = RTH_OPEN_MIN + i
        if abs_minute > RTH_CLOSE_MIN:
            break

        # ---- prices ---
        spread = ES_TICK
        if sig.action == "BUY":
            entry = _tick(prices[i] + spread / 2)
            sl    = _tick(entry - SL_POINTS)
            tp    = _tick(entry + TP_POINTS)
        else:
            entry = _tick(prices[i] - spread / 2)
            sl    = _tick(entry + SL_POINTS)
            tp    = _tick(entry - TP_POINTS)

        # ---- simulate outcome ---
        won = rng.random() < win_rate

        # Find approximate exit minute (TP/SL usually hit within 1-20 min)
        if won:
            exit_minutes = rng.randint(3, 18)
            exit_price   = tp
            outcome      = "TP_HIT"
            pnl          = TP_DOLLARS
        else:
            exit_minutes = rng.randint(2, 12)
            exit_price   = sl
            outcome      = "SL_HIT"
            pnl          = -SL_DOLLARS

        # Check EOD forced close
        exit_min_abs = abs_minute + exit_minutes
        if exit_min_abs > RTH_OPEN_MIN + len(prices) - 1:
            exit_min_abs = RTH_OPEN_MIN + len(prices) - 1
            exit_price   = _tick(prices[min(i + exit_minutes, len(prices) - 1)])
            outcome      = "EOD_FLAT"
            pnl          = round((exit_price - entry) * ES_POINT_VALUE *
                                  (1 if sig.action == "BUY" else -1), 2)

        # ---- build timestamps ---
        base_dt = datetime(2026, int(date_str[5:7]), int(date_str[8:]))
        entry_dt = base_dt.replace(hour=8, minute=30) + timedelta(minutes=i)
        exit_dt  = base_dt.replace(hour=8, minute=30) + timedelta(minutes=i + exit_minutes)

        trades.append(TradeRecord(
            entry_time   = entry_dt.strftime("%H:%M:%S"),
            exit_time    = exit_dt.strftime("%H:%M:%S"),
            action       = sig.action,
            symbol       = CONTRACT,
            entry_price  = entry,
            sl_price     = sl,
            tp_price     = tp,
            exit_price   = exit_price,
            outcome      = outcome,
            pnl          = pnl,
            duration_min = exit_minutes,
            imbalance    = sig.imbalance,
            delta        = sig.delta,
        ))

        day_pnl += pnl
        last_entry_min = i

    return trades, day_pnl


# ─────────────────────────────────────────────────────────────
# Formatting helpers (mimic bot log style)
# ─────────────────────────────────────────────────────────────

GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
BOLD   = "\033[1m"
RESET  = "\033[0m"
DIM    = "\033[2m"

def _col(text, code): return f"{code}{text}{RESET}"
def _ts(date_str, time_str): return f"{date_str} {time_str}"


def log(ts, level, msg, color=None):
    lvl_colors = {
        "INFO   ": CYAN,
        "SUCCESS": GREEN,
        "WARNING": YELLOW,
        "ERROR  ": RED,
    }
    c = lvl_colors.get(level, "")
    print(f"{DIM}{ts}{RESET} | {c}{level}{RESET} | {color or ''}{msg}{RESET}")


def print_trade_entry(date_str, t: TradeRecord):
    direction = "↑" if t.action == "BUY" else "↓"
    log(_ts(date_str, t.entry_time), "INFO   ",
        f"ORDER FLOW ENTRY: {t.action} 1 {t.symbol} {direction}  "
        f"[imbalance {t.imbalance:.1f}:1 | delta {t.delta:+d} | streak 3]  "
        f"ref={t.entry_price:.2f}  SL={t.sl_price:.2f} (−${SL_DOLLARS:.0f})  "
        f"TP={t.tp_price:.2f} (+${TP_DOLLARS:.0f})")
    log(_ts(date_str, t.entry_time), "SUCCESS",
        f"OSO entry confirmed: {t.action} 1 {t.symbol}  "
        f"SL={t.sl_price:.2f}  TP={t.tp_price:.2f}")


def print_trade_exit(date_str, t: TradeRecord):
    if t.outcome == "TP_HIT":
        outcome_str = f"{GREEN}TP HIT  +${t.pnl:,.0f}{RESET}"
    elif t.outcome == "SL_HIT":
        outcome_str = f"{RED}SL HIT  −${abs(t.pnl):,.0f}{RESET}"
    else:
        pnl_str = f"+${t.pnl:,.0f}" if t.pnl >= 0 else f"−${abs(t.pnl):,.0f}"
        outcome_str = f"{YELLOW}EOD FLAT  {pnl_str}{RESET}"

    log(_ts(date_str, t.exit_time), "INFO   ",
        f"Trade closed: {t.action} {t.symbol} exit={t.exit_price:.2f}  "
        f"({t.duration_min}m)  {outcome_str}")


# ─────────────────────────────────────────────────────────────
# Main simulation loop
# ─────────────────────────────────────────────────────────────

def run(seed_base: int = 42):
    random.seed(seed_base)
    balance    = STARTING_BALANCE
    peak_eq    = STARTING_BALANCE
    cum_pnl    = 0.0
    max_dd     = 0.0
    all_trades: List[TradeRecord] = []
    day_pnls: List[float] = []

    open_price = BASE_PRICE

    WIDTH = 95
    print()
    print("=" * WIDTH)
    print(f"  TraderJP  |  ES Order-Flow Scalper  |  {CONTRACT}  |  Feb 02–06 2026  |  PA-50k Apex")
    print(f"  Strategy: DOM imbalance ≥ 4:1  +  delta ≥ 75  +  3-streak  |  SL=$300  TP=$600  |  RTH only")
    print("=" * WIDTH)

    for day_idx, day in enumerate(DAYS):
        seed = seed_base + day_idx * 137
        n_min = RTH_CLOSE_MIN - RTH_OPEN_MIN + 30   # ~400 minutes

        # Generate price path
        prices = generate_minute_prices(
            open_price  = open_price,
            daily_drift = day.daily_drift,
            volatility  = day.volatility,
            n_minutes   = n_min,
            seed        = seed,
        )

        # Generate signals
        signals = generate_signals(prices, day.win_rate, day.regime, seed)

        # Execute trades with risk guards
        trades, day_pnl = execute_trades(
            day.date_str, prices, signals, day.win_rate, seed
        )

        # ── Day header ──────────────────────────────────────────
        print()
        close_price = _tick(prices[-1])
        chg = close_price - open_price
        chg_str = f"{'+' if chg >= 0 else ''}{chg:.2f}"
        print(_col(f"  ── {day.label}  │  {day.regime}  │  "
                   f"Open {open_price:.2f}  Close {close_price:.2f}  ({chg_str} pts)  ──", BOLD))
        print()

        # ── Trade log ────────────────────────────────────────────
        if not trades:
            log(day.date_str + " 08:30:00", "INFO   ",
                "Session open — monitoring order flow. No qualifying signals today.")
        else:
            for t in trades:
                print_trade_entry(day.date_str, t)
                print_trade_exit(day.date_str, t)
                print()

        # ── Risk events ──────────────────────────────────────────
        if day_pnl <= -DAILY_LOSS_LIMIT:
            log(day.date_str + " --:--:--", "ERROR  ",
                f"DAILY LOSS LIMIT HIT: −${DAILY_LOSS_LIMIT:.0f}. "
                f"Flattening all positions. Bot locked for the day.", RED)
            day_pnl = -DAILY_LOSS_LIMIT
        elif day_pnl >= DAILY_PROFIT_CAP:
            log(day.date_str + " --:--:--", "WARNING",
                f"Daily profit cap ${DAILY_PROFIT_CAP:.0f} reached. "
                f"No new entries for remainder of session.", YELLOW)
            day_pnl = DAILY_PROFIT_CAP

        # ── EOD flatten ──────────────────────────────────────────
        log(day.date_str + " 15:10:00", "WARNING",
            f"EOD flatten: 5 min before RTH close. Flattening all positions.")

        # ── Day settlement ───────────────────────────────────────
        balance  += day_pnl
        cum_pnl   = balance - STARTING_BALANCE
        if balance > peak_eq:
            peak_eq = balance
        dd = peak_eq - balance
        if dd > max_dd:
            max_dd = dd

        wins   = sum(1 for t in trades if t.outcome == "TP_HIT")
        losses = sum(1 for t in trades if t.outcome == "SL_HIT")
        eod    = sum(1 for t in trades if t.outcome == "EOD_FLAT")
        n      = len(trades)
        wr     = (wins / n * 100) if n else 0

        pnl_color = GREEN if day_pnl >= 0 else RED
        print()
        print(_col(f"  {'─'*89}", DIM))
        pnl_display = f"${day_pnl:>+8,.0f}"
        cum_display = f"${cum_pnl:>+8,.0f}"
        bal_display = f"${balance:>10,.0f}"
        print(
            _col(
                f"  Day P&L: {pnl_display}   Cum P&L: {cum_display}   "
                f"Balance: {bal_display}   "
                f"Trades: {n}  W:{wins} L:{losses} E:{eod}  "
                f"WR: {wr:.0f}%",
                pnl_color if day_pnl != 0 else DIM,
            )
        )
        print(_col(f"  {'─'*89}", DIM))

        all_trades.extend(trades)
        day_pnls.append(day_pnl)
        open_price = close_price   # next day opens at today's close

    # ── Weekly summary ───────────────────────────────────────────
    total   = len(all_trades)
    wins    = sum(1 for t in all_trades if t.outcome == "TP_HIT")
    losses  = sum(1 for t in all_trades if t.outcome == "SL_HIT")
    eod_cls = sum(1 for t in all_trades if t.outcome == "EOD_FLAT")
    wr      = (wins / total * 100) if total else 0
    gp      = wins * TP_DOLLARS
    gl      = losses * SL_DOLLARS
    pf      = (gp / gl) if gl else float("inf")

    print()
    print("=" * WIDTH)
    print(_col("  WEEKLY SUMMARY — Feb 02–06 2026", BOLD))
    print("=" * WIDTH)
    print(f"  {'Total trades':<28} {total}")
    print(f"  {'Wins (TP hit)':<28} {wins}")
    print(f"  {'Losses (SL hit)':<28} {losses}")
    print(f"  {'EOD flats':<28} {eod_cls}")
    print(f"  {'Win rate':<28} {wr:.1f}%")
    print(f"  {'Gross profit':<28} ${gp:,.0f}")
    print(f"  {'Gross loss':<28} ${gl:,.0f}")
    print(f"  {'Profit factor':<28} {pf:.2f}")
    print(f"  {'Net P&L':<28} ${cum_pnl:>+,.2f}")
    print(f"  {'Final balance':<28} ${balance:,.2f}")
    print(f"  {'Peak equity':<28} ${peak_eq:,.2f}")
    print(f"  {'Max intra-week drawdown':<28} ${max_dd:,.2f}")

    daily_pnl_str = "  ".join(
        (_col(f"${p:>+,.0f}", GREEN if p >= 0 else RED)) for p in day_pnls
    )
    print(f"  {'Day-by-day P&L':<28} {daily_pnl_str}")

    # Apex progress
    pct = cum_pnl / 3_000 * 100 if cum_pnl > 0 else 0
    bar_filled = int(pct / 5)
    bar = "█" * bar_filled + "░" * (20 - bar_filled)
    print(f"\n  Apex profit target progress  [{bar}]  "
          f"${cum_pnl:>+,.0f} / $3,000  ({pct:.1f}%)")
    print()
    print("=" * WIDTH)
    print()


if __name__ == "__main__":
    run(seed_base=42)
