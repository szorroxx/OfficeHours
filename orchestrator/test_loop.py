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
import re
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
    assert len(advertised) == 14, f"expected 14 tools, got {len(advertised)}"


def t_every_tool_runs_in_mock():
    """Every advertised tool must return a dict without raising."""
    sample_args = {
        "check_freshness": {},
        "get_assignments": {"due_within_days": 7},
        "get_overdue": {},
        "refresh_from_canvas": {},
        "make_schedule": {"horizon_days": 7, "constraints": "no Fridays"},
        "get_schedule": {},
        "add_to_schedule": {"items": [{"task": "x",
                                       "starts_at": "2026-09-20T13:00:00-04:00"}]},
        "make_study_guide": {"course": "PHYS 1361", "topics": ["Gauss's law"]},
        "get_events": {"within_days": 14},
        "get_workload_history": {"days": 30},
        "find_campus_events": {"days": 14},
        "fetch_page": {"url": "https://calendar.pitt.edu/"},
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


def t_database_url_validation():
    """A placeholder connection string must be caught before we try to connect."""
    import db

    bad = {
        "": "empty",
        "postgres://user:pass@host:port/tsdb?sslmode=require": "placeholder",
        "postgres://tsdbadmin:REPLACE_ME@real.host:5432/tsdb": "placeholder",
        "mysql://a:b@real.host:3306/x": "wrong scheme",
        "postgres://tsdbadmin:pw@real.tsdb.cloud.timescale.com/tsdb": "no port",
    }
    for url, why in bad.items():
        assert db.check_url(url) is not None, f"should reject ({why}): {url!r}"

    good = [
        "postgres://tsdbadmin:pw@abc.xyz.tsdb.cloud.timescale.com:36756/tsdb?sslmode=require",
        "postgresql://tsdbadmin:pw@abc.tsdb.cloud.timescale.com:36756/tsdb?sslmode=require",
        # localhost is legitimate for a local Postgres and must NOT be rejected
        "postgres://me:pw@localhost:5432/officehours",
    ]
    for url in good:
        assert db.check_url(url) is None, f"should accept: {url}  got {db.check_url(url)}"


def t_placeholder_credentials_are_flagged():
    import config

    assert config._looks_real("nvapi-abc123", "nvapi-")
    assert not config._looks_real("nvapi-REPLACE_ME", "nvapi-")
    assert not config._looks_real("", "nvapi-")
    assert not config._looks_real(
        "postgres://user:pass@host:port/tsdb", "postgres")
    assert config._looks_real(
        "postgres://tsdbadmin:real@x.tsdb.cloud.timescale.com:36756/tsdb", "postgres")


def t_webfetch_blocks_hostile_urls():
    """
    The model picks the URL; your machine makes the request. So the guards are
    the only thing between a chosen URL and your local network.
    """
    import webfetch

    saved = list(webfetch.ALLOWED_DOMAINS)
    try:
        webfetch.ALLOWED_DOMAINS[:] = ["calendar.pitt.edu"]

        must_block = [
            ("file:///etc/passwd", "scheme"),
            ("ftp://calendar.pitt.edu/x", "scheme"),
            ("https://example.com/", "not allowlisted"),
            ("https://calendar.pitt.edu.evil.com/", "lookalike domain"),
            ("", "empty"),
        ]
        for url, why in must_block:
            try:
                webfetch.validate(url)
                raise AssertionError(f"should have blocked ({why}): {url!r}")
            except webfetch.FetchBlocked:
                pass
    finally:
        webfetch.ALLOWED_DOMAINS[:] = saved


def t_webfetch_blocks_private_addresses():
    """
    Even an allowlisted hostname must be refused if it resolves to a private
    address -- that's the SSRF path to a router or a cloud metadata service.
    """
    import webfetch

    saved = list(webfetch.ALLOWED_DOMAINS)
    try:
        webfetch.ALLOWED_DOMAINS[:] = ["localhost"]
        try:
            webfetch.validate("http://localhost:8000/admin")
            raise AssertionError("localhost resolves to 127.0.0.1 and must be refused")
        except webfetch.FetchBlocked as exc:
            assert "not a public address" in str(exc), str(exc)
    finally:
        webfetch.ALLOWED_DOMAINS[:] = saved


def t_webfetch_empty_allowlist_disables_fetching():
    import webfetch

    saved = list(webfetch.ALLOWED_DOMAINS)
    try:
        webfetch.ALLOWED_DOMAINS[:] = []
        try:
            webfetch.validate("https://calendar.pitt.edu/")
            raise AssertionError("empty allowlist should permit nothing")
        except webfetch.FetchBlocked as exc:
            assert "empty" in str(exc).lower()
    finally:
        webfetch.ALLOWED_DOMAINS[:] = saved


def t_webfetch_subdomain_matching():
    import webfetch

    saved = list(webfetch.ALLOWED_DOMAINS)
    try:
        webfetch.ALLOWED_DOMAINS[:] = ["pitt.edu"]
        assert webfetch._host_allowed("calendar.pitt.edu")
        assert webfetch._host_allowed("pitt.edu")
        assert not webfetch._host_allowed("pitt.edu.attacker.net")
        assert not webfetch._host_allowed("notpitt.edu")
    finally:
        webfetch.ALLOWED_DOMAINS[:] = saved


def t_fetched_text_is_labelled_untrusted():
    """
    Fetched page text goes into the model's context, and the model has write
    tools. The wrapper is what tells it to treat the content as data.
    """
    import webfetch

    assert "UNTRUSTED" in webfetch.UNTRUSTED_HEADER
    assert "not" in webfetch.UNTRUSTED_HEADER.lower()
    assert "instructions" in webfetch.UNTRUSTED_HEADER.lower()


def t_system_prompt_warns_about_injection():
    assert "fetch_page" in orchestrator.SYSTEM_PROMPT
    lowered = orchestrator.SYSTEM_PROMPT.lower()
    assert "data, not" in lowered or "not instructions" in lowered
    assert "suspicious" in lowered


def t_localist_parsing():
    """Localist nests events two levels deep; make sure we unwrap correctly."""
    ev = {
        "title": "Heinz Chapel Choir",
        "location_name": "Heinz Memorial Chapel",
        "localist_url": "https://calendar.pitt.edu/event/choir",
        "keywords": ["music", "arts"],
        "event_instances": [
            {"event_instance": {"start": "2026-09-24T19:30:00-04:00"}}
        ],
    }
    assert tools._first_instance(ev) == "2026-09-24T19:30:00-04:00"
    assert tools._first_instance({"first_date": "2026-10-01"}) == "2026-10-01"
    assert tools._first_instance({}) is None


def t_detects_javascript_shell_pages():
    """
    Notion and Canvas both send an empty shell. That must be detected, not
    silently returned as an empty answer.
    """
    import webfetch

    notion = ("<html><head><title>Notion</title></head><body>"
              "JavaScript must be enabled in order to use Notion. "
              "Please enable JavaScript to continue.</body></html>"
              "<script>" + "x" * 9000 + "</script>")
    text = canvas.html_to_text(notion)
    assert webfetch._looks_like_js_shell(notion, text), "Notion shell missed"

    # Mostly-script page with almost no text is also a shell.
    bare = "<html><body><div id='app'></div></body></html>" + "<script>" + "y" * 9000 + "</script>"
    assert webfetch._looks_like_js_shell(bare, canvas.html_to_text(bare))

    # A real content page must NOT be flagged.
    name = _first_html_page()
    if name is not None:
        raw = canvas.read_source(name)
        assert not webfetch._looks_like_js_shell(raw, canvas.html_to_text(raw)), \
            f"{name} wrongly flagged as a JS shell"


def t_plain_text_captures_recognised():
    assert canvas.is_plain_text("cs1684_notion.txt")
    assert not canvas.is_plain_text("page.html")
    assert not canvas.is_plain_text("canvas_export.json")


def t_rendered_fetch_still_validates_url():
    """The headless browser must not be a way round the allowlist."""
    import webfetch

    saved = list(webfetch.ALLOWED_DOMAINS)
    try:
        webfetch.ALLOWED_DOMAINS[:] = ["calendar.pitt.edu"]
        try:
            webfetch.fetch_rendered("https://example.com/")
            raise AssertionError("fetch_rendered must run the same guards")
        except webfetch.FetchBlocked:
            pass
    finally:
        webfetch.ALLOWED_DOMAINS[:] = saved


def t_database_types_are_json_safe():
    """
    Postgres returns datetime, date and Decimal, none of which json.dumps can
    handle. Every tool result gets serialized, so one unconverted value
    anywhere crashes the whole request -- and only when a row happens to
    contain one, so an empty result set passes while a populated one fails.
    """
    import json
    from datetime import date, datetime, timedelta
    from decimal import Decimal

    import db

    row = {
        "due_at": datetime(2026, 9, 22, 23, 59),
        "day": date(2026, 9, 19),
        "points": Decimal("50"),
        "est_hours": Decimal("2.5"),
        "gap": timedelta(hours=3),
        "nested": [{"when": datetime(2026, 10, 1)}],
        "deeper": {"list": [Decimal("1.5")]},
        "title": "Problem Set 4",
        "nothing": None,
    }
    safe = db.jsonable(row)
    json.dumps(safe)  # must not raise

    assert safe["due_at"] == "2026-09-22T23:59:00"
    assert safe["day"] == "2026-09-19"
    # float, not str -- the frontend and the model do arithmetic on these
    assert safe["points"] == 50.0 and isinstance(safe["points"], float)
    assert safe["est_hours"] == 2.5
    assert safe["nested"][0]["when"] == "2026-10-01T00:00:00"
    assert safe["deeper"]["list"][0] == 1.5
    assert safe["title"] == "Problem Set 4"
    assert safe["nothing"] is None


def t_tool_results_serialize_even_if_db_bypassed():
    """
    default=str in the orchestrator is the safety net for any tool that doesn't
    go through db.jsonable. Check the net is actually there.
    """
    import re
    from pathlib import Path

    src = (Path(__file__).parent / "orchestrator.py").read_text()
    dumps = re.findall(r"json\.dumps\([^)]*\)", src)
    bare = [d for d in dumps if "default=" not in d]
    assert not bare, f"json.dumps without default=str in orchestrator.py: {bare}"


def t_add_to_schedule():
    r = tools.execute("add_to_schedule", {"items": [
        {"task": "Asia Film Festival", "starts_at": "2026-09-20T13:00:00-04:00",
         "est_minutes": 120},
    ]})
    assert "error" not in r, r
    assert r["blocks_added"] == 1
    assert r["added"] == ["Asia Film Festival"]

    for bad in [{"items": []}, {"items": "nope"}]:
        assert "error" in tools.execute("add_to_schedule", bad), bad


def t_scheduling_tools_are_distinguished():
    """
    The model has to know make_schedule re-plans while add_to_schedule appends.
    Without that, asking to add one event wipes the existing schedule.
    """
    names = {t["function"]["name"] for t in tools.TOOL_SCHEMAS}
    assert {"make_schedule", "add_to_schedule"} <= names

    add = next(t for t in tools.TOOL_SCHEMAS
               if t["function"]["name"] == "add_to_schedule")
    desc = add["function"]["description"].lower()
    assert "without rebuilding" in desc or "without re-plan" in desc

    prompt = orchestrator.SYSTEM_PROMPT.lower()
    assert "add_to_schedule" in prompt
    assert "make_schedule" in prompt


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


def t_unsupported_param_parsing():
    """The exact 400 the hosted NVIDIA endpoint returns must be understood."""
    import nemotron_client as nc

    real = ("Error code: 400 - {'error': {'message': 'Validation: Unsupported "
            "parameter(s): `thinking_token_budget`', 'type': 'Bad Request', "
            "'code': 400}}")
    assert nc._parse_unsupported(real) == {"thinking_token_budget"}, \
        nc._parse_unsupported(real)

    multi = "Validation: Unsupported parameter(s): `low_effort`, `force_nonempty_content`"
    assert nc._parse_unsupported(multi) == {"low_effort", "force_nonempty_content"}

    # A 400 about something else must NOT ban any parameters.
    other = ("Error code: 400 - {'error': {'message': 'messages must not be "
             "empty', 'type': 'Bad Request'}}")
    assert nc._parse_unsupported(other) == set(), nc._parse_unsupported(other)
    assert nc._parse_unsupported("Error code: 429 - rate limited") == set()


def t_unsupported_params_are_stripped():
    import nemotron_client as nc

    saved = set(nc.UNSUPPORTED_PARAMS)
    try:
        nc.UNSUPPORTED_PARAMS.clear()
        nc.UNSUPPORTED_PARAMS.update({"thinking_token_budget", "low_effort"})
        extra = {
            "chat_template_kwargs": {"enable_thinking": True, "low_effort": True},
            "thinking_token_budget": 1024,
        }
        cleaned = nc._drop_unsupported(extra)
        assert "thinking_token_budget" not in cleaned
        assert "low_effort" not in cleaned["chat_template_kwargs"]
        assert cleaned["chat_template_kwargs"]["enable_thinking"] is True
    finally:
        nc.UNSUPPORTED_PARAMS.clear()
        nc.UNSUPPORTED_PARAMS.update(saved)


def t_thinking_budget_off_by_default():
    """Hosted build.nvidia.com rejects it, so it must not ship enabled."""
    import nemotron_client as nc

    assert "thinking_token_budget" in nc.UNSUPPORTED_PARAMS, (
        "thinking_token_budget should be disabled by default; set "
        "NEMOTRON_ALLOW_THINKING_BUDGET=1 only for self-hosted NIM"
    )


def t_overdue_tool():
    r = tools.execute("get_overdue", {})
    assert "error" not in r, r
    assert "items" in r and "count" in r
    for item in r["items"]:
        assert "days_late" in item, "overdue items need days_late for the UI"
        assert item["status"] == "open", "a submitted assignment isn't overdue"


def t_nat_config_matches_tool_file():
    """
    config.yml and tiger_data_tool.py must not drift. This is the failure that
    shows up as a NAT startup error nobody can read.
    """
    from pathlib import Path

    try:
        import yaml
    except ImportError:
        return  # pyyaml not installed; skip rather than fail

    here = Path(__file__).parent
    cfg_path, tool_path = here / "config.yml", here / "tiger_data_tool.py"
    if not (cfg_path.exists() and tool_path.exists()):
        return  # NAT integration not in use

    cfg = yaml.safe_load(cfg_path.read_text())
    src = tool_path.read_text()

    registered = set(re.findall(r'FunctionBaseConfig, name="([a-z_]+)"', src))
    declared = {v["_type"] for v in cfg["functions"].values()}
    assert not (declared - registered), \
        f"config.yml references unregistered tools: {sorted(declared - registered)}"

    listed = set(cfg["workflow"]["tool_names"])
    assert not (listed - set(cfg["functions"])), \
        f"workflow.tool_names has undeclared entries: {sorted(listed - set(cfg['functions']))}"

    # And the thing that started all this: no secrets in a committed file.
    #
    # Check the PARSED values, not the raw text -- a connection string
    # mentioned in a comment is documentation, not a leak. Then scan the raw
    # text only for credentials that look genuinely real (a long key, or a
    # connection string with an actual password in it).
    import json as _json

    values = _json.dumps(cfg)
    for marker in ("postgres://", "postgresql://", "nvapi-", "sk-ant-"):
        assert marker not in values, \
            f"config.yml has a hardcoded {marker} value — use ${{VAR}} instead"

    raw = cfg_path.read_text()
    real_looking = [
        r"nvapi-[A-Za-z0-9_\-]{20,}",              # a real NVIDIA key
        r"sk-ant-[A-Za-z0-9_\-]{20,}",             # a real Anthropic key
        r"postgres(?:ql)?://[^\s:]+:[^\s@$]{6,}@",  # user:password@host
    ]
    for pattern in real_looking:
        found = re.search(pattern, raw)
        assert not found, (
            f"config.yml contains what looks like a live credential "
            f"({found.group(0)[:18]}...). Move it to .env."
        )


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


def t_display_fallback_builds_real_cards():
    """With Claude unavailable, cards must still come out of the tool results."""
    steps = [{
        "tool": "get_overdue", "arguments": {}, "ok": True, "ms": 12,
        "result_preview": "truncated...",
        "result": {"items": [
            {"course_code": "PHYS 1351", "title": "Problem Set 4",
             "due_at": "2026-09-15T23:59:00-04:00", "est_hours": 3.0,
             "status": "open", "days_late": 4.2},
        ], "count": 1},
    }]
    spec = display.fallback_spec("You have one overdue item.", steps)
    card = spec["cards"][0]
    assert card["type"] == "assignment_list", spec["cards"]
    assert card["items"][0]["course"] == "PHYS 1351"
    assert "days late" in card["items"][0]["due"]
    assert card["items"][0]["est_minutes"] == 180
    assert spec["headline"] == "1 overdue", spec["headline"]


def t_display_fallback_uses_full_result_not_preview():
    """
    result_preview is truncated at 400 chars for the UI. The fallback must read
    step['result'] instead, or every multi-item result silently becomes a plain
    text card.
    """
    steps = [{
        "tool": "get_assignments", "arguments": {}, "ok": True, "ms": 5,
        "result_preview": '{"items": [{"course_code": "CS 1675", "title": "Lab 3"...',
        "result": {"items": [
            {"course_code": "CS 1675", "title": f"Item {i}",
             "due_at": "2026-10-01T23:59:00-04:00", "est_hours": 1.0}
            for i in range(6)
        ]},
    }]
    spec = display.fallback_spec("Six things.", steps)
    assert spec["cards"][0]["type"] == "assignment_list", spec["cards"]
    assert len(spec["cards"][0]["items"]) == 6


def t_display_fallback_covers_every_tool():
    """Each tool that returns displayable data should map to a card."""
    cases = {
        "get_overdue": ({"items": []}, "alert"),
        "get_assignments": ({"items": []}, "alert"),
        "get_schedule": ({"blocks": [
            {"task": "Study", "starts_at": "2026-09-21T19:00:00-04:00",
             "ends_at": "2026-09-21T20:00:00-04:00", "est_minutes": 60}]}, "schedule"),
        "make_study_guide": ({"course": "PHYS 1351", "sections": [
            {"topic": "Gauss", "summary": "flux", "questions": ["q"]}]}, "study_set"),
        "get_events": ({"items": [
            {"title": "Fair", "starts_at": "2026-09-24T16:00:00-04:00",
             "location": "Alumni Hall"}]}, "event_list"),
        "get_workload_history": ({"points": [
            {"day": "2026-09-19", "course_code": "CS 1675", "est_hours": 4.0}]},
            "workload_chart"),
        "refresh_from_canvas": ({"synced": 3, "note": "ok"}, "text"),
        "update_preferences": ({"prefs_keys": ["display_name"]}, "text"),
    }
    for tool, (result, want) in cases.items():
        step = [{"tool": tool, "arguments": {}, "ok": True, "ms": 1,
                 "result_preview": "", "result": result}]
        spec = display.fallback_spec("summary", step)
        got = spec["cards"][0]["type"]
        assert got == want, f"{tool} -> {got}, expected {want}"


def t_display_explains_auth_failure():
    import nemotron_client  # noqa: F401

    class Fake401(Exception):
        status_code = 401
        def __str__(self):
            return "Error code: 401 - invalid x-api-key"

    note = display._explain(Fake401())
    assert "ANTHROPIC_API_KEY" in note, note
    assert "console.anthropic.com" in note


def t_failed_tools_are_skipped_by_fallback():
    steps = [
        {"tool": "get_overdue", "arguments": {}, "ok": False, "ms": 1,
         "result_preview": '{"error": "boom"}', "result": {"error": "boom"}},
    ]
    spec = display.fallback_spec("Something went wrong.", steps)
    assert spec["cards"][0]["type"] == "text"
    assert spec["cards"][0]["body"] == "Something went wrong."


# ==========================================================================
# The Canvas parser
# ==========================================================================


def t_canvas_finds_pages():
    pages = canvas.list_sources()
    assert pages, f"no pages in ./{canvas.CANVAS_DIR}/"


def _first_html_page() -> str | None:
    """
    list_sources() now returns .json and .txt too, and 'canvas_export.json'
    sorts first alphabetically -- so tests about HTML stripping must pick an
    HTML page explicitly rather than taking element zero.
    """
    for name in canvas.list_sources():
        if name.lower().endswith((".html", ".htm")):
            return name
    return None


def t_canvas_strips_to_clean_text():
    """Markup goes away, real content survives. Not tied to any one course."""
    import re

    name = _first_html_page()
    if name is None:
        return  # no HTML pages in this checkout; nothing to assert
    raw = canvas.read_source(name)
    text = canvas.html_to_text(raw)

    assert len(text) < len(raw) / 2, "stripping should shrink the page a lot"
    for junk in ["<div", "<script", "ic-Layout", "stylesheet", "Privacy Policy"]:
        assert junk not in text, f"'{junk}' survived stripping"

    # The things an extractor actually needs, checked by shape not by value.
    assert re.search(r"[A-Z]{2,8}\s\d{3,4}", text), "no course code survived"
    assert re.search(r"\d+\s*pts", text), "no point values survived"
    assert re.search(r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d",
                     text), "no due dates survived"
    assert "Assignments" in text, "no assignment heading survived"


def t_canvas_all_pages_usable():
    """Every page in the folder must actually have content in it."""
    bad = []
    for name in canvas.list_sources():
        if not name.lower().endswith((".html", ".htm")):
            continue  # diagnose() is for HTML; json/txt have their own paths
        check = canvas.diagnose(canvas.read_source(name), name)
        if not check["usable"]:
            bad.append(f"{name} ({check['verdict']})")
    assert not bad, f"unusable pages present: {bad}"


def t_canvas_detects_javascript_shell():
    """A saved Canvas page must be flagged, not silently parsed to nothing."""
    shell = """<html><head><script>ENV = {"a":1};</script></head><body>
      <noscript>You need to have JavaScript enabled in order to access this site.</noscript>
      <div id="application"><div>Empty Card</div><div>Empty Card</div></div>
      </body></html>"""
    check = canvas.diagnose(shell, "saved_dashboard.html")
    assert check["usable"] is False
    assert check["verdict"] == "javascript_shell", check
    assert "make_dummy_canvas.py" in check["advice"], "advice should say what to do"


def t_canvas_good_page_passes_diagnosis():
    name = _first_html_page()
    if name is None:
        return
    check = canvas.diagnose(canvas.read_source(name), name)
    assert check["usable"], f"{name}: {check['verdict']}"
    assert check["advice"] == "", "a good page needs no advice"


def t_canvas_json_export_loads():
    """A Canvas API export should parse with no model call at all."""
    import json
    import tempfile
    from pathlib import Path

    export = {
        "exported_at": "2026-09-19T19:40:00Z",
        "courses": [{
            "id": 382312,
            "course_code": "2271 CS 1684 SEC1010 BIAS & ETHICAL IMPLICTNS IN AI",
            "assignments": [
                {"id": 1, "name": "Final Project Proposal",
                 "due_at": "2026-10-03T03:59:00Z", "points_possible": 50,
                 "submission_types": ["online_upload"],
                 "workflow_state": "unsubmitted", "submitted": False, "score": None},
                {"id": 2, "name": "Reading Response 1",
                 "due_at": "2026-09-22T03:59:00Z", "points_possible": 10,
                 "submission_types": ["discussion_topic"],
                 "workflow_state": "graded", "submitted": True, "score": 9.5},
            ],
        }],
    }
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "canvas_export.json"
        path.write_text(json.dumps(export))
        data = canvas.load_export(path)

    assert data["courses"][0]["code"] == "CS 1684", data["courses"]
    assert data["courses"][0]["canvas_id"] == "382312"
    assert len(data["assignments"]) == 2

    by_title = {a["title"]: a for a in data["assignments"]}
    # 'Final Project Proposal' must not be filed as an exam.
    assert by_title["Final Project Proposal"]["kind"] == "project"
    assert by_title["Final Project Proposal"]["status"] == "open"
    assert by_title["Reading Response 1"]["kind"] == "reading"
    assert by_title["Reading Response 1"]["status"] == "graded"
    assert all(a["est_hours"] > 0 for a in data["assignments"])


def t_canvas_export_filters_stale_terms():
    """
    enrollment_state=active includes finished semesters. A real export had 358
    assignments across three terms plus a 2020 sandbox course with 159 of
    them; only 13 were current. Without filtering, "what's due" is swamped.
    """
    import json
    import tempfile
    from pathlib import Path

    export = {"courses": [
        {"id": 1, "course_code": "2271 CS 1675 SEC1100",
         "name": "2271 CS 1675 SEC1100 INTRO TO MACHINE LEARNING",
         "assignments": [{"id": 1, "name": "Homework 03",
                          "due_at": "2026-09-22T03:59:00Z", "points_possible": 40,
                          "submission_types": ["online_upload"],
                          "workflow_state": "unsubmitted"}]},
        {"id": 2, "course_code": "2261 CS 1501 SEC1065",
         "name": "2261 CS 1501 SEC1065 ALGORITHM IMPLEMENTATION",
         "assignments": [{"id": 2, "name": "Old Lab", "due_at": "2026-02-01T03:59:00Z",
                          "points_possible": 20, "submission_types": [],
                          "workflow_state": "graded", "score": 18}]},
        {"id": 3, "course_code": "Open Lab @ Canvas", "name": "Open Lab @ Canvas",
         "assignments": [{"id": 3, "name": "Sandbox item", "due_at": None,
                          "points_possible": 1, "submission_types": [],
                          "workflow_state": "unsubmitted"}]},
    ]}

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "canvas_export.json"
        path.write_text(json.dumps(export))
        data = canvas.load_export(path)

    assert data["term"] == "2271", data["term"]
    assert [c["code"] for c in data["courses"]] == ["CS 1675"], data["courses"]
    assert len(data["assignments"]) == 1
    assert data["assignments"][0]["title"] == "Homework 03"
    assert data["skipped_courses"] == 2, data["skipped_detail"]
    assert data["skipped_assignments"] == 2


def t_current_term_is_the_highest_code():
    """Pitt term codes increase over time, so max() keeps working next term."""
    courses = [
        {"course_code": "2261 CS 0447 SEC1070"},
        {"course_code": "2271 CS 1675 SEC1100"},
        {"course_code": "2264 CS 0449 SEC1200"},
        {"course_code": "Open Lab @ Canvas"},
    ]
    assert canvas.detect_current_term(courses) == "2271"
    assert canvas.detect_current_term([{"course_code": "Open Lab"}]) is None

    assert canvas._term_of("2271 CS 1675 SEC1100") == "2271"
    assert canvas._term_of("Open Lab @ Canvas") is None


def t_course_titles_are_readable():
    """
    Two bugs lived here: the SEC group required trailing whitespace, so a code
    ending in 'SEC1100' became the title; and the title was read from
    course_code, which has no title in it.
    """
    cases = {
        "2271 CS 1675 SEC1100 INTRO TO MACHINE LEARNING":
            ("CS 1675", "Intro to Machine Learning"),
        "2271 CS 1684 SEC1010 BIAS & ETHICAL IMPLICTNS IN AI":
            ("CS 1684", "Bias & Ethical Implications in AI"),
        "2271 MUSIC 0516 SEC1010 VIOLA": ("MUSIC 0516", "Viola"),
        # Section at the end of the string, no title at all.
        "2271 CS 1675 SEC1100": ("CS 1675", ""),
    }
    for raw, (want_code, want_title) in cases.items():
        code, title = canvas._clean_course_name(raw)
        assert code == want_code, f"{raw} -> code {code!r}"
        assert title == want_title, f"{raw} -> title {title!r}, wanted {want_title!r}"


def t_explicit_term_overrides_detection():
    import json
    import tempfile
    from pathlib import Path

    export = {"courses": [
        {"id": 1, "course_code": "2271 CS 1675 SEC1100", "name": "2271 CS 1675 SEC1100 ML",
         "assignments": [{"id": 1, "name": "New", "workflow_state": "unsubmitted"}]},
        {"id": 2, "course_code": "2261 CS 1501 SEC1065", "name": "2261 CS 1501 SEC1065 ALGO",
         "assignments": [{"id": 2, "name": "Old", "workflow_state": "unsubmitted"}]},
    ]}
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "e.json"
        path.write_text(json.dumps(export))
        data = canvas.load_export(path, term="2261")
    assert [c["code"] for c in data["courses"]] == ["CS 1501"], data["courses"]


def t_canvas_kind_classification():
    """The ordering of the hint list is load-bearing; pin it down."""
    expected = {
        "Final Project Proposal": "project",
        "Final Exam": "exam",
        "Midterm Exam": "exam",
        "Midterm Project proposal": "project",
        "Quiz 2: Fairness Metrics": "quiz",
        "Lab 3 Writeup": "lab",
        "Problem Set 4": "homework",
        "Case Study Paper 1": "project",
        "Reading Response 1": "reading",
        "Concert Performance": "project",
    }
    for title, kind in expected.items():
        got = canvas._guess_kind(title, [])
        assert got == kind, f"'{title}' -> {got}, expected {kind}"


def t_canvas_rejects_wrong_json():
    from pathlib import Path
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "nope.json"
        path.write_text('{"something": "else"}')
        try:
            canvas.load_export(path)
            raise AssertionError("should have raised")
        except ValueError as exc:
            assert "canvas_export.js" in str(exc), "error should say how to fix it"


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
    check("overdue tool", t_overdue_tool)
    check("add_to_schedule", t_add_to_schedule)
    check("scheduling tools distinguished", t_scheduling_tools_are_distinguished)
    check("db types json-safe", t_database_types_are_json_safe)
    check("no bare json.dumps", t_tool_results_serialize_even_if_db_bypassed)
    check("blocks hostile urls", t_webfetch_blocks_hostile_urls)
    check("blocks private addresses", t_webfetch_blocks_private_addresses)
    check("empty allowlist disables fetch", t_webfetch_empty_allowlist_disables_fetching)
    check("subdomain matching", t_webfetch_subdomain_matching)
    check("fetched text labelled untrusted", t_fetched_text_is_labelled_untrusted)
    check("system prompt warns of injection", t_system_prompt_warns_about_injection)
    check("localist parsing", t_localist_parsing)
    check("detects js shell pages", t_detects_javascript_shell_pages)
    check("plain text captures", t_plain_text_captures_recognised)
    check("rendered fetch validates url", t_rendered_fetch_still_validates_url)
    check("NAT config matches tool file", t_nat_config_matches_tool_file)
    check("database url validation", t_database_url_validation)
    check("placeholder creds flagged", t_placeholder_credentials_are_flagged)

    print("\norchestration loop")
    check("single tool then summary", t_single_tool_then_summary)
    check("message history well-formed", t_history_is_well_formed)
    check("stale -> refresh -> read chain", t_stale_data_triggers_refresh_chain)
    check("turn limit stops runaway", t_turn_limit_stops_runaway)
    check("tool failure survivable", t_tool_failure_does_not_kill_the_run)
    check("voice path constrained", t_voice_path_is_constrained)
    check("mock plans sensibly", t_mock_nemotron_plans_sensibly)
    check("unsupported param parsing", t_unsupported_param_parsing)
    check("unsupported params stripped", t_unsupported_params_are_stripped)
    check("thinking budget off by default", t_thinking_budget_off_by_default)

    print("\ndisplay contract")
    check("drops junk cards", t_display_drops_junk_cards)
    check("strips markup from speech", t_display_strips_markup_from_speech)
    check("caps card count", t_display_caps_card_count)
    check("never returns empty", t_display_never_returns_empty)
    check("all card types validate", t_all_card_types_validate)
    check("fallback builds real cards", t_display_fallback_builds_real_cards)
    check("fallback uses full result", t_display_fallback_uses_full_result_not_preview)
    check("fallback covers every tool", t_display_fallback_covers_every_tool)
    check("explains auth failure", t_display_explains_auth_failure)
    check("failed tools skipped", t_failed_tools_are_skipped_by_fallback)

    print("\ncanvas parser")
    check("finds pages", t_canvas_finds_pages)
    check("strips to clean text", t_canvas_strips_to_clean_text)
    check("all pages usable", t_canvas_all_pages_usable)
    check("detects javascript shell", t_canvas_detects_javascript_shell)
    check("good page passes diagnosis", t_canvas_good_page_passes_diagnosis)
    check("json export loads", t_canvas_json_export_loads)
    check("filters stale terms", t_canvas_export_filters_stale_terms)
    check("current term detection", t_current_term_is_the_highest_code)
    check("course titles readable", t_course_titles_are_readable)
    check("explicit term override", t_explicit_term_overrides_detection)
    check("kind classification", t_canvas_kind_classification)
    check("rejects wrong json", t_canvas_rejects_wrong_json)
    check("handles broken html", t_canvas_handles_broken_html)
    check("missing page error is clear", t_canvas_missing_page_is_clear)

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        for name, why in FAIL:
            print(f"  {name}: {why}")
        sys.exit(1)
