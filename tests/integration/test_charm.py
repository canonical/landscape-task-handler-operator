# Copyright 2026-2026 Canonical Limited. All rights reserved.
#
# The integration tests use the Jubilant library and the pytest-jubilant plugin.
# See https://documentation.ubuntu.com/ops/latest/howto/write-integration-tests-for-a-charm/
#
# The `charm` fixture is defined in conftest.py.

import logging
import pathlib

import jubilant

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


def test_deploy(charm: pathlib.Path, juju: jubilant.Juju):
    """Deploy the charm under test."""
    juju.deploy(
        str(charm), app=APP_NAME
    )  # The charm blocks until its required relations are present.
    juju.wait(lambda status: APP_NAME in status.apps)


def test_snap_is_installed(juju: jubilant.Juju):
    """Verify that the snap was actually installed on the unit."""
    task = juju.exec(f"snap list {SNAP_NAME}", unit=f"{APP_NAME}/0")
    assert SNAP_NAME in task.stdout, f"Snap {SNAP_NAME} not found in output: {task.stdout}"


def test_task_db_relation(juju: jubilant.Juju):
    """Test that the task-handler and postgres charms can be related for the task DB."""
    juju.deploy(POSTGRES_APP, channel="16/stable")
    juju.wait(lambda status: POSTGRES_APP in status.apps)
    juju.integrate(f"{APP_NAME}:task-db", POSTGRES_APP)

    def _relation_ready(status: jubilant.Status) -> bool:
        return "task-db" in status.apps[APP_NAME].relations

    juju.wait(_relation_ready)

    relations = set(juju.status().apps[APP_NAME].relations)
    assert "task-db" in relations


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
    the task-db relation (and thus a resolvable host to prefer) exists.
    """
    juju.deploy(str(fake_landscape_server_charm), app=FAKE_LANDSCAPE_SERVER_APP)
    juju.wait(lambda status: FAKE_LANDSCAPE_SERVER_APP in status.apps)
    juju.integrate(f"{APP_NAME}:landscape-server", FAKE_LANDSCAPE_SERVER_APP)

    def _relation_ready(status: jubilant.Status) -> bool:
        return "landscape-server" in status.apps[APP_NAME].relations

    juju.wait(_relation_ready)

    task_db_endpoint = juju.exec(
        "snap get landscape-task-handler landscape.database.task-handler.host",
        unit=f"{APP_NAME}/0",
    ).stdout.strip()

    def _stores_configured_with_task_db_host(status: jubilant.Status) -> bool:
        del status  # unused, required by juju.wait's callback signature
        try:
            host = juju.exec(
                "snap get landscape-task-handler landscape.database.main.host",
                unit=f"{APP_NAME}/0",
            ).stdout.strip()
        except jubilant.TaskError:
            return False
        return host == task_db_endpoint

    juju.wait(_stores_configured_with_task_db_host)

    for prefix in ("main", "account", "resource"):
        host = juju.exec(
            f"snap get landscape-task-handler landscape.database.{prefix}.host",
            unit=f"{APP_NAME}/0",
        ).stdout.strip()
        ssl = juju.exec(
            f"snap get landscape-task-handler landscape.database.{prefix}.ssl",
            unit=f"{APP_NAME}/0",
        ).stdout.strip()
        # The fake provider publishes an unreachable host and `disable` SSL;
        # neither should ever appear here.
        assert host == task_db_endpoint, (
            f"{prefix} host {host!r} does not match task-db's real endpoint "
            f"{task_db_endpoint!r}; the charm used landscape-server's "
            "unreachable published host instead"
        )
        assert ssl == "require", f"{prefix} ssl mode was {ssl!r}, expected 'require'"


def test_certificates_relation(juju: jubilant.Juju):
    """Relate a tls-certificates provider and verify the gRPC server certs are written.

    Deploying ``self-signed-certificates`` and integrating it on the
    ``certificates`` endpoint exercises the full requirer flow: the task-handler
    requests its server (and the outbox client) certificate, the provider issues
    them, and the charm writes the server certificate material into the snap's
    active certs directory. Asserting those files exist confirms the provider
    integration works end to end.
    """
    juju.deploy(CERTS_APP, channel="1/stable")
    juju.wait(lambda status: CERTS_APP in status.apps)
    juju.integrate(f"{APP_NAME}:certificates", CERTS_APP)

    def _relation_ready(status: jubilant.Status) -> bool:
        return "certificates" in status.apps[APP_NAME].relations

    juju.wait(_relation_ready)

    def _server_certs_written(status: jubilant.Status) -> bool:
        try:
            result = juju.exec(f"ls {SERVER_CERTS_DIR}", unit=f"{APP_NAME}/0")
        except jubilant.TaskError:
            # Directory does not exist yet (certificate not written).
            return False
        return all(name in result.stdout for name in SERVER_CERT_FILES)

    juju.wait(_server_certs_written)

    relations = set(juju.status().apps[APP_NAME].relations)
    assert "certificates" in relations
