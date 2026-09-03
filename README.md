# ultracode-live-engineer

An unattended, self-paced loop for Claude Code that does two things on a
timer, forever, until you stop it:

1. **Reviews PRs people @-mention you on in a Slack channel** — scans the
   channel and its threads for messages that both mention you and link a
   GitHub PR, runs a full code review on each one, posts the findings (or
   approves outright if only nits survive), and replies in the thread.
2. **Works your Jira backlog** — picks up tickets assigned to you in a
   "ready to implement" status, root-causes and implements them in an
   isolated git worktree, opens a PR, requests review in Slack, and moves
   the ticket to "in review". Anything ambiguous — can't reproduce, needs a
   human judgment call, not converging after a reasonable number of
   attempts — gets parked with a Jira comment explaining the doubt and a
   Slack DM, instead of guessing or spinning forever.

It's generalized out of a real production deployment (redacted of any
project- or company-specific detail) and is fully config-driven — every
identifier (repo, Slack channel, Jira project, etc.) lives in a config file
you create per project, not in the plugin itself.

## How it fits together

- `skills/ultracode-live-engineer/SKILL.md` — a thin driver: reads your
  project's config, runs the workflow, reads its summary, and schedules the
  next wake-up via Claude Code's `ScheduleWakeup`/`/loop` mechanism. This is
  the piece `/loop` actually invokes.
- `workflows/ultracode-live-engineer.js` — the actual multi-agent Workflow:
  Slack scanning, PR review dispatch, Jira ticket selection/resume,
  implementation, and HITL (human-in-the-loop) escalation. All the domain
  logic lives here.
- `workflows/ultracode-live-engineer/` — the workflow's mechanical rule
  engine (`pass_rules.py` + `lib/`): things like "is this PR still open",
  "does this message really contain an exact @-mention", "is this branch
  abandoned" are answered by deterministic Python, not left to model
  judgment, because getting them wrong by "eyeballing" text was a real
  failure mode during development. See `CONFIG.md` for the full field
  reference and `tests/` for the test suite covering these rules.
- `skills/ticket-to-pr/SKILL.md` — a generic starter template for the
  `ticket_to_pr` skill this loop delegates ticket implementation to. It has
  the right phase structure and the `{status, pr_url?, reason?}` contract,
  but every project-specific detail is a marked `TODO(project)` — see
  Prerequisites below.

## Prerequisites

- Claude Code with `/loop` (autonomous scheduler) available.
- A connected Atlassian MCP server (Jira) and a connected Slack MCP server
  (the `slack_read_channel` / `slack_read_thread` / `slack_send_message`
  family of tools).
- `gh` CLI authenticated as the GitHub account you want this loop to act as.
- A `review-pr`-style skill for PR review (any generic one works out of the
  box) and an `ask-team-to-review`-style skill for posting a review request
  to Slack.
- **A filled-in `ticket_to_pr` skill.** This plugin ships a generic starter
  (`skills/ticket-to-pr/`) with the right phase structure and contract
  (given a ticket key + optional resume context: root-cause first, minimal
  diff, real tests, PR, mandatory multi-angle review, close the ticket,
  return `{status: "success"|"escalated"|"failed", pr_url?, reason?}`), but
  "how do I implement a ticket in this specific codebase" is inherently
  project-specific — walk its `TODO(project)` markers (repro data sources,
  formatter/lockfile traps, sub-agents, required Jira fields) and fill them
  in before pointing the unattended loop at it. Point `config.json`'s
  `skills.ticket_to_pr` at your customized copy (rename it if you fork it
  per-project).

## Install

1. Add this plugin (from a marketplace pointing at this repo, or by
   installing it directly — see Claude Code's plugin docs for the exact
   command your version supports). Once installed, `${CLAUDE_PLUGIN_ROOT}`
   resolves to this plugin's install directory.
2. In the **target project repo** you want the loop to work against, copy
   `workflows/ultracode-live-engineer/config.example.json` (from inside the
   plugin) to `.claude/ultracode-live-engineer/config.json` in that repo,
   and fill in every field — see `workflows/ultracode-live-engineer/CONFIG.md`
   for the full reference. Config is per-project and lives in the project,
   not in the plugin — you can point this loop at multiple repos, each with
   its own config.
3. Customize the shipped `ticket-to-pr` starter skill for your project (see
   Prerequisites above) — it already defaults into `config.json`'s
   `skills.ticket_to_pr` field, just fill in its `TODO(project)` markers.
4. Kick it off with `/loop` invoking `Skill("ultracode-live-engineer")`. It
   self-paces its own wake-ups from there (short recheck if it did work,
   longer idle backoff if there was nothing to do) — no cron/interval setup
   needed.

## Safety model

The workflow hard-codes a set of guardrails into every implementation/fix
prompt it dispatches: never merge a PR, never force-push, never push
directly to the default branch, no destructive DB/infra commands against
shared environments, never skip hooks or tests, always work in an isolated
git worktree (never the operator's live checkout), and a configurable cap
on how many tickets can be in flight at once. Anything a mechanical rule
can decide (is this PR open, is this branch abandoned, is this the exact
mention token) is decided by deterministic code in `lib/`, not by model
judgment — see the module docstrings in `lib/slack_scan.py` and
`lib/github.py` for the specific incidents that motivated each rule.

## License

MIT — see `LICENSE`.
