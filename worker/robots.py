"""robots.txt 읽기 · 판정.

파이썬 기본 urllib.robotparser는 경로의 '*' · '$'를 모르므로 직접 판정한다.
규칙은 구글 방식(RFC 9309): 우리 이름표와 맞는 그룹이 있으면 그 그룹만, 없으면 '*' 그룹.
경로에 맞는 규칙 중 가장 긴 것이 이기고, 길이가 같으면 Allow가 이긴다.
"""
import re

AGENT_TOKEN = "newstextcollector"   # 이름표(User-Agent)의 앞 단어를 소문자로


class Robots:
    def __init__(self, text, status):
        self.status = status          # robots.txt 응답 코드 (0 = 접속 실패)
        self.groups = []              # [(agents[], rules[(allow, pattern)])]
        self.sitemaps = []
        if text:
            self._parse(text)

    def _parse(self, text):
        agents, rules, last_was_agent = [], [], False
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            key, val = line.split(":", 1)
            key, val = key.strip().lower(), val.strip()
            if key == "user-agent":
                if not last_was_agent and agents:
                    self.groups.append((agents, rules))
                    agents, rules = [], []
                agents.append(val.lower())
                last_was_agent = True
            elif key in ("allow", "disallow"):
                if agents:
                    rules.append((key == "allow", val))
                last_was_agent = False
            elif key == "sitemap":
                self.sitemaps.append(val)
            else:
                last_was_agent = False
        if agents:
            self.groups.append((agents, rules))

    def _rules(self):
        mine = [r for a, r in self.groups if any(x == AGENT_TOKEN for x in a)]
        if mine:
            return [x for r in mine for x in r]
        return [x for a, r in self.groups if "*" in a for x in r]

    def named_us(self):
        """우리 이름표를 따로 지목한 그룹이 있나 (있으면 사람이 봐야 함)."""
        return any(any(x == AGENT_TOKEN for x in a) for a, _ in self.groups)

    def allowed(self, path):
        """path는 '/경로?쿼리'. robots를 못 읽은 경우: 4xx는 허용(규칙 없음), 5xx · 접속 실패는 거부."""
        if self.status == 0 or self.status >= 500:
            return False
        if self.status >= 400:
            return True
        best_len, best_allow = -1, True
        for allow, pat in self._rules():
            if pat == "":
                if not allow:
                    continue        # 'Disallow:' 빈 값 = 모두 허용
                pat = "/"
            if _match(pat, path):
                n = len(pat)
                if n > best_len or (n == best_len and allow):
                    best_len, best_allow = n, allow
        return best_allow


def _match(pattern, path):
    anchored = pattern.endswith("$")
    if anchored:
        pattern = pattern[:-1]
    rx = "".join(".*" if c == "*" else re.escape(c) for c in pattern)
    return re.match(rx + ("$" if anchored else ""), path) is not None
