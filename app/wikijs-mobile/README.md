# Wiki Mobile — Wiki.js 모바일 PWA

`https://wiki.kwonmin.com/m/` 에 붙는 모바일 전용 Wiki.js 뷰어입니다.
Wiki.js와 **같은 도메인**에서 서빙되므로 Wiki.js 로그인 세션(jwt 쿠키)을 그대로 쓰고, API 키를 앱에 넣지 않습니다.

## 기능
- 하단 탭: 홈 / 탐색(폴더 트리) / 검색 / 즐겨찾기
- 홈: 최근 본 문서, 최상위 폴더, 최근 업데이트 문서
- 문서: 경로(브레드크럼), 태그, 목차, 글자 크기 조절, 코드 블록 복사, 표 가로 스크롤, 이미지 확대, 위키 내부 링크는 앱 안에서 이동
- 즐겨찾기 / 최근 본 문서 (기기에 저장)
- 오프라인: 한 번 연 문서·목록·이미지는 망이 없을 때도 열림 (Service Worker + Cache)
- 다크 모드 자동 전환

## 파일 구성
```
wikijs-mobile/
├─ index.html      앱 본체 (HTML/CSS/JS 한 파일)
├─ sw.js           Service Worker (오프라인 캐시)
├─ manifest.json   PWA 매니페스트
└─ icons/          앱 아이콘 (180/192/512, maskable)
```

## 배포 (Nginx Proxy Manager)

### 1. 파일 복사
이 폴더(`app/wikijs-mobile`)의 내용을 NPM 컨테이너의 `/data/wiki-m/` 에 복사합니다.

```bash
# 예: NPM docker-compose에서 ./data:/data 로 마운트한 경우
cp -r app/wikijs-mobile  <NPM 설치 폴더>/data/wiki-m
# 컨테이너 안에서 /data/wiki-m/index.html 로 보이면 정상
docker exec <npm 컨테이너명> ls /data/wiki-m
```

### 2. Proxy Host 설정
NPM → Hosts → Proxy Hosts → `wiki.kwonmin.com` → **Edit → Advanced** 탭의 Custom Nginx Configuration에 추가:

```nginx
location = /m {
    return 301 /m/;
}
location /m/ {
    alias /data/wiki-m/;
    index index.html;
    add_header Cache-Control "no-cache";
}
```

저장하면 바로 적용됩니다. (기존 Wiki.js 프록시 설정은 그대로 두면 됩니다.)

### 3. 확인
- PC 브라우저에서 `https://wiki.kwonmin.com/m/` 접속 → 홈 화면이 뜨면 성공
- `https://wiki.kwonmin.com/m/manifest.json` 이 JSON으로 열리는지 확인

## 아이폰 설치
1. **Safari**로 `https://wiki.kwonmin.com/m/` 접속
2. 공유 버튼 → **홈 화면에 추가** → 이름 `Wiki`
3. 홈 화면의 Wiki 아이콘으로 실행
4. 비공개 문서가 안 보이면 홈 화면의 **로그인** 버튼 → Wiki.js 로그인 → 앱으로 자동 복귀

> 홈 화면 앱은 Safari와 로그인 정보(쿠키)가 분리되어 있어서, 앱 안에서 한 번 로그인해야 합니다.
> Wiki.js 세션이 유지되는 동안은 다시 로그인할 필요 없습니다.

## 업데이트
파일을 덮어쓴 뒤 `sw.js`의 `VER` 값을 올리면 앱이 새 버전을 받아 "새 버전이 설치되었습니다" 알림을 띄웁니다.

## 설정 값 (index.html 상단)
```js
const CFG = {
  locale: 'ko',        // Wiki.js 로케일
  gql: '/graphql',     // GraphQL 엔드포인트
  title: 'Wiki',
  ver: '1.0.0'
};
```

## 참고 / 제한
- 편집은 Wiki.js 원래 편집기(Safari)로 열립니다. 앱은 열람 전용입니다.
- 검색 결과는 Wiki.js에 설정된 검색 엔진(기본 DB 검색은 제목·설명 위주)을 따릅니다.
- Mermaid 다이어그램 등 Wiki.js가 브라우저에서 그리는 요소는 원문 텍스트로 보일 수 있습니다.
- Cloudflare 프록시를 쓰는 경우 `/m/` 파일이 캐시되어 업데이트가 늦게 보이면 Cloudflare에서 캐시 퍼지를 하세요.
