"""Risk engine -- pre-tick validation and guardrails.

Enforces position limits, daily loss caps, drawdown limits, executor counts,
and LLM cost caps.  Also provides a permission callback that auto-approves
safe tool calls and blocks dangerous ones that violate risk limits.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .ownership import BotLedger

log = logging.getLogger(__name__)

# The one negative value that means "no drawdown limit" rather than a magnitude.
DRAWDOWN_DISABLED = -1.0


def _normalize_drawdown_limit(name: str, value: Any) -> Any:
    """Read a negatively-spelled drawdown limit as its magnitude.

    Configs are routinely written as ``max_drawdown_pct: -8`` -- the natural way
    to say "pause me at 8% down" -- but the checks in ``get_state`` compare
    against a positive magnitude and treat anything below zero as the documented
    ``-1`` disabled sentinel. That spelling therefore switched the soft pause
    silently OFF while the playbook still told the agent a limit was in force.
    Any negative other than the sentinel is read as the magnitude its author
    meant, and the coercion is logged so it is visible rather than invisible.

    Applied ONLY to ``max_drawdown_pct`` -- see ``RiskLimits.__post_init__``.
    """
    # Non-numeric config is left untouched: it should fail where it always did
    # (at the comparison), not turn construction into the error site.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return value
    if value >= 0 or float(value) == DRAWDOWN_DISABLED:
        return value
    coerced = abs(float(value))
    log.warning(
        "Risk limit %s=%s read as %.1f%%: a negative magnitude enables the "
        "limit at its absolute value (only -1 disables it)",
        name,
        value,
        coerced,
    )
    return coerced


# Config keys that can carry the quote notional of an executor create, in no
# particular order -- the gate reads every one that is present (see
# ``_executor_notional``).
_NOTIONAL_KEYS = ("total_amount_quote", "amounts_quote", "amount")


def _executor_notional(config: dict) -> tuple[float, str]:
    """Quote notional an executor create would put at risk.

    A ``dca_executor`` config carries no ``total_amount_quote`` at all: its size
    lives in ``amounts_quote``, a LIST with one entry per ladder level (see
    ``_build_ladder`` in agents/flow_edge/routines/flow_edge_signal.py). Reading
    only the scalar keys therefore measured every DCA create as $0 and the
    position-size gate never bound, whatever the ladder's notional. Lists are
    summed; the largest reading across the keys present wins, so a config whose
    ``amounts_quote`` sums past its own declared ``total_amount_quote`` is
    gated on what it actually deploys.

    Returns ``(notional, problem)``. ``problem`` is non-empty when a size field
    is present but unreadable -- the caller blocks in that case rather than
    approving an unmeasurable create against a 0.
    """
    notional = 0.0
    for key in _NOTIONAL_KEYS:
        raw = config.get(key)
        if raw is None:
            continue
        levels = raw if isinstance(raw, (list, tuple)) else [raw]
        total = 0.0
        for level in levels:
            try:
                total += float(level)
            except (TypeError, ValueError):
                return 0.0, f"{key}={raw!r} is not a readable amount"
        notional = max(notional, total)
    return notional, ""


@dataclass
class RiskLimits:
    max_position_size_quote: float = 500.0
    max_open_executors: int = 5
    max_drawdown_pct: float = -1.0
    # Hard kill-switch: a deeper drawdown than the soft ``max_drawdown_pct`` pause;
    # breaching it winds down positions (see condor.agents.shutdown). -1 = disabled.
    shutdown_drawdown_pct: float = -1.0

    def __post_init__(self) -> None:
        # Normalise at construction rather than at the comparison so the value
        # RiskState.to_dict exports -- and which the prompt's [RISK STATE] block
        # and the journal snapshots render as "Drawdown: ..." -- is already the
        # limit actually being enforced, instead of reading "disabled".
        #
        # Deliberately NOT applied to shutdown_drawdown_pct. The two fields are
        # different classes of action, so re-reading a negative differently is
        # only safe for one of them:
        #   * max_drawdown_pct only pauses ticks. Reading -8 as 8 can at worst
        #     stop an agent trading; nothing is placed or closed as a result,
        #     and every shipped session writes it as a negative magnitude.
        #   * shutdown_drawdown_pct arms an emergency winddown that CLOSES
        #     positions (condor.agents.shutdown). Coercing it would re-arm a
        #     hard kill switch on configs whose author wrote a negative to mean
        #     "off" -- which is exactly what condor/agents/config.py ("-1 =
        #     disabled") and the frontend's AgentControls invite. Turning a
        #     position-closing action back ON by reinterpreting existing config
        #     is not a change this normalisation is entitled to make, so any
        #     negative here keeps its documented meaning: disabled.
        self.max_drawdown_pct = _normalize_drawdown_limit(
            "max_drawdown_pct", self.max_drawdown_pct
        )

    @classmethod
    def from_dict(cls, d: dict) -> RiskLimits:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class RiskState:
    total_exposure: float = 0.0
    executor_count: int = 0
    drawdown_pct: float = 0.0
    is_blocked: bool = False
    block_reason: str = ""
    # Hard escalation: set when the shutdown drawdown threshold is breached. The
    # soft ``is_blocked`` only pauses the tick; this triggers an emergency winddown.
    should_shutdown: bool = False
    shutdown_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_exposure": self.total_exposure,
            "executor_count": self.executor_count,
            "drawdown_pct": self.drawdown_pct,
            "is_blocked": self.is_blocked,
            "block_reason": self.block_reason,
            "should_shutdown": self.should_shutdown,
            "shutdown_reason": self.shutdown_reason,
            # Include limits for prompt display
            "max_position_size": (
                self._limits.max_position_size_quote
                if hasattr(self, "_limits")
                else 500
            ),
            "max_open_executors": (
                self._limits.max_open_executors if hasattr(self, "_limits") else 5
            ),
            "max_drawdown_pct": (
                self._limits.max_drawdown_pct if hasattr(self, "_limits") else -1
            ),
            "shutdown_drawdown_pct": (
                self._limits.shutdown_drawdown_pct if hasattr(self, "_limits") else -1
            ),
        }


class RiskEngine:
    """Evaluates risk state and can block snapshots or individual tool calls."""

    def __init__(self, limits: RiskLimits | None = None):
        self.limits = limits or RiskLimits()

    def get_state(self, tracker: Any) -> RiskState:
        """Compute current risk metrics from tracker data."""
        state = RiskState()
        state._limits = self.limits

        try:
            state.total_exposure = tracker.get_total_exposure()
            state.executor_count = tracker.get_open_executor_count()
            state.drawdown_pct = tracker.get_drawdown_pct()
        except Exception as exc:
            log.exception("Failed to compute risk state from tracker")
            # Fail closed: without real metrics we must not approve creates
            # against zeroed exposure/count. A blocked state makes the engine
            # pause the tick and notify instead of trading blind.
            state.is_blocked = True
            state.block_reason = f"risk state unavailable: {exc}"
            return state

        # Check blocking conditions
        reasons = []

        if (
            self.limits.max_drawdown_pct >= 0
            and state.drawdown_pct > self.limits.max_drawdown_pct
        ):
            reasons.append(
                f"Drawdown {state.drawdown_pct:.1f}% exceeds limit {self.limits.max_drawdown_pct:.1f}%"
            )

        if reasons:
            state.is_blocked = True
            state.block_reason = "; ".join(reasons)

        # Hard kill-switch: a deeper drawdown than the soft pause. Evaluated
        # independently so a breach escalates to a winddown even though it also
        # trips the soft block (the engine checks should_shutdown first).
        if (
            self.limits.shutdown_drawdown_pct >= 0
            and state.drawdown_pct > self.limits.shutdown_drawdown_pct
        ):
            state.should_shutdown = True
            state.shutdown_reason = (
                f"Drawdown {state.drawdown_pct:.1f}% exceeds shutdown limit "
                f"{self.limits.shutdown_drawdown_pct:.1f}%"
            )

        return state

    def check_executor_action(
        self, tool_call: dict, current_state: RiskState
    ) -> tuple[bool, str]:
        """Check if an executor creation is within risk limits.

        On approval of a "create", accumulates it into ``current_state``
        (executor count and exposure) so subsequent checks within the same
        tick see the running totals instead of the frozen per-tick snapshot.
        The state is recomputed from the journal at the start of each tick.

        Returns (allowed, reason).
        """
        input_data = tool_call.get("input", {})
        action = input_data.get("action", "")

        # Only gate "create" actions
        if action != "create":
            return True, ""

        # Check executor count
        if current_state.executor_count >= self.limits.max_open_executors:
            return (
                False,
                f"Max open executors ({self.limits.max_open_executors}) reached",
            )

        # Check position size. Reads scalar *and* list-valued size keys, so a
        # dca_executor ladder (amounts_quote: [...]) is measured instead of
        # silently sizing to 0.
        config = input_data.get("executor_config", {})
        amount, problem = _executor_notional(config)
        if problem:
            # Fail closed: an unmeasurable create cannot be gated, and letting
            # it through would be gating it against a fabricated $0.
            return False, f"Cannot size executor create: {problem}"

        if current_state.total_exposure + amount > self.limits.max_position_size_quote:
            return False, (
                f"Would exceed position limit: ${current_state.total_exposure + amount:.2f} > "
                f"${self.limits.max_position_size_quote:.2f}"
            )

        # Approved: accumulate into the snapshot so the next create in this
        # tick is gated against the running totals, not the pre-tick numbers.
        current_state.executor_count += 1
        current_state.total_exposure += amount

        return True, ""

    def check_bot_action(self, tool_call: dict) -> tuple[bool, str]:
        """Check a manage_bots call against risk limits.

        A bot's capital lives in saved controller configs on the API server,
        so exposure can't be computed from the tool inputs alone. Instead,
        bound the loss: a deploy must declare ``max_global_drawdown_quote``
        (the platform-enforced kill switch) no larger than the strategy's
        position limit. Stops are risk-reducing and always allowed; an
        ``update_config`` is only gated when it declares a
        ``total_amount_quote`` above the position limit.

        Returns (allowed, reason).
        """
        input_data = tool_call.get("input", {})
        action = input_data.get("action", "")

        if action == "deploy":
            cap = input_data.get("max_global_drawdown_quote")
            if not cap:
                return False, (
                    "Bot deploy must declare max_global_drawdown_quote "
                    f"(≤ ${self.limits.max_position_size_quote:.2f}) so the "
                    "platform kill switch bounds the loss"
                )
            if float(cap) > self.limits.max_position_size_quote:
                return False, (
                    f"max_global_drawdown_quote ${float(cap):.2f} exceeds "
                    f"position limit ${self.limits.max_position_size_quote:.2f}"
                )
        elif action == "update_config":
            amount = float(
                (input_data.get("config_data") or {}).get("total_amount_quote", 0) or 0
            )
            if amount > self.limits.max_position_size_quote:
                return False, (
                    f"update_config total_amount_quote ${amount:.2f} exceeds "
                    f"position limit ${self.limits.max_position_size_quote:.2f}"
                )

        return True, ""


def auto_approve_with_risk_check(
    risk_engine: RiskEngine,
    risk_state: RiskState,
    execution_mode: str = "loop",
    ledger: "BotLedger | None" = None,
):
    """Build a permission callback that auto-approves safe tools and risk-checks dangerous ones.

    ``ledger`` (FEAT-017) scopes bot ownership: with one, a ``manage_bots`` action
    that deploys or mutates a bot outside the session's namespace is cancelled and
    recorded. ``None`` (consults, delegations, chat, executor-mode agents) keeps
    today's behavior exactly.
    """
    from handlers.agents._shared import DANGEROUS_BOT_ACTIONS, is_dangerous_tool_call

    async def callback(tool_call: dict, options: list[dict]) -> dict:
        if is_dangerous_tool_call(tool_call):
            raw_name = tool_call.get("tool", "") or tool_call.get("title", "")
            tool_name = raw_name.rsplit("__", 1)[-1] if "__" in raw_name else raw_name

            # Dry-run mode: block ALL mutating actions
            if execution_mode == "dry_run":
                if tool_name == "manage_executors":
                    input_data = tool_call.get("input", {})
                    action = input_data.get("action", "")
                    if action in ("create", "stop"):
                        log.info("Dry-run mode: blocked manage_executors(%s)", action)
                        return {"outcome": {"outcome": "cancelled"}}
                elif tool_name == "manage_bots":
                    input_data = tool_call.get("input", {})
                    action = input_data.get("action", "")
                    if action in DANGEROUS_BOT_ACTIONS:
                        log.info("Dry-run mode: blocked manage_bots(%s)", action)
                        return {"outcome": {"outcome": "cancelled"}}
                elif tool_name in (
                    "place_order",
                    "manage_gateway_swaps",
                    "manage_gateway_clmm",
                ):
                    log.info("Dry-run mode: blocked %s", tool_name)
                    return {"outcome": {"outcome": "cancelled"}}

            # For executor actions, run risk check
            if tool_name == "manage_executors":
                input_data = tool_call.get("input", {})
                action = input_data.get("action", "")

                # Validate controller_id on create
                if action == "create":
                    executor_config = input_data.get("executor_config", {})
                    if not executor_config.get("controller_id"):
                        log.warning("Blocked executor create: missing controller_id")
                        return {"outcome": {"outcome": "cancelled"}}

                allowed, reason = risk_engine.check_executor_action(
                    tool_call, risk_state
                )
                if not allowed:
                    log.warning("Risk engine blocked tool call: %s", reason)
                    return {"outcome": {"outcome": "cancelled"}}

            # Bot deploys place real capital via controllers — bound the loss
            # (declared drawdown kill switch) since the amount isn't in the call
            if tool_name == "manage_bots":
                # Ownership first: an agent may only touch bots in its own
                # namespace. Read-only actions (status/logs/get_config) are not
                # in DANGEROUS_BOT_ACTIONS, so it still sees the whole fleet.
                if ledger is not None:
                    input_data = tool_call.get("input", {})
                    action = input_data.get("action", "")
                    if action in DANGEROUS_BOT_ACTIONS:
                        bot_name = input_data.get("bot_name", "") or ""
                        if not ledger.owns(bot_name):
                            log.warning(
                                "Ownership: blocked manage_bots(%s) on '%s' "
                                "(namespace %s)",
                                action,
                                bot_name,
                                ledger.namespace,
                            )
                            ledger.note_violation(bot_name, action)
                            return {"outcome": {"outcome": "cancelled"}}

                allowed, reason = risk_engine.check_bot_action(tool_call)
                if not allowed:
                    log.warning("Risk engine blocked tool call: %s", reason)
                    return {"outcome": {"outcome": "cancelled"}}

                # Recorded only once the call is actually going through, so a
                # risk-rejected deploy never lands in the ledger.
                if ledger is not None:
                    input_data = tool_call.get("input", {})
                    if input_data.get("action", "") == "deploy":
                        ledger.note_deploy(input_data.get("bot_name", "") or "")

            # Block direct order placement entirely
            if tool_name == "place_order":
                log.warning("Blocked direct place_order (agents must use executors)")
                return {"outcome": {"outcome": "cancelled"}}

        # Auto-approve everything else
        for opt in options:
            if opt.get("kind") in ("allow_once", "allow_always"):
                return {"outcome": {"outcome": "selected", "optionId": opt["optionId"]}}
        if options:
            return {
                "outcome": {"outcome": "selected", "optionId": options[0]["optionId"]}
            }
        return {"outcome": {"outcome": "cancelled"}}

    return callback
