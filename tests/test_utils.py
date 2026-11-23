"""get_entry_points finds a group's entry points on every supported Python.

pytest's own console script stands in for a registered group: it is installed
wherever the tests run, whether or not this package is.
"""

import importlib.metadata

from aem_processes.utils import get_entry_points


def test_a_group_that_exists_returns_its_entry_points():
    found = get_entry_points("console_scripts")
    assert isinstance(found["pytest"], importlib.metadata.EntryPoint)
    assert all(name == entry.name for name, entry in found.items())


def test_an_unknown_group_returns_nothing():
    assert get_entry_points("no.such.group") == {}
