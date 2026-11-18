"""IO 层：封装 io-wechat 的 /messages、/send（文字）与 /send_image（图片）。"""

from __future__ import annotations

import httpx
import os

from astrbot.api import logger


def _json(resp) -> dict:
    """io-wechat 失败时是 5xx，但正文里有真实原因，不要直接丢掉。"""
    try:
        data = resp.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _err(data: dict):
    return data.get("error") or data.get("message") or data or "未知错误"


class IoWechatClient:
    def __init__(self, base_url: str) -> None:
        self.base = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, read=60.0)
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def messages(self, since: int, wait_ms: int = 0) -> dict:
        params = {"since": since}
        if wait_ms:
            params["wait"] = wait_ms
        resp = await self._client.get(f"{self.base}/messages", params=params)
        resp.raise_for_status()
        return resp.json()

    async def send_text(self, to: str, text: str, keep_newlines: bool = False) -> bool:
        # Self-@ guard (2026-11-17, P0): the bot must NEVER send a message
        # that @-mentions itself. This bit us live in 迈阿醚: a message
        # starting with "@大肥鱼" went out from the bot account and the
        # group read it as the bot talking to itself. Any outgoing group
        # text containing "@大肥鱼" is self-@ by definition -- block it
        # here (the single choke point every text send goes through) and
        # log it, instead of sending.
        if str(to or "").endswith("@chatroom") and "@大肥鱼" in (text or ""):
            logger.warning(
                f"[wechat_bridge] 自@拦截，未发送 -> {to}: {(text or '')[:60]}")
            return False
        # Guangwai mute guard (2026-11-17): bot muted by group admin
        # for 24h (~21:29). While C:\dafeiyu-open\guangwai_muted
        # exists, block ALL sends to Guangwai (WeChat drops them
        # silently anyway) and log it, instead of sending.
        if to == "45270768515@chatroom" and os.path.exists(
            r"C:\dafeiyu-open\guangwai_muted"
        ):
            logger.warning(
                f"[wechat_bridge] 广外禁言中，拦截 -> {to}")
            return False
        payload = {"to": to, "text": text, "keep_newlines": keep_newlines}
        try:
            resp = await self._client.post(
                f"{self.base}/send",
                json=payload,
                timeout=httpx.Timeout(120.0, read=120.0),
            )
            data = _json(resp)
            if not data.get("ok"):
                logger.warning(f"[wechat_bridge] 发送失败 to={to} [{resp.status_code}]: {_err(data)}")
                return False
            logger.info(f"[wechat_bridge] 已发送 -> {to}: {text[:40]}")
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[wechat_bridge] 发送异常 to={to}: {exc}")
            return False

    async def send_image(self, to: str, path: str) -> bool:
        """POST /send_image: send an image file to a group (sticker send-back)."""
        # Guangwai mute guard (2026-11-17): bot muted by group admin
        # for 24h (~21:29). While C:\dafeiyu-open\guangwai_muted
        # exists, block ALL sends to Guangwai (WeChat drops them
        # silently anyway) and log it, instead of sending.
        if to == "45270768515@chatroom" and os.path.exists(
            r"C:\dafeiyu-open\guangwai_muted"
        ):
            logger.warning(
                f"[wechat_bridge] 广外禁言中，拦截 -> {to}")
            return False
        payload = {"to": to, "path": path}
        try:
            resp = await self._client.post(
                f"{self.base}/send_image",
                json=payload,
                timeout=httpx.Timeout(120.0, read=120.0),
            )
            data = _json(resp)
            if not data.get("ok"):
                logger.warning(f"[wechat_bridge] 发图失败 to={to} [{resp.status_code}]: {_err(data)}")
                return False
            logger.info(f"[wechat_bridge] 已发图 -> {to}: {path}")
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[wechat_bridge] 发图异常 to={to}: {exc}")
            return False
