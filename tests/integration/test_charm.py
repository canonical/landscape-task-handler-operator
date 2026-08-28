# Copyright 2026-2026 Canonical Limited. All rights reserved.
#
# The integration tests use the Jubilant library and the pytest-jubilant plugin.
# See https://documentation.ubuntu.com/ops/latest/howto/write-integration-tests-for-a-charm/
#
# The `charm` fixture is defined in conftest.py.

import logging
import pathlib

import jubilant
import pytest

from tests.integration.conftest import USE_HOST_JUJU_MODEL

logger = logging.getLogger(__name__)

APP_NAME = "landscape-task-handler"
SNAP_NAME = "landscape-task-handler"
POSTGRES_APP = "postgresql"
CERTS_APP = "self-signed-certificates"
FAKE_LANDSCAPE_SERVER_APP = "fake-landscape-server"

# The task-handler writes its issued gRPC server certificate material here on
# `certificate_available` (see landscape_task_handler.CERTS_ACTIVE_DIR).
SERVER_CERTS_DIR = f"/var/snap/{SNAP_NAME}/common/certs/active"
SERVER_CERT_FILES = ("ca.crt", "server.crt", "server.key")


def _unit_agent_idle(status: jubilant.Status, app: str = APP_NAME) -> bool:
    """Return whether any of ``app``'s units has an idle agent.

    Waiting for this (rather than just an app appearing in status) avoids
    racing ahead of the charm's install hook: right after `juju deploy`, the
    unit briefly shows up while still provisioning/installing.
    """
    units = status.apps[app].units
    return bool(units) and any(u.juju_status.current == "idle" for u in units.values())


def leader_unit_name(juju: jubilant.Juju, app: str = APP_NAME) -> str:
    """Return the leader unit name for an application.

    Never assumes a fixed unit index/name: a freshly-deployed app in this
    suite's own temporary-model tests happens to have a single unit, but a
    live, long-lived model (e.g. one passed in via
    ``TASK_HANDLER_USE_HOST_JUJU_MODEL``) may have any unit as its current
    leader if the original one was ever replaced.
    """
    app_status = juju.status().apps[app]
    for name, unit_status in app_status.units.items():
        if unit_status.leader:
            return name
    pytest.fail(f"no leader unit found for {app}")


def test_deploy(charm: pathlib.Path, juju: jubilant.Juju):
    """Deploy the charm under test, unless it's already deployed.

    Skips the deploy entirely when the app is already present (e.g. a live
    model passed in via ``TASK_HANDLER_USE_HOST_JUJU_MODEL``), so this suite
    never redeploys over, or otherwise mutates, an already-running unit.
    """
    if APP_NAME in juju.status().apps:
        logger.info("%s is already deployed; skipping deploy", APP_NAME)
        return
    juju.deploy(
        str(charm), app=APP_NAME
    )  # The charm blocks until its required relations are present.
    juju.wait(_unit_agent_idle)


def test_snap_is_installed(juju: jubilant.Juju):
    """Verify that the snap was actually installed on the unit."""
    task = juju.exec(f"snap list {SNAP_NAME}", unit=leader_unit_name(juju))
    assert SNAP_NAME in task.stdout, f"Snap {SNAP_NAME} not found in output: {task.stdout}"


@pytest.mark.skipif(
    USE_HOST_JUJU_MODEL, reason="would deploy postgresql into a live, shared model"
)
def test_task_db_relation(juju: jubilant.Juju):
    """Test that the task-handler and postgres charms can be related for the task DB."""
    status = juju.status()
    if POSTGRES_APP not in status.apps:
        juju.deploy(POSTGRES_APP, channel="16/stable")
        juju.wait(lambda status: POSTGRES_APP in status.apps)

    if "task-db" not in juju.status().apps[APP_NAME].relations:
        juju.integrate(f"{APP_NAME}:task-db", POSTGRES_APP)

    def _relation_ready(status: jubilant.Status) -> bool:
        return "task-db" in status.apps[APP_NAME].relations

    juju.wait(_relation_ready)

    relations = set(juju.status().apps[APP_NAME].relations)
    assert "task-db" in relations


@pytest.mark.skipif(
    USE_HOST_JUJU_MODEL,
    reason="would deploy a test-double charm and add a relation to a live, shared model",
)
def test_stores_relation_prefers_task_db_host_over_unreachable_landscape_server_host(
    fake_landscape_server_charm: pathlib.Path, juju: jubilant.Juju
):
    """The shared stores must use task-db's real host, not landscape-server's.

    Reproduces the real-world bug: when landscape-server fronts the shared
    main/account/resource stores with PgBouncer, it publishes ``localhost``
    (its own PgBouncer subordinate's address) on the ``landscape-server``
    relation, which is meaningless from any other unit's own machine. The
    fake provider charm here publishes that exact ``localhost``/``disable``
    SSL data (see ``fake_landscape_server``); on this unit, ``localhost:6432``
    has nothing listening, so it reproduces the bug's unreachability. If the
    charm still preferred that data, the shared-store snap config would end
    up pointing at a host this unit can never reach; asserting it instead
    matches ``task-db``'s already-verified-reachable host and ``require`` SSL
    mode proves the fix takes effect end-to-end, not just in unit tests.

    Requires ``test_task_db_relation`` to have already run in this module, so
    the task-db relation (and thus a resolvable host to prefer) exists. Only
    runs against a temporary model: deploying the fake provider charm and
    relating it would be a mutating, non-representative change on a live,
    shared model, where the real landscape-server relation already exists.
    """
    status = juju.status()
    if FAKE_LANDSCAPE_SERVER_APP not in status.apps:
        juju.deploy(str(fake_landscape_server_charm), app=FAKE_LANDSCAPE_SERVER_APP)
        juju.wait(lambda status: FAKE_LANDSCAPE_SERVER_APP in status.apps)

    if "landscape-server" not in juju.status().apps[APP_NAME].relations:
        juju.integrate(f"{APP_NAME}:landscape-server", FAKE_LANDSCAPE_SERVER_APP)

    def _relation_ready(status: jubilant.Status) -> bool:
        return "landscape-server" in status.apps[APP_NAME].relations

    juju.wait(_relation_ready)

    unit = leader_unit_name(juju)

    def _task_db_host_available(status: jubilant.Status) -> str | None:
        del status  # unused, required by juju.wait's callback signature
        try:
            host = juju.exec(
                "snap get landscape-task-handler landscape.database.task-handler.host",
                unit=unit,
            ).stdout.strip()
        except jubilant.TaskError:
            return None
        return host or None

    juju.wait(lambda status: _task_db_host_available(status) is not None)
    task_db_endpoint = _task_db_host_available(None)

    def _stores_configured_with_task_db_host(status: jubilant.Status) -> bool:
        del status  # unused, required by juju.wait's callback signature
        try:
            host = juju.exec(
                "snap get landscape-task-handler landscape.database.main.host",
                unit=unit,
            ).stdout.strip()
        except jubilant.TaskError:
            return False
        return host == task_db_endpoint

    juju.wait(_stores_configured_with_task_db_host)

    for prefix in ("main", "account", "resource"):
        host = juju.exec(
            f"snap get landscape-task-handler landscape.database.{prefix}.host",
            unit=unit,
        ).stdout.strip()
        ssl = juju.exec(
            f"snap get landscape-task-handler landscape.database.{prefix}.ssl",
            unit=unit,
        ).stdout.strip()
        # The fake provider publishes an unreachable host and `disable` SSL;
        # neither should ever appear here.
        assert host == task_db_endpoint, (
            f"{prefix} host {host!r} does not match task-db's real endpoint "
            f"{task_db_endpoint!r}; the charm used landscape-server's "
            "unreachable published host instead"
        )
        assert ssl == "require", f"{prefix} ssl mode was {ssl!r}, expected 'require'"


@pytest.mark.skipif(
    not USE_HOST_JUJU_MODEL,
    reason="verifies real, already-deployed relation data; nothing to check without it",
)
def test_live_stores_configuration_uses_reachable_task_db_host(juju: jubilant.Juju):
    """Read-only check that the shared stores use a reachable host, on a live model.

    Unlike ``test_stores_relation_prefers_task_db_host_over_unreachable_landscape_server_host``,
    this makes no deploys or relation changes: it only reads the current,
    already-applied snap configuration on an already-deployed, real
    environment (for example a live stg/prod-like model passed in via
    ``TASK_HANDLER_USE_HOST_JUJU_MODEL``), where landscape-server and task-db
    are both already related. If landscape-server there is fronted by
    PgBouncer for the shared stores (as in the real HA topology this fix
    targets), the main/account/resource host must match task-db's own
    already-reachable host, with SSL required for that non-loopback
    connection.
    """
    status = juju.status()
    app_status = status.apps.get(APP_NAME)
    assert app_status is not None, f"{APP_NAME} not found in the current model"
    assert "task-db" in app_status.relations, (
        f"{APP_NAME} has no task-db relation in the current model; nothing to verify"
    )
    assert "landscape-server" in app_status.relations, (
        f"{APP_NAME} has no landscape-server relation in the current model; nothing to verify"
    )

    unit = leader_unit_name(juju)

    task_db_host = juju.exec(
        "snap get landscape-task-handler landscape.database.task-handler.host",
        unit=unit,
    ).stdout.strip()
    assert task_db_host, "task-db host is not set; is the task-db relation fully settled?"

    for prefix in ("main", "account", "resource"):
        host = juju.exec(
            f"snap get landscape-task-handler landscape.database.{prefix}.host",
            unit=unit,
        ).stdout.strip()
        ssl = juju.exec(
            f"snap get landscape-task-handler landscape.database.{prefix}.ssl",
            unit=unit,
        ).stdout.strip()
        assert host == task_db_host, (
            f"{prefix} host {host!r} does not match task-db's reachable host "
            f"{task_db_host!r}; this unit may not be reaching the shared stores"
        )
        assert ssl == "require", f"{prefix} ssl mode was {ssl!r}, expected 'require'"


@pytest.mark.skipif(
    USE_HOST_JUJU_MODEL, reason="would deploy self-signed-certificates into a live, shared model"
)
def test_certificates_relation(juju: jubilant.Juju):
    """Relate a tls-certificates provider and verify the gRPC server certs are written.

    Deploying ``self-signed-certificates`` and integrating it on the
    ``certificates`` endpoint exercises the full requirer flow: the task-handler
    requests its server (and the outbox client) certificate, the provider issues
    them, and the charm writes the server certificate material into the snap's
    active certs directory. Asserting those files exist confirms the provider
    integration works end to end.
    """
    status = juju.status()
    if CERTS_APP not in status.apps:
        juju.deploy(CERTS_APP, channel="1/stable")
        juju.wait(lambda status: CERTS_APP in status.apps)

    if "certificates" not in juju.status().apps[APP_NAME].relations:
        juju.integrate(f"{APP_NAME}:certificates", CERTS_APP)

    def _relation_ready(status: jubilant.Status) -> bool:
        return "certificates" in status.apps[APP_NAME].relations

    juju.wait(_relation_ready)

    unit = leader_unit_name(juju)

    def _server_certs_written(status: jubilant.Status) -> bool:
        try:
            result = juju.exec(f"ls {SERVER_CERTS_DIR}", unit=unit)
        except jubilant.TaskError:
            # Directory does not exist yet (certificate not written).
            return False
        return all(name in result.stdout for name in SERVER_CERT_FILES)

    juju.wait(_server_certs_written)

    relations = set(juju.status().apps[APP_NAME].relations)
    assert "certificates" in relations
