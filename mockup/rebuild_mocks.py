"""목업 전체 다시 그리기 — 목업과 실제 페이지가 늘 같은 코드 · 같은 디자인으로 보이게.

목업은 실제 페이지를 만드는 렌더러로 그린 결과물이다. 공통 디자인(공통 CSS · 렌더러)을 고친 뒤
이 스크립트를 돌리면 쓰는 목업이 모두 최신 코드로 다시 그려진다.
여기서는 AI 호출 · 배포 · 외부 쓰기를 하지 않는다 — 받아 둔 표본 자료로 그리기만 한다.

    python mockup/rebuild_mocks.py          # 전부
    python mockup/rebuild_mocks.py 홈 기사   # 이름에 그 말이 든 묶음만

새 목업 묶음을 만들면 JOBS 에 한 줄 추가한다 — 여기 없는 목업은 공통 디자인이 바뀌어도 옛 모양으로 남는다.
"""
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # 저장소 맨 위

# (묶음 이름, 명령) — 명령은 저장소 맨 위에서 돈다. 예:
#   ("홈", ["site/render.py", "--mock", "home"]),
#   ("기사 페이지", ["site/render.py", "--mock", "article"]),
JOBS = [
    # 운영자 페이지는 비공개 문서 저장소(News-text-doc, 이 맥에서는 바탕화면에 나란히)에 있다. 없으면 [건너뜀]
    ("운영자 페이지", ["../News-text-doc/admin/render.py", "--mock"]),
]


def main(words):
    jobs = [j for j in JOBS if not words or any(w in j[0] for w in words)]
    if not jobs:
        print("다시 그릴 묶음 없음 (JOBS 가 비었거나 이름이 맞지 않음)")
        return 0
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    bad, t0 = [], time.time()
    for name, args in jobs:
        if args[0] != "-c" and not os.path.exists(os.path.join(ROOT, args[0])):
            print(f"[건너뜀] {name}: {args[0]} 없음")
            bad.append(name)
            continue
        t = time.time()
        r = subprocess.run([sys.executable, *args], cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r.returncode:
            tail = (r.stderr or r.stdout).strip().splitlines()[-1:] or [""]
            print(f"[실패] {name} ({time.time() - t:.0f}초): {tail[0][:160]}")
            bad.append(name)
        else:
            print(f"[완료] {name} ({time.time() - t:.0f}초)")
    print(f"\n{len(jobs) - len(bad)}/{len(jobs)} 묶음 다시 그림 · {time.time() - t0:.0f}초" + (f" · 실패 {', '.join(bad)}" if bad else ""))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
