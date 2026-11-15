"""텔레그램으로 알림 보내기 / 텔레그램 API 부르기."""
import html
import os

import requests

API = os.environ.get("TELEGRAM_API", "https://api.telegram.org").rstrip("/")
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
TEXT_LIMIT = 3800   # 텔레그램 글자 제한(4096)보다 여유 있게


def _esc(s):
    return html.escape(str(s), quote=False)


def tg(method, payload=None, quiet=False):
    """텔레그램 Bot API 호출 → 응답 JSON. 실패하면 RuntimeError (quiet=True면 그냥 {} 반환)."""
    payload = {k: v for k, v in (payload or {}).items() if v is not None}
    try:
        r = requests.post(f"{API}/bot{TOKEN}/{method}", json=payload, timeout=25)
        data = r.json()
    except (requests.RequestException, ValueError) as e:
        if quiet:
            return {}
        raise RuntimeError(f"텔레그램 연결 실패 ({method}): {type(e).__name__}") from None
    if not data.get("ok"):
        if quiet:
            return {}
        raise RuntimeError(f"텔레그램 {method} 실패: {str(data.get('description') or data)[:200]}")
    return data


def _inline(rows):
    return {"inline_keyboard": [[{"text": b["text"], **({"url": b["url"]} if b.get("url") else {"callback_data": b["cb"]})}
                                 for b in row] for row in rows if row]}


def send(title, lines=(), link=None, button="자세히 보기", photo=None, buttons=None, keyboard=None):
    """제목(굵게) + 본문 + 버튼. photo가 있으면 사진과 함께.
    buttons: [[{"text": "..", "url": ".."} 또는 {"text": "..", "cb": "콜백값"}], ...]  /  keyboard: 채팅창 아래 고정 버튼"""
    text = f"<b>{_esc(title)}</b>"
    body, cut = [], False
    for l in lines:
        if l is None or l is False:
            continue
        piece = _esc(l)
        if len(text) + sum(len(x) + 1 for x in body) + len(piece) > TEXT_LIMIT:
            cut = True
            break
        body.append(piece)
    while body and not body[-1]:
        body.pop()
    if body:
        text += "\n\n" + "\n".join(body) + ("\n…" if cut else "")
    rows = list(buttons or []) + ([[{"text": button, "url": link}]] if link else [])
    markup = keyboard or (_inline(rows) if rows else None)

    if not TOKEN or not CHAT:
        print("[알림 미발송: 텔레그램 설정 없음]\n" + text + (f"\n-> {link}" if link else ""))
        return

    if photo and len(text) <= 1000:
        if tg("sendPhoto", {"chat_id": CHAT, "photo": photo, "caption": text,
                            "parse_mode": "HTML", "reply_markup": markup}, quiet=True):
            return
        print("[telegram] 사진 전송 실패, 글자로 다시 보냄")
    tg("sendMessage", {"chat_id": CHAT, "text": text, "parse_mode": "HTML",
                       "reply_markup": markup, "disable_web_page_preview": True})


def print_chat_ids():
    """CHAT_ID를 아직 모를 때: 봇에게 말을 건 채팅 목록을 로그에 출력."""
    if not TOKEN:
        print("TELEGRAM_BOT_TOKEN 시크릿이 비어 있어요. README 3단계를 확인하세요.")
        return
    chats = {}
    for u in tg("getUpdates", quiet=True).get("result", []):
        msg = u.get("message") or u.get("my_chat_member") or {}
        c = msg.get("chat") or {}
        if c.get("id"):
            chats[c["id"]] = c.get("first_name") or c.get("title") or c.get("username") or ""
    if not chats:
        print("봇과 대화한 기록이 없어요. 텔레그램에서 내 봇을 열고 [시작]을 누른 뒤 다시 실행하세요.")
    for cid, name in chats.items():
        print(f"===> TELEGRAM_CHAT_ID 후보: {cid}  ({name})")
