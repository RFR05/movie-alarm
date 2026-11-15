"""자리 감시: 선택 화면에서 고른 회차에, 조건에 맞는 자리가 생기면 알림."""
import datetime as dt
import re

from . import megabox, notify, seats

MAX_WATCHES = 20
COOLDOWN = dt.timedelta(minutes=15)   # 같은 감시는 15분 안에 다시 알리지 않음 (결제 중 좌석이 들락날락할 때 대비)
MISS_LIMIT = dt.timedelta(minutes=20)  # 회차가 시간표에서 이만큼 계속 안 보이면 감시를 끝냄 (잠깐 빠진 건 봐줌)
MODE_LABEL = {"any": "아무 자리", "zone": "좋은 자리", "pick": "지정 좌석"}
SEAT_RE = re.compile(r"^[0-9A-Za-z가-힣]{1,6}$")


def cond_text(w):
    label = MODE_LABEL.get(w.get("k"), "아무 자리")
    if w.get("k") == "pick":
        label += f" {len(w.get('seats') or [])}곳 중"
    return f"{label} {w['n']}석" + (" 연석" if w["n"] >= 2 and w.get("adj") else "")


def when_text(w):
    return f"{megabox.fmt_day(w['d'])} {w['t']}" + (f" {w['hall']}" if w.get("hall") else "")


def one_line(w):
    return f"{w['mn']} · {w['bn']} · {when_text(w)} — {cond_text(w)}"


def start_dt(w):
    """상영 시작 시각. 심야 회차는 24:30처럼 올 수 있어서 시(hour)를 그대로 더함."""
    day = dt.datetime.strptime(w["d"], "%Y%m%d").replace(tzinfo=megabox.KST)
    m = re.match(r"(\d{1,2}):(\d{2})", w.get("t") or "")
    return day + dt.timedelta(hours=int(m.group(1)), minutes=int(m.group(2))) if m else day + dt.timedelta(days=1)


def active(s):
    return bool(s.get("watches"))


def _same(a, b):
    return (a["show"] == b["show"] and a["k"] == b["k"] and a["n"] == b["n"] and bool(a.get("adj")) == bool(b.get("adj"))
            and sorted(a.get("seats") or []) == sorted(b.get("seats") or []))


def clean_item(it):
    """선택 화면에서 온 감시 요청 한 덩어리를 검사해서 정리. 이상하면 None."""
    if not isinstance(it, dict):
        return None
    ids = [str(x) for x in (it.get("ss") or []) if re.fullmatch(r"\d{6,20}", str(x))][:60]
    k = it.get("k") if it.get("k") in MODE_LABEL else "any"
    try:
        n = min(8, max(1, int(it.get("n") or 1)))
    except (TypeError, ValueError):
        n = 1
    picked = sorted({str(x) for x in (it.get("seats") or []) if SEAT_RE.match(str(x))})[:400]
    if not ids or (k == "pick" and not picked):
        return None
    return {"ss": ids, "k": k, "n": n, "adj": bool(it.get("adj")) and n >= 2, "seats": picked if k == "pick" else []}


def add(page, s, items, catalog):
    """감시 추가 → (추가된 감시들, 이미 있던 감시들, 문제 문구들)"""
    watches = s.setdefault("watches", [])
    cat = {str(r[0]): r for r in (catalog.get("shows") or []) if isinstance(r, list) and len(r) >= 3}
    fresh, added, dup, problems = {}, [], [], []
    now = megabox.now()

    def lookup(sid):
        row = cat.get(sid)
        if row:
            b, d = str(row[1]), str(row[2])
        elif re.fullmatch(r"\d{13}", sid):      # 회차 번호 = 날짜(YYMMDD) + 지점(4) + 순번(3)
            b, d = sid[6:10], "20" + sid[:6]
        else:
            return None
        if (b, d) not in fresh:
            try:
                fresh[(b, d)] = {x["id"]: x for x in megabox.fetch_shows(page, b, d)[0]}
            except RuntimeError as e:
                print("[감시] 회차 확인 실패:", e)
                fresh[(b, d)] = None
        live = fresh[(b, d)]
        if live is not None:
            return live.get(sid)
        if row and len(row) >= 11:              # 사이트가 잠깐 안 될 땐 받아 둔 목록의 정보로
            movie = (catalog.get("movies") or {}).get(str(row[3])) or {}
            return {"id": sid, "b": b, "d": d, "m": str(row[3]), "rpst": str(row[3]), "mn": movie.get("n", ""),
                    "t": row[4], "e": row[5], "hall": row[6], "hn": row[7], "kind": row[8],
                    "rest": row[9], "tot": row[10], "bn": ""}
        return None

    names = {b["no"]: b["name"] for b in (s.get("branches") or {}).get("list") or []}
    for it in items:
        for sid in it["ss"]:
            sh = lookup(sid)
            if not sh:
                problems.append(f"회차 {sid}: 시간표에서 못 찾았어요 (이미 지났거나 취소된 회차)")
                continue
            w = {"show": sid, "b": sh["b"], "bn": sh.get("bn") or names.get(sh["b"], ""), "d": sh["d"],
                 "t": sh["t"], "e": sh.get("e") or "", "m": sh.get("m") or "", "rpst": sh.get("rpst") or sh.get("m") or "",
                 "mn": sh.get("mn") or "", "hall": sh.get("hall") or "", "hn": sh.get("hn") or "",
                 "kind": sh.get("kind") or "", "n": it["n"], "k": it["k"], "adj": it["adj"], "seats": it["seats"],
                 "rest": sh.get("rest"), "tot": sh.get("tot")}
            if start_dt(w) <= now:
                problems.append(f"{w['mn']} {when_text(w)}: 이미 시작한 회차예요")
                continue
            if any(_same(x, w) for x in watches):
                dup.append(w)
                continue
            if len(watches) >= MAX_WATCHES:
                problems.append(f"감시는 최대 {MAX_WATCHES}개까지예요. 안 쓰는 감시를 지우고 다시 등록해 주세요.")
                return added, dup, problems
            s["watch_seq"] = int(s.get("watch_seq", 0)) + 1
            w.update({"id": f"w{s['watch_seq']}", "created": now.isoformat(timespec="seconds"),
                      "ok": None, "notified": False})
            if sh.get("poster"):
                s.setdefault("posters", {})[w["m"]] = sh["poster"]
            watches.append(w)
            added.append(w)
    return added, dup, problems


def remove(s, ids):
    ids = set(ids)
    gone = [w for w in s.get("watches") or [] if w["id"] in ids]
    s["watches"] = [w for w in s.get("watches") or [] if w["id"] not in ids]
    return gone


def public(s, cfg):
    """선택 화면에 보여줄 감시 목록 (watchlist.json)"""
    keys = ("id", "show", "b", "bn", "d", "t", "e", "m", "mn", "hall", "kind", "n", "k", "adj", "rest", "tot", "ok", "free")
    return {
        "max": MAX_WATCHES,
        "watches": [{**{k: w.get(k) for k in keys}, "cond": cond_text(w), "picked": len(w.get("seats") or [])}
                    for w in s.get("watches") or []],
        "movies": list(s.get("movies") or []),
        "fixed": [str(m) for m in (cfg.get("movies") or []) if m],
    }


def _alert(s, w, res):
    if res.get("rough"):
        seat_line = f"🪑 잔여 {res['free']}석 (좌석 위치는 확인 못 함, 장애인석일 수도 있어요)"
    elif w["k"] == "any" and len(res["seats"]) > 12:
        seat_line = f"🪑 고를 수 있는 자리 {res['free']}석"
    else:
        seat_line = f"🪑 {seats.fmt_seats(res['seats'])}"
    lines = [
        f"🎬 {w['mn']}" + (f" ({w['kind']})" if w.get("kind") else ""),
        f"🕒 {megabox.fmt_day(w['d'])} {w['t']}" + (f"~{w['e']}" if w.get("e") else "") + (f" · {w['hall']}" if w.get("hall") else ""),
        seat_line,
        f"조건: {cond_text(w)}",
        "",
        "취소표는 금방 나가니까 서둘러!",
    ]
    notify.send(
        f"💺 자리 생김 | 메가박스 {w['bn']}", lines,
        buttons=[[{"text": "지금 예매하기", "url": f"{megabox.BASE}/movie-detail?rpstMovieNo={w.get('rpst') or w['m']}"}],
                 [{"text": "🔕 이 감시 끝내기", "cb": f"del:{w['id']}"}]],
        photo=megabox.abs_img(s.get("posters", {}).get(w["m"]) or ""),
    )


def _check(page, s, w, sh, now, debug, seat_errors):
    rest = sh.get("rest")
    w.update({"rest": rest, "tot": sh.get("tot"), "t": sh["t"] or w["t"], "e": sh.get("e") or w.get("e", "")})
    if sh.get("hall"):
        w["hall"] = sh["hall"]
    res = None
    if rest == 0:
        res = {"ok": False, "seats": [], "free": 0}       # 잔여 0이면 좌석 배치도까지 볼 필요 없음
    else:
        try:
            sm = megabox.fetch_seatmap(page, w["show"])
            info = sm["info"]
            if info.get("poster") and w.get("m"):
                s.setdefault("posters", {})[w["m"]] = info["poster"]
            if info.get("hall") and not w.get("hall"):
                w["hall"] = info["hall"]
            res = seats.evaluate(w, sm)
            if debug:
                print(f"[디버그] 좌석 {w['id']} {one_line(w)}: 상태 {sm['stats']}, 사이트 선택가능 {sm['choi']}, "
                      f"조건 충족 {res['ok']} ({seats.fmt_seats(res['seats'], 10)})")
        except RuntimeError as e:
            seat_errors.append(e)
            print(f"[감시] {w['id']} 좌석 배치도 실패: {e}")
            if w["k"] == "any" and rest is not None:   # 배치도가 안 되면 잔여석 수로라도 판단
                res = {"ok": rest >= w["n"], "seats": [], "free": rest, "rough": True}
    if res is None:
        return
    w.update({"ok": res["ok"], "free": res["free"], "checked": now.isoformat(timespec="seconds")})
    if not res["ok"]:
        w["notified"] = False
        return
    if w.get("notified"):
        return
    last = w.get("alerted")
    if last and now - dt.datetime.fromisoformat(last) < COOLDOWN:
        return                                             # 쿨다운 중 → 다음 확인 때 다시 봄
    if not debug:
        _alert(s, w, res)
    w["notified"], w["alerted"] = True, now.isoformat(timespec="seconds")


def run(page, cfg, s, summary, debug=False):
    watches = s.get("watches") or []
    if not watches:
        return
    now = megabox.now()
    keep, ended, vanished, errors, seat_errors = [], [], [], [], []
    groups = {}
    for w in watches:
        groups.setdefault((w["b"], w["d"]), []).append(w)

    for (b, d), ws in groups.items():
        live = [w for w in ws if start_dt(w) > now]
        ended += [w for w in ws if start_dt(w) <= now]
        if not live:
            continue
        try:
            by_id = {x["id"]: x for x in megabox.fetch_shows(page, b, d)[0]}
        except RuntimeError as e:
            errors.append(e)
            keep += live
            continue
        for w in live:
            sh = by_id.get(w["show"])
            if not sh:
                since = w.setdefault("miss_since", now.isoformat(timespec="seconds"))
                (vanished if now - dt.datetime.fromisoformat(since) >= MISS_LIMIT else keep).append(w)
                continue
            w.pop("miss_since", None)
            try:
                _check(page, s, w, sh, now, debug, seat_errors)
            finally:
                keep.append(w)

    s["watches"] = sorted(keep, key=lambda w: (w["d"], w["t"], w["id"]))
    print(f"[감시] 자리 감시 {len(keep)}건 확인" + (f", 종료 {len(ended) + len(vanished)}건" if ended or vanished else ""))
    if not debug:
        if ended:
            notify.send("⏰ 상영이 시작돼서 감시를 끝냈어요", [f"• {one_line(w)}" for w in ended])
        if vanished:
            notify.send("🚫 시간표에서 사라진 회차", [f"• {one_line(w)}" for w in vanished] + ["", "감시를 끝냈어요."])
    if errors:
        raise errors[0]
    if seat_errors:
        raise RuntimeError(f"좌석 배치도를 못 읽었어요 ({len(seat_errors)}건): {seat_errors[0]}")
