"""0단계 접속 시험: 언론사 기사 페이지가 이 실행기에서 실제로 받아지는지 본다.

- 언론사 목록은 Firestore `outlets`에서 읽는다 (코드 · 로그에 언론사 이름 · 주소를 두지 않는다).
- 언론사마다 최근 기사 주소를 찾고(구글 뉴스 검색 RSS → 안 되면 언론사 사이트맵) 최대 3건을 받아 본다.
- 본문은 글자 수만 세고 바로 버린다. 파일 · 로그 · Firestore 어디에도 쓰지 않는다. AI는 쓰지 않는다.
- 결과는 Firestore `probe/{실행 번호}`에 쓴다. 공개 로그에는 언론사 번호 · 건수 · 글자 수 · 판정만 찍는다.
- 요청 예절: robots를 따른다, 같은 사이트 10초 간격, 403 · 429 · 차단 화면이면 그 언론사는 거기서 멈춘다.
"""
import base64
import gzip
import json
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote, urlsplit

import requests
from lxml import html as lxml_html

import robots as robots_mod

USER_AGENT = "NewsTextCollector/0.1"
HOST_GAP = 10           # 같은 사이트 요청 간격(초)
GOOGLE_GAP = 3          # 구글 뉴스 요청 간격(초)
ARTICLES = 3            # 언론사마다 받아 볼 기사 수
TIMEOUT = 20
MAX_BYTES = 10_000_000  # 응답 하나에서 읽을 최대 바이트 (사이트맵이 매우 큰 경우 대비)
MIN_TEXT = 300          # 본문으로 볼 최소 글자 수

GNEWS = "news.google.com"
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


def fetch(url, gap=HOST_GAP, method="GET", data=None, headers=None):
    """예절을 지켜 받는다. (응답 코드, 바이트, 최종 주소). 접속 실패는 코드 0.
    오류 메시지에는 주소가 들어가므로 예외는 종류 이름만 남긴다."""
    global requests_made
    host = urlsplit(url).hostname
    _wait(host, gap)
    requests_made += 1
    try:
        with session.request(method, url, data=data, headers=headers, timeout=TIMEOUT,
                             stream=True, allow_redirects=True) as r:
            body = r.raw.read(MAX_BYTES + 1, decode_content=True)
            return r.status_code, body[:MAX_BYTES], r.url
    except requests.RequestException as e:
        return 0, b"", type(e).__name__


def robots_for(host):
    if host not in _robots:
        status, body, _ = fetch(f"https://{host}/robots.txt", gap=HOST_GAP)
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


def gnews_items(host):
    """구글 뉴스 검색 RSS의 최근 항목 [(구글 주소, 게시 시각)]."""
    q = quote(f"site:{_base(host)} when:2d")
    url = f"https://{GNEWS}/rss/search?q={q}&hl=ko&gl=KR&ceid=KR:ko"
    if not allowed(url):
        return None, "구글 robots 거부"
    status, body, _ = fetch(url, gap=GOOGLE_GAP)
    if status != 200:
        return None, f"구글 RSS {status}"
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        return None, "구글 RSS 해석 실패"
    items = []
    for it in root.iter("item"):
        link = (it.findtext("link") or "").strip()
        when = it.findtext("pubDate")
        try:
            ts = parsedate_to_datetime(when).timestamp() if when else 0
        except (TypeError, ValueError):
            ts = 0
        if link:
            items.append((link, ts))
    items.sort(key=lambda x: -x[1])
    return items, f"구글 RSS {len(items)}건"


def _gnews_id(link):
    m = re.search(r"/articles/([^?/]+)", link)
    return m.group(1) if m else None


def _decode_old(gid):
    """옛 형식 구글 기사 번호에는 실제 주소가 그대로 들어 있다."""
    try:
        raw = base64.urlsafe_b64decode(gid + "=" * (-len(gid) % 4))
    except (ValueError, TypeError):
        return None
    m = re.search(rb"https?://[\x21-\x7e]+", raw)
    if not m:
        return None
    url = m.group(0).decode("ascii", "replace")
    return None if "AU_yqL" in url else url


def _decode_new(gid):
    """새 형식: 기사 화면에서 서명 · 시각을 얻어 구글 내부 변환 요청으로 실제 주소를 받는다."""
    page = f"https://{GNEWS}/rss/articles/{gid}"
    api = f"https://{GNEWS}/_/DotsSplashUi/data/batchexecute"
    if not (allowed(page) and allowed(api)):
        return None, "구글 robots 거부(변환)"
    status, body, _ = fetch(page, gap=GOOGLE_GAP)
    if status != 200:
        return None, f"구글 변환 화면 {status}"
    text = body.decode("utf-8", "replace")
    sg = re.search(r'data-n-a-sg="([^"]+)"', text)
    ts = re.search(r'data-n-a-ts="([^"]+)"', text)
    if not (sg and ts):
        return None, "구글 변환 값 없음"
    inner = ["garturlreq", [["X", "X", ["X", "X"], None, None, 1, 1, "US:en", None, 1, None, None, None, None, None, 0, 1],
                            "X", "X", 1, [1, 1, 1], 1, 1, None, 0, 0, None, 0], gid, int(ts.group(1)), sg.group(1)]
    req = [[["Fbv4je", json.dumps(inner), None, "generic"]]]
    status, body, _ = fetch(api, gap=GOOGLE_GAP, method="POST", data={"f.req": json.dumps(req)},
                            headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"})
    if status != 200:
        return None, f"구글 변환 {status}"
    try:
        parts = body.decode("utf-8", "replace").split("\n\n")[1]
        url = json.loads(json.loads(parts)[0][2])[1]
        return (url, "") if url.startswith("http") else (None, "구글 변환 답 이상")
    except (IndexError, ValueError, TypeError):
        return None, "구글 변환 답 해석 실패"


def find_by_gnews(host, notes):
    items, note = gnews_items(host)
    notes.append(note)
    found = []
    for link, ts in (items or [])[:ARTICLES * 2]:
        gid = _gnews_id(link)
        if not gid:
            continue
        url = _decode_old(gid)
        if not url:
            url, why = _decode_new(gid)
            if not url:
                notes.append(why)
                if "robots" in why or "변환 값 없음" in why:
                    break   # 같은 이유로 계속 안 되므로 구글 요청을 더 하지 않는다
                continue
        if _same_site(url, host) and url not in found and allowed(url):
            found.append(url)
        if len(found) >= ARTICLES:
            break
    return found


def _xml_root(body):
    if body[:2] == b"\x1f\x8b":
        body = gzip.decompress(body)
    return ET.fromstring(body)


def _local(tag):
    return tag.rsplit("}", 1)[-1]


def find_by_sitemap(host, notes):
    rb = robots_for(host)
    maps = sorted(rb.sitemaps, key=lambda u: 0 if "news" in u.lower() else 1)
    if not maps:
        notes.append("사이트맵 없음")
        return []
    for sm in maps[:2]:
        if not allowed(sm):
            continue
        status, body, _ = fetch(sm)
        if status != 200:
            notes.append(f"사이트맵 {status}")
            continue
        try:
            root = _xml_root(body)
        except (ET.ParseError, OSError, EOFError):
            notes.append("사이트맵 해석 실패")
            continue
        if _local(root.tag) == "sitemapindex":
            subs = [e.text.strip() for e in root.iter() if _local(e.tag) == "loc" and e.text]
            subs.sort(key=lambda u: 0 if "news" in u.lower() else 1)
            if not subs or not allowed(subs[0]):
                continue
            status, body, _ = fetch(subs[0])
            if status != 200:
                notes.append(f"하위 사이트맵 {status}")
                continue
            try:
                root = _xml_root(body)
            except (ET.ParseError, OSError, EOFError):
                notes.append("하위 사이트맵 해석 실패")
                continue
        rows = []
        for u in root.iter():
            if _local(u.tag) != "url":
                continue
            loc, when = "", ""
            for c in u.iter():
                name = _local(c.tag)
                if name == "loc" and c.text:
                    loc = c.text.strip()
                elif name in ("publication_date", "lastmod") and c.text and not when:
                    when = c.text.strip()
            if loc and _same_site(loc, host) and urlsplit(loc).path not in ("", "/") and allowed(loc):
                rows.append((when, loc))
        rows.sort(reverse=True)
        notes.append(f"사이트맵 {len(rows)}건")
        out = [loc for _, loc in rows[:ARTICLES]]
        if out:
            return out
    return []


# ---------- 기사 받아 보기 ----------

def _text_lengths(body):
    """(보이는 글자 수, 본문 추출 글자 수). 본문은 이 함수 밖으로 나가지 않는다."""
    import trafilatura
    visible = 0
    try:
        doc = lxml_html.fromstring(body)
        for bad in doc.xpath("//script|//style|//noscript"):
            bad.drop_tree()
        visible = len(re.sub(r"\s+", " ", doc.text_content()).strip())
    except (ValueError, lxml_html.etree.ParserError):
        pass
    main = trafilatura.extract(body, include_comments=False, include_tables=False, favor_precision=True) or ""
    return visible, len(main)


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
    visible, main = _text_lengths(body)
    head = body[:200000].decode("utf-8", "ignore")
    del body                                   # 본문은 여기서 버린다
    row["visibleChars"] = visible
    row["textChars"] = main
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
    urls = find_by_gnews(host, res["notes"])
    res["foundGoogle"] = len(urls)
    if len(urls) < ARTICLES:
        more = [u for u in find_by_sitemap(host, res["notes"]) if u not in urls]
        urls += more[:ARTICLES - len(urls)]
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
    ok = sum(1 for a in arts if a.get("verdict") == "본문 받음")
    chars = [a["textChars"] for a in arts if a.get("verdict") == "본문 받음"]
    avg = f", 본문 평균 {sum(chars) // len(chars):,}자" if chars else ""
    found = f"찾기 {r.get('foundTotal', 0)}건(구글 {r.get('foundGoogle', 0)})"
    return f"언론사 {r['no']:02d}: robots {r['robotsStatus']}, {found}, 받기 {ok}/{len(arts)}{avg} → {r['verdict']}"


def main():
    from google.cloud import firestore
    project = os.environ["GCP_PROJECT"]
    db = firestore.Client(project=project)
    only = {int(x) for x in os.environ.get("ONLY", "").replace(",", " ").split() if x.isdigit()}
    outlets = [d.to_dict() for d in db.collection("outlets").stream()]
    outlets = sorted((o for o in outlets if o.get("enabled", True) and (not only or o["no"] in only)),
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
