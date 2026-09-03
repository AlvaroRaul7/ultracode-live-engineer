"""Persistent local cache: terminal PR outcomes (never reverts, safe to
cache forever) and thread-scan verdicts (expire — see THREAD_CACHE_TTL_HOURS).
NOT for correctness — every mutating decision still goes through a live `gh`
call the first time — only for cost: re-fetching a settled fact every idle
pass forever is pure waste."""
import fcntl
import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone

from .config import CONFIG

CACHE_PATH = os.path.join(CONFIG["cache_dir"], "ultracode-cache.json")

# A cached thread verdict — candidate or not — must expire: Slack's "N replies (latest: ...)"
# fingerprint tracks new replies, not edits to EXISTING replies, so someone editing a reply to add
# a previously-missing mention/PR link (on a not-yet-candidate thread) or an ADDITIONAL PR link (on
# an already-candidate thread whose cached pr_numbers would otherwise be reused forever) would
# never change the fingerprint. gh-filter only re-verifies the OPEN/authored/reviewed status of the
# PR numbers a candidate verdict already names — it never re-derives the PR set from thread
# content, so a cached candidate's pr_numbers is just as exposed to this staleness as a cached
# negative is. Applying the same TTL to both closes that gap uniformly instead of leaving it open
# on one branch.
THREAD_CACHE_TTL_HOURS = 24


def _load_cache() -> dict:
    try:
        with open(CACHE_PATH) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"handled_prs": {}, "scanned_threads": {}}
    if not isinstance(data.get("handled_prs"), dict):
        data["handled_prs"] = {}
    if not isinstance(data.get("scanned_threads"), dict):
        data["scanned_threads"] = {}
    return data


def _save_cache(cache: dict) -> None:
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    tmp = CACHE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cache, f, indent=2)
    os.replace(tmp, CACHE_PATH)


@contextmanager
def _cache_lock():
    """Exclusive-lock a dedicated lock file for the duration of a load-modify-save cycle on the
    cache. record-thread-scan is invoked once per thread by threadChecks' pipeline(), which runs
    those agents concurrently — without this, two concurrent record-thread-scan processes can both
    _load_cache() the same base state and the second _save_cache() silently clobbers the first's
    entry. A separate lock file (not the cache file itself) because _save_cache's os.replace swaps
    the cache path to a new inode, which would orphan a lock held on the old fd."""
    lock_path = CACHE_PATH + ".lock"
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    with open(lock_path, "a") as lockfile:
        fcntl.flock(lockfile, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lockfile, fcntl.LOCK_UN)


def thread_unchanged(scanned_threads: dict, message_ts: str, reply_count, latest):
    """Return the cached verdict for message_ts if its (reply_count, latest) fingerprint matches
    AND the verdict hasn't expired (see THREAD_CACHE_TTL_HOURS); otherwise None (caller must
    re-scan live)."""
    prior = scanned_threads.get(message_ts)
    if not prior or reply_count is None or latest is None:
        return None
    if prior.get("reply_count") != reply_count or prior.get("latest") != latest:
        return None
    recorded_at = prior.get("recorded_at")
    if not recorded_at:
        return None  # pre-TTL cache entry from before this field existed
    try:
        age_hours = (
            datetime.now(timezone.utc) - datetime.fromisoformat(recorded_at)
        ).total_seconds() / 3600
    except (ValueError, TypeError):
        return None
    if age_hours >= THREAD_CACHE_TTL_HOURS:
        return None
    return prior
