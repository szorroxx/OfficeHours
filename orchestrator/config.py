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
_loaded = False


def load(path: Path | None = None, override: bool = False) -> dict[str, str]:
    """Read a .env file and add its values to os.environ."""
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
    return {
        "env_file": str(ENV_PATH),
        "env_file_exists": ENV_PATH.exists(),
        "mode": os.getenv("MODE", "mock"),
        "nvidia_key": _describe(nvidia, "nvapi-"),
        "anthropic_key": _describe(anthropic, "sk-ant"),
        "database_url": _describe(database, "postgres"),
    }


def _describe(value: str, prefix: str) -> str:
    if not value:
        return "missing"
    if not _looks_real(value, prefix):
        return "PLACEHOLDER — still the .env.example value"
    return "set"


# Runs the moment anything imports this module.
if not _loaded:
    load()


if __name__ == "__main__":
    import json

    values = load()
    print(json.dumps(status(), indent=2))
    print(f"\nkeys found in .env: {sorted(values) or '(none — does .env exist?)'}")
    if not ENV_PATH.exists():
        print(f"\nNo .env file at {ENV_PATH}")
        print("Create one with:  cp .env.example .env")
