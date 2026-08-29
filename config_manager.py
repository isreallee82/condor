"""
Unified Configuration Manager for Condor Bot.
Manages servers, users, permissions, and settings in a single config.yml file.
"""

import asyncio
import logging
import secrets
import time
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

import yaml
from aiohttp import ClientTimeout

logger = logging.getLogger(__name__)


def _build_base_url(host: str, port: int) -> str:
    """Build the API base URL for a configured server.

    `host` is meant to be a bare hostname ("localhost"), but a full URL pasted
    into the Host field is common enough to handle: prefixing "http://" blindly
    turns "https://example.com" into "http://https://example.com:443", which
    aiohttp reads as host "https" on port 80. A scheme already in `host` wins;
    otherwise port 443 implies TLS.
    """
    host = host.strip().rstrip("/")
    parsed = urlparse(host if "://" in host else f"//{host}")
    scheme = parsed.scheme or ("https" if str(port) == "443" else "http")
    return f"{scheme}://{parsed.hostname or host}:{port}"


class ClientAcquisitionError(ConnectionError):
    """The last attempt to acquire an API client failed and is still cached.

    Subclasses ``ConnectionError`` so every existing ``except`` keeps working,
    but carries a name that asserts nothing about *why*: the cached failure may
    be a refused connect, a timeout that proves nothing, or an HTTP error from a
    server that answered perfectly well. The class name is user-visible -- the
    routine layer renders uncaught exceptions as ``Error: <ClassName>: <msg>``
    straight into the agent's context -- so it must not name a cause the code
    has not established.
    """


# What each class of acquisition failure actually establishes. The fail-fast
# branch quotes these verbatim, and they reach the agent, so each says only what
# the exception itself proves.
#
# "connect" deliberately does NOT name a layer. It is reached from
# ClientConnectionError/OSError, which covers DNS failures (no TCP connect was
# ever attempted), TLS failures (the TCP connect SUCCEEDED and the handshake
# did not), and a server that ACCEPTED the connection and then dropped it --
# ServerDisconnectedError, which is what a restarting API container looks like.
# Naming the TCP connect would assert the opposite of what half of them prove.
_FAILURE_HEADLINE = {
    "connect": (
        "could not be reached over the network -- the connection attempt failed; "
        "whether that was DNS, the TCP connect, TLS, or the server dropping a "
        "connection it had accepted is NOT established"
    ),
    "timeout": (
        "did not respond in time -- whether it is reachable is NOT established "
        "(the connect may have succeeded and the request then hung)"
    ),
    "http": (
        "answered but returned an HTTP error -- it IS reachable; this is not a "
        "connectivity fault"
    ),
    "auth": (
        "answered but rejected the credentials -- it IS reachable; the fault is "
        "authentication, not connectivity"
    ),
    "cancelled": (
        "was not reached before the attempt was cancelled -- our own deadline "
        "expired first, so NOTHING about the server was established"
    ),
    "unknown": (
        "could not be used, for a reason this error does NOT establish -- do "
        "not infer a network, venue or credential fault from it"
    ),
}


class UserRole(str, Enum):
    """User roles in the system"""

    ADMIN = "admin"
    USER = "user"
    PENDING = "pending"
    BLOCKED = "blocked"


class ServerPermission(str, Enum):
    """Permission levels for server access"""

    OWNER = "owner"
    TRADER = "trader"


PERMISSION_HIERARCHY = {
    ServerPermission.TRADER: 0,
    ServerPermission.OWNER: 1,
}


class ConfigManager:
    """
    Unified configuration manager for Condor Bot.
    Handles servers, users, permissions, and chat defaults in a single YAML file.
    Uses singleton pattern - access via ConfigManager.instance()
    """

    VERSION = 1
    MAX_AUDIT_LOG_ENTRIES = 500

    _instance: Optional["ConfigManager"] = None

    def __init__(self, config_path: str = "config.yml"):
        self.config_path = Path(config_path)
        self.audit_log_path = Path("audit_log.yml")
        self._data: dict = {}
        self._audit_log: list = []
        self._clients: Dict[str, Tuple[Any, float]] = (
            {}
        )  # server_name -> (client, connect_time)
        self._client_ttl = 300  # 5 minutes
        self._client_verify_interval = 60  # seconds between liveness checks
        self._client_locks: Dict[str, asyncio.Lock] = (
            {}
        )  # per-server lock for get_client
        # Negative cache for the create path. Client creation happens under the
        # per-server lock, so without this every queued waiter (each TickEngine,
        # each routine run, each dashboard poll) pays its own full-cost connect
        # attempt, serially, against a server already known to be down.
        # Timestamps are time.monotonic(), not time.time(): a backward wall-clock
        # step would make the age negative and keep an entry inside the fail-fast
        # window past its TTL. (_clients above stays on time.time(); the two sets
        # of timestamps are never compared with each other.)
        # Emergency callers bypass this entirely -- see get_client(force=True)
        # and clear_client_failures().
        # The `kind` is decided from the exception TYPE at record time (see
        # _classify_client_failure) so the fail-fast branch can say what the
        # failure actually was instead of asserting "unreachable" for every
        # cause. Auth rejections are deliberately never recorded here.
        self._client_failures: Dict[str, Tuple[float, str, str]] = (
            {}
        )  # server_name -> (failed_at_monotonic, reason, kind)
        self._client_failure_ttl = 20  # seconds to fail fast after a failed connect
        self._load_config()
        self._load_audit_log()

    @classmethod
    def instance(cls, config_path: str = "config.yml") -> "ConfigManager":
        """Get the singleton instance."""
        if cls._instance is None:
            cls._instance = cls(config_path)
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Reset the singleton (for testing)."""
        cls._instance = None

    def _get_admin_from_env(self) -> Optional[int]:
        """Get admin user ID from environment."""
        from utils.config import ADMIN_USER_ID

        return ADMIN_USER_ID

    def _load_config(self):
        """Load configuration from YAML file."""
        if not self.config_path.exists():
            self._init_default_config()
            return

        try:
            with open(self.config_path, "r") as f:
                self._data = yaml.safe_load(f) or {}

            # Ensure all sections exist
            self._data.setdefault("servers", {})
            self._data.setdefault("default_server", None)
            self._data.setdefault("users", {})
            self._data.setdefault("server_access", {})
            self._data.setdefault("chat_defaults", {})
            self._data.setdefault("user_preferences", {})
            # Migrate audit_log from config.yml to separate file (one-time)
            if "audit_log" in self._data:
                self._audit_log = self._data.pop("audit_log")
                self._save_audit_log()
                self._save_config()  # Save config without audit_log

            # Always trust admin_id from env
            admin_id = self._get_admin_from_env()
            if admin_id:
                self._data["admin_id"] = admin_id
                self._ensure_admin_user(admin_id)

            logger.info(f"Loaded config from {self.config_path}")
        except Exception as e:
            logger.error(f"Failed to load config: {e}")
            self._init_default_config()

    def _init_default_config(self):
        """Initialize with default configuration."""
        admin_id = self._get_admin_from_env()
        self._data = {
            "servers": {},
            "default_server": None,
            "admin_id": admin_id,
            "users": {},
            "server_access": {},
            "chat_defaults": {},
            "user_preferences": {},
            "version": self.VERSION,
        }
        self._audit_log = []
        if admin_id:
            self._ensure_admin_user(admin_id)
        self._save_config()
        logger.info(f"Created new config at {self.config_path}")

    def _ensure_admin_user(self, admin_id: int):
        """Ensure admin user exists in users dict."""
        if admin_id not in self._data["users"]:
            self._data["users"][admin_id] = {
                "user_id": admin_id,
                "role": UserRole.ADMIN.value,
                "created_at": time.time(),
                "notes": "Primary admin from ADMIN_USER_ID",
            }
            self._save_config()

    def _save_config(self):
        """Save configuration to YAML file."""
        try:
            data = {
                "servers": self._data.get("servers", {}),
                "default_server": self._data.get("default_server"),
                "admin_id": self._data.get("admin_id"),
                "users": self._data.get("users", {}),
                "server_access": self._data.get("server_access", {}),
                "chat_defaults": self._data.get("chat_defaults", {}),
                "web_jwt_secret": self._data.get("web_jwt_secret"),
                "version": self._data.get("version", self.VERSION),
            }
            with open(self.config_path, "w") as f:
                yaml.dump(
                    data,
                    f,
                    default_flow_style=False,
                    sort_keys=False,
                    allow_unicode=True,
                )
            logger.debug(f"Saved config to {self.config_path}")
        except Exception as e:
            logger.error(f"Failed to save config: {e}")
            raise

    def _load_audit_log(self):
        """Load audit log from separate file."""
        if not self.audit_log_path.exists():
            self._audit_log = []
            return

        try:
            with open(self.audit_log_path, "r") as f:
                data = yaml.safe_load(f) or {}
                self._audit_log = data.get("entries", [])
            logger.debug(f"Loaded {len(self._audit_log)} audit log entries")
        except Exception as e:
            logger.error(f"Failed to load audit log: {e}")
            self._audit_log = []

    def _save_audit_log(self):
        """Save audit log to separate file."""
        try:
            # Trim to max entries
            if len(self._audit_log) > self.MAX_AUDIT_LOG_ENTRIES:
                self._audit_log = self._audit_log[-self.MAX_AUDIT_LOG_ENTRIES :]

            data = {"entries": self._audit_log}
            with open(self.audit_log_path, "w") as f:
                yaml.dump(
                    data,
                    f,
                    default_flow_style=False,
                    sort_keys=False,
                    allow_unicode=True,
                )
            logger.debug(f"Saved {len(self._audit_log)} audit log entries")
        except Exception as e:
            logger.error(f"Failed to save audit log: {e}")

    def reload(self):
        """Reload configuration from file."""
        self._load_config()
        self._load_audit_log()

    @property
    def admin_id(self) -> Optional[int]:
        return self._data.get("admin_id")

    # =========================================================================
    # SERVER MANAGEMENT
    # =========================================================================

    def list_servers(self) -> Dict[str, dict]:
        """List all configured servers."""
        return self._data.get("servers", {}).copy()

    def get_server(self, name: str) -> Optional[dict]:
        """Get a specific server configuration."""
        return self._data.get("servers", {}).get(name)

    def add_server(
        self,
        name: str,
        host: str,
        port: int,
        username: str,
        password: str,
        owner_id: int = None,
    ) -> bool:
        """Add a new server."""
        servers = self._data["servers"]
        if name in servers:
            logger.error(f"Server '{name}' already exists")
            return False

        servers[name] = {
            "host": host,
            "port": port,
            "username": username,
            "password": password,
        }

        # Register ownership
        if owner_id:
            self.register_server_owner(name, owner_id)

        self._save_config()
        logger.info(f"Added server '{name}'")
        return True

    def modify_server(
        self,
        name: str,
        host: str = None,
        port: int = None,
        username: str = None,
        password: str = None,
    ) -> bool:
        """Modify an existing server."""
        servers = self._data["servers"]
        if name not in servers:
            logger.error(f"Server '{name}' not found")
            return False

        # Clear cached client
        if name in self._clients:
            del self._clients[name]
        # ...and any negative-cache entry: a re-pointed or re-credentialed
        # server deserves an immediate retry, not the fail-fast window.
        self._client_failures.pop(name, None)

        if host is not None:
            servers[name]["host"] = host
        if port is not None:
            servers[name]["port"] = port
        if username is not None:
            servers[name]["username"] = username
        if password is not None:
            servers[name]["password"] = password

        self._save_config()
        logger.info(f"Modified server '{name}'")
        return True

    def delete_server(self, name: str, actor_id: int = None) -> bool:
        """Delete a server."""
        servers = self._data["servers"]
        if name not in servers:
            logger.error(f"Server '{name}' not found")
            return False

        # Clear cached client
        if name in self._clients:
            del self._clients[name]
        self._client_failures.pop(name, None)

        del servers[name]

        # Unregister from access control
        if name in self._data["server_access"]:
            del self._data["server_access"][name]

        self._save_config()
        logger.info(f"Deleted server '{name}'")
        return True

    def get_default_server(self) -> Optional[str]:
        """Get the default server name."""
        return self._data.get("default_server")

    def set_default_server(self, name: str) -> bool:
        """Set the default server."""
        if name not in self._data["servers"]:
            logger.error(f"Server '{name}' not found")
            return False

        self._data["default_server"] = name
        self._save_config()
        logger.info(f"Set default server to '{name}'")
        return True

    def get_or_create_web_jwt_secret(self) -> str:
        """Return the web dashboard JWT signing secret, generating one on demand.

        On first use a strong random secret is generated and persisted to
        ``config.yml`` so web sessions survive restarts. The web layer prefers an
        explicit ``WEB_JWT_SECRET`` env var over this value for multi-instance or
        rotation scenarios; this is the zero-config default for everyone else.
        """
        secret = self._data.get("web_jwt_secret")
        if secret:
            return secret
        secret = secrets.token_urlsafe(32)
        self._data["web_jwt_secret"] = secret
        self._save_config()
        logger.info(
            "Generated and persisted a new web dashboard JWT secret in %s",
            self.config_path,
        )
        return secret

    async def get_client(self, name: str = None, force: bool = False):
        """Get or create API client for a server.

        ``force=True`` ignores the negative cache and pays for a real connect
        attempt. It exists for emergency paths -- the agent kill switch in
        condor.agents.shutdown, which closes open positions -- where being
        told "unreachable" by an entry some unrelated tick, routine run or
        dashboard poll left behind seconds ago means the winddown aborts with
        positions still open, having never touched the network. Ordinary
        callers must keep the default so a down server still fails fast
        instead of queueing every caller behind its own connect budget.
        """
        from hummingbot_api_client import HummingbotAPIClient

        if name is None:
            name = self.get_default_server()
            if not name:
                if self._data["servers"]:
                    name = list(self._data["servers"].keys())[0]
                else:
                    raise ValueError("No servers configured")

        if name not in self._data["servers"]:
            raise ValueError(f"Server '{name}' not found")

        # Fast path (no lock): return cached client if recently verified
        if name in self._clients:
            client, last_verified = self._clients[name]
            if time.time() - last_verified < self._client_verify_interval:
                return client

        # Serialize client creation/verification per server to prevent
        # concurrent coroutines from creating duplicate sessions
        if name not in self._client_locks:
            self._client_locks[name] = asyncio.Lock()

        async with self._client_locks[name]:
            return await self._get_or_create_client(
                name, HummingbotAPIClient, force=force
            )

    async def _get_or_create_client(
        self, name: str, HummingbotAPIClient, force: bool = False
    ):
        """Inner client acquisition — must be called under _client_locks[name].

        ``force`` bypasses the negative cache only; see ``get_client``.
        """
        # Re-check under lock (another coroutine may have just created it)
        if name in self._clients:
            client, last_verified = self._clients[name]
            if time.time() - last_verified < self._client_verify_interval:
                # Fast path: recently verified
                return client
            elif time.time() - last_verified < self._client_ttl:
                # Needs liveness check
                try:
                    await asyncio.wait_for(client.accounts.list_accounts(), timeout=5)
                    self._clients[name] = (client, time.time())
                    return client
                except Exception:
                    logger.warning(
                        f"Stale connection to '{name}' detected, reconnecting"
                    )
                    try:
                        await client.close()
                    except Exception:
                        pass
                    del self._clients[name]
            else:
                try:
                    await client.close()
                except Exception:
                    pass
                del self._clients[name]

        # Fail fast while a recent connect failure is still fresh. Creation runs
        # under _client_locks[name], so refusing here keeps a queue of waiters
        # from each re-paying the connect budget one after another.
        failure = self._client_failures.get(name)
        if failure:
            failed_at, reason, kind = failure
            age = time.monotonic() - failed_at
            if force:
                # An emergency caller is never starved by an entry somebody
                # else's poll left behind: drop it and connect for real.
                logger.warning(
                    f"Forcing a connect to '{name}' despite a failure "
                    f"{age:.0f}s ago [{kind}]: {reason}"
                )
                del self._client_failures[name]
            elif age < self._client_failure_ttl:
                # Report the cached failure for what it was. The old wording
                # said "unreachable (connect failed ...)" for every cause,
                # including a server that answered; this text propagates
                # uncaught to the agent, so a cause nobody measured must never
                # be asserted in it. It also states plainly that NO attempt was
                # made just now -- the caller is seeing a cached verdict, not a
                # fresh measurement.
                headline = _FAILURE_HEADLINE.get(
                    kind, _FAILURE_HEADLINE["unknown"]
                )
                raise ClientAcquisitionError(
                    f"Server '{name}' {headline}. "
                    f"Last acquisition attempt failed {age:.0f}s ago and is still "
                    f"inside the {self._client_failure_ttl}s fail-fast window, so "
                    f"no new attempt was made just now: {reason}"
                )
            else:
                del self._client_failures[name]

        # Create new client
        server = self._data["servers"][name]
        base_url = _build_base_url(server["host"], server["port"])
        client = HummingbotAPIClient(
            base_url=base_url,
            username=server["username"],
            password=server["password"],
            timeout=ClientTimeout(total=60, connect=10),
        )

        try:
            # init() only builds the aiohttp session and the router objects --
            # it issues no request, so it needs no cap. The liveness call does,
            # and it is the one await that can hold the per-server lock: cap it
            # rather than letting the client's 60s *total* timeout stall every
            # other caller queued on this server. 12s, deliberately just past
            # the client's own connect=10, so a stalled TCP/TLS connect trips
            # the connector first and raises its informative ClientConnectorError
            # ("Cannot connect to host ...") instead of this bare TimeoutError.
            # Worst case this function holds the lock for ~17s: 5s for the
            # stale-connection liveness check above, then 12s here.
            await client.init()
            await asyncio.wait_for(client.accounts.list_accounts(), timeout=12)
            self._clients[name] = (client, time.time())
            self._client_failures.pop(name, None)
            logger.info(f"Connected to server '{name}' at {base_url}")
            return client
        except BaseException as e:
            # BaseException, not Exception: TickEngine._tick acquires the client
            # inside `async with asyncio.timeout(_SETUP_BUDGET_SEC)`, so a budget
            # expiry lands here as asyncio.CancelledError, which is NOT an
            # Exception. Catching only Exception skipped this whole handler --
            # the aiohttp session leaked AND nothing was recorded, so the next
            # caller paid the full budget over again: precisely the queue-of-
            # waiters case the negative cache exists to prevent. Everything
            # below is re-raised unchanged, KeyboardInterrupt included.
            #
            # Record and log BEFORE closing: a close() that raises would
            # otherwise replace `e` and skip both the negative-cache write and
            # the log line, leaving the failure invisible and uncached.
            kind, reason = self._classify_client_failure(e)
            if kind == "auth":
                # An auth rejection does not belong in the negative cache. The
                # cache exists so a queue of waiters does not each pay a full
                # connect budget against a server that will not answer; a 401
                # comes back immediately and costs nothing to re-learn. Caching
                # it would tell every caller for the next 20s something untrue
                # about the server, and would keep refusing for 20s after the
                # credentials are fixed. The real ClientResponseError is
                # re-raised instead, so callers see the actual 401.
                logger.error(
                    f"Server '{name}' rejected our credentials: {reason} "
                    f"(not negatively cached -- the server is reachable)"
                )
            else:
                self._client_failures[name] = (time.monotonic(), reason, kind)
                logger.error(
                    f"Failed to acquire a client for '{name}' [{kind}]: {reason}"
                )
            try:
                await client.close()
            except BaseException:
                # Swallow anything close() raises (including a CancelledError
                # re-delivered while unwinding) so it cannot mask `e`, which is
                # re-raised immediately below.
                logger.debug(f"Error closing client for '{name}'", exc_info=True)
            raise

    @staticmethod
    def _classify_client_failure(e: BaseException) -> Tuple[str, str]:
        """Classify a failed client acquisition, returning ``(kind, reason)``.

        Classification is by exception TYPE, never by message text: a hung
        request raises a bare ``asyncio.TimeoutError`` whose ``str()`` is
        EMPTY, so any taxonomy built on ``str(e)`` silently falls through to
        its default for exactly the case it most needs to name.

        Kinds (see ``_FAILURE_HEADLINE`` for what each one is allowed to claim):
          ``connect``  the TCP/TLS connect itself failed -- the server really is
                       unreachable at that address.
          ``auth``     the server answered and rejected the credentials (401/403).
                       It IS reachable; the fault is credentials.
          ``http``     the server answered with some other error status. It IS
                       reachable; the fault is server-side or in the request.
          ``timeout``  nothing came back in time. Reachability is NOT
                       established: the connect may have succeeded and the
                       request then hung.
          ``cancelled`` our own caller's deadline expired mid-attempt
                       (TickEngine's setup budget). Says nothing about the
                       server at all.
          ``unknown``  anything else. The cause is NOT established -- say so
                       rather than guessing one.

        ``reason`` is display text only; nothing branches on it. The class name
        is always included because a bare ``asyncio.TimeoutError`` stringifies
        to "", which would leave the log line and the cached reason ending at a
        colon.
        """
        import aiohttp

        reason = f"{type(e).__name__}({e})"
        if isinstance(e, aiohttp.ClientResponseError):
            # The server answered. Whatever went wrong, it was not the network.
            return ("auth" if e.status in (401, 403) else "http"), reason
        # TimeoutError must be tested before OSError: since Python 3.3 the
        # builtin TimeoutError IS an OSError subclass (and since 3.11
        # asyncio.TimeoutError is an alias of it), and aiohttp's
        # ServerTimeoutError is both a TimeoutError and a ClientConnectionError.
        # Ordering it first keeps "nothing came back" from being reported as a
        # proven connect failure.
        if isinstance(e, asyncio.CancelledError):
            # Our caller gave up; the server never got a verdict. Kept distinct
            # from "timeout" so the cached text cannot imply the server was slow
            # when it may never have been asked.
            return "cancelled", reason
        if isinstance(e, asyncio.TimeoutError):
            return "timeout", reason
        if isinstance(e, (aiohttp.ClientConnectionError, OSError)):
            return "connect", reason
        return "unknown", reason

    def clear_client_failures(self, name: Optional[str] = None) -> None:
        """Drop cached acquisition failures so the next call connects for real.

        ``name=None`` clears every server. This is the emergency-path escape
        hatch for a caller that cannot name its server in advance: the
        ``get_bots_client`` fallback in
        ``condor.agents.engine.TickEngine._get_client`` resolves the server
        itself and calls ``get_client()`` without ``force``, so without this a
        winddown could still be refused in 0.000s by an entry an unrelated tick
        or dashboard poll left behind. Ordinary callers must NOT use it -- the
        cache is what keeps a queue of waiters from each paying its own connect
        budget against a server already known to be down.
        """
        if name is None:
            self._client_failures.clear()
        else:
            self._client_failures.pop(name, None)

    async def get_client_for_chat(
        self, chat_id: int, user_id: int = None, preferred_server: str = None
    ):
        """Get the API client for a user's preferred or first accessible server.

        Priority:
        1. preferred_server (from user preferences/context) if accessible
        2. chat_defaults[chat_id] if accessible
        3. First accessible server for the user
        4. If no user_id, use chat default or any available server
        """
        if user_id:
            accessible = self.get_accessible_servers(user_id)
            if not accessible:
                raise ValueError(
                    "No servers available. Ask the admin to share a server with you."
                )

            # 1. User's preferred server if accessible
            if preferred_server and preferred_server in accessible:
                return await self.get_client(preferred_server)

            # 2. Chat's default server if accessible
            chat_default = self._data.get("chat_defaults", {}).get(chat_id)
            if chat_default and chat_default in accessible:
                return await self.get_client(chat_default)

            # 3. First accessible server
            return await self.get_client(accessible[0])

        # No user_id - use chat default with proper fallbacks
        server_name = self.get_chat_default_server(chat_id)
        if not server_name:
            raise ValueError("No servers configured")
        return await self.get_client(server_name)

    async def check_server_status(self, name: str) -> dict:
        """Check if a server is online."""
        from hummingbot_api_client import HummingbotAPIClient

        if name not in self._data["servers"]:
            return {"status": "error", "message": "Server not found"}

        server = self._data["servers"][name]
        base_url = _build_base_url(server["host"], server["port"])

        client = HummingbotAPIClient(
            base_url=base_url,
            username=server["username"],
            password=server["password"],
            timeout=ClientTimeout(total=3, connect=2),
        )

        try:
            await client.init()
            await client.accounts.list_accounts()
            # A server independently verified as reachable must not keep
            # fast-failing get_client() for the rest of the negative-cache
            # window: the dashboard would read "online" while every acquisition
            # raised ConnectionError. This is also the only operator-facing way
            # to clear the window short of modify_server.
            self._client_failures.pop(name, None)
            return {"status": "online", "message": "Connected and authenticated"}
        except Exception as e:
            # The reverse is deliberately not done: this probe runs on a much
            # tighter 3s/2s budget than get_client's, so a failure here does not
            # prove get_client would fail and must not poison its cache.
            #
            # Classified by exception type, sharing get_client's classifier. The
            # previous substring taxonomy over str(e) mis-handled its most
            # important case: this probe uses ClientTimeout(total=3, connect=2),
            # and a hung server raises a bare asyncio.TimeoutError whose str()
            # is EMPTY -- matching none of "401"/"timeout"/"connect" and
            # rendering to the operator as "Error: " with nothing after it.
            kind, reason = self._classify_client_failure(e)
            if kind == "auth":
                return {"status": "auth_error", "message": "Invalid credentials"}
            elif kind == "connect":
                return {"status": "offline", "message": "Cannot reach server"}
            elif kind == "timeout":
                # "offline" is the closest of the four statuses this dashboard
                # understands, but the message must not claim more than a
                # timeout proves.
                return {
                    "status": "offline",
                    "message": "No response within 3s (unreachable or too slow)",
                }
            elif kind == "http":
                return {
                    "status": "error",
                    "message": f"Server answered with an error: {reason[:80]}",
                }
            else:
                return {"status": "error", "message": f"Error: {reason[:80]}"}
        finally:
            try:
                await client.close()
            except Exception:
                pass

    async def close_all_clients(self):
        """Close all cached client connections."""
        for name, (client, _) in list(self._clients.items()):
            try:
                await client.close()
                logger.info(f"Closed connection to '{name}'")
            except Exception as e:
                logger.error(f"Error closing client '{name}': {e}")
        self._clients.clear()
        # Drop the negative cache too. It is keyed on wall-independent monotonic
        # time and survives this teardown otherwise, so a shutdown-then-reuse of
        # the same ConfigManager instance would carry stale poison into the new
        # life of the process for up to _client_failure_ttl seconds.
        self._client_failures.clear()

    # =========================================================================
    # USER MANAGEMENT
    # =========================================================================

    def get_user(self, user_id: int) -> Optional[dict]:
        """Get user record."""
        return self._data.get("users", {}).get(user_id)

    def get_user_role(self, user_id: int) -> Optional[UserRole]:
        """Get user's role."""
        user = self.get_user(user_id)
        if user:
            try:
                return UserRole(user["role"])
            except ValueError:
                return None
        return None

    def is_admin(self, user_id: int) -> bool:
        return self.get_user_role(user_id) == UserRole.ADMIN

    def is_approved(self, user_id: int) -> bool:
        role = self.get_user_role(user_id)
        return role in (UserRole.ADMIN, UserRole.USER)

    def register_pending(self, user_id: int, username: str = None) -> bool:
        """Register a new pending user."""
        users = self._data["users"]
        if user_id in users:
            return False

        users[user_id] = {
            "user_id": user_id,
            "username": username,
            "role": UserRole.PENDING.value,
            "created_at": time.time(),
        }
        self._audit("user_registered", "user", str(user_id), user_id)
        self._save_config()
        logger.info(f"Registered pending user {user_id}")
        return True

    def approve_user(self, user_id: int, admin_id: int) -> bool:
        """Approve a pending user."""
        users = self._data["users"]
        if user_id not in users:
            return False
        if users[user_id]["role"] == UserRole.BLOCKED.value:
            return False

        users[user_id]["role"] = UserRole.USER.value
        users[user_id]["approved_by"] = admin_id
        users[user_id]["approved_at"] = time.time()

        self._audit("user_approved", "user", str(user_id), admin_id)
        self._save_config()
        logger.info(f"User {user_id} approved by {admin_id}")
        return True

    def reject_user(self, user_id: int, admin_id: int) -> bool:
        """Reject a pending user."""
        users = self._data["users"]
        if user_id not in users or users[user_id]["role"] != UserRole.PENDING.value:
            return False

        del users[user_id]
        self._audit("user_rejected", "user", str(user_id), admin_id)
        self._save_config()
        return True

    def block_user(self, user_id: int, admin_id: int) -> bool:
        """Block a user."""
        users = self._data["users"]
        if user_id not in users or user_id == admin_id:
            return False
        if users[user_id]["role"] == UserRole.ADMIN.value:
            return False

        users[user_id]["role"] = UserRole.BLOCKED.value
        self._audit("user_blocked", "user", str(user_id), admin_id)
        self._save_config()
        return True

    def unblock_user(self, user_id: int, admin_id: int) -> bool:
        """Unblock a user (sets to pending)."""
        users = self._data["users"]
        if user_id not in users or users[user_id]["role"] != UserRole.BLOCKED.value:
            return False

        users[user_id]["role"] = UserRole.PENDING.value
        self._audit("user_unblocked", "user", str(user_id), admin_id)
        self._save_config()
        return True

    def get_pending_users(self) -> list:
        return [
            u
            for u in self._data.get("users", {}).values()
            if u.get("role") == UserRole.PENDING.value
        ]

    def get_all_users(self) -> list:
        return list(self._data.get("users", {}).values())

    # =========================================================================
    # USER PREFERENCES (persisted in config.yml, shared across TG + Web)
    # =========================================================================

    def get_user_preferences(self, user_id: int) -> dict:
        """Get all preferences for a user. Returns a copy."""
        prefs = self._data.setdefault("user_preferences", {})
        return dict(prefs.get(user_id, {}))

    def get_user_preference(self, user_id: int, key: str, default=None):
        """Get a single preference value."""
        prefs = self._data.get("user_preferences", {}).get(user_id, {})
        return prefs.get(key, default)

    def set_user_preference(self, user_id: int, key: str, value) -> None:
        """Set a single preference value and persist."""
        prefs = self._data.setdefault("user_preferences", {})
        if user_id not in prefs:
            prefs[user_id] = {}
        prefs[user_id][key] = value
        self._save_config()

    def set_user_preferences(self, user_id: int, updates: dict) -> None:
        """Merge multiple preference values and persist."""
        prefs = self._data.setdefault("user_preferences", {})
        if user_id not in prefs:
            prefs[user_id] = {}
        prefs[user_id].update(updates)
        self._save_config()

    def delete_user_preference(self, user_id: int, key: str) -> bool:
        """Delete a preference key. Returns True if it existed."""
        prefs = self._data.setdefault("user_preferences", {})
        user_prefs = prefs.get(user_id)
        if user_prefs and key in user_prefs:
            del user_prefs[key]
            self._save_config()
            return True
        return False

    # =========================================================================
    # SERVER ACCESS CONTROL
    # =========================================================================

    def register_server_owner(self, server_name: str, owner_id: int) -> bool:
        """Register server ownership."""
        access = self._data["server_access"]
        if server_name in access:
            return False

        access[server_name] = {
            "owner_id": owner_id,
            "created_at": time.time(),
            "shared_with": {},
        }
        self._audit("server_registered", "server", server_name, owner_id)
        self._save_config()
        return True

    def ensure_server_registered(
        self, server_name: str, default_owner_id: int = None
    ) -> bool:
        """Ensure server is registered in access control."""
        if server_name in self._data["server_access"]:
            return True

        owner_id = default_owner_id or self.admin_id
        if owner_id:
            self._data["server_access"][server_name] = {
                "owner_id": owner_id,
                "created_at": time.time(),
                "shared_with": {},
            }
            self._save_config()
            return True
        return False

    def get_server_owner(self, server_name: str) -> Optional[int]:
        access = self._data.get("server_access", {}).get(server_name)
        return access.get("owner_id") if access else None

    def get_server_permission(
        self, user_id: int, server_name: str
    ) -> Optional[ServerPermission]:
        """Get user's permission level for a server."""
        if self.is_admin(user_id):
            return ServerPermission.OWNER

        access = self._data.get("server_access", {}).get(server_name)
        if not access:
            return None

        if access.get("owner_id") == user_id:
            return ServerPermission.OWNER

        perm_str = access.get("shared_with", {}).get(user_id)
        if perm_str:
            try:
                return ServerPermission(perm_str)
            except ValueError:
                return None
        return None

    def has_server_access(
        self,
        user_id: int,
        server_name: str,
        min_permission: ServerPermission = ServerPermission.TRADER,
    ) -> bool:
        perm = self.get_server_permission(user_id, server_name)
        if perm is None:
            return False
        return PERMISSION_HIERARCHY.get(perm, 0) >= PERMISSION_HIERARCHY.get(
            min_permission, 0
        )

    def share_server(
        self,
        server_name: str,
        owner_id: int,
        target_user_id: int,
        permission: ServerPermission,
    ) -> bool:
        """Share a server with another user."""
        access = self._data.get("server_access", {}).get(server_name)
        if not access:
            return False
        if access.get("owner_id") != owner_id and not self.is_admin(owner_id):
            return False
        if target_user_id == access.get("owner_id"):
            return False
        if not self.is_approved(target_user_id):
            return False

        access.setdefault("shared_with", {})[target_user_id] = permission.value
        self._audit(
            "server_shared",
            "server",
            server_name,
            owner_id,
            {"target_user": target_user_id, "permission": permission.value},
        )
        self._save_config()
        return True

    def revoke_server_access(
        self, server_name: str, owner_id: int, target_user_id: int
    ) -> bool:
        """Revoke a user's access to a server."""
        access = self._data.get("server_access", {}).get(server_name)
        if not access:
            return False
        if access.get("owner_id") != owner_id and not self.is_admin(owner_id):
            return False

        shared = access.get("shared_with", {})
        if target_user_id not in shared:
            return False

        del shared[target_user_id]
        self._audit(
            "server_access_revoked",
            "server",
            server_name,
            owner_id,
            {"target_user": target_user_id},
        )
        self._save_config()
        return True

    def get_server_shared_users(self, server_name: str) -> list:
        """Get list of users a server is shared with."""
        access = self._data.get("server_access", {}).get(server_name)
        if not access:
            return []

        result = []
        for user_id, perm_str in access.get("shared_with", {}).items():
            try:
                result.append((user_id, ServerPermission(perm_str)))
            except ValueError:
                pass
        return result

    def get_accessible_servers(self, user_id: int) -> list:
        """Get all servers a user can access."""
        if self.is_admin(user_id):
            return list(self._data.get("server_access", {}).keys())

        accessible = []
        for server_name, access in self._data.get("server_access", {}).items():
            if access.get("owner_id") == user_id:
                accessible.append(server_name)
            elif user_id in access.get("shared_with", {}):
                accessible.append(server_name)
        return accessible

    def get_owned_servers(self, user_id: int) -> list:
        return [
            s
            for s, a in self._data.get("server_access", {}).items()
            if a.get("owner_id") == user_id
        ]

    def get_shared_servers(self, user_id: int) -> list:
        """Get servers shared with user (not owned)."""
        result = []
        for server_name, access in self._data.get("server_access", {}).items():
            if access.get("owner_id") == user_id:
                continue
            perm_str = access.get("shared_with", {}).get(user_id)
            if perm_str:
                try:
                    result.append((server_name, ServerPermission(perm_str)))
                except ValueError:
                    pass
        return result

    def list_accessible_servers(self, user_id: int) -> Dict[str, dict]:
        """List servers accessible by a user with their configs."""
        if self.is_admin(user_id):
            # Auto-register unregistered servers for admin
            for name in self._data["servers"]:
                self.ensure_server_registered(name, self.admin_id)
            return self._data["servers"].copy()

        accessible = {}
        for name in self.get_accessible_servers(user_id):
            if name in self._data["servers"]:
                accessible[name] = self._data["servers"][name]
        return accessible

    # =========================================================================
    # CHAT DEFAULTS
    # =========================================================================

    def get_chat_default_server(self, chat_id: int) -> Optional[str]:
        """Get the default server for a chat."""
        server = self._data.get("chat_defaults", {}).get(chat_id)
        if server and server in self._data["servers"]:
            return server
        # Fallback to global default
        default = self.get_default_server()
        if default and default in self._data["servers"]:
            return default
        # Last resort: first server
        if self._data["servers"]:
            return list(self._data["servers"].keys())[0]
        return None

    def set_chat_default_server(self, chat_id: int, server_name: str) -> bool:
        """Set the default server for a chat."""
        if server_name not in self._data["servers"]:
            return False
        self._data.setdefault("chat_defaults", {})[chat_id] = server_name
        self._save_config()
        return True

    def clear_chat_default_server(self, chat_id: int) -> bool:
        """Clear the default server for a chat."""
        defaults = self._data.get("chat_defaults", {})
        if chat_id in defaults:
            del defaults[chat_id]
            self._save_config()
            return True
        return False

    def get_chat_server_info(self, chat_id: int) -> dict:
        """Get server info for a chat."""
        per_chat = self._data.get("chat_defaults", {}).get(chat_id)
        if per_chat and per_chat in self._data["servers"]:
            return {
                "server": per_chat,
                "is_per_chat": True,
                "global_default": self.get_default_server(),
            }
        return {
            "server": self.get_default_server(),
            "is_per_chat": False,
            "global_default": self.get_default_server(),
        }

    # =========================================================================
    # AUDIT LOG
    # =========================================================================

    def _audit(
        self,
        action: str,
        target_type: str,
        target_id: str,
        actor_id: int,
        details: dict = None,
    ):
        self._audit_log.append(
            {
                "timestamp": time.time(),
                "actor_id": actor_id,
                "action": action,
                "target_type": target_type,
                "target_id": target_id,
                "details": details,
            }
        )
        self._save_audit_log()

    def get_audit_log(self, limit: int = 50) -> list:
        return list(reversed(self._audit_log))[:limit]


# Convenience functions
def get_config_manager() -> ConfigManager:
    """Get the ConfigManager singleton instance."""
    return ConfigManager.instance()


def get_effective_server(chat_id: int, user_data: dict = None) -> str | None:
    """Get the effective default server for a chat, checking both user_data and config.yml.

    Priority:
    1. user_data active_server (from pickle, fast in-memory)
    2. chat_defaults from config.yml (persistent across hard kills)
    3. None if nothing configured

    Args:
        chat_id: The chat ID
        user_data: Optional user_data dict from context

    Returns:
        Server name or None
    """
    from handlers.config.user_preferences import get_active_server

    # First check user_data (pickle - might be lost on hard kill)
    if user_data:
        active = get_active_server(user_data)
        if active:
            return active

    # Fall back to chat_defaults in config.yml (always persisted)
    cm = get_config_manager()
    chat_default = cm._data.get("chat_defaults", {}).get(chat_id)
    if chat_default and chat_default in cm._data.get("servers", {}):
        # Also sync back to user_data for fast future access
        if user_data is not None:
            from handlers.config.user_preferences import set_active_server

            set_active_server(user_data, chat_default)
        return chat_default

    return None


async def get_client(chat_id: int, user_id: int = None, context=None):
    """Get the API client for the user's preferred server."""
    preferred_server = None
    if context is not None:
        # Handle both normal context and job context (where user_data may be None)
        user_data = context.user_data
        if user_data is None:
            user_data = getattr(context, "_user_data", None)

        if user_id is None and user_data is not None:
            user_id = user_data.get("_user_id")
        preferred_server = get_effective_server(chat_id, user_data)

    return await get_config_manager().get_client_for_chat(
        chat_id, user_id, preferred_server
    )
