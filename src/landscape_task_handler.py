# Copyright 2026-2026 Canonical Limited. All rights reserved.
"""Functions for managing and interacting with the workload.

The intention is that this module could be used outside the context of a charm.
"""

import logging
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from charmlibs import snap

logger = logging.getLogger(__name__)

TASK_HANDLER_SNAP_NAME = "landscape-task-handler"
DEFAULT_SNAP_CHANNEL = "latest/edge"

# The long-running (always-active) snap services. Other snap services (for
# example the cleanup and cert-renewer services) are one-shot daemons that are
# not expected to stay active, so they are excluded from health checks.
TASK_HANDLER_ACTIVE_SERVICES = ("server", "worker")

_STORES_DB_PREFIXES = ("main", "account", "resource")
_TASK_DB_PREFIX = "task-handler"

SNAP_COMMON = Path(f"/var/snap/{TASK_HANDLER_SNAP_NAME}/common")
CERTS_ACTIVE_DIR = SNAP_COMMON / "certs" / "active"
CUSTOM_CERTS_DIR = SNAP_COMMON / "custom-certs"
CA_CERT_FILE = "ca.crt"
SERVER_CERT_FILE = "server.crt"
SERVER_KEY_FILE = "server.key"
CLIENT_CERT_FILE = "client.crt"
CLIENT_KEY_FILE = "client.key"
CERT_RENEWER_SERVICE = "cert-renewer"
DEFAULT_GRPC_PORT = "50051"

SENSITIVE_CONFIG_FIELDS = frozenset({"password", "secret"})

# Maps charm config option names to the snap config keys read by the workload's
# start scripts. Covers the shared logging settings (server, worker and cleanup),
# the worker (landscape.worker.*) and the cleanup service (landscape.cleanup.*).
_RUNTIME_KEY_MAP = {
    "log-level": "landscape.logging.level",
    "log-human-readable": "landscape.logging.human-readable",
    "worker-sleep": "landscape.worker.sleep",
    "worker-max-retries": "landscape.worker.max-retries",
    "worker-batch-size": "landscape.worker.batch-size",
    "worker-lease-duration": "landscape.worker.lease-duration",
    "worker-lease-reset-interval": "landscape.worker.lease-reset-interval",
    "worker-concurrency": "landscape.worker.concurrency",
    "worker-conn-max-lifetime": "landscape.worker.conn-max-lifetime",
    "cleanup-failed-retention-duration": "landscape.cleanup.failed-retention-duration",
    "cleanup-batch-size": "landscape.cleanup.batch-size",
    "cleanup-batch-sleep": "landscape.cleanup.batch-sleep",
}


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

    This is the database that owns the task-handler's own task queue table(s).
    The snap auto-creates the tables on startup; the database and login role
    must already exist.

    Setting snap config is enough to apply it: the snap's ``configure`` hook
    restarts the affected services, so the charm must not restart the snap here
    (doing so would cause redundant, back-to-back restarts).
    """
    task_handler_snap = snap.SnapCache()[TASK_HANDLER_SNAP_NAME]
    config = _database_section(_TASK_DB_PREFIX, host, port, user, password, database, ssl)
    _set_snap_config_if_changed(task_handler_snap, config)


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

    _set_snap_config_if_changed(task_handler_snap, config)


def configure_runtime(options: Mapping[str, Any]) -> None:
    """Apply the logging, worker and cleanup runtime settings to the snap.

    ``options`` is the charm's config mapping; only the keys in
    ``_RUNTIME_KEY_MAP`` are consumed and the rest are ignored. Options that are
    unset (absent, ``None`` or empty) are left untouched so the snap's own
    defaults apply. The logging settings are shared by the server, worker and
    cleanup services; the worker and cleanup settings are read by their
    respective services. Only writes snap config when a value actually changes,
    so unrelated events do not trigger a restart.
    """
    task_handler_snap = snap.SnapCache()[TASK_HANDLER_SNAP_NAME]
    config: dict[str, str] = {}
    for option, snap_key in _RUNTIME_KEY_MAP.items():
        value = options.get(option)
        if value is None or value == "":
            continue
        config[snap_key] = _to_snap_value(value)

    if config:
        _set_snap_config_if_changed(task_handler_snap, config)


def _to_snap_value(value: Any) -> str:
    """Render a charm config value as the string the snap expects.

    Booleans become lowercase ``true``/``false`` (matching the snap's boolean
    parsing); everything else is stringified as-is.
    """
    if isinstance(value, bool):
        return str(value).lower()
    return str(value)


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


def write_custom_certificates(
    ca: str,
    server_cert: str,
    server_key: str,
    client_cert: str,
    client_key: str,
) -> None:
    """Provision the operator-managed mTLS material for the task-handler snap.

    The snap treats certificates placed in ``custom-certs`` as operator-provided
    ("custom") material: its cert-manager validates them, copies them into the
    active tree, and writes a sentinel that disables auto-rotation so the snap
    never regenerates over the charm-issued certificates. Writing straight into
    the active tree would instead look like auto-generated material and get
    clobbered by the snap's self-signed rotation.

    All five files (CA, server and client certificate/key) are required for the
    snap to adopt the bundle, and the server and client certificates must chain
    to the same CA. Writes are atomic (temp file + ``os.replace``) so a
    concurrent read never observes a partial file. After writing, the snap's
    cert-manager is run so the new material is adopted immediately instead of
    waiting for the next restart or the daily rotation timer.
    """
    CUSTOM_CERTS_DIR.mkdir(parents=True, exist_ok=True)
    _atomic_write(CUSTOM_CERTS_DIR / CA_CERT_FILE, ca, 0o644)
    _atomic_write(CUSTOM_CERTS_DIR / SERVER_CERT_FILE, server_cert, 0o644)
    _atomic_write(CUSTOM_CERTS_DIR / SERVER_KEY_FILE, server_key, 0o600)
    _atomic_write(CUSTOM_CERTS_DIR / CLIENT_CERT_FILE, client_cert, 0o644)
    _atomic_write(CUSTOM_CERTS_DIR / CLIENT_KEY_FILE, client_key, 0o600)
    _adopt_custom_certificates()


def _adopt_custom_certificates() -> None:
    """Run the snap's cert-manager so it adopts the custom certificates.

    The cert-manager runs as the ``cert-renewer`` one-shot service; starting it
    triggers an immediate, idempotent reconcile that adopts any valid custom
    certificates into the active tree without restarting the gRPC server (which
    reloads its certificate material on every handshake).
    """
    task_handler_snap = snap.SnapCache()[TASK_HANDLER_SNAP_NAME]
    if not task_handler_snap.present:
        raise snap.SnapNotFoundError(TASK_HANDLER_SNAP_NAME)
    task_handler_snap.start(services=[CERT_RENEWER_SERVICE])


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
    _set_snap_config_if_changed(task_handler_snap, config)


def _atomic_write(path: Path, content: str, mode: int) -> None:
    """Atomically write ``content`` to ``path`` with the given permission bits.

    The temporary file is created with the intended permissions up-front (via
    ``os.open`` with ``mode``) so a private key is never briefly readable with
    broader, umask-derived permissions before a ``chmod``.
    """
    tmp_path = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w") as tmp_file:
        tmp_file.write(content)
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
        for name in TASK_HANDLER_ACTIVE_SERVICES
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

    Returns True if any keys were changed. Callers do not need to restart the
    snap: any ``snap set`` fires the snap's ``configure`` hook, which restarts
    the affected services. Skipping unchanged keys avoids firing that hook (and
    the restart) when nothing actually changed.

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
