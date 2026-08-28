# Copyright 2026-2026 Canonical Limited. All rights reserved.
#
# The integration tests use the Jubilant library and the pytest-jubilant plugin.
# See https://documentation.ubuntu.com/ops/latest/howto/write-integration-tests-for-a-charm/

import os
import pathlib
import subprocess

import jubilant
import pytest

APP_NAME = "landscape-task-handler"

USE_HOST_JUJU_MODEL = os.getenv("TASK_HANDLER_USE_HOST_JUJU_MODEL", False)
"""
If set, attach to the current Juju model on the host instead of creating a
temporary one, and only deploy charms/relations that aren't already present.
Useful for iterating against a real, already-deployed environment (e.g. a
live stg/prod-like model) without redeploying everything on each run.
"""


def _host_juju() -> jubilant.Juju:
    """Return a reference to the current host Juju model.

    Runs a light check that the charm under test is already deployed there,
    since the test suite otherwise assumes it exists before adding relations
    to it.
    """
    juju = jubilant.Juju()
    model_applications = juju.status().apps
    assert APP_NAME in model_applications, (
        f"{APP_NAME} not found in the current model; deploy it first or unset "
        "TASK_HANDLER_USE_HOST_JUJU_MODEL to use a temporary model instead."
    )
    return juju


@pytest.fixture(scope="module")
def juju():
    """Return a Juju model for the test run.

    Uses the current host model (without destroying anything in it
    afterwards) when ``TASK_HANDLER_USE_HOST_JUJU_MODEL`` is set; otherwise
    creates a temporary model and destroys it after the test module
    finishes.
    """
    if USE_HOST_JUJU_MODEL:
        yield _host_juju()
    else:
        with jubilant.temp_model() as juju:
            yield juju


@pytest.fixture(scope="session")
def charm():
    """Return the path of the charm under test."""
    charm = os.environ.get("CHARM_PATH")
    if not charm:
        charm_dir = pathlib.Path()  # Assume the current working directory is the charm root.
        charms = list(charm_dir.glob("*.charm"))
        assert charms, f"No charms were found in {charm_dir.absolute()}"
        assert len(charms) == 1, f"Found more than one charm {charms}"
        charm = charms[0]
    path = pathlib.Path(charm).resolve()
    assert path.is_file(), f"{path} is not a file"
    return path


@pytest.fixture(scope="session")
def fake_landscape_server_charm():
    """Return the path of the fake landscape-server test-double charm.

    Packs it with charmcraft if a pre-built ``.charm`` isn't already present
    (mirroring the ``charm`` fixture's ``CHARM_PATH`` override), so CI can
    prebuild it once and point at the artifact via
    ``FAKE_LANDSCAPE_SERVER_CHARM_PATH`` instead of packing it on every run.
    """
    charm = os.environ.get("FAKE_LANDSCAPE_SERVER_CHARM_PATH")
    charm_dir = pathlib.Path(__file__).parent / "charms" / "fake_landscape_server"
    if not charm:
        charms = list(charm_dir.glob("*.charm"))
        if not charms:
            subprocess.run(["charmcraft", "pack"], cwd=charm_dir, check=True)
            charms = list(charm_dir.glob("*.charm"))
        assert charms, f"No charms were found in {charm_dir.absolute()}"
        assert len(charms) == 1, f"Found more than one charm {charms}"
        charm = charms[0]
    path = pathlib.Path(charm).resolve()
    assert path.is_file(), f"{path} is not a file"
    return path
