"""后台附着微信窗口：断线不影响 HTTP 服务，附着成功后自动收消息。"""

from __future__ import annotations

import time

import wechatauto

from . import state, window
from .inbox import on_message
from .util import log


def attach_loop() -> None:
    shown = False
    while state.wx is None:
        try:
            client = wechatauto.WeChat()
            # 先记下附着时刻：AddListenAll 会把停机期间积压的消息补发一遍，
            # 这些消息的 create_time 早于此刻，由 inbox 按时间丢弃。
            state.attach_time = time.time()
            client.AddListenAll(on_message)
            state.wx = client
            window.tame_gui()
            state.bot_name = str(getattr(client, "nickname", "") or state.bot_name)
            state.bot_wxid = str(getattr(client, "_wxid", "") or "")
            log("attached to WeChat", f"bot={state.bot_name or '?'}")
            return
        except Exception as exc:  # noqa: BLE001
            if not shown:
                log("attach failed, retrying:", exc)
                shown = True
            # 2026-11-26：附着失败最常见的原因是微信最小化到了托盘、
            # UIA 树收起，干等 5 秒重试没用。先把窗口还原（不抢焦点），
            # 再重试，附着才有机会成功。
            try:
                if window.restore_wechat_window():
                    log("微信窗口已从托盘还原，重试附着")
            except Exception:  # noqa: BLE001
                pass
            time.sleep(5)
