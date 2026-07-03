"""Tencent iLink Bot API client for WeChat personal accounts.

Extracted and decoupled from hermes-agent gateway/platforms/weixin.py (MIT).

- QR login binds a bot identity to a scanned WeChat account.
- Long-poll ``getupdates`` drives inbound delivery.
- Every outbound reply should echo the latest ``context_token`` for the peer.
- Media moves through an AES-128-ECB encrypted CDN protocol.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import mimetypes
import secrets
import ssl
import sys
import struct
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote, urlparse

import aiohttp
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from . import store

logger = logging.getLogger(__name__)

ILINK_BASE_URL = "https://ilinkai.weixin.qq.com"
CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"
ILINK_APP_ID = "bot"
CHANNEL_VERSION = "2.2.0"
ILINK_APP_CLIENT_VERSION = (2 << 16) | (2 << 8) | 0

EP_GET_UPDATES = "ilink/bot/getupdates"
EP_SEND_MESSAGE = "ilink/bot/sendmessage"
EP_SEND_TYPING = "ilink/bot/sendtyping"
EP_GET_CONFIG = "ilink/bot/getconfig"
EP_GET_UPLOAD_URL = "ilink/bot/getuploadurl"
EP_GET_BOT_QR = "ilink/bot/get_bot_qrcode"
EP_GET_QR_STATUS = "ilink/bot/get_qrcode_status"

LONG_POLL_TIMEOUT_MS = 35_000
API_TIMEOUT_MS = 15_000
CONFIG_TIMEOUT_MS = 10_000
QR_TIMEOUT_MS = 35_000

SESSION_EXPIRED_ERRCODE = -14
RATE_LIMIT_ERRCODE = -2

MEDIA_IMAGE = 1
MEDIA_VIDEO = 2
MEDIA_FILE = 3
MEDIA_VOICE = 4

ITEM_TEXT = 1
ITEM_IMAGE = 2
ITEM_VOICE = 3
ITEM_FILE = 4
ITEM_VIDEO = 5

MSG_TYPE_BOT = 2
MSG_STATE_FINISH = 2

TYPING_START = 1
TYPING_STOP = 2

CDN_HOST_ALLOWLIST = frozenset(
    {
        "novac2c.cdn.weixin.qq.com",
        "ilinkai.weixin.qq.com",
        "wx.qlogo.cn",
        "thirdwx.qlogo.cn",
        "res.wx.qq.com",
        "mmbiz.qpic.cn",
        "mmbiz.qlogo.cn",
    }
)


def is_stale_session(ret: Optional[int], errcode: Optional[int], errmsg: Optional[str]) -> bool:
    """iLink signals a stale session either via errcode -14 or via a -2
    "unknown error" (which is *not* a genuine rate limit)."""
    if ret == SESSION_EXPIRED_ERRCODE or errcode == SESSION_EXPIRED_ERRCODE:
        return True
    if ret != RATE_LIMIT_ERRCODE and errcode != RATE_LIMIT_ERRCODE:
        return False
    return (errmsg or "").lower() == "unknown error"


def _pkcs7_pad(data: bytes, block_size: int = 16) -> bytes:
    pad_len = block_size - (len(data) % block_size)
    return data + bytes([pad_len] * pad_len)


def _aes128_ecb_encrypt(plaintext: bytes, key: bytes) -> bytes:
    cipher = Cipher(algorithms.AES(key), modes.ECB(), backend=default_backend())
    encryptor = cipher.encryptor()
    return encryptor.update(_pkcs7_pad(plaintext)) + encryptor.finalize()


def _aes128_ecb_decrypt(ciphertext: bytes, key: bytes) -> bytes:
    cipher = Cipher(algorithms.AES(key), modes.ECB(), backend=default_backend())
    decryptor = cipher.decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    if not padded:
        return padded
    pad_len = padded[-1]
    if 1 <= pad_len <= 16 and padded.endswith(bytes([pad_len]) * pad_len):
        return padded[:-pad_len]
    return padded


def _aes_padded_size(size: int) -> int:
    return ((size + 1 + 15) // 16) * 16


def _parse_aes_key(aes_key_b64: str) -> bytes:
    decoded = base64.b64decode(aes_key_b64)
    if len(decoded) == 16:
        return decoded
    if len(decoded) == 32:
        text = decoded.decode("ascii", errors="ignore")
        if text and all(ch in "0123456789abcdefABCDEF" for ch in text):
            return bytes.fromhex(text)
    raise ValueError(f"unexpected aes_key format ({len(decoded)} decoded bytes)")


def _random_wechat_uin() -> str:
    value = struct.unpack(">I", secrets.token_bytes(4))[0]
    return base64.b64encode(str(value).encode("utf-8")).decode("ascii")


def _json_dumps(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _headers(token: Optional[str], body: str) -> Dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "Content-Length": str(len(body.encode("utf-8"))),
        "X-WECHAT-UIN": _random_wechat_uin(),
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION),
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _make_connector() -> Optional[aiohttp.TCPConnector]:
    """certifi CA bundle when available — some system stores can't verify
    ilinkai.weixin.qq.com."""
    try:
        import certifi
    except ImportError:
        return None
    ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    return aiohttp.TCPConnector(ssl=ssl_ctx)


def _cdn_download_url(encrypted_query_param: str) -> str:
    return f"{CDN_BASE_URL}/download?encrypted_query_param={quote(encrypted_query_param, safe='')}"


def _cdn_upload_url(upload_param: str, filekey: str) -> str:
    return (
        f"{CDN_BASE_URL}/upload"
        f"?encrypted_query_param={quote(upload_param, safe='')}"
        f"&filekey={quote(filekey, safe='')}"
    )


def _assert_cdn_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise ValueError(f"disallowed scheme in media URL: {url!r}")
    if (parsed.hostname or "") not in CDN_HOST_ALLOWLIST:
        raise ValueError(f"media URL host not in WeChat CDN allowlist: {url!r}")


async def _api_post(
    session: aiohttp.ClientSession,
    *,
    base_url: str,
    endpoint: str,
    payload: Dict[str, Any],
    token: Optional[str],
    timeout_ms: int,
) -> Dict[str, Any]:
    body = _json_dumps({**payload, "base_info": {"channel_version": CHANNEL_VERSION}})
    url = f"{base_url.rstrip('/')}/{endpoint}"

    async def _do() -> Dict[str, Any]:
        async with session.post(url, data=body, headers=_headers(token, body)) as response:
            raw = await response.text()
            if not response.ok:
                raise RuntimeError(f"iLink POST {endpoint} HTTP {response.status}: {raw[:200]}")
            return json.loads(raw)

    return await asyncio.wait_for(_do(), timeout=timeout_ms / 1000)


async def _api_get(
    session: aiohttp.ClientSession,
    *,
    base_url: str,
    endpoint: str,
    timeout_ms: int,
) -> Dict[str, Any]:
    url = f"{base_url.rstrip('/')}/{endpoint}"
    headers = {
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION),
    }

    async def _do() -> Dict[str, Any]:
        async with session.get(url, headers=headers) as response:
            raw = await response.text()
            if not response.ok:
                raise RuntimeError(f"iLink GET {endpoint} HTTP {response.status}: {raw[:200]}")
            return json.loads(raw)

    return await asyncio.wait_for(_do(), timeout=timeout_ms / 1000)


async def qr_login(*, bot_type: str = "3", timeout_seconds: int = 480) -> Optional[Dict[str, str]]:
    """Interactive QR login. Prints the QR to the terminal, polls until the
    scan is confirmed, persists and returns the credential dict."""
    # trust_env=False: iLink endpoints are Tencent-domestic; routing them through
    # a system HTTP(S)_PROXY (VPN) adds ~2s per request vs ~0.1s direct.
    async with aiohttp.ClientSession(trust_env=False, connector=_make_connector()) as session:
        try:
            qr_resp = await _api_get(
                session,
                base_url=ILINK_BASE_URL,
                endpoint=f"{EP_GET_BOT_QR}?bot_type={bot_type}",
                timeout_ms=QR_TIMEOUT_MS,
            )
        except Exception as exc:
            logger.error("failed to fetch QR code: %s", exc)
            return None

        qrcode_value = str(qr_resp.get("qrcode") or "")
        qrcode_url = str(qr_resp.get("qrcode_img_content") or "")
        if not qrcode_value:
            logger.error("QR response missing qrcode")
            return None

        def _print_qr(url: str, value: str) -> None:
            data = url or value
            print("\n请使用微信扫描以下二维码：")
            if url:
                print(url)
            try:
                import io

                import qrcode as _qrcode

                qr = _qrcode.QRCode()
                qr.add_data(data)
                qr.make(fit=True)
                # Render to a text buffer, then write bytes to stdout directly so
                # a non-UTF-8 console (e.g. Windows GBK) can't choke on the
                # non-breaking-space glyphs print_ascii uses.
                buf = io.StringIO()
                qr.print_ascii(out=buf, invert=True)
                ascii_qr = buf.getvalue()
                try:
                    sys.stdout.buffer.write(ascii_qr.encode("utf-8"))
                    sys.stdout.buffer.flush()
                except Exception:
                    print(ascii_qr.encode("ascii", "replace").decode("ascii"))
            except Exception as exc:
                print(f"（终端二维码渲染失败: {exc}）")
            # Always drop a scannable PNG next to WXCC_HOME as a reliable fallback.
            try:
                png_path = store.get_home() / "qr.png"
                _qrcode.make(data).save(str(png_path))
                print(f"（也可打开二维码图片扫描：{png_path}）")
            except Exception:
                pass

        _print_qr(qrcode_url, qrcode_value)

        deadline = time.monotonic() + timeout_seconds
        current_base_url = ILINK_BASE_URL
        refresh_count = 0

        while time.monotonic() < deadline:
            try:
                status_resp = await _api_get(
                    session,
                    base_url=current_base_url,
                    endpoint=f"{EP_GET_QR_STATUS}?qrcode={qrcode_value}",
                    timeout_ms=QR_TIMEOUT_MS,
                )
            except asyncio.TimeoutError:
                await asyncio.sleep(1)
                continue
            except Exception as exc:
                logger.warning("QR poll error: %s", exc)
                await asyncio.sleep(1)
                continue

            status = str(status_resp.get("status") or "wait")
            if status == "wait":
                print(".", end="", flush=True)
            elif status == "scaned":
                print("\n已扫码，请在微信里确认...")
            elif status == "scaned_but_redirect":
                redirect_host = str(status_resp.get("redirect_host") or "")
                if redirect_host:
                    current_base_url = f"https://{redirect_host}"
            elif status == "expired":
                refresh_count += 1
                if refresh_count > 3:
                    print("\n二维码多次过期，请重新执行登录。")
                    return None
                print(f"\n二维码已过期，正在刷新... ({refresh_count}/3)")
                try:
                    qr_resp = await _api_get(
                        session,
                        base_url=ILINK_BASE_URL,
                        endpoint=f"{EP_GET_BOT_QR}?bot_type={bot_type}",
                        timeout_ms=QR_TIMEOUT_MS,
                    )
                    qrcode_value = str(qr_resp.get("qrcode") or "")
                    qrcode_url = str(qr_resp.get("qrcode_img_content") or "")
                    _print_qr(qrcode_url, qrcode_value)
                except Exception as exc:
                    logger.error("QR refresh failed: %s", exc)
                    return None
            elif status == "confirmed":
                account_id = str(status_resp.get("ilink_bot_id") or "")
                token = str(status_resp.get("bot_token") or "")
                base_url = str(status_resp.get("baseurl") or ILINK_BASE_URL)
                user_id = str(status_resp.get("ilink_user_id") or "")
                if not account_id or not token:
                    logger.error("QR confirmed but credential payload was incomplete")
                    return None
                store.save_account(
                    account_id=account_id, token=token, base_url=base_url, user_id=user_id
                )
                print(f"\n微信绑定成功，account_id={account_id}")
                return {
                    "account_id": account_id,
                    "token": token,
                    "base_url": base_url,
                    "user_id": user_id,
                }
            await asyncio.sleep(1)

        print("\n微信登录超时。")
        return None


def extract_text(item_list: List[Dict[str, Any]]) -> str:
    """Text content of an inbound message, including quoted-message context and
    server-side voice transcription."""
    for item in item_list:
        if item.get("type") == ITEM_TEXT:
            text = str((item.get("text_item") or {}).get("text") or "")
            ref = item.get("ref_msg") or {}
            ref_item = ref.get("message_item") or {}
            ref_type = ref_item.get("type")
            if ref_type in {ITEM_IMAGE, ITEM_VIDEO, ITEM_FILE, ITEM_VOICE}:
                title = ref.get("title") or ""
                prefix = f"[引用媒体: {title}]\n" if title else "[引用媒体]\n"
                return f"{prefix}{text}".strip()
            if ref_item:
                parts: List[str] = []
                if ref.get("title"):
                    parts.append(str(ref["title"]))
                ref_text = extract_text([ref_item])
                if ref_text:
                    parts.append(ref_text)
                if parts:
                    return f"[引用: {' | '.join(parts)}]\n{text}".strip()
            return text
    for item in item_list:
        if item.get("type") == ITEM_VOICE:
            voice_text = str((item.get("voice_item") or {}).get("text") or "")
            if voice_text:
                return voice_text
    return ""


class IlinkClient:
    """Thin async client bound to one logged-in bot account."""

    def __init__(
        self,
        *,
        account_id: str,
        token: str,
        base_url: str = ILINK_BASE_URL,
        send_chunk_retries: int = 4,
        send_chunk_retry_delay: float = 1.0,
    ):
        self.account_id = account_id
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.tokens = store.ContextTokenStore(account_id)
        self._send_chunk_retries = send_chunk_retries
        self._send_chunk_retry_delay = send_chunk_retry_delay
        self._session: Optional[aiohttp.ClientSession] = None
        self._send_gate = asyncio.Lock()
        self._typing_tickets: Dict[str, Tuple[str, float]] = {}

    @classmethod
    def from_saved(cls, account_id: str) -> "IlinkClient":
        creds = store.load_account(account_id)
        if not creds or not creds.get("token"):
            raise RuntimeError(f"no saved credentials for account {account_id}; run login first")
        return cls(
            account_id=account_id,
            token=str(creds["token"]),
            base_url=str(creds.get("base_url") or ILINK_BASE_URL),
        )

    async def __aenter__(self) -> "IlinkClient":
        await self.start()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def start(self) -> None:
        if self._session is None or self._session.closed:
            no_timeout = aiohttp.ClientTimeout(
                total=None, connect=None, sock_connect=None, sock_read=None
            )
            # trust_env=False: keep Tencent-domestic iLink traffic off the system
            # proxy (see qr_login) — the VPN hop costs ~2s per request.
            self._session = aiohttp.ClientSession(
                trust_env=False, connector=_make_connector(), timeout=no_timeout
            )

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    @property
    def session(self) -> aiohttp.ClientSession:
        assert self._session is not None, "IlinkClient not started"
        return self._session

    # ------------------------------------------------------------------
    # Inbound
    # ------------------------------------------------------------------

    async def get_updates(self, sync_buf: str, timeout_ms: int = LONG_POLL_TIMEOUT_MS) -> Dict[str, Any]:
        try:
            return await _api_post(
                self.session,
                base_url=self.base_url,
                endpoint=EP_GET_UPDATES,
                payload={"get_updates_buf": sync_buf},
                token=self.token,
                timeout_ms=timeout_ms,
            )
        except asyncio.TimeoutError:
            return {"ret": 0, "msgs": [], "get_updates_buf": sync_buf}

    async def download_media_bytes(
        self,
        *,
        encrypted_query_param: Optional[str],
        aes_key_b64: Optional[str],
        full_url: Optional[str],
        timeout_seconds: float = 60.0,
    ) -> bytes:
        if encrypted_query_param:
            url = _cdn_download_url(encrypted_query_param)
        elif full_url:
            _assert_cdn_url(full_url)
            url = full_url
        else:
            raise RuntimeError("media item had neither encrypt_query_param nor full_url")

        async def _do() -> bytes:
            async with self.session.get(url) as response:
                response.raise_for_status()
                return await response.read()

        raw = await asyncio.wait_for(_do(), timeout=timeout_seconds)
        if aes_key_b64:
            raw = _aes128_ecb_decrypt(raw, _parse_aes_key(aes_key_b64))
        return raw

    # ------------------------------------------------------------------
    # Typing indicator
    # ------------------------------------------------------------------

    async def _typing_ticket(self, chat_id: str) -> Optional[str]:
        entry = self._typing_tickets.get(chat_id)
        if entry and time.time() - entry[1] < 590:
            return entry[0]
        try:
            payload: Dict[str, Any] = {"ilink_user_id": chat_id}
            context_token = self.tokens.get(chat_id)
            if context_token:
                payload["context_token"] = context_token
            response = await _api_post(
                self.session,
                base_url=self.base_url,
                endpoint=EP_GET_CONFIG,
                payload=payload,
                token=self.token,
                timeout_ms=CONFIG_TIMEOUT_MS,
            )
            ticket = str(response.get("typing_ticket") or "")
            if ticket:
                self._typing_tickets[chat_id] = (ticket, time.time())
                return ticket
        except Exception as exc:
            logger.debug("typing ticket fetch failed for %s: %s", chat_id[:8], exc)
        return None

    async def set_typing(self, chat_id: str, active: bool) -> None:
        ticket = await self._typing_ticket(chat_id)
        if not ticket:
            return
        try:
            await _api_post(
                self.session,
                base_url=self.base_url,
                endpoint=EP_SEND_TYPING,
                payload={
                    "ilink_user_id": chat_id,
                    "typing_ticket": ticket,
                    "status": TYPING_START if active else TYPING_STOP,
                },
                token=self.token,
                timeout_ms=CONFIG_TIMEOUT_MS,
            )
        except Exception as exc:
            logger.debug("typing signal failed for %s: %s", chat_id[:8], exc)

    # ------------------------------------------------------------------
    # Outbound text
    # ------------------------------------------------------------------

    async def send_text(self, chat_id: str, text: str) -> str:
        """Send one text chunk (<=2000 chars) with retries. Returns client_id.

        On a stale-session error the send is retried once without the cached
        context_token (iLink accepts tokenless sends as a degraded fallback,
        which keeps agent-initiated pushes working)."""
        if not text or not text.strip():
            raise ValueError("send_text: text must not be empty")
        async with self._send_gate:
            return await self._send_text_locked(chat_id, text)

    async def _send_text_locked(self, chat_id: str, text: str) -> str:
        context_token = self.tokens.get(chat_id)
        client_id = f"wxcc-{uuid.uuid4().hex}"
        retried_without_token = False
        last_error: Optional[Exception] = None
        for attempt in range(self._send_chunk_retries + 1):
            try:
                message: Dict[str, Any] = {
                    "from_user_id": "",
                    "to_user_id": chat_id,
                    "client_id": client_id,
                    "message_type": MSG_TYPE_BOT,
                    "message_state": MSG_STATE_FINISH,
                    "item_list": [{"type": ITEM_TEXT, "text_item": {"text": text}}],
                }
                if context_token:
                    message["context_token"] = context_token
                resp = await _api_post(
                    self.session,
                    base_url=self.base_url,
                    endpoint=EP_SEND_MESSAGE,
                    payload={"msg": message},
                    token=self.token,
                    timeout_ms=API_TIMEOUT_MS,
                )
                ret = resp.get("ret")
                errcode = resp.get("errcode")
                if (ret not in {0, None}) or (errcode not in {0, None}):
                    errmsg = resp.get("errmsg") or resp.get("msg") or "unknown error"
                    if is_stale_session(ret, errcode, resp.get("errmsg")) and not retried_without_token and context_token:
                        retried_without_token = True
                        context_token = None
                        self.tokens.pop(chat_id)
                        logger.warning("session expired for %s; retrying without context_token", chat_id[:8])
                        continue
                    if ret == RATE_LIMIT_ERRCODE or errcode == RATE_LIMIT_ERRCODE:
                        last_error = RuntimeError(f"iLink rate limited: {errmsg}")
                        if attempt >= self._send_chunk_retries:
                            break
                        wait = self._send_chunk_retry_delay * 3
                        logger.warning("rate limited for %s; backing off %.1fs", chat_id[:8], wait)
                        await asyncio.sleep(wait)
                        continue
                    raise RuntimeError(f"iLink sendmessage error: ret={ret} errcode={errcode} errmsg={errmsg}")
                return client_id
            except Exception as exc:
                last_error = exc
                if attempt >= self._send_chunk_retries:
                    break
                wait = self._send_chunk_retry_delay * (attempt + 1)
                logger.warning(
                    "send failed to=%s attempt=%d/%d, retrying in %.1fs: %s",
                    chat_id[:8], attempt + 1, self._send_chunk_retries + 1, wait, exc,
                )
                await asyncio.sleep(wait)
        assert last_error is not None
        raise last_error

    # ------------------------------------------------------------------
    # Outbound media
    # ------------------------------------------------------------------

    def _outbound_media_builder(
        self, path: str, force_file_attachment: bool = False
    ) -> Tuple[int, Callable[..., Dict[str, Any]]]:
        mime = mimetypes.guess_type(path)[0] or "application/octet-stream"

        def _media(kw: Dict[str, Any]) -> Dict[str, Any]:
            return {
                "encrypt_query_param": kw["encrypt_query_param"],
                "aes_key": kw["aes_key_for_api"],
                "encrypt_type": 1,
            }

        if mime.startswith("image/"):
            return MEDIA_IMAGE, lambda **kw: {
                "type": ITEM_IMAGE,
                "image_item": {"media": _media(kw), "mid_size": kw["ciphertext_size"]},
            }
        if mime.startswith("video/"):
            return MEDIA_VIDEO, lambda **kw: {
                "type": ITEM_VIDEO,
                "video_item": {
                    "media": _media(kw),
                    "video_size": kw["ciphertext_size"],
                    "play_length": kw.get("play_length", 0),
                    "video_md5": kw.get("rawfilemd5", ""),
                },
            }
        if path.endswith(".silk") and not force_file_attachment:
            return MEDIA_VOICE, lambda **kw: {
                "type": ITEM_VOICE,
                "voice_item": {
                    "media": _media(kw),
                    "encode_type": kw.get("encode_type"),
                    "bits_per_sample": kw.get("bits_per_sample"),
                    "sample_rate": kw.get("sample_rate"),
                    "playtime": kw.get("playtime", 0),
                },
            }
        return MEDIA_FILE, lambda **kw: {
            "type": ITEM_FILE,
            "file_item": {
                "media": _media(kw),
                "file_name": kw["filename"],
                "len": str(kw["plaintext_size"]),
            },
        }

    async def send_file(
        self,
        chat_id: str,
        path: str,
        caption: str = "",
        force_file_attachment: bool = False,
    ) -> str:
        """Encrypt + upload a local file to the WeChat CDN, then send it.
        Images/videos render natively; audio/other go as file attachments."""
        plaintext = Path(path).read_bytes()
        media_type, item_builder = self._outbound_media_builder(
            path, force_file_attachment=force_file_attachment
        )
        filekey = secrets.token_hex(16)
        aes_key = secrets.token_bytes(16)
        rawsize = len(plaintext)
        rawfilemd5 = hashlib.md5(plaintext).hexdigest()
        upload_response = await _api_post(
            self.session,
            base_url=self.base_url,
            endpoint=EP_GET_UPLOAD_URL,
            payload={
                "filekey": filekey,
                "media_type": media_type,
                "to_user_id": chat_id,
                "rawsize": rawsize,
                "rawfilemd5": rawfilemd5,
                "filesize": _aes_padded_size(rawsize),
                "no_need_thumb": True,
                "aeskey": aes_key.hex(),
            },
            token=self.token,
            timeout_ms=API_TIMEOUT_MS,
        )
        upload_param = str(upload_response.get("upload_param") or "")
        upload_full_url = str(upload_response.get("upload_full_url") or "")
        ciphertext = _aes128_ecb_encrypt(plaintext, aes_key)

        if upload_full_url:
            upload_url = upload_full_url
        elif upload_param:
            upload_url = _cdn_upload_url(upload_param, filekey)
        else:
            raise RuntimeError(
                f"getUploadUrl returned neither upload_param nor upload_full_url: {upload_response}"
            )

        async def _do_upload() -> str:
            async with self.session.post(
                upload_url, data=ciphertext, headers={"Content-Type": "application/octet-stream"}
            ) as response:
                if response.status == 200:
                    encrypted_param = response.headers.get("x-encrypted-param")
                    if encrypted_param:
                        await response.read()
                        return encrypted_param
                    raw = await response.text()
                    raise RuntimeError(f"CDN upload missing x-encrypted-param header: {raw[:200]}")
                raw = await response.text()
                raise RuntimeError(f"CDN upload HTTP {response.status}: {raw[:200]}")

        encrypted_query_param = await asyncio.wait_for(_do_upload(), timeout=120)

        # iLink expects aes_key as base64(hex_string), not base64(raw_bytes) —
        # base64(raw_bytes) makes images render as grey boxes on the receiver.
        aes_key_for_api = base64.b64encode(aes_key.hex().encode("ascii")).decode("ascii")
        item_kwargs: Dict[str, Any] = {
            "encrypt_query_param": encrypted_query_param,
            "aes_key_for_api": aes_key_for_api,
            "ciphertext_size": len(ciphertext),
            "plaintext_size": rawsize,
            "filename": Path(path).name,
            "rawfilemd5": rawfilemd5,
        }
        if media_type == MEDIA_VOICE and path.endswith(".silk"):
            item_kwargs["encode_type"] = 6
            item_kwargs["sample_rate"] = 24000
            item_kwargs["bits_per_sample"] = 16
        media_item = item_builder(**item_kwargs)

        if caption:
            await self.send_text(chat_id, caption)

        context_token = self.tokens.get(chat_id)
        client_id = f"wxcc-{uuid.uuid4().hex}"
        await _api_post(
            self.session,
            base_url=self.base_url,
            endpoint=EP_SEND_MESSAGE,
            payload={
                "msg": {
                    "from_user_id": "",
                    "to_user_id": chat_id,
                    "client_id": client_id,
                    "message_type": MSG_TYPE_BOT,
                    "message_state": MSG_STATE_FINISH,
                    "item_list": [media_item],
                    **({"context_token": context_token} if context_token else {}),
                }
            },
            token=self.token,
            timeout_ms=API_TIMEOUT_MS,
        )
        return client_id
