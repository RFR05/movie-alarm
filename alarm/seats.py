"""좌석 배치도 해석 + '좋은 자리'·연석 판정. 네트워크 없이 계산만 하는 부분.

메가박스 좌석 응답(selectSeatList.do)의 seatListSD01 항목 예:
  {"rowNm": "C", "seatNo": 7, "horzCoorVal": 9, "vertCoorVal": 4, "horzSizeRt": 1,
   "seatClassCd": "RECLINE_CLS", "seatSellTyCd": "GERN_SELL", "seatStatCd": "GERN_SELL", ...}
seatStatCd 가 GERN_SELL 이면 지금 고를 수 있는 좌석, SCT01·SCT04 등은 이미 나간 좌석.
"""
import collections
import html
import math

AVAILABLE = {"GERN_SELL"}        # 지금 고를 수 있는 좌석의 seatStatCd
DISABLED_CLASS = "DISABLED_CLS"  # 장애인석(복지카드 필요) → 자리 계산에서 뺌
ADJ_GAP = 1.5                    # 같은 줄에서 좌표 차이가 이 이하면 붙은 자리 (2 이상이면 통로)


def _num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _clean(s):
    return html.unescape(str(s if s is not None else "")).strip()


def parse_seatmap(raw):
    """좌석 응답 → {"info": 회차 정보, "seats": [좌석...], "choi": 사이트가 말한 선택 가능 좌석 수}"""
    items = raw.get("seatListSD01") if isinstance(raw, dict) else None
    if not isinstance(items, list) or not items:
        keys = ", ".join(map(str, raw.keys())) if isinstance(raw, dict) else type(raw).__name__
        msg = _clean(raw.get("msg")) if isinstance(raw, dict) else ""
        raise ValueError(f"좌석 배치도가 응답에 없어요 ({msg or '키: ' + keys[:200]})")

    seats, seen = [], set()
    for o in items:
        if not isinstance(o, dict):
            continue
        row, no = _clean(o.get("rowNm")), o.get("seatNo")
        if not row or no in (None, "") or str(o.get("seatExpoAt") or "Y").upper() == "N":
            continue
        sid = f"{row}{no}"
        if sid in seen:
            continue
        seen.add(sid)
        stat = str(o.get("seatStatCd") or "")
        seats.append({
            "id": sid, "row": row, "no": int(no) if str(no).isdigit() else str(no),
            "x": _num(o.get("horzCoorVal"), _num(o.get("colNo"))),
            "y": _num(o.get("vertCoorVal"), _num(o.get("rowNo"))),
            "w": max(1.0, _num(o.get("horzSizeRt"), 1.0)),
            "stat": stat, "sell": str(o.get("seatSellTyCd") or ""),
            "ok": stat in AVAILABLE,
            "dis": o.get("seatClassCd") == DISABLED_CLASS,
        })
    if not seats:
        raise ValueError("좌석 목록은 있는데 줄/번호를 못 읽었어요")

    info = raw.get("movieDtlInfo") if isinstance(raw.get("movieDtlInfo"), dict) else {}
    sid = str(info.get("playSchdlNo") or "")
    choi = None
    for p in raw.get("playSeqList") or []:
        if isinstance(p, dict) and str(p.get("choiCnt", "")).isdigit() and (not sid or str(p.get("playSchdlNo")) == sid):
            choi = int(p["choiCnt"])
            break

    # 상태 코드 이름이 바뀐 경우 대비: 고를 수 있는 좌석이 있다는데 하나도 못 찾았으면
    # '판매 유형과 상태가 같은 좌석'을 고를 수 있는 좌석으로 봄 (그 수가 사이트 숫자와 맞을 때만)
    if choi and not any(s["ok"] for s in seats):
        alt = [s for s in seats if s["stat"] and s["stat"] == s["sell"]]
        if len(alt) == choi:
            for s in alt:
                s["ok"] = True

    return {
        "info": {
            "show": sid, "b": str(info.get("brchNo") or ""), "bn": _clean(info.get("brchNm")),
            "hn": str(info.get("theabNo") or ""), "hall": _clean(info.get("theabExpoNm") or info.get("theabNm")),
            "m": str(info.get("movieNo") or ""), "mn": _clean(info.get("movieNm")),
            "kind": _clean(info.get("playKindName")), "poster": info.get("imgPath") or "",
        },
        "seats": seats, "choi": choi,
        "stats": dict(collections.Counter(s["stat"] for s in seats)),
    }


def _half_up(v):
    return int(math.floor(v + 0.5))


def row_order(seats):
    """줄 이름을 스크린에서 가까운 순서(앞→뒤)로."""
    ys = {}
    for s in seats:
        ys[s["row"]] = min(ys.get(s["row"], s["y"]), s["y"])
    return sorted(ys, key=lambda r: (ys[r], r))


def zone_ids(seats):
    """'좋은 자리' 구역: 가운데 절반 폭 + 중간~뒤쪽 줄. (선택 화면 index.html의 zoneIds와 같은 규칙)"""
    if not seats:
        return set()
    rows = row_order(seats)
    n = len(rows)
    if n <= 2:
        good = set(rows)
    elif n <= 4:
        good = set(rows[(n + 1) // 2:])
    else:
        good = set(rows[_half_up((n - 1) * 0.45):_half_up((n - 1) * 0.85) + 1])
    x0 = min(s["x"] for s in seats)
    x1 = max(s["x"] + s["w"] for s in seats)
    center, half = (x0 + x1) / 2, (x1 - x0) / 2
    return {s["id"] for s in seats
            if s["row"] in good and not s["dis"] and abs(s["x"] + s["w"] / 2 - center) <= half * 0.5 + 1e-9}


def runs(cands, n):
    """같은 줄에서 n석 이상 붙어 있는 묶음들."""
    by_row = collections.defaultdict(list)
    for s in cands:
        by_row[s["row"]].append(s)
    out = []
    for row in by_row.values():
        row.sort(key=lambda s: s["x"])
        cur = [row[0]]
        for a, b in zip(row, row[1:]):
            if 0 < b["x"] - a["x"] <= a["w"] + (ADJ_GAP - 1):
                cur.append(b)
            else:
                if len(cur) >= n:
                    out.append(cur)
                cur = [b]
        if len(cur) >= n:
            out.append(cur)
    return out


def evaluate(w, sm):
    """감시 조건 w 를 좌석 배치도 sm 에 대어 봄 → {"ok", "seats"(조건에 맞는 좌석), "free"(후보 좌석 수)}"""
    cands = [s for s in sm["seats"] if s["ok"] and not s["dis"]]
    mode = w.get("k") or "any"
    if mode == "pick":
        want = set(w.get("seats") or [])
        cands = [s for s in cands if s["id"] in want]
    elif mode == "zone":
        zone = zone_ids(sm["seats"])
        cands = [s for s in cands if s["id"] in zone]
    n = max(1, int(w.get("n") or 1))
    if n >= 2 and w.get("adj"):
        groups = runs(cands, n)
        return {"ok": bool(groups), "seats": [s for g in groups for s in g], "free": len(cands)}
    return {"ok": len(cands) >= n, "seats": cands, "free": len(cands)}


def fmt_seats(seats, limit=18):
    """[C7, C8, D3] → 'C열 7·8 / D열 3'"""
    seats = sorted(seats, key=lambda s: (s["y"], s["x"]))
    shown, rest = seats[:limit], len(seats) - limit
    by_row = collections.OrderedDict()
    for s in shown:
        by_row.setdefault(s["row"], []).append(str(s["no"]))
    text = " / ".join(f"{row}열 {'·'.join(nos)}" for row, nos in by_row.items())
    return text + (f" 외 {rest}석" if rest > 0 else "")


def layout_of(sm):
    """선택 화면에 그릴 배치도: [[줄, 번호, x, y, 폭, 장애인석?], ...] (좌표는 0부터)"""
    x0 = min(s["x"] for s in sm["seats"])
    y0 = min(s["y"] for s in sm["seats"])
    r = lambda v: int(v) if float(v).is_integer() else round(v, 2)
    return [[s["row"], s["no"], r(s["x"] - x0), r(s["y"] - y0), r(s["w"]), 1 if s["dis"] else 0]
            for s in sorted(sm["seats"], key=lambda s: (s["y"], s["x"]))]
