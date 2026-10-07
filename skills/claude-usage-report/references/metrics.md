# What the numbers mean

Every number below comes from `scripts/usage.py`. "Main" is a session the user started;
"subagents" are the transcripts under `<session>/subagents/`. A **call** is one API
request; Claude Code writes it as several transcript lines (one per content block)
sharing a `requestId`, and the script counts it once, with the largest value of each
token field (the output count grows across the lines).

## The cost index: equivalent tokens (eqt)

```
eqt = input x 1 + cache_write x 2 + cache_read x 0.1 + output x 5
```

The weights follow the ratio of list prices between token kinds within one model: a
cache read is about a tenth of an input token, a cache write with the one-hour lifetime
Claude Code uses is twice an input token (a five-minute write is 1.25x), and output is
about five times input. The index therefore answers "which calls and which windows cost
more" without a price list that goes stale. It does NOT compare models: a call on a
model priced five times higher costs five times more at the same eqt. For money, use
`--prices` (local) or the cloud USD. Change the weights with `--weights` only for a
reason; a record taken with other weights makes the comparison "unknown".

In practice almost all input is cache reads (90-99%), so the cost of a session is
roughly **context size x number of calls**. Most of the advice below follows from that.

## Per population (main, subagents, total)

| number | meaning | a bad value suggests |
|---|---|---|
| calls by model | how many requests each model served | the expensive model doing simple work: pin cheaper models on agents |
| context per call: mean, p50, p90, max | input + cache write + cache read of each call: what the model re-read | a session that keeps growing; compact earlier, start fresh after a task |
| calls over budget (count, share) | calls whose context exceeds `--budget` (default 250k) | many: compaction comes too late or not at all; a visible context meter helps more than a written rule |
| cache-read share | cache reads / all input | low (< 80%): the cache keeps being rebuilt; high is normal and NOT by itself good, it also grows with context |
| cache write per call | new tokens written to the cache, averaged | large tool outputs (whole files, full logs, unfiltered JSON) that every later call re-reads; cut them at the source with limits, `tail`, filters |
| cache rebuilds (calls, share of cost) | calls whose cache write is >= `--rebuild-min` (100k) AND more than half the context: the cache expired or was dropped and the whole context was paid again | sessions left idle past the cache lifetime (about an hour) on a large context, scheduled check-ins waking a huge session, frequent resumes; compact or hand the wait to a small agent before idling |
| compactions | `compact_boundary` markers (manual `/compact` or automatic) | none at all with a high p90: nobody compacts; many with a high p90: compaction happens only at the hard limit |
| tool calls | `tool_use` blocks | denominator for refusals |
| refused tool calls, by cause | the harness stopped the call before it ran: worktree guard, auto-mode classifier, user rejected, foreground sleep blocked, hook, permission rule | each refusal is a full-context turn spent for nothing; fix the cause (allow safe read-only commands, split compound commands, wait in the background) |
| failed tool calls, by cause | the tool ran and errored: command exit code, tool input error, file not found, other | not waste in itself (tests fail on purpose); a large "tool input error" count means agents guess tool parameters |

## Across populations

| number | meaning | a bad value suggests |
|---|---|---|
| main sessions' share of the cost | main eqt / all eqt | high (> 50%): the coordinating session reads files itself; heavy reading belongs in subagents that return conclusions |
| subagents: share of calls on the expensive tier | calls whose model matches `--top-tier` | high (> 50%): agents without a pinned model inherit the parent's model; give every agent definition a model, and pass one to ad-hoc agents |
| subagents by type | calls, eqt and models per `agentType` (from `<agent>.meta.json`) | a generic or ad-hoc type with a big share: work no defined agent covers, running on whatever model it inherited |
| top sessions | the sessions with the most eqt, with their mean context and over-budget share | one long-lived session dominating: it should have been compacted or restarted |

## Headline and outcomes

| number | meaning | caveat |
|---|---|---|
| eqt / USD per merged PR | cost of the window / PRs merged in it | outcomes are repository-wide: include every machine's and the cloud's cost, or the ratio flatters |
| eqt / USD per opened PR | the same per PR opened | a PR opened late in the window merges in the next one; read both together |
| per 10% of plan | totals x (10 / plan used %) | needs `--plan-used`; it is the only way totals of windows of different lengths compare |
| cloud window USD | lifetime `cost_usd` minus the lifetime in the previous record | a session "first seen" counts whole, even the part before the window |

## The comparison

Each run classes a fixed list of rates and shares (never raw totals) against the last
record:

- **better / worse** - moved more than `--tolerance` (5%) in the stated direction;
- **unchanged** - within the tolerance;
- **unknown** - no previous record, a value missing on one side, a setting it depends on
  changed (budget, weights, top tier), or the metric has no better direction (the
  cache-read share).

Read a "worse" together with its neighbours: p90 context going up while cost per merged
PR goes down can be a better window with bigger tasks.

## Signals (thresholds)

The script prints a warning when:

| condition | likely cause | one fix |
|---|---|---|
| > 25% of main calls over budget | late or no compaction | a context meter in the statusline; compact after each finished task |
| > 50% of subagent calls on the expensive tier | unpinned agents | pin a model per agent; cheaper tier for lookups, CI watching, docs |
| cache rebuilds > 20% of cost | idling past the cache lifetime on a large context | compact before a long wait or let a small agent wait |
| > 2 refusals per 100 tool calls | commands the guard or permissions refuse | allow safe read-only commands; scripts instead of compound one-liners |
| cache write > 8,000 tokens per call | large tool outputs | cut output at the source |
| main sessions > 50% of the cost | the coordinator reads instead of delegating | delegate reading; ask for conclusions, not file dumps |

These are rules of thumb, not standards; a project with a good reason to differ should
set its own and say so in its rules.

## Rules and the numbers that test them

| a written rule like... | tested by | "not held" when | "reason gone" when |
|---|---|---|---|
| a context budget per call ("stay under N tokens") | main over-budget share, p90 | the share stays high across records | main sessions became a small share of the cost |
| compact after a task / before idling | compactions, cache rebuilds | rebuilds stay a large share | rebuilds near zero for several records |
| model per task, pinned agent models | subagent top-tier share, subagents by type | ad-hoc types on the expensive tier | the cheap tier's work is gone or the price gap closed |
| delegate heavy reading to subagents | main share of the cost | the main share rises | - |
| tool hygiene (no compound commands, no foreground waits) | refusals by cause | the same cause keeps appearing | refusals near zero after a tool-level fix |
| cut large outputs | cache write per call | no movement after the rule | - |
| measure regularly | dates between records | gaps much longer than the stated cadence | - |

A rule that is "not held" needs enforcement (a tool, a hook, a statusline), not a
stricter sentence. A rule whose "reason is gone" may be simplified or dropped. Both are
proposals for the owner of the rules, never edits.
