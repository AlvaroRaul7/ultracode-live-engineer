"""Loads config.json (see ../CONFIG.md). Computed once per process — every
other lib/ module imports CONFIG from here rather than re-reading the file.

As a plugin, this whole workflows/ directory is installed once, shared
across every project the adopter runs the loop against — it is NOT checked
into the target repo the way a repo-local copy of this workflow would be.
So config.json can't live next to this file (a path derived from
__file__ would resolve to the same shared plugin install for every
project). Instead it lives in the TARGET project repo, at
.claude/ultracode-live-engineer/config.json, and this module resolves it
relative to the current working directory — pass_rules.py is always
invoked "from the repo root" (see the workflow's own agent prompts), so cwd
is the correct project at runtime. ULE_CONFIG_PATH overrides this (tests,
or an adopter who wants a non-default location)."""
import json
import os
from pathlib import Path

CONFIG_PATH = Path(
    os.environ.get("ULE_CONFIG_PATH")
    or (Path.cwd() / ".claude" / "ultracode-live-engineer" / "config.json")
)


def _derive_cache_dir(repo: str) -> str:
    """Default cache location when config.json omits cache_dir: independent
    of Claude Code's own internal project-hash directory naming (which is
    what the pre-config-driven CACHE_PATH used to be hardcoded to) — another
    adopter gets their own cache directory for free just by having a
    different `repo` value."""
    return os.path.expanduser(f"~/.claude/ultracode-live-engineer/{repo.replace('/', '-')}")


def load_config(path=CONFIG_PATH) -> dict:
    with open(path) as f:
        data = json.load(f)
    data["cache_dir"] = (
        os.path.expanduser(data["cache_dir"]) if data.get("cache_dir") else _derive_cache_dir(data["repo"])
    )
    return data


CONFIG = load_config()
