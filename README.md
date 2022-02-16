## Overview.

This document explains how to integrate with the Prometheus charm
for the purpose of providing a metrics endpoint to Prometheus. It
also explains how alternative implementations of the Prometheus charms
may maintain the same interface and be backward compatible with all
currently integrated charms. Finally this document is the
authoritative reference on the structure of relation data that is
shared between Prometheus charms and any other charm that intends to
provide a scrape target for Prometheus.

## Provider Library Usage

This Prometheus charm interacts with its scrape targets using its
charm library. Charms seeking to expose metric endpoints for the
Prometheus charm, must do so using the `MetricsEndpointProvider`
object from this charm library. For the simplest use cases, using the
`MetricsEndpointProvider` object only requires instantiating it,
typically in the constructor of your charm (the one which exposes a
metrics endpoint). The `MetricsEndpointProvider` constructor requires
the name of the relation over which a scrape target (metrics endpoint)
is exposed to the Prometheus charm. This relation must use the
`prometheus_scrape` interface. By default address of the metrics
endpoint is set to the unit IP address, by each unit of the
`MetricsEndpointProvider` charm. These units set their address in
response to the `PebbleReady` event of each container in the unit,
since container restarts of Kubernetes charms can result in change of
IP addresses. The default name for the metrics endpoint relation is
`metrics-endpoint`. It is strongly recommended to use the same
relation name for consistency across charms and doing so obviates the
need for an additional constructor argument. The
`MetricsEndpointProvider` object may be instantiated as follows

    from charms.prometheus_k8s.v0.prometheus_scrape import MetricsEndpointProvider

    def __init__(self, *args):
        super().__init__(*args)
        ...
        self.metrics_endpoint = MetricsEndpointProvider(self)
        ...

Note that the first argument (`self`) to `MetricsEndpointProvider` is
always a reference to the parent (scrape target) charm.

An instantiated `MetricsEndpointProvider` object will ensure that each
unit of its parent charm, is a scrape target for the
`MetricsEndpointConsumer` (Prometheus) charm. By default
`MetricsEndpointProvider` assumes each unit of the consumer charm
exports its metrics at a path given by `/metrics` on port 80. These
defaults may be changed by providing the `MetricsEndpointProvider`
constructor an optional argument (`jobs`) that represents a
Prometheus scrape job specification using Python standard data
structures. This job specification is a subset of Prometheus' own
[scrape
configuration](https://prometheus.io/docs/prometheus/latest/configuration/configuration/#scrape_config)
format but represented using Python data structures. More than one job
may be provided using the `jobs` argument. Hence `jobs` accepts a list
of dictionaries where each dictionary represents one `<scrape_config>`
object as described in the Prometheus documentation. The currently
supported configuration subset is: `job_name`, `metrics_path`,
`static_configs`

Suppose it is required to change the port on which scraped metrics are
exposed to 8000. This may be done by providing the following data
structure as the value of `jobs`.

```
[
    {
        "static_configs": [
            {
                "targets": ["*:8000"]
            }
        ]
    }
]
```

The wildcard ("*") host specification implies that the scrape targets
will automatically be set to the host addresses advertised by each
unit of the consumer charm.

It is also possible to change the metrics path and scrape multiple
ports, for example

```
[
    {
        "metrics_path": "/my-metrics-path",
        "static_configs": [
            {
                "targets": ["*:8000", "*:8081"],
            }
        ]
    }
]
```

More complex scrape configurations are possible. For example

```
[
    {
        "static_configs": [
            {
                "targets": ["10.1.32.215:7000", "*:8000"],
                "labels": {
                    "some-key": "some-value"
                }
            }
        ]
    }
]
```

This example scrapes the target "10.1.32.215" at port 7000 in addition
to scraping each unit at port 8000. There is however one difference
between wildcard targets (specified using "*") and fully qualified
targets (such as "10.1.32.215"). The Prometheus charm automatically
associates labels with metrics generated by each target. These labels
localise the source of metrics within the Juju topology by specifying
its "model name", "model UUID", "application name" and "unit
name". However unit name is associated only with wildcard targets but
not with fully qualified targets.

Multiple jobs with different metrics paths and labels are allowed, but
each job must be given a unique name. For example

```
[
    {
        "job_name": "my-first-job",
        "metrics_path": "one-path",
        "static_configs": [
            {
                "targets": ["*:7000"],
                "labels": {
                    "some-key": "some-value"
                }
            }
        ]
    },
    {
        "job_name": "my-second-job",
        "metrics_path": "another-path",
        "static_configs": [
            {
                "targets": ["*:8000"],
                "labels": {
                    "some-other-key": "some-other-value"
                }
            }
        ]
    }
]
```

It is also possible to configure other scrape related parameters using
these job specifications as described by the Prometheus
[documentation](https://prometheus.io/docs/prometheus/latest/configuration/configuration/#scrape_config).
The permissible subset of job specific scrape configuration parameters
supported in a `MetricsEndpointProvider` job specification are:

- `job_name`
- `metrics_path`
- `static_configs`
- `scrape_interval`
- `scrape_timeout`
- `proxy_url`
- `relabel_configs`
- `metrics_relabel_configs`
- `sample_limit`
- `label_limit`
- `label_name_length_limit`
- `label_value_length_limit`

## Consumer Library Usage

The `MetricsEndpointConsumer` object may be used by Prometheus
charms to manage relations with their scrape targets. For this
purposes a Prometheus charm needs to do two things

1. Instantiate the `MetricsEndpointConsumer` object by providing it a
reference to the parent (Prometheus) charm and optionally the name of
the relation that the Prometheus charm uses to interact with scrape
targets. This relation must confirm to the `prometheus_scrape`
interface and it is strongly recommended that this relation be named
`metrics-endpoint` which is its default value.

For example a Prometheus charm may instantiate the
`MetricsEndpointConsumer` in its constructor as follows

    from charms.prometheus_k8s.v0.prometheus_scrape import MetricsEndpointConsumer

    def __init__(self, *args):
        super().__init__(*args)
        ...
        self.metrics_consumer = MetricsEndpointConsumer(self)
        ...

2. A Prometheus charm also needs to respond to the
`TargetsChangedEvent` event of the `MetricsEndpointConsumer` by adding itself as
an observer for these events, as in

    self.framework.observe(
        self.metrics_consumer.on.targets_changed,
        self._on_scrape_targets_changed,
    )

In responding to the `TargetsChangedEvent` event the Prometheus
charm must update the Prometheus configuration so that any new scrape
targets are added and/or old ones removed from the list of scraped
endpoints. For this purpose the `MetricsEndpointConsumer` object
exposes a `jobs()` method that returns a list of scrape jobs. Each
element of this list is the Prometheus scrape configuration for that
job. In order to update the Prometheus configuration, the Prometheus
charm needs to replace the current list of jobs with the list provided
by `jobs()` as follows

    def _on_scrape_targets_changed(self, event):
        ...
        scrape_jobs = self.metrics_consumer.jobs()
        for job in scrape_jobs:
            prometheus_scrape_config.append(job)
        ...

## Alerting Rules

This charm library also supports gathering alerting rules from all
related `MetricsEndpointProvider` charms and enabling corresponding alerts within the
Prometheus charm.  Alert rules are automatically gathered by `MetricsEndpointProvider`
charms when using this library, from a directory conventionally named
`prometheus_alert_rules`. This directory must reside at the top level
in the `src` folder of the consumer charm. Each file in this directory
is assumed to be in one of two formats:
- the official prometheus alert rule format, conforming to the
[Prometheus docs](https://prometheus.io/docs/prometheus/latest/configuration/alerting_rules/)
- a single rule format, which is a simplified subset of the official format,
comprising a single alert rule per file, using the same YAML fields.

The file name must have the `.rule` extension.

An example of the contents of such a file in the custom single rule
format is shown below.

```
alert: HighRequestLatency
expr: job:request_latency_seconds:mean5m{my_key=my_value} > 0.5
for: 10m
labels:
  severity: Medium
  type: HighLatency
annotations:
  summary: High request latency for {{ $labels.instance }}.
```

The `MetricsEndpointProvider` will read all available alert rules and
also inject "filtering labels" into the alert expressions. The
filtering labels ensure that alert rules are localised to the metrics
provider charm's Juju topology (application, model and its UUID). Such
a topology filter is essential to ensure that alert rules submitted by
one provider charm generates alerts only for that same charm. When
alert rules are embedded in a charm, and the charm is deployed as a
Juju application, the alert rules from that application have their
expressions automatically updated to filter for metrics coming from
the units of that application alone. This remove risk of spurious
evaluation, e.g., when you have multiple deployments of the same charm
monitored by the same Prometheus.

Not all alerts one may want to specify can be embedded in a
charm. Some alert rules will be specific to a user's use case. This is
the case, for example, of alert rules that are based on business
constraints, like expecting a certain amount of requests to a specific
API every five minutes. Such alert rules can be specified via the
[COS Config Charm](https://charmhub.io/cos-configuration-k8s),
which allows importing alert rules and other settings like dashboards
from a Git repository.

Gathering alert rules and generating rule files within the Prometheus
charm is easily done using the `alerts()` method of
`MetricsEndpointConsumer`. Alerts generated by Prometheus will
automatically include Juju topology labels in the alerts. These labels
indicate the source of the alert. The following labels are
automatically included with each alert

- `juju_model`
- `juju_model_uuid`
- `juju_application`

## Relation Data

The Prometheus charm uses both application and unit relation data to
obtain information regarding its scrape jobs, alert rules and scrape
targets. This relation data is in JSON format and it closely resembles
the YAML structure of Prometheus [scrape configuration]
(https://prometheus.io/docs/prometheus/latest/configuration/configuration/#scrape_config).

Units of Metrics provider charms advertise their names and addresses
over unit relation data using the `prometheus_scrape_unit_name` and
`prometheus_scrape_unit_address` keys. While the `scrape_metadata`,
`scrape_jobs` and `alert_rules` keys in application relation data
of Metrics provider charms hold eponymous information.
