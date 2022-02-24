#!/usr/bin/env python3
# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.

"""A Charm to functionally test the prometheus-scrape interface."""

import logging
import subprocess
from pathlib import Path

import yaml
from ops.charm import CharmBase
from ops.main import main
from ops.model import ActiveStatus, BlockedStatus, MaintenanceStatus

from prometheus_scrape import MetricsEndpointConsumer

logger = logging.getLogger(__name__)


class PrometheusScrapeConsumerCharm(CharmBase):
    """A Charm used to test the required relation with prometheus-scrape interface."""

    def __init__(self, *args):
        super().__init__(*args)
        # Gathers scrape job information from metrics endpoints
        self.metrics_consumer = MetricsEndpointConsumer(self)

        self.framework.observe(self.on.install, self._on_install)
        self.framework.observe(
            self.metrics_consumer.on.targets_changed, self._configure
        )
        self.unit.status = ActiveStatus("unit is ready")

    def _on_install(self, _):
        """Install prometheus via snap."""
        self.unit.status = MaintenanceStatus("Installing prometheus via snap")
        subprocess.check_call(["snap", "install", "prometheus"])
        self.unit.status = ActiveStatus("unit is ready")

    def _configure(self, _):
        """Reconfigure and either reload or restart Prometheus."""
        self.unit.status = MaintenanceStatus("Configure Prometheus")
        prometheus_config = Path("/var/snap/prometheus/current/prometheus.yml")
        if not prometheus_config.exists():
            BlockedStatus("prometheus config file was not found")

        with open(prometheus_config, "r") as file:
            original_config = yaml.safe_load(file)

        config = original_config.copy()
        jobs = self.metrics_consumer.jobs()
        logger.info("jobs: %s", jobs)
        # never edit first job
        config["scrape_configs"] = [config["scrape_configs"][0], *jobs]

        with open(prometheus_config, "w") as file:
            yaml.dump(config, file)

        try:
            subprocess.check_call(
                ["prometheus.promtool", "check", "config", str(prometheus_config)]
            )
            subprocess.check_call(["snap", "restart", "prometheus"])
        except subprocess.SubprocessError:
            with open(prometheus_config, "w") as file:
                yaml.dump(original_config, file)

            self.unit.status = BlockedStatus("Prometheus configuration failed")
        else:
            self.unit.status = ActiveStatus("unit is ready")


if __name__ == "__main__":
    main(PrometheusScrapeConsumerCharm)
