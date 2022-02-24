#!/usr/bin/env python3
# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.
import subprocess
from pathlib import Path

import yaml
from charmhelpers.core import hookenv
from charms import layer
from charms.reactive import endpoint_from_name, hook, set_flag, when, when_not


@when_not("charm.status.is-set")
def set_status():
    layer.status.active("unit is ready")
    set_flag("charm.status.is-set")


@hook("install")
def install():
    """Trigger charm installation."""
    layer.status.maintenance("Installing Prometheus")
    subprocess.check_call(["snap", "install", "prometheus"])
    layer.status.active("unit is ready")


@when("endpoint.metrics-endpoint.changed")
def configure_jobs():
    """Configure Prometheus jobs."""
    prometheus = endpoint_from_name("metrics-endpoint")
    layer.status.maintenance("Configure Prometheus")
    prometheus_config = Path("/var/snap/prometheus/current/prometheus.yml")
    if not prometheus_config.exists():
        layer.status.blocked("prometheus config file was not found")

    with open(prometheus_config, "r") as file:
        original_config = yaml.safe_load(file)

    config = original_config.copy()
    jobs = prometheus.jobs()
    hookenv.log("jobs: {}".format(jobs))
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

        layer.status.blocked("Prometheus configuration failed")
    else:
        layer.status.active("unit is ready")
