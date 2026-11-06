"""Persist Claude's reported quota reset across local CLI callers and processes.

Only provider failures update this state. It contains no prompt or raw error
text, and never changes the configured provider order: callers skip Claude
while the returned reason is nonempty and retry normally after expiry.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from atomic_io import atomic_write_json, with_file_lock

STATE_PATH = Path(__file__).resolve().parent / "signals" / "ai_cli_cooldown.json"
RETRY_BACKOFF = timedelta(minutes=15)
MAX_RESET_DELAY = timedelta(days=8)
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_MONTHS = {name: i for i, name in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
_QUOTA = re.compile(
    r"\b(?:you(?:'|’)ve hit your limit|hit your limit|usage limit|quota(?: exceeded)?|"
    r"insufficient_quota|rate[ _-]?limit(?:[ _-]exceeded|ed|ing)?|too many requests|limit reached)\b|"
    r"\b(?:HTTP(?:Error)?[ :]+|status(?:_code)?[ :=]+|error[ :=]+)429\b",
    re.IGNORECASE,
)
_TIME = r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<ampm>am|pm)?"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def is_quota_error(status: str) -> bool:
    """Classify a failed CLI call; ordinary successful prose is not an error."""
    return bool(_QUOTA.search(_ANSI.sub("", str(status or ""))))


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("quota state timestamps must include a timezone")
    return value.astimezone(timezone.utc)


def _timestamp(value: object) -> datetime | None:
    try:
        return _utc(datetime.fromisoformat(value)) if isinstance(value, str) else None
    except (ValueError, TypeError, OverflowError):
        return None


def _read_state() -> dict:
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if not isinstance(state, dict) or state.get("version") != 1:
            return {}
        started = _timestamp(state.get("started_at"))
        observed = _timestamp(state.get("observed_at"))
        until = _timestamp(state.get("until"))
        if not started or not observed or started > observed:
            return {}
        if state.get("until") is not None:
            if not until or not observed < until <= observed + MAX_RESET_DELAY:
                return {}
            if state.get("source") not in ("provider_reset", "retry_backoff"):
                return {}
        elif state.get("source") != "success":
            return {}
        return state
    except (OSError, ValueError, TypeError):
        return {}


def cooldown_reason(*, now: datetime | None = None) -> str:
    """Read the shared state without writes; an expired state means retry Claude."""
    current = _utc(now or _utcnow())
    state = _read_state()
    until = _timestamp(state.get("until"))
    if until is None or until <= current:
        return ""
    return f"quota_cooldown: Claude unavailable until {until.isoformat()} ({state['source']})"


def _clock(match: re.Match) -> tuple[int, int] | None:
    hour = int(match.group("hour"))
    minute = int(match.group("minute") or 0)
    ampm = match.group("ampm")
    if minute > 59:
        return None
    if ampm:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if ampm.lower() == "pm" else 0)
    elif hour > 23:
        return None
    return hour, minute


def _parse_reset(status: str, now: datetime) -> datetime | None:
    """Parse provider reset hints only; ambiguous dates/zones use short backoff."""
    text = _ANSI.sub("", str(status or ""))
    marker = re.search(r"\bresets?\s+(?:at\s+)?(.+)", text, re.I)
    if not marker:
        return None
    hint = marker.group(1).strip()

    # A relative duration does not require a local timezone.
    duration = re.match(r"in\s+((?:\d+\s*(?:hours?|hrs?|h|minutes?|mins?|m)\s*)+)(?:\b|$)", hint, re.I)
    if duration:
        seconds = 0
        for number, unit in re.findall(r"(\d+)\s*(hours?|hrs?|h|minutes?|mins?|m)", duration.group(1), re.I):
            seconds += int(number) * (3600 if unit.lower().startswith("h") else 60)
        result = now + timedelta(seconds=seconds)
        return result if now < result <= now + MAX_RESET_DELAY else None

    # ISO timestamps carry their own offset and avoid locale-dependent dates.
    iso = re.match(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:\d{2})", hint)
    if iso:
        result = _timestamp(iso.group(0))
        return result if result and now < result <= now + MAX_RESET_DELAY else None

    zone = re.search(r"\(([^()]+)\)", hint)
    if not zone:
        return None
    try:
        tz = ZoneInfo(zone.group(1).strip())
    except (ZoneInfoNotFoundError, ValueError):
        return None
    local_now = now.astimezone(tz)
    clock_hint = hint[:zone.start()].strip()
    date_clock = re.fullmatch(
        rf"(?P<month>[A-Za-z]+)\s+(?P<day>\d{{1,2}})(?:,?\s+(?P<year>\d{{4}}))?,?\s+{_TIME}",
        clock_hint, re.I,
    )
    try:
        if date_clock:
            clock = _clock(date_clock)
            month = _MONTHS.get(date_clock.group("month")[:3].lower())
            if clock is None or month is None:
                return None
            year = int(date_clock.group("year") or local_now.year)
            # A December response can legitimately report the next January.
            if not date_clock.group("year") and local_now.month == 12 and month == 1:
                year += 1
            result = datetime(year, month, int(date_clock.group("day")), *clock, tzinfo=tz)
        else:
            clock_match = re.fullmatch(_TIME, clock_hint, re.I)
            if not clock_match or (clock := _clock(clock_match)) is None:
                return None
            result = local_now.replace(hour=clock[0], minute=clock[1], second=0, microsecond=0)
            if result <= local_now:
                result += timedelta(days=1)
        # DST gaps/ambiguous folds do not identify one reset instant.
        if (result.replace(fold=0).utcoffset() != result.replace(fold=1).utcoffset()
                or result.astimezone(timezone.utc).astimezone(tz).replace(tzinfo=None)
                != result.replace(tzinfo=None)):
            return None
        result = result.astimezone(timezone.utc)
        return result if now < result <= now + MAX_RESET_DELAY else None
    except (ValueError, OverflowError):
        return None


def record_result(*, success: bool, status: str, started_at: datetime,
                  now: datetime | None = None) -> None:
    """Record a completed Claude call without letting old results undo newer ones.

    A successful call only clears a cooldown observed before that call started.
    Other failure classes leave quota state intact. Disk/lock errors preserve
    existing routing behavior instead of turning a successful CLI call into an
    application failure.
    """
    if not success and not is_quota_error(status):
        return
    try:
        started = _utc(started_at)
        observed = _utc(now or _utcnow())
        if started > observed:
            return
        reset = None if success else _parse_reset(status, observed)
        until = None if success else reset or observed + RETRY_BACKOFF
        state = {
            "version": 1,
            "started_at": started.isoformat(),
            "observed_at": observed.isoformat(),
            "until": until.isoformat() if until else None,
            "source": "success" if success else "provider_reset" if reset else "retry_backoff",
        }

        def update() -> None:
            previous = _read_state()
            prior_started = _timestamp(previous.get("started_at"))
            prior_observed = _timestamp(previous.get("observed_at"))
            if prior_started and started < prior_started:
                return
            if prior_observed and (observed < prior_observed or (success and started < prior_observed)):
                return
            # Another in-flight call may report a generic 429 after one has
            # supplied the weekly reset. A short fallback delay must not erase
            # that still-active provider evidence.
            prior_until = _timestamp(previous.get("until"))
            if not success and prior_until and until and prior_until > until:
                state["until"] = prior_until.isoformat()
                state["source"] = previous["source"]
            atomic_write_json(STATE_PATH, state)

        with_file_lock(STATE_PATH, update)
    except (OSError, ValueError, TypeError, OverflowError, TimeoutError):
        pass
