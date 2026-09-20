"""
Loads your .env file. Imported first by every module that reads settings.

WHY THIS EXISTS
Python does not read .env files on its own. `os.getenv("MODE")` only sees
variables that are already in your shell's environment. Without this file you'd
have to type MODE=mock in front of every single command forever, and setting
MODE=live in .env would silently do nothing -- which is exactly the kind of bug
that wastes an hour.

So: this reads .env once, at startup, and puts everything in it into the
environment where os.getenv can find it.

IMPORTANT RULE: a real environment variable always wins over .env. So

    MODE=live python3 ask.py "..."

overrides whatever .env says, which is what you want for one-off runs.

No third-party library needed -- it's about 20 lines of standard Python.
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_PATH = Path(__file__).parent / ".env"
# The repo root's .env. This is now the canonical one: one file, one set of
# values, regardless of whether you're running app.py from the root or ask.py
# from inside orchestrator/.
ROOT_ENV_PATH = Path(__file__).parent.parent / ".env"
_loaded = False
_conflicts: dict[str, tuple[str, str]] = {}


def load_all() -> dict[str, str]:
    """
    Read the project's .env file(s).

    THE BUG THIS FIXES
    There used to be two .env files -- one at the repo root, one in
    orchestrator/ -- holding DIFFERENT values for some of the same keys (a
    different NVIDIA_API_KEY in each; only the root one set MODE and
    ANTHROPIC_API_KEY). Nothing read both, so which value applied depended on
    which script you happened to run: ask.py got one, the website got the
    other, and a key that "definitely works" would fail in one place and not
    the other.

    Now the root file is canonical and orchestrator/.env is read afterwards
    only to fill in keys the root file doesn't define -- so an existing local
    setup keeps working, but it can never silently override the root. Any key
    the two files disagree on is recorded and reported by status(), because a
    conflict you can't see is the whole problem.

    A real environment variable still wins over both, so
        MODE=live python3 app.py
    behaves as expected.
    """
    global _loaded
    found: dict[str, str] = {}

    root_values = load(ROOT_ENV_PATH) if ROOT_ENV_PATH.exists() else {}
    found.update(root_values)

    if ENV_PATH.exists():
        local_values = load(ENV_PATH)
        for key, value in local_values.items():
            if key in root_values and root_values[key] != value:
                _conflicts[key] = (root_values[key], value)
            found.setdefault(key, value)

    _loaded = True
    return found


def load(path: Path | None = None, override: bool = False) -> dict[str, str]:
    """Read one .env file and add its values to os.environ."""
    global _loaded
    path = path or ENV_PATH
    found: dict[str, str] = {}

    if not path.exists():
        _loaded = True
        return found

    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()

        # Skip blank lines and comments.
        if not line or line.startswith("#"):
            continue

        # Skip anything that isn't KEY=VALUE.
        if "=" not in line:
            continue

        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()

        # Drop a trailing comment, but not a '#' inside quotes.
        if not (value.startswith(("'", '"'))) and " #" in value:
            value = value.split(" #", 1)[0].strip()

        # Strip surrounding quotes if present.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]

        if not key:
            continue

        found[key] = value

        # A variable already in the real environment wins, unless told otherwise.
        if override or key not in os.environ:
            os.environ[key] = value

    _loaded = True
    return found


def _looks_real(value: str, prefix: str = "") -> bool:
    """
    A value that's present but still the .env.example placeholder is worse than
    a missing one -- it looks configured and then fails confusingly. So check
    for the template text, not just for non-emptiness.
    """
    if not value:
        return False
    lowered = value.lower()
    if any(m in lowered for m in
           ("replace_me", "replaceme", "user:pass@", "@host:", "changeme",
            "your-host", "<host>")):
        return False
    return value.startswith(prefix) if prefix else True


def status() -> dict:
    """Used by /health and the CLI tools to show what got loaded."""
    nvidia = os.getenv("NVIDIA_API_KEY", "")
    anthropic = os.getenv("ANTHROPIC_API_KEY", "")
    database = os.getenv("DATABASE_URL", "")
    out = {
        "env_file": str(ROOT_ENV_PATH),
        "env_file_exists": ROOT_ENV_PATH.exists(),
        "also_read": str(ENV_PATH) if ENV_PATH.exists() else None,
        "mode": os.getenv("MODE", "mock"),
        "nvidia_key": _describe(nvidia, "nvapi-"),
        "anthropic_key": _describe(anthropic, "sk-ant"),
        "database_url": _describe(database, "postgres"),
    }
    if _conflicts:
        # Named, not swallowed: these are the keys where the two .env files
        # disagree. The root file's value is the one in effect.
        out["env_conflicts"] = {
            key: "root .env wins; orchestrator/.env has a different value"
            for key in sorted(_conflicts)
        }
    return out


def _describe(value: str, prefix: str) -> str:
    if not value:
        return "missing"
    if not _looks_real(value, prefix):
        return "PLACEHOLDER — still the .env.example value"
    return "set"


# Runs the moment anything imports this module.
if not _loaded:
    load_all()


if __name__ == "__main__":
    import json

    values = load_all()
    print(json.dumps(status(), indent=2))
    print(f"\nkeys found in .env: {sorted(values) or '(none — does .env exist?)'}")
    if not ROOT_ENV_PATH.exists() and not ENV_PATH.exists():
        print(f"\nNo .env file at {ROOT_ENV_PATH}")
        print("Create one with:  cp .env.example .env")
