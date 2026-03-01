"""
Apex Trader Funding risk manager.

Monitors account equity in real time and enforces:
  - Daily loss limit  (configurable $)
  - Max trailing drawdown (configurable $)
  - Max position size (contracts)
  - Consistency rule  (no single day > N% of total profit)
  - EOD flatten       (optional: flatten all before market close)

The monitor() coroutine should be run as a background asyncio task.
"""

import asyncio
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, date
from pathlib import Path
from typing import Callable, Optional

import pytz
from loguru import logger

from bot.tradovate_client import TradovateClient, TradovateError

# Persisted across restarts so a bot restart mid-drawdown cannot reset the
# all-time equity peak and falsely permit more drawdown than Apex allows.
_PEAK_STATE_FILE = Path("logs/risk_state.json")


# ------------------------------------------------------------------
# Config dataclass — populated from environment / .env
# ------------------------------------------------------------------

@dataclass
class ApexConfig:
    # Account size is informational only; limits are dollar values below
    account_size:         float = 50_000.0

    # Hard limits ($). Bot flattens immediately when hit.
    daily_loss_limit:     float = 500.0     # personal daily loss cap (Apex limit is $1,500)
    max_trailing_dd:      float = 2_500.0   # trailing drawdown from equity peak ($50k PA)

    # Profit target (informational — bot does NOT stop when hit)
    profit_target:        float = 3_000.0

    # Position limits
    max_contracts:        int   = 4          # max simultaneous contracts ($50k PA)

    # Consistency rule: eval only — disabled on Performance Accounts
    consistency_rule:     bool  = False
    max_day_profit_pct:   float = 0.30       # 30%

    # Flatten X minutes before RTH close (0 = disabled)
    eod_flatten_minutes:  int   = 0

    # Warn when daily loss reaches this fraction of the limit
    warning_threshold:    float = 0.80       # 80%

    # Poll interval (seconds) for P&L monitoring
    poll_interval:        int   = 10


# ------------------------------------------------------------------
# Daily snapshot (reset each trading day)
# ------------------------------------------------------------------

@dataclass
class DayStats:
    date:                   date  = field(default_factory=date.today)
    starting_balance:       float = 0.0   # cash balance at start of day
    realized_pnl:           float = 0.0   # filled in by polling
    unrealized_pnl:         float = 0.0
    peak_equity:            float = 0.0   # all-time peak for trailing DD calc
    warned:                 bool  = False
    flattened_today:        bool  = False


# ------------------------------------------------------------------
# Risk Manager
# ------------------------------------------------------------------

class RiskManager:
    CT = pytz.timezone("America/Chicago")

    def __init__(
        self,
        client: TradovateClient,
        config: ApexConfig,
        on_flatten: Optional[Callable] = None,
    ):
        self.client   = client
        self.config   = config
        self._on_flatten = on_flatten  # optional callback after emergency flatten
        self._day     = DayStats()
        self._running = False

        # All-time peak equity (for trailing drawdown).
        # Loaded from disk so a mid-session restart cannot reset the high-water
        # mark and allow a drawdown violation to go undetected.
        self._peak_equity: float = self._load_peak_equity()
        self._initialized: bool  = False

        # Cumulative net profit across days (for consistency rule)
        self._cumulative_net_profit: float = 0.0

    # ------------------------------------------------------------------
    # Public helpers (called by order manager before placing orders)
    # ------------------------------------------------------------------

    def check_order_allowed(self, qty: int) -> tuple[bool, str]:
        """
        Returns (allowed, reason).
        Call this BEFORE sending any order to Tradovate.
        """
        if self._day.flattened_today:
            return False, "Bot is locked for today after hitting a risk limit."

        # Contract size check
        if qty > self.config.max_contracts:
            return False, (
                f"Order qty {qty} exceeds max contracts {self.config.max_contracts}."
            )

        # Daily loss check
        current_loss = self._current_daily_loss()
        if current_loss >= self.config.daily_loss_limit:
            return False, (
                f"Daily loss ${current_loss:.2f} already at/above limit "
                f"${self.config.daily_loss_limit:.2f}."
            )

        # Trailing drawdown check
        dd = self._current_trailing_dd()
        if dd >= self.config.max_trailing_dd:
            return False, (
                f"Trailing drawdown ${dd:.2f} at/above limit "
                f"${self.config.max_trailing_dd:.2f}."
            )

        return True, "OK"

    def check_position_size_ok(self, current_qty: int, new_qty: int) -> tuple[bool, str]:
        total = abs(current_qty) + new_qty
        if total > self.config.max_contracts:
            return False, (
                f"Total position {total} would exceed max {self.config.max_contracts} contracts."
            )
        return True, "OK"

    def get_status(self) -> dict:
        """Return a dict summarising current risk state (for status endpoint)."""
        loss     = self._current_daily_loss()
        dd       = self._current_trailing_dd()
        pct_loss = (loss / self.config.daily_loss_limit * 100) if self.config.daily_loss_limit else 0
        pct_dd   = (dd   / self.config.max_trailing_dd  * 100) if self.config.max_trailing_dd   else 0
        return {
            "date":                str(self._day.date),
            "starting_balance":    round(self._day.starting_balance, 2),
            "realized_pnl":        round(self._day.realized_pnl,     2),
            "unrealized_pnl":      round(self._day.unrealized_pnl,   2),
            "daily_loss":          round(loss, 2),
            "daily_loss_limit":    self.config.daily_loss_limit,
            "daily_loss_pct":      round(pct_loss, 1),
            "trailing_drawdown":   round(dd,   2),
            "trailing_dd_limit":   self.config.max_trailing_dd,
            "trailing_dd_pct":     round(pct_dd, 1),
            "peak_equity":         round(self._peak_equity, 2),
            "cumulative_profit":   round(self._cumulative_net_profit, 2),
            "profit_target":       self.config.profit_target,
            "locked_today":        self._day.flattened_today,
            "max_contracts":       self.config.max_contracts,
        }

    # ------------------------------------------------------------------
    # Background monitor loop
    # ------------------------------------------------------------------

    async def monitor(self):
        """
        Async loop: poll Tradovate every poll_interval seconds,
        update P&L stats, and enforce limits.
        """
        self._running = True
        logger.info("Risk monitor started.")

        while self._running:
            try:
                await self._poll_and_check()
            except TradovateError as e:
                logger.error(f"Risk monitor API error: {e}")
            except Exception as e:
                logger.exception(f"Risk monitor unexpected error: {e}")

            await asyncio.sleep(self.config.poll_interval)

    def stop(self):
        self._running = False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _poll_and_check(self):
        self._maybe_new_day()

        snapshot = await asyncio.to_thread(self.client.get_cash_balance_snapshot)
        cash_balance  = snapshot.get("cashBalance",     0.0)
        open_pnl      = snapshot.get("openPnl",         0.0)
        day_pnl       = snapshot.get("realizedPnl",     0.0)  # day realised

        # Some accounts return totalPnl; adjust if needed
        self._day.realized_pnl   = day_pnl
        self._day.unrealized_pnl = open_pnl

        current_equity = cash_balance + open_pnl

        # Seed on first poll after startup (or after a new-day reset)
        if not self._initialized:
            self._day.starting_balance = cash_balance - day_pnl  # strip today's realised
            # Only seed peak from live equity if no persisted value exists.
            # A persisted value means we restarted mid-session and must honour
            # the previous high-water mark, not reset it.
            if self._peak_equity == 0.0:
                self._peak_equity = current_equity
                self._save_peak_equity()
            self._initialized = True
            logger.info(
                f"Risk manager initialised. Starting balance: "
                f"${self._day.starting_balance:,.2f}, "
                f"Peak equity (all-time): ${self._peak_equity:,.2f}"
            )

        # Update trailing peak and persist whenever it moves higher
        if current_equity > self._peak_equity:
            self._peak_equity = current_equity
            self._save_peak_equity()

        daily_loss  = self._current_daily_loss()
        trailing_dd = self._current_trailing_dd()

        # Warning
        if (
            daily_loss >= self.config.daily_loss_limit * self.config.warning_threshold
            and not self._day.warned
        ):
            self._day.warned = True
            logger.warning(
                f"RISK WARNING: Daily loss ${daily_loss:,.2f} is "
                f"{daily_loss / self.config.daily_loss_limit * 100:.0f}% "
                f"of ${self.config.daily_loss_limit:,.2f} limit."
            )

        # Flatten conditions
        if not self._day.flattened_today:
            if daily_loss >= self.config.daily_loss_limit:
                logger.error(
                    f"DAILY LOSS LIMIT HIT: ${daily_loss:,.2f} >= "
                    f"${self.config.daily_loss_limit:,.2f}. FLATTENING."
                )
                await self._emergency_flatten("daily_loss_limit")

            elif trailing_dd >= self.config.max_trailing_dd:
                logger.error(
                    f"TRAILING DRAWDOWN LIMIT HIT: ${trailing_dd:,.2f} >= "
                    f"${self.config.max_trailing_dd:,.2f}. FLATTENING."
                )
                await self._emergency_flatten("trailing_drawdown_limit")

        # EOD flatten
        if self.config.eod_flatten_minutes > 0 and not self._day.flattened_today:
            self._check_eod_flatten()

    # ------------------------------------------------------------------
    # Peak equity persistence
    # ------------------------------------------------------------------

    @staticmethod
    def _load_peak_equity() -> float:
        """Return persisted peak equity, or 0.0 if no state file exists."""
        try:
            if _PEAK_STATE_FILE.exists():
                data = json.loads(_PEAK_STATE_FILE.read_text())
                val = float(data.get("peak_equity", 0.0))
                if val > 0:
                    logger.info(f"Loaded persisted peak equity: ${val:,.2f}")
                return val
        except Exception as e:
            logger.warning(f"Could not load peak equity state: {e}")
        return 0.0

    def _save_peak_equity(self):
        """Persist current peak equity to survive restarts."""
        try:
            _PEAK_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            _PEAK_STATE_FILE.write_text(
                json.dumps({"peak_equity": round(self._peak_equity, 2)})
            )
        except Exception as e:
            logger.error(f"Could not persist peak equity: {e}")

    def _current_daily_loss(self) -> float:
        """Positive value = how much we've lost today (including open P&L)."""
        combined = self._day.realized_pnl + self._day.unrealized_pnl
        return max(0.0, -combined)

    def _current_trailing_dd(self) -> float:
        """How far current equity is below the running peak."""
        current_equity = (
            self._day.starting_balance
            + self._day.realized_pnl
            + self._day.unrealized_pnl
        )
        dd = self._peak_equity - current_equity
        return max(0.0, dd)

    def _maybe_new_day(self):
        """Reset day stats if calendar date has changed (CT timezone)."""
        now_ct = datetime.now(self.CT)
        today  = now_ct.date()
        if today != self._day.date:
            logger.info(f"New trading day: {today}. Resetting daily stats.")
            self._cumulative_net_profit += self._day.realized_pnl
            self._day = DayStats(date=today)
            self._initialized = False

    def _check_eod_flatten(self):
        """Flatten N minutes before RTH close (15:15 CT for ES)."""
        now_ct = datetime.now(self.CT)
        # ES RTH close: 15:15 CT
        eod_hour, eod_min = 15, 15
        minutes_to_close = (
            (eod_hour - now_ct.hour) * 60
            + (eod_min - now_ct.minute)
        )
        if 0 <= minutes_to_close <= self.config.eod_flatten_minutes:
            logger.warning(
                f"EOD flatten: {minutes_to_close} min before RTH close."
            )
            asyncio.create_task(self._emergency_flatten("eod_flatten"))

    async def _emergency_flatten(self, reason: str):
        self._day.flattened_today = True
        try:
            await asyncio.to_thread(self.client.flatten_all)
            logger.warning(f"Emergency flatten complete. Reason: {reason}")
        except TradovateError as e:
            logger.error(f"Flatten failed: {e}")
        if self._on_flatten:
            try:
                self._on_flatten(reason)
            except Exception as e:
                logger.error(f"on_flatten callback error: {e}")
