from pathlib import Path
from typing import Union

import pytest


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

    def render(self, charm_resource: str, lib_path: Union[str, Path]):
        """Rendering the charms for testing purpose.

        :param charm_resource: it's name of charm in examples
        :param lib_path: it's path to prometheus-scrape library, Path should be provided
        only for charms build with charmcraft
        """
        path = Path(__file__).parent.parent / "examples"
        charm_dst_path = self._ops_test.render_charm(
            path / charm_resource,
            include=["wheelhouse.txt", "requirements.txt"],
            context={"lib_path": lib_path},
        )
        return charm_dst_path
