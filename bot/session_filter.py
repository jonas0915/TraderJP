"""
Trading session filter for ES futures.

ES Globex / CME trading hours (all times CT = Chicago):
  - Globex (ETH):  Sunday 17:00 → Friday 16:00  (with a 15-min break 15:15-15:30 each day)
  - RTH:           Mon–Fri 08:30 → 15:15

This filter lets you restrict bot trading to RTH only, ETH only, both, or specific
time ranges. Outside allowed sessions all orders are blocked.
"""

from dataclasses import dataclass, field
from datetime import datetime, time as dtime
from typing import Optional

import pytz
from loguru import logger


@dataclass
class SessionConfig:
    # Which sessions to allow trading
    trade_rth:    bool = True   # Regular Trading Hours (08:30-15:15 CT Mon-Fri)
    trade_eth:    bool = False  # Extended / Globex hours (outside RTH)

    # Optional hard overrides: only trade between these times (CT) regardless of session
    # e.g. "09:00" and "14:00" to only trade 9-2 CT
    force_start:  str  = ""     # "HH:MM" or blank to use session defaults
    force_end:    str  = ""     # "HH:MM" or blank to use session defaults

    # Days allowed (0=Mon … 6=Sun). Default: Mon-Fri
    allowed_days: list = field(default_factory=lambda: [0, 1, 2, 3, 4])


class SessionFilter:
    CT = pytz.timezone("America/Chicago")

    # ES RTH hours (CT)
    RTH_START = dtime(8, 30)
    RTH_END   = dtime(15, 15)

    # Daily maintenance break (CT): 15:15-15:30 each day
    BREAK_START = dtime(15, 15)
    BREAK_END   = dtime(15, 30)

    def __init__(self, config: SessionConfig = None):
        self.config = config or SessionConfig()

    def is_trading_allowed(self, now: Optional[datetime] = None) -> tuple[bool, str]:
        """
        Returns (allowed, reason).
        allowed=False means "do NOT trade right now".
        """
        now_ct = (now or datetime.now(self.CT)).astimezone(self.CT)
        current_time = now_ct.time().replace(second=0, microsecond=0)
        weekday = now_ct.weekday()  # 0=Monday … 6=Sunday

        # Day-of-week check
        if weekday not in self.config.allowed_days:
            return False, f"Not an allowed trading day (weekday={weekday})"

        # Maintenance break
        if self.BREAK_START <= current_time < self.BREAK_END:
            return False, f"Daily maintenance break ({self.BREAK_START}–{self.BREAK_END} CT)"

        # Hard time override
        if self.config.force_start and self.config.force_end:
            fs = self._parse_time(self.config.force_start)
            fe = self._parse_time(self.config.force_end)
            if not (fs <= current_time < fe):
                return False, (
                    f"Outside forced window {self.config.force_start}–{self.config.force_end} CT"
                )
            return True, "Within forced trading window"

        in_rth = self.RTH_START <= current_time < self.RTH_END
        # Sunday: market opens at 17:00 CT — before that is a closed gap
        if weekday == 6 and current_time < dtime(17, 0):
            return False, "Market closed (Sunday before 17:00 CT open)"

        if in_rth:
            if self.config.trade_rth:
                return True, "RTH session"
            else:
                return False, "RTH trading is disabled in config"
        else:
            if self.config.trade_eth:
                return True, "ETH/Globex session"
            else:
                return False, "ETH trading is disabled (trade_eth=False)"

    def current_session(self, now: Optional[datetime] = None) -> str:
        now_ct = (now or datetime.now(self.CT)).astimezone(self.CT)
        t = now_ct.time().replace(second=0, microsecond=0)
        if self.RTH_START <= t < self.RTH_END:
            return "RTH"
        if self.BREAK_START <= t < self.BREAK_END:
            return "BREAK"
        return "ETH"

    def minutes_to_rth_open(self, now: Optional[datetime] = None) -> Optional[float]:
        """Return minutes until next RTH open. None if RTH is currently open."""
        now_ct = (now or datetime.now(self.CT)).astimezone(self.CT)
        t = now_ct.time()
        if self.RTH_START <= t < self.RTH_END:
            return None  # already open

        # Build next RTH open datetime
        today   = now_ct.date()
        weekday = now_ct.weekday()
        days_ahead = 0
        # Skip weekends
        candidate = today
        while True:
            rth_open = self.CT.localize(
                datetime.combine(candidate, self.RTH_START)
            )
            if rth_open > now_ct and candidate.weekday() < 5:
                return (rth_open - now_ct).total_seconds() / 60
            candidate = candidate.__class__.fromordinal(candidate.toordinal() + 1)
            days_ahead += 1
            if days_ahead > 7:
                break
        return None

    @staticmethod
    def _parse_time(s: str) -> dtime:
        h, m = s.split(":")
        return dtime(int(h), int(m))
