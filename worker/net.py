"""언론사 사이트에 예절을 지켜 접속하는 공통 부분 (probe · collect가 같이 쓴다).

- robots.txt를 따른다. 같은 사이트는 HOST_GAP초 간격. 브라우저인 척하지 않는다(이름표 USER_AGENT).
- 오류 메시지에는 주소가 들어가므로 예외는 종류 이름만 돌려준다 (공개 로그에 주소가 찍히지 않게).
- 기사 본문은 이 모듈 밖으로 문자열로만 나가고, 어디에도 저장하지 않는다.
"""
import gzip
import json
import re
import time
from datetime import datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit

import requests
from lxml import etree
from lxml import html as lxml_html

import robots as robots_mod

USER_AGENT = "NewsTextCollector/0.1"
HOST_GAP = 10           # 같은 사이트 요청 간격(초)
TIMEOUT = 20
MAX_BYTES = 10_000_000  # 응답 하나에서 읽을 최대 바이트 (사이트맵이 매우 큰 경우 대비)
MIN_TEXT = 300          # 본문으로 볼 최소 글자 수
MAX_FEED_TRIES = 6      # 언론사마다 목록(사이트맵 · RSS)을 받아 보는 최대 횟수

# robots에 사이트맵이 없을 때 차례로 볼 흔한 자리 (국내 언론사 CMS에서 흔한 RSS 주소 포함)
COMMON_FEEDS = ["/rss/allArticle.xml", "/sitemap.xml", "/rss", "/rss.xml", "/sitemap_index.xml"]
NOT_ARTICLE = re.compile(r"\.(jpe?g|png|gif|webp|svg|mp4|pdf|css|js)(\?|$)|/(photo|video|vod|tag|tags|search|section|category|"
                         r"sitemap|reporter|author|about)\b", re.I)
CHALLENGE = re.compile(r"just a moment|cf-chl|captcha|access denied|비정상적인 접근|자동화된 접근|robot check", re.I)
LOGIN = re.compile(r"로그인 후 이용|로그인이 필요|유료 ?회원|구독자 전용|프리미엄 회원|유료 기사|구독하고 계속")

session = requests.Session()
session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "ko-KR,ko;q=0.9"})
_last = {}          # 사이트별 마지막 요청 시각
_robots = {}        # 사이트별 Robots
stats = {"requests": 0}


class Blocked(Exception):
    """403 · 429 · 차단 화면. 그 언론사는 이번 실행에서 그만 받는다."""


def _wait(host, gap):
    t = _last.get(host)
    if t is not None:
        left = gap - (time.monotonic() - t)
        if left > 0:
            time.sleep(left)
    _last[host] = time.monotonic()


def fetch(url, gap=HOST_GAP):
    """(응답 코드, 바이트, 최종 주소). 접속 실패는 코드 0이고 최종 주소 자리에 예외 종류 이름."""
    host = urlsplit(url).hostname
    _wait(host, gap)
    stats["requests"] += 1
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
    if not p.hostname:
        return False
    path = (p.path or "/") + (f"?{p.query}" if p.query else "")
    return robots_for(p.hostname).allowed(path)


# ---------- 주소 판정 ----------

def base(host):
    return host[4:] if host.startswith("www.") else host


def same_site(url, host):
    h = urlsplit(url).hostname or ""
    b = base(host)
    return h == b or h.endswith("." + b)


def looks_article(url, host):
    """기사 화면으로 보이는 주소: 같은 사이트, 사진 · 섹션 · 소개 화면이 아니고, 기사 번호(숫자 5자리 이상)나 긴 경로가 있다."""
    if not same_site(url, host) or NOT_ARTICLE.search(url):
        return False
    p = urlsplit(url)
    path = p.path or "/"
    if path in ("", "/"):
        return False
    if re.search(r"\d{5,}", path) or len(path.strip("/").split("/")) >= 3:
        return True
    # 번호가 쿼리에 있는 CMS (예: articleView.html?idxno=…). 섹션 번호(cid · sid)는 기사로 보지 않는다
    return bool(re.search(r"(?:^|&)(?:idxno|aid|art_?id|article_?id|news_?id|no|id)=\d{5,}", p.query, re.I))


# ---------- 목록(사이트맵 · RSS) ----------

def _local(tag):
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _child(el, name):
    """바로 아래 자식 중 이름이 맞는 것의 글자 (image:loc 같은 손자 항목은 보지 않는다)."""
    for c in el:
        if _local(c.tag) == name and c.text:
            return c.text.strip()
    return ""


def _deep(el, name):
    for c in el.iter():
        if _local(c.tag) == name and c.text:
            return c.text.strip()
    return ""


def ts(text):
    if not text:
        return 0
    try:
        return datetime.fromisoformat(text.strip().replace("Z", "+00:00")).timestamp()
    except ValueError:
        pass
    try:
        return parsedate_to_datetime(text).timestamp()
    except (TypeError, ValueError, IndexError):
        return 0


def parse_feed(body):
    """사이트맵 · 사이트맵 목록 · RSS · Atom을 읽는다.
    ('index', [하위 주소, 최신 순]) 또는 ('urls', [(시각, 주소, 제목)]). 형식이 조금 깨진 XML도 읽는다."""
    if body[:2] == b"\x1f\x8b":
        body = gzip.decompress(body)
    body = re.sub(rb"[\x00-\x08\x0b\x0c\x0e-\x1f]", b"", body)   # XML에 쓸 수 없는 제어 문자 (11-19 인포스탁데일리)
    root = etree.fromstring(body, etree.XMLParser(recover=True, huge_tree=True, resolve_entities=False, no_network=True))
    if root is None:
        raise ValueError("빈 목록")
    kind = _local(root.tag)
    if kind == "sitemapindex":
        subs = [(ts(_child(s, "lastmod")), _child(s, "loc")) for s in root if _local(s.tag) == "sitemap"]
        return "index", [u for _, u in sorted(subs, reverse=True) if u]
    rows = []
    if kind == "urlset":
        for u in root:
            if _local(u.tag) == "url":
                rows.append((ts(_deep(u, "publication_date") or _child(u, "lastmod")), _child(u, "loc"), _deep(u, "title")))
    elif kind in ("rss", "RDF", "rdf"):
        for it in root.iter():
            if _local(it.tag) == "item":
                rows.append((ts(_child(it, "pubDate") or _child(it, "date")), _child(it, "link"), _child(it, "title")))
    elif kind == "feed":
        for e in root:
            if _local(e.tag) == "entry":
                link = next((c.get("href") for c in e if _local(c.tag) == "link" and c.get("href")), "")
                rows.append((ts(_child(e, "published") or _child(e, "updated")), link, _child(e, "title")))
    return "urls", [(t, u.strip(), re.sub(r"\s+", " ", ti).strip()) for t, u, ti in rows if u]


def pick_subs(subs):
    """사이트맵 목록에서 기사 쪽 하위 사이트맵을 앞으로 (최신 순은 유지)."""
    good = [u for u in subs if re.search(r"news|article|daily|today|latest|all", u, re.I)
            and not re.search(r"image|photo|video|sports|section|category|tag", u, re.I)]
    rest = [u for u in subs if u not in good and not re.search(r"image|photo|video", u, re.I)]
    return good + rest


def home_feeds(host):
    """첫 화면에 적힌 RSS · Atom 링크 (표준 자동 발견, <link rel="alternate" type="application/rss+xml">)."""
    url = f"https://{host}/"
    if not allowed(url):
        return []
    status, body, final = fetch(url)
    if status != 200 or not body:
        return []
    try:
        doc = lxml_html.fromstring(body)
    except (ValueError, etree.ParserError):
        return []
    out = []
    for ln in doc.xpath('//link[@rel="alternate"][@href]'):
        if re.search(r"rss|atom|xml", ln.get("type", ""), re.I):
            out.append(urljoin(final, ln.get("href")))
    return out


def discover(host, notes, override=None, cached=None, need_title=False, want=1):
    """언론사 자체 목록에서 기사 항목을 찾는다. (쓴 목록 주소, [(시각, 주소, 제목)] 최신 순).
    need_title이면 제목이 붙은 기사 항목이 있는 목록만 쓴다 (제목으로 기사를 고르는 collect용)."""
    rb = robots_for(host)
    queue = list(override or []) + ([cached] if cached else [])
    queue += sorted(rb.sitemaps, key=lambda u: 0 if re.search(r"news", u, re.I) else 1)
    queue += [f"https://{host}{p}" for p in COMMON_FEEDS]
    queue = list(dict.fromkeys(queue))
    tries, seen, looked_home = 0, set(), False
    while tries < MAX_FEED_TRIES:
        if not queue:
            if looked_home:
                break
            looked_home = True
            queue = [u for u in home_feeds(host) if u not in seen]
            notes.append(f"첫 화면 RSS {len(queue)}개")
            continue
        url = queue.pop(0)
        if url in seen or not same_site(url, host) or not allowed(url):
            continue
        seen.add(url)
        tries += 1
        status, body, _ = fetch(url)
        if status in (403, 429):
            notes.append(f"목록 {status}")
            raise Blocked({"verdict": "막힘", "status": status})
        if status != 200:
            notes.append(f"목록 {status}")
            continue
        try:
            kind, items = parse_feed(body)
        except (etree.XMLSyntaxError, ValueError, OSError, EOFError):
            notes.append("목록 해석 실패")
            continue
        if kind == "index":
            notes.append(f"사이트맵 목록 {len(items)}개")
            queue = [u for u in pick_subs(items) if u not in seen][:4] + queue
            continue
        arts = [(t, u, ti) for t, u, ti in items if looks_article(u, host) and allowed(u)]
        titled = [a for a in arts if a[2]]
        notes.append(f"목록 {len(items)}건 중 기사 {len(arts)}건(제목 {len(titled)})")
        use = titled if need_title else arts
        if len(use) >= want:
            use.sort(key=lambda x: -x[0])
            uniq = {}
            for a in use:
                uniq.setdefault(a[1], a)
            return url, list(uniq.values())
    if not tries:
        notes.append("볼 수 있는 목록 없음")
    return None, []


# ---------- 기사 본문 ----------

def _json_ld_body(doc):
    best = ""
    for s in doc.xpath('//script[@type="application/ld+json"]'):
        try:
            data = json.loads(s.text or "")
        except ValueError:
            continue
        stack = [data]
        while stack:
            x = stack.pop()
            if isinstance(x, dict):
                if isinstance(x.get("articleBody"), str) and len(x["articleBody"].strip()) > len(best):
                    best = x["articleBody"].strip()
                stack.extend(x.values())
            elif isinstance(x, list):
                stack.extend(x)
    return best


def _embedded_body(text):
    """화면을 나중에 그리는 사이트: 페이지 안 데이터 블록의 본문 문단 (Arc 계열 content_elements)."""
    parts = []
    for m in re.finditer(r'"type"\s*:\s*"text"\s*,\s*"content"\s*:\s*"((?:[^"\\]|\\.)*)"', text):
        try:
            parts.append(re.sub(r"<[^>]+>", "", json.loads(f'"{m.group(1)}"')).strip())
        except ValueError:
            pass
    return "\n".join(p for p in parts if p)


def extract(body):
    """(본문, 찾은 방법, 보이는 글자 수). 본문은 부르는 쪽 메모리에서만 쓴다."""
    import trafilatura
    visible, ld = 0, ""
    try:
        doc = lxml_html.fromstring(body)
        ld = _json_ld_body(doc)
        for bad in doc.xpath("//script|//style|//noscript"):
            bad.drop_tree()
        visible = len(re.sub(r"\s+", " ", doc.text_content()).strip())
    except (ValueError, etree.ParserError):
        pass
    main = trafilatura.extract(body, include_comments=False, include_tables=False, favor_precision=True) or ""
    if len(main) >= MIN_TEXT:
        return main, "화면", visible
    if len(ld) >= MIN_TEXT:
        return ld, "JSON-LD", visible
    emb = _embedded_body(body.decode("utf-8", "ignore"))
    if len(emb) >= MIN_TEXT:
        return emb, "데이터 블록", visible
    return max((main, ld, emb), key=len), "", visible


def get_article(url, host):
    """기사 하나를 받는다. (판정 칸 dict, 본문 또는 None). 403 · 429 · 차단 화면이면 Blocked."""
    row = {"robots": allowed(url)}
    if not row["robots"]:
        row["verdict"] = "robots 거부"
        return row, None
    status, body, final = fetch(url)
    row["status"] = status
    row["sameSite"] = status != 0 and same_site(final, host)
    row["bytes"] = len(body)
    if status in (403, 429) or (status == 200 and CHALLENGE.search(body[:20000].decode("utf-8", "ignore"))):
        row["verdict"] = "막힘"
        raise Blocked(row)
    if status != 200:
        row["verdict"] = f"응답 {status}" if status else f"접속 실패({final})"
        return row, None
    text, how, visible = extract(body)
    login = bool(LOGIN.search(body[:200000].decode("utf-8", "ignore")))
    del body
    row.update(visibleChars=visible, textChars=len(text), bodyFrom=how, loginMarker=login)
    if len(text) >= MIN_TEXT:
        row["verdict"] = "본문 받음"
        return row, text
    row["verdict"] = ("유료 · 로그인 의심" if login else
                      "본문 비어 있음(화면을 나중에 그리는 구조 의심)" if visible < MIN_TEXT else "본문 추출 실패")
    return row, None
