#!/usr/bin/env python3
# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.
import subprocess

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
    jobs = [
        {
            "scrape_interval": hookenv.config("scrape-interval"),
            "static_configs": [
                {"targets": ["*:9090"], "labels": {"name": hookenv.local_unit()}}
            ],
        }
    ]
    prometheus.set_jobs(jobs)
    prometheus.set_alert_path("./reactive/prometheus_alert_rules")
    prometheus.set_scrape_job_spec()
