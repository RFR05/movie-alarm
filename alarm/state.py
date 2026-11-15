"""실행 사이에 기억할 내용 저장/불러오기.

DATA_DIR(_data) 폴더의 파일들은 워크플로가 저장소의 'data' 브랜치에 올려 둠:
  state.json      알리미가 기억하는 것 (본 이벤트, 감시 목록, 텔레그램 수신 위치 등)
  catalog.json    선택 화면에 보여줄 지점·영화·회차 목록
  layouts.json    상영관별 좌석 배치도
  watchlist.json  선택 화면에 보여줄 현재 감시 목록
"""
import json
import os

DIR = os.environ.get("DATA_DIR", "_data")


def path(name):
    return os.path.join(DIR, name)


def load_json(name, default=None):
    try:
        with open(path(name), encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {} if default is None else default


def save_json(name, obj, compact=False):
    os.makedirs(DIR, exist_ok=True)
    tmp = path(name) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        if compact:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
        else:
            json.dump(obj, f, ensure_ascii=False, indent=1, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path(name))


def load():
    return load_json("state.json")


def save(s):
    save_json("state.json", s)
