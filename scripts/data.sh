#!/usr/bin/env bash
# 알리미가 기억하는 데이터(_data 폴더)를 저장소의 'data' 브랜치에 보관한다.
#  - 코드가 있는 main 브랜치는 건드리지 않아서, 선택 화면(GitHub Pages)이 5분마다 다시 배포되지 않는다.
#  - 매번 '커밋 1개짜리' 브랜치로 덮어써서 저장소가 계속 커지지 않는다.
# 쓰는 법:  bash scripts/data.sh pull   (실행 전에 불러오기)
#           bash scripts/data.sh push   (실행 후에 저장하기)
set -euo pipefail
DIR="${DATA_DIR:-_data}"
BRANCH="${DATA_BRANCH:-data}"
MARK="$(git rev-parse --git-dir)/movie-alarm-data-tree"

case "${1:-}" in
  pull)
    mkdir -p "$DIR"
    rm -f "$MARK"
    if git fetch --quiet --depth 1 origin "$BRANCH" 2>/dev/null; then
      git archive FETCH_HEAD | tar -x -C "$DIR"
      git rev-parse 'FETCH_HEAD^{tree}' > "$MARK"
      echo "data 브랜치에서 불러옴: $(ls "$DIR" | tr '\n' ' ')"
    else
      echo "data 브랜치가 아직 없음 (첫 실행이면 정상)"
    fi
    ;;
  push)
    if [ ! -d "$DIR" ] || [ -z "$(ls -A "$DIR" 2>/dev/null)" ]; then
      echo "저장할 데이터 없음"; exit 0
    fi
    GIT_INDEX_FILE="$(mktemp -u)"; export GIT_INDEX_FILE
    git --work-tree="$DIR" add -A
    TREE="$(git write-tree)"
    rm -f "$GIT_INDEX_FILE"; unset GIT_INDEX_FILE
    if [ -f "$MARK" ] && [ "$TREE" = "$(cat "$MARK")" ]; then
      echo "바뀐 내용 없음"; exit 0
    fi
    COMMIT="$(git -c user.name='movie-alarm-bot' -c user.email='41898282+github-actions[bot]@users.noreply.github.com' \
      commit-tree "$TREE" -m "데이터 $(date -u +%FT%TZ)")"
    git -c push.negotiate=false push --quiet --force origin "$COMMIT:refs/heads/$BRANCH"
    echo "$TREE" > "$MARK"
    echo "data 브랜치에 저장함"
    ;;
  *)
    echo "사용법: bash scripts/data.sh pull|push" >&2; exit 2
    ;;
esac
