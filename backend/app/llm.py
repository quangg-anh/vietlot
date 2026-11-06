"""Optional OpenAI-compatible advisor.

The statistical ensemble is the only source of the locked probability vector.
This module may add a *bounded* tilt: it asks an OpenAI-compatible chat model to
score numbers in ``[-1, 1]`` and the caller mixes those scores in with
``tilt <= confidence * OPENAI_MAX_TILT`` (see ``ensemble.tilt``). The advisor can
never replace, invert or dominate the distribution, its output is recorded in the
immutable ledger payload, and the blend is scored against the statistical-only
vector so its contribution is measurable afterwards.

Draws remain random. A language model has no privileged access to future results;
treat any improvement as noise unless the ledger shows otherwise. The module is
disabled unless ``OPENAI_API_KEY`` is configured, makes exactly one (optionally
two) bounded request(s), never retries on timeout, and returns ``None`` on any
failure so prediction keeps working without it.
"""

import hashlib
import json
import logging
import os
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit


log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_TIMEOUT = 45.0
DEFAULT_MAX_TILT = 0.25
MAX_RESPONSE_BYTES = 200_000
MAX_RATIONALE = 400
MAX_ADJUSTMENTS = 10

SYSTEM_PROMPT = (
    "You are a bounded numerical advisor for a lottery research tool. "
    "Lottery draws are independent and random; you cannot predict them. "
    "Your only task is to output small relative adjustments for numbers based on "
    "frequency, recency and dispersion patterns the operator already computed. "
    "Never claim an edge, never invent draw results, never output betting advice. "
    "Reply with one JSON object and nothing else."
)


def _env_float(name, default):
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        return default


def config():
    base = (os.getenv("OPENAI_BASE_URL") or DEFAULT_BASE_URL).strip().rstrip("/")
    key = (os.getenv("OPENAI_API_KEY") or "").strip()
    enabled = (os.getenv("OPENAI_ENABLED") or "auto").strip().lower()
    forced_off = enabled in ("0", "false", "off", "no")
    return {"base_url": base, "api_key": key,
            "model": (os.getenv("OPENAI_MODEL") or DEFAULT_MODEL).strip(),
            "timeout": max(1.0, min(180.0, _env_float("OPENAI_TIMEOUT", DEFAULT_TIMEOUT))),
            "max_tilt": max(0.0, min(1.0, _env_float("OPENAI_MAX_TILT", DEFAULT_MAX_TILT))),
            "temperature": _env_float("OPENAI_TEMPERATURE", 0.0),
            "json_mode": (os.getenv("OPENAI_JSON_MODE") or "1").strip() != "0",
            "enabled": not forced_off and bool(key),
            "forced_off": forced_off,
            "flag": enabled}


def disabled_reason(settings):
    """Why the advisor is off, so the dashboard never points at the wrong cause."""
    if settings["forced_off"]:
        return ("disabled_by_flag",
                f"OPENAI_ENABLED={settings['flag']} đang tắt advisor. Xóa dòng đó "
                "(hoặc đặt OPENAI_ENABLED=auto) rồi khởi động lại tiến trình.")
    if not settings["api_key"]:
        return ("missing_key",
                "Chưa có OPENAI_API_KEY trong môi trường tiến trình. Đặt trong .env "
                "rồi khởi động lại backend (đổi .env không tự nạp lại).")
    return (None, None)


def check(timeout=None, transport=None):
    """Minimal round-trip to the configured endpoint: proves reachability + auth.

    Sends one short prompt instead of a full advisory payload, so it answers
    "why is the advisor not working" in seconds rather than half a minute.
    """
    settings = config()
    settings["timeout"] = timeout or settings["timeout"]
    result = {"enabled": settings["enabled"], "configured": bool(settings["api_key"]),
              "base_url": settings["base_url"], "model": settings["model"],
              "flag": settings["flag"], "disabled_reason": None,
              "reachable": False, "latency_ms": None, "error": None}
    reason, message = disabled_reason(settings)
    result["disabled_reason"] = reason
    if not settings["enabled"]:
        result["error"] = message
        return result
    request = {"model": settings["model"],
               "messages": [{"role": "user", "content": "Reply with the single word: ok"}],
               "temperature": 0, "max_tokens": 16}
    started = time.monotonic()
    try:
        url = _endpoint(settings)
        body = (transport or _post)(url, settings["api_key"], request, settings["timeout"])
        # A connectivity probe must not require JSON: plain "ok" is a valid reply.
        content = ""
        choices = body.get("choices") if isinstance(body, dict) else None
        if isinstance(choices, list) and choices:
            raw_content = (choices[0].get("message") or {}).get("content")
            if isinstance(raw_content, list):
                raw_content = "".join(part.get("text", "") for part in raw_content if isinstance(part, dict))
            content = (raw_content or "").strip()
        result.update(reachable=True, latency_ms=int((time.monotonic() - started) * 1000),
                      model_echoed=body.get("model") if isinstance(body, dict) else None,
                      usage=body.get("usage") or {} if isinstance(body, dict) else {},
                      reply=content[:120])
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        result["error"] = f"HTTP {exc.code} {exc.reason} {detail}".strip()
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError) as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result


def status():
    """Dashboard-safe description: no key, no secrets."""
    settings = config()
    parts = urlsplit(settings["base_url"])
    reason, message = disabled_reason(settings)
    return {"enabled": settings["enabled"], "configured": bool(settings["api_key"]),
            "provider": parts.hostname, "model": settings["model"],
            "max_tilt": settings["max_tilt"], "timeout": settings["timeout"],
            "json_mode": settings["json_mode"], "flag": settings["flag"],
            "disabled_reason": reason, "disabled_message": message,
            "effect": "bounded_tilt_on_ensemble_probabilities",
            "note": "CHI la dieu chinh nho, co gioi han; khong thay the model thong ke va khong dam bao trung thuong."}


def _endpoint(settings):
    parts = urlsplit(settings["base_url"])
    if parts.scheme not in ("https", "http") or not parts.hostname:
        raise ValueError("OPENAI_BASE_URL must be an http(s) URL")
    if parts.scheme == "http" and parts.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("Plain HTTP is only allowed for loopback OpenAI-compatible servers")
    return settings["base_url"] + "/chat/completions"


def _brief(rows, limit=12):
    return [{"draw_id": row["draw_id"], "date": row["draw_date"], "slot": row["draw_slot"],
             "main": list(row["main"]), "special": row.get("special")} for row in rows[-limit:]]


def _prompt(code, spec, rows, probabilities, frequency):
    size, count = spec["number_max"], spec["pick_count"]
    order = sorted(range(1, size + 1), key=lambda number: (-probabilities[number - 1], number))
    payload = {
        "task": "Return bounded per-number adjustment scores for the next draw.",
        "game": {"code": code, "main_numbers": size, "pick_count": count},
        "recent_draws": _brief(rows),
        "model_probabilities": [{"number": number, "inclusion": round(probabilities[number - 1], 6)}
                                for number in order],
        "frequency": frequency,
        "output_schema": {
            "confidence": "number in [0,1]; your overall confidence in the adjustments",
            "boost": [{"number": "int in range", "score": "number in [0,1]"}],
            "suppress": [{"number": "int in range", "score": "number in [-1,0]"}],
            "rationale": "<= 300 characters, no win claims",
        },
        "limits": {"max_boost": MAX_ADJUSTMENTS, "max_suppress": MAX_ADJUSTMENTS,
                   "scores_are_relative_adjustments_not_win_probabilities": True},
    }
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=True, sort_keys=True)},
    ]


def _extract(body):
    """Pull the assistant JSON out of an OpenAI-compatible chat completion."""
    if not isinstance(body, dict):
        raise ValueError("Advisor response is not an object")
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("Advisor response has no choices")
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, list):  # some servers return content parts
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    if not isinstance(content, str) or not content.strip():
        raise ValueError("Advisor returned empty content")
    text = content.strip()
    if text.startswith("```"):
        text = text.split("```")[1] if text.count("```") >= 2 else text.strip("`")
        text = text[4:] if text.lower().startswith("json") else text
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("Advisor content is not JSON")
    return json.loads(text[start:end + 1]), body.get("usage") or {}


def _score_list(entries, spec, positive):
    scores = {}
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        try:
            number, score = int(entry.get("number")), float(entry.get("score"))
        except (TypeError, ValueError):
            continue
        if not 1 <= number <= spec["number_max"] or number in scores:
            continue
        score = max(-1.0, min(1.0, score))
        if positive and score <= 0:
            continue
        if not positive and score >= 0:
            continue
        scores[number] = score
        if len(scores) >= MAX_ADJUSTMENTS:
            break
    return scores


def parse(answer, spec):
    """Validate the advisor answer into a bounded score vector."""
    confidence = answer.get("confidence", 0.0)
    try:
        confidence = max(0.0, min(1.0, float(confidence)))
    except (TypeError, ValueError):
        confidence = 0.0
    boosts = _score_list(answer.get("boost"), spec, positive=True)
    suppresses = _score_list(answer.get("suppress"), spec, positive=False)
    merged = {**boosts, **suppresses}
    if confidence <= 0 or not merged:
        return None
    rationale = answer.get("rationale")
    rationale = rationale.strip()[:MAX_RATIONALE] if isinstance(rationale, str) else ""
    return {"confidence": confidence,
            "scores": [merged.get(number, 0.0) for number in range(1, spec["number_max"] + 1)],
            "boost": [{"number": number, "score": score} for number, score in sorted(boosts.items())],
            "suppress": [{"number": number, "score": score} for number, score in sorted(suppresses.items())],
            "rationale": rationale}


def _post(url, api_key, body, timeout):
    request = urllib.request.Request(
        url, data=json.dumps(body, ensure_ascii=True).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}",
                 "Accept": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError("Advisor response exceeds size limit")
    return json.loads(raw.decode("utf-8", "replace"))


def advise(code, spec, rows, probabilities, frequency, transport=None):
    """Ask the advisor for bounded scores. Returns ``None`` when unavailable."""
    settings = config()
    if not settings["enabled"] or spec["kind"] != "pick_k" or not rows:
        return None
    messages = _prompt(code, spec, rows, probabilities, frequency)
    request = {"model": settings["model"], "messages": messages,
               "temperature": settings["temperature"], "max_tokens": 700}
    if settings["json_mode"]:
        request["response_format"] = {"type": "json_object"}
    started = time.monotonic()
    try:
        url = _endpoint(settings)
        send = transport or _post
        try:
            body = send(url, settings["api_key"], request, settings["timeout"])
        except urllib.error.HTTPError as exc:
            if exc.code != 400 or "response_format" not in request:
                raise
            request.pop("response_format")
            body = send(url, settings["api_key"], request, settings["timeout"])
        answer, usage = _extract(body)
        parsed = parse(answer, spec)
        if not parsed:
            log.info("Advisor returned no usable adjustment for %s", code)
            return None
        parsed.update(model=settings["model"], provider=urlsplit(settings["base_url"]).hostname,
                      max_tilt=settings["max_tilt"], latency_ms=int((time.monotonic() - started) * 1000),
                      request_sha256=hashlib.sha256(json.dumps(request, sort_keys=True, ensure_ascii=True).encode()).hexdigest(),
                      usage={key: usage[key] for key in ("prompt_tokens", "completion_tokens", "total_tokens")
                             if isinstance(usage, dict) and key in usage})
        log.info("Advisor answered game=%s model=%s confidence=%.2f boosts=%s suppresses=%s latency_ms=%s",
                 code, settings["model"], parsed["confidence"], len(parsed["boost"]), len(parsed["suppress"]),
                 parsed["latency_ms"])
        return parsed
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError, KeyError) as exc:
        log.warning("Advisor unavailable for %s: %s", code, exc)
        return None