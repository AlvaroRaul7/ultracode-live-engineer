# Configuring `ultracode-live-engineer`

This workflow ships as a plugin — installed once, shared across every
project you run it against. Config is NOT part of the plugin install: it's
per-project, and lives in the repo you're actually running the loop
against, at `.claude/ultracode-live-engineer/config.json`. In that repo,
copy this plugin's `workflows/ultracode-live-engineer/config.example.json`
to `.claude/ultracode-live-engineer/config.json` and fill in every field.
No secrets belong here — everything below is a non-sensitive identifier;
authentication is handled by the already-connected Atlassian/Slack MCP
servers and your `gh` CLI login.

## Fields

- `repo` — `owner/name` of the GitHub repo this loop works against.
- `bot_github_login` — the `gh`-authenticated account this loop runs as.
  Used to detect the loop's own PRs/reviews/commits.
- `slack_channel_id` / `slack_channel_name` — the channel this loop watches
  for `@mention`-tagged PR review requests, and announces its own PRs in.
- `human_slack_id` — the Slack user id whose mentions trigger a review, and
  who receives best-effort escalation DMs.
- `jira_cloud_id` — the Atlassian Cloud site (e.g. `yourcompany.atlassian.net`).
- `jira_project_key` — the Jira project this loop selects/implements tickets
  from.
- `max_concurrent_tickets` — max Jira tickets this loop will implement at
  once (in flight simultaneously, each in its own isolated worktree). The
  only concurrency limiter — does not gate PR review/follow-up work.
- `branch_prefix` — the branch-naming prefix used for ticket branches (e.g.
  `fix/` produces `fix/PROJ-123-short-slug`). Used to detect stale/abandoned
  branches from an interrupted prior attempt.
- `jira_statuses` — the _real_ Jira workflow status names (not labels) for:
  `in_development` (a ticket ready to implement), `in_review` (transitioned
  to after a PR opens), `on_hold` (used for HITL escalation). These vary by
  Jira project configuration — verify via `getTransitionsForJiraIssue`
  against a real ticket before trusting the example values.
- `skills` — names of the three skills this loop delegates to:
  - `ticket_to_pr`: defaults to the `ticket-to-pr` skill this plugin ships
    (`skills/ticket-to-pr/`) — a generic starter template, **not a finished
    skill**: it has the right phase structure (understand, root-cause,
    branch, implement, test, PR, mandatory multi-angle review, close the
    ticket) and the `{status, pr_url?, reason?}` contract this loop expects,
    but every project-specific detail (repro data sources, formatter/
    lockfile traps, sub-agents, required Jira fields) is a marked
    `TODO(project)` for you to fill in before trusting it unattended.
  - `pr_review`: defaults to the generic, already-portable `review-pr` skill
    — no changes needed. This plugin also ships a stricter fork,
    `review-pr-strict` (see `skills/review-pr-strict/`), tuned for
    unattended use (no human in the loop to catch what a lighter pass
    misses) — point `pr_review` at it if you want the higher bar.
  - `request_review`: defaults to `ask-team-to-review` (see
    `skills/ask-team-to-review/`) — fully config-driven, reads this same
    `config.json` for the default channel, the ticket-key pattern, and the
    optional `reviewer_areas` tagging map below. No changes needed even if
    you don't use Jira or `reviewer_areas` — it degrades gracefully to
    asking the user.
- `jira_root_cause_hint` — optional (`null` if not applicable). Some Jira
  Bug-transition screens require a custom field (e.g. "Root Cause Analysis")
  before allowing the transition. If your project has this, set `field_id`
  (the custom field's API id) and `options` (a name→option-id map for the
  common cases). If omitted, the transition prompt still works — it falls
  back to discovering required fields live via `editmeta`, just slower on
  the first hit.
- `reviewer_areas` — optional (`null` if not applicable). Read by the
  `ask-team-to-review` skill (not the loop itself) to auto-tag reviewers by
  which part of the codebase a PR touches. An array of:
  ```json
  [
    {
      "area": "backend",
      "path_prefixes": ["backend/"],
      "reviewers": ["Jordan Lee"]
    },
    {
      "area": "frontend",
      "path_prefixes": ["frontend/"],
      "reviewers": ["Alex Rivera", "Sam Patel"]
    }
  ]
  ```
  (illustrative names only — this plugin ships no roster; every name here is
  supplied by you). Names are resolved to Slack `<@ID>` live via
  `slack_search_users` on every run, never cached in this file or the skill.
  If omitted, `ask-team-to-review` just asks the user who to tag instead of
  auto-tagging.
- `cache_dir` — optional. Where this loop's cache/log files live. Defaults
  to `~/.claude/ultracode-live-engineer/<repo with / replaced by ->` if
  omitted. Only set this explicitly when migrating an existing deployment
  (to preserve its cache) or to relocate storage.

## Cache directory derivation (must match between the driver skill and

`pass_rules.py` — implemented independently in each since they run in
different processes)

```
cache_dir = config["cache_dir"] if present
            else "~/.claude/ultracode-live-engineer/" + config["repo"].replace("/", "-")
```

## `config.json` resolution (`pass_rules.py` / `lib/config.py`)

`pass_rules.py` is invoked via Bash from the target repo's root (see the
workflow script's own agent prompts), so it resolves `config.json` relative
to the current working directory, not relative to its own installed
location:

```
config_path = os.environ["ULE_CONFIG_PATH"] if set
              else "<cwd>/.claude/ultracode-live-engineer/config.json"
```

`ULE_CONFIG_PATH` is an escape hatch (tests, or a non-default location) —
you shouldn't normally need it.
