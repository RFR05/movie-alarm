"""메가박스 사이트에서 정보 읽기: 이벤트, 지점, 상영 회차, 좌석 배치 + 예매 오픈 감시."""
import datetime as dt
import html
import json
import os
import re

from playwright.sync_api import TimeoutError as PWTimeout

from . import notify, seats

BASE = os.environ.get("MEGABOX_BASE", "https://www.megabox.co.kr").rstrip("/")
IMG_BASE = os.environ.get("MEGABOX_IMG_BASE", "https://img.megabox.co.kr").rstrip("/")
DELAY_MS = int(os.environ.get("MEGABOX_DELAY_MS", "250"))   # 요청 사이 쉬는 시간 (사이트에 부담 안 주려고)

P_BOKD = "/on/oh/ohb/SimpleBooking/selectBokdList.do"       # 빠른예매: 지점·영화 목록, 열린 날짜
P_SCHED = "/on/oh/ohc/Brch/schedulePage.do"                 # 극장별 상영시간표: 회차 + 잔여석
P_SEATS = "/on/oh/ohz/PcntSeatChoi/selectSeatList.do"       # 회차별 좌석 배치도 + 좌석 상태
ORIGIN_PAGES = ("/on/oh/ohb/SimpleBooking/simpleBookingPage.do", "/booking", "/")
AREA_CODES = ("55", "10", "30", "35", "45", "65", "70", "27", "47", "48", "26", "11", "41")  # 55 = 부산/대구/경상

KST = dt.timezone(dt.timedelta(hours=9))
WEEK = "월화수목금토일"
DEBUG = False


def now():
    fake = os.environ.get("ALARM_NOW")      # 테스트할 때만 쓰는 '지금 시각' 고정 값
    return dt.datetime.fromisoformat(fake).astimezone(KST) if fake else dt.datetime.now(KST)


def today():
    return now().strftime("%Y%m%d")


def norm(s):
    """공백 무시 + 소문자: '아트 그라피' == '아트그라피'"""
    return re.sub(r"\s+", "", str(s or "")).lower()


def clean(s):
    """사이트가 이름을 &#40; 같은 HTML 표기로 줄 때가 있어서 풀어 줌."""
    return html.unescape(str(s if s is not None else "")).strip()


def abs_img(src):
    if not src or src.startswith("data:"):
        return None
    if src.startswith("//"):
        return "https:" + src
    if src.startswith("/"):
        return IMG_BASE + src
    return src if src.startswith("http") else None


def fmt_day(d):
    x = dt.datetime.strptime(d, "%Y%m%d")
    return f"{x.month}/{x.day}({WEEK[x.weekday()]})"


def hhmm(t):
    d = re.sub(r"\D", "", str(t or ""))
    return f"{d[:2]}:{d[2:4]}" if len(d) >= 4 else str(t or "")


def to_int(v):
    return int(v) if str(v if v is not None else "").strip().isdigit() else None


def interest_movies(cfg, s):
    """관심 영화 = config.yaml의 movies + 선택 화면에서 추가한 영화"""
    out = []
    for m in list(cfg.get("movies") or []) + list(s.get("movies") or []):
        m = str(m or "").strip()
        if m and norm(m) not in {norm(x) for x in out}:
            out.append(m)
    return out


# ───────────────────────── 이벤트 ─────────────────────────

EVENT_JS = r"""() => {
  const out = [], seen = new Set();
  const isDate = s => /\d{4}[.\-]\d{1,2}[.\-]\d{1,2}/.test(s);
  for (const a of document.querySelectorAll('a[href*="eventNo="], [data-no]')) {
    const m = (a.getAttribute('href') || '').match(/eventNo=(\d+)/);
    let no = m ? m[1] : (/^\d+$/.test(a.dataset.no || '') ? a.dataset.no : null);
    if (!no || seen.has(no)) continue;
    if (!m && !a.closest('[class*="event"], [id*="event"]')) continue;   // data-no는 이벤트 목록 안에 있을 때만
    seen.add(no);
    const img = a.querySelector('img');
    const tEl = a.querySelector('.tit, .title, strong');
    const lines = (a.innerText || '').split('\n').map(s => s.trim()).filter(Boolean);
    out.push({
      no,
      title: (tEl && tEl.innerText.trim()) || a.getAttribute('title') || lines.find(s => !isDate(s)) || (img && img.alt) || '',
      period: lines.find(isDate) || '',
      alt: img ? (img.alt || '') : '',
      img: img ? (img.currentSrc || img.src || img.dataset.src || '') : '',
    });
  }
  return out;
}"""


def fetch_events(page, paths, debug=False):
    found = {}
    for path in paths:
        url = path if path.startswith("http") else BASE + path
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_selector('a[href*="eventNo="], [class*="event"] [data-no]', timeout=15000)
        except PWTimeout:
            print(f"[메가박스] {url}: 이벤트 목록을 못 찾음")
            if debug:
                print(page.content()[:3000])
            continue
        page.wait_for_timeout(1500)  # 목록이 다 그려질 때까지 잠깐
        items = page.evaluate(EVENT_JS)
        print(f"[메가박스] {url}: 이벤트 {len(items)}개")
        for ev in items:
            ev["link"] = f"{BASE}/event/detail?eventNo={ev['no']}"
            found.setdefault(ev["no"], ev)
    if not found:
        raise RuntimeError("메가박스 이벤트 목록을 하나도 못 읽었어요 (사이트 구조가 바뀌었을 수 있음)")
    if debug:
        for ev in list(found.values())[:15]:
            print("  ", ev)
    return found


def run_events(page, cfg, s, summary, debug=False):
    mcfg = cfg.get("megabox") or {}
    keywords = [k for k in (cfg.get("event_keywords") or []) if k]
    movies = interest_movies(cfg, s) if cfg.get("event_include_movies", True) else []
    events = fetch_events(page, mcfg.get("event_pages") or ["/event/movie", "/event"], debug)

    def why(ev):
        text = norm(ev["title"] + " " + ev["alt"])
        for k in keywords:
            if norm(k) in text:
                return k
        for m in movies:
            if norm(m) in text:
                return m
        return None

    seen = set(s.get("mb_events_seen", []))
    init = s.setdefault("init", {})
    hits = [(ev, why(ev)) for ev in events.values()]
    hits = [(ev, w) for ev, w in hits if w]

    if not init.get("mb_events"):
        summary.append(f"메가박스 이벤트 {len(events)}개 확인, 관심 이벤트 {len(hits)}개 진행 중")
        summary += [f"  • {ev['title']}" for ev, _ in hits[:10]]
        init["mb_events"] = True
    else:
        for ev, w in hits:
            if ev["no"] in seen:
                continue
            art = any(x in norm(w) for x in ("아트그라피", "아트그래피", "artgraphy"))
            notify.send(
                f"{'🖼️' if art else '🎁'} 메가박스 새 이벤트 | {w}",
                [ev["title"], f"🗓 {ev['period']}" if ev["period"] else ""],
                link=ev["link"], button="이벤트 보러 가기", photo=abs_img(ev["img"]),
            )
    seen |= set(events)
    s["mb_events_seen"] = sorted(seen, key=int)[-600:]


# ───────────────────────── 사이트 API 공통 ─────────────────────────

FETCH_JS = """async ([url, body]) => {
  const r = await fetch(url, {
    method: 'POST', credentials: 'include',
    headers: {'Content-Type': 'application/json; charset=UTF-8',
              'Accept': 'application/json, text/javascript, */*; q=0.01',
              'X-Requested-With': 'XMLHttpRequest'},
    body: JSON.stringify(body)});
  return {status: r.status, text: await r.text()};
}"""


def ensure_origin(page):
    """API는 메가박스 페이지 안에서 불러야 해서(같은 사이트 요청), 예매 페이지를 먼저 열어 둠."""
    if page.url.startswith(BASE + ORIGIN_PAGES[0]) or page.url.rstrip("/") == BASE + "/booking":
        return
    last = None
    for path in ORIGIN_PAGES:
        try:
            page.goto(BASE + path, wait_until="domcontentloaded", timeout=30000)
            return
        except Exception as e:  # noqa: BLE001 - 다음 주소로 다시 시도
            last = e
    raise RuntimeError(f"메가박스 접속 실패: {str(last).splitlines()[0][:150]}")


def post(page, path, body, tries=2):
    ensure_origin(page)
    name = path.rsplit("/", 1)[-1]
    err = None
    for attempt in range(tries):
        try:
            res = page.evaluate(FETCH_JS, [BASE + path, body])
            if res["status"] == 200:
                try:
                    data = json.loads(res["text"].lstrip("﻿"))
                except json.JSONDecodeError:
                    raise RuntimeError(f"메가박스가 JSON이 아닌 답을 보냄 ({name}): {res['text'][:120]!r}") from None
                if DELAY_MS:
                    page.wait_for_timeout(DELAY_MS)
                return data
            err = RuntimeError(f"메가박스 응답 {res['status']} ({name})")
        except RuntimeError as e:
            err = e
        except Exception as e:  # noqa: BLE001 - 네트워크 오류 등
            err = RuntimeError(f"메가박스 요청 실패 ({name}): {str(e).splitlines()[0][:150]}")
        if attempt + 1 < tries:
            page.wait_for_timeout(800)
    raise err


def payload(play_de, brch=None, movie_no=None, area="", hall=""):
    """빠른예매 화면이 보내는 요청과 같은 모양. hall은 특별관 코드(예: 돌비시네마 'DBC')."""
    return {
        "arrMovieNo": movie_no or "", "playDe": play_de,
        "brchNoListCnt": 1 if brch else 0, "brchNo1": brch or "", "brchNo2": "", "brchNo3": "",
        "areaCd1": hall or area or "", "areaCd2": "", "areaCd3": "",
        "spclbYn1": ("Y" if hall else "N") if brch else "", "spclbYn2": "", "spclbYn3": "",
        "theabKindCd1": hall or "", "theabKindCd2": "", "theabKindCd3": "",
        "brchAll": "", "brchSpcl": hall or "",
        "movieNo1": movie_no or "", "movieNo2": "", "movieNo3": "", "sellChnlCd": "",
    }


def walk(o):
    if isinstance(o, dict):
        yield o
        for v in o.values():
            yield from walk(v)
    elif isinstance(o, list):
        for v in o:
            yield from walk(v)


def find_objs(data, name_key, id_keys):
    """응답 JSON 어디에 있든 name_key와 id_key를 가진 객체를 모음 (응답 구조가 조금 바뀌어도 버팀)."""
    out, seen = [], set()
    for o in walk(data):
        if o.get(name_key) and any(o.get(k) for k in id_keys):
            key = (o[name_key], next(o.get(k) for k in id_keys if o.get(k)))
            if key not in seen:
                seen.add(key)
                out.append(o)
    return out


def find_dates(data, strict=False):
    """열린 날짜 목록(movieFormDeList). strict=False면 목록이 없을 때 응답 전체에서 playDe를 긁어 봄."""
    lst = data.get("movieFormDeList") if isinstance(data, dict) else None
    if not isinstance(lst, list):
        if strict:
            return []
        lst = list(walk(data))
    return sorted({str(o["playDe"]) for o in lst if isinstance(o, dict) and re.fullmatch(r"\d{8}", str(o.get("playDe", "")))})


def poster_of(movie):
    for v in movie.values():
        if isinstance(v, str) and re.search(r"\.(jpe?g|png|webp)(\?|$)", v, re.I):
            return abs_img(v)
    return None


_DUMPED = set()


def debug_dump(data, label, once=None):
    """debug 모드에서 응답 모양을 로그에 남김 (once를 주면 같은 종류는 한 번만)."""
    if once:
        if once in _DUMPED:
            return
        _DUMPED.add(once)
    print(f"[디버그] {label} 최상위 키:", list(data.keys()) if isinstance(data, dict) else type(data))
    if isinstance(data, dict):
        for k, v in data.items():
            if isinstance(v, list) and v:
                print(f"  {k}: {len(v)}개, 첫 항목 = {json.dumps(v[0], ensure_ascii=False)[:1500]}")
            elif isinstance(v, dict) and v:
                print(f"  {k}: 키 {list(v.keys())[:40]}")


# ───────────────────────── 지점·영화 목록 ─────────────────────────

def base_list(page):
    """오늘 기준 전체 목록 → (영화 목록, 지점 목록, 원본 응답)"""
    data = post(page, P_BOKD, payload(today()))
    if DEBUG:
        debug_dump(data, "빠른예매 기본 조회")
    return find_objs(data, "movieNm", ("movieNo", "rpstMovieNo")), _branches_in(data, ""), data


def _branches_in(data, area):
    out = {}
    for o in find_objs(data, "brchNm", ("brchNo",)):
        no = str(o["brchNo"])
        out.setdefault(no, {"no": no, "name": clean(o["brchNm"]), "area": str(o.get("areaCd") or area or "")})
    return out


def wanted_branches(cfg):
    """config.yaml megabox.branches → [(이름 일부, 특별관 코드)]"""
    out = []
    for entry in (cfg.get("megabox") or {}).get("branches") or ["대구"]:
        name, hall = (entry, "") if isinstance(entry, str) else (entry.get("name", ""), entry.get("hall", ""))
        if str(name).strip():
            out.append((str(name).strip(), str(hall or "")))
    return out


def all_branches(page, cfg, s, known=None):
    """전국 지점 목록. 하루에 한 번만 새로 받고 나머지는 저장해 둔 걸 씀.
    기본 조회에 원하는 지점이 없으면(지역별로만 주는 경우) 지역 코드를 바꿔 가며 찾음."""
    want = [n for n, _ in wanted_branches(cfg)]
    cache = s.get("branches") or {}
    if cache.get("day") == today() and cache.get("want") == want and cache.get("list"):
        return cache["list"]
    found = dict(known) if known is not None else base_list(page)[1]
    has = lambda name: any(norm(name) in norm(b["name"]) for b in found.values())
    for area in AREA_CODES:
        if all(has(n) for n in want):
            break
        body = payload(today(), area=area)
        body.update({"brchNoListCnt": 1, "spclbYn1": "N"})
        try:
            for no, b in _branches_in(post(page, P_BOKD, body), area).items():
                found.setdefault(no, b)
        except RuntimeError as e:
            print(f"[메가박스] 지역 {area} 지점 조회 실패: {e}")
    lst = sorted(found.values(), key=lambda b: b["name"])
    if lst:
        s["branches"] = {"day": today(), "want": want, "list": lst}
    return lst


def target_branches(page, cfg, s, known=None):
    """설정한 이름에 맞는 지점들 → ([(지점, 특별관 코드, 새로 추가된 설정인가)], [못 찾은 이름])"""
    branches = all_branches(page, cfg, s, known)
    seen_entries = set(s.get("mb_branch_entries", []))
    targets, missing = [], []
    for name, hall in wanted_branches(cfg):
        hits = [b for b in branches if norm(name) in norm(b["name"])]
        if hits:
            is_new = f"{norm(name)}|{hall}" not in seen_entries
            targets += [(b, hall, is_new) for b in hits]
        else:
            near = [b["name"] for b in branches if norm(name)[:2] in norm(b["name"])][:8]
            missing.append((name, near))
    return targets, missing


# ───────────────────────── 회차·좌석 ─────────────────────────

TIME_KEYS = ("playStartTime", "playStrtTime", "startTime")
END_KEYS = ("playEndTime", "endTime")
REST_KEYS = ("restSeatCnt", "rmnSeatCnt", "remainSeatCnt")
TOT_KEYS = ("totSeatCnt", "totSeatSum", "totalSeatCnt")
HALL_KEYS = ("theabExpoNm", "theabNm", "theabEngNm")
KIND_KEYS = ("playKindNm", "playKindName", "movieKindNm")


def _first(o, keys):
    for k in keys:
        if o.get(k) not in (None, ""):
            return o[k]
    return None


def parse_shows(rows, brch, day):
    out, seen = [], set()
    for o in rows:
        if not isinstance(o, dict):
            continue
        sid, start = str(o.get("playSchdlNo") or ""), _first(o, TIME_KEYS)
        if not sid or start is None or sid in seen:
            continue
        if o.get("brchNo") and str(o["brchNo"]) != str(brch):
            continue
        if re.fullmatch(r"\d{8}", str(o.get("playDe") or "")) and str(o["playDe"]) != day:
            continue
        seen.add(sid)
        hall_no = str(o.get("theabNo") or "")
        out.append({
            "id": sid, "b": str(brch), "bn": clean(o.get("brchNm")), "d": day,
            "m": str(o.get("movieNo") or o.get("rpstMovieNo") or ""),
            "rpst": str(o.get("rpstMovieNo") or o.get("movieNo") or ""),
            "mn": clean(o.get("movieNm")), "t": hhmm(start), "e": hhmm(_first(o, END_KEYS)),
            "rest": to_int(_first(o, REST_KEYS)), "tot": to_int(_first(o, TOT_KEYS)),
            "hall": clean(_first(o, HALL_KEYS)) or (f"{hall_no.lstrip('0') or hall_no}관" if hall_no else ""),
            "hn": hall_no, "kind": clean(_first(o, KIND_KEYS)),
            "poster": poster_of(o) or "",
        })
    return sorted(out, key=lambda x: (x["t"], x["hall"], x["id"]))


def fetch_shows(page, brch, day, area=""):
    """한 지점의 하루 치 회차 → (회차 목록, 그 지점에 일정이 있는 날짜들[응답에 있을 때만])"""
    err = None
    bodies = ({"masterType": "brch", "detailType": "area", "brchNo": brch, "brchNo1": brch,
               "firstAt": "N", "crtDe": today(), "playDe": day},          # 사이트 화면이 보내는 모양
              {"masterType": "brch", "brchNo1": brch, "playDe": day})     # 꼭 필요한 값만
    for body in bodies:
        try:
            data = post(page, P_SCHED, body)
            mm = data.get("megaMap") if isinstance(data, dict) else None
            rows = mm.get("movieFormList") if isinstance(mm, dict) else None
            if DEBUG and isinstance(mm, dict) and mm.get("movieFormList"):
                debug_dump(mm, f"상영시간표 {brch} {day}", once="sched")
            if isinstance(rows, list):
                return parse_shows(rows, brch, day), find_dates(mm, strict=True)
            err = RuntimeError("상영시간표 응답에 회차 목록(movieFormList)이 없음")
        except RuntimeError as e:
            err = e
    # 대체 경로: 빠른예매 API
    try:
        data = post(page, P_BOKD, payload(day, brch, None, area))
        rows = data.get("movieFormList") if isinstance(data, dict) else None
        if DEBUG and isinstance(data, dict):
            debug_dump(data, f"빠른예매 {brch} {day}", once="bokd-brch")
        if isinstance(rows, list):
            return parse_shows(rows, brch, day), find_dates(data, strict=True)
    except RuntimeError as e:
        err = RuntimeError(f"{err} / {e}")
    raise RuntimeError(f"회차 목록을 못 읽었어요 (지점 {brch}, {fmt_day(day)}): {err}")


def fetch_seatmap(page, show_id):
    """회차 하나의 좌석 배치도 + 좌석별 상태"""
    data = post(page, P_SEATS, {"playSchdlNo": str(show_id)})
    try:
        return seats.parse_seatmap(data)
    except ValueError as e:
        if DEBUG and isinstance(data, dict):
            debug_dump(data, f"좌석 {show_id}", once="seats")
        raise RuntimeError(str(e)) from None


# ───────────────────────── 예매 오픈 (관심 영화의 새 날짜) ─────────────────────────

def run_booking(page, cfg, s, summary, debug=False):
    want_movies = interest_movies(cfg, s)
    movies, known, _ = base_list(page)
    targets, missing = target_branches(page, cfg, s, known)
    print(f"[메가박스] 예매 목록 영화 {len(movies)}편, 지점 {len(s.get('branches', {}).get('list') or known)}곳, "
          f"감시 지점 {len(targets)}곳")
    if not known:
        raise RuntimeError("메가박스 지점 목록을 못 받았어요 (사이트 구조 확인 필요)")

    posters = {str(m.get("movieNo") or m.get("rpstMovieNo")): poster_of(m) for m in movies if poster_of(m)}
    if posters:      # 선택 화면과 알림에 쓸 포스터 주소 (지금 예매 목록에 있는 영화 것만 유지)
        s["posters"] = {**{k: v for k, v in (s.get("posters") or {}).items() if len(s.get("posters") or {}) < 400}, **posters}

    init = s.setdefault("init", {})
    first = not init.get("mb_booking")
    dates_state = s.setdefault("mb_dates", {})
    warned = set(s.get("mb_warned", []))
    # 새로 추가된 영화/지점은 첫 확인 때 '현황'으로만 알려주고, 그 뒤부터 새 날짜를 알림
    known_titles = set(s.get("mb_titles", []))

    for name, near in missing:
        if name in warned:
            continue
        warned.add(name)
        msg = f"⚠️ 메가박스에서 '{name}' 지점을 못 찾았어요." + (f" 비슷한 지점: {', '.join(near)}" if near else "")
        if first:
            summary.append(msg)
        else:
            notify.send("⚠️ 영화 알리미 설정 확인", [msg, "config.yaml의 지점 이름을 고쳐 주세요."])
    s["mb_warned"] = sorted(warned)

    day = today()
    opened = {}    # 새 날짜가 열린 영화: 영화 번호 → {"name", "rpst", "places": [(지점, 새 날짜들)]}
    status = {}    # 처음 보는 영화/지점의 현황: 영화 → {"open": [(지점, 날짜들)], "none": [지점]}
    for title in want_movies:
        new_title = norm(title) not in known_titles
        hits = [m for m in movies if norm(title) in norm(clean(m["movieNm"]))]
        if not hits:
            if first or new_title:
                summary.append(f"• {title}: 아직 메가박스 예매 목록에 없음 (열리면 알려줄게요)")
            continue
        for m in hits:
            mno = str(m.get("movieNo") or m.get("rpstMovieNo"))
            rpst = m.get("rpstMovieNo") or mno
            mname = clean(m["movieNm"])
            for b, hall, new_branch in targets:
                data = post(page, P_BOKD, payload(day, b["no"], mno, b.get("area", ""), hall))
                if debug:
                    debug_dump(data, f"{b['name']} × {mname}", once="bokd-movie")
                dates = [d for d in find_dates(data) if d >= day]
                key = f"{b['no']}|{hall}|{mno}"
                prev = dates_state.get(key)
                place = f"메가박스 {b['name']}" + (f" ({hall})" if hall else "")
                if prev is None and (first or new_title or new_branch):
                    st_ = status.setdefault(mname, {"open": [], "none": []})
                    short = b["name"] + (f" ({hall})" if hall else "")
                    if dates:
                        st_["open"].append((short, dates))
                    else:
                        st_["none"].append(short)
                else:
                    new = [d for d in dates if d not in (prev or [])]
                    if new:
                        opened.setdefault(mno, {"name": mname, "rpst": rpst, "places": []})["places"].append((place, new))
                dates_state[key] = sorted({d for d in (prev or []) + dates if d >= day})
    for mname, st_ in status.items():
        summary.append(f"• {mname} (메가박스)")
        for place, dates in st_["open"]:
            summary.append(f"   {place}: 예매 가능 " + ", ".join(fmt_day(d) for d in dates[:6]) +
                           (f" 외 {len(dates) - 6}일" if len(dates) > 6 else ""))
        if st_["none"]:
            summary.append("   아직 일정 없음: " + ", ".join(st_["none"]))
    for mno, o in opened.items():     # 영화 하나당 알림 한 번 (지점이 여러 곳이어도 묶어서)
        notify.send(
            f"🎟️ 예매 오픈 | {o['name']}",
            [f"📍 {place}: " + ", ".join(fmt_day(d) for d in new[:8]) + (f" 외 {len(new) - 8}일" if len(new) > 8 else "")
             for place, new in o["places"]] +
            ["", "아래 🎬 버튼으로 회차를 골라 두면 자리가 날 때도 알려줘요."],
            link=f"{BASE}/movie-detail?rpstMovieNo={o['rpst']}", button="예매하러 가기",
            photo=abs_img(s.get("posters", {}).get(mno) or ""),
        )
    if opened:
        s["catalog_req"] = True   # 새 회차가 선택 화면에 바로 보이게 목록을 다시 받음
    s["mb_titles"] = sorted({norm(t) for t in want_movies})
    s["mb_branch_entries"] = sorted(f"{norm(n)}|{h}" for n, h in wanted_branches(cfg))
    init["mb_booking"] = True
