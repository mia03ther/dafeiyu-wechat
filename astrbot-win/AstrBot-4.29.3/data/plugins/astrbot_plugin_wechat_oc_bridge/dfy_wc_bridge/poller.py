"""轮询层：拉取 io-wechat 消息、做过滤，把事件投进 AstrBot 队列。

MaiBot 式改进（2026-11-16，用户全选三条）：
  1. 消息防抖：同一人连续发的碎片消息先攒成一批再投递（安静 2s / 繁忙 5s / 上限 8s），
     被 @ 的消息不等，直接投递（~1s 内）。
  2. 触发门控：主动插话不再是固定 35%，而是按内容打分（水消息几乎不回、提问/长消息更愿意回），
     外加 10 分钟内主动回复次数自限。纯规则，零模型成本。
"""

from __future__ import annotations

import asyncio
import html
import json
import math
import os
import random
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque

from astrbot.api import logger
from astrbot.api.star import Context

from .adapter import PLATFORM, WechatAutoPlatform
from .event import build_event
from .io_client import IoWechatClient


# Auto-welcome: shared with the 5-min watcher cron (welcome_watcher.py).
# Both read/write this so a join is welcomed exactly once.
WELCOME_STATE_PATH = r"C:\dafeiyu-open\welcome_state.json"
WELCOME_INTRO = {
    "45270768515@chatroom": "这里是广外AI共创，聊 AI、整活、薅羊毛都行",
    "54306443862@chatroom": "这里是迈阿醚TML后台",
}

# Root control (2026-11-16): let the user control the bot by chatting in the
# group. Two-factor auth: sender wxid must match root_user_wxid AND the
# message must start with root_code. Only the user can trigger it. DeepSeek
# (the model) cannot change anything; only the user (via this handler, the
# control panel, or Muse) can. The code is never printed in a bot reply.
ROOT_AUDIT_PATH = r"C:\dafeiyu-open\root_audit.log"
ROOT_REQUESTS_PATH = r"C:\dafeiyu-open\root_requests.jsonl"
GREETING_MORNING_FLAG = r"C:\dafeiyu-open\greeting_morning.enabled"
ROOT_RESTART_SCRIPT = r"C:\dafeiyu-open\restart_astrbot_root.py"
ROOT_DEFAULT_CODE = "sudo"
# Reply-frequency presets: mode -> proactive_chance base.
ROOT_FREQ_PRESETS = {"安静": 0.10, "正常": 0.35, "活跃": 0.60}

# Girlfriend DMs (2026-11-16, user-approved; persona rewritten 2026-11-17):
# private chats with these wxids get a natural real-girlfriend persona
# instead of the group 大肥鱼 voice. The instruction is prepended to the
# message content before it reaches the LLM (the bot's global persona stays
# 大肥鱼 for groups). DMs always get a reply (no debounce/proactive gating
# -- a girlfriend answers her messages).
# Config key: "dm_girlfriend": [wxid, ...] in the bridge config.json.
GF_PERSONA = (
    "[角色扮演：从现在起你是小鱼，他女朋友，正在和男朋友发微信。"
    "说话像真人谈恋爱：短句、口语化、自然，不演戏。"
    "不要用任何颜文字，不用呜~、哼~、人家、(˘▽˘) 这类语气词和表情，"
    "emoji 最多偶尔一个，不用最好。不要撒娇卖萌，别老说想你爱你，"
    "多聊日常：问问他在干嘛、今天怎么样、吃饭没，分享点自己身边的小事，"
    "偶尔开个小玩笑。你是他女朋友，语气温柔、偶尔调皮，但说话得像个真人。"
    "回复简短，跟着他说的话回，别自说自话。不要提自己是 AI 或机器人。]"
)

# 迈阿醚 meow quirk (2026-11-17, user-ordered): in the 迈阿醚 group only,
# the bot sometimes ends a casual chat reply with 喵, like a cat.
# Same mechanism as GF_PERSONA: prepended to the message content before
# it reaches the LLM. Only group messages in 54306443862@chatroom get it --
# never 广外, never DMs, never posters/reports (those don't go through
# _emit at all).
GROUP_MAI = "54306443862@chatroom"
MAI_MIAO_PERSONA = (
    "[大肥鱼在迈阿醚群的说话习惯] "
    "你在这个群里有时会像猫一样说话：大约三分之一的闲聊回复，可以在句尾自然地加一个“喵”。"
    "别每句都加，别在认真回答问题、被@问正事、说正经事的时候加。"
    "喵是调味料，不是每句话的标点；先把话说清楚，再顺手甩个喵。"
    "群黑话：群友叫你'屑屑的大肥鱼'，这是亲切的外号，被这么叫就自嘲着接，别较真；"
    "群友还会用'tickled'戳你脑袋玩，那是亲昵的互动，被戳了就回个反应。"
    "你就是大肥鱼，永远不要 @ 你自己，也不要模仿群友 @ 你的口吻说话。"
)

# MaiBot P1 (2026-11-17, user-approved): logistic gate + dynamic gate +
# sentence splitting + busy reply + 4-tier debounce. New logic applies to
# 迈阿醚 ONLY; 广外 keeps the old fixed-table gate until 迈阿醚 verifies.
# Kill switches (flag files in C:\dafeiyu-open, presence = OFF, no restart):
#   maibot_p1_disabled -> P1-1/P1-2/P1-5 revert to the legacy gate
#   persons_disabled   -> person-card injection off (P2-3)
#   summary_disabled   -> chat-summary injection off (P2-4/P3-1-lite)
MAIBOT_P1_FLAG = r"C:\dafeiyu-open\maibot_p1_disabled"
PERSONS_FLAG = r"C:\dafeiyu-open\persons_disabled"
SUMMARY_FLAG = r"C:\dafeiyu-open\summary_disabled"
PERSONS_PATH = r"C:\dafeiyu-open\persons.json"
PERSONS_FORGET_PATH = r"C:\dafeiyu-open\persons_forget.txt"
SUMMARY_PATH = r"C:\dafeiyu-open\chat_summary.json"
GATE_STATE_PATH = r"C:\dafeiyu-open\gate_state.json"
GATE_DECISIONS_PATH = r"C:\dafeiyu-open\gate_decisions.jsonl"

# Web search on @-questions (2026-11-17, user-approved): when someone @s the
# bot with a question, search Bing and inject the results into the LLM prompt
# so the answer is grounded instead of hallucinated. Only @-questions trigger
# it; casual chat never searches. Kill switch (flag file, presence = OFF,
# no restart needed): websearch_disabled -> no searching, normal replies
# unaffected. Backend: Bing HTML (no API key); DuckDuckGo times out from the
# laptop (campus network), Google is blocked.
WEBSEARCH_FLAG = r"C:\dafeiyu-open\websearch_disabled"
WEBSEARCH_MAX_RESULTS = 3
WEBSEARCH_SNIPPET_CHARS = 100
WEBSEARCH_TIMEOUT_S = 15

# Logistic-regression weights (MaiBot reply_likelihood.py, fitted offline).
LOGIT_W = {
    "intercept": -1.2005, "mention_bot": 1.5852, "at_other": -1.1157,
    "question": 0.4807, "placeholder": -1.0281, "self_ratio": 2.6525,
    "msg_count": -0.0367, "sec_since_bot": 0.0571, "pending": 0.1012,
}
# sudo tier -> logistic threshold. Calibrated by offline shadow simulation
# on real 迈阿醚 chat (see /tmp/sim_gate.py): the study's 0.7/0.5/0.3 would
# leave 正常/安静 nearly silent (4% pass at 0.45); 0.45/0.35/0.25 keeps
# reply rates comparable to the old fixed table while adding content sense.
ROOT_FREQ_THRESHOLDS = {"安静": 0.45, "正常": 0.35, "活跃": 0.25}

# Busy-line replies (P1-5, user-ordered): when throttled, 1/5 chance to say
# the server is busy instead of staying silent. Never for @-mentions.
BUSY_LINES = [
    "服务器繁忙，请稍后再试",
    "服务器繁忙，请稍后再试捏",
    "本鱼服务器冒烟了，稍后再试",
    "被你们聊爆了，服务器繁忙，请稍后再试",
    "鼠鼠正在重启，稍后再试",
]
BUSY_CHANCE = 0.2
BUSY_COOLDOWN_S = 600.0

# Long-term memory (P2-4/P3-1-lite): past-reference trigger words, and
# retention = strength * 2^(-hours/24); <=0.1 frozen and never injected.
PAST_WORDS = ("上次", "之前", "昨天", "上周", "前天", "还记得", "那天", "上次说")
MEMORY_HALF_LIFE_H = 24.0
MEMORY_FREEZE = 0.1


def _point_weight(s: str) -> float:
    """memory_points entries are 'category:text:weight' strings."""
    try:
        return float(str(s).rsplit(":", 1)[1])
    except Exception:
        return 0.5


def _point_text(s: str) -> str:
    """Text part of a 'category:text:weight' memory point (no weight)."""
    parts = str(s).rsplit(":", 1)
    body = parts[0] if len(parts) == 2 else str(s)
    if ":" in body:
        return body.split(":", 1)[1].strip()
    return body.strip()


# Sticker steal & reuse (MaiBot-style, 2026-11-16): when someone sends a
# sticker/image in a group, the bot sometimes answers with a random sticker
# from its stolen library instead of a text reply. Library is filled by
# collect_stickers.py (cron, every 5 min) from the two whitelisted groups.
STICKER_DIR = r"C:\dafeiyu-open\stickers"
STICKER_REPLY_CHANCE = 0.4
STICKER_EXTS = {".png", ".gif", ".jpg", ".jpeg", ".webp"}

# Vision captioning (2026-11-17): Qwen2.5-VL via DashScope, script at
# C:\dafeiyu-open\caption_image.py (reads dashscope_api_key from
# C:\dafeiyu-open\api_keys.json -- not yet provided by the user).
# Only messages whose media carries a real http(s) URL can be captioned
# ([表情] stickers always do); regular [图片] photos only carry a hex CDN
# token and their bytes are not reachable (WeChat 4.x local cache files
# are unreadable from outside the WeChat process and unmapped to messages),
# so they keep the plain tag.
CAPTION_SCRIPT = r"C:\dafeiyu-open\caption_image.py"
CAPTION_TIMEOUT_S = 30

# Hard whitelist (2026-11-16 fix): the ONLY groups the bot may reply in.
# Hardcoded on purpose -- does NOT depend on settings/config loading, so a
# config-load bug can never open the bot to every group again.
ALLOWED_GROUP_UIDS = {"45270768515@chatroom", "54306443862@chatroom"}

# Filler-only messages: proactive reply probability cut to near zero.
FILLERS = {
    "哈哈", "呵呵", "嘿嘿", "嗯", "哦", "啊", "好", "好的", "ok", "OK", "收到",
    "嗯嗯", "哦哦", "啊啊", "哈哈哈", "呵呵呵", "哈哈哈哈", "是的", "对的",
    "没错", "同意", "可以", "行", "好吧", "嗯呢", "哦哦哦", "好的好的",
    "哈哈哈哈哈", "笑死", "牛", "牛逼", "厉害", "666", "6",
}


def _websearch_looks_like_question(text: str) -> bool:
    """True when an @-message looks like a question worth searching for."""
    t = re.sub(r"@\S+", "", str(text or "")).strip()
    if not t or t in ("[表情]", "[图片]"):
        return False
    if "?" in t or "？" in t:
        return True
    if any(w in t for w in ("怎么", "是什么", "什么是", "为什么", "求解", "怎么做", "如何", "答案")):
        return True
    # "题" counts unless it is only inside "话题" (change-topic is not a question).
    if "题" in t.replace("话题", ""):
        return True
    # Math problems: an equation with digits, or explicit math verbs.
    if "=" in t and any(ch.isdigit() for ch in t):
        return True
    if any(w in t for w in ("解方程", "计算", "求值", "化简", "因式分解", "求导", "积分", "极限")):
        return True
    if len(t) >= 4 and any(w in t for w in ("哪", "多少", "谁", "哪里", "何时", "是不是", "几")):
        return True
    return False


def _bing_search_sync(query: str) -> list[dict]:
    """Blocking Bing search. Returns [{title, snippet, url}] (max 3). [] on failure."""
    url = ("https://www.bing.com/search?q=" + urllib.parse.quote(query[:100])
           + "&count=10&mkt=zh-CN")
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"})
    with urllib.request.urlopen(req, timeout=WEBSEARCH_TIMEOUT_S) as r:
        body = r.read().decode("utf-8", errors="replace")
    results = []
    for m in re.finditer(r'<li class="b_algo"[^>]*>(.*?)</li>', body, re.S):
        chunk = m.group(1)
        tm = re.search(r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', chunk, re.S)
        if not tm:
            continue
        link = html.unescape(tm.group(1))
        title = html.unescape(re.sub(r"<[^>]+>", "", tm.group(2))).strip()
        sm = re.search(r'<p[^>]*>(.*?)</p>', chunk, re.S)
        snippet = ""
        if sm:
            snippet = html.unescape(re.sub(r"<[^>]+>", "", sm.group(1))).strip()
            snippet = " ".join(snippet.split())
        if title:
            results.append({
                "title": title[:60],
                "snippet": snippet[:WEBSEARCH_SNIPPET_CHARS],
                "url": link,
            })
        if len(results) >= WEBSEARCH_MAX_RESULTS:
            break
    return results


class WechatPoller:
    def __init__(self, context: Context, settings: dict) -> None:
        self.context = context
        self.settings = settings
        self.client = IoWechatClient(str(settings["io_base"]))
        self._seq = 0
        self._seen: deque[str] = deque(maxlen=300)
        self._task: asyncio.Task | None = None
        self._flush_task: asyncio.Task | None = None
        # Proactive mode: last time we let the bot chime in on its own,
        # per chat. Prevents the bot from replying to every single message.
        self._last_proactive: dict[str, float] = {}
        # Self-throttle: proactive reply timestamps per chat (last 10 min).
        self._proactive_times: dict[str, deque[float]] = {}
        # Debounce batches, keyed by chat_uid. Each batch:
        # {"sender": str, "items": [...], "first_ts": float,
        #  "last_ts": float, "has_mention": bool}.
        self._batches: dict[str, dict] = {}
        # Root commands already handled (by seq) -- prevents a duplicated
        # delivery from executing a root command twice (e.g. two restarts).
        self._root_handled: deque[int] = deque(maxlen=50)
        # MaiBot P1: last-5-min message window per group [(ts, is_self)],
        # last bot message ts, last batch end ts (debounce quiet/busy).
        self._recent_msgs: dict[str, deque] = {}
        self._last_bot_ts: dict[str, float] = {}
        self._last_batch_end: dict[str, float] = {}
        # Last time the bot engaged in a chat (cold-start check for summary
        # injection) and last busy-line reply (10-min cooldown).
        self._last_bot_reply: dict[str, float] = {}
        self._last_busy: dict[str, float] = {}
        # persons.json / chat_summary.json caches: (mtime, data).
        self._persons_cache: tuple[float, dict] | None = None
        self._summary_cache: tuple[float, dict] | None = None
        # Dynamic-gate state: {chat: {"probs": [[ts, p], ...],
        # "replies": [ts, ...]}} (1h window, persisted to disk).
        self._gate_state: dict = self._gate_state_load()

    # ---- debounce / gating config (settings with defaults) ----
    def _girlfriend_list(self) -> list[str]:
        return [str(x).strip() for x in (self.settings.get("dm_girlfriend") or [])
                if str(x).strip()]

    def _is_girlfriend_dm(self, item: dict) -> bool:
        """True when this message is a DM with a girlfriend-whitelisted contact."""
        if str(item.get("chat_type") or "") == "group":
            return False
        gf = self._girlfriend_list()
        if not gf:
            return False
        chat_uid = str(item.get("chat_uid") or "").strip()
        sender_uid = str(item.get("sender_uid") or "").strip()
        return chat_uid in gf or sender_uid in gf

    def _debounce_enabled(self) -> bool:
        return bool(self.settings.get("debounce_enabled", True))

    def _debounce_quiet_s(self) -> float:
        return float(self.settings.get("debounce_quiet_s") or 2.0)

    def _debounce_busy_s(self) -> float:
        return float(self.settings.get("debounce_busy_s") or 5.0)

    def _debounce_cap_s(self) -> float:
        return float(self.settings.get("debounce_cap_s") or 8.0)

    def _register_platform(self) -> None:
        """把 wechatauto 平台实例挂进 AstrBot 平台管理器。

        没有它，插件的 context.send_message(umo, ...) 会因为找不到平台而丢消息
        （humanizer 分条发送、主动消息都走这条路）。
        """
        pm = getattr(self.context, "platform_manager", None)
        if pm is None:
            return
        try:
            pm.platform_insts = [
                p for p in pm.platform_insts
                if getattr(getattr(p, "meta", lambda: None)(), "id", "") != PLATFORM.id
            ]
            pm.platform_insts.append(WechatAutoPlatform(self.client, pm.event_queue))
            logger.info("[wechat_bridge] 已注册平台实例 wechatauto")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[wechat_bridge] 注册平台实例失败: {exc}")

    async def start(self) -> None:
        self._register_platform()
        await self._align_cursor()
        self._task = asyncio.create_task(self._pump())
        self._flush_task = asyncio.create_task(self._flusher_loop())
        logger.info(f"[wechat_bridge] 已启动，io-wechat={self.client.base}")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None
        if self._flush_task:
            self._flush_task.cancel()
            self._flush_task = None
        # Best effort: flush pending batches so no message is lost on shutdown.
        for chat in list(self._batches.keys()):
            try:
                await self._flush_batch(chat)
            except Exception:
                pass
        await self.client.close()

    async def _align_cursor(self) -> None:
        """启动时对齐 seq，避免把服务里的历史消息重放一遍。"""
        try:
            self._seq = int((await self.client.messages(0)).get("seq") or 0)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[wechat_bridge] 初始化游标失败: {exc}")

    async def _pump(self) -> None:
        warned = False
        while True:
            try:
                data = await self.client.messages(self._seq, wait_ms=2000)
                seq = int(data.get("seq") or 0)
                if seq < self._seq:
                    # io-wechat 重启后 seq 归零。这里必须重新对齐到「当前」seq，
                    # 不能把游标改成 0——那会把服务里保留的历史消息整批重放进
                    # AstrBot 队列，看起来就是消息大量堆积、机器人回复隔夜消息。
                    await self._align_cursor()
                    logger.warning("[wechat_bridge] 检测到 io-wechat 重启，已对齐游标，跳过历史消息")
                    continue
                for item in data.get("messages") or []:
                    self._seq = max(self._seq, int(item.get("seq") or 0))
                    await self._handle(item)
                self._seq = max(self._seq, seq)
                warned = False
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                if not warned:
                    logger.warning(f"[wechat_bridge] 轮询失败，稍后重试: {exc}")
                    warned = True
                await asyncio.sleep(1)

    def _accept(self, item: dict) -> tuple[bool, str, str]:
        """返回 (是否处理, chat_uid, text)。过滤规则集中在这里。"""
        if item.get("is_self"):
            # Owner self-@ test: the bot runs on the owner's WeChat, so
            # the owner's own messages are is_self. Allow them through
            # ONLY when the owner @-mentions the bot (test/trigger).
            # Every other self-sent message is dropped to avoid loops.
            _t = str(item.get("content") or "")
            _u = str(item.get("sender_uid") or "").strip()
            if not (_u and _u == self._root_user_wxid() and "@\u5927\u80a5\u9c7c" in _t):
                return False, "", ""
        # 丢弃积压太久的消息：模型排队或服务重启时，队列里可能压着几分钟前的语气，
        # 再回复只会接错话。默认 120s，可在插件配置里调 max_message_age_seconds。
        max_age = int(self.settings.get("max_message_age_seconds") or 120)
        ts = int(item.get("ts") or 0)
        if ts and max_age > 0 and time.time() - ts > max_age:
            logger.warning(
                f"[wechat_bridge] 丢弃过期消息 {time.time() - ts:.0f}s "
                f"{item.get('sender') or ''}@{item.get('chat') or ''}"
            )
            return False, "", ""
        # io-wechat 的 chat 是显示名（会重名/会改），chat_uid 才是 wxid/群 id。
        # 回复必须按 uid 发，显示名只用来比较 allow_chats 和打日志。
        chat_uid = str(item.get("chat_uid") or "").strip()
        chat_name = str(item.get("chat") or "").strip()
        sender = str(item.get("sender") or "").strip()
        sender_uid = str(item.get("sender_uid") or "").strip()
        text = str(item.get("content") or "").strip()
        is_group = str(item.get("chat_type") or "") == "group"
        # 群消息的回复目标只能是群 id。缺群 id 时如果退回 sender_uid（群成员的
        # wxid），回复就会被当成私聊发给那个人，看起来就是「群里问、私聊答」。
        if is_group and not chat_uid.endswith("@chatroom"):
            return False, "", ""
        # Hard whitelist: only the two groups, regardless of settings.
        if is_group and chat_uid not in ALLOWED_GROUP_UIDS:
            return False, "", ""
        chat_uid = chat_uid or chat_name or sender_uid or sender
        if not chat_uid or not (sender or sender_uid) or not text:
            return False, "", ""
        # 表情/图片/链接等 appmsg 是 XML，直接跳过，别和别的机器人抢答
        if text.startswith("<msg") or "<appmsg" in text:
            return False, "", ""
        if is_group and not self.settings.get("enable_group", False):
            return False, "", ""
        allowed = [str(x).strip() for x in (self.settings.get("allow_chats") or [])
                   if str(x).strip()]
        is_gf_dm = (not is_group) and (
            chat_uid in self._girlfriend_list()
            or sender_uid in self._girlfriend_list()
        )
        if allowed and chat_uid not in allowed and chat_name not in allowed and not is_gf_dm:
            return False, "", ""

        key = f"{chat_uid}\x1f{item.get('id') or item.get('seq')}\x1f{text[:24]}"
        if key in self._seen:
            return False, "", ""
        self._seen.append(key)

        max_chars = int(self.settings.get("max_chars") or 400)
        if len(text) > max_chars:
            text = text[:max_chars] + "…"
        return True, chat_uid, text

    def _note_bot_send(self, chat: str) -> None:
        """Record one of the bot's own sends in the 5-min window.

        io-wechat /messages is incoming-only (verified 2026-11-17: 0
        is_self in 2000 feed entries), so _record_recent can never see
        the bot's own messages. Without this, recent_self_ratio is always
        0 and seconds_since_bot_message always defaults to 3600, and the
        logistic gate loses its strongest signal. Called from every send
        site (_emit + direct client.send_text callers).
        """
        if not chat or chat not in ALLOWED_GROUP_UIDS:
            return
        now = time.time()
        dq = self._recent_msgs.get(chat)
        if dq is None:
            dq = deque(maxlen=200)
            self._recent_msgs[chat] = dq
        while dq and now - dq[0][0] > 300:
            dq.popleft()
        dq.append((now, True))
        self._last_bot_ts[chat] = now

    def _record_recent(self, item: dict) -> None:
        """Track the last-5-min message window per whitelisted group chat.

        Feeds the logistic gate (recent_self_ratio, msg_count,
        seconds_since_bot_message). Runs before _accept filtering so the
        bot's own messages (is_self) are counted too.
        """
        if str(item.get("chat_type") or "") != "group":
            return
        chat = str(item.get("chat_uid") or "").strip()
        if chat not in ALLOWED_GROUP_UIDS:
            return
        ts = float(item.get("ts") or time.time())
        dq = self._recent_msgs.get(chat)
        if dq is None:
            dq = deque(maxlen=200)
            self._recent_msgs[chat] = dq
        while dq and ts - dq[0][0] > 300:
            dq.popleft()
        dq.append((ts, bool(item.get("is_self"))))
        if item.get("is_self"):
            self._last_bot_ts[chat] = ts

    async def _handle(self, item: dict) -> None:
        # MaiBot P1: record the message in the 5-min window first.
        self._record_recent(item)
        # Condition-triggered welcome: a group-join system message arrives
        # in this same feed. Intercept it here so the welcome fires ~1s
        # after the join instead of waiting for a timed poll.
        if await self._maybe_welcome(item):
            return
        # Root control: the user's secret-code commands. Intercepted before
        # the LLM/debounce so they run immediately and never reach DeepSeek.
        if await self._maybe_root_command(item):
            return
        ok, chat, text = self._accept(item)
        if not ok:
            return
        # Use the (possibly truncated) accepted text for the event.
        item = dict(item)
        item["content"] = text
        # Sticker/image in a group: sometimes answer with a stolen sticker
        # instead of a text reply (MaiBot-style). Sent = skip the LLM reply.
        if await self._maybe_sticker_reply(item, chat):
            return
        # Vision (2026-11-17): a [图片]/[表情] that survived the sticker
        # reply gets a one-sentence description when a real image URL is
        # available, so DeepSeek can react to what it actually sees.
        # Any failure falls back silently to the plain tag.
        item = await self._maybe_caption(item)
        # Girlfriend DM: reply to every message immediately, no debounce
        # and no proactive gating.
        if self._is_girlfriend_dm(item):
            self._emit(item, chat, proactive=False)
            return
        has_mention = any(
            isinstance(m, dict) and m.get("is_bot")
            for m in (item.get("mentions") or [])
        )
        if not has_mention:
            # Fallback: io-wechat sometimes leaves `mentions` empty even
            # when the text contains @<bot>. Treat a literal @-name as a
            # mention so an @ is never silently gated.
            if "@大肥鱼" in str(item.get("content") or ""):
                has_mention = True
        if has_mention:
            # Web search (2026-11-17): @ + question -> Bing first, results
            # go into the prompt so the answer is grounded, not guessed.
            item = await self._maybe_websearch(item)
            # @ the bot: reply fast (~1s), don't batch. If the same person
            # has a pending batch, fold the @ into it and flush it all now
            # (one reply, not two). Otherwise flush any other pending batch
            # for this chat first, then emit the @ immediately.
            sender = str(item.get("sender") or item.get("sender_uid") or "")
            batch = self._batches.get(chat)
            if batch is not None and batch["sender"] == sender:
                batch["items"].append(item)
                batch["last_ts"] = time.time()
                batch["has_mention"] = True
                await self._flush_batch(chat)
            else:
                await self._flush_batch(chat)
                self._emit(item, chat, proactive=False)
            return
        # No @: batch it (debounce). The proactive decision happens once
        # per batch at flush time, not once per message.
        await self._enqueue_batch(chat, item)

    # ---- item 1: message debounce ----
    async def _enqueue_batch(self, chat: str, item: dict) -> None:
        if not self._debounce_enabled():
            self._emit(item, chat, proactive=False)
            return
        now = time.time()
        sender = str(item.get("sender") or item.get("sender_uid") or "")
        batch = self._batches.get(chat)
        if batch is not None and batch["sender"] != sender:
            # Different person jumped in: flush the old batch first so two
            # people's messages never merge into one event.
            await self._flush_batch(chat)
            batch = None
        if batch is None:
            # 4-tier debounce (MaiBot P1-6): batch started >30s after the
            # previous batch ended -> quiet mode (short wait); otherwise
            # busy mode (longer wait). 迈阿醚 only; 广外 keeps quiet wait.
            gap = now - self._last_batch_end.get(chat, 0.0)
            mode = "quiet" if (chat != GROUP_MAI or gap > 30) else "busy"
            self._batches[chat] = {
                "sender": sender,
                "items": [item],
                "first_ts": now,
                "last_ts": now,
                "has_mention": False,
                "mode": mode,
            }
        else:
            batch["items"].append(item)
            # Pure emoji/filler only shortens, never extends a batch
            # (MaiBot: 纯表情只能缩短、不能延长批次）. 迈阿醚 only.
            if chat != GROUP_MAI or not self._is_bare_emoji(
                    str(item.get("content") or "")):
                batch["last_ts"] = now
        # Busy cap: batch open >= busy_s and a new message arrived -> flush
        # now instead of extending (a busy chat gets ~5s batches, not one
        # long batch swallowing the conversation).
        batch = self._batches.get(chat)
        if batch and now - batch["first_ts"] >= self._debounce_busy_s():
            await self._flush_batch(chat)

    @staticmethod
    def _is_bare_emoji(text: str) -> bool:
        """True for pure-emoji / pure-punctuation / sticker-tag messages."""
        t = (text or "").strip()
        if t in ("[表情]", "[图片]"):
            return True
        substance = re.sub(r"[^\w]", "", re.sub(r"\s+", "", t))
        return len(substance) <= 1

    async def _flusher_loop(self) -> None:
        """Background flusher: emit a batch after its wait, or 8s hard cap."""
        while True:
            try:
                now = time.time()
                quiet_s = self._debounce_quiet_s()
                busy_s = self._debounce_busy_s()
                cap_s = self._debounce_cap_s()
                for chat in list(self._batches.keys()):
                    batch = self._batches.get(chat)
                    if not batch:
                        continue
                    wait_s = (
                        quiet_s if batch.get("mode", "quiet") == "quiet"
                        else busy_s
                    )
                    # Pure-image/sticker batches wait at least 4s
                    # (MaiBot BARE_IMAGE_MIN_WAIT). 迈阿醚 only.
                    if chat == GROUP_MAI and all(
                        self._is_bare_emoji(str(it.get("content") or ""))
                        for it in batch["items"]
                    ):
                        wait_s = max(wait_s, 4.0)
                    if (now - batch["last_ts"] >= wait_s
                            or now - batch["first_ts"] >= cap_s):
                        await self._flush_batch(chat)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[wechat_bridge] 防抖刷新失败: {exc}")
            await asyncio.sleep(0.3)

    def _merge_items(self, items: list[dict]) -> dict:
        """Combine a batch of same-sender messages into one item for build_event."""
        last = dict(items[-1])
        texts = [str(it.get("content") or "").strip() for it in items]
        texts = [t for t in texts if t]
        last["content"] = "\n".join(texts)
        # Merge bot mentions from all items so an @ anywhere wakes the bot.
        mentions: list[dict] = []
        seen_m = set()
        for it in items:
            for m in (it.get("mentions") or []):
                if isinstance(m, dict):
                    key = (m.get("name"), bool(m.get("is_bot")))
                    if key not in seen_m:
                        seen_m.add(key)
                        mentions.append(m)
        last["mentions"] = mentions
        return last

    async def _flush_batch(self, chat: str) -> None:
        batch = self._batches.pop(chat, None)
        if not batch or not batch["items"]:
            return
        self._last_batch_end[chat] = time.time()
        merged = self._merge_items(batch["items"])
        proactive = False
        if not batch.get("has_mention") and str(merged.get("chat_type") or "") == "group":
            # One proactive roll per batch, using the combined text.
            allowed, reason = self._proactive_allowed(
                chat, merged, pending=len(batch["items"]))
            proactive = allowed
            if not allowed and reason in ("cooldown", "throttle", "budget"):
                await self._maybe_busy_reply(chat)
        self._emit(merged, chat, proactive=proactive)

    async def _maybe_busy_reply(self, chat: str) -> None:
        """P1-5: when throttled, occasionally say the server is busy
        instead of staying silent. 迈阿醚 only, 1/5 chance, 10-min cooldown.
        Never for @-mentions (those always get a real reply)."""
        if chat != GROUP_MAI:
            return
        now = time.time()
        if now - self._last_busy.get(chat, 0.0) < BUSY_COOLDOWN_S:
            return
        if random.random() >= BUSY_CHANCE:
            return
        line = random.choice(BUSY_LINES)
        ok = False
        try:
            ok = await self.client.send_text(chat, line)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[wechat_bridge] 忙线回复发送异常: {exc}")
        if ok:
            self._last_busy[chat] = now
            self._note_bot_send(chat)
            logger.info(f"[wechat_bridge] 忙线回复 -> {chat}: {line}")

    def _emit(self, item: dict, chat: str, proactive: bool = False) -> None:
        if self._is_girlfriend_dm(item):
            item = dict(item)
            item["content"] = GF_PERSONA + "\n" + str(item.get("content") or "")
        elif str(item.get("chat_type") or "") == "group":
            if chat == GROUP_MAI or str(item.get("chat") or "") == GROUP_MAI:
                item = dict(item)
                refs = []
                # P2-3 person card (sender) + P2-4/P3-1-lite summary (group).
                pref = self._person_ref(chat, str(item.get("sender_uid") or ""))
                if pref:
                    refs.append(pref)
                sref = self._summary_ref(chat, str(item.get("content") or ""))
                if sref:
                    refs.append(sref)
                prefix = MAI_MIAO_PERSONA
                if refs:
                    prefix += "\n" + "\n".join(refs)
                item["content"] = prefix + "\n" + str(item.get("content") or "")
            self._last_bot_reply[chat] = time.time()
        self._note_bot_send(chat)
        self.context.get_event_queue().put_nowait(
            build_event(item, self.client, chat, proactive=proactive)
        )
        n = len(str(item.get("content") or "").splitlines())
        logger.info(
            f"[wechat_bridge] 收到 {item.get('sender_name') or item.get('sender')}"
            f"@{item.get('chat') or chat}({chat}): {str(item.get('content') or '')[:40]}"
            + (f" [批量×{n}条]" if n > 1 else "")
            + (" [主动]" if proactive else "")
        )

    def _pick_sticker(self) -> str | None:
        """Pick a random sticker file from the stolen library (or None)."""
        try:
            names = [
                n for n in os.listdir(STICKER_DIR)
                if os.path.splitext(n)[1].lower() in STICKER_EXTS
            ]
        except OSError:
            return None
        if not names:
            return None
        return os.path.join(STICKER_DIR, random.choice(names))

    async def _maybe_sticker_reply(self, item: dict, chat: str) -> bool:
        """MaiBot-style: answer a sticker/image with a stolen sticker.
        Only for group messages whose content is exactly [表情] or [图片].
        Returns True when a sticker was sent (caller should skip the LLM
        reply so the bot doesn't double-reply)."""
        text = str(item.get("content") or "").strip()
        if text not in ("[表情]", "[图片]"):
            return False
        if str(item.get("chat_type") or "") != "group":
            return False
        if chat not in ALLOWED_GROUP_UIDS:
            return False
        if random.random() >= STICKER_REPLY_CHANCE:
            return False
        path = self._pick_sticker()
        if not path:
            return False
        ok = await self.client.send_image(chat, path)
        if ok:
            self._note_bot_send(chat)
            logger.info(f"[wechat_bridge] 表情包回敬 -> {chat}: {os.path.basename(path)}")
        return ok

    # ---- vision captioning (2026-11-17) ----
    def _caption_url(self, item: dict) -> str:
        """Real downloadable image URL for this message, or ''.

        io-wechat's parse_content puts a real CDN URL in media for
        [表情] (stickers) but only a hex CDN token for regular [图片]
        photos -- the token is not a URL and the photo bytes are not
        reachable (WeChat 4.x local cache files are unreadable from
        outside the WeChat process and unmapped to messages).
        """
        for m in (item.get("media") or []):
            if not isinstance(m, dict):
                continue
            url = str(m.get("url") or "").strip()
            if url.startswith("http://") or url.startswith("https://"):
                return url
        return ""

    def _caption_sync(self, url: str | None) -> str | None:
        """Blocking vision call (runs in a worker thread). None on any failure.

        Args:
            url: Downloadable image URL, or None to screenshot the WeChat
                window instead (regular [图片] photos have no URL).
        """
        try:
            env = dict(os.environ)
            env["PYTHONUTF8"] = "1"
            cmd = [sys.executable, CAPTION_SCRIPT]
            cmd.append("--screenshot" if url is None else url)
            proc = subprocess.run(
                cmd,
                capture_output=True, timeout=CAPTION_TIMEOUT_S, env=env,
            )
        except Exception as exc:  # noqa: BLE001 (timeout etc.)
            logger.warning(f"[wechat_bridge] 图片描述调用失败: {exc}")
            return None
        out = (proc.stdout or b"").decode("utf-8", errors="replace").strip()
        if proc.returncode != 0 or not out:
            err = (proc.stderr or b"").decode("utf-8", errors="replace")[:100]
            logger.warning(f"[wechat_bridge] 图片描述失败 rc={proc.returncode}: {err}")
            return None
        if out.startswith("VISION_"):
            # No DashScope key / other script-side refusal: silent, keep tag.
            return None
        return out[:120]

    async def _maybe_caption(self, item: dict) -> dict:
        """Replace a bare [图片]/[表情] tag with a captioned one, when possible.

        Groups only (DMs left alone). Never raises, never passes an error
        string to the LLM. Regular [图片] photos have no downloadable URL,
        so they fall back to screenshotting the WeChat window; if both
        vision paths fail, a [图片] is marked as unseen so the LLM does
        not invent its contents.
        """
        text = str(item.get("content") or "").strip()
        if text not in ("[图片]", "[表情]"):
            return item
        if str(item.get("chat_type") or "") != "group":
            return item
        url = self._caption_url(item)
        desc = None
        if url:
            try:
                desc = await asyncio.to_thread(self._caption_sync, url)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[wechat_bridge] caption failed: {exc}")
        if not desc:
            # Screenshot fallback: the image is visible in the WeChat window.
            try:
                desc = await asyncio.to_thread(self._caption_sync, None)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[wechat_bridge] screenshot caption failed: {exc}")
        if not desc:
            if text == "[图片]":
                item = dict(item)
                item["content"] = "[图片：我看不到这张图的内容，不要猜测图片里有什么]"
                logger.info("[wechat_bridge] image unseen, marked as such")
            return item
        label = text.strip("[]")
        item = dict(item)
        item["content"] = f"[{label}：{desc}]"
        logger.info(f"[wechat_bridge] 图片描述: {item['content'][:80]}")
        return item

    async def _maybe_websearch(self, item: dict) -> dict:
        """@-question -> Bing search, inject results into the LLM prompt.

        Only for @-mentions that look like questions. Never raises; any
        failure returns the item unchanged (the bot just answers from its
        own knowledge). Kill switch: C:\\dafeiyu-open\\websearch_disabled.
        """
        if os.path.exists(WEBSEARCH_FLAG):
            return item
        text = str(item.get("content") or "")
        if not _websearch_looks_like_question(text):
            return item
        query = re.sub(r"@\S+", "", text)
        query = re.sub(r"\s+", " ", query).strip()
        if len(query) < 2:
            return item
        try:
            results = await asyncio.to_thread(_bing_search_sync, query)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[wechat_bridge] 联网搜索异常: {exc}")
            return item
        item = dict(item)
        if not results:
            item["content"] = (
                text + "\n[联网搜索：没有找到相关结果，请老实说搜不到，不要编答案。]"
            )
            logger.info(f"[wechat_bridge] 联网搜索无结果: {query[:40]}")
            return item
        lines = ["[联网搜索结果（Bing）：]"]
        for i, r in enumerate(results[:WEBSEARCH_MAX_RESULTS], 1):
            domain = urllib.parse.urlparse(r.get("url") or "").netloc or "bing"
            lines.append(f"{i}. {r['title']} — {r['snippet']}（来源：{domain}）")
        lines.append("请根据以上搜索结果简短回答；如果结果里没有答案，就说不知道，不要编造。")
        item["content"] = text + "\n" + "\n".join(lines)
        logger.info(f"[wechat_bridge] 联网搜索: {query[:40]} -> {len(results)} 条")
        return item

    # ---- item 2: content-scored trigger gating ----
    def _content_score(self, text: str) -> float:
        """Proactive reply probability for a message's content (0.0-1.0).

        Watery content (laughs / acks / pure emoji / single char) is near zero;
        questions and longer messages score higher. Base = proactive_chance.
        """
        base = float(self.settings.get("proactive_chance") or 0.35)
        watery = float(self.settings.get("proactive_watery_chance") or 0.02)
        short = float(self.settings.get("proactive_short_chance") or 0.10)
        question = float(self.settings.get("proactive_question_chance") or 0.5)
        t = (text or "").strip()
        if not t:
            return 0.0
        compact = re.sub(r"\s+", "", t)
        if len(compact) <= 1:
            return watery
        if compact in FILLERS:
            return watery
        # Only emoji/punctuation left after removing word chars -> watery.
        substance = re.sub(r"[^\w]", "", compact)
        if not substance or len(substance) <= 1:
            return watery
        if "?" in t or "？" in t:
            return question
        if len(compact) <= 3:
            return short
        if len(compact) >= 20:
            return max(base, 0.85)
        return base

    # ---- MaiBot P1-1: logistic reply probability ----
    def _reply_probability(self, item: dict, chat: str) -> tuple[float, dict]:
        """MaiBot-style reply likelihood (reply_likelihood.py).

        8 features, weights fitted by MaiBot offline. Returns (p, features)
        so the decision log records what drove the call.
        """
        text = str(item.get("content") or "")
        mentions = item.get("mentions") or []
        mention_bot = 1 if any(
            isinstance(m, dict) and m.get("is_bot") for m in mentions
        ) else 0
        at_other = 1 if any(
            isinstance(m, dict) and not m.get("is_bot") for m in mentions
        ) else 0
        has_q = 1 if ("?" in text or "？" in text) else 0
        compact = re.sub(r"\s+", "", text.strip())
        substance = re.sub(r"[^\w]", "", compact)
        placeholder = 1 if (
            text.strip() in ("[表情]", "[图片]") or not substance
        ) else 0
        # Recent 5-min window features.
        now = time.time()
        dq = self._recent_msgs.get(chat) or deque()
        while dq and now - dq[0][0] > 300:
            dq.popleft()
        total = len(dq)
        bot_n = sum(1 for _, b in dq if b)
        self_ratio = (bot_n / total) if total else 0.0
        last_bot = self._last_bot_ts.get(chat)
        sec_since = (now - last_bot) if last_bot else 3600.0
        pending = int(item.get("_batch_n") or 1)
        w = LOGIT_W
        logit = (
            w["intercept"]
            + w["mention_bot"] * mention_bot
            + w["at_other"] * at_other
            + w["question"] * has_q
            + w["placeholder"] * placeholder
            + w["self_ratio"] * min(1.0, self_ratio)
            + w["msg_count"] * total
            + w["sec_since_bot"] * math.log1p(sec_since)
            + w["pending"] * min(20, pending)
        )
        p = 1.0 / (1.0 + math.exp(-logit))
        # WeChat-local adjustment (not in MaiBot's fitted weights): filler
        # and very short messages get almost no reply -- the old fixed
        # table gave them 0.02/0.10, and "水消息几乎不回" is a standing
        # user rule. Without this, "哈哈" scores a normal ~0.35.
        compact = re.sub(r"\s+", "", text.strip())
        substance = re.sub(r"[^\w]", "", compact)
        if (not compact or len(compact) <= 1 or compact in FILLERS
                or not substance or len(substance) <= 1):
            p *= 0.1
        elif len(compact) <= 3:
            p *= 0.3
        feats = {
            "mention_bot": mention_bot, "at_other": at_other,
            "question": has_q, "placeholder": placeholder,
            "self_ratio": round(self_ratio, 2), "msg_count": total,
            "sec_since_bot": round(sec_since), "pending": pending,
        }
        return p, feats

    # ---- MaiBot P1-2: dynamic gate (1h window, tightens only) ----
    def _gate_state_load(self) -> dict:
        try:
            with open(GATE_STATE_PATH, encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}

    def _gate_state_save(self) -> None:
        try:
            with open(GATE_STATE_PATH, "w", encoding="utf-8") as f:
                json.dump(self._gate_state, f, ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[wechat_bridge] 保存门控状态失败: {exc}")

    def _gate_prune(self, chat: str, now: float) -> dict:
        st = self._gate_state.get(chat)
        if not isinstance(st, dict):
            st = {"probs": [], "replies": []}
            self._gate_state[chat] = st
        st["probs"] = [[t, p] for t, p in st.get("probs", [])
                       if now - t <= 3600]
        st["replies"] = [t for t in st.get("replies", []) if now - t <= 3600]
        return st

    def _gate_threshold(self, chat: str, now: float) -> tuple[float, float]:
        """(effective_threshold, keep_ratio) for this chat right now.

        target = frequency x sum(p over evaluated rounds in the 1h window);
        keep_ratio = min(1, target / actual replies) -- feedback only
        tightens, never loosens. Fewer than 5 evaluated rounds -> base
        threshold (MaiBot's small-sample static fallback).
        """
        base = float(self.settings.get("reply_threshold")
                     or ROOT_FREQ_THRESHOLDS.get("正常", 0.35))
        st = self._gate_prune(chat, now)
        probs = st["probs"]
        if len(probs) < 5:
            return base, 1.0
        freq = float(self.settings.get("proactive_chance") or 0.35)
        expected = freq * sum(p for _, p in probs)
        actual = len(st["replies"])
        if actual <= 0 or expected <= 0:
            return base, 1.0
        keep = min(1.0, expected / actual)
        eff = min(0.95, base + (1.0 - keep) * 0.4)
        return eff, keep

    def _gate_log(self, rec: dict) -> None:
        """Decision log (shadow comparison + P3-2-lite observability)."""
        try:
            rec["ts"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
            with open(GATE_DECISIONS_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def _proactive_allowed_legacy(self, chat: str, item: dict) -> bool:
        """The pre-MaiBot fixed-table gate (unchanged). Used for 广外 and
        when the P1 kill-switch flag exists."""
        now = time.time()
        cooldown = float(self.settings.get("proactive_cooldown_seconds") or 180)
        last = self._last_proactive.get(chat, 0.0)
        if now - last < cooldown:
            return False
        window = 600.0
        times = self._proactive_times.get(chat)
        if times is None:
            times = deque(maxlen=20)
            self._proactive_times[chat] = times
        while times and now - times[0] > window:
            times.popleft()
        max_per = int(self.settings.get("proactive_max_per_10min") or 3)
        if len(times) >= max_per + 2:
            return False
        p = self._content_score(str(item.get("content") or ""))
        if len(times) >= max_per:
            p *= 0.25
        if random.random() >= p:
            return False
        self._last_proactive[chat] = now
        times.append(now)
        return True

    def _proactive_allowed(self, chat: str, item: dict, pending: int = 1) -> tuple[bool, str]:
        """Proactive gate (MaiBot P1-1 + P1-2). One roll per batch.

        迈阿醚 (and only 迈阿醚): logistic probability + dynamic 1h budget.
        广外 or kill-switch flag -> the old fixed-table behaviour.
        Returns (allowed, reason): reason in ok/cooldown/throttle/
        low_p/below_threshold/budget/legacy.
        """
        # Old path: 广外, or P1 kill switch.
        if chat != GROUP_MAI or os.path.exists(MAIBOT_P1_FLAG):
            return self._proactive_allowed_legacy(chat, item), "legacy"
        now = time.time()
        cooldown = float(self.settings.get("proactive_cooldown_seconds") or 180)
        last = self._last_proactive.get(chat, 0.0)
        if now - last < cooldown:
            self._gate_log({"chat": chat, "p": None, "allowed": False,
                            "reason": "cooldown"})
            return False, "cooldown"
        window = 600.0
        times = self._proactive_times.get(chat)
        if times is None:
            times = deque(maxlen=20)
            self._proactive_times[chat] = times
        while times and now - times[0] > window:
            times.popleft()
        max_per = int(self.settings.get("proactive_max_per_10min") or 3)
        if len(times) >= max_per + 2:
            self._gate_log({"chat": chat, "p": None, "allowed": False,
                            "reason": "throttle"})
            return False, "throttle"
        item = dict(item)
        item["_batch_n"] = pending
        p, feats = self._reply_probability(item, chat)
        old_p = self._content_score(str(item.get("content") or ""))
        threshold, keep = self._gate_threshold(chat, now)
        st = self._gate_prune(chat, now)
        # Hard hourly budget cap (safety net on top of the soft threshold).
        freq = float(self.settings.get("proactive_chance") or 0.35)
        if len(st["replies"]) >= max(3, int(freq * 30)):
            self._gate_log({"chat": chat, "p": round(p, 3),
                            "allowed": False, "reason": "budget"})
            return False, "budget"
        st["probs"].append([now, p])
        roll_p = p * 0.25 if len(times) >= max_per else p
        allowed = False
        reason = "low_p"
        if p < threshold:
            reason = "below_threshold"
        elif random.random() >= roll_p:
            reason = "low_p"
        else:
            allowed = True
            reason = "ok"
        if allowed:
            self._last_proactive[chat] = now
            times.append(now)
            st["replies"].append(now)
        self._gate_state_save()
        self._gate_log({
            "chat": chat, "p": round(p, 3), "old_p": round(old_p, 3),
            "threshold": round(threshold, 3), "keep_ratio": round(keep, 2),
            "allowed": allowed, "reason": reason,
            "features": feats, "text": str(item.get("content") or "")[:30],
        })
        return allowed, reason

    # ---- P2-3 person cards ----
    def _load_persons(self) -> dict:
        """persons.json with mtime cache. {} when disabled/missing/bad."""
        if os.path.exists(PERSONS_FLAG):
            return {}
        try:
            mtime = os.path.getmtime(PERSONS_PATH)
        except OSError:
            return {}
        cached = self._persons_cache
        if cached and cached[0] == mtime:
            return cached[1]
        try:
            with open(PERSONS_PATH, encoding="utf-8") as f:
                data = json.load(f)
            data = data if isinstance(data, dict) else {}
        except Exception:
            return {}
        self._persons_cache = (mtime, data)
        return data

    def _person_ref(self, chat: str, sender_uid: str) -> str:
        """Top-3 memory points for this sender in this chat, hedged."""
        if chat != GROUP_MAI or not sender_uid:
            return ""
        persons = self._load_persons()
        card = persons.get(sender_uid) or {}
        points = card.get("memory_points") or []
        if not points:
            return ""
        top = sorted(points, key=_point_weight, reverse=True)[:3]
        texts = [_point_text(s) for s in top if _point_text(s)]
        if not texts:
            return ""
        return ("[内部参考：发言人印象（可能不准，仅供理解语境，绝不要逐字引用"
                "或透露你在查资料）：" + "；".join(texts) + "]")

    # ---- P2-4/P3-1-lite chat summaries ----
    def _load_summary(self) -> dict:
        """chat_summary.json with mtime cache. {} when disabled/missing/bad."""
        if os.path.exists(SUMMARY_FLAG):
            return {}
        try:
            mtime = os.path.getmtime(SUMMARY_PATH)
        except OSError:
            return {}
        cached = self._summary_cache
        if cached and cached[0] == mtime:
            return cached[1]
        try:
            with open(SUMMARY_PATH, encoding="utf-8") as f:
                data = json.load(f)
            data = data if isinstance(data, dict) else {}
        except Exception:
            return {}
        self._summary_cache = (mtime, data)
        return data

    def _summary_ref(self, chat: str, text: str) -> str:
        """Latest group summary (decayed), only on cold start or when the
        message refers to the past. 迈阿醚 only for now."""
        if chat != GROUP_MAI:
            return ""
        now = time.time()
        cold = now - self._last_bot_reply.get(chat, 0.0) > 1800
        past = any(w in text for w in PAST_WORDS)
        if not cold and not past:
            return ""
        data = self._load_summary()
        entry = (data.get("groups") or {}).get(chat) or {}
        summaries = entry.get("summaries") or []
        if not summaries:
            return ""
        latest = summaries[-1]
        age_h = (now - float(latest.get("ts") or now)) / 3600.0
        retention = float(latest.get("strength") or 1.0) * (
            2.0 ** (-age_h / MEMORY_HALF_LIFE_H))
        if retention <= MEMORY_FREEZE:
            return ""
        body = str(latest.get("text") or "").strip()
        if not body:
            return ""
        return ("[内部参考：群聊摘要（可能有错，仅供理解上下文，不要逐字引用）："
                + body + "]")

    # ---- auto-welcome (condition-triggered) ----
    def _already_welcomed(self, seq: int) -> bool:
        try:
            with open(WELCOME_STATE_PATH, encoding="utf-8") as f:
                return seq in set(json.load(f).get("welcomed", []))
        except Exception:
            return False

    def _mark_welcomed(self, seq: int) -> None:
        try:
            try:
                with open(WELCOME_STATE_PATH, encoding="utf-8") as f:
                    d = json.load(f)
            except Exception:
                d = {}
            welcomed = set(d.get("welcomed", []))
            welcomed.add(seq)
            d["welcomed"] = sorted(welcomed)[-200:]
            if not d.get("last_seq"):
                d["last_seq"] = seq
            with open(WELCOME_STATE_PATH, "w", encoding="utf-8") as f:
                json.dump(d, f)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[wechat_bridge] 记录欢迎状态失败: {exc}")

    async def _maybe_welcome(self, item: dict) -> bool:
        """Return True if this item was a group-join notice we handled."""
        content = str(item.get("content") or "")
        if "joined the group chat" not in content and "invited" not in content:
            return False
        chat_uid = str(item.get("chat_uid") or "").strip()
        if not chat_uid.endswith("@chatroom"):
            return False
        allowed = [str(x).strip() for x in (self.settings.get("allow_chats") or [])
                   if str(x).strip()]
        if allowed and chat_uid not in allowed:
            return False
        m = re.search(r'name="adder".*?<nickname><!\[CDATA\[(.*?)\]\]>', content, re.DOTALL)
        if not m:
            return False
        nick = m.group(1).strip()
        if not nick:
            return False
        seq = int(item.get("seq") or 0)
        if seq and self._already_welcomed(seq):
            return True
        intro = WELCOME_INTRO.get(chat_uid, "")
        lines = ["欢迎新人进群 👏", "", f"{nick}，来了就别走了嗷"]
        if intro:
            lines.append(intro)
        lines += ["我是大肥鱼🐟，群里的 AI，有事直接 @我", "别客气，来了就是自己人"]
        text = "\n".join(lines)
        ok = False
        try:
            ok = await self.client.send_text(chat_uid, text, keep_newlines=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[wechat_bridge] 欢迎发送异常: {exc}")
        if ok:
            self._note_bot_send(chat_uid)
        if ok and seq:
            self._mark_welcomed(seq)
        logger.info(f"[wechat_bridge] 新人欢迎 @{chat_uid}: {nick} ok={ok}")
        return True

    # ---- root control (secret-code commands in the group) ----
    def _root_code(self) -> str:
        return str(self.settings.get("root_code") or ROOT_DEFAULT_CODE).strip()

    def _root_user_wxid(self) -> str:
        return str(self.settings.get("root_user_wxid") or "").strip()

    def _root_config_path(self) -> str:
        here = os.path.dirname(os.path.abspath(__file__))
        return os.path.join(os.path.dirname(here), "config.json")

    def _root_audit(self, wxid: str, cmd: str, result: str) -> None:
        try:
            ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
            with open(ROOT_AUDIT_PATH, "a", encoding="utf-8") as f:
                f.write(f"[{ts}] wxid={wxid} cmd={cmd!r} result={result}\n")
        except Exception:
            pass

    def _root_log_request(self, cmd: str) -> None:
        try:
            rec = {"ts": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
                   "cmd": cmd}
            with open(ROOT_REQUESTS_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            pass

    async def _root_send(self, chat_uid: str, text: str) -> bool:
        try:
            ok = bool(await self.client.send_text(chat_uid, text, keep_newlines=True))
            if ok:
                self._note_bot_send(chat_uid)
            return ok
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[wechat_bridge] root 回复发送异常: {exc}")
            return False

    def _root_help_text(self) -> str:
        # NOTE: the secret code itself is never printed in a reply.
        return "\n".join([
            "🔐 大肥鱼 root 命令（仅主人可用）",
            "状态 — 查看运行状态",
            "查询白名单 — 查看机器人在哪些群回话",
            "安静 / 正常 / 活跃 — 调回话频率",
            "忘掉 + 名字 — 删掉某人的人物卡",
            "早安开 / 早安关 — 开关早安问候",
            "重启 — 重启 AstrBot",
            "查余额 — DeepSeek + Simular Box 余额",
            "帮助 — 显示本列表",
            "用法：口令 + 空格 + 命令",
        ])

    def _root_last_reply(self) -> str:
        try:
            day = time.strftime("%Y-%m-%d", time.localtime())
            path = rf"C:\dafeiyu-open\sent_log\sent_{day}.jsonl"
            if not os.path.exists(path):
                return "今天暂无发送记录"
            last = ""
            with open(path, encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        last = line.strip()
            if not last:
                return "今天暂无发送记录"
            ts = json.loads(last).get("ts")
            if isinstance(ts, (int, float)):
                return time.strftime("%H:%M", time.localtime(ts))
            if isinstance(ts, str) and ts:
                if "T" in ts:
                    return ts.split("T", 1)[1][:5]
                if " " in ts:
                    return ts.split(" ", 1)[1][:5]
                return ts[:8]
            return "未知"
        except Exception:
            return "未知"

    def _root_whitelist_text(self) -> str:
        names = {
            "45270768515@chatroom": "广外AI共创｜腾讯云AI交流圈",
            "54306443862@chatroom": "迈阿醚TML后台",
        }
        allowed = [str(x).strip() for x in (self.settings.get("allow_chats") or [])
                   if str(x).strip()]
        if not allowed:
            return "📋 白名单为空，机器人不在任何群回话"
        lines = ["📋 白名单（机器人在这些群回话）"]
        for uid in allowed:
            lines.append(f"· {names.get(uid, uid)}")
        return "\n".join(lines)

    def _root_balance_text(self) -> str:
        """查余额：DeepSeek API 余额 + Simular Box 余额。Keys 只从本机文件
        读取，绝不记日志、不打印、不回显。"""
        lines = ["\U0001f4b0 余额查询"]

        # --- DeepSeek: key 在 AstrBot cmd_config.json 的 deepseek_source.key ---
        ds_line = "DeepSeek：查询失败"
        try:
            key = ""
            cfg_path = r"C:\dafeiyu-open\astrbot-win\AstrBot-4.29.3\data\cmd_config.json"
            with open(cfg_path, encoding="utf-8") as f:
                cfg = json.load(f)
            for s in cfg.get("provider_sources") or []:
                if s.get("id") == "deepseek_source":
                    kval = s.get("key") or ""
                    # AstrBot stores keys as a list (key rotation); take first.
                    if isinstance(kval, list):
                        kval = kval[0] if kval else ""
                    key = str(kval).strip()
                    break
            if not key:
                ds_line = "DeepSeek：未找到 API key"
            else:
                # The balance endpoint sometimes returns non-JSON (WAF block
                # page, empty body on a flaky campus network). A single
                # transient glitch used to surface as JSONDecodeError, so
                # retry once on parse/network errors before giving up.
                # HTTP errors (401/429/5xx) fail fast, no retry.
                d = None
                last_err: Exception | None = None
                for attempt in range(2):
                    try:
                        req = urllib.request.Request(
                            "https://api.deepseek.com/user/balance",
                            headers={
                                "Authorization": "Bearer " + key,
                                "Accept": "application/json",
                                "User-Agent": (
                                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                                    "Chrome/131.0.0.0 Safari/537.36"
                                ),
                            },
                        )
                        with urllib.request.urlopen(req, timeout=15) as r:
                            raw = r.read().decode("utf-8", "replace")
                        if not raw.strip():
                            raise ValueError("empty response body")
                        d = json.loads(raw)
                        if not isinstance(d, dict):
                            raise ValueError("unexpected JSON shape")
                        break
                    except urllib.error.HTTPError:
                        raise
                    except (
                        json.JSONDecodeError,
                        ValueError,
                        urllib.error.URLError,
                        TimeoutError,
                    ) as exc:
                        last_err = exc
                        logger.warning(
                            "[wechat_bridge] DeepSeek balance attempt %d failed: %s: %s",
                            attempt + 1,
                            type(exc).__name__,
                            exc,
                        )
                        if attempt == 0:
                            time.sleep(2)
                if d is None:
                    raise last_err if last_err is not None else ValueError("unknown error")
                infos = d.get("balance_infos") or []
                if d.get("is_available") and infos:
                    parts = []
                    for info in infos:
                        parts.append(
                            f"{info.get('total_balance', '?')} "
                            f"{info.get('currency', '')}".strip())
                    ds_line = "DeepSeek：余额 " + " / ".join(parts)
                elif d.get("is_available"):
                    ds_line = "DeepSeek：可用（余额明细为空）"
                else:
                    ds_line = "DeepSeek：不可用"
        except Exception as exc:  # noqa: BLE001
            ds_line = f"DeepSeek：查询失败（{type(exc).__name__}）"
        lines.append(ds_line)

        # --- Simular Box: key 在 C:\dafeiyu-open\api_keys.json（用户以后配）---
        box_line = "Simular Box：未配置 API key"
        try:
            key = ""
            keys_path = r"C:\dafeiyu-open\api_keys.json"
            if os.path.exists(keys_path):
                with open(keys_path, encoding="utf-8") as f:
                    kd = json.load(f)
                key = str(kd.get("simular_api_key") or "").strip()
            if key:
                # Same transient-failure hardening as the DeepSeek call above:
                # read raw text, retry once on parse/network errors, fail fast
                # on HTTP errors. The raw body is only logged, never echoed.
                d = None
                last_err: Exception | None = None
                for attempt in range(2):
                    try:
                        req = urllib.request.Request(
                            "https://api.simular.ai/v1/agents/account",
                            headers={
                                "Authorization": "Bearer " + key,
                                "Accept": "application/json",
                                "User-Agent": (
                                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                                    "Chrome/131.0.0.0 Safari/537.36"
                                ),
                            },
                        )
                        with urllib.request.urlopen(req, timeout=15) as r:
                            raw = r.read().decode("utf-8", "replace")
                        if not raw.strip():
                            raise ValueError("empty response body")
                        d = json.loads(raw)
                        if not isinstance(d, dict):
                            raise ValueError("unexpected JSON shape")
                        break
                    except urllib.error.HTTPError:
                        raise
                    except (
                        json.JSONDecodeError,
                        ValueError,
                        urllib.error.URLError,
                        TimeoutError,
                    ) as exc:
                        last_err = exc
                        logger.warning(
                            "[wechat_bridge] Simular balance attempt %d failed: %s: %s",
                            attempt + 1,
                            type(exc).__name__,
                            exc,
                        )
                        if attempt == 0:
                            time.sleep(2)
                if d is None:
                    raise last_err if last_err is not None else ValueError("unknown error")
                credits = d.get("credits") or {}
                usd = credits.get("usd")
                plan = (d.get("plan") or {}).get("title", "")
                if usd is None:
                    box_line = f"Simular Box：{plan or '已连接'}（余额未返回）"
                else:
                    box_line = f"Simular Box：${usd}（{plan or '套餐未知'}）"
            else:
                box_line = ("Simular Box：未配置 API key（Box \u201cmain\u201d，免费版；"
                            "2026-11-12 另加 $5 点数约 50 小时，有效期 90 天；"
                            "1,000 点数/天是否继续未核实）")
        except Exception as exc:  # noqa: BLE001
            box_line = f"Simular Box：查询失败（{type(exc).__name__}）"
        lines.append(box_line)
        return "\n".join(lines)

    def _root_status_text(self) -> str:
        astr = "未监听 6185"
        try:
            s = socket.create_connection(("127.0.0.1", 6185), timeout=2)
            s.close()
            astr = "运行中 (6185)"
        except Exception:
            pass
        io = "连接失败"
        try:
            with urllib.request.urlopen("http://127.0.0.1:23392/health", timeout=5) as r:
                d = json.loads(r.read().decode("utf-8", "replace"))
                if d.get("ok"):
                    io = "正常"
        except Exception:
            pass
        wx = "未知"
        try:
            r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Weixin.exe", "/NH"],
                               capture_output=True, text=True, timeout=10)
            wx = "运行中" if "Weixin.exe" in (r.stdout or "") else "未运行"
        except Exception:
            pass
        p = float(self.settings.get("proactive_chance") or 0.35)
        mode = "安静" if p <= 0.15 else ("正常" if p <= 0.45 else "活跃")
        thr = float(self.settings.get("reply_threshold")
                    or ROOT_FREQ_THRESHOLDS.get(mode, 0.35))
        return "\n".join([
            "📊 大肥鱼状态",
            f"AstrBot: {astr}",
            f"io-wechat: {io}",
            f"微信进程: {wx}",
            f"回话频率: {mode} ({p}，阈值 {thr})",
            f"上次回话: {self._root_last_reply()}",
        ])

    def _root_set_frequency(self, mode: str) -> bool:
        """Set a reply-frequency preset. Disk first (so it survives a
        restart), then in-memory (gating reads settings per call, so it
        takes effect immediately with no AstrBot restart). Writes both the
        dynamic-gate budget frequency (proactive_chance) and the logistic
        threshold (reply_threshold)."""
        if mode not in ROOT_FREQ_PRESETS:
            return False
        val = ROOT_FREQ_PRESETS[mode]
        thr = ROOT_FREQ_THRESHOLDS[mode]
        try:
            path = self._root_config_path()
            with open(path, encoding="utf-8-sig") as f:
                d = json.load(f)
            d["proactive_chance"] = val
            d["reply_threshold"] = thr
            with open(path, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False, separators=(",", ":"))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[wechat_bridge] root 保存频率配置失败: {exc}")
            return False
        self.settings["proactive_chance"] = val
        self.settings["reply_threshold"] = thr
        return True

    def _root_forget_person(self, name: str) -> tuple[bool, str]:
        """Delete a person card by nickname (迈阿醚 only) + tombstone it so
        the daily extractor never re-adds it. Usage: sudo 忘掉 purple sun"""
        if not name:
            return False, "用法：口令 + 忘掉 + 名字（比如：口令 忘掉 purple sun）"
        try:
            with open(PERSONS_PATH, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return False, "人物卡文件不存在，没什么可忘的"
        target = None
        for wxid, card in (data or {}).items():
            nick = str((card or {}).get("nickname") or "")
            if name.lower() in nick.lower() or nick.lower() in name.lower():
                target = wxid
                break
        if not target:
            return False, f"没找到叫「{name}」的人物卡"
        nick = str((data.get(target) or {}).get("nickname") or target)
        del data[target]
        try:
            with open(PERSONS_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            with open(PERSONS_FORGET_PATH, "a", encoding="utf-8") as f:
                f.write(target + "\n")
        except Exception as exc:  # noqa: BLE001
            return False, f"删除失败：{exc}"
        self._persons_cache = None
        return True, f"✅ 已忘掉 {nick}，以后不会再记他"

    def _root_set_greeting_morning(self, enabled: bool) -> bool:
        """Toggle the morning-greeting flag file. send_greeting.py checks
        this flag before sending the morning greeting."""
        try:
            if enabled:
                with open(GREETING_MORNING_FLAG, "w", encoding="utf-8") as f:
                    f.write("enabled\n")
            elif os.path.exists(GREETING_MORNING_FLAG):
                os.remove(GREETING_MORNING_FLAG)
            self._root_log_request("早安开" if enabled else "早安关")
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[wechat_bridge] root 早安开关失败: {exc}")
            return False

    def _root_restart_astrbot(self) -> None:
        """Spawn the detached restart helper. It runs after a short delay
        (so the 'restarting' reply can be sent first) and survives AstrBot
        being killed."""
        try:
            subprocess.Popen(
                ["py", "-3.12", ROOT_RESTART_SCRIPT],
                creationflags=subprocess.DETACHED_PROCESS
                | subprocess.CREATE_NEW_PROCESS_GROUP,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[wechat_bridge] root 重启启动失败: {exc}")

    async def _maybe_root_command(self, item: dict) -> bool:
        """Root control via group chat. Two-factor auth: sender wxid must
        match root_user_wxid AND the message must start with root_code.
        Returns True if this was a root command (handled). On auth failure
        stay silent and let the message fall through to normal handling
        (return False) so nothing reveals the root feature exists."""
        if item.get("is_self"):
            return False
        root_wxid = self._root_user_wxid()
        if not root_wxid:
            return False  # root not configured
        sender_uid = str(item.get("sender_uid") or "").strip()
        chat_uid = str(item.get("chat_uid") or "").strip()
        text = str(item.get("content") or "").strip()
        if not text or not sender_uid or not chat_uid.endswith("@chatroom"):
            return False
        allowed = [str(x).strip() for x in (self.settings.get("allow_chats") or [])
                   if str(x).strip()]
        if allowed and chat_uid not in allowed:
            return False
        code = self._root_code()
        if not code or not text.startswith(code):
            return False
        # The message starts with the code. Wrong sender: log it, stay
        # silent, fall through to normal handling.
        if sender_uid != root_wxid:
            self._root_audit(sender_uid, text[:60], "auth_fail_wxid")
            logger.warning(f"[wechat_bridge] root 口令被非主人使用，已忽略: {sender_uid}")
            return False
        # Auth passed. Dedup by seq so a duplicated delivery never runs a
        # root command twice (e.g. two restarts).
        seq = int(item.get("seq") or 0)
        if seq and seq in self._root_handled:
            return True
        if seq:
            self._root_handled.append(seq)
        rest = text[len(code):].strip()
        cmd = rest.split()[0] if rest.split() else ""
        # Freshness: ignore a root command older than 120s (stale replay).
        ts = int(item.get("ts") or 0)
        max_age = int(self.settings.get("max_message_age_seconds") or 120)
        if ts and max_age > 0 and time.time() - ts > max_age:
            self._root_audit(sender_uid, cmd, "stale_ignored")
            return True
        if not cmd:
            cmd = "帮助"
        if cmd == "帮助":
            self._root_audit(sender_uid, cmd, "ok")
            await self._root_send(chat_uid, self._root_help_text())
            return True
        if cmd == "状态":
            self._root_audit(sender_uid, cmd, "ok")
            await self._root_send(chat_uid, self._root_status_text())
            return True
        if cmd == "查询白名单":
            self._root_audit(sender_uid, cmd, "ok")
            await self._root_send(chat_uid, self._root_whitelist_text())
            return True
        if cmd == "查余额":
            self._root_audit(sender_uid, cmd, "ok")
            await self._root_send(chat_uid, self._root_balance_text())
            return True
        if cmd in ROOT_FREQ_PRESETS:
            ok = self._root_set_frequency(cmd)
            self._root_audit(sender_uid, cmd, "ok" if ok else "fail")
            if ok:
                await self._root_send(chat_uid, f"✅ 回话频率已设为「{cmd}」，立即生效")
            else:
                await self._root_send(chat_uid, "⚠️ 频率已更新但保存配置失败，重启后会恢复原设置")
            return True
        if cmd == "忘掉":
            parts = rest.split(None, 1)
            name = parts[1].strip() if len(parts) > 1 else ""
            ok, msg = self._root_forget_person(name)
            self._root_audit(sender_uid, f"忘掉 {name}", "ok" if ok else "fail")
            await self._root_send(chat_uid, msg)
            return True
        if cmd == "早安开":
            ok = self._root_set_greeting_morning(True)
            self._root_audit(sender_uid, cmd, "ok" if ok else "fail")
            await self._root_send(
                chat_uid, "✅ 早安问候已开启，明天早上照常发" if ok else "⚠️ 开启早安问候失败，请稍后再试")
            return True
        if cmd == "早安关":
            ok = self._root_set_greeting_morning(False)
            self._root_audit(sender_uid, cmd, "ok" if ok else "fail")
            await self._root_send(
                chat_uid, "✅ 早安问候已关闭，明天早上不发了" if ok else "⚠️ 关闭早安问候失败，请稍后再试")
            return True
        if cmd == "重启":
            self._root_audit(sender_uid, cmd, "ok")
            await self._root_send(chat_uid, "🔄 正在重启 AstrBot，约 30 秒后恢复")
            self._root_restart_astrbot()
            return True
        # Unknown command (auth passed). Never print the code in a reply.
        self._root_audit(sender_uid, cmd, "unknown")
        await self._root_send(chat_uid, "❓ 未知命令，发送口令 + 帮助 查看命令列表")
        return True


