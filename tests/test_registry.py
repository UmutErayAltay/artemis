from typing import ClassVar

import pytest
from pydantic import BaseModel, Field, ValidationError

from jarvis.tools.base import BaseTool, Risk, ToolContext, ToolDefinition, ToolResult
from jarvis.tools.registry import ToolRegistry, decode_tool_name, encode_tool_name


class ExampleArgs(BaseModel):
    count: int = Field(ge=1)


class ExampleTool(BaseTool):
    name = "example-tool_1"
    description = "Şema otomatik üretiminin örnek aracı."
    args_model = ExampleArgs
    risk: ClassVar[Risk] = Risk.READ_ONLY

    async def execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult:
        return ToolResult(success=True, status="ok", message="Tamamlandı.")


def test_registration_generates_schema_and_rejects_duplicates() -> None:
    registry = ToolRegistry()
    assert registry.register(ExampleTool) is ExampleTool
    assert isinstance(registry.get("example-tool_1"), ExampleTool)
    assert registry.get("unknown") is None
    definition = registry.definitions()[0]
    assert definition.parameters["properties"]["count"]["minimum"] == 1
    assert definition.parameters["required"] == ["count"]
    assert definition.risk == Risk.READ_ONLY
    with pytest.raises(ValueError, match="zaten kayıtlı"):
        registry.register(ExampleTool)


@pytest.mark.parametrize("name", ["a", "A_1-2", "x" * 64])
def test_provider_name_roundtrip(name: str) -> None:
    assert decode_tool_name(encode_tool_name(name)) == name


@pytest.mark.parametrize("name", ["", "a b", "türkçe", "a.b", "x" * 65, "a\n"])
def test_invalid_provider_name_rejected(name: str) -> None:
    with pytest.raises(ValueError):
        encode_tool_name(name)
    with pytest.raises(ValueError):
        decode_tool_name(name)


def test_unknown_risk_fails_closed() -> None:
    definition = ToolDefinition.model_validate(
        {"name": "test", "description": "", "parameters": {}, "risk": "NEW_RISK"}
    )
    assert definition.risk == Risk.SIDE_EFFECT
    assert BaseTool.risk == Risk.SIDE_EFFECT


@pytest.mark.parametrize(
    ("success", "status"), [(True, "sent"), (True, "error"), (True, "denied"), (False, "ok")]
)
def test_result_status_cannot_claim_unverified_success(success: bool, status: str) -> None:
    with pytest.raises(ValidationError):
        ToolResult.model_validate({"success": success, "status": status, "message": ""})
