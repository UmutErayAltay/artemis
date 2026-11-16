"""Her kayıtlı aracın ve argümanın Türkçe adı olmalı (yeni araç eklenince bu test kırılır)."""

from artemis.channels.desktop.tool_titles import _ARGS, TOOL_TITLES, tool_display, tool_summary
from artemis.tools.registry import discover_tools


def test_every_registered_tool_and_arg_has_turkish_text() -> None:
    for d in discover_tools().definitions():
        assert d.name in TOOL_TITLES, d.name
        assert tool_summary(d.name), d.name
        for arg in d.parameters.get("properties", {}):
            assert arg in _ARGS, f"{d.name}.{arg}"


def test_unknown_tool_falls_back_to_id() -> None:
    assert tool_display("mcp_x") == "mcp_x"
    assert tool_display("web_search") == "Web'de ara (web_search)"
