"""Sahte sağlayıcı gerçek orkestrasyonu ağ ve model belirsizliği olmadan sınar."""

from artemis.brain.errors import ProviderError
from artemis.brain.models import Completion, Message
from artemis.tools.base import ToolDefinition


class ScriptedProvider:
    def __init__(self, responses: list[Completion]) -> None:
        self.responses = list(responses)
        self.requests: list[list[Message]] = []

    async def complete(
        self, messages: list[Message], tools: list[ToolDefinition], *, timeout: float
    ) -> Completion:
        self.requests.append([message.model_copy(deep=True) for message in messages])
        if not self.responses:
            raise ProviderError("Sahte sağlayıcının senaryolu yanıtları tükendi")
        return self.responses.pop(0).model_copy(deep=True)
