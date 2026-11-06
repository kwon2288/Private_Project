# wikijs3-migration

시놀로지 NAS(Docker)에서 운영하던 **Wiki.js 2.x(2.5.317) → 3.x(3.1.0)** 이전 기록과 도구 모음.
2.x 내보내기(`.wkbackup`) → 3.x 가져오기 방식으로 페이지 1,910 / 이력 15,432 / 첨부 2,919건을 옮겼다.

```
wikijs3-migration/
├── README.md                      ← 이 문서 (절차 + 겪은 문제)
├── config/
│   ├── docker-compose.yml         ← 3.x 스택 예시 (postgres:18)
│   └── npm-advanced.conf          ← Nginx Proxy Manager Advanced 설정
└── scripts/
    ├── wikijs2_fix_paths.py       ← [2.x] 경로를 3.x 규칙에 맞게 일괄 이동
    ├── wikijs2_fix_links.py       ← [2.x] 이동으로 깨진 본문 내부 링크 수정
    ├── wikijs2_fix_attachments.py ← [2.x] 상대 경로 첨부 링크 → 절대 경로
    └── wikijs3_backup.sh          ← [3.x] DB 덤프 + 파일 백업 (스케줄러용)
```

> 2.x 스크립트는 모두 2.x GraphQL API를 쓴다. 공통 환경변수:
> `WIKI_URL=http://127.0.0.1:<2.x 포트>`, `WIKI_TOKEN=<2.x Full Access API 키>`
> 기본은 **미리보기**이고 `--apply` 를 붙여야 실제로 바뀐다. 다시 실행해도 안전하다(이미 처리된 건 건너뜀).

---

## 핵심 요약 (먼저 알아둘 것)

| 항목 | 내용 |
|---|---|
| 업그레이드 방식 | **2.x DB에 3.x를 그대로 붙이는 제자리 업그레이드는 안 됨.** 3.x를 빈 DB로 새로 설치하고, 2.x에서 내보낸 `.wkbackup`을 3.x 가져오기로 넣는다 |
| DB | PostgreSQL **17/18** 필요. `postgres:18` 이미지는 볼륨을 **`/var/lib/postgresql`** 에 마운트 |
| 페이지 경로 | 3.x는 **문자·숫자·하이픈·슬래시만** 허용. `+ , ( ) ' ’ ® — % ! [ ]` 등이 있으면 가져오기 실패 |
| 경로 정규화 | 3.x는 경로를 **소문자**로, **`_` → `-`** 로 바꿔 저장 → 대소문자/밑줄만 다른 페이지 쌍은 **하나로 합쳐짐** |
| 가져오기 | 브라우저가 파일을 읽어 서버로 스트리밍. **탭을 닫거나 PC가 절전하면 중단**. 리버스 프록시 타임아웃이 짧으면 504 |
| 사이트 hostname | 3.x는 접속 호스트명으로 사이트를 찾음. 값이 틀리면 **검은 화면**(`/_api/bootstrap` 404) |

---

## 이전 절차

### 0. 백업
```bash
docker exec wikijs_db pg_dump wiki -U wikijs -F c > wiki2_$(date +%Y%m%d).dump
```
`docker exec` 에 `-t` 를 붙이지 말 것 (바이너리 덤프가 깨짐).

### 1. 2.x 정리 (스크립트)

**1-1. Storage 동기화 끄기**
2.x 관리 → Storage 에서 Local File System / Git 을 **비활성화**.
켜져 있으면 페이지 이동 시 디스크(`/data/...`)나 Git(`/wiki/data/repo/...`)에 파일이 없을 때 `ENOENT` 로 실패한다.

**1-2. 첨부 링크 절대 경로화** — `wikijs2_fix_attachments.py`
본문에 `attachments\abc.jpg` 처럼 **상대 경로(백슬래시 포함)** 로 걸린 첨부를 `/폴더/attachments/abc.jpg` 로 바꾼다.
- 첨부 존재 여부는 첨부 URL이 아니라 **GraphQL 자산 목록**으로 확인 (게스트 403 회피)
- 같은 폴더에 없으면 **다른 첨부 폴더에서 같은 파일명**을 찾아 연결 (예: 페이지는 `Arctera/BackupExec` 로 옮겼는데 첨부는 `Veritas/BackupExec` 에 남은 경우 → 2.x에서도 깨져 있던 이미지가 살아남)
- 대소문자·공백·`_` 차이는 무시하고 비교 (2.x는 업로드 시 파일명을 소문자/`_` 로 바꿔 저장)
```bash
python3 -u wikijs2_fix_attachments.py > attach_dry.log 2>&1 ; tail -1 attach_dry.log
nohup python3 -u wikijs2_fix_attachments.py --apply > attach_apply.log 2>&1 &
```

**1-3. 경로 정리** — `wikijs2_fix_paths.py`
기호와 `_` 를 `-` 로 바꾼 경로로 이동(`pages.move`). 3.x에서 합쳐질 쌍은 `[COLLISION]` 으로 먼저 알려 준다 → 가져오기 전에 한쪽을 지우거나 이름을 바꿔 둘 것.
```bash
python3 -u wikijs2_fix_paths.py | tee fix_paths_dry.log
nohup python3 -u wikijs2_fix_paths.py --apply > fix_paths_$(date +%Y%m%d_%H%M).log 2>&1 &
```

**1-4. 내부 링크 수정** — `wikijs2_fix_links.py`
1-3 로그의 `[MOVE]` 줄(바로 다음 줄이 `!! 실패` 인 건 제외)로 예전 경로 → 새 경로 목록을 만들어, 모든 페이지 본문의 링크를 바꾼다.
`/path`, `/ko/path#앵커`, `https://도메인/path`, URL 인코딩된 링크, `href="..."`, `[id]: /path` 형태를 처리.
```bash
python3 -u wikijs2_fix_links.py fix_paths_2*.log | tee links_dry.log
python3 -u wikijs2_fix_links.py fix_paths_2*.log --apply | tee links_apply.log
```
> 와일드카드는 `fix_paths_2*.log` 처럼 앞부분을 정확히. `*.log` 로 넣으면 다른 로그까지 읽힌다.

### 2. 2.x 내보내기
정리가 끝난 상태에서 `.wkbackup` 을 **새로** 내보낸다 (첨부 파일 포함 — 파일 크기가 2.x 첨부 용량과 비슷한지 확인).

### 3. 3.x 설치
`config/docker-compose.yml` 참고. 최초 설정 후 사이트 hostname 확인:
```bash
docker exec -it wikijs3_db psql -U wikijs -d wiki -c 'select id, hostname from wiki.sites;'
# 도메인 전체가 아니면(예: 'wiki3') 고친다. '*' 는 모든 호스트 허용
docker exec -it wikijs3_db psql -U wikijs -d wiki -c "update wiki.sites set hostname='*';"
docker restart wikijs3_wiki
```
3.x 테이블은 `wiki` 스키마 안에 있다 (`wiki.pages`, `wiki.sites`, `wiki.assets` …).

### 4. 3.x 가져오기
- **리버스 프록시를 거치지 말 것.** 내부 IP로 직접 접속하거나, https가 아니라 문제가 되면 SSH 터널(`ssh -L 3000:127.0.0.1:3001 root@NAS` → `http://localhost:3000`).
- NPM을 거칠 거면 `config/npm-advanced.conf` 처럼 타임아웃 3600s, 버퍼링 off 필수.
- 끝날 때까지 탭 유지, PC 절전 해제. 진행 확인:
```bash
docker exec wikijs3_db psql -U wikijs -d wiki -tAc 'select count(*) from wiki.pages;'
```
페이지 수는 묶음(약 250개) 단위로 한꺼번에 늘어난다. 변환(ckeditor HTML → markdown) 중에는 멈춘 것처럼 보임.

### 5. 마무리
- 관리 → Security: **Trust X-Forwarded-\* Proxy Headers** 켜기 (프록시 뒤 로그인/OAuth)
- 2.x/3.x 경로 비교로 누락 확인:
```bash
docker exec wikijs_db  psql -U wikijs -d wiki -tAc "select path from pages order by 1;"      > p2.txt
docker exec wikijs3_db psql -U wikijs -d wiki -tAc "select path from wiki.pages order by 1;" > p3.txt
LC_ALL=C comm -23 <(tr 'A-Z_' 'a-z-' < p2.txt | sort) <(sort p3.txt)
```
- 2.x는 3.x가 안정될 때까지 보관 (가져오지 못한 예전 이력 확인용)

---

## 실제 결과 (2026-11-07)

```
Import finished. 1 settings, 101 folders, 1910 pages, 4 users, 2919 assets,
                 4 groups, 1 locales, 8 comments, 2 navigation, 15432 page history
```

| 항목 | 결과 | 비고 |
|---|---|---|
| 페이지 | 1,910 → 3.x 1,905 | 5건은 2.x에서 대소문자·밑줄·언어만 다른 쌍이 합쳐짐 (현재 스크립트는 `[COLLISION]` 으로 미리 알려 줌) |
| 페이지 이력 | 15,432 / 275 건너뜀 | 삭제된 페이지, 이름 바꾸기 전 경로로 기록된 이력 |
| 첨부 | 2,919 / 272 건너뜀 | 262건은 시놀로지 썸네일(`@eaDir/SYNOFILE_THUMB_*`) — 폴더명 `@` 때문에 거부, 무시해도 됨 |
| 서식 | 34페이지 | 코드 블록 안 굵게/기울임/링크만 빠지고 내용은 유지 |
| 사용자 | 외부 인증 사용자 | 비밀번호 없이 들어옴 → 3.x 관리 → Authentication 에서 같은 전략 설정 필요 |

---

## 겪은 문제와 해결

| 증상 | 원인 | 해결 |
|---|---|---|
| `postgres:18` DB 컨테이너가 계속 재시작 | 18 이미지부터 PGDATA 경로 변경 | 볼륨을 `/var/lib/postgresql` 에 마운트. 기존 15~17 데이터는 메이저 버전이 달라 그대로 못 씀 → 덤프/복원 |
| `docker logs` 에 아무것도 안 나옴 | compose 에 `logging: driver: "none"` | 문제 해결 중에는 주석 처리 |
| 2.x 덤프를 복원한 DB에 `wiki:3` 을 붙이니 이상 동작 | 2.x → 3.x 제자리 업그레이드 미지원 | 3.x 빈 DB 새 설치 + `.wkbackup` 가져오기 |
| 3.x 접속 시 검은 화면 | `wiki.sites.hostname` 이 `wiki3` (도메인 일부) → `/_api/bootstrap` 404 | hostname 을 도메인 전체 또는 `*` 로 |
| `could not be imported: A page path may only contain…` | 경로에 기호 | `wikijs2_fix_paths.py` |
| 페이지 이동 시 `ENOENT ... /wiki/data/repo/...html` | 2.x Git Storage 에 파일 없음 | Storage 동기화 비활성화 후 재실행 |
| 가져오기 중 `504` (POST `.../streams/pages`) | NPM `proxy_read_timeout` 기본 60초 (설정이 주석 처리돼 있었음) | 프록시 우회(IP/SSH 터널) 또는 `npm-advanced.conf` |
| 504 후 페이지 수가 250에서 멈춤 | 연결이 끊기기 전 받은 묶음까지만 처리 | 같은 `.wkbackup` 으로 다시 가져오기(덮어쓰기됨, 중복 없음) |
| 첨부 확인 스크립트가 전부 "파일 없음" | 첨부 URL이 게스트에 403, API 키도 첨부 URL엔 무효 | GraphQL 자산 목록으로 확인하도록 변경 |
| 첨부 링크가 `attachments\이미지` 처럼 잘림 | 파일명에 공백 | 링크 끝을 `)`/따옴표까지 매칭 |
| 페이지 1,910 → 1,905 | 3.x 경로 소문자화 + `_`→`-` 로 쌍이 합쳐짐 | 2.x에서 미리 정리 (`[COLLISION]`) |

---

## 3.x 에서 달라진 점 메모

- **사이드바 폴더 트리 없음.** 2.x의 Navigation Mode "Site Tree" 같은 기능이 없다. 상단 `찾아보기`(Allow Browsing) 버튼을 쓰거나, Edit Nav → Menu 항목으로 폴더를 직접 건다(순서는 손잡이로 드래그). 커뮤니티 우회: [requarks/wiki#8129](https://github.com/requarks/wiki/discussions/8129) (Body HTML Injection, 비공식)
- **내장 MCP 서버** `https://<도메인>/_mcp` — 관리 → MCP 에서 켬(기본 꺼짐). 사전 조건: API 활성화, Personal Access Tokens 허용, Puppeteer 확장. claude.ai 커스텀 커넥터는 OAuth 2.1 이라 위키 쪽 추가 설정 없음. 2.x용 외부 MCP(`jaalbin24/wikijs-mcp`)는 3.x 미지원 → 내장 MCP 로 대체 예정
- **모바일 뷰어(wikijs-mobile)** 는 2.x GraphQL 기준 → 3.x 대응 필요
- 문서: [Requirements](https://docs.js.wiki/setup/requirements) · [Installation](https://docs.js.wiki/setup/installation) · [Navigation](https://docs.js.wiki/guide/navigation) · [MCP](https://docs.js.wiki/dev/mcp)

---

## 백업 — `wikijs3_backup.sh`

시놀로지 작업 스케줄러에 등록해서 사용.
- DB: `pg_dump -F c` → 임시 파일로 받고 **성공했을 때만** 교체, 날짜별 보관 (`data/db/wikibackup_YYMMDD_HHMM.dump`)
- 파일: `SOURCE_DIR` 전체 `tar.gz` (DB 원본 폴더를 SOURCE_DIR 아래에 바인드 마운트했다면 `PG_DATA_SUBDIR` 로 제외)
- `KEEP_DAYS`(기본 7일) 지난 백업 삭제, `backup.log` 기록
- DB 덤프 실패 시 종료 코드 1 (스케줄러 알림용)

복원:
```bash
docker exec -i wikijs3_db pg_restore -l < wikibackup_XXXX.dump | head        # 파일 확인
docker exec -i wikijs3_db pg_restore -U wikijs -d wiki --clean --if-exists < wikibackup_XXXX.dump
```
