"""Markers of the contract tests."""


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "private_fixtures: parse the operator's recorded broker payloads (COUNCIL_PRIVATE_FIXTURES); "
        "skipped unless the operator context passes",
    )
