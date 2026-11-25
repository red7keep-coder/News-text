"""글(posts)과 사실 사건(facts)을 잇는 열쇠 (요청-알파스코프-뉴스-글-발행 28절 R36 · R37).

posts.event = 그 글을 쓴 사건 이름 (facts · headlines의 event와 글자까지 같게).
facts 사건 .post = 그 사건으로 쓴 글의 slug — 같은 실행 글 + 지난 POST_HOURS 안의 글.

사건 이름은 다음 실행에서 바뀔 수 있다 (R26 합치기). 그래서 이름이 같거나, 이름이 달라도 글을 쓴 기사(eventKey)의
절반 넘게가 그 사건에 들어 있으면 같은 사건으로 본다. 글 하나는 사건 하나에만 붙인다 (가장 많이 겹치는 사건).
잘못 붙이면 블로그가 다른 사건을 지우므로, 애매하면 붙이지 않는다.
"""
from datetime import timedelta

POST_HOURS = 36


def event_name(ev):
    return ev.get("event") or ev.get("title") or ""


def event_key(ev):
    return sorted({s.get("url") for s in ev.get("sources", []) if s.get("url")})


def recent_posts(db, blog_db, now, hours=POST_HOURS):
    """지난 hours 동안 블로그에 넘긴 글 [{event, eventTitle, key, slug}], 새 글 먼저.
    slug는 블로그 저장소 글에서 읽는다 (같은 문서 번호, 겹치면 -2가 붙으므로 수집기 칸이 아니라 실제 값)."""
    from google.cloud.firestore_v1.base_query import FieldFilter
    rows = [(d.id, d.to_dict()) for d in
            db.collection("posts").where(filter=FieldFilter("createdAt", ">=", now - timedelta(hours=hours))).stream()]
    rows = [(i, x) for i, x in rows if x.get("status") == "초안"]
    rows.sort(key=lambda r: r[1].get("createdAt"), reverse=True)
    out = []
    for doc_id, x in rows:
        b = blog_db.collection("posts").document(doc_id).get()
        slug = (b.to_dict() or {}).get("slug") if b.exists else ""
        if slug:
            out.append({"event": x.get("event", ""), "eventTitle": x.get("eventTitle", ""),
                        "key": set(x.get("eventKey") or []), "slug": slug})
    return out


def assign(events, posts):
    """{사건 이름: 글 slug}. 새 글부터 붙이고, 이미 글이 붙은 사건에는 더 붙이지 않는다."""
    out = {}
    for p in posts:
        hit = None
        for ev in events:
            if p["event"] and event_name(ev) == p["event"] or p["eventTitle"] and ev.get("title") == p["eventTitle"]:
                hit = ev
                break
        if hit is None and p["key"]:
            best, n = None, 0
            for ev in events:
                k = len(p["key"] & set(event_key(ev)))
                if k > n:
                    best, n = ev, k
            if best is not None and n * 2 > len(p["key"]):
                hit = best
        if hit is not None:
            out.setdefault(event_name(hit), p["slug"])
    return out
