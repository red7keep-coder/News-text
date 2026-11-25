"""1단계 수집: 목록 → 고르기 → 받기 → 사실 뽑기 → 사건 묶기. 글은 아직 쓰지 않는다.

  ① 목록    켜 둔 언론사의 자체 사이트맵 · RSS에서 지난 실행 이후 새 기사(주소 · 제목 · 시각)
  ② 고르기  제목만 보고 시장 · 업종 · 기업 · 경제 정책 기사를 고르고 같은 사건끼리 이름을 맞춘다 (Gemini lite)
  ③ 받기    고른 기사 본문을 받는다 (실행당 MAX_FETCH건, 여러 곳이 다룬 사건 먼저). 본문은 메모리에서만
  ④ 사실    기사 몇 건씩 묶어 사실(누가 · 언제 · 무엇을 · 숫자 · 짧은 발언)을 뽑는다 (Gemini lite)
  ⑤ 사건    지난 36시간의 사실을 사건으로 묶고(같은 사실끼리도 묶음), 몇 곳이 썼는지 · 숫자가 두 곳 이상에서 맞는지 센다 (Gemini lite)

저장(Firestore, 비공개): articles · eventsets · runs · outlet_state · eventnames, outlets.feed
블로그 저장소(BLOG_PROJECT, 블로그가 읽기만): headlines — 고르기를 통과한 제목 묶음(본문 없음, 문서당 1,500건). 30일 지나면 지운다
  facts — 정기 회차 ⑤의 사건별 사실(언론사 · 주소 · 제목 없음, 문서당 900KB, 15절). 30일 지나면 지운다
가벼운 회차(LIGHT=1, 평일 14:40 예약): ① 목록 · ② 고르기만 하고 headlines만 쓴다. '이미 본 것'으로 남기지 않는다
  (그래야 16:30 회차가 같은 기사를 다시 골라 본문을 받는다 — 요청-알파스코프-뉴스-글-발행 11절 Q12)
공개 로그: 건수 · 글자 수 · 호출 수만. 언론사 이름 · 주소 · 제목 · 본문 · AI 입출력은 찍지 않는다.
"""
import hashlib
import html
import json
import os
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher

import llm
import names
import net

KST = timezone(timedelta(hours=9))
MAX_FETCH = int(os.environ.get("MAX_FETCH") or 50)     # 실행당 본문을 받을 기사 수 (하루 두 번 → 100건)
FRESH_HOURS = 36           # 이보다 오래된 목록 항목은 보지 않는다
EVENT_HOURS = 36           # 사건 묶기에 넣을 사실의 기간
SEEN_CAP = 5000            # 언론사마다 기억할 '이미 본 기사' 수
TITLE_CHUNK = 1500         # 고르기 한 번에 넣을 제목 수
HEADLINES_PER_DOC = 1500   # headlines 문서 하나에 담을 제목 수 (Firestore 문서 1MB 한도, 블로그 읽기 수를 줄이려고 몰아 담음)
HEADLINES_DAYS = 30        # 이보다 오래된 headlines 문서는 지운다
FACTS_DOC_BYTES = 900_000  # facts 문서 하나 크기 (Firestore 1MB 한도에 여유를 둠)
CARRY_HOURS = 24           # 이 기간 실행들의 사건 이름을 이어 쓴다 (11절 Q13)
LIGHT = os.environ.get("LIGHT") == "1"
OUTLOOK_EXTRA = 10         # 시장 전망 기사를 본문 받기에 따로 더 넣을 수 (MAX_FETCH와 따로, 25절 Q25)
PER_EVENT = 3              # 한 사건에서 본문을 받을 최대 언론사 수
FACT_BATCH_N = 5           # 사실 뽑기 한 번에 넣을 기사 수
FACT_BATCH_CHARS = 40_000  # 사실 뽑기 한 번에 넣을 본문 글자 수 (기사를 자르지 않고 묶음을 나눈다)

CATEGORIES = ["RATES_FED", "INFLATION", "GROWTH_JOBS", "TARIFF_TRADE", "GEOPOLITICS", "OIL_ENERGY", "AI_CAPEX",
              "EARNINGS", "CORP_EVENT", "CREDIT_BANKS", "MARKET_STRUCTURE", "REGULATION", "FISCAL_POLITICS",
              "FX_GLOBAL", "CRYPTO", "REAL_ESTATE", "OTHER"]   # 유튜브 수집기 · 블로그와 같은 분류표 (데이터-설계.md)
# 정의는 alphascope-docs 이슈-분류-코드표.md를 국내 기사에 맞게 줄인 것. 코드만 주면 모델이 이름으로 짐작해 어긋난다 (11-19: 연구 성과 → EARNINGS)
CAT_DEFS = {
    "RATES_FED": "금리 · 중앙은행: 한국은행 · 연준 등 금리 결정, 통화정책, 국채 금리",
    "INFLATION": "물가: 소비자 · 생산자 물가, 임금, 기대 인플레",
    "GROWTH_JOBS": "경기 · 고용 · 소비: GDP, 고용 · 실업, 소매판매, 소비자심리",
    "TARIFF_TRADE": "관세 · 무역: 관세, 무역 협상, 수출입 실적, 공급망 재편 (수출 통제는 REGULATION)",
    "GEOPOLITICS": "지정학: 전쟁 · 휴전, 외교 · 안보, 제재, 재난",
    "OIL_ENERGY": "유가 · 에너지 · 원자재: 원유, 가스, 전력, 원전, 금 · 원자재",
    "AI_CAPEX": "AI 투자 · 반도체 수요: 데이터센터, GPU · HBM 수요, AI 설비투자, AI 사업 진출",
    "EARNINGS": "실적: 매출 · 영업이익 등 실적 발표, 가이던스, 컨센서스, 목표가. 실적 숫자가 핵심일 때만",
    "CORP_EVENT": "기업 사건: 인수합병, 상장 · 공모, 자사주, 분할, 배당, 증자 · 감자 · 사채, 경영진, 대형 계약, 신제품 · 신사업, 설비 투자, 노사",
    "REGULATION": "규제 · 산업정책: 법안, 금융당국 규제, 수출 통제, 보조금, 소송 · 판결, 인허가",
    "FISCAL_POLITICS": "재정 · 정치: 예산, 세제, 국가 부채, 선거 · 정권, 신용등급",
    "CREDIT_BANKS": "금융 시스템 · 신용: 은행 · 보험 · 카드사, 대출 · 부실, 신용 경색, 금융사고",
    "MARKET_STRUCTURE": "수급 · 시장 구조: 지수 등락 · 마감 시황, 외국인 · 기관 수급, 공매도, 지수 편입, 거래 제도",
    "FX_GLOBAL": "환율 · 해외 시장: 원 · 달러 환율, 외환 보유 · 대외 자산, 해외 증시 급변",
    "CRYPTO": "암호화폐: 비트코인, 거래소, 스테이블코인",
    "REAL_ESTATE": "부동산: 집값, 분양, 재건축, 주택 대출 금리, 리츠",
    "OTHER": "그 밖: 시장과 관련은 있으나 위 어디에도 안 맞는 것 (예: 기술 · 연구 성과)",
}
CAT_TEXT = "\n" + "\n".join(f"- {c}: {CAT_DEFS[c]}" for c in CATEGORIES)

PICK_PROMPT = """아래는 국내 언론사 기사 제목 목록입니다. 한 줄 형식: 번호|언론사번호|제목
주식 투자 블로그의 사건별 글 재료로 쓸 기사만 고르세요.

고르는 것: 국내 증시(코스피 · 코스닥)와 수급, 업종 · 산업 동향, 기업(실적 · 공시 · 투자 · 인수합병 · 신사업 · 소송),
경제 정책 · 규제 · 세제, 환율 · 금리 · 물가 · 경기지표, 해외 사건 중 국내 시장이나 기업에 영향이 드러나는 것,
증권사 · 운용사 · 은행의 시장 전망(지수 밴드 · 목표 · 환율 · 금리 전망 · 업종 비중 의견 — 종목 추천 나열이 아니면 고르세요).
빼는 것: 스포츠, 연예, 사건사고, 생활 · 건강, 시장 영향이 드러나지 않는 일반 정치, 인사 · 부고 · 동정, 행사 홍보,
사진 · 영상 안내, 칼럼 · 사설 · 기자수첩, 종목 추천 나열, 광고성 기사. 애매하면 빼세요.

고른 기사마다 event에 그 기사가 다루는 사건 이름을 붙이세요 (명사구 3 ~ 8단어, 회사 · 기관 이름이 있으면 넣기).
다른 언론사가 같은 사건을 썼으면 반드시 똑같은 event 이름을 쓰세요.
cat은 다음 분류 코드 중 하나: {cats}

JSON만 답하세요: {{"keep":[{{"i":번호,"event":"사건 이름","cat":"코드"}}]}}

목록:
{lines}
"""

MERGE_PROMPT = """아래는 여러 언론사 기사 제목에 붙인 사건 이름 목록입니다. 한 줄 형식: 번호|사건 이름|예시 제목
제목을 나눠서 따로 이름을 붙였기 때문에, 같은 소식에 다른 이름이 붙어 있을 수 있습니다.
같은 소식을 가리키는 번호끼리 묶으세요. 같은 소식이란:
- 같은 회사 · 인물 · 기관의 같은 일(같은 발표 · 같은 정책 · 같은 지표 발표 · 같은 날 같은 시장 마감)
- 그 일에 대한 직접 반응(주가 · 시장 · 업계 · 정치권 반응)과 후속 소식(조사 · 해명 · 영향 분석)
  예: "A사 대표 복귀" · "A사 대표 복귀에 주가 급등" · "A사 대표 복귀로 업종 투자심리 회복" · "A사 대표 복귀에 의회 조사 착수"는 한 묶음입니다.
묶지 않는 것: 주제만 비슷한 다른 회사 · 다른 발표, 같은 회사라도 서로 관계없는 다른 일(예: 실적 발표와 별개의 소송),
다른 날 나온 같은 정기 지표(날마다 다른 사건).
두 개 이상 묶이는 것만 답하세요. 묶음마다 소식 전체를 가장 잘 나타내는 이름의 번호를 맨 앞에 두세요.
줄 끝에 (앞 실행)이 붙은 이름은 앞선 실행에서 이미 쓴 사건 이름입니다. 같은 사건이면 그 줄과 함께 묶으세요.

JSON만 답하세요: {{"groups":[[번호,번호]]}}

목록:
{lines}
"""

FACTS_PROMPT = """아래 기사들에서 사실만 뽑으세요. 결과는 내부 재료이며, 기사 문장을 옮기면 안 됩니다.

뽑는 것: 누가 · 언제 · 무엇을 했나, 숫자(발표치 · 예상치 · 금액 · 비율 · 날짜), 누가 말했는지 분명한 직접 인용 발언(한두 문장),
그 사실의 원문 출처(공시 · 보도자료 · 정부 발표 · 통계)가 무엇인지.
뽑지 않는 것: 기자의 해석 · 전망 · 평가 · 비유, 증권사 연구원 · 전문가의 견해(짧은 발언 하나만 quote로 가능),
사진 · 그래프 · 표 설명, 기사에 없는 내용.

규칙:
- what은 사실 하나를 당신의 말로 새로 쓴 한 문장(60자 안팎). 기사 문장 · 제목을 그대로 쓰거나 어순만 바꾸지 마세요.
- "검토 중 · 추진 · 논의" 같은 아직 정해지지 않은 사실도 넣고 stage를 discussing으로 적으세요.
- direction은 그 사실이 관련 종목 · 업종 · 자산 가격에 주는 방향(기사에 드러난 대로) up / down / neutral. 모르면 neutral.
- 매수 · 매도를 권하는 말을 만들지 마세요.
- quote.text는 기사에서 따옴표로 직접 인용한 말만, 따옴표 안 글자 그대로 옮기세요(이것만은 기사 글자 그대로). 다듬거나 간접 인용을 직접 인용으로 바꾸지 마세요.
  speaker는 기사에 적힌 이름(이름이 없으면 "업계 관계자"처럼 기사 표현 그대로), title은 직함, venue는 말한 자리(기자간담회 · 국회 · 실적 발표 · 인터뷰 등, 모르면 빈 값).
- what · who에 한국 사람 이름은 대통령 · 국무총리 · 장관 · 국회의장 · 여야 당대표 · 한국은행 총재 · 금융위원장 · 금융감독원장 ·
  공정거래위원장 · 국세청장 · 그룹 회장만 씁니다. 그 밖의 사람은 이름 없이 '현대모비스 기획팀 부장'처럼 소속 · 직함으로 씁니다.
  외국 사람은 '엔비디아 젠슨 황 CEO'처럼 소속 · 이름 · 직함을 함께 씁니다. (quote.speaker와 people에는 이름을 그대로 적으세요.)
- people은 그 사실에 관련된, 기사에 이름이 적힌 사람 전부입니다(what에 이름을 쓰지 않았어도 넣으세요). name은 기사에 적힌 이름 그대로(성만 있으면 빈 값으로 넣지 말고 빼세요),
  org는 소속 회사 · 기관, title은 직함, foreign은 외국 사람이면 true. 사람이 없으면 빈 배열.
- 숫자는 기사에 적힌 값 그대로(단위 포함). 계산하거나 반올림하지 마세요.
- when은 기사에 적힌 만큼만 씁니다. 연월만 있으면 YYYY-MM, 연도만 있으면 YYYY. 없는 월 · 일을 01로 채우지 마세요.
- 숫자가 알맹이인 사실은 what에 숫자를 넣으세요. "자산총계 · 실적이 공개되었다"처럼 무엇이 나왔다는 말만 있는 문장은 쓰지 마세요.
- 기사 하나에 사실 1 ~ 8개. 사실이 없는 기사(칼럼 · 목록 기사 등)는 facts를 빈 배열로.
- cat은 다음 분류 코드 중 하나: {cats}

outlooks — 사실과 따로, 투자회사가 낸 시장 전망 숫자 · 결론만 기사마다 뽑습니다(없으면 빈 배열):
- 대상(target): 코스피 · 코스닥 · 원달러 · 기준금리 · 국고채3년 · 국고채10년 · 미국10년물 · 연준금리 중 하나, 또는 업종 비중이면 업종 이름.
  지수는 연간 범위(밴드) · 연말 목표 · 다음 달 범위. 종목 목표가는 넣지 않습니다.
- firm은 전망한 곳 이름(증권사 · 운용사 · 은행 · 해외 투자은행 · 리서치 회사). 사람 이름은 넣지 않습니다.
  kind: 회사면 firm, 정부 · 한국은행 · 국제기구면 official, "전문가 n%가 ~" 같은 설문이면 survey.
- 값이 하나면 value, 범위면 low · high. 기사에 적힌 숫자 그대로(쉼표는 있어도 됨, 계산 · 환산하지 않음). unit은 pt · 원 · % · 회 중 하나.
- stance는 업종 비중만: 확대 · 중립 · 축소(최선호 · 탑픽은 확대).
- horizon은 전망 기한이 끝나는 달 YYYY-MM(기사 게시일 기준: 내년 연간이면 다음 해 -12, 다음 달 범위면 그 달), period는 year · half · quarter · month.
- revised: 앞 전망을 올렸으면 up, 내렸으면 down, 아니면 빈 값. scenario: 강세 · 기본 · 약세처럼 나뉘어 있으면 그 이름, 아니면 빈 값.
- 기사에 없는 칸은 비웁니다. 지어내지 마세요.

JSON만 답하세요:
{{"articles":[{{"id":"기사 id","outlooks":[{{"firm":"","kind":"firm|official|survey","target":"","value":"","low":"","high":"",
"unit":"","stance":"","horizon":"YYYY-MM","period":"year|half|quarter|month","revised":"","scenario":""}}],"facts":[{{"what":"","who":"","when":"YYYY-MM-DD · YYYY-MM · YYYY 또는 빈 값",
"numbers":[{{"name":"","value":"","unit":"","kind":"발표|예상|기타"}}],
"quote":{{"speaker":"","title":"","venue":"","text":""}} 또는 null,
"people":[{{"name":"","org":"","title":"","foreign":false}}],
"stocks":[{{"name":"","code":"6자리 종목코드 또는 빈 값"}}],
"sector":"","cat":"코드","direction":"up|down|neutral","stage":"confirmed|discussing",
"origin":"공시|보도자료|정부 발표|통계|없음"}}]}}]}}

기사들:
{articles}
"""

EVENTS_PROMPT = """아래는 여러 언론사 기사에서 뽑은 사실 목록입니다. 한 줄 형식: 사실id|언론사번호|분류|내용
같은 사건을 다룬 사실끼리 묶으세요. 같은 회사의 같은 발표 · 같은 정책 · 같은 지표 발표면 같은 사건입니다.
주제가 비슷해도 다른 회사 · 다른 발표면 다른 사건입니다.
모든 사실id는 정확히 한 사건에 들어가야 합니다 (사실 하나뿐인 사건도 됩니다).
한 사건 안에서는 같은 사실(다른 기사가 같은 내용 · 같은 숫자를 전한 것)끼리 한 묶음으로 넣으세요.
facts는 묶음의 목록입니다. 예: [["사실id","사실id"],["사실id"]]. 내용이 조금이라도 다르면 다른 묶음입니다.
사건마다 title을 새로 쓴 한 문장(40자 안팎)으로, cat을 아래 분류 코드 중 하나로 붙이세요. 분류: {cats}
사건마다 scheduled와 surprise도 붙이세요.
- scheduled: 미리 일정이 잡혀 있던 발표 · 결정 · 보고면 true(실적 발표 · 금통위 · FOMC · CPI · 고용 · 수출입 통계 등). 예고 없이 생긴 일이면 false.
- surprise: scheduled가 true일 때만, 시장 예상 대비 결과. -2 훨씬 나쁨 · -1 나쁨 · 0 예상과 같음 · +1 좋음 · +2 훨씬 좋음.
  사실 목록에 예상치([예상] 표시 숫자 · 컨센서스 · 전망치)가 없거나 scheduled가 false면 0. 예상치를 지어내지 마세요.
  예: 영업이익 1조 2천억 원, 시장 전망 9천억 원 → +2.

JSON만 답하세요: {{"events":[{{"title":"","cat":"코드","scheduled":false,"surprise":0,"facts":[["사실id"]]}}]}}

사실 목록:
{lines}
"""


def log(msg):
    print(msg, flush=True)


def aid(url):
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]


def clean_title(t):
    """목록 제목에 남은 HTML 기호(&apos; · 두 번 감싼 &amp;quot;)를 푼다."""
    for _ in range(2):
        t = html.unescape(t)
    return t.strip()


def norm_label(s):
    return re.sub(r"[\s·,.'\"“”‘’]+", "", (s or "").lower())


# ---------- ① 목록 ----------

def list_new(outlets, states, now, stats):
    """새 기사 후보 [{id, no, url, title, ts}]와 언론사별 새로 본 id.
    outlets 문서의 선택 칸: feeds(사람이 적은 목록 주소), freshHours(목록 시각이 하루 단위처럼 늦은 곳),
    titleFromPage(제목 없는 목록만 있는 곳: 가장 새 기사 N건의 페이지 제목을 받는다)."""
    cands, new_ids, feeds = [], {}, {}
    for o in outlets:
        notes = []
        key = f"{o['no']:02d}"
        fresh_after = (now - timedelta(hours=o.get("freshHours") or FRESH_HOURS)).timestamp()
        seen = set(states.get(o["no"], {}).get("seen", []))
        title_n = int(o.get("titleFromPage") or 0)
        try:
            feed, items = net.discover(o["host"], notes, override=o.get("feeds"), cached=o.get("feed"), need_title=True,
                                       fresh_after=fresh_after, allow_untitled=title_n > 0)
            if feed and title_n and items and not any(ti for _, _, ti in items):
                items = _titles_from_pages(items, seen, fresh_after, title_n, notes)
        except net.Blocked:
            stats["blockedList"].append(o["no"])
            stats["listNotes"][key] = notes
            continue
        except Exception as e:        # 한 곳의 오류로 전체를 멈추지 않는다
            stats["errorList"][key] = type(e).__name__
            stats["listNotes"][key] = notes
            continue
        if not feed:
            stats["noFeed"].append(o["no"])
            stats["listNotes"][key] = notes
            continue
        if feed != o.get("feed"):
            feeds[o["no"]] = feed
        got = 0
        for t, url, title in items:
            i = aid(url)
            if i in seen or (t and t < fresh_after) or not title:
                continue
            cands.append({"id": i, "no": o["no"], "url": url, "title": clean_title(title)[:200], "ts": t})
            new_ids.setdefault(o["no"], []).append(i)
            got += 1
        stats["listed"][key] = got
        if not got:
            stats["listNotes"][key] = notes
    return cands, new_ids, feeds


def _titles_from_pages(items, seen, fresh_after, n, notes):
    """제목 없는 목록: 아직 안 본 새 기사 중 가장 새 n건만 페이지에서 제목을 받는다 (같은 사이트 간격은 net이 지킨다)."""
    todo = [(t, u) for t, u, _ in items if aid(u) not in seen and not (t and t < fresh_after)][:n]
    out = []
    for t, u in todo:
        title = net.page_title(u)       # Blocked는 부르는 쪽으로
        if title:
            out.append((t, u, title))
    notes.append(f"페이지 제목 {len(out)}/{len(todo)}")
    return out


# ---------- ② 고르기 ----------

def pick(cands, stats):
    """고른 후보에 event · cat을 붙여 돌려준다."""
    kept = []
    for s in range(0, len(cands), TITLE_CHUNK):
        chunk = cands[s:s + TITLE_CHUNK]
        lines = "\n".join(f"{k}|{c['no']:02d}|{c['title']}" for k, c in enumerate(chunk))
        out = llm.generate_json(PICK_PROMPT.format(cats=CAT_TEXT, lines=lines), max_out=32768)
        for row in (out or {}).get("keep", []) if isinstance(out, dict) else []:
            try:
                c = chunk[int(row["i"])]
            except (KeyError, ValueError, TypeError, IndexError):
                continue
            c = dict(c, event=str(row.get("event") or c["title"])[:80],
                     cat=row.get("cat") if row.get("cat") in CATEGORIES else "OTHER")
            kept.append(c)
    stats["kept"] = len(kept)
    return kept


def merge_events(kept, stats, prev=None):
    """고르기는 제목을 TITLE_CHUNK개씩 나눠 이름을 붙이므로 같은 사건에 다른 이름이 붙는다 (11-19: 두 곳 이상 사건 0개).
    이름 목록을 한 번 더 보여 주고 같은 사건 이름을 하나로 합친다. 실패해도 고르기는 그대로 간다.
    prev: 지난 실행들의 [(사건 이름, 예시 제목)]. 같은 사건이면 앞 실행 이름을 그대로 쓴다 (블로그가 실행 사이를 잇게, 11-22)."""
    names = {}
    for c in kept:
        names.setdefault(norm_label(c["event"]), (c["event"], c["title"]))
    stats["eventNames"] = len(names)
    old = set()
    for name, title in prev or []:
        k = norm_label(name)
        if k and k not in names:
            names[k] = (name, title)
            old.add(k)
    keys = [k for k in names if k not in old] + [k for k in names if k in old]   # 이번 이름 먼저 (나눠 넣어도 함께 보이게)
    if len(keys) < 2:
        return kept
    remap = {}
    for s in range(0, len(keys), TITLE_CHUNK):
        part = keys[s:s + TITLE_CHUNK]
        lines = "\n".join(f"{k}|{names[key][0]}|{names[key][1][:60]}" + (" (앞 실행)" if key in old else "")
                          for k, key in enumerate(part))
        try:
            out = llm.generate_json(MERGE_PROMPT.format(lines=lines), max_out=16384)
        except llm.LLMError:
            stats["mergeFailed"] = True
            continue
        for g in (out or {}).get("groups", []) if isinstance(out, dict) else []:
            try:
                idx = list(dict.fromkeys(int(i) for i in g if 0 <= int(i) < len(part)))   # 모델 순서 (맨 앞 = 대표 이름)
            except (TypeError, ValueError):
                continue
            if len(idx) < 2:
                continue
            first = next((i for i in idx if part[i] in old), idx[0])    # 앞 실행 이름이 있으면 그 이름으로
            head = remap.get(part[first], part[first])
            for i in idx:
                if i != first and part[i] not in old:
                    remap[part[i]] = head
    out_kept = []
    for c in kept:
        k, hops = norm_label(c["event"]), 0
        while k in remap and remap[k] != k and hops < 20:     # 합친 이름이 다시 합쳐진 경우까지 따라간다
            k, hops = remap[k], hops + 1
        if hops:
            c = dict(c, event=names[k][0])
        out_kept.append(c)
    stats["eventNamesMerged"] = len({norm_label(c["event"]) for c in out_kept})
    stats["eventNamesCarried"] = len({norm_label(c["event"]) for c in out_kept} & old)
    return out_kept


# ---------- 블로그 제목 묶음 ----------

def _kst_iso(dt):
    return dt.astimezone(KST).isoformat(timespec="seconds") if dt else None


def dedupe_titles(kept):
    """21절 R25: 제목이 SAME_TITLE 이상 같은 기사는 하나만 (가장 먼저 나온 것). [(남긴 기사, [뺀 기사 id])]를 원래 순서로."""
    order = sorted(range(len(kept)), key=lambda i: (kept[i].get("ts") or float("inf"), i))
    heads, exact = [], {}                            # heads: [(원래 자리, 제목 키, 뺀 id 목록)]
    sm = SequenceMatcher(autojunk=False)
    for i in order:
        key = _title_key(kept[i]["title"])
        hit = exact.get(key) if key else None
        if hit is None and key:
            sm.set_seq2(key)
            for h in heads:
                if not h[1]:
                    continue
                sm.set_seq1(h[1])
                if sm.real_quick_ratio() >= SAME_TITLE and sm.quick_ratio() >= SAME_TITLE and sm.ratio() >= SAME_TITLE:
                    hit = h
                    break
        if hit is not None:
            hit[2].append(kept[i]["id"])
            continue
        h = (i, key, [])
        heads.append(h)
        if key:
            exact[key] = h
    return [(kept[i], dropped) for i, _, dropped in sorted(heads, key=lambda h: h[0])]


def headline_items(kept, names, now, stocks=None, stats=None):
    """고르기를 통과한 제목 → headlines 항목. 본문 · 사실은 넣지 않는다 (요청-알파스코프-뉴스-글-발행 10 · 11절).
    같은 제목은 하나만 넘기고, 뺀 기사에 붙은 종목은 남긴 기사로 옮긴다 (21절 R25)."""
    stocks = stocks or {}
    rows = dedupe_titles(kept)
    if stats is not None:
        stats["headlinesDupDropped"] = len(kept) - len(rows)
    out = []
    for c, dropped in rows:
        st = {}
        for i in [c["id"]] + dropped:
            for s in stocks.get(i, []):
                st.setdefault(s["name"], s)
        out.append({"title": html.unescape(c["title"]), "outlet": names.get(c["no"], ""), "url": c["url"],
                    "publishedAt": _kst_iso(datetime.fromtimestamp(c["ts"], timezone.utc)) if c.get("ts") else None,
                    "foundAt": _kst_iso(now), "event": c["event"], "cat": c.get("cat") or "",
                    "stocks": list(st.values())[:5]})
    return out


def write_headlines(blog_db, run_id, items, now, light):
    """headlines/{실행 번호}-{n}: 문서당 HEADLINES_PER_DOC건. 같은 실행이 다시 쓰면 덮어쓴다."""
    parts = [items[i:i + HEADLINES_PER_DOC] for i in range(0, len(items), HEADLINES_PER_DOC)] or [[]]
    batch = blog_db.batch()
    for n, chunk in enumerate(parts, 1):
        batch.set(blog_db.collection("headlines").document(f"{run_id}-{n}"), {
            "run": str(run_id), "part": n, "parts": len(parts), "createdAt": now, "version": 1,
            "light": light, "count": len(chunk), "items": chunk})
    batch.commit()
    return len(parts)


def prune_headlines(blog_db, now, coll="headlines"):
    from google.cloud.firestore_v1.base_query import FieldFilter
    old = list(blog_db.collection(coll)
               .where(filter=FieldFilter("createdAt", "<", now - timedelta(days=HEADLINES_DAYS))).stream())
    for d in old:
        d.reference.delete()
    return len(old)


# ---------- 블로그 사건별 사실 (15절 R17 ~ R22) ----------
# 언론사가 드러나는 칸(sources · outlets · 사실의 no · article, 숫자의 outlets)은 넘기지 않는다 (9절).

def _blog_quote(q):
    if not q:
        return None
    verified = bool(q.get("verified"))                  # 확인 전 저장된 발언은 확인 안 된 것으로
    return {"speaker": q.get("speaker", ""), "title": q.get("title", ""), "venue": q.get("venue", ""),
            "text": q.get("text", "") if verified else "", "verified": verified,
            "anonymous": bool(q.get("anonymous", ANON.search(q.get("speaker", ""))))}


def blog_event(ev):
    rows = {}
    for f in ev["facts"]:
        rows.setdefault(f.get("group", id(f)), []).append(f)
    facts = []
    ppl = names.people(ev["facts"])                     # 사람 이름 규칙 (23절 알림 · 사용자 11-25: 사실 줄에도)
    for g in rows.values():                             # 같은 사실 묶음에서 한 줄: 확인된 발언 · 숫자가 많은 것
        f = max(g, key=lambda f: (bool(f.get("quote") and f["quote"].get("verified")), len(f["numbers"])))
        what = names.scrub_text(f["what"], ppl)
        if not what:                                    # 이름을 직함으로 못 바꾼 줄은 넘기지 않는다
            continue
        facts.append({"what": what, "who": names.scrub_text(f["who"], ppl) or "", "when": f["when"],
                      "numbers": [{k: n[k] for k in ("name", "value", "unit", "kind")} for n in f["numbers"]],
                      "quote": _blog_quote(names.scrub_quote(f.get("quote"), ppl)), "stocks": f["stocks"], "sector": f["sector"],
                      "cat": f["cat"], "direction": f["direction"], "stage": f["stage"], "origin": f["origin"],
                      "sources": f.get("sources", 1)})
    stocks, info = Counter(), {}
    for f in ev["facts"]:
        for st in f.get("stocks") or []:
            stocks[st["name"]] += 1
            d = info.setdefault(st["name"], {"name": st["name"], "code": ""})
            d["code"] = d["code"] or st.get("code", "")
    sectors = Counter(f["sector"].strip() for f in ev["facts"] if f.get("sector", "").strip())
    return {"event": ev.get("event") or ev["title"], "cat": ev["cat"], "direction": ev["direction"], "stage": ev["stage"],
            "sourceCount": ev["sourceCount"], "outletCount": ev["outletCount"], "single": ev["single"],
            "scheduled": bool(ev.get("scheduled")), "surprise": ev.get("surprise", 0),
            "stocks": [info[n] for n, _ in stocks.most_common(10)],
            "sector": sectors.most_common(1)[0][0] if sectors else "",
            "numbers": [{k: n[k] for k in ("name", "value", "unit", "kind", "sources", "agreed")} for n in ev["numbers"]],
            "facts": facts}


def write_facts(blog_db, run_id, events, copied_dropped, now):
    """facts/{실행 번호}-{n}: 사건 전부, 문서 크기 FACTS_DOC_BYTES마다 나눈다 (버리지 않음)."""
    rows = [blog_event(e) for e in events if e["facts"]]
    parts = _parts(rows)
    batch = blog_db.batch()
    for n, chunk in enumerate(parts, 1):
        batch.set(blog_db.collection("facts").document(f"{run_id}-{n}"), {
            "run": str(run_id), "part": n, "parts": len(parts), "createdAt": now, "version": 1,
            "count": len(chunk), "copiedDropped": copied_dropped, "events": chunk})
    batch.commit()
    return len(rows), len(parts)


def choose(kept, limit, same=None):
    """독립 출처 여러 곳이 다룬 사건부터, 사건마다 서로 다른 출처 PER_EVENT곳까지. 남으면 한 출처만 쓴 사건을 언론사 돌아가며."""
    groups = {}
    for c in kept:
        g = groups.setdefault(norm_label(c["event"]), {})
        g.setdefault(c["no"], c)                      # 한 사건에서 언론사당 한 건
    picks = {}                                        # 사건마다 출처당 가장 최근 한 건
    for key, g in groups.items():
        cs = sorted(g.values(), key=lambda c: -c["ts"])
        by_src = {}
        for c, s in zip(cs, source_ids([(c["no"], c["title"]) for c in cs], same or {})):
            by_src.setdefault(s, c)
        picks[key] = list(by_src.values())
    multi = sorted((k for k in groups if len(picks[k]) >= 2), key=lambda k: -len(picks[k]))
    chosen = []
    for k in multi:
        for c in picks[k][:PER_EVENT]:
            if len(chosen) >= limit:
                return chosen, len(multi)
            chosen.append(c)
    singles = {}
    for k in groups:
        if len(picks[k]) == 1:
            c = picks[k][0]
            singles.setdefault(c["no"], []).append(c)
    for v in singles.values():
        v.sort(key=lambda c: -c["ts"])
    while len(chosen) < limit and any(singles.values()):
        for no in list(singles):
            if singles[no] and len(chosen) < limit:
                chosen.append(singles[no].pop(0))
    return chosen, len(multi)


# ---------- ③ 받기 ----------

def fetch_all(chosen, hosts, stats):
    """본문을 받는다. 언론사를 번갈아 받아 같은 사이트 간격을 겹쳐 쓴다. {id: 본문}"""
    by = {}
    for c in chosen:
        by.setdefault(c["no"], []).append(c)
    order = []
    while any(by.values()):
        for no in list(by):
            if by[no]:
                order.append(by[no].pop(0))
    texts, blocked = {}, set()
    for c in order:
        if c["no"] in blocked:
            c["fetch"] = {"verdict": "건너뜀(같은 언론사 막힘)"}
            continue
        try:
            row, text = net.get_article(c["url"], hosts[c["no"]])
        except net.Blocked as b:
            row, text = b.args[0], None
            blocked.add(c["no"])
        except Exception as e:
            row, text = {"verdict": f"오류({type(e).__name__})"}, None
        c["fetch"] = row
        if text:
            texts[c["id"]] = text
    stats["fetched"] = len(texts)
    stats["fetchFailed"] = len(chosen) - len(texts)
    stats["blockedFetch"] = sorted(blocked)
    return texts


# ---------- ④ 사실 ----------

def _clean_fact(f):
    if not isinstance(f, dict) or not str(f.get("what") or "").strip():
        return None
    q = f.get("quote") if isinstance(f.get("quote"), dict) else None
    if q and (not q.get("text") or not q.get("speaker") or len(str(q["text"])) > 200):
        q = None
    if q:
        q = {"speaker": str(q["speaker"]).strip()[:60], "title": str(q.get("title") or "").strip()[:60],
             "venue": str(q.get("venue") or "").strip()[:40], "text": str(q["text"]).strip()}
    nums = []
    for n in f.get("numbers") or []:
        if isinstance(n, dict) and str(n.get("value") or "").strip():
            nums.append({"name": str(n.get("name") or "")[:60], "value": str(n["value"])[:40],
                         "unit": str(n.get("unit") or "")[:20],
                         "kind": n.get("kind") if n.get("kind") in ("발표", "예상", "기타") else "기타"})
    stocks = [{"name": str(s.get("name") or "")[:40],
               "code": s.get("code") if re.fullmatch(r"\d{6}", str(s.get("code") or "")) else ""}
              for s in (f.get("stocks") or []) if isinstance(s, dict) and s.get("name")]
    people = [{"name": str(p["name"]).strip()[:40], "org": str(p.get("org") or "").strip()[:60],
               "title": str(p.get("title") or "").strip()[:60], "foreign": p.get("foreign") is True}
              for p in (f.get("people") or []) if isinstance(p, dict) and len(str(p.get("name") or "").strip()) >= 2][:8]
    return {
        "what": str(f["what"]).strip()[:200], "who": str(f.get("who") or "")[:80], "when": str(f.get("when") or "")[:20],
        "numbers": nums, "quote": q, "people": people, "stocks": stocks, "sector": str(f.get("sector") or "")[:40],
        "cat": f.get("cat") if f.get("cat") in CATEGORIES else "OTHER",
        "direction": f.get("direction") if f.get("direction") in ("up", "down", "neutral") else "neutral",
        "stage": f.get("stage") if f.get("stage") in ("confirmed", "discussing") else "confirmed",
        "origin": f.get("origin") if f.get("origin") in ("공시", "보도자료", "정부 발표", "통계") else "",
    }


def copied(what, text, n=25):
    """what에 기사 본문과 n글자 이상 같은 구간이 있나 (띄어쓰기 무시)."""
    a, b = re.sub(r"\s+", "", what), re.sub(r"\s+", "", text)
    return any(a[i:i + n] in b for i in range(0, max(0, len(a) - n + 1), 3))


_QUOTE_MARKS = re.compile(r"[\s\"'“”‘’「」『』〈〉《》·.,…]")
ANON = re.compile(r"관계자|업계|당국자|소식통|측근|핵심 ?인사|고위 ?인사|정통한|익명|한 인사|일각")


def quote_in(text, body):
    """발언이 본문에 글자 그대로 있나 (띄어쓰기 · 따옴표 · 문장부호 무시). copied 확인을 반대로 쓴 것."""
    a, b = _QUOTE_MARKS.sub("", text), _QUOTE_MARKS.sub("", body)
    return bool(a) and a in b


def check_quote(q, body):
    """R19 · R20: verified(본문에 그대로 있나, 없으면 text를 비움) · anonymous(이름 없는 말한 사람)."""
    ok = quote_in(q["text"], body)
    return dict(q, text=q["text"] if ok else "", verified=ok, anonymous=bool(ANON.search(q["speaker"])))


def batches(items, texts):
    cur, size = [], 0
    for c in items:
        n = len(texts[c["id"]])
        if cur and (len(cur) >= FACT_BATCH_N or size + n > FACT_BATCH_CHARS):
            yield cur
            cur, size = [], 0
        cur.append(c)
        size += n
    if cur:
        yield cur


# ---------- 시장 전망 (25절 R32 ~ R35) ----------
# 증권사 전망 기사는 대개 한 곳만 써서 본문 받기(여러 곳 사건 먼저)에서 빠진다. 제목으로 골라 OUTLOOK_EXTRA건을 따로 더 받는다.
OUTLOOK_WORD = re.compile(r"전망|밴드|목표|비중\s?확대|비중\s?축소|최선호|탑픽|톱픽|상단|하단")
OUTLOOK_FIRM = re.compile(r"증권|투자|자산운용|운용|은행|리서치|골드만|모건|JP|씨티|BofA|뱅크오브아메리카|노무라|UBS|HSBC|바클레이즈"
                          r"|도이치|맥쿼리|CLSA|블랙록|피델리티|웰스파고")
OUTLOOK_TARGETS = ["코스피", "코스닥", "원달러", "기준금리", "국고채3년", "국고채10년", "미국10년물", "연준금리"]
OUTLOOK_ALIAS = {"원달러환율": "원달러", "원화": "원달러", "환율": "원달러", "국고채3년물": "국고채3년", "국고채10년물": "국고채10년",
                 "미국채10년": "미국10년물", "미국채10년물": "미국10년물", "미국10년": "미국10년물", "미국국채10년물": "미국10년물",
                 "연방기금금리": "연준금리", "미국기준금리": "연준금리", "한국은행기준금리": "기준금리", "한국기준금리": "기준금리"}


def outlook_title(title):
    return bool(OUTLOOK_WORD.search(title or "") and OUTLOOK_FIRM.search(title or ""))


def pick_outlooks(kept, chosen, n):
    """본문 받기에 못 든 전망 제목 중 가장 새 n건."""
    have = {c["id"] for c in chosen}
    cands = sorted((c for c in kept if c["id"] not in have and outlook_title(c["title"])), key=lambda c: -(c.get("ts") or 0))
    return cands[:n]


def _num_text(v):
    v = re.sub(r"[\s,]", "", str(v or ""))
    return v if re.fullmatch(r"-?\d+(?:\.\d+)?", v) else ""


def _clean_outlook(o, body):
    """R32 칸 정리 + R33 verified (숫자가 본문에 그대로 있나 — 쉼표 · 띄어쓰기 무시)."""
    if not isinstance(o, dict) or not str(o.get("firm") or "").strip():
        return None
    t = re.sub(r"[\s·/\-]", "", str(o.get("target") or ""))
    t = OUTLOOK_ALIAS.get(t, t)
    stance = o.get("stance") if o.get("stance") in ("확대", "중립", "축소") else ""
    if not t or (t not in OUTLOOK_TARGETS and not stance):      # 업종은 비중 의견이 있을 때만
        return None
    nums = {k: _num_text(o.get(k)) for k in ("value", "low", "high")}
    if not any(nums.values()) and not stance:
        return None
    flat = re.sub(r"[\s,]", "", body or "")
    if any(nums.values()):
        ok = all(re.search(rf"(?<![\d.]){re.escape(v)}(?![\d])", flat) for v in nums.values() if v)
    else:
        ok = t in flat                                             # 숫자 없는 업종 비중: 업종 이름이 본문에 있나
    return {"firm": str(o["firm"]).strip()[:40],
            "kind": o.get("kind") if o.get("kind") in ("firm", "official", "survey") else "firm",
            "target": t[:30], **nums,
            "unit": o.get("unit") if o.get("unit") in ("pt", "원", "%", "회") else "",
            "stance": stance,
            "horizon": o.get("horizon") if re.fullmatch(r"\d{4}-\d{2}", str(o.get("horizon") or "")) else "",
            "period": o.get("period") if o.get("period") in ("year", "half", "quarter", "month") else "",
            "revised": o.get("revised") if o.get("revised") in ("up", "down") else "",
            "scenario": str(o.get("scenario") or "").strip()[:20], "verified": ok}


def build_outlooks(arts, same=None):
    """지난 EVENT_HOURS 기사의 전망을 회사 · 대상 · 기한 · 값으로 묶고, 실은 독립 출처 수를 센다 (R34, ⑤와 같은 방식)."""
    groups = {}
    for a in arts:
        for o in a.get("outlooks") or []:
            k = (re.sub(r"\s|\(주\)|㈜", "", o["firm"]), o["target"], o["horizon"], o["value"], o["low"], o["high"],
                 o["stance"], o["scenario"])
            groups.setdefault(k, []).append((a, o))
    items = []
    for rows in groups.values():
        art_list = list({a["id"]: a for a, _ in rows}.values())
        src = source_ids([(a["no"], a.get("title", "")) for a in art_list], same or {})
        pub = [a["publishedAt"] for a in art_list if a.get("publishedAt")]
        o = next((o for _, o in rows if o["verified"]), rows[0][1])
        items.append(dict(o, verified=any(x["verified"] for _, x in rows), sources=len(set(src)),
                          published=min(pub).astimezone(KST).strftime("%Y-%m-%d") if pub else ""))
    items.sort(key=lambda x: (x["target"], x["horizon"], x["firm"]))
    return items


def _parts(rows):
    """FACTS_DOC_BYTES마다 나눈 목록 (버리지 않음, 비어도 한 묶음)."""
    parts, cur, size = [], [], 0
    for r in rows:
        n = len(json.dumps(r, ensure_ascii=False).encode()) + 100
        if cur and size + n > FACTS_DOC_BYTES:
            parts.append(cur)
            cur, size = [], 0
        cur.append(r)
        size += n
    return parts + [cur] if cur or not parts else parts


def write_outlooks(blog_db, run_id, items, now):
    """outlooks/{실행 번호}-{n} (R34). 0건이어도 -1 문서는 쓴다."""
    parts = _parts(items)
    batch = blog_db.batch()
    for n, chunk in enumerate(parts, 1):
        batch.set(blog_db.collection("outlooks").document(f"{run_id}-{n}"), {
            "run": str(run_id), "part": n, "parts": len(parts), "createdAt": now, "version": 1,
            "count": len(chunk), "items": chunk})
    batch.commit()
    return len(parts)


def extract_facts(chosen, texts, stats):
    have = [c for c in chosen if c["id"] in texts]
    stats.update(facts=0, copiedFacts=0, factBatchesFailed=0, quotes=0, quotesUnverified=0, outlooks=0, outlooksUnverified=0)
    for batch in batches(have, texts):
        parts = []
        for c in batch:
            when = datetime.fromtimestamp(c["ts"], KST).strftime("%Y-%m-%d %H:%M") if c["ts"] else "모름"
            parts.append(f"=== id: {c['id']} | 언론사 {c['no']:02d} | 게시 {when} ===\n제목: {c['title']}\n{texts[c['id']]}")
        try:
            out = llm.generate_json(FACTS_PROMPT.format(cats=CAT_TEXT, articles="\n\n".join(parts)), max_out=16384)
        except llm.LLMError:
            stats["factBatchesFailed"] += 1
            continue
        rows = [a for a in (out or {}).get("articles", []) if isinstance(a, dict)] if isinstance(out, dict) else []
        got = {str(a.get("id")): a.get("facts") or [] for a in rows}
        outs = {str(a.get("id")): a.get("outlooks") or [] for a in rows}
        for c in batch:
            if c["id"] not in got:
                continue
            ols = [o for o in (_clean_outlook(x, texts[c["id"]]) for x in outs.get(c["id"], [])) if o][:20]
            c["outlooks"] = ols
            stats["outlooks"] += len(ols)
            stats["outlooksUnverified"] += sum(1 for o in ols if not o["verified"])
            facts = []
            for f in got[c["id"]]:
                f = _clean_fact(f)
                if not f:
                    continue
                if f["quote"]:
                    f["quote"] = check_quote(f["quote"], texts[c["id"]])
                    stats["quotes"] += 1
                    stats["quotesUnverified"] += not f["quote"]["verified"]
                if copied(f["what"], texts[c["id"]]):
                    f["copied"] = True          # 기사 문장과 겹침. 글에 쓰지 않는다 (받는 쪽이 거름)
                    stats["copiedFacts"] += 1
                facts.append(f)
            c["facts"] = facts[:8]
            stats["facts"] += len(c["facts"])
    for c in chosen:
        texts.pop(c["id"], None)                # 본문은 여기서 모두 버린다


# ---------- ⑤ 사건 ----------

def num_key(n):
    """값과 단위를 붙여 띄어쓰기 · 쉼표를 뺀다. '1조 달러' · '1조달러' · ('1조', '달러')를 같은 숫자로 본다."""
    return re.sub(r"[\s,]", "", f"{n['value']}{n['unit']}")


# ---------- 서로 다른 출처 세기 ----------
# 같은 기사를 받아 실은 언론사를 두 곳으로 세면 '두 곳 이상' 확인이 무너진다 (11-19: 파이낸셜뉴스가 뉴시스 기사를 그대로 실음).
# 두 가지로 묶는다: outlets의 sameSource(같은 출처로 볼 언론사 번호), 그리고 제목이 거의 같은 기사.
SAME_TITLE = 0.85          # 제목 유사도가 이 이상이면 같은 기사를 받아 실은 것으로 본다


def _title_key(t):
    return re.sub(r"[^0-9A-Za-z가-힣]", "", re.sub(r"\[[^\]]*\]", "", t or ""))


def source_ids(items, same):
    """items: [(언론사 번호, 제목)], same: {번호: 같은 출처 번호}. 항목마다 '독립 출처' 번호를 돌려준다."""
    parent = list(range(len(items)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    keys = [_title_key(t) for _, t in items]
    for i in range(len(items)):
        for j in range(i):
            a, b = items[i][0], items[j][0]
            if (a == b or same.get(a, a) == same.get(b, b)
                    or (keys[i] and keys[j] and SequenceMatcher(None, keys[i], keys[j]).ratio() >= SAME_TITLE)):
                parent[find(i)] = find(j)
    return [find(i) for i in range(len(items))]


def event_surprise(scheduled, surprise, facts):
    """19절 R24: 예고된 발표(scheduled)이고 사실에 예상치 숫자(kind 예상)가 있을 때만 모델 값(-2 ~ +2), 아니면 0."""
    if not scheduled or not any(n.get("kind") == "예상" for f in facts for n in f.get("numbers") or []):
        return 0
    try:
        return max(-2, min(2, int(surprise)))
    except (TypeError, ValueError):
        return 0


def build_events(arts, stats, same=None):
    """arts: [{id, no, url, title, publishedAt, facts}] → 사건 목록.
    확인 수(sourceCount)는 언론사 수가 아니라 독립 출처 수다 (source_ids). 숫자 일치도 독립 출처 두 곳 이상이어야 한다."""
    same = same or {}
    fidx, lines = {}, []
    for a in arts:
        for j, f in enumerate(a.get("facts") or []):
            if f.get("copied"):
                continue
            fid = f"{a['id'][:10]}-{j}"
            fidx[fid] = (a, f)
            nums = "; ".join(f"{n['name']} {n['value']}{n['unit']}" + ("[예상]" if n.get("kind") == "예상" else "")
                             for n in f["numbers"])
            lines.append(f"{fid}|{a['no']:02d}|{f['cat']}|{f['what']}" + (f" ({nums})" if nums else ""))
    if not lines:
        return []
    out = llm.generate_json(EVENTS_PROMPT.format(cats=CAT_TEXT, lines="\n".join(lines)), max_out=32768)
    groups, used = [], set()
    for e in (out or {}).get("events", []) if isinstance(out, dict) else []:
        same_facts = []                                      # 같은 사실 묶음 (예전 꼴 ["id", ...]도 받는다)
        for g in e.get("facts") or []:
            g = [x for x in (g if isinstance(g, list) else [g]) if isinstance(x, str) and x in fidx and x not in used]
            if g:
                used.update(g)
                same_facts.append(g)
        if same_facts:
            groups.append((str(e.get("title") or "")[:120], e.get("cat") if e.get("cat") in CATEGORIES else "OTHER",
                           same_facts, e.get("scheduled") is True, e.get("surprise")))
    for fid in fidx:                                         # 모델이 빠뜨린 사실은 혼자 한 사건
        if fid not in used:
            groups.append((fidx[fid][1]["what"][:120], fidx[fid][1]["cat"], [[fid]], False, 0))
    events = []
    for title, cat, same_facts, scheduled, surprise in groups:
        ids = [x for g in same_facts for x in g]
        outlets = sorted({fidx[x][0]["no"] for x in ids})
        art_list = list({fidx[x][0]["id"]: fidx[x][0] for x in ids}.values())
        src = dict(zip((a["id"] for a in art_list), source_ids([(a["no"], a.get("title", "")) for a in art_list], same)))
        nums = {}
        for x in ids:
            a, f = fidx[x]
            for n in f["numbers"]:
                k = num_key(n)
                d = nums.setdefault(k, {"name": n["name"], "value": n["value"], "unit": n["unit"], "kind": n["kind"],
                                        "outlets": set(), "src": set()})
                d["outlets"].add(a["no"])
                d["src"].add(src[a["id"]])
        numbers = [dict({k: v for k, v in d.items() if k != "src"}, outlets=sorted(d["outlets"]),
                        sources=len(d["src"]), agreed=len(d["src"]) >= 2) for d in nums.values()]
        source_count = len(set(src.values()))
        dirs = [fidx[x][1]["direction"] for x in ids]
        sources = {}
        for x in ids:
            a = fidx[x][0]
            sources[a["id"]] = {"no": a["no"], "url": a["url"], "title": a["title"], "publishedAt": a.get("publishedAt")}
        names = Counter(a.get("event") for a in art_list if a.get("event"))
        fact_rows = []
        for k, g in enumerate(same_facts):               # 같은 사실 묶음마다 실은 곳(독립 출처) 수
            n_src = len({src[fidx[x][0]["id"]] for x in g})
            fact_rows += [dict(fidx[x][1], no=fidx[x][0]["no"], article=fidx[x][0]["id"], group=k, sources=n_src)
                          for x in g]
        events.append({
            "title": title, "event": names.most_common(1)[0][0] if names else title,   # event: headlines와 같은 이름
            "cat": cat, "outlets": outlets, "outletCount": len(outlets),
            "sourceCount": source_count, "single": source_count < 2,
            "facts": fact_rows,
            "numbers": numbers, "agreedNumbers": sum(1 for n in numbers if n["agreed"]),
            "direction": ("up" if dirs.count("up") > dirs.count("down") else
                          "down" if dirs.count("down") > dirs.count("up") else "neutral"),
            "stage": "discussing" if all(fidx[x][1]["stage"] == "discussing" for x in ids) else "confirmed",
            "scheduled": scheduled,
            "surprise": event_surprise(scheduled, surprise, [fidx[x][1] for x in ids]),
            "sources": list(sources.values()),
        })
    events.sort(key=lambda e: (-e["sourceCount"], -e["outletCount"], -len(e["facts"])))
    stats["events"] = len(events)
    stats["copiedDropped"] = sum(1 for a in arts for f in a.get("facts") or [] if f.get("copied"))
    stats["multiEvents"] = sum(1 for e in events if not e["single"])
    return events


# ---------- 실행 ----------

def main():
    from google.cloud import firestore
    from google.cloud.firestore_v1.base_query import FieldFilter

    db = firestore.Client(project=os.environ["GCP_PROJECT"])
    blog_db = (firestore.Client(project=os.environ["BLOG_PROJECT"], database=os.environ.get("BLOG_DATABASE") or "(default)")
               if os.environ.get("BLOG_PROJECT") else None)
    now = datetime.now(timezone.utc)
    run_id = os.environ.get("GITHUB_RUN_ID") or now.strftime("local-%Y%m%d%H%M%S")
    stats = {"listed": {}, "blockedList": [], "noFeed": [], "errorList": {}, "listNotes": {}, "stage": "목록", "light": LIGHT}
    run_ref = db.collection("runs").document(str(run_id))
    try:
        outlets = sorted((d.to_dict() for d in db.collection("outlets").stream() if d.to_dict().get("enabled", True)),
                         key=lambda o: o["no"])
        hosts = {o["no"]: o["host"] for o in outlets}
        same = {o["no"]: int(o["sameSource"]) for o in outlets if o.get("sameSource")}   # 같은 출처로 셀 언론사
        states = {}
        for o in outlets:
            s = db.collection("outlet_state").document(f"{o['no']:02d}").get()
            states[o["no"]] = s.to_dict() if s.exists else {}

        # ① 목록
        cands, new_ids, feeds = list_new(outlets, states, now, stats)
        for no, feed in feeds.items():
            db.collection("outlets").document(f"{no:02d}").update({"feed": feed, "feedAt": now})
        log(f"① 목록: 언론사 {len(outlets)}곳 중 {len(stats['listed'])}곳에서 새 기사 {len(cands):,}건 "
            f"(목록 없음 {len(stats['noFeed'])}곳, 막힘 {len(stats['blockedList'])}곳, 오류 {len(stats['errorList'])}곳)")
        if not llm.key():
            stats["stage"] = "키 없음"
            log("GEMINI_API_KEY가 없어 목록만 보고 끝냅니다 (이미 본 기사로 기록하지 않음)")
            return
        if not cands:
            stats["stage"] = "새 기사 없음"
            return

        # ② 고르기 (지난 CARRY_HOURS 실행들의 사건 이름을 이어 쓴다)
        stats["stage"] = "고르기"
        prev = []
        for d in db.collection("eventnames").where(
                filter=FieldFilter("createdAt", ">=", now - timedelta(hours=CARRY_HOURS))).stream():
            prev += [tuple(x) for x in d.to_dict().get("names", []) if isinstance(x, list) and len(x) == 2]
        kept = merge_events(pick(cands, stats), stats, prev)
        pairs = {}
        for c in kept:
            pairs.setdefault(norm_label(c["event"]), [c["event"], c["title"][:60]])
        db.collection("eventnames").document(str(run_id)).set({"createdAt": now, "light": LIGHT, "names": list(pairs.values())})
        chosen, multi = ([], 0) if LIGHT else choose(kept, MAX_FETCH, same)
        extra = [] if LIGHT else pick_outlooks(kept, chosen, OUTLOOK_EXTRA)   # 시장 전망 기사 몫 (25절)
        stats["outlookTitles"] = sum(1 for c in kept if outlook_title(c["title"]))
        stats["outlookExtra"] = len(extra)
        chosen = chosen + extra
        stats["chosen"], stats["multiGroups"] = len(chosen), multi
        log(f"② 고르기: {len(kept):,}건 남김, 사건 이름 {stats.get('eventNames', 0)}개 → 합쳐서 {stats.get('eventNamesMerged', 0)}개 "
            f"(앞 실행 이름 이어 씀 {stats.get('eventNamesCarried', 0)}개), 두 곳 이상이 다룬 사건 {multi}개 → 본문 받기 {len(chosen)}건 "
            f"(전망 제목 {stats['outlookTitles']}건 중 따로 더함 {stats['outlookExtra']}건)")
        out_names = {o["no"]: o.get("name", "") for o in outlets}
        if blog_db is not None:
            try:
                items = headline_items(kept, out_names, now, stats=stats)
                n = write_headlines(blog_db, run_id, items, now, LIGHT)
                stats["headlines"] = len(items)
                log(f"   블로그 제목 묶음: {len(items):,}건(같은 제목 {stats['headlinesDupDropped']}건 뺌) → 문서 {n}개, "
                    f"오래된 문서 {prune_headlines(blog_db, now)}개 지움")
            except Exception as e:                        # 블로그 저장소가 안 돼도 수집은 이어 간다
                log(f"   블로그 제목 묶음을 못 씀 ({type(e).__name__})")
        if LIGHT:                                         # 가벼운 회차: '이미 본 것'으로 남기지 않고 끝낸다
            stats["stage"] = "끝(가벼운 회차)"
            return
        # 고르기까지 끝난 기사는 '이미 본 것'으로 남긴다 (다음 실행에서 제목을 다시 넣지 않게)
        batch = db.batch()
        for no, ids in new_ids.items():
            seen = (states.get(no, {}).get("seen", []) + ids)[-SEEN_CAP:]
            batch.set(db.collection("outlet_state").document(f"{no:02d}"), {"seen": seen, "lastRunAt": now})
        batch.commit()

        # ③ 받기
        stats["stage"] = "받기"
        texts = fetch_all(chosen, hosts, stats)
        log(f"③ 받기: 성공 {stats['fetched']}건, 실패 {stats['fetchFailed']}건, 막힌 언론사 {len(stats['blockedFetch'])}곳, "
            f"본문 평균 {sum(map(len, texts.values())) // max(1, len(texts)):,}자")

        # ④ 사실
        stats["stage"] = "사실"
        extract_facts(chosen, texts, stats)
        log(f"④ 사실: 사실 {stats['facts']}개 (기사 문장과 겹친 줄 {stats['copiedFacts']}개, 실패한 묶음 {stats['factBatchesFailed']}개, "
            f"발언 {stats['quotes']}개 중 본문에 없음 {stats['quotesUnverified']}개, "
            f"전망 {stats['outlooks']}개 중 숫자가 본문에 없음 {stats['outlooksUnverified']}개)")
        if blog_db is not None and stats.get("headlines") is not None:   # 본문을 받은 기사에는 종목을 붙여 다시 쓴다
            stocks = {}
            for c in chosen:
                seen_st = {}
                for f in c.get("facts", []):
                    for st in f.get("stocks", []):
                        seen_st.setdefault(st["name"], st)
                if seen_st:
                    stocks[c["id"]] = list(seen_st.values())[:5]
            try:
                write_headlines(blog_db, run_id, headline_items(kept, out_names, now, stocks), now, LIGHT)
            except Exception as e:
                log(f"   블로그 제목 묶음에 종목을 못 붙임 ({type(e).__name__})")
        batch = db.batch()
        for c in chosen:
            doc = {"no": c["no"], "url": c["url"], "title": c["title"],
                   "publishedAt": datetime.fromtimestamp(c["ts"], timezone.utc) if c["ts"] else None,
                   "foundAt": now, "run": str(run_id), "event": c["event"], "cat": c["cat"],
                   "fetch": c.get("fetch", {}), "facts": c.get("facts", []), "factCount": len(c.get("facts", [])),
                   "outlooks": c.get("outlooks", []),
                   "model": ",".join(llm.MODELS)}
            if "facts" in c:
                doc["extractedAt"] = now
            batch.set(db.collection("articles").document(c["id"]), doc)
        batch.commit()

        # ⑤ 사건 (지난 실행의 사실까지 함께)
        stats["stage"] = "사건"
        since = now - timedelta(hours=EVENT_HOURS)
        arts = [dict(d.to_dict(), id=d.id) for d in
                db.collection("articles").where(filter=FieldFilter("extractedAt", ">=", since)).stream()]
        events = build_events(arts, stats, same)
        db.collection("eventsets").document(str(run_id)).set({
            "createdAt": now, "since": since, "articles": len(arts), "events": events,
            "edition": now.astimezone(KST).strftime("%Y-%m-%d %H시"),
        })
        log(f"⑤ 사건: 기사 {len(arts)}건에서 사건 {stats.get('events', 0)}개, 두 곳 이상 {stats.get('multiEvents', 0)}개")
        if blog_db is not None:
            try:
                n, parts = write_facts(blog_db, run_id, events, stats.get("copiedDropped", 0), now)
                stats["blogFacts"] = n
                log(f"   블로그 사건별 사실: 사건 {n}개 → 문서 {parts}개, 오래된 문서 {prune_headlines(blog_db, now, 'facts')}개 지움")
            except Exception as e:                        # 블로그 저장소가 안 돼도 수집은 이어 간다
                log(f"   블로그 사건별 사실을 못 씀 ({type(e).__name__})")
            try:                                          # 시장 전망 (25절 R34, 지난 EVENT_HOURS 기사로 출처를 센다)
                items = build_outlooks(arts, same)
                parts = write_outlooks(blog_db, run_id, items, now)
                stats["blogOutlooks"] = len(items)
                log(f"   블로그 시장 전망: {len(items)}건(본문 확인 {sum(1 for x in items if x['verified'])}건, "
                    f"두 곳 이상 {sum(1 for x in items if x['sources'] >= 2)}건) → 문서 {parts}개, "
                    f"오래된 문서 {prune_headlines(blog_db, now, 'outlooks')}개 지움")
            except Exception as e:
                log(f"   블로그 시장 전망을 못 씀 ({type(e).__name__})")
        stats["stage"] = "끝"
    except llm.LLMError as e:
        stats["llmError"] = str(e)
        log(f"Gemini 실패({stats['stage']} 단계): {e}")
        raise
    finally:
        stats["requests"] = net.stats["requests"]
        stats["llm"] = {"calls": llm.stats["calls"], "byModel": llm.stats["byModel"], "errors": llm.stats["errors"]}
        stats["startedAt"], stats["finishedAt"] = now, datetime.now(timezone.utc)
        run_ref.set(stats)
        log(f"사이트 요청 {net.stats['requests']}번, Gemini 호출 {llm.stats['calls']}번 {llm.stats['byModel']}"
            + (f", 오류 {llm.stats['errors']}" if llm.stats["errors"] else ""))


if __name__ == "__main__":
    main()
