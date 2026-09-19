"""
Offline tests. No API key, no network, no database, no cost.

    MODE=mock python3 test_loop.py

Run this after every change. It catches the boring bugs (malformed message
history, renamed tools, a card type that stopped validating) that otherwise
show up as the model "acting weird" and cost you an hour of staring at it.
"""

from __future__ import annotations

import json
import os
import sys

os.environ.setdefault("MODE", "mock")

import canvas  # noqa: E402
import display  # noqa: E402
import orchestrator  # noqa: E402
import tools  # noqa: E402
from nemotron_client import Reply  # noqa: E402

PASS, FAIL = [], []


def check(name: str, fn) -> None:
    try:
        fn()
        PASS.append(name)
        print(f"  ok    {name}")
    except AssertionError as exc:
        FAIL.append((name, str(exc) or "assertion failed"))
        print(f"  FAIL  {name}: {exc}")
    except Exception as exc:  # noqa: BLE001
        FAIL.append((name, f"{type(exc).__name__}: {exc}"))
        print(f"  ERROR {name}: {type(exc).__name__}: {exc}")


class ScriptedNemotron:
    """Replays a list of Replies and records the message history it was sent."""

    def __init__(self, script: list[Reply]):
        self.script = list(script)
        self.seen: list[list[dict]] = []
        self.call_log: list[dict] = []

    def chat(self, messages, **kwargs):
        self.seen.append([dict(m) for m in messages])
        if not self.script:
            raise AssertionError("loop asked for more turns than the script has")
        return self.script.pop(0)


def tool_call(cid: str, name: str, args: dict) -> dict:
    return {"id": cid, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}


# ==========================================================================
# The tool layer
# ==========================================================================


def t_schema_matches_dispatch():
    advertised = {t["function"]["name"] for t in tools.TOOL_SCHEMAS}
    assert advertised == set(tools.DISPATCH), "schema and dispatch disagree"
    assert len(advertised) == 10, f"expected 10 tools, got {len(advertised)}"


def t_every_tool_runs_in_mock():
    """Every advertised tool must return a dict without raising."""
    sample_args = {
        "check_freshness": {},
        "get_assignments": {"due_within_days": 7},
        "refresh_from_canvas": {},
        "make_schedule": {"horizon_days": 7, "constraints": "no Fridays"},
        "get_schedule": {},
        "make_study_guide": {"course": "PHYS 1361", "topics": ["Gauss's law"]},
        "get_events": {"within_days": 14},
        "get_workload_history": {"days": 30},
        "update_preferences": {"updates": {"display_name": "Finn"}},
        "log_time": {"minutes": 90},
    }
    for name in tools.DISPATCH:
        result = tools.execute(name, sample_args[name])
        assert isinstance(result, dict), f"{name} did not return a dict"
        assert "error" not in result, f"{name} errored: {result.get('error')}"


def t_unknown_tool_is_data_not_a_crash():
    r = tools.execute("drop_all_tables", {})
    assert "error" in r and "unknown tool" in r["error"]


def t_malformed_arguments_handled():
    r = tools.execute("get_assignments", "{not valid json")
    assert "error" in r and "parse" in r["error"]


def t_wrong_argument_names_handled():
    r = tools.execute("get_assignments", {"nonexistent_param": 5})
    assert "error" in r and "bad arguments" in r["error"]


def t_credentials_are_rejected():
    for field in ["password", "canvas_token", "user_password", "API_KEY", "my-secret"]:
        assert tools.is_banned(field), f"{field} should be banned"
    for field in ["display_name", "study_time", "nickname", "timezone"]:
        assert not tools.is_banned(field), f"{field} should be allowed"

    r = tools.execute("update_preferences", {"updates": {
        "display_name": "Finn", "password": "hunter2", "canvas_token": "abc"}})
    assert r["prefs_keys"] == ["display_name"], r
    assert set(r["rejected_fields"]) == {"password", "canvas_token"}, r


# ==========================================================================
# The orchestration loop
# ==========================================================================


def t_single_tool_then_summary():
    nem = ScriptedNemotron([
        Reply(reasoning="Check freshness before trusting stored data.",
              tool_calls=[tool_call("c1", "get_assignments", {"due_within_days": 7})]),
        Reply(content="You have four open items this week."),
    ])
    run = orchestrator.run("what's due this week?", nem=nem)
    assert run.error is None, run.error
    assert run.turns == 2, run.turns
    assert [s["tool"] for s in run.steps] == ["get_assignments"]
    assert run.steps[0]["ok"] is True, run.steps[0]
    assert "four open items" in run.summary
    assert run.reasoning, "reasoning should be captured for the demo trace"
    _HISTORY.append(nem)


_HISTORY: list[ScriptedNemotron] = []


def t_history_is_well_formed():
    """Turn 2 must be: system, user, assistant(tool_calls), tool(result)."""
    nem = _HISTORY[0]
    second = nem.seen[1]
    assert [m["role"] for m in second] == ["system", "user", "assistant", "tool"], \
        [m["role"] for m in second]

    assistant = second[2]
    assert assistant["tool_calls"][0]["function"]["name"] == "get_assignments"
    # NVIDIA's docs: never feed the reasoning trace back in.
    assert "reasoning" not in assistant and "reasoning_content" not in assistant

    tool_msg = second[3]
    assert tool_msg["tool_call_id"] == assistant["tool_calls"][0]["id"]
    payload = json.loads(tool_msg["content"])
    assert payload["items"][0]["course_code"] == "PHYS 1361"


def t_stale_data_triggers_refresh_chain():
    """The demo beat: stale -> refresh -> read -> answer."""
    nem = ScriptedNemotron([
        Reply(tool_calls=[tool_call("c1", "check_freshness", {})]),
        Reply(tool_calls=[tool_call("c2", "refresh_from_canvas", {})]),
        Reply(tool_calls=[tool_call("c3", "get_assignments", {"due_within_days": 7})]),
        Reply(content="Refreshed and found four items."),
    ])
    run = orchestrator.run("give me the latest from canvas", nem=nem)
    assert [s["tool"] for s in run.steps] == \
        ["check_freshness", "refresh_from_canvas", "get_assignments"]
    assert run.turns == 4
    assert all(s["ok"] for s in run.steps)


def t_turn_limit_stops_runaway():
    forever = [Reply(tool_calls=[tool_call(f"c{i}", "get_assignments", {})])
               for i in range(orchestrator.MAX_TURNS)]
    run = orchestrator.run("loop forever", nem=ScriptedNemotron(forever))
    assert run.turns == orchestrator.MAX_TURNS
    assert run.error is None
    assert run.summary, "should still produce something to show"


def t_tool_failure_does_not_kill_the_run():
    nem = ScriptedNemotron([
        Reply(tool_calls=[tool_call("c1", "no_such_tool", {})]),
        Reply(content="I couldn't do that, but here's what I can see."),
    ])
    run = orchestrator.run("do something impossible", nem=nem)
    assert run.error is None
    assert run.steps[0]["ok"] is False
    assert "unknown tool" in run.steps[0]["result_preview"]


def t_voice_path_is_constrained():
    nem = ScriptedNemotron([Reply(content="Two things due tomorrow.")])
    run = orchestrator.run("what's due", nem=nem, channel="voice")
    assert run.turns == 1
    assert run.display["speech"]


def t_mock_nemotron_plans_sensibly():
    """The built-in mock should pick different tools for different prompts."""
    from nemotron_client import NemotronClient

    available = {t["function"]["name"] for t in tools.TOOL_SCHEMAS}
    cases = {
        "refresh my canvas data": "refresh_from_canvas",
        "make me a schedule for this week": "make_schedule",
        "help me study for the quiz": "make_study_guide",
        "what events are on campus": "get_events",
        "is this week busier than last week": "get_workload_history",
    }
    for prompt, expected in cases.items():
        run = orchestrator.run(prompt, nem=NemotronClient(mock=True))
        called = [s["tool"] for s in run.steps]
        assert expected in called, f"'{prompt}' -> {called}, expected {expected}"
        assert run.error is None, f"'{prompt}': {run.error}"


# ==========================================================================
# The display contract
# ==========================================================================


def t_display_drops_junk_cards():
    spec = display.validate({
        "speech": "ok",
        "cards": [
            {"type": "text", "title": "Good", "body": "kept"},
            {"type": "iframe", "src": "http://evil.example"},          # unknown type
            {"type": "schedule", "title": "no blocks key"},             # missing field
            {"type": "text", "title": "Extra", "body": "kept",
             "onclick": "alert(1)"},                                    # stray key
        ],
    })
    assert [c["type"] for c in spec["cards"]] == ["text", "text"], spec["cards"]
    assert "onclick" not in spec["cards"][1]
    assert any("unknown-type" in d for d in spec["_dropped"])


def t_display_strips_markup_from_speech():
    spec = display.validate({"speech": "hi <script>steal()</script> there", "cards": []})
    assert "<script" not in spec["speech"]


def t_display_caps_card_count():
    many = [{"type": "text", "title": f"c{i}", "body": "x"} for i in range(10)]
    spec = display.validate({"speech": "x", "cards": many})
    assert len(spec["cards"]) == display.MAX_CARDS


def t_display_never_returns_empty():
    spec = display.validate({"cards": []}, fallback_summary="the answer")
    assert len(spec["cards"]) == 1
    assert spec["cards"][0]["body"] == "the answer"


def t_all_card_types_validate():
    """Every type in the contract must survive validation when well-formed."""
    samples = {
        "assignment_list": {"title": "t", "items": []},
        "schedule": {"title": "t", "blocks": []},
        "study_set": {"title": "t", "sections": []},
        "event_list": {"title": "t", "items": []},
        "workload_chart": {"title": "t", "series": []},
        "text": {"title": "t", "body": "b"},
        "alert": {"title": "t", "body": "b"},
    }
    assert set(samples) == set(display.CARD_TYPES), "test is out of sync with contract"
    for ctype, fields in samples.items():
        spec = display.validate({"speech": "x", "cards": [{"type": ctype, **fields}]})
        assert spec["cards"][0]["type"] == ctype, f"{ctype} was dropped"


# ==========================================================================
# The Canvas parser
# ==========================================================================


def t_canvas_finds_pages():
    pages = canvas.list_sources()
    assert pages, f"no pages in ./{canvas.CANVAS_DIR}/"


def t_canvas_strips_to_clean_text():
    raw = canvas.read_source(canvas.list_sources()[0])
    text = canvas.html_to_text(raw)
    assert len(text) < len(raw) / 2, "stripping should shrink the page a lot"
    for junk in ["<div", "<script", "ic-Layout", "stylesheet", "Privacy Policy"]:
        assert junk not in text, f"'{junk}' survived stripping"
    for wanted in ["PHYS 1361", "Problem Set 4", "Sep 22", "50 pts"]:
        assert wanted in text, f"'{wanted}' was lost"


def t_canvas_handles_broken_html():
    """Real scraped pages are malformed. The parser must not care."""
    for bad in ["<div><p>unclosed", "", "<html>", "not html at all",
                "<li>A<li>B<li>C"]:
        canvas.html_to_text(bad)  # just must not raise


def t_canvas_missing_page_is_clear():
    try:
        canvas.read_source("does_not_exist.html")
        raise AssertionError("should have raised")
    except FileNotFoundError as exc:
        assert "Available" in str(exc), "error should list what IS available"


# ==========================================================================

if __name__ == "__main__":
    print(f"MODE={os.environ['MODE']}  (no network, no cost)\n")

    print("tool layer")
    check("schema matches dispatch", t_schema_matches_dispatch)
    check("every tool runs in mock", t_every_tool_runs_in_mock)
    check("unknown tool is data", t_unknown_tool_is_data_not_a_crash)
    check("malformed arguments handled", t_malformed_arguments_handled)
    check("wrong argument names handled", t_wrong_argument_names_handled)
    check("credentials rejected", t_credentials_are_rejected)

    print("\norchestration loop")
    check("single tool then summary", t_single_tool_then_summary)
    check("message history well-formed", t_history_is_well_formed)
    check("stale -> refresh -> read chain", t_stale_data_triggers_refresh_chain)
    check("turn limit stops runaway", t_turn_limit_stops_runaway)
    check("tool failure survivable", t_tool_failure_does_not_kill_the_run)
    check("voice path constrained", t_voice_path_is_constrained)
    check("mock plans sensibly", t_mock_nemotron_plans_sensibly)

    print("\ndisplay contract")
    check("drops junk cards", t_display_drops_junk_cards)
    check("strips markup from speech", t_display_strips_markup_from_speech)
    check("caps card count", t_display_caps_card_count)
    check("never returns empty", t_display_never_returns_empty)
    check("all card types validate", t_all_card_types_validate)

    print("\ncanvas parser")
    check("finds pages", t_canvas_finds_pages)
    check("strips to clean text", t_canvas_strips_to_clean_text)
    check("handles broken html", t_canvas_handles_broken_html)
    check("missing page error is clear", t_canvas_missing_page_is_clear)

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        for name, why in FAIL:
            print(f"  {name}: {why}")
        sys.exit(1)
