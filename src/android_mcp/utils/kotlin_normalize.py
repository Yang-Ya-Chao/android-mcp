"""Conservative source normalization used by ``android_file`` old_content checks."""

from __future__ import annotations

import re


_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\d+(?:\.\d+)?")


def normalize_code(value: str) -> str:
    """Remove comments and insignificant whitespace while keeping token boundaries.

    This is deliberately a small lexer, not a Kotlin parser.  It handles line/block
    comments, quoted strings and triple-quoted strings so an old-content check does
    not fail merely because an editor reformatted a surrounding block.
    """

    output: list[str] = []
    index = 0
    pending_space = False
    length = len(value)
    while index < length:
        char = value[index]
        next_char = value[index + 1] if index + 1 < length else ""
        if char == "/" and next_char == "/":
            index += 2
            while index < length and value[index] not in "\r\n":
                index += 1
            continue
        if char == "/" and next_char == "*":
            index += 2
            while index + 1 < length and value[index : index + 2] != "*/":
                index += 1
            index = min(length, index + 2)
            continue
        if value.startswith('"""', index):
            output.append("<string>")
            index += 3
            end = value.find('"""', index)
            index = length if end < 0 else end + 3
            pending_space = False
            continue
        if char == '"':
            output.append("<string>")
            index += 1
            escaped = False
            while index < length:
                current = value[index]
                index += 1
                if escaped:
                    escaped = False
                elif current == "\\":
                    escaped = True
                elif current == '"':
                    break
            pending_space = False
            continue
        if char.isspace():
            pending_space = True
            index += 1
            continue
        token_match = _IDENTIFIER.match(value, index)
        if token_match:
            token = token_match.group(0)
            if pending_space and output and _needs_separator(output[-1], token):
                output.append(" ")
            output.append(token)
            pending_space = False
            index = token_match.end()
            continue
        if pending_space and output and _needs_separator(output[-1], char):
            output.append(" ")
        output.append(char)
        pending_space = False
        index += 1
    return "".join(output).strip()


def normalize_text(value: str) -> str:
    """Whitespace-normalized comparison for XML/Gradle and other text files."""

    return " ".join(value.replace("\r\n", "\n").split())


def _needs_separator(previous: str, current: str) -> bool:
    return (previous[-1:].isalnum() or previous[-1:] == "_") and (current[:1].isalnum() or current[:1] == "_")
