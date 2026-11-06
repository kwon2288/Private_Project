#!/usr/bin/env python3
"""
Wiki.js 2.x — 경로 변경(wikijs2_fix_paths.py)으로 깨진 '본문 내부 링크'를 새 경로로 일괄 수정.

fix_paths 로그의 [MOVE] 줄에서 (예전 경로 -> 새 경로) 목록을 만들고,
모든 페이지 본문에서 예전 경로를 가리키는 링크를 찾아 새 경로로 바꾼다.
(바로 다음 줄이 '!! 실패'인 MOVE는 실제로 옮겨지지 않았으므로 제외)

처리하는 링크 형태:
  [글](/업무/a/NBU_x)             [글](/ko/업무/a/NBU_x#섹션)
  [글](https://wiki.kwonmin.com/업무/a/NBU_x)
  [글](/%EC%97%85%EB%AC%B4/...)   (URL 인코딩된 링크)
  <a href="/업무/a/NBU_x">         [id]: /업무/a/NBU_x  (참조형 링크)
→ 앞부분(도메인, /ko)과 #앵커는 그대로 두고 경로만 교체

사용법:
  export WIKI_URL=http://127.0.0.1:<2.x 포트>
  export WIKI_TOKEN=<Full Access API 키>
  python3 -u wikijs2_fix_links.py fix_paths_*.log            # 미리보기
  python3 -u wikijs2_fix_links.py fix_paths_*.log --apply    # 적용
  (로그 파일 여러 개 지정 가능 — 재시도 로그까지 같이 넣으면 됨)
"""
import json
import os
import re
import sys
import urllib.parse
import urllib.request

WIKI_URL = os.environ.get("WIKI_URL", "http://127.0.0.1:3000").rstrip("/")
TOKEN = os.environ.get("WIKI_TOKEN")
APPLY = "--apply" in sys.argv
LOGS = [a for a in sys.argv[1:] if not a.startswith("--")]

MOVE_RE = re.compile(r"^\[MOVE\]\s+(.+?)\s{2}->\s{2}(.+?)\s*$")

# 링크 대상 추출: (앞부분)(대상)
LINK_RES = [
    re.compile(r"""(?P<pre>\]\(\s*<?)(?P<url>[^)\s>]+)"""),                      # [txt](url)
    re.compile(r"""(?P<pre>href\s*=\s*["'])(?P<url>[^"']+)"""),                   # href="url"
    re.compile(r"""(?P<pre>^[ \t]*\[[^\]\n]+\]:[ \t]*<?)(?P<url>[^\s>]+)""", re.M),  # [id]: url
]

HOST_PREFIXES = ("https://wiki.kwonmin.com", "http://wiki.kwonmin.com", WIKI_URL)
LOCALE_RE = re.compile(r"^/(ko|en)(?=/)")


def load_mapping(logs):
    mapping = {}
    for path in logs:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
        for i, line in enumerate(lines):
            m = MOVE_RE.match(line)
            if not m:
                continue
            nxt = lines[i + 1] if i + 1 < len(lines) else ""
            if "!! 실패" in nxt:
                continue
            mapping[m.group(1)] = m.group(2)
    # 연쇄 이동(a->b, b->c) 정리
    for k in list(mapping):
        seen = set()
        v = mapping[k]
        while v in mapping and v not in seen:
            seen.add(v)
            v = mapping[v]
        mapping[k] = v
    return mapping


def rewrite_url(url, mapping):
    """링크 url이 예전 경로를 가리키면 새 url 반환, 아니면 None."""
    host = ""
    rest = url
    for h in HOST_PREFIXES:
        if h and rest.startswith(h + "/"):
            host, rest = h, rest[len(h):]
            break
    if not rest.startswith("/"):
        return None

    anchor = ""
    for sep in ("#", "?"):
        if sep in rest:
            idx = rest.index(sep)
            rest, anchor = rest[:idx], rest[idx:] + anchor
            break

    locale = ""
    lm = LOCALE_RE.match(rest)
    if lm:
        locale, rest = lm.group(0), rest[lm.end():]

    encoded = "%" in rest
    decoded = urllib.parse.unquote(rest).strip("/")
    new = mapping.get(decoded)
    if not new:
        return None
    new_part = urllib.parse.quote(new, safe="/-_") if encoded else new
    return f"{host}{locale}/{new_part}{anchor}"


def gql(query, variables=None):
    req = urllib.request.Request(
        f"{WIKI_URL}/graphql",
        data=json.dumps({"query": query, "variables": variables or {}}).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {TOKEN}"},
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        res = json.load(r)
    if res.get("errors"):
        raise RuntimeError(res["errors"])
    return res["data"]


SINGLE_Q = """query($id:Int!){ pages { single(id:$id){
  id path locale title description editor content isPublished isPrivate
  publishStartDate publishEndDate scriptCss scriptJs tags { tag } } } }"""

UPDATE_M = """mutation($id:Int!,$content:String!,$description:String!,$editor:String!,
  $isPublished:Boolean!,$isPrivate:Boolean!,$locale:String!,$path:String!,
  $publishStartDate:Date,$publishEndDate:Date,$scriptCss:String,$scriptJs:String,
  $tags:[String]!,$title:String!){
  pages { update(id:$id, content:$content, description:$description, editor:$editor,
    isPublished:$isPublished, isPrivate:$isPrivate, locale:$locale, path:$path,
    publishStartDate:$publishStartDate, publishEndDate:$publishEndDate,
    scriptCss:$scriptCss, scriptJs:$scriptJs, tags:$tags, title:$title) {
    responseResult { succeeded message } } } }"""


def main():
    if not LOGS:
        sys.exit("fix_paths 로그 파일 경로를 인자로 주세요. 예) python3 wikijs2_fix_links.py fix_paths_*.log")
    if not TOKEN:
        sys.exit("WIKI_TOKEN 환경변수에 2.x API 키를 넣어 주세요.")

    mapping = load_mapping(LOGS)
    print(f"이동 목록 {len(mapping)}건 로드 ({', '.join(LOGS)})")

    pages = gql("{ pages { list(limit: 100000) { id path } } }")["pages"]["list"]
    print(f"전체 페이지 {len(pages)}개 검사 시작\n")

    n_pages = n_links = n_fail = 0
    for item in sorted(pages, key=lambda p: p["path"]):
        p = gql(SINGLE_Q, {"id": item["id"]})["pages"]["single"]
        content = p["content"] or ""
        changes = []

        def repl(m):
            new_url = rewrite_url(m.group("url"), mapping)
            if not new_url:
                return m.group(0)
            changes.append((m.group("url"), new_url))
            return m.group("pre") + new_url

        new_content = content
        for rx in LINK_RES:
            new_content = rx.sub(repl, new_content)

        if not changes:
            continue

        n_pages += 1
        n_links += len(changes)
        print(f"{'[UPDATE]' if APPLY else '[DRY]'} {p['path']}  ({len(changes)}개 링크)")
        for old, new in changes:
            print(f"    {old}  ->  {new}")

        if not APPLY:
            continue

        r = gql(UPDATE_M, {
            "id": p["id"],
            "content": new_content,
            "description": p["description"] or "",
            "editor": p["editor"],
            "isPublished": bool(p["isPublished"]),
            "isPrivate": bool(p["isPrivate"]),
            "locale": p["locale"],
            "path": p["path"],
            "publishStartDate": p["publishStartDate"] or None,
            "publishEndDate": p["publishEndDate"] or None,
            "scriptCss": p["scriptCss"] or "",
            "scriptJs": p["scriptJs"] or "",
            "tags": [t["tag"] for t in (p["tags"] or [])],
            "title": p["title"],
        })["pages"]["update"]["responseResult"]
        if not r["succeeded"]:
            n_fail += 1
            print(f"    !! 실패: {r['message']}")

    print(f"\n완료: 수정 페이지 {n_pages}개 / 링크 {n_links}개 / 실패 {n_fail}개"
          + ("" if APPLY else "  (미리보기 모드 — 적용하려면 --apply)"))


if __name__ == "__main__":
    main()
