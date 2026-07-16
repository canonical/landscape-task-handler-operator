# Copyright 2026-2026 Canonical Limited. All rights reserved.
#
# To learn more about testing, see https://documentation.ubuntu.com/ops/latest/explanation/testing/

from unittest.mock import MagicMock, PropertyMock

import ops
import pytest
from charmlibs import snap
from ops import testing

import landscape_task_handler
from charm import LandscapeTaskHandlerCharm


@pytest.fixture
def mock_snap(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Patch snap.SnapCache to return a single mock snap object."""
    snap_obj = MagicMock()
    snap_obj.present = False

    cache = MagicMock()
    cache.__getitem__.return_value = snap_obj

    monkeypatch.setattr("landscape_task_handler.snap.SnapCache", lambda: cache)
    return snap_obj


class TestInstallAndLifecycle:
    def test_install(self, mock_snap: MagicMock):
        """The install hook installs the snap from the configured channel."""
        ctx = testing.Context(LandscapeTaskHandlerCharm)

        ctx.run(ctx.on.install(), testing.State())

        mock_snap.ensure.assert_called_once_with(snap.SnapState.Latest, channel="latest/stable")

    def test_install_snap_error_blocks(self, mock_snap: MagicMock):
        """A SnapError during install blocks the unit."""
        mock_snap.ensure.side_effect = snap.SnapError("boom")
        ctx = testing.Context(LandscapeTaskHandlerCharm)

        state_out = ctx.run(ctx.on.install(), testing.State())

        assert state_out.unit_status == testing.BlockedStatus(
            "Failed to install task-handler snap"
        )

    def test_start_sets_version(self, mock_snap: MagicMock):
        """The start hook starts the snap and records its revision as the version."""
        mock_snap.present = True
        mock_snap.revision = "42"
        ctx = testing.Context(LandscapeTaskHandlerCharm)

        state_out = ctx.run(ctx.on.start(), testing.State())

        mock_snap.start.assert_called_once_with(enable=True)
        assert state_out.workload_version == "42"

    def test_upgrade_refreshes_snap(self, mock_snap: MagicMock):
        """The upgrade hook refreshes the snap to the configured channel."""
        mock_snap.present = True
        mock_snap.revision = "43"
        ctx = testing.Context(LandscapeTaskHandlerCharm)

        state_out = ctx.run(ctx.on.upgrade_charm(), testing.State())

        mock_snap.ensure.assert_called_once_with(snap.SnapState.Latest, channel="latest/stable")
        assert state_out.workload_version == "43"

    def test_config_changed_refreshes_channel(self, mock_snap: MagicMock):
        """A channel config change refreshes the snap to the new channel."""
        mock_snap.present = True
        mock_snap.revision = "44"
        ctx = testing.Context(LandscapeTaskHandlerCharm)

        ctx.run(
            ctx.on.config_changed(),
            testing.State(config={"task-handler-snap-channel": "latest/edge"}),
        )

        mock_snap.ensure.assert_called_once_with(snap.SnapState.Latest, channel="latest/edge")


class TestTaskDbRelation:
    def test_task_db_configures_snap(self, mock_snap: MagicMock):
        """The task-db relation sets the task-handler database section."""
        mock_snap.present = True
        mock_snap.get.return_value = None
        ctx = testing.Context(LandscapeTaskHandlerCharm)

        secret = testing.Secret(
            tracked_content={"username": "taskuser", "password": "taskpw"},
        )
        relation = testing.Relation(
            endpoint="task-db",
            interface="postgresql_client",
            remote_app_name="postgresql",
            remote_app_data={
                "database": "task-handler",
                "endpoints": "10.0.0.5:5432",
                "username": "taskuser",
                "password": "taskpw",
                "secret-user": secret.id,
            },
        )
        state_in = testing.State(relations={relation}, secrets={secret})

        ctx.run(ctx.on.relation_changed(relation), state_in)

        set_config = mock_snap.set.call_args[0][0]
        assert set_config["landscape.database.task-handler.host"] == "10.0.0.5"
        assert set_config["landscape.database.task-handler.port"] == "5432"
        assert set_config["landscape.database.task-handler.name"] == "task-handler"
        assert set_config["landscape.database.task-handler.user"] == "taskuser"
        assert set_config["landscape.database.task-handler.password"] == "taskpw"
        mock_snap.restart.assert_called_once()


class TestStoresRelation:
    def test_stores_fans_out_to_three_sections(self, mock_snap: MagicMock):
        """The stores relation fans out into main/account/resource DB sections."""
        mock_snap.present = True
        mock_snap.get.return_value = None
        ctx = testing.Context(LandscapeTaskHandlerCharm)

        secret = testing.Secret(tracked_content={"password": "storespw"})
        relation = testing.Relation(
            endpoint="landscape-server",
            interface="landscape-task-handler",
            remote_app_name="landscape-server",
            remote_app_data={
                "host": "db.example.com",
                "port": "5432",
                "user": "landscape",
                "main": "landscape-standalone-main",
                "account_1": "landscape-standalone-account-1",
                "resource_1": "landscape-standalone-resource-1",
                "sslmode": "require",
                "secret-id": secret.id,
            },
        )
        state_in = testing.State(relations={relation}, secrets={secret})

        ctx.run(ctx.on.relation_changed(relation), state_in)

        set_config = mock_snap.set.call_args[0][0]
        assert set_config["landscape.database.main.name"] == "landscape-standalone-main"
        assert set_config["landscape.database.account.name"] == "landscape-standalone-account-1"
        assert set_config["landscape.database.resource.name"] == "landscape-standalone-resource-1"
        for prefix in ("main", "account", "resource"):
            assert set_config[f"landscape.database.{prefix}.host"] == "db.example.com"
            assert set_config[f"landscape.database.{prefix}.password"] == "storespw"
            assert set_config[f"landscape.database.{prefix}.ssl"] == "require"
        mock_snap.restart.assert_called_once()

    def test_stores_missing_fields_defers(self, mock_snap: MagicMock):
        """Incomplete stores databag defers without touching the snap."""
        mock_snap.present = True
        ctx = testing.Context(LandscapeTaskHandlerCharm)

        relation = testing.Relation(
            endpoint="landscape-server",
            interface="landscape-task-handler",
            remote_app_name="landscape-server",
            remote_app_data={"host": "db.example.com"},
        )
        state_in = testing.State(relations={relation})

        ctx.run(ctx.on.relation_changed(relation), state_in)

        mock_snap.set.assert_not_called()


class TestStatus:
    def test_waiting_without_relations(self, mock_snap: MagicMock):
        """Without the required relations the unit reports waiting after start."""
        mock_snap.present = True
        mock_snap.revision = "1"
        ctx = testing.Context(LandscapeTaskHandlerCharm)

        state_out = ctx.run(ctx.on.start(), testing.State())

        assert isinstance(state_out.unit_status, testing.WaitingStatus)


class TestGrpcCertificates:
    def test_write_custom_certificates(
        self, tmp_path, mock_snap: MagicMock, monkeypatch: pytest.MonkeyPatch
    ):
        """The full mTLS bundle is written to custom-certs with keys locked to 0600."""
        custom_dir = tmp_path / "custom-certs"
        monkeypatch.setattr(landscape_task_handler, "CUSTOM_CERTS_DIR", custom_dir)
        mock_snap.present = True

        landscape_task_handler.write_custom_certificates(
            ca="CA-PEM",
            server_cert="SERVER-PEM",
            server_key="SERVER-KEY-PEM",
            client_cert="CLIENT-PEM",
            client_key="CLIENT-KEY-PEM",
        )

        assert (custom_dir / "ca.crt").read_text() == "CA-PEM"
        assert (custom_dir / "server.crt").read_text() == "SERVER-PEM"
        assert (custom_dir / "client.crt").read_text() == "CLIENT-PEM"
        server_key = custom_dir / "server.key"
        client_key = custom_dir / "client.key"
        assert server_key.read_text() == "SERVER-KEY-PEM"
        assert client_key.read_text() == "CLIENT-KEY-PEM"
        assert (server_key.stat().st_mode & 0o777) == 0o600
        assert (client_key.stat().st_mode & 0o777) == 0o600
        # The snap's cert-manager is run so the material is adopted immediately.
        mock_snap.start.assert_called_once_with(services=["cert-renewer"])

    def test_write_custom_certificates_requires_installed_snap(
        self, tmp_path, mock_snap: MagicMock, monkeypatch: pytest.MonkeyPatch
    ):
        """Adoption fails loudly if the snap is not installed."""
        monkeypatch.setattr(landscape_task_handler, "CUSTOM_CERTS_DIR", tmp_path / "custom-certs")
        mock_snap.present = False

        with pytest.raises(snap.SnapNotFoundError):
            landscape_task_handler.write_custom_certificates(
                ca="CA-PEM",
                server_cert="SERVER-PEM",
                server_key="SERVER-KEY-PEM",
                client_cert="CLIENT-PEM",
                client_key="CLIENT-KEY-PEM",
            )

    def test_configure_grpc_sets_host_and_restarts(self, mock_snap: MagicMock):
        """The initial gRPC config sets the listen host + certs dir and restarts once."""
        mock_snap.present = True
        mock_snap.get.return_value = None

        landscape_task_handler.configure_grpc(host="10.0.0.9")

        cfg = mock_snap.set.call_args[0][0]
        assert cfg["landscape.task-handler.host"] == "10.0.0.9"
        assert cfg["landscape.task-handler.grpc-port"] == "50051"
        assert cfg["landscape.task-handler.grpc-certs-dir"] == str(
            landscape_task_handler.CERTS_ACTIVE_DIR
        )
        mock_snap.restart.assert_called_once()

    def test_configure_grpc_unchanged_does_not_restart(self, mock_snap: MagicMock):
        """Rotation (same host/dir) must not re-set config or restart the snap."""
        mock_snap.present = True
        # snap get -d returns the whole config as a nested dict; unset keys are
        # simply absent (they must not raise).
        mock_snap.get.return_value = {
            "landscape": {
                "task-handler": {
                    "host": "10.0.0.9",
                    "grpc-port": "50051",
                    "grpc-certs-dir": str(landscape_task_handler.CERTS_ACTIVE_DIR),
                }
            }
        }

        landscape_task_handler.configure_grpc(host="10.0.0.9")

        mock_snap.set.assert_not_called()
        mock_snap.restart.assert_not_called()


class _FakePem:
    """A stand-in for the tls_certificates Certificate/PrivateKey PEM objects."""

    def __init__(self, pem: str):
        self._pem = pem

    def __str__(self) -> str:
        return self._pem


class _FakeProviderCertificate:
    def __init__(self, ca: str, certificate: str):
        self.ca = _FakePem(ca)
        self.certificate = _FakePem(certificate)


class TestOutboxCertPublishing:
    def test_publishes_client_bundle_on_stores_relation(self, monkeypatch: pytest.MonkeyPatch):
        """The leader publishes the outbox client bundle + grpc-address on stores."""
        monkeypatch.setattr(
            "charm.LandscapeTaskHandlerCharm._routable_address",
            lambda self: "10.1.1.9",
        )
        monkeypatch.setattr(
            "charm.TLSCertificatesRequiresV4.get_assigned_certificate",
            lambda self, certificate_request: (
                _FakeProviderCertificate("CA-PEM", "CLIENT-PEM"),
                _FakePem("CLIENT-KEY-PEM"),
            ),
        )
        ctx = testing.Context(LandscapeTaskHandlerCharm)
        relation = testing.Relation(
            endpoint="landscape-server",
            interface="landscape-task-handler",
            remote_app_name="landscape-server",
            remote_app_data={},
        )
        state_in = testing.State(leader=True, relations={relation})

        state_out = ctx.run(ctx.on.relation_changed(relation), state_in)

        app_data = dict(next(iter(state_out.relations)).local_app_data)
        assert app_data["grpc-address"] == "10.1.1.9:50051"
        assert app_data["certs-revision"] == "1"
        secret = state_out.get_secret(id=app_data["certs-secret-id"])
        assert secret.latest_content == {
            "ca-cert": "CA-PEM",
            "client-cert": "CLIENT-PEM",
            "client-key": "CLIENT-KEY-PEM",
        }

    def test_non_leader_does_not_publish(self, monkeypatch: pytest.MonkeyPatch):
        """A non-leader unit must not write to the stores app databag."""
        monkeypatch.setattr(
            "charm.LandscapeTaskHandlerCharm._routable_address",
            lambda self: "10.1.1.9",
        )
        monkeypatch.setattr(
            "charm.TLSCertificatesRequiresV4.get_assigned_certificate",
            lambda self, certificate_request: (
                _FakeProviderCertificate("CA-PEM", "CLIENT-PEM"),
                _FakePem("CLIENT-KEY-PEM"),
            ),
        )
        ctx = testing.Context(LandscapeTaskHandlerCharm)
        relation = testing.Relation(
            endpoint="landscape-server",
            interface="landscape-task-handler",
            remote_app_name="landscape-server",
            remote_app_data={},
        )
        state_in = testing.State(leader=False, relations={relation})

        state_out = ctx.run(ctx.on.relation_changed(relation), state_in)

        assert dict(next(iter(state_out.relations)).local_app_data) == {}


class TestGrpcHaproxyRoute:
    def test_publishes_grpc_route_requirements(self, monkeypatch: pytest.MonkeyPatch):
        """Relating to haproxy publishes a TLS-passthrough TCP route under the hostname."""
        monkeypatch.setattr(
            "charm.LandscapeTaskHandlerCharm._routable_address",
            lambda self: "10.2.2.2",
        )
        ctx = testing.Context(LandscapeTaskHandlerCharm)
        relation = testing.Relation(
            endpoint="grpc-haproxy-route",
            interface="haproxy-route-tcp",
            remote_app_name="haproxy",
        )
        # The haproxy hostname is the external Landscape hostname supplied by
        # landscape-server (as in landscape-debarchive), stored on the charm.
        stored = testing.StoredState(
            owner_path="LandscapeTaskHandlerCharm",
            content={"hostname": "landscape.example.com"},
        )
        state_in = testing.State(leader=True, relations={relation}, stored_states={stored})

        state_out = ctx.run(ctx.on.relation_joined(relation), state_in)

        app_data = dict(next(iter(state_out.relations)).local_app_data)
        serialized = "".join(app_data.values())
        # External frontend port, backend (snap) port, SNI hostname, and passthrough.
        assert "50053" in serialized
        assert "50051" in serialized
        assert "landscape.example.com" in serialized
        assert "false" in app_data["tls_terminate"]

    def test_no_hostname_skips_haproxy_route(self, monkeypatch: pytest.MonkeyPatch):
        """Without a Landscape hostname yet, no gRPC backend is published."""
        monkeypatch.setattr(
            "charm.LandscapeTaskHandlerCharm._routable_address",
            lambda self: "10.2.2.2",
        )
        ctx = testing.Context(LandscapeTaskHandlerCharm)
        relation = testing.Relation(
            endpoint="grpc-haproxy-route",
            interface="haproxy-route-tcp",
            remote_app_name="haproxy",
        )
        state_in = testing.State(leader=True, relations={relation})

        state_out = ctx.run(ctx.on.relation_joined(relation), state_in)

        assert dict(next(iter(state_out.relations)).local_app_data) == {}

    def test_stores_relation_stores_landscape_hostname(self, monkeypatch: pytest.MonkeyPatch):
        """A hostname on the stores relation is registered with haproxy (like debarchive)."""
        monkeypatch.setattr(
            "charm.LandscapeTaskHandlerCharm._routable_address",
            lambda self: "10.2.2.2",
        )
        ctx = testing.Context(LandscapeTaskHandlerCharm)
        grpc = testing.Relation(
            endpoint="grpc-haproxy-route",
            interface="haproxy-route-tcp",
            remote_app_name="haproxy",
        )
        stores = testing.Relation(
            endpoint="landscape-server",
            interface="landscape-task-handler",
            remote_app_name="landscape-server",
            remote_app_data={"hostname": "landscape.example.com"},
        )
        state_in = testing.State(leader=True, relations={stores, grpc})

        state_out = ctx.run(ctx.on.relation_changed(stores), state_in)

        grpc_out = next(r for r in state_out.relations if r.endpoint == "grpc-haproxy-route")
        assert "landscape.example.com" in "".join(dict(grpc_out.local_app_data).values())


def _stores_relation(secret_id: str | None = None, **overrides):
    """Build a stores relation with a complete databag (optionally a secret-id)."""
    data = {
        "host": "db.example.com",
        "port": "5432",
        "user": "landscape",
        "main": "landscape-standalone-main",
        "account_1": "landscape-standalone-account-1",
        "resource_1": "landscape-standalone-resource-1",
        "sslmode": "require",
    }
    data.update(overrides)
    if secret_id is not None:
        data["secret-id"] = secret_id
    return testing.Relation(
        endpoint="landscape-server",
        interface="landscape-task-handler",
        remote_app_name="landscape-server",
        remote_app_data=data,
    )


class TestLifecycleErrors:
    def test_start_snap_error_blocks(self, mock_snap: MagicMock):
        """A SnapError while starting blocks the unit."""
        mock_snap.present = True
        mock_snap.start.side_effect = snap.SnapError("boom")
        ctx = testing.Context(LandscapeTaskHandlerCharm)

        state_out = ctx.run(ctx.on.start(), testing.State())

        assert state_out.unit_status == testing.BlockedStatus("Failed to start task-handler snap")

    def test_upgrade_snap_error_blocks(self, mock_snap: MagicMock):
        """A SnapError while refreshing on upgrade blocks the unit."""
        mock_snap.present = True
        mock_snap.ensure.side_effect = snap.SnapError("boom")
        ctx = testing.Context(LandscapeTaskHandlerCharm)

        state_out = ctx.run(ctx.on.upgrade_charm(), testing.State())

        assert state_out.unit_status == testing.BlockedStatus(
            "Failed to refresh task-handler snap"
        )

    def test_config_changed_snap_error_blocks(self, mock_snap: MagicMock):
        """A SnapError while applying configuration blocks the unit."""
        mock_snap.present = True
        mock_snap.ensure.side_effect = snap.SnapError("boom")
        ctx = testing.Context(LandscapeTaskHandlerCharm)

        state_out = ctx.run(ctx.on.config_changed(), testing.State())

        assert state_out.unit_status == testing.BlockedStatus("Failed to apply configuration")

    def test_update_status_reconciles(self, mock_snap: MagicMock):
        """update-status reconciles and reports waiting when relations are missing."""
        mock_snap.present = True
        ctx = testing.Context(LandscapeTaskHandlerCharm)

        state_out = ctx.run(ctx.on.update_status(), testing.State())

        assert isinstance(state_out.unit_status, testing.WaitingStatus)

    def test_task_db_snap_error_blocks(self, mock_snap: MagicMock):
        """A snap failure while configuring the task DB blocks the unit."""
        mock_snap.present = True
        mock_snap.get.return_value = None
        mock_snap.set.side_effect = snap.SnapError("boom")
        ctx = testing.Context(LandscapeTaskHandlerCharm)

        secret = testing.Secret(tracked_content={"username": "u", "password": "p"})
        relation = testing.Relation(
            endpoint="task-db",
            interface="postgresql_client",
            remote_app_name="postgresql",
            remote_app_data={
                "database": "task-handler",
                "endpoints": "10.0.0.5:5432",
                "username": "u",
                "password": "p",
                "secret-user": secret.id,
            },
        )
        state_in = testing.State(relations={relation}, secrets={secret})

        state_out = ctx.run(ctx.on.relation_changed(relation), state_in)

        assert state_out.unit_status == testing.BlockedStatus("Failed to configure task database")

    def test_stores_snap_error_blocks(self, mock_snap: MagicMock):
        """A snap failure while configuring the stores blocks the unit."""
        mock_snap.present = True
        mock_snap.get.return_value = None
        mock_snap.set.side_effect = snap.SnapError("boom")
        ctx = testing.Context(LandscapeTaskHandlerCharm)

        secret = testing.Secret(tracked_content={"password": "storespw"})
        relation = _stores_relation(secret_id=secret.id)
        state_in = testing.State(relations={relation}, secrets={secret})

        state_out = ctx.run(ctx.on.relation_changed(relation), state_in)

        assert state_out.unit_status == testing.BlockedStatus(
            "Failed to configure Landscape stores"
        )

    def test_stores_broken_evaluates_status(self, mock_snap: MagicMock):
        """The stores relation-broken hook re-evaluates status."""
        mock_snap.present = True
        ctx = testing.Context(LandscapeTaskHandlerCharm)
        relation = _stores_relation()
        state_in = testing.State(relations={relation})

        ctx.run(ctx.on.relation_broken(relation), state_in)


class TestStatusEvaluation:
    def test_active_when_configured(self, mock_snap: MagicMock, monkeypatch: pytest.MonkeyPatch):
        """With both relations present and the snap configured the unit is active."""
        mock_snap.present = True
        mock_snap.get.return_value = None
        monkeypatch.setattr("landscape_task_handler.stores_configured", lambda: True)
        monkeypatch.setattr("landscape_task_handler.task_db_configured", lambda: True)
        ctx = testing.Context(LandscapeTaskHandlerCharm)
        stores = _stores_relation()
        task_db = testing.Relation(
            endpoint="task-db", interface="postgresql_client", remote_app_name="postgresql"
        )
        state_in = testing.State(relations={stores, task_db})

        state_out = ctx.run(ctx.on.update_status(), state_in)

        assert state_out.unit_status == testing.ActiveStatus()

    def test_waiting_for_db_config(self, mock_snap: MagicMock):
        """With both relations present but the snap unconfigured, waits for DB config."""
        mock_snap.present = True
        mock_snap.get.return_value = None
        ctx = testing.Context(LandscapeTaskHandlerCharm)
        stores = testing.Relation(
            endpoint="landscape-server",
            interface="landscape-task-handler",
            remote_app_name="landscape-server",
        )
        task_db = testing.Relation(
            endpoint="task-db", interface="postgresql_client", remote_app_name="postgresql"
        )
        state_in = testing.State(relations={stores, task_db})

        state_out = ctx.run(ctx.on.update_status(), state_in)

        assert isinstance(state_out.unit_status, testing.WaitingStatus)
        assert "database configuration" in state_out.unit_status.message


class TestStoresPassword:
    def test_no_secret_id_skips(self, mock_snap: MagicMock):
        """Complete DB fields but no secret-id: nothing is configured, no error."""
        mock_snap.present = True
        mock_snap.get.return_value = None
        ctx = testing.Context(LandscapeTaskHandlerCharm)
        relation = _stores_relation()  # no secret-id
        state_in = testing.State(relations={relation})

        ctx.run(ctx.on.relation_changed(relation), state_in)

        mock_snap.set.assert_not_called()

    def test_missing_secret_blocks(self, mock_snap: MagicMock):
        """A secret-id that resolves to nothing blocks the unit."""
        mock_snap.present = True
        mock_snap.get.return_value = None
        ctx = testing.Context(LandscapeTaskHandlerCharm)
        relation = _stores_relation(secret_id="secret:doesnotexist")
        state_in = testing.State(relations={relation})

        state_out = ctx.run(ctx.on.relation_changed(relation), state_in)

        assert state_out.unit_status == testing.BlockedStatus("no stores password")

    def test_secret_without_password_blocks(self, mock_snap: MagicMock):
        """A secret without a password field blocks the unit."""
        mock_snap.present = True
        mock_snap.get.return_value = None
        ctx = testing.Context(LandscapeTaskHandlerCharm)
        secret = testing.Secret(tracked_content={"other": "value"})
        relation = _stores_relation(secret_id=secret.id)
        state_in = testing.State(relations={relation}, secrets={secret})

        state_out = ctx.run(ctx.on.relation_changed(relation), state_in)

        assert state_out.unit_status == testing.BlockedStatus("no stores password")


class TestGrpcCertificatesReconcile:
    @staticmethod
    def _patch_certs(monkeypatch, write_mock):
        monkeypatch.setattr(
            "charm.LandscapeTaskHandlerCharm._routable_address",
            lambda self: "10.0.0.9",
        )
        monkeypatch.setattr(
            "charm.TLSCertificatesRequiresV4.get_assigned_certificate",
            lambda self, certificate_request: (
                _FakeProviderCertificate("CA-PEM", "CRT-PEM"),
                _FakePem("KEY-PEM"),
            ),
        )
        monkeypatch.setattr("landscape_task_handler.write_custom_certificates", write_mock)

    def test_certificate_configured(self, mock_snap: MagicMock, monkeypatch: pytest.MonkeyPatch):
        """When a certificate is available the server certs are written and grpc configured."""
        mock_snap.present = True
        mock_snap.get.return_value = None
        write_mock = MagicMock()
        self._patch_certs(monkeypatch, write_mock)
        ctx = testing.Context(LandscapeTaskHandlerCharm)
        certs = testing.Relation(
            endpoint="certificates", interface="tls-certificates", remote_app_name="ssc"
        )
        state_in = testing.State(relations={certs})

        ctx.run(ctx.on.update_status(), state_in)

        write_mock.assert_called_once()

    def test_certificate_error_blocks(self, mock_snap: MagicMock, monkeypatch: pytest.MonkeyPatch):
        """A failure writing certs blocks the unit."""
        mock_snap.present = True
        mock_snap.get.return_value = None
        self._patch_certs(monkeypatch, MagicMock(side_effect=OSError("disk full")))
        ctx = testing.Context(LandscapeTaskHandlerCharm)
        certs = testing.Relation(
            endpoint="certificates", interface="tls-certificates", remote_app_name="ssc"
        )
        state_in = testing.State(relations={certs})

        state_out = ctx.run(ctx.on.update_status(), state_in)

        assert state_out.unit_status == testing.BlockedStatus(
            "Failed to configure gRPC certificates"
        )

    def test_certificate_available_handler(
        self, mock_snap: MagicMock, monkeypatch: pytest.MonkeyPatch
    ):
        """The certificate-available handler reconciles."""
        mock_snap.present = True
        mock_snap.get.return_value = None
        self._patch_certs(monkeypatch, MagicMock())
        ctx = testing.Context(LandscapeTaskHandlerCharm)
        with ctx(ctx.on.update_status(), testing.State()) as manager:
            manager.charm._on_certificate_available(MagicMock())
            manager.run()

    def test_leader_elected_publishes(self, mock_snap: MagicMock, monkeypatch: pytest.MonkeyPatch):
        """Leadership change re-publishes the outbox bundle."""
        mock_snap.present = True
        self._patch_certs(monkeypatch, MagicMock())
        ctx = testing.Context(LandscapeTaskHandlerCharm)
        relation = testing.Relation(
            endpoint="landscape-server",
            interface="landscape-task-handler",
            remote_app_name="landscape-server",
        )
        state_in = testing.State(leader=True, relations={relation})

        state_out = ctx.run(ctx.on.leader_elected(), state_in)

        app_data = dict(next(iter(state_out.relations)).local_app_data)
        assert app_data["grpc-address"] == "10.0.0.9:50051"


class TestActions:
    def test_show_config(self, mock_snap: MagicMock):
        """show-config returns the redacted snap configuration."""
        mock_snap.present = True
        mock_snap.get.return_value = {"plain": "v", "password": "secret"}
        ctx = testing.Context(LandscapeTaskHandlerCharm)

        ctx.run(ctx.on.action("show-config"), testing.State())

        assert ctx.action_results == {"plain": "v", "password": "<redacted>"}

    def test_show_config_fail(self, mock_snap: MagicMock):
        """A snap error while reading config fails the action."""
        mock_snap.present = True
        mock_snap.get.side_effect = snap.SnapError("boom")
        ctx = testing.Context(LandscapeTaskHandlerCharm)

        with pytest.raises(testing.ActionFailed):
            ctx.run(ctx.on.action("show-config"), testing.State())

    def test_check_health_healthy(self, mock_snap: MagicMock):
        """check-health reports healthy when services are active."""
        mock_snap.present = True
        mock_snap.services = {"server": {"active": True}, "worker": {"active": True}}
        ctx = testing.Context(LandscapeTaskHandlerCharm)

        ctx.run(ctx.on.action("check-health"), testing.State())

        assert ctx.action_results is not None
        assert ctx.action_results["healthy"] is True

    def test_check_health_unhealthy_fails(self, mock_snap: MagicMock):
        """check-health fails the action when the snap is not installed."""
        mock_snap.present = False
        ctx = testing.Context(LandscapeTaskHandlerCharm)

        with pytest.raises(testing.ActionFailed):
            ctx.run(ctx.on.action("check-health"), testing.State())

    def test_check_health_snap_error(self, monkeypatch: pytest.MonkeyPatch):
        """A snap error while checking health fails the action."""
        monkeypatch.setattr(
            "landscape_task_handler.check_health", MagicMock(side_effect=snap.SnapError("boom"))
        )
        ctx = testing.Context(LandscapeTaskHandlerCharm)

        with pytest.raises(testing.ActionFailed):
            ctx.run(ctx.on.action("check-health"), testing.State())

    def test_show_version(self, mock_snap: MagicMock):
        """show-version returns snap version information."""
        mock_snap.present = True
        mock_snap.revision = "9"
        mock_snap.version = "1.0"
        mock_snap.channel = "latest/stable"
        ctx = testing.Context(LandscapeTaskHandlerCharm)

        ctx.run(ctx.on.action("show-version"), testing.State())

        assert ctx.action_results is not None
        assert ctx.action_results["revision"] == "9"

    def test_show_version_fail(self, monkeypatch: pytest.MonkeyPatch):
        """A snap error while reading version fails the action."""
        monkeypatch.setattr(
            "landscape_task_handler.get_version_info",
            MagicMock(side_effect=snap.SnapError("boom")),
        )
        ctx = testing.Context(LandscapeTaskHandlerCharm)

        with pytest.raises(testing.ActionFailed):
            ctx.run(ctx.on.action("show-version"), testing.State())

    def test_restart_snap(self, mock_snap: MagicMock):
        """restart-snap restarts the workload."""
        mock_snap.present = True
        ctx = testing.Context(LandscapeTaskHandlerCharm)

        ctx.run(ctx.on.action("restart-snap"), testing.State())

        assert ctx.action_results == {"restarted": True}
        mock_snap.restart.assert_called_once()

    def test_restart_snap_fail(self, mock_snap: MagicMock):
        """A snap failure fails the restart action."""
        mock_snap.present = False
        ctx = testing.Context(LandscapeTaskHandlerCharm)

        with pytest.raises(testing.ActionFailed):
            ctx.run(ctx.on.action("restart-snap"), testing.State())


class TestRoutableAddress:
    def test_no_binding(self, mock_snap: MagicMock, monkeypatch: pytest.MonkeyPatch):
        """No network binding yields no routable address."""
        ctx = testing.Context(LandscapeTaskHandlerCharm)
        with ctx(ctx.on.update_status(), testing.State()) as manager:
            monkeypatch.setattr(manager.charm.model, "get_binding", lambda name: None)
            assert manager.charm._routable_address() is None

    def test_binding_model_error(self, mock_snap: MagicMock, monkeypatch: pytest.MonkeyPatch):
        """A ModelError while reading the binding address yields no routable address."""
        ctx = testing.Context(LandscapeTaskHandlerCharm)
        with ctx(ctx.on.update_status(), testing.State()) as manager:
            binding = MagicMock()
            type(binding.network).bind_address = PropertyMock(side_effect=ops.ModelError())
            monkeypatch.setattr(manager.charm.model, "get_binding", lambda name: binding)
            assert manager.charm._routable_address() is None

    def test_binding_no_address(self, mock_snap: MagicMock, monkeypatch: pytest.MonkeyPatch):
        """A binding without a bind address yields no routable address."""
        ctx = testing.Context(LandscapeTaskHandlerCharm)
        with ctx(ctx.on.update_status(), testing.State()) as manager:
            binding = MagicMock()
            binding.network.bind_address = None
            monkeypatch.setattr(manager.charm.model, "get_binding", lambda name: binding)
            assert manager.charm._routable_address() is None


class TestModuleFunctions:
    def test_start_not_present_raises(self, mock_snap: MagicMock):
        mock_snap.present = False
        with pytest.raises(snap.SnapNotFoundError):
            landscape_task_handler.start()

    def test_restart_not_present_raises(self, mock_snap: MagicMock):
        mock_snap.present = False
        with pytest.raises(snap.SnapNotFoundError):
            landscape_task_handler.restart()

    def test_restart_present(self, mock_snap: MagicMock):
        mock_snap.present = True
        landscape_task_handler.restart()
        mock_snap.restart.assert_called_once()

    def test_install_already_present_no_refresh(self, mock_snap: MagicMock):
        mock_snap.present = True
        landscape_task_handler.install()
        mock_snap.ensure.assert_not_called()

    def test_install_error_reraises(self, mock_snap: MagicMock):
        mock_snap.present = False
        mock_snap.ensure.side_effect = snap.SnapError("boom")
        with pytest.raises(snap.SnapError):
            landscape_task_handler.install()

    def test_configure_task_db_unchanged_no_restart(self, mock_snap: MagicMock):
        mock_snap.present = True
        mock_snap.get.return_value = {
            "landscape": {
                "database": {
                    "task-handler": {
                        "host": "h",
                        "port": "5432",
                        "name": "task-handler",
                        "user": "u",
                        "password": "p",
                        "ssl": "disable",
                    }
                }
            }
        }
        landscape_task_handler.configure_task_db("h", "5432", "u", "p", "task-handler", "disable")
        mock_snap.set.assert_not_called()
        mock_snap.restart.assert_not_called()

    def test_configure_stores_unchanged_no_restart(self, mock_snap: MagicMock):
        mock_snap.present = True
        section = {
            "host": "h",
            "port": "5432",
            "user": "u",
            "password": "p",
            "ssl": "disable",
        }
        mock_snap.get.return_value = {
            "landscape": {
                "database": {
                    "main": {**section, "name": "m"},
                    "account": {**section, "name": "a"},
                    "resource": {**section, "name": "r"},
                }
            }
        }
        landscape_task_handler.configure_stores("h", "5432", "u", "p", "m", "a", "r", "disable")
        mock_snap.set.assert_not_called()
        mock_snap.restart.assert_not_called()

    def test_configure_stores_with_ssl_certs(self, mock_snap: MagicMock):
        mock_snap.present = True
        mock_snap.get.return_value = None
        landscape_task_handler.configure_stores(
            "h",
            "5432",
            "u",
            "p",
            "m",
            "a",
            "r",
            "require",
            ssl_root_cert="ROOT",
            ssl_cert="CERT",
            ssl_key="KEY",
        )
        cfg = mock_snap.set.call_args[0][0]
        assert cfg["landscape.database.main.ssl-root-cert"] == "ROOT"
        assert cfg["landscape.database.main.ssl-cert"] == "CERT"
        assert cfg["landscape.database.main.ssl-key"] == "KEY"

    def test_configure_snap_error_on_get_treated_empty(self, mock_snap: MagicMock):
        mock_snap.present = True
        mock_snap.get.side_effect = snap.SnapError("no config")
        landscape_task_handler.configure_task_db("h", "5432", "u", "p", "d", "disable")
        mock_snap.set.assert_called_once()
        mock_snap.restart.assert_called_once()

    def test_configure_grpc_with_certs_dir(self, mock_snap: MagicMock):
        mock_snap.present = True
        mock_snap.get.return_value = None
        landscape_task_handler.configure_grpc(host="1.2.3.4", certs_dir="/custom")
        cfg = mock_snap.set.call_args[0][0]
        assert cfg["landscape.task-handler.grpc-certs-dir"] == "/custom"

    def test_get_version_present(self, mock_snap: MagicMock):
        mock_snap.present = True
        mock_snap.revision = "7"
        assert landscape_task_handler.get_version() == "7"

    def test_get_version_not_present(self, mock_snap: MagicMock):
        mock_snap.present = False
        assert landscape_task_handler.get_version() is None

    def test_get_version_snap_error(self, monkeypatch: pytest.MonkeyPatch):
        def _raise():
            raise snap.SnapError("boom")

        monkeypatch.setattr("landscape_task_handler.snap.SnapCache", _raise)
        assert landscape_task_handler.get_version() is None

    def test_get_version_info_present(self, mock_snap: MagicMock):
        mock_snap.present = True
        mock_snap.revision = "7"
        mock_snap.version = "1.0"
        mock_snap.channel = "latest/stable"
        info = landscape_task_handler.get_version_info()
        assert info == {
            "installed": True,
            "revision": "7",
            "version": "1.0",
            "channel": "latest/stable",
        }

    def test_get_version_info_not_present(self, mock_snap: MagicMock):
        mock_snap.present = False
        info = landscape_task_handler.get_version_info()
        assert info["installed"] is False
        assert info["revision"] is None

    def test_get_config_not_present(self, mock_snap: MagicMock):
        mock_snap.present = False
        assert landscape_task_handler.get_config() == {}

    def test_get_config_redacts(self, mock_snap: MagicMock):
        mock_snap.present = True
        mock_snap.get.return_value = {
            "password": "s3cret",
            "nested": {"secret": "x", "keep": "v"},
            "plain": "y",
        }
        cfg = landscape_task_handler.get_config()
        assert cfg["password"] == "<redacted>"
        assert cfg["nested"]["secret"] == "<redacted>"
        assert cfg["nested"]["keep"] == "v"
        assert cfg["plain"] == "y"

    def test_check_health_active(self, mock_snap: MagicMock):
        mock_snap.present = True
        mock_snap.services = {"server": {"active": True}, "worker": {"active": True}}
        result = landscape_task_handler.check_health()
        assert result["healthy"] is True
        assert result["installed"] is True

    def test_check_health_inactive(self, mock_snap: MagicMock):
        mock_snap.present = True
        mock_snap.services = {"server": {"active": True}, "worker": {"active": False}}
        result = landscape_task_handler.check_health()
        assert result["healthy"] is False
        assert "worker" in str(result["message"])

    def test_check_health_not_installed(self, mock_snap: MagicMock):
        mock_snap.present = False
        result = landscape_task_handler.check_health()
        assert result["healthy"] is False
        assert "not installed" in str(result["message"])

    def test_is_installed(self, mock_snap: MagicMock):
        mock_snap.present = True
        assert landscape_task_handler.is_installed() is True

    def test_stores_configured_true(self, mock_snap: MagicMock):
        mock_snap.present = True
        mock_snap.get.return_value = {
            "landscape": {
                "database": {
                    "main": {"host": "h"},
                    "account": {"host": "h"},
                    "resource": {"host": "h"},
                }
            }
        }
        assert landscape_task_handler.stores_configured() is True

    def test_stores_configured_false_missing(self, mock_snap: MagicMock):
        mock_snap.present = True
        mock_snap.get.return_value = {"landscape": {"database": {"main": {"host": "h"}}}}
        assert landscape_task_handler.stores_configured() is False

    def test_task_db_configured_true(self, mock_snap: MagicMock):
        mock_snap.present = True
        mock_snap.get.return_value = {"landscape": {"database": {"task-handler": {"host": "h"}}}}
        assert landscape_task_handler.task_db_configured() is True

    def test_has_config_keys_not_present(self, mock_snap: MagicMock):
        mock_snap.present = False
        assert landscape_task_handler.stores_configured() is False

    def test_has_config_keys_snap_error(self, mock_snap: MagicMock):
        mock_snap.present = True
        mock_snap.get.side_effect = snap.SnapError("boom")
        assert landscape_task_handler.stores_configured() is False

    def test_snap_config_matches_bool(self, mock_snap: MagicMock):
        """A bool value in the current snap config compares against its string form."""
        mock_snap.present = True
        mock_snap.get.return_value = {
            "landscape": {
                "database": {
                    "task-handler": {
                        "host": "h",
                        "port": "5432",
                        "name": "d",
                        "user": "u",
                        "password": "p",
                        "ssl": True,
                    }
                }
            }
        }
        # ssl is stored as the bool True; desired "true" matches it, so nothing changes.
        landscape_task_handler.configure_task_db("h", "5432", "u", "p", "d", "true")
        mock_snap.set.assert_not_called()


class TestHaproxyRouteBranches:
    def test_grpc_route_ready(self, monkeypatch: pytest.MonkeyPatch):
        """The haproxy-route-tcp ready event re-provisions and reconciles."""
        monkeypatch.setattr(
            "charm.LandscapeTaskHandlerCharm._routable_address", lambda self: "10.2.2.2"
        )
        ctx = testing.Context(LandscapeTaskHandlerCharm)
        grpc = testing.Relation(
            endpoint="grpc-haproxy-route",
            interface="haproxy-route-tcp",
            remote_app_name="haproxy",
            remote_app_data={"endpoints": '["landscape.local:50053"]'},
        )
        stored = testing.StoredState(
            owner_path="LandscapeTaskHandlerCharm",
            content={"hostname": "landscape.example.com"},
        )
        state_in = testing.State(leader=True, relations={grpc}, stored_states={stored})

        state_out = ctx.run(ctx.on.relation_changed(grpc), state_in)

        app_data = dict(next(iter(state_out.relations)).local_app_data)
        assert "landscape.example.com" in "".join(app_data.values())

    def test_haproxy_route_no_routable_skips(self, monkeypatch: pytest.MonkeyPatch):
        """A stored hostname but no routable address publishes nothing."""
        monkeypatch.setattr("charm.LandscapeTaskHandlerCharm._routable_address", lambda self: None)
        ctx = testing.Context(LandscapeTaskHandlerCharm)
        grpc = testing.Relation(
            endpoint="grpc-haproxy-route",
            interface="haproxy-route-tcp",
            remote_app_name="haproxy",
        )
        stored = testing.StoredState(
            owner_path="LandscapeTaskHandlerCharm",
            content={"hostname": "landscape.example.com"},
        )
        state_in = testing.State(leader=True, relations={grpc}, stored_states={stored})

        state_out = ctx.run(ctx.on.relation_joined(grpc), state_in)

        assert dict(next(iter(state_out.relations)).local_app_data) == {}


class TestOutboxCertPublishingBranches:
    @staticmethod
    def _patch_client_cert(monkeypatch, routable="10.1.1.9"):
        monkeypatch.setattr(
            "charm.LandscapeTaskHandlerCharm._routable_address", lambda self: routable
        )
        monkeypatch.setattr(
            "charm.TLSCertificatesRequiresV4.get_assigned_certificate",
            lambda self, certificate_request: (
                _FakeProviderCertificate("CA-PEM", "CLIENT-PEM"),
                _FakePem("CLIENT-KEY-PEM"),
            ),
        )

    @staticmethod
    def _stores_with_local(**local):
        return testing.Relation(
            endpoint="landscape-server",
            interface="landscape-task-handler",
            remote_app_name="landscape-server",
            local_app_data=local,
        )

    def test_ingress_endpoint_used(self, monkeypatch: pytest.MonkeyPatch):
        """When routed through haproxy the advertised address uses the frontend port."""
        self._patch_client_cert(monkeypatch)
        ctx = testing.Context(LandscapeTaskHandlerCharm)
        stores = self._stores_with_local()
        grpc = testing.Relation(
            endpoint="grpc-haproxy-route",
            interface="haproxy-route-tcp",
            remote_app_name="haproxy",
        )
        stored = testing.StoredState(
            owner_path="LandscapeTaskHandlerCharm",
            content={"hostname": "landscape.example.com"},
        )
        state_in = testing.State(leader=True, relations={stores, grpc}, stored_states={stored})

        state_out = ctx.run(ctx.on.relation_changed(stores), state_in)

        stores_out = next(r for r in state_out.relations if r.endpoint == "landscape-server")
        assert dict(stores_out.local_app_data)["grpc-address"] == "landscape.example.com:50053"

    def test_cert_not_available(self, monkeypatch: pytest.MonkeyPatch):
        """No issued client certificate: nothing is published."""
        monkeypatch.setattr(
            "charm.TLSCertificatesRequiresV4.get_assigned_certificate",
            lambda self, certificate_request: (None, None),
        )
        ctx = testing.Context(LandscapeTaskHandlerCharm)
        stores = self._stores_with_local()
        state_in = testing.State(leader=True, relations={stores})

        state_out = ctx.run(ctx.on.leader_elected(), state_in)

        assert dict(next(iter(state_out.relations)).local_app_data) == {}

    def test_no_endpoint_address(self, monkeypatch: pytest.MonkeyPatch):
        """A cert but no routable endpoint: nothing is published."""
        self._patch_client_cert(monkeypatch, routable=None)
        ctx = testing.Context(LandscapeTaskHandlerCharm)
        stores = self._stores_with_local()
        state_in = testing.State(leader=True, relations={stores})

        state_out = ctx.run(ctx.on.leader_elected(), state_in)

        assert dict(next(iter(state_out.relations)).local_app_data) == {}

    def test_secret_unchanged(self, monkeypatch: pytest.MonkeyPatch):
        """An existing secret with identical content is not rotated."""
        self._patch_client_cert(monkeypatch)
        ctx = testing.Context(LandscapeTaskHandlerCharm)
        bundle = {"ca-cert": "CA-PEM", "client-cert": "CLIENT-PEM", "client-key": "CLIENT-KEY-PEM"}
        secret = testing.Secret(tracked_content=bundle, owner="app")
        stores = self._stores_with_local(
            **{"certs-secret-id": secret.id, "certs-revision": "3", "grpc-address": "old"}
        )
        state_in = testing.State(leader=True, relations={stores}, secrets={secret})

        state_out = ctx.run(ctx.on.leader_elected(), state_in)

        out = dict(next(iter(state_out.relations)).local_app_data)
        assert out["certs-revision"] == "3"

    def test_secret_rotation(self, monkeypatch: pytest.MonkeyPatch):
        """An existing secret with different content is rotated and the revision bumped."""
        self._patch_client_cert(monkeypatch)
        ctx = testing.Context(LandscapeTaskHandlerCharm)
        secret = testing.Secret(
            tracked_content={"ca-cert": "OLD", "client-cert": "OLD", "client-key": "OLD"},
            owner="app",
        )
        stores = self._stores_with_local(**{"certs-secret-id": secret.id, "certs-revision": "3"})
        state_in = testing.State(leader=True, relations={stores}, secrets={secret})

        state_out = ctx.run(ctx.on.leader_elected(), state_in)

        out = dict(next(iter(state_out.relations)).local_app_data)
        assert out["certs-revision"] == "4"

    def test_secret_not_found_recreated(self, monkeypatch: pytest.MonkeyPatch):
        """A dangling secret-id is recreated."""
        self._patch_client_cert(monkeypatch)
        ctx = testing.Context(LandscapeTaskHandlerCharm)
        stores = self._stores_with_local(
            **{"certs-secret-id": "secret:doesnotexist", "certs-revision": "3"}
        )
        state_in = testing.State(leader=True, relations={stores})

        state_out = ctx.run(ctx.on.leader_elected(), state_in)

        out = dict(next(iter(state_out.relations)).local_app_data)
        assert out["certs-revision"] == "4"
        assert out["certs-secret-id"] != "secret:doesnotexist"


class TestWorkloadVersion:
    def test_set_workload_version_none(
        self, mock_snap: MagicMock, monkeypatch: pytest.MonkeyPatch
    ):
        """When the snap has no reported version, the workload version is left unset."""
        mock_snap.present = True
        monkeypatch.setattr("landscape_task_handler.get_version", lambda: None)
        ctx = testing.Context(LandscapeTaskHandlerCharm)

        state_out = ctx.run(ctx.on.config_changed(), testing.State())

        assert state_out.workload_version == ""
