"""2단계 글쓰기 (기획안 4절 5 ~ 8번). 가장 최근 사건 목록(eventsets)에서 사건을 골라 글 초안을 쓴다.

  ⑤ 고르기  독립 출처 두 곳 이상인 사건 중 출처가 많은 순. 지난 48시간에 이미 쓴 사건(참고 기사가 절반 넘게 겹침)은 건너뛴다
  ⑥ 쓰기    사건의 사실 목록과 '독립 출처 두 곳 이상이 맞춘 숫자'만 재료로 준다. 기사 본문은 주지 않는다
  ⑦ 검사    재료에 없는 숫자 · 한 곳만 쓴 숫자 · 매수 · 매도 권유 · 기사 제목과 겹침 · 문체 · 분량을 코드로 보고,
            걸리면 이유를 붙여 고쳐 쓰게 한다 (쓰기 포함 최대 WRITE_TRIES번)
  ⑧ 저장    posts/{id}: 통과하면 status '초안', 끝내 못 넘으면 '검사 실패'와 이유. 참고 기사 링크는 sources로 따로 둔다

공개 로그에는 건수 · 상태만 찍는다. 재료 · 글 · 제목 · 주소는 찍지 않는다 (기획안 3절).
모델: GEMINI_WRITE_MODELS (기본 gemini-4-hydrogen → gemini-3.6-flash). 둘 다 하루 20회 · 분당 5회.
"""
import html
import os
import re
import time
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher

import llm

KST = timezone(timedelta(hours=9))
WRITE_MODELS = [m.strip() for m in (os.environ.get("GEMINI_WRITE_MODELS") or "gemini-4-hydrogen,gemini-3.6-flash").split(",")
                if m.strip()]
WRITE_GAP = 13             # 분당 5회 한도
WRITE_TRIES = 3            # 쓰기 + 고쳐 쓰기 2번
DEDUP_HOURS = 48
BUSY_WAIT = 300            # Gemini 혼잡(503)이면 5분 쉬고
BUSY_ROUNDS = 2            # 두 번까지 다시
MIN_CHARS, MAX_CHARS = 300, 900   # 600자 하한은 재료가 적은 사건에서 같은 말 되풀이를 낳았다 (11-20)
REPEAT_SCORE = 0.6                # 두 문장의 낱말이 이만큼 겹치면 되풀이로 본다 (11-20 초안 두 편으로 맞춤)
TITLE_MAX = 48

CAT_KO = {
    "RATES_FED": "금리 · 중앙은행", "INFLATION": "물가", "GROWTH_JOBS": "경기 · 고용", "TARIFF_TRADE": "관세 · 무역",
    "GEOPOLITICS": "지정학", "OIL_ENERGY": "유가 · 에너지", "AI_CAPEX": "AI · 반도체", "EARNINGS": "실적",
    "CORP_EVENT": "기업 사건", "REGULATION": "규제", "FISCAL_POLITICS": "재정 · 정치", "CREDIT_BANKS": "금융 · 신용",
    "MARKET_STRUCTURE": "수급 · 시장", "FX_GLOBAL": "환율 · 해외", "CRYPTO": "암호화폐", "REAL_ESTATE": "부동산", "OTHER": "그 밖",
}

WRITE_PROMPT = """당신은 AlphaScope 블로그의 경제 뉴스 글을 씁니다. 아래 '재료'만 보고 사건 하나를 설명하는 글을 새로 쓰세요.
재료는 여러 언론사 기사에서 뽑은 사실입니다. 기사 원문은 없으며, 재료에 없는 내용을 더하면 안 됩니다.

사건: {title}
분류: {cat}
기준일: {date}

재료 (사실, 괄호는 그 사실을 쓴 독립 출처 수):
{facts}

글에 쓸 수 있는 숫자 (독립 출처 두 곳 이상이 같은 값을 씀):
{agreed}

글에 쓰면 안 되는 숫자 (한 곳만 씀):
{single}

규칙:
- 분량: 본문 {min_chars} ~ {max_chars}자. 문단 2 ~ 5개, 문단 사이는 빈 줄.
  재료가 적으면 짧게 씁니다. 분량을 채우려고 같은 사실을 다른 말로 되풀이하지 않습니다. 한 사실은 글 전체에서 한 번만 씁니다.
- 문체: 본문은 합니다체(-했습니다 · -입니다)로 끝냅니다. 모든 문장이 '니다.'로 끝나야 합니다.
- 제목: 헤드라인체. 명사형으로 끝내고 마침표 없이 {title_max}자 이내.
- 숫자: '쓸 수 있는 숫자'만 값 그대로 씁니다. 계산 · 반올림 · 환산을 하지 않습니다. 숫자가 없으면 숫자 없이 씁니다.
  날짜는 '19일'처럼 씁니다. 기준일과 달이 다르면 '10월 31일', 해가 다르면 '2027년 6월 30일'처럼 씁니다.
- 사실만 씁니다. 전망 · 평가 · 비유 · 기자 해석을 더하지 않습니다(좋다 · 우려 · 기대감 · 호재 · 악재 같은 말 금지).
- 매수 · 매도 · 추천 같은 투자 권유를 쓰지 않습니다.
- 재료의 문장을 그대로 옮기지 말고 새 문장으로 씁니다. 언론사 이름을 쓰지 않습니다.
- 줄표(—)를 쓰지 않습니다. ':' 와 '·' 는 앞뒤를 띄웁니다.
- 약어는 한글로 풀고 괄호에 둡니다(주당순이익(EPS)). AI · ETF · CPI 같은 널리 쓰는 이름은 그대로.
- '섹터' · '닿다'라는 말, '이 사건' 같은 지시어를 쓰지 않습니다. 이름을 그대로 씁니다.
- 출처 · 면책 · AI 고지 꼬리말을 붙이지 않습니다.
{fix}
JSON만 답하세요: {{"title":"제목","body":"본문"}}
"""

FIX_BLOCK = """
지난번 글이 검사에 걸렸습니다. 아래를 고쳐서 다시 쓰세요 (걸린 것만 고치고 나머지 규칙도 지키세요):
{issues}
지난번 글:
제목: {title}
본문: {body}
"""

# 투자 권유 · 평가어 (사실 보도에 쓰이는 '순매수 · 순매도 · 매수세 · 매도세 · 매수 의견'은 뺀다)
ADVICE = re.compile(r"(?<!순)(매수|매도)(?!세|우위|의견|\s?의견|액|량|대금|잔고|청구)|추천|사세요|파세요|담아|저가 매수|비중 (확대|축소)|투자 기회")
OPINION = re.compile(r"호재|악재|기대감|우려|전망된다|전망입니다|보입니다|것으로 보|좋은|나쁜|긍정적|부정적")
BANNED = [(re.compile(r"\d{4}\.\d{1,2}\.\d{1,2}"), "날짜를 '2026.11.19'처럼 썼습니다. '19일' · '11월 19일'처럼 쓰세요"),
          (re.compile(r"—"), "줄표(—)를 썼습니다"), (re.compile(r"섹터"), "'섹터'라는 말을 썼습니다"),
          (re.compile(r"닿"), "'닿다'를 썼습니다"), (re.compile(r"이 (사건|소식|발표|기업)"), "'이 사건' 같은 지시어를 썼습니다")]
NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")
DATE = re.compile(r"\d{4}\.\d{1,2}\.\d{1,2}")
TIME_UNIT = re.compile(r"^\s*(년|월|일|분기|시|분|차|번째|개월|주|회|호)")
JOSA = re.compile(r"(으로|에서|까지|부터|보다|이라고|라고|에게|하며|하고|했습니다|됐습니다|입니다|습니다|은|는|이|가|을|를|의|에|로|와|과|도|만)$")
# 지수 마감 · 시황 기사: 사건을 한 줄 언급만 해도 사실이 뽑혀 출처로 붙는다 (11-20: 수출 1조 달러 글에 '코스닥 마감')
WRAP = re.compile(r"마감|시황|증시|특징주|코스피|코스닥|지수|혼조|\d+(?:\.\d+)?p\b")
# 시세가 곧 사건인 분류는 시황 기사가 제 출처다 (유가 사건의 '[뉴욕유가] … 혼조'를 빼지 않도록)
PRICE_CATS = {"MARKET_STRUCTURE", "OIL_ENERGY", "RATES_FED", "FX_GLOBAL", "CRYPTO", "INFLATION", "GROWTH_JOBS"}


def log(msg):
    print(msg, flush=True)


def clean_title(t):
    """목록의 제목에 남은 HTML 기호(&apos; · 두 번 감싼 &amp;quot;)를 푼다."""
    t = t or ""
    for _ in range(2):
        t = html.unescape(t)
    return t.strip()


def _words(s):
    """낱말 앞 세 글자 집합 (조사 · 어미는 떼고, 두 글자 이상만). 되풀이 · 제목 겹침을 대강 보는 데 쓴다."""
    out = set()
    for w in re.findall(r"[가-힣A-Za-z0-9.]+", s or ""):
        w = JOSA.sub("", w)
        if len(w) >= 2:
            out.add(w[:3])
    return out


def repeats(body):
    """같은 사실을 되풀이한 문장 쌍 [(앞 문장, 뒤 문장)]."""
    sents = [x.strip() for x in re.split(r"(?<=[.?!])\s+", body) if x.strip()]
    ws = [_words(s) for s in sents]
    out = []
    for i in range(len(sents)):
        for j in range(i + 1, len(sents)):
            a, b = ws[i], ws[j]
            if min(len(a), len(b)) >= 3 and len(a & b) / min(len(a), len(b)) >= REPEAT_SCORE:
                out.append((sents[i], sents[j]))
    return out


def shown_sources(ev):
    """글에 붙일 참고 기사. 시황 기사인데 제목이 사건과 낱말 하나 겹치지 않으면 뺀다 (사실 · 출처 수 세기에는 그대로 남는다)."""
    key = _words(ev.get("title"))
    out = []
    for s in ev.get("sources", []):
        t = clean_title(s.get("title"))
        if ev.get("cat") not in PRICE_CATS and WRAP.search(t) and not (_words(t) & key):
            continue
        out.append({"no": s.get("no"), "title": t, "url": s.get("url"), "publishedAt": s.get("publishedAt")})
    return out


def _digits(text):
    return {n.replace(",", "") for n in NUM.findall(text or "")}


def materials(ev):
    """(사실 줄, 쓸 숫자 줄, 쓰면 안 되는 숫자 줄, 쓸 숫자 집합, 한 곳 숫자 집합, 사실 글 전체)."""
    seen, facts = [], []
    for f in ev.get("facts", []):
        w = (f.get("what") or "").strip()
        if not w or any(SequenceMatcher(None, w, x).ratio() > 0.8 for x in seen):
            continue
        seen.append(w)
        extra = []
        if f.get("when"):
            extra.append(f"때 : {f['when']}")
        if f.get("stage") == "discussing":
            extra.append("아직 정해지지 않음")
        if f.get("quote") and f["quote"].get("text"):
            extra.append(f"발언 : {f['quote'].get('speaker', '')} \"{f['quote']['text']}\"")
        facts.append(f"- {w}" + (f" ({' · '.join(extra)})" if extra else ""))
    agreed = [n for n in ev.get("numbers", []) if n.get("agreed")]
    single = [n for n in ev.get("numbers", []) if not n.get("agreed")]
    line = lambda n: f"- {n['name']} : {n['value']}{n['unit']}" + (f" ({n.get('sources', len(n.get('outlets', [])))}곳)" if n.get("agreed") else "")
    ok_set = set().union(*[_digits(n["value"]) for n in agreed]) if agreed else set()
    bad_set = (set().union(*[_digits(n["value"]) for n in single]) if single else set()) - ok_set
    fact_text = " ".join(seen) + " " + " ".join(f.get("when") or "" for f in ev.get("facts", []))
    return ("\n".join(facts), "\n".join(map(line, agreed)) or "- 없음", "\n".join(map(line, single)) or "- 없음",
            ok_set, bad_set, fact_text)


def check(post, ev, ok_set, bad_set, fact_text):
    """검사에 걸린 것 [문장]. 빈 목록이면 통과."""
    issues = []
    title, body = (post.get("title") or "").strip(), (post.get("body") or "").strip()
    if not title or not body:
        return ["제목이나 본문이 비었습니다"]
    n = len(body.replace("\n", ""))
    if n < MIN_CHARS or n > MAX_CHARS:
        issues.append(f"본문이 {n}자입니다. {MIN_CHARS} ~ {MAX_CHARS}자로 맞추세요")
    if len(title) > TITLE_MAX:
        issues.append(f"제목이 {len(title)}자입니다. {TITLE_MAX}자 이내로 줄이세요")
    if title.endswith("."):
        issues.append("제목 끝 마침표를 빼세요")
    for text, where in ((title, "제목"), (body, "본문")):
        text = DATE.sub(" ", text)                         # 2026.11.19 같은 날짜 표기는 숫자 검사에서 뺀다
        for m in NUM.finditer(text):
            v = m.group().replace(",", "")
            if v in ok_set:
                continue
            after = text[m.end():m.end() + 4]
            if TIME_UNIT.match(after) and v in _digits(fact_text):
                continue                                   # 재료에 있는 날짜 · 분기 · 횟수
            if v in bad_set:
                issues.append(f"{where}의 숫자 '{m.group()}'는 한 곳만 쓴 숫자입니다. 빼세요")
            else:
                issues.append(f"{where}의 숫자 '{m.group()}'는 재료에 없습니다. 빼세요")
    sentences = [x.strip() for x in re.split(r"(?<=[.?!])\s+", body) if x.strip()]
    bad_end = [x for x in sentences if not x.endswith("니다.")]
    if bad_end:
        issues.append(f"합니다체로 끝나지 않은 문장이 {len(bad_end)}개 있습니다 (예: '{bad_end[0][-20:]}')")
    rep = repeats(body)
    if rep:
        a, b = rep[0]
        issues.append(f"같은 사실을 되풀이한 문장이 {len(rep)}쌍 있습니다 (예: '{a[:30]}' / '{b[:30]}'). "
                      "하나만 남기고, 분량이 줄어도 괜찮습니다")
    for rx, msg in BANNED:
        if rx.search(title + body):
            issues.append(msg)
    if ADVICE.search(title + body):
        issues.append(f"투자 권유로 읽히는 말('{ADVICE.search(title + body).group()}')을 썼습니다. 빼세요")
    if OPINION.search(body):
        issues.append(f"평가 · 전망 말('{OPINION.search(body).group()}')을 썼습니다. 사실만 쓰세요")
    for s in ev.get("sources", []):                         # 기사 제목과 겹치는 표현 (본문은 없으므로 제목과 비교)
        st = re.sub(r"\s+", " ", clean_title(s.get("title")))
        m = SequenceMatcher(None, st, title + " " + body).find_longest_match(0, len(st), 0, len(title) + 1 + len(body))
        if m.size >= 15:
            issues.append(f"기사 제목과 {m.size}자가 겹칩니다('{st[m.a:m.a + m.size]}'). 새 말로 쓰세요")
            break
    return list(dict.fromkeys(issues))


def _generate(prompt):
    """혼잡(503 · 시간 초과)이면 BUSY_WAIT초 쉬고 BUSY_ROUNDS번까지 다시. 한도(429)는 기다리지 않고 올려 보낸다.
    max_out은 넉넉히: 생각하는 모델(Hydrogen · 3.6 Flash)은 생각 토큰도 출력 한도에 들어가 4096에서 답이 잘렸다(11-19)."""
    for round_ in range(BUSY_ROUNDS + 1):
        try:
            return llm.generate_json(prompt, max_out=16384, models=WRITE_MODELS, gap=WRITE_GAP, temperature=0.4, attempts=3)
        except llm.LLMError as e:
            # 429도 기다린다: Hydrogen이 503이면 3.6 Flash로 넘어가는데, 그쪽 한도(429)에 걸리면 쉬었다가 Hydrogen부터 다시 (11-19)
            busy = "503" in str(e) or "접속 실패" in str(e) or "429" in str(e)
            if not busy or round_ == BUSY_ROUNDS:
                raise
            log(f"   Gemini 혼잡, {BUSY_WAIT // 60}분 쉬고 다시 ({round_ + 1}/{BUSY_ROUNDS})")
            time.sleep(BUSY_WAIT)


def write_one(ev, date):
    facts, agreed, single, ok_set, bad_set, fact_text = materials(ev)
    base = dict(title=ev["title"], cat=CAT_KO.get(ev.get("cat"), "그 밖"), date=date, facts=facts, agreed=agreed, single=single,
                min_chars=MIN_CHARS, max_chars=MAX_CHARS, title_max=TITLE_MAX)
    post, issues, tries, used = None, [], 0, []
    for tries in range(1, WRITE_TRIES + 1):
        fix = FIX_BLOCK.format(issues="\n".join(f"- {i}" for i in issues), title=post["title"], body=post["body"]) if post else ""
        out = _generate(WRITE_PROMPT.format(fix=fix, **base))
        if llm.stats.get("lastModel") and llm.stats["lastModel"] not in used:
            used.append(llm.stats["lastModel"])
        if not isinstance(out, dict):
            issues = ["JSON 형식이 아닙니다"]
            continue
        post = {"title": str(out.get("title") or "").strip(), "body": str(out.get("body") or "").strip()}
        issues = check(post, ev, ok_set, bad_set, fact_text)
        if not issues:
            break
    return post, issues, tries, used


def event_key(ev):
    return sorted({s.get("url") for s in ev.get("sources", []) if s.get("url")})


def pick(events, recent_keys, n):
    out = []
    for ev in sorted((e for e in events if not e.get("single")),
                     key=lambda e: (-e.get("sourceCount", 0), -e.get("agreedNumbers", 0), -len(e.get("facts", [])))):
        k = set(event_key(ev))
        if any(k and len(k & r) * 2 > len(k) for r in recent_keys):
            continue
        out.append(ev)
        if len(out) >= n:
            break
    return out


def main():
    from google.cloud import firestore
    from google.cloud.firestore_v1.base_query import FieldFilter

    db = firestore.Client(project=os.environ["GCP_PROJECT"])
    now = datetime.now(timezone.utc)
    run_id = os.environ.get("GITHUB_RUN_ID") or now.strftime("local-%Y%m%d%H%M%S")
    n = int(os.environ.get("WRITE_MAX") or (2 if now.astimezone(KST).hour < 12 else 3))   # 07:00 두 편 · 16:30 세 편
    if not llm.key():
        log("GEMINI_API_KEY가 없어 글을 쓰지 않습니다")
        return
    sets = list(db.collection("eventsets").order_by("createdAt", direction=firestore.Query.DESCENDING).limit(1).stream())
    if not sets:
        log("사건 목록이 없어 글을 쓰지 않습니다")
        return
    es = sets[0].to_dict()
    since = now - timedelta(hours=DEDUP_HOURS)
    recent = [set(d.to_dict().get("eventKey", [])) for d in
              db.collection("posts").where(filter=FieldFilter("createdAt", ">=", since)).stream()]
    chosen = pick(es.get("events", []), recent, n)
    log(f"⑤ 고르기: 사건 {len(es.get('events', []))}개 중 독립 출처 두 곳 이상 "
        f"{sum(1 for e in es.get('events', []) if not e.get('single'))}개, 이미 쓴 사건 빼고 {len(chosen)}편 쓰기 (목표 {n}편)")
    date = now.astimezone(KST)
    date_s = f"{date.year}년 {date.month}월 {date.day}일"
    stats = {"ok": 0, "failed": 0, "skipped": 0}
    for i, ev in enumerate(chosen):
        try:
            post, issues, tries, used = write_one(ev, date_s)
        except llm.LLMError as e:
            stats["skipped"] += 1
            log(f"⑥ {i + 1}편: Gemini 실패로 건너뜀 ({e})")
            if "모든 모델 실패" in str(e) or "429" in str(e) or "503" in str(e) or "접속 실패" in str(e):
                break                                         # 두 모델 모두 한도면 남은 글은 그날 건너뛴다 (기획안 6절)
            continue
        status = "초안" if not issues else "검사 실패"
        stats["ok" if not issues else "failed"] += 1
        db.collection("posts").document(f"{run_id}-{i + 1}").set({
            "status": status, "title": post["title"] if post else "", "body": post["body"] if post else "",
            "issues": issues, "tries": tries, "models": used,
            "eventTitle": ev["title"], "cat": ev.get("cat"), "direction": ev.get("direction"), "stage": ev.get("stage"),
            "sourceCount": ev.get("sourceCount"), "outlets": ev.get("outlets", []),
            "agreedNumbers": [n for n in ev.get("numbers", []) if n.get("agreed")],
            "sources": shown_sources(ev),
            "eventKey": event_key(ev), "edition": es.get("edition"), "run": str(run_id), "createdAt": now,
        })
        log(f"⑥ ⑦ {i + 1}편: {status} (시도 {tries}번, 걸린 것 {len(issues)}개)")
    log(f"⑧ 저장: 초안 {stats['ok']}편, 검사 실패 {stats['failed']}편, 건너뜀 {stats['skipped']}편 · "
        f"Gemini 호출 {llm.stats['calls']}번 {llm.stats['byModel']}" + (f", 오류 {llm.stats['errors']}" if llm.stats["errors"] else ""))


if __name__ == "__main__":
    main()
