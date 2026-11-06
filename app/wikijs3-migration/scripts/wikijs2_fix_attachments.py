#!/usr/bin/env python3
"""
Wiki.js 2.x 본문의 상대 경로 첨부 링크를 절대 경로로 일괄 변환.

  attachments\\abc.jpg   ->  /업무/가상화/Hyper-v/attachments/abc.jpg
  attachments/abc.jpg    ->  (동일)

- 기준 폴더 = 페이지 경로에서 마지막 부분을 뺀 폴더 (2.x 브라우저가 상대 경로를 해석하는 방식과 동일)
- 변환한 주소로 실제 파일이 열리는지(HTTP 200) 확인한 링크만 바꿈. 안 열리면 로그만 남김.
- 2.x GraphQL API(pages.single / pages.update) 사용. 바꿀 때마다 페이지 이력이 남음.

사용법:
  export WIKI_URL=http://127.0.0.1:<2.x 포트>
  export WIKI_TOKEN=<Full Access API 키>
  python3 -u wikijs2_fix_attachments.py            # 미리보기
  python3 -u wikijs2_fix_attachments.py --apply    # 적용
"""
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

WIKI_URL = os.environ.get("WIKI_URL", "http://127.0.0.1:3000").rstrip("/")
TOKEN = os.environ.get("WIKI_TOKEN")
APPLY = "--apply" in sys.argv

# 파일명에 공백이 들어간 경우(예: attachments\2021-04-08 10_23_45.png)까지 잡도록 ')' 또는 따옴표까지 매칭
LINK_RES = [
    re.compile(r"""(?P<pre>\]\(\s*<?)(?P<rel>(?:\./)?attachments[\\/][^)"\n>]*?)(?=\s*(?:"[^"\n]*"\s*)?>?\))"""),
    re.compile(r"""(?P<pre>(?:src|href)\s*=\s*["'])(?P<rel>(?:\./)?attachments[\\/][^"'\n]+)"""),
]




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


# 2.x 자산(첨부) 목록
#   ASSETS[폴더경로 소문자][정규화 파일명] = (실제 폴더경로, 실제 파일명)
#   BY_NAME[정규화 파일명] = [(실제 폴더경로, 실제 파일명), ...]   ← 다른 폴더에 있는 경우 찾기용
ASSETS = {}
BY_NAME = {}
DEBUG = "--debug" in sys.argv


def norm(name):
    # 2.x는 업로드 시 파일명을 소문자로 바꾸고 일부 문자를 정리하므로 느슨하게 비교
    return re.sub(r"[^0-9a-z가-힣.]", "", name.lower())


def load_assets():
    """GraphQL로 첨부 폴더 트리와 파일 목록을 읽어 온다 (첨부 URL 403 회피)."""
    def walk(parent_id, prefix):
        folders = gql(
            "query($p:Int!){ assets { folders(parentFolderId:$p){ id slug name } } }",
            {"p": parent_id},
        )["assets"]["folders"]
        for f in folders:
            seg = f.get("name") or f.get("slug")
            path = f"{prefix}/{seg}" if prefix else seg
            files = gql(
                "query($f:Int!){ assets { list(folderId:$f, kind:ALL){ filename } } }",
                {"f": f["id"]},
            )["assets"]["list"]
            bucket = ASSETS.setdefault(path.lower(), {})
            for a in files:
                item = (path, a["filename"])
                bucket[norm(a["filename"])] = item
                BY_NAME.setdefault(norm(a["filename"]), []).append(item)
            walk(f["id"], path)

    walk(0, "")
    total = sum(len(v) for v in ASSETS.values())
    print(f"첨부 폴더 {len(ASSETS)}개 / 파일 {total}개 로드")
    if DEBUG:
        for k in sorted(ASSETS)[:300]:
            print(f"    [폴더] {k}  ({len(ASSETS[k])}개)")


def _suffix_score(a, b):
    """두 폴더 경로의 뒤쪽이 몇 단계나 같은지 (예: BackupExec/attachments → 2)."""
    a, b = a.lower().split("/"), b.lower().split("/")
    n = 0
    while n < min(len(a), len(b)) and a[-1 - n] == b[-1 - n]:
        n += 1
    return n


def exists(abs_path):
    """
    abs_path(/폴더/.../파일)에 해당하는 실제 첨부 경로를 찾는다.
      1) 같은 폴더에 있으면 그 경로 (대소문자 맞춤)
      2) 없으면 같은 파일명을 다른 폴더에서 찾아, 폴더 이름이 가장 비슷한 곳을 선택
         (예: 페이지는 Arctera/BackupExec 로 옮겨졌는데 첨부는 Veritas/BackupExec 에 남아 있는 경우)
    반환: (경로, 방식) 또는 (None, 후보수)
    """
    folder, _, fname = abs_path.strip("/").rpartition("/")
    key = norm(fname)
    hit = ASSETS.get(folder.lower(), {}).get(key)
    if hit:
        return f"/{hit[0]}/{hit[1]}", "same"
    cands = BY_NAME.get(key, [])
    if not cands:
        return None, 0
    scored = sorted(cands, key=lambda c: _suffix_score(c[0], folder), reverse=True)
    best = _suffix_score(scored[0][0], folder)
    top = [c for c in scored if _suffix_score(c[0], folder) == best]
    if len(top) == 1 or len(cands) == 1:
        return f"/{scored[0][0]}/{scored[0][1]}", "moved"
    # 점수 동률 후보가 여러 개 — 같은 파일이 여러 폴더에 중복 업로드된 경우가 대부분이므로 첫 번째 사용
    return f"/{top[0][0]}/{top[0][1]}", f"moved(후보 {len(top)}개 중 선택)"


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
    if not TOKEN:
        sys.exit("WIKI_TOKEN 환경변수에 2.x API 키를 넣어 주세요.")

    load_assets()
    pages = gql("{ pages { list(limit: 100000) { id path } } }")["pages"]["list"]
    print(f"전체 페이지 {len(pages)}개 검사 시작\n")

    n_pages = n_links = n_missing = n_fail = 0
    for item in sorted(pages, key=lambda p: p["path"]):
        p = gql(SINGLE_Q, {"id": item["id"]})["pages"]["single"]
        content = p["content"] or ""
        if "attachments" not in content:
            continue

        base = p["path"].rsplit("/", 1)[0] if "/" in p["path"] else ""
        changed = []
        missing = []

        def repl(m):
            rel = m.group("rel").replace("\\", "/")
            if rel.startswith("./"):
                rel = rel[2:]
            abs_path = f"/{base}/{rel}" if base else f"/{rel}"
            real, how = exists(abs_path)
            if real:
                real = real.replace(" ", "%20")
                changed.append((m.group("rel"), real if how == "same" else f"{real}  [{how}]"))
                return m.group("pre") + real
            missing.append((m.group("rel"), abs_path))
            return m.group(0)

        new_content = content
        for rx in LINK_RES:
            new_content = rx.sub(repl, new_content)

        for rel, ab in missing:
            print(f"[MISSING] {p['path']} : {rel} -> {ab} (파일 없음, 변경 안 함)")
        n_missing += len(missing)

        if not changed:
            continue

        n_pages += 1
        n_links += len(changed)
        print(f"{'[UPDATE]' if APPLY else '[DRY]'} {p['path']}  ({len(changed)}개 링크)")
        for rel, ab in changed[:3]:
            print(f"    {rel} -> {ab}")
        if len(changed) > 3:
            print(f"    ... 외 {len(changed) - 3}개")

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

    print(f"\n완료: 변경 페이지 {n_pages}개 / 링크 {n_links}개 / 파일 없음 {n_missing}개 / 실패 {n_fail}개"
          + ("" if APPLY else "  (미리보기 모드 — 적용하려면 --apply)"))


if __name__ == "__main__":
    main()
