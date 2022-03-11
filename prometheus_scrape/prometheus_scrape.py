# Copyright 2021 Canonical Ltd.
# See LICENSE file for licensing details.

import json
import logging
import os
import platform
import subprocess
from collections import OrderedDict
from pathlib import Path
from typing import Dict, List, Optional, Union

import yaml
from ops.charm import CharmBase, RelationRole
from ops.framework import EventBase, EventSource, Object, ObjectEvents

# The unique Charmhub library identifier, never change it
from ops.model import ModelError

logger = logging.getLogger(__name__)


ALLOWED_KEYS = {
    "job_name",
    "metrics_path",
    "static_configs",
    "scrape_interval",
    "scrape_timeout",
    "proxy_url",
    "relabel_configs",
    "metrics_relabel_configs",
    "sample_limit",
    "label_limit",
    "label_name_length_limit",
    "label_value_lenght_limit",
}
DEFAULT_JOB = {
    "metrics_path": "/metrics",
    "static_configs": [{"targets": ["*:80"]}],
}


DEFAULT_RELATION_NAME = "metrics-endpoint"
RELATION_INTERFACE_NAME = "prometheus-scrape"

DEFAULT_ALERT_RULES_RELATIVE_PATH = "./src/prometheus_alert_rules"


class RelationNotFoundError(Exception):
    """Raised if there is no relation with the given name is found."""

    def __init__(self, relation_name: str):
        self.relation_name = relation_name
        self.message = "No relation named '{}' found".format(relation_name)

        super().__init__(self.message)


class RelationInterfaceMismatchError(Exception):
    """Raised if the relation with the given name has a different interface."""

    def __init__(
        self,
        relation_name: str,
        expected_relation_interface: str,
        actual_relation_interface: str,
    ):
        self.relation_name = relation_name
        self.expected_relation_interface = expected_relation_interface
        self.actual_relation_interface = actual_relation_interface
        self.message = (
            "The '{}' relation has '{}' as interface rather than the "
            "expected '{}'".format(
                relation_name, actual_relation_interface, expected_relation_interface
            )
        )

        super().__init__(self.message)


class RelationRoleMismatchError(Exception):
    """Raised if the relation with the given name has a different role."""

    def __init__(
        self,
        relation_name: str,
        expected_relation_role: RelationRole,
        actual_relation_role: RelationRole,
    ):
        self.relation_name = relation_name
        self.expected_relation_interface = expected_relation_role
        self.actual_relation_role = actual_relation_role
        self.message = (
            "The '{}' relation has role '{}' rather than the expected '{}'".format(
                relation_name, repr(actual_relation_role), repr(expected_relation_role)
            )
        )

        super().__init__(self.message)


def _validate_relation_by_interface_and_direction(
    charm: CharmBase,
    relation_name: str,
    expected_relation_interface: str,
    expected_relation_role: RelationRole,
):
    """Verifies that a relation has the necessary characteristics.

    Verifies that the `relation_name` provided: (1) exists in metadata.yaml,
    (2) declares as interface the interface name passed as `relation_interface`
    and (3) has the right "direction", i.e., it is a relation that `charm`
    provides or requires.

    Args:
        charm: a `CharmBase` object to scan for the matching relation.
        relation_name: the name of the relation to be verified.
        expected_relation_interface: the interface name to be matched by the
            relation named `relation_name`.
        expected_relation_role: whether the `relation_name` must be either
            provided or required by `charm`.

    Raises:
        RelationNotFoundError: If there is no relation in the charm's metadata.yaml
            with the same name as provided via `relation_name` argument.
        RelationInterfaceMismatchError: The relation with the same name as provided
            via `relation_name` argument does not have the same relation interface
            as specified via the `expected_relation_interface` argument.
        RelationRoleMismatchError: If the relation with the same name as provided
            via `relation_name` argument does not have the same role as specified
            via the `expected_relation_role` argument.
    """
    if relation_name not in charm.meta.relations:
        raise RelationNotFoundError(relation_name)

    relation = charm.meta.relations[relation_name]

    actual_relation_interface = relation.interface_name
    if actual_relation_interface != expected_relation_interface:
        raise RelationInterfaceMismatchError(
            relation_name, expected_relation_interface, actual_relation_interface
        )

    if expected_relation_role == RelationRole.provides:
        if relation_name not in charm.meta.provides:
            raise RelationRoleMismatchError(
                relation_name, RelationRole.provides, RelationRole.requires
            )
    elif expected_relation_role == RelationRole.requires:
        if relation_name not in charm.meta.requires:
            raise RelationRoleMismatchError(
                relation_name, RelationRole.requires, RelationRole.provides
            )
    else:
        raise Exception(
            "Unexpected RelationDirection: {}".format(expected_relation_role)
        )


def _sanitize_scrape_configuration(job) -> dict:
    """Restrict permissible scrape configuration options.

    If job is empty then a default job is returned. The
    default job is

    ```
    {
        "metrics_path": "/metrics",
        "static_configs": [{"targets": ["*:80"]}],
    }
    ```

    Args:
        job: a dict containing a single Prometheus job
            specification.

    Returns:
        a dictionary containing a sanitized job specification.
    """
    sanitized_job = DEFAULT_JOB.copy()
    sanitized_job.update(
        {key: value for key, value in job.items() if key in ALLOWED_KEYS}
    )
    return sanitized_job


class JujuTopology:
    """Class for storing and formatting juju topology information."""

    STUB = "%%juju_topology%%"

    def __new__(cls, *args, **kwargs):
        """Reject instantiation of a base JujuTopology class. Children only."""
        if cls is JujuTopology:
            raise TypeError(
                "only children of '{}' may be instantiated".format(cls.__name__)
            )
        return object.__new__(cls)

    def __init__(
        self,
        model: str,
        model_uuid: str,
        application: str,
        unit: Optional[str] = "",
        charm_name: Optional[str] = "",
    ):
        """Build a JujuTopology object.

        A `JujuTopology` object is used for storing and transforming
        Juju Topology information. This information is used to
        annotate Prometheus scrape jobs and alert rules. Such
        annotation when applied to scrape jobs helps in identifying
        the source of the scrapped metrics. On the other hand when
        applied to alert rules topology information ensures that
        evaluation of alert expressions is restricted to the source
        (charm) from which the alert rules were obtained.

        Args:
            model: a string name of the Juju model
            model_uuid: a globally unique string identifier for the Juju model
            application: an application name as a string
            unit: a unit name as a string
            charm_name: name of charm as a string

        Note:
            `JujuTopology` should not be constructed directly by charm code. Please
            use `ProviderTopology` or `AggregatorTopology`.
        """
        self.model = model
        self.model_uuid = model_uuid
        self.application = application
        self.charm_name = charm_name
        self.unit = unit

    @classmethod
    def from_charm(cls, charm):
        """Factory method for creating `JujuTopology` children from a given charm.

        Args:
            charm: a `CharmBase` object for which the `JujuTopology` has to be constructed

        Returns:
            a `JujuTopology` object.
        """
        return cls(
            model=charm.model.name,
            model_uuid=charm.model.uuid,
            application=charm.model.app.name,
            unit=charm.model.unit.name,
            charm_name=charm.meta.name,
        )

    @classmethod
    def from_relation_data(cls, data: dict):
        """Factory method for creating `JujuTopology` children from a dictionary.

        Args:
            data: a dictionary with four keys providing topology information. The keys are
                - "model"
                - "model_uuid"
                - "application"
                - "unit"
                - "charm_name"

                `unit` and `charm_name` may be empty, but will result in more limited
                labels. However, this allows us to support payload-only charms.

        Returns:
            a `JujuTopology` object.
        """
        return cls(
            model=data["model"],
            model_uuid=data["model_uuid"],
            application=data["application"],
            unit=data.get("unit", ""),
            charm_name=data.get("charm_name", ""),
        )

    @property
    def identifier(self) -> str:
        """Format the topology information into a terse string."""
        # This is odd, but may have `None` as a model key
        return "_".join(
            [str(val) for val in self.as_promql_label_dict().values()]
        ).replace("/", "_")

    @property
    def promql_labels(self) -> str:
        """Format the topology information into a verbose string."""
        return ", ".join(
            [
                '{}="{}"'.format(key, value)
                for key, value in self.as_promql_label_dict().items()
            ]
        )

    def as_dict(self, rename_keys: Optional[Dict[str, str]] = None) -> OrderedDict:
        """Format the topology information into a dict.

        Use an OrderedDict so we can rely on the insertion order on Python 3.5 (and 3.6,
        which still does not guarantee it).

        Args:
            rename_keys: A dictionary mapping old key names to new key names, which will
                be substituted when invoked.
        """
        ret = OrderedDict(
            [
                ("model", self.model),
                ("model_uuid", self.model_uuid),
                ("application", self.application),
                ("unit", self.unit),
                ("charm_name", self.charm_name),
            ]
        )

        ret["unit"] or ret.pop("unit")
        ret["charm_name"] or ret.pop("charm_name")

        # If a key exists in `rename_keys`, replace the value
        if rename_keys:
            ret = OrderedDict(
                (rename_keys.get(key, key), value) for key, value in ret.items()
            )

        return ret

    def as_promql_label_dict(self):
        """Format the topology information into a dict with keys having 'juju_' as prefix."""
        vals = {
            "juju_{}".format(key): val
            for key, val in self.as_dict(rename_keys={"charm_name": "charm"}).items()
        }
        # The leader is the only unit that sets alert rules, if "juju_unit" is present,
        # then the rules will only be evaluated for that unit
        if "juju_unit" in vals:
            vals.pop("juju_unit")

        return vals

    def render(self, template: str):
        """Render a juju-topology template string with topology info."""
        return template.replace(JujuTopology.STUB, self.promql_labels)


class AggregatorTopology(JujuTopology):
    """Class for initializing topology information for MetricsEndpointAggregator."""

    @classmethod
    def create(cls, model: str, model_uuid: str, application: str, unit: str):
        """Factory method for creating the `AggregatorTopology` dataclass from a given charm.

        Args:
            model: a string representing the model
            model_uuid: the model UUID as a string
            application: the application name
            unit: the unit name

        Returns:
            a `AggregatorTopology` object.
        """
        return cls(
            model=model,
            model_uuid=model_uuid,
            application=application,
            unit=unit,
        )

    def as_promql_label_dict(self):
        """Format the topology information into a dict with keys having 'juju_' as prefix."""
        vals = {"juju_{}".format(key): val for key, val in self.as_dict().items()}

        # FIXME: Why is this different? I have no idea. The uuid length should be the same
        vals["juju_model_uuid"] = vals["juju_model_uuid"][:7]

        return vals


class ProviderTopology(JujuTopology):
    """Class for initializing topology information for MetricsEndpointProvider."""

    @property
    def scrape_identifier(self):
        """Format the topology information into a scrape identifier."""
        # This is used only by Metrics[Consumer|Provider] and does not need a
        # unit name, so only check for the charm name
        return "juju_{}_prometheus_scrape".format(self.identifier)


class InvalidAlertRulePathError(Exception):
    """Raised if the alert rules folder cannot be found or is otherwise invalid."""

    def __init__(
        self,
        alert_rules_absolute_path: Path,
        message: str,
    ):
        self.alert_rules_absolute_path = alert_rules_absolute_path
        self.message = message

        super().__init__(self.message)


def _is_official_alert_rule_format(rules_dict: dict) -> bool:
    """Are alert rules in the upstream format as supported by Prometheus.

    Alert rules in dictionary format are in "official" form if they
    contain a "groups" key, since this implies they contain a list of
    alert rule groups.

    Args:
        rules_dict: a set of alert rules in Python dictionary format

    Returns:
        True if alert rules are in official Prometheus file format.
    """
    return "groups" in rules_dict


def _is_single_alert_rule_format(rules_dict: dict) -> bool:
    """Are alert rules in single rule format.

    The Prometheus charm library supports reading of alert rules in a
    custom format that consists of a single alert rule per file. This
    does not conform to the official Prometheus alert rule file format
    which requires that each alert rules file consists of a list of
    alert rule groups and each group consists of a list of alert
    rules.

    Alert rules in dictionary form are considered to be in single rule
    format if in the least it contains two keys corresponding to the
    alert rule name and alert expression.

    Returns:
        True if alert rule is in single rule file format.
    """
    # one alert rule per file
    return set(rules_dict) >= {"alert", "expr"}


class AlertRules:
    """Utility class for amalgamating prometheus alert rule files and injecting juju topology.

    An `AlertRules` object supports aggregating alert rules from files and directories in both
    official and single rule file formats using the `add_path()` method. All the alert rules
    read are annotated with Juju topology labels and amalgamated into a single data structure
    in the form of a Python dictionary using the `as_dict()` method. Such a dictionary can be
    easily dumped into JSON format and exchanged over relation data. The dictionary can also
    be dumped into YAML format and written directly into an alert rules file that is read by
    Prometheus. Note that multiple `AlertRules` objects must not be written into the same file,
    since Prometheus allows only a single list of alert rule groups per alert rules file.

    The official Prometheus format is a YAML file conforming to the Prometheus documentation
    (https://prometheus.io/docs/prometheus/latest/configuration/alerting_rules/).
    The custom single rule format is a subsection of the official YAML, having a single alert
    rule, effectively "one alert per file".
    """

    # This class uses the following terminology for the various parts of a rule file:
    # - alert rules file: the entire groups[] yaml, including the "groups:" key.
    # - alert groups (plural): the list of groups[] (a list, i.e. no "groups:" key) - it is a list
    #   of dictionaries that have the "name" and "rules" keys.
    # - alert group (singular): a single dictionary that has the "name" and "rules" keys.
    # - alert rules (plural): all the alerts in a given alert group - a list of dictionaries with
    #   the "alert" and "expr" keys.
    # - alert rule (singular): a single dictionary that has the "alert" and "expr" keys.

    def __init__(self, topology: Optional[JujuTopology] = None):
        """Build and alert rule object.

        Args:
            topology: an optional `JujuTopology` instance that is used to annotate all alert rules.
        """
        self.topology = topology
        self.alert_groups = []  # type: List[dict]

    def _from_file(self, root_path: Path, file_path: Path) -> List[dict]:
        """Read a rules file from path, injecting juju topology.

        Args:
            root_path: full path to the root rules folder (used only for generating group name)
            file_path: full path to a *.rule file.

        Returns:
            A list of dictionaries representing the rules file, if file is valid (the structure is
            formed by `yaml.safe_load` of the file); an empty list otherwise.
        """
        with file_path.open() as rf:
            # Load a list of rules from file then add labels and filters
            try:
                rule_file = yaml.safe_load(rf)

            except Exception as e:
                logger.error(
                    "Failed to read alert rules from %s: %s", file_path.name, e
                )
                return []

            if _is_official_alert_rule_format(rule_file):
                alert_groups = rule_file["groups"]
            elif _is_single_alert_rule_format(rule_file):
                # convert to list of alert groups
                # group name is made up from the file name
                alert_groups = [{"name": file_path.stem, "rules": [rule_file]}]
            else:
                # invalid/unsupported
                logger.error("Invalid rules file: %s", file_path.name)
                return []

            # update rules with additional metadata
            for alert_group in alert_groups:
                # update group name with topology and sub-path
                alert_group["name"] = self._group_name(
                    str(root_path),
                    str(file_path),
                    alert_group["name"],
                )

                # add "juju_" topology labels
                for alert_rule in alert_group["rules"]:
                    if "labels" not in alert_rule:
                        alert_rule["labels"] = {}

                    if self.topology:
                        alert_rule["labels"].update(
                            self.topology.as_promql_label_dict()
                        )
                        # insert juju topology filters into a prometheus alert rule
                        alert_rule["expr"] = self.topology.render(alert_rule["expr"])

            return alert_groups

    def _group_name(self, root_path: str, file_path: str, group_name: str) -> str:
        """Generate group name from path and topology.

        The group name is made up of the relative path between the root dir_path, the file path,
        and topology identifier.

        Args:
            root_path: path to the root rules dir.
            file_path: path to rule file.
            group_name: original group name to keep as part of the new augmented group name

        Returns:
            New group name, augmented by juju topology and relative path.
        """
        rel_path = os.path.relpath(os.path.dirname(file_path), root_path)
        rel_path = "" if rel_path == "." else rel_path.replace(os.path.sep, "_")

        # Generate group name:
        #  - name, from juju topology
        #  - suffix, from the relative path of the rule file;
        group_name_parts = [self.topology.identifier] if self.topology else []
        group_name_parts.extend([rel_path, group_name, "alerts"])
        # filter to remove empty strings
        return "_".join(filter(None, group_name_parts))

    @classmethod
    def _multi_suffix_glob(
        cls, dir_path: Path, suffixes: List[str], recursive: bool = True
    ) -> list:
        """Helper function for getting all files in a directory that have a matching suffix.

        Args:
            dir_path: path to the directory to glob from.
            suffixes: list of suffixes to include in the glob (items should begin with a period).
            recursive: a flag indicating whether a glob is recursive (nested) or not.

        Returns:
            List of files in `dir_path` that have one of the suffixes specified in `suffixes`.
        """
        all_files_in_dir = dir_path.glob("**/*" if recursive else "*")
        return list(
            filter(lambda f: f.is_file() and f.suffix in suffixes, all_files_in_dir)
        )

    def _from_dir(self, dir_path: Path, recursive: bool) -> List[dict]:
        """Read all rule files in a directory.

        All rules from files for the same directory are loaded into a single
        group. The generated name of this group includes juju topology.
        By default, only the top directory is scanned; for nested scanning, pass `recursive=True`.

        Args:
            dir_path: directory containing *.rule files (alert rules without groups).
            recursive: flag indicating whether to scan for rule files recursively.

        Returns:
            a list of dictionaries representing prometheus alert rule groups, each dictionary
            representing an alert group (structure determined by `yaml.safe_load`).
        """
        alert_groups = []  # type: List[dict]

        # Gather all alerts into a list of groups
        for file_path in self._multi_suffix_glob(
            dir_path, [".rule", ".rules"], recursive
        ):
            alert_groups_from_file = self._from_file(dir_path, file_path)
            if alert_groups_from_file:
                logger.debug("Reading alert rule from %s", file_path)
                alert_groups.extend(alert_groups_from_file)

        return alert_groups

    def add_path(self, path: str, *, recursive: bool = False) -> None:
        """Add rules from a dir path.

        All rules from files are aggregated into a data structure representing a single rule file.
        All group names are augmented with juju topology.

        Args:
            path: either a rules file or a dir of rules files.
            recursive: whether to read files recursively or not (no impact if `path` is a file).

        Returns:
            True if path was added else False.
        """
        path = Path(path)  # type: Path
        if path.is_dir():
            self.alert_groups.extend(self._from_dir(path, recursive))
        elif path.is_file():
            self.alert_groups.extend(self._from_file(path.parent, path))
        else:
            logger.warning("path does not exist: %s", path)

    def as_dict(self) -> dict:
        """Return standard alert rules file in dict representation.

        Returns:
            a dictionary containing a single list of alert rule groups.
            The list of alert rule groups is provided as value of the
            "groups" dictionary key.
        """
        return {"groups": self.alert_groups} if self.alert_groups else {}


class TargetsChangedEvent(EventBase):
    """Event emitted when Prometheus scrape targets change."""

    def __init__(self, handle, relation_id):
        super().__init__(handle)
        self.relation_id = relation_id

    def snapshot(self):
        """Save scrape target relation information."""
        return {"relation_id": self.relation_id}

    def restore(self, snapshot):
        """Restore scrape target relation information."""
        self.relation_id = snapshot["relation_id"]


class MonitoringEvents(ObjectEvents):
    """Event descriptor for events raised by `MetricsEndpointConsumer`."""

    targets_changed = EventSource(TargetsChangedEvent)


class MetricsEndpointConsumer(Object):
    """A Prometheus based Monitoring service."""

    on = MonitoringEvents()

    def __init__(self, charm: CharmBase, relation_name: str = DEFAULT_RELATION_NAME):
        """A Prometheus based Monitoring service.

        Args:
            charm: a `CharmBase` instance that manages this
                instance of the Prometheus service.
            relation_name: an optional string name of the relation between `charm`
                and the Prometheus charmed service. The default is "metrics-endpoint".
                It is strongly advised not to change the default, so that people
                deploying your charm will have a consistent experience with all
                other charms that consume metrics endpoints.

        Raises:
            RelationNotFoundError: If there is no relation in the charm's metadata.yaml
                with the same name as provided via `relation_name` argument.
            RelationInterfaceMismatchError: The relation with the same name as provided
                via `relation_name` argument does not have the `prometheus_scrape` relation
                interface.
            RelationRoleMismatchError: If the relation with the same name as provided
                via `relation_name` argument does not have the `RelationRole.requires`
                role.
        """
        _validate_relation_by_interface_and_direction(
            charm, relation_name, RELATION_INTERFACE_NAME, RelationRole.requires
        )

        super().__init__(charm, relation_name)
        self._charm = charm
        self._relation_name = relation_name
        self._transformer = PromqlTransformer(self._charm)
        events = self._charm.on[relation_name]
        self.framework.observe(
            events.relation_changed, self._on_metrics_provider_relation_changed
        )
        self.framework.observe(
            events.relation_departed, self._on_metrics_provider_relation_departed
        )

    def _on_metrics_provider_relation_changed(self, event):
        """Handle changes with related metrics providers.

        Anytime there are changes in relations between Prometheus
        and metrics provider charms the Prometheus charm is informed,
        through a `TargetsChangedEvent` event. The Prometheus charm can
        then choose to update its scrape configuration.

        Args:
            event: a `CharmEvent` in response to which the Prometheus
                charm must update its scrape configuration.
        """
        rel_id = event.relation.id

        self.on.targets_changed.emit(relation_id=rel_id)

    def _on_metrics_provider_relation_departed(self, event):
        """Update job config when a metrics provider departs.

        When a metrics provider departs the Prometheus charm is informed
        through a `TargetsChangedEvent` event so that it can update its
        scrape configuration to ensure that the departed metrics provider
        is removed from the list of scrape jobs and

        Args:
            event: a `CharmEvent` that indicates a metrics provider
               unit has departed.
        """
        rel_id = event.relation.id
        self.on.targets_changed.emit(relation_id=rel_id)

    def jobs(self) -> list:
        """Fetch the list of scrape jobs.

        Returns:
            A list consisting of all the static scrape configurations
            for each related `MetricsEndpointProvider` that has specified
            its scrape targets.
        """
        scrape_jobs = []

        for relation in self._charm.model.relations[self._relation_name]:
            static_scrape_jobs = self._static_scrape_config(relation)
            if static_scrape_jobs:
                scrape_jobs.extend(static_scrape_jobs)

        return scrape_jobs

    def alerts(self) -> dict:
        """Fetch alerts for all relations.

        A Prometheus alert rules file consists of a list of "groups". Each
        group consists of a list of alerts (`rules`) that are sequentially
        executed. This method returns all the alert rules provided by each
        related metrics provider charm. These rules may be used to generate a
        separate alert rules file for each relation since the returned list
        of alert groups are indexed by that relations Juju topology identifier.
        The Juju topology identifier string includes substrings that identify
        alert rule related metadata such as the Juju model, model UUID and the
        application name from where the alert rule originates. Since this
        topology identifier is globally unique, it may be used for instance as
        the name for the file into which the list of alert rule groups are
        written. For each relation, the structure of data returned is a dictionary
        representation of a standard prometheus rules file:

        {"groups": [{"name": ...}, ...]}

        per official prometheus documentation
        https://prometheus.io/docs/prometheus/latest/configuration/alerting_rules/

        The value of the `groups` key is such that it may be used to generate
        a Prometheus alert rules file directly using `yaml.dump` but the
        `groups` key itself must be included as this is required by Prometheus.

        For example the list of alert rule groups returned by this method may
        be written into files consumed by Prometheus as follows

        ```
        for topology_identifier, alert_rule_groups in self.metrics_consumer.alerts().items():
            filename = "juju_" + topology_identifier + ".rules"
            path = os.path.join(PROMETHEUS_RULES_DIR, filename)
            rules = yaml.dump(alert_rule_groups)
            container.push(path, rules, make_dirs=True)
        ```

        Returns:
            A dictionary mapping the Juju topology identifier of the source charm to
            its list of alert rule groups.
        """
        alerts = (
            {}
        )  # type: Dict[str, dict] # mapping b/w juju identifiers and alert rule files
        for relation in self._charm.model.relations[self._relation_name]:
            if not relation.units:
                continue

            alert_rules = json.loads(
                relation.data[relation.app].get("alert_rules", "{}")
            )
            if not alert_rules:
                continue

            identifier = None
            try:
                scrape_metadata = json.loads(
                    relation.data[relation.app]["scrape_metadata"]
                )
                identifier = ProviderTopology.from_relation_data(
                    scrape_metadata
                ).identifier
                alerts[identifier] = self._transformer.apply_label_matchers(alert_rules)

            except KeyError as e:
                logger.debug(
                    "Relation %s has no 'scrape_metadata': %s",
                    relation.id,
                    e,
                )
                identifier = self._get_identifier_by_alert_rules(alert_rules)

            if not identifier:
                logger.error(
                    "Alert rules were found but no usable group or identifier was present"
                )
                continue
            alerts[identifier] = alert_rules

        return alerts

    def _get_identifier_by_alert_rules(self, rules: dict) -> Union[str, None]:
        """Determine an appropriate dict key for alert rules.

        The key is used as the filename when writing alerts to disk, so the structure
        and uniqueness is important.

        Args:
            rules: a dict of alert rules
        """
        if "groups" not in rules:
            logger.warning("No alert groups were found in relation data")
            return None

        # Construct an ID based on what's in the alert rules if they have labels
        for group in rules["groups"]:
            try:
                labels = group["rules"][0]["labels"]
                identifier = "{}_{}_{}".format(
                    labels["juju_model"],
                    labels["juju_model_uuid"],
                    labels["juju_application"],
                )
                return identifier
            except KeyError:
                logger.debug("Alert rules were found but no usable labels were present")
                continue

        logger.warning(
            "No labeled alert rules were found, and no 'scrape_metadata' "
            "was available. Using the alert group name as filename."
        )
        try:
            for group in rules["groups"]:
                return group["name"]
        except KeyError:
            logger.debug("No group name was found to use as identifier")

        return None

    def _static_scrape_config(self, relation) -> list:
        """Generate the static scrape configuration for a single relation.

        If the relation data includes `scrape_metadata` then the value
        of this key is used to annotate the scrape jobs with Juju
        Topology labels before returning them.

        Args:
            relation: an `ops.model.Relation` object whose static
                scrape configuration is required.

        Returns:
            A list (possibly empty) of scrape jobs. Each job is a
            valid Prometheus scrape configuration for that job,
            represented as a Python dictionary.
        """
        if not relation.units:
            return []

        scrape_jobs = json.loads(relation.data[relation.app].get("scrape_jobs", "[]"))

        if not scrape_jobs:
            return []

        scrape_metadata = json.loads(
            relation.data[relation.app].get("scrape_metadata", "{}")
        )

        if not scrape_metadata:
            return scrape_jobs

        job_name_prefix = ProviderTopology.from_relation_data(
            scrape_metadata
        ).scrape_identifier

        hosts = self._relation_hosts(relation)

        labeled_job_configs = []
        for job in scrape_jobs:
            config = self._labeled_static_job_config(
                _sanitize_scrape_configuration(job),
                job_name_prefix,
                hosts,
                scrape_metadata,
            )
            labeled_job_configs.append(config)

        return labeled_job_configs

    def _relation_hosts(self, relation) -> dict:
        """Fetch unit names and address of all metrics provider units for a single relation.

        Args:
            relation: An `ops.model.Relation` object for which the unit name to
                address mapping is required.

        Returns:
            A dictionary that maps unit names to unit addresses for
            the specified relation.
        """
        hosts = {}
        for unit in relation.units:
            # TODO deprecate and remove unit.name
            unit_name = (
                relation.data[unit].get("prometheus_scrape_unit_name") or unit.name
            )
            # TODO deprecate and remove "prometheus_scrape_host"
            unit_address = relation.data[unit].get(
                "prometheus_scrape_unit_address"
            ) or relation.data[unit].get("prometheus_scrape_host")
            if unit_name and unit_address:
                hosts.update({unit_name: unit_address})

        return hosts

    def _labeled_static_job_config(
        self, job, job_name_prefix, hosts, scrape_metadata
    ) -> dict:
        """Construct labeled job configuration for a single job.

        Args:

            job: a dictionary representing the job configuration as obtained from
                `MetricsEndpointProvider` over relation data.
            job_name_prefix: a string that may either be used as the
                job name if the job has no associated name or used as a prefix for
                the job if it does have a job name.
            hosts: a dictionary mapping host names to host address for
                all units of the relation for which this job configuration
                must be constructed.
            scrape_metadata: scrape configuration metadata obtained
                from `MetricsEndpointProvider` from the same relation for
                which this job configuration is being constructed.

        Returns:
            A dictionary representing a Prometheus job configuration
            for a single job.
        """
        name = job.get("job_name")
        job_name = "{}_{}".format(job_name_prefix, name) if name else job_name_prefix

        labeled_job = job.copy()
        labeled_job["job_name"] = job_name

        static_configs = job.get("static_configs")
        labeled_job["static_configs"] = []

        # relabel instance labels so that instance identifiers are globally unique
        # stable over unit recreation
        instance_relabel_config = {
            "source_labels": ["juju_model", "juju_model_uuid", "juju_application"],
            "separator": "_",
            "target_label": "instance",
            "regex": "(.*)",
        }

        # label all static configs in the Prometheus job
        # labeling inserts Juju topology information and
        # sets a relable config for instance labels
        for static_config in static_configs:
            labels = static_config.get("labels", {}) if static_configs else {}
            all_targets = static_config.get("targets", [])

            # split all targets into those which will have unit labels
            # and those which will not
            ports = []
            unitless_targets = []
            for target in all_targets:
                host, port = target.split(":")
                if host.strip() == "*":
                    ports.append(port.strip())
                else:
                    unitless_targets.append(target)

            # label scrape targets that do not have unit labels
            if unitless_targets:
                unitless_config = self._labeled_unitless_config(
                    unitless_targets, labels, scrape_metadata
                )
                labeled_job["static_configs"].append(unitless_config)

            # label scrape targets that do have unit labels
            for host_name, host_address in hosts.items():
                static_config = self._labeled_unit_config(
                    host_name, host_address, ports, labels, scrape_metadata
                )
                labeled_job["static_configs"].append(static_config)
                if "juju_unit" not in instance_relabel_config["source_labels"]:
                    instance_relabel_config["source_labels"].append("juju_unit")  # type: ignore

        # ensure topology relabeling of instance label is last in order of relabelings
        relabel_configs = job.get("relabel_configs", [])
        relabel_configs.append(instance_relabel_config)
        labeled_job["relabel_configs"] = relabel_configs

        return labeled_job

    def _set_juju_labels(self, labels, scrape_metadata) -> dict:
        """Create a copy of metric labels with Juju topology information.

        Args:
            labels: a dictionary containing Prometheus metric labels.
            scrape_metadata: scrape related metadata provided by
                `MetricsEndpointProvider`.

        Returns:
            a copy of the `labels` dictionary augmented with Juju
            topology information with the exception of unit name.
        """
        juju_labels = labels.copy()  # deep copy not needed
        juju_labels.update(
            ProviderTopology.from_relation_data(scrape_metadata).as_promql_label_dict()
        )

        return juju_labels

    def _labeled_unitless_config(self, targets, labels, scrape_metadata) -> dict:
        """Static scrape configuration for fully qualified host addresses.

        Fully qualified hosts are those scrape targets for which the
        address are specified by the `MetricsEndpointProvider` as part
        of the scrape job specification set in application relation data.
        The address specified need not belong to any unit of the
        `MetricsEndpointProvider` charm. As a result there is no reliable
        way to determine the name (Juju topology unit name) for such a
        target.

        Args:
            targets: a list of addresses of fully qualified hosts.
            labels: labels specified by `MetricsEndpointProvider` clients
                 which are associated with `targets`.
            scrape_metadata: scrape related metadata provided by `MetricsEndpointProvider`.

        Returns:
            A dictionary containing the static scrape configuration
            for a list of fully qualified hosts.
        """
        juju_labels = self._set_juju_labels(labels, scrape_metadata)
        unitless_config = {"targets": targets, "labels": juju_labels}
        return unitless_config

    def _labeled_unit_config(
        self, unit_name, host_address, ports, labels, scrape_metadata
    ) -> dict:
        """Static scrape configuration for a wildcard host.

        Wildcard hosts are those scrape targets whose name (Juju unit
        name) and address (unit IP address) is set into unit relation
        data by the `MetricsEndpointProvider` charm, which sets this
        data for ALL its units.

        Args:
            unit_name: a string representing the unit name of the wildcard host.
            host_address: a string representing the address of the wildcard host.
            ports: list of ports on which this wildcard host exposes its metrics.
            labels: a dictionary of labels provided by
                `MetricsEndpointProvider` intended to be associated with
                this wildcard host.
            scrape_metadata: scrape related metadata provided by `MetricsEndpointProvider`.

        Returns:
            A dictionary containing the static scrape configuration
            for a single wildcard host.
        """
        juju_labels = self._set_juju_labels(labels, scrape_metadata)

        juju_labels["juju_unit"] = unit_name

        static_config = {"labels": juju_labels}

        if ports:
            targets = []
            for port in ports:
                targets.append("{}:{}".format(host_address, port))
            static_config["targets"] = targets  # type: ignore
        else:
            static_config["targets"] = [host_address]  # type: ignore

        return static_config


def _resolve_dir_against_charm_path(charm: CharmBase, *path_elements: str) -> str:
    """Resolve the provided path items against the directory of the main file.

    Look up the directory of the `main.py` file being executed. This is normally
    going to be the charm.py file of the charm including this library. Then, resolve
    the provided path elements and, if the result path exists and is a directory,
    return its absolute path; otherwise, raise en exception.

    Raises:
        InvalidAlertRulePathError, if the path does not exist or is not a directory.
    """
    charm_dir = Path(str(charm.charm_dir))
    if not charm_dir.exists() or not charm_dir.is_dir():
        # Operator Framework does not currently expose a robust
        # way to determine the top level charm source directory
        # that is consistent across deployed charms and unit tests
        # Hence for unit tests the current working directory is used
        # TODO: updated this logic when the following ticket is resolved
        # https://github.com/canonical/operator/issues/643
        charm_dir = Path(os.getcwd())

    alerts_dir_path = charm_dir.absolute().joinpath(*path_elements)

    if not alerts_dir_path.exists():
        raise InvalidAlertRulePathError(alerts_dir_path, "directory does not exist")
    if not alerts_dir_path.is_dir():
        raise InvalidAlertRulePathError(alerts_dir_path, "is not a directory")

    return str(alerts_dir_path)


class MetricsEndpointProvider(Object):
    """A metrics endpoint for Prometheus."""

    def __init__(
        self,
        charm,
        relation_name: str = DEFAULT_RELATION_NAME,
        jobs=None,
        alert_rules_path: str = DEFAULT_ALERT_RULES_RELATIVE_PATH,
    ):
        """Construct a metrics provider for a Prometheus charm.

        If your charm exposes a Prometheus metrics endpoint, the
        `MetricsEndpointProvider` object enables your charm to easily
        communicate how to reach that metrics endpoint.

        By default, a charm instantiating this object has the metrics
        endpoints of each of its units scraped by the related Prometheus
        charms. The scraped metrics are automatically tagged by the
        Prometheus charms with Juju topology data via the
        `juju_model_name`, `juju_model_uuid`, `juju_application_name`
        and `juju_unit` labels. To support such tagging `MetricsEndpointProvider`
        automatically forwards scrape metadata to a `MetricsEndpointConsumer`
        (Prometheus charm).

        Scrape targets provided by `MetricsEndpointProvider` can be
        customized when instantiating this object. For example in the
        case of a charm exposing the metrics endpoint for each of its
        units on port 8080 and the `/metrics` path, the
        `MetricsEndpointProvider` can be instantiated as follows:

            self.metrics_endpoint_provider = MetricsEndpointProvider(
                self,
                jobs=[{
                    "static_configs": [{"targets": ["*:8080"]}],
                }])

        The notation `*:<port>` means "scrape each unit of this charm on port
        `<port>`.

        In case the metrics endpoints are not on the standard `/metrics` path,
        a custom path can be specified as follows:

            self.metrics_endpoint_provider = MetricsEndpointProvider(
                self,
                jobs=[{
                    "metrics_path": "/my/strange/metrics/path",
                    "static_configs": [{"targets": ["*:8080"]}],
                }])

        Note how the `jobs` argument is a list: this allows you to expose multiple
        combinations of paths "metrics_path" and "static_configs" in case your charm
        exposes multiple endpoints, which could happen, for example, when you have
        multiple workload containers, with applications in each needing to be scraped.
        The structure of the objects in the `jobs` list is one-to-one with the
        `scrape_config` configuration item of Prometheus' own configuration (see
        https://prometheus.io/docs/prometheus/latest/configuration/configuration/#scrape_config
        ), but with only a subset of the fields allowed. The permitted fields are
        listed in `ALLOWED_KEYS` object in this charm library module.

        It is also possible to specify alert rules. By default, this library will look
        into the `<charm_parent_dir>/prometheus_alert_rules`, which in a standard charm
        layouts resolves to `src/prometheus_alert_rules`. Each alert rule goes into a
        separate `*.rule` file. If the syntax of a rule is invalid,
        the  `MetricsEndpointProvider` logs an error and does not load the particular
        rule.

        To avoid false positives and negatives in the evaluation of alert rules,
        all ingested alert rule expressions are automatically qualified using Juju
        Topology filters. This ensures that alert rules provided by your charm, trigger
        alerts based only on data scrapped from your charm. For example an alert rule
        such as the following

            alert: UnitUnavailable
            expr: up < 1
            for: 0m

        will be automatically transformed into something along the lines of the following

            alert: UnitUnavailable
            expr: up{juju_model=<model>, juju_model_uuid=<uuid-prefix>, juju_application=<app>} < 1
            for: 0m

        Args:
            charm: a `CharmBase` object that manages this
                `MetricsEndpointProvider` object. Typically this is
                `self` in the instantiating class.
            relation_name: an optional string name of the relation between `charm`
                and the Prometheus charmed service. The default is "metrics-endpoint".
                It is strongly advised not to change the default, so that people
                deploying your charm will have a consistent experience with all
                other charms that provide metrics endpoints.
            jobs: an optional list of dictionaries where each
                dictionary represents the Prometheus scrape
                configuration for a single job. When not provided, a
                default scrape configuration is provided for the
                `/metrics` endpoint polling all units of the charm on port `80`
                using the `MetricsEndpointProvider` object.
            alert_rules_path: an optional path for the location of alert rules
                files.  Defaults to "./prometheus_alert_rules",
                resolved relative to the directory hosting the charm entry file.
                The alert rules are automatically updated on charm upgrade.

        Raises:
            RelationNotFoundError: If there is no relation in the charm's metadata.yaml
                with the same name as provided via `relation_name` argument.
            RelationInterfaceMismatchError: The relation with the same name as provided
                via `relation_name` argument does not have the `prometheus_scrape` relation
                interface.
            RelationRoleMismatchError: If the relation with the same name as provided
                via `relation_name` argument does not have the `RelationRole.provides`
                role.
        """
        _validate_relation_by_interface_and_direction(
            charm, relation_name, RELATION_INTERFACE_NAME, RelationRole.provides
        )

        super().__init__(charm, relation_name)
        self.topology = ProviderTopology.from_charm(charm)

        self._charm = charm
        self._alert_rules_path = None
        self.set_alert_path(alert_rules_path)
        self._relation_name = relation_name
        self._jobs = None
        self.set_jobs(jobs or [])

        events = self._charm.on[self._relation_name]
        self.framework.observe(events.relation_joined, self.set_scrape_job_spec)
        self.framework.observe(events.relation_changed, self.set_scrape_job_spec)

        # dirty fix: set the ip address when the containers start, as a workaround
        # for not being able to lookup the pod ip
        for container_name in charm.unit.containers:
            self.framework.observe(
                charm.on[container_name].pebble_ready,
                self._set_unit_ip,
            )

        self.framework.observe(self._charm.on.upgrade_charm, self.set_scrape_job_spec)

    def set_jobs(self, jobs: list):
        """Set jobs."""
        # sanitize job configurations to the supported subset of parameters
        self._jobs = [_sanitize_scrape_configuration(job) for job in jobs]

    def set_alert_path(self, alert_rules_path):
        """Set alert rules' path."""
        try:
            self._alert_rules_path = _resolve_dir_against_charm_path(
                self._charm, alert_rules_path
            )
        except InvalidAlertRulePathError as e:
            logger.warning(
                "Invalid Prometheus alert rules folder at %s: %s",
                e.alert_rules_absolute_path,
                e.message,
            )
            self._alert_rules_path = alert_rules_path

    def set_scrape_job_spec(self, event=None):
        """Ensure scrape target information is made available to prometheus.

        When a metrics provider charm is related to a prometheus charm, the
        metrics provider sets specification and metadata related to its own
        scrape configuration. This information is set using Juju application
        data. In addition each of the consumer units also sets its own
        host address in Juju unit relation data.
        """
        self._set_unit_ip(event)

        if not self._charm.unit.is_leader():
            return

        alert_rules = AlertRules(topology=self.topology)
        alert_rules.add_path(self._alert_rules_path, recursive=True)
        alert_rules_as_dict = alert_rules.as_dict()

        for relation in self._charm.model.relations[self._relation_name]:
            relation.data[self._charm.app]["scrape_metadata"] = json.dumps(
                self._scrape_metadata
            )
            relation.data[self._charm.app]["scrape_jobs"] = json.dumps(
                self._scrape_jobs
            )

            if alert_rules_as_dict:
                # Update relation data with the string representation of the rule file.
                # Juju topology is already included in the "scrape_metadata" field above.
                # The consumer side of the relation uses this information to name the rules file
                # that is written to the filesystem.
                relation.data[self._charm.app]["alert_rules"] = json.dumps(
                    alert_rules_as_dict
                )

    def _set_unit_ip(self, _):
        """Set unit host address.

        Each time a metrics provider charm container is restarted it updates its own
        host address in the unit relation data for the prometheus charm.

        The only argument specified is an event and it ignored. this is for expediency
        to be able to use this method as an event handler, although no access to the
        event is actually needed.
        """
        for relation in self._charm.model.relations[self._relation_name]:
            relation.data[self._charm.unit]["prometheus_scrape_unit_address"] = str(
                self._charm.model.get_binding(relation).network.bind_address
            )
            relation.data[self._charm.unit]["prometheus_scrape_unit_name"] = str(
                self._charm.model.unit.name
            )

    @property
    def _scrape_jobs(self) -> list:
        """Fetch list of scrape jobs.

        Returns:
           A list of dictionaries, where each dictionary specifies a
           single scrape job for Prometheus.
        """
        return self._jobs if self._jobs else [DEFAULT_JOB]

    @property
    def _scrape_metadata(self) -> dict:
        """Generate scrape metadata.

        Returns:
            Scrape configuration metadata for this metrics provider charm.
        """
        return self.topology.as_dict()


class PrometheusRulesProvider(Object):
    """Forward rules to Prometheus.

    This object may be used to forward rules to Prometheus. At present it only supports
    forwarding alert rules. This is unlike :class:`MetricsEndpointProvider`, which
    is used for forwarding both scrape targets and associated alert rules. This object
    is typically used when there is a desire to forward rules that apply globally (across
    all deployed charms and units) rather than to a single charm. All rule files are
    forwarded using the same 'prometheus_scrape' interface that is also used by
    `MetricsEndpointProvider`.

    Args:
        charm: A charm instance that `provides` a relation with the `prometheus_scrape` interface.
        relation_name: Name of the relation in `metadata.yaml` that
            has the `prometheus_scrape` interface.
        dir_path: Root directory for the collection of rule files.
        recursive: Whether or not to scan for rule files recursively.
    """

    def __init__(
        self,
        charm: CharmBase,
        relation_name: str = DEFAULT_RELATION_NAME,
        dir_path: str = DEFAULT_ALERT_RULES_RELATIVE_PATH,
        recursive=True,
    ):
        super().__init__(charm, relation_name)
        self._charm = charm
        self._relation_name = relation_name
        self.topology = ProviderTopology.from_charm(charm)
        self._recursive = recursive

        try:
            dir_path = _resolve_dir_against_charm_path(charm, dir_path)
        except InvalidAlertRulePathError as e:
            logger.warning(
                "Invalid Prometheus alert rules folder at %s: %s",
                e.alert_rules_absolute_path,
                e.message,
            )
        self.dir_path = dir_path

        events = self._charm.on[self._relation_name]
        event_sources = [
            events.relation_joined,
            events.relation_changed,
            self._charm.on.leader_elected,
            self._charm.on.upgrade_charm,
        ]

        for event_source in event_sources:
            self.framework.observe(event_source, self._update_relation_data)

    def _reinitialize_alert_rules(self):
        """Reloads alert rules and updates all relations."""
        self._update_relation_data(None)

    def _update_relation_data(self, _):
        """Update application relation data with alert rules for all relations."""
        if not self._charm.unit.is_leader():
            return

        alert_rules = AlertRules()
        alert_rules.add_path(self.dir_path, recursive=self._recursive)
        alert_rules_as_dict = alert_rules.as_dict()

        logger.info("Updating relation data with rule files from disk")
        for relation in self._charm.model.relations[self._relation_name]:
            relation.data[self._charm.app]["alert_rules"] = json.dumps(
                alert_rules_as_dict,
                sort_keys=True,  # sort, to prevent unnecessary relation_changed events
            )


class MetricsEndpointAggregator(Object):
    """Aggregate metrics from multiple scrape targets.

    `MetricsEndpointAggregator` collects scrape target information from one
    or more related charms and forwards this to a `MetricsEndpointConsumer`
    charm, which may be in a different Juju model. However it is
    essential that `MetricsEndpointAggregator` itself resides in the same
    model as its scrape targets, as this is currently the only way to
    ensure in Juju that the `MetricsEndpointAggregator` will be able to
    determine the model name and uuid of the scrape targets.

    `MetricsEndpointAggregator` should be used in place of
    `MetricsEndpointProvider` in the following two use cases:

    1. Integrating one or more scrape targets that do not support the
    `prometheus_scrape` interface.

    2. Integrating one or more scrape targets through cross model
    relations. Although the [Scrape Config Operator](https://charmhub.io/cos-configuration-k8s)
    may also be used for the purpose of supporting cross model
    relations.

    Using `MetricsEndpointAggregator` to build a Prometheus charm client
    only requires instantiating it. Instantiating
    `MetricsEndpointAggregator` is similar to `MetricsEndpointProvider` except
    that it requires specifying the names of three relations: the
    relation with scrape targets, the relation for alert rules, and
    that with the Prometheus charms. For example

    ```python
    self._aggregator = MetricsEndpointAggregator(
        self,
        {
            "prometheus": "monitoring",
            "scrape_target": "prometheus-target",
            "alert_rules": "prometheus-rules"
        }
    )
    ```

    `MetricsEndpointAggregator` assumes that each unit of a scrape target
    sets in its unit-level relation data two entries with keys
    "hostname" and "port". If it is required to integrate with charms
    that do not honor these assumptions, it is always possible to
    derive from `MetricsEndpointAggregator` overriding the `_get_targets()`
    method, which is responsible for aggregating the unit name, host
    address ("hostname") and port of the scrape target.

    `MetricsEndpointAggregator` also assumes that each unit of a
    scrape target sets in its unit-level relation data a key named
    "groups". The value of this key is expected to be the string
    representation of list of Prometheus Alert rules in YAML format.
    An example of a single such alert rule is

    ```yaml
    - alert: HighRequestLatency
      expr: job:request_latency_seconds:mean5m{job="myjob"} > 0.5
      for: 10m
      labels:
        severity: page
      annotations:
        summary: High request latency
    ```

    Once again if it is required to integrate with charms that do not
    honour these assumptions about alert rules then an object derived
    from `MetricsEndpointAggregator` may be used by overriding the
    `_get_alert_rules()` method.

    `MetricsEndpointAggregator` ensures that Prometheus scrape job
    specifications and alert rules are annotated with Juju topology
    information, just like `MetricsEndpointProvider` and
    `MetricsEndpointConsumer` do.

    By default `MetricsEndpointAggregator` ensures that Prometheus
    "instance" labels refer to Juju topology. This ensures that
    instance labels are stable over unit recreation. While it is not
    advisable to change this option, if required it can be done by
    setting the "relabel_instance" keyword argument to `False` when
    constructing an aggregator object.
    """

    def __init__(self, charm, relation_names, relabel_instance=True):
        """Construct a `MetricsEndpointAggregator`.

        Args:
            charm: a `CharmBase` object that manages this
                `MetricsEndpointAggregator` object. Typically this is
                `self` in the instantiating class.
            relation_names: a dictionary with three keys. The value
                of the "scrape_target" and "alert_rules" keys are
                the relation names over which scrape job and alert rule
                information is gathered by this `MetricsEndpointAggregator`.
                And the value of the "prometheus" key is the name of
                the relation with a `MetricsEndpointConsumer` such as
                the Prometheus charm.
            relabel_instance: A boolean flag indicating if Prometheus
                scrape job "instance" labels must refer to Juju Topology.
        """
        super().__init__(charm, relation_names["prometheus"])

        self._charm = charm
        self._target_relation = relation_names["scrape_target"]
        self._prometheus_relation = relation_names["prometheus"]
        self._alert_rules_relation = relation_names["alert_rules"]
        self._relabel_instance = relabel_instance

        # manage Prometheus charm relation events
        prometheus_events = self._charm.on[self._prometheus_relation]
        self.framework.observe(
            prometheus_events.relation_joined, self._set_prometheus_data
        )

        # manage list of Prometheus scrape jobs from related scrape targets
        target_events = self._charm.on[self._target_relation]
        self.framework.observe(
            target_events.relation_changed, self._update_prometheus_jobs
        )
        self.framework.observe(
            target_events.relation_departed, self._remove_prometheus_jobs
        )

        # manage alert rules for Prometheus from related scrape targets
        alert_rule_events = self._charm.on[self._alert_rules_relation]
        self.framework.observe(
            alert_rule_events.relation_changed, self._update_alert_rules
        )
        self.framework.observe(
            alert_rule_events.relation_departed, self._remove_alert_rules
        )

    def _set_prometheus_data(self, event):
        """Ensure every new Prometheus instances is updated.

        Any time a new Prometheus unit joins the relation with
        `MetricsEndpointAggregator`, that Prometheus unit is provided
        with the complete set of existing scrape jobs and alert rules.
        """
        jobs = []  # list of scrape jobs, one per relation
        for relation in self.model.relations[self._target_relation]:
            targets = self._get_targets(relation)
            if targets:
                jobs.append(self._static_scrape_job(targets, relation.app.name))

        groups = []  # list of alert rule groups, one group per relation
        for relation in self.model.relations[self._alert_rules_relation]:
            unit_rules = self._get_alert_rules(relation)
            if unit_rules:
                appname = relation.app.name
                rules = self._label_alert_rules(unit_rules, appname)
                group = {"name": self._group_name(appname), "rules": rules}
                groups.append(group)

        event.relation.data[self._charm.app]["scrape_jobs"] = json.dumps(jobs)
        event.relation.data[self._charm.app]["alert_rules"] = json.dumps(
            {"groups": groups}
        )

    def _set_target_job_data(self, targets: dict, app_name: str, **kwargs) -> None:
        """Update scrape jobs in response to scrape target changes.

        When there is any change in relation data with any scrape
        target, the Prometheus scrape job, for that specific target is
        updated. Additionally, if this method is called manually, do the
        sameself.

        Args:
            targets: a `dict` containing target information
            app_name: a `str` identifying the application
        """
        # new scrape job for the relation that has changed
        updated_job = self._static_scrape_job(targets, app_name, **kwargs)

        for relation in self.model.relations[self._prometheus_relation]:
            jobs = json.loads(relation.data[self._charm.app].get("scrape_jobs", "[]"))
            # list of scrape jobs that have not changed
            jobs = [job for job in jobs if updated_job["job_name"] != job["job_name"]]
            jobs.append(updated_job)
            relation.data[self._charm.app]["scrape_jobs"] = json.dumps(jobs)

    def _update_prometheus_jobs(self, event):
        """Update scrape jobs in response to scrape target changes.

        When there is any change in relation data with any scrape
        target, the Prometheus scrape job, for that specific target is
        updated.
        """
        targets = self._get_targets(event.relation)
        if not targets:
            return

        # new scrape job for the relation that has changed
        updated_job = self._static_scrape_job(targets, event.relation.app.name)

        for relation in self.model.relations[self._prometheus_relation]:
            jobs = json.loads(relation.data[self._charm.app].get("scrape_jobs", "[]"))
            # list of scrape jobs that have not changed
            jobs = [job for job in jobs if updated_job["job_name"] != job["job_name"]]
            jobs.append(updated_job)
            relation.data[self._charm.app]["scrape_jobs"] = json.dumps(jobs)

    def _remove_prometheus_jobs(self, event):
        """Remove scrape jobs when a target departs.

        Any time a scrape target departs, any Prometheus scrape job
        associated with that specific scrape target is removed.
        """
        job_name = self._job_name(event.relation.app.name)
        unit_name = event.unit.name

        for relation in self.model.relations[self._prometheus_relation]:
            jobs = json.loads(relation.data[self._charm.app].get("scrape_jobs", "[]"))
            if not jobs:
                continue

            changed_job = [j for j in jobs if j.get("job_name") == job_name]
            if not changed_job:
                continue
            changed_job = changed_job[0]

            # list of scrape jobs that have not changed
            jobs = [job for job in jobs if job.get("job_name") != job_name]

            # list of scrape jobs for units of the same application that still exist
            configs_kept = [
                config
                for config in changed_job["static_configs"]  # type: ignore
                if config.get("labels", {}).get("juju_unit") != unit_name
            ]

            if configs_kept:
                changed_job["static_configs"] = configs_kept  # type: ignore
                jobs.append(changed_job)

            relation.data[self._charm.app]["scrape_jobs"] = json.dumps(jobs)

    def _update_alert_rules(self, event):
        """Update alert rules in response to scrape target changes.

        When there is any change in alert rule relation data for any
        scrape target, the list of alert rules for that specific
        target is updated.
        """
        unit_rules = self._get_alert_rules(event.relation)
        if not unit_rules:
            return

        appname = event.relation.app.name
        rules = self._label_alert_rules(unit_rules, appname)
        # the alert rule group that has changed
        updated_group = {"name": self._group_name(appname), "rules": rules}

        for relation in self.model.relations[self._prometheus_relation]:
            alert_rules = json.loads(
                relation.data[self._charm.app].get("alert_rules", "{}")
            )
            groups = alert_rules.get("groups", [])
            # list of alert rule groups that have not changed
            groups = [
                group for group in groups if updated_group["name"] != group["name"]
            ]
            groups.append(updated_group)
            relation.data[self._charm.app]["alert_rules"] = json.dumps(
                {"groups": groups}
            )

    def _remove_alert_rules(self, event):
        """Remove alert rules for departed targets.

        Any time a scrape target departs any alert rules associated
        with that specific scrape target is removed.
        """
        group_name = self._group_name(event.relation.app.name)
        unit_name = event.unit.name

        for relation in self.model.relations[self._prometheus_relation]:
            alert_rules = json.loads(
                relation.data[self._charm.app].get("alert_rules", "{}")
            )
            if not alert_rules:
                continue

            groups = alert_rules.get("groups", [])
            if not groups:
                continue

            changed_group = [group for group in groups if group["name"] == group_name]
            if not changed_group:
                continue
            changed_group = changed_group[0]

            # list of alert rule groups that have not changed
            groups = [group for group in groups if group["name"] != group_name]

            # list of alert rules not associated with departing unit
            rules_kept = [
                rule
                for rule in changed_group.get("rules")  # type: ignore
                if rule.get("labels").get("juju_unit") != unit_name
            ]

            if rules_kept:
                changed_group["rules"] = rules_kept  # type: ignore
                groups.append(changed_group)

            relation.data[self._charm.app]["alert_rules"] = (
                json.dumps({"groups": groups}) if groups else "{}"
            )

    def _get_targets(self, relation) -> dict:
        """Fetch scrape targets for a relation.

        Scrape target information is returned for each unit in the
        relation. This information contains the unit name, network
        hostname (or address) for that unit, and port on which an
        metrics endpoint is exposed in that unit.

        Args:
            relation: an `ops.model.Relation` object for which scrape
                targets are required.

        Returns:
            a dictionary whose keys are names of the units in the
            relation. There values associated with each key is itself
            a dictionary of the form
            ```
            {"hostname": hostname, "port": port}
            ```
        """
        targets = {}
        for unit in relation.units:
            port = relation.data[unit].get("port", 80)
            hostname = relation.data[unit].get("hostname")
            if hostname:
                targets.update({unit.name: {"hostname": hostname, "port": port}})

        return targets

    def _get_alert_rules(self, relation) -> dict:
        """Fetch alert rules for a relation.

        Each unit of the related scrape target may have its own
        associated alert rules. Alert rules for all units are returned
        indexed by unit name.

        Args:
            relation: an `ops.model.Relation` object for which alert
                rules are required.

        Returns:
            a dictionary whose keys are names of the units in the
            relation. There values associated with each key is a list
            of alert rules. Each rule is in dictionary format. The
            structure "rule dictionary" corresponds to single
            Prometheus alert rule.
        """
        rules = {}
        for unit in relation.units:
            unit_rules = yaml.safe_load(relation.data[unit].get("groups", ""))
            if unit_rules:
                rules.update({unit.name: unit_rules})

        return rules

    def _job_name(self, appname) -> str:
        """Construct a scrape job name.

        Each relation has its own unique scrape job name. All units in
        the relation are scraped as part of the same scrape job.

        Args:
            appname: string name of a related application.

        Returns:
            a string Prometheus scrape job name for the application.
        """
        return "juju_{}_{}_{}_prometheus_scrape".format(
            self.model.name, self.model.uuid[:7], appname
        )

    def _group_name(self, appname) -> str:
        """Construct name for an alert rule group.

        Each unit in a relation may define its own alert rules. All
        rules, for all units in a relation are grouped together and
        given a single alert rule group name.

        Args:
            appname: string name of a related application.

        Returns:
            a string Prometheus alert rules group name for the application.
        """
        return "juju_{}_{}_{}_alert_rules".format(
            self.model.name, self.model.uuid[:7], appname
        )

    def _label_alert_rules(self, unit_rules, appname) -> list:
        """Apply juju topology labels to alert rules.

        Args:
            unit_rules: a list of alert rules, where each rule is in
                dictionary format.
            appname: a string name of the application to which the
                alert rules belong.

        Returns:
            a list of alert rules with Juju topology labels.
        """
        labeled_rules = []
        for unit_name, rules in unit_rules.items():
            for rule in rules:
                rule["labels"].update(
                    AggregatorTopology.create(
                        self.model.name, self.model.uuid, appname, unit_name
                    ).as_promql_label_dict()
                )
                labeled_rules.append(rule)

        return labeled_rules

    def _static_scrape_job(self, targets, application_name, **kwargs) -> dict:
        """Construct a static scrape job for an application.

        Args:
            targets: a dictionary providing hostname and port for all
                scrape target. The keys of this dictionary are unit
                names. Values corresponding to these keys are
                themselves a dictionary with keys "hostname" and
                "port".
            application_name: a string name of the application for
                which this static scrape job is being constructed.

        Returns:
            A dictionary corresponding to a Prometheus static scrape
            job configuration for one application. The returned
            dictionary may be transformed into YAML and appended to
            the list of any existing list of Prometheus static configs.
        """
        juju_model = self.model.name
        juju_model_uuid = self.model.uuid
        job = {
            "job_name": self._job_name(application_name),
            "static_configs": [
                {
                    "targets": ["{}:{}".format(target["hostname"], target["port"])],
                    "labels": {
                        "juju_model": juju_model,
                        "juju_model_uuid": juju_model_uuid,
                        "juju_application": application_name,
                        "juju_unit": unit_name,
                        "host": target["hostname"],
                    },
                }
                for unit_name, target in targets.items()
            ],
            "relabel_configs": self._relabel_configs
            + kwargs.get("relabel_configs", []),
        }
        job.update(kwargs.get("updates", {}))

        return job

    @property
    def _relabel_configs(self) -> list:
        """Create Juju topology relabeling configuration.

        Using Juju topology for instance labels ensures that these
        labels are stable across unit recreation.

        Returns:
            a list of Prometheus relabling configurations. Each item in
            this list is one relabel configuration.
        """
        return (
            [
                {
                    "source_labels": [
                        "juju_model",
                        "juju_model_uuid",
                        "juju_application",
                        "juju_unit",
                    ],
                    "separator": "_",
                    "target_label": "instance",
                    "regex": "(.*)",
                }
            ]
            if self._relabel_instance
            else []
        )


class PromqlTransformer:
    """Uses promql-transform to inject label matchers into alert rule expressions."""

    _path = None
    _disabled = False

    @property
    def path(self):
        """Lazy lookup of the path of promql-transform."""
        if self._disabled:
            return None
        if not self._path:
            self._path = self._get_transformer_path()
            if not self._path:
                logger.debug("Skipping injection of juju topology as label matchers")
                self._disabled = True
        return self._path

    def __init__(self, charm):
        self._charm = charm

    def apply_label_matchers(self, rules):
        """Will apply label matchers to the expression of all alerts in all supplied groups."""
        if not self.path:
            return rules
        for group in rules["groups"]:
            rules_in_group = group.get("rules", [])
            for rule in rules_in_group:
                topology = {}
                # if the user for some reason has provided juju_unit, we'll need to honor it
                # in most cases, however, this will be empty
                for label in [
                    "juju_model",
                    "juju_model_uuid",
                    "juju_application",
                    "juju_charm",
                    "juju_unit",
                ]:
                    if label in rule["labels"]:
                        topology[label] = rule["labels"][label]

                rule["expr"] = self._apply_label_matcher(rule["expr"], topology)
        return rules

    def _apply_label_matcher(self, expression, topology):
        if not topology:
            return expression
        if not self.path:
            logger.debug(
                "`promql-transform` unavailable. leaving expression unchanged: %s",
                expression,
            )
            return expression
        args = [str(self.path)]
        args.extend(
            [
                "--label-matcher={}={}".format(key, value)
                for key, value in topology.items()
            ]
        )

        args.extend(["{}".format(expression)])
        # noinspection PyBroadException
        try:
            return self._exec(args)
        except Exception as e:
            logger.debug(
                'Applying the expression failed: "{}", falling back to the original', e
            )
            return expression

    def _get_transformer_path(self) -> Optional[Path]:
        arch = platform.processor()
        arch = "amd64" if arch == "x86_64" else arch
        res = "promql-transform-{}".format(arch)
        try:
            path = self._charm.model.resources.fetch(res)
            os.chmod(path, 0o777)
            return path
        except NotImplementedError:
            logger.debug("System lacks support for chmod")
        except (NameError, ModelError):
            logger.debug('No resource available for the platform "{}"'.format(arch))
        return None

    def _exec(self, cmd):
        result = subprocess.run(cmd, check=False, stdout=subprocess.PIPE)
        output = result.stdout.decode("utf-8").strip()
        return output
