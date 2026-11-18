"""消息链拆解：只取正文，落成 io-wechat 的 /send。

微信侧只保留文字收发；@、引用、图片/文件/语音都需要额外窗口点击或转文件发送，
已从 io-wechat 移除，这里也一并忽略。

MaiBot P1-3（2026-11-17）：群聊回复按句切成 2–3 段分开发，像真人打字，
第 2 段起带打字延迟（0.2s/汉字，单段封顶 4s）。私聊不拆。kill switch：
C:/dafeiyu-open/split_disabled 存在即不拆。
"""

from __future__ import annotations

import asyncio
import os
import re

from astrbot.api.event import MessageChain
from astrbot.api.message_components import Plain

from .io_client import IoWechatClient

SPLIT_FLAG = r"C:\dafeiyu-open\split_disabled"
MAX_SEGMENTS = 3


def _split_segments(text: str) -> list[str]:
    """Split a reply into <=3 sentence segments (MaiBot-style).

    Split on ，,。;！？ and newlines, never inside quotes; merge short or
    identical neighbours so we don't spam one-character segments.
    """
    text = (text or "").strip()
    if not text:
        return []
    parts = re.split(r"(?<=[，。；！？\n])", text)
    segs = [p.strip() for p in parts if p.strip()]
    if len(segs) <= 1:
        return segs
    merged: list[str] = []
    for s in segs:
        if merged and (len(s) <= 2 or s == merged[-1]):
            merged[-1] = merged[-1] + s
        else:
            merged.append(s)
    # If still over the cap, merge the shortest neighbours until it fits.
    while len(merged) > MAX_SEGMENTS:
        i = min(range(len(merged) - 1),
                key=lambda k: len(merged[k]) + len(merged[k + 1]))
        merged[i:i + 2] = [merged[i] + merged[i + 1]]
    return merged


def _typing_delay_s(segment: str) -> float:
    """0.2s per CJK char, 0.1s per other char, cap 4s (MaiBot P1-3)."""
    n_cjk = sum(1 for ch in segment if "\u4e00" <= ch <= "\u9fff")
    n_other = max(0, len(segment) - n_cjk)
    return min(4.0, 0.2 * n_cjk + 0.1 * n_other)


def chain_text(chain: MessageChain) -> str:
    """只取正文。At 不算正文：微信没有 At 消息段，塞进正文只会变成一串 "@wxid"。"""
    parts: list[str] = []
    for seg in chain.chain:
        if isinstance(seg, Plain) and seg.text:
            parts.append(seg.text)
    return "".join(parts).strip()


async def send_chain(
    client: IoWechatClient,
    session_id: str,
    message: MessageChain,
) -> None:
    """把一条 AstrBot 消息链落成 io-wechat 的 /send（仅文字）。

    群聊：长回复切成 2–3 段分开发（第 2 段起带打字延迟）；私聊和 kill
    switch 时保持原来的一次性发送。
    """
    text = chain_text(message)
    if not text:
        # 只有 At/Reply/媒体段的回合，不往微信里灌空白消息
        return
    is_group = str(session_id or "").endswith("@chatroom")
    if not is_group or os.path.exists(SPLIT_FLAG):
        await client.send_text(session_id, text)
        return
    segments = _split_segments(text)
    if len(segments) <= 1:
        await client.send_text(session_id, text)
        return
    for idx, seg in enumerate(segments):
        if idx > 0:
            await asyncio.sleep(_typing_delay_s(seg))
        ok = await client.send_text(session_id, seg)
        if not ok:
            # One segment failed: send the rest (incl. the failed one)
            # joined as a single message so nothing is lost.
            rest = "".join(segments[idx:])
            await client.send_text(session_id, rest)
            return
