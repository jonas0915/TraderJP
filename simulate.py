"""
TraderJP — 1-Month Monte Carlo Simulation
==========================================
Simulates the order-flow scalping strategy using the exact parameters
configured in .env for a PA-50k Apex account.

Strategy recap (from order_flow_strategy.py + .env):
  - Entry : DOM bid/ask imbalance + cumulative delta + 3-bar streak confirmation
  - Exit  : Fixed OSO bracket — TP $600 (+12 ES pts) / SL $300 (−6 ES pts)  → 2:1 R:R
  - Filter: RTH only (08:30–15:15 CT), news blackout, 60-sec cooldown
  - Size  : 1 contract

Apex PA-50k limits enforced per day:
  - Daily loss cap    : $500   → bot flattens + locks for the day
  - Daily profit cap  : $1,000 → no new entries after this is hit
  - Max trailing DD   : $2,500 → bot flattens + locks permanently

Simulation period: February 2026 (19 trading days)
Monte Carlo runs : 1,000 independent paths

Usage:
    python3 simulate.py
"""

import random
import statistics
from dataclasses import dataclass, field
from typing import List, Tuple

# ─────────────────────────────────────────────────
# Constants matching the live bot
# ─────────────────────────────────────────────────

STARTING_BALANCE    = 50_000.0
TP_DOLLARS          = 600.0
SL_DOLLARS          = 300.0
DAILY_LOSS_LIMIT    = 500.0
DAILY_PROFIT_CAP    = 1_000.0
MAX_TRAILING_DD     = 2_500.0
PROFIT_TARGET       = 3_000.0

# ─────────────────────────────────────────────────
# Simulation assumptions
# ─────────────────────────────────────────────────

# Market regime probabilities and their estimated win rates.
# Order-flow scalping with tight multi-filter confirmation:
#   Trending day  (30 %) : cleaner flow, 52% win rate
#   Choppy day    (50 %) : noisy DOM,    42% win rate
#   News/volatile (20 %) : erratic flow, 35% win rate  (many filtered out)
REGIMES = [
    ("Trending",  0.30, 0.52),
    ("Choppy",    0.50, 0.42),
    ("Volatile",  0.20, 0.35),
]

# Trades per RTH session after all filters (cooldown, streak, delta accel, spread)
# Modelled as a discrete uniform draw in the given range.
MIN_TRADES_PER_DAY = 4
MAX_TRADES_PER_DAY = 12

# February 2026 trading days (Presidents Day = Feb 16 is a holiday)
TRADING_DAYS = [
    "Mon Feb 02", "Tue Feb 03", "Wed Feb 04", "Thu Feb 05", "Fri Feb 06",
    "Mon Feb 09", "Tue Feb 10", "Wed Feb 11", "Thu Feb 12", "Fri Feb 13",
    "Tue Feb 17", "Wed Feb 18", "Thu Feb 19", "Fri Feb 20",
    "Mon Feb 23", "Tue Feb 24", "Wed Feb 25", "Thu Feb 26", "Fri Feb 27",
]

MONTE_CARLO_RUNS = 1_000


# ─────────────────────────────────────────────────
# Data classes
# ─────────────────────────────────────────────────

@dataclass
class Trade:
    day:    str
    action: str    # BUY / SELL
    result: str    # WIN / LOSS
    pnl:    float


@dataclass
class DayResult:
    date:       str
    regime:     str
    win_rate:   float
    trades:     int
    wins:       int
    losses:     int
    day_pnl:    float
    cum_pnl:    float
    balance:    float
    risk_event: str   # "" / "DAILY_LOSS_CAP" / "PROFIT_CAP" / "TRAILING_DD"


@dataclass
class SimResult:
    days:           List[DayResult] = field(default_factory=list)
    trades:         List[Trade]     = field(default_factory=list)
    final_balance:  float = 0.0
    net_pnl:        float = 0.0
    max_dd:         float = 0.0
    peak_equity:    float = 0.0
    total_trades:   int   = 0
    total_wins:     int   = 0
    locked_out:     bool  = False   # hit max trailing DD
    passed_target:  bool  = False


# ─────────────────────────────────────────────────
# Simulation engine
# ─────────────────────────────────────────────────

def _pick_regime() -> Tuple[str, float]:
    r = random.random()
    cumulative = 0.0
    for name, prob, win_rate in REGIMES:
        cumulative += prob
        if r < cumulative:
            return name, win_rate
    return REGIMES[-1][0], REGIMES[-1][2]


def run_simulation(seed: int = None) -> SimResult:
    if seed is not None:
        random.seed(seed)

    result      = SimResult()
    balance     = STARTING_BALANCE
    peak_equity = STARTING_BALANCE
    cum_pnl     = 0.0
    max_dd      = 0.0
    locked_out  = False   # permanent lock after max trailing DD hit

    for day_label in TRADING_DAYS:
        if locked_out:
            # Account locked — no more trading
            result.days.append(DayResult(
                date=day_label, regime="—", win_rate=0,
                trades=0, wins=0, losses=0,
                day_pnl=0.0, cum_pnl=cum_pnl, balance=balance,
                risk_event="LOCKED OUT",
            ))
            continue

        regime, win_rate = _pick_regime()
        n_trades         = random.randint(MIN_TRADES_PER_DAY, MAX_TRADES_PER_DAY)

        day_pnl    = 0.0
        day_wins   = 0
        day_losses = 0
        risk_event = ""

        for _ in range(n_trades):
            # ---- daily risk checks before each trade ----
            if day_pnl <= -DAILY_LOSS_LIMIT:
                risk_event = "DAILY_LOSS_CAP"
                break
            if day_pnl >= DAILY_PROFIT_CAP:
                risk_event = "PROFIT_CAP"
                break

            # Trailing drawdown check
            current_equity = balance + day_pnl
            dd = peak_equity - current_equity
            if dd >= MAX_TRAILING_DD:
                risk_event = "TRAILING_DD"
                locked_out = True
                break

            # ---- simulate trade outcome ----
            won    = random.random() < win_rate
            pnl    = TP_DOLLARS if won else -SL_DOLLARS
            action = random.choice(["BUY", "SELL"])

            day_pnl += pnl
            if won:
                day_wins += 1
            else:
                day_losses += 1

            result.trades.append(Trade(day=day_label, action=action,
                                       result="WIN" if won else "LOSS", pnl=pnl))

        # ---- day settlement ----
        # Clamp day_pnl: never lose more than daily limit or gain more than cap
        if day_pnl < -DAILY_LOSS_LIMIT:
            day_pnl = -DAILY_LOSS_LIMIT
        if day_pnl > DAILY_PROFIT_CAP and not risk_event:
            day_pnl = DAILY_PROFIT_CAP

        balance  += day_pnl
        cum_pnl   = balance - STARTING_BALANCE

        # Update peak and drawdown
        if balance > peak_equity:
            peak_equity = balance
        current_dd = peak_equity - balance
        if current_dd > max_dd:
            max_dd = current_dd

        result.days.append(DayResult(
            date=day_label, regime=regime, win_rate=win_rate,
            trades=day_wins + day_losses,
            wins=day_wins, losses=day_losses,
            day_pnl=day_pnl, cum_pnl=cum_pnl, balance=balance,
            risk_event=risk_event,
        ))

    result.final_balance = balance
    result.net_pnl       = cum_pnl
    result.max_dd        = max_dd
    result.peak_equity   = peak_equity
    result.total_trades  = len(result.trades)
    result.total_wins    = sum(1 for t in result.trades if t.result == "WIN")
    result.locked_out    = locked_out
    result.passed_target = cum_pnl >= PROFIT_TARGET
    return result


# ─────────────────────────────────────────────────
# Reporting
# ─────────────────────────────────────────────────

def print_single_sim(sim: SimResult):
    WIDTH = 97
    print("=" * WIDTH)
    print("  TraderJP — ES Order-Flow Strategy  |  February 2026 Simulation  |  PA-50k Account")
    print("=" * WIDTH)

    header = (
        f"{'Date':<14} {'Regime':<10} {'Trades':>6} {'W':>4} {'L':>4}"
        f" {'Day P&L':>9} {'Cum P&L':>10} {'Balance':>11}  {'Note'}"
    )
    print(header)
    print("-" * WIDTH)

    for d in sim.days:
        note = d.risk_event if d.risk_event else ""
        day_pnl_str = f"${d.day_pnl:>+8,.0f}"
        cum_pnl_str = f"${d.cum_pnl:>+9,.0f}"
        bal_str     = f"${d.balance:>10,.0f}"
        wr_str      = f"{d.win_rate*100:.0f}%" if d.win_rate else "  —"
        print(
            f"{d.date:<14} {d.regime:<6} {wr_str:<4}  {d.trades:>4}  "
            f"{d.wins:>3}  {d.losses:>3}"
            f"  {day_pnl_str}  {cum_pnl_str}  {bal_str}  {note}"
        )

    print("-" * WIDTH)
    total_trades = sim.total_trades
    total_wins   = sim.total_wins
    total_losses = total_trades - total_wins
    win_rate     = (total_wins / total_trades * 100) if total_trades else 0
    gross_profit = total_wins * TP_DOLLARS
    gross_loss   = total_losses * SL_DOLLARS
    profit_factor = (gross_profit / gross_loss) if gross_loss else float("inf")

    print(f"\n{'MONTHLY SUMMARY':─^{WIDTH}}")
    print(f"  Total trades   : {total_trades}")
    print(f"  Wins / Losses  : {total_wins} / {total_losses}  ({win_rate:.1f}% win rate)")
    print(f"  Gross profit   : ${gross_profit:,.0f}")
    print(f"  Gross loss     : ${gross_loss:,.0f}")
    print(f"  Net P&L        : ${sim.net_pnl:>+,.2f}")
    print(f"  Final balance  : ${sim.final_balance:,.2f}")
    print(f"  Profit factor  : {profit_factor:.2f}")
    print(f"  Max drawdown   : ${sim.max_dd:,.2f}")
    print(f"  Peak equity    : ${sim.peak_equity:,.2f}")
    status = "PASS — Profit target reached!" if sim.passed_target else (
             "FAIL — Trailing DD limit hit" if sim.locked_out else
             "OPEN — Target not yet reached")
    print(f"  Apex status    : {status}")
    print()


def print_monte_carlo(results: List[SimResult]):
    WIDTH = 60
    net_pnls      = [r.net_pnl   for r in results]
    max_dds       = [r.max_dd    for r in results]
    pass_rate     = sum(1 for r in results if r.passed_target) / len(results) * 100
    lockout_rate  = sum(1 for r in results if r.locked_out)    / len(results) * 100
    trade_counts  = [r.total_trades for r in results]
    win_rates     = [
        r.total_wins / r.total_trades * 100 if r.total_trades else 0
        for r in results
    ]

    print(f"\n{'MONTE CARLO — 1,000 SIMULATIONS':═^{WIDTH}}")
    print(f"  Metric                      {'Value':>16}")
    print(f"  {'─'*44}")
    print(f"  Pass rate (≥ $3k profit)    {pass_rate:>14.1f}%")
    print(f"  Lockout rate (trailing DD)  {lockout_rate:>14.1f}%")
    print(f"  ─")
    print(f"  Net P&L — median            ${statistics.median(net_pnls):>+14,.0f}")
    print(f"  Net P&L — mean              ${statistics.mean(net_pnls):>+14,.0f}")
    print(f"  Net P&L — best 10%          ${sorted(net_pnls)[int(len(net_pnls)*0.90)]:>+14,.0f}")
    print(f"  Net P&L — worst 10%         ${sorted(net_pnls)[int(len(net_pnls)*0.10)]:>+14,.0f}")
    print(f"  Net P&L — best case         ${max(net_pnls):>+14,.0f}")
    print(f"  Net P&L — worst case        ${min(net_pnls):>+14,.0f}")
    print(f"  ─")
    print(f"  Max DD  — median            ${statistics.median(max_dds):>14,.0f}")
    print(f"  Max DD  — worst 10%         ${sorted(max_dds)[int(len(max_dds)*0.90)]:>14,.0f}")
    print(f"  ─")
    print(f"  Avg trades / month          {statistics.mean(trade_counts):>14.0f}")
    print(f"  Avg win rate                {statistics.mean(win_rates):>14.1f}%")

    # P&L distribution buckets
    buckets = [
        ("Profit > $3,000 (PASS)",   lambda x: x >= 3_000),
        ("Profit $1,000–$2,999",      lambda x: 1_000 <= x < 3_000),
        ("Profit $0–$999",            lambda x: 0 <= x < 1_000),
        ("Loss $0–$499",              lambda x: -500 < x < 0),
        ("Loss ≥ $500",               lambda x: x <= -500),
    ]
    print(f"\n  {'P&L Distribution':─^44}")
    for label, fn in buckets:
        count = sum(1 for p in net_pnls if fn(p))
        bar   = "█" * (count // 20)
        print(f"  {label:<30} {count:>4} / 1000  {bar}")

    print()
    print("  ─────────────────────────────────────────────────")
    print("  NOTE: Simulation uses statistical modelling only.")
    print("  Actual results depend on live market conditions,")
    print("  signal quality, slippage, and execution latency.")
    print("=" * WIDTH)


# ─────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────

if __name__ == "__main__":
    print("\n  Running single deterministic simulation (seed=42)...")
    single = run_simulation(seed=42)
    print_single_sim(single)

    print(f"  Running {MONTE_CARLO_RUNS:,} Monte Carlo paths...")
    mc_results = [run_simulation() for _ in range(MONTE_CARLO_RUNS)]
    print_monte_carlo(mc_results)
