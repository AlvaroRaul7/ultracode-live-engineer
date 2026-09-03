---
name: ask-team-to-review
description: >-
  Send a Slack notification asking the team to review a GitHub pull request.
  Writes one short, casual review-request message in the author's own voice and
  posts it via the official Slack MCP after a single preview-and-confirm step.
  Use when the user asks to "notify the team", "ask for review", "request
  review", "send PR to Slack", "post in Slack", "Slack the team", "ask team to
  review", "ping the team", "let the team know", or any request to send a PR
  review notification to a Slack channel.
---

# Ask Team to Review

Send a short, casual PR review request to a Slack channel via the official Slack MCP. One style, one preview, one confirmation — no menu.

> **Scope note:** this skill targets **GitHub PRs via `gh`**. `gh` reads the
> **current working directory's** git remote and, by default, the PR for the
> **current branch** — so make sure you're on the branch whose PR you want to
> announce (or pass its number/branch explicitly).

## Config (optional — works with zero config too)

If the repo has `.claude/ultracode-live-engineer/config.json` (this plugin's
shared per-project config — see the workflow's `CONFIG.md`), this skill reads
it for defaults:

- `slack_channel_id` / `slack_channel_name` — default channel to post in.
- `jira_project_key` + `jira_cloud_id` — enables the optional ticket line:
  a `<jira_project_key>-NNNN` id found in the branch/title becomes
  `[<ticket>](https://<jira_cloud_id>/browse/<ticket>)`.
- `reviewer_areas` (optional array, new field this skill introduces — see
  `config.example.json`): `[{"area": "...", "path_prefixes": ["..."],
"reviewers": ["Full Name", ...]}, ...]`. Used for automatic reviewer
  tagging by area (see step 1a). **This skill ships no roster of its own —
  every name in `reviewer_areas` is 100% supplied by the adopter.**

**No config file, or a field missing?** The skill still works:

- No `reviewer_areas` → no automatic tagging; ask the user who to tag, or
  honor `tag @Name` in their prompt. Never invent names.
- No `jira_project_key` → skip the ticket line entirely unless the user's
  prompt supplies a ticket to reference.
- No `slack_channel_id`/`slack_channel_name` → ask the user for the channel
  (or resolve one they name via `slack_search_channels`).

## Invocation

```text
/ask-team-to-review                  # Current branch's PR, default channel
/ask-team-to-review #other-channel   # Override channel
/ask-team-to-review 1237             # Announce a specific PR number
```

## Parameters

| Parameter | Required | Default                                      | Description                     |
| --------- | -------- | -------------------------------------------- | ------------------------------- |
| Channel   | No       | `config.slack_channel_id`, else ask the user | Slack channel to post in        |
| PR        | No       | current branch's open PR                     | PR number or branch to announce |

The message **@-mentions the area owners** for whatever the PR actually changed (see _Reviewer tagging_ below) so the right people get pinged; it does not use `@here` / `@channel`. The user can override in their prompt (`tag @Name`, `also tag frontend`, or `no tags`).

## Execution Flow

### 1. Gather PR information

From the repo whose PR you're announcing:

```bash
gh pr view --json number,title,url,body,headRefName,files   # current branch's open PR
# or a specific one:
gh pr view <number> --json number,title,url,body,headRefName,files
```

Extract from the JSON:

- `number` — the PR number (GitHub prefixes PRs with `#`, e.g. `#1237`)
- `title`
- `url` — the PR link
- `body` — take one line from a `## What` or `## Summary` section, or fall back to the title
- `headRefName` — the branch; if `config.jira_project_key` is set, scan it (and the title) for a `<jira_project_key>-\d+` id for the ticket line
- `files[].path` — the changed paths; map them to areas for reviewer tagging (next section)

If `gh` reports no PR for the branch, the developer is likely not on a branch with an open PR — see Error handling.

### 1a. Reviewer tagging (by area)

If `config.reviewer_areas` is set: map each changed `files[].path` to an area by matching its `path_prefixes`, then tag the **union** of that area's `reviewers` for every area the PR touches (a PR that changes paths in two areas tags both). If a PR only touches paths outside every configured area, say so when you preview and ask the user whether to tag anyone.

If `config.reviewer_areas` is absent (no config, or the field is empty): skip automatic tagging — ask the user who to tag, or rely on `tag @Name` in their prompt.

The user's prompt always wins: `no tags` → omit mentions; `tag @Name` / `also tag <area>` → add to the set.

**A real ping needs `<@USER_ID>`, not plain `@Name` text.** For every name to tag (from config or the user's prompt), resolve it live via `slack_search_users` (by full name) — this skill does not ship or cache a name→id table; resolve fresh each run so it never goes stale or leaks a roster into version control. If `slack_search_users` returns no match or is ambiguous for a name, fall back to plain `@Name` text for that person and warn the user in the preview that it won't hard-ping until the id is resolved.

### 2. Write a casual intro

Write one friendly sentence in the author's own voice — the way they'd write it to a colleague. The Slack channel already shows the author's avatar and name, so no third-person self-reference. No character voices, no themes.

Examples of the tone:

- `Hey team, would love a review when you have a sec — small backend fix.`
- `PR up whenever you have a minute, nothing urgent.`
- `Ready for review — small perf fix for the ingestion path, should be quick.`

Keep it to one or two short sentences. Adapt the wording to the PR's actual content (size, urgency, which area it touches).

### 3. Compose the final message

Wrap the intro in this template. **The Slack MCP renders standard Markdown, not Slack's legacy `mrkdwn`** — so links are `[label](url)` and bold is `**double asterisks**`. See _Formatting rules_ below; getting this wrong posts visible junk to a team channel.

```text
:mag: {intro}

**[PR #{number}: {title}]({pr_url})**
{short_description}
[{ticket}](https://{jira_cloud_id}/browse/{ticket})
cc {mentions}
```

- The **ticket line is included only when a ticket id is found** (per the `config.jira_project_key` pattern above) **and** `config.jira_cloud_id` is set. If there is no ticket, or no Jira config, drop that line entirely — the PR link carries the reference. Never post a bare URL; if a PR references another tracker item, add it as its own `[label](url)` line.
- `{mentions}` is the space-separated `<@USER_ID>` list for the area owners resolved in step 1a (e.g. `cc <@U123> <@U456>`). Drop the whole `cc` line if the user said `no tags` or nobody was resolved. **User mentions are the one exception to "standard Markdown"** — `<@U123>` is Slack's own syntax and is correct as written; there is no Markdown equivalent.

### 4. Preview and confirm

Show the full composed message to the user in the chat. Wait for:

- `send` / `yes` / `ok` / `ship it` → proceed to step 5
- `edit` / `change` → ask what to adjust, regenerate
- `cancel` / `no` → stop, don't send

If the reply is ambiguous, ask once rather than guessing.

**Unattended/scripted invocation** (e.g. driven by another skill or workflow with no human to reply "send"): the caller may explicitly instruct you to skip this gate — only do so when told to in the invoking prompt, never on your own judgment for an interactive request.

### 5. Send via Slack MCP

`slack_send_message` takes a **channel ID**, not a channel name, and its message parameter is `message` (not `text`):

- `channel_id`: resolve the channel name to an id via `slack_search_channels` if you only have the name (or use `config.slack_channel_id` directly if set)
- `message`: the formatted message from step 3

Use `slack_send_message` directly, **not** `slack_send_message_draft` — the tool description steers unreviewed messages to the draft variant, but step 4's preview-and-confirm _is_ the review, so a draft would strand the message unsent.

### 6. Confirm

Report to the user:

- Success: `Review request posted to #{channel}`
- Failure: show the error and suggest `slack_search_channels` for the correct channel name

## Formatting rules (standard Markdown, NOT Slack `mrkdwn`)

**This is the single most common way this skill produces a broken post.** Slack's own docs and most Slack snippets on the internet describe `mrkdwn` — `<url|label>` links and `*single asterisk*` bold. The `slack_send_message` MCP tool does **not** take `mrkdwn`; its own description says "Message uses standard markdown (`**bold**`, `_italic_`, `` `code` ``, ~~strikethrough~~, >blockquotes, lists, links, code blocks, tables, headers)".

Feed it `mrkdwn` and it does not error — it posts the raw syntax as literal text, in a team channel, where everyone sees it.

| Intent       | ✅ Correct (Markdown)                                | ❌ Wrong (Slack `mrkdwn`)                            |
| ------------ | ---------------------------------------------------- | ---------------------------------------------------- |
| Link         | `[PR #1237: short title](https://github.com/…/1237)` | `<https://github.com/…/1237\|PR #1237: short title>` |
| Bold         | `**bold**`                                           | `*bold*` — renders _italic_ here                     |
| Italic       | `_italic_`                                           | —                                                    |
| Code         | `` `code` ``                                         | same                                                 |
| User mention | `<@U123>`                                            | `<@U123>` — Slack syntax, correct, no Markdown form  |

Two traps worth naming:

- **`*text*` silently degrades.** It does not post literally — it renders as _italic_, so the PR line just looks unemphasised and nobody notices the bug. Always `**text**`.
- **Never post a bare URL.** Slack auto-linkifies by scanning to the next whitespace and can absorb trailing punctuation or the next word. Always give it an explicit `[label](url)`.

The label can be any human-readable text — usually `PR #N: title` or the ticket id.

If in doubt, re-read the tool's own description (`ToolSearch` → `select:mcp__plugin_slack_slack__slack_send_message`) rather than trusting memory or a Slack docs page.

## Style guardrails

- **English only.** Even when the user's prompt is in another language.
- **Tag reviewers, don't call people out.** @-mentioning the area owners to ask for review is the point; never comment on a named person's work habits or performance.
- **Short.** Two sentences max for the intro. The PR template carries the info; the intro carries the ask.

## Error handling

- **No PR found**: tell the user — likely not on a branch with an open PR, or `gh` is run from the wrong repo. Suggest opening the PR first (`gh pr create`) or passing the PR number.
- **`gh` not authenticated**: suggest `gh auth login` (github.com).
- **Channel not found / `channel_not_found`**: you probably passed a channel _name_ where an ID belongs. Resolve it with `slack_search_channels` (it returns `#name (CXXXXXXXX)`) and use the ID.
- **Message posted with visible `<https://…|label>` or unemphasised text**: you used Slack `mrkdwn` instead of standard Markdown — see _Formatting rules_. Fix by editing or deleting the message, then repost.
- **Slack MCP tools missing entirely** (no `slack_*` tools): the `enabledPlugins` entry in `.claude/settings.json` does _not_ install the plugin on its own. Run `/plugin` → install `slack@claude-plugins-official`, then `/reload-plugins`. Check with `claude mcp list` — `plugin:slack:slack` should appear.
- **`requires re-authorization (token expired)`**: run `/mcp`, pick `plugin:slack:slack`, and complete the browser OAuth flow. The token expires periodically, so expect this occasionally even on a working setup.
