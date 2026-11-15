"""텔레그램에서 온 요청 처리: 선택 화면이 보낸 내용, 채팅창 버튼, 삭제 버튼."""
import json
import os
import traceback

import requests

from . import megabox, notify, watch
from . import state as st

BTN_PICK = "🎬 영화·자리 고르기"
BTN_LIST = "📋 감시 목록"
BTN_REFRESH = "🔄 시간표 새로고침"
CODE_PREFIX = "#감시"
MAX_MOVIES = 20
_MENU_SENT = False   # 이번 실행에서 메뉴를 이미 보냈는지 (같은 안내를 두 번 보내지 않으려고)


def page_url(cfg):
    """선택 화면 주소 (GitHub Pages). config.yaml의 page_url이 있으면 그걸 씀."""
    url = str(cfg.get("page_url") or os.environ.get("PAGE_URL") or "").strip()
    if not url:
        repo = os.environ.get("GITHUB_REPOSITORY", "")
        if "/" in repo:
            owner, name = repo.split("/", 1)
            url = f"https://{owner.lower()}.github.io/{name}/"
    return url


def _menu(url):
    rows = [[{"text": BTN_PICK, "web_app": {"url": url}}]] if url else []
    rows.append([{"text": BTN_LIST}, {"text": BTN_REFRESH}])
    return {"keyboard": rows, "resize_keyboard": True, "is_persistent": True}


def send_menu(cfg, s, title="🎬 영화 알리미 메뉴", lines=None):
    """채팅창 아래 고정 버튼을 보냄. 선택 화면이 열린 게 확인된 뒤에만 [고르기] 버튼을 넣음."""
    global _MENU_SENT
    url = s.get("menu_url") or ""
    if lines is None:
        lines = ([f"{BTN_PICK}: 영화관·날짜·영화·회차를 골라 자리 감시 걸기"] if url else
                 ["⚠️ 선택 화면이 아직 안 열려요 → 저장소 Settings → Pages에서 켜 주세요 (README 5단계)"])
        lines += [
            f"{BTN_LIST}: 지금 걸어 둔 감시 보기·지우기",
            f"{BTN_REFRESH}: 선택 화면의 시간표를 지금 다시 받기",
            "",
            "버튼은 채팅 입력창 아래에 있어요. 안 보이면 입력창 옆의 네모 4개 모양 아이콘을 눌러 봐.",
        ]
    notify.send(title, lines, keyboard=_menu(url))
    _MENU_SENT = True


def ensure_menu(cfg, s, summary, debug=False):
    """선택 화면(Pages)이 열려 있으면 채팅창 버튼을 한 번 보내 둠. 아직이면 안내."""
    url = page_url(cfg)
    if debug or not url or s.get("menu_url") == url:
        return
    try:
        ok = requests.get(url, timeout=10).status_code == 200
    except requests.RequestException:
        ok = False
    if ok:
        s["menu_url"] = url
        send_menu(cfg, s, "🎬 선택 화면 준비 완료", [
            f"아래 [{BTN_PICK}] 버튼을 누르면 선택 화면이 열려요.",
            "영화관 → 날짜 → 영화 → 회차 → 자리 조건을 고르고 보내면 그 회차를 지켜보다가 자리가 나면 알려줄게요.",
        ])
        s.pop("menu_warned", None)
    elif not s.get("menu_warned"):
        s["menu_warned"] = True
        summary.append("⚠️ 선택 화면이 아직 안 열려요 → 저장소 Settings → Pages에서 켜 주세요 (README 5단계). "
                       "켜지면 채팅창에 버튼을 보내 줄게요.")


def send_list(s, cfg):
    ws = s.get("watches") or []
    lines, rows = [], []
    if ws:
        lines.append(f"💺 자리 감시 {len(ws)}건")
        for i, w in enumerate(ws, 1):
            if w.get("ok"):
                state = "지금 자리 있음"
            elif w.get("rest") == 0:
                state = "매진"
            elif w.get("ok") is False:
                state = f"맞는 자리 없음, 전체 잔여 {w['rest'] if w.get('rest') is not None else '?'}석"
            else:
                state = "확인 전"
            lines.append(f"{i}. {watch.one_line(w)} ({state})")
        btns = [{"text": f"🗑 {i}번", "cb": f"del:{w['id']}"} for i, w in enumerate(ws, 1)]
        rows = [btns[i:i + 4] for i in range(0, len(btns), 4)]
    else:
        lines.append(f"걸어 둔 자리 감시가 없어요. [{BTN_PICK}] 버튼으로 추가해 봐.")
    movies = megabox.interest_movies(cfg, s)
    lines += ["", "🎬 관심 영화(예매 오픈·특전 알림): " + (", ".join(movies) if movies else "없음")]
    notify.send("📋 감시 목록", lines, buttons=rows)


def _titles(v):
    out = []
    for x in v if isinstance(v, list) else []:
        x = " ".join(str(x).split())[:40]
        if x and megabox.norm(x) not in {megabox.norm(y) for y in out}:
            out.append(x)
    return out


def apply_payload(page, cfg, s, data):
    """선택 화면에서 보낸 변경사항 적용 → 결과를 한 메시지로 답함"""
    if not isinstance(data, dict):
        raise ValueError("요청 형식이 이상해요")
    lines, problems = [], []

    gone = watch.remove(s, [str(x) for x in data.get("del") or []][:50])
    if gone:
        lines += [f"🗑 감시 삭제 {len(gone)}건"] + [f"  • {watch.one_line(w)}" for w in gone]

    movies = list(s.get("movies") or [])
    plus, minus = _titles(data.get("mv+")), _titles(data.get("mv-"))
    if minus:
        drop = {megabox.norm(x) for x in minus}
        removed = [m for m in movies if megabox.norm(m) in drop]
        movies = [m for m in movies if megabox.norm(m) not in drop]
        if removed:
            lines.append("🎬 관심 영화 삭제: " + ", ".join(removed))
    fresh = [m for m in plus if megabox.norm(m) not in {megabox.norm(x) for x in megabox.interest_movies(cfg, {"movies": movies})}]
    if fresh:
        room = max(0, MAX_MOVIES - len(movies))
        movies += fresh[:room]
        if fresh[:room]:
            lines.append("🎬 관심 영화 추가: " + ", ".join(fresh[:room]))
            lines.append("  → 새 날짜 예매가 열리거나 특전 이벤트가 올라오면 알려줄게요.")
        if fresh[room:]:
            problems.append(f"관심 영화는 최대 {MAX_MOVIES}개까지예요.")
    s["movies"] = movies

    raw_items = data.get("add") if isinstance(data.get("add"), list) else []
    items = [x for x in (watch.clean_item(it) for it in raw_items[:20]) if x]
    if len(items) < len(raw_items):
        problems.append("일부 감시 요청은 형식이 이상해서 건너뛰었어요.")
    if items:
        added, dup, more = watch.add(page, s, items, st.load_json("catalog.json"))
        problems += more
        if added:
            lines.append(f"💺 자리 감시 추가 {len(added)}건")
            for w in added:
                rest = f"지금 잔여 {w['rest']}석" if w.get("rest") is not None else "잔여석 확인 전"
                lines.append(f"  • {watch.one_line(w)} ({rest})")
            lines.append("  → 조건에 맞는 자리가 생기면 바로 알려줄게요.")
        if dup:
            lines.append(f"이미 같은 조건으로 감시 중: {len(dup)}건")

    if problems:
        lines += [""] + [f"⚠️ {p}" for p in dict.fromkeys(problems)]
    if not lines:
        lines = ["바뀐 내용이 없어요."]
    notify.send("✅ 요청 처리 완료", lines, buttons=[[{"text": BTN_LIST, "cb": "list"}]])


def _parse(text):
    text = (text or "").strip()
    if text.startswith(CODE_PREFIX):
        text = text[len(CODE_PREFIX):].strip()
    return json.loads(text)


def _handle(page, cfg, s, u):
    cq, msg = u.get("callback_query"), u.get("message")
    if cq:
        chat = ((cq.get("message") or {}).get("chat") or {}).get("id") or (cq.get("from") or {}).get("id")
        if str(chat) != notify.CHAT:
            return
        data, note = str(cq.get("data") or ""), ""
        if data.startswith("del:"):
            gone = watch.remove(s, [data[4:]])
            note = "감시를 끝냈어요" if gone else "이미 끝난 감시예요"
            if gone:
                notify.send("🗑 감시 삭제", [f"• {watch.one_line(w)}" for w in gone])
        elif data == "list":
            send_list(s, cfg)
        notify.tg("answerCallbackQuery", {"callback_query_id": cq.get("id"), "text": note or None}, quiet=True)
        return

    if not msg or str((msg.get("chat") or {}).get("id")) != notify.CHAT:
        return                                   # 내 채팅방이 아닌 곳에서 온 건 무시
    wad, text = msg.get("web_app_data"), (msg.get("text") or "").strip()
    if wad:
        apply_payload(page, cfg, s, _parse(wad.get("data")))
    elif text.startswith(CODE_PREFIX) or (text.startswith("{") and '"v"' in text):
        apply_payload(page, cfg, s, _parse(text))
    elif text in (BTN_LIST, "/list", "목록"):
        send_list(s, cfg)
    elif text in (BTN_REFRESH, "/refresh", "새로고침"):
        s["catalog_req"] = True
        notify.send("🔄 시간표를 다시 받는 중", ["1~2분 뒤에 선택 화면을 다시 열면 최신 시간표가 보여요."])
    elif text and not _MENU_SENT:
        send_menu(cfg, s)


def process(page, cfg, s, summary, debug=False):
    """텔레그램에 쌓인 요청을 읽어서 처리 (내 채팅방에서 온 것만)."""
    if debug or not (notify.TOKEN and notify.CHAT):
        return
    res = notify.tg("getUpdates", {"offset": int(s.get("tg_offset") or 0), "timeout": 0,
                                   "allowed_updates": ["message", "callback_query"]})
    updates = res.get("result") or []
    for u in updates:
        s["tg_offset"] = max(int(s.get("tg_offset") or 0), int(u.get("update_id", 0)) + 1)
        try:
            _handle(page, cfg, s, u)
        except Exception as e:  # noqa: BLE001 - 요청 하나가 잘못돼도 나머지는 계속
            traceback.print_exc()
            notify.send("⚠️ 요청을 처리하지 못했어요", [f"{type(e).__name__}: {str(e)[:200]}", "", "선택 화면에서 다시 보내 줘."])
    if updates:
        print(f"[텔레그램] 요청 {len(updates)}건 처리")
