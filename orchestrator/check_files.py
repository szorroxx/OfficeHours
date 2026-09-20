"""
Check that every file in the project is the current version.

    python3 check_files.py

Handing files around one download at a time makes it easy to end up with a new
test file and an old module, which shows up as a confusing AttributeError
rather than "your file is stale". This looks for a marker in each file -- a
function or string that only exists in the current version -- and tells you
exactly which ones to re-download.

It only reads files. It changes nothing.
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).parent

# filename -> (markers that must be present, what that version added)
EXPECTED: dict[str, tuple[list[str], str]] = {
    "config.py": (
        ["def load(", "def status(", "ENV_PATH", "def _looks_real(",
         "def _describe(", "PLACEHOLDER"],
        "reads .env + flags placeholder credentials",
    ),
    "cache.py": (
        ["def wrap(", "def claude(", "class ReplayMiss", "def save_run("],
        "mock/live/replay modes",
    ),
    "nemotron_client.py": (
        ["UNSUPPORTED_PARAMS", "def _parse_unsupported(", "def _drop_unsupported(",
         "NEMOTRON_ALLOW_THINKING_BUDGET"],
        "handles the 400 Unsupported parameter error",
    ),
    "canvas.py": (
        ["def diagnose(", "def load_export(", "def _guess_kind(",
         "def _estimate_hours(", "final project", "def is_plain_text(",
         "def detect_current_term(", "skipped_assignments"],
        "detects JS shells + reads canvas_export.json",
    ),
    "tools.py": (
        ["BANNED_WORDS", "def is_banned(", "check_freshness",
         "get_workload_history", "def get_overdue(",
         "def find_campus_events(", "def fetch_page(",
         "def add_to_schedule("],
        "credential rejection + freshness + overdue tools",
    ),
    "db.py": (
        ["def record_workload_snapshot(", "def check_freshness(",
         "def get_workload_history(", "def upsert_assignments(",
         "def check_url(", "def get_overdue(", "def show_url(",
         "url is None", "def jsonable(", "def add_schedule_blocks("],
        "schema access + overdue query + URL validation",
    ),
    "display.py": (
        ["CARD_TYPES", "workload_chart", "def validate(", "def render_html(",
         "def fallback_spec(", "def _card_for(", "def _explain(",
         "find_campus_events"],
        "card contract + model-free fallback layout",
    ),
    "orchestrator.py": (
        ["MAX_TURNS", "use_recorded", "replayed", "def run(",
         "display_note", "fallback_spec", "fetch_page",
         "add_to_schedule", "default=str"],
        "tool loop + replay + non-fatal display failures",
    ),
    "server.py": (
        ["/health", "def workload(", "def crawl(", "config.status()"],
        "HTTP endpoints",
    ),
    "ask.py": (
        ["def main(", "run.replayed", "run.display_note"],
        "command-line runner",
    ),
    "test_loop.py": (
        ["t_unsupported_param_parsing", "t_canvas_json_export_loads",
         "t_canvas_detects_javascript_shell", "t_credentials_are_rejected",
         "t_overdue_tool", "t_nat_config_matches_tool_file",
         "t_database_url_validation", "t_display_fallback_builds_real_cards",
         "t_webfetch_blocks_private_addresses",
         "t_database_types_are_json_safe", "t_add_to_schedule",
         "t_canvas_export_filters_stale_terms"],
        "58 tests",
    ),
    "make_dummy_canvas.py": (
        ["def extract_env(", "SENSITIVE", "def filename_for("],
        "builds dummy pages from a saved Canvas page",
    ),
    "page_grab.js": (
        ["BOILERPLATE", "innerText", "Page grab", "CAPTURED"],
        "console script to capture any rendered page",
    ),
    "canvas_export.js": (
        ["SAFE_ASSIGNMENT_FIELDS", "planner", "canvas_export.json",
         "include[]=term"],
        "browser exporter, requests term data",
    ),
    "canvas_export_short.js": (
        ["canvas_export.json", "include[]=term"],
        "condensed console exporter",
    ),
    "tiger_data_tool.py": (
        ["tiger_data_overdue", "asyncio.to_thread", "import tools",
         "get_overdue"],
        "NAT tools that delegate to tools.py",
    ),
    "config.yml": (
        ["${NVIDIA_API_KEY}", "max_iterations", "tiger_data_overdue"],
        "NAT workflow, no inline secrets",
    ),
    "webfetch.py": (
        ["ALLOWED_DOMAINS", "def validate(", "FetchBlocked",
         "UNTRUSTED_HEADER", "_check_addresses",
         "def fetch_rendered(", "_looks_like_js_shell"],
        "guarded HTTP + JS rendering + SSRF checks",
    ),
    "schema.sql": (
        ["create_hypertable", "workload_snapshots", "study_sessions"],
        "tables + hypertables",
    ),
    "requirements.txt": (
        ["psycopg", "beautifulsoup4", "anthropic", "pyyaml"],
        "dependencies (ASCII-only, old pip chokes otherwise)",
    ),
    ".gitignore": ([".env", "cache/", "Dashboard.html"], "secret protection"),
}

OPTIONAL = {"canvas_export.js", "canvas_export_short.js",
            "make_dummy_canvas.py", "page_grab.js",
            "tiger_data_tool.py", "config.yml"}


def main() -> int:
    missing, stale, ok = [], [], []

    for name, (markers, purpose) in EXPECTED.items():
        path = HERE / name
        if not path.exists():
            (missing if name not in OPTIONAL else stale).append((name, purpose, "not found"))
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        absent = [m for m in markers if m not in text]
        if absent:
            stale.append((name, purpose, f"missing: {', '.join(absent[:3])}"))
        else:
            ok.append(name)

    print(f"checked {len(EXPECTED)} files in {HERE.name}/\n")

    if ok:
        print(f"current ({len(ok)}):")
        for name in sorted(ok):
            print(f"  ok       {name}")

    if stale:
        print(f"\nSTALE OR MISSING ({len(stale)}) — re-download these:")
        for name, purpose, why in stale:
            print(f"  STALE    {name}")
            print(f"           needs: {purpose}")
            print(f"           {why}")

    if missing:
        print(f"\nMISSING ({len(missing)}):")
        for name, purpose, _ in missing:
            print(f"  MISSING  {name}  ({purpose})")

    # A couple of things the markers can't catch.
    print()
    if not (HERE / ".env").exists():
        print("note: no .env file yet — run `cp .env.example .env`")
    else:
        try:
            sys.path.insert(0, str(HERE))
            import config

            st = config.status()
            expected = ("mode", "nvidia_key", "anthropic_key", "database_url")
            if not all(k in st for k in expected):
                print("!! config.py is an OLD version — re-download it.")
                print(f"   it reports: {sorted(st)}")
            else:
                print("settings:")
                for key in expected:
                    good = st[key] in ("set", "mock", "live", "replay")
                    print(f"  {'  ' if good else '!!'} {key:16} {st[key]}")
                if "PLACEHOLDER" in st["database_url"]:
                    print("     -> python3 db.py --url  for details")
        except Exception as exc:  # noqa: BLE001
            print(f"note: couldn't read .env ({exc})")
    pages = HERE / "canvas_pages"
    if not pages.exists() or not any(pages.iterdir()):
        print("note: canvas_pages/ is empty — nothing to crawl")
    else:
        html = len(list(pages.glob("*.html")))
        js = len(list(pages.glob("*.json")))
        print(f"canvas_pages/: {html} html page(s), {js} json export(s)")

    if stale or missing:
        print("\nAfter replacing files: python3 test_loop.py")
        return 1

    print("\nAll files current. Run: python3 test_loop.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
