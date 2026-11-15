"""영화 알리미 실행 진입점. GitHub Actions가 5분마다 `python -m alarm.main`으로 실행.

한 번 실행에서 하는 일
  1) 텔레그램에 쌓인 요청 처리 (선택 화면에서 보낸 감시 추가/삭제 등)
  2) 메가박스 이벤트(아트그라피·특전) 확인, 관심 영화의 새 날짜(예매 오픈) 확인
  3) 선택 화면에 보여줄 시간표 목록 갱신 (20분마다)
  4) 걸어 둔 자리 감시 확인 → 감시가 있으면 한 번 실행 안에서 여러 번 다시 확인

ALARM_MODE
  normal : 평소 감시 (기본)
  test   : 텔레그램 테스트 메시지만 보냄 (CHAT_ID 모르면 로그에 출력)
  debug  : 사이트에서 읽은 내용을 로그에 자세히 출력, 알림/저장 안 함
"""
import os
import time
import traceback

import yaml
from playwright.sync_api import sync_playwright

from . import bot, catalog, megabox, notify, watch
from . import state as st

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")
FAIL_ALERT_AT = 3  # 연속 실패 몇 번째에 오류 알림을 보낼지 (5분 x 3 = 15분)


def run_url():
    srv, repo, rid = (os.environ.get(k) for k in ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID"))
    return f"{srv}/{repo}/actions/runs/{rid}" if srv and repo and rid else None


def launch(p):
    channel = os.environ.get("BROWSER_CHANNEL", "chrome")
    try:
        return p.chromium.launch(channel=channel or None, headless=True)
    except Exception as e:  # noqa: BLE001 - 시스템 크롬이 없으면 Playwright 크로미움으로
        print("[브라우저] 시스템 크롬 실행 실패, 기본 크로미움 사용:", str(e).splitlines()[0])
        return p.chromium.launch(headless=True)


def _int(v, default, lo, hi):
    try:
        return min(hi, max(lo, int(v)))
    except (TypeError, ValueError):
        return default


def main():
    mode = os.environ.get("ALARM_MODE", "normal").strip() or "normal"
    with open(os.environ.get("CONFIG_PATH", "config.yaml"), encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

    if mode == "test":
        if not notify.CHAT:
            notify.print_chat_ids()
            return
        notify.send("✅ 영화 알리미 연결 성공", ["이 메시지가 보이면 텔레그램 설정 끝!",
                                              "이제 5분마다 메가박스를 확인해요."],
                    link="https://www.megabox.co.kr", button="메가박스 열기")
        print("선택 화면 주소:", bot.page_url(cfg) or "(없음)")
        return

    debug = mode == "debug"
    megabox.DEBUG = debug
    if not debug and not (notify.TOKEN and notify.CHAT):
        print("텔레그램 시크릿(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID) 등록 전이라 이번 실행은 건너뜀 (README 3~4단계)")
        return

    s = st.load()
    summary, errors = [], {}
    checks = _int(os.environ.get("CHECKS_PER_RUN") or cfg.get("checks_per_run"), 3, 1, 6)
    gap = _int(os.environ.get("CHECK_INTERVAL_SEC") or cfg.get("check_interval_sec"), 70, 0, 120)

    with sync_playwright() as p:
        browser = launch(p)
        ctx = browser.new_context(locale="ko-KR", timezone_id="Asia/Seoul", user_agent=UA,
                                  viewport={"width": 1366, "height": 900})
        page = ctx.new_page()

        def step(name, fn):
            try:
                fn(page, cfg, s, summary, debug)
                errors.pop(name, None)          # 다시 해서 됐으면 오류로 안 침
            except Exception as e:  # noqa: BLE001 - 한 단계가 실패해도 나머지는 계속
                errors[name] = e
                traceback.print_exc()

        for i in range(checks):
            if i:
                time.sleep(gap)
            else:
                step("선택 화면", lambda _p, c, st_, sm, dbg: bot.ensure_menu(c, st_, sm, dbg))
            step("텔레그램", bot.process)
            if i == 0:
                step("메가박스 이벤트", megabox.run_events)
                step("메가박스 예매", megabox.run_booking)
            if i == 0 or s.get("catalog_req"):
                step("시간표 목록", catalog.refresh)
            step("메가박스 좌석", watch.run)
            if debug:
                break
            st.save(s)
            st.save_json("watchlist.json", watch.public(s, cfg), compact=True)
            if not watch.active(s):
                break                           # 걸어 둔 감시가 없으면 한 번만 확인하고 끝
        browser.close()

    if debug:
        print("\n[디버그] 요약:", *summary, sep="\n  ")
        print("[디버그] 오류:", {k: str(v) for k, v in errors.items()} or "없음")
        return

    if summary:
        title = "✅ 감시 목록 업데이트" if s.get("started") else "✅ 영화 알리미 시작"
        notify.send(title, summary + ["", "지금부터 새 소식이 생기면 바로 알려줄게요."])
        s["started"] = True

    if errors:
        s["fail_count"] = s.get("fail_count", 0) + 1
        if s["fail_count"] == FAIL_ALERT_AT:
            notify.send("⚠️ 영화 알리미 오류",
                        [f"{n}: {type(e).__name__}: {str(e)[:200]}" for n, e in errors.items()] +
                        ["", "15분째 확인이 안 되고 있어요. 실행 기록을 확인해 주세요."],
                        link=run_url(), button="실행 기록 보기")
    else:
        if s.get("fail_count", 0) >= FAIL_ALERT_AT:
            notify.send("✅ 영화 알리미 복구", ["다시 정상적으로 확인하고 있어요."])
        s["fail_count"] = 0
    st.save(s)


if __name__ == "__main__":
    main()
