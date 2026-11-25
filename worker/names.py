"""사람 이름 규칙 (사용자 11-25) — 글(write.py)과 블로그 사실 줄(collect.py facts)이 함께 쓴다.

한국 사람: 국가 요직 · 경제 · 금융 수장 · 대기업 총수만 실명. 나머지는 '회사 · 부서 직함'으로만.
외국 사람: '회사 + 이름 + 직함'을 함께.
"""
import re

# 이름을 써도 되는 한국 사람의 직함 (소속 + 직함에서 찾는다)
VIP_TITLE = re.compile(r"대통령|총리|장관|국회의장|당\s?대표|대표최고위원|(?<!부)총재|금융위원장|금융감독원장|금감원장"
                       r"|공정거래위원장|공정위원장|국세청장|그룹\s?회장|(?<![부명예])회장|총수")
NOT_VIP = re.compile(r"협회|연합회|조합|학회|재단|단체|위원회 위원|부회장|명예회장|대행")
KO_NAME = re.compile(r"[가-힣]{2,4}")
NOT_NAME = {"관계자", "대변인", "당국자", "소식통"}
# 외국인 직함의 다른 표기: 어느 쪽을 써도 직함을 쓴 것으로 본다
TITLE_ALIAS = [("CEO", "최고경영자"), ("CFO", "최고재무책임자"), ("CTO", "최고기술책임자"), ("COO", "최고운영책임자"),
               ("Chairman", "의장"), ("President", "대통령"), ("Governor", "총재")]
# 이름 바로 앞 · 뒤에 오면 '직함이 이미 있다'고 보는 말
TITLE_WORD = re.compile(r"(대표|사장|부사장|회장|부회장|의장|이사|전무|상무|부장|차장|과장|팀장|실장|본부장|센터장|원장|청장|국장"
                        r"|장관|차관|위원장|위원|의원|총재|부총재|교수|연구원|애널리스트|CEO|CFO|CTO|COO)")


def people(facts):
    """사실들에 나오는 사람 [{name, org, title, foreign}]. ④ people 칸(11-25부터)과 발언자를 합친다 (이름마다 하나)."""
    out = {}
    for f in facts or []:
        for p in f.get("people") or []:
            name = (p.get("name") or "").strip()
            if name and name not in NOT_NAME:
                d = out.setdefault(name, {"name": name, "org": "", "title": "", "foreign": False})
                d["org"], d["title"] = d["org"] or (p.get("org") or "").strip(), d["title"] or (p.get("title") or "").strip()
                d["foreign"] = d["foreign"] or p.get("foreign") is True
        q = f.get("quote") or {}
        name = (q.get("speaker") or "").strip()
        if name and name not in NOT_NAME and not q.get("anonymous"):
            d = out.setdefault(name, {"name": name, "org": "", "title": "", "foreign": not KO_NAME.fullmatch(name)})
            d["title"] = d["title"] or (q.get("title") or "").strip()
    return list(out.values())


def is_vip(p):
    role = f"{p.get('org', '')} {p.get('title', '')}"
    return bool(VIP_TITLE.search(role)) and not NOT_VIP.search(role)


def is_hidden(p):
    """이름을 쓰면 안 되는 한국 사람인가 (한글 이름, 띄어쓰기 빼고 2 ~ 4자, 세 부류가 아님)."""
    return not p["foreign"] and bool(KO_NAME.fullmatch(p["name"].replace(" ", ""))) and not is_vip(p)


def role(p):
    """소속 + 직함 (직함에 소속이 이미 들어 있으면 한 번만)."""
    org, title = p.get("org", ""), p.get("title", "")
    if org and title.startswith(org):
        return title
    return " ".join(x for x in (org, title) if x)


def hidden_names(facts):
    """{이름: 소속 직함} — 글 · 사실 줄에 쓰면 안 되는 이름."""
    return {p["name"]: role(p) for p in people(facts) if is_hidden(p)}


def has_title(text, title):
    if title in text:
        return True
    for a, b in TITLE_ALIAS:
        if (a in title or b in title) and (a in text or b in text):
            return True
    return False


# ---------- 사실 줄 고치기 (블로그 facts — 고쳐 쓰기 호출 없이 코드로) ----------

JOSA = [("으로", "로"), ("은", "는"), ("이", "가"), ("을", "를"), ("과", "와")]


def _batchim(ch):
    if "가" <= ch <= "힣":
        return (ord(ch) - 0xAC00) % 28 != 0
    return ch.upper() in "LMNR013678"


def _fix_josa(prev, right):
    """right 맨 앞 조사를 prev(앞 낱말 끝 글자)에 맞춘다."""
    for a, b in JOSA:
        for x in (a, b):
            if right.startswith(x) and (len(right) == len(x) or not ("가" <= right[len(x)] <= "힣")):
                return (a if _batchim(prev) else b) + right[len(x):]
    return right


def _drop_name(text, name, who):
    """한 문장에서 이름을 뺀다. 앞뒤에 직함 · 소속이 있으면 이름만 지우고, 없으면 소속 직함으로 바꾼다. 못 고치면 None."""
    while name in text:
        i = text.index(name)
        left, right = text[:i].rstrip(), text[i + len(name):]
        nxt = re.match(r"\s+(\S+)", right)
        if nxt and (TITLE_WORD.match(nxt.group(1)) or (who["org"] and nxt.group(1).startswith(who["org"]))):
            org = who["org"] if who["org"] and who["org"] not in text else ""   # 소속이 문장에 없으면 이름 자리에
            text = (left + " " if left else "") + org + right if org else left + right   # '현대모비스 이규석 대표는' → '현대모비스 대표는'
        elif left and TITLE_WORD.search(left.split()[-1]) and not right[:1].isspace():
            text = left + _fix_josa(left[-1], right)              # '현대모비스 대표이사 이규석은' → '현대모비스 대표이사는'
        elif role(who):
            r = role(who)
            if who["org"] and left.endswith(who["org"]):
                r = who["title"] or r
            if not r:
                return None
            text = (left + " " if left else "") + r + _fix_josa(r[-1], right)
        else:
            return None
    return re.sub(r"\s{2,}", " ", text).strip()


def _full_foreign(text, p):
    """외국 사람을 처음 쓴 곳에 소속 · 직함을 붙인다 ('젠슨 황은' → '엔비디아 젠슨 황 CEO는')."""
    if p["name"] not in text:
        return text
    full = p["name"]
    i = text.index(p["name"])
    if p["org"] and p["org"] not in text:
        full = f"{p['org']} {full}"
    if p["title"] and not has_title(text, p["title"]):
        full = f"{full} {p['title']}"
    if full == p["name"]:
        return text
    right = text[i + len(p["name"]):]
    return text[:i] + full + _fix_josa(full[-1], right)


def scrub_text(text, ppl):
    """사실 줄 글에 이름 규칙을 적용한다. 못 고치면 None (그 줄은 넘기지 않는다)."""
    for p in ppl:
        if not text:
            break
        if is_hidden(p) and p["name"] in text:
            text = _drop_name(text, p["name"], p)
            if text is None:
                return None
        elif p["foreign"]:
            text = _full_foreign(text, p)
    return text


def scrub_quote(q, ppl):
    """발언자 칸: 이름을 쓰면 안 되는 사람은 이름 대신 소속 직함(직함 칸은 비움). 소속 · 직함을 모르면 발언을 뺀다(None)."""
    if not q or not q.get("speaker"):
        return q
    p = next((x for x in ppl if x["name"] == q["speaker"].strip()), None)
    if p is None or not is_hidden(p):
        if p and p["foreign"] and p["org"] and p["org"] not in (q.get("title") or ""):
            return dict(q, title=f"{p['org']} {q.get('title') or p['title']}".strip())
        return q
    r = role(p) or (q.get("title") or "").strip()
    return dict(q, speaker=r, title="") if r else None
