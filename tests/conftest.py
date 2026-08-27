"""Pytest config for the slimserver-python controller-compat suite."""
import pytest


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "contract: live-server controller-compat contract tests "
        "(require an LMS server up on LMS_HTTP/LMS_CLI)",
    )
