"""Keyboard-based group send for WeChat 4.x (xwechat).

wechatauto's open_chat cannot open group sessions on this WeChat 4.x build
(UIA current_chat is mangled, OCR find_session never matches), so group sends
via SendMsg always fail at "会话没打开". This module drives the GUI with raw
keyboard/mouse input instead -- the sequence proven to work on 2026-11-14:

  focus main WeChat window -> open group -> click input box
  -> paste message -> Enter

Group opening is SIDEBAR-FIRST (2026-11-15): click the sidebar's own
search box (a real UIA Edit control) -> paste group name -> UIA-click the
search result. The sidebar session rows themselves are NOT exposed as UIA
elements (mmui::XTableView 'Minimized Groups' has no children -- probed
2026-11-15), so a direct 'click the row' is impossible; the sidebar search
box is the deterministic trigger that replaces the flaky Ctrl+F hotkey
(which sometimes misfires into the 'Search chat history' popup). When the
sidebar search box is unavailable, it falls back to the Ctrl+F path.

DPI: the laptop runs at 125% scaling (LOGPIXELSX=120). SetProcessDPIAware()
makes SetCursorPos coordinates match screenshot pixels.

Groups use this module (uid endswith @chatroom). DMs (1:1 chats) ALSO use
it now (2026-11-15): wechatauto's SendMsg cannot open a DM session on this
WeChat 4.x build either ("会话没打开", UIA down, OCR-only), so the DM path
tries kbd_send_dm first (same sidebar-search open as groups) and only falls
back to wechatauto. The sidebar search dropdown lists contacts as well as
groups, so _open_group works for a contact display name unchanged.
"""

from __future__ import annotations

import ctypes
import time

try:
    ctypes.windll.user32.SetProcessDPIAware()
except Exception:
    pass

import win32api
import win32clipboard
import win32con
import win32gui
import win32process

from .util import log


def _set_clipboard(text: str) -> None:
    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
    finally:
        win32clipboard.CloseClipboard()

def _set_clipboard_image(path: str) -> None:
    """Put an image file on the clipboard as CF_DIB so Ctrl+V pastes it
    into WeChat as a picture/sticker."""
    from PIL import Image
    import io
    im = Image.open(path)
    if im.mode != "RGB":
        im = im.convert("RGB")
    # DIB: BITMAPINFOHEADER (40 bytes) + pixel data, bottom-up, BGR.
    w, h = im.size
    # BGR = 3 bytes/px, no per-pixel padding. Pad each row to a multiple
    # of 4 bytes with zeroes (DIB requirement).
    data = im.tobytes("raw", "BGR")
    stride = ((w * 3 + 3) // 4) * 4
    rows = []
    for y in range(h - 1, -1, -1):
        row = data[y * w * 3:(y + 1) * w * 3]
        rows.append(row + b"\x00" * (stride - w * 3))
    dib = (
        (40).to_bytes(4, "little")
        + w.to_bytes(4, "little", signed=True)
        + h.to_bytes(4, "little", signed=True)
        + (1).to_bytes(2, "little")
        + (24).to_bytes(2, "little")
        + (0).to_bytes(4, "little")
        + (len(b"".join(rows))).to_bytes(4, "little")
        + (2835).to_bytes(4, "little", signed=True)
        + (2835).to_bytes(4, "little", signed=True)
        + (0).to_bytes(4, "little")
        + (0).to_bytes(4, "little")
        + b"".join(rows)
    )
    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32con.CF_DIB, dib)
    finally:
        win32clipboard.CloseClipboard()


def _key(vk: int) -> None:
    win32api.keybd_event(vk, 0, 0, 0)
    time.sleep(0.05)
    win32api.keybd_event(vk, 0, win32con.KEYEVENTF_KEYUP, 0)
    time.sleep(0.05)


def _hotkey(*vks: int) -> None:
    for vk in vks:
        win32api.keybd_event(vk, 0, 0, 0)
        time.sleep(0.05)
    for vk in reversed(vks):
        win32api.keybd_event(vk, 0, win32con.KEYEVENTF_KEYUP, 0)
        time.sleep(0.05)


def _click(x: int, y: int) -> None:
    win32api.SetCursorPos((x, y))
    time.sleep(0.2)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, x, y, 0, 0)
    time.sleep(0.05)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, x, y, 0, 0)
    time.sleep(0.3)


def _main_weixin_hwnd() -> int | None:
    """Largest visible window titled Weixin / class Qt51514QWindowIcon.

    There are two: a tiny Qt51514QWindowToolSaveBits window and the large main
    window. The tiny one is NOT the chat window -- focusing/clicking it does
    nothing. Pick the largest by area.
    """
    wins: list[tuple[int, str, str]] = []

    def _cb(hwnd, _):
        if win32gui.IsWindowVisible(hwnd):
            wins.append((hwnd, win32gui.GetWindowText(hwnd), win32gui.GetClassName(hwnd)))
        return True

    win32gui.EnumWindows(_cb, None)
    best = None
    best_area = 0
    for hwnd, title, cls in wins:
        if title.lower() == "weixin" or cls == "Qt51514QWindowIcon":
            try:
                l, t, r, b = win32gui.GetWindowRect(hwnd)
                area = (r - l) * (b - t)
            except Exception:
                area = 0
            if area > best_area:
                best_area = area
                best = hwnd
    return best


def _focus(hwnd: int) -> bool:
    try:
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    except Exception:
        pass
    time.sleep(0.3)
    try:
        win32gui.SetForegroundWindow(hwnd)
    except Exception as exc:  # noqa: BLE001
        log("kbd_send focus FAIL:", exc)
    time.sleep(0.8)
    return win32gui.GetForegroundWindow() == hwnd


def _is_window_hung(hwnd: int) -> bool | None:
    """True if the window's UI thread is not responding (Task Manager's
    'Not Responding' test). Uses user32.IsHungAppWindow. Returns None when
    the API is unavailable. A hung UI thread is the 'frontend freeze':
    the window stays visible and focusable, but UIA queries and keyboard
    input go nowhere, so the search box / search dropdown never appear.
    """
    try:
        fn = ctypes.windll.user32.IsHungAppWindow
        fn.restype = ctypes.c_bool
        fn.argtypes = [ctypes.c_void_p]
        return bool(fn(hwnd))
    except Exception:
        return None


def _window_has_search_box(hwnd: int) -> bool:
    """True when the sidebar search box is findable in this window's UIA
    tree. This is the cheapest reliable 'is the WeChat UI alive' probe:
    a healthy main window always exposes mmui::XValidatorTextEdit
    'Search'; a frozen window (or a bare chat window) does not.
    """
    return _sidebar_search_edit(hwnd) is not None


def _hot_activate_uia(hwnd: int) -> bool:
    """Hot-activate WeChat 4.x's Qt accessibility gate (the real fix for
    the 'UIA tree collapsed' freeze).

    WeChat 4.x is Qt Quick: QAccessible only materializes the mmui UIA
    tree when a runtime 'active byte' in Weixin.dll is 1. On cold start
    (and intermittently afterwards) it is 0, so the tree collapses to
    ~2 nodes and no sidebar search box exists -- the 'freeze'. Writing
    that one byte to 1 (via wechatauto's AccessibilityGate, already
    installed as wechatauto 1.2.10) makes the current process start
    returning the mmui provider immediately, no WeChat restart needed.
    The gate RVA is cached per WeChat version
    (~/.wechatauto/gate_cache.json) so repeat activations are instant
    (verified: WeChat 4.2.0.52 -> Weixin.dll+0xb4ae448).
    Safe: a single-byte flag write in WeChat's own process, verified by
    checking mmui nodes actually appear; the module restores the byte
    if verification fails. Returns True when the search box is findable
    afterwards. Never raises (returns False on any error) so a missing
    wechatauto can never break sending.
    """
    try:
        from wechatauto.accessibility import AccessibilityGate
    except Exception as exc:  # noqa: BLE001
        log("kbd_send: hot-activate unavailable (wechatauto import failed):", exc)
        return False
    try:
        ok = AccessibilityGate._hot_activate_accessibility(hwnd, verify=True)
        log("kbd_send: hot-activate UIA gate:", ok)
    except Exception as exc:  # noqa: BLE001
        log("kbd_send: hot-activate failed:", exc)
        return False
    if not ok:
        return False
    try:
        time.sleep(0.5)
        return _window_has_search_box(hwnd)
    except Exception:
        return False


def _all_weixin_hwnds(include_hidden: bool = False) -> list[tuple[int, int, bool]]:
    """All Weixin windows as (hwnd, area, visible), largest first.

    There is usually a large main window (session list + chat pane) and
    sometimes a smaller separate chat window. Both are titled 'Weixin' /
    class Qt51514QWindowIcon; only the main one has the sidebar search
    box. The main window can be HIDDEN (minimized to tray or hidden
    behind a popped-out chat) while a bare chat window stays visible;
    driving the chat window can never open a group. Pass
    include_hidden=True to see hidden windows too.
    """
    wins: list[tuple[int, str, str]] = []

    def _cb(hwnd, _):
        if include_hidden or win32gui.IsWindowVisible(hwnd):
            wins.append((hwnd, win32gui.GetWindowText(hwnd), win32gui.GetClassName(hwnd)))
        return True

    win32gui.EnumWindows(_cb, None)
    out: list[tuple[int, int, bool]] = []
    for hwnd, title, cls in wins:
        if title.lower() == "weixin" or cls == "Qt51514QWindowIcon":
            try:
                l, t, r, b = win32gui.GetWindowRect(hwnd)
                area = (r - l) * (b - t)
            except Exception:
                area = 0
            try:
                vis = bool(win32gui.IsWindowVisible(hwnd))
            except Exception:
                vis = False
            out.append((hwnd, area, vis))
    out.sort(key=lambda item: -item[1])
    return out


def _restore_window(hwnd: int) -> bool:
    """Make a hidden/minimized WeChat window visible again.

    SW_RESTORE un-minimizes; SW_SHOW ensures it is shown. Safe and
    non-destructive: it only changes window visibility, never logs out
    or closes anything. Returns True when the window is visible after.
    """
    try:
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    except Exception:
        pass
    try:
        win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
    except Exception:
        pass
    time.sleep(1.0)
    try:
        return bool(win32gui.IsWindowVisible(hwnd))
    except Exception:
        return False


def _best_weixin_hwnd() -> int | None:
    """The Weixin window to drive: the largest WeChat window, restoring
    it first if it is hidden.

    The main window (largest) can become hidden while a smaller chat
    window stays visible; driving the chat window can never open a group
    (it has no sidebar search box), so a hidden main window must be
    restored rather than ignored. Returns None when no Weixin window
    exists at all (visible or hidden).
    """
    cands = _all_weixin_hwnds(include_hidden=True)
    if not cands:
        return None
    hwnd, _area, vis = cands[0]
    if not vis:
        log("kbd_send: main WeChat window hidden, restoring it")
        if _restore_window(hwnd):
            log("kbd_send: main WeChat window restored")
        else:
            log("kbd_send: main WeChat window restore failed, using largest visible")
            vis_cands = [c for c in cands if c[2]]
            if vis_cands:
                return vis_cands[0][0]
    return hwnd


def _wake_window(hwnd: int) -> bool:
    """Try to revive a stale WeChat UI without restarting anything.

    Clicks the window's title bar (activates it and forces a repaint /
    UIA refresh), re-focuses, waits, then re-probes the sidebar search
    box. Returns True when the search box is findable afterwards.
    Safe: a single click on the title bar, no text is typed or sent.
    """
    try:
        l, t, r, _b = win32gui.GetWindowRect(hwnd)
        cx = (l + r) // 2
        cy = t + 10
        _click(cx, cy)
    except Exception as exc:  # noqa: BLE001
        log("kbd_send: wake title-bar click failed:", exc)
    _focus(hwnd)
    time.sleep(1.5)
    try:
        return _window_has_search_box(hwnd)
    except Exception:
        return False


def _ui_diagnosis() -> dict:
    """Snapshot of the WeChat UI state for specific error messages.

    Returns {'windows': [(hwnd, area, visible, hung)], 'search_box':
    bool, 'best_hwnd': int|None}. 'search_box' is True when at least one
    Weixin window exposes the sidebar search box. Callers should let the
    UI settle (Escape + short wait) before calling this, so a
    mid-transition UIA tree is not mistaken for a freeze.
    """
    cands = _all_weixin_hwnds(include_hidden=True)
    windows = []
    search_box = False
    best = None
    for hwnd, area, vis in cands[:3]:
        hung = _is_window_hung(hwnd)
        windows.append((hwnd, area, vis, hung))
        if not vis:
            continue
        try:
            has = _window_has_search_box(hwnd)
        except Exception:
            has = False
        if has and best is None:
            best = hwnd
            search_box = True
    if best is None and cands:
        best = cands[0][0]
    return {"windows": windows, "search_box": search_box, "best_hwnd": best}


def _freeze_error() -> RuntimeError:
    """A specific error for the frozen-frontend failure, so the operator
    knows it is a WeChat UI freeze and not a wrong group name. Distinguishes
    a hung UI thread (needs time or a WeChat restart) from a missing
    search UI (window not showing the chat list).
    """
    diag = _ui_diagnosis()
    hung_any = any(w[3] is True for w in diag["windows"])
    if hung_any:
        return RuntimeError(
            "kbd_send: WeChat frontend HUNG (UI thread not responding) -- "
            "send aborted; wait for WeChat to recover or restart WeChat "
            "(user-confirmed only, may need re-login)"
        )
    if diag["windows"]:
        return RuntimeError(
            "kbd_send: WeChat search UI unavailable (sidebar search box "
            "not found in any WeChat window) -- send aborted; check that "
            "the WeChat main window shows the chat list"
        )
    return RuntimeError("kbd_send: no Weixin main window found")



def _close_search_history() -> None:
    """Close any stray 'Search chat history' popup.

    That window (a large Qt51514QWindowIcon top-level window) steals the
    Down/Enter keystrokes after Ctrl+F and swallows the send -- the group
    never opens and the message goes nowhere, yet no error is raised.
    Close it before every group send.
    """
    closed = 0

    def _cb(hwnd, _):
        nonlocal closed
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return True
            title = win32gui.GetWindowText(hwnd) or ""
            if "search chat history" in title.lower():
                win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
                closed += 1
                log("kbd_send: closed stray window:", title, hwnd)
        except Exception:
            pass
        return True

    try:
        win32gui.EnumWindows(_cb, None)
    except Exception as exc:  # noqa: BLE001
        log("kbd_send: EnumWindows for search-history failed:", exc)
    if closed:
        time.sleep(0.8)



def _search_history_open() -> bool:
    """True if a 'Search chat history' popup window is currently open."""
    found = []

    def _cb(hwnd, _):
        try:
            if win32gui.IsWindowVisible(hwnd):
                t = win32gui.GetWindowText(hwnd) or ""
                if "search chat history" in t.lower():
                    found.append(hwnd)
        except Exception:
            pass
        return True

    try:
        win32gui.EnumWindows(_cb, None)
    except Exception:
        pass
    return bool(found)



def _click_search_result(short: str, full_name: str) -> bool:
    """Click the target group in the Ctrl+F search dropdown via UIA.

    Finds the first mmui::SearchContentCellView whose Name contains the
    distinctive short query (the Group Chats section comes before Official
    Accounts in the dropdown, so the first match is the group). Returns
    True when it clicked something, False when nothing matched.
    """
    try:
        import uiautomation as auto  # type: ignore
    except Exception as exc:  # noqa: BLE001
        log("kbd_send: uiautomation import failed:", exc)
        return False
    target = None
    try:
        root = auto.GetRootControl()
    except Exception as exc:  # noqa: BLE001
        log("kbd_send: UIA root failed:", exc)
        return False

    def _walk(ctrl, depth=0):
        nonlocal target
        if target is not None or depth > 9:
            return
        try:
            name = ctrl.Name or ""
            cls = ctrl.ClassName or ""
        except Exception:
            return
        if cls == "mmui::SearchContentCellView" and name and short in name:
            # Prefer the exact full group name when present.
            if full_name in name or name == full_name:
                target = ctrl
                return
            if target is None:
                target = ctrl
                return
        try:
            children = ctrl.GetChildren()
        except Exception:
            children = []
        for ch in children:
            _walk(ch, depth + 1)
            if target is not None:
                return

    try:
        _walk(root)
    except Exception as exc:  # noqa: BLE001
        log("kbd_send: UIA walk failed:", exc)
        return False
    if target is None:
        return False
    try:
        r = target.BoundingRectangle
        cx = (r.left + r.right) // 2
        cy = (r.top + r.bottom) // 2
    except Exception as exc:  # noqa: BLE001
        log("kbd_send: UIA rect failed:", exc)
        return False
    log("kbd_send: UIA clicked search result center (%d,%d)" % (cx, cy))
    _click(cx, cy)
    return True



# Chatroom ID -> display name. The DB-backed display_name() lookup can
# fail (DB locked/unavailable after a restart), and then the raw ID is
# searched for ("45270768515@") which matches nothing in WeChat search --
# Down+Enter opens nothing and the message lands in the wrong chat.
# Hardcode the two known groups so the search query is always right.
_CHATROOM_NAMES = {
    "45270768515@chatroom": "广外AI共创｜腾讯云AI交流圈",
    "54306443862@chatroom": "迈阿醚TML后台",
    # Girlfriend DMs: display_name() can fail when the WeChat DB is locked,
    # and searching by raw wxid never matches anything. Map them here so
    # the search always uses the real nickname.
    "wxid_3pc1tr6o9yg522": "对方正在输入...",
    "wxid_felikc783cjk22": "B",
}


def _short_name(group_name: str) -> str:
    """Distinctive short query for the group (part before |/｜, max 12 chars)."""
    group_name = _CHATROOM_NAMES.get(group_name, group_name)
    short = group_name
    for sep in ("|", "｜"):
        if sep in short:
            short = short.split(sep)[0]
    return short.strip()[:12] or group_name


# --- "Already in this chat" skip (2026-11-17) ---
# User request: when the target group is already open, don't search for
# it again -- write straight into the input box.
#
# What the probes found (2026-11-17, live UIA dump of the main window):
# WeChat 4.x does NOT expose the current chat name through UIA. The chat
# title bar (mmui::ChatTitleBarChatRoomView) has an empty Name, no
# children, and an empty LegacyIAccessible name/value; the session list
# rows are not exposed either. So "which chat is open" cannot be read
# from UIA on this build, and we must NOT guess it.
#
# The skip therefore rests on the next-best evidence, and it is
# deliberately narrow:
#   * _open_group records the chat it REALLY opened (via search) in
#     last_open_chat.json, with a timestamp.
#   * A later send to the SAME chat skips the search ONLY when that
#     record is < SKIP_MAX_AGE_S old AND UIA confirms a chat of the
#     right kind is open (ChatTitleBarChatRoomView for groups;
#     MasterView present and no ChatRoomView for DMs).
#   * A skipped send does NOT refresh the record, so a wrong skip
#     cannot perpetuate itself past the window.
#   * Any doubt (missing/corrupt record, stale, UIA unreadable or
#     collapsed, wrong chat kind) -> old search flow. Never guess.
# Residual risk: the user manually clicks a different group within the
# 60 s window after a real open; the next send to the old group would
# skip the search and land in the newly opened group. Kill switch:
# create C:\dafeiyu-open\skip_search_disabled to force the old flow.
LAST_OPEN_PATH = r"C:\dafeiyu-open\last_open_chat.json"
SKIP_SEARCH_FLAG = r"C:\dafeiyu-open\skip_search_disabled"
SKIP_MAX_AGE_S = 60.0


def _record_opened_chat(group_name: str) -> None:
    """Record that `group_name` was REALLY opened (via a search path).

    Called only after a search-based open succeeded. Skipped sends do
    NOT call this, so the window stays anchored to the last real open.
    Failures are logged, never raised (a missing record just disables
    the skip -- the old flow still works).
    """
    try:
        import json
        name = _CHATROOM_NAMES.get(group_name, group_name)
        with open(LAST_OPEN_PATH, "w", encoding="utf-8") as f:
            json.dump({"name": name, "ts": time.time()}, f)
    except Exception as exc:  # noqa: BLE001
        log("kbd_send: record opened chat failed:", exc)


def _title_bar_kind(hwnd: int) -> str | None:
    """'group' / 'dm' / None: what kind of chat the UIA tree shows open.

    'group' when a mmui::ChatTitleBarChatRoomView is present, 'dm' when
    the master title bar is present but no chatroom view (a 1:1 chat),
    None when unreadable (collapsed tree) or no chat is open. Any
    unexpected shape -> None (caller falls back to the search flow).
    """
    try:
        import uiautomation as auto  # type: ignore
    except Exception:
        return None
    try:
        root = auto.ControlFromHandle(hwnd)
    except Exception:
        return None
    found = {"room": False, "master": False}

    def _walk(ctrl, depth=0):
        if depth > 14 or (found["room"] and found["master"]):
            return
        try:
            cls = ctrl.ClassName or ""
        except Exception:
            return
        if cls == "mmui::ChatTitleBarChatRoomView":
            found["room"] = True
        elif cls == "mmui::ChatTitleBarMasterView":
            found["master"] = True
        try:
            children = ctrl.GetChildren()
        except Exception:
            children = []
        for ch in children:
            _walk(ch, depth + 1)
            if found["room"] and found["master"]:
                return

    try:
        _walk(root)
    except Exception:
        return None
    if found["room"]:
        return "group"
    if found["master"]:
        return "dm"
    return None


def _name_matches(a: str, b: str) -> bool:
    """True when two chat names refer to the same chat.

    The hardcoded _CHATROOM_NAMES has '迈阿醚TML后台' but WeChat's real
    group name (from display_name / the DB) is '迈阿醚TML后台🔞' -- the
    record and the target can differ by a trailing emoji/suffix
    depending on which path resolved the name. Treat exact equality
    OR one being a prefix of the other as a match. The group names
    here are distinctive enough that a prefix collision is not a
    realistic risk; anything else still falls back to the search.
    """
    if not a or not b:
        return False
    return a == b or a.startswith(b) or b.startswith(a)


def _current_chat_is_target(group_name: str, hwnd: int | None) -> bool:
    """True only when we are confident the target chat is already open.

    All of these must hold: no kill-switch flag; a fresh (< 60 s)
    open record naming exactly this chat; UIA showing a chat of the
    right kind open. Anything else -> False (search as before).
    """
    if not hwnd:
        return False
    try:
        import os
        if os.path.exists(SKIP_SEARCH_FLAG):
            return False
    except Exception:
        return False
    try:
        import json
        with open(LAST_OPEN_PATH, encoding="utf-8") as f:
            rec = json.load(f)
        name = (rec or {}).get("name") or ""
        ts = float((rec or {}).get("ts") or 0)
    except Exception:
        return False
    target = _CHATROOM_NAMES.get(group_name, group_name)
    if not name or not _name_matches(name, target):
        return False
    if time.time() - ts > SKIP_MAX_AGE_S:
        return False
    kind = _title_bar_kind(hwnd)
    if kind is None:
        return False
    group_names = {
        v for k, v in _CHATROOM_NAMES.items() if k.endswith("@chatroom")
    }
    is_group = str(group_name or "").endswith("@chatroom") or any(
        _name_matches(target, g) for g in group_names
    )
    if is_group:
        return kind == "group"
    return kind == "dm"


def _sidebar_search_edit(hwnd: int):
    """Find the sidebar's own search box (mmui::XValidatorTextEdit 'Search').

    It sits at the top of the session list (ChatMasterView) and is a real
    UIA Edit control, so clicking it is deterministic -- unlike the Ctrl+F
    hotkey, which sometimes misfires into the 'Search chat history' popup.
    Returns the control, or None when UIA cannot find it (caller falls
    back to the Ctrl+F path).
    """
    try:
        import uiautomation as auto  # type: ignore
    except Exception as exc:  # noqa: BLE001
        log("kbd_send: uiautomation import failed (sidebar):", exc)
        return None
    try:
        root = auto.ControlFromHandle(hwnd)
    except Exception as exc:  # noqa: BLE001
        log("kbd_send: UIA window handle failed (sidebar):", exc)
        return None
    found = []

    def _walk(ctrl, depth=0):
        if found or depth > 14:
            return
        try:
            name = ctrl.Name or ""
            cls = ctrl.ClassName or ""
        except Exception:
            return
        if cls == "mmui::XValidatorTextEdit" and name == "Search":
            found.append(ctrl)
            return
        try:
            children = ctrl.GetChildren()
        except Exception:
            children = []
        for ch in children:
            _walk(ch, depth + 1)
            if found:
                return

    try:
        _walk(root)
    except Exception as exc:  # noqa: BLE001
        log("kbd_send: UIA walk failed (sidebar):", exc)
        return None
    return found[0] if found else None


def _open_group_sidebar(group_name: str, hwnd: int) -> bool:
    """Open the group via the sidebar's own search box (primary path).

    Click the sidebar Search Edit -> paste short name -> UIA-click the
    SearchContentCellView result. Returns True on success, False when the
    sidebar control is missing or no result matched (caller falls back to
    Ctrl+F). Raises RuntimeError only when a group opened but the search
    history popup appeared instead (same guard as the Ctrl+F path).
    """
    edit = _sidebar_search_edit(hwnd)
    if edit is None:
        log("kbd_send: sidebar search box not found, falling back to Ctrl+F")
        return False
    try:
        r = edit.BoundingRectangle
        cx = (r.left + r.right) // 2
        cy = (r.top + r.bottom) // 2
    except Exception as exc:  # noqa: BLE001
        log("kbd_send: sidebar search rect failed:", exc)
        return False
    _click(cx, cy)
    time.sleep(0.5)
    short = _short_name(group_name)
    _set_clipboard(short)
    _hotkey(win32con.VK_CONTROL, ord("V"))
    time.sleep(1.5)
    if not _click_search_result(short, group_name):
        log("kbd_send: sidebar search found no result for %r" % (group_name,))
        return False
    time.sleep(2.0)
    _close_search_history()
    if _search_history_open():
        raise RuntimeError(
            "kbd_send: search history popup opened instead of group %r; aborting"
            % (group_name,)
        )
    log("kbd_send: opened group via sidebar search:", group_name)
    return True


def _read_clipboard() -> str:
    """Read current clipboard text ('' on failure)."""
    try:
        import win32clipboard
        win32clipboard.OpenClipboard()
        try:
            if win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
                return win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT) or ""
            return ""
        finally:
            win32clipboard.CloseClipboard()
    except Exception:
        return ""


def _input_click(hwnd: int | None) -> None:
    """Click the message input box, positioned relative to the window rect.

    The input box sits in the chat pane (right side), near the bottom.
    The chat pane does NOT span the full window width: the session list
    takes ~300px on the left and (on wide windows) WeChat leaves an
    empty panel on the right, so 0.65x window width lands in the empty
    right panel and the paste goes nowhere (proven 2026-11-16 by
    screenshot: click at 0.65x missed the input box entirely). 0.45x
    lands inside the input box for both narrow (~786px) and wide
    (~1409px) windows. Falls back to (1100, 800) when the rect is
    unavailable.
    """
    try:
        if hwnd:
            l, t, r, b = win32gui.GetWindowRect(hwnd)
            # Click the TEXT area of the input box, not the toolbar.
            # The input box is ~150px tall anchored to the bottom; its
            # text area is the top ~110px and the toolbar (emoji/Send)
            # is the bottom ~40px. bottom-60 lands on the toolbar and
            # never focuses the text input, so the paste goes nowhere
            # (proven 2026-11-16 by screenshot). bottom-100 lands in
            # the text area.
            _click(l + int((r - l) * 0.45), b - 100)
            return
    except Exception:
        pass
    _click(1100, 800)


def _verify_input_box(text: str) -> bool:
    """True when the message input box currently holds exactly `text`.

    Clears the clipboard, Ctrl+A / Ctrl+C in the (already focused)
    input box, reads the clipboard back. This is the honesty check that
    makes 'kbd-sent' mean something: if the paste did not land in the
    input box (wrong click, wrong chat, unfocused window), the read-back
    will not match and the caller must NOT report success.
    """
    try:
        import win32clipboard
        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
        finally:
            win32clipboard.CloseClipboard()
    except Exception:
        pass
    _hotkey(win32con.VK_CONTROL, ord("A"))
    _hotkey(win32con.VK_CONTROL, ord("C"))
    time.sleep(0.3)
    got = _read_clipboard()
    # Normalize line endings (WeChat input may return \r\n).
    norm = lambda s: (s or "").replace("\r\n", "\n").strip()
    return norm(got) == norm(text)


def _open_group_coords(group_name: str, hwnd: int) -> bool:
    """Open the group by COORDINATES, no UIA needed (2026-11-16).

    Used when the WeChat UIA tree is collapsed (gate already on, tree still
    2 nodes -- the logged-in main window does not materialize the tree even
    after hot-activation). Clicks the sidebar search box at a position
    relative to the window rect, pastes the short group name, then VERIFIES
    the search box actually holds that text (Ctrl+A / Ctrl+C / read the
    clipboard) before pressing Down+Enter to open the first match. The
    clipboard check is the safety net: if the click missed the search box,
    the text never lands there and we abort instead of opening (and then
    messaging) the wrong chat.
    Returns True when the group was (very likely) opened.
    """
    # Re-focus first: the Escape/close-history steps before this fallback
    # can leave focus elsewhere, and an unfocused click goes nowhere.
    _focus(hwnd)
    time.sleep(0.5)
    try:
        l, t, r, b = win32gui.GetWindowRect(hwnd)
    except Exception as exc:  # noqa: BLE001
        log("kbd_send: coords open failed (no window rect):", exc)
        return False
    w = r - l
    h = b - t
    short = _short_name(group_name)
    # The window rect flips between DPI-logical (786x619) and physical
    # (982x774) sizes, so a fixed pixel offset misses. Use FRACTIONS of the
    # window size for both axes (grid-probed: search box ~23% across,
    # ~8.9% down). Probe candidates until one verifies.
    candidates = [
        (0.23, 0.089), (0.20, 0.089), (0.26, 0.089), (0.16, 0.089), (0.30, 0.089),
        (0.23, 0.073), (0.20, 0.073), (0.26, 0.073),
        (0.23, 0.105), (0.20, 0.105), (0.26, 0.105),
        (0.12, 0.089), (0.35, 0.089),
    ]
    found = False
    for xf, yf in candidates:
        cx = l + int(w * xf)
        cy = t + int(h * yf)
        _click(cx, cy)
        time.sleep(0.5)
        # Clear whatever is in the box, then paste the short name.
        _hotkey(win32con.VK_CONTROL, ord("A"))
        _key(win32con.VK_DELETE)
        time.sleep(0.15)
        _set_clipboard(short)
        _hotkey(win32con.VK_CONTROL, ord("V"))
        # Wait longer for the dropdown to populate (2026-11-16): the
        # dropdown updates asynchronously after typing, and Down+Enter
        # on a STALE dropdown opens the wrong chat (proven: searched
        # for 迈阿醚 but the open chat stayed on B). 1.5s gives the
        # dropdown time to refresh to the current query.
        time.sleep(1.5)
        # Verify the box holds our text (proves the click hit the search box).
        # MUST clear the clipboard first: _set_clipboard() above already put
        # `short` on it, so a Ctrl+C that copies nothing would still read back
        # `short` and fake a pass. Clearing first makes this a real check.
        try:
            import win32clipboard
            win32clipboard.OpenClipboard()
            try:
                win32clipboard.EmptyClipboard()
            finally:
                win32clipboard.CloseClipboard()
        except Exception:
            pass
        _hotkey(win32con.VK_CONTROL, ord("A"))
        _hotkey(win32con.VK_CONTROL, ord("C"))
        time.sleep(0.3)
        got = _read_clipboard()
        if got.strip() == short:
            found = True
            found_xy = (cx, cy)
            log("kbd_send: coords search box found at (%d,%d)" % (cx, cy))
            break
        log("kbd_send: coords candidate (%d,%d) missed (clipboard=%r)" % (cx, cy, got))
        _key(win32con.VK_ESCAPE)
        time.sleep(0.2)
    if not found:
        log("kbd_send: coords open: no candidate hit the search box")
        return False
    # Open the first dropdown result by CLICKING it, not Down+Enter.
    # Down+Enter proved unreliable (2026-11-16): the Ctrl+A/Ctrl+C
    # verification leaves the search text selected, which can dismiss
    # the dropdown, so Down+Enter then clears the search without
    # opening anything (chat stayed on the previous conversation).
    # The dropdown's first result sits directly below the search box,
    # ~0.125x window height down, same x as the search box (verified
    # by screenshot 2026-11-16: search box y~0.089h, first result
    # y~0.208h). Clicking it opens the chat deterministically.
    _key(win32con.VK_END)
    time.sleep(0.5)
    try:
        l, t, r, b = win32gui.GetWindowRect(hwnd)
        h = b - t
    except Exception:
        h = 800
    cx, cy = found_xy
    _click(cx, cy + int(h * 0.125))
    time.sleep(2.0)
    _close_search_history()
    if _search_history_open():
        raise RuntimeError(
            "kbd_send: search history popup opened instead of group %r; aborting"
            % (group_name,)
        )
    log("kbd_send: opened group via coords search:", group_name)
    return True


def _open_group_ctrlf(group_name: str) -> None:
    """Open the group via Ctrl+F search + UIA click (fallback path).

    The Ctrl+F hotkey can misfire into the 'Search chat history' popup, so
    this is only used when the sidebar search box is unavailable.
    Raises RuntimeError if the group cannot be opened.
    """
    short = _short_name(group_name)
    _hotkey(win32con.VK_CONTROL, ord("F"))
    time.sleep(1.0)
    _set_clipboard(short)
    _hotkey(win32con.VK_CONTROL, ord("V"))
    time.sleep(1.5)
    if not _click_search_result(short, group_name):
        raise RuntimeError(
            "kbd_send: group %r not found in search dropdown; aborting"
            % (group_name,)
        )
    time.sleep(2.0)
    _close_search_history()
    if _search_history_open():
        raise RuntimeError(
            "kbd_send: search history popup opened instead of group %r; aborting"
            % (group_name,)
        )


def _open_group(group_name: str, hwnd: int | None = None) -> None:
    """Open the named group: sidebar search first, Ctrl+F as fallback.

    Shared by text and image sends. Raises RuntimeError if the group
    cannot be opened (never send into the wrong chat).

    NOTE (2026-11-15 probe): the sidebar session rows themselves are NOT
    exposed as UIA elements (mmui::XTableView 'Minimized Groups' has no
    children), so a direct 'click the row' is impossible on this WeChat
    4.x build. The sidebar-first path therefore uses the sidebar's own
    search box (a real UIA Edit control) instead of the Ctrl+F hotkey --
    same result dropdown, but a deterministic trigger.

    FREEZE HANDLING (2026-11-16): the WeChat frontend sometimes freezes
    (UI thread stops responding) while the window stays visible and
    focusable, and the main window can also become HIDDEN while a bare
    chat window stays visible. When that happens the UIA tree stops
    exposing the sidebar search box and no search dropdown ever appears,
    so both paths fail with a misleading 'group not found'. To handle
    it: resolve the best window first (largest WeChat window, restoring
    it if hidden), try a title-bar wake when the box is missing, and on
    total failure settle the UI and raise a specific freeze error (see
    _freeze_error) instead of blaming the group name.
    """
    # Resolve the window to drive: the largest WeChat window, restoring
    # it first if it is hidden. A bare chat window has no search box,
    # so driving it can never open a group.
    best = _best_weixin_hwnd()
    if best is not None:
        hwnd = best
    # Skip the search when the target chat is already open (2026-11-17).
    # _current_chat_is_target is deliberately strict: it only returns
    # True on positive evidence (fresh open record + right chat kind);
    # anything else falls through to the search below. Never guess.
    if hwnd and _current_chat_is_target(group_name, hwnd):
        log("kbd_send: already in target chat, skipping search:", group_name)
        return
    if hwnd:
        try:
            if _open_group_sidebar(group_name, hwnd):
                _record_opened_chat(group_name)
                return
        except RuntimeError:
            raise
        except Exception as exc:  # noqa: BLE001
            log("kbd_send: sidebar open failed, falling back to Ctrl+F:", exc)
        # Sidebar search box was missing (or the open failed): the UI may
        # be stale, not dead. First hot-activate the Qt accessibility
        # gate (the real fix for a collapsed UIA tree -- see
        # _hot_activate_uia), then fall back to the title-bar wake.
        if not _window_has_search_box(hwnd):
            log("kbd_send: search box missing, hot-activating UIA gate")
            if _hot_activate_uia(hwnd):
                try:
                    if _open_group_sidebar(group_name, hwnd):
                        _record_opened_chat(group_name)
                        return
                except RuntimeError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    log("kbd_send: sidebar open after hot-activate failed:", exc)
            log("kbd_send: search box still missing, waking window and re-probing")
            if _wake_window(hwnd):
                try:
                    if _open_group_sidebar(group_name, hwnd):
                        _record_opened_chat(group_name)
                        return
                except RuntimeError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    log("kbd_send: sidebar open after wake failed:", exc)
        # Dismiss any half-open sidebar search UI before the fallback.
        _key(win32con.VK_ESCAPE)
        time.sleep(0.5)
        _close_search_history()
        # Coordinate fallback (2026-11-16): when the UIA tree is collapsed
        # the Ctrl+F path cannot click its dropdown result either, so try
        # the no-UIA coordinate open before falling back to Ctrl+F.
        if hwnd:
            try:
                if _open_group_coords(group_name, hwnd):
                    _record_opened_chat(group_name)
                    return
            except RuntimeError:
                raise
            except Exception as exc:  # noqa: BLE001
                log("kbd_send: coords open failed:", exc)
    try:
        _open_group_ctrlf(group_name)
    except RuntimeError:
        # Ctrl+F also found nothing. Settle the UI first (dismiss any
        # half-open dropdown and let the UIA tree rebuild) so the freeze
        # diagnosis is not fooled by a mid-transition tree, then raise
        # the specific diagnosis instead of blaming the group name.
        _key(win32con.VK_ESCAPE)
        time.sleep(0.5)
        _close_search_history()
        time.sleep(2.0)
        raise _freeze_error()
    _record_opened_chat(group_name)


def kbd_send_group(group_name: str, text: str, keep_newlines: bool = False) -> str:
    """Send `text` to the group named `group_name` via keyboard automation.

    Returns a short status string. Raises RuntimeError on failure.
    """
    _close_search_history()
    hwnd = _best_weixin_hwnd()
    if not hwnd:
        raise RuntimeError("kbd_send: no Weixin main window found")
    if not _focus(hwnd):
        log("kbd_send: foreground did not match, continuing anyway")

    # Open the group; retry once if the first attempt fails (UIA can be
    # stale on first try -- this is the "微信附着断了" 503). Retry is safe:
    # no message text has been pasted yet, so nothing can double-send.
    # If the WeChat UI thread is hung, wait longer before retrying: a
    # transient hang (busy UI thread) can clear in ~10s, while an
    # immediate retry against a hung window always fails the same way.
    last_exc = None
    for attempt in (1, 2):
        try:
            _open_group(group_name, hwnd)
            break
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            log(f"kbd_send: open group attempt {attempt} failed: {exc}")
            if attempt == 1:
                hung = _is_window_hung(hwnd)
                wait = 10 if hung else 3
                if hung:
                    log("kbd_send: window hung, waiting %ds for UI thread" % wait)
                time.sleep(wait)
                # Dismiss any half-open search UI, re-focus, retry.
                _key(win32con.VK_ESCAPE)
                time.sleep(0.5)
                _close_search_history()
                hwnd2 = _best_weixin_hwnd()
                if hwnd2:
                    hwnd = hwnd2
                    _focus(hwnd)
                continue
            raise
    # (open-group retry above only covers the pre-send phase; no text has
    # been pasted yet, so a retry can never double-send.)

    # Click the message input box, paste, send.
    # Input box sits near the bottom of the chat pane; (1100, 800) is the
    # position proven on 2026-11-14 (125% DPI, DPI-aware process).
    # Sanitize: the model often emits leading/trailing newlines or a stray
    # blank line, which renders as an odd empty line / line break in the
    # bubble. By default collapse all whitespace runs to single spaces and
    # trim, so a reply goes out as one clean line. When keep_newlines is
    # True (structured posts: standings, news), preserve line breaks:
    # strip each line, drop empties, collapse inner spaces only.
    import re as _re
    if keep_newlines:
        _lines = [_re.sub(r"[^\S\n]+", " ", ln).strip() for ln in (text or "").splitlines()]
        # Keep single blank lines between sections (collapse 2+ blanks to 1,
        # drop leading/trailing blanks) so sections read as separate blocks.
        _out = []
        _prev_blank = True
        for _ln in _lines:
            if _ln:
                _out.append(_ln)
                _prev_blank = False
            elif not _prev_blank:
                _out.append("")
                _prev_blank = True
        text = "\n".join(_out).rstrip("\n")
    else:
        text = _re.sub(r"\s+", " ", (text or "")).strip()
    if not text:
        raise RuntimeError("kbd_send: empty text after sanitize")

    # Paste + verify, retry once on failure (2026-11-27): the noon greeting
    # to 广外 failed twice (503) with the group open but the text never
    # landing in the input box -- a transient missed click / slow UI.
    # Retry is safe against double-send: on verify failure we clear the
    # input box (Ctrl+A + Delete) before re-pasting, so a partial paste
    # can never accumulate into a duplicated message.
    pasted = False
    for attempt in (1, 2):
        _input_click(hwnd)
        time.sleep(0.5)
        _set_clipboard(text)
        _hotkey(win32con.VK_CONTROL, ord("V"))
        time.sleep(1.0)
        # Honesty check (2026-11-16): verify the text actually landed in the
        # input box before pressing Enter. If the click missed the box or the
        # wrong chat is open, the read-back will not match and we must NOT
        # report kbd-sent. Without this, a blind paste+Enter reports success
        # while sending nothing (proven by screenshot 2026-11-16).
        if _verify_input_box(text):
            pasted = True
            break
        log(f"kbd_send: paste verify attempt {attempt} failed for {group_name}")
        if attempt == 1:
            # Clear whatever (partially) landed, then retry the click+paste.
            _hotkey(win32con.VK_CONTROL, ord("A"))
            _key(win32con.VK_DELETE)
            time.sleep(0.5)
    if not pasted:
        raise RuntimeError(
            "kbd_send: pasted text did not land in the input box "
            "(wrong click or wrong chat open); aborting, NOT sent"
        )
    _key(win32con.VK_RETURN)
    time.sleep(1.0)
    log("kbd_send: sent to group:", group_name)
    return "kbd-sent"


def kbd_send_dm(contact_name: str, text: str, keep_newlines: bool = False) -> str:
    """Send `text` to the 1:1 contact named `contact_name` via keyboard automation.

    Same flow as kbd_send_group: open the chat via the sidebar search box
    (the dropdown lists contacts as well as groups), click the input box,
    paste, Enter. Returns a short status string. Raises RuntimeError on
    failure. Used by the DM path in sending.py because wechatauto's SendMsg
    cannot open a DM session on this WeChat 4.x build.
    """
    _close_search_history()
    hwnd = _best_weixin_hwnd()
    if not hwnd:
        raise RuntimeError("kbd_send: no Weixin main window found")
    if not _focus(hwnd):
        log("kbd_send: foreground did not match, continuing anyway")

    # Open the contact chat; retry once if the first attempt fails (UIA can
    # be stale on first try). Retry is safe: no message text has been pasted
    # yet, so nothing can double-send. If the WeChat UI thread is hung,
    # wait longer before retrying (a transient hang can clear in ~10s).
    last_exc = None
    for attempt in (1, 2):
        try:
            _open_group(contact_name, hwnd)
            break
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            log(f"kbd_send: open DM attempt {attempt} failed: {exc}")
            if attempt == 1:
                hung = _is_window_hung(hwnd)
                wait = 10 if hung else 3
                if hung:
                    log("kbd_send: window hung, waiting %ds for UI thread" % wait)
                time.sleep(wait)
                _key(win32con.VK_ESCAPE)
                time.sleep(0.5)
                _close_search_history()
                hwnd2 = _best_weixin_hwnd()
                if hwnd2:
                    hwnd = hwnd2
                    _focus(hwnd)
                continue
            raise

    import re as _re
    if keep_newlines:
        _lines = [_re.sub(r"[^\S\n]+", " ", ln).strip() for ln in (text or "").splitlines()]
        _out = []
        _prev_blank = True
        for _ln in _lines:
            if _ln:
                _out.append(_ln)
                _prev_blank = False
            elif not _prev_blank:
                _out.append("")
                _prev_blank = True
        text = "\n".join(_out).rstrip("\n")
    else:
        text = _re.sub(r"\s+", " ", (text or "")).strip()
    if not text:
        raise RuntimeError("kbd_send: empty text after sanitize")

    # Paste + verify, retry once on failure (2026-11-27, same as
    # kbd_send_group): a transient missed click / slow UI must not fail
    # the whole send. Clear the input box before re-pasting so a partial
    # paste can never accumulate into a duplicated message.
    pasted = False
    for attempt in (1, 2):
        _input_click(hwnd)
        time.sleep(0.5)
        _set_clipboard(text)
        _hotkey(win32con.VK_CONTROL, ord("V"))
        time.sleep(1.0)
        # Honesty check (2026-11-16): verify the text actually landed in the
        # input box before pressing Enter. See kbd_send_group.
        if _verify_input_box(text):
            pasted = True
            break
        log(f"kbd_send: paste verify attempt {attempt} failed for DM {contact_name}")
        if attempt == 1:
            _hotkey(win32con.VK_CONTROL, ord("A"))
            _key(win32con.VK_DELETE)
            time.sleep(0.5)
    if not pasted:
        raise RuntimeError(
            "kbd_send: pasted text did not land in the input box "
            "(wrong click or wrong chat open); aborting, NOT sent"
        )
    _key(win32con.VK_RETURN)
    time.sleep(1.0)
    log("kbd_send: sent to DM:", contact_name)
    return "kbd-sent"


def kbd_send_image(group_name: str, image_path: str) -> str:
    """Send an image file to the group named `group_name`.

    Opens the group (same UIA search+click as text), pastes the image
    from the clipboard into the input box, Enter to send.
    Returns a short status string. Raises RuntimeError on failure.
    """
    _close_search_history()
    hwnd = _best_weixin_hwnd()
    if not hwnd:
        raise RuntimeError("kbd_send: no Weixin main window found")
    if not _focus(hwnd):
        log("kbd_send: foreground did not match, continuing anyway")
    _open_group(group_name, hwnd)
    _input_click(hwnd)
    time.sleep(0.5)
    _set_clipboard_image(image_path)
    _hotkey(win32con.VK_CONTROL, ord("V"))
    time.sleep(1.5)
    _key(win32con.VK_RETURN)
    time.sleep(1.0)
    log("kbd_send: sent image to group:", group_name, image_path)
    return "kbd-sent-image"
