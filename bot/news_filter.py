"""
Economic calendar news filter.

Fetches the week's high-impact USD events from ForexFactory's public JSON feed
and blocks trading within a configurable window around each event.

ForexFactory JSON endpoint (public, no auth required):
  https://nfs.faireconomy.media/ff_calendar_thisweek.json

Refreshes the calendar once per hour so the bot doesn't hammer the endpoint.
"""

import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

import pytz
import requests
from loguru import logger

FF_CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"


@dataclass
class NewsEvent:
    title:    str
    country:  str
    dt_utc:   datetime      # event time in UTC
    impact:   str           # "Low", "Medium", "High", "Holiday"


class NewsFilter:
    ET = pytz.timezone("America/New_York")
    UTC = pytz.utc

    def __init__(
        self,
        enabled:              bool  = True,
        blackout_before_min:  int   = 10,   # minutes before event
        blackout_after_min:   int   = 5,    # minutes after event
        impact_levels:        tuple = ("High",),
        countries:            tuple = ("USD",),
        refresh_interval_sec: int   = 3600,  # refresh calendar every hour
    ):
        self.enabled             = enabled
        self.blackout_before_min = blackout_before_min
        self.blackout_after_min  = blackout_after_min
        self.impact_levels       = [i.strip() for i in impact_levels]
        self.countries           = [c.strip().upper() for c in countries]
        self.refresh_interval    = refresh_interval_sec

        self._events:       list[NewsEvent] = []
        self._last_refresh: Optional[datetime] = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def is_news_blackout(self, now: Optional[datetime] = None) -> tuple[bool, str]:
        """
        Returns (blocked, reason).
        blocked=True means "do NOT trade right now".
        """
        if not self.enabled:
            return False, "News filter disabled"

        self._maybe_refresh()
        now = now or datetime.now(self.UTC)

        for event in self._events:
            before = event.dt_utc - timedelta(minutes=self.blackout_before_min)
            after  = event.dt_utc + timedelta(minutes=self.blackout_after_min)
            if before <= now <= after:
                local_time = event.dt_utc.astimezone(self.ET).strftime("%H:%M ET")
                return True, (
                    f"News blackout: '{event.title}' @ {local_time} "
                    f"[{event.impact} / {event.country}]"
                )

        return False, "No news blackout"

    def next_event_minutes(self, now: Optional[datetime] = None) -> Optional[float]:
        """Return minutes until the next relevant news event (None if none this week)."""
        self._maybe_refresh()
        now = now or datetime.now(self.UTC)
        upcoming = [e for e in self._events if e.dt_utc > now]
        if not upcoming:
            return None
        soonest = min(upcoming, key=lambda e: e.dt_utc)
        return (soonest.dt_utc - now).total_seconds() / 60

    def get_todays_events(self, now: Optional[datetime] = None) -> list[dict]:
        self._maybe_refresh()
        now = now or datetime.now(self.ET)
        today = now.date()
        result = []
        for e in self._events:
            local = e.dt_utc.astimezone(self.ET)
            if local.date() == today:
                result.append({
                    "title":   e.title,
                    "country": e.country,
                    "time_et": local.strftime("%H:%M"),
                    "impact":  e.impact,
                })
        return result

    # ------------------------------------------------------------------
    # Calendar fetching
    # ------------------------------------------------------------------

    def _maybe_refresh(self):
        now = datetime.now(self.UTC)
        with self._lock:
            if (
                self._last_refresh is None
                or (now - self._last_refresh).total_seconds() > self.refresh_interval
            ):
                self._refresh_calendar()
                self._last_refresh = now

    def _refresh_calendar(self):
        """Fetch and parse the ForexFactory calendar JSON."""
        try:
            resp = requests.get(FF_CALENDAR_URL, timeout=10)
            resp.raise_for_status()
            raw = resp.json()
        except Exception as e:
            logger.warning(f"NewsFilter: could not fetch calendar: {e}")
            return

        events = []
        for item in raw:
            country = (item.get("country") or "").upper()
            impact  = item.get("impact", "")
            title   = item.get("title", "")
            date_s  = item.get("date", "")
            time_s  = item.get("time", "")

            if country not in self.countries:
                continue
            if impact not in self.impact_levels:
                continue
            if not date_s:
                continue

            dt_utc = self._parse_dt(date_s, time_s)
            if dt_utc is None:
                continue

            events.append(NewsEvent(title=title, country=country, dt_utc=dt_utc, impact=impact))

        self._events = events
        logger.info(
            f"NewsFilter: loaded {len(events)} {self.impact_levels} "
            f"{self.countries} events for the week."
        )
        for e in events:
            et = e.dt_utc.astimezone(self.ET).strftime("%a %m/%d %H:%M ET")
            logger.debug(f"  [{e.impact}] {e.title} @ {et}")

    @staticmethod
    def _parse_dt(date_s: str, time_s: str) -> Optional[datetime]:
        """
        Parse ForexFactory date/time strings into a UTC datetime.
        date_s example: "2024-01-15"
        time_s example: "8:30am" or "" (all-day)
        """
        ET = pytz.timezone("America/New_York")
        try:
            if time_s:
                combined = f"{date_s} {time_s}"
                # Try 12-hour format
                for fmt in ("%Y-%m-%d %I:%M%p", "%Y-%m-%d %I%p"):
                    try:
                        naive = datetime.strptime(combined.upper(), fmt)
                        return ET.localize(naive).astimezone(pytz.utc)
                    except ValueError:
                        continue
            # All-day event: use midnight ET
            naive = datetime.strptime(date_s, "%Y-%m-%d").replace(hour=0, minute=0)
            return ET.localize(naive).astimezone(pytz.utc)
        except Exception:
            return None
