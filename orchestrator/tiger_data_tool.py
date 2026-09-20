"""
NeMo Agent Toolkit (NAT) tools for Office Hours.

WHAT CHANGED AND WHY

The original version wrote its own SQL. That meant two different places in the
project could query the same tables with different assumptions -- and they
already disagreed. Three concrete bugs it had:

  1. It accepted `student_id` but the WHERE clause was commented out, so it
     returned EVERY student's assignments and ignored the argument entirely.
  2. No status filter, so assignments you'd already submitted and had graded
     came back listed as upcoming.
  3. It returned a formatted string ("CS 1675: Lab 3 - due ..."), which the
     display agent can't turn into cards. Cards need fields, not prose.

So this version doesn't write SQL at all. Every function here delegates to
`tools.py`, which delegates to `db.py`, which owns the schema. One
implementation, one set of validation rules, one place to fix a bug.

That also means these tools automatically respect MODE=mock, so the NAT agent
can be developed and demoed without a database or any credits.

Register all of them in config.yml under `functions:`.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from nat.builder.builder import Builder
from nat.builder.function_info import FunctionInfo
from nat.cli.register_workflow import register_function
from nat.data_models.function import FunctionBaseConfig
from pydantic import Field

# tools.py imports config, which loads .env. Import it once here so every NAT
# function below shares the same settings and MODE.
import config  # noqa: F401
import tools


# --------------------------------------------------------------------------
# Shared helpers
async def _call(name: str, **kwargs: Any) -> str:
    """
    Run one of our tools and return JSON.

    Two things worth knowing:

    - tools.py is synchronous (it uses psycopg, not asyncpg). NAT wants async
      functions. asyncio.to_thread runs the sync call on a worker thread so it
      doesn't block the event loop -- much simpler than maintaining a second,
      async database layer that could drift from the first.

    - We return a JSON string rather than prose. The agent reads JSON fine,
      and the website's display agent needs real fields to build cards from.
    """
    result = await asyncio.to_thread(tools.execute, name, kwargs)
    return json.dumps(result, default=str)


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------


class TigerDataQueryConfig(FunctionBaseConfig, name="tiger_data_query"):
    """Read assignments out of Tiger Data."""

    # No connection string here. It lives in DATABASE_URL, loaded from .env by
    # config.py, so it never appears in a file that gets committed.
    default_days: int = Field(
        default=14, description="How far ahead to look when the agent doesn't say."
    )


@register_function(config_type=TigerDataQueryConfig)
async def tiger_data_query_function(config: TigerDataQueryConfig, builder: Builder):
    async def _query(
        course: str = "",
        due_within_days: int | None = None,
        status: str = "open",
    ) -> str:
        return await _call(
            "get_assignments",
            course=course or None,
            due_within_days=due_within_days or config.default_days,
            status=status,
        )

    yield FunctionInfo.from_fn(
        _query,
        description=(
            "Read the student's assignments from Tiger Data. Returns JSON with "
            "course_code, title, kind, due_at, points, est_hours and status for "
            "each one. Optional filters: course (e.g. 'PHYS 1351'), "
            "due_within_days, status (open/submitted/graded/any). Fast and free "
            "-- prefer this over refreshing from Canvas."
        ),
    )


class OverdueConfig(FunctionBaseConfig, name="tiger_data_overdue"):
    """Find work that is past its due date and not submitted."""


@register_function(config_type=OverdueConfig)
async def overdue_function(config: OverdueConfig, builder: Builder):
    async def _overdue() -> str:
        # Delegates to tools.get_overdue, which runs the comparison in SQL so
        # 'now' is the database's clock. Doing the date math here in Python
        # would reintroduce a timezone mismatch between the laptop and the
        # stored timestamps.
        return await _call("get_overdue")

    yield FunctionInfo.from_fn(
        _overdue,
        description=(
            "List assignments that are PAST their due date and still not "
            "submitted, each with days_late. Use this for 'do I have anything "
            "overdue', 'am I behind', 'what did I miss'. Returns JSON."
        ),
    )


class FreshnessConfig(FunctionBaseConfig, name="tiger_data_freshness"):
    """Check how stale the stored data is."""


@register_function(config_type=FreshnessConfig)
async def freshness_function(config: FreshnessConfig, builder: Builder):
    async def _freshness() -> str:
        return await _call("check_freshness")

    yield FunctionInfo.from_fn(
        _freshness,
        description=(
            "Check how old the stored coursework data is, per course. Returns "
            "last_crawled_at, age_hours and a 'stale' flag. Call this FIRST for "
            "questions about current coursework, so you know whether to trust "
            "what's stored or refresh it. Instant and free."
        ),
    )


class ScheduleReadConfig(FunctionBaseConfig, name="tiger_data_schedule"):
    """Read the most recent generated schedule."""


@register_function(config_type=ScheduleReadConfig)
async def schedule_read_function(config: ScheduleReadConfig, builder: Builder):
    async def _schedule() -> str:
        return await _call("get_schedule")

    yield FunctionInfo.from_fn(
        _schedule,
        description="Read the most recently generated study schedule. Returns JSON.",
    )


class WorkloadHistoryConfig(FunctionBaseConfig, name="tiger_data_workload_history"):
    """Time-series workload trend from the Timescale hypertable."""


@register_function(config_type=WorkloadHistoryConfig)
async def workload_history_function(config: WorkloadHistoryConfig, builder: Builder):
    async def _history(days: int = 30) -> str:
        return await _call("get_workload_history", days=days)

    yield FunctionInfo.from_fn(
        _history,
        description=(
            "Read how the student's workload has changed over time: estimated "
            "hours and open assignment count per course per day, from the "
            "Timescale hypertable. Use for 'is this week busier than last "
            "week' or 'how has my workload trended'. Returns JSON."
        ),
    )


class EventsConfig(FunctionBaseConfig, name="tiger_data_events"):
    """Upcoming on-campus events."""


@register_function(config_type=EventsConfig)
async def events_function(config: EventsConfig, builder: Builder):
    async def _events(within_days: int = 14) -> str:
        return await _call("get_events", within_days=within_days)

    yield FunctionInfo.from_fn(
        _events,
        description="Read upcoming on-campus events. Returns JSON.",
    )


# --------------------------------------------------------------------------
# Writes
# --------------------------------------------------------------------------


class RefreshConfig(FunctionBaseConfig, name="canvas_refresh"):
    """Re-read Canvas and update Tiger Data."""


@register_function(config_type=RefreshConfig)
async def refresh_function(config: RefreshConfig, builder: Builder):
    async def _refresh() -> str:
        return await _call("refresh_from_canvas")

    yield FunctionInfo.from_fn(
        _refresh,
        description=(
            "Re-read the student's Canvas pages and write what's found into "
            "Tiger Data. SLOW (10-40 seconds) and costs money. Only call when "
            "the student explicitly asks to refresh or sync, or when "
            "tiger_data_freshness says the data is stale."
        ),
    )


class MakeScheduleConfig(FunctionBaseConfig, name="make_schedule"):
    """Build and save a time-blocked study plan."""


@register_function(config_type=MakeScheduleConfig)
async def make_schedule_function(config: MakeScheduleConfig, builder: Builder):
    async def _make(horizon_days: int = 7, constraints: str = "") -> str:
        return await _call(
            "make_schedule", horizon_days=horizon_days, constraints=constraints
        )

    yield FunctionInfo.from_fn(
        _make,
        description=(
            "Build a time-blocked study plan from the student's open "
            "assignments and save it. Pass constraints as free text, e.g. "
            "'class 9-11am weekdays, orchestra Tuesday, no work Friday night'. "
            "Returns JSON with blocks and a rationale."
        ),
    )


class StudyGuideConfig(FunctionBaseConfig, name="make_study_guide"):
    """Build and save a study set."""


@register_function(config_type=StudyGuideConfig)
async def study_guide_function(config: StudyGuideConfig, builder: Builder):
    async def _guide(course: str, topics: str, format: str = "outline") -> str:  # noqa: A002
        # Agents pass lists inconsistently; accept a comma-separated string too.
        topic_list = (
            [t.strip() for t in topics.split(",") if t.strip()]
            if isinstance(topics, str)
            else list(topics)
        )
        return await _call(
            "make_study_guide", course=course, topics=topic_list, format=format
        )

    yield FunctionInfo.from_fn(
        _guide,
        description=(
            "Build a study set (summaries plus practice questions) for a course "
            "and topics, and save it. 'topics' is a comma-separated list. "
            "'format' is flashcards, outline, or practice_problems."
        ),
    )


class PreferencesConfig(FunctionBaseConfig, name="update_preferences"):
    """Remember something about the student."""


@register_function(config_type=PreferencesConfig)
async def preferences_function(config: PreferencesConfig, builder: Builder):
    async def _update(updates_json: str) -> str:
        try:
            updates = json.loads(updates_json)
        except json.JSONDecodeError as exc:
            return json.dumps({"error": f"updates_json must be valid JSON: {exc}"})
        if not isinstance(updates, dict):
            return json.dumps({"error": "updates_json must be a JSON object"})
        return await _call("update_preferences", updates=updates)

    yield FunctionInfo.from_fn(
        _update,
        description=(
            "Save something the student wants remembered: their name, timezone, "
            "when they prefer to study, days they can't work, anything. Pass a "
            "JSON object of key-value pairs, e.g. "
            '{"display_name": "Finn", "no_work_days": ["Friday"]}. '
            "NEVER pass passwords or login credentials -- they are rejected."
        ),
    )


# --------------------------------------------------------------------------
