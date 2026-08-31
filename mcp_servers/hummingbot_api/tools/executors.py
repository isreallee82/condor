"""
Executor management tools for Hummingbot MCP Server.

This module provides business logic for managing trading executors including
creation, viewing, stopping, and position management with progressive disclosure.
"""

import logging
from typing import Any

from mcp_servers.hummingbot_api.executor_preferences import executor_preferences
from mcp_servers.hummingbot_api.formatters.executors import (
    format_executor_detail,
    format_executor_schema_table,
    format_executors_table,
    format_positions_held_table,
    format_positions_summary,
)
from mcp_servers.hummingbot_api.schemas import ManageExecutorsRequest

logger = logging.getLogger("hummingbot-mcp")

# Internal fields injected by the MCP layer, not user-supplied
_INTERNAL_FIELDS = {"type", "executor_type", "id"}


def validate_executor_config(
    config: dict[str, Any], schema: dict[str, Any]
) -> list[str]:
    """Validate config keys against the backend schema properties.

    Returns a list of error strings. An empty list means the config is valid.
    """
    errors: list[str] = []
    _validate_level(config, schema, "", errors)
    return errors


def _validate_level(
    config: dict[str, Any], schema: dict[str, Any], path: str, errors: list[str]
) -> None:
    """Recursively validate config keys against schema properties."""
    properties = schema.get("properties", {})
    if not properties:
        return

    allowed = set(properties.keys())

    for key in config:
        if not path and key in _INTERNAL_FIELDS:
            continue
        if key not in allowed:
            field_list = ", ".join(sorted(allowed - _INTERNAL_FIELDS))
            location = f" inside '{path}'" if path else ""
            errors.append(
                f"Unknown field '{key}'{location}. Allowed fields: {field_list}"
            )
            continue
        # Recurse into nested objects
        prop_schema = properties[key]
        if (
            isinstance(prop_schema, dict)
            and isinstance(config[key], dict)
            and "properties" in prop_schema
        ):
            _validate_level(config[key], prop_schema, key, errors)


class ControllerIdError(ValueError):
    """A caller-supplied controller_id could not be honoured.

    Raised instead of quietly falling back to "main". The shared "main" bucket is
    what per-controller PnL, open-executor counts, position scans and emergency
    exits are all scoped by, so silently booking an executor there mis-states risk
    for every controller at once - and does it invisibly.
    """


def resolve_controller_id(
    top_level: Any,
    caller_config: dict[str, Any] | None,
    merged_config: dict[str, Any],
) -> tuple[str, str]:
    """Resolve which controller owns a new executor.

    `controller_id` is accepted in two places because callers legitimately use
    both: the top-level `controller_id` argument (preferred) and nested inside
    `executor_config`. Either is honoured; supplying both is fine as long as they
    agree. A `controller_id` sitting in the saved executor defaults acts as a
    lower-priority fallback, below anything the caller passed on this call.

    Args:
        top_level: the request's top-level `controller_id`.
        caller_config: the caller's own `executor_config`, before defaults were
            merged in - used so a saved default never conflicts with an explicit
            argument.
        merged_config: `executor_config` after defaults were merged in.

    Returns:
        `(controller_id, source)` - the resolved id, plus a short label naming
        where it came from, for logging.

    Raises:
        ControllerIdError: if the caller named controller_id but no usable value
            can be taken from it - every value blank or non-string, or two usable
            values that disagree. These are refused loudly rather than defaulted
            to "main". A `null` in one place is ignored when the other place holds
            a usable value.
    """
    # Everywhere the caller named controller_id on this call, in priority order.
    named: list[tuple[str, Any]] = []
    if top_level is not None:
        named.append(("the top-level controller_id argument", top_level))
    if caller_config is not None and "controller_id" in caller_config:
        named.append(
            ("executor_config['controller_id']", caller_config["controller_id"])
        )

    usable = [
        (source, value.strip())
        for source, value in named
        if isinstance(value, str) and value.strip()
    ]

    if usable:
        distinct = {value for _, value in usable}
        if len(distinct) > 1:
            detail = " and ".join(f"{source} = {value!r}" for source, value in usable)
            raise ControllerIdError(
                f"Conflicting controller_id: {detail}. Refusing to guess which one "
                "owns the executor - picking either would split this controller's "
                "executors across two buckets. Pass the same controller_id in both "
                "places, or in only one."
            )
        return usable[0][1], usable[0][0]

    if named:
        # The caller meant to set an owner but gave nothing usable. Falling back to
        # "main" here is precisely the silent mis-booking this guard exists to stop.
        detail = " and ".join(f"{source} = {value!r}" for source, value in named)
        raise ControllerIdError(
            f"controller_id was supplied but is not a usable identifier: {detail}. "
            "Refusing to fall back to 'main': that would book this executor in the "
            "shared bucket and silently corrupt per-controller PnL, open-executor "
            "counts and position scans. Pass a non-empty controller_id string, or "
            "omit the argument entirely to deliberately use 'main'."
        )

    # Nothing supplied by the caller. Fall back to a saved default if one exists,
    # then to "main" - the genuine no-controller case, unchanged from before.
    from_defaults = merged_config.get("controller_id")
    if isinstance(from_defaults, str) and from_defaults.strip():
        return from_defaults.strip(), "saved executor defaults"

    return "main", "default (nothing supplied)"


async def manage_executors(
    client: Any, request: ManageExecutorsRequest
) -> dict[str, Any]:
    """
    Manage executors with progressive disclosure.

    Args:
        client: Hummingbot API client
        request: ManageExecutorsRequest with action and parameters

    Returns:
        Dictionary containing results and formatted output
    """
    flow_stage = request.get_flow_stage()

    if flow_stage == "list_types":
        # Brief static response — full descriptions are in the tool docstring
        formatted = (
            "Available Executor Types:\n\n"
            "- **position_executor** — Directional trading with entry, stop-loss, and take-profit\n"
            "- **dca_executor** — Dollar-cost averaging for gradual position building\n"
            "- **grid_executor** — Grid trading across multiple price levels in ranging markets\n"
            "- **order_executor** — Simple BUY/SELL order with execution strategy\n"
            "- **lp_executor** — Liquidity provision on CLMM DEXs (Meteora, Raydium)\n\n"
            "Provide `executor_type` to see the configuration schema."
        )

        return {
            "action": "list_types",
            "formatted_output": formatted,
            "next_step": "Call again with 'executor_type' to see the configuration schema",
            "example": "manage_executors(executor_type='position_executor')",
        }

    elif flow_stage == "show_schema":
        # Stage 2: Show config schema with user defaults
        try:
            schema = await client.executors.get_executor_config_schema(
                request.executor_type
            )
        except Exception as e:
            return {
                "action": "show_schema",
                "error": f"Failed to get schema for {request.executor_type}: {e}",
                "formatted_output": f"Error: Failed to get schema for {request.executor_type}: {e}",
            }

        # Get user defaults
        user_defaults = executor_preferences.get_defaults(request.executor_type)

        # Get the guide from the markdown file
        executor_guide = executor_preferences.get_executor_guide(request.executor_type)

        formatted = f"Configuration Schema for {request.executor_type}\n\n"
        if executor_guide:
            formatted += f"{executor_guide}\n\n"

        formatted += format_executor_schema_table(schema, user_defaults)

        if user_defaults:
            formatted += f"\n\nYour saved defaults for {request.executor_type}:\n"
            for key, value in user_defaults.items():
                formatted += f"  {key}: {value}\n"
            formatted += (
                f"\nPreferences file: {executor_preferences.get_preferences_path()}"
            )

        return {
            "action": "show_schema",
            "executor_type": request.executor_type,
            "schema": schema,
            "user_defaults": user_defaults,
            "formatted_output": formatted,
            "next_step": "Call with action='create' and executor_config to create an executor",
            "example": f"manage_executors(action='create', executor_type='{request.executor_type}', executor_config={{...}})",
        }

    elif flow_stage == "create":
        # Stage 3: Create executor
        executor_type = (
            request.executor_type
            or request.executor_config.get("type")
            or request.executor_config.get("executor_type")
        )

        if not executor_type:
            return {
                "action": "create",
                "error": "executor_type is required for creating an executor",
                "formatted_output": "Error: Please provide executor_type",
            }

        # Merge with defaults
        merged_config = executor_preferences.merge_with_defaults(
            executor_type, request.executor_config
        )

        # Ensure type is set in config
        if "type" not in merged_config and "executor_type" not in merged_config:
            merged_config["type"] = executor_type

        # Validate config fields against backend schema before sending
        try:
            schema = await client.executors.get_executor_config_schema(executor_type)
            validation_errors = validate_executor_config(merged_config, schema)
            if validation_errors:
                error_list = "\n".join(f"  - {e}" for e in validation_errors)
                return {
                    "action": "create",
                    "error": f"Invalid executor configuration:\n{error_list}",
                    "formatted_output": (
                        f"Error: Invalid configuration for {executor_type}:\n\n"
                        f"{error_list}\n\n"
                        f"Please fix the fields above and try again."
                    ),
                }
        except Exception:
            pass  # If schema fetch fails, skip validation

        account = request.account_name or "master_account"

        try:
            controller_id, controller_id_source = resolve_controller_id(
                request.controller_id, request.executor_config, merged_config
            )
        except ControllerIdError as e:
            return {
                "action": "create",
                "error": str(e),
                "formatted_output": f"Error: {e}",
            }

        # Write the resolved owner back INTO the config. This is the load-bearing
        # line, not the keyword argument below: the backend stores the sibling
        # `controller_id` on the executor's record, but the executor object itself
        # is built from `executor_config`, and `ExecutorConfigBase.controller_id`
        # hard-defaults to "main". A config without the field therefore produces an
        # executor whose own identity - and the `config` block echoed back by
        # search/detail, the only place callers can read it - says "main" no matter
        # what was passed alongside it. That is what made the top-level argument
        # look like it was being ignored.
        merged_config["controller_id"] = controller_id

        logger.info(
            "create_executor: controller_id=%r (from %s; request=%r, config_had=%r), "
            "type=%s, account=%s",
            controller_id,
            controller_id_source,
            request.controller_id,
            "controller_id" in (request.executor_config or {}),
            executor_type,
            account,
        )

        try:
            result = await client.executors.create_executor(
                executor_config=merged_config,
                account_name=account,
                controller_id=controller_id,
            )

            # Save as default if requested
            if request.save_as_default:
                executor_preferences.update_defaults(
                    executor_type, request.executor_config
                )

            executor_id = result.get("executor_id") or result.get("id")

            formatted = f"Executor created successfully!\n\n"
            formatted += f"Executor ID: {executor_id or 'N/A'}\n"
            formatted += f"Type: {executor_type}\n"
            formatted += f"Account: {account}\n"
            # Show the OWNER that was actually resolved. Without this the caller has
            # no way to confirm it on create -- the only readback is a separate
            # `search` for the id -- which is how every executor silently booking
            # under "main" went unnoticed for a whole session. An agent told to
            # verify its controller_id needs something here to verify against.
            formatted += f"Controller ID: {controller_id}\n"

            if request.save_as_default:
                formatted += f"\nConfiguration saved as default for {executor_type}"

            return {
                "action": "create",
                "executor_id": executor_id,
                "executor_type": executor_type,
                "account": account,
                "config_used": merged_config,
                "saved_as_default": request.save_as_default,
                "result": result,
                "formatted_output": formatted,
            }

        except Exception as e:
            return {
                "action": "create",
                "error": str(e),
                "formatted_output": f"Error creating executor: {e}",
            }

    elif flow_stage == "search":
        # Search executors, or get detail for a specific executor_id
        try:
            if request.executor_id:
                # Get specific executor detail
                result = await client.executors.get_executor(request.executor_id)
                formatted = format_executor_detail(result)
                return {
                    "action": "search",
                    "executor_id": request.executor_id,
                    "executor": result,
                    "formatted_output": formatted,
                }

            result = await client.executors.search_executors(
                account_names=request.account_names,
                connector_names=request.connector_names,
                trading_pairs=request.trading_pairs,
                executor_types=request.executor_types,
                status=request.status,
                cursor=request.cursor,
                limit=request.limit,
                controller_ids=request.controller_ids,
            )

            executors = (
                result.get("data", result) if isinstance(result, dict) else result
            )
            if not isinstance(executors, list):
                executors = [executors] if executors else []

            formatted = f"Executors Found: {len(executors)}\n\n"
            formatted += format_executors_table(executors)

            # Add pagination info if available
            if isinstance(result, dict) and "next_cursor" in result:
                formatted += f"\n\nNext cursor: {result.get('next_cursor')}"

            return {
                "action": "search",
                "executors": executors,
                "count": len(executors),
                "cursor": (
                    result.get("next_cursor") if isinstance(result, dict) else None
                ),
                "formatted_output": formatted,
            }

        except Exception as e:
            return {
                "action": "search",
                "error": str(e),
                "formatted_output": f"Error searching executors: {e}",
            }

    elif flow_stage == "stop":
        # Stage 6: Stop executor
        try:
            result = await client.executors.stop_executor(
                executor_id=request.executor_id,
                keep_position=request.keep_position,
            )

            if result.get("status") == "already_terminated":
                # No-op: the executor was already terminal. Say so — a generic
                # "stopped successfully" would hide the one payload that matters
                # (an orphaned on-chain position needing recovery).
                formatted = f"Executor was ALREADY terminated (stop was a no-op).\n\n"
                formatted += f"Executor ID: {request.executor_id}\n"
                formatted += f"Final close_type: {result.get('close_type')}\n"
                if result.get("orphaned_position"):
                    formatted += (
                        f"\n🚨 ORPHANED POSITION: {result.get('position_address')} is still "
                        "open on-chain with no automated owner. Stopping the executor does not "
                        "close it — it has already terminated. Close it with "
                        'manage_clmm(action="close", position_address=..., pool_address=...), '
                        'then mark it recovered with action="resolve_orphan", '
                        f'executor_id="{request.executor_id}".\n'
                        'Run action="orphaned" to get the dex, pool and network for the call.\n'
                    )
                elif result.get("position_address"):
                    formatted += f"Position address (final state): {result.get('position_address')}\n"
            else:
                formatted = f"Executor stopped successfully!\n\n"
                formatted += f"Executor ID: {request.executor_id}\n"
                formatted += f"Keep Position: {request.keep_position}\n"

            return {
                "action": "stop",
                "executor_id": request.executor_id,
                "keep_position": request.keep_position,
                "result": result,
                "formatted_output": formatted,
            }

        except Exception as e:
            return {
                "action": "stop",
                "error": str(e),
                "formatted_output": f"Error stopping executor {request.executor_id}: {e}",
            }

    elif flow_stage == "orphaned":
        # List terminated executors that may still own an on-chain position
        try:
            resp = await client.executors.session.get(
                f"{client.executors.base_url}/executors/positions/orphaned",
            )
            resp.raise_for_status()
            result = await resp.json()

            orphans = result.get("orphans", [])
            formatted = (
                f"Orphaned position candidates: {result.get('count', len(orphans))}\n\n"
            )
            if not orphans:
                formatted += (
                    "No orphaned positions. All terminated executors closed cleanly."
                )
            else:
                for o in orphans:
                    formatted += (
                        f"- {o.get('executor_id')} ({o.get('executor_type')}, "
                        f"{o.get('trading_pair')} on {o.get('connector_name')}, "
                        f"close_type={o.get('close_type')}, closed_at={o.get('closed_at')})\n"
                    )
                    if o.get("position_address"):
                        formatted += f"    position: {o['position_address']}\n"
                    if o.get("lp_provider") or o.get("pool_address"):
                        formatted += f"    dex: {o.get('lp_provider')}  pool: {o.get('pool_address')}\n"
                    if o.get("needs_onchain_reconciliation"):
                        formatted += (
                            "    position address unknown (API restart) - reconcile against "
                            "on-chain positions (get_portfolio_overview include_lp_positions=True)\n"
                        )
                    elif o.get("lp_provider") and o.get("pool_address"):
                        # Spell the recovery call out: the executor is terminated, so stopping it is
                        # a no-op and the position can only be closed by address.
                        formatted += (
                            '    close with: manage_clmm(action="close", '
                            f"connector=\"{o.get('lp_provider')}\", "
                            f"network=\"{o.get('connector_name')}\", "
                            f"position_address=\"{o.get('position_address')}\", "
                            f"pool_address=\"{o.get('pool_address')}\")\n"
                        )
                formatted += (
                    '\nRecover each by closing the position with manage_clmm(action="close") - '
                    "pool_address is required because LP-executor positions are not in the API "
                    "database. Stopping the executor will NOT close it; it has already terminated. "
                    'Then mark it recovered with action="resolve_orphan", executor_id="...".'
                )

            return {
                "action": "orphaned",
                "result": result,
                "formatted_output": formatted,
            }

        except Exception as e:
            return {
                "action": "orphaned",
                "error": str(e),
                "formatted_output": f"Error listing orphaned positions: {e}",
            }

    elif flow_stage == "resolve_orphan":
        # Mark an orphaned position as recovered (after closing it externally)
        if not request.executor_id:
            return {
                "action": "resolve_orphan",
                "error": "executor_id is required",
                "formatted_output": (
                    "resolve_orphan requires executor_id. "
                    'Run manage_executors(action="orphaned") to list candidates.'
                ),
            }
        try:
            resp = await client.executors.session.post(
                f"{client.executors.base_url}/executors/{request.executor_id}/resolve-orphan",
            )
            resp.raise_for_status()
            result = await resp.json()

            return {
                "action": "resolve_orphan",
                "executor_id": request.executor_id,
                "result": result,
                "formatted_output": (
                    f"Orphaned position for executor {request.executor_id} marked recovered. "
                    "It will no longer appear in orphan listings or warnings."
                ),
            }

        except Exception as e:
            return {
                "action": "resolve_orphan",
                "error": str(e),
                "formatted_output": f"Error resolving orphan for executor {request.executor_id}: {e}",
            }

    elif flow_stage == "get_logs":
        # Get executor logs via direct API call (not yet in client library)
        try:
            params = {"limit": request.limit}
            if request.log_level:
                params["level"] = request.log_level.upper()

            resp = await client.executors.session.get(
                f"{client.executors.base_url}/executors/{request.executor_id}/logs",
                params=params,
            )
            resp.raise_for_status()
            result = await resp.json()

            logs = result.get("logs", [])
            total = result.get("total_count", len(logs))

            formatted = f"Executor Logs: {request.executor_id}\n"
            formatted += f"Total entries: {total}"
            if request.log_level:
                formatted += f" (filtered: {request.log_level.upper()})"
            formatted += f", showing: {len(logs)}\n\n"

            if not logs:
                formatted += "No log entries found. Note: logs are only available for active executors and are cleared on completion."
            else:
                for entry in logs:
                    ts = entry.get("timestamp", "")
                    level = entry.get("level", "")
                    msg = entry.get("message", "")
                    formatted += f"[{ts}] {level}: {msg}\n"
                    exc = entry.get("exc_info")
                    if exc:
                        formatted += f"  Exception: {exc}\n"

            return {
                "action": "get_logs",
                "executor_id": request.executor_id,
                "logs": logs,
                "total_count": total,
                "formatted_output": formatted,
            }

        except Exception as e:
            return {
                "action": "get_logs",
                "error": str(e),
                "formatted_output": f"Error getting logs for executor {request.executor_id}: {e}",
            }

    elif flow_stage == "get_preferences":
        # Stage 8: Get saved preferences (returns raw markdown content)
        raw_content = executor_preferences.get_raw_content()

        formatted = (
            f"Preferences file: {executor_preferences.get_preferences_path()}\n\n"
        )
        formatted += raw_content

        return {
            "action": "get_preferences",
            "executor_type": request.executor_type,
            "raw_content": raw_content,
            "preferences_path": executor_preferences.get_preferences_path(),
            "formatted_output": formatted,
        }

    elif flow_stage == "save_preferences":
        # Stage 9: Save full preferences file content
        executor_preferences.save_content(request.preferences_content)

        formatted = f"Preferences file saved successfully.\n\n"
        formatted += f"Preferences file: {executor_preferences.get_preferences_path()}"

        return {
            "action": "save_preferences",
            "preferences_path": executor_preferences.get_preferences_path(),
            "formatted_output": formatted,
        }

    elif flow_stage == "reset_preferences":
        # Stage 10: Reset preferences to defaults (preserves YAML configs)
        preserved = executor_preferences.reset_to_defaults()
        preserved_count = sum(1 for c in preserved.values() if c)

        formatted = "Preferences documentation updated to latest version.\n\n"
        if preserved_count > 0:
            preserved_names = [k for k, v in preserved.items() if v]
            formatted += (
                f"Preserved {preserved_count} config(s): {', '.join(preserved_names)}\n"
            )
        else:
            formatted += "No existing configs to preserve.\n"
        formatted += (
            f"\nPreferences file: {executor_preferences.get_preferences_path()}"
        )

        return {
            "action": "reset_preferences",
            "preserved_configs": preserved,
            "preserved_count": preserved_count,
            "formatted_output": formatted,
        }

    # Position management stages (merged from manage_executor_positions)

    elif flow_stage == "positions_summary":
        # Get all positions, or specific position if connector_name + trading_pair given
        try:
            if request.connector_name and request.trading_pair:
                # Get specific position detail
                account = request.account_name or "master_account"
                result = await client.executors.get_position_held(
                    connector_name=request.connector_name,
                    trading_pair=request.trading_pair,
                    account_name=account,
                    controller_id=request.controller_id,
                )

                formatted = f"Position Details\n\n"
                formatted += f"Connector: {request.connector_name}\n"
                formatted += f"Trading Pair: {request.trading_pair}\n"
                formatted += f"Account: {account}\n\n"

                if result:
                    positions = [result] if not isinstance(result, list) else result
                    formatted += format_positions_held_table(positions)
                else:
                    formatted += (
                        "No position found for this connector/pair combination."
                    )

                return {
                    "action": "positions_summary",
                    "connector_name": request.connector_name,
                    "trading_pair": request.trading_pair,
                    "account": account,
                    "position": result,
                    "formatted_output": formatted,
                }

            result = await client.executors.get_positions_summary(
                controller_id=request.controller_id,
            )

            positions = (
                result.get("positions", result) if isinstance(result, dict) else result
            )
            if not isinstance(positions, list):
                positions = [positions] if positions else []

            formatted = f"Positions Held Summary\n\n"

            if isinstance(result, dict) and any(
                k in result for k in ["total_positions", "total_value", "by_connector"]
            ):
                formatted += format_positions_summary(result)
                if positions:
                    formatted += "\n\nPositions Detail:\n"
                    formatted += format_positions_held_table(positions)
            else:
                formatted += format_positions_held_table(positions)

            return {
                "action": "positions_summary",
                "positions": positions,
                "summary": (
                    result if isinstance(result, dict) else {"positions": positions}
                ),
                "formatted_output": formatted,
            }

        except Exception as e:
            return {
                "action": "positions_summary",
                "error": str(e),
                "formatted_output": f"Error getting positions: {e}",
            }

    elif flow_stage == "clear_position":
        # Clear a position that was closed manually
        account = request.account_name or "master_account"
        try:
            result = await client.executors.clear_position_held(
                connector_name=request.connector_name,
                trading_pair=request.trading_pair,
                account_name=account,
                controller_id=request.controller_id,
            )

            formatted = f"Position cleared successfully!\n\n"
            formatted += f"Connector: {request.connector_name}\n"
            formatted += f"Trading Pair: {request.trading_pair}\n"
            formatted += f"Account: {account}\n"

            return {
                "action": "clear_position",
                "connector_name": request.connector_name,
                "trading_pair": request.trading_pair,
                "account": account,
                "result": result,
                "formatted_output": formatted,
            }

        except Exception as e:
            return {
                "action": "clear_position",
                "error": str(e),
                "formatted_output": f"Error clearing position: {e}",
            }

    elif flow_stage == "performance_report":
        try:
            result = await client.executors.get_performance_report(
                controller_id=request.controller_id,
            )
            formatted = "Executor Performance Report\n\n"
            if request.controller_id:
                formatted += f"Controller: {request.controller_id}\n\n"
            if isinstance(result, dict):
                for key, value in result.items():
                    formatted += f"{key}: {value}\n"
            else:
                formatted += str(result)
            return {
                "action": "performance_report",
                "result": result,
                "formatted_output": formatted,
            }
        except Exception as e:
            return {
                "action": "performance_report",
                "error": str(e),
                "formatted_output": f"Error getting performance report: {e}",
            }

    else:
        return {
            "action": "unknown",
            "error": f"Unknown flow stage: {flow_stage}",
            "formatted_output": f"Error: Unknown flow stage: {flow_stage}",
        }
