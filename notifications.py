"""Pure, JSON-serializable CCfolia channel notification transitions.

The caller deduplicates message IDs and supplies authorized messages in source
creation order. Hidden and removed messages never affect the reminder state.
Burst gaps and quiet deadlines use source times; no message body is retained.
"""

from __future__ import annotations

import datetime as dt
import math
import re
from collections.abc import Mapping


BURST_GAP = 90
QUIET_GAP = 300
BURST_COUNT = 3
_BEIJING = dt.timezone(dt.timedelta(hours=8))


def epoch(value):
    """Parse timezone-explicit RFC3339, including Python 3.10 fractions."""
    match = re.fullmatch(
        r"(\d{4}-\d{2}-\d{2})[Tt](\d{2}:\d{2}:\d{2})"
        r"(?:\.(\d+))?([Zz]|[+-](?:[01]\d|2[0-3]):[0-5]\d)",
        value if isinstance(value, str) else "",
    )
    if match is None:
        raise ValueError("Invalid source timestamp")
    date, clock, fraction, offset = match.groups()
    micros = "." + (fraction + "000000")[:6] if fraction is not None else ""
    offset = "+00:00" if offset in ("Z", "z") else offset
    return dt.datetime.fromisoformat(date + "T" + clock + micros + offset).timestamp()


def _line(value):
    return str(value).replace("\r", " ").replace("\n", " ").strip()


def _name(message):
    return _line(message.get("name") or message.get("senderId") or "未知发言者")


def _clock(timestamp):
    return dt.datetime.fromtimestamp(timestamp, _BEIJING).strftime("%H:%M:%S")


def render(message, title):
    """Render exactly one metadata-only notice in Beijing time."""
    return [f"「{_line(title)}」[{_clock(epoch(message['created']))}] {_name(message)}回复了。"]


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _state(state):
    """Return only supported state fields, excluding accidental content."""
    source = state if isinstance(state, Mapping) else {}
    qualifying = source.get("last_qualifying")
    last = None
    if isinstance(qualifying, Mapping):
        created = _number(qualifying.get("created"))
        if created is not None:
            last = {"created": created, "name": _line(qualifying.get("name") or "未知发言者")}
    streak = source.get("streak", 0)
    if isinstance(streak, bool) or not isinstance(streak, int):
        streak = 0
    return {
        "phase": "muted" if source.get("phase") == "muted" else "normal",
        "streak": max(0, min(BURST_COUNT, streak)),
        "last_source_time": _number(source.get("last_source_time")),
        "last_activity": _number(source.get("last_activity")),
        "last_qualifying": last,
    }


def resume(state, title, now):
    """Emit one recovery notice after 300 s of source-channel silence."""
    updated = _state(state)
    current = _number(now)
    if current is None:
        raise ValueError("Invalid current timestamp")
    activity, last = updated["last_activity"], updated["last_qualifying"]
    if (updated["phase"] != "muted" or activity is None or last is None
            or current - activity < QUIET_GAP):
        return updated, []
    updated.update(phase="normal", streak=0, last_source_time=None)
    return updated, [
        f"「{_line(title)}」最后一条消息由[{_clock(last['created'])}] "
        f"{last['name']} 回复。重新开始提醒该频道消息。"
    ]


def transition(state, message, title="", now=None):
    """Process one message; the third consecutive close reply mutes a channel.

    Every visible message counts. ``now`` is accepted for caller compatibility
    but never replaces source creation time, including during backfill.
    """
    updated = _state(state)
    if message.get("_hidden") or message.get("_removed"):
        return updated, []
    created = epoch(message["created"])
    updated, notices = resume(updated, title, created)
    activity = updated["last_activity"]
    updated["last_activity"] = created if activity is None else max(activity, created)
    previous = updated["last_source_time"]
    # Delayed older events must not move the source clock or latest speaker.
    if previous is not None and created < previous:
        if updated["phase"] == "normal":
            notices.extend(render(message, title))
        return updated, notices
    updated["last_qualifying"] = {"created": created, "name": _name(message)}
    updated["last_source_time"] = created
    if updated["phase"] == "muted":
        return updated, notices
    updated["streak"] = (
        updated["streak"] + 1
        if previous is not None and 0 <= created - previous <= BURST_GAP else 1
    )
    if updated["streak"] >= BURST_COUNT:
        updated.update(phase="muted", streak=BURST_COUNT)
        notices.append(
            f"监测到「{_line(title)}」高频回复。超过5分钟无人回复前，不再提醒该频道消息。"
        )
    else:
        notices.extend(render(message, title))
    return updated, notices
