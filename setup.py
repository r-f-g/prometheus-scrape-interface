from pathlib import Path

from setuptools import setup

this_directory = Path(__file__).parent
long_description = (this_directory / "README.md").read_text()

setup(
    name="prometheus-scrape",
    version="0.1",
    description="'prometheus-scrape' interface to help prometheus collecting metrics",
    long_description_content_type="text/markdown",
    long_description=long_description,
    author="Robert Gildein",
    author_email="robert.gildein@canonical.com",
    install_requires=[
        "ops >= 1.0.0",
        "ops_reactive_interface",
        "pyaml",
    ],
    packages=[
        "prometheus_scrape",
        "prometheus_scrape.examples",  # Synthetic package populated at build time
    ],
    package_dir={
        "prometheus_scrape.examples": "examples",
    },
    package_data={
        "prometheus_scrape.examples": [
            # Charmed Operator Framework
            "../examples/operator/prometheus-scrape-provider/*",
            "../examples/operator/prometheus-scrape-provider/src/*",
            "../examples/operator/prometheus-scrape-provider/src/prometheus_alert_rules/*",
            "../examples/operator/prometheus-scrape-consumer/*",
            "../examples/operator/prometheus-scrape-consumer/src/*",
            # reactive framework
            "../examples/reactive/prometheus-scrape-provider/*",
            "../examples/reactive/prometheus-scrape-provider/reactive/*",
            "../examples/reactive/prometheus-scrape-provider/reactive/prometheus_alert_rules/*",
            "../examples/reactive/prometheus-scrape-consumer/*",
            "../examples/reactive/prometheus-scrape-consumer/reactive/*",
        ],
    },
    include_package_data=True,
    entry_points={
        "ops_reactive_interface.provides": "prometheus-scrape = prometheus_scrape:MetricsEndpointProvider",  # noqa
        "ops_reactive_interface.requires": "prometheus-scrape = prometheus_scrape:MetricsEndpointConsumer",  # noqa
        "pytest11": [
            "prometheus-scrape-test-charm = prometheus_scrape.pytest_plugin",
        ],
    },
)
