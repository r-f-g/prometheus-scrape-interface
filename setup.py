from pathlib import Path

from setuptools import find_packages, setup

this_directory = Path(__file__).parent
long_description = (this_directory / "README.md").read_text()

setup(
    name="prometheus-scrape",
    description="'loadbalancer' interface protocol API library",
    long_description_content_type="text/markdown",
    long_description=long_description,
    author="Robert Gildein",
    author_email="robert.gildein@canonical.com",
    install_requires=[
        "ops>=1.0.0",
        "ops_reactive_interface",
    ],
    packages=find_packages(exclude=["tests"]),
    entry_points={
        # TODO: edit this
        "ops_reactive_interface.requires": "speaking = speaking_interface:Listener",
    },
)
