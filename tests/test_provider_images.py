"""Sağlayıcı mesajlarındaki tek PNG (ADR 0039): yük biçimi, gizlilik ve kalıcılık dışı bırakma."""
# ruff: noqa: F811

import base64
from pathlib import Path

import pytest

from artemis.brain.conversation import TurnRecord
from artemis.brain.errors import ProviderError
from artemis.brain.models import Message
from artemis.brain.providers import ClaudeCliProvider, _chat_messages, _cloud_messages
from tests.test_claude_cli import provider as cli_provider
from tests.test_providers import (  # noqa: F401
    HTTPState,
    ProviderName,
    ephemeral_keys,
    http_server,
    provider_for,
    response_for,
)

PNG = b"\x89PNG\r\n\x1a\n-sahte-goruntu"
B64 = base64.b64encode(PNG).decode("ascii")


def _image(content: str = "Ekran bu.", **kw: bool) -> Message:
    return Message(role="user", content=content, image_png=PNG, **kw)


async def _sent(name: ProviderName, http_server: HTTPState, tmp_path: Path) -> dict[str, object]:
    http_server.body = response_for(name)
    await provider_for(name, http_server, tmp_path).complete([_image()], [], timeout=5)
    return http_server.requests[0][1]


async def test_openai_style_image_parts(http_server: HTTPState, tmp_path: Path) -> None:
    body = await _sent("openrouter", http_server, tmp_path)
    assert body["messages"] == [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Ekran bu."},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{B64}"}},
            ],
        }
    ]


def test_openai_style_empty_text_omitted() -> None:
    [item] = _chat_messages([_image("")])
    assert [part["type"] for part in item["content"]] == ["image_url"]


async def test_ollama_images_field(http_server: HTTPState, tmp_path: Path) -> None:
    body = await _sent("ollama", http_server, tmp_path)
    assert body["messages"] == [{"role": "user", "content": "Ekran bu.", "images": [B64]}]


async def test_anthropic_image_block_before_text(http_server: HTTPState, tmp_path: Path) -> None:
    body = await _sent("anthropic", http_server, tmp_path)
    assert body["messages"] == [
        {
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/png", "data": B64},
                },
                {"type": "text", "text": "Ekran bu."},
            ],
        }
    ]


def test_plain_message_unchanged() -> None:
    assert _chat_messages([Message(role="user", content="x")]) == [
        {"role": "user", "content": "x"}
    ]


def test_private_message_drops_image() -> None:
    [masked] = _cloud_messages([_image(private=True)])
    assert masked.image_png is None
    assert "images" not in _chat_messages([masked], ollama=True)[0]


async def test_claude_cli_rejects_image(tmp_path: Path) -> None:
    with pytest.raises(ProviderError, match="ekran görüntüsü"):
        await cli_provider(tmp_path).complete([_image()], [], timeout=5)
    assert isinstance(cli_provider(tmp_path), ClaudeCliProvider)


def test_repr_and_dump_hide_image() -> None:
    message = _image()
    assert "PNG" not in repr(message) and B64 not in repr(message)
    assert "image_png" not in message.model_dump()
    assert B64 not in message.model_dump_json()


def test_turn_record_persistence_excludes_image() -> None:
    record = TurnRecord(
        turn_id="t", user_text="u", steps=(_image(),), answer="a", answer_private=False,
        tainted=False,
    )  # fmt: skip
    assert B64 not in record.model_dump_json()
    assert TurnRecord.model_validate_json(record.model_dump_json()).steps[0].image_png is None  # type: ignore[union-attr]
