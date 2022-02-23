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
        "ops>=1.0.0",
        "ops_reactive_interface",
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
            "../examples/prometheus-scrape-provider/*",
            "../examples/prometheus-scrape-provider/src/*",
            "../examples/prometheus-scrape-provider/src/prometheus_alert_rules/*"
            "../examples/prometheus-scrape-consumer/*",
            "../examples/prometheus-scrape-consumer/src/*",
        ],
    },
    include_package_data=True,
    entry_points={
        "ops_reactive_interface.provides": "prometheus-scrape = prometheus_scrape:MetricsEndpointProvider",
        "ops_reactive_interface.requires": "prometheus-scrape = prometheus_scrape:MetricsEndpointConsumer",
        "pytest11": [
            "prometheus-scrape-test-charm = prometheus_scrape.pytest_plugin",
        ]
    },
)
