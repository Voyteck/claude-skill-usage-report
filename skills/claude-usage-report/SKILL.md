---
name: claude-usage-report
description: SUPERSEDED by the retro skill (github.com/Voyteck/claude-skill-retro, 0.5.0+), which runs the same measurements in every retrospective; prefer retro when it is installed and use this skill only when the user names claude-usage-report explicitly. Measures how efficiently Claude Code sessions spend tokens and compares the numbers with the previous measurement - calls by model, main sessions vs subagents, context per call (mean, p90, max, calls over a budget), cache reads and writes, cache rebuilds, refused tool calls by cause, an equivalent-tokens cost index, cloud (claude.ai/code) session cost, and cost per merged and per opened pull request, scaled per 10% of the plan. Appends a dated record and classes every number better / worse / unchanged / unknown, then checks the project's written rules for working with Claude against the numbers. Use it whenever someone asks where their tokens or plan went, how much a week of Claude Code cost, whether sessions or subagents are wasteful, whether their CLAUDE.md rules (context budget, model per task, compaction) are working, or wants a weekly usage measurement or a cost-per-PR figure - "token usage report", "how much did I burn", "measure my sessions", "I've used 40% of my plan", "ile spaliłem", "efektywność tokenów", "pomiar sesji", "zużycie tokenów w tym tygodniu". Use it even when the user only mentions hitting a usage limit or asks why a session got expensive.
---

# Claude usage report

> Superseded by the retro skill (`Voyteck/claude-skill-retro`, 0.5.0+). When retro is installed, tell the user that
> its signals summary ("Tokens and cost", "Tool calls: refused and failed", "Cloud sessions", "Share of the plan",
> "Tokens and writing efficiency") covers this report, and run this skill only if they still ask for it.

A measurement, not an opinion: read what the sessions spent, put it next to what they
produced, and compare it with the last time. Rules for working with Claude are usually
written after one measurement; without a repeated one nobody notices when a rule stops
working or its reason disappears. This skill is that repeat.

Everything heavy is done by `scripts/usage.py` (standard library only). Your job is to
gather the inputs the script cannot see, run it once, and turn its output into a short
report with proposals. Keep your own context small: never paste transcripts, never page
session events, read the script's output with a limit.

## 1. Settle the window

- `--since` (UTC date or timestamp). If the records file has a previous record, default
  to where it ended so windows do not overlap: the day after its `window.until` (which is
  inclusive), or its `measured_at` timestamp when it was open-ended. Otherwise ask, or take the last 7 days.
- `--until` only if the user names an end; open-ended otherwise.
- If the user says how much of the plan the window used ("I've used 30% of my plan"),
  pass `--plan-used 30`. Two windows of different lengths compare only through this
  scaling ("per 10% of plan"); without it, totals are not compared, only rates.
- The records file: `--record PATH` (default `docs/claude/usage-records.jsonl`, relative
  to the current directory). If the project has no `docs/`, ask where records should
  live before the first run. Use `--no-record` for a trial run.

## 2. Cloud sessions (claude.ai/code)

Cloud sessions leave no transcript on the local machine; their cost is visible only
through the Remote MCP tools, when the session has them (`list_sessions`,
`get_session`). If they are not available, say "cloud: not measured" and go on.

1. `list_sessions` once (page only as far as sessions updated inside the window).
2. For each session updated inside the window: ONE `get_session` call, and take
   `external_metadata.usage.cost_usd`. That is the session's LIFETIME cost.
3. Write a JSON list to a scratch file and pass it with `--cloud FILE`:
   `[{"id": "<session id>", "title": "<short title>", "cost_usd": 12.34}]`.

The script subtracts the lifetime recorded for the same session in the previous record,
so the window gets only its own spend. A session seen for the first time counts whole and
is flagged "first seen"; say so when it may predate the window.

**Never page a session's events or transcript to rebuild its usage.** Doing that for one
week of a handful of sessions once cost about 0.6M tokens and nearly a thousand tool
calls, more than many of the sessions it measured; `get_session` answers in one call. Do
it only if the user explicitly asks for a per-turn breakdown of one session, and warn
about the cost first.

## 3. Outcomes

The script counts PRs opened, PRs merged and issues closed as completed in the window
with `gh` (search API, or repository-scoped REST lists when search is not allowed; the
repository comes from `origin` or `--repo OWNER/NAME`). If `gh` is absent or fails, count
them with the GitHub MCP tools (list or search pull requests and issues filtered by the
window) and pass `--prs-opened N --prs-merged N --issues-closed N`. If neither works,
the headline says "outcomes: unknown"; do not guess.

Outcomes are repository-wide. If work in the window also came from machines or accounts
you did not measure, the cost per PR flatters; say so.

## 4. Run

```bash
python <skill-dir>/scripts/usage.py --since 2025-03-03 --plan-used 30 --cloud /tmp/cloud.json
```

Useful options (all in `--help`):

| option | default | when |
|---|---|---|
| `--project PATH` / `--all-projects` | the main checkout of the current repo (else the current directory) | sessions of another checkout, or of every project on the machine |
| `--projects-dir DIR` | `~/.claude/projects` | a copy of another machine's transcripts (measured whole) |
| `--budget N` | 250000 | the project's own context budget per call |
| `--weights input=1,cache_write=2,cache_read=0.1,output=5` | as shown | change only with a reason; changing them makes the next comparison "unknown" |
| `--top-tier REGEX` | `opus` | which models count as the expensive tier |
| `--prices FILE` | none | a USD estimate for local calls: `{"model-substring": {"input": .., "cache_write": .., "cache_read": .., "output": ..}}` per million tokens |
| `--json` | off | the record and the comparison as JSON |

Exit code 1 means nothing was found in the window: report that, check `--since` and the
project path (sessions started in a worktree live in sibling project directories, which
the script includes), do not invent numbers.

Run `python <skill-dir>/scripts/usage.py --self-test` once if the output looks wrong.

## 5. Check the project's rules

Look for written rules about working with Claude: `CLAUDE.md` (root and nested),
`AGENTS.md`, `CONTRIBUTING.md`, `.claude/agents/*.md`, a `docs/` page on sessions or
agents. Read only the relevant sections. For each rule that a number speaks to (the
mapping is in `references/metrics.md` → "Rules and the numbers that test them"), decide:

- **held** - the numbers are within what the rule asks;
- **not held** - the rule is written but the numbers show it is not followed (an
  enforcement problem: a tool, a hook, a statusline, not a stricter sentence);
- **reason gone** - the rule's stated reason no longer shows in the numbers (e.g. it was
  adopted because main sessions were most of the cost, and now they are a small share);
- **no data** - nothing measured speaks to it.

Report "not held" and "reason gone" as PROPOSALS with the number behind each. Never edit
the rules yourself, even if asked to "fix" them in the same breath: write the proposal
and let the owner decide.

## 6. Report

Keep it short; the record file holds the detail. In this order:

1. **Headline** - the window, local eqt, cloud USD, PRs merged/opened, cost per merged PR
   and per opened PR, and per 10% of plan when given.
2. **Changes against the previous record** - worse first, then better, each with its
   reason line from the script; "unchanged" as one line listing the metrics; "unknown"
   only where the reason matters (settings changed, missing data).
3. **Signals** - the script's rule-of-thumb warnings, each with the likely cause and one
   concrete fix (see `references/metrics.md`).
4. **Rules** - the proposals from step 5, or "no rule contradicted by the numbers".
5. Where the record was written, and what was not measured (cloud, outcomes) and why.

Do not paste the whole script output into the reply. Quote numbers as the script prints
them; do not round them into a different story.

## Notes

- The equivalent-tokens index (eqt) = input x1 + cache write x2 + cache read x0.1 +
  output x5. It follows the ratio of list prices between token kinds of one model; use
  it to compare calls and windows, and `--prices` or the cloud USD for money.
- Records are append-only JSON lines; a comparison always uses the last record in the
  file. To compare against an older one, copy it to the end or use a separate file.
- `references/metrics.md` explains every number, what a bad value suggests, and the
  thresholds behind the signals.
