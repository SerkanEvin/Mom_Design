"""synthetic.new models through its Anthropic-compatible Messages API (same endpoint aicp uses)."""
from __future__ import annotations

import base64
import json
import re
from pathlib import Path

import httpx

from . import db
from .config import get_settings, secret


class LLMError(Exception):
    pass


def _rate_limit(user_id: int | None) -> None:
    if user_id is None:
        return
    u = db.row("SELECT role FROM users WHERE id=?", user_id)
    if not u or u["role"] == "owner":
        return
    limit = get_settings().member_llm_per_hour
    n = db.row("SELECT COUNT(*) AS n FROM llm_calls WHERE user_id=? AND at>?", user_id, db.now() - 3600)["n"]
    if n >= limit:
        raise LLMError("Saatlik yapay zekâ sınırına ulaşıldı, biraz sonra tekrar dene.")
    db.execute("INSERT INTO llm_calls(user_id,at) VALUES(?,?)", user_id, db.now())


def image_block(path: Path, max_px: int = 768) -> dict:
    from PIL import Image
    import io
    im = Image.open(path)
    im.thumbnail((max_px, max_px))
    if im.mode not in ("RGB", "RGBA"):
        im = im.convert("RGBA")
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                        "data": base64.b64encode(buf.getvalue()).decode()}}


def chat(system: str, content: list | str, *, vision: bool = False, user_id: int | None = None,
         max_tokens: int = 2500, temperature: float = 0.3) -> str:
    s = get_settings()
    syn_key = secret("syn")
    if not syn_key:
        raise LLMError("synthetic anahtarı ayarlı değil.")
    _rate_limit(user_id)
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    body = {"model": s.syn_vision_model if vision else s.syn_text_model, "max_tokens": max_tokens,
            "temperature": temperature, "system": system, "messages": [{"role": "user", "content": content}]}
    url = s.syn_base_url.rstrip("/") + "/v1/messages"
    last = None
    for attempt in range(3):
        try:
            r = httpx.post(url, json=body, timeout=180, headers={
                "x-api-key": syn_key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
        except httpx.HTTPError as e:
            last = type(e).__name__
            continue
        if r.status_code == 200:
            data = r.json()
            text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
            if text.strip():
                return text
            last = "empty answer"
            continue
        last = f"HTTP {r.status_code}"
        if r.status_code in (400, 401, 402, 403):
            break
    raise LLMError(f"Yapay zekâ çağrısı başarısız ({last}).")


def parse_json(text: str):
    """First JSON object/array in the answer (models sometimes wrap it in prose or fences)."""
    m = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if m:
        text = m.group(1)
    start = min([i for i in (text.find("{"), text.find("[")) if i >= 0], default=-1)
    if start < 0:
        raise LLMError("Yapay zekâ cevabında JSON yok.")
    depth, in_str, esc = 0, False, False
    opener = text[start]
    closer = "}" if opener == "{" else "]"
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return json.loads(text[start:i + 1])
    raise LLMError("Yapay zekâ cevabındaki JSON yarım.")


def chat_json(system: str, content, **kw):
    text = chat(system + "\nAnswer with ONE JSON value only, no prose.", content, **kw)
    try:
        return parse_json(text)
    except (LLMError, json.JSONDecodeError):
        fix = chat("You repair JSON. Return only the corrected JSON.", text[:12000], user_id=None)
        return parse_json(fix)
