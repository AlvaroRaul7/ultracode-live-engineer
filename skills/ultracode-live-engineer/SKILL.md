---
name: ultracode-live-engineer
description: One self-paced wake pass of the unattended Jira-implement and Slack-PR-review loop. Use only when invoked by the /loop autonomous scheduler for this purpose — not for interactively working a single ticket (use your project's ticket-to-pr skill for that) or interactively reviewing one PR (use review-pr for that).
---

# Ultracode Live Engineer — one wake pass

The actual pass (Slack-tagged PR review, Jira ticket selection/resume,
implementation dispatch, HITL escalation) is an autonomous multi-agent
Workflow: `${CLAUDE_PLUGIN_ROOT}/workflows/ultracode-live-engineer.js`. This
skill is a thin driver around it — its only job each wake is to run that
workflow and turn its returned summary into the next `ScheduleWakeup` call.
All of the domain rules (guardrails, mechanical Slack/GitHub/Jira checks,
transition-id and required-field gotchas) live in the workflow script now,
not here — read it directly if you need the details, don't duplicate them
in this file.

This workflow is config-driven (see
`${CLAUDE_PLUGIN_ROOT}/workflows/ultracode-live-engineer/CONFIG.md`) — the
script itself has no filesystem access, so THIS skill is responsible for
reading `config.json` and handing it to the script via `args`. Config is
per-project, not part of the plugin install: it lives in the repo you're
running the loop against, at `.claude/ultracode-live-engineer/config.json`
(copy it from the plugin's shipped `config.example.json` and fill it in —
see CONFIG.md).

## Pass driver

Every wake, in order:

1. **Read config.** `Read(".claude/ultracode-live-engineer/config.json")`
   from the target repo's root, parse it. Compute `cache_dir`:
   - if `config.cache_dir` is set, use it verbatim (after `~` expansion)
   - else derive it: `~/.claude/ultracode-live-engineer/<config.repo with / replaced by ->`
     Merge the resolved absolute `cache_dir` into the object you'll pass as
     `args` (`{...config, cache_dir: <resolved absolute path>}`) — the script
     needs an already-resolved path, it can't do `~`-expansion or derivation
     itself.
2. Run `Workflow({scriptPath: "${CLAUDE_PLUGIN_ROOT}/workflows/ultracode-live-engineer.js", args: <the merged config object from step 1>})`.
   **Not** `Workflow({name: "ultracode-live-engineer"})` — the `name`
   registry does not pick up plugin-shipped workflow files automatically
   (confirmed live: only a small set of built-ins are pre-registered there),
   so `scriptPath` is the only way to run this script. It runs in the
   background and spans many sub-agents (Slack scanning, PR review dispatch,
   Jira selection, the worktree-isolated implementer) — wait for its
   completion notification rather than polling. It never blocks on human
   input mid-pass: any ambiguity is escalated via a Jira comment + transition
   to "On Hold" inside the workflow itself, and the pass moves on. A crash
   inside the script (a thrown error) is caught internally and returned as
   part of the summary (`crashed: true`) rather than surfacing as a task
   `status: "failed"` — treat an actual `status: "failed"` task (no summary
   object at all) as a genuine infra-level failure (the harness itself lost
   the process), not something this driver has a protocol for; report it to
   the user and stop, don't silently reschedule.
3. Read the workflow's returned summary object: `prsReviewed`,
   `prsFollowedUp` (own PRs where review feedback was fixed or escalated),
   `ticketKeys` (every ticket resumed/selected and implemented this pass,
   possibly several — up to `max_concurrent_tickets` run concurrently),
   `ticketOutcomes` (`{key, status, pr_url?, reason?}[]`, one per
   `ticketKeys` entry), `escalatedTicketKeys` (array — resume-transition
   failures, stale-branch escalations, and implement failures all land
   here), `didWork`, `crashed`, `crashedPhase`, `crashedError`,
   `onHoldCount`, `oldestOnHoldKey`.
4. **Append structured history.** Append one JSON line to
   `<cache_dir>/ultracode-pass-history.jsonl` (create the file/directory if
   missing) combining:
   - every field from the summary object (step 3)
   - `timestamp`: current UTC time (`date -u +"%Y-%m-%dT%H:%M:%SZ"` via Bash)
   - usage stats already present in the completion notification's `<usage>`
     block: `subagent_tokens`, `tool_uses`, `duration_ms`, `agent_count`
     This is best-effort bookkeeping — if it fails, don't retry and don't
     treat it as a pass failure. This is separate from (and in addition to)
     the human-readable `pass-log.md` the workflow script itself already
     appends to — don't remove or duplicate that.
5. Reschedule via `ScheduleWakeup` (`delaySeconds`, `prompt`, and `reason`
   are all required by that tool):
   - If `crashed` is `true`: treat like `didWork` (short recheck — a crash
     usually means something needs attention soon) and say so plainly in
     your turn's status text to the user — don't silently reschedule as if
     nothing happened.
   - Else if `didWork` is `true` (reviewed ≥1 PR, followed up on ≥1 own PR,
     or `ticketKeys` is non-empty): `delaySeconds` in the 300–600 range
     (short — there may be more to do).
   - Else (fully idle — nothing in Slack, nothing eligible in Jira, no
     concurrency slots freed up): `delaySeconds` in the 1200–1800 range.
   - Always pass the same invocation prompt (`Skill("ultracode-live-engineer")`)
     as the `prompt` field so the next wake repeats this pass, and a short
     human-readable `reason` summarizing the workflow's summary (e.g.
     "reviewed 2 PRs, worked PROJ-1234, PROJ-5678" or "idle — no PRs or
     tickets eligible" or "CRASHED in phase Implement: <error>").

That's the whole driver. Don't re-implement Slack scanning, Jira JQL, or
GitHub filtering logic here — it's all in the workflow script, which is the
single source of truth going forward.
