"""목업 보기 서버 — 폰 · 태블릿에서 즐겨찾기 하나로 모든 목업을 연다.

    python mockup/mock_server.py            # 포트 8810 고정 (주소가 바뀌지 않게)
    python mockup/mock_server.py 8811       # 포트 바꾸기
    python mockup/mock_server.py --new "home/*.html" --note "무엇을 바꿨나 한 줄"   # N 표시 (서버가 꺼져 있어도 됨)

- 목록(`/`)은 열 때마다 mockup/output/ 아래 *.html 을 새로 읽는다 — 목업이 늘거나 바뀌면 새로고침만 하면 된다.
- 목록은 세 칸: 발행 예정 · 발행된 화면(접힘) · 자동 보관(접힘). 어느 칸인지는 mockup/stages.json 이 정한다(Claude 가 관리).
- N 표시: 만들거나 고친 목업은 맨 위 `새로 바뀐 목업` 에 메모와 함께 모이고, 한 번 열면 사라진다.
- 같은 와이파이에 열리므로 mockup/output/ 밖(.env · 코드)은 내주지 않는다.
- 기록 파일 = mockup/.prefs.json (output 밖이라 서버가 내주지 않는다).
"""
import html
import json
import os
import socket
import subprocess
import sys
import threading
import time
from fnmatch import fnmatch
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, unquote, urlparse

BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BASE, "output")
STAGES = os.path.join(BASE, "stages.json")
PREFS = os.path.join(BASE, ".prefs.json")
PORT = next((int(a) for a in sys.argv[1:] if a.isdigit()), 8810)
SERVE_EXT = (".html", ".css", ".js", ".json", ".png", ".jpg", ".jpeg", ".webp", ".svg", ".gif", ".woff2")
_lock = threading.Lock()


# ── 기록 ────────────────────────────────────────────
def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def load_prefs():
    p = load_json(PREFS, {})
    return {"new": p.get("new", {})}


def save_prefs(p):
    with open(PREFS + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(p, fh, ensure_ascii=False, indent=1)
    os.replace(PREFS + ".tmp", PREFS)


# ── 목업 찾기 ──────────────────────────────────────
def mockups():
    """[(상대 경로 'home/mock_home.html', 수정 시각)] — 최신 먼저."""
    out = []
    for root, _, files in os.walk(OUT):
        for f in files:
            if f.endswith(".html"):
                fp = os.path.join(root, f)
                out.append((os.path.relpath(fp, OUT).replace(os.sep, "/"), os.path.getmtime(fp)))
    return sorted(out, key=lambda x: -x[1])


_titles = {}


def title_of(rel, mtime):
    hit = _titles.get(rel)
    if not hit or hit[0] != mtime:
        with open(os.path.join(OUT, rel), encoding="utf-8", errors="replace") as fh:
            raw = fh.read(20000)
        i, j = raw.lower().find("<title>"), raw.lower().find("</title>")
        t = html.unescape(raw[i + 7:j]).strip() if 0 <= i < j else ""
        hit = _titles[rel] = (mtime, t or rel)
    return hit[1]


def stage_of(rel, stages):
    """('plan', 번호) · ('live', 번호) · None(= 자동 보관)."""
    for kind in ("plan", "live"):
        for i, s in enumerate(stages.get(kind, [])):
            if any(fnmatch(rel, p) for p in s.get("files", [])):
                return kind, i
    return None


# ── 목록 화면 ──────────────────────────────────────
CSS = """
:root{--bg:#FFFFFF;--line:#E2E8F0;--line-soft:#F3F3F3;--text:#111;--text-2:#333;--sub:#6B7280;--muted:#9CA3AF;
--font:'Noto Sans KR',sans-serif;--num:'Inter','Noto Sans KR',sans-serif}
*{box-sizing:border-box}html,body{margin:0;background:var(--bg)}
body{color:var(--text-2);font-family:var(--font);font-size:15px;line-height:23px;font-weight:400;word-break:keep-all;overflow-wrap:break-word}
main{max-width:720px;margin:0 auto;padding:16px 16px 40px}
@media (max-width:680px){main{padding-left:14px;padding-right:14px}}
h1{font-size:19px;line-height:29px;font-weight:700;color:var(--text);margin:4px 0 12px}
h2{font-size:15px;line-height:23px;font-weight:700;color:var(--text);margin:24px 0 8px;display:flex;align-items:center;gap:5px}
input{width:100%;padding:9px 12px;border:1px solid var(--line);border-radius:4px;background:var(--bg);color:var(--text-2);
font-family:var(--font);font-size:15px;line-height:23px}
.box{background:var(--bg);border:1px solid var(--line);border-radius:10px;margin:8px 0;overflow:hidden}
.feat{padding:10px 16px;border-bottom:1px solid var(--line)}
.feat b{display:block;font-size:15px;line-height:23px;font-weight:700;color:var(--text)}
.feat small{display:block;color:var(--sub);font-size:13px;line-height:19px}
a.row{display:flex;gap:10px;align-items:center;padding:12px 16px;min-height:48px;color:inherit;text-decoration:none;border-top:1px solid var(--line-soft)}
a.row:first-of-type{border-top:0}a.row:active{background:var(--line-soft)}
.t{flex:1;min-width:0;color:var(--text);font-weight:500}
.t small{display:block;color:var(--sub);font-size:13px;line-height:19px;font-weight:400}
.n{flex:none;background:#B91C1C;color:#FFFFFF;font-family:var(--num);font-size:12px;line-height:12px;font-weight:700;border-radius:3px;padding:1px 4px}
.note{display:block;color:var(--text-2);font-size:13px;line-height:19px;font-weight:400}
.when{color:var(--sub);font-size:13px;line-height:19px;white-space:nowrap;font-family:var(--num);font-variant-numeric:tabular-nums}
details>summary{cursor:pointer;padding:12px 16px;font-size:14px;line-height:20px;font-weight:700;color:var(--text-2);list-style:none}
details>summary::-webkit-details-marker{display:none}
details>summary::after{content:' ▾';color:var(--sub)}details[open]>summary::after{content:' ▴'}
.empty{color:var(--muted);padding:12px 16px;font-size:14px;line-height:20px}
.cnt{font-family:var(--num);font-variant-numeric:tabular-nums;color:var(--sub);font-weight:400}
"""


def when(ts):
    d = time.time() - ts
    if d < 3600:
        return f"{max(1, int(d // 60))}분 전"
    if d < 86400:
        return f"{int(d // 3600)}시간 전"
    t = time.localtime(ts)
    return f"{t.tm_mon}.{t.tm_mday}"


def row(rel, mtime, new=None):
    t = html.escape(title_of(rel, mtime))
    badge = '<span class="n">N</span>' if new is not None else ""
    note = f'<span class="note">{html.escape(new.get("note", ""))}</span>' if new and new.get("note") else ""
    return (f'<a class="row" href="/m/{quote(rel)}" data-k="{html.escape(rel.lower() + " " + t.lower())}">{badge}'
            f'<span class="t">{t}{note}<small>{html.escape(rel)}</small></span><span class="when">{when(mtime)}</span></a>')


def index_page():
    stages = load_json(STAGES, {"plan": [], "live": []})
    items = mockups()
    new = load_prefs()["new"]
    plan = {i: [] for i in range(len(stages.get("plan", [])))}
    live = {i: [] for i in range(len(stages.get("live", [])))}
    archive = []
    for rel, mt in items:
        st = stage_of(rel, stages)
        (plan[st[1]] if st and st[0] == "plan" else live[st[1]] if st else archive).append((rel, mt))

    h = [f"<!doctype html><html lang=ko><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
         f"<title>목업 목록</title><link rel=preconnect href='https://fonts.googleapis.com'><link rel=preconnect href='https://fonts.gstatic.com' crossorigin>"
         f"<link href='https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&family=Noto+Sans+KR:wght@400;500;700;900&display=swap' rel=stylesheet>"
         f"<style>{CSS}</style><main><h1>목업 목록</h1>"
         "<input id=q placeholder='이름으로 찾기' oninput=\"for(const a of document.querySelectorAll('a.row'))"
         "a.style.display=a.dataset.k.includes(this.value.toLowerCase())?'':'none'\">"]

    fresh = [(r, m) for r, m in items if r in new]
    if fresh:
        h.append(f"<h2><span class=n>N</span>새로 바뀐 목업 <span class=cnt>{len(fresh)}</span></h2><div class=box>")
        h += [row(r, m, new[r]) for r, m in fresh]
        h.append("</div>")

    h.append("<h2>발행 예정</h2>")
    for i, s in enumerate(stages.get("plan", [])):
        rows = "".join(row(r, m, new.get(r)) for r, m in plan[i]) or "<div class=empty>목업 파일이 아직 없습니다</div>"
        h.append(f"<div class=box><div class=feat><b>{html.escape(s.get('name', ''))}</b>"
                 f"<small>남은 일 : {html.escape(s.get('todo', ''))}</small></div>{rows}</div>")
    if not stages.get("plan"):
        h.append("<div class='box empty'>없음</div>")

    h.append("<h2>발행된 화면</h2>")
    for i, s in enumerate(stages.get("live", [])):
        h.append(f"<details class=box><summary>{html.escape(s.get('name', ''))} <span class=cnt>{len(live[i])}</span></summary>"
                 + "".join(row(r, m, new.get(r)) for r, m in live[i]) + "</details>")
    if not stages.get("live"):
        h.append("<div class='box empty'>없음</div>")

    h.append(f"<h2>자동 보관</h2><details class=box><summary>어느 표에도 없는 목업 <span class=cnt>{len(archive)}</span></summary>"
             + "".join(row(r, m, new.get(r)) for r, m in archive) + "</details></main>")
    return "".join(h)


BACK = ("<a href='/' style='position:fixed;right:12px;bottom:12px;z-index:99999;background:#FFFFFF;color:#333;"
        "border:1px solid #E2E8F0;font:700 14px/20px \"Noto Sans KR\",sans-serif;padding:8px 14px;border-radius:4px;"
        "text-decoration:none'>목록</a>")


# ── 서버 ────────────────────────────────────────────
class Handler(SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Cache-Control", "no-store")   # 고친 뒤 새로고침하면 바로 보이게
        super().end_headers()

    def _send(self, body, ctype="text/html; charset=utf-8"):
        body = body.encode() if isinstance(body, str) else body
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = unquote(urlparse(self.path).path)
        if path in ("/", "/index.html"):
            return self._send(index_page())
        if not path.startswith("/m/"):
            return self.send_error(404)
        rel = path[3:]
        fp = os.path.realpath(os.path.join(OUT, rel))
        if not fp.startswith(os.path.realpath(OUT) + os.sep) or not fp.lower().endswith(SERVE_EXT) or not os.path.isfile(fp):
            return self.send_error(404)   # output 폴더 안 · 정해진 종류만
        if fp.endswith(".html"):
            mark_seen(rel)
            with open(fp, encoding="utf-8", errors="replace") as fh:
                page = fh.read()
            i = page.lower().rfind("</body>")
            return self._send(page[:i] + BACK + page[i:] if i >= 0 else page + BACK)
        with open(fp, "rb") as fh:
            self._send(fh.read(), self.guess_type(fp))

    def log_message(self, *a):
        pass


def mark_seen(rel):
    with _lock:
        p = load_prefs()
        if rel in p["new"]:
            del p["new"][rel]
            save_prefs(p)


def mark_new(pats, note):
    hit = [r for r, _ in mockups() if any(fnmatch(r, x) for x in pats)]
    with _lock:
        p = load_prefs()
        for r in hit:
            p["new"][r] = {"t": time.time(), "note": note}
        save_prefs(p)
    print(f"N 표시 {len(hit)}개: " + ", ".join(hit) if hit else f"맞는 목업 없음: {pats}")


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "?"


if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    if "--new" in sys.argv:
        a = sys.argv[sys.argv.index("--new") + 1:]
        note = a[a.index("--note") + 1] if "--note" in a else ""
        mark_new(a[:a.index("--note")] if "--note" in a else a, note)
        sys.exit(0)
    host = ""
    try:
        host = subprocess.run(["scutil", "--get", "LocalHostName"], capture_output=True, text=True).stdout.strip()
    except OSError:
        pass
    print("목업 목록:")
    if host:
        print(f"  http://{host}.local:{PORT}/   ← 즐겨찾기 추천 (와이파이 IP 가 바뀌어도 그대로)")
    print(f"  http://{lan_ip()}:{PORT}/")
    print(f"  http://localhost:{PORT}/")
    print("끄기: Ctrl+C", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
