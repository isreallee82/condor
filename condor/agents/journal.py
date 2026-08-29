"""JournalManager -- compact persistent memory for trading agents.

Data is organized by strategy (a playbook owned by an Agent) and session::

    agents/
        {agent_slug}/
            strategies/
                {strategy_slug}/
                    strategy.md        # strategy definition (tactic + config)
                    config.yml         # runtime config
                    learnings.md       # cross-session learnings
                    dry_runs/          # experiment snapshots (experiment_N.md)
                    sessions/
                        session_1/
                            journal.md  # summary + decisions + ticks + executors
                            snapshots/
                                snapshot_1.md
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_DATA_ROOT = Path(__file__).parent.parent.parent / "agents"

# Cap on entries PER injected section, so the two sections together can reach
# twice this many -- learnings.md's own header budgets ~20 across both.
# Overflow is retired rather than deleted; see write_learning.
MAX_LEARNINGS = 20

# Fewest content words a correction must carry before it is allowed to name an
# entry to replace. Below this it cannot identify one entry rather than
# another, so it is written as a new learning instead of overwriting something.
MIN_TARGET_WORDS = 3

# Word overlap above which a new learning reads as a repeat of an existing one.
# The same threshold bounds the supersede search, so anything the dedup can
# suppress is something an explicit correction can replace (see write_learning).
DEDUP_THRESHOLD = 0.5

# Where text pushed out of an injected section is parked. read_learnings only
# emits LEARNING_CATEGORIES, so this section never reaches the prompt and costs
# nothing to keep -- it exists so a retired lesson is not re-learnt from scratch.
RETIRED_SECTION = "Retired Insights"

_EMPTY_DETAIL = (
    "nothing was written: the text is empty once its leading date stamp is "
    "removed (a stamp is added for you), and an empty bullet would take a "
    "learnings slot in every future prompt"
)

_CORRECTION_HINT = (
    "The comparison is a bag of words and cannot see negation, so a CORRECTION "
    "of that entry scores like a repeat of it and lands here too. If this "
    "contradicts the existing entry rather than restating it, write it as a "
    "correction (supersede): that REPLACES the entry, which is the only way to "
    "change one."
)


def _strategy_base_dir(prefix: str) -> Path:
    """Resolve the per-strategy base dir from an agent_id prefix.

    New format prefixes are ``"{agent_slug}.{strategy_slug}"`` →
    ``agents/{agent_slug}/strategies/{strategy_slug}/``. Legacy flat
    prefixes (no dot) fall back to ``agents/{slug}/`` so old ids still
    resolve.
    """
    if "." in prefix:
        agent_slug, sslug = prefix.split(".", 1)
        return _DATA_ROOT / agent_slug / "strategies" / sslug
    return _DATA_ROOT / prefix


def resolve_agent_dirs(agent_id: str) -> tuple[Path | None, Path | None]:
    """Derive (session_dir, base_dir) from an agent_id.

    agent_id format: ``"{agent_slug}.{strategy_slug}_{N}"`` (session) or
    ``"..._e{N}"`` (experiment). ``base_dir`` is the strategy folder that holds
    ``sessions/`` and ``learnings.md``.

    Returns (None, None) if the path doesn't exist on disk.
    """
    last_sep = agent_id.rfind("_")
    if last_sep == -1:
        return None, None
    prefix = agent_id[:last_sep]
    num_part = agent_id[last_sep + 1 :]

    base_dir = _strategy_base_dir(prefix)
    if not base_dir.is_dir():
        return None, None

    # Experiments (e.g. "e3") are flat files, not directories
    if num_part.startswith("e"):
        return None, base_dir

    try:
        session_num = int(num_part)
    except ValueError:
        return None, None
    session_dir = base_dir / "sessions" / f"session_{session_num}"
    return session_dir, base_dir


MAX_SNAPSHOTS = 100

JOURNAL_TEMPLATE = """\
# Journal - {agent_id}

## Summary
No ticks yet.

## Decisions

## Ticks

## Executors

## Snapshots
"""

LEARNINGS_TEMPLATE = """\
# Learnings

## Market Observations

## Execution Notes

## Retired Insights
"""

LEARNING_CATEGORIES = {
    "market": "Market Observations",
    "execution": "Execution Notes",
}
DEFAULT_LEARNING_CATEGORY = "market"

SNAPSHOT_TEMPLATE = """\
# Snapshot #{tick} — {timestamp}

<details><summary>System Prompt ({prompt_len} chars)</summary>

{system_prompt}

</details>

## Executor State
{executors_data}

## Risk State
{risk_state}

## Agent Response
{response_text}

## Tool Calls ({tool_count})

{tool_calls}

## Stats
Duration: {duration:.1f}s
"""


def get_session_dir(run_key: str, session_number: int) -> Path:
    """Build the path for a specific session directory.

    ``run_key`` is the ``"{agent_slug}.{strategy_slug}"`` prefix (legacy flat
    slugs without a dot still resolve to ``agents/{slug}/``).
    """
    return _strategy_base_dir(run_key) / "sessions" / f"session_{session_number}"


def next_session_number(agent_dir: Path) -> int:
    """Determine the next session number by scanning existing session_* dirs."""
    # Check new location first
    sessions_dir = agent_dir / "sessions"
    if not sessions_dir.exists():
        # Check legacy location
        legacy_dir = agent_dir / "trading_sessions"
        if legacy_dir.exists():
            sessions_dir = legacy_dir
        else:
            return 1
    existing = [
        int(d.name.split("_", 1)[1])
        for d in sessions_dir.iterdir()
        if d.is_dir() and d.name.startswith("session_")
    ]
    return max(existing, default=0) + 1


def next_experiment_number(agent_dir: Path) -> int:
    """Determine the next experiment number by scanning experiment_*.md files.

    Checks both dry_runs/ (new) and experiments/ (legacy) directories.
    """
    existing = []
    for dir_name in ("dry_runs", "experiments"):
        d = agent_dir / dir_name
        if not d.exists():
            continue
        for f in d.iterdir():
            if f.is_file() and f.suffix == ".md":
                m = re.match(r"experiment_(\d+)\.md", f.name)
                if m:
                    existing.append(int(m.group(1)))
    return max(existing, default=0) + 1


# Line format written by JournalManager.record_tick; count_journal_ticks and
# _count_ticks parse it back, so the format has a single owner here.
TICK_LINE_PREFIX = "- tick#"


def count_journal_ticks(journal_path: Path) -> int:
    """Count tick entries in a session's journal.md (lines record_tick writes)."""
    if not journal_path.exists():
        return 0
    text = journal_path.read_text(errors="replace")
    return sum(1 for line in text.splitlines() if line.startswith(TICK_LINE_PREFIX))


EXPERIMENT_TEMPLATE = """\
# Experiment #{num} — {timestamp}
Mode: {execution_mode}
Model: {agent_key}

<details><summary>System Prompt ({prompt_len} chars)</summary>

{system_prompt}

</details>

## Executor State
{executors_data}

## Risk State
{risk_state}

## Agent Response
{response_text}

## Tool Calls ({tool_count})

{tool_calls}

## Stats
Duration: {duration:.1f}s
"""


def save_experiment_snapshot(
    agent_dir: Path,
    experiment_num: int,
    execution_mode: str,
    timestamp: str,
    system_prompt: str,
    response_text: str,
    tool_calls: list[dict],
    executors_data: str,
    risk_state: dict,
    duration: float,
    agent_key: str = "",
) -> Path:
    """Save a single experiment snapshot as a flat .md file."""
    experiments_dir = agent_dir / "dry_runs"
    experiments_dir.mkdir(parents=True, exist_ok=True)

    # Format risk state
    max_dd = risk_state.get("max_drawdown_pct", -1)
    dd_display = (
        f"{risk_state.get('drawdown_pct', 0):.1f}% / {max_dd:.1f}% limit"
        if max_dd >= 0
        else "disabled"
    )
    risk_lines = [
        f"- Position Size: ${risk_state.get('total_exposure', 0):.2f} / ${risk_state.get('max_position_size', 500):.2f} limit",
        f"- Open Executors: {risk_state.get('executor_count', 0)} / {risk_state.get('max_open_executors', 5)} limit",
        f"- Drawdown: {dd_display}",
        f"- Status: {'BLOCKED - ' + risk_state.get('block_reason', '') if risk_state.get('is_blocked') else 'ACTIVE'}",
    ]

    # Format tool calls
    import json

    tool_parts = []
    for i, tc in enumerate(tool_calls, 1):
        tc_name = tc.get("name", tc.get("title", "unknown"))
        tc_status = tc.get("status", "")
        tool_parts.append(f"### {i}. {tc_name} ({tc_status})")
        if tc.get("input"):
            input_str = (
                json.dumps(tc["input"], indent=2)
                if isinstance(tc["input"], dict)
                else str(tc["input"])
            )
            tool_parts.append(f"**Input:**\n```json\n{input_str}\n```")
        if tc.get("output"):
            output_str = str(tc["output"])[:2000]
            tool_parts.append(f"**Output:**\n```\n{output_str}\n```")
        tool_parts.append("")

    content = EXPERIMENT_TEMPLATE.format(
        num=experiment_num,
        timestamp=timestamp,
        execution_mode=execution_mode,
        agent_key=agent_key or "unknown",
        prompt_len=len(system_prompt),
        system_prompt=system_prompt,
        executors_data=executors_data or "No executors.",
        risk_state="\n".join(risk_lines),
        response_text=response_text or "No response.",
        tool_count=len(tool_calls),
        tool_calls="\n".join(tool_parts) or "No tool calls.",
        duration=duration,
    )

    path = experiments_dir / f"experiment_{experiment_num}.md"
    path.write_text(content)
    return path


class JournalManager:
    """Read/write journal + tracker for one agent session.

    Combines living memory (Summary) with execution tracking
    (Decisions, Ticks, Executors, Snapshots) in a single ``journal.md`` file.
    Learnings are stored separately in ``{agent_dir}/learnings.md``.
    Full snapshots go into ``snapshots/snapshot_N.md``.
    """

    def __init__(
        self,
        agent_id: str,
        strategy_name: str = "",
        strategy_description: str = "",
        session_dir: Path | None = None,
        agent_dir: Path | None = None,
    ):
        self.agent_id = agent_id
        if session_dir:
            self._session_dir = session_dir
        else:
            # Try to resolve from agent_id before falling back
            resolved_session, resolved_agent = resolve_agent_dirs(agent_id)
            self._session_dir = (
                resolved_session if resolved_session else _DATA_ROOT / agent_id
            )
            if not agent_dir and resolved_agent:
                agent_dir = resolved_agent
        self._agent_dir = agent_dir  # For cross-session learnings
        self._path = self._session_dir / "journal.md"
        self._snapshots_dir = self._session_dir / "snapshots"
        self._session_dir.mkdir(parents=True, exist_ok=True)

        # Also support legacy runs/ dir for reading
        self._legacy_runs_dir = self._session_dir / "runs"

        # Read cache: journal.md text keyed by (mtime_ns, size), plus parsed
        # sections derived from that text. Invalidated when the stamp changes
        # (external writers, e.g. the MCP journal_write tool) or explicitly on
        # our own writes (mtime granularity could otherwise mask them).
        self._cache_stamp: tuple[int, int] | None = None
        self._cache_text: str = ""
        self._parsed_cache: dict[str, list[dict]] = {}

        if not self._path.exists():
            self._path.write_text(JOURNAL_TEMPLATE.format(agent_id=agent_id))

        # Ensure learnings.md exists at agent level
        if self._agent_dir:
            learnings_path = self._agent_dir / "learnings.md"
            if not learnings_path.exists():
                learnings_path.write_text(LEARNINGS_TEMPLATE)

        self._tick_count = self._count_ticks()

    # ------------------------------------------------------------------
    # Learnings (cross-session, stored in agent_dir/learnings.md)
    # ------------------------------------------------------------------

    def _learnings_path(self) -> Path | None:
        """Get the learnings file path."""
        if self._agent_dir:
            return self._agent_dir / "learnings.md"
        # Fallback: try to find learnings in session dir parent
        parent = self._session_dir.parent
        if parent.name == "sessions" or parent.name == "trading_sessions":
            return parent.parent / "learnings.md"
        return None

    def read_learnings(self) -> str:
        """Return the learnings content (cross-session), grouped by category."""
        path = self._learnings_path()
        if path and path.exists():
            text = path.read_text()
            parts = []
            # Read each category section
            for cat_key, cat_header in LEARNING_CATEGORIES.items():
                m = re.search(
                    rf"^## {re.escape(cat_header)}\n(.*?)(?=^## |\Z)",
                    text,
                    re.MULTILINE | re.DOTALL,
                )
                if m and m.group(1).strip():
                    parts.append(f"**{cat_header}:**\n{m.group(1).strip()}")
            if parts:
                return "\n\n".join(parts)
            # Legacy fallback: try Active Insights
            m = re.search(
                r"^## Active Insights\n(.*?)(?=^## |\Z)", text, re.MULTILINE | re.DOTALL
            )
            if m:
                return m.group(1).strip()
            lines = text.strip().splitlines()
            content = [l for l in lines if not l.startswith("# ")]
            return "\n".join(content).strip()
        # Fallback: read from journal Learnings section (legacy)
        return self._get_section("Learnings")

    def append_learning(
        self,
        text_content: str,
        category: str = "",
        supersede: bool = False,
        replaces: str = "",
    ) -> bool:
        """Add a learning under a category, deduplicating against existing ones.

        Thin boolean wrapper over :meth:`write_learning`. Callers that have to
        tell the agent WHY nothing was written should call that one instead --
        this return says only that something was dropped, not what it hit.

        Args:
            text_content: The learning text.
            category: "market" or "execution". Defaults to "market".
            supersede: Correction mode -- see :meth:`write_learning`.
            replaces: Text identifying the entry being corrected; implies
                ``supersede``.

        Returns:
            True if the learning was written (appended or superseded), False if
            it was suppressed. Suppression is also logged -- it used to be
            entirely silent, and a dropped corrective learning is worse than a
            repeat.
        """
        return self.write_learning(
            text_content, category=category, supersede=supersede, replaces=replaces
        )["written"]

    def write_learning(
        self,
        text_content: str,
        category: str = "",
        supersede: bool = False,
        replaces: str = "",
    ) -> dict[str, Any]:
        """Write a learning and report exactly what happened to it.

        Two modes:

        * default -- append, unless the text repeats an existing entry.
        * ``supersede=True`` (or a non-empty ``replaces``) -- REPLACE the entry
          this learning corrects, in place, and park the old text under
          "## Retired Insights" (a section that is not injected into the prompt).

        The supersede path exists because append is otherwise the only write
        path -- there is no edit/replace/delete tool anywhere above this method
        -- while the two injected sections are re-asserted to the agent in every
        tick prompt. Dedup is a bag-of-words overlap that cannot see negation,
        so a full-sentence REVERSAL of a wrong entry, written in that entry's own
        vocabulary, scores ~0.8 against it and is suppressed as a repeat (see
        ``_word_overlap``). Without an explicit correction path an entry the
        agent has just measured to be WRONG is unfixable by that agent, and the
        wrong entry keeps being asserted to it every tick. That is the failure
        this method exists to remove, so the correction path is deliberately
        wider than the dedup: anything dedup can suppress, a correction can
        replace (both use ``DEDUP_THRESHOLD``).

        Args:
            text_content: The learning text.
            category: "market" or "execution". Defaults to "market". It decides
                where a NEW entry is filed only; a superseded entry is rewritten
                where it already lives.
            supersede: Treat this as a correction of an existing entry.
            replaces: Verbatim text, or a distinctive fragment, of the entry
                being corrected; implies ``supersede``. When empty, the target
                is the existing entry the new text most closely matches.

        Returns:
            ``{"written": bool, "status": str, "detail": str, ...}`` where
            ``status`` is "written" (appended), "superseded" (swapped for an
            existing entry), "duplicate", "unchanged" or "empty", and ``detail``
            is the reason, naming the entry the decision was taken against.
            Optional keys: ``replaced`` / ``duplicate_of`` (the other entry's
            text, truncated), ``overlap`` (the score that decided it),
            ``retired`` (entries moved out of an injected section).
        """
        # Agents habitually date their own learning text. A timestamp is
        # prepended below, so drop a leading one instead of writing the
        # doubled stamp "- [2026-08-29 18:48] [2026-08-29] ...".
        text_content = _strip_leading_date(text_content)
        if not text_content:
            # A date-only learning ("[2026-08-29]") strips to nothing. Writing
            # it would spend one of the MAX_LEARNINGS slots on a content-free
            # bullet that is then injected into every tick prompt.
            return _learning_result(False, "empty", _EMPTY_DETAIL)
        supersede = supersede or bool(replaces.strip())

        path = self._learnings_path()
        if not path:
            return self._write_learning_to_journal(text_content, supersede, replaces)

        if not path.exists():
            path.write_text(LEARNINGS_TEMPLATE)

        # Resolve category to section header
        cat = category if category in LEARNING_CATEGORIES else DEFAULT_LEARNING_CATEGORY
        section_header = LEARNING_CATEGORIES[cat]

        full_text = path.read_text()
        normalized_new = _normalize(text_content)

        # Every entry of every injected section, tagged with the section it
        # lives in. Dedup and supersede both search all of them, because an
        # entry filed under the other category is asserted to the agent just the
        # same. Compare COMPLETE entries, not first lines: a first-line fragment
        # is a truncation of the entry, so it scores against whatever new
        # learning happens to share its opening clause.
        existing = _collect_learning_entries(full_text)

        note = ""
        if supersede:
            target = _find_supersede_target([replaces, text_content], existing)
            if target is None:
                # Say so instead of reporting a replacement that did not happen.
                # The write still goes ahead below (via the normal append path,
                # dedup included) -- a correction with no target on file is just
                # new information.
                quoted = replaces.strip()
                note = (
                    "no existing entry matched "
                    + (f"replaces={_truncate(quoted, 80)!r}" if quoted else "this text")
                    + ", so nothing was replaced; "
                )
            else:
                t_header, t_entry, t_text, t_score = target
                if _normalize(t_text) == normalized_new:
                    return _learning_result(
                        False,
                        "unchanged",
                        "nothing was written: this is the entry it would replace, "
                        "word for word",
                        duplicate_of=_truncate(t_text),
                        overlap=1.0,
                    )
                now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
                new_entry = _format_learning_entry(now, text_content)
                new_text = _replace_entry_in_section(
                    full_text, t_header, t_entry, new_entry
                )
                new_text = _retire_entries(
                    new_text, [t_entry], f"superseded {now} by a corrected entry"
                )
                path.write_text(new_text)
                log.info(
                    "learning superseded in %s (match %.2f): %r replaced %r",
                    t_header,
                    t_score,
                    text_content[:120],
                    t_text.replace("\n", " ")[:120],
                )
                return _learning_result(
                    True,
                    "superseded",
                    f"replaced the {t_header} entry this corrects "
                    f"(match {t_score:.2f}); the old text was moved to "
                    f"'{RETIRED_SECTION}', which is never injected into the "
                    f"prompt. Check 'replaced' is the entry you meant.",
                    replaced=_truncate(t_text),
                    overlap=t_score,
                )

        for _header, _entry, existing_text in existing:
            normalized_existing = _normalize(existing_text)
            if normalized_existing == normalized_new:
                _log_suppressed(text_content, existing_text, 1.0)
                return _learning_result(
                    False,
                    "duplicate",
                    note + "nothing was written: an existing entry already says "
                    "this, word for word",
                    duplicate_of=_truncate(existing_text),
                    overlap=1.0,
                )
            score = _word_overlap(normalized_new, normalized_existing)
            if score > DEDUP_THRESHOLD:
                _log_suppressed(text_content, existing_text, score)
                return _learning_result(
                    False,
                    "duplicate",
                    note
                    + f"nothing was written: this reads as a repeat of an existing "
                    f"entry (word overlap {score:.2f}). {_CORRECTION_HINT}",
                    duplicate_of=_truncate(existing_text),
                    overlap=score,
                )

        # Extract existing learnings from the target section
        pattern = rf"(^## {re.escape(section_header)}\n)(.*?)(?=^## |\Z)"
        m = re.search(pattern, full_text, re.MULTILINE | re.DOTALL)
        if m:
            # Whole entries, not physical lines: a wrapped learning must survive
            # the rewrite below intact (see _group_bullet_entries).
            existing_entries = _group_bullet_entries(m.group(2).strip())
        else:
            existing_entries = []

        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        existing_entries.append(_format_learning_entry(now, text_content))

        evicted_entries: list[str] = []
        if len(existing_entries) > MAX_LEARNINGS:
            # The cap drops the OLDEST entries, which in a seeded file are the
            # safety lessons. That used to happen silently on both sides; the
            # old text is now parked in "## Retired Insights" and reported back.
            evicted_entries = existing_entries[:-MAX_LEARNINGS]
            existing_entries = existing_entries[-MAX_LEARNINGS:]
        evicted = [_ENTRY_STAMP_RE.sub("", e).strip() for e in evicted_entries]

        new_section = "\n".join(existing_entries)
        if m:
            new_text = (
                full_text[: m.start(2)] + new_section + "\n\n" + full_text[m.end(2) :]
            )
        else:
            new_text = full_text.rstrip() + f"\n\n## {section_header}\n{new_section}\n"

        detail = note + f"written to {section_header}"
        if evicted:
            log.warning(
                "%d learning(s) evicted from %s at the %d-entry cap, moved to %s: %s",
                len(evicted),
                section_header,
                MAX_LEARNINGS,
                RETIRED_SECTION,
                " | ".join(e.replace("\n", " ")[:120] for e in evicted),
            )
            new_text = _retire_entries(
                new_text,
                evicted_entries,
                f"evicted from {section_header} at the {MAX_LEARNINGS}-entry cap",
            )
            detail += (
                f"; {len(evicted)} older entr"
                + ("y was" if len(evicted) == 1 else "ies were")
                + f" pushed out of {section_header} by the {MAX_LEARNINGS}-entry "
                f"cap and moved to '{RETIRED_SECTION}' (no longer injected)"
            )

        path.write_text(new_text)
        result = _learning_result(True, "written", detail)
        if evicted:
            result["retired"] = [_truncate(e) for e in evicted]
        return result

    def _write_learning_to_journal(
        self, text_content: str, supersede: bool = False, replaces: str = ""
    ) -> dict[str, Any]:
        """Legacy: write the learning to the journal's Learnings section.

        Same contract as ``write_learning`` (which delegates here when there is
        no learnings.md), with one difference this path cannot avoid: a journal
        has no "Retired Insights" section, so a superseded or evicted entry
        survives only in the log and in the returned result.
        ``_strip_leading_date`` is idempotent, so stripping again here matches
        the learnings.md path exactly.
        """
        text_content = _strip_leading_date(text_content)
        if not text_content:
            return _learning_result(False, "empty", _EMPTY_DETAIL)

        section = self._get_section("Learnings")
        # Whole entries, so wrapped learnings are not decapitated by the
        # rewrite and dedup compares full text (see _group_bullet_entries).
        existing_entries = _group_bullet_entries(section)
        existing = [
            ("Learnings", entry, _ENTRY_STAMP_RE.sub("", entry))
            for entry in existing_entries
        ]

        normalized_new = _normalize(text_content)
        now = datetime.now(timezone.utc).strftime("%H:%M")

        note = ""
        if supersede or replaces.strip():
            target = _find_supersede_target([replaces, text_content], existing)
            if target is None:
                quoted = replaces.strip()
                note = (
                    "no existing entry matched "
                    + (f"replaces={_truncate(quoted, 80)!r}" if quoted else "this text")
                    + ", so nothing was replaced; "
                )
            else:
                _header, t_entry, t_text, t_score = target
                if _normalize(t_text) == normalized_new:
                    return _learning_result(
                        False,
                        "unchanged",
                        "nothing was written: this is the entry it would replace, "
                        "word for word",
                        duplicate_of=_truncate(t_text),
                        overlap=1.0,
                    )
                new_entry = _format_learning_entry(now, text_content)
                rewritten = []
                for entry in existing_entries:
                    if entry == t_entry:
                        _body, blanks = _split_trailing_blanks(entry)
                        rewritten.append("\n".join([new_entry] + blanks))
                    else:
                        rewritten.append(entry)
                self._replace_section("Learnings", "\n".join(rewritten))
                # The journal has nowhere to retire the old text to, so this log
                # line is the only copy that survives the swap.
                log.info(
                    "learning superseded in the journal (match %.2f): %r replaced %r",
                    t_score,
                    text_content[:120],
                    t_text.replace("\n", " ")[:120],
                )
                return _learning_result(
                    True,
                    "superseded",
                    f"replaced the journal entry this corrects "
                    f"(match {t_score:.2f}); the old text is not kept anywhere "
                    f"else, so check 'replaced' is the entry you meant.",
                    replaced=_truncate(t_text),
                    overlap=t_score,
                )

        for _header, _entry, existing_text in existing:
            normalized_existing = _normalize(existing_text)
            if normalized_existing == normalized_new:
                _log_suppressed(text_content, existing_text, 1.0)
                return _learning_result(
                    False,
                    "duplicate",
                    note + "nothing was written: an existing entry already says "
                    "this, word for word",
                    duplicate_of=_truncate(existing_text),
                    overlap=1.0,
                )
            score = _word_overlap(normalized_new, normalized_existing)
            if score > DEDUP_THRESHOLD:
                _log_suppressed(text_content, existing_text, score)
                return _learning_result(
                    False,
                    "duplicate",
                    note
                    + f"nothing was written: this reads as a repeat of an existing "
                    f"entry (word overlap {score:.2f}). {_CORRECTION_HINT}",
                    duplicate_of=_truncate(existing_text),
                    overlap=score,
                )

        existing_entries.append(_format_learning_entry(now, text_content))

        evicted: list[str] = []
        if len(existing_entries) > MAX_LEARNINGS:
            evicted = [
                _ENTRY_STAMP_RE.sub("", e).strip()
                for e in existing_entries[:-MAX_LEARNINGS]
            ]
            existing_entries = existing_entries[-MAX_LEARNINGS:]
            log.warning(
                "%d learning(s) dropped from the journal at the %d-entry cap: %s",
                len(evicted),
                MAX_LEARNINGS,
                " | ".join(e.replace("\n", " ")[:120] for e in evicted),
            )

        self._replace_section("Learnings", "\n".join(existing_entries))
        detail = note + "written to the journal's Learnings section"
        if evicted:
            detail += (
                f"; {len(evicted)} older entr"
                + ("y was" if len(evicted) == 1 else "ies were")
                + f" dropped at the {MAX_LEARNINGS}-entry cap"
            )
        result = _learning_result(True, "written", detail)
        if evicted:
            result["retired"] = [_truncate(e) for e in evicted]
        return result

    # ------------------------------------------------------------------
    # Reading (journal)
    # ------------------------------------------------------------------

    def read_full(self) -> str:
        """Return the entire journal contents (cached until the file changes)."""
        try:
            stat = self._path.stat()
        except OSError:
            return ""
        stamp = (stat.st_mtime_ns, stat.st_size)
        if stamp != self._cache_stamp:
            self._cache_text = self._path.read_text()
            self._cache_stamp = stamp
            self._parsed_cache.clear()
        return self._cache_text

    def _write_journal(self, text: str) -> None:
        """Write journal.md and invalidate the read cache."""
        self._path.write_text(text)
        self._cache_stamp = None
        self._parsed_cache.clear()

    def read_summary(self) -> str:
        """Return the summary section."""
        return self._get_section("Summary")

    def read_state(self) -> str:
        """Return the summary (backwards compat alias for read_summary)."""
        summary = self._get_section("Summary")
        if summary:
            return summary
        return self._get_section("State")

    def read_recent(self, max_entries: int = 10) -> str:
        """Return recent decisions from snapshots."""
        snapshots = self.list_snapshots(limit=max_entries)
        if snapshots:
            parts = []
            for snap in snapshots:
                content = self.read_snapshot(snap["tick"])
                if content:
                    m = re.search(
                        r"^## Agent Response\n(.*?)(?=^## |\Z|^<details)",
                        content,
                        re.MULTILINE | re.DOTALL,
                    )
                    if m:
                        decision = m.group(1).strip()[:200]
                        parts.append(f"- **#{snap['tick']}** {decision}")
            if parts:
                return "\n".join(parts)

        # Legacy: check runs/ and Recent Actions
        runs = self._list_legacy_runs(limit=max_entries)
        if runs:
            parts = []
            for run in runs:
                content = self._read_legacy_run(run["tick"])
                if content:
                    m = re.search(
                        r"^## Decision\n(.*?)(?=^## |\Z)",
                        content,
                        re.MULTILINE | re.DOTALL,
                    )
                    if m:
                        parts.append(f"- **#{run['tick']}** {m.group(1).strip()}")
            if parts:
                return "\n".join(parts)

        content = self._get_section("Recent Actions")
        if not content:
            content = self._get_section("Actions Log")
        return content

    # ------------------------------------------------------------------
    # Snapshots (full context dumps)
    # ------------------------------------------------------------------

    def save_full_snapshot(
        self,
        tick: int,
        timestamp: str,
        system_prompt: str,
        response_text: str,
        tool_calls: list[dict[str, Any]],
        executors_data: str,
        risk_state: dict[str, Any],
        duration: float,
    ) -> Path:
        """Write a full snapshot capturing everything."""
        self._snapshots_dir.mkdir(parents=True, exist_ok=True)

        # Format risk state
        max_dd = risk_state.get("max_drawdown_pct", -1)
        dd_display = (
            f"{risk_state.get('drawdown_pct', 0):.1f}% / {max_dd:.1f}% limit"
            if max_dd >= 0
            else "disabled"
        )
        risk_lines = [
            f"- Position Size: ${risk_state.get('total_exposure', 0):.2f} / ${risk_state.get('max_position_size', 500):.2f} limit",
            f"- Open Executors: {risk_state.get('executor_count', 0)} / {risk_state.get('max_open_executors', 5)} limit",
            f"- Drawdown: {dd_display}",
            f"- Status: {'BLOCKED - ' + risk_state.get('block_reason', '') if risk_state.get('is_blocked') else 'ACTIVE'}",
        ]

        # Format tool calls
        tool_parts = []
        for i, tc in enumerate(tool_calls, 1):
            tc_name = tc.get("name", tc.get("title", "unknown"))
            tc_status = tc.get("status", "")
            tool_parts.append(f"### {i}. {tc_name} ({tc_status})")
            if tc.get("input"):
                input_str = (
                    json.dumps(tc["input"], indent=2)
                    if isinstance(tc["input"], dict)
                    else str(tc["input"])
                )
                tool_parts.append(f"**Input:**\n```json\n{input_str}\n```")
            if tc.get("output"):
                output_str = str(tc["output"])[:2000]
                tool_parts.append(f"**Output:**\n```\n{output_str}\n```")
            tool_parts.append("")

        content = SNAPSHOT_TEMPLATE.format(
            tick=tick,
            timestamp=timestamp,
            prompt_len=len(system_prompt),
            system_prompt=system_prompt,
            executors_data=executors_data or "No executors.",
            risk_state="\n".join(risk_lines),
            response_text=response_text or "No response.",
            tool_count=len(tool_calls),
            tool_calls="\n".join(tool_parts) or "No tool calls.",
            duration=duration,
        )

        path = self._snapshots_dir / f"snapshot_{tick}.md"
        path.write_text(content)
        self._cleanup_old_snapshots()
        return path

    def read_snapshot(self, tick: int) -> str:
        """Read a specific snapshot by tick number."""
        path = self._snapshots_dir / f"snapshot_{tick}.md"
        if path.exists():
            return path.read_text()
        # Legacy fallback
        return self._read_legacy_run(tick)

    def list_snapshots(self, limit: int = 10) -> list[dict[str, Any]]:
        """List recent snapshots, newest first."""
        results = []

        # Check new snapshots/ dir
        if self._snapshots_dir.exists():
            files = sorted(
                self._snapshots_dir.glob("snapshot_*.md"),
                key=lambda f: f.stat().st_mtime,
                reverse=True,
            )
            for f in files[:limit]:
                m = re.match(r"snapshot_(\d+)\.md", f.name)
                if m:
                    results.append(
                        {
                            "tick": int(m.group(1)),
                            "file": f.name,
                            "size": f.stat().st_size,
                        }
                    )

        # If none found, check legacy runs/
        if not results:
            return self._list_legacy_runs(limit=limit)

        return results

    def get_recent_decisions(self, count: int = 3) -> str:
        """Get the last N decision entries from the Decisions section of journal.md.

        This is much cheaper than reading snapshot files and produces compact
        one-line entries that were already written by append_action().
        """
        section = self._get_section("Decisions")
        if not section:
            return ""

        lines = [l for l in section.splitlines() if l.startswith("- ")]
        return "\n".join(lines[-count:])

    def _cleanup_old_snapshots(self) -> None:
        """Remove oldest snapshots if over MAX_SNAPSHOTS."""
        if not self._snapshots_dir.exists():
            return
        files = sorted(self._snapshots_dir.glob("snapshot_*.md"))
        if len(files) > MAX_SNAPSHOTS:
            for f in files[: len(files) - MAX_SNAPSHOTS]:
                f.unlink()

    # ------------------------------------------------------------------
    # Legacy run support (reads from runs/ dir)
    # ------------------------------------------------------------------

    def _read_legacy_run(self, tick: int) -> str:
        path = self._legacy_runs_dir / f"run_{tick}.md"
        if path.exists():
            return path.read_text()
        return ""

    def _list_legacy_runs(self, limit: int = 10) -> list[dict[str, Any]]:
        if not self._legacy_runs_dir.exists():
            return []
        files = sorted(
            self._legacy_runs_dir.glob("run_*.md"),
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        )
        results = []
        for f in files[:limit]:
            m = re.match(r"run_(\d+)\.md", f.name)
            if m:
                results.append(
                    {
                        "tick": int(m.group(1)),
                        "file": f.name,
                        "size": f.stat().st_size,
                    }
                )
        return results

    def read_run_snapshot(self, tick: int) -> str:
        """Legacy compat: try snapshots first, then runs."""
        return self.read_snapshot(tick)

    def list_runs(self, limit: int = 10) -> list[dict[str, Any]]:
        """Legacy compat: try snapshots first, then runs."""
        return self.list_snapshots(limit=limit)

    # ------------------------------------------------------------------
    # Writing (journal)
    # ------------------------------------------------------------------

    def write_summary(
        self, tick: int, status: str, pnl: float, open_count: int, last_action: str
    ) -> None:
        """Update the Summary section."""
        now = datetime.now(timezone.utc).strftime("%H:%M UTC")
        summary = (
            f"Last tick: #{tick} at {now}\n"
            f"Status: {status} | PnL: ${pnl:+.2f} | Open: {open_count} executors\n"
            f"Last action: {last_action[:100]}"
        )
        self._replace_section("Summary", summary)

    def write_state(self, state_text: str) -> None:
        """Overwrite the Summary section (backwards compat for write via MCP)."""
        if self._get_section("State"):
            self._replace_section("State", state_text.strip())
        else:
            self._replace_section("Summary", state_text.strip())

    def append_action(
        self,
        tick: int,
        action: str,
        reasoning: str,
        risk_note: str = "",
    ) -> None:
        """Record an action in the Decisions section."""
        now = datetime.now(timezone.utc).strftime("%H:%M")
        parts = [f"- **#{tick}** ({now}) {action}"]
        if reasoning:
            parts[0] += f" -- {reasoning}"
        if risk_note:
            parts[0] += f" [{risk_note}]"
        entry = parts[0]

        # Write to Decisions section
        section = self._get_section("Decisions")
        lines = [l for l in section.splitlines() if l.strip()]
        lines.append(entry)
        if len(lines) > 20:
            lines = lines[-20:]
        self._replace_section("Decisions", "\n".join(lines))

        # Also write to Recent Actions if it exists (legacy compat)
        if "## Recent Actions" in self.read_full():
            ra_section = self._get_section("Recent Actions")
            ra_lines = [l for l in ra_section.splitlines() if l.strip()]
            ra_lines.append(entry)
            if len(ra_lines) > 10:
                ra_lines = ra_lines[-10:]
            self._replace_section("Recent Actions", "\n".join(ra_lines))

    def append_error(self, error: str) -> None:
        """Append an error as a decision entry."""
        now = datetime.now(timezone.utc).strftime("%H:%M")
        section = self._get_section("Decisions")
        lines = [l for l in section.splitlines() if l.strip()]
        lines.append(f"- **error** ({now}) {error}")
        if len(lines) > 20:
            lines = lines[-20:]
        self._replace_section("Decisions", "\n".join(lines))

    # ------------------------------------------------------------------
    # Tick tracking
    # ------------------------------------------------------------------

    @property
    def tick_count(self) -> int:
        return self._tick_count

    def _count_ticks(self) -> int:
        section = self._get_section("Ticks")
        return len([l for l in section.splitlines() if l.startswith(TICK_LINE_PREFIX)])

    def record_tick(self, response_summary: str = "", actions: int = 0) -> int:
        """Record a tick entry. Returns the new tick number."""
        self._tick_count += 1
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        summary = response_summary[:200].replace("\n", " ")
        entry = (
            f"{TICK_LINE_PREFIX}{self._tick_count} | {now} "
            f"| actions={actions} | {summary}"
        )
        self._append_to_section("Ticks", entry)
        return self._tick_count

    # ------------------------------------------------------------------
    # Executor tracking
    # ------------------------------------------------------------------

    def track_executor(self, executor_id: str, ex_type: str, config: dict) -> None:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        connector = config.get("connector_name", "")
        pair = config.get("trading_pair", "")
        side = config.get("side", "")
        amount = config.get("total_amount_quote", 0) or config.get("amount", 0) or 0
        entry = (
            f"- executor={executor_id} | type={ex_type} | {connector} {pair} {side} "
            f"| amount=${float(amount):.2f} | created={now} | status=open | pnl=0 | volume=0"
        )
        self._append_to_section("Executors", entry)

    def update_executor(
        self, executor_id: str, pnl: float, volume: float, stopped: bool = False
    ) -> None:
        text = self.read_full()
        pattern = rf"(- executor={re.escape(executor_id)} \|.*)"
        m = re.search(pattern, text)
        if not m:
            return

        old_line = m.group(1)
        new_line = re.sub(r"pnl=[^ |]*", f"pnl={pnl:.2f}", old_line)
        new_line = re.sub(r"volume=[^ |]*", f"volume={volume:.2f}", new_line)
        if stopped:
            new_line = re.sub(r"status=\w+", "status=closed", new_line)
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
            new_line += f" | stopped={now}"

        text = text.replace(old_line, new_line)
        self._write_journal(text)

    # ------------------------------------------------------------------
    # Metric snapshots (inline in journal)
    # ------------------------------------------------------------------

    def record_snapshot(
        self,
        total_pnl: float,
        total_volume: float,
        open_count: int,
        position_size: float,
    ) -> None:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        entry = (
            f"- {now} | pnl=${total_pnl:+.2f} | volume=${total_volume:,.0f} "
            f"| open={open_count} | exposure=${position_size:.2f}"
        )
        self._append_to_section("Snapshots", entry)

    # ------------------------------------------------------------------
    # Queries (used by RiskEngine)
    # ------------------------------------------------------------------

    def _parse_executors(self) -> list[dict]:
        self.read_full()  # refresh parsed cache if the file changed
        cached = self._parsed_cache.get("executors")
        if cached is not None:
            return cached
        section = self._get_section("Executors")
        results = []
        for line in section.splitlines():
            if not line.startswith("- executor="):
                continue
            entry: dict[str, Any] = {}
            for part in line[2:].split(" | "):
                if "=" in part:
                    k, v = part.split("=", 1)
                    entry[k.strip()] = v.strip()
            results.append(entry)
        self._parsed_cache["executors"] = results
        return results

    def _parse_ticks(self) -> list[dict]:
        self.read_full()  # refresh parsed cache if the file changed
        cached = self._parsed_cache.get("ticks")
        if cached is not None:
            return cached
        section = self._get_section("Ticks")
        results = []
        for line in section.splitlines():
            if not line.startswith(TICK_LINE_PREFIX):
                continue
            entry: dict[str, Any] = {}
            parts = line[2:].split(" | ")
            for part in parts:
                if part.startswith("tick#"):
                    entry["tick"] = int(part.replace("tick#", ""))
                elif part.startswith("actions="):
                    entry["actions"] = int(part.replace("actions=", ""))
                else:
                    if re.match(r"\d{4}-\d{2}-\d{2}", part.strip()):
                        entry["timestamp"] = part.strip()
                    else:
                        entry["summary"] = part.strip()
            results.append(entry)
        self._parsed_cache["ticks"] = results
        return results

    def _parse_snapshots(self) -> list[dict]:
        self.read_full()  # refresh parsed cache if the file changed
        cached = self._parsed_cache.get("snapshots")
        if cached is not None:
            return cached
        section = self._get_section("Snapshots")
        results = []
        for line in section.splitlines():
            if not line.startswith("- "):
                continue
            entry: dict[str, Any] = {}
            for part in line[2:].split(" | "):
                part = part.strip()
                if part.startswith("pnl=$"):
                    entry["pnl"] = float(part.replace("pnl=$", "").replace("+", ""))
                elif part.startswith("volume=$"):
                    entry["volume"] = float(
                        part.replace("volume=$", "").replace(",", "")
                    )
                elif part.startswith("exposure=$"):
                    entry["exposure"] = float(part.replace("exposure=$", ""))
                elif part.startswith("open="):
                    entry["open"] = int(part.replace("open=", ""))
                elif re.match(r"\d{4}-\d{2}-\d{2}", part):
                    entry["timestamp"] = part
            results.append(entry)
        self._parsed_cache["snapshots"] = results
        return results

    def get_daily_pnl(self) -> float:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        total = 0.0
        for ex in self._parse_executors():
            created = ex.get("created", "")
            if created.startswith(today):
                try:
                    total += float(ex.get("pnl", 0))
                except (ValueError, TypeError):
                    pass
        return total

    def get_total_exposure(self) -> float:
        total = 0.0
        for ex in self._parse_executors():
            if ex.get("status") == "open":
                amount_str = ex.get("amount", "$0").lstrip("$")
                try:
                    total += float(amount_str)
                except (ValueError, TypeError):
                    pass
        return total

    def get_open_executor_count(self) -> int:
        return sum(1 for ex in self._parse_executors() if ex.get("status") == "open")

    def get_drawdown_pct(self) -> float:
        snapshots = self._parse_snapshots()
        if not snapshots:
            return 0.0
        pnls = [s.get("pnl", 0) for s in snapshots]
        peak = max(pnls)
        current = pnls[-1]
        drawdown = peak - current
        if drawdown <= 0:
            return 0.0
        exposure = snapshots[-1].get("exposure", 0.0)
        if exposure <= 0:
            return 0.0
        return drawdown / exposure * 100

    def get_pnl_series(self, hours: int = 24) -> list[dict]:
        return [
            {"timestamp": s.get("timestamp", ""), "pnl": s.get("pnl", 0)}
            for s in self._parse_snapshots()
        ]

    def get_total_volume(self) -> float:
        snapshots = self._parse_snapshots()
        if not snapshots:
            return 0.0
        return snapshots[-1].get("volume", 0.0)

    def get_summary_dict(self) -> dict[str, Any]:
        """Overall summary for display."""
        return {
            "total_ticks": self._tick_count,
            "daily_pnl": self.get_daily_pnl(),
            "total_volume": self.get_total_volume(),
            "total_exposure": self.get_total_exposure(),
            "open_executors": self.get_open_executor_count(),
            "drawdown_pct": self.get_drawdown_pct(),
        }

    def close(self):
        """No-op, kept for API compat."""
        pass

    # ------------------------------------------------------------------
    # Section helpers
    # ------------------------------------------------------------------

    def _get_section(self, name: str) -> str:
        """Extract content between ## {name} and the next ## header."""
        text = self.read_full()
        pattern = rf"^## {re.escape(name)}\n(.*?)(?=^## |\Z)"
        m = re.search(pattern, text, re.MULTILINE | re.DOTALL)
        return m.group(1).strip() if m else ""

    def _replace_section(self, name: str, content: str) -> None:
        """Replace the content of a section, preserving other sections."""
        text = self.read_full()
        pattern = rf"(^## {re.escape(name)}\n).*?(?=^## |\Z)"
        replacement = rf"\g<1>{content}\n\n"
        new_text, count = re.subn(
            pattern, replacement, text, count=1, flags=re.MULTILINE | re.DOTALL
        )
        if count == 0:
            new_text = text.rstrip() + f"\n\n## {name}\n{content}\n"
        self._write_journal(new_text)

    def _append_to_section(self, section: str, entry: str) -> None:
        """Append a line to a section."""
        text = self.read_full()
        marker = f"## {section}\n"
        idx = text.find(marker)
        if idx == -1:
            text += f"\n{marker}{entry}\n"
        else:
            insert_at = idx + len(marker)
            next_section = text.find("\n## ", insert_at)
            if next_section == -1:
                text += entry + "\n"
            else:
                text = text[:next_section] + entry + "\n" + text[next_section:]
        self._write_journal(text)

    # ------------------------------------------------------------------
    # Info
    # ------------------------------------------------------------------

    def entry_count(self) -> int:
        """Count snapshots."""
        snaps = self.list_snapshots(limit=1000)
        if snaps:
            return len(snaps)
        section = self._get_section("Recent Actions")
        return len([l for l in section.splitlines() if l.startswith("- ")])

    def get_data_dir(self) -> Path:
        """Return the session data directory."""
        return self._session_dir

    def size_bytes(self) -> int:
        """Current file size."""
        return self._path.stat().st_size if self._path.exists() else 0


def _group_bullet_entries(section_content: str) -> list[str]:
    """Group a markdown section's physical lines into whole "- " entries.

    A learning is usually wrapped over several lines: the first starts with
    "- " and the continuations are indented beneath it. Filtering the section
    with ``startswith("- ")`` silently decapitates every wrapped entry, and
    since the learning writers rebuild the section from that filtered list the
    loss is persisted -- so grouping must happen BEFORE any dedup, before the
    MAX_LEARNINGS slice and before the rewrite.

    Blank separator lines are kept with the entry above them so a read/write
    round-trip is byte-stable; text before the first bullet is dropped, as it
    always was.
    """
    entries: list[str] = []
    for line in section_content.splitlines():
        if line.startswith("- "):
            entries.append(line)
        elif entries:
            entries[-1] += "\n" + line
    return entries


def _strip_leading_date(text: str) -> str:
    """Drop the leading timestamp stamps an agent wrote into its own text.

    Every form in ``_STAMP``: "[YYYY-MM-DD]", "[YYYY-MM-DD HH:MM]", "[HH:MM]"
    and a dated stamp with a qualifier ("[2026-08-27, corrected 2026-08-29]").

    Both learning writers prepend a timestamp of their own; agents that date
    their text too would otherwise produce a doubled stamp. Repeated (``+``)
    so the function is idempotent: ``write_learning`` strips before it decides
    which writer to use, and the legacy journal writer strips again, so a
    one-shot regex made the two paths disagree on "[d1] [d2] text".

    The bare "[HH:MM]" form is the one the legacy journal writer emits, so text
    copied out of a journal and re-submitted doubled its stamp until this
    matched it -- and _ENTRY_STAMP_RE already stripped "[HH:MM]" on the
    existing side, so the two sides of every comparison disagreed as well.
    Both sides now share ``_STAMP``, which is what keeps them in step.
    """
    return re.sub(rf"^(?:{_STAMP})+", "", text.strip())


# One bracketed timestamp, in every form that appears in these files: the
# writers' "[2026-08-27]" and "[2026-08-27 20:07]", the legacy journal's
# "[20:07]", and the hand-written dated stamps that carry a qualifier --
# "[2026-08-27, corrected 2026-08-29]", "[retired 2026-08-29]". The trailing
# "[^\]]*" only extends a stamp that ALREADY starts with a date or a time, so
# an entry opening with "[EMPTY_BOOK] ..." is left alone.
#
# Both sides of every comparison use this one pattern on purpose: when the two
# regexes disagreed, the existing side kept a spurious "20260827" token that
# _strip_leading_date had already removed from the new side, and every score
# was computed against text neither writer ever wrote.
_STAMP = r"\[(?:\d{4}-\d{2}-\d{2}|\d{2}:\d{2}|retired \d{4}-\d{2}-\d{2})[^\]]*\]\s*"

# The bullet + stamp(s) both learning writers prepend, stripped before dedup so
# an entry compares as the agent wrote it.
_ENTRY_STAMP_RE = re.compile(rf"^- (?:{_STAMP})*")


def _format_learning_entry(stamp: str, text: str) -> str:
    """Render one "- [stamp] text" learning, indenting continuation lines.

    ``_group_bullet_entries`` starts a new entry at any line beginning "- ", so
    a continuation line the agent wrote flush-left (especially one that itself
    looks like a bullet) would round-trip as a separate, stamp-less learning --
    the same "fragment presented as fact" shape the grouping fix removed.
    Indenting matches how the wrapped entries in learnings.md are written.
    """
    first, sep, rest = text.partition("\n")
    lines = [f"- [{stamp}] {first}"]
    if sep:
        for line in rest.split("\n"):
            lines.append(line if not line.strip() or line[0].isspace() else "  " + line)
    return "\n".join(lines)


def _log_suppressed(new_text: str, existing_text: str, score: float) -> None:
    """Record a learning that dedup dropped.

    Suppression used to be entirely silent -- ``append_learning`` just returned
    and the MCP journal_write tool reported ``written: True`` regardless -- so a
    corrective learning could be lost with no trace anywhere. Both halves are
    closed now: the caller gets ``written: False`` with the reason and the entry
    it hit, and the tool passes that verdict on to the agent. This line stays as
    the operator-side record, and for any caller that ignores the return.
    """
    log.info(
        "learning suppressed as duplicate (overlap %.2f): %r ~ existing %r",
        score,
        new_text[:120],
        existing_text.replace("\n", " ")[:120],
    )


def _normalize(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s]", "", text)
    return re.sub(r"\s+", " ", text)


def _word_overlap(a: str, b: str) -> float:
    """Fraction of the LONGER string's words that the shorter one repeats.

    The denominator is ``max``, not ``min``: dividing by the shorter side makes
    "is a subset of" score 1.0, so any short new learning built from words a
    long existing entry already contains was suppressed -- including negations
    of that entry, since this is a bag of words and cannot see "not". Dividing
    by the longer side means a short entry can never be a duplicate of a long
    one, while a genuine near-verbatim repeat (same length, same vocabulary)
    still scores ~1.0 and is still suppressed.

    For any given pair of strings this is <= the old min-denominator score
    (max >= min), so the metric change on its own can only suppress less.

    What it does NOT fix, and cannot: for two strings of comparable length
    ``max`` equals ``min``, so a full-sentence REVERSAL of an entry, written in
    that entry's own vocabulary, still scores ~0.8 and is still suppressed --
    the same property that makes a repeat score ~1.0. No bag-of-words metric
    can separate "X is true" from "X is not true"; that is why corrections go
    through the explicit supersede path in ``write_learning`` instead of being
    detected here, and why a suppression tells the caller that path exists.
    """
    words_a = set(a.split())
    words_b = set(b.split())
    if not words_a or not words_b:
        return 0.0
    intersection = words_a & words_b
    return len(intersection) / max(len(words_a), len(words_b))


def _truncate(text: str, limit: int = 300) -> str:
    """One-line, length-capped copy of an entry, for a result the agent reads.

    Whitespace is collapsed so a wrapped entry stays one line, and the head is
    kept: it is enough to recognise the entry, and it is still a usable
    ``replaces=`` fragment because the supersede search matches on containment.
    """
    flat = re.sub(r"\s+", " ", text).strip()
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _learning_result(
    written: bool, status: str, detail: str, **extra: Any
) -> dict[str, Any]:
    """Build the result of a learning write.

    ``detail`` must say what actually happened and name the evidence for it --
    this dict is what the caller shows the agent, and a write that reports
    success it did not have is how a measured correction gets lost.
    """
    result: dict[str, Any] = {"written": written, "status": status, "detail": detail}
    result.update(extra)
    return result


def _collect_learning_entries(full_text: str) -> list[tuple[str, str, str]]:
    """Every entry of every INJECTED section as (section_header, entry, text).

    ``entry`` is the raw block (bullet, stamp and continuation lines) so it can
    be located again for an in-place rewrite; ``text`` is the same block with
    the leading bullet/stamp removed, which is what comparisons run against.
    Sections outside LEARNING_CATEGORIES (Operational Notes, Retired Insights)
    are deliberately skipped: they never reach the agent, so they can neither
    duplicate nor be corrected by what it writes.
    """
    entries: list[tuple[str, str, str]] = []
    for cat_header in LEARNING_CATEGORIES.values():
        cm = re.search(
            rf"^## {re.escape(cat_header)}\n(.*?)(?=^## |\Z)",
            full_text,
            re.MULTILINE | re.DOTALL,
        )
        if not cm:
            continue
        for entry in _group_bullet_entries(cm.group(1).strip()):
            entries.append((cat_header, entry, _ENTRY_STAMP_RE.sub("", entry)))
    return entries


# Words carried by nearly every English sentence. Dropped from the SUPERSEDE
# search only: with a min denominator they are enough on their own to score a
# short phrase above the threshold against any long entry ("a claim that does
# not appear in this file at all" scored 0.58 against a real entry on stopwords
# alone), and a mis-targeted correction overwrites a good entry. They stay in
# the dedup metric, which divides by the longer side and is not fooled by them.
# Negations (not/no/never) are dropped here too: this search is looking for the
# entry a correction CONTRADICTS, which is the entry it argues with word for
# word.
_STOPWORDS = frozenset(
    """a an the and or but if then so that this these those it its is are was
    were be been being do does did done has have had will would can could
    should may might must not no never nor of in on at to for with by from as
    into over under up down out off about after before against between during
    than too very all any both each more most other same such only own just
    when where while what which who whom why how there here we you they i me my
    our your their them us he she him her one two ago yet still even also""".split()
)


def _content_words(normalized: str) -> set[str]:
    """The words of a normalized string that carry its meaning."""
    return set(normalized.split()) - _STOPWORDS


def _containment(a: str, b: str) -> float:
    """Fraction of the SHORTER side's CONTENT words that the longer one repeats.

    The mirror of ``_word_overlap``: this divides by ``min``, so "is a subset
    of" scores 1.0. That makes it wrong for dedup (it eats short corrections)
    and right for TARGETING -- a correction quotes or reuses the wording of the
    entry it corrects, whether it is shorter, longer or the same length, and
    the entry it picks is reported back and retired, not silently dropped.
    Stopwords are excluded (see ``_STOPWORDS``) so the match is carried by the
    subject matter and not by the grammar the two sentences happen to share.
    """
    words_a = _content_words(a)
    words_b = _content_words(b)
    if not words_a or not words_b:
        return 0.0
    return len(words_a & words_b) / min(len(words_a), len(words_b))


def _find_supersede_target(
    needles: list[str], entries: list[tuple[str, str, str]]
) -> tuple[str, str, str, float] | None:
    """Pick the existing entry a correction replaces, or None if there is none.

    ``needles`` are the caller's ``replaces`` text (when it gave one -- a
    verbatim quote or a fragment) and the new learning itself, scored
    independently against every entry; the best match across both wins. Both are
    used because ``replaces`` is not always the quote it is asked for: a caller
    that puts a REASON there ("measured directly this tick") would otherwise
    aim the replacement with a handful of incidental words, and the correction
    text is a far better description of what it corrects. An exact quote still
    wins outright -- a normalized substring match in either direction scores 1.0.

    Otherwise the best containment score above ``DEDUP_THRESHOLD`` wins, so any
    entry the dedup would suppress against is reachable as a target. A needle
    too vague to aim with (fewer than ``MIN_TARGET_WORDS`` content words) is
    ignored, rather than replacing whichever entry a couple of shared words
    happened to hit; if every needle is that vague there is no target.

    Returns ``(section_header, entry, entry_text, score)`` for the best match.
    The caller reports the matched text back to the agent and retires the text
    it replaced -- nothing here can tell a correction of entry A from a
    correction of a similar entry B, so the decision is shown, not hidden.
    """
    keys = [_normalize(n) for n in needles if n and n.strip()]
    keys = [k for k in keys if len(_content_words(k)) >= MIN_TARGET_WORDS]
    if not keys:
        return None
    best: tuple[str, str, str, float] | None = None
    for cat_header, entry, text in entries:
        norm_existing = _normalize(text)
        if not norm_existing:
            continue
        score = 0.0
        for key in keys:
            if key in norm_existing or norm_existing in key:
                score = 1.0
                break
            score = max(score, _containment(key, norm_existing))
        if score > DEDUP_THRESHOLD and (best is None or score > best[3]):
            best = (cat_header, entry, text, score)
    return best


def _split_trailing_blanks(entry: str) -> tuple[str, list[str]]:
    """Split an entry into its text and the blank separator lines below it.

    ``_group_bullet_entries`` keeps a blank separator with the entry above it,
    so replacing an entry means putting those blanks back -- otherwise every
    supersede silently reflows the section around it.
    """
    lines = entry.split("\n")
    end = len(lines)
    while end > 0 and not lines[end - 1].strip():
        end -= 1
    return "\n".join(lines[:end]), lines[end:]


def _replace_entry_in_section(
    full_text: str, section_header: str, old_entry: str, new_entry: str
) -> str:
    """Swap one entry for another in place, leaving the rest byte-identical.

    In place: the replacement keeps the old entry's position in the section and
    its trailing blank lines, so a corrected entry does not jump to the end and
    the surrounding entries do not move.
    """
    pattern = rf"(^## {re.escape(section_header)}\n)(.*?)(?=^## |\Z)"
    m = re.search(pattern, full_text, re.MULTILINE | re.DOTALL)
    if not m:
        return full_text
    rewritten = []
    for entry in _group_bullet_entries(m.group(2).strip()):
        if entry == old_entry:
            _body, blanks = _split_trailing_blanks(entry)
            rewritten.append("\n".join([new_entry] + blanks))
        else:
            rewritten.append(entry)
    return (
        full_text[: m.start(2)] + "\n".join(rewritten) + "\n\n" + full_text[m.end(2) :]
    )


def _retire_entries(full_text: str, entries: list[str], reason: str) -> str:
    """Park whole entries under RETIRED_SECTION instead of deleting them.

    Text leaves an injected section two ways -- superseded by a correction, or
    pushed out by the MAX_LEARNINGS cap -- and both used to destroy it. Retired
    Insights is not injected, so this costs no prompt tokens; it exists so the
    same lesson is not re-learnt from scratch, and so a correction leaves the
    claim it overturned on the record with the reason it went.

    ``entries`` are raw entry blocks. The retired copy carries a fresh "retired"
    stamp, in this file's own convention, and the entry's original stamp is
    named in the parenthetical so the date the claim was made is not lost.
    """
    if not entries:
        return full_text
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    rendered = []
    for entry in entries:
        m = _ENTRY_STAMP_RE.match(entry)
        original = entry[2 : m.end()].strip() if m else ""
        note = reason + (f"; originally stamped {original}" if original else "")
        text = _ENTRY_STAMP_RE.sub("", entry).strip()
        rendered.append(_format_learning_entry(f"retired {day}", f"{text}\n({note})"))
    block = "\n".join(rendered)
    pattern = rf"(^## {re.escape(RETIRED_SECTION)}\n)(.*?)(?=^## |\Z)"
    m = re.search(pattern, full_text, re.MULTILINE | re.DOTALL)
    if not m:
        return full_text.rstrip() + f"\n\n## {RETIRED_SECTION}\n{block}\n"
    content = m.group(2).strip()
    merged = f"{content}\n{block}" if content else block
    return full_text[: m.start(2)] + merged + "\n\n" + full_text[m.end(2) :]
