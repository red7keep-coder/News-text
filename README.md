# News-text

국내 뉴스 기사에서 사실(누가 · 언제 · 무엇을 · 숫자)만 뽑아, 여러 언론사가 함께 다룬 사건마다 글을 새로 쓰는 수집기.

## 원칙

- 기사 본문은 실행 중 메모리에서만 쓰고 버린다. 파일 · 로그 · 저장소 · DB 어디에도 쓰지 않는다.
- robots.txt를 따른다. 403 · 429 · 차단 화면 · 로그인 · 유료 기사는 받지 않고, 브라우저인 척하거나 우회하지 않는다.
- 같은 사이트는 10초 이상 간격을 둔다. 이름표는 `NewsTextCollector/0.1`.
- 이 저장소는 공개라 실행 로그에 기사 내용 · 주소 · AI 입출력을 찍지 않는다. 결과는 비공개 Firestore에만 쓴다.

## 지금 단계

하루 두 번(07:00 · 16:30 KST) `collect` 작업이 1단계 · 2단계를 이어서 돌린다.

- **1단계 사실 뽑기** (`worker/collect.py`): 목록 → 제목으로 고르기 → 본문 받기 → 사실 뽑기 → 사건 묶기.
- **2단계 글쓰기** (`worker/write.py`): 독립 출처 두 곳 이상인 사건을 골라, 사실 목록과 두 곳 이상이 맞춘 숫자만으로 글을 쓴다. 기사 본문은 주지 않는다. 코드 검사(재료에 없는 숫자 · 매수 · 매도 권유 · 제목 겹침 · 되풀이 · 분량 등)에 걸리면 고쳐 쓰게 하고, 검사를 통과한 글은 사실 확인(Flash Lite가 문장마다 재료에 근거가 있는지 봄)을 한 번 더 거쳐, 근거 없는 문장은 지운다. 결과는 Firestore `posts`에 '초안' 또는 '검사 실패'로 남긴다. 07:00 두 편 · 16:30 세 편.
- **블로그 넘기기** (`worker/write.py`): '초안'은 블로그가 읽는 저장소(`BLOG_PROJECT`)에 글 칸(제목 · 본문 · 날짜 · 분류 · 종목 · 업종 · slug)만 쓴다. 참고 기사 · 언론사 · 재료는 넘기지 않는다. 블로그는 들어온 글을 전부 자동으로 발행한다.

| 파일 | 하는 일 |
| --- | --- |
| `worker/net.py` | robots · 간격 · 목록(사이트맵 · RSS) · 본문 추출 |
| `worker/robots.py` | robots.txt 읽기 |
| `worker/llm.py` | Gemini 호출 (입력 · 응답 원문은 로그에 남기지 않음) |
| `worker/collect.py` | 1단계 수집 |
| `worker/write.py` | 2단계 글쓰기 · 검사 · 사실 확인 · 초안 저장 · 블로그 넘기기 |
| `worker/probe.py` | 0단계 접속 시험 (손으로만) |

글쓰기만 따로 돌릴 때는 `write` 작업을 손으로 실행한다(`write_max`로 글 수 지정, 언론사에는 접속하지 않음).

## 설정

- 저장소 변수(Variables): `GCP_PROJECT` · `WIF_PROVIDER` · `WIF_SERVICE_ACCOUNT`, 선택 `GEMINI_MODELS`(쉼표로, 비우면 `gemini-3.5-flash-lite,gemini-3.1-flash-lite`), 선택 `GEMINI_WRITE_MODELS`(글쓰기용, 비우면 `gemini-4-hydrogen,gemini-3.6-flash`), 선택 `GEMINI_CHECK_MODELS`(사실 확인용, 비우면 `gemini-3.5-flash-lite,gemini-3.1-flash-lite`), `BLOG_PROJECT` · 선택 `BLOG_DATABASE`(블로그가 읽는 Firestore, 비우면 블로그에 넘기지 않음). Google Cloud에는 키 파일 없이 접속한다.
- 비밀 값(Secrets): `GEMINI_API_KEY`. 없으면 목록만 보고 끝낸다.
