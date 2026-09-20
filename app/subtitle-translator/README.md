# 자막 번역기 (Subtitle Translator)

SRT / ASS / SSA / VTT 자막을 한국어로 자동 번역하는 GUI 프로그램.

## 기능
- 번역 엔진 선택: Claude, OpenAI(호환 API 포함), DeepL, Google 번역(Cloud Translation API), Ollama(로컬 LLM)
- 배치 번역 + 직전 대사 문맥 전달로 말투/대명사 일관성 유지
- 서식 태그(`<i>`, `{\an8}`, 줄바꿈 등) 보존
- 입력 인코딩 자동 감지(UTF-8, UTF-16, CP949 등), 출력 인코딩 선택
- 이중 자막(번역 + 원문) 옵션
- 작품 정보 / 용어집 입력
- 여러 파일·폴더 일괄 처리, 결과는 `원본명.ko.확장자`로 저장

## 실행
```bash
pip install -r requirements.txt
python subtitle_translator.py
```

## exe 빌드
- **로컬**: `build.bat` 실행 → `dist\SubtitleTranslator.exe`
- **GitHub Actions**: Actions 탭 → *Build subtitle-translator* → Run workflow,
  또는 `subtitle-translator-v*` 태그 push 시 Release에 exe 첨부

## 설정 파일
API 키 등 설정은 `~/.subtitle_translator.json`에 평문으로 저장됩니다. 저장소에 커밋하지 마세요.
