"""Append model responses before history trimming; never expose logs as model context."""

import json
from pathlib import Path
from typing import override

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import ModelMessagesTypeAdapter, ModelResponse
from pydantic_ai.models import ModelRequestContext


class ResponseRecorder[DepsT](AbstractCapability[DepsT]):
    """Save endpoint-returned reasoning and text, including intermediate/retry responses."""

    def __init__(
        self,
        path: Path,
        *,
        role: str,
        call_id: str | None = None,
        buffer: list[str] | None = None,
    ) -> None:
        """Bind the sink and role; call_id associates judge retries with a proposal."""
        self.buffer: list[str] | None = buffer
        self.path: Path = path
        self.role: str = role
        self.call_id: str | None = call_id

    @override
    async def after_model_request(
        self,
        ctx: RunContext[DepsT],
        *,
        request_context: ModelRequestContext,
        response: ModelResponse,
    ) -> ModelResponse:
        del ctx, request_context
        event = {
            "schema_version": 1,
            "role": self.role,
            "call_id": self.call_id,
            "response": json.loads(ModelMessagesTypeAdapter.dump_json([response]))[0],
        }
        line = json.dumps(event) + "\n"
        if self.buffer is not None:
            self.buffer.append(line)
        else:
            with self.path.open("a", encoding="utf-8") as stream:
                _ = stream.write(line)
        return response
