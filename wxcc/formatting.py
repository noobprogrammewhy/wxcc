"""WeChat text delivery formatting, ported from hermes-agent weixin.py (MIT).

WeChat renders Markdown but chunks at ~2048 chars and can't edit sent
messages, so content is normalized, wrapped for copy-friendliness, and packed
into <=2000-char messages along Markdown block boundaries.
"""

from __future__ import annotations

import re
import textwrap
from typing import List

MAX_MESSAGE_LENGTH = 2000
COPY_LINE_WIDTH = 120

_FENCE_RE = re.compile(r"^```([^\n`]*)\s*$")
_TABLE_RULE_RE = re.compile(r"^\s*\|?(?:\s*:?-{3,}:?\s*\|)+\s*:?-{3,}:?\s*\|?\s*$")


def _normalize_markdown_blocks(content: str) -> str:
    lines = content.splitlines()
    result: List[str] = []
    in_code_block = False
    blank_run = 0

    for raw_line in lines:
        line = raw_line.rstrip()
        if _FENCE_RE.match(line.strip()):
            in_code_block = not in_code_block
            result.append(line)
            blank_run = 0
            continue
        if in_code_block:
            result.append(line)
            continue
        if not line.strip():
            blank_run += 1
            if blank_run <= 1:
                result.append("")
            continue
        blank_run = 0
        result.append(line)

    return "\n".join(result).strip()


def _wrap_copy_friendly_lines(content: str) -> str:
    if not content:
        return content

    wrapped: List[str] = []
    in_code_block = False

    for raw_line in content.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if _FENCE_RE.match(stripped):
            in_code_block = not in_code_block
            wrapped.append(line)
            continue
        if (
            in_code_block
            or len(line) <= COPY_LINE_WIDTH
            or not stripped
            or stripped.startswith("|")
            or _TABLE_RULE_RE.match(stripped)
        ):
            wrapped.append(line)
            continue
        wrapped_lines = textwrap.wrap(
            line,
            width=COPY_LINE_WIDTH,
            break_long_words=False,
            break_on_hyphens=False,
            replace_whitespace=False,
            drop_whitespace=True,
        )
        wrapped.extend(wrapped_lines or [line])

    return "\n".join(wrapped).strip()


def format_message(content: str) -> str:
    if content is None:
        return ""
    return _wrap_copy_friendly_lines(_normalize_markdown_blocks(content))


def _split_markdown_blocks(content: str) -> List[str]:
    if not content:
        return []

    blocks: List[str] = []
    current: List[str] = []
    in_code_block = False

    for raw_line in content.splitlines():
        line = raw_line.rstrip()
        if _FENCE_RE.match(line.strip()):
            if not in_code_block and current:
                blocks.append("\n".join(current).strip())
                current = []
            current.append(line)
            in_code_block = not in_code_block
            if not in_code_block:
                blocks.append("\n".join(current).strip())
                current = []
            continue
        if in_code_block:
            current.append(line)
            continue
        if not line.strip():
            if current:
                blocks.append("\n".join(current).strip())
                current = []
            continue
        current.append(line)

    if current:
        blocks.append("\n".join(current).strip())
    return [block for block in blocks if block]


def _hard_split(content: str, max_length: int) -> List[str]:
    """Last-resort splitter for a single oversized block: prefer line breaks,
    fall back to hard cuts."""
    chunks: List[str] = []
    current = ""
    for line in content.splitlines():
        while len(line) > max_length:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:max_length])
            line = line[max_length:]
        candidate = line if not current else f"{current}\n{line}"
        if len(candidate) <= max_length:
            current = candidate
        else:
            chunks.append(current)
            current = line
    if current:
        chunks.append(current)
    return [c for c in chunks if c.strip()]


def split_for_delivery(content: str, max_length: int = MAX_MESSAGE_LENGTH) -> List[str]:
    """Pack content into sequential WeChat messages along block boundaries."""
    if not content:
        return []
    if len(content) <= max_length:
        return [content]

    packed: List[str] = []
    current = ""
    for block in _split_markdown_blocks(content):
        candidate = block if not current else f"{current}\n\n{block}"
        if len(candidate) <= max_length:
            current = candidate
            continue
        if current:
            packed.append(current)
            current = ""
        if len(block) <= max_length:
            current = block
            continue
        packed.extend(_hard_split(block, max_length))
    if current:
        packed.append(current)
    return packed or [content]
