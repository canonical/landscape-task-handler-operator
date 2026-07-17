#!/usr/bin/env python3

# Copyright 2026-2026 Canonical Limited. All rights reserved.

"""Charm the landscape-task-handler snap."""

import logging
import socket

import ops
from charmlibs import snap
from charmlibs.interfaces.tls_certificates import (
    CertificateAvailableEvent,
    CertificateRequestAttributes,
    Mode,
    TLSCertificatesRequiresV4,
)
from charms.data_platform_libs.v0.data_interfaces import DatabaseRequires
from charms.haproxy.v1.haproxy_route_tcp import HaproxyRouteTcpRequirer

import landscape_task_handler

logger = logging.getLogger(__name__)

TASK_DB_RELATION = "task-db"
TASK_DB_NAME = "task-handler"

LANDSCAPE_SERVER_RELATION = "landscape-server"
CERTIFICATES_RELATION = "certificates"
GRPC_HAPROXY_ROUTE_RELATION = "grpc-haproxy-route"

SERVER_CERT_COMMON_NAME = "landscape-task-handler"
CLIENT_CERT_COMMON_NAME = "landscape-outbox"

STORES_SECRET_ID_KEY = "secret-id"
STORES_PASSWORD_FIELD = "password"

LANDSCAPE_HOSTNAME_KEY = "hostname"

GRPC_ADDRESS_KEY = "grpc-address"
CERTS_SECRET_ID_KEY = "certs-secret-id"
CERTS_REVISION_KEY = "certs-revision"


class LandscapeTaskHandlerCharm(ops.CharmBase):
    """Operate the landscape-task-handler snap.

    Installs and manages the snap, configures its four databases (its own task DB
    plus the shared Landscape main/account/resource stores from landscape-server),
    exposes its mTLS gRPC service to the outbox through a TLS-passthrough haproxy
    route, and exchanges certificates and the gRPC address with landscape-server.
    """

    _stored = ops.StoredState()

    def __init__(self, framework: ops.Framework):
        super().__init__(framework)

        self._stored.set_default(hostname=None)

        self.task_db = DatabaseRequires(
            self, relation_name=TASK_DB_RELATION, database_name=TASK_DB_NAME
        )

        self.grpc_haproxy_route = HaproxyRouteTcpRequirer(
            self, relation_name=GRPC_HAPROXY_ROUTE_RELATION
        )

        self.certificates = TLSCertificatesRequiresV4(
            charm=self,
            relationship_name=CERTIFICATES_RELATION,
            certificate_requests=self._certificate_requests(),
            mode=Mode.UNIT,
            refresh_events=[self.on.update_status],
        )

        framework.observe(self.on.install, self._on_install)
        framework.observe(self.on.start, self._on_start)
        framework.observe(self.on.upgrade_charm, self._on_upgrade_charm)
        framework.observe(self.on.config_changed, self._on_config_changed)
        framework.observe(self.on.update_status, self._on_update_status)

        framework.observe(self.task_db.on.database_created, self._on_task_db_changed)
        framework.observe(self.task_db.on.endpoints_changed, self._on_task_db_changed)

        framework.observe(
            self.on[LANDSCAPE_SERVER_RELATION].relation_changed, self._on_landscape_server_changed
        )
        framework.observe(
            self.on[LANDSCAPE_SERVER_RELATION].relation_broken, self._on_landscape_server_broken
        )
        framework.observe(self.on.leader_elected, self._on_leader_elected)

        framework.observe(
            self.certificates.on.certificate_available, self._on_certificate_available
        )

        framework.observe(
            self.on[GRPC_HAPROXY_ROUTE_RELATION].relation_joined,
            self._on_grpc_haproxy_route_changed,
        )
        framework.observe(
            self.on[GRPC_HAPROXY_ROUTE_RELATION].relation_changed,
            self._on_grpc_haproxy_route_changed,
        )
        framework.observe(self.grpc_haproxy_route.on.ready, self._on_grpc_haproxy_route_ready)
        framework.observe(self.grpc_haproxy_route.on.removed, self._on_grpc_haproxy_route_ready)

        framework.observe(self.on.show_config_action, self._on_show_config_action)
        framework.observe(self.on.check_health_action, self._on_check_health_action)
        framework.observe(self.on.show_version_action, self._on_show_version_action)
        framework.observe(self.on.restart_snap_action, self._on_restart_snap_action)

    @property
    def _snap_channel(self) -> str:
        """Return the configured snap channel."""
        return str(self.config["task-handler-snap-channel"])

    @property
    def _grpc_external_port(self) -> int:
        """Return the haproxy frontend port the outbox dials for the gRPC service.

        Distinct from the snap's backend listen port (50051) so the task-handler
        can coexist with other haproxy-route gRPC services that share the same
        Landscape hostname (for example landscape-server's ubuntu-installer-attach,
        which also uses 50051).
        """
        return int(self.config["grpc-external-port"])

    def _on_install(self, event: ops.InstallEvent) -> None:
        """Install the workload snap on the machine."""
        self.unit.status = ops.MaintenanceStatus("installing task-handler snap")
        try:
            landscape_task_handler.install(channel=self._snap_channel)
        except (snap.SnapError, snap.SnapNotFoundError):
            logger.exception("failed to install landscape-task-handler snap")
            self.unit.status = ops.BlockedStatus("Failed to install task-handler snap")

    def _on_start(self, event: ops.StartEvent) -> None:
        """Start the workload snap."""
        self.unit.status = ops.MaintenanceStatus("starting workload")
        try:
            landscape_task_handler.start()
        except (snap.SnapError, snap.SnapNotFoundError):
            logger.exception("failed to start landscape-task-handler snap")
            self.unit.status = ops.BlockedStatus("Failed to start task-handler snap")
            return
        self._set_workload_version()
        self._reconcile()

    def _on_upgrade_charm(self, event: ops.UpgradeCharmEvent) -> None:
        """Refresh the snap to the configured channel on charm upgrade."""
        self.unit.status = ops.MaintenanceStatus("refreshing workload snap")
        try:
            landscape_task_handler.refresh(channel=self._snap_channel)
        except (snap.SnapError, snap.SnapNotFoundError):
            logger.exception("failed to refresh landscape-task-handler snap")
            self.unit.status = ops.BlockedStatus("Failed to refresh task-handler snap")
            return
        self._set_workload_version()
        self._reconcile()

    def _on_config_changed(self, event: ops.ConfigChangedEvent) -> None:
        """Refresh the snap when the configured channel changes."""
        self.unit.status = ops.MaintenanceStatus("applying configuration")
        try:
            landscape_task_handler.refresh(channel=self._snap_channel)
        except (snap.SnapError, snap.SnapNotFoundError):
            logger.exception("failed to apply landscape-task-handler configuration")
            self.unit.status = ops.BlockedStatus("Failed to apply configuration")
            return
        self._set_workload_version()
        self._reconcile()

    def _on_update_status(self, event: ops.UpdateStatusEvent) -> None:
        """Periodically re-apply configuration so the workload self-heals."""
        self._reconcile()

    def _on_task_db_changed(self, event) -> None:
        """Reconcile when the task-db relation publishes new database credentials."""
        self._reconcile()

    def _on_landscape_server_changed(self, event: ops.RelationChangedEvent) -> None:
        """Reconcile when landscape-server updates the landscape-server relation."""
        self._reconcile()

    def _reconcile(self) -> None:
        """Re-apply all workload configuration from the current relation data.

        Safe to run on any event (start, upgrade, config-changed, relation
        events, update-status). Each step is a no-op when its relation data is
        unavailable, so the charm self-heals after a charm refresh or snap
        reinstall without needing the relation hooks to re-fire.
        """
        self._register_landscape_hostname()
        self._provide_haproxy_route_requirements()
        self._publish_outbox_certificates()

        applied = True
        if landscape_task_handler.is_installed():
            applied &= self._apply_task_db_config()
            applied &= self._apply_stores_config()
            applied &= self._apply_runtime_config()
            applied &= self._apply_grpc_certificates()

        if applied:
            self._evaluate_status()

    def _apply_task_db_config(self) -> bool:
        """Apply the task-handler's own database config from the task-db relation.

        Returns False (and sets BlockedStatus) only when a snap operation fails;
        a missing or incomplete relation is treated as nothing to do.
        """
        params = self._task_db_params()
        if params is None:
            return True
        host, port, username, password, database, ssl = params
        self.unit.status = ops.MaintenanceStatus("Configuring task database...")
        try:
            landscape_task_handler.configure_task_db(host, port, username, password, database, ssl)
        except (snap.SnapError, snap.SnapNotFoundError):
            logger.exception("failed to configure task-handler database")
            self.unit.status = ops.BlockedStatus("Failed to configure task database")
            return False
        return True

    def _apply_stores_config(self) -> bool:
        """Apply the shared Landscape store DB config from the landscape-server relation.

        Fans the single stores block out into the main/account/resource snap DB
        sections. Returns False when the workload cannot be configured: either a
        snap operation failed or a published stores password could not be
        resolved. In both cases a BlockedStatus has already been set (here or by
        ``_resolve_stores_password``). A missing relation, an incomplete databag,
        or a not-yet-published stores secret is treated as nothing to do and
        returns True.
        """
        relation = self.model.get_relation(LANDSCAPE_SERVER_RELATION)
        if relation is None or relation.app is None:
            return True
        app_data = relation.data[relation.app]

        host = app_data.get("host")
        port = app_data.get("port")
        user = app_data.get("user")
        main = app_data.get("main")
        account = app_data.get("account_1") or app_data.get("account-1")
        resource = app_data.get("resource_1") or app_data.get("resource-1")
        ssl = app_data.get("sslmode", "disable")
        ssl_root_cert = app_data.get("sslrootcert")
        ssl_cert = app_data.get("sslcert")
        ssl_key = app_data.get("sslkey")

        if (
            host is None
            or port is None
            or user is None
            or main is None
            or account is None
            or resource is None
        ):
            logger.info("landscape-server relation has not published all store fields yet")
            return True

        password = self._resolve_stores_password(app_data)
        if password is None:
            return app_data.get(STORES_SECRET_ID_KEY) is None

        self.unit.status = ops.MaintenanceStatus("Configuring Landscape stores...")
        try:
            landscape_task_handler.configure_stores(
                host,
                port,
                user,
                password,
                main,
                account,
                resource,
                ssl,
                ssl_root_cert=ssl_root_cert,
                ssl_cert=ssl_cert,
                ssl_key=ssl_key,
            )
        except (snap.SnapError, snap.SnapNotFoundError):
            logger.exception("failed to configure Landscape stores")
            self.unit.status = ops.BlockedStatus("Failed to configure Landscape stores")
            return False
        return True

    def _apply_runtime_config(self) -> bool:
        """Apply the logging, worker and cleanup runtime settings from charm config.

        These are plain charm configuration (not relation data), so they apply
        whenever the snap is installed. Only options the operator has explicitly
        set are pushed; unset options fall back to the snap's own defaults.
        Returns False (and sets BlockedStatus) only when a snap operation fails.
        """
        try:
            landscape_task_handler.configure_runtime(dict(self.config))
        except (snap.SnapError, snap.SnapNotFoundError):
            logger.exception("failed to apply runtime configuration")
            self.unit.status = ops.BlockedStatus("Failed to apply runtime configuration")
            return False
        return True

    def _apply_grpc_certificates(self) -> bool:
        """Provision the gRPC mTLS certificates and point the snap at them.

        The server certificate and the outbox client certificate are requested
        from the same provider (so they share a CA) and handed to the snap as
        operator-provided "custom" material. The snap adopts them into its active
        tree and stops auto-rotating, so it never clobbers the charm-issued
        certificates. Adopting new files rotates the certificate without a
        restart. Returns False (and sets BlockedStatus) on failure.
        """
        server_certificate, server_key = self.certificates.get_assigned_certificate(
            certificate_request=self._server_certificate_request()
        )
        client_certificate, client_key = self.certificates.get_assigned_certificate(
            certificate_request=self._client_certificate_request()
        )
        host = self._routable_address()
        if (
            server_certificate is None
            or server_key is None
            or client_certificate is None
            or client_key is None
            or host is None
        ):
            logger.info("gRPC certificates or routable address are not available yet")
            return True
        self.unit.status = ops.MaintenanceStatus("Configuring gRPC certificates...")
        try:
            landscape_task_handler.write_custom_certificates(
                ca=str(server_certificate.ca),
                server_cert=str(server_certificate.certificate),
                server_key=str(server_key),
                client_cert=str(client_certificate.certificate),
                client_key=str(client_key),
            )
            landscape_task_handler.configure_grpc(host=host)
        except (snap.SnapError, snap.SnapNotFoundError, OSError):
            logger.exception("failed to configure gRPC certificates")
            self.unit.status = ops.BlockedStatus("Failed to configure gRPC certificates")
            return False
        return True

    def _task_db_params(self) -> tuple[str, str, str, str, str, str] | None:
        """Read (host, port, user, password, database, ssl) from the task-db relation.

        Reads the current relation data (not an event) so it can be used from the
        reconcile path too. Returns None when the relation or its credentials are
        not yet available.
        """
        relation = self.model.get_relation(TASK_DB_RELATION)
        if relation is None or relation.app is None:
            return None
        data = self.task_db.fetch_relation_data(
            relation_ids=[relation.id],
            fields=["endpoints", "username", "password", "database", "tls"],
        ).get(relation.id, {})

        endpoints = data.get("endpoints")
        username = data.get("username")
        password = data.get("password")
        database = data.get("database") or TASK_DB_NAME
        if not endpoints or not username or not password:
            return None

        endpoint = endpoints.split(",")[0]
        host, port = endpoint.split(":") if ":" in endpoint else (endpoint, "5432")
        ssl = "require" if str(data.get("tls")).lower() == "true" else "disable"
        return host, port, username, password, database, ssl

    def _on_landscape_server_broken(self, event: ops.RelationBrokenEvent) -> None:
        """Surface that the task-handler can no longer process its tasks."""
        logger.info("landscape-server relation removed; task-handler cannot process tasks")
        self._evaluate_status()

    def _on_certificate_available(self, event: CertificateAvailableEvent) -> None:
        """Write the issued certificates and re-publish the outbox bundle."""
        self._reconcile()

    def _on_leader_elected(self, event: ops.LeaderElectedEvent) -> None:
        """Re-publish the outbox client certificate when leadership changes."""
        self._publish_outbox_certificates()

    def _on_grpc_haproxy_route_changed(
        self, event: ops.RelationJoinedEvent | ops.RelationChangedEvent
    ) -> None:
        """Publish this unit's gRPC backend to the related haproxy charm."""
        self._provide_haproxy_route_requirements()

    def _on_grpc_haproxy_route_ready(self, event: ops.EventBase) -> None:
        """React to the haproxy frontend becoming available (or going away).

        The task-handler is reachable through the haproxy frontend, so the server
        certificate must carry that hostname as a SAN and the outbox must dial
        the frontend address. Re-request the certificate and re-publish the
        outbox endpoint accordingly.
        """
        self._provide_haproxy_route_requirements()
        self.certificates.certificate_requests = self._certificate_requests()
        self.certificates.sync()
        self._reconcile()

    def _provide_haproxy_route_requirements(self) -> None:
        """Publish the gRPC backend requirements to the haproxy-route-tcp provider.

        Uses a TCP frontend with TLS passthrough (``tls_terminate=False``) so the
        outbox's mutual-TLS session terminates at the task-handler unit rather
        than at haproxy. haproxy routes by SNI (the external Landscape hostname
        supplied by landscape-server) and load-balances across task-handler
        units, while the client certificate still reaches the workload. The
        external frontend port differs from the snap's own listen port so it can
        coexist with landscape-server's other gRPC services.
        """
        hostname = self._stored.hostname
        if not hostname:
            logger.info("no Landscape hostname yet; skipping haproxy-route requirements")
            return
        unit_ip = self._routable_address()
        if unit_ip is None:
            logger.info("no routable address yet; skipping haproxy-route requirements")
            return
        self.grpc_haproxy_route.provide_haproxy_route_tcp_requirements(
            port=self._grpc_external_port,
            backend_port=int(landscape_task_handler.DEFAULT_GRPC_PORT),
            sni=hostname,
            enforce_tls=True,
            tls_terminate=False,
            unit_address=unit_ip,
        )

    def _register_landscape_hostname(self) -> None:
        """Store the external Landscape hostname published by landscape-server.

        Reads the current landscape-server relation databag (not an event) so it
        can run from the reconcile path. When the hostname changes, re-register
        with haproxy and re-request the server certificate so it carries the
        hostname as a SAN (the outbox verifies the server name it dials against
        the certificate).
        """
        relation = self.model.get_relation(LANDSCAPE_SERVER_RELATION)
        if relation is None or relation.app is None:
            return
        hostname = relation.data[relation.app].get(LANDSCAPE_HOSTNAME_KEY)
        if not hostname or hostname == self._stored.hostname:
            return
        self._stored.hostname = hostname
        logger.info("stored Landscape hostname: %s", hostname)
        self._provide_haproxy_route_requirements()
        self.certificates.certificate_requests = self._certificate_requests()
        self.certificates.sync()

    def _grpc_ingress_hostname(self) -> str | None:
        """Return the haproxy frontend hostname for the gRPC service, if routed.

        The frontend is the external Landscape hostname supplied by
        landscape-server, so it is only used once the service is routed through
        the grpc-haproxy-route relation.
        """
        if self.model.get_relation(GRPC_HAPROXY_ROUTE_RELATION) is None:
            return None
        return self._stored.hostname or None

    def _grpc_endpoint(self) -> str | None:
        """Return the ``host:port`` the outbox should dial to reach the gRPC server.

        Prefers the haproxy frontend when the service is routed through haproxy,
        otherwise falls back to this unit's routable address. The haproxy path
        uses the external frontend port; the direct path uses the snap's own
        backend listen port.
        """
        ingress = self._grpc_ingress_hostname()
        if ingress:
            return f"{ingress}:{self._grpc_external_port}"
        address = self._routable_address()
        if address:
            return f"{address}:{landscape_task_handler.DEFAULT_GRPC_PORT}"
        return None

    def _publish_outbox_certificates(self) -> None:
        """Publish the outbox client certificate bundle on the landscape-server relation.

        The task-handler requests both its own server certificate and the
        outbox's client certificate from the same provider (so they share a CA).
        The client bundle is placed in a Juju secret granted to the
        landscape-server relation; landscape-server writes it out for the outbox
        snap. Only the leader writes relation data. A ``certs-revision`` counter
        is bumped when the certificate content changes so that a rotation (same
        secret id, new content) still notifies landscape-server via a
        relation-changed event.
        """
        if not self.unit.is_leader():
            return
        relation = self.model.get_relation(LANDSCAPE_SERVER_RELATION)
        if relation is None:
            return

        provider_certificate, private_key = self.certificates.get_assigned_certificate(
            certificate_request=self._client_certificate_request()
        )
        if provider_certificate is None or private_key is None:
            logger.info("outbox client certificate is not available yet")
            return

        address = self._grpc_endpoint()
        if address is None:
            logger.info("no routable gRPC endpoint address yet")
            return

        bundle = {
            "ca-cert": str(provider_certificate.ca),
            "client-cert": str(provider_certificate.certificate),
            "client-key": str(private_key),
        }
        databag = relation.data[self.app]
        secret_id = databag.get(CERTS_SECRET_ID_KEY)
        revision = int(databag.get(CERTS_REVISION_KEY, "0"))

        if secret_id:
            try:
                secret = self.model.get_secret(id=secret_id)
                if secret.get_content(refresh=True) != bundle:
                    secret.set_content(bundle)
                    revision += 1
            except (ops.SecretNotFoundError, ops.ModelError):
                secret = self.app.add_secret(bundle)
                secret_id = secret.id
                revision += 1
        else:
            secret = self.app.add_secret(bundle)
            secret_id = secret.id
            revision = 1

        secret.grant(relation)
        new_secret_id = secret.id
        if new_secret_id is None:  # pragma: no cover - defensive; add_secret always sets an id
            logger.warning("granted certs secret has no id; cannot publish to relation")
            return
        databag[GRPC_ADDRESS_KEY] = address
        databag[CERTS_SECRET_ID_KEY] = new_secret_id
        databag[CERTS_REVISION_KEY] = str(revision)

    def _certificate_requests(self) -> list[CertificateRequestAttributes]:
        """Return the certificate requests published on the certificates relation."""
        return [self._server_certificate_request(), self._client_certificate_request()]

    def _server_certificate_request(self) -> CertificateRequestAttributes:
        """Build the gRPC server certificate request with routable SANs.

        Includes the haproxy frontend hostname (when the service is routed
        through haproxy) so the outbox can verify the server name it dials.
        """
        sans_dns = {socket.getfqdn()}
        ingress = self._grpc_ingress_hostname()
        if ingress:
            sans_dns.add(ingress)
        sans_ip: set[str] = set()
        address = self._routable_address()
        if address:
            sans_ip.add(address)
        return CertificateRequestAttributes(
            common_name=SERVER_CERT_COMMON_NAME,
            sans_dns=frozenset(sans_dns),
            sans_ip=frozenset(sans_ip) or None,
        )

    def _client_certificate_request(self) -> CertificateRequestAttributes:
        """Build the outbox client certificate request (CN only, no SANs)."""
        return CertificateRequestAttributes(common_name=CLIENT_CERT_COMMON_NAME)

    def _routable_address(self) -> str | None:
        """Return the unit's routable IP for the certificates relation binding."""
        binding = self.model.get_binding(CERTIFICATES_RELATION)
        if binding is None:
            return None
        try:
            bind_address = binding.network.bind_address
        except ops.ModelError:
            return None
        return str(bind_address) if bind_address else None

    def _resolve_stores_password(self, app_data) -> str | None:
        """Resolve the store DB password from the Juju secret granted on the relation."""
        secret_id = app_data.get(STORES_SECRET_ID_KEY)
        if not secret_id:
            logger.info(
                "landscape-server relation has not published a %s yet", STORES_SECRET_ID_KEY
            )
            return None

        try:
            secret = self.model.get_secret(id=secret_id)
            content = secret.get_content(refresh=True)
        except (ops.SecretNotFoundError, ops.ModelError):
            logger.warning("no stores password for %s %s", STORES_SECRET_ID_KEY, secret_id)
            self.unit.status = ops.BlockedStatus("no stores password")
            return None

        password = content.get(STORES_PASSWORD_FIELD)
        if not password:
            logger.warning("secret %s does not contain a %s", secret_id, STORES_PASSWORD_FIELD)
            self.unit.status = ops.BlockedStatus("no stores password")
            return None
        return password

    def _set_workload_version(self) -> None:
        """Record the running snap revision as the workload version."""
        version = landscape_task_handler.get_version()
        if version is not None:
            self.unit.set_workload_version(version)

    def _evaluate_status(self) -> None:
        """Set Active, or Waiting when a required relation or its config is absent.

        The unit only reports Active once the workload snap actually holds the
        database configuration, not merely because the relations exist. This
        avoids reporting Active while the snap is unconfigured (for example after
        a charm refresh that has not yet re-applied the relation data). A missing
        workload snap is surfaced as Blocked so a broken install is not masked as
        merely waiting for configuration.
        """
        try:
            installed = landscape_task_handler.is_installed()
        except (snap.SnapError, snap.SnapNotFoundError):
            logger.exception("failed to query task-handler snap state")
            self.unit.status = ops.BlockedStatus("Failed to query task-handler snap")
            return
        if not installed:
            self.unit.status = ops.BlockedStatus("task-handler snap is not installed")
            return

        missing = [
            name
            for name in (LANDSCAPE_SERVER_RELATION, TASK_DB_RELATION)
            if self.model.get_relation(name) is None
        ]
        if missing:
            self.unit.status = ops.WaitingStatus(f"Waiting for relation(s): {', '.join(missing)}")
            return

        unconfigured = []
        if not landscape_task_handler.stores_configured():
            unconfigured.append("stores")
        if not landscape_task_handler.task_db_configured():
            unconfigured.append("task-db")
        if unconfigured:
            self.unit.status = ops.WaitingStatus(
                f"Waiting for database configuration: {', '.join(unconfigured)}"
            )
            return

        self.unit.status = ops.ActiveStatus()

    def _on_show_config_action(self, event: ops.ActionEvent) -> None:
        """Show redacted task-handler snap configuration."""
        try:
            event.set_results(landscape_task_handler.get_config())
        except (snap.SnapError, snap.SnapNotFoundError) as e:
            event.fail(f"Failed to read task-handler configuration: {e}")

    def _on_check_health_action(self, event: ops.ActionEvent) -> None:
        """Check whether the task-handler snap appears healthy."""
        try:
            result = landscape_task_handler.check_health()
        except (snap.SnapError, snap.SnapNotFoundError) as e:
            event.fail(f"Failed to check task-handler health: {e}")
            return

        event.set_results(result)
        if not result["healthy"]:
            event.fail(str(result["message"]))

    def _on_show_version_action(self, event: ops.ActionEvent) -> None:
        """Show task-handler snap version information."""
        try:
            event.set_results(landscape_task_handler.get_version_info())
        except (snap.SnapError, snap.SnapNotFoundError) as e:
            event.fail(f"Failed to read task-handler version: {e}")

    def _on_restart_snap_action(self, event: ops.ActionEvent) -> None:
        """Restart task-handler snap services."""
        try:
            landscape_task_handler.restart()
        except (snap.SnapError, snap.SnapNotFoundError) as e:
            event.fail(f"Failed to restart task-handler snap: {e}")
            return
        event.set_results({"restarted": True})


if __name__ == "__main__":  # pragma: nocover
    ops.main(LandscapeTaskHandlerCharm)
