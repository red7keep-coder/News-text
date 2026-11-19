"""Gemini 호출 (REST). 텍스트를 넣고 JSON 하나를 받는다.

- 키는 이 수집기의 GEMINI_API_KEY 하나만 쓴다. 키를 더 받거나 돌려 쓰지 않는다 (시작-안내 6절).
- 모델은 GEMINI_MODELS 순서대로. 첫 모델이 404 · 429(한도) · 5xx 두 번이면 다음 모델로 (3.5 lite와 3.1 lite는 한도를 따로 센다).
- 공개 로그에는 응답 코드 · 호출 수만 남긴다. 입력문 · 응답 원문 · 오류 본문은 찍지 않는다 (입력에 기사 본문이 들어간다).
"""
import json
import os
import re
import time

import requests

URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
MODELS = [m.strip() for m in (os.environ.get("GEMINI_MODELS") or "gemini-3.5-flash-lite,gemini-3.1-flash-lite").split(",")
          if m.strip()]   # 저장소 변수가 비어 있으면 빈 문자열로 들어오므로 'or'로 기본값
GAP = 4.5               # 분당 15회 한도 → 4초 남짓 간격
ATTEMPTS = 4
TIMEOUT = 180
stats = {"calls": 0, "byModel": {}, "errors": {}}
_last = 0.0


class LLMError(Exception):
    """다시 해도 안 됐다. 메시지에는 코드 · 종류만 있다."""


class _Switch(Exception):
    pass


def key():
    return os.environ.get("GEMINI_API_KEY", "").strip()


def _wait(gap=GAP):
    global _last
    left = gap - (time.monotonic() - _last)
    if _last and left > 0:
        time.sleep(left)
    _last = time.monotonic()


def _count_error(what):
    stats["errors"][what] = stats["errors"].get(what, 0) + 1


def extract_json(text):
    text = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        text = m.group(1).strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    starts = [i for i in (text.find("{"), text.find("[")) if i >= 0]
    if not starts:
        raise ValueError("JSON 없음")
    return json.loads(text[min(starts):max(text.rfind("}"), text.rfind("]")) + 1])


def _call(model, prompt, max_out, gap=GAP, temperature=0):
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": temperature, "responseMimeType": "application/json", "maxOutputTokens": max_out},
    }
    _wait(gap)
    stats["calls"] += 1
    stats["byModel"][model] = stats["byModel"].get(model, 0) + 1
    try:
        r = requests.post(URL.format(model=model), json=body, timeout=TIMEOUT,
                          headers={"x-goog-api-key": key(), "Content-Type": "application/json"})
    except requests.RequestException as e:
        return 0, type(e).__name__, ""
    if r.status_code != 200:
        return r.status_code, "", ""
    try:
        data = r.json()
        cand = data["candidates"][0]
        finish = str(cand.get("finishReason") or "")
        text = "".join(p.get("text", "") for p in cand.get("content", {}).get("parts", []))
        return 200, text, finish
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        return 200, "", ""


def generate_json(prompt, max_out=8192, models=None, gap=GAP, temperature=0, attempts=ATTEMPTS):
    """prompt → 파싱된 JSON. 안 되면 LLMError.
    models · gap: 글쓰기(write.py)는 Hydrogen → 3.6 Flash(분당 5회라 13초 간격)를 따로 넘긴다."""
    if not key():
        raise LLMError("GEMINI_API_KEY 없음")
    models = models or MODELS
    for i, model in enumerate(models):
        has_next = i + 1 < len(models)
        try:
            return _with_model(model, prompt, max_out, has_next, gap, temperature, attempts)
        except _Switch as e:
            _count_error(f"{model} → 예비 ({e})")
            continue
    raise LLMError("모든 모델 실패")


def _with_model(model, prompt, max_out, has_next, gap=GAP, temperature=0, attempts=ATTEMPTS):
    last = ""
    for attempt in range(1, attempts + 1):
        code, text, finish = _call(model, prompt, max_out, gap, temperature)
        if code == 200:
            try:
                return extract_json(text)
            except ValueError:
                # 끝난 이유를 붙인다: MAX_TOKENS면 답이 잘린 것, STOP이면 형식이 틀린 것 (응답 원문은 남기지 않는다)
                last = f"JSON 깨짐({finish or '이유 없음'})"
                _count_error(last)
                continue
        last = f"응답 {code}" if code else f"접속 실패({text})"
        _count_error(last)
        if code in (400, 401, 403):
            raise LLMError(f"{last}: 키 · 요청 확인 필요")
        if has_next and (code in (404, 429) or (code >= 500 and attempt >= 2)):
            raise _Switch(last)
        if code == 404:
            raise LLMError(f"{last}: 모델 이름 확인 필요")
        time.sleep(60 if code == 429 else 15 * attempt)
    raise LLMError(f"{attempts}회 실패({last})")
