"""0단계 접속 시험: 언론사 기사 페이지가 이 실행기에서 실제로 받아지는지 본다.

- 언론사 목록은 Firestore `outlets`에서 읽는다 (코드 · 로그에 언론사 이름 · 주소를 두지 않는다).
- 언론사마다 최근 기사 주소를 언론사 자체 사이트맵 · RSS에서 찾고 최대 3건을 받아 본다.
  구글 뉴스 RSS는 쓰지 않는다: news.google.com의 robots가 /rss/를 허용하지 않는다 (11-19 첫 시험에서 확인).
- 본문은 글자 수만 세고 바로 버린다. AI는 쓰지 않는다.
- 결과는 Firestore `probe/{실행 번호}`에 쓴다. 공개 로그에는 언론사 번호 · 건수 · 글자 수 · 판정만 찍는다.
"""
import os
from datetime import datetime, timezone

import net

ARTICLES = 3            # 언론사마다 받아 볼 기사 수


def log(msg):
    print(msg, flush=True)


def probe_outlet(o):
    host = o["host"]
    res = {"no": o["no"], "notes": [], "articles": []}
    rb = net.robots_for(host)
    res["robotsStatus"] = rb.status
    res["robotsNamesUs"] = rb.named_us()
    res["robotsAllowsRoot"] = rb.allowed("/")
    if rb.status in (403, 429):
        res["verdict"] = "막힘(robots 403 · 429)"
        return res
    if rb.status == 0 or rb.status >= 500:
        res["verdict"] = f"robots 못 읽음({rb.status})"
        return res
    try:
        feed, items = net.discover(host, res["notes"], override=o.get("feeds"), cached=o.get("feed"))
        urls = [u for _, u, _ in items[:ARTICLES]]
        res["foundTotal"] = len(urls)
        if feed:
            res["feed"] = feed
        if not urls:
            res["verdict"] = "기사 주소 못 찾음"
            return res
        for u in urls:
            row, text = net.get_article(u, host)
            del text                                # 본문은 여기서 버린다
            row["url"] = u
            res["articles"].append(row)
    except net.Blocked as b:
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
    db = firestore.Client(project=os.environ["GCP_PROJECT"])
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
        "userAgent": net.USER_AGENT, "runner": os.environ.get("RUNNER_NAME", "local"),
        "requests": net.stats["requests"], "counts": counts, "results": results,
    })
    log(f"끝. 요청 {net.stats['requests']}번, 판정 " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))


if __name__ == "__main__":
    main()
