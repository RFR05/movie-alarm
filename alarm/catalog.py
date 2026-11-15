"""선택 화면에 보여줄 목록 만들기: 지점 → 날짜 → 영화 → 회차 (+ 상영관 좌석 배치도)."""
import datetime as dt

from . import megabox, seats
from . import state as st

LAYOUTS_PER_RUN = 8      # 한 번에 새로 받아 오는 좌석 배치도 수 (나머지는 다음 실행 때)
LAYOUT_DAYS = 30         # 배치도를 다시 받아 오는 주기(일)
MAX_DAYS = 21            # 한 지점당 최대 며칠 치를 담을지


def due(s, cfg):
    if s.get("catalog_req") or not s.get("catalog_ts"):
        return True
    age = megabox.now() - dt.datetime.fromisoformat(s["catalog_ts"])
    return age >= dt.timedelta(minutes=int(cfg.get("catalog_minutes") or 20))


def refresh(page, cfg, s, summary, debug=False):
    if not debug and not due(s, cfg):
        return
    targets, _ = megabox.target_branches(page, cfg, s)
    branches = []
    for b, _hall, _new in targets:
        if b["no"] not in {x["no"] for x in branches}:
            branches.append(b)
    if not branches:
        raise RuntimeError("선택 화면에 넣을 지점을 못 찾았어요 (config.yaml의 megabox.branches 확인)")

    now = megabox.now()
    today = now.strftime("%Y%m%d")
    horizon = [(now + dt.timedelta(days=i)).strftime("%Y%m%d") for i in range(int(cfg.get("days_ahead") or 14))]
    last_day = (now + dt.timedelta(days=60)).strftime("%Y%m%d")
    hhmm_now = now.strftime("%H:%M")

    shows, movies, calls = [], {}, 0
    for b in branches:
        first, dates = megabox.fetch_shows(page, b["no"], today, b.get("area", ""))
        calls += 1
        days = sorted({d for d in dates if today <= d <= last_day} | {today})[:MAX_DAYS] if dates else horizon
        if debug:
            print(f"[디버그] {b['name']}({b['no']}): 날짜 {'사이트 제공 ' + str(len(days)) + '일' if dates else '기본 ' + str(len(days)) + '일'}, "
                  f"오늘 회차 {len(first)}개" + (f", 첫 회차 = {first[0]}" if first else ""))
        for d in days:
            if d == today:
                rows = first
            else:
                rows = megabox.fetch_shows(page, b["no"], d, b.get("area", ""))[0]
                calls += 1
            for x in rows:
                if d == today and x["t"] <= hhmm_now:
                    continue                                   # 이미 시작한 회차는 뺌
                shows.append([x["id"], b["no"], d, x["m"], x["t"], x["e"], x["hall"], x["hn"], x["kind"], x["rest"], x["tot"]])
                mv = movies.setdefault(x["m"], {"n": x["mn"]})
                poster = x.get("poster") or s.get("posters", {}).get(x["m"])
                if poster and "p" not in mv:
                    mv["p"] = megabox.abs_img(poster)
    print(f"[시간표] 지점 {len(branches)}곳, 회차 {len(shows)}개, 영화 {len(movies)}편 (요청 {calls}번)")

    # 상영관 좌석 배치도: 아직 없는 관만 조금씩 받아 옴
    layouts = st.load_json("layouts.json")
    halls = {}
    for row in shows:
        if row[7]:
            halls.setdefault(f"{row[1]}|{row[7]}", row)
    stale = (now - dt.timedelta(days=LAYOUT_DAYS)).strftime("%Y%m%d")
    todo = [k for k in halls if k not in layouts or layouts[k].get("day", "") < stale]
    got = 0
    for key in ((todo or list(halls))[:1] if debug else todo[:LAYOUTS_PER_RUN]):   # debug: 배치도 하나는 꼭 읽어 봄
        row = halls[key]
        try:
            sm = megabox.fetch_seatmap(page, row[0])
        except RuntimeError as e:
            print(f"[시간표] 좌석 배치도 실패 ({key}): {e}")
            if debug:
                raise
            continue
        if debug:
            ok = sum(1 for x in sm["seats"] if x["ok"])
            print(f"[디버그] 좌석 배치도 {key} ({sm['info'].get('hall')}): 좌석 {len(sm['seats'])}개, 상태 {sm['stats']}, "
                  f"고를 수 있음 {ok} / 사이트 숫자 {sm['choi']} / 시간표 잔여 {row[9]}, "
                  f"좋은 자리 구역 {len(seats.zone_ids(sm['seats']))}석")
        layouts[key] = {"day": today, "name": sm["info"].get("hall") or row[6], "seats": seats.layout_of(sm)}
        if sm["info"].get("poster") and row[3]:
            s.setdefault("posters", {})[row[3]] = sm["info"]["poster"]
            movies.get(row[3], {}).setdefault("p", megabox.abs_img(sm["info"]["poster"]))
        got += 1
    for key in [k for k in layouts if k not in halls and layouts[k].get("day", "") < stale]:
        del layouts[key]                                       # 더는 안 쓰는 오래된 배치도 정리
    if todo:
        print(f"[시간표] 좌석 배치도 {got}개 새로 받음 (남은 {max(0, len(todo) - got)}개는 다음에)")

    if debug:
        return
    st.save_json("catalog.json", {
        "v": 1, "updated": now.isoformat(timespec="seconds"),
        "cols": ["id", "branch", "date", "movie", "start", "end", "hall", "hallNo", "kind", "rest", "total"],
        "branches": [{"no": b["no"], "name": b["name"]} for b in branches],
        "movies": movies, "shows": shows,
    }, compact=True)
    st.save_json("layouts.json", layouts, compact=True)
    s["catalog_ts"] = now.isoformat(timespec="seconds")
    # 배치도가 아직 덜 채워졌으면 다음 실행에도 이어서 받음 (계속 실패하면 8번까지만)
    s["layout_tries"] = int(s.get("layout_tries", 0)) + 1 if len(todo) > got else 0
    s["catalog_req"] = len(todo) > got and s["layout_tries"] < 8
