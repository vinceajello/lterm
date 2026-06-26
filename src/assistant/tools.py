"""Tools and handlers for the LTerm AI agent."""

from __future__ import annotations

import os
import re
import subprocess
from typing import Any, Callable, Sequence

_HISTORY_TOOL_NAME = "retrieve_internal_history"
_MANDB_TOOL_NAME = "retrieve_mandb_documentation"
_MAN_OVERSTRIKE_RE = re.compile(r".(?:\x08.)")

HistoryEntry = dict[str, Any]

def get_tool_definitions() -> list[dict[str, Any]]:
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

def _clamp_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(result, maximum))

def retrieve_internal_history(arguments: dict[str, Any], history: Sequence[HistoryEntry]) -> dict[str, Any]:
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

def retrieve_mandb_documentation(arguments: dict[str, Any]) -> dict[str, Any]:
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

TOOL_HANDLERS = {
    _HISTORY_TOOL_NAME: retrieve_internal_history,
    _MANDB_TOOL_NAME: retrieve_mandb_documentation,
}
