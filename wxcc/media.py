"""MEDIA: tag extraction and inbound media cache.

Ports the hermes-agent convention: the agent marks deliverable files in its
reply text as ``MEDIA:/abs/path`` (optionally quoted/backticked); the bridge
strips the tags and sends the files natively. Bare absolute paths with a known
extension that exist on disk are delivered too. Paths inside code blocks or
inline code are never touched.
"""

from __future__ import annotations

import os
import re
import uuid
from pathlib import Path
from typing import List, Tuple

from . import store

MEDIA_DELIVERY_EXTS: Tuple[str, ...] = (
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".svg",
    ".mp4", ".mov", ".avi", ".mkv", ".webm",
    ".mp3", ".wav", ".ogg", ".opus", ".m4a", ".flac", ".silk",
    ".pdf", ".docx", ".doc", ".odt", ".rtf", ".txt", ".md", ".epub",
    ".xlsx", ".xls", ".ods", ".csv", ".tsv", ".json", ".xml", ".yaml", ".yml",
    ".pptx", ".ppt", ".odp", ".key",
    ".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar", ".apk", ".ipa",
    ".html", ".htm",
)

_EXT_ALTERNATION = "|".join(ext.lstrip(".") for ext in MEDIA_DELIVERY_EXTS)

# MEDIA: tag whose path ends in a known deliverable extension. Path anchors:
# ~/ (home-relative), / (Unix absolute), X:\ or X:/ (Windows absolute).
MEDIA_TAG_RE = re.compile(
    r'''[`"']?MEDIA:\s*'''
    r'''(?P<path>`[^`\n]+`|"[^"\n]+"|'[^'\n]+'|'''
    r'''(?:~/|/|[A-Za-z]:[/\\])\S+(?:[^\S\n]+\S+)*?\.(?:''' + _EXT_ALTERNATION + r'''))'''
    r'''(?=[\s`"',;:)\]}]|$)[`"']?''',
    re.IGNORECASE,
)

_BARE_PATH_RE = re.compile(
    r'(?<![/:\w.])(?:~/|/|[A-Za-z]:[/\\])(?:[\w.\- ]+[/\\])*[\w.\-]+\.(?:' + _EXT_ALTERNATION + r')\b',
    re.IGNORECASE,
)

_VOICE_DIRECTIVE = "[[audio_as_voice]]"
_DOCUMENT_DIRECTIVE = "[[as_document]]"

_AUDIO_EXTS = {".ogg", ".opus", ".mp3", ".wav", ".m4a", ".flac", ".silk"}
_VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".3gp"}
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}


def _code_spans(content: str) -> List[Tuple[int, int]]:
    spans: List[Tuple[int, int]] = []
    for m in re.finditer(r"```[^\n]*\n.*?```", content, re.DOTALL):
        spans.append((m.start(), m.end()))
    for m in re.finditer(r"`[^`\n]+`", content):
        spans.append((m.start(), m.end()))
    return spans


def _normalize_tag_path(raw: str) -> str:
    path = str(raw or "").strip()
    if len(path) >= 2 and path[0] == path[-1] and path[0] in "`\"'":
        path = path[1:-1].strip()
    return path.lstrip("`\"'").rstrip("`\"',.;:)}]")


def extract_media(content: str) -> Tuple[List[Tuple[str, bool]], str]:
    """Return ([(abs_path, as_voice)], cleaned_text).

    Only files that actually exist are extracted; their tags/paths are removed
    from the delivered text. Non-existent paths stay visible for debugging.
    """
    has_voice = _VOICE_DIRECTIVE in content
    cleaned = content.replace(_VOICE_DIRECTIVE, "").replace(_DOCUMENT_DIRECTIVE, "")

    spans = _code_spans(cleaned)

    def in_code(pos: int) -> bool:
        return any(s <= pos < e for s, e in spans)

    media: List[Tuple[str, bool]] = []
    remove: List[Tuple[int, int]] = []
    seen: set[str] = set()

    for match in MEDIA_TAG_RE.finditer(cleaned):
        if in_code(match.start()):
            continue
        path = os.path.expanduser(_normalize_tag_path(match.group("path")))
        if path and os.path.isfile(path):
            if path not in seen:
                media.append((path, has_voice))
                seen.add(path)
            remove.append(match.span())

    for match in _BARE_PATH_RE.finditer(cleaned):
        if in_code(match.start()):
            continue
        if any(s <= match.start() < e for s, e in remove):
            continue
        path = os.path.expanduser(match.group(0))
        if os.path.isfile(path) and path not in seen:
            media.append((path, has_voice))
            seen.add(path)
            remove.append(match.span())

    if remove:
        chars = list(cleaned)
        for start, end in sorted(remove, reverse=True):
            del chars[start:end]
        cleaned = "".join(chars)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()

    return media, cleaned


def media_kind(path: str) -> str:
    ext = Path(path).suffix.lower()
    if ext in _IMAGE_EXTS:
        return "image"
    if ext in _VIDEO_EXTS:
        return "video"
    if ext in _AUDIO_EXTS:
        return "audio"
    return "document"


def cache_dir() -> Path:
    path = store.get_home() / "cache"
    path.mkdir(parents=True, exist_ok=True)
    return path


def cache_bytes(data: bytes, filename: str) -> str:
    """Persist inbound media bytes and return the absolute path."""
    safe_name = Path(filename).name.replace("\x00", "").strip() or "file.bin"
    if safe_name in {".", ".."}:
        safe_name = "file.bin"
    filepath = cache_dir() / f"{uuid.uuid4().hex[:12]}_{safe_name}"
    if not filepath.resolve().is_relative_to(cache_dir().resolve()):
        raise ValueError(f"path traversal rejected: {filename!r}")
    filepath.write_bytes(data)
    return str(filepath)
