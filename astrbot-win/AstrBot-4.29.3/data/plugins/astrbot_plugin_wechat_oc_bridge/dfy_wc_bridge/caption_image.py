# -*- coding: utf-8 -*-
"""Vision caption for 大肥鱼 (2026-11-16, model switched 2026-11-20,
screenshot mode added 2026-11-20).
Sends an image to Qwen3-VL-Plus via DashScope (Alibaba, OpenAI-compatible)
and prints a short Chinese description. The poller will feed this
description to DeepSeek so the bot can react to pictures it cannot
itself see ("[图片：一只橘猫躺在沙发上]").
Needs a DashScope API key (free signup at dashscope.aliyun.com):
put it in C:\\dafeiyu-open\\api_keys.json as {"dashscope_api_key": "sk-..."}.
NOTE 2026-11-20: Qwen2.5-VL was retired from Bailian (model no longer
exists -> 403); switched to Qwen3-VL-Plus, which has 1M free tokens
(expiry 2027-02-18) and works with the same key.
Screenshot mode 2026-11-20: regular [图片] photos have no downloadable
URL in the io-wechat feed (only a hex token), so the poller falls back to
screenshotting the WeChat window and asking Qwen3-VL to describe the
image visible in the screenshot.
Usage:
  python3 caption_image.py <image_path_or_url>
  python3 caption_image.py --screenshot
"""
import base64
import json
import os
import sys
import tempfile
import urllib.request

DASHSCOPE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
MODEL = "qwen3-vl-plus"

def load_key():
    # VM copy first (for testing), laptop copy is the live one.
    for path in (
        os.path.expanduser("~/workspace/dafeiyu-greetings/api_keys.json"),
        r"C:\dafeiyu-open\api_keys.json",
    ):
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                key = json.load(f).get("dashscope_api_key", "")
            if key:
                return key
        except Exception:
            pass
    return ""

def image_to_data_url(path_or_url):
    if path_or_url.startswith("http"):
        req = urllib.request.Request(path_or_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=25) as r:
            blob = r.read()
        mime = r.headers.get("Content-Type", "image/png").split(";")[0]
    else:
        with open(path_or_url, "rb") as f:
            blob = f.read()
        ext = os.path.splitext(path_or_url)[1].lower()
        mime = {"png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".gif": "image/gif", ".webp": "image/webp"}.get(ext, "image/png")
    return "data:" + mime + ";base64," + base64.b64encode(blob).decode("ascii")

def call_vl(data_url, prompt):
    key = load_key()
    if not key:
        return "VISION_NO_KEY: no dashscope_api_key in api_keys.json"
    payload = {
        "model": MODEL,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": data_url}},
                {"type": "text", "text": prompt},
            ],
        }],
        "max_tokens": 120,
    }
    req = urllib.request.Request(
        DASHSCOPE_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.load(r)
    return data["choices"][0]["message"]["content"].strip()

def caption(path_or_url):
    data_url = image_to_data_url(path_or_url)
    return call_vl(data_url, "用一句话中文描述这张图里有什么、什么氛围，不要客套话。")

def take_screenshot():
    """Capture the primary screen to a temp PNG, return its path or None."""
    try:
        from PIL import ImageGrab
    except ImportError:
        return None
    path = os.path.join(tempfile.gettempdir(), "dafeiyu_screenshot.png")
    img = ImageGrab.grab()
    img.save(path, "PNG")
    return path

def caption_screenshot():
    path = take_screenshot()
    if not path:
        return "VISION_NO_PIL: Pillow not available for screenshots"
    data_url = image_to_data_url(path)
    return call_vl(
        data_url,
        "这是一张微信聊天窗口的截图。请描述截图里最新出现的那张图片或照片的内容，"
        "用一句话中文，只描述图片本身，不要描述聊天界面、文字、头像或表情符号。",
    )

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: caption_image.py <image_path_or_url> | --screenshot")
        sys.exit(2)
    if sys.argv[1] == "--screenshot":
        print(caption_screenshot())
    else:
        print(caption(sys.argv[1]))
