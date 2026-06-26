"""LTerm AI agent — powered by the OpenAI Responses API."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Sequence

from openai import OpenAI
from pydantic import BaseModel

from config import OPENAI_API_KEY, OPENAI_MODEL
from src.assistant.prompts import (
    COMMAND_SYSTEM_PROMPT,
    ERROR_SYSTEM_PROMPT,
    SUGGESTIONS_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
)
from src.assistant.tools import TOOL_HANDLERS, HistoryEntry, get_tool_definitions


@dataclass(frozen=True, slots=True)
class AgentEvent:
    kind: str
    content: str
    meta: dict[str, Any] | None = None


class Suggestions(BaseModel):
    items: list[str]


def _collect_function_calls(response: Any) -> list[Any]:
    output = getattr(response, "output", None) or []
    return [item for item in output if getattr(item, "type", None) == "function_call"]


def _response_text(response: Any) -> str:
    output_text = getattr(response, "output_text", None)
    if isinstance(output_text, str) and output_text:
        return output_text

    chunks: list[str] = []
    for item in getattr(response, "output", None) or []:
        if getattr(item, "type", None) != "message":
            continue
        for content in getattr(item, "content", None) or []:
            text = getattr(content, "text", None)
            if isinstance(text, str):
                chunks.append(text)
    return "".join(chunks)


def _run_with_tools(
    prompt: str,
    *,
    instructions: str,
    model: str | None = None,
    context: str | None = None,
    history: Sequence[HistoryEntry] | None = None,
) -> Iterator[AgentEvent]:
    client = _client()
    user_content = prompt if not context else f"{context}\n\n{prompt}"

    tool_handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {}
    for name, handler in TOOL_HANDLERS.items():
        if name == "retrieve_internal_history":
            # Pass history to this specific tool
            tool_handlers[name] = lambda args, h=handler: h(args, history or [])
        else:
            tool_handlers[name] = handler

    response = client.responses.create(
        model=model or OPENAI_MODEL,
        instructions=instructions,
        input=user_content,
        tools=get_tool_definitions(),
    )

    while True:
        function_calls = _collect_function_calls(response)
        if not function_calls:
            answer = _response_text(response)
            if answer:
                yield AgentEvent(kind="text", content=answer)
            return

        tool_outputs: list[dict[str, Any]] = []
        for call in function_calls:
            name = getattr(call, "name", "")
            raw_arguments = getattr(call, "arguments", "") or "{}"
            try:
                arguments = json.loads(raw_arguments)
            except json.JSONDecodeError:
                arguments = {}

            yield AgentEvent(
                kind="tool",
                content=name,
                meta={"arguments": dict(arguments)},
            )

            handler = tool_handlers.get(name)
            if handler is None:
                result: dict[str, Any] = {"error": f"Unknown tool: {name}"}
            else:
                result = handler(arguments)

            tool_outputs.append(
                {
                    "type": "function_call_output",
                    "call_id": getattr(call, "call_id", ""),
                    "output": json.dumps(result),
                }
            )

        response = client.responses.create(
            model=model or OPENAI_MODEL,
            instructions=instructions,
            previous_response_id=getattr(response, "id"),
            input=tool_outputs,
            tools=get_tool_definitions(),
        )


def _client() -> OpenAI:
    api_key = OPENAI_API_KEY.strip()
    if not api_key:
        raise RuntimeError(
            "OpenAI API key is not configured. Set OPENAI_API_KEY in config.py or in the environment."
        )
    return OpenAI(api_key=api_key)


def ask(prompt: str, model: str | None = None, context: str | None = None, history: Sequence[HistoryEntry] | None = None) -> Iterator[AgentEvent]:
    """Stream a response from the OpenAI Responses API."""
    yield from _run_with_tools(
        prompt,
        instructions=SYSTEM_PROMPT,
        model=model,
        context=context,
        history=history,
    )


def ask_command(command: str, model: str | None = None, context: str | None = None, history: Sequence[HistoryEntry] | None = None) -> Iterator[AgentEvent]:
    """Stream an explanation of a shell command (triggered by '??')."""
    yield from _run_with_tools(
        command,
        instructions=COMMAND_SYSTEM_PROMPT,
        model=model,
        context=context,
        history=history,
    )


def ask_error(error_info: str, model: str | None = None) -> Iterator[str]:
    """Stream an explanation and fix for a failed shell command."""
    client = _client()
    stream = client.responses.create(
        model=model or OPENAI_MODEL,
        instructions=ERROR_SYSTEM_PROMPT,
        input=error_info,
        stream=True,
    )
    for event in stream:
        if hasattr(event, "delta") and isinstance(event.delta, str):
            yield event.delta
        elif hasattr(event, "type") and event.type == "response.output_text.delta":
            yield event.delta


def generate_suggestions(answer: str, model: str | None = None) -> list[str]:
    """Return up to 3 follow-up command suggestions based on *answer*."""
    client = _client()
    response = client.responses.parse(
        model=model or OPENAI_MODEL,
        instructions=SUGGESTIONS_SYSTEM_PROMPT,
        input=answer,
        text_format=Suggestions,
    )
    result = response.output_parsed
    if result is None:
        return []
    return result.items
