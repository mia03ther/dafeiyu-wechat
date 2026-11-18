"""HTTP 发送：/send。微信侧只保留文字收发。

朋友圈、拍一拍、撤回、引用、@、图片/文件/语音等需要额外窗口点击或转文件发送的
功能都已移除，不再提供 /action 与 /space。
"""

from __future__ import annotations

from typing import Any

from infra import fail, ok

from . import state
from .config import NIGHT_REPLY_WINDOW, QUIET_END, QUIET_START
from .contacts import resolve_target
from .sending import in_quiet, night_reply_allowed, send_uid, send_image_uid
from .util import log



SENT_LOG_DIR = r"C:\dafeiyu-open\sent_log"


def _log_sent(uid, text, status):
    """Best-effort append of an outbound message to a dated JSONL log.

    Powers the learning loop's bot-message metrics. Never raises: a
    logging failure must never break a send.
    """
    try:
        import datetime
        import json
        import os
        import time

        day = datetime.datetime.now().strftime("%Y-%m-%d")
        os.makedirs(SENT_LOG_DIR, exist_ok=True)
        rec = {
            "ts": int(time.time()),
            "to": uid,
            "text": text,
            "status": status,
        }
        with open(
            os.path.join(SENT_LOG_DIR, "sent_" + day + ".jsonl"),
            "a",
            encoding="utf-8",
        ) as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def handle_send(req) -> Any:
    payload = req.payload if isinstance(req.payload, dict) else {}
    to = str(payload.get("to") or "").strip()
    text = str(payload.get("text") or "")
    if not to or not text.strip():
        return fail("to/text required（微信侧只支持文字发送）", 400)
    # NOTE 2026-11-16: do NOT gate on state.wx here. Text sends go through
    # the keyboard sender (kbd_send_group / kbd_send_dm), which finds the
    # WeChat window via win32gui and does not need the wechatauto attach.
    # When the UIA tree is collapsed the attach fails (state.wx is None)
    # even though WeChat is logged in and the window is visible, and this
    # gate used to reject every send with 503. If WeChat really is gone,
    # kbd_send raises "no Weixin main window found" below and we still 503.
    # (wechatauto is only a fallback inside send_uid.)
    try:
        uid = resolve_target(to)
    except ValueError as exc:
        return fail(str(exc), 400)

    proactive = bool(payload.get("proactive"))
    # force: operator override (explicit user request / scheduled post).
    # The /send endpoint only listens on 127.0.0.1, so only someone with
    # laptop access can set it. The bot's own autonomous sends never pass
    # force, so night quiet still protects the groups from the bot.
    force = bool(payload.get("force", False))
    if in_quiet() and not force and not night_reply_allowed(uid, proactive):
        with state.stats_lock:
            state.stats["blocked_night"] += 1
        state.counts.inc("send_blocked_night")
        log("夜间拦截主动消息:", uid, "proactive=", proactive)
        return fail(
            f"夜间 {QUIET_START:02d}:00-{QUIET_END:02d}:00 禁止主动消息；"
            f"只在收到消息后 {NIGHT_REPLY_WINDOW} 秒内允许即时回复",
            429,
        )

    exact = bool(payload.get("exact", False))
    keep_newlines = bool(payload.get("keep_newlines", False))
    try:
        status = send_uid(uid, text, exact=exact, keep_newlines=keep_newlines)
        _log_sent(uid, text, status)
        return ok({"to": uid, "status": status})
    except Exception as exc:  # noqa: BLE001
        state.counts.inc("send_failed")
        return fail(f"{type(exc).__name__}: {exc}", 503)


def handle_send_image(req) -> Any:
    """POST /send_image {to, path}: send an image file to a group.
    Sticker send-back (MaiBot-style). Same 127.0.0.1-only trust as /send.
    """
    payload = req.payload if isinstance(req.payload, dict) else {}
    to = str(payload.get("to") or "").strip()
    path = str(payload.get("path") or "").strip()
    if not to or not path:
        return fail("to/path required", 400)
    try:
        uid = resolve_target(to)
    except ValueError as exc:
        return fail(str(exc), 400)
    try:
        status = send_image_uid(uid, path)
        _log_sent(uid, "[图片] " + path, status)
        return ok({"to": uid, "status": status})
    except Exception as exc:  # noqa: BLE001
        state.counts.inc("send_failed")
        return fail(f"{type(exc).__name__}: {exc}", 503)
