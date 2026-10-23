"""Dekoratör ve paket keşfi yeni aracın tek dosyada tanımlanmasını sağlar."""

from __future__ import annotations

import importlib
import pkgutil
import re

from artemis.tools.base import BaseTool, ToolDefinition, normalize_risk

_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


def encode_tool_name(name: str) -> str:
    """Kimlik eşlemesi sağlayıcılar arasında sessiz ad çakışmasını önler."""
    if not _NAME_PATTERN.fullmatch(name):
        raise ValueError("Araç adı 1–64 ASCII harf, rakam, alt çizgi veya tire olmalıdır.")
    return name


def decode_tool_name(name: str) -> str:
    return encode_tool_name(name)


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}

    def register(self, cls: type[BaseTool]) -> type[BaseTool]:
        name = encode_tool_name(cls.name)
        if name in self._tools:
            raise ValueError(f"Araç adı zaten kayıtlı: {name}")
        self._tools[name] = cls()
        return cls

    def copy(self) -> ToolRegistry:
        """Aynı araç örnekleriyle yeni registry: runtime'a özel eklemeler globali değiştirmez."""
        clone = ToolRegistry()
        clone._tools = dict(self._tools)
        return clone

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def get(self, name: str) -> BaseTool | None:
        return self._tools.get(name)

    def definitions(self) -> list[ToolDefinition]:
        return [
            ToolDefinition(
                name=encode_tool_name(tool.name),
                description=tool.description,
                parameters=(
                    tool.parameters_schema
                    if tool.parameters_schema is not None
                    else tool.args_model.model_json_schema()
                ),
                risk=normalize_risk(tool.risk),
            )
            for tool in self._tools.values()
        ]


_registry = ToolRegistry()


def register_tool(cls: type[BaseTool]) -> type[BaseTool]:
    return _registry.register(cls)


def discover_tools() -> ToolRegistry:
    """Yeni plugin için merkezi bir import listesini değiştirmek gerekmez."""
    package = importlib.import_module("artemis.tools.plugins")
    for module in sorted(pkgutil.iter_modules(package.__path__), key=lambda item: item.name):
        importlib.import_module(f"{package.__name__}.{module.name}")
    return _registry
