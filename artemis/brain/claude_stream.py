"""Claude Code CLI ``stream-json`` çıktısından yalnız yanıt metnini çıkarır (ADR 0043).

``--json-schema`` ile yanıt, CLI'nin eklediği ``StructuredOutput`` aracının girdisidir ve
``input_json_delta`` parçalarıyla gelir (Agent SDK belgesi, "Recognize the StructuredOutput
tool call"). Bu modül o JSON'un yalnız **üst düzey** ``content`` dizgesini akıtır; üst
düzey ``tool_calls`` dizisinde öğe başladığında "araç başladı" der. İç içe nesnelerdeki
``content`` (ör. bir aracın argümanı) asla akmaz. Tam yanıt yine son ``result`` satırının
``structured_output`` alanından, bugünkü ayrıştırıcıyla doğrulanır.
"""

from __future__ import annotations

import json
from typing import Any

from artemis.brain.streaming import TextStream

_STRUCTURED_TOOL = "StructuredOutput"


class TopLevelContentScanner:
    """Artımlı JSON tarayıcı: derinlik ve dizge durumunu izler, ``content`` değerini çözer."""

    def __init__(self, stream: TextStream) -> None:
        self.stream = stream
        self.depth = 0
        self.in_string = False
        self.escape = ""  # yarım kaçış dizisi (\\ dahil)
        self.string_is_key = False
        self.capturing = False
        self.expect_key = False
        self.last_key = ""
        self.key_chars: list[str] = []
        self.tool_array = False
        self.tool_signalled = False

    def feed(self, text: str) -> None:
        for char in text:
            if self.in_string:
                self._string_char(char)
            else:
                self._structural(char)

    def _structural(self, char: str) -> None:
        if char.isspace():
            return
        if self.tool_array and self.depth == 2 and char != "]" and not self.tool_signalled:
            # Üst düzey tool_calls dizisine ilk öğe yazılıyor: bu adım araçlı.
            self.tool_signalled = True
            self.stream.tool_call()
        if char == '"':
            self.in_string = True
            self.string_is_key = self.depth == 1 and self.expect_key
            self.capturing = (
                self.depth == 1 and not self.string_is_key and self.last_key == "content"
            )
            self.key_chars = []
        elif char in "{[":
            if char == "[" and self.depth == 1 and self.last_key == "tool_calls":
                self.tool_array = True
            self.depth += 1
            self.expect_key = char == "{" and self.depth == 1
        elif char in "}]":
            self.depth -= 1
            if self.depth == 1:
                self.tool_array = False
        elif char == ",":
            self.expect_key = self.depth == 1
        elif char == ":":
            self.expect_key = False

    def _string_char(self, char: str) -> None:
        if self.escape:
            self.escape += char
            decoded = self._decode_escape()
            if decoded is not None:
                self.escape = ""
                self._append(decoded)
            return
        if char == "\\":
            self.escape = char
            return
        if char == '"':
            self.in_string = False
            if self.string_is_key:
                self.last_key = "".join(self.key_chars)
            self.capturing = False
            return
        self._append(char)

    def _decode_escape(self) -> str | None:
        """Tamamlanan kaçışı çözer; geçersiz kaçışta akışı keser (tam yanıt yine doğrulanır)."""
        sequence = self.escape
        try:
            if sequence[1] != "u":
                return str(json.loads(f'"{sequence}"')) if len(sequence) == 2 else None
            if len(sequence) < 6:
                return None
            # Vekil çift: düşük yarım gelmeden çözülemez.
            if 0xD800 <= int(sequence[2:6], 16) <= 0xDBFF and len(sequence) < 12:
                return None
            return str(json.loads(f'"{sequence}"'))
        except ValueError:
            self.capturing = False
            return ""

    def _append(self, text: str) -> None:
        if self.string_is_key:
            self.key_chars.append(text)
        elif self.capturing:
            self.stream.text(text)


class ClaudeStreamReader:
    """``stream-json`` satırlarını işler; son ``result`` nesnesini saklar."""

    def __init__(self, stream: TextStream) -> None:
        self.stream = stream
        self.result: dict[str, Any] | None = None
        self._scanner: TopLevelContentScanner | None = None
        self._index: int | None = None
        self._attempts = 0

    def line(self, raw: str) -> None:
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return  # stdout kirliliği; tam yanıt yine result satırından doğrulanır
        if not isinstance(data, dict):
            return
        if data.get("type") == "result":
            self.result = data
            return
        if data.get("type") != "stream_event":
            return
        event = data.get("event")
        if not isinstance(event, dict):
            return
        kind = event.get("type")
        if kind == "content_block_start":
            block = event.get("content_block")
            if isinstance(block, dict) and block.get("type") == "tool_use" \
                    and block.get("name") == _STRUCTURED_TOOL:
                self._attempts += 1
                if self._attempts == 1:
                    self._scanner = TopLevelContentScanner(self.stream)
                    self._index = event.get("index")
                else:
                    # Şema yeniden denemesi/yedek model geri alması: ikinci yanıt
                    # akıtılmaz; doğrulanmış son yanıt tur sonunda söylenir.
                    self._scanner = None
                    self.stream.tool_call()
        elif kind == "content_block_delta" and self._scanner is not None:
            delta = event.get("delta")
            if event.get("index") == self._index and isinstance(delta, dict) \
                    and delta.get("type") == "input_json_delta":
                partial = delta.get("partial_json")
                if isinstance(partial, str):
                    self._scanner.feed(partial)
