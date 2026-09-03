"""GitHub-facing mechanical rules: which PRs are reviewable, stale-branch
detection, and unaddressed-feedback detection on the bot's own PRs
(including CI check failures — see cmd_pr_feedback's failing_checks, added
in Phase D). These are mechanical checks that must NOT be left to model
judgment — each one exists because a prior pass got a rule wrong by
eyeballing text instead of matching it exactly. This is the GitHub-facing
half of the mechanical-rule pair; lib/slack_scan.py is the other half."""
import json
import subprocess
import sys

from .cache import _load_cache, _save_cache
from .config import CONFIG

REPO = CONFIG["repo"]
BOT_LOGIN = CONFIG["bot_github_login"]
BRANCH_PREFIX = CONFIG["branch_prefix"]


def gh_json(*args):
    result = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        return None, result.stderr.strip()
    return result.stdout, None


def cmd_gh_filter(pr_numbers_json: str):
    """Given a JSON list of candidate PR numbers, drop any that are not
    OPEN (never review merged/closed PRs), authored by this bot's own
    GitHub account (never self-review — surfaced live in production: a
    teammate approving the human's own PR and @-mentioning them to say so is
    a literal mention+PR-link match, but not a review request), or already
    have a review posted by this bot's GitHub account (dedup lives in
    GitHub, not Slack).

    Cost optimization: a PR whose outcome is already cached as terminal
    (MERGED/CLOSED, or already reviewed by this bot — see _load_cache) is
    resolved from the cache with zero `gh` calls. Only PRs not yet seen, or
    seen but still genuinely open-and-unreviewed (never cached, since that
    can change any time), hit the API — on a settled backlog this is most of
    the historical cost that used to get re-paid every single idle pass.

    Note: this read-modify-write on handled_prs is safe without _cache_lock
    only because the workflow dispatches gh-filter as a single batched call,
    never concurrently. If a future change parallelizes PR-number lookups,
    add the same locking used in cmd_record_thread_scan."""
    try:
        pr_numbers = json.loads(pr_numbers_json)
    except json.JSONDecodeError as exc:
        print(json.dumps({"error": f"bad PR number list: {exc}"}))
        sys.exit(1)

    cache = _load_cache()
    handled = cache["handled_prs"]
    cache_dirty = False

    unhandled = []
    skipped = []
    for n in pr_numbers:
        key = str(n)
        if key in handled:
            skipped.append({"pr": n, "reason": f"{handled[key]} (cached)"})
            continue

        out, err = gh_json(
            "pr", "view", str(n), "--repo", REPO, "--json", "state,title,url,author"
        )
        if err:
            skipped.append({"pr": n, "reason": f"gh pr view failed: {err}"})
            continue
        info = json.loads(out)
        if info["state"] != "OPEN":
            reason = f"state={info['state']} (not OPEN — never review merged/closed PRs)"
            skipped.append({"pr": n, "reason": reason})
            handled[key] = reason
            cache_dirty = True
            continue
        if info.get("author", {}).get("login") == BOT_LOGIN:
            reason = f"authored by {BOT_LOGIN} — never self-review"
            skipped.append({"pr": n, "reason": reason})
            handled[key] = reason
            cache_dirty = True
            continue

        out, err = gh_json(
            "api", f"repos/{REPO}/pulls/{n}/reviews",
            "--jq", ".[] | select(.user.login == \"%s\") | .submitted_at" % BOT_LOGIN,
        )
        if err:
            skipped.append({"pr": n, "reason": f"gh api reviews failed: {err}"})
            continue
        bot_review_dates = sorted(out.split())
        if bot_review_dates:
            out, err = gh_json(
                "api", f"repos/{REPO}/pulls/{n}/commits", "--paginate",
                "--jq", ".[].commit.committer.date",
            )
            if err:
                skipped.append({"pr": n, "reason": f"gh api commits failed: {err}"})
                continue
            commit_dates = sorted(out.split())
            latest_commit = commit_dates[-1] if commit_dates else None
            last_review = bot_review_dates[-1]
            if latest_commit and latest_commit > last_review:
                unhandled.append(
                    {"pr": n, "title": info["title"], "url": info["url"], "re_review": True}
                )
                continue
            reason = f"already reviewed by {BOT_LOGIN}, no commits since"
            skipped.append({"pr": n, "reason": reason})
            continue

        unhandled.append(
            {"pr": n, "title": info["title"], "url": info["url"], "re_review": False}
        )

    if cache_dirty:
        cache["handled_prs"] = handled
        _save_cache(cache)

    return {"unhandled": unhandled, "skipped": skipped}


def cmd_check_stale_branch(ticket_key: str):
    """Detect a leftover fix/<TICKET>-* branch from an interrupted prior
    attempt: exists, but no open PR references it, and it was never merged
    either. A branch whose PR already MERGED is not abandoned work — it's
    just leftover branch-cleanup debt (the repo has no auto-delete-on-merge,
    see the delete-merged-branches convention) and must not block ticket
    selection. This was a real false positive live: a ticket's branch was
    flagged stale even though the PR off it had merged, which stopped the
    pass from selecting any ticket that wake."""
    out, err = gh_json("api", f"repos/{REPO}/branches", "--paginate", "--jq", ".[].name")
    if err:
        print(json.dumps({"error": err}))
        sys.exit(1)
    prefix = f"{BRANCH_PREFIX}{ticket_key}-"
    matches = [b for b in out.splitlines() if b.startswith(prefix)]
    if not matches:
        return {"stale": False, "branch": None}
    for branch in matches:
        out, err = gh_json(
            "pr", "list", "--repo", REPO, "--head", branch, "--state", "all",
            "--json", "number,state,url",
        )
        prs = json.loads(out) if not err and out else []
        open_prs = [p for p in prs if p["state"] == "OPEN"]
        if open_prs:
            return {"stale": False, "branch": branch, "open_pr": open_prs[0]}
        merged_prs = [p for p in prs if p["state"] == "MERGED"]
        if merged_prs:
            return {"stale": False, "branch": branch, "merged_pr": merged_prs[0]}
    return {"stale": True, "branch": matches[0]}


def cmd_list_owned_open_prs():
    """List this bot's own OPEN PRs — the candidate set for follow-up
    (someone requesting changes on a PR the loop authored)."""
    out, err = gh_json(
        "pr", "list", "--repo", REPO, "--author", BOT_LOGIN, "--state", "open",
        "--json", "number,title,url,headRefName,updatedAt",
    )
    if err:
        print(json.dumps({"error": err}))
        sys.exit(1)
    return json.loads(out) if out else []


def cmd_pr_feedback(pr_number: str):
    """Find GitHub feedback on one of the bot's own open PRs that hasn't
    been addressed yet. Three independent sources, each with its own exact
    rule for "unaddressed" — this exists for the same reason every other
    rule in this file does: "did someone ask for a change and did we already
    reply" is a mechanical fact, not a judgment call, and got it wrong once
    already (see module docstring) by being left to prose reading.

    1. Inline review-comment threads (grouped by ``in_reply_to_id`` — GitHub
       points every reply at the ROOT comment's id, not at its immediate
       parent, so grouping by that field is exact). A thread is unaddressed
       if its own GitHub resolution state is NOT resolved (via GraphQL
       ``reviewThreads.isResolved``) AND its latest comment's author is not
       this bot — i.e. a human had the last word.
    2. Formal reviews with ``state == CHANGES_REQUESTED`` from someone other
       than this bot, submitted after this bot's own most recent commit on
       the PR (a review submitted before the last commit may already be
       stale/addressed by that commit; one submitted after it never was).
    3. Top-level issue-style PR comments posted after this bot's own most
       recent one (issue comments have no reply-threading, so "addressed"
       here means "does the bot have the last word chronologically").
    4. Failing CI checks on the PR's current HEAD commit (via `gh pr checks`)
       — no staleness comparison needed here, unlike the three sources
       above, since a check result is keyed to one commit, not a reply
       chain. "Failing" is determined by `gh`'s own `bucket` field (one of
       `pass`/`fail`/`pending`/`skipping`/`cancel`), not by pattern-matching
       the raw `state` field ourselves: `state` mixes the Commit-Status-API
       vocabulary (`error`/`failure`/`pending`/`success`) with the
       Check-Runs-API conclusion vocabulary (`success`/`failure`/`neutral`/
       `cancelled`/`timed_out`/`action_required`/`stale`/`skipped`), and a
       hand-rolled `state in (...)` filter silently misses states like
       `TIMED_OUT`/`ACTION_REQUIRED` that `gh` itself already buckets as
       `fail`.

    Nothing here mutates state (no replying, no pushing, no resolving) —
    matches every other subcommand in this file: it only answers the
    question, the workflow decides what to do with the answer."""
    n = pr_number

    out, err = gh_json("api", f"repos/{REPO}/pulls/{n}/comments", "--paginate")
    if err:
        print(json.dumps({"error": f"pulls/comments failed: {err}"}))
        sys.exit(1)
    comments = json.loads(out) if out else []
    threads = {}
    for c in comments:
        root = c.get("in_reply_to_id") or c["id"]
        threads.setdefault(root, []).append(c)
    for members in threads.values():
        members.sort(key=lambda c: c["created_at"])

    resolved_roots = set()
    owner, repo_name = REPO.split("/")
    query = (
        "query { repository(owner: \"%s\", name: \"%s\") { pullRequest(number: %s) "
        "{ reviewThreads(first: 100) { nodes { isResolved "
        "comments(first: 1) { nodes { databaseId } } } } } } }"
    ) % (owner, repo_name, n)
    gql, gerr = gh_json("api", "graphql", "-f", f"query={query}")
    if not gerr and gql:
        try:
            nodes = json.loads(gql)["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"]
            for node in nodes:
                if node["isResolved"]:
                    cn = node["comments"]["nodes"]
                    if cn:
                        resolved_roots.add(cn[0]["databaseId"])
        except (KeyError, TypeError, json.JSONDecodeError):
            pass

    unaddressed_review_threads = []
    for root_id, members in threads.items():
        if root_id in resolved_roots:
            continue
        if members[-1]["user"]["login"] == BOT_LOGIN:
            continue
        unaddressed_review_threads.append(
            {
                "root_comment_id": root_id,
                "path": members[0].get("path"),
                "line": members[0].get("line") or members[0].get("original_line"),
                "messages": [
                    {"author": m["user"]["login"], "body": m["body"], "created_at": m["created_at"]}
                    for m in members
                ],
            }
        )

    out, err = gh_json("api", f"repos/{REPO}/pulls/{n}/reviews", "--paginate")
    reviews = json.loads(out) if not err and out else []
    out, err = gh_json("api", f"repos/{REPO}/pulls/{n}/commits", "--paginate")
    commits = json.loads(out) if not err and out else []
    last_commit_at = commits[-1]["commit"]["committer"]["date"] if commits else None
    unaddressed_reviews = [
        {"id": r["id"], "user": r["user"]["login"], "body": r.get("body", ""), "submitted_at": r["submitted_at"]}
        for r in reviews
        if r.get("state") == "CHANGES_REQUESTED"
        and r["user"]["login"] != BOT_LOGIN
        and (last_commit_at is None or r["submitted_at"] > last_commit_at)
    ]

    out, err = gh_json("api", f"repos/{REPO}/issues/{n}/comments", "--paginate")
    issue_comments = json.loads(out) if not err and out else []
    issue_comments.sort(key=lambda c: c["created_at"])
    last_bot_idx = -1
    for i, c in enumerate(issue_comments):
        if c["user"]["login"] == BOT_LOGIN:
            last_bot_idx = i
    unaddressed_issue_comments = [
        {"id": c["id"], "user": c["user"]["login"], "body": c["body"], "created_at": c["created_at"]}
        for c in issue_comments[last_bot_idx + 1 :]
        if c["user"]["login"] != BOT_LOGIN
    ]

    # 4. Failing checks on the PR's current HEAD commit. Unlike the three sources
    # above, this needs no staleness/last-word comparison — a check result is keyed
    # to one commit, not a reply chain, so "still failing" is just "still in the
    # output of `gh pr checks` right now."
    out, err = gh_json("pr", "checks", str(n), "--repo", REPO, "--json", "name,state,link,bucket")
    checks = json.loads(out) if not err and out else []
    failing_checks = [
        {"name": c["name"], "state": c["state"], "link": c.get("link")}
        for c in checks
        if c.get("bucket") == "fail"
    ]

    return {
        "unaddressed_review_threads": unaddressed_review_threads,
        "unaddressed_reviews": unaddressed_reviews,
        "unaddressed_issue_comments": unaddressed_issue_comments,
        "failing_checks": failing_checks,
    }
