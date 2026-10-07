# claude-usage-report

A Claude Code skill that measures how efficiently your Claude Code sessions spend
tokens, normalises the cost by what the sessions produced (pull requests, closed issues,
share of your plan), and tells you what got better or worse since the last measurement.

## What it measures

From the transcripts Claude Code keeps on your machine (`~/.claude/projects/`), for main
sessions and subagents separately:

- API calls by model;
- context per call (mean, p50, p90, max) and the calls over a context budget;
- cache-read share, cache write per call, and **cache rebuilds** (calls that pay for the
  whole context again after the prompt cache expired);
- compactions;
- refused tool calls by cause (worktree guard, classifier, user rejection, blocked
  sleep, hooks, permission rules) and failed tool calls by cause;
- subagents by type and model, and the most expensive sessions;
- an **equivalent tokens** (eqt) cost index: input x1 + cache write x2 + cache read x0.1
  + output x5, the price ratios between token kinds of one model.

Beyond the local transcripts:

- **cloud sessions** (claude.ai/code): their lifetime cost from the Remote MCP
  `get_session` tool, diffed against the previous record so each window gets its own
  spend;
- **outcomes**: PRs opened and merged and issues closed in the window, through `gh` (or
  counts you pass in);
- **plan scaling**: "I've used 30% of my plan" turns the window's totals into "per 10%
  of plan", so windows of different lengths compare.

Each run appends a dated JSON record and compares it with the previous one, classing
every number **better / worse / unchanged / unknown** with the reason. Claude then checks
the project's written rules for working with Claude (a context budget, model per task,
compaction habits) against the numbers and proposes changes to the rules the numbers no
longer support. It never edits them.

## Why

With prompt caching, nearly all input is cache reads, so a session's cost is roughly
**context size x number of calls**. That makes the expensive habits invisible from inside
a session: a coordinator that never compacts, subagents that inherit the most expensive
model, a session idling past the cache lifetime and paying for its whole context again,
a guard refusing the same compound command a hundred times. Each of these shows up only
when somebody measures. Rules written after one measurement need the same measurement
repeated, or nobody notices when a rule stops working or its reason disappears.

## Install

**As a Claude Code plugin** (updates with the repository):

```text
/plugin marketplace add Voyteck/claude-skill-usage-report
/plugin install usage-report@voyteck-usage-report
```

**As a plain skill**: copy `skills/claude-usage-report/` into your personal skills or one project's:

```bash
git clone https://github.com/Voyteck/claude-skill-usage-report
cp -r claude-skill-usage-report/skills/claude-usage-report ~/.claude/skills/                # every project on this machine
cp -r claude-skill-usage-report/skills/claude-usage-report /path/to/repo/.claude/skills/    # one project, shared with its team
```

**On claude.ai** (Settings → Capabilities → Skills): upload a zip with the skill folder at
its root:

```bash
cd claude-skill-usage-report/skills && zip -r ../claude-usage-report.zip claude-usage-report
```

Claude Code picks the skill up at the next session start.

The script runs on its own too (Python 3.9+, standard library only):

```bash
python skills/claude-usage-report/scripts/usage.py --since 2025-03-03 --plan-used 40
python skills/claude-usage-report/scripts/usage.py --since 2025-03-03 --json --no-record
python skills/claude-usage-report/scripts/usage.py --self-test
```

`gh` is optional (outcomes); the Remote MCP tools are optional (cloud cost).

## Example output

Illustrative numbers:

```
Claude Code usage  2025-03-03 .. now  (/home/me/project)

Headline
  local: 48.20M eqt   cloud: $61.40 (3 sessions, 0 first seen)   USD total: $61.40
  PRs opened 22, merged 19, issues closed 14  (source: gh search)
  per merged PR: 2.54M eqt, $3.23   per opened PR: 2.19M eqt, $2.79
  per 10% of plan (window used 40.0%): 12.05M eqt, merged PRs 4.75, opened PRs 5.5, issues closed 3.5

Main sessions: 4 transcripts, 610 calls, 9.10M eqt
  models:            claude-opus-x 610 (100%)
  context per call:  mean 182k  p50 160k  p90 331k  max 498k
  over 250k:          128 (21.0%)
  cache:             reads 97.6% of input, write 5,920/call, 3 rebuilds (9.8% of cost), 6 compactions
  refused tool calls: 4 of 702 (permission rule 3, user rejected 1)
  failed tool calls:  command exit code 9, other error 2
Subagents: 57 transcripts, 3,940 calls, 39.10M eqt
  models:            claude-sonnet-x 2,480 (63%), claude-opus-x 1,410 (36%), claude-haiku-x 50 (1%)
  ...
Main sessions' share of the cost: 18.9%

Signals
  - 9,140 tokens of cache write per call: large tool outputs; cut them at the source (tail, limits, filters)

Against the previous record (week 9, 2025-03-03, window 2025-02-24 .. 2025-03-02)
  better    eqt per merged PR: 3.41M -> 2.54M (-26%); lower is better
  better    main: share of calls over budget: 44.0% -> 21.0% (-52%); lower is better
  worse     cache write per call: 7,400 -> 9,140 (+24%); lower is better
  unchanged main: p90 context per call: 338k -> 331k (-2%), within 5%
  ...
```

The record behind it is one JSON line in `docs/claude/usage-records.jsonl` (or the file
you choose with `--record`).

## Built from measurements

The skill generalises a measurement loop run on a real multi-agent project over several
weeks (September and October 2026). Aggregated findings that shaped what it measures:

- **Cache reads were 97% of all tokens** in the first measured weekend, so cost was
  context size times calls; context per call is the first thing the skill reports.
- **Main sessions were 55% of the cost** at first. After a context budget, a visible
  context meter and delegation rules they fell to about a third, then to about a sixth;
  mean context per main-session call fell by roughly half.
- **A written budget alone did not hold**: one measurement after the rule, 60-90% of
  main-session calls were still over it, and long-lived coordinators compacted only near
  the hard limit. The share over budget fell to about a quarter once the meter was
  visible. Hence "calls over budget" and the "not held -> enforce with a tool" advice.
- **Every subagent ran on the most expensive model** in the first measurement; later,
  subagent calls that named no defined agent were about 31% of all cost. Hence "subagents
  on the expensive tier" and "subagents by type".
- **Cache rebuilds were about 59% of the cloud sessions' cost**: large sessions woken by
  check-ins after the cache had expired paid for their whole context again before doing
  anything. Hence the rebuild metric.
- **About 83% of ~270 refused tool calls** came from one guard refusing compound shell
  commands, and about 5% from blocked foreground waits; each refusal is a full-context
  turn. After allowing safe read-only commands, refusals fell by about 90%. Hence
  "refusals by cause".
- **Cache writes stayed around 7k tokens per call** across measurements, mostly large
  tool outputs every later call re-reads. Hence "cache write per call".
- **Paging cloud session events to measure them** cost the measuring agent about 0.6M
  tokens and nearly a thousand tool calls for one week of a handful of sessions; one
  `get_session` call per session gives the lifetime cost. Hence the rule never to page
  transcripts.
- A pricier coordinator model cost about 1.8x per equivalent token and found no more
  bugs in a side-by-side trial: a reminder to measure model choices, not assume them.

## Files

- [`SKILL.md`](skills/claude-usage-report/SKILL.md) - the instructions Claude follows.
- `skills/claude-usage-report/scripts/usage.py` - the measurement (`--help`, `--self-test`).
- `skills/claude-usage-report/references/metrics.md` - what each number means, what a bad value suggests, the
  signal thresholds, and which rules each number tests.

## Privacy

The script reads local files and, for outcomes, calls `gh`. It sends nothing anywhere
else. Records hold aggregates, truncated session ids and cloud session ids/titles; keep
the records file out of public repositories if those are sensitive.

## License

MIT - see [LICENSE](LICENSE). Plugin name `usage-report`, skill name `claude-usage-report`.
