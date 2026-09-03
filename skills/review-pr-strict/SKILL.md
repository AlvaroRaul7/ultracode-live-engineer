---
name: review-pr-strict
description: Stricter fork of review-pr — use only when explicitly asked for a "strict review", "high-bar review", "review-pr-strict", or when invoked programmatically by name (e.g. by the ultracode-live-engineer loop). For a plain "review this PR" / "review PR #n" request, use the team-shared review-pr skill instead — this fork is not the default.
---

# Review PR (strict)

> **Fork note:** this is a personal, stricter variant of the team-shared
> `review-pr` skill — forked instead of edited in place so the team's default
> stays unchanged. Structurally identical (same 7 phases, same worktree
> isolation, same finder angles); the divergence is entirely in **how much
> gets escalated to the fuller machine and how hard verification/CI trust is
> pushed**. If `review-pr`'s core process (phases, finder angles, posting
> mechanics) changes, port those structural changes here too — only the
> strictness knobs below are meant to diverge.

## Overview

Review a real GitHub PR end-to-end: understand it, establish its **true** scope,
read the correctness-critical code with full context, hunt for defects with
**independent adversarial agents that did not write the code**, verify each
finding, and post a **grouped review with inline comments** anchored to real
lines.

**Core principle:** a review is only as good as (a) reviewing the _actual_ diff
(not a stale one) and (b) findings that survive an adversarial verify. Never
sign off on a skim.

**Model:** run every sub-agent on the **default (session) model** — pass no
`model:` override. Keep the highest-risk correctness core (the predicate, the
state-flag lifecycle, the money path) in the main context yourself; delegate
exhaustive per-file coverage and blind verification to sub-agents.

**Scale the machine to the PR (Phase 3 sizes it).** Fan-out exists because one
context cannot hold a big diff — that premise is false for a small PR, and paying
the full pipeline anyway is the main way this review gets slow for no added
signal. Small PRs are reviewed **in the main context with zero sub-agents**. Only
PRs too big to hold at once earn the four-stage machine (Phases 4–5): **①
partition by file** so every changed path is actually read → **② a cross-file
pass** for integration bugs + refuting single-file false positives + dedup → **③
a blind adversarial verify** (verifiers never see the finder's confidence) → **④
route by verifier _agreement_, not self-reported confidence** (agreed-real →
inline; split-verdict → a collapsed section, never silently dropped).

**Never spawn one agent per finding.** A verifier is blind because it is _fresh
and didn't raise the claim_ — not because it runs alone. One batched verifier
handles the whole candidate list at once (Stage 3).

## When to use

- An explicit "strict review" / "high-bar review" / "review-pr-strict" request.
- Programmatic invocation by name (e.g. the ultracode-live-engineer loop's
  automated Slack-tagged PR reviews, which always want the higher bar since no
  human is in the loop to catch what a lighter pass misses).
- **vs `review-pr`:** same skill, tuned stricter — smaller tier thresholds
  (more PRs earn the full machine), a wider set of forced-Standard-minimum
  categories, a second verifier pass on Standard tier too (not just Deep), and
  less willingness to trust a green CI run or an unreproduced flake on
  higher-risk PRs. Use plain `review-pr` for everyday interactive reviews.
- **vs the built-in `code-review` skill:** `code-review` is the _lighter_ pass — it
  can review the local working-tree diff (`Skill("code-review", args="high")`) or
  post a single pass to a PR (`Skill("code-review", args="high --comment PR #<n>")`).
  Reach for **this** skill when you want the thorough treatment a pushed PR
  deserves: true-scope establishment, worktree isolation, exhaustive
  partition-by-file coverage, a cross-file pass, and blind agreement-based routing.
  For an uncommitted local tree, use `code-review` — this skill needs a pushed branch.

## Phase 1 — Understand the PR

- `gh pr view <n> --repo <owner>/<repo> --json number,title,state,author,baseRefName,headRefName,additions,deletions,changedFiles,body`
- Read the body: what tickets, what root cause, what the author claims changed,
  the test plan, and any "design decisions worth flagging". These claims are
  **hypotheses to verify**, not facts.
- Wrong-account guard: a `404`/"Not Found" from `gh` on a repo you _can_ open in
  the browser means `gh` is authenticated as an account without access — switch to
  the gh account that has push access to this repo:
  `gh auth switch --user <your-account>` (`gh auth status` lists your accounts,
  `gh api user --jq .login` shows the active one). This is per-user setup, not a
  fixed account — use whichever of your logins can see the repo.

## Phase 2 — Establish the TRUE scope (do this before reading any diff)

The single most common review error is reviewing the wrong diff. A stale local
`main` makes a two-dot diff show changes that are **already merged** — you will
"find" scope creep that isn't there.

1. `git fetch origin main <head-branch>`.
2. Use the **three-dot** diff — it is exactly what GitHub shows:
   `git diff --stat origin/main...origin/<head-branch>`. Confirm the file count
   matches `gh pr view`'s `changedFiles`. If it doesn't, your base is stale — do
   NOT proceed on the two-dot diff.
3. Before calling anything "unrelated / bundled scope", prove it isn't already on
   main: `git grep -l "<NewSymbol>" origin/main -- <path>`. If main already has
   it, it is not this PR's change — drop it.

**Do all per-file diffs as `git diff origin/main...<head-branch> -- <path>`.**

## Phase 3 — Size the review, then isolate in a worktree

### Pick the tier (from the Phase-2 `--stat`, before dispatching anything)

**Size on PRODUCTION lines, not total.** Test lines are an order of magnitude
cheaper to review than production lines — they have no callers, no runtime
behaviour, and no blast radius, so a finder reads them far faster and a mistake
in them costs a red suite rather than an incident. Counting them toward the tier
is the single most common way this review is over-provisioned: a 350-line fix
with 1200 lines of tests is a **Standard** review, not a Deep one.

Split the count before you pick:

```bash
# production lines (the number that picks the tier)
git diff --numstat origin/main...origin/<head-branch> \
  | grep -vE '(^|/)(tests?|__tests__|spec)/|_test\.|\.test\.|test_.*\.py' \
  | awk '{a+=$1; d+=$2} END {print a+d " changed production lines, " NR " files"}'
# then the same without the grep -v for the total, so you can state both
```

**Tightened from review-pr's defaults — this fork earns the fuller machine
sooner:**

| Tier         | When (**production** files / lines)                                                                                                | How you review it                                                                                                                                                                                                                                             |
| ------------ | ---------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Quick**    | ≤3 files **and** ≤80 lines, no migration / CI / IaC / lockfile / any forced-escalation category below                              | **No sub-agents.** Read the full diff + every enclosing function yourself, apply all 7 finder angles in the main context, self-refute each candidate by quoting the code that would disprove it, post.                                                        |
| **Standard** | ≤12 files or ≤400 lines                                                                                                            | 2–4 finders (Stage 1). **You** do Stage 2 in the main context — you already hold the whole diff. **Two** batched verifiers (Stage 3): one over the full list, one over just the `[HIGH]`/`[MEDIUM]` set — same double-verify as Deep, not just a single pass. |
| **Deep**     | Bigger than Standard, **or** the user said "thorough"/"deep"/"ultra", **or** the diff touches any forced-escalation category below | The full four-stage machine, second batched verifier over the `[HIGH]`/`[MEDIUM]` set.                                                                                                                                                                        |

Tests still need a reader — coverage is non-negotiable (Phase 4) — they just
don't buy a bigger machine. On Standard, give the test files their own finder
bucket; on Quick, read them yourself.

Two escalation rules, so the cheap tiers stay honest:

- **A forced-escalation category forces Standard minimum**, no matter how few
  lines — that is exactly where a small diff hides silent breakage. The
  categories: **migration, CI workflow, IaC, auth, payment/billing, external
  API/webhook integration, background jobs/queues/schedulers,
  permissions/authorization (Cedar or equivalent), secrets/credential
  handling, and any change to a public API's request/response contract.**
- **Escalate mid-review, don't restart:** if the Quick pass finds the diff is
  wider than the `--stat` suggested (a small hunk in a load-bearing module, an
  unfamiliar subsystem), promote to Standard and dispatch finders for the parts
  you haven't covered. The tier is an opening bid, not a contract.

State the chosen tier in one line before proceeding, so the user can override it.

### Isolate in a worktree (all tiers — never disturb the user's tree)

The user's checkout is often dirty / on another branch. Review in an isolated
worktree (~1s; this is not where the time goes — keep it even on Quick):

```bash
WT=<scratchpad>/wt-<n>
git worktree add --detach "$WT" origin/<head-branch>
# ... read files in $WT for full context (not just diff hunks) ...
git worktree remove --force "$WT"   # clean up when done
```

Read whole functions/modules in the worktree — a diff hunk without its enclosing
function hides the bug.

## Phase 4 — Find: partition by file (Stage 1), then a cross-file pass (Stage 2)

**Quick tier does this phase in the main context — skip to the angles list, apply
all 7 yourself, then go to Phase 5.** The rest of Phase 4 is the Standard/Deep
fan-out.

The two failures this phase exists to prevent: a changed file that **nobody
actually read**, and a bug that lives **between** files that no single-file view
can see. Both are coverage failures — they are not solved by _more_ agents, only
by every path being assigned to _someone_ (a bucket, or you).

### Stage 1 — Partition & find (every path assigned exactly once)

Take the **exact file list** from Phase 2 — not a re-derived one:

```bash
git diff --name-only origin/main...origin/<head-branch>
```

Assign **every** path to exactly one finder and keep a coverage ledger — before
you dispatch, confirm the union of the buckets equals this list. **Nothing falls
through by "area":** bundle the boring-but-risky files (CI workflows, DB
migrations, Terraform/config, lockfiles, generated clients) into their own bucket
and review them too — a dropped migration or a CI/permissions change is exactly
where silent breakage hides. Group the rest by cohesion (a module _with_ its
tests), not by a vague area label.

Dispatch the finders **in parallel** (default model, read-only, background) —
**2–4 on Standard, as many as the buckets need on Deep.** More finders than
buckets is waste; a finder per file is waste.

**Check finder liveness; never block indefinitely on one.** A hung finder is
indistinguishable from a slow one until you look, and waiting on it can cost more
wall-clock than the entire rest of the review. While the others are running, poll
the stalled one's output file — size and mtime only, **never read it** (it is the
full subagent transcript and will flood your context):

```bash
stat -f '%z bytes, mtime %Sm' -t '%H:%M:%S' <output-file>   # macOS
```

A file still at a few hundred bytes with an mtime several minutes old is dead or
wedged, not thinking. **At ~8 minutes of no growth, give up on it and relaunch
that bucket** with a tighter, explicitly time-boxed prompt ("target ~8 minutes";
name the exact helpers to read). Do not wait for a straggler before starting
Stage 2 on the buckets that did return — and if a late finder lands after you've
posted, don't silently drop it: verify anything genuinely new yourself and post a
short addendum comment, clearly marked as arriving after the review.

**Each finder must read its assigned files with full enclosing context** — never
an excerpt/keyword scan. The `Explore` agent defaults to reading _excerpts to
locate_ code, which is the opposite of what a review needs; use a
general-purpose read agent and instruct it explicitly. Bound the reading so a
single huge module doesn't eat the pass:

- **File ≤600 lines → read it end to end**, then diff against `origin/main...<head>`.
- **Larger → read every hunk's enclosing function/class in full, plus the module's
  imports and public surface**, and say so in the return. Full-file reads on a
  3000-line module for a one-line hunk are the token cost with the worst
  signal-per-second in this skill.

Give each finder the **worktree path** + the **three-dot base**.

Each finder applies the angles below to its files and returns candidates —
`file:line`, a one-line claim, and a concrete failure scenario each. Finders
**surface every borderline candidate** (Stage 3 filters) and do **not** self-rank
by confidence — Stage 4 routes on independent agreement, not on how sure a finder
sounded.

Finder angles:

1. **Line-by-line scan** — every hunk + its enclosing function; what
   input/state/timing makes each line wrong?
2. **Removed/claimed-behavior audit** — for deletions: which invariant died,
   where is it re-established? For "mirrors X" code: diff against the template and
   find where the mirror silently diverges.
3. **Cross-file tracer** — verify every imported symbol/kwarg/exception against
   the REAL source (never memory); check callers, packaging/deploy inclusion,
   test monkeypatch targets, and whether a new field/flag is actually _consumed_.
4. **Reuse** — does the codebase already have this helper? Count the copies the
   PR creates.
5. **Simplification** — dead branches, derivable state, near-duplicate bodies.
6. **Efficiency** — I/O calls vs data needed, caching, redundant paid calls,
   un-truncated payloads measured against real data.
7. **Altitude** — shadow-schemas duck-typing a contract, unregistered wire
   shapes, bandaids on shared infrastructure, feature flags wired in code but not
   in infra (or vice versa).

### Stage 2 — Cross-file pass (integration + refute + dedup)

**Do this yourself in the main context by default** — you already hold the whole
diff, so delegating it buys nothing but a round-trip. Hand it to a dedicated
agent only on Deep tier when the diff genuinely doesn't fit. The pass:

- **finds integration bugs** a per-file finder structurally cannot: a caller/callee
  signature or contract mismatch, a new field written but never consumed, a wire
  shape the other side rejects, a flag set in code but not wired in infra;
- **refutes** Stage-1 candidates the wider context disproves — a "missing guard"
  that an adjacent file already enforces (quote the guard); these die here, before
  they ever reach a verifier;
- **dedups** near-duplicates (same defect, same root, different file) into one.

Output: a single consolidated candidate list.

## Phase 5 — Verify blind (Stage 3), then route by agreement (Stage 4)

### Stage 3 — Blind adversarial verify (batched)

Every surviving candidate is verified by a **fresh agent that did NOT raise it and
does NOT see the finder's severity or confidence** — only the claim and the code —
returning **CONFIRMED / PLAUSIBLE / REFUTED** with `file:line` evidence per
candidate. REFUTED only when provable from the code (quote the line).

**Batch it: one verifier agent takes the whole consolidated list, not one agent
per finding.** Blindness comes from _who_ verifies (fresh, didn't write the
claim, confidence stripped from the prompt), not from process isolation — so a
per-finding fan-out costs N× the wall-clock and buys nothing. Concretely:

- **Quick:** no verifier agent. Self-refute in the main context by quoting the
  code that would disprove each candidate; anything you can't refute goes on.
- **Standard:** **two** batched verifiers — one over the full list, one over just
  the `[HIGH]`/`[MEDIUM]` set — same double-verify as Deep (this fork drops the
  single-verifier Standard tier from plain `review-pr`: agreement is a real
  signal wherever a wrong post is expensive, and Standard is no longer assumed
  cheap enough to skip it).
- **Deep:** one batched verifier over the full list, plus a **second** batched
  verifier over just the `[HIGH]`/`[MEDIUM]` set — so agreement is a real signal
  exactly where a wrong post is expensive.
- **Zero candidates → skip Stage 3 entirely** and post the clean verdict. (The
  PR-body-claims verification and mutation-testing below still run regardless —
  there's nothing to verify blind when no candidate was raised, so this
  shortcut isn't a rigor gap.)

Strip severity/confidence from the candidate list before handing it over — that
is what makes the pass blind, and it survives batching intact.

Also, independently of the candidate list: verify the **PR body's riskiest claims**
against the code, and — if the PR touches tests — prove **non-vacuity** by mutation
(flip the guard the test pins; the test must go red). For an "X clears on
completion" claim, trace the set-site and the clear-site: same condition? Decoupled
set/clear is a classic latent bug.

**Mutation is the only thing here that needs local execution — a suite run is
not, usually.** CI answers "do the tests pass"; it cannot answer "would this test
fail if the fix were reverted", which is where every finding with teeth comes
from. So:

```bash
gh pr checks <n> --repo <owner>/<repo>    # do this BEFORE running anything
```

- **A green CI job covers the suite AND the PR is Quick/Standard tier on an
  ordinary change (no forced-escalation category) → do not re-run it.**
  Reproducing a green run costs minutes and buys nothing in that case.
  Mutate the specific tests you're judging and stop there.
- **Deep tier, OR the PR touches money, auth, migrations, or a state chart →
  run the affected tests locally yourself even if CI is green.** A single green
  CI run doesn't rule out environment-dependent non-determinism, and this is
  exactly the risk tier where that gap matters. Run the **base branch too** in
  a second worktree — "the branch's failures are a strict superset of main's"
  is the only clean verdict; a bare pass/fail isn't.
- **No CI job runs these tests, or it's red → always run it**, base branch
  baselined the same way.
- **A PR body that argues pass/fail counts by hand** (tables, failing-name-set
  comparisons, "please don't read a red suite as a regression") is itself the
  signal that CI doesn't cover this suite. Check, don't assume.

**Flake cap: 2 runs on Quick/Standard, 3 runs on Deep (or any forced-escalation
category) before declaring unreproduced** — the higher-risk tier gets one more
attempt before benefit of the doubt, since a real intermittent bug there is more
expensive to miss. If it still doesn't reproduce: record it in the review body
as an unreproduced observation with the run count and what else was running —
but on Deep tier / a forced-escalation category, tag that observation
**`[LOW] confirm before merge`** in the summary body instead of a purely passive
aside, so it doesn't get lost as "just a note." Never assert a flake you can't
reproduce as a defect; never re-run past the cap chasing it either.

### Stage 4 — Route by verdict, never by self-reported confidence

**Self-assessed confidence tracks how _plausible_ a finding sounds, not whether
it's _true_ — so routing on confidence auto-posts exactly the confident-but-wrong
ones.** Route on independent-verifier **agreement** instead:

On Quick, the single self-refute pass _is_ the routing signal. On Standard and
Deep (both now double-verified in this fork), "agreement" is a real cross-check:

- **Agreed real** (CONFIRMED, or all verifiers PLAUSIBLE) → post **inline**
  (Phase 6), ranked by severity. Cap the inline set ~8–10; correctness outranks
  cleanup when cutting.
- **Split verdict** (verifiers disagree — e.g. one CONFIRMED, one REFUTED) →
  **never drop and never auto-post inline.** Surface it in a **collapsed
  `<details>` section** of the review body ("Split-verdict — reviewer judgement
  needed"), carrying both verdicts + the evidence, so a human decides. This
  section is uncapped — it costs nothing and never silently loses a finding.
- **Agreed refuted** → drop (optionally a one-liner in the collapsed section if it
  looked plausible, so the author sees it was considered and why it's not real).

## Phase 6 — Post the review (grouped, inline, anchored)

Post **one grouped review** with a summary body + inline comments. Default the
event to **`COMMENT`** — do **not** `APPROVE`/`REQUEST_CHANGES` on the user's
behalf unless they explicitly ask; offer it instead.

**Anchor every inline comment to a line that is in the diff (an added `+` line).**
Get the new-side line number with:

```bash
# NOTE the `--` : `git show "$BR:path"` breaks in zsh because ":a"/":h"/":t"
# after $BR are treated as history modifiers. `git grep ... BR -- path` avoids it.
git grep -n "<unique added-line pattern>" origin/<head-branch> -- <path>
```

Build the payload with a script (never hand-escape JSON), then post:

```python
# build_review.py — dump to review_payload.json
import json
json.dump({
  "commit_id": "<head sha>",           # git rev-parse origin/<head-branch>
  "event": "COMMENT",
  "body": "<summary: verdict + counts + non-blocking notes>",
  "comments": [
    {"path": "<file>", "line": <n>, "side": "RIGHT", "body": "<[SEV] finding + fix>"},
    # ...
  ],
}, open("review_payload.json","w"), indent=2)
```

```bash
gh api --method POST repos/<owner>/<repo>/pulls/<n>/reviews --input review_payload.json \
  --jq '{id,state,url:.html_url}'
# 404 here = gh is on an account without repo access → gh auth switch --user <your-account>, retry.
```

A single review is atomic: **one bad line rejects the whole thing (422)**. If
unsure a line is in the diff, either verify it first or post that comment
standalone via `.../pulls/<n>/comments` (partial success tolerated).

**Verify the comments landed:** `gh api "repos/<owner>/<repo>/pulls/<n>/comments?per_page=100"
--jq '.[] | select(.pull_request_review_id==<id>) | "\(.path):\(.line)"'`.

Each **inline** body (the Stage-4 agreed-real findings): severity tag
(`[MEDIUM]`/`[LOW]`/`[nit]`), the failure scenario, and a one-line fix direction.
The **summary body**: overall verdict, the finding counts, non-blocking notes
(pre-existing limits, nits), **and the collapsed `<details>` split-verdict section
from Stage 4** — those go in the review body, not inline, so they're surfaced for
human judgement without asserting a defect the verifiers disagreed on.

## Phase 7 — Resolve (only when it's YOUR PR and you're fixing)

Reviewing someone else's PR ends at Phase 6 — post and stop; the author resolves.
When it's your own PR and you will fix: apply `Skill("superpowers:receiving-code-review")`
rigor (verify each finding against the code first), fix in a follow-up commit
with a **regression test per fix** (keeps inline comments anchored), then
**re-review the new commit** and re-verdict — loop until a pass finds no new
must-fix (cap 3 passes). **Re-size the follow-up diff in Phase 3:** a fix commit
is almost always Quick tier, so the re-review is a main-context read of the new
hunks plus a check that each original finding is actually resolved — not another
full fan-out over the whole PR.

## Gotchas recap

- **Stale base → phantom scope.** Always three-dot `origin/main...<head>`; confirm
  file count vs `gh pr view`; `git grep origin/main` before calling anything new.
- **Wrong gh account → 404.** Switch to the login with repo access:
  `gh auth switch --user <your-account>` (`gh auth status` lists them).
- **zsh eats `$BR:path`** (`:a` modifier). Use `git grep … BR -- path`, or quote
  differently; verify the ref actually resolved.
- **Atomic review 422** if any inline line isn't in the diff — anchor on added
  lines only.
- **Coverage:** every path in the Phase-2 file list is assigned to exactly one
  reader — a Stage-1 finder, or you on Quick tier. CI / migrations / config
  included; nothing falls through.
- **This fork earns Standard/Deep sooner** — tighter tier thresholds and a wider
  forced-escalation category list than plain `review-pr`. Still say which tier
  you chose.
- **Test lines don't buy a bigger tier.** Size on production lines; a big test
  diff is the most common reason a Standard review gets over-provisioned to Deep.
- **Standard is double-verified in this fork** — don't skip the second
  `[HIGH]`/`[MEDIUM]` verifier pass just because it's not Deep tier.
- **Check `gh pr checks` before running any suite** — but on Deep tier / a
  forced-escalation category, run the affected tests locally yourself even on
  green CI, baselined against the base branch.
- **Flake cap is tier-dependent: 2 runs (Quick/Standard), 3 runs (Deep /
  forced-escalation).** An unreproduced flake on the higher tier is tagged
  `[LOW] confirm before merge`, not a silent aside.
- **A stalled finder is dead, not slow.** Poll size/mtime (never read the
  transcript); relaunch the bucket at ~8 min of no growth rather than blocking.
- **One agent per finding is the slowness bug.** Batch the verify; the blindness
  comes from a fresh agent with confidence stripped, not from N processes.
- **Route by agreement, not confidence** — split verdicts go in the collapsed
  section, never silently dropped.
- **Don't approve/block for the user** — default `COMMENT`, offer the formal verdict.
- **Clean up the worktree** (`git worktree remove --force`).

## Done when

- [ ] Reviewed the three-dot diff; file count matches GitHub; no phantom scope.
- [ ] **Tier chosen and stated** (Quick / Standard / Deep), sized on **production**
      lines against this fork's tightened thresholds, with the widened
      forced-escalation list applied.
- [ ] **`gh pr checks` consulted before any local suite run**; suite re-run where
      CI doesn't cover it, or unconditionally on Deep/forced-escalation tier even
      if CI is green, baselined against the base branch.
- [ ] **Coverage:** every path in the Phase-2 list read by exactly one reader —
      a Stage-1 finder, or you on Quick — CI / migrations / config included, with
      each hunk's enclosing function.
- [ ] **Stage 2:** a cross-file pass ran (you, or an agent on Deep) — integration
      bugs found, single-file false positives refuted, duplicates merged.
- [ ] **Stage 3:** every surviving candidate verified blind (confidence stripped),
      `file:line` evidence each — **batched**, and **double-verified on both
      Standard and Deep**, not just Deep.
- [ ] **Stage 4:** routed by verdict / agreement — real ones inline, split
      verdicts in the collapsed section, nothing silently dropped.
- [ ] Posted one grouped review (event `COMMENT` unless asked) with inline
      comments anchored to real lines + a summary verdict; verified they landed.
- [ ] Worktree removed; scratch files left only in the scratchpad.
