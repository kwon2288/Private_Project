#!/bin/bash
# Wiki.js 3 백업 스크립트 (시놀로지 작업 스케줄러용)
#  - DB: pg_dump (custom 포맷, 날짜별 보관)
#  - 파일: SOURCE_DIR 전체를 tar.gz (DB 원본 폴더는 제외)
#  - KEEP_DAYS 보다 오래된 백업 자동 삭제
set -u
set -o pipefail

## 설정
DATE=$(date +%y%m%d_%H%M)
BACKUP_DIR='/volume4/BACKUP/NAS/Docker-Backup/wikijs'
SOURCE_DIR='/volume4/docker/wikijs3'
DB_CONTAINER='wikijs3_db'
DB_NAME='wiki'
DB_USER='wikijs'
KEEP_DAYS=7                 # 보관 기간(일)
# PostgreSQL 데이터를 SOURCE_DIR 아래 폴더로 마운트했다면 그 폴더 이름 (예: db). 없으면 비워 두기
PG_DATA_SUBDIR=''

LOG="$BACKUP_DIR/backup.log"
mkdir -p "$BACKUP_DIR/data/db" "$SOURCE_DIR/backup"
log() { echo "$(date '+%F %T') $*" | tee -a "$LOG"; }

log "===== 백업 시작 ====="

## 1. DB 덤프 — 임시 파일에 받고, 성공했을 때만 정식 파일로 바꿈
DUMP_TMP="$SOURCE_DIR/backup/wikibackup.dump.tmp"
DUMP="$SOURCE_DIR/backup/wikibackup.dump"
if docker exec "$DB_CONTAINER" pg_dump "$DB_NAME" -U "$DB_USER" -F c > "$DUMP_TMP" && [ -s "$DUMP_TMP" ]; then
    mv -f "$DUMP_TMP" "$DUMP"
    cp -f "$DUMP" "$BACKUP_DIR/data/db/wikibackup_$DATE.dump"
    log "DB 덤프 성공: $(du -h "$DUMP" | cut -f1)"
else
    rm -f "$DUMP_TMP"
    log "!! DB 덤프 실패 — 이전 덤프는 그대로 둠"
    DB_FAIL=1
fi

## 2. 파일 백업 (tar.gz) — 실행 중인 DB 원본 폴더는 제외 (덤프로 대신함)
EXCLUDE=()
[ -n "$PG_DATA_SUBDIR" ] && EXCLUDE=(--exclude="$(basename "$SOURCE_DIR")/$PG_DATA_SUBDIR")
if tar -czf "$BACKUP_DIR/data/wikijs_$DATE.tar.gz" "${EXCLUDE[@]}" \
        -C "$(dirname "$SOURCE_DIR")" "$(basename "$SOURCE_DIR")"; then
    log "파일 백업 성공: $(du -h "$BACKUP_DIR/data/wikijs_$DATE.tar.gz" | cut -f1)"
else
    log "!! 파일 백업 중 경고/실패 (tar 종료 코드 $?)"
fi

## 3. 오래된 백업 삭제
log "$KEEP_DAYS일 지난 백업 삭제:"
find "$BACKUP_DIR/data" -maxdepth 1 -name 'wikijs_*.tar*' -mtime +"$KEEP_DAYS" -print -delete | tee -a "$LOG"
find "$BACKUP_DIR/data/db" -maxdepth 1 -name 'wikibackup_*.dump' -mtime +"$KEEP_DAYS" -print -delete | tee -a "$LOG"

log "===== 백업 종료 ====="
[ "${DB_FAIL:-0}" = 1 ] && exit 1
exit 0
