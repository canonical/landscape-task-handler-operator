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
