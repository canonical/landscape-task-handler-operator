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

logger = logging.getLogger(__name__)

APP_NAME = "landscape-task-handler"
SNAP_NAME = "landscape-task-handler"
POSTGRES_APP = "postgresql"
PGBOUNCER_APP = "pgbouncer"
LANDSCAPE_SERVER_APP = "landscape-server"
CERTS_APP = "self-signed-certificates"

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


def test_task_db_relation(juju: jubilant.Juju):
    """Test that the task-handler and postgres charms can be related for the task DB.

    Deploys `postgresql` and relates it, unless both are already present
    (e.g. on a live model where this relation already exists), making this
    safe to run against any model.
    """
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


def _deploy_if_missing(juju: jubilant.Juju, app: str, channel: str) -> None:
    """Deploy ``app`` from ``channel``, unless it's already present in the model."""
    if app in juju.status().apps:
        return
    juju.deploy(app, channel=channel)
    juju.wait(lambda status: app in status.apps)


def test_stores_relation_uses_reachable_task_db_host(juju: jubilant.Juju):
    """The shared stores must use a real, reachable PostgreSQL host.

    Deploys real `postgresql`, `pgbouncer`, and `landscape-server` charms
    (skipping any that are already present), relates `pgbouncer` in front of
    `landscape-server` (the real HA topology this fix targets), and confirms
    `landscape-task-handler` ends up with a working main/account/resource
    connection, not `pgbouncer`'s unreachable loopback address.
    """
    _deploy_if_missing(juju, POSTGRES_APP, channel="16/stable")
    _deploy_if_missing(juju, PGBOUNCER_APP, channel="1/stable")
    _deploy_if_missing(juju, LANDSCAPE_SERVER_APP, channel="26.04/stable")

    status = juju.status()
    if "backend-database" not in status.apps[PGBOUNCER_APP].relations:
        juju.integrate(f"{PGBOUNCER_APP}:backend-database", POSTGRES_APP)
    if "database" not in status.apps[LANDSCAPE_SERVER_APP].relations:
        juju.integrate(f"{LANDSCAPE_SERVER_APP}:database", PGBOUNCER_APP)
    if "landscape-server" not in status.apps[APP_NAME].relations:
        juju.integrate(f"{APP_NAME}:landscape-server", LANDSCAPE_SERVER_APP)

    def _relations_ready(status: jubilant.Status) -> bool:
        return "landscape-server" in status.apps[APP_NAME].relations

    juju.wait(_relations_ready, timeout=600)

    unit = leader_unit_name(juju)

    def _stores_host_available(status: jubilant.Status) -> bool:
        del status  # unused, required by juju.wait's callback signature
        try:
            host = juju.exec(
                "snap get landscape-task-handler landscape.database.main.host",
                unit=unit,
            ).stdout.strip()
        except jubilant.TaskError:
            return False
        return bool(host)

    juju.wait(_stores_host_available, timeout=600)

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
            f"{prefix} host {host!r} does not match task-db's reachable host {task_db_host!r}"
        )
        assert ssl == "require", f"{prefix} ssl mode was {ssl!r}, expected 'require'"


def test_certificates_relation(juju: jubilant.Juju):
    """Relate a tls-certificates provider and verify the gRPC server certs are written.

    Deploying ``self-signed-certificates`` and integrating it on the
    ``certificates`` endpoint exercises the full requirer flow: the task-handler
    requests its server (and the outbox client) certificate, the provider issues
    them, and the charm writes the server certificate material into the snap's
    active certs directory. Asserting those files exist confirms the provider
    integration works end to end. Skips the deploy/relate steps if both are
    already present (e.g. on a live model), making this safe to run anywhere.
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
