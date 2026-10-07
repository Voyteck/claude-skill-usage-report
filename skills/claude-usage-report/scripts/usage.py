#!/usr/bin/env python3
"""Measure how Claude Code sessions spend tokens and compare with the previous measurement.

Claude Code keeps one JSONL transcript per session under ``~/.claude/projects/<project>/``
(``<session>.jsonl``) and one per subagent under ``<session>/subagents/agent-*.jsonl``.
This script reads them for a time window and reports, for main sessions and subagents
separately:

* API calls by model;
* context per call (input + cache write + cache read): mean, p50, p90, max, and the number
  of calls over a context budget;
* the cache-read share of input and the cache write per call;
* cache rebuilds: calls that re-write most of a large context (an idle session whose
  prompt cache expired, a resumed session, a compaction);
* refused and failed tool calls, by cause;
* an "equivalent tokens" (eqt) cost index: input x1 + cache write x2 + cache read x0.1 +
  output x5 by default, close to the list-price ratios between token kinds of one model.

It can add what the transcripts cannot see: the cost of cloud sessions (a JSON file the
caller collects from the Remote MCP ``get_session`` tool), the outcomes of the window
(pull requests opened and merged, issues closed, through ``gh``) and the share of the
plan the window used. It then appends a dated JSON record to a records file and prints the
difference against the previous record, each metric classed better / worse / unchanged /
unknown with the reason.

It reads local files and, for outcomes, calls ``gh``; it sends nothing anywhere else.
Standard library only::

    python usage.py --since 2025-03-01
    python usage.py --since 2025-03-01 --plan-used 30 --cloud cloud.json
    python usage.py --since 2025-03-01 --json --no-record
    python usage.py --self-test
"""

from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = 1

#: Context per call above which a call counts as "over budget".
DEFAULT_BUDGET = 250_000

#: Relative weights of token kinds in the equivalent-tokens index. They follow the ratios of
#: list prices within one model: a cache read costs ~0.1x an input token, a 1-hour cache
#: write 2x (a 5-minute one 1.25x) and an output token ~5x. The index compares calls of
#: one model; across models multiply by the model's price (``--prices``).
DEFAULT_WEIGHTS = {"input": 1.0, "cache_write": 2.0, "cache_read": 0.1, "output": 5.0}

#: A call whose cache write is at least this many tokens AND over half its context is a
#: cache rebuild: the prompt cache expired or was dropped and the whole context was paid
#: for again.
DEFAULT_REBUILD_MIN = 100_000

#: Model names that count as the expensive tier for the "subagents on the expensive model"
#: metric. A regular expression, matched case-insensitively.
DEFAULT_TOP_TIER = "opus"

#: Relative change under which a metric counts as unchanged.
DEFAULT_TOLERANCE = 0.05

#: Refusals: the harness stopped the tool call before it ran. Checked in order; the first
#: match names the cause.
REFUSAL_CAUSES: list[tuple[str, re.Pattern[str]]] = [
    ("worktree guard", re.compile(r"isolated in the worktree|too complex to verify", re.I)),
    ("auto-mode classifier", re.compile(r"classifier", re.I)),
    ("user rejected", re.compile(r"doesn't want to proceed|was rejected", re.I)),
    ("foreground sleep blocked", re.compile(r"Blocked: sleep", re.I)),
    ("hook", re.compile(r"hook .*(?:blocked|denied)|blocked by (?:a )?hook", re.I)),
    (
        "permission rule",
        re.compile(r"permission .*(?:has been|was) denied|denied by|not allowed", re.I),
    ),
    ("blocked (other)", re.compile(r"<tool_use_error>\s*Blocked", re.I)),
]

#: Failures: the tool ran (or was called) and returned an error. Not a refusal.
FAILURE_CAUSES: list[tuple[str, re.Pattern[str]]] = [
    ("command exit code", re.compile(r"^\s*Exit code \d+", re.I)),
    ("tool input error", re.compile(r"<tool_use_error>", re.I)),
    ("file not found", re.compile(r"does not exist|no such file", re.I)),
]


# --------------------------------------------------------------------------- data


@dataclass
class Tokens:
    """Token counts of one or many API calls."""

    input: int = 0
    cache_write: int = 0
    cache_read: int = 0
    output: int = 0

    def add(self, other: Tokens) -> None:
        """Add ``other`` to these counts in place.

        Args:
            other: The counts to add.
        """
        self.input += other.input
        self.cache_write += other.cache_write
        self.cache_read += other.cache_read
        self.output += other.output

    @property
    def context(self) -> int:
        """All input-side tokens of the call: what the model had to read."""
        return self.input + self.cache_write + self.cache_read

    def eqt(self, weights: dict[str, float]) -> float:
        """The equivalent-tokens index of these counts.

        Args:
            weights: The weight per token kind (keys of ``DEFAULT_WEIGHTS``).

        Returns:
            The weighted sum.
        """
        return (
            self.input * weights["input"]
            + self.cache_write * weights["cache_write"]
            + self.cache_read * weights["cache_read"]
            + self.output * weights["output"]
        )

    def as_dict(self) -> dict[str, int]:
        """The counts as a JSON-ready dict."""
        return {
            "input": self.input,
            "cache_write": self.cache_write,
            "cache_read": self.cache_read,
            "output": self.output,
        }


@dataclass
class Call:
    """One API call, counted once however many transcript lines it was written as."""

    model: str
    tokens: Tokens
    timestamp: str


@dataclass
class TranscriptStats:
    """What one transcript file holds inside the window."""

    calls: list[Call] = field(default_factory=list)
    tool_calls: int = 0
    refusals: Counter[str] = field(default_factory=Counter)
    failures: Counter[str] = field(default_factory=Counter)
    compactions: int = 0

    @property
    def empty(self) -> bool:
        """True when nothing in the window was found."""
        return not (self.calls or self.tool_calls or self.refusals or self.failures)


@dataclass
class Population:
    """Aggregates of a group of transcripts (main sessions, subagents, one agent type)."""

    transcripts: int = 0
    calls_by_model: Counter[str] = field(default_factory=Counter)
    tokens_by_model: dict[str, Tokens] = field(default_factory=lambda: defaultdict(Tokens))
    contexts: list[int] = field(default_factory=list)
    rebuilds: int = 0
    rebuild_tokens: Tokens = field(default_factory=Tokens)
    tool_calls: int = 0
    refusals: Counter[str] = field(default_factory=Counter)
    failures: Counter[str] = field(default_factory=Counter)
    compactions: int = 0

    def add(self, stats: TranscriptStats, rebuild_min: int) -> None:
        """Fold one transcript's stats into this population.

        Args:
            stats: The transcript's stats.
            rebuild_min: The cache-write size from which a call can be a rebuild.
        """
        self.transcripts += 1
        for call in stats.calls:
            self.calls_by_model[call.model] += 1
            self.tokens_by_model[call.model].add(call.tokens)
            context = call.tokens.context
            self.contexts.append(context)
            if call.tokens.cache_write >= rebuild_min and call.tokens.cache_write * 2 > context:
                self.rebuilds += 1
                self.rebuild_tokens.add(call.tokens)
        self.tool_calls += stats.tool_calls
        self.refusals.update(stats.refusals)
        self.failures.update(stats.failures)
        self.compactions += stats.compactions

    @property
    def calls(self) -> int:
        """The number of API calls."""
        return sum(self.calls_by_model.values())

    def tokens(self) -> Tokens:
        """All tokens of the population."""
        total = Tokens()
        for tokens in self.tokens_by_model.values():
            total.add(tokens)
        return total


# --------------------------------------------------------------------------- reading


def _texts(content: object) -> str:
    """Join the text of a ``tool_result`` content (a string or a list of blocks)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(str(b.get("text", "")) for b in content if isinstance(b, dict))
    return ""


def classify_error(text: str) -> tuple[str, str]:
    """Class an error ``tool_result`` as a refusal or a failure, with its cause.

    Args:
        text: The text of the tool result.

    Returns:
        ``("refusal", cause)`` or ``("failure", cause)``.
    """
    for cause, pattern in REFUSAL_CAUSES:
        if pattern.search(text):
            return "refusal", cause
    for cause, pattern in FAILURE_CAUSES:
        if pattern.search(text):
            return "failure", cause
    return "failure", "other error"


def _in_window(timestamp: str, since: str | None, until: str | None) -> bool:
    """Whether an ISO timestamp falls in the window; ``until`` is inclusive of its prefix."""
    if since and timestamp < since:
        return False
    return not (until and timestamp[: len(until)] > until)


def read_transcript(path: Path, since: str | None, until: str | None) -> TranscriptStats:
    """Read one transcript's calls, tool calls, refusals and compactions in the window.

    One API call is written as several ``assistant`` lines (one per content block) sharing
    a ``requestId``; the output count grows across them, so the call takes the largest
    value of each field. Synthetic messages (no API call behind them) are skipped.

    Args:
        path: A ``.jsonl`` transcript.
        since: ISO date or timestamp; earlier lines are ignored. None: no lower bound.
        until: ISO date or timestamp; later lines are ignored. None: no upper bound.

    Returns:
        The transcript's stats; empty when nothing fell in the window.
    """
    stats = TranscriptStats()
    by_request: dict[str, Call] = {}
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return stats
    for number, line in enumerate(lines):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue  # a line cut by a crash; the rest of the file still counts
        if not isinstance(entry, dict):
            continue
        timestamp = str(entry.get("timestamp") or "")
        if (since or until) and not (timestamp and _in_window(timestamp, since, until)):
            continue
        kind = entry.get("type")
        if kind == "system" and entry.get("subtype") == "compact_boundary":
            stats.compactions += 1
            continue
        message = entry.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if kind == "assistant":
            usage = message.get("usage")
            model = str(message.get("model") or "unknown")
            if isinstance(content, list):
                stats.tool_calls += sum(
                    1 for b in content if isinstance(b, dict) and b.get("type") == "tool_use"
                )
            if not isinstance(usage, dict) or model.startswith("<"):
                continue
            tokens = Tokens(
                input=int(usage.get("input_tokens") or 0),
                cache_write=int(usage.get("cache_creation_input_tokens") or 0),
                cache_read=int(usage.get("cache_read_input_tokens") or 0),
                output=int(usage.get("output_tokens") or 0),
            )
            key = str(entry.get("requestId") or message.get("id") or f"line-{number}")
            seen = by_request.get(key)
            if seen is None:
                by_request[key] = Call(model, tokens, timestamp)
            else:
                old = seen.tokens
                seen.tokens = Tokens(
                    max(old.input, tokens.input),
                    max(old.cache_write, tokens.cache_write),
                    max(old.cache_read, tokens.cache_read),
                    max(old.output, tokens.output),
                )
        elif kind == "user" and isinstance(content, list):
            for block in content:
                if not (
                    isinstance(block, dict)
                    and block.get("type") == "tool_result"
                    and block.get("is_error")
                ):
                    continue
                verdict, cause = classify_error(_texts(block.get("content")))
                (stats.refusals if verdict == "refusal" else stats.failures)[cause] += 1
    stats.calls = list(by_request.values())
    return stats


def _agent_type(path: Path) -> str:
    """The subagent type from the ``<agent>.meta.json`` beside its transcript, if any."""
    meta = path.with_name(path.stem + ".meta.json")
    try:
        data = json.loads(meta.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "unknown"
    return str(data.get("agentType") or "unknown") if isinstance(data, dict) else "unknown"


def mangle(path: Path) -> str:
    """The directory name Claude Code gives a project path (non-alphanumerics -> ``-``)."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def default_project() -> Path:
    """The project to measure when none is named: the main checkout of the current repo.

    Run from a git worktree, the sessions that matter were started in the main checkout
    (a worktree's own sessions sit in sibling directories, which ``find_transcripts``
    includes). Outside git, the current directory.

    Returns:
        The resolved project path.
    """
    common = (
        _run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"]) or ""
    ).strip()
    if common and Path(common).name == ".git":
        return Path(common).parent.resolve()
    return Path.cwd().resolve()


def find_transcripts(projects_dir: Path, project: Path | None) -> list[Path]:
    """List transcripts under ``projects_dir``, optionally only one project's.

    A project's sessions started in its worktrees live in sibling directories whose name
    starts with the project's, so those are included.

    Args:
        projects_dir: A Claude Code projects directory (``~/.claude/projects``).
        project: The project path to keep; None keeps every project.

    Returns:
        The ``.jsonl`` files, sorted.
    """
    if not projects_dir.is_dir():
        return []
    roots: list[Path] = [projects_dir]
    if project is not None:
        name = mangle(project)
        roots = [
            d
            for d in projects_dir.iterdir()
            if d.is_dir() and (d.name == name or d.name.startswith(name + "-"))
        ]
    return sorted(p for root in roots for p in root.rglob("*.jsonl"))


@dataclass
class Measurement:
    """The local measurement: populations, agent types and sessions."""

    main: Population = field(default_factory=Population)
    sub: Population = field(default_factory=Population)
    agent_types: dict[str, Population] = field(default_factory=lambda: defaultdict(Population))
    sessions: dict[str, Population] = field(default_factory=lambda: defaultdict(Population))


def measure(
    files: list[Path], since: str | None, until: str | None, rebuild_min: int
) -> Measurement:
    """Read every transcript and split it into main sessions and subagents.

    Args:
        files: The transcripts.
        since: Window start (ISO), or None.
        until: Window end (ISO, inclusive of its prefix), or None.
        rebuild_min: The cache-write size from which a call can be a rebuild.

    Returns:
        The measurement.
    """
    result = Measurement()
    for path in files:
        stats = read_transcript(path, since, until)
        if stats.empty:
            continue
        if "subagents" in path.parts:
            result.sub.add(stats, rebuild_min)
            result.agent_types[_agent_type(path)].add(stats, rebuild_min)
            session = path.parent.parent.name
        else:
            result.main.add(stats, rebuild_min)
            session = path.stem
        result.sessions[session].add(stats, rebuild_min)
    return result


# --------------------------------------------------------------------------- outcomes


def _run(args: list[str]) -> str | None:
    """Run a command and return its stdout, or None when it is absent or fails."""
    try:
        done = subprocess.run(args, capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout if done.returncode == 0 else None


def current_repo() -> str | None:
    """``OWNER/NAME`` of the current directory's GitHub ``origin`` remote, or None."""
    url = (_run(["git", "remote", "get-url", "origin"]) or "").strip()
    found = re.search(r"github\.com[:/]+([^/]+/[^/]+?)(?:\.git)?/?$", url)
    return found.group(1) if found else None


def _search_count(query: str) -> int | None:
    """The ``total_count`` of a GitHub issue/PR search, through ``gh api`` (REST only).

    REST rather than ``gh pr list``: the list commands use GraphQL, which some sandboxes
    and tokens do not allow, and one search call returns the count without paging.
    """
    out = _run(["gh", "api", "-X", "GET", "search/issues", "-f", f"q={query}", "-f", "per_page=1"])
    try:
        return int(json.loads(out)["total_count"]) if out else None
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


def _paged(path: str, stop_key: str, since: str, max_pages: int = 30) -> list[dict[str, Any]]:
    """Page a repository-scoped REST list sorted newest first, until ``stop_key`` < ``since``.

    Args:
        path: The endpoint with its query (without ``page``).
        stop_key: The timestamp field the list is sorted by, descending.
        since: Stop once an item's ``stop_key`` is before this.
        max_pages: A hard limit on calls.

    Returns:
        The items read; may include some older than ``since`` (the caller filters).

    Raises:
        RuntimeError: When the first page cannot be read.
    """
    items: list[dict[str, Any]] = []
    for page in range(1, max_pages + 1):
        out = _run(["gh", "api", f"{path}&per_page=100&page={page}"])
        if out is None:
            if page == 1:
                raise RuntimeError(path)
            break
        batch = json.loads(out)
        items += batch
        if len(batch) < 100 or str(batch[-1].get(stop_key) or "") < since:
            break
    return items


def _rest_outcomes(repo: str, since: str, until: str | None) -> dict[str, int]:
    """Count the window's outcomes through repository-scoped REST lists (no search API)."""

    def inside(ts: object) -> bool:
        return bool(ts) and _in_window(str(ts), since, until)

    by_created = _paged(
        f"repos/{repo}/pulls?state=all&sort=created&direction=desc", "created_at", since
    )
    by_updated = _paged(
        f"repos/{repo}/pulls?state=closed&sort=updated&direction=desc", "updated_at", since
    )
    issues = _paged(
        f"repos/{repo}/issues?state=closed&since={since[:10]}T00:00:00Z", "updated_at", ""
    )
    return {
        "prs_opened": sum(1 for p in by_created if inside(p.get("created_at"))),
        "prs_merged": sum(1 for p in by_updated if inside(p.get("merged_at"))),
        "issues_closed": sum(
            1
            for i in issues
            if "pull_request" not in i
            and inside(i.get("closed_at"))
            and i.get("state_reason") in (None, "completed")
        ),
    }


def gh_outcomes(since: str, until: str | None, repo: str | None) -> dict[str, Any] | None:
    """Count PRs opened and merged and issues closed as completed in the window, via ``gh``.

    The search API answers each count in one call; where it is not allowed (a token bound
    to one repository), repository-scoped lists are paged newest first instead.

    Args:
        since: Window start (``YYYY-MM-DD``).
        until: Window end (``YYYY-MM-DD``), or None for open-ended.
        repo: ``OWNER/NAME``; None takes the ``origin`` remote of the current directory.

    Returns:
        The counts with their ``source``, or None when ``gh`` is absent or every call fails.
    """
    repo = repo or current_repo()
    if shutil.which("gh") is None or not repo:
        return None
    span = f"{since[:10]}..{until[:10]}" if until else f">={since[:10]}"
    opened = _search_count(f"repo:{repo} is:pr created:{span}")
    if opened is not None:
        merged = _search_count(f"repo:{repo} is:pr is:merged merged:{span}")
        closed = _search_count(f"repo:{repo} is:issue is:closed reason:completed closed:{span}")
        return {
            "prs_opened": opened,
            "prs_merged": merged,
            "issues_closed": closed,
            "source": "gh search",
        }
    try:
        return {**_rest_outcomes(repo, since, until), "source": "gh rest"}
    except (RuntimeError, json.JSONDecodeError, TypeError, AttributeError):
        return None


# --------------------------------------------------------------------------- summary


def percentile(values: list[int], share: float) -> int:
    """The nearest-rank percentile of ``values``.

    Args:
        values: The sample.
        share: The percentile as a fraction (0.9 for p90).

    Returns:
        The value, or 0 for an empty sample.
    """
    if not values:
        return 0
    ordered = sorted(values)
    rank = max(0, min(len(ordered) - 1, math.ceil(share * len(ordered)) - 1))
    return ordered[rank]


def _share(part: float, whole: float) -> float | None:
    """``part / whole`` rounded to 4 places, or None when ``whole`` is 0."""
    return round(part / whole, 4) if whole else None


def summarise(pop: Population, cfg: dict[str, Any]) -> dict[str, Any]:
    """Turn a population into the JSON-ready numbers of the record.

    Args:
        pop: The population.
        cfg: Settings: ``budget``, ``weights``, ``top_tier`` and optional ``prices``.

    Returns:
        The summary dict.
    """
    weights = cfg["weights"]
    tokens = pop.tokens()
    calls = pop.calls
    over = sum(1 for c in pop.contexts if c > cfg["budget"])
    top = re.compile(cfg["top_tier"], re.I)
    top_calls = sum(n for m, n in pop.calls_by_model.items() if top.search(m))
    eqt = tokens.eqt(weights)
    summary: dict[str, Any] = {
        "transcripts": pop.transcripts,
        "calls": calls,
        "calls_by_model": dict(pop.calls_by_model.most_common()),
        "top_tier_call_share": _share(top_calls, calls),
        "tokens": tokens.as_dict(),
        "eqt": round(eqt),
        "eqt_by_model": {m: round(t.eqt(weights)) for m, t in pop.tokens_by_model.items()},
        "context": {
            "mean": round(sum(pop.contexts) / calls) if calls else 0,
            "p50": percentile(pop.contexts, 0.5),
            "p90": percentile(pop.contexts, 0.9),
            "max": max(pop.contexts, default=0),
        },
        "over_budget": {"calls": over, "share": _share(over, calls)},
        "cache_read_share": _share(tokens.cache_read, tokens.context),
        "cache_write_per_call": round(tokens.cache_write / calls) if calls else 0,
        "rebuilds": {
            "calls": pop.rebuilds,
            "eqt_share": _share(pop.rebuild_tokens.eqt(weights), eqt),
        },
        "compactions": pop.compactions,
        "tool_calls": pop.tool_calls,
        "refusals": dict(pop.refusals.most_common()),
        "refusals_total": sum(pop.refusals.values()),
        "refusals_per_100_tool_calls": (
            round(100 * sum(pop.refusals.values()) / pop.tool_calls, 2) if pop.tool_calls else None
        ),
        "failures": dict(pop.failures.most_common()),
    }
    if cfg.get("prices"):
        summary["usd_estimate"] = round(
            sum(_usd(m, t, cfg["prices"]) or 0.0 for m, t in pop.tokens_by_model.items()), 2
        )
    return summary


def _usd(model: str, tokens: Tokens, prices: dict[str, dict[str, float]]) -> float | None:
    """Price ``tokens`` of ``model`` from a USD-per-million table matched by substring."""
    for key, price in prices.items():
        if key.lower() in model.lower():
            return (
                tokens.input * price.get("input", 0)
                + tokens.cache_write * price.get("cache_write", 0)
                + tokens.cache_read * price.get("cache_read", 0)
                + tokens.output * price.get("output", 0)
            ) / 1e6
    return None


def cloud_window(sessions: list[dict[str, Any]], previous: dict[str, Any] | None) -> dict[str, Any]:
    """Turn lifetime cloud-session costs into the window's cost.

    ``get_session`` returns a session's LIFETIME cost; the window's cost is that minus the
    lifetime recorded for the same session in the previous record. A session not in the
    previous record counts whole and is flagged, because it may predate the window.

    Args:
        sessions: Items with ``id``, ``cost_usd`` and optional ``title``.
        previous: The previous record, or None.

    Returns:
        ``{"sessions": [...], "window_usd": float, "first_seen": int}``.
    """
    before = {
        s["id"]: float(s.get("lifetime_usd") or 0)
        for s in ((previous or {}).get("cloud") or {}).get("sessions", [])
        if isinstance(s, dict) and "id" in s
    }
    rows = []
    for item in sessions:
        lifetime = float(item.get("cost_usd") or 0)
        sid = str(item.get("id"))
        seen = sid in before
        rows.append(
            {
                "id": sid,
                "title": str(item.get("title") or "")[:60],
                "lifetime_usd": round(lifetime, 2),
                "window_usd": round(lifetime - before[sid] if seen else lifetime, 2),
                "first_seen": not seen,
            }
        )
    return {
        "sessions": rows,
        "window_usd": round(sum(r["window_usd"] for r in rows), 2),
        "first_seen": sum(1 for r in rows if r["first_seen"]),
    }


def headline(record: dict[str, Any]) -> dict[str, Any]:
    """Compute the normalised headline: cost per merged/opened PR and per 10 % of plan.

    Args:
        record: The record so far (``local``, ``cloud``, ``outcomes``, ``plan_used_pct``).

    Returns:
        The headline numbers; a value is None when its inputs are missing.
    """
    local = record.get("local") or {}
    eqt = (local.get("total") or {}).get("eqt") or 0
    usd_local = (local.get("total") or {}).get("usd_estimate")
    cloud = (record.get("cloud") or {}).get("window_usd")
    usd = None if usd_local is None and cloud is None else (usd_local or 0) + (cloud or 0)
    out = record.get("outcomes") or {}
    merged, opened = out.get("prs_merged"), out.get("prs_opened")
    result: dict[str, Any] = {
        "local_eqt": eqt,
        "usd": None if usd is None else round(usd, 2),
        "eqt_per_merged_pr": round(eqt / merged) if merged else None,
        "eqt_per_opened_pr": round(eqt / opened) if opened else None,
        "usd_per_merged_pr": round(usd / merged, 2) if usd is not None and merged else None,
        "usd_per_opened_pr": round(usd / opened, 2) if usd is not None and opened else None,
    }
    plan = record.get("plan_used_pct")
    if plan:
        scale = 10.0 / plan
        result["per_10pct_plan"] = {
            "eqt": round(eqt * scale),
            "usd": None if usd is None else round(usd * scale, 2),
            "prs_merged": None if merged is None else round(merged * scale, 2),
            "prs_opened": None if opened is None else round(opened * scale, 2),
            "issues_closed": (
                None if out.get("issues_closed") is None else round(out["issues_closed"] * scale, 2)
            ),
        }
    return result


def signals(record: dict[str, Any]) -> list[str]:
    """Rule-of-thumb warnings from the numbers (thresholds: ``references/metrics.md``).

    Args:
        record: The record.

    Returns:
        One line per warning; empty when nothing stands out.
    """
    local = record.get("local") or {}
    main, sub = local.get("main") or {}, local.get("subagents") or {}
    total = local.get("total") or {}
    budget = record["settings"]["budget"]
    found = []
    share = (main.get("over_budget") or {}).get("share")
    if share is not None and share > 0.25:
        found.append(
            f"{share:.0%} of main-session calls are over {budget // 1000}k context:"
            " compact earlier or move reading to subagents"
        )
    top = sub.get("top_tier_call_share")
    if top is not None and top > 0.5:
        found.append(
            f"{top:.0%} of subagent calls run on the expensive tier:"
            " pin a cheaper model on agents that do simple work"
        )
    rebuild = (total.get("rebuilds") or {}).get("eqt_share")
    if rebuild is not None and rebuild > 0.2:
        found.append(
            f"{rebuild:.0%} of the cost is cache rebuilds:"
            " sessions idle past the cache lifetime on a large context, or resume too often"
        )
    per100 = total.get("refusals_per_100_tool_calls")
    if per100 is not None and per100 > 2:
        found.append(
            f"{per100} refused tool calls per 100: each is a full-context turn;"
            " allow the safe commands or simplify the ones the guard refuses"
        )
    if (total.get("cache_write_per_call") or 0) > 8000:
        found.append(
            f"{total['cache_write_per_call']:,} tokens of cache write per call:"
            " large tool outputs; cut them at the source (tail, limits, filters)"
        )
    if main.get("calls") and total.get("eqt") and main["eqt"] / total["eqt"] > 0.5:
        found.append(
            f"main sessions are {main['eqt'] / total['eqt']:.0%} of the cost:"
            " heavy reading belongs in subagents that return conclusions"
        )
    return found


# --------------------------------------------------------------------------- comparison

#: (path in the record, label, the better direction or None, settings that must match)
COMPARED: list[tuple[str, str, str | None, tuple[str, ...]]] = [
    ("headline.usd_per_merged_pr", "USD per merged PR", "lower", ()),
    ("headline.eqt_per_merged_pr", "eqt per merged PR", "lower", ("weights",)),
    ("headline.eqt_per_opened_pr", "eqt per opened PR", "lower", ("weights",)),
    ("headline.per_10pct_plan.prs_merged", "merged PRs per 10% of plan", "higher", ()),
    ("headline.per_10pct_plan.issues_closed", "issues closed per 10% of plan", "higher", ()),
    ("local.main.context.mean", "main: mean context per call", "lower", ()),
    ("local.main.context.p90", "main: p90 context per call", "lower", ()),
    ("local.main.over_budget.share", "main: share of calls over budget", "lower", ("budget",)),
    ("local.subagents.context.mean", "subagents: mean context per call", "lower", ()),
    (
        "local.subagents.top_tier_call_share",
        "subagents: share of calls on the expensive tier",
        "lower",
        ("top_tier",),
    ),
    ("local.main_eqt_share", "main sessions' share of the cost", "lower", ("weights",)),
    ("local.total.cache_write_per_call", "cache write per call", "lower", ()),
    ("local.total.rebuilds.eqt_share", "share of cost in cache rebuilds", "lower", ("weights",)),
    ("local.total.refusals_per_100_tool_calls", "refused tool calls per 100", "lower", ()),
    ("local.total.cache_read_share", "cache-read share of input", None, ()),
]

_WHY_NO_DIRECTION = (
    "no better direction: a higher share means more cache reuse but usually a larger context"
)


def _get(record: dict[str, Any], path: str) -> Any:
    """Read a dotted path from a nested dict; None when any step is missing."""
    node: Any = record
    for part in path.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


def compare(
    current: dict[str, Any], previous: dict[str, Any] | None, tolerance: float
) -> list[dict[str, Any]]:
    """Class each compared metric as better, worse, unchanged or unknown, with the reason.

    Rates and shares are compared, never window totals, so windows of different lengths
    stay comparable; totals enter only through "per 10 % of plan".

    Args:
        current: This run's record.
        previous: The previous record, or None.
        tolerance: Relative change under which a metric is unchanged.

    Returns:
        One dict per metric: ``metric``, ``class``, ``before``, ``after``, ``reason``.
    """
    findings = []
    for path, label, better, needs in COMPARED:
        after = _get(current, path)
        before = _get(previous, path) if previous else None
        row = {"metric": label, "before": before, "after": after}
        changed = [
            k for k in needs if previous and previous["settings"].get(k) != current["settings"][k]
        ]
        if previous is None:
            row.update({"class": "unknown", "reason": "no previous record"})
        elif before is None or after is None:
            side = "previous record" if before is None else "this window"
            row.update({"class": "unknown", "reason": f"missing in the {side}"})
        elif changed:
            row.update({"class": "unknown", "reason": f"settings changed: {', '.join(changed)}"})
        else:
            delta = (after - before) / abs(before) if before else (1.0 if after else 0.0)
            share = path.endswith("share")
            arrow = f"{_fmt(before, share)} -> {_fmt(after, share)} ({delta:+.0%})"
            if better is None:
                row.update({"class": "unknown", "reason": f"{arrow}; {_WHY_NO_DIRECTION}"})
            elif abs(delta) <= tolerance:
                row.update({"class": "unchanged", "reason": f"{arrow}, within {tolerance:.0%}"})
            else:
                good = (delta < 0) == (better == "lower")
                row.update(
                    {
                        "class": "better" if good else "worse",
                        "reason": f"{arrow}; {better} is better",
                    }
                )
        findings.append(row)
    return findings


def _fmt(value: Any, share: bool = False) -> str:
    """Format a number for the report.

    Args:
        value: The number, or None.
        share: True when the value is a fraction to print as a percentage.

    Returns:
        ``-`` for None, a percentage for a share, large counts with k/M suffixes.
    """
    if value is None:
        return "-"
    if share:
        return f"{value:.1%}"
    if abs(value) >= 1e6:
        return f"{value / 1e6:.2f}M"
    if abs(value) >= 1e4:
        return f"{value / 1e3:.0f}k"
    return f"{value:,.2f}" if isinstance(value, float) else f"{value:,}"


# --------------------------------------------------------------------------- records


def last_record(path: Path) -> dict[str, Any] | None:
    """The last valid record in a JSONL records file, or None.

    Args:
        path: The records file.

    Returns:
        The record dict.
    """
    if not path.is_file():
        return None
    found = None
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict) and item.get("schema") == SCHEMA:
            found = item
    return found


def append_record(path: Path, record: dict[str, Any]) -> None:
    """Append ``record`` as one JSON line, creating the file and its directory.

    Args:
        path: The records file.
        record: The record.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


# --------------------------------------------------------------------------- report


def build_record(
    m: Measurement, cfg: dict[str, Any], args: argparse.Namespace, previous: dict[str, Any] | None
) -> dict[str, Any]:
    """Assemble the dated record of this run.

    Args:
        m: The local measurement.
        cfg: Settings (budget, weights, top tier, rebuild size, prices).
        args: The parsed command line (window, label, outcomes, cloud, plan).
        previous: The previous record, used for the cloud window.

    Returns:
        The record.
    """
    total = Population()
    for pop in (m.main, m.sub):
        total.transcripts += pop.transcripts
        total.calls_by_model.update(pop.calls_by_model)
        for model, tokens in pop.tokens_by_model.items():
            total.tokens_by_model[model].add(tokens)
        total.contexts += pop.contexts
        total.rebuilds += pop.rebuilds
        total.rebuild_tokens.add(pop.rebuild_tokens)
        total.tool_calls += pop.tool_calls
        total.refusals.update(pop.refusals)
        total.failures.update(pop.failures)
        total.compactions += pop.compactions
    main_s, total_s = summarise(m.main, cfg), summarise(total, cfg)
    sessions = sorted(m.sessions.items(), key=lambda kv: -kv[1].tokens().eqt(cfg["weights"]))
    record: dict[str, Any] = {
        "schema": SCHEMA,
        "label": args.label or "",
        "measured_at": datetime.now(timezone.utc).strftime(  # noqa: UP017 - Python 3.9+
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "window": {"since": args.since, "until": args.until},
        "scope": args.scope,
        "settings": {
            "budget": cfg["budget"],
            "weights": cfg["weights"],
            "top_tier": cfg["top_tier"],
            "rebuild_min": cfg["rebuild_min"],
        },
        "local": {
            "main": main_s,
            "subagents": summarise(m.sub, cfg),
            "total": total_s,
            "main_eqt_share": _share(main_s["eqt"], total_s["eqt"]),
            "agent_types": {
                name: {
                    "calls": pop.calls,
                    "calls_by_model": dict(pop.calls_by_model.most_common()),
                    "eqt": round(pop.tokens().eqt(cfg["weights"])),
                }
                for name, pop in sorted(m.agent_types.items(), key=lambda kv: -kv[1].calls)
            },
            "top_sessions": [
                {
                    "session": sid[:8],
                    "calls": pop.calls,
                    "eqt": round(pop.tokens().eqt(cfg["weights"])),
                    "mean_context": round(sum(pop.contexts) / pop.calls) if pop.calls else 0,
                    "over_budget_share": _share(
                        sum(1 for c in pop.contexts if c > cfg["budget"]), pop.calls
                    ),
                }
                for sid, pop in sessions[:10]
            ],
        },
        "cloud": None,
        "outcomes": None,
        "plan_used_pct": args.plan_used,
    }
    if args.cloud:
        items = json.loads(Path(args.cloud).read_text(encoding="utf-8"))
        record["cloud"] = cloud_window(items if isinstance(items, list) else [], previous)
    manual = {
        "prs_opened": args.prs_opened,
        "prs_merged": args.prs_merged,
        "issues_closed": args.issues_closed,
    }
    if any(v is not None for v in manual.values()):
        record["outcomes"] = {**manual, "source": "manual"}
    elif args.outcomes == "auto" and args.since:
        record["outcomes"] = gh_outcomes(args.since, args.until, args.repo)
    record["headline"] = headline(record)
    record["signals"] = signals(record)
    return record


def _population_lines(name: str, s: dict[str, Any], budget: int) -> list[str]:
    """Format one population summary for the text report."""
    if not s["calls"]:
        return [f"{name}: no calls"]
    models = ", ".join(
        f"{model} {n} ({n / s['calls']:.0%})" for model, n in s["calls_by_model"].items()
    )
    ctx = s["context"]
    refusals = ", ".join(f"{c} {n}" for c, n in s["refusals"].items()) or "none"
    failures = ", ".join(f"{c} {n}" for c, n in s["failures"].items()) or "none"
    return [
        f"{name}: {s['transcripts']} transcripts, {s['calls']:,} calls, {_fmt(s['eqt'])} eqt",
        f"  models:            {models}",
        f"  context per call:  mean {_fmt(ctx['mean'])}  p50 {_fmt(ctx['p50'])}"
        f"  p90 {_fmt(ctx['p90'])}  max {_fmt(ctx['max'])}",
        f"  over {budget // 1000}k:          {s['over_budget']['calls']:,}"
        f" ({_fmt(s['over_budget']['share'], True)})",
        f"  cache:             reads {_fmt(s['cache_read_share'], True)} of input,"
        f" write {s['cache_write_per_call']:,}/call, {s['rebuilds']['calls']} rebuilds"
        f" ({_fmt(s['rebuilds']['eqt_share'], True)} of cost), {s['compactions']} compactions",
        f"  refused tool calls: {s['refusals_total']} of {s['tool_calls']:,} ({refusals})",
        f"  failed tool calls:  {failures}",
    ]


def render(record: dict[str, Any], findings: list[dict[str, Any]], previous: Any) -> str:
    """Format the record and the comparison as the printed report.

    Args:
        record: This run's record.
        findings: The output of :func:`compare`.
        previous: The previous record, or None.

    Returns:
        The report text.
    """
    w, h, local = record["window"], record["headline"], record["local"]
    out = [
        f"Claude Code usage  {w['since'] or 'start'} .. {w['until'] or 'now'}  ({record['scope']})",
        "",
        "Headline",
        f"  local: {_fmt(h['local_eqt'])} eqt"
        + (
            f"   cloud: ${record['cloud']['window_usd']:,.2f}"
            f" ({len(record['cloud']['sessions'])} sessions,"
            f" {record['cloud']['first_seen']} first seen)"
            if record["cloud"]
            else "   cloud: not given"
        )
        + (f"   USD total: ${h['usd']:,.2f}" if h["usd"] is not None else ""),
    ]
    o = record["outcomes"]
    if o:
        out.append(
            f"  PRs opened {o['prs_opened']}, merged {o['prs_merged']},"
            f" issues closed {o['issues_closed']}  (source: {o['source']})"
        )
        out.append(
            f"  per merged PR: {_fmt(h['eqt_per_merged_pr'])} eqt"
            + (f", ${h['usd_per_merged_pr']}" if h["usd_per_merged_pr"] is not None else "")
            + f"   per opened PR: {_fmt(h['eqt_per_opened_pr'])} eqt"
            + (f", ${h['usd_per_opened_pr']}" if h["usd_per_opened_pr"] is not None else "")
        )
    else:
        out.append("  outcomes: unknown (no gh; pass --prs-opened/--prs-merged/--issues-closed)")
    if "per_10pct_plan" in h:
        p = h["per_10pct_plan"]
        out.append(
            f"  per 10% of plan (window used {record['plan_used_pct']}%): {_fmt(p['eqt'])} eqt,"
            f" merged PRs {p['prs_merged']}, opened PRs {p['prs_opened']},"
            f" issues closed {p['issues_closed']}"
        )
    budget = record["settings"]["budget"]
    out += [""] + _population_lines("Main sessions", local["main"], budget)
    out += _population_lines("Subagents", local["subagents"], budget)
    if local["main_eqt_share"] is not None:
        out.append(f"Main sessions' share of the cost: {_fmt(local['main_eqt_share'], True)}")
    if local["agent_types"]:
        out.append("Subagents by type (calls, eqt, models):")
        for name, t in local["agent_types"].items():
            models = ", ".join(f"{m} {n}" for m, n in t["calls_by_model"].items())
            out.append(f"  {name:<24} {t['calls']:>6}  {_fmt(t['eqt']):>8}  {models}")
    if local["top_sessions"]:
        out.append("Top sessions by eqt (session, calls, eqt, mean context, over budget):")
        for s in local["top_sessions"][:5]:
            out.append(
                f"  {s['session']}  {s['calls']:>6}  {_fmt(s['eqt']):>8}"
                f"  {_fmt(s['mean_context']):>6}  {_fmt(s['over_budget_share'], True)}"
            )
    out += ["", "Signals"] + [f"  - {s}" for s in record["signals"] or ["none"]]
    if previous:
        out += [
            "",
            f"Against the previous record ({previous.get('label') or 'unlabelled'},"
            f" {previous.get('measured_at', '?')[:10]}, window"
            f" {previous['window'].get('since')} .. {previous['window'].get('until') or 'run'})",
        ]
        for f in findings:
            out.append(f"  {f['class']:<9} {f['metric']}: {f['reason']}")
    else:
        out += ["", "Against the previous record: none yet (every finding is unknown)"]
    return "\n".join(out)


# --------------------------------------------------------------------------- self-test


def _write_jsonl(path: Path, entries: list[dict[str, Any]]) -> None:
    """Write synthetic transcript entries, one JSON per line."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")


def _assistant(ts: str, req: str, model: str, i: int, cw: int, cr: int, o: int) -> dict[str, Any]:
    """A synthetic assistant line with usage and one tool call."""
    usage = {
        "input_tokens": i,
        "cache_creation_input_tokens": cw,
        "cache_read_input_tokens": cr,
        "output_tokens": o,
    }
    content = [{"type": "tool_use", "name": "Bash", "input": {}}]
    return {
        "type": "assistant",
        "timestamp": ts,
        "requestId": req,
        "message": {"model": model, "usage": usage, "content": content},
    }


def _error(ts: str, text: str) -> dict[str, Any]:
    """A synthetic user line holding one error tool result."""
    block = {"type": "tool_result", "is_error": True, "content": text}
    return {"type": "user", "timestamp": ts, "message": {"content": [block]}}


def self_test() -> int:
    """Run the script end to end on a synthetic transcript tree and check the numbers.

    Returns:
        0 when every check passes; raises AssertionError otherwise.
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "projects" / "-tmp-demo"
        session = root / "sess-0001.jsonl"
        _write_jsonl(
            session,
            [
                _assistant("2025-01-01T09:00:00Z", "r0", "model-opus-x", 5, 1000, 0, 10),  # before
                _assistant("2025-01-02T09:00:00Z", "r1", "model-opus-x", 10, 300_000, 0, 50),
                _assistant("2025-01-02T09:00:01Z", "r1", "model-opus-x", 10, 300_000, 0, 400),
                _assistant("2025-01-02T09:05:00Z", "r2", "model-opus-x", 10, 2_000, 300_000, 100),
                {
                    "type": "system",
                    "subtype": "compact_boundary",
                    "timestamp": "2025-01-02T09:06:00Z",
                },
                _assistant("2025-01-02T09:07:00Z", "r3", "<synthetic>", 0, 0, 0, 0),
                _error(
                    "2025-01-02T09:01:00Z",
                    "This agent is isolated in the worktree x, but it is too complex to verify",
                ),
                _error("2025-01-02T09:02:00Z", "Exit code 1\nboom"),
                "not json",  # type: ignore[list-item]
            ],
        )
        sub = root / "sess-0001" / "subagents" / "agent-a1.jsonl"
        _write_jsonl(
            sub,
            [
                _assistant("2025-01-02T10:00:00Z", "s1", "model-sonnet-y", 20, 5_000, 10_000, 200),
                _error(
                    "2025-01-02T10:01:00Z", "The user doesn't want to proceed with this tool use."
                ),
            ],
        )
        sub.with_name("agent-a1.meta.json").write_text('{"agentType": "reviewer"}', "utf-8")
        cloud = Path(tmp) / "cloud.json"
        cloud.write_text(json.dumps([{"id": "c1", "title": "t", "cost_usd": 12.5}]), "utf-8")
        records = Path(tmp) / "records.jsonl"
        common = [
            "--projects-dir",
            str(root.parent),
            "--all-projects",
            "--since",
            "2025-01-02",
            "--outcomes",
            "off",
            "--record",
            str(records),
            "--cloud",
            str(cloud),
        ]
        assert (
            main(
                [
                    *common,
                    "--prs-merged",
                    "2",
                    "--prs-opened",
                    "4",
                    "--issues-closed",
                    "1",
                    "--plan-used",
                    "20",
                    "--label",
                    "first",
                    "--quiet",
                ]
            )
            == 0
        )
        first = last_record(records)
        assert first is not None
        loc = first["local"]
        assert loc["main"]["calls"] == 2, loc["main"]["calls"]
        assert loc["main"]["tokens"]["output"] == 500, "the largest output per request counts"
        assert loc["main"]["over_budget"]["calls"] == 2
        assert loc["main"]["rebuilds"]["calls"] == 1
        assert loc["main"]["compactions"] == 1
        assert loc["main"]["refusals"] == {"worktree guard": 1}
        assert loc["main"]["failures"] == {"command exit code": 1}
        assert loc["subagents"]["refusals"] == {"user rejected": 1}
        assert loc["agent_types"]["reviewer"]["calls"] == 1
        assert loc["subagents"]["top_tier_call_share"] == 0.0
        eqt = 10 + 300_000 * 2 + 400 * 5 + 10 + 2_000 * 2 + 300_000 * 0.1 + 100 * 5
        assert loc["main"]["eqt"] == round(eqt), (loc["main"]["eqt"], eqt)
        assert first["cloud"]["window_usd"] == 12.5 and first["cloud"]["first_seen"] == 1
        assert first["headline"]["per_10pct_plan"]["prs_merged"] == 1.0
        assert first["headline"]["usd_per_merged_pr"] == 6.25
        # Second run: more merged PRs and a grown cloud session -> better per merged PR.
        cloud.write_text(json.dumps([{"id": "c1", "cost_usd": 15.0}]), "utf-8")
        assert (
            main(
                [
                    *common,
                    "--prs-merged",
                    "4",
                    "--prs-opened",
                    "4",
                    "--issues-closed",
                    "1",
                    "--plan-used",
                    "20",
                    "--quiet",
                ]
            )
            == 0
        )
        second = last_record(records)
        assert second is not None and second["cloud"]["window_usd"] == 2.5
        found = {f["metric"]: f for f in compare(second, first, DEFAULT_TOLERANCE)}
        assert found["USD per merged PR"]["class"] == "better", found["USD per merged PR"]
        assert found["main: p90 context per call"]["class"] == "unchanged"
        assert found["cache-read share of input"]["class"] == "unknown"
        # An empty projects directory is reported, not a crash.
        empty = Path(tmp) / "empty"
        empty.mkdir()
        assert (
            main(
                [
                    "--projects-dir",
                    str(empty),
                    "--all-projects",
                    "--no-record",
                    "--outcomes",
                    "off",
                    "--quiet",
                ]
            )
            == 1
        )
    print("self-test OK")
    return 0


# --------------------------------------------------------------------------- main


def _weights(text: str | None) -> dict[str, float]:
    """Parse ``--weights input=1,cache_write=2,...`` over the defaults.

    Args:
        text: The option value, or None.

    Returns:
        The weights.

    Raises:
        argparse.ArgumentTypeError: On an unknown key or a non-number.
    """
    weights = dict(DEFAULT_WEIGHTS)
    for part in filter(None, (text or "").split(",")):
        key, _, value = part.partition("=")
        key = key.strip()
        if key not in weights:
            raise argparse.ArgumentTypeError(f"unknown weight {key!r}; use {sorted(weights)}")
        try:
            weights[key] = float(value)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"weight {key} is not a number: {value!r}") from exc
    return weights


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Parse the command line.

    Args:
        argv: Arguments; ``sys.argv[1:]`` when None.

    Returns:
        The namespace.
    """
    p = argparse.ArgumentParser(description=(__doc__ or "").split("\n", 1)[0])
    p.add_argument("--since", help="window start, ISO date or timestamp (UTC)")
    p.add_argument("--until", help="window end, inclusive, ISO date or timestamp (UTC)")
    p.add_argument("--projects-dir", type=Path, default=Path.home() / ".claude" / "projects")
    p.add_argument(
        "--project",
        type=Path,
        help="project path to measure (default: the main checkout of the current git repo, else cwd)",
    )
    p.add_argument("--all-projects", action="store_true", help="measure every project")
    p.add_argument("--budget", type=int, default=DEFAULT_BUDGET, help="context budget per call")
    p.add_argument(
        "--weights",
        type=_weights,
        default=None,
        help="eqt weights, e.g. input=1,cache_write=2,cache_read=0.1,output=5",
    )
    p.add_argument("--top-tier", default=DEFAULT_TOP_TIER, help="regex of expensive models")
    p.add_argument("--rebuild-min", type=int, default=DEFAULT_REBUILD_MIN)
    p.add_argument(
        "--prices",
        type=Path,
        help="JSON {model-substring: {input,cache_write,"
        "cache_read,output}} in USD per million tokens, for a local USD estimate",
    )
    p.add_argument(
        "--cloud",
        type=Path,
        help="JSON list [{id, title, cost_usd}] of cloud sessions' LIFETIME cost from get_session",
    )
    p.add_argument(
        "--outcomes", choices=("auto", "off"), default="auto", help="auto: count PRs/issues with gh"
    )
    p.add_argument("--repo", help="OWNER/NAME for gh (default: the current repository)")
    p.add_argument("--prs-opened", type=int)
    p.add_argument("--prs-merged", type=int)
    p.add_argument("--issues-closed", type=int)
    p.add_argument("--plan-used", type=float, help="percent of the plan the window used")
    p.add_argument("--record", type=Path, default=Path("docs/claude/usage-records.jsonl"))
    p.add_argument("--no-record", action="store_true", help="do not append a record")
    p.add_argument("--label", help="a short label stored with the record")
    p.add_argument("--tolerance", type=float, default=DEFAULT_TOLERANCE)
    p.add_argument("--json", action="store_true", help="print the record and diff as JSON")
    p.add_argument("--quiet", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--self-test", action="store_true", help="run the built-in self-test")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Measure, record and compare.

    Args:
        argv: Command-line arguments; ``sys.argv[1:]`` when None.

    Returns:
        0 on success; 1 when there is nothing to measure (no transcript in the window and
        no cloud data), because a measurement of nothing must not look like a pass.
    """
    args = parse_args(argv)
    if args.self_test:
        return self_test()
    project = None
    if not args.all_projects:
        project = args.project.resolve() if args.project else default_project()
    if args.projects_dir != Path.home() / ".claude" / "projects" and args.project is None:
        project = None  # an explicit directory is measured whole unless --project narrows it
    cfg: dict[str, Any] = {
        "budget": args.budget,
        "weights": args.weights or dict(DEFAULT_WEIGHTS),
        "top_tier": args.top_tier,
        "rebuild_min": args.rebuild_min,
        "prices": json.loads(args.prices.read_text(encoding="utf-8")) if args.prices else None,
    }
    args.scope = str(project) if project else f"all projects under {args.projects_dir}"
    files = find_transcripts(args.projects_dir, project)
    m = measure(files, args.since, args.until, args.rebuild_min)
    if not (m.main.transcripts or m.sub.transcripts or args.cloud):
        if args.quiet:
            return 1
        where = f"{args.projects_dir}" + (f" for {project}" if project else "")
        print(
            f"no transcripts in the window under {where}"
            " (try --all-projects or an earlier --since)",
            file=sys.stderr,
        )
        return 1
    previous = last_record(args.record)
    record = build_record(m, cfg, args, previous)
    findings = compare(record, previous, args.tolerance)
    if not args.no_record:
        append_record(args.record, record)
    if args.quiet:
        return 0
    if args.json:
        print(json.dumps({"record": record, "comparison": findings}, indent=2, ensure_ascii=False))
    else:
        print(render(record, findings, previous))
        if not args.no_record:
            print(f"\nrecord appended to {args.record}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
