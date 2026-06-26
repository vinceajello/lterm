"""LTerm AI agent — powered by the OpenAI Responses API."""

from __future__ import annotations

import os
from typing import Iterator

from openai import OpenAI
from pydantic import BaseModel

_SYSTEM_PROMPT = """\
You are LTerm, an expert terminal assistant embedded inside a TUI terminal emulator.
The user is typing commands in a Linux shell. When they prefix a message with '--'
they are asking you a question or requesting help.

Keep answers concise and practical. Prefer shell commands than code snippets.
If you suggest a command, put it on its own line so it can be copy-pasted easily.
"""

_SUGGESTIONS_SYSTEM_PROMPT = """\
You are LTerm, an expert terminal assistant.
Given the AI answer below, extract up to 3 short follow-up shell commands or actions
that the user might want to run next. Each suggestion must be a single shell command. 
Return only the suggestions list.
Do not suggest destructive commands unless it's absolutely necessary. Always prefer safe suggestions.
If there are no good suggestions, return an empty list.
"""

_ERROR_SYSTEM_PROMPT = """\
You are LTerm, an expert terminal assistant embedded inside a TUI terminal emulator.
The user just ran a shell command that failed (non-zero exit code).
Analyze the error output and provide a concise explanation and fix.
Keep the answer short and textual. Do not use markdown formatting.
"""

_COMMAND_SYSTEM_PROMPT = """\
You are LTerm, an expert terminal assistant embedded inside a TUI terminal emulator.
The user prefixed a shell command or expression with '??' to ask for an explanation.
Explain what the command does, what each flag/argument means, and any important caveats.
Keep the answer concise and practical. Do not use markdown formatting.
"""


class Suggestions(BaseModel):
    items: list[str]


def _client() -> OpenAI:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "OPENAI_API_KEY environment variable is not set."
        )
    return OpenAI(api_key=api_key)


def ask(
    prompt: str,
    *,
    model: str = "gpt-4o-mini",
    context: str | None = None,
) -> Iterator[str]:
    """Stream a response from the OpenAI Responses API.

    Parameters
    ----------
    prompt:
        The user's question / instruction (text after ``--``).
    model:
        OpenAI model name.
    context:
        Optional shell context string (cwd, last command, last output)
        prepended to the user message.

    Yields
    ------
    str
        Incremental text chunks as they arrive from the stream.
    """
    client = _client()

    user_content = prompt
    if context:
        user_content = f"{context}\n\n{prompt}"

    stream = client.responses.create(
        model=model,
        instructions=_SYSTEM_PROMPT,
        input=user_content,
        stream=True,
    )

    for event in stream:
        # The Responses API streams ResponseTextDeltaEvent objects
        if hasattr(event, "delta") and isinstance(event.delta, str):
            yield event.delta
        elif hasattr(event, "type") and event.type == "response.output_text.delta":
            yield event.delta


def ask_command(
    command: str,
    *,
    model: str = "gpt-4o-mini",
    context: str | None = None,
) -> Iterator[str]:
    """Stream an explanation of a shell command (triggered by '??')."""
    client = _client()
    user_content = command
    if context:
        user_content = f"{context}\n\n{command}"
    stream = client.responses.create(
        model=model,
        instructions=_COMMAND_SYSTEM_PROMPT,
        input=user_content,
        stream=True,
    )
    for event in stream:
        if hasattr(event, "delta") and isinstance(event.delta, str):
            yield event.delta
        elif hasattr(event, "type") and event.type == "response.output_text.delta":
            yield event.delta


def ask_error(
    error_info: str,
    *,
    model: str = "gpt-4o-mini",
) -> Iterator[str]:
    """Stream an explanation and fix for a failed shell command.

    Parameters
    ----------
    error_info:
        The formatted error block shown in the panel (command + output).
    model:
        OpenAI model name.

    Yields
    ------
    str
        Incremental text chunks.
    """
    client = _client()
    stream = client.responses.create(
        model=model,
        instructions=_ERROR_SYSTEM_PROMPT,
        input=error_info,
        stream=True,
    )
    for event in stream:
        if hasattr(event, "delta") and isinstance(event.delta, str):
            yield event.delta
        elif hasattr(event, "type") and event.type == "response.output_text.delta":
            yield event.delta


def generate_suggestions(
    answer: str,
    *,
    model: str = "gpt-4o-mini",
) -> list[str]:
    """Return up to 5 follow-up command suggestions based on *answer*.

    Uses OpenAI structured output to guarantee a typed ``Suggestions`` response.

    Parameters
    ----------
    answer:
        The full text of the agent's previous answer.
    model:
        OpenAI model name.

    Returns
    -------
    list[str]
        A list of short suggestion strings (may be empty on error).
    """
    client = _client()
    response = client.responses.parse(
        model=model,
        instructions=_SUGGESTIONS_SYSTEM_PROMPT,
        input=answer,
        text_format=Suggestions,
    )
    result = response.output_parsed
    if result is None:
        return []
    return result.items
