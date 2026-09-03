import io
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

from lib import slack_scan
from lib.config import CONFIG
from tests.helpers import seed_cache


def _thread_cache_entry(
    is_candidate=False,
    pr_numbers=None,
    recorded_at=None,
    reply_count=3,
    latest="2026-01-02 09:00:00",
):
    """Build a single scanned_threads cache entry matching the shape written
    by pass_rules' thread-verdict cache."""
    return {
        "reply_count": reply_count,
        "latest": latest,
        "is_candidate": is_candidate,
        "pr_numbers": pr_numbers or [],
        "recorded_at": recorded_at or datetime.now(timezone.utc).isoformat(),
    }


def _message_block(
    author="Alice",
    uid="U999",
    ts_human="2026-01-01 10:00:00",
    message_ts="1000.001",
    mention=True,
    pr_number=42,
    thread=None,
):
    """Build a single === Message from ... === block matching MSG_HEADER_RE /
    MSG_TS_RE / THREAD_INFO_RE."""
    lines = [f"=== Message from {author} ({uid}) at {ts_human} ==="]
    lines.append(f"Message TS: {message_ts}")
    body = "please review"
    if mention:
        body = f"<@{CONFIG['human_slack_id']}> {body}"
    if pr_number is not None:
        body = f"{body} https://github.com/x/y/pull/{pr_number}"
    lines.append(body)
    if thread is not None:
        reply_count, latest = thread
        lines.append(f"Thread: {reply_count} replies (latest: {latest})")
    return "\n".join(lines) + "\n"


def _thread_text(mention=True, pr_number=42, message_ts="1000.001"):
    """Build a slack_read_thread-shaped text block (parent message header,
    NOT the channel format) with an optional mention token and PR link."""
    lines = ["=== THREAD PARENT MESSAGE ==="]
    lines.append("From: Alice (U999)")
    lines.append("Time: 2026-01-01 10:00:00")
    lines.append(f"Message TS: {message_ts}")
    body = "please take a look"
    if mention:
        body = f"<@{CONFIG['human_slack_id']}> {body}"
    if pr_number is not None:
        body = f"{body} https://github.com/x/y/pull/{pr_number}"
    lines.append(body)
    return "\n".join(lines) + "\n"


class TestNormalizeRaw:
    def test_plain_text_passes_through_unchanged(self):
        raw = "=== Message from Alice (U999) at 2026-01-01 10:00:00 ===\nMessage TS: 1000.001\nhello"
        assert slack_scan._normalize_raw(raw) == raw

    def test_unwraps_json_escaped_overflow_shape(self):
        real_text = (
            "=== Message from Alice (U999) at 2026-01-01 10:00:00 ===\n"
            "Message TS: 1000.001\nhello there"
        )
        raw = json.dumps({"messages": real_text})
        result = slack_scan._normalize_raw(raw)
        assert result == real_text
        assert "\\n" not in result
        assert "\n" in result


def test_split_blocks_extracts_mention_pr_and_thread_fields():
    text = _message_block(
        ts_human="2026-01-01 10:00:00",
        message_ts="1000.001",
        mention=True,
        pr_number=42,
        thread=(3, "2026-01-02 09:00:00"),
    )
    blocks = slack_scan.split_blocks(text)
    assert len(blocks) == 1
    block = blocks[0]
    assert block["message_ts"] == "1000.001"
    assert block["mentions_human"] is True
    assert block["pr_numbers"] == [42]
    assert block["thread_reply_count"] == 3
    assert block["thread_latest"] == "2026-01-02 09:00:00"


def test_scan_channel_mention_and_pr_in_same_message_is_definite_candidate(cache_path, monkeypatch):
    text = _message_block(message_ts="1000.001", mention=True, pr_number=42, thread=None)
    monkeypatch.setattr(sys, "stdin", io.StringIO(text))

    result = slack_scan.cmd_scan_channel()

    assert len(result["definite_candidates"]) == 1
    candidate = result["definite_candidates"][0]
    assert {k: v for k, v in candidate.items() if k != "reason"} == {
        "message_ts": "1000.001",
        "pr_numbers": [42],
    }
    assert "same top-level message" in candidate["reason"]
    assert result["needs_thread_check"] == []


def test_scan_channel_pr_only_with_thread_needs_thread_check(cache_path, monkeypatch):
    text = _message_block(
        message_ts="1000.001",
        mention=False,
        pr_number=42,
        thread=(3, "2026-01-02 09:00:00"),
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO(text))

    result = slack_scan.cmd_scan_channel()

    assert result["definite_candidates"] == []
    assert result["needs_thread_check"] == [
        {
            "message_ts": "1000.001",
            "has_mention_in_root": False,
            "pr_numbers_in_root": [42],
            "thread_reply_count": 3,
            "thread_latest": "2026-01-02 09:00:00",
        }
    ]


def test_scan_channel_skips_prs_already_in_handled_cache(cache_path, monkeypatch):
    text = _message_block(message_ts="1000.001", mention=True, pr_number=42, thread=None)
    monkeypatch.setattr(sys, "stdin", io.StringIO(text))
    seed_cache(
        cache_path,
        {"handled_prs": {"42": "state=MERGED (not OPEN)"}, "scanned_threads": {}},
    )

    result = slack_scan.cmd_scan_channel()

    assert result["definite_candidates"] == []
    assert result["needs_thread_check"] == []
    assert result["skipped_already_handled"] == [
        {"message_ts": "1000.001", "pr_numbers": [42]}
    ]


def test_scan_channel_unchanged_thread_verdict_not_candidate_is_skipped(cache_path, monkeypatch):
    text = _message_block(
        message_ts="1000.001",
        mention=False,
        pr_number=42,
        thread=(3, "2026-01-02 09:00:00"),
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO(text))
    seed_cache(
        cache_path,
        {
            "handled_prs": {},
            "scanned_threads": {"1000.001": _thread_cache_entry(is_candidate=False)},
        },
    )

    result = slack_scan.cmd_scan_channel()

    assert result["needs_thread_check"] == []
    assert result["definite_candidates"] == []
    assert len(result["skipped_unchanged_thread"]) == 1
    skipped = result["skipped_unchanged_thread"][0]
    assert {k: v for k, v in skipped.items() if k != "reason"} == {
        "message_ts": "1000.001",
    }
    assert "not a candidate" in skipped["reason"]


def test_scan_channel_unchanged_thread_verdict_candidate_is_promoted(cache_path, monkeypatch):
    text = _message_block(
        message_ts="1000.001",
        mention=False,
        pr_number=42,
        thread=(3, "2026-01-02 09:00:00"),
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO(text))
    seed_cache(
        cache_path,
        {
            "handled_prs": {},
            "scanned_threads": {
                "1000.001": _thread_cache_entry(is_candidate=True, pr_numbers=[99])
            },
        },
    )

    result = slack_scan.cmd_scan_channel()

    assert result["needs_thread_check"] == []
    assert result["skipped_unchanged_thread"] == []
    assert len(result["definite_candidates"]) == 1
    candidate = result["definite_candidates"][0]
    assert {k: v for k, v in candidate.items() if k != "reason"} == {
        "message_ts": "1000.001",
        "pr_numbers": [99],
    }
    assert "cached scan-thread verdict" in candidate["reason"]


def test_scan_channel_stale_cached_thread_verdict_is_rescanned(cache_path, monkeypatch):
    text = _message_block(
        message_ts="1000.001",
        mention=False,
        pr_number=42,
        thread=(3, "2026-01-02 09:00:00"),
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO(text))
    stale_recorded_at = (
        datetime.now(timezone.utc) - timedelta(hours=25)
    ).isoformat()
    seed_cache(
        cache_path,
        {
            "handled_prs": {},
            "scanned_threads": {
                "1000.001": _thread_cache_entry(recorded_at=stale_recorded_at)
            },
        },
    )

    result = slack_scan.cmd_scan_channel()

    assert result["skipped_unchanged_thread"] == []
    assert result["needs_thread_check"] == [
        {
            "message_ts": "1000.001",
            "has_mention_in_root": False,
            "pr_numbers_in_root": [42],
            "thread_reply_count": 3,
            "thread_latest": "2026-01-02 09:00:00",
        }
    ]


def test_scan_channel_changed_reply_count_invalidates_cached_thread_verdict(cache_path, monkeypatch):
    text = _message_block(
        message_ts="1000.001",
        mention=False,
        pr_number=42,
        thread=(4, "2026-01-02 09:00:00"),
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO(text))
    seed_cache(
        cache_path,
        {
            "handled_prs": {},
            "scanned_threads": {"1000.001": _thread_cache_entry()},
        },
    )

    result = slack_scan.cmd_scan_channel()

    assert result["skipped_unchanged_thread"] == []
    assert result["needs_thread_check"] == [
        {
            "message_ts": "1000.001",
            "has_mention_in_root": False,
            "pr_numbers_in_root": [42],
            "thread_reply_count": 4,
            "thread_latest": "2026-01-02 09:00:00",
        }
    ]


class TestCmdScanThread:
    def test_mention_and_pr_link_is_candidate(self, monkeypatch):
        text = _thread_text(mention=True, pr_number=42, message_ts="1000.001")
        monkeypatch.setattr(sys, "stdin", io.StringIO(text))

        result = slack_scan.cmd_scan_thread()

        assert result["is_candidate"] is True
        assert result["pr_numbers"] == [42]
        assert result["thread_ts"] == "1000.001"

    def test_pr_link_without_mention_is_not_candidate(self, monkeypatch):
        text = _thread_text(mention=False, pr_number=42, message_ts="1000.001")
        monkeypatch.setattr(sys, "stdin", io.StringIO(text))

        result = slack_scan.cmd_scan_thread()

        assert result["is_candidate"] is False
        assert result["pr_numbers"] == [42]

    def test_mention_without_pr_link_is_not_candidate(self, monkeypatch):
        text = _thread_text(mention=True, pr_number=None, message_ts="1000.001")
        monkeypatch.setattr(sys, "stdin", io.StringIO(text))

        result = slack_scan.cmd_scan_thread()

        assert result["is_candidate"] is False
        assert result["mentions_human"] is True
        assert result["pr_numbers"] == []

    def test_detects_candidate_inside_json_escaped_overflow_shape(self, monkeypatch):
        real_text = _thread_text(mention=True, pr_number=42, message_ts="1000.001")
        raw = json.dumps({"messages": real_text})
        monkeypatch.setattr(sys, "stdin", io.StringIO(raw))

        result = slack_scan.cmd_scan_thread()

        assert result["is_candidate"] is True
        assert result["pr_numbers"] == [42]
        assert result["thread_ts"] == "1000.001"


class TestCmdRecordThreadScan:
    @pytest.mark.parametrize(
        "is_candidate_str, expected",
        [
            ("true", True),
            ("false", False),
            ("False", False),
        ],
    )
    def test_records_verdict_to_cache(self, cache_path, is_candidate_str, expected):
        result = slack_scan.cmd_record_thread_scan(
            "1000.001", "3", "2026-01-02 09:00:00", is_candidate_str, "[42]"
        )

        assert result == {"recorded": True, "thread_ts": "1000.001"}
        with open(cache_path) as f:
            cache = json.load(f)
        entry = cache["scanned_threads"]["1000.001"]
        assert entry["reply_count"] == 3
        assert isinstance(entry["reply_count"], int)
        assert entry["is_candidate"] is expected
        assert entry["pr_numbers"] == [42]
        # recorded_at must be a valid ISO datetime string
        datetime.fromisoformat(entry["recorded_at"])

    def test_non_numeric_reply_count_does_not_write_cache(self, cache_path):
        result = slack_scan.cmd_record_thread_scan(
            "1000.001", "null", "2026-01-02 09:00:00", "true", "[42]"
        )

        assert result == {
            "recorded": False,
            "reason": "non-numeric reply_count: 'null'",
        }
        assert not os.path.exists(cache_path)

    def test_invalid_pr_numbers_json_exits(self, cache_path):
        with pytest.raises(SystemExit):
            slack_scan.cmd_record_thread_scan(
                "1000.001", "3", "2026-01-02 09:00:00", "true", "not json"
            )


def _reply_block(body, message_ts, author="Bob", uid="U222", ts_human="2026-01-01 10:05:00"):
    """Build a single thread-reply block, chronologically after the parent
    built by _thread_text — position in the concatenated string is the only
    ordering signal cmd_check_hold relies on, matching real slack_read_thread
    dumps (replies appear after the parent, in posting order)."""
    lines = [
        "=== REPLY ===",
        f"From: {author} ({uid})",
        f"Time: {ts_human}",
        f"Message TS: {message_ts}",
        body,
    ]
    return "\n".join(lines) + "\n"


class TestCmdCheckHold:
    def test_no_signal_not_held(self, monkeypatch):
        text = _thread_text(mention=True, pr_number=42)
        monkeypatch.setattr(sys, "stdin", io.StringIO(text))

        result = slack_scan.cmd_check_hold()

        assert result == {"held": False, "matched_phrase": None, "reason": None}

    def test_fresh_objection_after_bot_review_is_held(self, monkeypatch):
        text = _thread_text(mention=True, pr_number=42, message_ts="1000.001")
        text += _reply_block("Reviewed and approved — only nits, see pull/42", "1000.002")
        text += _reply_block(
            "don't merge it. I have objections. I'm still in review", "1000.003"
        )
        monkeypatch.setattr(sys, "stdin", io.StringIO(text))

        result = slack_scan.cmd_check_hold()

        assert result["held"] is True
        assert result["reason"] == "objection"
        assert result["matched_phrase"] is not None

    def test_bot_review_after_old_objection_is_not_held(self, monkeypatch):
        text = _thread_text(mention=True, pr_number=42, message_ts="1000.001")
        text += _reply_block("hold off for now, still checking", "1000.002")
        text += _reply_block("Reviewed — 2 findings posted, see pull/42", "1000.003")
        monkeypatch.setattr(sys, "stdin", io.StringIO(text))

        result = slack_scan.cmd_check_hold()

        assert result == {"held": False, "matched_phrase": None, "reason": None}

    def test_hold_ack_with_no_reply_since_stays_held(self, monkeypatch):
        text = _thread_text(mention=True, pr_number=42, message_ts="1000.001")
        text += _reply_block("don't merge, objections open", "1000.002")
        text += _reply_block(
            "Holding off on re-reviewing — noted the concern above. Reply here "
            "once it's cleared and this will pick back up next pass.",
            "1000.003",
        )
        monkeypatch.setattr(sys, "stdin", io.StringIO(text))

        result = slack_scan.cmd_check_hold()

        assert result["held"] is True
        assert result["reason"] == "awaiting reply since last hold notice"
        assert result["matched_phrase"] is None

    def test_any_reply_after_hold_ack_clears_the_hold(self, monkeypatch):
        text = _thread_text(mention=True, pr_number=42, message_ts="1000.001")
        text += _reply_block("don't merge, objections open", "1000.002")
        text += _reply_block(
            "Holding off on re-reviewing — noted the concern above.", "1000.003"
        )
        text += _reply_block("ok, go ahead", "1000.004")
        monkeypatch.setattr(sys, "stdin", io.StringIO(text))

        result = slack_scan.cmd_check_hold()

        assert result == {"held": False, "matched_phrase": None, "reason": None}

    def test_new_objection_after_hold_ack_stays_held(self, monkeypatch):
        text = _thread_text(mention=True, pr_number=42, message_ts="1000.001")
        text += _reply_block("please stop for now", "1000.002")
        text += _reply_block("Holding off on re-reviewing — noted the concern above.", "1000.003")
        text += _reply_block("still not ready, please wait, pls", "1000.004")
        monkeypatch.setattr(sys, "stdin", io.StringIO(text))

        result = slack_scan.cmd_check_hold()

        assert result["held"] is True
        assert result["reason"] == "objection"
