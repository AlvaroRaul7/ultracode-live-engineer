export const meta = {
  name: "ultracode-live-engineer",
  description:
    "One autonomous pass of the unattended Jira-implement and Slack-PR-review loop",
  whenToUse:
    "Fired once per wake by the ultracode-live-engineer skill (itself invoked by /loop). Not for interactively working a single ticket or reviewing one PR.",
  phases: [
    { title: "Slack Review" },
    { title: "PR Follow-up" },
    { title: "Jira Selection" },
    { title: "Implement" },
  ],
};

// ---------------------------------------------------------------------------
// Config — this workflow is shareable across projects/people: every identity
// value comes from config.json, loaded by the driver skill (SKILL.md, which
// has real filesystem access) and passed in as `args` — this script itself
// has no filesystem access, per the Workflow tool's constraints.
// ---------------------------------------------------------------------------

// args may arrive as a parsed object OR as a JSON-encoded string depending on invocation path
// (confirmed empirically, 2026-08-12: a live Workflow() call with args passed as an object still
// produced typeof args === 'string' inside the script) — parse defensively so this works either way.
let cfg;
if (typeof args === "string") {
  try {
    cfg = JSON.parse(args);
  } catch (e) {
    cfg = null;
  }
} else {
  cfg = args;
}
const REQUIRED_CONFIG_KEYS = [
  "repo",
  "bot_github_login",
  "slack_channel_id",
  "slack_channel_name",
  "human_slack_id",
  "jira_project_key",
  "max_concurrent_tickets",
  "branch_prefix",
  "jira_statuses",
  "skills",
  "cache_dir",
];
const REQUIRED_NESTED_CONFIG_KEYS = [
  "jira_statuses.in_development",
  "jira_statuses.in_review",
  "jira_statuses.on_hold",
  "skills.ticket_to_pr",
  "skills.pr_review",
  "skills.request_review",
];

function resolveDottedPath(obj, dottedPath) {
  return dottedPath
    .split(".")
    .reduce((o, key) => (o == null ? o : o[key]), obj);
}

const RULES = ".claude/workflows/ultracode-live-engineer/pass_rules.py";

// ---------------------------------------------------------------------------
// Schemas
// ---------------------------------------------------------------------------

const SCAN_CHANNEL_SCHEMA = {
  type: "object",
  required: ["definite_candidates", "needs_thread_check"],
  properties: {
    definite_candidates: {
      type: "array",
      items: {
        type: "object",
        properties: {
          message_ts: { type: "string" },
          pr_numbers: { type: "array", items: { type: "number" } },
        },
      },
    },
    needs_thread_check: {
      type: "array",
      items: {
        type: "object",
        properties: {
          message_ts: { type: "string" },
          thread_reply_count: { type: ["number", "null"] },
          thread_latest: { type: ["string", "null"] },
        },
      },
    },
  },
};

const SCAN_THREAD_SCHEMA = {
  type: "object",
  required: ["is_candidate", "pr_numbers"],
  properties: {
    is_candidate: { type: "boolean" },
    thread_ts: { type: ["string", "null"] },
    pr_numbers: { type: "array", items: { type: "number" } },
  },
};

const GH_FILTER_SCHEMA = {
  type: "object",
  required: ["unhandled", "skipped"],
  properties: {
    unhandled: {
      type: "array",
      items: {
        type: "object",
        properties: {
          pr: { type: "number" },
          title: { type: "string" },
          url: { type: "string" },
          re_review: { type: "boolean" },
        },
      },
    },
    skipped: {
      type: "array",
      items: {
        type: "object",
        required: ["pr", "reason"],
        properties: { pr: { type: "number" }, reason: { type: "string" } },
      },
    },
  },
};

const REVIEW_RESULT_SCHEMA = {
  type: "object",
  required: ["posted_event", "finding_count", "summary"],
  properties: {
    posted_event: { type: "string", enum: ["APPROVE", "COMMENT"] },
    finding_count: { type: "number" },
    summary: { type: "string" },
  },
};

const HOLD_SCHEMA = {
  type: "object",
  required: ["held"],
  properties: {
    held: { type: "boolean" },
    matched_phrase: { type: ["string", "null"] },
    reason: { type: ["string", "null"] },
  },
};

const FLIGHT_SCHEMA = {
  type: "object",
  required: ["in_flight_count", "resumable"],
  properties: {
    in_flight_count: { type: "number" },
    resumable: {
      type: "array",
      items: {
        type: "object",
        required: ["key", "reply"],
        properties: { key: { type: "string" }, reply: { type: "string" } },
      },
    },
    on_hold_count: { type: ["number", "null"] },
    oldest_on_hold_key: { type: ["string", "null"] },
  },
};

const RESUME_TRANSITION_SCHEMA = {
  type: "object",
  required: ["transitioned"],
  properties: {
    transitioned: { type: "boolean" },
    reason: { type: ["string", "null"] },
  },
};

const SELECTION_SCHEMA = {
  type: "object",
  required: ["ticket_keys"],
  properties: {
    ticket_keys: { type: "array", items: { type: "string" } },
  },
};

// Batched over ALL fresh-selection candidates in one agent() call instead of one call per
// candidate — check-stale-branch is pure Bash (no MCP, no judgment), so paying the fixed
// per-agent-call overhead once instead of once per candidate is a clear latency win.
const STALE_BATCH_SCHEMA = {
  type: "object",
  required: ["results"],
  properties: {
    results: {
      type: "array",
      items: {
        type: "object",
        required: ["key", "stale"],
        properties: {
          key: { type: "string" },
          stale: { type: "boolean" },
          branch: { type: ["string", "null"] },
        },
      },
    },
  },
};

const IMPLEMENT_SCHEMA = {
  type: "object",
  required: ["status"],
  properties: {
    status: { type: "string", enum: ["success", "escalated", "failed"] },
    pr_url: { type: ["string", "null"] },
    reason: { type: ["string", "null"] },
  },
};

// Merges what used to be list-owned-open-prs (1 call) + pr-feedback (1 call per owned PR) into a
// single agent() call — both are pure Bash/gh CLI, no MCP, no judgment, so there's no reason to
// pay the fixed per-agent-call overhead N+1 times instead of once.
const OWNED_PRS_WITH_FEEDBACK_SCHEMA = {
  type: "object",
  required: ["prs"],
  properties: {
    prs: {
      type: "array",
      items: {
        type: "object",
        required: ["number", "title", "url", "headRefName", "feedback"],
        properties: {
          number: { type: "number" },
          title: { type: "string" },
          url: { type: "string" },
          headRefName: { type: "string" },
          updatedAt: { type: "string" },
          feedback: {
            type: "object",
            required: [
              "unaddressed_review_threads",
              "unaddressed_reviews",
              "unaddressed_issue_comments",
              "failing_checks",
            ],
            properties: {
              unaddressed_review_threads: { type: "array" },
              unaddressed_reviews: { type: "array" },
              unaddressed_issue_comments: { type: "array" },
              failing_checks: { type: "array" },
            },
          },
        },
      },
    },
  },
};

const PR_FOLLOWUP_SCHEMA = {
  type: "object",
  required: ["status"],
  properties: {
    status: { type: "string", enum: ["fixed", "escalated", "clean", "failed"] },
    summary: { type: ["string", "null"] },
    commit_sha: { type: ["string", "null"] },
  },
};

// ---------------------------------------------------------------------------
// Crash safety — everything below runs inside one try/catch. A thrown error
// (missing config key, unguarded null dereference, etc.) is caught here
// instead of propagating out as an undocumented task failure with no logged
// trace (confirmed live, 2026-08-12: implementOutcome.status on a null
// crashed the whole pass with nothing written to the pass log, since that
// line only ran at the very bottom on a clean path). All state the final
// summary/log needs is hoisted to `let` here with safe defaults, so a crash
// at any point still produces a well-formed, honest summary.
// ---------------------------------------------------------------------------

let crashed = false;
let crashedPhase = null;
let crashedError = null;

let reviewOutcomes = [];
let followUpOutcomes = [];
let onHoldCount = null;
let oldestOnHoldKey = null;
let escalatedTicketKeys = [];
// {key, resumedReply: string | null} — resumedReply is set only for resumed (not freshly selected) tickets
let ticketsToImplement = [];
let implementResults = []; // {key, outcome: IMPLEMENT_SCHEMA result | null}

// Slack Review, PR Follow-up, and Jira Selection are mutually independent (none reads another's
// output), so they run concurrently via parallel() below instead of one after another. Each is
// wrapped by runPhase so a crash in one doesn't abort the other two — every phase-scoped failure
// is recorded here instead of thrown, and folded into the top-level crashed/crashedPhase/
// crashedError after the parallel() call resolves.
let phaseCrashes = []; // {phase, error}

async function runPhase(title, fn) {
  phase(title);
  try {
    await fn();
  } catch (err) {
    const message = err && err.message ? err.message : String(err);
    phaseCrashes.push({ phase: title, error: message });
    log(`Pass phase "${title}" CRASHED: ${message}`);
  }
}

try {
  const missingKeys = REQUIRED_CONFIG_KEYS.filter(
    (k) => !cfg || cfg[k] === undefined || cfg[k] === null,
  );
  const missingNestedKeys = REQUIRED_NESTED_CONFIG_KEYS.filter((k) => {
    const value = resolveDottedPath(cfg, k);
    return value === undefined || value === null;
  });
  const allMissing = [...missingKeys, ...missingNestedKeys];
  if (allMissing.length > 0) {
    throw new Error(
      `config.json missing required key(s): ${allMissing.join(", ")}`,
    );
  }

  const SLACK_CHANNEL_NAME = cfg.slack_channel_name;
  const SLACK_CHANNEL_ID = cfg.slack_channel_id;
  const HUMAN_SLACK_ID = cfg.human_slack_id;
  const REPO = cfg.repo;
  const PR_URL_BASE = `https://github.com/${REPO}/pull/`;
  const MAX_CONCURRENT_TICKETS = cfg.max_concurrent_tickets;
  const JIRA_PROJECT_KEY = cfg.jira_project_key;
  const JIRA_STATUS_IN_DEVELOPMENT = cfg.jira_statuses.in_development;
  const JIRA_STATUS_IN_REVIEW = cfg.jira_statuses.in_review;
  const JIRA_STATUS_ON_HOLD = cfg.jira_statuses.on_hold;
  const SKILL_TICKET_TO_PR = cfg.skills.ticket_to_pr;
  const SKILL_PR_REVIEW = cfg.skills.pr_review;
  const SKILL_REQUEST_REVIEW = cfg.skills.request_review;

  const GUARDRAILS = `Hard guardrails, always: never merge a PR; never force-push; never push
directly to main; no destructive DB/infra commands against shared
environments; never skip hooks or tests (--no-verify forbidden);
${JIRA_PROJECT_KEY} project only; always work in an isolated git worktree,
never the human's live checkout; at most ${MAX_CONCURRENT_TICKETS} tickets
in flight at once.`;

  // De-duplicated escalation prompt: the old version repeated this near-identically three times
  // (escalate-stale, escalate-implement, resume-transition-failed) — one helper, one place to fix
  // the escalation pattern if it ever needs to change.
  function buildEscalationPrompt(
    ticketKey,
    reason,
    { alsoChannel = false } = {},
  ) {
    const channelClause = alsoChannel ? ` or in ${SLACK_CHANNEL_ID}` : "";
    return (
      `Escalate Jira ticket ${ticketKey}: ${reason}. Add a Jira comment (addCommentToJiraIssue) ` +
      `with the ticket key, a one-sentence description of the specific doubt, and — if applicable — ` +
      `the two or three concrete options being weighed. Transition the ticket to "${JIRA_STATUS_ON_HOLD}" ` +
      `(transitionJiraIssue). Best-effort only, don't block on it: also send a Slack message via ` +
      `mcp__claude_ai_Slack__slack_send_message, prefixed "🤖 [ultracode-live-engineer]", as a ` +
      `self-DM to ${HUMAN_SLACK_ID}${channelClause}. Don't retry automatically — this ticket is ` +
      `parked until a human replies.`
    );
  }

  // -------------------------------------------------------------------------
  // Step 1 — Slack-tagged PR review
  // -------------------------------------------------------------------------

  async function runSlackReview() {
    const scan = await agent(
      `Fetch Slack channel ${SLACK_CHANNEL_NAME} (channel ID ${SLACK_CHANNEL_ID}) with ` +
        `mcp__claude_ai_Slack__slack_read_channel. Pipe the returned messages text on stdin into ` +
        `\`python3 ${RULES} scan-channel\` via Bash (run from the repo root). ` +
        `Do NOT judge mentions or PR links yourself — the script does an exact-token match for ` +
        `"<@${HUMAN_SLACK_ID}" and a pull/<n> regex. Return its JSON output verbatim.`,
      {
        label: "scan-channel",
        phase: "Slack Review",
        schema: SCAN_CHANNEL_SCHEMA,
      },
    );
    if (!scan) throw new Error("scan-channel agent returned null");

    const threadChecks = await pipeline(scan.needs_thread_check, (item) =>
      agent(
        `Fetch the Slack thread at ts ${item.message_ts} in channel ${SLACK_CHANNEL_ID} with ` +
          `mcp__claude_ai_Slack__slack_read_thread. Pipe the full thread text on stdin into ` +
          `\`python3 ${RULES} scan-thread\` via Bash (run from the repo root). Never assume a ` +
          `missing mention or PR link is present in the thread — trust only the script's verdict.\n\n` +
          `Then, unless thread_reply_count or thread_latest below is null, persist that verdict for next ` +
          `pass's cache by running \`python3 ${RULES} record-thread-scan '${item.message_ts}' ` +
          `'${item.thread_reply_count}' '${item.thread_latest}' <is_candidate> '<pr_numbers_json>'\` via ` +
          `Bash, substituting <is_candidate> with the scan-thread JSON's own is_candidate field (the ` +
          `literal word true or false) and <pr_numbers_json> with its pr_numbers field re-serialized as ` +
          `JSON — do not invent or re-derive either value, copy them verbatim from the scan-thread output ` +
          `you just got. thread_reply_count for this item is ${item.thread_reply_count} and thread_latest ` +
          `is ${item.thread_latest}; if either is null, skip the record-thread-scan call entirely.\n\n` +
          `Return the scan-thread JSON output verbatim (not the record-thread-scan output).`,
        {
          label: `scan-thread:${item.message_ts}`,
          phase: "Slack Review",
          schema: SCAN_THREAD_SCHEMA,
        },
      ),
    );

    const candidateThreads = [
      ...scan.definite_candidates.map((c) => ({
        thread_ts: c.message_ts,
        pr_numbers: c.pr_numbers,
      })),
      ...threadChecks
        .filter(Boolean)
        .filter((t) => t.is_candidate)
        .map((t) => ({ thread_ts: t.thread_ts, pr_numbers: t.pr_numbers })),
    ];

    const candidatePrNumbers = [
      ...new Set(candidateThreads.flatMap((c) => c.pr_numbers)),
    ];

    let unhandledPrs = [];
    if (candidatePrNumbers.length > 0) {
      const filtered = await agent(
        `Run \`python3 ${RULES} gh-filter '${JSON.stringify(candidatePrNumbers)}'\` via Bash from the ` +
          `repo root. This is the single source of truth for two rules: never review a PR that isn't ` +
          `currently OPEN, and never review one already reviewed by this bot's own GitHub account. ` +
          `Return its JSON output verbatim.`,
        { label: "gh-filter", phase: "Slack Review", schema: GH_FILTER_SCHEMA },
      );
      unhandledPrs = (filtered && filtered.unhandled) || [];
    }

    function threadForPr(prNumber) {
      const match = candidateThreads.find((c) =>
        c.pr_numbers.includes(prNumber),
      );
      return match ? match.thread_ts : null;
    }

    reviewOutcomes = await pipeline(
      unhandledPrs,
      (pr) => {
        // Mechanical gate — run BEFORE any ack/review, never skipped: does a human have an
        // explicit, unresolved objection open in this thread right now? (see
        // ultracode-respect-explicit-hold — this loop used to re-APPROVE a PR on the very next
        // pass after a reviewer said "don't merge it, I have objections" in the same thread the
        // bot was watching, because the review-dispatch prompt below says a prior human reply is
        // "not a substitute for this review — run it regardless", with no carve-out for an
        // explicit objection.) No thread, nothing to check.
        const threadTs = threadForPr(pr.pr);
        if (!threadTs) return Promise.resolve({ held: false });
        return agent(
          `Fetch the Slack thread at ts ${threadTs} in channel ${SLACK_CHANNEL_ID} with ` +
            `mcp__claude_ai_Slack__slack_read_thread. Pipe the full thread text on stdin into ` +
            `\`python3 ${RULES} check-hold\` via Bash (run from the repo root). This is a mechanical, ` +
            `exact-phrase check for an explicit human hold/objection reply (e.g. "don't merge", "still ` +
            `in review", "stop", "hold off") — never judge this yourself from the thread text, trust ` +
            `only the script's verdict. Return its JSON output verbatim.`,
          {
            label: `check-hold:${pr.pr}`,
            phase: "Slack Review",
            schema: HOLD_SCHEMA,
          },
        );
      },
      (hold, pr) => {
        const threadTs = threadForPr(pr.pr);
        if (hold && hold.held) {
          // Held: post a one-line notice (if we haven't already — the ack text itself is what
          // check-hold's next-pass "awaiting reply" branch looks for) and drop this PR from the
          // pipeline entirely — no review, no re-APPROVE, no "Reviewed ..." reply.
          if (
            !threadTs ||
            hold.reason === "awaiting reply since last hold notice"
          ) {
            return Promise.resolve(null);
          }
          return agent(
            `Post a reply in the Slack thread with thread_ts ${threadTs} in channel ${SLACK_CHANNEL_ID} ` +
              `via mcp__claude_ai_Slack__slack_send_message. No bot prefix — read as a normal reply. ` +
              `Message: "Holding off on re-reviewing — noted the concern above. Reply here once it's ` +
              `cleared and this will pick back up next pass."`,
            { label: `slack-hold-ack:${pr.pr}`, phase: "Slack Review" },
          ).then(() => null);
        }
        // Resolve to a non-null sentinel, never null/undefined, when skipping the ack: pipeline()
        // treats a stage resolving to null the same as a stage that threw and drops the item,
        // skipping the review-pr/slack-reply stages entirely.
        if (!pr.re_review) return Promise.resolve("no-ack-needed");
        if (!threadTs) return Promise.resolve("no-thread-for-ack");
        return agent(
          `Post a reply in the Slack thread with thread_ts ${threadTs} in channel ${SLACK_CHANNEL_ID} via ` +
            `mcp__claude_ai_Slack__slack_send_message acknowledging the re-review request before doing the ` +
            `actual work — a short, natural reply like "On it, re-reviewing now." No bot prefix.`,
          { label: `slack-ack:${pr.pr}`, phase: "Slack Review" },
        );
      },
      (_ack, pr) =>
        agent(
          `Run Skill("${SKILL_PR_REVIEW}") against ${PR_URL_BASE}${pr.pr} ("${pr.title}"). Let that skill do ` +
            `the actual review and post GitHub PR comments — don't duplicate its logic. A prior human reply ` +
            `already in the Slack thread (e.g. "approved") is not a substitute for this review — run it ` +
            `regardless. (An EXPLICIT objection/hold, unlike a casual "approved", already stopped this PR ` +
            `before it reached you — see the check-hold gate above; if you're running, that gate cleared.)\n\n` +
            `Explicit standing instruction, overriding the skill's own default of never approving on the ` +
            `user's behalf: if the review's surviving findings are either zero or all [nit]/[LOW] severity ` +
            `(no [MEDIUM]/[HIGH], no unresolved split-verdicts), post the review with event: "APPROVE" ` +
            `instead of the skill's default COMMENT. Any [MEDIUM]/[HIGH] finding or split-verdict present ` +
            `still means COMMENT, not APPROVE.\n\n` +
            `Report back: which event you posted (APPROVE or COMMENT), how many findings survived, and a ` +
            `one-sentence summary.`,
          {
            label: `review-pr:${pr.pr}`,
            phase: "Slack Review",
            schema: REVIEW_RESULT_SCHEMA,
          },
        ),
      (review, pr) => {
        const threadTs = threadForPr(pr.pr);
        const approvedNote =
          review && review.posted_event === "APPROVE"
            ? `Reviewed and approved — only nits, see ${PR_URL_BASE}${pr.pr}`
            : `Reviewed — ${review ? review.finding_count : "?"} findings posted, see ${PR_URL_BASE}${pr.pr}`;
        return agent(
          `Post a reply in the Slack thread with thread_ts ${threadTs} in channel ${SLACK_CHANNEL_ID} via ` +
            `mcp__claude_ai_Slack__slack_send_message. No bot prefix — this should read as a normal reply, ` +
            `not a bot announcement. Message: "${approvedNote}"`,
          { label: `slack-reply:${pr.pr}`, phase: "Slack Review" },
        );
      },
    );
  }

  // -------------------------------------------------------------------------
  // Step 1.5 — Follow up on the bot's own open PRs
  // -------------------------------------------------------------------------

  async function runPrFollowup() {
    const ownedPrsResult = await agent(
      `Run \`python3 ${RULES} list-owned-open-prs\` via Bash from the repo root — a JSON array ` +
        `(possibly empty) of {number,title,url,headRefName,updatedAt}. For EACH PR number in that ` +
        `array, ALSO run \`python3 ${RULES} pr-feedback <number>\` via Bash and attach its JSON output ` +
        `verbatim as that PR's "feedback" field (four lists: unaddressed_review_threads, ` +
        `unaddressed_reviews, unaddressed_issue_comments, failing_checks). Do both commands for every PR ` +
        `in this same turn — this is mechanical, don't judge or re-derive anything, just chain the two ` +
        `commands per PR and merge their output. Report back {"prs": [<each PR object merged with its ` +
        `feedback field>]} (empty array if list-owned-open-prs returned none).`,
      {
        label: "gather-pr-followup-context",
        phase: "PR Follow-up",
        schema: OWNED_PRS_WITH_FEEDBACK_SCHEMA,
      },
    );
    const ownedPrs = (ownedPrsResult && ownedPrsResult.prs) || [];

    followUpOutcomes = await pipeline(ownedPrs, (pr) =>
      agent(
        `${GUARDRAILS}\n\n` +
          `PR ${PR_URL_BASE}${pr.number} ("${pr.title}", branch ${pr.headRefName}) is one of your own open ` +
          `PRs. Its GitHub feedback state (already computed by the script above — trust it, don't ` +
          `re-derive): ${JSON.stringify(pr.feedback)}\n\n` +
          `Separately, ALSO check Slack: search channel ${SLACK_CHANNEL_ID} for the message where this PR ` +
          `was originally announced (it will contain "pull/${pr.number}" in a link) via ` +
          `mcp__claude_ai_Slack__slack_read_channel, then read that message's thread via ` +
          `mcp__claude_ai_Slack__slack_read_thread. A reply in that thread from anyone other than yourself ` +
          `(self-DM/bot account ${HUMAN_SLACK_ID}), posted after your own most recent message in the ` +
          `thread, asking for a change counts as feedback too — this part isn't a mechanical check, use ` +
          `judgment: a reply that's only "approved"/"lgtm"/a reaction is NOT feedback requiring action.\n\n` +
          `If ALL FOUR GitHub feedback lists (including failing_checks) are empty AND there's no ` +
          `actionable new Slack reply: report status "clean" and do nothing else.\n\n` +
          `A non-empty failing_checks list is feedback exactly like a review comment: investigate the ` +
          `failure (\`gh run view <run-id> --log-failed\` or the check's own link), fix the underlying ` +
          `cause with a new commit if it's mechanical (same worktree, same branch, same guardrails as ` +
          `every other fix in this step), or fold it into the "ambiguous — escalate" path below if it ` +
          `isn't. NEVER make a failing check pass by skipping, disabling, or weakening it — that ` +
          `violates the "never skip hooks or tests" guardrail just as much as --no-verify would. ` +
          `Retrying or rerunning a check without a new commit that changes the code responsible for the ` +
          `failure is not a fix — it masks flakiness rather than fixing the cause; only report status ` +
          `"fixed" for a failing check when a new commit sha that actually changes the relevant source ` +
          `is included, and escalate instead if the failure looks intermittent/non-reproducible. On a ` +
          `security/SAST/dependency-scan check specifically: a suppression-style edit (baseline update, ` +
          `exemption comment, ignore-rule) that silences the finding without remediating the underlying ` +
          `vulnerability is NOT a fix either — treat that exactly like the ambiguous case and escalate ` +
          `it for a human decision.\n\n` +
          `If there IS something to act on and it's concrete/implementable (not ambiguous, doesn't need a ` +
          `human decision): checkout the EXISTING branch ${pr.headRefName} in an isolated worktree (never ` +
          `create a new branch or open a new PR for this — every fix here is a new commit on the SAME ` +
          `PR). Implement every requested fix with a minimal diff, run the real test suite for what you ` +
          `touched, re-sync with origin/main (fetch, rebase only if behind, re-test if you had to resolve ` +
          `conflicts), commit, and push (force-with-lease only if you rebased and the branch was already ` +
          `pushed — never a plain --force). Then close the loop on every concern you addressed:\n` +
          `  - For each item in unaddressed_review_threads: reply with \`gh api ` +
          `repos/${REPO}/pulls/${pr.number}/comments/<root_comment_id>/replies -f body="<what changed, ` +
          `citing the commit sha>"\`, then resolve the conversation via GraphQL — first find its thread id ` +
          `with \`gh api graphql -f query='query { repository(owner: "${REPO.split("/")[0]}", name: "${REPO.split("/")[1]}") { ` +
          `pullRequest(number: ${pr.number}) { reviewThreads(first: 100) { nodes { id comments(first: 1) ` +
          `{ nodes { databaseId } } } } } } }'\`, match the node whose comments[0].databaseId equals the ` +
          `root_comment_id, then \`gh api graphql -f query='mutation { resolveReviewThread(input: ` +
          `{threadId: "<thread id>"}) { thread { isResolved } } }'\`.\n` +
          `  - For each item in unaddressed_reviews / unaddressed_issue_comments: reply with \`gh pr ` +
          `comment ${pr.number} --body "<what changed>"\` (one combined reply is fine if several map to ` +
          `the same fix).\n` +
          `  - If the Slack thread had actionable feedback: post one reply in it (no bot prefix) ` +
          `summarizing the fix and citing the commit sha, same tone as a normal review-request reply.\n\n` +
          `Self-timebox this exactly like ticket implementation: if you're not converging, stop and report ` +
          `status "escalated" rather than spinning indefinitely.\n\n` +
          `If the feedback is ambiguous or needs a human decision instead of a mechanical fix: do NOT push ` +
          `anything. Extract the Jira ticket key (a ${JIRA_PROJECT_KEY}-#### pattern) from the PR title or ` +
          `branch name and escalate it exactly like the HITL section elsewhere in this pass — a Jira ` +
          `comment describing the doubt plus the options, transition to "${JIRA_STATUS_ON_HOLD}" — plus a ` +
          `reply in the GitHub thread(s) and/or the Slack thread saying it's parked pending human input. ` +
          `If no ${JIRA_PROJECT_KEY}-#### key is found in the title/branch, still post the GitHub/Slack ` +
          `replies explaining what's blocking, but skip the Jira step.\n\n` +
          `Report back: status "fixed" (one-sentence summary + the new commit sha), status "escalated" ` +
          `(with why), status "clean" (nothing to do), or status "failed" (errored with no clear ` +
          `escalation reason).`,
        {
          label: `pr-followup:${pr.number}`,
          phase: "PR Follow-up",
          isolation: "worktree",
          schema: PR_FOLLOWUP_SCHEMA,
        },
      ),
    );
  }

  // -------------------------------------------------------------------------
  // Step 2 — Jira ticket selection
  // -------------------------------------------------------------------------

  async function runJiraSelection() {
    const flight = await agent(
      `Search Jira (searchJiraIssuesUsingJql) for tickets assigned to the current user, project = ` +
        `${JIRA_PROJECT_KEY}, status = "${JIRA_STATUS_IN_DEVELOPMENT}", that already have a ` +
        `linked/commented PR URL — those count as "in flight" (report their total count as ` +
        `in_flight_count; this can be >0 only as crash-recovery leftovers from an interrupted prior ` +
        `pass, since a normal pass always moves a ticket to "${JIRA_STATUS_IN_REVIEW}" or ` +
        `"${JIRA_STATUS_ON_HOLD}" before finishing). Separately, search for tickets ALSO assigned to ` +
        `the current user (never resume a ticket assigned to anyone else — this pass only ever works ` +
        `the current user's own backlog, exactly like the fresh-ticket-selection JQL below already ` +
        `scopes to assignee = currentUser()) in status "${JIRA_STATUS_ON_HOLD}": report their total ` +
        `count as on_hold_count and the key of the OLDEST one (by created date) as oldest_on_hold_key ` +
        `(null if none). For each, ALSO check its Jira comments (getJiraIssue) for a human reply posted ` +
        `after the escalation comment.\n\n` +
        `Report resumable as an array of {key, reply} — one entry for every "${JIRA_STATUS_ON_HOLD}" ` +
        `ticket that has a human reply after its escalation comment, ORDERED oldest reply first (empty ` +
        `array if none). on_hold_count/oldest_on_hold_key are reported regardless of what's in ` +
        `resumable — they're pure observability, not selection logic.`,
      {
        label: "flight-check",
        phase: "Jira Selection",
        schema: FLIGHT_SCHEMA,
      },
    );

    onHoldCount =
      flight && typeof flight.on_hold_count === "number"
        ? flight.on_hold_count
        : null;
    oldestOnHoldKey = (flight && flight.oldest_on_hold_key) || null;

    const inFlightCount =
      flight && typeof flight.in_flight_count === "number"
        ? flight.in_flight_count
        : MAX_CONCURRENT_TICKETS; // fail-closed: an unreadable flight-check leaves zero slots, same spirit as the old cap's fail-closed default
    let availableSlots = Math.max(0, MAX_CONCURRENT_TICKETS - inFlightCount);

    // Resume-first: every on-hold ticket with a fresh human reply gets a slot before any new ticket
    // is selected — unblocking parked work takes priority over starting new work.
    const resumable = (flight && flight.resumable) || [];
    for (const r of resumable) {
      if (availableSlots <= 0) break;
      const resumeTransition = await agent(
        `Transition Jira ticket ${r.key} back to "${JIRA_STATUS_IN_DEVELOPMENT}" via ` +
          `transitionJiraIssue. Call getTransitionsForJiraIssue first. If a transition whose to.name is ` +
          `"${JIRA_STATUS_IN_DEVELOPMENT}" exists, use it and report transitioned: true. If NO such ` +
          `transition is directly available from the ticket's current status, do NOT attempt any other ` +
          `transition or force it through an intermediate state — report transitioned: false with a ` +
          `reason listing the transitions that WERE available, so a human can decide the right path.`,
        {
          label: `resume-transition:${r.key}`,
          phase: "Jira Selection",
          schema: RESUME_TRANSITION_SCHEMA,
        },
      );
      if (resumeTransition && resumeTransition.transitioned) {
        ticketsToImplement.push({ key: r.key, resumedReply: r.reply });
        availableSlots--;
      } else {
        const why =
          (resumeTransition && resumeTransition.reason) ||
          "transition agent failed with no result";
        await agent(
          buildEscalationPrompt(
            r.key,
            `could not be auto-resumed this pass: ${why}`,
          ),
          {
            label: `resume-transition-failed:${r.key}`,
            phase: "Jira Selection",
          },
        );
        escalatedTicketKeys.push(r.key);
      }
    }

    // Fill any remaining slots with fresh tickets, oldest-created first.
    if (availableSlots > 0) {
      const selection = await agent(
        `Run this JQL via searchJiraIssuesUsingJql: project = ${JIRA_PROJECT_KEY} AND assignee = ` +
          `currentUser() AND status = "${JIRA_STATUS_IN_DEVELOPMENT}", ORDER BY created ASC. Filter out ` +
          `any ticket that has an issuelinks of type "is blocked by" pointing at a non-Done issue, or ` +
          `whose parent/epic (if any) is itself blocked. Report ticket_keys: the full ordered list of ` +
          `eligible ticket keys, oldest created first (empty array if none qualify) — the caller will ` +
          `take as many from the front as it needs, don't truncate the list yourself.`,
        {
          label: "select-tickets",
          phase: "Jira Selection",
          schema: SELECTION_SCHEMA,
        },
      );

      const candidates = (selection && selection.ticket_keys) || [];

      // One batched check-stale-branch call over every candidate instead of one call per
      // candidate — same latency rationale as the PR Follow-up consolidation above.
      let staleResults = [];
      if (candidates.length > 0) {
        const staleCheck = await agent(
          `For EACH of these ticket keys, run \`python3 ${RULES} check-stale-branch <key>\` via Bash ` +
            `from the repo root, in this same turn: ${JSON.stringify(candidates)}. This is mechanical ` +
            `— don't judge or re-derive anything, just run the command once per key and collect its ` +
            `JSON output. Report back {"results": [{key, stale, branch}, ...]}, one entry per key, in ` +
            `the SAME order as the input list.`,
          {
            label: "check-stale-batch",
            phase: "Jira Selection",
            schema: STALE_BATCH_SCHEMA,
          },
        );
        staleResults = (staleCheck && staleCheck.results) || [];
      }
      const staleByKey = new Map(staleResults.map((r) => [r.key, r]));

      for (const key of candidates) {
        if (availableSlots <= 0) break;
        const stale = staleByKey.get(key);
        if (stale && stale.stale) {
          await agent(
            buildEscalationPrompt(
              key,
              `it has a leftover branch ${stale.branch} from an interrupted prior attempt (host crash ` +
                `/ manual interrupt mid-implementation), with no open PR — a human should decide whether ` +
                `to resume or discard the abandoned work`,
              { alsoChannel: true },
            ),
            {
              label: `escalate-stale:${key}`,
              phase: "Jira Selection",
            },
          );
          escalatedTicketKeys.push(key);
          // stale tickets don't consume a slot — keep walking the candidate list
        } else {
          ticketsToImplement.push({ key, resumedReply: null });
          availableSlots--;
        }
      }
    }
  }

  await parallel([
    () => runPhase("Slack Review", runSlackReview),
    () => runPhase("PR Follow-up", runPrFollowup),
    () => runPhase("Jira Selection", runJiraSelection),
  ]);

  if (phaseCrashes.length > 0) {
    crashed = true;
    crashedPhase = phaseCrashes[0].phase;
    crashedError = phaseCrashes
      .map((c) => `${c.phase}: ${c.error}`)
      .join(" | ");
  }

  // -------------------------------------------------------------------------
  // Step 3 — Implement every selected/resumed ticket, concurrently
  // -------------------------------------------------------------------------

  phase("Implement");

  async function implementOneTicket(t) {
    const resumeNote = t.resumedReply
      ? `\n\nThis is a resume of an "${JIRA_STATUS_ON_HOLD}" ticket — fold this human reply in as ` +
        `extra context: "${t.resumedReply}"`
      : "";

    const outcome = await agent(
      `${GUARDRAILS}\n\n` +
        `Run Skill("${SKILL_TICKET_TO_PR}") for ${t.key}. That skill covers: fetch + understand, ` +
        `root-cause first (for a CASE bug specifically — one whose repro depends on a real case's ` +
        `data/logs, not every Bug-type ticket — confirm against real data via the project's own ` +
        `debugging skill and post the confirmed finding as a Jira comment before coding), branch, size, ` +
        `implement, test, PR, and the mandatory high-effort multi-angle self-review.${resumeNote}\n\n` +
        `Self-timebox this: this is one unattended pass among many, not the only chance to land ` +
        `${t.key}. If you are not converging — repeated tool failures, going in circles, no PR and ` +
        `no clear next step after a reasonable number of attempts — stop and report status "escalated" ` +
        `with what's blocking you, rather than continuing indefinitely. A timely escalation is always ` +
        `preferable to a pass that never returns.\n\n` +
        `Report back one of: status "success" with the PR url, status "escalated" with the reason ` +
        `(ambiguous AC, can't reproduce, touches auth/migrations/infra, missing access, not converging), ` +
        `or status "failed" with what broke, if the agent errors out with no clear escalation reason.`,
      {
        label: `implement:${t.key}`,
        phase: "Implement",
        isolation: "worktree",
        schema: IMPLEMENT_SCHEMA,
      },
    );

    if (outcome && outcome.status === "success") {
      await agent(
        `Run Skill("${SKILL_REQUEST_REVIEW}") targeting ${SLACK_CHANNEL_NAME} with the PR link ` +
          `${outcome.pr_url}. This is an unattended pass — there is no human on the other end ` +
          `of the chat to reply "send". Skip the skill's step 4 preview-and-confirm gate entirely: ` +
          `compose the message per steps 1-3 and go straight to step 5 (slack_send_message). Do not ` +
          `wait for or ask for approval.`,
        { label: `ask-team-to-review:${t.key}`, phase: "Implement" },
      );
      const rootCauseHint = cfg.jira_root_cause_hint
        ? `On a Bug-type ticket this transition has a required screen field: it fails with "Please ` +
          `enter an option for the root cause of this issue" unless fields: {"${cfg.jira_root_cause_hint.field_id}": ` +
          `{"id": "<option id>"}} is supplied. Known option ids for this project: ${JSON.stringify(cfg.jira_root_cause_hint.options)} ` +
          `— call getJiraIssue with expand: "editmeta" on the ticket first if a different category ` +
          `clearly fits better than these. This field is NOT required on a Sub-task's equivalent ` +
          `transition. `
        : "";
      await agent(
        `Transition Jira ticket ${t.key} to "${JIRA_STATUS_IN_REVIEW}" via transitionJiraIssue. ` +
          `The transition id is NOT fixed across issue types — always call getTransitionsForJiraIssue on ` +
          `the actual ticket first and pick the transition whose to.name is "${JIRA_STATUS_IN_REVIEW}", ` +
          `never hardcode an id. ${rootCauseHint}If the transition call fails with any "Please enter ..." ` +
          `required-field message, fetch editmeta for that field's id and allowed values and retry with ` +
          `it filled in — don't treat a required-field validation error as a hard failure requiring ` +
          `escalation.\n\n` +
          `Do NOT add a separate Jira comment with the PR link — Skill("${SKILL_TICKET_TO_PR}")'s own ` +
          `final phase already comments the PR link on the ticket. Adding another one here would ` +
          `duplicate it.`,
        { label: `transition-in-review:${t.key}`, phase: "Implement" },
      );
    } else {
      const failReason =
        (outcome && outcome.reason) ||
        "implementation agent failed with no clear escalation reason";
      await agent(
        buildEscalationPrompt(t.key, failReason, { alsoChannel: true }),
        { label: `escalate-implement:${t.key}`, phase: "Implement" },
      );
      escalatedTicketKeys.push(t.key);
    }

    return { key: t.key, outcome: outcome || null };
  }

  implementResults = await parallel(
    ticketsToImplement.map((t) => () => implementOneTicket(t)),
  );
} catch (err) {
  // This outer catch only fires for config validation (before any phase runs) or an orchestration
  // bug in the Implement step itself — Slack Review/PR Follow-up/Jira Selection crashes are caught
  // per-phase by runPhase() above and folded into crashedPhase/crashedError there instead.
  crashed = true;
  crashedPhase = crashedPhase || "Config or Implement";
  crashedError = err && err.message ? err.message : String(err);
  log(`Pass CRASHED in phase "${crashedPhase}": ${crashedError}`);
}

// ---------------------------------------------------------------------------
// Summary — always runs, crash or not. Uses whatever state was hoisted
// above; safe defaults mean a crash early (e.g. in config validation) still
// produces a well-formed summary instead of an empty/undefined one.
// ---------------------------------------------------------------------------

const ticketKeys = implementResults.map((r) => r.key);
const ticketOutcomes = implementResults.map((r) => ({
  key: r.key,
  status: r.outcome ? r.outcome.status : null,
  pr_url: r.outcome ? r.outcome.pr_url || null : null,
  reason: r.outcome ? r.outcome.reason || null : null,
}));
const prsReviewed = reviewOutcomes.filter(Boolean).length;
const prsFollowedUp = followUpOutcomes
  .filter(Boolean)
  .filter((f) => f.status === "fixed" || f.status === "escalated").length;
const didWork =
  prsReviewed > 0 || prsFollowedUp > 0 || ticketKeys.length > 0 || crashed;

const ticketsSummary = ticketOutcomes.length
  ? ticketOutcomes
      .map((t) => `${t.key} (${t.status || "no result"})`)
      .join(", ")
  : "none";
const escalatedSummary = escalatedTicketKeys.length
  ? escalatedTicketKeys.join(", ")
  : "none";

log(
  `Pass done — PRs reviewed: ${prsReviewed}, PRs followed up: ${prsFollowedUp}, ` +
    `tickets worked: ${ticketsSummary}, escalated: ${escalatedSummary}, crashed: ${crashed}`,
);

// Durable pass history, independent of whatever side effects landed in Jira/Slack/GitHub. Written
// OUTSIDE the repo working tree — never touches the human's live checkout, no git operation. Always
// runs, even after a crash (this is after the try/catch, not inside it) — never blocks on failure.
await agent(
  `Append one line to the plain text file ${cfg.cache_dir}/ultracode-pass-log.md (create the ` +
    `directory and file with a one-line header if they don't exist yet) via Bash, using ` +
    `\`date -u +"%Y-%m-%d %H:%M UTC"\` for the timestamp. This is a plain local file, NOT inside the ` +
    `git repository — do not run any git command against it. Line format: "- <timestamp> — PRs ` +
    `reviewed: ${prsReviewed}, PRs followed up: ${prsFollowedUp}, tickets: ${ticketsSummary}, ` +
    `escalated: ${escalatedSummary}${crashed ? `, CRASHED in phase \\"${crashedPhase}\\": ${crashedError}` : ""}". ` +
    `If anything about this fails, don't retry and don't treat it as a pass failure — this is ` +
    `best-effort bookkeeping only.`,
  { label: "pass-log", phase: "Implement" },
);

return {
  prsReviewed,
  prsFollowedUp,
  ticketKeys,
  ticketOutcomes,
  escalatedTicketKeys,
  didWork,
  crashed,
  crashedPhase,
  crashedError,
  onHoldCount,
  oldestOnHoldKey,
};
