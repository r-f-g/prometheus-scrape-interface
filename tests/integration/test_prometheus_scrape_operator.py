import logging

import pytest
import requests

logger = logging.getLogger(__name__)


@pytest.mark.abort_on_fail
async def test_build_and_deploy(ops_test, prometheus_scrape_charms):
    logger.info("Building prometheus-scrape interface")
    lib_path = await ops_test.build_lib(".")
    logger.info("Building charms")
    # NOTE (rgildein): Building charms separately due to an issue 554 in charmcraft.
    # https://github.com/canonical/charmcraft/issues/554
    charm_provider = await ops_test.build_charm(
        prometheus_scrape_charms.render("operator/prometheus-scrape-provider", lib_path)
    )
    charm_consumer = await ops_test.build_charm(
        prometheus_scrape_charms.render(
            "operator/prometheus-scrape-consumer", lib_path
        ),
    )
    logger.info("Rendering bundle")
    bundle = ops_test.render_bundle(
        "tests/data/bundle-operator.yaml",
        prometheus_scrape_provider=charm_provider,
        prometheus_scrape_consumer=charm_consumer,
    )
    logger.info("Deploying bundle")
    await ops_test.model.deploy(bundle)
    await ops_test.model.wait_for_idle(
        wait_for_active=True, raise_on_blocked=True, timeout=60 * 60
    )


async def test_metrics_endpoint_relation(ops_test):
    """Test add-relation between provider and cunsumer."""
    await ops_test.model.add_relation(
        "prometheus-scrape-provider", "prometheus-scrape-consumer"
    )
    await ops_test.model.wait_for_idle(
        wait_for_active=True, raise_on_blocked=True, timeout=60 * 60
    )
    # check that new target was added
    consumer_unit = ops_test.model.applications["prometheus-scrape-consumer"].units[0]
    response = requests.get(
        f"http://{consumer_unit.public_address}:9090/api/v1/targets"
    )
    assert response.status_code == 200
    assert len(response.json()["data"]["activeTargets"])
