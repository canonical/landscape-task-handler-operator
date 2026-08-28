#!/usr/bin/env python3

# Copyright 2026-2026 Canonical Limited. All rights reserved.

"""Fake landscape-server charm: test double for integration tests only.

Publishes the same `landscape-server` relation data shape the real
landscape-server charm does for the shared Landscape stores, using a
PgBouncer-style `localhost` host/port to reproduce the real-world bug
scenario the task-handler charm needs to work around.
"""

import logging

import ops

logger = logging.getLogger(__name__)

RELATION_NAME = "landscape-server"


class FakeLandscapeServerCharm(ops.CharmBase):
    """Publish fake shared-store relation data for task-handler integration tests."""

    def __init__(self, framework: ops.Framework):
        super().__init__(framework)
        framework.observe(self.on.install, self._publish)
        framework.observe(self.on[RELATION_NAME].relation_joined, self._on_relation_joined)
        framework.observe(self.on.config_changed, self._publish)

    def _on_relation_joined(self, event: ops.RelationJoinedEvent) -> None:
        self._publish(event)

    def _publish(self, event: ops.EventBase) -> None:
        if not self.unit.is_leader():
            return
        for relation in self.model.relations.get(RELATION_NAME, []):
            secret = self.app.add_secret({"password": "fakestorespw"})
            secret.grant(relation)
            relation.data[self.app].update(
                {
                    # A PgBouncer-style loopback address: only reachable from
                    # this fake unit's own machine, reproducing the real bug.
                    "host": "localhost",
                    "port": "6432",
                    "user": "landscape",
                    "main": "landscape-fake-main",
                    "account_1": "landscape-fake-account-1",
                    "resource_1": "landscape-fake-resource-1",
                    "sslmode": "disable",
                    "secret-id": secret.id,
                }
            )
        self.unit.status = ops.ActiveStatus()


if __name__ == "__main__":
    ops.main(FakeLandscapeServerCharm)
