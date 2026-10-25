"""Sahte sağlayıcı gerçek orkestrasyonu ağ ve model belirsizliği olmadan sınar."""

import re

from artemis.brain.errors import ProviderError
from artemis.brain.models import Completion, Message
from artemis.brain.streaming import TextStream
from artemis.tools.base import ToolDefinition


class ScriptedProvider:
    def __init__(self, responses: list[Completion]) -> None:
        self.responses = list(responses)
        self.requests: list[list[Message]] = []

    async def complete(
        self,
        messages: list[Message],
        tools: list[ToolDefinition],
        *,
        timeout: float,
        stream: TextStream | None = None,
    ) -> Completion:
        self.requests.append([message.model_copy(deep=True) for message in messages])
        if not self.responses:
            raise ProviderError("Sahte sağlayıcının senaryolu yanıtları tükendi")
        completion = self.responses.pop(0).model_copy(deep=True)
        if stream is not None:
            # Gerçek sağlayıcılar gibi: önce metin parçaları, sonra araç çağrısı başlar.
            for piece in re.findall(r"\S+\s*|\s+", completion.content):
                stream.text(piece)
            if completion.tool_calls:
                stream.tool_call()
        return completion
