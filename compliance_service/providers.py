"""Opt-in OCR and vision adapters.

Adapters return evidence candidates only.  They do not decide compliance and
never replace the immutable source document or its extracted blocks.
"""

from __future__ import annotations

import base64
import json
import math
import os
import re
from time import perf_counter
from uuid import uuid4
from urllib import request
from urllib.parse import urlsplit


def _transport_call(transport, url, headers, payload, timeout):
    if transport is None:
        body = payload if isinstance(payload, (bytes, bytearray)) else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = request.Request(url, data=body, headers=headers, method="POST")
        with request.urlopen(req, timeout=timeout) as response:  # noqa: S310 - explicit user configuration
            return response.read()
    if callable(transport):
        return transport(url, headers, payload, timeout)
    if hasattr(transport, "post"):
        if isinstance(payload, (bytes, bytearray)):
            return transport.post(url, headers=headers, data=payload, timeout=timeout)
        return transport.post(url, headers=headers, json=payload, timeout=timeout)
    raise TypeError("transport must be callable or provide post")


def _json_response(raw):
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="replace")
    if hasattr(raw, "read"):
        raw = raw.read()
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        return json.loads(raw)
    return json.loads(bytes(raw))


def _candidate_result(*, text="", blocks=None, provider, model=None, issues=None):
    result = {"text": text if isinstance(text, str) else "", "blocks": blocks if isinstance(blocks, list) else [],
              "provider": provider, "quality": "candidate", "issues": list(issues or [])}
    if model:
        result["model"] = model
    result["blocks"] = [_normalize_block(block) for block in result["blocks"] if isinstance(block, dict)]
    if not result["text"].strip() and not any(block["text"].strip() for block in result["blocks"]):
        result["issues"].append("no_text")
    _numeric_conflicts(result)
    return result


def _normalize_block(block):
    value = {"text": block.get("text", "") if isinstance(block.get("text", ""), str) else ""}
    if "bbox" in block:
        value["bbox"] = block["bbox"]
    if "confidence" in block:
        value["confidence"] = block["confidence"]
    return value


def _ocr_content(response):
    """Normalize explicit Paddle-style lines without inventing text or boxes."""
    if "rec_texts" not in response:
        return response.get("text", ""), response.get("blocks", []), []
    texts = response.get("rec_texts")
    if not isinstance(texts, list) or not all(isinstance(text, str) for text in texts):
        return "", [], ["invalid_ocr_texts"]
    issues = []
    scores = response.get("rec_scores")
    polygons = response.get("rec_polys", response.get("dt_polys"))
    if scores is not None and (not isinstance(scores, list) or len(scores) != len(texts)):
        issues.append("ocr_score_count_mismatch")
        scores = None
    if polygons is not None and (not isinstance(polygons, list) or len(polygons) != len(texts)):
        issues.append("ocr_polygon_count_mismatch")
        polygons = None
    blocks = []
    for index, text in enumerate(texts):
        block = {"text": text}
        if scores is not None:
            score = scores[index]
            if isinstance(score, (float, int)) and not isinstance(score, bool) and math.isfinite(score) and 0 <= score <= 1:
                block["confidence"] = score
            else:
                if "invalid_ocr_score" not in issues:
                    issues.append("invalid_ocr_score")
        if polygons is not None:
            polygon = polygons[index]
            if isinstance(polygon, list) and len(polygon) >= 4 and all(isinstance(point, list) and len(point) == 2 and all(isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value) for value in point) for point in polygon):
                block["bbox"] = polygon
            elif "invalid_ocr_polygon" not in issues:
                issues.append("invalid_ocr_polygon")
        blocks.append(block)
    return "\n".join(texts), blocks, issues


def _numeric_conflicts(result):
    # Distinct measurements on one page are normal. A conflict exists when
    # aggregate text and its block transcription disagree, or overlapping
    # observations of exactly the same box contain different numbers.
    numbers = lambda text: re.findall(r"-?\d+(?:\.\d+)?", text)
    text_numbers = numbers(result.get("text", ""))
    block_numbers = numbers("\n".join(b.get("text", "") for b in result.get("blocks", [])))
    conflict = bool(text_numbers and block_numbers and text_numbers != block_numbers)
    seen = {}
    for block in result.get("blocks", []):
        bbox = block.get("bbox")
        value = numbers(block.get("text", ""))
        if bbox is not None and value:
            key = json.dumps(bbox, sort_keys=True)
            conflict = conflict or (key in seen and seen[key] != value)
            seen[key] = value
    if conflict and "numeric_candidates_conflict" not in result["issues"]:
        result["issues"].append("numeric_candidates_conflict")
    return result


def _enabled_section(settings, name):
    section = settings.get(name, {}) if isinstance(settings, dict) else {}
    return section if isinstance(section, dict) else {}


def _headers(section):
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    env_name = str(section.get("api_key_env", "")).strip()
    secret = (os.environ.get(env_name) if env_name else "") or section.get("api_key", "")
    if secret:
        headers["Authorization"] = f"Bearer {secret}"
    return headers


def _missing_key(section):
    name = str(section.get("api_key_env", "")).strip()
    return bool(name and not os.environ.get(name) and not section.get("api_key"))


def _multipart_image(image_bytes, mime_type):
    boundary = "----compliance-" + uuid4().hex
    extension = {"image/png": "png", "image/jpeg": "jpg", "image/tiff": "tiff", "image/webp": "webp", "image/bmp": "bmp"}.get(mime_type, "bin")
    head = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"image.{extension}\"\r\n"
            f"Content-Type: {mime_type}\r\n\r\n").encode("utf-8")
    return head + bytes(image_bytes) + f"\r\n--{boundary}--\r\n".encode("ascii"), boundary


def _vision_content(response):
    choices = response.get("choices") if isinstance(response, dict) else None
    if not choices or not isinstance(choices[0], dict):
        return {}
    message = choices[0].get("message", {})
    content = message.get("content", {}) if isinstance(message, dict) else {}
    if isinstance(content, dict):
        return content
    if isinstance(content, str):
        try:
            fenced = re.fullmatch(r"\s*```(?:json)?\s*(\{.*\})\s*```\s*", content, re.DOTALL | re.IGNORECASE)
            parsed = json.loads(fenced.group(1) if fenced else content)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def recognize_image(image_bytes, mime_type, settings, transport=None, *, progress_callback=None,
                    file_id=None, page=None):
    """Return an evidence candidate from the configured provider.

    Disabled/missing/invalid configuration returns an issue result and never
    makes a network request.  ``transport`` is intended for offline tests.
    """
    if not isinstance(image_bytes, (bytes, bytearray)) or not image_bytes:
        return _candidate_result(provider="none", issues=["image_bytes_missing"])
    selected = None
    name = None
    for candidate in ("ocr", "vision"):
        section = _enabled_section(settings, candidate)
        if section.get("enabled") is True:
            selected, name = section, candidate
            break
    if selected is None:
        return _candidate_result(provider="none", issues=["provider_disabled"])
    provider = selected.get("provider", name)
    try:
        timeout = float(selected.get("timeout_seconds", 30))
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError
    except (TypeError, ValueError):
        return _candidate_result(provider=str(provider), issues=["invalid_timeout"])
    encoded = base64.b64encode(bytes(image_bytes)).decode("ascii")
    started = perf_counter()
    if progress_callback:
        progress_callback({"stage": "provider", "event": "request_started", "file_id": file_id,
                           "page": page, "provider": str(provider), "status": "running", "issues": []})
    def finish(candidate):
        if progress_callback:
            progress_callback({"stage": "provider", "event": "request_finished", "file_id": file_id,
                               "page": page, "provider": str(provider),
                               "elapsed_ms": round((perf_counter() - started) * 1000),
                               "status": "ok" if not candidate.get("issues") else "issues",
                               "issues": list(candidate.get("issues", []))})
        return candidate
    if name == "ocr":
        ocr_url = str(selected.get("url") or selected.get("base_url") or "").rstrip("/")
        if ocr_url and not str(selected.get("url") or "").strip() and not urlsplit(ocr_url).path.rstrip("/").endswith("/ocr"):
            ocr_url += "/ocr"
        if provider not in {"http-json", "multipart-http"} or not ocr_url:
            return finish(_candidate_result(provider=str(provider), issues=["invalid_ocr_configuration"]))
        if urlsplit(ocr_url).scheme not in {"http", "https"} or not urlsplit(ocr_url).netloc:
            return finish(_candidate_result(provider=str(provider), issues=["invalid_ocr_url"]))
        if _missing_key(selected):
            return finish(_candidate_result(provider=str(provider), issues=["api_key_missing"]))
        if provider == "multipart-http":
            payload, boundary = _multipart_image(image_bytes, mime_type)
            headers = _headers(selected) | {"Content-Type": f"multipart/form-data; boundary={boundary}"}
        else:
            payload = {"image_base64": encoded, "mime_type": mime_type}
            headers = _headers(selected)
        try:
            response = _json_response(_transport_call(transport, ocr_url, headers, payload, timeout))
        except json.JSONDecodeError:
            return finish(_candidate_result(provider=str(provider), issues=["invalid_json"]))
        except Exception as exc:
            return finish(_candidate_result(provider=str(provider), issues=[f"provider_error:{type(exc).__name__}"]))
        if not isinstance(response, dict):
            return finish(_candidate_result(provider=str(provider), issues=["invalid_json"]))
        text, blocks, issues = _ocr_content(response)
        return finish(_candidate_result(text=text, blocks=blocks, provider=str(provider), issues=issues))
    if provider != "openai-compatible" or not selected.get("base_url") or not selected.get("model"):
        return finish(_candidate_result(provider=str(provider), model=selected.get("model"), issues=["invalid_vision_configuration"]))
    if _missing_key(selected):
        return finish(_candidate_result(provider=str(provider), model=selected.get("model"), issues=["api_key_missing"]))
    base_url = str(selected["base_url"]).rstrip("/")
    if urlsplit(base_url).scheme not in {"http", "https"} or not urlsplit(base_url).netloc:
        return finish(_candidate_result(provider=str(provider), issues=["invalid_vision_url"]))
    if not urlsplit(base_url).path:
        base_url += "/v1"
    url = base_url if base_url.endswith("/chat/completions") else base_url + "/chat/completions"
    try:
        max_tokens = int(selected.get("max_output_tokens", 8192))
        if not 256 <= max_tokens <= 32768:
            raise ValueError
    except (ValueError, TypeError):
        return finish(_candidate_result(provider=str(provider), issues=["invalid_output_token_budget"]))
    payload = {"model": selected["model"], "messages": [{"role": "user", "content": [
        {"type": "text", "text": "Transcribe visible text only. Return valid JSON {\"text\":\"...\",\"blocks\":[{\"text\":\"...\",\"bbox\":[x1,y1,x2,y2]}]}. Do not infer missing numbers, signatures, approvals or compliance conclusions. Report illegible areas as empty text."},
        {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{encoded}"}},
    ]}], "temperature": 0, "max_tokens": max_tokens, "response_format": {"type": "json_object"}}
    try:
        response = _json_response(_transport_call(transport, url, _headers(selected), payload, timeout))
    except json.JSONDecodeError:
        return finish(_candidate_result(provider=str(provider), model=selected.get("model"), issues=["invalid_json"]))
    except Exception as exc:
        return finish(_candidate_result(provider=str(provider), model=selected.get("model"), issues=[f"provider_error:{type(exc).__name__}"]))
    content = _vision_content(response)
    choice = (response.get("choices") or [{}])[0] if isinstance(response, dict) else {}
    finish_reason = choice.get("finish_reason") if isinstance(choice, dict) else None
    diagnostic = ["output_token_limit_reached"] if finish_reason == "length" else []
    if not content:
        return finish(_candidate_result(provider=str(provider), model=selected.get("model"), issues=diagnostic + ["invalid_json"]))
    return finish(_candidate_result(text=content.get("text", ""), blocks=content.get("blocks", []), provider=str(provider), model=selected.get("model"), issues=diagnostic))

