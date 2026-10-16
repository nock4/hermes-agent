"""Regression coverage for tool registration versus toolset exposure."""

import model_tools  # noqa: F401 - imports built-in tools into the registry
from tools.registry import registry
from toolsets import TOOLSETS, get_toolset_names, resolve_toolset


def test_every_static_toolset_direct_tool_is_registered():
    """Static toolsets should not expose misspelled or missing tool names."""
    registered = set(registry.get_all_tool_names())
    missing = {
        toolset_name: sorted(set(toolset.get("tools", [])) - registered)
        for toolset_name, toolset in TOOLSETS.items()
    }
    missing = {name: tools for name, tools in missing.items() if tools}

    assert missing == {}


def test_registered_tools_are_reachable_through_toolsets():
    """A self-registering tool still needs a toolset entry to be usable."""
    registered = set(registry.get_all_tool_names())
    exposed = set()
    for toolset_name in get_toolset_names():
        exposed.update(resolve_toolset(toolset_name))

    assert sorted(registered - exposed) == []
    assert sorted(exposed - registered) == []
