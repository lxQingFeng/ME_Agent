"""OneBot v11 协议:事件解析(数组消息段+CQ 码兜底)/动作帧/信封。"""
from __future__ import annotations
import re
import time
import uuid
from typing import Any

CQ_RE = re.compile(r"\[CQ:(?P<type>[a-zA-Z0-9_-]+)(?P<params>[^\]]*)\]")

def parse_cq(text: str) -> list[dict]:
    segs: list[dict] = []
    pos = 0
    for m in CQ_RE.finditer(text):
        if m.start() > pos:
            segs.append({"type": "text", "data": {"text": text[pos:m.start()]}})
        params: dict[str, str] = {}
        ps = m.group("params")
        if ps:
            for kv in ps.split(","):
                if "=" in kv:
                    k, _, v = kv.partition("=")
                    params[k.strip()] = _cq_unescape(v.strip())
        segs.append({"type": m.group("type"), "data": params})
        pos = m.end()
    if pos < len(text):
        segs.append({"type": "text", "data": {"text": text[pos:]}})
    return segs or [{"type": "text", "data": {"text": ""}}]

def _cq_unescape(s: str) -> str:
    return (s.replace("&#44;", ",").replace("&#91;", "[").replace("&#93;", "]")
             .replace("&amp;", "&"))

def parse_message(raw: Any) -> list[dict]:
    """数组格式优先;字符串按 CQ 码解析。"""
    if isinstance(raw, list):
        out = []
        for seg in raw:
            if isinstance(seg, dict) and "type" in seg:
                out.append({"type": seg["type"], "data": seg.get("data") or {}})
        return out or [{"type": "text", "data": {"text": ""}}]
    if isinstance(raw, str):
        return parse_cq(raw)
    return [{"type": "text", "data": {"text": ""}}]

def seg_text(segs: list[dict]) -> str:
    buf = []
    for s in segs:
        if s["type"] == "text":
            buf.append(s["data"].get("text", ""))
    return "".join(buf).strip()

def at_me(segs: list[dict], self_id: Any) -> bool:
    sid = str(self_id or "")
    for s in segs:
        if s["type"] == "at" and str(s["data"].get("qq", "")) == sid and sid:
            return True
        if s["type"] == "reply":
            return True
    return False

def mentions_name(segs: list[dict], name: str) -> bool:
    if not name:
        return False
    return name in seg_text(segs)

def envelope(event: dict) -> dict | None:
    """原始 OneBot 事件 → 内部信封(下游只认这个结构)。"""
    if event.get("post_type") != "message":
        return None
    segs = parse_message(event.get("message"))
    chat_type = event.get("message_type", "group")
    chat_id = str(event.get("group_id") or event.get("user_id") or "")
    sender = event.get("sender") or {}
    nickname = (event.get("sender", {}).get("card") or sender.get("nickname")
                or str(event.get("user_id", "")))
    return {
        "chat_id": chat_id,
        "chat_type": chat_type,
        "message_id": event.get("message_id"),
        "sender_id": str(event.get("user_id", "")),
        "sender_name": nickname,
        "text": seg_text(segs),
        "segments": segs,
        "ts": event.get("time") or int(time.time()),
        "raw": event,
        "self_id": str(event.get("self_id", "")),
        "at_me": at_me(segs, event.get("self_id")),
        "sub_type": event.get("sub_type", ""),
    }

# ---- 动作帧 ----
def action_frame(action: str, params: dict, echo: str | None = None) -> dict:
    return {"action": action, "params": params, "echo": echo or uuid.uuid4().hex[:16]}

def text_segment(text: str) -> dict:
    return {"type": "text", "data": {"text": text}}

def at_segment(qq: str) -> dict:
    return {"type": "at", "data": {"qq": str(qq)}}

def reply_segment(message_id: Any) -> dict:
    return {"type": "reply", "data": {"id": message_id}}

def record_segment(file: str) -> dict:
    """file: base64://... 或 file://... 或 http url"""
    return {"type": "record", "data": {"file": file}}

def send_params(chat_type: str, chat_id: str, segments: list[dict]) -> dict:
    if chat_type == "group":
        return {"group_id": int(chat_id), "message": segments, "auto_escape": False}
    return {"user_id": int(chat_id), "message": segments, "auto_escape": False}
