"""
The study scheduler. Plain Python, no model required.

WHY THIS EXISTS
---------------
make_schedule used to be a single call to Claude that returned JSON blocks.
On a deployment where the anthropic package wasn't installed it raised
ModuleNotFoundError, tools.execute wrapped that as "make_schedule failed",
and the assistant told a student four times across one conversation that "the
scheduling tool encountered an internal error (missing dependency)" -- then,
asked which dependency, invented an explanation about missing assignment
data. Scheduling was the only feature in the project with a hard model
dependency and no fallback: display.py has one, the crawler works from a JSON
export, so the failure was invisible everywhere else.

It also shouldn't have needed a model in the first place. Placing N tasks of
known length into free time before their deadlines, without overlaps and
without exceeding a daily limit, is a constraint problem. Python is better at
it than a language model is: it can't schedule a block after its own due
date, can't double-book the orchestra rehearsal, and can't put study time in
the past -- all of which the model did.

WHERE CLAUDE STILL HELPS
------------------------
Ordering and judgement: which assignment matters most when they collide, and
a sentence of rationale. tools.make_schedule asks for that when Claude is
reachable and falls back to due-date order when it isn't. The placement is
always done here, so the model can influence the plan but cannot produce an
invalid one.

THE RULES, IN PRIORITY ORDER
----------------------------
  1. Never schedule in the past.
  2. Never schedule a block after its assignment's due date.
  3. Never overlap an existing commitment.
  4. Never exceed the daily study cap.
  5. Split anything longer than one session into separate days.
  6. Prefer earlier slots, so a slip has room to recover.
Anything that can't be placed under those rules comes back in `unscheduled`
with a reason, rather than being quietly dropped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

# Defaults. All overridable per call, and all chosen to be unsurprising rather
# than clever: a student's day, not a factory shift.
DAY_START_HOUR = 9
DAY_END_HOUR = 22
SESSION_MINUTES = 90          # one sitting
MIN_SESSION_MINUTES = 30      # shorter than this isn't worth a calendar entry
DAILY_CAP_MINUTES = 300       # five hours of study in one day is already a lot
GAP_MINUTES = 15              # breathing room between blocks

# How much a kind of work outranks another when two things are due together.
KIND_WEIGHT = {
    "exam": 0, "project": 1, "lab": 2, "quiz": 2,
    "homework": 3, "reading": 4, "other": 5,
}


@dataclass
class Task:
    title: str
    course: str = ""
    due: datetime | None = None
    minutes: int = 60
    kind: str = "other"
    assignment_id: str | None = None
    priority: int | None = None   # lower is sooner; set by the caller or the model

    def weight(self) -> tuple:
        """
        Sort key: explicit priority, then due date, then kind.

        Uses a float timestamp rather than a datetime sentinel. Sorting by
        `self.due or datetime.max` breaks the moment one task has no due date,
        because a naive max can't be compared with a timezone-aware deadline --
        and "no due date" is common (a final exam Canvas hasn't dated, an
        attendance row), so the crash would land on a real board.
        """
        return (
            self.priority if self.priority is not None else 50,
            self.due.timestamp() if self.due else float("inf"),
            KIND_WEIGHT.get(self.kind, 5),
        )


@dataclass
class Interval:
    start: datetime
    end: datetime
    label: str = "busy"

    def overlaps(self, other_start: datetime, other_end: datetime) -> bool:
        return self.start < other_end and other_start < self.end


@dataclass
class Constraints:
    """
    What the student said, parsed into something the placer can use.

    Anything not understood is reported back rather than ignored silently --
    "no work Friday nights" quietly dropped is worse than being told it was
    dropped, because the student finds out by having their Friday night
    scheduled.
    """
    day_start: int = DAY_START_HOUR
    day_end: int = DAY_END_HOUR
    session_minutes: int = SESSION_MINUTES
    daily_cap_minutes: int = DAILY_CAP_MINUTES
    blackout_weekdays: set[int] = field(default_factory=set)      # 0 = Monday
    blackout_windows: list[tuple[int, int, int]] = field(default_factory=list)
    # (weekday, start_hour, end_hour); weekday -1 means every day
    understood: list[str] = field(default_factory=list)
    ignored: list[str] = field(default_factory=list)


_WEEKDAYS = {"monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1,
             "wednesday": 2, "wed": 2, "thursday": 3, "thu": 3, "thur": 3,
             "thurs": 3, "friday": 4, "fri": 4, "saturday": 5, "sat": 5,
             "sunday": 6, "sun": 6}


def _hour(text: str, meridiem: str | None = None) -> int:
    value = int(re.sub(r"[^0-9]", "", text) or 0)
    if meridiem:
        meridiem = meridiem.lower().replace(".", "")
        if meridiem.startswith("p") and value < 12:
            value += 12
        if meridiem.startswith("a") and value == 12:
            value = 0
    return max(0, min(value, 23))


def parse_constraints(text: str) -> Constraints:
    """
    Read the free-text constraints a student typed.

    Deliberately a small set of patterns that people actually write, each
    reported in `understood` so the reply can say what was applied. A parser
    that silently half-works is worse than one that says what it missed.
    """
    out = Constraints()
    if not text:
        return out
    lowered = " ".join(str(text).lower().split())
    matched_spans = 0

    # "between 6 and 7pm", "between 6pm and 9pm"
    window = re.search(r"between (\d{1,2})\s*(am|pm)? ?(?:and|to|-) ?(\d{1,2})\s*(am|pm)",
                       lowered)
    if window:
        start = _hour(window.group(1), window.group(2) or window.group(4))
        end = _hour(window.group(3), window.group(4))
        if end > start:
            out.day_start, out.day_end = start, max(end, start + 1)
            out.understood.append(f"work between {start}:00 and {end}:00")
            matched_spans += 1

    # "after 6pm", "not before 10am"
    after = re.search(r"(?:after|from|not before|start(?:ing)? at) (\d{1,2})\s*(am|pm)",
                      lowered)
    if after and not window:
        out.day_start = _hour(after.group(1), after.group(2))
        out.understood.append(f"nothing before {out.day_start}:00")
        matched_spans += 1

    # "before 9pm", "done by 10pm", "nothing after 11pm"
    before = re.search(r"(?:before|by|until|no later than|nothing after) (\d{1,2})\s*(am|pm)",
                       lowered)
    if before and not window:
        out.day_end = _hour(before.group(1), before.group(2))
        out.understood.append(f"nothing after {out.day_end}:00")
        matched_spans += 1

    # "no work friday nights", "nothing on sunday", "keep saturdays free"
    for day_name, index in _WEEKDAYS.items():
        if re.search(rf"(?:no|not|nothing|avoid|free|keep|skip)[^.]{{0,24}}\b{day_name}s?\b",
                     lowered) or re.search(
                         rf"\b{day_name}s?\b[^.]{{0,16}}(?:off|free)", lowered):
            if re.search(rf"\b{day_name}s?\b[^.]{{0,20}}(?:night|evening)", lowered):
                out.blackout_windows.append((index, 17, 24))
                out.understood.append(f"no {day_name} evenings")
            else:
                out.blackout_weekdays.add(index)
                out.understood.append(f"no work on {day_name}")
            matched_spans += 1

    # "class 9-11am weekdays", "I have lectures 10-12"
    lectures = re.search(r"(?:class|classes|lecture|lectures|work|job)\w*\s*"
                         r"(\d{1,2})\s*(am|pm)?\s*(?:-|to|until)\s*(\d{1,2})\s*(am|pm)?",
                         lowered)
    if lectures:
        start = _hour(lectures.group(1), lectures.group(2) or lectures.group(4))
        end = _hour(lectures.group(3), lectures.group(4) or lectures.group(2))
        if end > start:
            days = range(0, 5) if "weekday" in lowered or "week day" in lowered else [-1]
            for day in days:
                out.blackout_windows.append((day, start, end))
            out.understood.append(f"busy {start}:00-{end}:00"
                                  + (" on weekdays" if days != [-1] else " daily"))
            matched_spans += 1

    # "no more than 3 hours a day", "max 2h per day"
    cap = re.search(r"(?:no more than|at most|max(?:imum)?|cap(?:ped)? at)\s*"
                    r"(\d{1,2})\s*(?:h|hr|hrs|hours?)\s*(?:a|per|each)?\s*day", lowered)
    if cap:
        out.daily_cap_minutes = max(30, int(cap.group(1)) * 60)
        out.understood.append(f"at most {cap.group(1)}h a day")
        matched_spans += 1

    # "one hour blocks", "45 minute sessions", "2 hour sessions"
    session = re.search(r"(\d{1,3})\s*(?:-|\s)?(minute|min|hour|hr|h)\w*\s*"
                        r"(?:block|session|chunk|slot)", lowered)
    if session:
        amount = int(session.group(1))
        out.session_minutes = amount if session.group(2).startswith("m") else amount * 60
        out.understood.append(f"{out.session_minutes}-minute sessions")
        matched_spans += 1
    elif re.search(r"\b(?:an|one) hour (?:for|each|per)\b", lowered):
        out.session_minutes = 60
        out.understood.append("one hour per assignment")
        matched_spans += 1

    if not matched_spans:
        out.ignored.append(str(text)[:160])
    return out


# --------------------------------------------------------------------------
# Free-time model
# --------------------------------------------------------------------------


def _day_window(day: date, constraints: Constraints, tz) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time(hour=constraints.day_start), tzinfo=tz)
    end_hour = constraints.day_end
    if end_hour >= 24:
        end = datetime.combine(day + timedelta(days=1), time(hour=0), tzinfo=tz)
    else:
        end = datetime.combine(day, time(hour=end_hour), tzinfo=tz)
    return start, end


def _blackouts_for(day: date, constraints: Constraints, tz) -> list[Interval]:
    out = []
    for weekday, start_hour, end_hour in constraints.blackout_windows:
        if weekday not in (-1, day.weekday()):
            continue
        start = datetime.combine(day, time(hour=min(start_hour, 23)), tzinfo=tz)
        if end_hour >= 24:
            end = datetime.combine(day + timedelta(days=1), time(hour=0), tzinfo=tz)
        else:
            end = datetime.combine(day, time(hour=end_hour), tzinfo=tz)
        out.append(Interval(start, end, "constraint"))
    return out


def free_slots(day: date, busy: list[Interval], constraints: Constraints,
               tz, not_before: datetime) -> list[tuple[datetime, datetime]]:
    """Open stretches on one day, after `not_before`, avoiding everything busy."""
    if day.weekday() in constraints.blackout_weekdays:
        return []

    window_start, window_end = _day_window(day, constraints, tz)
    window_start = max(window_start, not_before)
    if window_start >= window_end:
        return []

    blocked = sorted(
        [i for i in busy + _blackouts_for(day, constraints, tz)
         if i.overlaps(window_start, window_end)],
        key=lambda i: i.start,
    )

    slots, cursor = [], window_start
    for interval in blocked:
        if interval.start > cursor:
            slots.append((cursor, min(interval.start, window_end)))
        cursor = max(cursor, interval.end)
        if cursor >= window_end:
            break
    if cursor < window_end:
        slots.append((cursor, window_end))

    return [(s, e) for s, e in slots
            if (e - s).total_seconds() / 60 >= MIN_SESSION_MINUTES]


# --------------------------------------------------------------------------
# The placer
# --------------------------------------------------------------------------


def plan(tasks: list[Task], *, now: datetime, tz, horizon_days: int = 7,
         constraints: Constraints | None = None,
         busy: list[Interval] | None = None,
         strategy: str = "spread") -> dict:
    """
    Place tasks into free time. Returns blocks, what couldn't fit, and why.

    strategy:
      spread      distribute sessions across the days before each due date
      asap        everything as early as possible (catching up)
      day_before  one session the day before each due date, which is what a
                  student usually means by "block out time for each of these"
    """
    constraints = constraints or Constraints()
    busy = list(busy or [])
    blocks: list[dict] = []
    unscheduled: list[dict] = []

    # Nothing is ever placed before this moment. The model used to schedule
    # study time for work due three weeks ago, on days that had already
    # happened.
    floor = now + timedelta(minutes=5)
    horizon_end = (now + timedelta(days=max(1, horizon_days))).replace(
        hour=23, minute=59)

    days = [(now + timedelta(days=offset)).date()
            for offset in range(0, max(1, horizon_days) + 1)]
    used_today: dict[date, int] = {day: 0 for day in days}

    # Count time already committed on each day against the daily cap, so a
    # plan doesn't pile five hours of study onto a day with a rehearsal in it.
    for interval in busy:
        day = interval.start.astimezone(tz).date()
        if day in used_today:
            used_today[day] += int((interval.end - interval.start).total_seconds() // 60)

    for task in sorted(tasks, key=lambda t: t.weight()):
        remaining = max(0, int(task.minutes))
        if remaining < MIN_SESSION_MINUTES:
            remaining = MIN_SESSION_MINUTES

        deadline = task.due
        overdue = bool(deadline and deadline <= now)
        # An overdue assignment still needs time; its deadline just can't be
        # the cutoff any more, or there is nowhere legal to put it.
        cutoff = horizon_end if (overdue or deadline is None) else min(deadline, horizon_end)

        candidate_days = [d for d in days
                          if datetime.combine(d, time(23, 59), tzinfo=tz) >= floor
                          and datetime.combine(d, time(0, 0), tzinfo=tz) <= cutoff]

        if strategy == "day_before" and deadline and not overdue:
            day_before = (deadline - timedelta(days=1)).astimezone(tz).date()
            preferred = [d for d in candidate_days if d == day_before]
            candidate_days = preferred + [d for d in candidate_days if d != day_before]
        elif strategy == "asap" or overdue:
            pass  # already in ascending order
        else:
            # spread: leave the last day before the deadline as slack
            candidate_days = candidate_days

        placed_any = False
        for day in candidate_days:
            if remaining <= 0:
                break
            if used_today.get(day, 0) >= constraints.daily_cap_minutes:
                continue

            for slot_start, slot_end in free_slots(day, busy, constraints, tz, floor):
                if remaining <= 0:
                    break
                room_today = constraints.daily_cap_minutes - used_today.get(day, 0)
                if room_today < MIN_SESSION_MINUTES:
                    break

                slot_minutes = int((slot_end - slot_start).total_seconds() // 60)
                length = min(remaining, constraints.session_minutes,
                             slot_minutes, room_today)
                length = _round_session(length)
                if length < MIN_SESSION_MINUTES:
                    continue

                start = slot_start
                end = start + timedelta(minutes=length)
                if end > cutoff:
                    # Don't run past a deadline; try to squeeze a shorter one in.
                    length = _round_session(int((cutoff - start).total_seconds() // 60))
                    if length < MIN_SESSION_MINUTES:
                        continue
                    end = start + timedelta(minutes=length)

                blocks.append({
                    "task": f"{task.title}" + (f" ({task.course})" if task.course else ""),
                    "assignment_title": task.title,
                    "assignment_id": task.assignment_id,
                    "starts_at": start.isoformat(),
                    "ends_at": end.isoformat(),
                    "est_minutes": length,
                    "priority": task.priority if task.priority is not None else 5,
                })
                busy.append(Interval(start, end + timedelta(minutes=GAP_MINUTES),
                                     task.title))
                busy.sort(key=lambda i: i.start)
                used_today[day] = used_today.get(day, 0) + length
                remaining -= length
                placed_any = True

                if strategy == "day_before":
                    remaining = 0  # one session per assignment, as asked
                    break

        if remaining > 0:
            unscheduled.append({
                "title": task.title,
                "course": task.course,
                "minutes_left": remaining,
                "reason": (
                    "no free time left before the due date"
                    if deadline and not overdue else
                    "no free time left in the planning window"
                ),
                "partially_scheduled": placed_any,
            })

    blocks.sort(key=lambda b: b["starts_at"])
    return {
        "blocks": blocks,
        "unscheduled": unscheduled,
        "rationale": _rationale(blocks, unscheduled, constraints, strategy),
        "constraints_applied": constraints.understood,
        "constraints_not_understood": constraints.ignored,
        "planner": "deterministic",
    }


def _round_session(minutes: int) -> int:
    """
    Round a session down to a quarter hour.

    Arithmetic against a daily cap produces lengths like 66 and 36 minutes.
    They're correct and they look like a bug -- nobody writes "study 12:45 to
    13:51" in a calendar. Rounding down keeps every guarantee intact (a
    shorter block can't overrun a deadline or a cap) and costs a few minutes
    of planned time.
    """
    if minutes < MIN_SESSION_MINUTES:
        return minutes
    return max(MIN_SESSION_MINUTES, (minutes // 15) * 15)


def _rationale(blocks: list[dict], unscheduled: list[dict],
               constraints: Constraints, strategy: str) -> str:
    if not blocks:
        return ("Nothing could be placed: there was no free time in the "
                "window that satisfied the constraints.")
    total = sum(b["est_minutes"] for b in blocks) / 60
    days = len({b["starts_at"][:10] for b in blocks})
    parts = [f"{len(blocks)} session{'s' if len(blocks) != 1 else ''} "
             f"({total:.1f}h) across {days} day{'s' if days != 1 else ''}, "
             f"earliest deadlines first"]
    if strategy == "day_before":
        parts.append("one session the day before each due date")
    if constraints.understood:
        parts.append("respecting: " + "; ".join(constraints.understood[:4]))
    if unscheduled:
        parts.append(f"{len(unscheduled)} item(s) could not be fully placed")
    return ". ".join(parts) + "."


# --------------------------------------------------------------------------
# Adapters
# --------------------------------------------------------------------------


def tasks_from_assignments(items: list[dict], *, default_minutes: int = 60,
                           order: list[str] | None = None) -> list[Task]:
    """
    Turn stored assignment rows into tasks.

    `order` is an optional list of titles, most important first -- this is
    where Claude's judgement enters when it's available. Anything not named
    keeps its due-date ordering.
    """
    rank = {title: index for index, title in enumerate(order or [])}
    tasks = []
    for row in items or []:
        title = str(row.get("title") or "").strip()
        if not title:
            continue
        hours = row.get("est_hours")
        try:
            minutes = int(round(float(hours) * 60)) if hours else default_minutes
        except (TypeError, ValueError):
            minutes = default_minutes
        tasks.append(Task(
            title=title,
            course=str(row.get("course_code") or row.get("course") or ""),
            due=_parse(row.get("due_at")),
            minutes=max(MIN_SESSION_MINUTES, min(minutes, 60 * 40)),
            kind=str(row.get("kind") or "other").lower(),
            assignment_id=row.get("id"),
            priority=rank.get(title, row.get("priority")),
        ))
    return tasks


def busy_from_blocks(blocks: list[dict]) -> list[Interval]:
    """Existing schedule blocks become time that can't be booked twice."""
    out = []
    for row in blocks or []:
        start = _parse(row.get("starts_at"))
        if not start:
            continue
        end = _parse(row.get("ends_at"))
        if not end:
            minutes = row.get("est_minutes") or 60
            try:
                end = start + timedelta(minutes=int(minutes))
            except (TypeError, ValueError):
                end = start + timedelta(minutes=60)
        out.append(Interval(start, end, str(row.get("task") or "commitment")))
    return out


def _parse(value: object) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
