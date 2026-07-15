# Copyright 2026-2026 Canonical Limited. All rights reserved.
"""Functions for managing and interacting with the workload.

The intention is that this module could be used outside the context of a charm.
"""

import logging
import os
from pathlib import Path
from typing import Any

from charmlibs import snap

logger = logging.getLogger(__name__)

TASK_HANDLER_SNAP_NAME = "landscape-task-handler"
DEFAULT_SNAP_CHANNEL = "latest/edge"

# The snap runs two long-lived services that must be restarted after a config change.
TASK_HANDLER_SERVICES = ("server", "worker")

# Prefixes for the four database sections the snap reads from its config.
# See landscape-task-handler-charm-plan.md for the mapping of relation data to
# these snap configuration keys.
_STORES_DB_PREFIXES = ("main", "account", "resource")
_TASK_DB_PREFIX = "task-handler"

# mTLS / gRPC. The snap reads its server certificate material from a directory
# that defaults to $SNAP_COMMON/certs/active. In a Juju deployment the charm
# writes the certs supplied by a tls-certificates provider into that directory.
SNAP_COMMON = Path(f"/var/snap/{TASK_HANDLER_SNAP_NAME}/common")
CERTS_ACTIVE_DIR = SNAP_COMMON / "certs" / "active"
CA_CERT_FILE = "ca.crt"
SERVER_CERT_FILE = "server.crt"
SERVER_KEY_FILE = "server.key"
DEFAULT_GRPC_PORT = "50051"

SENSITIVE_CONFIG_FIELDS = frozenset({"password", "secret"})


def install(channel: str = DEFAULT_SNAP_CHANNEL) -> None:
    """Install the landscape-task-handler snap from the given channel."""
    _install_snap(channel=channel)


def refresh(channel: str = DEFAULT_SNAP_CHANNEL) -> None:
    """Refresh the landscape-task-handler snap to the given channel.

    Called on charm upgrade and on channel configuration changes so that a snap
    refresh accompanies the charm refresh.
    """
    _install_snap(channel=channel, refresh=True)


def start() -> None:
    """Start the landscape-task-handler snap services."""
    task_handler_snap = snap.SnapCache()[TASK_HANDLER_SNAP_NAME]
    if not task_handler_snap.present:
        raise snap.SnapNotFoundError(TASK_HANDLER_SNAP_NAME)
    task_handler_snap.start(enable=True)


def restart() -> None:
    """Restart the landscape-task-handler snap services."""
    task_handler_snap = snap.SnapCache()[TASK_HANDLER_SNAP_NAME]
    if not task_handler_snap.present:
        raise snap.SnapNotFoundError(TASK_HANDLER_SNAP_NAME)
    task_handler_snap.restart()


def _install_snap(channel: str, refresh: bool = False) -> None:
    """Install (or refresh) the landscape-task-handler snap to the given channel."""
    try:
        task_handler_snap = snap.SnapCache()[TASK_HANDLER_SNAP_NAME]

        if not task_handler_snap.present or refresh:
            task_handler_snap.ensure(snap.SnapState.Latest, channel=channel)
    except (snap.SnapError, snap.SnapNotFoundError) as e:
        logger.error(
            "An exception occurred when installing %s. Reason: %s",
            TASK_HANDLER_SNAP_NAME,
            str(e),
        )
        raise


def configure_task_db(
    host: str, port: str, user: str, password: str, database: str, ssl: str = "disable"
) -> None:
    """Set the task-handler's own database connection parameters in the snap.

    This is the database that owns the ``deletion_task`` queue table. The snap
    auto-creates the table on startup; the database and login role must already
    exist.
    """
    task_handler_snap = snap.SnapCache()[TASK_HANDLER_SNAP_NAME]
    config = _database_section(_TASK_DB_PREFIX, host, port, user, password, database, ssl)
    if _set_snap_config_if_changed(task_handler_snap, config):
        restart()


def configure_stores(
    host: str,
    port: str,
    user: str,
    password: str,
    main: str,
    account: str,
    resource: str,
    ssl: str = "disable",
    ssl_root_cert: str | None = None,
    ssl_cert: str | None = None,
    ssl_key: str | None = None,
) -> None:
    """Fan out the shared Landscape stores block into the snap DB sections.

    The relation carries a single stores block; main, account and resource share
    host, port, user, password and ssl settings and differ only by database name.
    """
    task_handler_snap = snap.SnapCache()[TASK_HANDLER_SNAP_NAME]

    names = {"main": main, "account": account, "resource": resource}
    config: dict[str, str] = {}
    for prefix in _STORES_DB_PREFIXES:
        config.update(
            _database_section(
                prefix,
                host,
                port,
                user,
                password,
                names[prefix],
                ssl,
                ssl_root_cert=ssl_root_cert,
                ssl_cert=ssl_cert,
                ssl_key=ssl_key,
            )
        )

    if _set_snap_config_if_changed(task_handler_snap, config):
        restart()


def _database_section(
    prefix: str,
    host: str,
    port: str,
    user: str,
    password: str,
    name: str,
    ssl: str,
    ssl_root_cert: str | None = None,
    ssl_cert: str | None = None,
    ssl_key: str | None = None,
) -> dict[str, str]:
    """Build the snap config keys for a single ``landscape.database.<prefix>`` section."""
    base = f"landscape.database.{prefix}"
    section = {
        f"{base}.host": host,
        f"{base}.port": port,
        f"{base}.name": name,
        f"{base}.user": user,
        f"{base}.password": password,
        f"{base}.ssl": ssl,
    }
    if ssl_root_cert is not None:
        section[f"{base}.ssl-root-cert"] = ssl_root_cert
    if ssl_cert is not None:
        section[f"{base}.ssl-cert"] = ssl_cert
    if ssl_key is not None:
        section[f"{base}.ssl-key"] = ssl_key
    return section


def write_server_certificates(ca: str, certificate: str, private_key: str) -> None:
    """Write the gRPC server certificate material into the snap's active certs dir.

    The snap reads these files fresh on every TLS handshake, so writing new
    files here rotates the certificate without a restart. Writes are atomic
    (write to a temp file, then ``os.replace``) so a concurrent handshake never
    observes a partial file.
    """
    CERTS_ACTIVE_DIR.mkdir(parents=True, exist_ok=True)
    _atomic_write(CERTS_ACTIVE_DIR / CA_CERT_FILE, ca, 0o644)
    _atomic_write(CERTS_ACTIVE_DIR / SERVER_CERT_FILE, certificate, 0o644)
    _atomic_write(CERTS_ACTIVE_DIR / SERVER_KEY_FILE, private_key, 0o600)


def configure_grpc(host: str, port: str = DEFAULT_GRPC_PORT, certs_dir: str | None = None) -> None:
    """Point the snap at its gRPC listen host and certificate directory.

    ``host`` is required by the snap (it has no default) and must be routable by
    the outbox client. This only writes snap config when a value actually
    changes, so cert rotation (which re-writes files but keeps the same host and
    directory) does not trigger a restart.
    """
    task_handler_snap = snap.SnapCache()[TASK_HANDLER_SNAP_NAME]
    config = {
        "landscape.task-handler.host": host,
        "landscape.task-handler.grpc-port": port,
        "landscape.task-handler.grpc-certs-dir": certs_dir or str(CERTS_ACTIVE_DIR),
    }
    if _set_snap_config_if_changed(task_handler_snap, config):
        restart()


def _atomic_write(path: Path, content: str, mode: int) -> None:
    """Atomically write ``content`` to ``path`` with the given permission bits."""
    tmp_path = path.with_name(f".{path.name}.tmp")
    tmp_path.write_text(content)
    os.chmod(tmp_path, mode)
    os.replace(tmp_path, path)


def get_version() -> str | None:
    """Get the running version (snap revision) of the workload."""
    try:
        task_handler_snap = snap.SnapCache()[TASK_HANDLER_SNAP_NAME]
    except (snap.SnapError, snap.SnapNotFoundError) as e:
        logger.warning("Unable to query %s snap version: %s", TASK_HANDLER_SNAP_NAME, e)
        return None

    return str(task_handler_snap.revision) if task_handler_snap.present else None


def get_version_info() -> dict[str, str | bool | None]:
    """Get landscape-task-handler snap version information."""
    task_handler_snap = snap.SnapCache()[TASK_HANDLER_SNAP_NAME]
    return {
        "installed": task_handler_snap.present,
        "revision": str(task_handler_snap.revision) if task_handler_snap.present else None,
        "version": task_handler_snap.version if task_handler_snap.present else None,
        "channel": task_handler_snap.channel if task_handler_snap.present else None,
    }


def get_config() -> dict[str, Any]:
    """Get redacted landscape-task-handler snap configuration."""
    task_handler_snap = snap.SnapCache()[TASK_HANDLER_SNAP_NAME]
    if not task_handler_snap.present:
        return {}

    config = task_handler_snap.get(None, typed=True)
    return _redact_config(config)


def check_health() -> dict[str, bool | str]:
    """Check whether the snap is installed and its services are active."""
    task_handler_snap = snap.SnapCache()[TASK_HANDLER_SNAP_NAME]
    installed = task_handler_snap.present
    services = task_handler_snap.services if installed else {}

    inactive = [
        name
        for name in TASK_HANDLER_SERVICES
        if not (services.get(name) and services[name]["active"])
    ]
    healthy = installed and not inactive

    if not installed:
        message = f"{TASK_HANDLER_SNAP_NAME} snap is not installed"
    elif inactive:
        message = f"{TASK_HANDLER_SNAP_NAME} snap services are not active: {', '.join(inactive)}"
    else:
        message = f"{TASK_HANDLER_SNAP_NAME} snap services are active"

    return {"installed": installed, "healthy": healthy, "message": message}


def _redact_config(config: dict[str, Any]) -> dict[str, Any]:
    """Redact sensitive values from snap configuration."""
    redacted: dict[str, Any] = {}
    for key, value in config.items():
        if key in SENSITIVE_CONFIG_FIELDS:
            redacted[key] = "<redacted>"
        elif isinstance(value, dict):
            redacted[key] = _redact_config(value)
        else:
            redacted[key] = value
    return redacted


def _set_snap_config_if_changed(task_handler_snap: snap.Snap, config: dict[str, str]) -> bool:
    """Set snap config keys that differ from their desired value.

    Returns True if any keys were changed, so the caller can decide whether a
    restart is required.

    The current configuration is fetched in a single ``snap get`` call. A
    per-key ``snap get`` raises ``SnapError`` for keys that are not yet set (as
    on first configuration), so reading the whole config at once lets unset keys
    resolve to ``None`` instead.
    """
    try:
        current = task_handler_snap.get(None, typed=True) or {}
    except snap.SnapError:
        current = {}

    changed_config = {
        key: desired_value
        for key, desired_value in config.items()
        if not _snap_config_matches(_nested_get(current, key), desired_value)
    }
    if changed_config:
        task_handler_snap.set(changed_config)
    return bool(changed_config)


def _nested_get(config: dict[str, Any], dotted_key: str) -> Any:
    """Look up a dotted snap config key (e.g. ``landscape.database.main.host``).

    ``snap get -d`` returns a nested dict, so a dotted key must be traversed
    section by section. Returns ``None`` when any section along the path is
    missing (the key is unset).
    """
    value: Any = config
    for part in dotted_key.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _snap_config_matches(current_value: Any, desired_value: str) -> bool:
    """Return whether a current snap config value matches its desired string value."""
    if current_value is None:
        return False
    if isinstance(current_value, bool):
        return str(current_value).lower() == desired_value
    return str(current_value) == desired_value


def is_installed() -> bool:
    """Return whether the task-handler snap is present on the machine."""
    return snap.SnapCache()[TASK_HANDLER_SNAP_NAME].present


def stores_configured() -> bool:
    """Return whether the shared Landscape stores DB sections are set in the snap.

    Used by the charm to decide whether the workload is actually configured,
    rather than assuming so just because the relation exists.
    """
    keys = tuple(f"landscape.database.{prefix}.host" for prefix in _STORES_DB_PREFIXES)
    return _has_config_keys(keys)


def task_db_configured() -> bool:
    """Return whether the task-handler's own database section is set in the snap."""
    return _has_config_keys((f"landscape.database.{_TASK_DB_PREFIX}.host",))


def _has_config_keys(keys: tuple[str, ...]) -> bool:
    """Return whether every dotted key is set to a non-empty value in the snap config."""
    task_handler_snap = snap.SnapCache()[TASK_HANDLER_SNAP_NAME]
    if not task_handler_snap.present:
        return False
    try:
        current = task_handler_snap.get(None, typed=True) or {}
    except snap.SnapError:
        return False
    return all(_nested_get(current, key) not in (None, "") for key in keys)
