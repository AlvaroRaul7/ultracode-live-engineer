"""Slack channel/thread scanning: find PR-review candidates, with a
two-layer cache (terminal PR outcomes, thread-scan verdicts) to avoid
re-fetching/re-scanning settled state every pass — this file is the
mechanical-rule half of the ultracode-live-engineer workflow's rule
enforcement; lib/github.py is the other half.

These are mechanical checks that must NOT be left to model judgment — each
one exists because a prior pass got a rule wrong by eyeballing text instead
of matching it exactly. See the auto-memory entries:
  - ultracode-verify-mention-before-review (reviewed a PR with no actual
    @-mention of the human in it — a plain text-matching miss)
  - ultracode-skip-merged-prs (reviewed PRs that had already merged)"""
import json
import re
import sys
from datetime import datetime, timezone

from .cache import _cache_lock, _load_cache, _save_cache, thread_unchanged
from .config import CONFIG

# Exact-token match only — this is the whole point. "mentions the human" means
# the literal Slack mention tag appears, not "this channel is mostly about
# them" or any other inference.
MENTION_RE = re.compile(re.escape(f"<@{CONFIG['human_slack_id']}"))
MSG_HEADER_RE = re.compile(
    r"^=== Message from .+? \((?P<uid>\S+?)\) at (?P<ts_human>.+?) ===\s*$",
    re.MULTILINE,
)
MSG_TS_RE = re.compile(r"^Message TS: (?P<ts>[\d.]+)\s*$", re.MULTILINE)
PR_LINK_RE = re.compile(r"pull/(\d+)")
THREAD_INFO_RE = re.compile(r"Thread: (?P<count>\d+) repl\w+ \(latest: (?P<latest>[^)]+)\)")


def _normalize_raw(raw: str) -> str:
    """Undo the JSON-escaping a Slack tool result gets when it overflows to a file.

    When slack_read_channel/slack_read_thread's output exceeds the token
    limit, the harness saves the RAW (still JSON-encoded) tool result to a
    file instead of returning decoded text — e.g.
    ``{"messages": "Channel: ...\\n\\n=== Message from ... ===..."}`` with
    literal ``\\n``/``\\/``/``\\uXXXX`` escapes, not real newlines or slashes.
    Piped straight into this script, every regex here silently matches
    nothing (no real newlines means MSG_HEADER_RE's ``^...$`` anchors never
    fire) — confirmed live: a PR announcement that plainly mentioned the
    human and linked a PR was missed this way because the channel was busy
    enough to overflow.  Detect that shape and unwrap it back to plain text before
    parsing; anything that isn't this exact shape (the normal, un-truncated
    case) passes through unchanged.
    """
    stripped = raw.strip()
    if not stripped.startswith("{"):
        return raw
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return raw
    if not isinstance(parsed, dict):
        return raw
    for key in ("messages", "text", "content"):
        value = parsed.get(key)
        if isinstance(value, str):
            return value
    return raw


def split_blocks(raw: str):
    raw = _normalize_raw(raw)
    headers = list(MSG_HEADER_RE.finditer(raw))
    blocks = []
    for i, m in enumerate(headers):
        start = m.start()
        end = headers[i + 1].start() if i + 1 < len(headers) else len(raw)
        body = raw[start:end]
        ts_match = MSG_TS_RE.search(body)
        thread_match = THREAD_INFO_RE.search(body)
        blocks.append(
            {
                "author_id": m.group("uid"),
                "ts_human": m.group("ts_human"),
                "message_ts": ts_match.group("ts") if ts_match else None,
                "text": body,
                "mentions_human": bool(MENTION_RE.search(body)),
                "pr_numbers": sorted({int(n) for n in PR_LINK_RE.findall(body)}),
                "thread_reply_count": int(thread_match.group("count")) if thread_match else None,
                "thread_latest": thread_match.group("latest") if thread_match else None,
            }
        )
    return blocks


def cmd_scan_channel():
    """Find candidate PR-review threads in a slack_read_channel dump.

    A message is a DEFINITE candidate only if the exact mention token and a
    PR link both appear in the same top-level message. If only one half is
    present but the message has a thread, it goes to needs_thread_check —
    never assume the other half is there; fetch the thread and run
    scan-thread on it instead.

    Cost optimization (kept out of the correctness rules above): a candidate
    whose PR number(s) are ALL already in the terminal-outcome cache (see
    _load_cache) is dropped here, before scan-thread ever gets dispatched for
    it — on a settled backlog this is what actually saves the cost, since
    re-running gh-filter alone doesn't stop the (much more expensive)
    per-thread agent dispatches scan-channel's own output triggers.

    Second cost optimization, distinct from the PR-number cache above: a
    thread only earns a "needs_thread_check" (Slack thread fetch + agent
    dispatch) if its content has actually changed since the last pass. Every
    pass was previously re-fetching and re-scanning the SAME threads forever
    whenever the PR link/mention only lives in a reply, not the root message
    — the PR-number cache can't help there because scan-thread hasn't run
    yet to even know what PR numbers are in play. scanned_threads (keyed by
    thread_ts) records the (reply_count, latest_reply) fingerprint and
    verdict from the last scan-thread run (see record-thread-scan). If the
    fingerprint is unchanged: a thread previously found NOT a candidate is
    dropped entirely (nothing new to find — a stale verdict here is safe
    because "unchanged fingerprint" means no new reply could have added the
    missing mention/PR link); a thread previously found a candidate is
    promoted straight into definite_candidates using its cached pr_numbers,
    skipping the re-fetch. Only a genuinely new/changed thread pays for a
    live scan-thread dispatch.
    """
    raw = sys.stdin.read()
    blocks = split_blocks(raw)
    cache = _load_cache()
    handled = cache["handled_prs"]
    scanned_threads = cache["scanned_threads"]

    def _all_handled(pr_numbers):
        return bool(pr_numbers) and all(str(n) in handled for n in pr_numbers)

    definite = []
    needs_thread_check = []
    skipped_already_handled = []
    skipped_unchanged_thread = []
    for b in blocks:
        has_mention = b["mentions_human"]
        has_pr = bool(b["pr_numbers"])
        has_thread = "Thread:" in b["text"] and "replies" in b["text"]
        if has_mention and has_pr:
            if _all_handled(b["pr_numbers"]):
                skipped_already_handled.append(
                    {"message_ts": b["message_ts"], "pr_numbers": b["pr_numbers"]}
                )
                continue
            definite.append(
                {
                    "message_ts": b["message_ts"],
                    "pr_numbers": b["pr_numbers"],
                    "reason": "mention+PR link in same top-level message",
                }
            )
        elif has_thread and (has_mention or has_pr):
            if _all_handled(b["pr_numbers"]):
                skipped_already_handled.append(
                    {"message_ts": b["message_ts"], "pr_numbers": b["pr_numbers"]}
                )
                continue
            prior = thread_unchanged(
                scanned_threads, b["message_ts"], b["thread_reply_count"], b["thread_latest"]
            )
            if prior is not None:
                if prior.get("is_candidate"):
                    definite.append(
                        {
                            "message_ts": b["message_ts"],
                            "pr_numbers": prior.get("pr_numbers", []),
                            "reason": "cached scan-thread verdict, thread unchanged since last pass",
                        }
                    )
                else:
                    skipped_unchanged_thread.append(
                        {"message_ts": b["message_ts"], "reason": "not a candidate, thread unchanged since last pass"}
                    )
                continue
            needs_thread_check.append(
                {
                    "message_ts": b["message_ts"],
                    "has_mention_in_root": has_mention,
                    "pr_numbers_in_root": b["pr_numbers"],
                    "thread_reply_count": b["thread_reply_count"],
                    "thread_latest": b["thread_latest"],
                }
            )
    return {
        "definite_candidates": definite,
        "needs_thread_check": needs_thread_check,
        "skipped_already_handled": skipped_already_handled,
        "skipped_unchanged_thread": skipped_unchanged_thread,
    }


def cmd_scan_thread():
    """Decide whether a slack_read_thread dump (parent + replies) is a
    review candidate: the exact mention token and a PR link must both
    appear somewhere in the thread (either message).

    NOTE: slack_read_thread's output uses a different per-message header
    format ("=== THREAD PARENT MESSAGE ===" / "From: ... (uid)\\nTime: ...")
    than slack_read_channel's ("=== Message from Name <email> (uid) at ...
    ==="), so this does NOT reuse split_blocks (which is channel-format-
    specific and would silently parse zero blocks here, previously a real
    bug — see MSG-format regression note in the commit that added this
    comment). Instead it searches the whole thread text directly: the
    mention token and PR link regex both match literally in either format,
    and the thread's own "Message TS:" lines appear in both formats too —
    the parent's is always first. Also passes through _normalize_raw: an
    overflowed slack_read_thread result hits the same JSON-escaped-file shape
    as slack_read_channel's (see _normalize_raw), and the escaped "\\/" alone
    breaks PR_LINK_RE here even though this function doesn't rely on real
    newlines the way split_blocks does."""
    raw = _normalize_raw(sys.stdin.read())
    mentions_anywhere = bool(MENTION_RE.search(raw))
    prs = sorted({int(n) for n in PR_LINK_RE.findall(raw)})
    ts_matches = MSG_TS_RE.findall(raw)
    thread_ts = ts_matches[0] if ts_matches else None
    return {
        "is_candidate": mentions_anywhere and bool(prs),
        "thread_ts": thread_ts,
        "pr_numbers": prs,
        "mentions_human": mentions_anywhere,
    }


def cmd_record_thread_scan(thread_ts: str, reply_count: str, latest: str, is_candidate: str, pr_numbers_json: str):
    """Persist a live scan-thread verdict into the thread-level cache, keyed
    by the (reply_count, latest_reply) fingerprint scan-channel saw for this
    thread at dispatch time. Call this once, immediately after every live
    scan-thread run — it's what lets a future scan-channel pass recognize
    "this exact thread, unchanged" and skip re-dispatching it (see
    cmd_scan_channel's skipped_unchanged_thread docstring). reply_count/latest
    are the SAME values scan-channel put on the needs_thread_check item that
    triggered this scan-thread call, not re-derived here — the thread dump
    format scan-thread reads has no reply-count summary line of its own."""
    try:
        pr_numbers = json.loads(pr_numbers_json)
    except json.JSONDecodeError as exc:
        print(json.dumps({"error": f"bad pr_numbers list: {exc}"}))
        sys.exit(1)
    try:
        reply_count_int = int(reply_count)
    except ValueError:
        return {"recorded": False, "reason": f"non-numeric reply_count: {reply_count!r}"}
    with _cache_lock():
        cache = _load_cache()
        cache["scanned_threads"][thread_ts] = {
            "reply_count": reply_count_int,
            "latest": latest,
            "is_candidate": is_candidate.strip().lower() in ("true", "1", "yes"),
            "pr_numbers": pr_numbers,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
        _save_cache(cache)
    return {"recorded": True, "thread_ts": thread_ts}
