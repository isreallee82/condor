"""Prompt builder for trading agent ticks.

Assembles the single prompt sent to a fresh ACP session each tick,
combining: base rules, strategy instructions, config, risk state,
pre-computed core data, and journal context (learnings + recent decisions).
"""

from __future__ import annotations

import re
from typing import Any

from .agent import Agent
from .strategy import Strategy

BASE_PROMPT_LIVE = """\
You are an autonomous trading agent running inside Condor.

RULES:
- Trade ONLY via manage_executors(action="create"). NEVER use place_order.
- If your strategy deploys a controller-based bot, manage_bots(action="deploy")
  MUST include max_global_drawdown_quote within your risk limits — deploys
  without a declared loss cap are blocked by the risk engine.
- Be conservative. When in doubt, hold and journal why.

ERROR RECOVERY:
- If manage_executors(action="create") fails, call manage_executors(executor_type="<type>") \
to fetch the full config schema, compare it against what you sent, fix the missing/wrong \
fields, and retry ONCE. Journal the error and fix as a learning.
"""

BASE_PROMPT_DRY_RUN = """\
You are an autonomous trading agent running inside Condor in 🧪 DRY RUN mode.

RULES:
- This is OBSERVATION ONLY. Do NOT create or stop executors, and do NOT deploy,
  stop, or update a controller-based bot (manage_bots with action="deploy",
  "stop_bot", "stop_controllers", "start_controllers", or "update_config").
- manage_executors and manage_bots are available for read-only queries
  (performance_report; status/logs/get_config).
- Analyze the market and describe what you WOULD do, but take NO trading action.

DRY RUN MESSAGING:
- Use conditional language: "Would place grid..." not "Grid placed"
- Prefix actions with 🧪 to signal dry-run
- End with: "No executors were created (dry run)"
"""

BASE_PROMPT_COMMON = """\
GENERAL:
- The mcp-hummingbot server is pre-configured. Do NOT call configure_server.
- Keep tool chains short (1-5 calls per tick).
- Your executor state and positions are pre-loaded in [CORE DATA] below — no need to query them.

SKILLS & ROUTINES:
- [AVAILABLE SKILLS & ROUTINES] below lists SKILLS (playbooks — know-how: when to
  act + steps) and ROUTINES (executable scripts).
- Before a known flow, read the relevant playbook with manage_skill(action="read",
  name="...") and follow it instead of re-deriving the procedure.
- A skill may reference a routine (shown as "→ routine: <name>"); run it with
  manage_routines(action="run", name="...", config={...}). manage_routines(action="list")
  to discover routines; routines tagged "agent" are local to your strategy.
- Before AUTHORING a routine (create/edit/fix), read the routine_cookbook playbook
  with manage_skill(action="read", name="routine_cookbook") and follow it — then
  test what you wrote with manage_routines(action="run", ...) before relying on it.
- Skills are read-only playbooks shipped with this agent — follow them, you can't
  create or edit them. Operational facts you learn go to [LEARNINGS] (journal).

MEMORY (about the user, NOT operational learnings):
- [USER MEMORY] below is what is known about the OWNER (preferences, profile).
  This is distinct from [LEARNINGS] (market/execution), which go to the journal.
- Read detail with manage_memory(action="read", name="...").
- If you learn something new and stable about the USER (a standing preference,
  a profile fact, a correction), save it with manage_memory(action="write",
  name="short-name", description="one line", content="...", type="preference|fact").
  Operational/market learnings go to the journal (see JOURNAL above), NOT here.

NOTIFICATIONS:
- Use send_notification(text="...") to message the user on Telegram.
"""

# Journal guidance. In experiment modes (dry_run / run_once) the engine keeps NO
# journal — the whole tick is captured in a dry-run snapshot instead — so the agent
# must not call trading_agent_journal_write (it would fail with "no journal
# available"). Loop mode gets the full journal protocol.
JOURNAL_SECTION_LIVE = """\
JOURNAL:
- Write ONE action entry per tick via trading_agent_journal_write(entry_type="action"). One line.
- Learnings must specify a category: "market" or "execution".
  trading_agent_journal_write(entry_type="learning", category="market|execution", text="...")
  - market: band behavior, volatility regimes, S/R patterns, routine observations.
  - execution: executor errors, schema issues, fill problems, timing.
- Keep learnings factual and short (1 line). No speculation.
- Only write a learning if it's genuinely NEW. Duplicates are auto-filtered.
- Do NOT call trading_agent_journal_read — context is already in this prompt.
"""

JOURNAL_SECTION_EXPERIMENT = """\
JOURNAL:
- This is an experiment (dry-run / run-once): there is NO journal this tick.
- Do NOT call trading_agent_journal_write or trading_agent_journal_read — they are
  unavailable here and will error.
- Put all observations, reasoning, and what you WOULD record straight into your
  response. The full tick is saved automatically as a dry-run snapshot.
"""


def _build_tool_preload(*, is_dry_run: bool, is_experiment: bool) -> str:
    """ToolSearch preload line for ACP sessions.

    Dry-run omits manage_executors (read-only). Experiment modes (dry_run /
    run_once) omit trading_agent_journal_write since they have no journal.
    """
    tools = ["mcp__mcp-hummingbot__get_market_data"]
    if not is_dry_run:
        tools.append("mcp__mcp-hummingbot__manage_executors")
    tools += [
        "mcp__mcp-hummingbot__search_history",
        "mcp__mcp-hummingbot__explore_geckoterminal",
    ]
    if not is_experiment:
        tools.append("mcp__condor__trading_agent_journal_write")
    tools += [
        "mcp__condor__send_notification",
        "mcp__condor__manage_memory",
        "mcp__condor__manage_skill",
        "mcp__condor__manage_routines",
    ]
    return (
        "IMPORTANT: At the very start, load ALL MCP tools in a single ToolSearch call:\n"
        f'ToolSearch(query="select:{",".join(tools)}")\n'
        "Do this silently."
    )


def _build_routines_section(strategy: Strategy) -> str:
    """Build an [AVAILABLE ROUTINES] section listing this agent's own routines.

    Domain experts/trading agents are isolated: they see only their own routines
    (``agents/<slug>/routines``), never the chat's general library.
    """
    from routines.base import assistant_routines_dir, discover_routines_from_path

    lines = ["ROUTINES — executable analysis scripts:"]
    lines.append(
        f'Call via: manage_routines(action="run", name="<name>", strategy_id="{strategy.key}", config={{...}})'
    )
    lines.append("")

    # Agent-level routines (shared across this agent's strategies, isolated from
    # the chat's general library).
    routines_dir = assistant_routines_dir(strategy.agent_slug)
    local = discover_routines_from_path(routines_dir) if routines_dir.exists() else {}
    if local:
        for name, r in sorted(local.items()):
            lines.append(f"  - {name}: {r.description}")
    else:
        lines.append('  (none yet — create one with action="create_routine")')

    return "\n".join(lines)


def _build_controller_mode_section(bot_name: str, ledger: Any | None) -> str:
    """The [CONTROLLER MODE] block, generated from the session's bot ledger.

    Without a ledger (executor-mode callers, tests) this is the plain statement of
    the bot the agent operates. With one, it also states the namespace rule the
    permission callback enforces, the bots already owned, and any call refused so
    far — the only channel that guard has to teach, since it can merely cancel.
    """
    lines = [
        "[CONTROLLER MODE]",
        f"You operate the Hummingbot bot '{bot_name}'. Steer its controllers "
        "instead of creating standalone executors:",
        '- Check current state first: manage_bots(action="status").',
        "- Define/update controller config templates with manage_controllers.",
        f"- Apply them with manage_bots: deploy if '{bot_name}' is not running, "
        "otherwise update_config / start_controllers / stop_controllers.",
    ]

    if ledger is not None:
        ns = ledger.namespace
        lines += [
            "",
            "OWNERSHIP — enforced at the tool call, not by convention:",
            f"- You may deploy or mutate ONLY bots named '{ns}' or '{ns}-<tag>' "
            f"(e.g. '{ns}-btc'). Any other bot_name in a manage_bots deploy / "
            "stop_bot / start_controllers / stop_controllers / update_config call "
            "is REFUSED and recorded — the call simply does not happen.",
            "- Read-only actions (status, logs, get_config) are never restricted: "
            "you can still inspect the whole fleet.",
            f"- Open a second book by deploying '{ns}-<tag>'; every bot in the "
            "namespace rolls up to this session.",
        ]
        for extra in ledger.declared:
            lines.append(
                f"- Legacy name '{extra}' is also yours (configured before this "
                "convention)."
            )
        owned = ledger.owned()
        if owned:
            lines.append(
                "- Bots you own right now: "
                + ", ".join(f"{b.base} ({b.origin})" for b in owned)
            )
        else:
            lines.append("- You own no bot yet this session.")
        recent = ledger.violations[-3:]
        if recent:
            lines.append(
                "- REFUSED so far: "
                + ", ".join(f"{v['action']} on '{v['name']}'" for v in recent)
                + " — use a name inside your namespace instead."
            )

    lines.append(
        "Do NOT create standalone executors unless the strategy instructions "
        "explicitly tell you to. The bot's PnL is attributed to you automatically."
    )
    return "\n".join(lines)


# ── Session context vs typed config ──

# A trading_context is free text; connector_name / trading_pair are typed config
# the engine, credentials and PnL attribution actually key off. These patterns
# only exist to spot the case where the prose names a DIFFERENT venue than the
# typed values, so the prompt can state which one wins instead of leaving the
# agent to invent a resolution (session_6 invented one and switched venue at
# tick #8). Deliberately conservative: a token counts as a venue only when it
# carries a venue suffix, so prose like "one-way mode" or "connector_name" is
# never read as a venue. Bare single-word connectors ("kucoin") are missed —
# missing a mention is safe, inventing one is not.
_CONNECTOR_TOKEN_RE = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")
_CONNECTOR_SUFFIXES = ("_perpetual", "_paper_trade", "_testnet", "_spot")
# Pairs carry an optional uppercase ``ISSUER:`` prefix on HIP-3 markets
# ("XYZ:CL-USD", handlers/cex/_shared.py:532) — capture it, or a context
# naming the configured pair VERBATIM matches only the "CL-USD" tail and is
# scored as a foreign pair.
_PAIR_TOKEN_RE = re.compile(
    r"\b(?:([A-Z0-9]{1,12}):)?([A-Z0-9]{2,10})-([A-Z0-9]{2,10})\b"
)
# Quote side must look like a quote asset, so "TP-SL" in prose is not a pair.
_QUOTE_ASSETS = {
    "USD",
    "USDT",
    "USDC",
    "USDE",
    "BUSD",
    "DAI",
    "TUSD",
    "FDUSD",
    "BTC",
    "ETH",
    "BNB",
    "SOL",
    "EUR",
    "GBP",
    "JPY",
    "TRY",
}
_LEVERAGE_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*x\s*leverage|leverage\s*(?:of|:|=)?\s*(\d+(?:\.\d+)?)",
    re.IGNORECASE,
)


_TRADE_VERB_RE = re.compile(
    r"\b(?:trade|trades|trading|market-?make|market-?making|execute|enter|"
    r"entries|entry|deploy|quote|quotes|ladder|position)\b"
)
# Words that make a venue mention a READ, not an order: the config's own proxy
# candle feed is described with these ("candles from hyperliquid_perpetual").
_DATA_WORD_RE = re.compile(
    r"\b(?:candle|candles|feed|proxy|data|funding|reference|watch|monitor|"
    r"compare|depth|book|orderbook|price|prices|signal|signals)\b"
)


# A prohibition, or a hedge leg, is not an override. "Do not trade on X" names a
# trade verb and X in one clause, but instructs the opposite; a hedge leg is a
# second venue the operator wants traded ALONGSIDE the configured one, which
# detect_venue_conflicts' own docstring calls as likely as an override. Both veto
# the clause, in the same fail-toward-the-typed-config direction as _DATA_WORD_RE.
_NEGATION_RE = re.compile(
    r"\b(?:do\s*n[o']t|don't|does\s*not|never|no|not|avoid|without|except|"
    r"refrain|forbid|forbidden|prohibited)\b"
)
_HEDGE_RE = re.compile(r"\b(?:hedge|hedges|hedging|hedged)\b")


def _named_as_trading_venue(text: str, token: str) -> bool:
    """Does the prose present ``token`` as the venue to TRADE, not one to read?

    Asked about EVERY venue the prose names: the ones this session configures
    under a non-trading key (``candle_connector`` / ``candle_trading_pair``, the
    proxy feed) AND the ones no config key carries at all. Merely mentioning the
    feed agrees with the config; "Trade XRP-USD on hyperliquid_perpetual" is the
    session_6 override wearing the feed's clothes, and has to stay a conflict.
    Equally, "Reference the binance_perpetual mark price; do not trade there."
    names a venue the config has never heard of and overrides nothing.
    Clause-scoped and conservative: no trade verb, or a data word, a prohibition
    ("do not trade on X") or a hedge leg alongside it in the same clause, means no — a missed override still meets the [CURRENT
    CONFIG] precedence line, while an invented one is a false claim put to the
    agent on every tick.
    """
    tok = token.lower()
    for clause in re.split(r"[.;\n,]", text.lower()):
        if (
            tok in clause
            and _TRADE_VERB_RE.search(clause)
            and not _DATA_WORD_RE.search(clause)
            and not _NEGATION_RE.search(clause)
            and not _HEDGE_RE.search(clause)
        ):
            return True
    return False


def _configured_values(config: dict[str, Any], marker: str) -> set[str]:
    """Every configured value of a kind, not just the primary key.

    ``candle_connector`` / ``candle_trading_pair`` are legitimately part of this
    session (the proxy candle feed), so a context that mentions them is normally
    agreeing with the config rather than contradicting it — unless it names one
    of them as the venue to TRADE, which :func:`_named_as_trading_venue` catches.
    """
    out: set[str] = set()
    for k, v in config.items():
        if marker in k and isinstance(v, str) and v.strip():
            out.add(v.strip())
    return out


def _split_pair(pair: str) -> tuple[str, str]:
    """``ISSUER:BASE-QUOTE`` -> ``("ISSUER", "BASE-QUOTE")``; no prefix -> ``("", pair)``."""
    p = pair.strip().upper()
    issuer, sep, tail = p.rpartition(":")
    return (issuer, tail) if sep else ("", p)


def _pair_matches(named: str, configured: str) -> bool:
    """Does a pair named in prose refer to the configured market?

    HIP-3 pairs carry an uppercase ``ISSUER:`` prefix (``XYZ:CL-USD``) that prose
    routinely omits, so the prefix is compared only when BOTH sides carry one.
    Otherwise ``XYZ:CL-USD`` in the config and the same string in the text (or
    its bare ``CL-USD`` tail) would score as two different markets and a context
    naming the configured pair would be reported as a conflict.
    """
    named_issuer, named_tail = _split_pair(named)
    conf_issuer, conf_tail = _split_pair(configured)
    if named_tail != conf_tail:
        return False
    return not (named_issuer and conf_issuer and named_issuer != conf_issuer)


def _dedupe(values: list[str]) -> list[str]:
    """Order-preserving dedupe (prose repeats a venue; a reader should not)."""
    seen: set[str] = set()
    return [v for v in values if not (v in seen or seen.add(v))]


def _scan_venues(config: dict[str, Any], text: str) -> dict[str, Any]:
    """Split the venues the prose names against the ones this session configures.

    Keys: ``foreign_connectors`` / ``foreign_pairs`` (the prose names them as the
    venue/market to TRADE and they are not the configured one — see
    :func:`_named_as_trading_venue`; whether the config carries them elsewhere
    makes no difference), ``connector_named`` / ``pair_named`` (the TRADING value
    is named), ``other_connectors`` / ``other_pairs`` (configured under some other
    key — the proxy candle feed — and mentioned without a trade instruction:
    agreeing with the config, yet still not where orders go). A venue that is in
    no config key and carries no trade instruction lands in NO list: the text
    establishes nothing about where to trade, so neither does this. A list stays
    empty when the config carries no such typed value: there is then nothing to
    disagree with.
    """
    out: dict[str, Any] = {
        "foreign_connectors": [],
        "connector_named": False,
        "other_connectors": [],
        "foreign_pairs": [],
        "pair_named": False,
        "other_pairs": [],
    }

    connector = str(config.get("connector_name") or "").strip()
    if connector:
        known = {c.lower() for c in _configured_values(config, "connector")}
        named = [
            t
            for t in _CONNECTOR_TOKEN_RE.findall(text.lower())
            if t.endswith(_CONNECTOR_SUFFIXES) or t in known
        ]
        # Foreign = presented as the venue to TRADE and not the configured one.
        # The intent test applies to EVERY named venue, including one that no
        # config key carries: being absent from the config is not evidence of an
        # override, only the prose is. Scoring unconfigured venues foreign
        # unconditionally made "Reference the binance_perpetual mark price; do not
        # trade there." render a [CONFIG CONFLICT] every tick — a confident claim
        # of an operator error nobody made, over a context that FORBIDS trading
        # there. The test still catches the config's own proxy feed when the prose
        # promotes it: the live flowedge_dca default_config carries
        # candle_connector=hyperliquid_perpetual, so the session_6 string ("Trade
        # XRP-USD on hyperliquid_perpetual") stays a conflict.
        out["foreign_connectors"] = _dedupe(
            [
                t
                for t in named
                if t != connector.lower() and _named_as_trading_venue(text, t)
            ]
        )
        out["connector_named"] = connector.lower() in named
        out["other_connectors"] = _dedupe(
            [
                t
                for t in named
                if t in known
                and t != connector.lower()
                and not _named_as_trading_venue(text, t)
            ]
        )

    pair = str(config.get("trading_pair") or "").strip()
    if pair:
        known_pairs = _configured_values(config, "pair")
        named_pairs: list[str] = []
        for issuer, base, quote in _PAIR_TOKEN_RE.findall(text):
            candidate = f"{issuer}:{base}-{quote}" if issuer else f"{base}-{quote}"
            if quote in _QUOTE_ASSETS or any(
                _pair_matches(candidate, k) for k in known_pairs
            ):
                named_pairs.append(candidate)
        # Mirror of the connector rule above, same reason: a pair the config does
        # not carry is foreign only when the prose names it as the market to
        # TRADE. "Reference the BTC-USDC mark price" names no override.
        out["foreign_pairs"] = _dedupe(
            [
                p
                for p in named_pairs
                if not _pair_matches(p, pair) and _named_as_trading_venue(text, p)
            ]
        )
        out["pair_named"] = any(_pair_matches(p, pair) for p in named_pairs)
        out["other_pairs"] = _dedupe(
            [
                p
                for p in named_pairs
                if not _pair_matches(p, pair)
                and any(_pair_matches(p, k) for k in known_pairs)
                and not _named_as_trading_venue(text, p)
            ]
        )

    return out


def detect_venue_conflicts(
    config: dict[str, Any], trading_context: str
) -> dict[str, tuple[str, str]]:
    """Where the free-text session context contradicts the typed config.

    Returns ``{field: (value named in the context, active typed value)}`` for
    ``connector_name`` / ``trading_pair`` / ``leverage``; empty when the two
    agree, when the context names nothing typed, when nothing in it is presented
    as the venue to TRADE (``"watch funding on binance_perpetual"`` names no
    override), or when it also names the configured value (``"trade
    derive_perpetual and hedge on binance_perpetual"`` is not a conflict — see
    :func:`named_other_venues` for the softer signal that case still produces).

    That last case is a deliberate under-read, not an oversight: when the text
    names the configured venue too, a foreign trade verb is as likely to be a
    hedge leg as an override, and the two are indistinguishable to a regex.
    Raising [CONFIG CONFLICT] there would tell the agent an operator error exists
    on the strength of a guess — the failure this block was added to prevent — so
    the case gets the [VENUE NOTE], which asserts no cause. Free text must never
    be over-read: every branch here fails towards "no conflict".
    """
    conflicts: dict[str, tuple[str, str]] = {}
    text = trading_context or ""
    if not text.strip():
        return conflicts

    scan = _scan_venues(config, text)

    connector = str(config.get("connector_name") or "").strip()
    if scan["foreign_connectors"] and not scan["connector_named"]:
        conflicts["connector_name"] = (scan["foreign_connectors"][0], connector)

    pair = str(config.get("trading_pair") or "").strip()
    if scan["foreign_pairs"] and not scan["pair_named"]:
        conflicts["trading_pair"] = (scan["foreign_pairs"][0], pair)

    lev = str(config.get("leverage") or "").strip()
    if lev:
        m = _LEVERAGE_RE.search(text)
        named_lev = (m.group(1) or m.group(2)) if m else ""
        try:
            differs = bool(named_lev) and float(named_lev) != float(lev)
        except ValueError:
            differs = False  # unparseable prose is not a conflict
        if differs:
            conflicts["leverage"] = (named_lev, lev)

    return conflicts


def named_other_venues(config: dict[str, Any], trading_context: str) -> list[str]:
    """Venues/pairs the context names that are not the TRADING venue — no verdict.

    :func:`detect_venue_conflicts` stays silent in two cases that are still worth
    a word in the prompt. (1) The prose names another venue as a trade target and
    names the configured venue TOO: "trade derive_perpetual and hedge on
    binance_perpetual" is no contradiction — but "derive_perpetual is broken,
    trade hyperliquid_perpetual instead" reads identically to a pattern and is
    exactly the session_6 failure. (2) The prose names a venue configured under
    another key (``candle_connector``, the proxy candle feed), which agrees with
    the config yet is not where orders go. A venue merely mentioned — in no
    config key and under no trade verb — is in neither case and returns nothing:
    "reference the binance_perpetual mark price" needs no word in the prompt.
    Telling those apart needs intent, which a regex does not have, so the prompt
    gets this softer signal instead: these venues are named and are not the one
    this session trades, with no claim about what the operator meant.
    """
    text = trading_context or ""
    if not text.strip():
        return []
    scan = _scan_venues(config, text)
    return _dedupe(
        scan["foreign_connectors"]
        + scan["other_connectors"]
        + scan["foreign_pairs"]
        + scan["other_pairs"]
    )


def build_tick_prompt(
    agent: Agent,
    strategy: Strategy,
    config: dict[str, Any],
    core_data: dict[str, str],
    learnings: str,
    summary: str,
    recent_decisions: str,
    risk_state: dict[str, Any],
    tick_number: int = 1,
    agent_id: str = "",
    cached_routines_section: str | None = None,
    user_memory: str = "",
    skills_index: str = "",
    ledger: Any | None = None,
) -> str:
    """Build the full prompt for one agent tick.

    Composes the Agent's domain identity (``agent.instructions``) with the
    strategy's tactic (``strategy.instructions``): the Agent says *who you are and
    what you know*; the strategy says *what to do this tick*.
    """
    from condor.acp.pydantic_ai_client import is_pydantic_ai_model

    execution_mode = config.get("execution_mode", "loop")
    is_dry_run = execution_mode == "dry_run"
    # Experiments (dry_run + run_once) keep no journal — the tick is captured as a
    # dry-run snapshot instead. Mirrors TickEngine.is_experiment in engine.py.
    is_experiment = execution_mode in ("dry_run", "run_once")
    agent_key = config.get("agent_key") or strategy.agent_key or agent.agent_key
    use_pydantic_ai = is_pydantic_ai_model(agent_key)

    # Select base prompt and journal protocol based on mode
    base_prompt = BASE_PROMPT_DRY_RUN if is_dry_run else BASE_PROMPT_LIVE
    journal_section = (
        JOURNAL_SECTION_EXPERIMENT if is_experiment else JOURNAL_SECTION_LIVE
    )
    sections: list[str] = [base_prompt, journal_section, BASE_PROMPT_COMMON]

    # Tool preload is ACP-specific (ToolSearch); pydantic-ai auto-discovers MCP tools
    if not use_pydantic_ai:
        sections.append(
            _build_tool_preload(is_dry_run=is_dry_run, is_experiment=is_experiment)
        )
    else:
        sections.append(
            "TOOLS:\n"
            "All MCP tools are pre-loaded and available. Call them directly by name."
        )

    # Tick identity
    tick_info = f"[TICK INFO]\nThis is tick #{tick_number}. Use this number in journal entries and notifications."
    if agent_id:
        tick_info += f"\nAgent ID: {agent_id}"
        if not is_dry_run:
            tick_info += f'\nPass controller_id="{agent_id}" as a TOP-LEVEL arg to manage_executors (not inside executor_config).'
    sections.append(tick_info)

    # Run-once mode note
    if execution_mode == "run_once":
        sections.append(
            "[EXECUTION MODE — RUN ONCE]\n"
            "Single-tick session with LIVE execution. The engine will stop after this tick. "
            "Make your best move now — there will be no follow-up ticks."
        )

    # Server credentials are injected via env vars into the MCP process,
    # so no need to include them in the prompt or call configure_server.

    # Agent identity + domain knowledge (who you are), then the strategy tactic
    # (what to do this tick). The Agent body is shared across all its strategies.
    if agent.instructions.strip():
        sections.append(f"[AGENT — domain identity & knowledge]\n{agent.instructions}")
    sections.append(f"[STRATEGY INSTRUCTIONS]\n{strategy.instructions}")

    # Available skills (playbooks) + routines, unified under one header. Skills
    # are read fresh each tick (the agent may create its own mid-session), so
    # they arrive via skills_index; routine discovery is cached (it's expensive).
    routines_section = cached_routines_section
    if routines_section is None:
        try:
            routines_section = _build_routines_section(strategy)
        except Exception:
            routines_section = ""  # Don't fail the tick if discovery fails
    skills_routines = ["[AVAILABLE SKILLS & ROUTINES]"]
    if skills_index:
        skills_routines.append(
            "\nSKILLS — playbooks (read before a known flow with "
            'manage_skill(action="read", name="..."); "→ routine:" links to an '
            "executable routine):\n"
            f"{skills_index}"
        )
    if routines_section:
        skills_routines.append(f"\n{routines_section}")
    sections.append("\n".join(skills_routines))

    # Session trading context (natural language directives for this session)
    trading_context = config.get("trading_context", "")
    # Which venue fields does this session actually TYPE? When it types none
    # (session_3 has no connector_name/trading_pair, and pmm_mister_operator
    # documents launching with trading_context="Do MM on PAIR on CONNECTOR"), the
    # context is the only venue source there is, and telling the agent that
    # [CURRENT CONFIG] outranks it would point at a section naming no venue. Only
    # the keys actually present are named below, so a config that types leverage
    # alone never claims [CURRENT CONFIG] is where the venue comes from.
    typed_venue_keys = [
        k for k in ("connector_name", "trading_pair", "leverage") if config.get(k)
    ]
    typed_venue = bool(typed_venue_keys)
    # "a different connector_name, trading_pair or leverage" — reads as prose for
    # one typed key or three.
    venue_fields = (
        ", ".join(typed_venue_keys[:-1]) + " or " + typed_venue_keys[-1]
        if len(typed_venue_keys) > 1
        else "".join(typed_venue_keys)
    )
    if trading_context:
        sections.append(
            "[SESSION CONTEXT]\n"
            "The user provided the following natural language context for this trading session. "
            + (
                "Use this to guide your risk appetite and trading style. It does NOT "
                "override the typed config — [CURRENT CONFIG] below is authoritative "
                f"for {' / '.join(typed_venue_keys)}:\n\n"
                if typed_venue
                else "Use this to guide your market selection, risk appetite, and "
                "trading style:\n\n"
            )
            + f"{trading_context}"
        )

    # Current config (exclude keys shown elsewhere or not useful to the LLM)
    _CONFIG_EXCLUDE = {
        "trading_context",
        "risk_limits",  # shown in dedicated sections
        "agent_key",
        "server_name",
        "frequency_sec",
        "execution_mode",  # noise / internal
    }
    # Name [SESSION CONTEXT] only when that section was actually rendered above.
    # A session that types venue keys but carries no trading_context — the shape
    # of hip_3_delta_neutral_funding_mm, hip_3_mm_operator, pmm_mister_operator
    # and lp_slot_operator, whose default_trading_context is '' — would otherwise
    # be told to outrank a section that is not in this prompt.
    outranked = (
        "the strategy instructions OR [SESSION CONTEXT]"
        if trading_context
        else "the strategy instructions"
    )
    config_lines = [
        "[CURRENT CONFIG]",
        (
            "These are the ACTIVE values for this session and they OUTRANK every other "
            f"section. If {outranked} mention a different "
            f"{venue_fields}, IGNORE them and use these values "
            "instead — they are what the engine, the credentials and the PnL attribution "
            "are keyed to."
            if typed_venue
            else "These are the ACTIVE values for this session. If the strategy "
            "instructions mention different defaults, IGNORE them and use these values "
            "instead."
        ),
    ]
    for k, v in config.items():
        if k in _CONFIG_EXCLUDE:
            continue
        config_lines.append(f"{k}: {v}")
    sections.append("\n".join(config_lines))

    # When the free text and the typed config actually name different venues,
    # say so outright. Left implicit, the agent resolves the contradiction
    # itself: session_6 read both blocks, decided the config was "misconfigured"
    # and from tick #8 computed entries for the venue named in the prose.
    try:
        conflicts = detect_venue_conflicts(config, trading_context)
        # Venues the context names that detect_venue_conflicts does NOT call a
        # contradiction (the configured candle feed; a venue named as a trade
        # target in a text that names the configured venue too) get a softer note
        # instead: state the precedence without asserting WHY the other venue is
        # in the text, because from the text alone that is not established.
        # Anything the conflict block already names is left out of it.
        already = {v for both in conflicts.values() for v in both}
        other_venues = [
            v for v in named_other_venues(config, trading_context) if v not in already
        ]
    except Exception:  # free text — never fail a tick over a parse
        conflicts, other_venues = {}, []
    if conflicts:
        named = ", ".join(f"{k}={ctx}" for k, (ctx, _) in conflicts.items())
        active = ", ".join(f"{k}={cfg}" for k, (_, cfg) in conflicts.items())
        sections.append(
            f"[CONFIG CONFLICT]\n[SESSION CONTEXT] names {named}, but the ACTIVE config is "
            f"{active}. Trade {active} — the config wins. Do NOT switch venue, pair or "
            "leverage to match the text; journal the discrepancy and ask the operator to "
            "fix it instead."
        )
    if other_venues:
        # Deliberately claims nothing about the operator's intent. The previous
        # wording said these venues were named "alongside this session's
        # configured venue" and were "reference or hedge data only" — both are
        # assertions the text does not support ("trade BTC-USDC and ETH-USDC"
        # names the configured pair nowhere and names both as trade targets).
        # Name only the venue keys this config actually types: a session with a
        # connector_name and no trading_pair must not be pointed at an ACTIVE
        # trading_pair that [CURRENT CONFIG] does not carry.
        note_keys = [k for k in ("connector_name", "trading_pair") if config.get(k)]
        if note_keys:
            note_subject = (
                f"The ACTIVE {' / '.join(note_keys)} above "
                f"{'are' if len(note_keys) > 1 else 'is'}"
            )
        else:
            # Unreachable: a named venue needs a typed one to be "other" than.
            # Kept so a surprising config cannot crash the tick on an index.
            note_subject = "The ACTIVE values above are"
        sections.append(
            "[VENUE NOTE]\n[SESSION CONTEXT] also names "
            + ", ".join(other_venues)
            + f". {note_subject} where this session "
            "trades — keep orders there. Why the context names the other venue is NOT "
            "established here: it may be a reference feed, a hedge, or an instruction to "
            "trade elsewhere. Do not switch venue or pair on it and do not conclude the "
            "config is wrong; if it reads as an instruction to act there, journal that "
            "and ask the operator."
        )

    # Controller mode: the agent steers a named bot's controllers instead of
    # spawning standalone executors. Triggered solely by a non-empty bot_name.
    bot_name = config.get("bot_name", "")
    if bot_name:
        sections.append(_build_controller_mode_section(bot_name, ledger))

    # Risk state
    rs = risk_state
    max_dd = rs.get("max_drawdown_pct", -1)
    dd_display = (
        f"{rs.get('drawdown_pct', 0):.1f}% / {max_dd:.1f}% limit"
        if max_dd >= 0
        else "disabled"
    )
    risk_lines = [
        "[RISK STATE]",
        f"Position Size: ${rs.get('total_exposure', 0):.2f} / ${rs.get('max_position_size', 500):.2f} limit",
        f"Open Executors: {rs.get('executor_count', 0)} / {rs.get('max_open_executors', 5)} limit",
        f"Drawdown: {dd_display}",
        f"Status: {'BLOCKED - ' + rs.get('block_reason', '') if rs.get('is_blocked') else 'ACTIVE'}",
    ]
    sections.append("\n".join(risk_lines))

    # Core skill data (pre-computed)
    for name, data_summary in core_data.items():
        sections.append(f"[CORE DATA - {name}]\n{data_summary}")

    # User memory -- what is known about the owner (preferences/profile)
    if user_memory:
        sections.append(
            "[USER MEMORY — what is known about the owner; advisory]\n"
            'Read detail with manage_memory(action="read", name="...").\n\n'
            f"{user_memory}"
        )

    # Journal -- compact memory
    if learnings:
        sections.append(
            f"[LEARNINGS — do NOT repeat these, only add genuinely new insights]\n{learnings}"
        )
    if summary:
        sections.append(f"[CURRENT STATUS]\n{summary}")
    if recent_decisions:
        sections.append(f"[RECENT DECISIONS — last 3 snapshots]\n{recent_decisions}")

    return "\n\n".join(sections)
