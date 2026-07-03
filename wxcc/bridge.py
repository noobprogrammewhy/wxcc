"""The bridge: iLink long-poll inbound -> Claude Code -> iLink outbound.

Access control (dm_policy):
    first     — the first DM sender becomes the persisted owner; others ignored
    allowlist — only wxids in config allow_from
    open      — everyone (full Claude Code access; dangerous)

Per-chat serialization: one Claude turn per chat at a time (messages that
arrive mid-turn are folded into the next turn), so a chat can't spawn parallel
agent runs against the same session.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from . import agent as agent_mod
from . import formatting, media, store
from .ilink import IlinkClient, extract_text

logger = logging.getLogger(__name__)

# Short aliases accepted by the in-chat `!model` command.
MODEL_ALIASES = {
    "opus": "claude-opus-4-8",
    "sonnet": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5",
    "fable": "claude-fable-5",
}

PERMISSION_MODES = {"default", "acceptEdits", "plan", "dontAsk", "bypassPermissions"}

# Advertised context window per model, for the !context occupancy estimate.
CONTEXT_WINDOWS = {
    "claude-opus-4-8": 1_000_000,
    "claude-opus-4-7": 1_000_000,
    "claude-opus-4-6": 1_000_000,
    "claude-sonnet-4-6": 1_000_000,
    "claude-haiku-4-5": 200_000,
    "claude-fable-5": 1_000_000,
}
DEFAULT_CONTEXT_WINDOW = 200_000


def _context_window(model: str | None) -> int:
    if not model:
        return DEFAULT_CONTEXT_WINDOW
    return CONTEXT_WINDOWS.get(model, DEFAULT_CONTEXT_WINDOW)


def _bar(pct: float, width: int = 10) -> str:
    filled = max(0, min(width, int(pct / 100 * width)))
    return "█" * filled + "░" * (width - filled)


def _format_context(usage: Dict[str, Any]) -> str:
    """Render the CLI's live context-usage response for WeChat."""
    total = int(usage.get("totalTokens", 0) or 0)
    mx = int(usage.get("maxTokens", 0) or 0)
    pct = float(usage.get("percentage", 0) or 0)
    lines = [
        "上下文窗口占用：",
        f"{_bar(pct)} {pct:.1f}%",
        f"已用: {total:,} / {mx:,} tokens",
    ]
    # Show the largest few categories so it's clear what fills the window.
    cats = [c for c in (usage.get("categories") or []) if int(c.get("tokens", 0) or 0) > 0]
    cats.sort(key=lambda c: int(c.get("tokens", 0) or 0), reverse=True)
    for c in cats[:5]:
        lines.append(f"  · {c.get('name', '?')}: {int(c.get('tokens', 0)):,}")
    if usage.get("isAutoCompactEnabled"):
        thr = usage.get("autoCompactThreshold")
        lines.append(f"自动压缩: 开{'（阈值 ' + format(int(thr), ',') + '）' if thr else ''}")
    lines.append("手动压缩发 !compact，清空发 !reset。")
    return "\n".join(lines)

HELP_TEXT = (
    "可用命令：\n"
    "!help — 显示本帮助\n"
    "!status — 当前模型 / 会话 / 目录 / 权限\n"
    "!model [名字] — 查看或切换模型（opus/sonnet/haiku/fable 或完整 id）\n"
    "!cwd [路径] — 查看或切换工作目录\n"
    "!perm [模式] — 查看或切换权限模式\n"
    "  （default / acceptEdits / plan / dontAsk / bypassPermissions）\n"
    "!reset — 清空当前会话上下文，重新开始\n"
    "!resume [编号] — 列出 / 切回之前的历史会话\n"
    "!usage — 本会话花费 / token / 轮次统计\n"
    "!context — 当前上下文窗口占用情况\n"
    "!compact [说明] — 压缩上下文（可加聚焦说明），释放窗口\n"
    "!stop — 打断正在进行的回复\n"
    "!clear — 清空正在排队的待处理消息\n"
    "!id — 显示你的会话 id（配 allowlist 用）\n"
    "!ping — 探活"
)


def _resolve_model(name: str) -> str | None:
    name = name.strip()
    if name.lower() in MODEL_ALIASES:
        return MODEL_ALIASES[name.lower()]
    if name.startswith("claude-"):
        return name
    return None


class Bridge:
    def __init__(self, account_id: str, config: Dict[str, Any]):
        self.config = config
        self.ilink = IlinkClient.from_saved(account_id)
        self.sessions = store.SessionStore()
        self.state = store.BridgeState()
        self._mcp_server = None
        self._running = False
        # Per-chat warm Claude client + lock + pending-follow-up queue.
        self._agents: Dict[str, "agent_mod.ChatSession"] = {}
        self._locks: Dict[str, asyncio.Lock] = {}
        self._pending: Dict[str, List[str]] = {}
        self._dedup: set[str] = set()
        self._dedup_order: List[str] = []

    # ------------------------------------------------------------------
    # Access control
    # ------------------------------------------------------------------

    def _is_allowed(self, sender_id: str) -> bool:
        policy = str(self.config.get("dm_policy") or "first").lower()
        if policy == "open":
            return True
        if policy == "allowlist":
            return sender_id in (self.config.get("allow_from") or [])
        # "first": first ever sender claims ownership
        owner = self.state.owner_id
        if owner is None:
            self.state.set_owner(sender_id)
            logger.info("owner claimed by %s", sender_id[:10])
            return True
        return sender_id == owner

    def _seen(self, message_id: str) -> bool:
        if not message_id:
            return False
        if message_id in self._dedup:
            return True
        self._dedup.add(message_id)
        self._dedup_order.append(message_id)
        if len(self._dedup_order) > 2000:
            old = self._dedup_order.pop(0)
            self._dedup.discard(old)
        return False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def run(self) -> None:
        await self.ilink.start()
        self._mcp_server = agent_mod.build_wechat_mcp_server(self.ilink)
        self._running = True
        logger.info(
            "bridge up: account=%s dm_policy=%s cwd=%s",
            self.ilink.account_id[:10],
            self.config.get("dm_policy"),
            self.config.get("cwd"),
        )
        try:
            await self._poll_loop()
        finally:
            for a in self._agents.values():
                try:
                    await a.close()
                except Exception:  # noqa: BLE001
                    pass
            await self.ilink.close()

    def _agent_for(self, chat_id: str) -> "agent_mod.ChatSession":
        a = self._agents.get(chat_id)
        if a is None:
            resume_id = self.sessions.get(chat_id)
            # Backfill a pre-existing conversation into history so !resume sees it.
            if resume_id:
                self.sessions.ensure(chat_id, resume_id, label="(之前的会话)")
            a = agent_mod.ChatSession(
                chat_id=chat_id,
                ilink=self.ilink,
                config=self.config,
                mcp_server=self._mcp_server,
                resume_session_id=resume_id,
            )
            self._agents[chat_id] = a
        return a

    async def _poll_loop(self) -> None:
        sync_buf = store.load_sync_buf(self.ilink.account_id)
        timeout_ms = None
        consecutive_failures = 0
        while self._running:
            try:
                resp = await self.ilink.get_updates(sync_buf, timeout_ms or 35_000)
                suggested = resp.get("longpolling_timeout_ms")
                if isinstance(suggested, int) and suggested > 0:
                    timeout_ms = suggested

                ret = resp.get("ret", 0)
                errcode = resp.get("errcode", 0)
                if ret not in {0, None} or errcode not in {0, None}:
                    from .ilink import is_stale_session

                    if is_stale_session(ret, errcode, resp.get("errmsg")):
                        logger.error("session expired; pausing 10 min. Re-run `wxcc login` if this persists.")
                        await asyncio.sleep(600)
                        continue
                    consecutive_failures += 1
                    logger.warning("getupdates ret=%s errcode=%s errmsg=%s", ret, errcode, resp.get("errmsg"))
                    await asyncio.sleep(30 if consecutive_failures >= 3 else 2)
                    consecutive_failures = 0 if consecutive_failures >= 3 else consecutive_failures
                    continue

                consecutive_failures = 0
                new_buf = str(resp.get("get_updates_buf") or "")
                if new_buf:
                    sync_buf = new_buf
                    store.save_sync_buf(self.ilink.account_id, sync_buf)

                for message in resp.get("msgs") or []:
                    asyncio.create_task(self._handle_inbound(message))
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                consecutive_failures += 1
                logger.error("poll error (%d): %s", consecutive_failures, exc)
                await asyncio.sleep(30 if consecutive_failures >= 3 else 2)
                if consecutive_failures >= 3:
                    consecutive_failures = 0

    # ------------------------------------------------------------------
    # Inbound
    # ------------------------------------------------------------------

    async def _handle_inbound(self, message: Dict[str, Any]) -> None:
        try:
            sender_id = str(message.get("from_user_id") or "").strip()
            if not sender_id or sender_id == self.ilink.account_id:
                return
            # Only handle direct messages; groups are ignored (iLink bot
            # identities usually can't participate in ordinary groups anyway).
            room_id = str(message.get("room_id") or message.get("chat_room_id") or "").strip()
            if room_id:
                return
            if self._seen(str(message.get("message_id") or "").strip()):
                return
            if not self._is_allowed(sender_id):
                logger.info("ignoring message from non-owner %s", sender_id[:10])
                return

            item_list = message.get("item_list") or []
            text = extract_text(item_list)

            # Refresh the peer's context_token so outbound replies are accepted.
            context_token = str(message.get("context_token") or "").strip()
            if context_token:
                self.ilink.tokens.set(sender_id, context_token)

            # Intercept in-chat control commands before involving Claude.
            if text and text.strip().startswith("!"):
                if await self._handle_command(sender_id, text.strip()):
                    return

            media_notes = await self._collect_inbound_media(item_list)
            if not text and not media_notes:
                return

            prompt = text
            if media_notes:
                prompt = (prompt + "\n\n" if prompt else "") + "\n".join(media_notes)

            await self._dispatch(sender_id, prompt)
        except Exception as exc:  # noqa: BLE001
            logger.error("inbound handling failed: %s", exc, exc_info=True)

    async def _collect_inbound_media(self, item_list: List[Dict[str, Any]]) -> List[str]:
        """Download inbound media to the cache; return note lines pointing Claude
        at the local paths."""
        notes: List[str] = []
        for item in item_list:
            itype = item.get("type")
            spec = None
            filename = "file.bin"
            if itype == 2:  # image
                m = (item.get("image_item") or {}).get("media") or {}
                aeskey = (item.get("image_item") or {}).get("aeskey")
                aes_b64 = None
                if aeskey:
                    import base64

                    aes_b64 = base64.b64encode(bytes.fromhex(str(aeskey))).decode("ascii")
                spec = (m.get("encrypt_query_param"), aes_b64 or m.get("aes_key"), m.get("full_url"))
                filename = "image.jpg"
            elif itype in (4, 5, 3):  # file / video / voice
                key = {4: "file_item", 5: "video_item", 3: "voice_item"}[itype]
                sub = item.get(key) or {}
                if itype == 3 and sub.get("text"):
                    continue  # transcribed voice is already in the text
                m = sub.get("media") or {}
                spec = (m.get("encrypt_query_param"), m.get("aes_key"), m.get("full_url"))
                filename = str(sub.get("file_name") or {4: "document.bin", 5: "video.mp4", 3: "voice.silk"}[itype])
            if not spec:
                continue
            try:
                data = await self.ilink.download_media_bytes(
                    encrypted_query_param=spec[0], aes_key_b64=spec[1], full_url=spec[2],
                    timeout_seconds=120.0,
                )
                path = media.cache_bytes(data, filename)
                notes.append(f"[The user sent a file, saved locally at: {path}]")
            except Exception as exc:  # noqa: BLE001
                logger.warning("inbound media download failed: %s", exc)
        return notes

    # ------------------------------------------------------------------
    # In-chat control commands (!model, !reset, !status, !stop, !help)
    # ------------------------------------------------------------------

    async def _handle_command(self, chat_id: str, text: str) -> bool:
        """Handle a `!command`. Returns True if it was a known command (and was
        handled + replied), False to let the message fall through to Claude."""
        parts = text.split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""

        async def reply(msg: str) -> None:
            try:
                await self.ilink.send_text(chat_id, msg)
            except Exception as exc:  # noqa: BLE001
                logger.warning("command reply failed: %s", exc)

        if cmd == "!help":
            await reply(HELP_TEXT)
            return True

        if cmd == "!status":
            agent = self._agents.get(chat_id)
            model = (agent.current_model if agent else self.config.get("model")) or "(Claude Code 默认)"
            cwd = agent.current_cwd if agent else self.config.get("cwd")
            perm = agent.current_permission if agent else self.config.get("permission_mode")
            session = self.sessions.get(chat_id) or "(新会话)"
            live = "热(常驻)" if (agent and agent._client is not None) else "冷(未连接)"
            queued = len(self._pending.get(chat_id, []))
            await reply(
                f"模型: {model}\n会话: {session}\n状态: {live}\n"
                f"目录: {cwd}\n权限: {perm}\n排队: {queued} 条"
            )
            return True

        if cmd == "!ping":
            await reply("pong ✅ 桥接在线。")
            return True

        if cmd in ("!usage", "!cost"):
            agent = self._agents.get(chat_id)
            if not agent or agent.stats.get("turns", 0) == 0:
                await reply("本会话还没有用量数据（先聊一句）。")
                return True
            st = agent.stats
            secs = st.get("duration_ms", 0) / 1000.0
            await reply(
                "本会话用量（进程内累计，重启或 !reset 后清零）：\n"
                f"轮次: {int(st['turns'])}\n"
                f"花费: ${st['cost_usd']:.4f}\n"
                f"输入 tokens: {int(st['input_tokens']):,}\n"
                f"输出 tokens: {int(st['output_tokens']):,}\n"
                f"缓存 读/写: {int(st['cache_read']):,} / {int(st['cache_creation']):,}\n"
                f"累计耗时: {secs:.1f}s\n"
                "（订阅套餐的周额度/限额无法通过微信这个通道查询，"
                "需要在终端用 Claude Code 的 /usage 看。）"
            )
            return True

        if cmd in ("!id", "!whoami"):
            await reply(f"你的会话 id:\n{chat_id}\n（配 allow_from 白名单时用这个）")
            return True

        if cmd == "!clear":
            n = len(self._pending.pop(chat_id, []))
            await reply(f"已清空 {n} 条排队消息。" if n else "当前没有排队消息。")
            return True

        if cmd in ("!context", "!ctx"):
            agent = self._agents.get(chat_id)
            live = await agent.context_usage() if agent else None
            if live:
                await reply(_format_context(live))
                return True
            # Fall back to the per-turn estimate when the client is cold.
            if not agent or agent.stats.get("turns", 0) == 0:
                await reply("本会话还没有上下文数据（先聊一句）。")
                return True
            st = agent.stats
            used = int(st["last_input"] + st["last_cache_read"] + st["last_cache_creation"])
            window = _context_window(agent.current_model)
            pct = used / window * 100 if window else 0
            await reply(
                "上下文窗口占用（估算，最近一轮输入）：\n"
                f"{_bar(pct)} {pct:.1f}%\n"
                f"已用: {used:,} / {window:,} tokens\n"
                f"  其中 缓存命中: {int(st['last_cache_read']):,}\n"
                f"  新输入: {int(st['last_input'] + st['last_cache_creation']):,}\n"
                "（超出后 Claude Code 会自动压缩；手动压缩发 !compact，清空发 !reset。）"
            )
            return True

        if cmd == "!compact":
            lock = self._locks.setdefault(chat_id, asyncio.Lock())
            async with lock:
                agent = self._agent_for(chat_id)
                if agent._client is None and not self.sessions.get(chat_id):
                    await reply("当前没有上下文可压缩（先聊一句）。")
                    return True
                await self.ilink.set_typing(chat_id, True)
                try:
                    before = await agent.context_usage()
                    prev = agent.session_id
                    _, ok = await agent.compact(arg)
                    after = await agent.context_usage()
                finally:
                    await self.ilink.set_typing(chat_id, False)
                if agent.session_id and agent.session_id != prev:
                    self.sessions.set(chat_id, agent.session_id)
            if not ok:
                await reply("压缩未成功（可能被打断或出错），可再试一次。")
                return True
            msg = "上下文已压缩 ✅"
            b = before.get("percentage") if before else None
            a = after.get("percentage") if after else None
            if a is not None:
                if b is not None:
                    msg += f"\n占用: {b:.1f}% → {a:.1f}%"
                else:
                    msg += f"\n当前占用: {a:.1f}%"
            await reply(msg)
            return True

        if cmd in ("!stop", "!interrupt"):
            agent = self._agents.get(chat_id)
            self._pending.pop(chat_id, None)
            if agent and await agent.interrupt():
                await reply("已打断当前回复。")
            else:
                await reply("当前没有正在进行的回复。")
            return True

        if cmd == "!model":
            if not arg:
                agent = self._agents.get(chat_id)
                cur = (agent.current_model if agent else self.config.get("model")) or "(Claude Code 默认)"
                await reply(f"当前模型: {cur}\n可选: opus / sonnet / haiku / fable，或完整 id。\n用法: !model sonnet")
                return True
            model_id = _resolve_model(arg)
            if not model_id:
                await reply(f"未知模型 “{arg}”。可选: opus / sonnet / haiku / fable，或 claude- 开头的完整 id。")
                return True
            lock = self._locks.setdefault(chat_id, asyncio.Lock())
            async with lock:
                await self._agent_for(chat_id).set_model(model_id)
            await reply(f"已切换到 {model_id}，下条消息生效（上下文保留）。")
            return True

        if cmd == "!cwd":
            if not arg:
                agent = self._agents.get(chat_id)
                cur = agent.current_cwd if agent else self.config.get("cwd")
                await reply(f"当前工作目录:\n{cur}\n用法: !cwd D:\\path\\to\\dir")
                return True
            from pathlib import Path as _P

            target = _P(arg).expanduser()
            if not target.is_dir():
                await reply(f"目录不存在: {arg}")
                return True
            lock = self._locks.setdefault(chat_id, asyncio.Lock())
            async with lock:
                await self._agent_for(chat_id).set_cwd(str(target))
            await reply(f"已切换工作目录到:\n{target}\n下条消息生效（上下文保留）。")
            return True

        if cmd == "!perm":
            if not arg:
                agent = self._agents.get(chat_id)
                cur = agent.current_permission if agent else self.config.get("permission_mode")
                await reply(
                    f"当前权限模式: {cur}\n可选: {' / '.join(sorted(PERMISSION_MODES))}\n用法: !perm acceptEdits"
                )
                return True
            if arg not in PERMISSION_MODES:
                await reply(f"未知权限模式 “{arg}”。可选: {' / '.join(sorted(PERMISSION_MODES))}")
                return True
            lock = self._locks.setdefault(chat_id, asyncio.Lock())
            async with lock:
                await self._agent_for(chat_id).set_permission(arg)
            await reply(f"已切换权限模式到 {arg}，下条消息生效（上下文保留）。")
            return True

        if cmd in ("!reset", "!new"):
            lock = self._locks.setdefault(chat_id, asyncio.Lock())
            async with lock:
                agent = self._agents.pop(chat_id, None)
                if agent:
                    await agent.close()
                self.sessions.reset(chat_id)
            await reply("已清空当前会话上下文，下条消息重新开始。")
            return True

        if cmd in ("!resume", "!sessions"):
            # Backfill the current conversation so it shows even if it predates
            # the history feature.
            cur = self.sessions.get(chat_id)
            if cur:
                self.sessions.ensure(chat_id, cur, label="(当前会话)")
            entries = self.sessions.history(chat_id)
            if not entries:
                await reply("还没有历史会话。")
                return True
            current = self.sessions.get(chat_id)
            if not arg:
                lines = ["历史会话（新→旧）："]
                for i, e in enumerate(entries, 1):
                    mark = " ←当前" if e.get("id") == current else ""
                    label = e.get("label") or "(无标题)"
                    lines.append(f"{i}. {label} · {e.get('ts', '')}{mark}")
                lines.append("用法: !resume <编号> 切回该会话")
                await reply("\n".join(lines))
                return True
            if not arg.isdigit() or not (1 <= int(arg) <= len(entries)):
                await reply(f"编号无效。发 !resume 查看列表（1-{len(entries)}）。")
                return True
            chosen = entries[int(arg) - 1]
            sid = chosen.get("id")
            lock = self._locks.setdefault(chat_id, asyncio.Lock())
            async with lock:
                await self._agent_for(chat_id).resume_session(sid)
                self.sessions.switch(chat_id, sid)
            await reply(f"已切回会话：{chosen.get('label') or sid}\n下条消息接着这个上下文继续。")
            return True

        # Unknown !word — let it fall through to Claude as ordinary text.
        return False

    # ------------------------------------------------------------------
    # Dispatch to Claude (serialized per chat)
    # ------------------------------------------------------------------

    async def _dispatch(self, chat_id: str, prompt: str) -> None:
        lock = self._locks.setdefault(chat_id, asyncio.Lock())
        if lock.locked():
            # A turn is already running for this chat; fold this message in.
            self._pending.setdefault(chat_id, []).append(prompt)
            return
        async with lock:
            queued = self._pending.pop(chat_id, [])
            full_prompt = "\n".join([prompt, *queued]) if queued else prompt
            await self._run_and_reply(chat_id, full_prompt)
        # Drain anything that arrived during the turn.
        if self._pending.get(chat_id):
            leftover = "\n".join(self._pending.pop(chat_id))
            await self._dispatch(chat_id, leftover)

    async def _run_and_reply(self, chat_id: str, prompt: str) -> None:
        await self.ilink.set_typing(chat_id, True)
        try:
            agent = self._agent_for(chat_id)
            prev_session = agent.session_id
            logger.info(
                "turn start chat=%s resume_session=%s prompt=%.60r",
                chat_id[:10], (prev_session or "(new)"), prompt,
            )
            reply, new_session_id, is_error = await agent.ask(prompt)
            logger.info(
                "turn done chat=%s reply_len=%d new_session=%s is_error=%s",
                chat_id[:10], len(reply or ""), (new_session_id or "?"), is_error,
            )
            if new_session_id and new_session_id != prev_session:
                # Label a brand-new conversation with its first prompt.
                label = prompt if prev_session is None else None
                self.sessions.set(chat_id, new_session_id, label=label)

            media_files, cleaned = media.extract_media(reply)

            for path, _is_voice in media_files:
                try:
                    await self.ilink.send_file(chat_id, path)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("outbound media send failed for %s: %s", path, exc)

            if cleaned:
                for chunk in formatting.split_for_delivery(formatting.format_message(cleaned)):
                    if chunk.strip():
                        await self.ilink.send_text(chat_id, chunk)
                        delay = float(self.config.get("send_chunk_delay_seconds") or 1.5)
                        if delay > 0:
                            await asyncio.sleep(delay)
            elif not media_files:
                await self.ilink.send_text(chat_id, "(Claude returned an empty response.)")

            if is_error:
                logger.warning("turn for %s finished with is_error=True", chat_id[:10])
        except Exception as exc:  # noqa: BLE001
            logger.error("agent turn failed for %s: %s", chat_id[:10], exc, exc_info=True)
            try:
                await self.ilink.send_text(chat_id, f"⚠️ 出错了: {exc}")
            except Exception:  # noqa: BLE001
                pass
        finally:
            await self.ilink.set_typing(chat_id, False)
