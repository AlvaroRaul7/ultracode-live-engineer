---
name: ticket-to-pr
description: Use when asked to take a Jira ticket end-to-end from analysis to PR — "work this ticket", "fix PROJ-1234 and open a PR", "take this ticket to a PR", "pick up PROJ-42". Runs the repeatable loop — fetch + understand, root-cause first, branch, right-size the fix, minimal diff, real tests, sync + commit + PR, a MANDATORY high-effort multi-angle review, then comment the PR link back on the ticket — and satisfies the {status, pr_url?, reason?} contract `ultracode-live-engineer` expects from its configured `ticket_to_pr` skill.
---

# Ticket → PR

The repeatable engineering loop for taking one Jira ticket from "assigned"
to "PR open + ticket updated". Work the phases in order. **Root cause before
any fix. Evidence before any claim.** Don't skip to code.

**This is a starter template, not a finished skill.** It's deliberately
generic — no project's AWS tables, custom Jira fields, debugging tools, or
sub-agent roster are the same, so this file ships with clearly marked
customization points (look for `TODO(project)`) instead of guessing at
yours. Fill those in for your own repo before relying on it, especially if
you're wiring it up as the `skills.ticket_to_pr` target for
`ultracode-live-engineer`'s unattended loop — that loop calls this skill
with no human watching, so the more of your repo's real gotchas you bake in
here, the fewer things it gets wrong unattended.

**The loop:** 1 Understand → 2 Root-cause (+ post the confirmed finding on
the ticket) → 3 Branch → 4 Size → 5 Implement → 6 Test → 7 Sync + commit +
PR → 8 High-effort review (multi-angle find → verify → inline PR comments →
fix or defer every finding → verdict) → 9 Close the ticket (PR link on the
ticket). Each phase has a gate; don't advance past a failing one.

## Contract (if driven by `ultracode-live-engineer`)

Report back exactly one of:

- `{status: "success", pr_url: "<url>"}`
- `{status: "escalated", reason: "<why — ambiguous AC, can't reproduce, touches something too sensitive to fix unattended, not converging>"}`
- `{status: "failed", reason: "<what broke, if you errored out with no clear escalation reason>"}`

Self-timebox: if you're not converging after a reasonable number of
attempts, report `"escalated"` rather than continuing indefinitely.

## Phase 1 — Understand the ticket

- Fetch it: `getJiraIssue` via the Atlassian MCP. Read description, comments,
  attachments, linked issues.
- **If the Atlassian server is disconnected this session**, stop and tell
  the user to re-auth via `/mcp` — don't guess the ticket contents.
- TODO(project): if bugs on your project are typically reproduced against
  real data (production logs, a specific database, a staging environment),
  say so here and name where that data lives — pull the _real_ repro data
  _before_ proposing anything, don't reason from the ticket text alone.

## Phase 2 — Root cause FIRST

- Invoke `Skill("superpowers:systematic-debugging")`. Do **not** propose a
  fix until you can point at the cause with `file:line`.
- TODO(project): name any project-specific "where things live" skill here
  (a workflow-map / architecture-index skill, if you have one) so the ticket
  gets read with the right domain context.
- TODO(project): name any project-specific glossary/domain-terms skill here.
- Output the root cause (cited `file:line`) before moving on.
- For a bug whose repro depends on real production data: confirm the root
  cause against that real data first, **then post the confirmed finding as
  a Jira comment** (`addCommentToJiraIssue`, `contentFormat: markdown`)
  _before_ writing code. The comment is the analysis the reporter reviews:
  root cause (`file:line`), the evidence, and the fix direction.

## Phase 3 — Branch

```bash
# Ensure gh is on YOUR account with push access to this repo (per-user, not a
# fixed login): `gh auth status` lists your accounts, `gh auth switch --user <you>`.
git checkout main && git pull --ff-only
git checkout -b fix/<TICKET-KEY>-<short-slug>   # e.g. fix/PROJ-42-fix-slug
```

A **"Repository not found"** on `git pull`/`push`/`gh` means `gh` is on an
account without access → `gh auth switch --user <your-account>` (the one
that can see this repo). Never work on `main` or a stale branch.

## Phase 4 — Size the fix (don't over-ceremony)

- TODO(project): if your repo has a project-memory / capability index that
  tracks ownership of endpoints, tables, events, flags, or agent behavior,
  consult it FIRST here and reuse/extend an owned surface instead of
  duplicating it.
- **Focused, single-site / single-component change** (the common case):
  implement it directly, or dispatch a right-sized sub-agent if your
  project has language/framework-specific implementer agents.
- **Genuinely big / cross-component work only**: escalate to whatever
  planning process your project uses for larger changes. Don't run that
  ceremony for a small fix.

## Phase 5 — Implement a MINIMAL diff

Keep the diff to the intended lines only.

- TODO(project): list any repo-specific traps here — a formatter hook that
  reflows unrelated lines on every edit, a lockfile that gets rewritten by
  your package manager and needs reverting before commit, anything that
  routinely pollutes an otherwise-minimal diff. (Two common examples worth
  checking for in any repo: a format-on-save hook touching more than your
  intended lines, and a lockfile your tooling rewrites on every run.)
- TODO(project): if your diff can add/move/remove an "owned surface"
  (endpoint, table, event, flag, agent/worker) and you have a memory/index
  system tracking those, update it in the same PR.

## Phase 6 — Test thoroughly (real suite, real output)

- Run the **real** test suite, targeted at what you touched.
- Show the **actual** pass/fail output — never assert "tests pass" without
  it.
- Run the project's lint/typecheck.
- Invoke `Skill("superpowers:verification-before-completion")` — evidence
  before claims, including re-confirming the original repro now passes.

## Phase 7 — Sync with `main`, commit + PR

- **Re-sync with `main` BEFORE opening the PR.** `main` almost always moved
  while you worked — never open (or refresh) a PR from a branch that is
  behind, it invites merge conflicts and stale-base CI.
  ```bash
  git fetch origin main
  git log --oneline HEAD..origin/main   # behind if this prints anything
  git rebase origin/main                # rebase only if behind
  ```
  After any rebase that touched your files: **re-run Phase 6** (resolve
  conflicts, confirm the suite is still green). If the branch was already
  pushed, publish the rebased history with `git push --force-with-lease`
  (never a plain `--force`).
- Commit with a `Co-Authored-By:` trailer per your project's attribution
  convention.
- `git push -u origin <branch>`, then `gh pr create --base main` with a body
  covering: **Problem / Root cause / Fix / Tests / Out-of-scope**.
- Confirm the PR's base is `main` and `gh pr view <PR#> --json mergeable` is
  not `CONFLICTING`. If it conflicts, the branch drifted — rebase again and
  force-with-lease.

## Phase 8 — High-effort independent review (MANDATORY for every PR)

Never ship on the author's own green run. **The review engine lives in one
place — the `review-pr` skill — don't re-implement it here.** Invoke
`Skill("review-pr", args="PR #<n>")`. `review-pr` owns the whole machine:
three-dot scope, worktree isolation, partition-by-file finders, the
cross-file pass, blind adversarial verify, and routing by verifier
agreement to inline comments + a verdict. This phase adds only the
ticket-to-pr wrapper around it:

1. TODO(project): if your repo has recurring review traps a generic review
   won't know to look for (a hidden contract between two services, a
   flaky-test dialect gap, a gateway/library internal that's easy to get
   wrong from memory instead of source), hand those to the review here.
2. **Resolve every finding — none may dangle.** Apply
   `Skill("superpowers:receiving-code-review")` rigor (verify each claim
   against the code first — neither blindly implement nor dismiss): fix in
   a follow-up commit **with a regression test per fix**, or defer with a
   written rationale + a named owner/ticket on the thread. Re-run the suite
   post-fix.
3. **Iterate.** A fix is itself new, unreviewed code, so after any round
   that changed the diff, **re-run `review-pr` over the new commit** and
   re-verdict (label it "Review pass N"). Loop until a pass finds **no new
   must-fix (correctness) finding** — cleanup/altitude may be knowingly
   deferred. Cap **3 passes**; if pass 3 still surfaces a must-fix, stop and
   escalate rather than spin. **Record the pass count in the final
   verdict.**

## Phase 9 — Close the loop on the ticket

- Comment the PR link on the Jira ticket via `addCommentToJiraIssue`.
- TODO(project): if your Jira workflow needs other fields set on close
  (sprint, story points, a required transition field), name them and their
  custom-field ids here — look up volatile values (like an active sprint
  id) live rather than hardcoding them, they roll over time.
- **Comments: pass `contentFormat: markdown` AND write real GitHub-flavored
  Markdown — never Jira wiki markup.** The Atlassian MCP converts the body
  per the declared `contentFormat`; mixing wiki syntax into a `markdown`
  body renders the wiki tokens as **literal text**. Translate every token:
  | want                                                                       | Markdown (✅ use)       | Jira wiki (❌ never with `markdown`) |
  | -------------------------------------------------------------------------- | ----------------------- | ------------------------------------ |
  | heading                                                                    | `### Heading`           | `h3. Heading`                        |
  | inline code                                                                | `` `code` ``            | `{{code}}`                           |
  | bold                                                                       | `**bold**`              | `*bold*`                             |
  | italic                                                                     | `*italic*` / `_italic_` | `_italic_`                           |
  | link                                                                       | `[text](https://url)`   | `[text\|https://url]`                |
  | bullet                                                                     | `- item`                | `* item`                             |
  | A bare URL is fine as-is. If you genuinely need ADF fidelity (e.g. editing |
  | a description that already contains inline images — a markdown overwrite   |
  | drops the media), build the ADF JSON and pass `contentFormat: adf`         |
  | instead of hand-writing wiki markup.                                       |

---

## Done when

- [ ] Root cause stated with `file:line` (not just a symptom).
- [ ] Diff is minimal — only intended lines, no stray reformatting.
- [ ] Real test output shown; the original repro now passes; lint clean.
- [ ] Branch rebased on the latest `origin/main`; PR base `main`, not
      `CONFLICTING`.
- [ ] PR body has Problem / Root cause / Fix / Tests / Out-of-scope.
- [ ] High-effort review ran via `review-pr` (partition-by-file →
      cross-file → blind verify → route by agreement): findings posted as
      **inline PR comments**, every finding **fixed (with a regression
      test) or deferred with a written rationale + owner**, and a ✅/❌
      verdict comment is on the PR.
- [ ] Review **iterated**: after fixes, `review-pr` re-ran over the updated
      diff and re-verdicted; looped until a pass found no new must-fix
      (≤3 passes). Final verdict records the pass count.
- [ ] PR link commented on the Jira ticket.

## Gotchas recap (one-liners)

- Wrong `gh` account → "Repository not found"; fix: `gh auth switch --user <your-account>` (the login with repo access; `gh auth status` lists them).
- Jira comments → `contentFormat: markdown` + **real Markdown** (`###`, `` `code` ``, `**bold**`, `[t](url)`); wiki markup (`h3.`, `{{}}`, `*b*`, `[t|url]`) renders as literal text.
- Editing a Jira description with screenshots → use ADF, or the inline media is dropped.
- Every `TODO(project)` above is a real gap, not decoration — fill them in
  before pointing `ultracode-live-engineer`'s unattended loop at this skill.
