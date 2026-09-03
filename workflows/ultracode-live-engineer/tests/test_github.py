import json
import os

import pytest

from lib import github
from tests.helpers import make_gh_json, seed_cache

REPO = github.REPO
BOT_LOGIN = github.BOT_LOGIN


def _pr_view_prefix(n):
    return ("pr", "view", str(n), "--repo", REPO, "--json", "state,title,url,author")


def _pr_view_json(state="OPEN", author_login="someone", title="t", url="u"):
    return json.dumps({"state": state, "title": title, "url": url, "author": {"login": author_login}})


def _reviews_prefix(n):
    return (
        "api",
        f"repos/{REPO}/pulls/{n}/reviews",
        "--jq",
        f'.[] | select(.user.login == "{BOT_LOGIN}") | .submitted_at',
    )


def _commits_prefix(n):
    return (
        "api",
        f"repos/{REPO}/pulls/{n}/commits",
        "--paginate",
        "--jq",
        ".[].commit.committer.date",
    )


def _dates_out(*dates):
    return "\n".join(dates)


def _branches_prefix():
    return ("api", f"repos/{REPO}/branches")


def _pr_list_head_prefix(branch):
    return ("pr", "list", "--repo", REPO, "--head", branch)


class TestMakeGhJson:
    def test_ambiguous_prefixes_raise_instead_of_silently_picking_one(self):
        """Two scripted prefixes both matching the same call is a test-authoring
        bug (e.g. a later test file adding a broader `gh pr list` prefix that
        also matches an existing narrower one) -- it must fail loudly, not
        silently return whichever was listed first."""
        fake = make_gh_json(
            [
                (("pr", "list"), ("broad", None)),
                (("pr", "list", "--repo", REPO), ("narrow", None)),
            ]
        )

        with pytest.raises(AssertionError, match="ambiguous"):
            fake("pr", "list", "--repo", REPO)


class TestCmdGhFilter:
    def test_cached_pr_is_skipped_without_any_gh_call(self, cache_path, monkeypatch):
        seed_cache(
            cache_path,
            {"handled_prs": {"5": "state=CLOSED (not OPEN — never review merged/closed PRs)"}, "scanned_threads": {}},
        )
        fake = make_gh_json([])
        monkeypatch.setattr(github, "gh_json", fake)

        result = github.cmd_gh_filter("[5]")

        assert result["unhandled"] == []
        assert len(result["skipped"]) == 1
        assert result["skipped"][0]["pr"] == 5
        assert result["skipped"][0]["reason"].endswith("(cached)")
        assert fake.calls == []

    def test_closed_pr_is_skipped_and_cached(self, cache_path, monkeypatch):
        fake = make_gh_json(
            [(_pr_view_prefix(5), (_pr_view_json(state="CLOSED"), None))]
        )
        monkeypatch.setattr(github, "gh_json", fake)

        result = github.cmd_gh_filter("[5]")

        assert result["unhandled"] == []
        skipped = result["skipped"][0]
        assert skipped["pr"] == 5
        assert "not OPEN" in skipped["reason"]
        with open(cache_path) as f:
            cache = json.load(f)
        assert "5" in cache["handled_prs"]

    def test_self_authored_pr_is_skipped_and_cached(self, cache_path, monkeypatch):
        fake = make_gh_json(
            [(_pr_view_prefix(5), (_pr_view_json(state="OPEN", author_login=BOT_LOGIN), None))]
        )
        monkeypatch.setattr(github, "gh_json", fake)

        result = github.cmd_gh_filter("[5]")

        assert result["unhandled"] == []
        skipped = result["skipped"][0]
        assert skipped["pr"] == 5
        assert "never self-review" in skipped["reason"]
        with open(cache_path) as f:
            cache = json.load(f)
        assert "5" in cache["handled_prs"]

    def test_open_unreviewed_pr_is_unhandled_and_not_cached(self, cache_path, monkeypatch):
        fake = make_gh_json(
            [
                (_pr_view_prefix(5), (_pr_view_json(state="OPEN", author_login="someone"), None)),
                (_reviews_prefix(5), ("", None)),
            ]
        )
        monkeypatch.setattr(github, "gh_json", fake)

        result = github.cmd_gh_filter("[5]")

        assert result["skipped"] == []
        assert result["unhandled"] == [
            {"pr": 5, "title": "t", "url": "u", "re_review": False}
        ]
        assert not os.path.exists(cache_path)

    def test_reviewed_pr_with_commit_after_review_is_unhandled_re_review(self, cache_path, monkeypatch):
        fake = make_gh_json(
            [
                (_pr_view_prefix(5), (_pr_view_json(state="OPEN", author_login="someone"), None)),
                (_reviews_prefix(5), (_dates_out("2026-01-01T00:00:00Z"), None)),
                (_commits_prefix(5), (_dates_out("2026-01-02T00:00:00Z"), None)),
            ]
        )
        monkeypatch.setattr(github, "gh_json", fake)

        result = github.cmd_gh_filter("[5]")

        assert result["skipped"] == []
        assert result["unhandled"] == [
            {"pr": 5, "title": "t", "url": "u", "re_review": True}
        ]
        assert not os.path.exists(cache_path)

    def test_reviewed_pr_with_no_commits_since_is_skipped_not_cached(self, cache_path, monkeypatch):
        fake = make_gh_json(
            [
                (_pr_view_prefix(5), (_pr_view_json(state="OPEN", author_login="someone"), None)),
                (_reviews_prefix(5), (_dates_out("2026-01-02T00:00:00Z"), None)),
                (_commits_prefix(5), (_dates_out("2026-01-01T00:00:00Z"), None)),
            ]
        )
        monkeypatch.setattr(github, "gh_json", fake)

        result = github.cmd_gh_filter("[5]")

        assert result["unhandled"] == []
        skipped = result["skipped"][0]
        assert skipped["pr"] == 5
        assert "already reviewed" in skipped["reason"]
        assert not os.path.exists(cache_path)

    def test_gh_pr_view_failure_is_skipped(self, cache_path, monkeypatch):
        fake = make_gh_json([(_pr_view_prefix(5), (None, "some gh error"))])
        monkeypatch.setattr(github, "gh_json", fake)

        result = github.cmd_gh_filter("[5]")

        assert result["unhandled"] == []
        skipped = result["skipped"][0]
        assert skipped["pr"] == 5
        assert "gh pr view failed" in skipped["reason"]


class TestCmdCheckStaleBranch:
    def test_no_matching_branch_is_not_stale(self, monkeypatch):
        fake = make_gh_json([(_branches_prefix(), ("main\ndevelop", None))])
        monkeypatch.setattr(github, "gh_json", fake)

        result = github.cmd_check_stale_branch("PROJ-1234")

        assert result == {"stale": False, "branch": None}

    @pytest.mark.parametrize(
        "prs_json, expected_extra",
        [
            (
                [{"number": 7, "state": "OPEN", "url": "u7"}],
                {"open_pr": {"number": 7, "state": "OPEN", "url": "u7"}},
            ),
            (
                [{"number": 7, "state": "MERGED", "url": "u7"}],
                {"merged_pr": {"number": 7, "state": "MERGED", "url": "u7"}},
            ),
            ([], {}),
        ],
        ids=["open-pr", "merged-pr-only", "no-referencing-pr"],
    )
    def test_matching_branch(self, monkeypatch, prs_json, expected_extra):
        branch = "fix/PROJ-1234-something"
        fake = make_gh_json(
            [
                (_branches_prefix(), (branch, None)),
                (_pr_list_head_prefix(branch), (json.dumps(prs_json), None)),
            ]
        )
        monkeypatch.setattr(github, "gh_json", fake)

        result = github.cmd_check_stale_branch("PROJ-1234")

        # No PR at all -> stale=True and no extra key; an open OR merged PR
        # -> stale=False with the matching extra key (this collapses what
        # were three near-identical tests differing only in PR state/extra).
        assert result == {"stale": not expected_extra, "branch": branch, **expected_extra}

    def test_gh_pr_list_error_is_treated_same_as_no_prs(self, monkeypatch):
        """Characterizes CURRENT behavior, not desired behavior: the
        `gh pr list --head <branch>` call can fail transiently (e.g. a
        real (None, "some error") from gh, not just empty output), and
        `prs = json.loads(out) if not err and out else []` treats that
        identically to "no PRs found" -- falling through to stale=True.
        This is the same failure class as the PROJ-1234-style regression this
        function's docstring cites (a real PR existed but the function
        didn't see it), except triggered by a transient API error instead
        of a missed lookup. This test pins the current behavior; it does
        not assert this is correct."""
        branch = "fix/PROJ-1234-something"
        fake = make_gh_json(
            [
                (_branches_prefix(), (branch, None)),
                (_pr_list_head_prefix(branch), (None, "some transient gh error")),
            ]
        )
        monkeypatch.setattr(github, "gh_json", fake)

        result = github.cmd_check_stale_branch("PROJ-1234")

        assert result == {"stale": True, "branch": branch}


def _feedback_comments_prefix(n):
    return ("api", f"repos/{REPO}/pulls/{n}/comments", "--paginate")


def _feedback_graphql_query(n):
    owner, repo_name = REPO.split("/")
    return (
        "query { repository(owner: \"%s\", name: \"%s\") { pullRequest(number: %s) "
        "{ reviewThreads(first: 100) { nodes { isResolved "
        "comments(first: 1) { nodes { databaseId } } } } } } }"
    ) % (owner, repo_name, n)


def _feedback_graphql_prefix(n):
    return ("api", "graphql", "-f", f"query={_feedback_graphql_query(n)}")


# Distinct from _reviews_prefix/_commits_prefix above: those two script
# cmd_gh_filter's own reviews/commits calls (which pass extra --jq
# arguments), while cmd_pr_feedback hits the SAME two endpoints with a
# plain --paginate call and no --jq -- a genuinely different call shape
# under a name collision, so these stay separately scoped rather than
# shadowing the gh_filter ones.
def _feedback_reviews_prefix(n):
    return ("api", f"repos/{REPO}/pulls/{n}/reviews", "--paginate")


def _feedback_commits_prefix(n):
    return ("api", f"repos/{REPO}/pulls/{n}/commits", "--paginate")


def _feedback_issue_comments_prefix(n):
    return ("api", f"repos/{REPO}/issues/{n}/comments", "--paginate")


def _feedback_checks_prefix(n):
    # Full argument list, not an abbreviated prefix -- an under-specified
    # prefix here would let a semantic regression (e.g. dropping --json or
    # swapping the field list) pass silently. See B5/C4 review history.
    return ("pr", "checks", str(n), "--repo", REPO, "--json", "name,state,link,bucket")


def _feedback_graphql_response(resolved_root_ids=()):
    """A valid graphql payload with one resolved-thread node per id in
    resolved_root_ids, plus one always-unresolved filler node (so the
    resolved case still exercises the "not every node resolved" path)."""
    nodes = [
        {"isResolved": True, "comments": {"nodes": [{"databaseId": rid}]}}
        for rid in resolved_root_ids
    ]
    nodes.append({"isResolved": False, "comments": {"nodes": [{"databaseId": 999999}]}})
    return json.dumps({"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": nodes}}}}})


def _review_comment(comment_id, in_reply_to_id, author, body, created_at, path="a.py", line=10):
    c = {
        "id": comment_id,
        "user": {"login": author},
        "body": body,
        "created_at": created_at,
        "path": path,
        "line": line,
    }
    if in_reply_to_id is not None:
        c["in_reply_to_id"] = in_reply_to_id
    return c


def _review(review_id, author, state, submitted_at, body=""):
    return {
        "id": review_id,
        "user": {"login": author},
        "state": state,
        "submitted_at": submitted_at,
        "body": body,
    }


def _commit(date):
    return {"commit": {"committer": {"date": date}}}


def _issue_comment(comment_id, author, body, created_at):
    return {"id": comment_id, "user": {"login": author}, "body": body, "created_at": created_at}


def _feedback_script(
    n="5", comments=None, graphql=None, reviews=None, commits=None, issue_comments=None, checks=None
):
    """Every call site cmd_pr_feedback hits, in order, with a script
    covering all six -- every one fires unconditionally on every
    invocation (only the comments call exits early on error), so leaving
    any of these unscripted makes make_gh_json raise "unscripted gh_json
    call" rather than exercising the scenario under test."""
    return make_gh_json(
        [
            (_feedback_comments_prefix(n), (json.dumps(comments if comments is not None else []), None)),
            (_feedback_graphql_prefix(n), (graphql if graphql is not None else _feedback_graphql_response(), None)),
            (_feedback_reviews_prefix(n), (json.dumps(reviews if reviews is not None else []), None)),
            (_feedback_commits_prefix(n), (json.dumps(commits if commits is not None else []), None)),
            (
                _feedback_issue_comments_prefix(n),
                (json.dumps(issue_comments if issue_comments is not None else []), None),
            ),
            (_feedback_checks_prefix(n), (json.dumps(checks if checks is not None else []), None)),
        ]
    )


class TestUnaddressedReviewThreads:
    def test_unresolved_thread_with_non_bot_last_reply_appears_with_full_shape(self, monkeypatch):
        # Fed out of chronological order so the sort-by-created_at assertion
        # below is discriminating, not vacuous.
        comments = [
            _review_comment(102, 101, "human", "actually still an issue", "2026-01-01T10:00:00Z", path="a.py", line=7),
            _review_comment(101, None, "human", "please fix this", "2026-01-01T09:00:00Z", path="a.py", line=7),
        ]
        fake = _feedback_script(comments=comments, graphql=_feedback_graphql_response(resolved_root_ids=()))
        monkeypatch.setattr(github, "gh_json", fake)

        result = github.cmd_pr_feedback("5")

        assert result["unaddressed_review_threads"] == [
            {
                "root_comment_id": 101,
                "path": "a.py",
                "line": 7,
                "messages": [
                    {"author": "human", "body": "please fix this", "created_at": "2026-01-01T09:00:00Z"},
                    {"author": "human", "body": "actually still an issue", "created_at": "2026-01-01T10:00:00Z"},
                ],
            }
        ]

    @pytest.mark.parametrize(
        "resolved_root_ids, last_reply_author, expect_present",
        [
            ((101,), "human", False),  # scenario 2: GraphQL marks the root resolved
            ((), BOT_LOGIN, False),  # scenario 3: bot had the last reply
            ((), "human", True),  # scenario 1 restated: unresolved, non-bot last reply
        ],
        ids=["resolved-by-graphql", "bot-had-last-word", "unresolved-non-bot-last-word"],
    )
    def test_thread_inclusion_depends_on_resolution_and_last_author(
        self, monkeypatch, resolved_root_ids, last_reply_author, expect_present
    ):
        comments = [
            _review_comment(101, None, "human", "please fix this", "2026-01-01T09:00:00Z"),
            _review_comment(102, 101, last_reply_author, "reply", "2026-01-01T10:00:00Z"),
        ]
        fake = _feedback_script(comments=comments, graphql=_feedback_graphql_response(resolved_root_ids=resolved_root_ids))
        monkeypatch.setattr(github, "gh_json", fake)

        result = github.cmd_pr_feedback("5")

        root_ids = [t["root_comment_id"] for t in result["unaddressed_review_threads"]]
        assert (101 in root_ids) is expect_present


class TestUnaddressedReviews:
    @pytest.mark.parametrize(
        "author, state, submitted_at, expect_present",
        [
            ("human", "CHANGES_REQUESTED", "2026-01-02T00:00:00Z", True),  # after last commit
            ("human", "CHANGES_REQUESTED", "2025-12-31T00:00:00Z", False),  # before last commit
            (BOT_LOGIN, "CHANGES_REQUESTED", "2026-01-02T00:00:00Z", False),  # self-review
            ("human", "APPROVED", "2026-01-02T00:00:00Z", False),  # not CHANGES_REQUESTED
            # Falls strictly between the two commit dates below: this only
            # passes under commits[-1] semantics (compares against
            # 00:00:00Z); a max()-based implementation would compare
            # against 12:00:00Z and exclude it. See discriminating-case
            # note above `commits` for why this pins commits[-1].
            ("human", "CHANGES_REQUESTED", "2026-01-01T06:00:00Z", True),
        ],
        ids=[
            "changes-requested-after-commit",
            "changes-requested-before-commit-superseded",
            "self-review-never-counts",
            "approved-never-counts",
            "between-commits-pins-last-not-max",
        ],
    )
    def test_review_inclusion_by_state_author_and_timing(
        self, monkeypatch, author, state, submitted_at, expect_present
    ):
        reviews = [_review(1, author, state, submitted_at)]
        # Commits fed out of order -- last_commit_at uses the LAST array
        # element (commits[-1] = 00:00:00Z), not max() (which would be
        # 12:00:00Z), so this pins that exact (non-sorted) behavior. Note:
        # cmd_pr_feedback's own docstring calls this "after this bot's own
        # most recent commit," but the implementation applies no author
        # filter at all -- it's simply the last commit in the array,
        # regardless of who committed it (see _commit() below, which has
        # no author field and structurally can't express bot-vs-human
        # commits). This test pins CURRENT behavior, not endorsed-correct
        # behavior.
        commits = [_commit("2026-01-01T12:00:00Z"), _commit("2026-01-01T00:00:00Z")]
        fake = _feedback_script(reviews=reviews, commits=commits)
        monkeypatch.setattr(github, "gh_json", fake)

        result = github.cmd_pr_feedback("5")

        review_ids = [r["id"] for r in result["unaddressed_reviews"]]
        assert (1 in review_ids) is expect_present


class TestUnaddressedIssueComments:
    def test_comments_after_bots_last_one_appear_when_bot_not_last(self, monkeypatch):
        # Fed out of chronological order: the bot's comment is chronologically
        # LATEST-BUT-ONE relative to the list order, forcing the sort to matter.
        issue_comments = [
            _issue_comment(3, "human", "one more thing", "2026-01-01T12:00:00Z"),
            _issue_comment(1, "human", "please fix", "2026-01-01T09:00:00Z"),
            _issue_comment(2, BOT_LOGIN, "done", "2026-01-01T10:00:00Z"),
        ]
        fake = _feedback_script(issue_comments=issue_comments)
        monkeypatch.setattr(github, "gh_json", fake)

        result = github.cmd_pr_feedback("5")

        assert result["unaddressed_issue_comments"] == [
            {"id": 3, "user": "human", "body": "one more thing", "created_at": "2026-01-01T12:00:00Z"}
        ]

    def test_bot_comment_last_chronologically_yields_empty(self, monkeypatch):
        issue_comments = [
            _issue_comment(1, "human", "please fix", "2026-01-01T09:00:00Z"),
            _issue_comment(2, BOT_LOGIN, "done", "2026-01-01T10:00:00Z"),
        ]
        fake = _feedback_script(issue_comments=issue_comments)
        monkeypatch.setattr(github, "gh_json", fake)

        result = github.cmd_pr_feedback("5")

        assert result["unaddressed_issue_comments"] == []


def _check(name, state, bucket, link="https://ci.example/run/1"):
    return {"name": name, "state": state, "bucket": bucket, "link": link}


class TestFailingChecks:
    @pytest.mark.parametrize(
        "state, bucket, expect_present",
        [
            ("FAILURE", "fail", True),
            ("ERROR", "fail", True),
            ("SUCCESS", "pass", False),
            ("PENDING", "pending", False),
            # gh's own `bucket` categorization is what we filter on, not the
            # raw `state` string -- these two states never literally equal
            # "FAILURE"/"ERROR" but gh still buckets them as "fail", which a
            # naive `state in (...)` filter would silently miss.
            ("TIMED_OUT", "fail", True),
            ("ACTION_REQUIRED", "fail", True),
        ],
        ids=[
            "failure-counts",
            "error-counts",
            "success-excluded",
            "pending-excluded",
            "timed-out-counts-via-bucket",
            "action-required-counts-via-bucket",
        ],
    )
    def test_check_inclusion_by_bucket(self, monkeypatch, state, bucket, expect_present):
        checks = [_check("build", state, bucket)]
        fake = _feedback_script(checks=checks)
        monkeypatch.setattr(github, "gh_json", fake)

        result = github.cmd_pr_feedback("5")

        names = [c["name"] for c in result["failing_checks"]]
        assert ("build" in names) is expect_present

    def test_failing_check_carries_name_state_and_link(self, monkeypatch):
        checks = [_check("lint", "FAILURE", "fail", link="https://ci.example/run/9")]
        fake = _feedback_script(checks=checks)
        monkeypatch.setattr(github, "gh_json", fake)

        result = github.cmd_pr_feedback("5")

        assert result["failing_checks"] == [
            {"name": "lint", "state": "FAILURE", "link": "https://ci.example/run/9"}
        ]

    def test_gh_pr_checks_error_yields_empty_failing_checks_without_crashing(self, monkeypatch):
        fake = make_gh_json(
            [
                (_feedback_comments_prefix("5"), (json.dumps([]), None)),
                (_feedback_graphql_prefix("5"), (_feedback_graphql_response(), None)),
                (_feedback_reviews_prefix("5"), (json.dumps([]), None)),
                (_feedback_commits_prefix("5"), (json.dumps([]), None)),
                (_feedback_issue_comments_prefix("5"), (json.dumps([]), None)),
                (_feedback_checks_prefix("5"), (None, "no checks reported on the given ref")),
            ]
        )
        monkeypatch.setattr(github, "gh_json", fake)

        result = github.cmd_pr_feedback("5")

        assert result["failing_checks"] == []


class TestGraphqlFailureFallback:
    @pytest.mark.parametrize(
        "graphql_response",
        [
            (None, "boom"),
            ("{not valid json", None),
            (json.dumps({"data": {}}), None),
        ],
        ids=["gh-error", "unparsable-json", "unexpected-shape"],
    )
    def test_graphql_failure_does_not_crash_and_falls_back_to_unresolved(
        self, monkeypatch, graphql_response
    ):
        comments = [
            _review_comment(101, None, "human", "please fix this", "2026-01-01T09:00:00Z"),
            _review_comment(102, 101, "human", "still broken", "2026-01-01T10:00:00Z"),
        ]
        fake = make_gh_json(
            [
                (_feedback_comments_prefix("5"), (json.dumps(comments), None)),
                (_feedback_graphql_prefix("5"), graphql_response),
                (_feedback_reviews_prefix("5"), (json.dumps([]), None)),
                (_feedback_commits_prefix("5"), (json.dumps([]), None)),
                (_feedback_issue_comments_prefix("5"), (json.dumps([]), None)),
                (_feedback_checks_prefix("5"), (json.dumps([]), None)),
            ]
        )
        monkeypatch.setattr(github, "gh_json", fake)

        result = github.cmd_pr_feedback("5")

        root_ids = [t["root_comment_id"] for t in result["unaddressed_review_threads"]]
        assert 101 in root_ids
