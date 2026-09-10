#!/usr/bin/env bash
# 맥미니(콘솔 서버)를 claude 브랜치 최신으로 맞춘다.
#
# 왜 ssh 옵션을 붙이나: 맥북의 ~/.ssh/known_hosts 는 개인 장비라 주기적으로
# 초기화된다. 그때마다 "Host key verification failed" 로 배포가 멎었다.
# 호스트 키를 저장소 안(ops/known_hosts)에 고정해 두고 그것만 본다.
# StrictHostKeyChecking=yes 는 그대로 둔다 — 키가 진짜로 바뀌면 멈춰야 한다.
#
# 정적 파일(css/js/html)만 바꿨으면 재시작이 필요 없다. 파이썬을 고쳤으면
#   ssh ... 'launchctl kickstart -k gui/$(id -u)/com.connecteve.autodeploy'
# 를 따로 실행한다 (진행 중인 작업이 있으면 끊기므로 확인하고 할 것).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SSH=(ssh -o UserKnownHostsFile="$HERE/known_hosts" -o StrictHostKeyChecking=yes autodeploy)

git push -q origin claude
"${SSH[@]}" 'cd ~/deploy && git fetch -q origin claude && git reset -q --hard origin/claude && git log --oneline -1'
