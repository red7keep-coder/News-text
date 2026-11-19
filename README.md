# News-text

국내 뉴스 기사에서 사실(누가 · 언제 · 무엇을 · 숫자)만 뽑아, 여러 언론사가 함께 다룬 사건마다 글을 새로 쓰는 수집기.

## 원칙

- 기사 본문은 실행 중 메모리에서만 쓰고 버린다. 파일 · 로그 · 저장소 · DB 어디에도 쓰지 않는다.
- robots.txt를 따른다. 403 · 429 · 차단 화면 · 로그인 · 유료 기사는 받지 않고, 브라우저인 척하거나 우회하지 않는다.
- 같은 사이트는 10초 이상 간격을 둔다. 이름표는 `NewsTextCollector/0.1`.
- 이 저장소는 공개라 실행 로그에 기사 내용 · 주소 · AI 입출력을 찍지 않는다. 결과는 비공개 Firestore에만 쓴다.

## 지금 단계

1단계 사실 뽑기 (`worker/collect.py`, 하루 두 번 07:00 · 16:30 KST). 목록 → 제목으로 고르기 → 본문 받기 → 사실 뽑기 → 사건 묶기. 글은 아직 쓰지 않는다.

| 파일 | 하는 일 |
| --- | --- |
| `worker/net.py` | robots · 간격 · 목록(사이트맵 · RSS) · 본문 추출 |
| `worker/llm.py` | Gemini 호출 (입력 · 응답 원문은 로그에 남기지 않음) |
| `worker/collect.py` | 1단계 수집 |
| `worker/probe.py` | 0단계 접속 시험 (손으로만) |

## 설정

- 저장소 변수(Variables): `GCP_PROJECT` · `WIF_PROVIDER` · `WIF_SERVICE_ACCOUNT`, 선택 `GEMINI_MODELS`(쉼표로, 비우면 `gemini-3.5-flash-lite,gemini-3.1-flash-lite`). Google Cloud에는 키 파일 없이 접속한다.
- 비밀 값(Secrets): `GEMINI_API_KEY`. 없으면 목록만 보고 끝낸다.
