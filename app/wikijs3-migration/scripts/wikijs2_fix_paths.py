#!/usr/bin/env python3
"""
Wiki.js 2.x 페이지 경로를 Wiki.js 3.x 규칙에 맞게 일괄 변경 (2.x에서 실행).

3.x 경로 규칙:
  - 문자/숫자/하이픈/슬래시만 허용 (+ , ( ) ' ’ ® — % ! [ ] 공백 등은 가져오기 실패)
  - 3.x는 경로를 소문자로 저장하고 '_' 를 '-' 로 바꿔 저장함
    → 2.x에서 대소문자/밑줄만 다른 두 페이지는 3.x에서 하나로 합쳐짐(덮어쓰기)

이 스크립트는
  1) 기호와 '_' 를 '-' 로 바꾼 새 경로로 페이지를 이동(2.x GraphQL pages.move)하고
  2) 3.x에서 합쳐질 페이지 쌍(대소문자/밑줄/언어만 다른 경로)을 [COLLISION] 으로 미리 알려 준다.

사용법:
  1) 2.x 관리자 > API Access 에서 API 활성화 + Full Access 키 발급
  2) 2.x 관리자 > Storage 의 Local File System / Git 동기화는 잠시 비활성화
     (켜져 있으면 디스크/깃 쪽 파일이 없을 때 ENOENT 로 이동 실패)
  3) 미리보기(변경 안 함):  python3 -u wikijs2_fix_paths.py | tee fix_paths_dry.log
  4) 실제 적용:            nohup python3 -u wikijs2_fix_paths.py --apply > fix_paths_$(date +%Y%m%d_%H%M).log 2>&1 &

환경변수:
  WIKI_URL   (기본: http://127.0.0.1:3000)  ← 2.x 주소
  WIKI_TOKEN (필수) 2.x API 키

출력 로그의 [MOVE] 줄은 wikijs2_fix_links.py 가 내부 링크를 고칠 때 입력으로 쓴다.
"""
import json
import os
import re
import sys
import urllib.request
from collections import defaultdict

WIKI_URL = os.environ.get("WIKI_URL", "http://127.0.0.1:3000").rstrip("/")
TOKEN = os.environ.get("WIKI_TOKEN")
APPLY = "--apply" in sys.argv


def gql(query, variables=None):
    req = urllib.request.Request(
        f"{WIKI_URL}/graphql",
        data=json.dumps({"query": query, "variables": variables or {}}).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {TOKEN}"},
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        res = json.load(r)
    if res.get("errors"):
        raise RuntimeError(res["errors"])
    return res["data"]


def sanitize(path: str) -> str:
    """3.x에서 그대로 쓸 수 있는 경로로 변환 (대소문자는 유지 — 3.x가 알아서 소문자로 저장)."""
    parts = []
    for seg in path.split("/"):
        # 유니코드 문자/숫자/하이픈만 남기고 나머지(밑줄 포함)는 '-'로
        seg = re.sub(r"[^\w-]|_", "-", seg)
        seg = re.sub(r"-{2,}", "-", seg).strip("-")
        while len(seg.encode("utf-8")) > 240:   # 각 부분 240바이트 제한
            seg = seg[:-1]
        if seg:
            parts.append(seg)
    return "/".join(parts)


def key3x(path: str) -> str:
    """3.x에 저장될 때의 경로 (소문자, '_'→'-', 기호 정리) — 충돌 검사용."""
    return sanitize(path).lower()


def main():
    if not TOKEN:
        sys.exit("WIKI_TOKEN 환경변수에 2.x API 키를 넣어 주세요.")

    pages = gql("{ pages { list(limit: 100000) { id path locale title } } }")["pages"]["list"]
    existing = {(p["locale"], p["path"]) for p in pages}

    # 3.x에서 하나로 합쳐질 경로 미리 알림 (3.x는 언어별 경로를 따로 두지 않으므로 locale 무시)
    groups = defaultdict(list)
    for p in pages:
        groups[key3x(p["path"])].append(p)
    collisions = {k: v for k, v in groups.items() if len(v) > 1}
    for k, v in sorted(collisions.items()):
        names = ", ".join(f"{p['path']}({p['locale']},id={p['id']})" for p in v)
        print(f"[COLLISION] 3.x에서 '{k}' 하나로 합쳐짐: {names}")
    if collisions:
        print(f"→ 충돌 {len(collisions)}건: 가져오기 전에 한쪽을 삭제하거나 이름을 바꿔 두세요.\n")

    targets = [(p, sanitize(p["path"])) for p in pages if sanitize(p["path"]) != p["path"]]
    print(f"전체 {len(pages)}개 중 변경 대상 {len(targets)}개\n")

    ok = fail = skip = 0
    for p, new in sorted(targets, key=lambda t: t[0]["path"]):
        key = (p["locale"], new)
        if key in existing:
            print(f"[SKIP-충돌] {p['path']}  ->  {new}  (이미 존재)")
            skip += 1
            continue
        print(f"{'[MOVE]' if APPLY else '[DRY]'} {p['path']}  ->  {new}")
        if not APPLY:
            continue
        r = gql(
            """mutation($id:Int!,$path:String!,$locale:String!){
                 pages { move(id:$id, destinationPath:$path, destinationLocale:$locale){
                   responseResult { succeeded message } } } }""",
            {"id": p["id"], "path": new, "locale": p["locale"]},
        )["pages"]["move"]["responseResult"]
        if r["succeeded"]:
            ok += 1
            existing.discard((p["locale"], p["path"]))
            existing.add(key)
        else:
            fail += 1
            print(f"    !! 실패: {r['message']}")

    print(f"\n완료: 성공 {ok} / 실패 {fail} / 충돌 건너뜀 {skip}"
          + ("" if APPLY else "  (미리보기 모드 — 적용하려면 --apply)"))


if __name__ == "__main__":
    main()
