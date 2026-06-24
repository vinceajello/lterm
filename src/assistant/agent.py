"""LTerm AI agent — powered by the OpenAI Responses API."""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Sequence

from openai import OpenAI
from pydantic import BaseModel

from config import OPENAI_API_KEY, OPENAI_MODEL

_SYSTEM_PROMPT = """\
You are LTerm, an expert terminal assistant embedded inside a TUI terminal emulator.
The user is typing commands in a Linux shell. When they prefix a message with '--'
they are asking you a question or requesting help.

Keep answers concise and practical. Prefer shell commands than code snippets.
If you suggest a command, put it on its own line so it can be copy-pasted easily.
If the user asks about previous commands, recent attempts, past errors, or session intent,
use the available history tool instead of guessing.
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
Use the history tool when the explanation depends on what happened earlier in this terminal session.
Use the mandb documentation tool when you need authoritative manual-page details for a command.
"""

_HISTORY_TOOL_NAME = "retrieve_internal_history"
_MANDB_TOOL_NAME = "retrieve_mandb_documentation"
_MAN_OVERSTRIKE_RE = re.compile(r".(?:\x08.)")


@dataclass(frozen=True, slots=True)
class AgentEvent:
    kind: str
    content: str
    meta: dict[str, Any] | None = None


HistoryEntry = dict[str, Any]


def _tool_definitions() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "name": _HISTORY_TOOL_NAME,
            "description": (
                "Retrieve in-memory LTerm session history, including recent shell commands, "
                "agent questions, cwd, exit codes, and short output excerpts."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 50,
                        "description": "Maximum number of matching history entries to return.",
                    },
                    "query": {
                        "type": "string",
                        "description": "Optional case-insensitive text filter for command, cwd, or output.",
                    },
                    "include_agent_queries": {
                        "type": "boolean",
                        "description": "Whether entries created by -- and ?? queries should be included.",
                    },
                    "only_failures": {
                        "type": "boolean",
                        "description": "Whether to return only failed shell commands.",
                    },
                },
                "required": [],
                "additionalProperties": False,
            },
        },
        {
            "type": "function",
            "name": _MANDB_TOOL_NAME,
            "description": (
                "Retrieve manual-page content from the local man/mandb database for a shell command. "
                "Use this for authoritative command syntax, flags, and caveats."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The command name to look up, such as ls, grep, or systemctl.",
                    },
                    "section": {
                        "type": "string",
                        "description": "Optional manual section, such as 1, 5, or 8.",
                    },
                    "max_chars": {
                        "type": "integer",
                        "minimum": 200,
                        "maximum": 6000,
                        "description": "Maximum number of characters of cleaned manual text to return.",
                    },
                },
                "required": ["command"],
                "additionalProperties": False,
            },
        }
    ]


class Suggestions(BaseModel):
    items: list[str]


def _clamp_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(result, maximum))


def _retrieve_internal_history(arguments: dict[str, Any], history: Sequence[HistoryEntry]) -> dict[str, Any]:
    limit = _clamp_int(arguments.get("limit"), default=8, minimum=1, maximum=50)
    include_agent_queries = bool(arguments.get("include_agent_queries", True))
    only_failures = bool(arguments.get("only_failures", False))
    query = str(arguments.get("query") or "").strip().lower()

    items: list[dict[str, Any]] = []
    for entry in history:
        if not include_agent_queries and entry.get("intercepted"):
            continue
        if only_failures and entry.get("intercepted"):
            continue
        exit_code = entry.get("exit_code")
        if only_failures and (exit_code is None or int(exit_code) == 0):
            continue

        haystack = "\n".join(
            [
                str(entry.get("command") or ""),
                str(entry.get("cwd") or ""),
                str(entry.get("output") or ""),
                str(entry.get("kind") or ""),
            ]
        ).lower()
        if query and query not in haystack:
            continue

        items.append(
            {
                "kind": entry.get("kind", "shell_command"),
                "command": entry.get("command", ""),
                "cwd": entry.get("cwd", ""),
                "user": entry.get("user", ""),
                "exit_code": exit_code,
                "intercepted": bool(entry.get("intercepted", False)),
                "output_excerpt": str(entry.get("output") or "")[:400],
            }
        )

    matches = items[-limit:]
    return {
        "total_matches": len(items),
        "returned": len(matches),
        "items": matches,
    }


def _clean_man_text(text: str) -> str:
    text = _MAN_OVERSTRIKE_RE.sub(lambda match: match.group(0)[-1], text)
    text = text.replace("\r", "")
    lines = [line.rstrip() for line in text.splitlines()]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


def _retrieve_mandb_documentation(arguments: dict[str, Any]) -> dict[str, Any]:
    command = str(arguments.get("command") or "").strip()
    if not command:
        return {"error": "Missing required argument: command"}

    section = str(arguments.get("section") or "").strip()
    max_chars = _clamp_int(arguments.get("max_chars"), default=2400, minimum=200, maximum=6000)
    env = {
        **os.environ,
        "MANPAGER": "cat",
        "PAGER": "cat",
        "MANWIDTH": "80",
    }

    cmd = ["man"]
    if section:
        cmd.append(section)
    cmd.append(command)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=env,
            timeout=6,
            check=False,
        )
    except FileNotFoundError:
        return {"error": "The 'man' command is not available on this system."}
    except subprocess.TimeoutExpired:
        return {"error": f"Timed out while retrieving manual page for '{command}'."}

    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        message = stderr or f"No manual entry found for '{command}'."
        return {
            "command": command,
            "section": section or None,
            "error": message,
        }

    cleaned = _clean_man_text(result.stdout)
    excerpt = cleaned[:max_chars]
    if len(cleaned) > max_chars:
        excerpt = excerpt.rstrip() + "\n..."

    return {
        "command": command,
        "section": section or None,
        "truncated": len(cleaned) > len(excerpt),
        "content": excerpt,
    }


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

    tool_handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
        _HISTORY_TOOL_NAME: lambda arguments: _retrieve_internal_history(arguments, history or []),
        _MANDB_TOOL_NAME: _retrieve_mandb_documentation,
    }

    response = client.responses.create(
        model=model or OPENAI_MODEL,
        instructions=instructions,
        input=user_content,
        tools=_tool_definitions(),
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
            tools=_tool_definitions(),
        )


def _client() -> OpenAI:
    api_key = OPENAI_API_KEY.strip()
    if not api_key:
        raise RuntimeError(
            "OpenAI API key is not configured. Set OPENAI_API_KEY in config.py or in the environment."
        )
    return OpenAI(api_key=api_key)


def ask(prompt: str, model: str | None = None, context: str | None = None, history: Sequence[HistoryEntry] | None = None) -> Iterator[AgentEvent]:
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
    yield from _run_with_tools(
        prompt,
        instructions=_SYSTEM_PROMPT,
        model=model,
        context=context,
        history=history,
    )


def ask_command(command: str, model: str | None = None, context: str | None = None, history: Sequence[HistoryEntry] | None = None) -> Iterator[AgentEvent]:
    """Stream an explanation of a shell command (triggered by '??')."""
    yield from _run_with_tools(
        command,
        instructions=_COMMAND_SYSTEM_PROMPT,
        model=model,
        context=context,
        history=history,
    )


def ask_error(error_info: str, model: str | None = None) -> Iterator[str]:
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
        model=model or OPENAI_MODEL,
        instructions=_ERROR_SYSTEM_PROMPT,
        input=error_info,
        stream=True,
    )
    for event in stream:
        if hasattr(event, "delta") and isinstance(event.delta, str):
            yield event.delta
        elif hasattr(event, "type") and event.type == "response.output_text.delta":
            yield event.delta


def generate_suggestions(answer: str, model: str | None = None) -> list[str]:
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
        model=model or OPENAI_MODEL,
        instructions=_SUGGESTIONS_SYSTEM_PROMPT,
        input=answer,
        text_format=Suggestions,
    )
    result = response.output_parsed
    if result is None:
        return []
    return result.items
