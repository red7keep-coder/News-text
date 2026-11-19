"""0단계 접속 시험: 언론사 기사 페이지가 이 실행기에서 실제로 받아지는지 본다.

- 언론사 목록은 Firestore `outlets`에서 읽는다 (코드 · 로그에 언론사 이름 · 주소를 두지 않는다).
- 언론사마다 최근 기사 주소를 언론사 자체 사이트맵 · RSS에서 찾고 최대 3건을 받아 본다.
  구글 뉴스 RSS는 쓰지 않는다: news.google.com의 robots가 /rss/를 허용하지 않는다 (11-19 첫 시험에서 확인).
- 본문은 글자 수만 세고 바로 버린다. 파일 · 로그 · Firestore 어디에도 쓰지 않는다. AI는 쓰지 않는다.
- 결과는 Firestore `probe/{실행 번호}`에 쓴다. 공개 로그에는 언론사 번호 · 건수 · 글자 수 · 판정만 찍는다.
- 요청 예절: robots를 따른다, 같은 사이트 10초 간격, 403 · 429 · 차단 화면이면 그 언론사는 거기서 멈춘다.
"""
import gzip
import json
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import requests
from lxml import html as lxml_html

import robots as robots_mod

USER_AGENT = "NewsTextCollector/0.1"
HOST_GAP = 10           # 같은 사이트 요청 간격(초)
ARTICLES = 3            # 언론사마다 받아 볼 기사 수
TIMEOUT = 20
MAX_BYTES = 10_000_000  # 응답 하나에서 읽을 최대 바이트 (사이트맵이 매우 큰 경우 대비)
MIN_TEXT = 300          # 본문으로 볼 최소 글자 수
MAX_FEED_TRIES = 4      # 언론사마다 목록(사이트맵 · RSS)을 받아 보는 최대 횟수

# robots에 사이트맵이 없을 때 차례로 볼 흔한 자리 (국내 언론사 CMS에서 흔한 RSS 주소 포함)
COMMON_FEEDS = ["/sitemap.xml", "/rss/allArticle.xml", "/sitemap_index.xml", "/rss", "/rss.xml"]
NOT_ARTICLE = re.compile(r"\.(jpe?g|png|gif|webp|svg|mp4|pdf|css|js)(\?|$)|/(photo|video|vod|tag|tags|search|section|category|"
                         r"sitemap|reporter|author|about)\b", re.I)
CHALLENGE = re.compile(r"just a moment|cf-chl|captcha|access denied|비정상적인 접근|자동화된 접근|robot check", re.I)
LOGIN = re.compile(r"로그인 후 이용|로그인이 필요|유료 ?회원|구독자 전용|프리미엄 회원|유료 기사|구독하고 계속")

session = requests.Session()
session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "ko-KR,ko;q=0.9"})
_last = {}          # 사이트별 마지막 요청 시각
_robots = {}        # 사이트별 Robots
requests_made = 0


class Blocked(Exception):
    """403 · 429 · 차단 화면. 그 언론사는 이번 실행에서 그만 받는다."""


def log(msg):
    print(msg, flush=True)


def _wait(host, gap):
    t = _last.get(host)
    if t is not None:
        left = gap - (time.monotonic() - t)
        if left > 0:
            time.sleep(left)
    _last[host] = time.monotonic()


def fetch(url, gap=HOST_GAP):
    """예절을 지켜 받는다. (응답 코드, 바이트, 최종 주소). 접속 실패는 코드 0.
    오류 메시지에는 주소가 들어가므로 예외는 종류 이름만 남긴다."""
    global requests_made
    host = urlsplit(url).hostname
    _wait(host, gap)
    requests_made += 1
    try:
        with session.get(url, timeout=TIMEOUT, stream=True, allow_redirects=True) as r:
            body = r.raw.read(MAX_BYTES + 1, decode_content=True)
            return r.status_code, body[:MAX_BYTES], r.url
    except requests.RequestException as e:
        return 0, b"", type(e).__name__


def robots_for(host):
    if host not in _robots:
        status, body, _ = fetch(f"https://{host}/robots.txt")
        text = body.decode("utf-8", "replace") if status == 200 else ""
        # robots 자리에 일반 웹 페이지를 주는 곳이 있다 (11-18 확인). 그러면 규칙 없음으로 본다
        if text.lstrip().lower().startswith(("<!doctype", "<html")):
            text = ""
        _robots[host] = robots_mod.Robots(text, status)
    return _robots[host]


def allowed(url):
    p = urlsplit(url)
    path = (p.path or "/") + (f"?{p.query}" if p.query else "")
    return robots_for(p.hostname).allowed(path)


# ---------- 기사 주소 찾기 ----------

def _base(host):
    return host[4:] if host.startswith("www.") else host


def _same_site(url, host):
    h = urlsplit(url).hostname or ""
    b = _base(host)
    return h == b or h.endswith("." + b)


def looks_article(url, host):
    """기사 화면으로 보이는 주소: 같은 사이트, 사진 · 섹션 · 소개 화면이 아니고, 기사 번호(숫자 5자리 이상)나 긴 경로가 있다."""
    if not _same_site(url, host) or NOT_ARTICLE.search(url):
        return False
    p = urlsplit(url)
    path = p.path or "/"
    if path in ("", "/"):
        return False
    if re.search(r"\d{5,}", path) or len(path.strip("/").split("/")) >= 3:
        return True
    # 번호가 쿼리에 있는 CMS (예: articleView.html?idxno=…). 섹션 번호(cid · sid)는 기사로 보지 않는다
    return bool(re.search(r"(?:^|&)(?:idxno|aid|art_?id|article_?id|news_?id|no|id)=\d{5,}", p.query, re.I))


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _child(el, name):
    """바로 아래 자식 중 이름이 맞는 것의 글자 (image:loc 같은 손자 항목은 보지 않는다)."""
    for c in el:
        if _local(c.tag) == name and c.text:
            return c.text.strip()
    return ""


def _news_date(el):
    for c in el.iter():
        if _local(c.tag) == "publication_date" and c.text:
            return c.text.strip()
    return ""


def _ts(text):
    if not text:
        return 0
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        pass
    try:
        return parsedate_to_datetime(text).timestamp()
    except (TypeError, ValueError):
        return 0


def parse_feed(body):
    """사이트맵 · 사이트맵 목록 · RSS · Atom을 읽는다. ('index', [하위 주소]) 또는 ('urls', [(시각, 주소)])."""
    if body[:2] == b"\x1f\x8b":
        body = gzip.decompress(body)
    root = ET.fromstring(body)
    kind = _local(root.tag)
    if kind == "sitemapindex":
        subs = [(_ts(_child(s, "lastmod")), _child(s, "loc")) for s in root if _local(s.tag) == "sitemap"]
        return "index", [u for _, u in sorted(subs, reverse=True) if u]
    rows = []
    if kind == "urlset":
        for u in root:
            if _local(u.tag) == "url":
                loc = _child(u, "loc")
                rows.append((_ts(_news_date(u) or _child(u, "lastmod")), loc))
    elif kind in ("rss", "rdf", "RDF"):
        for it in root.iter():
            if _local(it.tag) == "item":
                rows.append((_ts(_child(it, "pubDate") or _child(it, "date")), _child(it, "link")))
    elif kind == "feed":
        for e in root:
            if _local(e.tag) == "entry":
                link = next((c.get("href") for c in e if _local(c.tag) == "link" and c.get("href")), "")
                rows.append((_ts(_child(e, "published") or _child(e, "updated")), link))
    return "urls", [(t, u) for t, u in rows if u]


def _pick_sub(subs):
    """사이트맵 목록에서 기사 쪽 하위 사이트맵을 고른다 (최신 순으로 이미 정렬됨)."""
    good = [u for u in subs if re.search(r"news|article|daily|today|latest", u, re.I)
            and not re.search(r"image|photo|video|sports|section|category|tag", u, re.I)]
    return (good or [u for u in subs if not re.search(r"image|photo|video", u, re.I)] or subs)[:1]


def find_articles(host, notes):
    """언론사 자체 목록(robots의 사이트맵 → 흔한 자리)에서 기사 주소를 최신 순으로 ARTICLES건."""
    rb = robots_for(host)
    cands = sorted(rb.sitemaps, key=lambda u: 0 if re.search(r"news", u, re.I) else 1)
    cands += [f"https://{host}{p}" for p in COMMON_FEEDS]
    tries = 0
    seen = set()
    queue = list(dict.fromkeys(cands))
    while queue and tries < MAX_FEED_TRIES:
        url = queue.pop(0)
        if url in seen or not _same_site(url, host) or not allowed(url):
            continue
        seen.add(url)
        tries += 1
        status, body, _ = fetch(url)
        if status != 200:
            notes.append(f"목록 {status}")
            continue
        try:
            kind, items = parse_feed(body)
        except (ET.ParseError, OSError, EOFError):
            notes.append("목록 해석 실패")
            continue
        if kind == "index":
            notes.append(f"사이트맵 목록 {len(items)}개")
            queue = _pick_sub(items) + queue
            continue
        arts = [(t, u) for t, u in items if looks_article(u, host) and allowed(u)]
        notes.append(f"목록 {len(items)}건 중 기사 {len(arts)}건")
        if arts:
            arts.sort(key=lambda x: -x[0])
            return list(dict.fromkeys(u for _, u in arts))[:ARTICLES]
    if not tries:
        notes.append("볼 수 있는 목록 없음")
    return []


# ---------- 기사 받아 보기 ----------

def _json_ld_body(doc):
    best = 0
    for s in doc.xpath('//script[@type="application/ld+json"]'):
        try:
            data = json.loads(s.text or "")
        except ValueError:
            continue
        stack = [data]
        while stack:
            x = stack.pop()
            if isinstance(x, dict):
                if isinstance(x.get("articleBody"), str):
                    best = max(best, len(x["articleBody"].strip()))
                stack.extend(x.values())
            elif isinstance(x, list):
                stack.extend(x)
    return best


def _embedded_body(text):
    """화면을 나중에 그리는 사이트: 페이지 안 데이터 블록의 본문 문단 글자 수 (Arc 계열 content_elements)."""
    total = 0
    for m in re.finditer(r'"type"\s*:\s*"text"\s*,\s*"content"\s*:\s*"((?:[^"\\]|\\.)*)"', text):
        try:
            total += len(re.sub(r"<[^>]+>", "", json.loads(f'"{m.group(1)}"')).strip())
        except ValueError:
            pass
    return total


def _text_lengths(body):
    """(보이는 글자 수, 본문 글자 수, 본문을 찾은 방법). 본문은 이 함수 밖으로 나가지 않는다."""
    import trafilatura
    visible, ld = 0, 0
    try:
        doc = lxml_html.fromstring(body)
        ld = _json_ld_body(doc)
        for bad in doc.xpath("//script|//style|//noscript"):
            bad.drop_tree()
        visible = len(re.sub(r"\s+", " ", doc.text_content()).strip())
    except (ValueError, lxml_html.etree.ParserError):
        pass
    main = len(trafilatura.extract(body, include_comments=False, include_tables=False, favor_precision=True) or "")
    if main >= MIN_TEXT:
        return visible, main, "화면"
    if ld >= MIN_TEXT:
        return visible, ld, "JSON-LD"
    emb = _embedded_body(body.decode("utf-8", "ignore"))
    if emb >= MIN_TEXT:
        return visible, emb, "데이터 블록"
    return visible, max(main, ld, emb), ""


def try_article(url, host):
    row = {"url": url, "robots": allowed(url)}
    if not row["robots"]:
        row["verdict"] = "robots 거부"
        return row
    status, body, final = fetch(url)
    row["status"] = status
    row["sameSite"] = status != 0 and _same_site(final, host)
    row["bytes"] = len(body)
    if status in (403, 429) or (status == 200 and CHALLENGE.search(body[:20000].decode("utf-8", "ignore"))):
        row["verdict"] = "막힘"
        raise Blocked(row)
    if status != 200:
        row["verdict"] = f"응답 {status}" if status else f"접속 실패({final})"
        return row
    visible, main, how = _text_lengths(body)
    head = body[:200000].decode("utf-8", "ignore")
    del body                                   # 본문은 여기서 버린다
    row["visibleChars"] = visible
    row["textChars"] = main
    row["bodyFrom"] = how
    row["loginMarker"] = bool(LOGIN.search(head))
    if main >= MIN_TEXT:
        row["verdict"] = "본문 받음"
    elif row["loginMarker"]:
        row["verdict"] = "유료 · 로그인 의심"
    elif visible < MIN_TEXT:
        row["verdict"] = "본문 비어 있음(화면을 나중에 그리는 구조 의심)"
    else:
        row["verdict"] = "본문 추출 실패"
    return row


def probe_outlet(o):
    host = o["host"]
    res = {"no": o["no"], "notes": [], "articles": []}
    rb = robots_for(host)
    res["robotsStatus"] = rb.status
    res["robotsNamesUs"] = rb.named_us()
    res["robotsAllowsRoot"] = rb.allowed("/")
    if rb.status in (403, 429):
        res["verdict"] = "막힘(robots 403 · 429)"
        return res
    if rb.status == 0 or rb.status >= 500:
        res["verdict"] = f"robots 못 읽음({rb.status})"
        return res
    urls = find_articles(host, res["notes"])
    res["foundTotal"] = len(urls)
    if not urls:
        res["verdict"] = "기사 주소 못 찾음"
        return res
    try:
        for u in urls:
            res["articles"].append(try_article(u, host))
    except Blocked as b:
        res["articles"].append(b.args[0])
        res["verdict"] = "막힘"
        return res
    ok = sum(1 for a in res["articles"] if a.get("verdict") == "본문 받음")
    res["ok"] = ok
    res["verdict"] = "쓸 수 있음" if ok == len(urls) else ("일부만" if ok else "못 받음")
    return res


def summary_line(r):
    arts = r["articles"]
    ok = [a for a in arts if a.get("verdict") == "본문 받음"]
    avg = f", 본문 평균 {sum(a['textChars'] for a in ok) // len(ok):,}자" if ok else ""
    how = sorted({a.get("bodyFrom") for a in ok if a.get("bodyFrom")})
    how = f"({'·'.join(how)})" if how else ""
    return (f"언론사 {r['no']:02d}: robots {r['robotsStatus']}, 찾기 {r.get('foundTotal', 0)}건, "
            f"받기 {len(ok)}/{len(arts)}{how}{avg} → {r['verdict']}")


def main():
    from google.cloud import firestore
    project = os.environ["GCP_PROJECT"]
    db = firestore.Client(project=project)
    only = {int(x) for x in os.environ.get("ONLY", "").replace(",", " ").split() if x.isdigit()}
    outlets = [d.to_dict() for d in db.collection("outlets").stream()]
    # 번호를 지정하면 꺼 둔 언론사도 시험한다 (보류한 곳을 다시 확인할 때)
    outlets = sorted((o for o in outlets if (o["no"] in only if only else o.get("enabled", True))),
                     key=lambda o: o["no"])
    log(f"언론사 {len(outlets)}곳 시험 시작")
    run_id = os.environ.get("GITHUB_RUN_ID") or datetime.now(timezone.utc).strftime("local-%Y%m%d%H%M%S")
    started = datetime.now(timezone.utc)
    results = {}
    for o in outlets:
        try:
            r = probe_outlet(o)
        except Exception as e:      # 한 곳의 오류로 전체를 멈추지 않는다. 메시지에 주소가 있을 수 있어 종류만
            r = {"no": o["no"], "articles": [], "robotsStatus": -1, "verdict": f"오류({type(e).__name__})"}
        results[f"{o['no']:02d}"] = r
        log(summary_line(r))
    counts = {}
    for r in results.values():
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    db.collection("probe").document(str(run_id)).set({
        "startedAt": started, "finishedAt": datetime.now(timezone.utc),
        "userAgent": USER_AGENT, "runner": os.environ.get("RUNNER_NAME", "local"),
        "requests": requests_made, "counts": counts, "results": results,
    })
    log(f"끝. 요청 {requests_made}번, 판정 " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))


if __name__ == "__main__":
    main()
