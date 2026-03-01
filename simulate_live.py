"""
TraderJP — Full Month Live Trade Simulation
============================================
Simulates all 21 trading days in March 2026 trade-by-trade with:
  - Realistic ES minute-bar price path (GBM + intraday structure)
  - Bot-style log output matching what you'd see in the terminal
  - Exact entry/SL/TP prices, timestamps, trade duration
  - Contract rollover: ESH6 → ESM6 on Mar 13 (5 days before Mar expiry)
  - FOMC week (Mar 16-20) modelled as elevated volatility
  - Weekly summaries + monthly summary with Apex progress

Usage:
    python3 simulate_live.py
"""

import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

# ─────────────────────────────────────────────────────────────
# Bot parameters (exact match to .env / strategy code)
# ─────────────────────────────────────────────────────────────

STARTING_BALANCE   = 50_000.0
TP_DOLLARS         = 600.0
SL_DOLLARS         = 300.0
ES_POINT_VALUE     = 50.0
ES_TICK            = 0.25
TP_POINTS          = TP_DOLLARS / ES_POINT_VALUE   # 12.0 pts
SL_POINTS          = SL_DOLLARS / ES_POINT_VALUE   #  6.0 pts
DAILY_LOSS_LIMIT   = 500.0
DAILY_PROFIT_CAP   = 1_000.0
MAX_TRAILING_DD    = 2_500.0
COOLDOWN_SECONDS   = 60
BASE_PRICE         = 6_050.00    # ES level entering March 2026

RTH_OPEN_MIN  =  8 * 60 + 30    # 510
RTH_CLOSE_MIN = 15 * 60 + 10    # 910 — last entry 5 min before RTH close

# ─────────────────────────────────────────────────────────────
# March 2026 — 21 trading days
# Contract: ESH6 through Mar 12, ESM6 from Mar 13 (roll day)
# FOMC:     March 18 decision day
# ES expiry: March 20 (triple witching)
# ─────────────────────────────────────────────────────────────

@dataclass
class DayProfile:
    label:       str
    date_str:    str     # "2026-03-DD"
    contract:    str     # "ESH6" or "ESM6"
    daily_drift: float   # net point move expected over the session
    volatility:  float   # intraday σ per minute
    regime:      str
    win_rate:    float
    note:        str = ""

DAYS = [
    # ── Week 1 ─────────────────────────────────────────────────────
    DayProfile("Mon Mar 02","2026-03-02","ESH6", +12.0,0.45,"Choppy    ↔",0.43),
    DayProfile("Tue Mar 03","2026-03-03","ESH6", +24.0,0.55,"Trending ↑",0.54),
    DayProfile("Wed Mar 04","2026-03-04","ESH6",  -9.0,0.75,"Volatile  ↕",0.38),
    DayProfile("Thu Mar 05","2026-03-05","ESH6",  +6.0,0.42,"Choppy    ↔",0.44),
    DayProfile("Fri Mar 06","2026-03-06","ESH6", -20.0,0.62,"Trending ↓",0.51, "NFP"),
    # ── Week 2  (rollover week) ────────────────────────────────────
    DayProfile("Mon Mar 09","2026-03-09","ESH6", -14.0,0.58,"Trending ↓",0.50),
    DayProfile("Tue Mar 10","2026-03-10","ESH6",  +9.0,0.48,"Choppy    ↔",0.43),
    DayProfile("Wed Mar 11","2026-03-11","ESH6", -26.0,0.90,"Volatile  ↕",0.36, "CPI"),
    DayProfile("Thu Mar 12","2026-03-12","ESH6", +21.0,0.65,"Trending ↑",0.53),
    DayProfile("Fri Mar 13","2026-03-13","ESM6",  +3.0,0.44,"Choppy    ↔",0.44, "ROLL→ESM6"),
    # ── Week 3  (FOMC week + ES expiry) ───────────────────────────
    DayProfile("Mon Mar 16","2026-03-16","ESM6", +16.0,0.50,"Trending ↑",0.54),
    DayProfile("Tue Mar 17","2026-03-17","ESM6",  +4.0,0.46,"Choppy    ↔",0.43, "FOMC eve"),
    DayProfile("Wed Mar 18","2026-03-18","ESM6", -32.0,1.10,"Volatile  ↕",0.34, "FOMC day"),
    DayProfile("Thu Mar 19","2026-03-19","ESM6", +22.0,0.68,"Trending ↑",0.55, "Post-FOMC"),
    DayProfile("Fri Mar 20","2026-03-20","ESM6",  -6.0,0.60,"Choppy    ↔",0.42, "Triple witch"),
    # ── Week 4 ─────────────────────────────────────────────────────
    DayProfile("Mon Mar 23","2026-03-23","ESM6", -13.0,0.52,"Trending ↓",0.50),
    DayProfile("Tue Mar 24","2026-03-24","ESM6",  +7.0,0.44,"Choppy    ↔",0.44),
    DayProfile("Wed Mar 25","2026-03-25","ESM6", +18.0,0.55,"Trending ↑",0.53),
    DayProfile("Thu Mar 26","2026-03-26","ESM6",  -4.0,0.47,"Choppy    ↔",0.43),
    # ── Week 5  (month-end) ────────────────────────────────────────
    DayProfile("Mon Mar 30","2026-03-30","ESM6",  +8.0,0.46,"Trending ↑",0.52),
    DayProfile("Tue Mar 31","2026-03-31","ESM6", -11.0,0.70,"Volatile  ↕",0.39, "Month-end"),
]

WEEK_LABELS = [
    "Week 1  Mar 02–06",
    "Week 2  Mar 09–13  (Rollover)",
    "Week 3  Mar 16–20  (FOMC / ES Expiry)",
    "Week 4  Mar 23–26",
    "Week 5  Mar 30–31  (Month-end)",
]
WEEK_SLICES = [
    slice(0,  5),
    slice(5,  10),
    slice(10, 15),
    slice(15, 19),
    slice(19, 21),
]

# ─────────────────────────────────────────────────────────────
# Price generation
# ─────────────────────────────────────────────────────────────

def _tick(p: float) -> float:
    return round(round(p / ES_TICK) * ES_TICK, 2)


def generate_minute_prices(open_price, daily_drift, volatility, n_minutes, seed):
    rng = random.Random(seed)
    prices = [open_price]
    drift_per_min = daily_drift / n_minutes
    for i in range(1, n_minutes):
        prev = prices[-1]
        if i < 30:          vol_scale = 1.6
        elif 120 <= i < 180: vol_scale = 0.5
        else:                vol_scale = 1.0
        noise = rng.gauss(0, volatility * vol_scale)
        prices.append(max(_tick(prev + drift_per_min + noise), 1.0))
    return prices


# ─────────────────────────────────────────────────────────────
# Signal generation
# ─────────────────────────────────────────────────────────────

@dataclass
class Signal:
    minute: int; action: str; imbalance: float; delta: int; streak: int


def generate_signals(prices, win_rate, regime, seed):
    rng = random.Random(seed + 1000)
    if "Trending" in regime:   n_signals = rng.randint(10, 14)
    elif "Volatile" in regime: n_signals = rng.randint(7,  10)
    else:                      n_signals = rng.randint(8,  12)

    usable = list(range(10, len(prices) - 15))
    if len(usable) < n_signals * 2:
        return []

    min_gap = 20
    candidates = []
    pool = usable[:]
    rng.shuffle(pool)
    for minute in pool:
        if all(abs(minute - c) >= min_gap for c in candidates):
            candidates.append(minute)
        if len(candidates) >= n_signals:
            break
    candidates.sort()

    signals = []
    for i in candidates:
        look = min(15, i)
        move = prices[i] - prices[i - look]
        if "Trending ↑" in regime:
            action = "BUY"  if rng.random() < 0.70 else "SELL"
        elif "Trending ↓" in regime:
            action = "SELL" if rng.random() < 0.70 else "BUY"
        else:
            action = "BUY" if move >= 0 else "SELL"
            if rng.random() < 0.25:
                action = "SELL" if action == "BUY" else "BUY"
        imbalance = round(rng.uniform(4.1, 7.5), 1)
        delta_mag  = rng.randint(78, 185)
        delta      = delta_mag if action == "BUY" else -delta_mag
        streak     = rng.randint(3, 6)
        signals.append(Signal(i, action, imbalance, delta, streak))
    return signals


# ─────────────────────────────────────────────────────────────
# Trade executor
# ─────────────────────────────────────────────────────────────

@dataclass
class TradeRecord:
    entry_time: str; exit_time: str; action: str; symbol: str
    entry_price: float; sl_price: float; tp_price: float; exit_price: float
    outcome: str; pnl: float; duration_min: int; imbalance: float; delta: int


def execute_trades(date_str, contract, prices, signals, win_rate, day_seed):
    rng = random.Random(day_seed + 9999)
    trades, day_pnl, last_entry_min = [], 0.0, -999

    for sig in signals:
        i = sig.minute
        if i - last_entry_min < (COOLDOWN_SECONDS // 60): continue
        if day_pnl <= -DAILY_LOSS_LIMIT: break
        if day_pnl >= DAILY_PROFIT_CAP:  break
        if RTH_OPEN_MIN + i > RTH_CLOSE_MIN: break

        if sig.action == "BUY":
            entry = _tick(prices[i] + ES_TICK / 2)
            sl    = _tick(entry - SL_POINTS)
            tp    = _tick(entry + TP_POINTS)
        else:
            entry = _tick(prices[i] - ES_TICK / 2)
            sl    = _tick(entry + SL_POINTS)
            tp    = _tick(entry - TP_POINTS)

        won = rng.random() < win_rate
        if won:
            exit_minutes, exit_price, outcome, pnl = (
                rng.randint(3, 18), tp, "TP_HIT", TP_DOLLARS)
        else:
            exit_minutes, exit_price, outcome, pnl = (
                rng.randint(2, 12), sl, "SL_HIT", -SL_DOLLARS)

        if RTH_OPEN_MIN + i + exit_minutes > RTH_OPEN_MIN + len(prices) - 1:
            exit_price = _tick(prices[min(i + exit_minutes, len(prices) - 1)])
            outcome    = "EOD_FLAT"
            pnl        = round((exit_price - entry) * ES_POINT_VALUE *
                               (1 if sig.action == "BUY" else -1), 2)

        base_dt  = datetime(int(date_str[:4]), int(date_str[5:7]), int(date_str[8:]))
        entry_dt = base_dt.replace(hour=8, minute=30) + timedelta(minutes=i)
        exit_dt  = base_dt.replace(hour=8, minute=30) + timedelta(minutes=i + exit_minutes)

        trades.append(TradeRecord(
            entry_time=entry_dt.strftime("%H:%M:%S"), exit_time=exit_dt.strftime("%H:%M:%S"),
            action=sig.action, symbol=contract,
            entry_price=entry, sl_price=sl, tp_price=tp, exit_price=exit_price,
            outcome=outcome, pnl=pnl, duration_min=exit_minutes,
            imbalance=sig.imbalance, delta=sig.delta,
        ))
        day_pnl += pnl
        last_entry_min = i

    return trades, day_pnl


# ─────────────────────────────────────────────────────────────
# Colours & log helpers
# ─────────────────────────────────────────────────────────────

G = "\033[92m"; R = "\033[91m"; Y = "\033[93m"
C = "\033[96m"; B = "\033[1m";  D = "\033[2m"; X = "\033[0m"

def _c(t, code): return f"{code}{t}{X}"
def _ts(d, t):   return f"{d} {t}"

def log(ts, level, msg, col=""):
    lc = {"INFO   ":C,"SUCCESS":G,"WARNING":Y,"ERROR  ":R}.get(level,"")
    print(f"{D}{ts}{X} | {lc}{level}{X} | {col}{msg}{X}")

def print_entry(d, t):
    arrow = "↑" if t.action == "BUY" else "↓"
    log(_ts(d, t.entry_time), "INFO   ",
        f"ORDER FLOW ENTRY: {t.action} 1 {t.symbol} {arrow}  "
        f"[imbalance {t.imbalance:.1f}:1 | delta {t.delta:+d} | streak 3]  "
        f"ref={t.entry_price:.2f}  SL={t.sl_price:.2f} (−${SL_DOLLARS:.0f})  "
        f"TP={t.tp_price:.2f} (+${TP_DOLLARS:.0f})")
    log(_ts(d, t.entry_time), "SUCCESS",
        f"OSO entry confirmed: {t.action} 1 {t.symbol}  "
        f"SL={t.sl_price:.2f}  TP={t.tp_price:.2f}")

def print_exit(d, t):
    if   t.outcome == "TP_HIT":  o = f"{G}TP HIT   +${t.pnl:,.0f}{X}"
    elif t.outcome == "SL_HIT":  o = f"{R}SL HIT   −${abs(t.pnl):,.0f}{X}"
    else:
        s = f"+${t.pnl:,.0f}" if t.pnl >= 0 else f"−${abs(t.pnl):,.0f}"
        o = f"{Y}EOD FLAT  {s}{X}"
    log(_ts(d, t.exit_time), "INFO   ",
        f"Trade closed: {t.action} {t.symbol}  exit={t.exit_price:.2f}  "
        f"({t.duration_min}m)  {o}")


# ─────────────────────────────────────────────────────────────
# Weekly summary printer
# ─────────────────────────────────────────────────────────────

def print_week_summary(label, week_day_pnls, week_trades, balance, cum_pnl, max_dd):
    W = 95
    total  = len(week_trades)
    wins   = sum(1 for t in week_trades if t.outcome == "TP_HIT")
    losses = sum(1 for t in week_trades if t.outcome == "SL_HIT")
    eod    = total - wins - losses
    wr     = wins / total * 100 if total else 0
    net    = sum(week_day_pnls)
    col    = G if net >= 0 else R
    pnl_by_day = "  ".join(
        _c(f"${p:>+,.0f}", G if p >= 0 else R) for p in week_day_pnls)
    print()
    print(_c(f"  ┌─ {label} ─ Net: ${net:>+,.0f}  │  "
             f"{total} trades  W:{wins} L:{losses} E:{eod}  WR:{wr:.0f}%  │  "
             f"Balance: ${balance:,.0f}  │  {pnl_by_day}", col))


# ─────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────

def run(seed_base: int = 42):
    balance = STARTING_BALANCE
    peak_eq = STARTING_BALANCE
    cum_pnl = 0.0
    max_dd  = 0.0
    open_price = BASE_PRICE
    locked_out = False

    all_trades:  List[TradeRecord] = []
    all_day_pnls: List[float] = []

    W = 95
    print()
    print("=" * W)
    print(f"  TraderJP  |  ES Order-Flow Scalper  |  March 2026 (All 21 Days)  |  PA-50k Apex")
    print(f"  ESH6 → ESM6 rollover Mar 13  |  FOMC Mar 18  |  ES expiry Mar 20")
    print(f"  Strategy: DOM imbalance ≥ 4:1  +  delta ≥ 75  +  3-streak  |  SL=$300  TP=$600")
    print("=" * W)

    for week_idx, (wlabel, wslice) in enumerate(zip(WEEK_LABELS, WEEK_SLICES)):
        week_days   = DAYS[wslice]
        week_trades: List[TradeRecord] = []
        week_pnls:   List[float] = []

        print()
        print(_c(f"  {'━'*W}", D))
        print(_c(f"  {wlabel}", B))
        print(_c(f"  {'━'*W}", D))

        for day_idx_global, day in enumerate(week_days):
            day_idx = wslice.start + day_idx_global
            seed    = seed_base + day_idx * 137
            n_min   = RTH_CLOSE_MIN - RTH_OPEN_MIN + 30

            prices  = generate_minute_prices(open_price, day.daily_drift,
                                             day.volatility, n_min, seed)
            signals = generate_signals(prices, day.win_rate, day.regime, seed)
            trades, day_pnl = execute_trades(
                day.date_str, day.contract, prices, signals, day.win_rate, seed)

            close_price = _tick(prices[-1])
            chg = close_price - open_price
            note_str = f"  [{day.note}]" if day.note else ""
            print()
            print(_c(
                f"  ── {day.label}  │  {day.regime}  │  "
                f"{day.contract}  │  Open {open_price:.2f}  "
                f"Close {close_price:.2f}  ({chg:+.2f} pts){note_str}  ──", B))
            print()

            if locked_out:
                log(day.date_str + " 08:30:00", "ERROR  ",
                    "Account locked — trailing drawdown limit reached. No trading today.", R)
            elif not trades:
                log(day.date_str + " 08:30:00", "INFO   ",
                    "Session open — monitoring order flow. No qualifying signals today.")
            else:
                for t in trades:
                    print_entry(day.date_str, t)
                    print_exit(day.date_str, t)
                    print()

            # Risk events
            if not locked_out:
                if day_pnl <= -DAILY_LOSS_LIMIT:
                    log(day.date_str + " --:--:--", "ERROR  ",
                        f"DAILY LOSS LIMIT HIT: −${DAILY_LOSS_LIMIT:.0f}. "
                        f"Bot locked for the day.", R)
                    day_pnl = -DAILY_LOSS_LIMIT
                elif day_pnl >= DAILY_PROFIT_CAP:
                    log(day.date_str + " --:--:--", "WARNING",
                        f"Daily profit cap ${DAILY_PROFIT_CAP:.0f} reached. "
                        f"No new entries for remainder of session.", Y)
                    day_pnl = DAILY_PROFIT_CAP

            log(day.date_str + " 15:10:00", "WARNING",
                "EOD flatten: 5 min before RTH close. All positions flat.")

            # Settlement
            if not locked_out:
                balance  += day_pnl
            cum_pnl = balance - STARTING_BALANCE
            if balance > peak_eq:
                peak_eq = balance
            dd = peak_eq - balance
            if dd > max_dd:
                max_dd = dd

            # Check trailing DD lockout
            if dd >= MAX_TRAILING_DD and not locked_out:
                locked_out = True
                log(day.date_str + " --:--:--", "ERROR  ",
                    f"MAX TRAILING DRAWDOWN HIT: −${dd:,.0f}. "
                    f"Account permanently locked. No further trading.", R)

            wins_d   = sum(1 for t in trades if t.outcome == "TP_HIT")
            losses_d = sum(1 for t in trades if t.outcome == "SL_HIT")
            eod_d    = len(trades) - wins_d - losses_d
            wr_d     = wins_d / len(trades) * 100 if trades else 0
            col_d    = G if day_pnl >= 0 else R

            print()
            print(_c("  " + "─" * 89, D))
            print(_c(
                f"  Day P&L: ${day_pnl:>+8,.0f}   Cum P&L: ${cum_pnl:>+8,.0f}   "
                f"Balance: ${balance:>10,.0f}   "
                f"Trades: {len(trades)}  W:{wins_d} L:{losses_d} E:{eod_d}  WR:{wr_d:.0f}%",
                col_d))
            print(_c("  " + "─" * 89, D))

            all_trades.extend(trades)
            week_trades.extend(trades)
            all_day_pnls.append(day_pnl)
            week_pnls.append(day_pnl)
            open_price = close_price

        print_week_summary(wlabel, week_pnls, week_trades, balance, cum_pnl, max_dd)

    # ── Monthly summary ───────────────────────────────────────────────
    total   = len(all_trades)
    wins    = sum(1 for t in all_trades if t.outcome == "TP_HIT")
    losses  = sum(1 for t in all_trades if t.outcome == "SL_HIT")
    eod_cls = total - wins - losses
    wr      = wins / total * 100 if total else 0
    gp      = wins   * TP_DOLLARS
    gl      = losses * SL_DOLLARS
    pf      = gp / gl if gl else float("inf")

    print()
    print("=" * W)
    print(_c("  MARCH 2026 — MONTHLY SUMMARY", B))
    print("=" * W)
    rows = [
        ("Trading days",        "21"),
        ("Total trades",        str(total)),
        ("Wins (TP hit)",       f"{wins}"),
        ("Losses (SL hit)",     f"{losses}"),
        ("EOD flats",           f"{eod_cls}"),
        ("Win rate",            f"{wr:.1f}%"),
        ("Gross profit",        f"${gp:,.0f}"),
        ("Gross loss",          f"${gl:,.0f}"),
        ("Profit factor",       f"{pf:.2f}"),
        ("Net P&L",             f"${cum_pnl:>+,.2f}"),
        ("Final balance",       f"${balance:,.2f}"),
        ("Peak equity",         f"${peak_eq:,.2f}"),
        ("Max drawdown",        f"${max_dd:,.2f}"),
        ("Account locked",      "YES — trailing DD" if locked_out else "No"),
    ]
    for k, v in rows:
        print(f"  {k:<28} {v}")

    # Day-by-day P&L strip
    print()
    print("  Day-by-day P&L:")
    for i, (day, pnl) in enumerate(zip(DAYS, all_day_pnls)):
        if i in (0, 5, 10, 15, 19):
            wk = ["Wk1","Wk2","Wk3","Wk4","Wk5"][{0:0,5:1,10:2,15:3,19:4}[i]]
            print(f"\n  {wk}  ", end="")
        col_p = G if pnl >= 0 else R
        print(_c(f"{day.label[-6:]} ${pnl:>+,.0f}", col_p), end="   ")
    print()

    # Apex progress bar
    pct       = max(0, cum_pnl / 3_000 * 100)
    bar_n     = int(min(pct / 5, 20))
    bar       = "█" * bar_n + "░" * (20 - bar_n)
    status    = "✓ PASS" if cum_pnl >= 3_000 else ("✗ LOCKED" if locked_out else "⏳ IN PROGRESS")
    col_status = G if cum_pnl >= 3_000 else (R if locked_out else Y)
    print()
    print(f"  Apex $3k profit target  [{bar}]  "
          f"${cum_pnl:>+,.0f} / $3,000  ({pct:.1f}%)  "
          + _c(status, col_status))
    print()
    print("=" * W)
    print()


if __name__ == "__main__":
    run(seed_base=42)
