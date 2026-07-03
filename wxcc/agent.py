"""Claude Code agent layer, via the Claude Agent SDK.

Each WeChat chat maps to a persistent Claude Code session (resumed by id) so
context survives across messages and process restarts. An in-process MCP tool
(`wechat__send_message`) is exposed so Claude can proactively push WeChat
messages/files to any chat — the analog of hermes-agent's send_message tool.
Outbound files to the *current* chat also work via the MEDIA:/path convention
handled in the bridge.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
    create_sdk_mcp_server,
    tool,
)

from .ilink import IlinkClient
from . import media as media_mod

logger = logging.getLogger(__name__)

DEFAULT_SYSTEM_PROMPT = """\
You are Claude Code, reachable by the user over WeChat. You are running on the \
user's own machine with full tool access (shell, files, editing).

Communication rules for the WeChat channel:
- Your final reply text is delivered to the user as WeChat messages. Keep it \
readable on a phone: short paragraphs, minimal heavy formatting.
- To send a file (image, document, audio, video) to the user you are talking \
to, put `MEDIA:/absolute/path` on its own line in your reply. The file is sent \
natively and the tag is stripped from the visible text.
- To message a *different* WeChat chat, or to push a file to someone, use the \
`send_message` tool.
- You are operating autonomously; the user is not watching a terminal. Do the \
work rather than asking whether to proceed, unless an action is destructive.
"""


def build_wechat_mcp_server(ilink: IlinkClient):
    """In-process MCP server exposing WeChat send capability to Claude."""

    @tool(
        "send_message",
        "Send a WeChat message and/or a file to a specific chat. `to` is a "
        "WeChat chat id (a wxid, an @chatroom id, or 'filehelper'). Provide "
        "`text`, `media_path` (absolute path to a local file), or both.",
        {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "Target WeChat chat id"},
                "text": {"type": "string", "description": "Message text to send"},
                "media_path": {
                    "type": "string",
                    "description": "Absolute path to a local file to send",
                },
            },
            "required": ["to"],
        },
    )
    async def send_message(args: Dict[str, Any]) -> Dict[str, Any]:
        to = str(args.get("to") or "").strip()
        text = str(args.get("text") or "").strip()
        media_path = str(args.get("media_path") or "").strip()
        if not to:
            return {"content": [{"type": "text", "text": "error: 'to' is required"}], "is_error": True}
        if not text and not media_path:
            return {
                "content": [{"type": "text", "text": "error: provide text or media_path"}],
                "is_error": True,
            }
        try:
            from . import formatting

            sent = []
            if text:
                for chunk in formatting.split_for_delivery(formatting.format_message(text)):
                    await ilink.send_text(to, chunk)
                sent.append("text")
            if media_path:
                if not Path(media_path).is_file():
                    return {
                        "content": [{"type": "text", "text": f"error: file not found: {media_path}"}],
                        "is_error": True,
                    }
                await ilink.send_file(to, media_path)
                sent.append(f"file {Path(media_path).name}")
            return {"content": [{"type": "text", "text": f"sent to {to}: {', '.join(sent)}"}]}
        except Exception as exc:  # noqa: BLE001
            logger.warning("send_message tool failed: %s", exc)
            return {"content": [{"type": "text", "text": f"send failed: {exc}"}], "is_error": True}

    return create_sdk_mcp_server(name="wechat", version="0.1.0", tools=[send_message])


class ChatSession:
    """A warm, persistent Claude Code client for one WeChat chat.

    The underlying `claude` subprocess is spawned once (on first message) and
    reused for every subsequent message, so per-message cold-start latency is
    paid only once. In-process context carries across turns; the session id is
    still captured and persisted so a process restart can `resume` where it left
    off. On a subprocess failure the client transparently reconnects once,
    resuming from the last known session id.
    """

    def __init__(
        self,
        *,
        chat_id: str,
        ilink: IlinkClient,
        config: Dict[str, Any],
        mcp_server: Any,
        resume_session_id: Optional[str] = None,
    ):
        self.chat_id = chat_id
        self._ilink = ilink
        self._config = config
        self._mcp_server = mcp_server
        self.session_id: Optional[str] = resume_session_id
        self.model_override: Optional[str] = None
        self.cwd_override: Optional[str] = None
        self.permission_override: Optional[str] = None
        self._client: Optional[ClaudeSDKClient] = None
        # In-process usage/activity stats accumulated over this chat's lifetime
        # (reset when the conversation is reset, since that makes a new session).
        self.stats: Dict[str, float] = {
            "turns": 0,
            "cost_usd": 0.0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read": 0,
            "cache_creation": 0,
            "duration_ms": 0,
            # Snapshot of the most recent turn (overwritten each turn) — used to
            # report current context-window occupancy.
            "last_input": 0,
            "last_cache_read": 0,
            "last_cache_creation": 0,
            "last_output": 0,
        }

    @property
    def current_model(self) -> Optional[str]:
        return self.model_override or self._config.get("model") or None

    @property
    def current_cwd(self) -> str:
        return self.cwd_override or self._config.get("cwd") or str(Path.home() / "wxcc-workspace")

    @property
    def current_permission(self) -> str:
        return self.permission_override or self._config.get("permission_mode") or "bypassPermissions"

    def _build_options(self) -> ClaudeAgentOptions:
        cwd = self.current_cwd
        Path(cwd).mkdir(parents=True, exist_ok=True)
        system_prompt = DEFAULT_SYSTEM_PROMPT
        extra = str(self._config.get("system_prompt_extra") or "").strip()
        if extra:
            system_prompt = f"{system_prompt}\n\n{extra}"
        return ClaudeAgentOptions(
            cwd=cwd,
            model=self.current_model,
            permission_mode=self.current_permission,
            max_turns=self._config.get("max_turns") or None,
            system_prompt=system_prompt,
            resume=self.session_id or None,  # only applied at connect
            mcp_servers={"wechat": self._mcp_server},
            allowed_tools=["mcp__wechat__send_message"],
        )

    async def _ensure_connected(self) -> None:
        if self._client is not None:
            return
        client = ClaudeSDKClient(options=self._build_options())
        await client.connect()
        self._client = client
        logger.info("chat=%s Claude client connected (resume=%s)", self.chat_id[:10], self.session_id or "(new)")

    async def _drain(self) -> Tuple[str, bool, bool]:
        assert self._client is not None
        reply_parts: List[str] = []
        is_error = False
        saw_result = False
        async for message in self._client.receive_response():
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock) and block.text:
                        reply_parts.append(block.text)
            elif isinstance(message, ResultMessage):
                saw_result = True
                self.session_id = message.session_id or self.session_id
                is_error = bool(message.is_error)
                self._accumulate_stats(message)
                if not reply_parts and getattr(message, "result", None):
                    reply_parts.append(str(message.result))
                logger.info(
                    "chat=%s ResultMessage subtype=%s is_error=%s session=%s",
                    self.chat_id[:10], getattr(message, "subtype", "?"), is_error, self.session_id,
                )
        return "\n".join(reply_parts).strip(), is_error, saw_result

    def _accumulate_stats(self, message: "ResultMessage") -> None:
        self.stats["turns"] += 1
        cost = getattr(message, "total_cost_usd", None)
        if cost:
            self.stats["cost_usd"] += float(cost)
        dur = getattr(message, "duration_ms", None)
        if dur:
            self.stats["duration_ms"] += int(dur)
        usage = getattr(message, "usage", None) or {}
        if isinstance(usage, dict):
            inp = int(usage.get("input_tokens", 0) or 0)
            out = int(usage.get("output_tokens", 0) or 0)
            cread = int(usage.get("cache_read_input_tokens", 0) or 0)
            ccreate = int(usage.get("cache_creation_input_tokens", 0) or 0)
            self.stats["input_tokens"] += inp
            self.stats["output_tokens"] += out
            self.stats["cache_read"] += cread
            self.stats["cache_creation"] += ccreate
            self.stats["last_input"] = inp
            self.stats["last_cache_read"] = cread
            self.stats["last_cache_creation"] = ccreate
            self.stats["last_output"] = out

    async def ask(self, prompt: str) -> Tuple[str, Optional[str], bool]:
        """Send one prompt and return (reply_text, session_id, is_error).

        Reconnects once (resuming the last session) if the warm subprocess has
        died since the previous turn.
        """
        for attempt in range(2):
            try:
                await self._ensure_connected()
                await self._client.query(prompt)
                reply, is_error, saw_result = await self._drain()
                if not saw_result:
                    logger.warning("chat=%s response ended without ResultMessage", self.chat_id[:10])
                return reply, self.session_id, is_error
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "chat=%s turn failed (attempt %d): %s", self.chat_id[:10], attempt + 1, exc
                )
                await self._reset()
                if attempt >= 1:
                    raise
        return "", self.session_id, True

    async def set_model(self, model: Optional[str]) -> None:
        """Change the model for this chat. Drops the warm client so the next
        turn reconnects with the new model, resuming the current session id so
        context is preserved."""
        self.model_override = model or None
        await self._reset()

    async def set_cwd(self, cwd: Optional[str]) -> None:
        """Change the working directory for this chat. Drops the warm client so
        the next turn reconnects in the new directory, resuming the session so
        context is preserved."""
        self.cwd_override = cwd or None
        await self._reset()

    async def set_permission(self, mode: Optional[str]) -> None:
        """Change the permission mode for this chat (reconnects on next turn)."""
        self.permission_override = mode or None
        await self._reset()

    async def resume_session(self, session_id: str) -> None:
        """Switch this chat to a previous Claude session, dropping the warm
        client so the next turn reconnects and resumes that session id."""
        self.session_id = session_id or None
        await self._reset()

    async def context_usage(self) -> Optional[Dict[str, Any]]:
        """Live context-window usage from the CLI, or None if not connected."""
        if self._client is None:
            return None
        try:
            return await self._client.get_context_usage()
        except Exception as exc:  # noqa: BLE001
            logger.warning("chat=%s get_context_usage failed: %s", self.chat_id[:10], exc)
            return None

    async def compact(self, instructions: str = "") -> Tuple[str, bool]:
        """Compact the conversation via the CLI's `/compact` command. Optional
        `instructions` focus what the summary should preserve. Returns
        (summary_text, ok). Reconnects once on a dead subprocess."""
        prompt = "/compact" + (f" {instructions.strip()}" if instructions.strip() else "")
        for attempt in range(2):
            try:
                await self._ensure_connected()
                await self._client.query(prompt)
                reply, is_error, saw_result = await self._drain()
                return reply, (saw_result and not is_error)
            except Exception as exc:  # noqa: BLE001
                logger.warning("chat=%s compact failed (attempt %d): %s", self.chat_id[:10], attempt + 1, exc)
                await self._reset()
                if attempt >= 1:
                    raise
        return "", False

    async def interrupt(self) -> bool:
        """Interrupt an in-flight turn. Returns True if a client was live."""
        if self._client is not None:
            try:
                await self._client.interrupt()
                return True
            except Exception as exc:  # noqa: BLE001
                logger.warning("chat=%s interrupt failed: %s", self.chat_id[:10], exc)
        return False

    async def _reset(self) -> None:
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:  # noqa: BLE001
                pass
            self._client = None

    async def close(self) -> None:
        await self._reset()
