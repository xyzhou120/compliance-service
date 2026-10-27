"""Image/page recognition with preserved independent candidates and quality gates."""

from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from importlib.metadata import version
import json
from pathlib import Path
import re
import shutil
import subprocess
import unicodedata
from tempfile import TemporaryDirectory
from tempfile import NamedTemporaryFile
import os
from time import perf_counter
from threading import Lock

from .parsers import parse_document
from .providers import recognize_image
from .contracts import RunPaused, StaleLease, canonical, digest

RECOGNITION_CACHE_VERSION = "recognition-v4-page-evidence-compact"
PROVIDER_CONTRACT_VERSION = "provider-candidate-contract-v2"


def public_model_settings(settings):
    return {name: {key: section.get(key) for key in
                   ("enabled", "provider", "url", "base_url", "model", "pdf_renderer", "pdf_render_dpi",
                    "max_pages", "recognition_concurrency",
                    "timeout_seconds", "max_tokens", "max_output_tokens", "prompt", "quality_gate") if key in section}
            for name, section in settings.items() if name in {"ocr", "vision"} and isinstance(section, dict)}


def _save_cache(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=path.parent, suffix=".part", delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(canonical({"payload": payload, "digest": digest(payload)}).encode("utf-8"))
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_cache(path):
    cached = json.loads(path.read_text(encoding="utf-8"))
    if cached.get("digest") != digest(cached.get("payload")):
        raise ValueError("recognition cache checksum mismatch")
    return cached["payload"]


def _normalized(text):
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", text))


def _numbers(text):
    return re.findall(r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?", unicodedata.normalize("NFKC", text))


def _comparison_text(text):
    # Preserve decimal points, signs and unit case. Only layout whitespace and
    # ordinary line punctuation may vary between independent observations.
    return re.sub(r"[\s，,。:：;；]+", "", unicodedata.normalize("NFKC", text))


def _segments(candidate):
    blocks = candidate.get("blocks") or [{"text": line} for line in candidate.get("text", "").splitlines()]
    return [(index, block) for index, block in enumerate(blocks) if block.get("text", "").strip()]


def recognize_checked(image_bytes, mime_type, settings, *, transport=None, progress_callback=None,
                      file_id=None, page=None, cache_dir=None, image_hash=None):
    candidates = []
    image_hash = image_hash or sha256(image_bytes).hexdigest()
    for kind in ("ocr", "vision"):
        if not settings.get(kind, {}).get("enabled"):
            continue
        selected = deepcopy(settings)
        for other in ("ocr", "vision"):
            selected[other]["enabled"] = other == kind
        cache_path = None
        candidate = None
        if cache_dir is not None:
            cache_key = digest({"image_hash": image_hash, "mime_type": mime_type, "provider": kind,
                                "settings": public_model_settings({kind: selected[kind]}),
                                "recognition_version": RECOGNITION_CACHE_VERSION,
                                "provider_contract_version": PROVIDER_CONTRACT_VERSION})
            cache_path = Path(cache_dir) / f"{cache_key}.json"
            try:
                cached = _read_cache(cache_path)
                if (cached.get("cache_key") == cache_key and isinstance(cached.get("candidate"), dict)
                        and not cached["candidate"].get("issues")
                        and isinstance(cached["candidate"].get("text"), str)
                        and isinstance(cached["candidate"].get("blocks"), list)):
                    candidate = cached["candidate"]
            except (OSError, ValueError, KeyError, TypeError):
                candidate = None
        started = perf_counter()
        if candidate is None:
            candidate = recognize_image(image_bytes, mime_type, selected, transport=transport,
                                        progress_callback=progress_callback, file_id=file_id, page=page)
            # Temporary provider failures are deliberately not cached. A later
            # resumed run can retry only this provider while retaining others.
            if cache_path is not None and not candidate.get("issues"):
                payload = {"cache_key": cache_key, "candidate": candidate}
                _save_cache(cache_path, payload)
        candidates.append(candidate)
        if progress_callback:
            progress_callback({"stage": "recognition", "event": "provider_finished", "file_id": file_id,
                               "page": page, "provider": kind,
                               "elapsed_ms": round((perf_counter() - started) * 1000),
                               "status": "ok" if not candidate.get("issues") else "issues",
                               "issues": list(candidate.get("issues", []))})
    checked_blocks = []
    matched_right = set()
    left, right = (_segments(candidates[0]), _segments(candidates[1])) if len(candidates) == 2 else ([], [])
    if len(candidates) == 2 and not any(candidate["issues"] for candidate in candidates):
        for left_index, block in left:
            matches = [(index, other) for index, other in right if index not in matched_right
                       and _comparison_text(block["text"]) == _comparison_text(other["text"])
                       and _numbers(block["text"]) == _numbers(other["text"])]
            if matches:
                right_index, other = matches[0]
                matched_right.add(right_index)
                checked_blocks.append({"text": block["text"], "source_refs": [
                    {"candidate": 0, "block": left_index, "bbox": block.get("bbox")},
                    {"candidate": 1, "block": right_index, "bbox": other.get("bbox")}],
                    "quality": "independently_compared_candidate"})
    readable = bool(left and right and len(checked_blocks) == len(left) == len(right))
    issues = list(dict.fromkeys(issue for c in candidates for issue in c["issues"]))
    if not readable:
        issues.append("independent_recognition_not_confirmed")
    return {"text": "\n".join(block["text"] for block in checked_blocks), "readable": readable,
            "checked_blocks": checked_blocks,
            "candidates": candidates, "issues": issues,
            "quality_policy": "candidate_sources_preserved; agreement_is_not_proof; fact_and_rule_validation_required"}


def _render_page(path, page, settings):
    try:
        return _render_page_with_external(path, page, settings)
    except RuntimeError as exc:
        # A broken/missing poppler must not block scanning: fall back to the
        # in-process renderer when the dependency is available.
        rendered = _render_page_with_pymupdf(path, page, settings)
        if rendered is not None:
            return rendered
        raise exc


def _render_page_with_pymupdf(path, page, settings):
    try:
        import pymupdf
    except ImportError:
        return None
    dpi = settings.get("ocr", {}).get("pdf_render_dpi", 150)
    if type(dpi) is not int or not 72 <= dpi <= 300:
        dpi = 150
    try:
        with pymupdf.open(str(path)) as document:
            if page < 1 or page > document.page_count:
                raise RuntimeError("pdf_page_render_failed")
            pixmap = document[page - 1].get_pixmap(matrix=pymupdf.Matrix(dpi / 72.0, dpi / 72.0))
            data = pixmap.tobytes("png")
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError("pdf_page_render_failed") from exc
    if not data or len(data) > 30 * 1024 * 1024:
        raise RuntimeError("rendered_image_exceeds_limit")
    return data


def _render_page_with_external(path, page, settings):
    renderer = settings.get("ocr", {}).get("pdf_renderer") or shutil.which("pdftoppm")
    if not renderer:
        raise RuntimeError("pdf_renderer_not_configured")
    with TemporaryDirectory(prefix="compliance-page-") as directory:
        target = Path(directory) / "page"
        dpi = settings.get("ocr", {}).get("pdf_render_dpi", 150)
        if type(dpi) is not int or not 72 <= dpi <= 300:
            dpi = 150
        completed = subprocess.run([str(renderer), "-f", str(page), "-l", str(page), "-r", str(dpi), "-singlefile", "-png", str(path), str(target)],
                                   capture_output=True, timeout=45, check=False)
        output = target.with_suffix(".png")
        if completed.returncode or not output.is_file():
            raise RuntimeError("pdf_page_render_failed")
        if output.stat().st_size > 30 * 1024 * 1024:
            raise RuntimeError("rendered_image_exceeds_limit")
        return output.read_bytes()


def _pdf_image_candidates(path, page_number, reader=None):
    """Return painted image XObjects for one page, deduplicated by bytes.

    Image resources are candidates only when the page content actually issues
    a ``Do`` operation for that resource. This avoids OCR on unused shared
    resources and avoids sending a whole mixed page when native text exists.
    """
    from pypdf import PdfReader
    reader = reader or PdfReader(path, strict=True)
    page = reader.pages[page_number - 1]
    resources = page.get("/Resources", {})
    resources = resources.get_object() if hasattr(resources, "get_object") else resources
    xobjects = resources.get("/XObject", {})
    xobjects = xobjects.get_object() if hasattr(xobjects, "get_object") else xobjects
    raw = page.get_contents().get_data() if page.get_contents() is not None else b""
    import re
    names = []
    for name in re.findall(rb"/(\w+)\s+Do", raw):
        key = "/" + name.decode("latin1")
        if key not in names:
            names.append(key)
    result, seen = [], set()
    for name in names:
        ref = xobjects.get(name)
        if ref is None:
            continue
        obj = ref.get_object()
        if obj.get("/Subtype") != "/Image":
            continue
        extracted = page.images[name]
        image = extracted.data
        digest = sha256(image).hexdigest()
        if digest in seen:
            continue
        seen.add(digest)
        suffix = Path(extracted.name).suffix.lower()
        mime = "image/jpeg" if suffix in {".jpg", ".jpeg"} else "image/png"
        result.append((image, mime, {"page": page_number, "image": name}))
    return result


def parse_with_models(path, *, file_id, settings, transport=None, progress_callback=None, cache_dir=None):
    path = Path(path)
    native_path = None
    result = None
    if cache_dir is not None:
        native_key = digest({"content": sha256(path.read_bytes()).hexdigest(), "suffix": path.suffix.lower(),
                             "parser": sha256(Path(__file__).with_name("parsers.py").read_bytes()).hexdigest(),
                             "dependencies": {name: version(name) for name in ("pypdf", "python-docx", "openpyxl")}})
        native_path = Path(cache_dir) / "native" / f"{native_key}.json"
        try:
            cached = _read_cache(native_path)
            if isinstance(cached.get("blocks"), list) and isinstance(cached.get("coverage"), list):
                result = deepcopy(cached)
                result["file_id"] = file_id
        except (OSError, ValueError, TypeError, KeyError):
            pass
    if result is None:
        result = parse_document(path, file_id=file_id, progress_callback=progress_callback)
        if native_path is not None and not any(unit.get("state") == "failed" for unit in result["coverage"]):
            _save_cache(native_path, result)
    if not any(settings.get(name, {}).get("enabled") for name in ("ocr", "vision")):
        return result
    path = Path(path)
    image_formats = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "bmp": "image/bmp",
                     "webp": "image/webp", "tif": "image/tiff", "tiff": "image/tiff"}
    eligible = [u for u in result["coverage"] if u["state"] in {"unreadable", "partial"}
                and (u.get("reason") in {"image_requires_ocr", "page_image_requires_ocr",
                                          "page_contains_image_requires_ocr", "nonempty_page_without_extractable_text"})]
    # Recognising only the first 20 pages silently capped real reports; the
    # budget is now explicit, configurable, and large enough for scanned dailies.
    requested_budget = settings.get("ocr", {}).get("max_pages", 200)
    maximum = requested_budget if type(requested_budget) is int and 1 <= requested_budget <= 2000 else 200
    result["recognition_budget"] = {"eligible_pages": len(eligible), "attempted_pages": 0,
                                    "skipped_pages": 0, "max_pages": maximum}
    recognition_cache = {}
    cache_lock = Lock()
    pdf_reader = None
    if result["format"] == "pdf":
        from pypdf import PdfReader
        pdf_reader = PdfReader(path, strict=True)
    concurrency = settings.get("ocr", {}).get("recognition_concurrency", 1)
    concurrency = concurrency if type(concurrency) is int and 1 <= concurrency <= 2 else 1

    def recognize_unit(unit):
        started = perf_counter()
        page_number = int(unit["unit_id"].split(":")[1]) if unit["unit_id"].startswith("page:") else None
        if progress_callback:
            progress_callback({"stage": "recognition", "event": "page_started", "file_id": file_id,
                               "page": page_number, "page_total": len(eligible), "status": "running", "issues": []})
        fallback_issue = None
        if result["format"] in image_formats:
            images = [(path.read_bytes(), image_formats[result["format"]], {"image": 1})]
        elif result["format"] == "pdf" and unit["unit_id"].startswith("page:"):
            page = int(unit["unit_id"].split(":")[1])
            try:
                images = _pdf_image_candidates(path, page, pdf_reader if concurrency == 1 else None)
            except Exception as exc:
                if type(exc).__module__ == "PIL.Image" and type(exc).__name__ == "DecompressionBombError":
                    images = []
                    fallback_issue = "embedded_image_limit"
                elif isinstance(exc, NotImplementedError):
                    images = []
                    fallback_issue = "embedded_image_extraction_unsupported"
                else:
                    raise
            if not images:
                images = [(_render_page(path, page, settings), "image/png",
                           {"page": page, "source": "page_render_fallback"} if fallback_issue else {"page": page})]
        else:
            return None
        all_compared, page_issues, observations, blocks = True, [], [], []
        if fallback_issue:
            page_issues.append(fallback_issue)
        for image_bytes, mime_type, locator in images:
            image_hash = sha256(image_bytes).hexdigest()
            with cache_lock:
                checked = recognition_cache.get(image_hash)
            reused = checked is not None
            if not reused:
                checked = recognize_checked(image_bytes, mime_type, settings, transport=transport,
                                            progress_callback=progress_callback, file_id=file_id,
                                            page=locator.get("page"), cache_dir=cache_dir,
                                            image_hash=image_hash)
                with cache_lock:
                    recognition_cache[image_hash] = checked
            all_compared = all_compared and checked["readable"]
            page_issues.extend(checked["issues"])
            metadata = {"recognition_candidates": checked["candidates"], "recognition_quality_policy": checked["quality_policy"],
                        "image_hash": image_hash, "recognition_issues": checked["issues"],
                        "image_locator": locator, "recognition_cache_reused": reused}
            observations.append(metadata)
            for segment_index, block in enumerate(checked["checked_blocks"]):
                blocks.append((locator, metadata, segment_index, block))
        return {"page": page_number, "elapsed_ms": round((perf_counter() - started) * 1000),
                "all_compared": all_compared, "issues": list(dict.fromkeys(page_issues)),
                "observations": observations, "blocks": blocks}

    def apply_result(unit, completed):
        if completed is None:
            return
        for locator, metadata, segment_index, block in completed["blocks"]:
            recognized_locator = dict(locator, recognized_block=segment_index + 1)
            result["blocks"].append({"block_id": sha256((result["content_hash"] + json.dumps(recognized_locator, sort_keys=True) + "recognized-v2").encode()).hexdigest(),
                                     "text": block["text"], "kind": "recognized_page", "locator": locator,
                                     "metadata": {"recognition_quality": block["quality"],
                                                  "recognition_source_refs": block["source_refs"],
                                                  "image_hash": metadata["image_hash"],
                                                  "image_locator": metadata["image_locator"],
                                                  "recognized_block": segment_index + 1}})
        unit["recognition"] = {"images": completed["observations"], "issues": completed["issues"],
                               "status": "compared" if completed["all_compared"] else "incomplete"}
        if completed["all_compared"] and unit["state"] == "unreadable":
            # Two independent providers agreeing is not proof of the source text.
            # The page therefore stays unreadable for coverage purposes and only
            # carries a review marker: a business verdict may never rest on model
            # transcription alone (INV-2 in docs/document-pipeline-spec-v0.md).
            unit["model_transcription"] = {
                "status": "independent_providers_agreed",
                "requires_review": True,
                "reason": "independent_blocks_compared_not_source_truth",
            }
        result["issues"].extend(unit["unit_id"] + ": " + issue for issue in completed["issues"])
        if progress_callback:
            progress_callback({"stage": "recognition", "event": "page_finished", "file_id": file_id,
                               "page": completed["page"], "page_total": len(eligible),
                               "elapsed_ms": completed["elapsed_ms"], "status": "candidate", "issues": completed["issues"]})

    def apply_failure(unit, exc):
        if isinstance(exc, (StaleLease, RunPaused)):
            raise exc
        pillow_limit = type(exc).__module__ == "PIL.Image" and type(exc).__name__ == "DecompressionBombError"
        if not pillow_limit and not isinstance(exc, (OSError, ValueError, RuntimeError, subprocess.SubprocessError)):
            raise exc
        issue = "recognition_failed:" + type(exc).__name__
        result["issues"].append(unit["unit_id"] + ": " + issue)
        unit["recognition"] = {"status": "failed", "issues": [issue]}
        if progress_callback:
            progress_callback({"stage": "recognition", "event": "page_finished", "file_id": file_id,
                               "page": int(unit["unit_id"].split(":")[1]) if unit["unit_id"].startswith("page:") else None,
                               "page_total": len(eligible), "status": "failed", "issues": [issue]})

    for number, unit in enumerate(eligible):
        if number >= maximum:
            result["issues"].append(unit["unit_id"] + ": recognition_budget_exhausted")
            unit["recognition"] = {"status": "budget_exhausted", "issues": ["recognition_budget_exhausted"],
                                   "max_pages": maximum}
            result["recognition_budget"]["skipped_pages"] += 1
            if progress_callback:
                progress_callback({"stage": "recognition", "event": "page_finished", "file_id": file_id,
                                   "page": int(unit["unit_id"].split(":")[1]) if unit["unit_id"].startswith("page:") else None,
                                   "page_total": len(eligible), "status": "skipped",
                                   "issues": ["recognition_budget_exhausted"]})
    attempted = eligible[:maximum]
    result["recognition_budget"]["attempted_pages"] = len(attempted)
    if concurrency == 1:
        for unit in attempted:
            try:
                apply_result(unit, recognize_unit(unit))
            except Exception as exc:
                apply_failure(unit, exc)
    else:
        with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="page-recognition") as pool:
            futures = [pool.submit(recognize_unit, unit) for unit in attempted]
            for unit, future in zip(attempted, futures):
                try:
                    apply_result(unit, future.result())
                except Exception as exc:
                    # A failed page must remain a page-level gap. Do not cancel
                    # sibling pages: cancellation would surface as a task-level
                    # CancelledError and discard otherwise valid pages.
                    apply_failure(unit, exc)
    return result
