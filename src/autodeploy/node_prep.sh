#!/usr/bin/env bash
# ===============================================================
# 타겟 PC 준비 (hybrid 프로파일 전용)
#
# AutoDeploy 가 `SSH 키 등록` 직후에 타겟으로 보내 root 로 실행한다.
# 원격 지원이 가능한 데스크톱으로 만드는 것이 목적이라, 배포 내용과는 무관하다.
# 그래서 설치할 때마다가 아니라 서버를 등록할 때 한 번만 돈다.
#
# 전부 멱등이다 — 다시 등록해도 중복되거나 덧쌓이지 않는다.
#
# 값은 AutoDeploy 가 실행할 때 채워 넣는다. 손으로 돌릴 때는 환경변수로 준다:
#   sudo TARGET_USER=connecteve ANYDESK_PASSWORD=... bash node_prep.sh
# ===============================================================
set -euo pipefail

TARGET_USER="${TARGET_USER:-connecteve}"
ANYDESK_PASSWORD="${ANYDESK_PASSWORD:-CHANGE_ME}"
# 기본은 꺼둔다. 병원 장비를 정기 재부팅시키는 것은 운영 정책이라
# 기본값으로 정할 일이 아니다 (.env 의 NODE_PREP_WEEKLY_REBOOT 로 켠다).
WEEKLY_REBOOT="${WEEKLY_REBOOT:-false}"
WEEKLY_REBOOT_CRON="${WEEKLY_REBOOT_CRON:-0 4 * * 0}"

log() { printf '\n== %s\n' "$1"; }


# ===============================================================
# 3. GDM Wayland 비활성화 (X11 강제)
#    /etc/gdm3/custom.conf 의 #WaylandEnable=false 주석을 해제한다.
#    AnyDesk 등 원격 도구는 Wayland 세션에서 화면 캡처가 제한되므로
#    X11 로 고정하는 것이 안전하다. 재부팅 후 반영된다.
# ===============================================================
log "GDM Wayland 비활성화"
GDM_CONF=/etc/gdm3/custom.conf

if [[ -f "$GDM_CONF" ]]; then
    cp -a "$GDM_CONF" "${GDM_CONF}.bak.$(date +%Y%m%d%H%M%S)"

    if grep -qE '^\s*WaylandEnable\s*=\s*false' "$GDM_CONF"; then
        echo "  이미 활성화됨 (WaylandEnable=false)"
    elif grep -qE '^\s*#\s*WaylandEnable\s*=' "$GDM_CONF"; then
        # 주석 해제 + 값 false 로 고정
        sed -i -E 's/^\s*#\s*WaylandEnable\s*=.*/WaylandEnable=false/' "$GDM_CONF"
        echo "  주석 해제 완료"
    elif grep -qE '^\s*WaylandEnable\s*=' "$GDM_CONF"; then
        sed -i -E 's/^\s*WaylandEnable\s*=.*/WaylandEnable=false/' "$GDM_CONF"
        echo "  값을 false 로 변경"
    elif grep -qE '^\s*\[daemon\]' "$GDM_CONF"; then
        sed -i -E '0,/^\s*\[daemon\]/s//[daemon]\nWaylandEnable=false/' "$GDM_CONF"
        echo "  [daemon] 섹션에 항목 추가"
    else
        printf '\n[daemon]\nWaylandEnable=false\n' >> "$GDM_CONF"
        echo "  [daemon] 섹션 신규 생성"
    fi

    echo "  현재 값: $(grep -E '^\s*WaylandEnable' "$GDM_CONF" || echo '(없음)')"
    echo "  !! 재부팅해야 반영됩니다."
else
    echo "  $GDM_CONF 없음 (GDM 미설치) - 건너뜀"
fi


# ===============================================================
# 4. GNOME 절전 / 화면 잠금 해제
#    GDM(로그인 화면)과 사용자 세션은 별개 경로이므로 둘 다 처리.
#    무인 관리 장비에서 화면 잠금은 AnyDesk 접속을 방해하므로 해제.
#    모니터 꺼짐(DPMS)은 시스템 동작에 영향이 없어 그대로 둔다.
# ===============================================================
apply_gnome_settings() {
    local as_user="$1"
    local -a keys=(
        "org.gnome.settings-daemon.plugins.power sleep-inactive-ac-type nothing"
        "org.gnome.settings-daemon.plugins.power sleep-inactive-battery-type nothing"
        "org.gnome.settings-daemon.plugins.power idle-dim false"
        "org.gnome.desktop.screensaver lock-enabled false"
    )
    local k
    for k in "${keys[@]}"; do
        # shellcheck disable=SC2086
        sudo -u "$as_user" dbus-launch gsettings set $k 2>/dev/null || true
    done
}

log "GDM 로그인 화면 절전/잠금 해제"
if id gdm &>/dev/null; then
    apply_gnome_settings gdm
else
    echo "  gdm 계정 없음 (GUI 미설치) - 건너뜀"
fi

log "사용자 세션 절전/잠금 해제"
if [[ -n "$TARGET_USER" ]] && id "$TARGET_USER" &>/dev/null; then
    apply_gnome_settings "$TARGET_USER"
else
    echo "  대상 사용자 없음 - 건너뜀"
fi

# ===============================================================
# 7. AnyDesk 설치
# ===============================================================
log "AnyDesk 설치"

if command -v anydesk &>/dev/null; then
    echo "  이미 설치됨: $(anydesk --version 2>/dev/null || echo unknown)"
else
    install -m 0755 -d /etc/apt/keyrings

    if curl -fsSL https://keys.anydesk.com/repos/DEB-GPG-KEY \
        | gpg --dearmor -o /etc/apt/keyrings/anydesk.gpg 2>/dev/null; then

        cat > /etc/apt/sources.list.d/anydesk.list <<'EOF'
deb [signed-by=/etc/apt/keyrings/anydesk.gpg] http://deb.anydesk.com/ all main
EOF
        apt-get update
        apt-get install -y anydesk
    else
        echo "  !! 저장소 접근 실패 (폐쇄망?)."
        echo "  !! .deb 를 직접 내려받아 설치하세요:"
        echo "  !!   sudo apt install -y ./anydesk_*_amd64.deb"
        echo "  !! 다운로드: https://anydesk.com/en/downloads/linux"
    fi
fi


# ===============================================================
# 8. AnyDesk 무인 접근(Full Access) 설정
#    AnyDesk 7.x 이상은 권한 프로파일 이름을 인자로 요구한다.
#      echo '<pw>' | anydesk --set-password full
#    6.x 는 프로파일 인자가 없다.
#      echo '<pw>' | anydesk --set-password
# ===============================================================
if command -v anydesk &>/dev/null; then
    log "AnyDesk 무인 접근 설정"

    systemctl enable --now anydesk 2>/dev/null || true
    sleep 3

    if [[ "$ANYDESK_PASSWORD" == "CHANGE_ME" || -z "$ANYDESK_PASSWORD" ]]; then
        echo "  !! ANYDESK_PASSWORD 가 설정되지 않았습니다. 건너뜁니다."
        ANYDESK_PASSWORD=""
    fi

    if [[ -n "$ANYDESK_PASSWORD" ]]; then
        if printf '%s' "$ANYDESK_PASSWORD" | anydesk --set-password full 2>/dev/null; then
            echo "  OK: Full Access 프로파일로 설정됨"
        elif printf '%s' "$ANYDESK_PASSWORD" | anydesk --set-password 2>/dev/null; then
            echo "  OK: 설정됨 (구버전 방식)"
        else
            echo "  !! 자동 설정 실패. GUI 에서 직접 지정하세요:"
            echo "  !!   설정 > 보안 > 무인 접근 허용 > 권한 프로파일 = Full Access"
        fi
    else
        echo "  건너뜀 (비밀번호 미설정)"
    fi

    echo
    echo "  이 장비의 AnyDesk ID:"
    anydesk --get-id 2>/dev/null | sed 's/^/    /' || echo "    (조회 실패 - 서비스 기동 후 재시도)"

    # AutoDeploy 가 이 줄을 읽어 서버 목록의 메모 칸에 적어둔다.
    # 사람이 읽는 위쪽 출력과 따로 두는 이유는, 줄 모양이 바뀌면 파싱이 조용히
    # 깨지기 때문이다. 이 줄은 기계용이라 형식을 고정한다.
    _adid="$(anydesk --get-id 2>/dev/null | tr -dc '0-9')" || _adid=""
    [[ -n "$_adid" ]] && echo "ANYDESK_ID=${_adid}"
fi


# ===============================================================
# 9. 주간 정기 재부팅 (root crontab)
#     기존 항목은 주석 태그로 걸러낸 뒤 다시 넣으므로
#     여러 번 실행해도 중복 등록되지 않는다.
# ===============================================================
log "주간 정기 재부팅 등록"

CRON_TAG='# node-prep weekly reboot'

if [[ "$WEEKLY_REBOOT" == "true" ]]; then
    {
        crontab -l 2>/dev/null | grep -vF "$CRON_TAG" || true
        echo "${WEEKLY_REBOOT_CRON} /sbin/reboot $CRON_TAG"
    } | crontab -

    echo "  등록: ${WEEKLY_REBOOT_CRON}"
    echo "  현재 root crontab:"
    crontab -l 2>/dev/null | sed 's/^/    /'
else
    crontab -l 2>/dev/null | grep -vF "$CRON_TAG" | crontab - || true
    echo "  비활성화 (기존 항목 제거)"
fi

log "준비 완료"
