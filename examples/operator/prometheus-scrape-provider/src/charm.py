#!/usr/bin/env python3
# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.

"""A Charm to functionally test the prometheus-scrape interface."""

import logging
import subprocess

from ops.charm import CharmBase
from ops.main import main
from ops.model import ActiveStatus, MaintenanceStatus

from prometheus_scrape import MetricsEndpointProvider

logger = logging.getLogger(__name__)


class PrometheusScrapeProviderCharm(CharmBase):
    """A Charm used to test the provided relation with prometheus-scrape interface."""

    def __init__(self, *args):
        super().__init__(*args)
        jobs = [
            {
                "scrape_interval": self.model.config["scrape-interval"],
                "static_configs": [
                    {"targets": ["*:9090"], "labels": {"name": self.unit.name}}
                ],
            }
        ]
        self.prometheus = MetricsEndpointProvider(self, jobs=jobs)
        self.framework.observe(self.on.install, self._on_install)

        self.unit.status = ActiveStatus("unit is ready")

    def _on_install(self, _):
        """Install Prometheus via snap."""
        self.unit.status = MaintenanceStatus("Installing prometheus via snap")
        subprocess.check_call(["snap", "install", "prometheus"])
        self.unit.status = ActiveStatus("unit is ready")


if __name__ == "__main__":
    main(PrometheusScrapeProviderCharm)
