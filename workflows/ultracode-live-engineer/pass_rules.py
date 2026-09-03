#!/usr/bin/env python3
"""CLI dispatcher for the ultracode-live-engineer workflow's mechanical
rules. All logic now lives in lib/ (see lib/slack_scan.py, lib/github.py,
lib/cache.py, lib/config.py) — this file only parses argv and prints the
JSON result. Every subcommand is safe to call repeatedly; nothing mutates
state beyond the local cache (see lib/cache.py).

Usage:
    python3 pass_rules.py scan-channel          < channel_dump.txt
    python3 pass_rules.py scan-thread           < thread_dump.txt
    python3 pass_rules.py record-thread-scan <thread_ts> <reply_count> <latest> <is_candidate> '<pr_numbers_json>'
    python3 pass_rules.py gh-filter '[1319, 1310]'
    python3 pass_rules.py check-stale-branch PROJ-2859
    python3 pass_rules.py list-owned-open-prs
    python3 pass_rules.py pr-feedback 1397
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import github, slack_scan  # noqa: E402


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    cmd = sys.argv[1]
    if cmd == "scan-channel":
        result = slack_scan.cmd_scan_channel()
    elif cmd == "scan-thread":
        result = slack_scan.cmd_scan_thread()
    elif cmd == "record-thread-scan":
        if len(sys.argv) < 7:
            print(
                "usage: record-thread-scan <thread_ts> <reply_count> <latest> <is_candidate> '<pr_numbers_json>'",
                file=sys.stderr,
            )
            sys.exit(1)
        result = slack_scan.cmd_record_thread_scan(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5], sys.argv[6])
    elif cmd == "gh-filter":
        if len(sys.argv) < 3:
            print("usage: gh-filter '[123, 456]'", file=sys.stderr)
            sys.exit(1)
        result = github.cmd_gh_filter(sys.argv[2])
    elif cmd == "check-stale-branch":
        if len(sys.argv) < 3:
            print("usage: check-stale-branch PROJ-1234", file=sys.stderr)
            sys.exit(1)
        result = github.cmd_check_stale_branch(sys.argv[2])
    elif cmd == "list-owned-open-prs":
        result = github.cmd_list_owned_open_prs()
    elif cmd == "pr-feedback":
        if len(sys.argv) < 3:
            print("usage: pr-feedback 1397", file=sys.stderr)
            sys.exit(1)
        result = github.cmd_pr_feedback(sys.argv[2])
    else:
        print(f"unknown command: {cmd}", file=sys.stderr)
        sys.exit(1)
        return
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
