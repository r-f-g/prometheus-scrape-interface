import shutil
from contextlib import contextmanager
from pathlib import Path

import pytest


try:
    from importlib.resources import path as resource_path
except ImportError:
    # Shims for importlib_resources and pkg_resources to behave like the stdlib
    # version from 3.7+.
    try:
        from importlib_resources import path as resource_path
    except ImportError:
        from pkg_resources import resource_filename

        @contextmanager
        def resource_path(package, resource):
            rf = resource_filename(package, resource)
            yield Path(rf)


@pytest.fixture(scope="module")
def prometheus_scrape_charms(ops_test):
    """Fixture which provides example charms using the prometheus-scrape for testing.

    This fixture returns an object with the following attributes:

      * prometheus_scrape_provider - An operator charm which provides prometheus-scrape

    Each of these will need to be passed to `ops_test.build_charm()`.
    """
    return PrometheusScrapeCharms(ops_test)


class PrometheusScrapeCharms:
    def __init__(self, ops_test):
        self._ops_test = ops_test
        self._provider_operator = None

    def render(self, charm_resource: str, lib_path: Path):
        with resource_path("prometheus_scrape", "examples") as path:
            charm_dst_path = self._ops_test.render_charm(
                path / charm_resource,
                include=["wheelhouse.txt", "requirements.txt"],
                context={"lib_path": lib_path},
            )
            return charm_dst_path
