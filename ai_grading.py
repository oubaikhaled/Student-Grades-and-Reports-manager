import base64
import io
import json
import logging
import os
import re
import time
import requests
import pymupdf
from PIL import Image

logger = logging.getLogger("ai_grading")

DEFAULT_GAS_URL = "https://script.google.com/macros/s/AKfycbyLOJdL4kViRmyqtjETxu6qjjVi98MaqymDvemG8mA6PPGGuhf9WR0dqDrh8sVa5VBTug/exec"

# Threshold of answered questions with has_working == False to trigger the "misses steps" note
MISSING_STEPS_THRESHOLD = 0.50

_ARABIC_DIGITS_MAP = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
_ROTATION_IDX = 0


def parse_api_keys(raw_input) -> list[str]:
    """Parse a string or list of API keys into a clean unique list."""
    if not raw_input:
        return []
    keys = []
    if isinstance(raw_input, (list, tuple, set)):
        for item in raw_input:
            keys.extend(parse_api_keys(item))
    elif isinstance(raw_input, str):
        for part in re.split(r"[,;\s\n\r]+", raw_input.strip()):
            clean_k = part.strip().strip('"').strip("'")
            if clean_k and len(clean_k) > 8 and clean_k not in keys:
                keys.append(clean_k)
    return keys


def get_provider_keys(st_secrets=None) -> dict[str, list[str]]:
    """
    Retrieves API keys for Groq, Mistral, and Gemini from environment variables or st.secrets.
    """
    providers = {"groq": [], "mistral": [], "gemini": []}

    def _collect(provider_name: str, env_names: list[str]):
        for ename in env_names:
            for k in parse_api_keys(os.environ.get(ename)):
                if k not in providers[provider_name]:
                    providers[provider_name].append(k)
        if st_secrets is not None:
            try:
                for ename in env_names:
                    if ename in st_secrets:
                        for k in parse_api_keys(st_secrets[ename]):
                            if k not in providers[provider_name]:
                                providers[provider_name].append(k)
            except Exception:
                pass

    _collect("groq", ["GROQ_API_KEYS", "GROQ_API_KEY", "GROQ_KEY"])
    _collect("mistral", ["MISTRAL_API_KEYS", "MISTRAL_API_KEY", "MISTRAL_KEY"])
    _collect("gemini", ["GEMINI_API_KEYS", "GEMINI_API_KEY"])
    return providers


def get_gemini_api_key(st_secrets=None) -> str | None:
    """
    Backwards-compatible helper called by app.py to check if AI grading is configured.
    Returns all configured keys (Groq, Mistral, or Gemini) so app.py knows AI is ready.
    """
    p = get_provider_keys(st_secrets)
    all_k = p["groq"] + p["mistral"] + p["gemini"]
    return ",".join(all_k) if all_k else None


def list_available_gemini_models(api_key=None) -> list[str]:
    """Backwards-compatible stub for app.py."""
    return ["mistral-ocr + groq-llama-3.3-70b", "groq-llama-4-scout-vision"]
    
    
def fetch_pdf_from_drive(
    filename: str,
    script_url: str = DEFAULT_GAS_URL,
    subfolder: str = ""
) -> bytes:
    """Fetch PDF file from Google Drive (searching the homework's subfolder first)."""
    payload = {
        "action": "get",
        "filename": filename,
        "subfolder": subfolder or ""
    }
    try:
        response = requests.post(script_url, json=payload, timeout=60)
        if response.status_code != 200:
            raise RuntimeError(f"Google Drive script responded with HTTP {response.status_code}")

        data = response.json()
        if data.get("status") == "success" and "base64" in data:
            return base64.b64decode(data["base64"])
        elif data.get("status") == "not_found":
            raise FileNotFoundError(f"File '{filename}' not found in Google Drive folder.")
        else:
            err_msg = data.get("message", "Unknown error from Drive script.")
            raise RuntimeError(f"Drive retrieval error: {err_msg}")
    except requests.RequestException as e:
        raise RuntimeError(f"Network error connecting to Google Apps Script: {e}")

def split_pdf_into_page_images(
    pdf_bytes: bytes,
    dpi: int = 140,
    on_page_progress=None
) -> list[dict]:
    """Renders PDF pages to clean JPEGs for UI preview & vision fallback."""
    pages = []
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as doc:
        total = len(doc)
        for page_index in range(total):
            if on_page_progress:
                on_page_progress(page_index + 1, total)

            page = doc[page_index]
            rect = page.rect
            max_dim = max(rect.width, rect.height)
            eff_dpi = min(dpi, int(1800 * 72 / max_dim)) if max_dim > 0 else dpi
            eff_dpi = max(100, eff_dpi)

            pix = page.get_pixmap(dpi=eff_dpi, colorspace=pymupdf.csRGB, alpha=False)
            im = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
            if im.width > 1500:
                new_h = int(im.height * 1500 / im.width)
                im = im.resize((1500, new_h), Image.Resampling.LANCZOS)

            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=85, optimize=True)

            pages.append({
                "page_num": page_index + 1,
                "image_bytes": buf.getvalue(),
                "mime_type": "image/jpeg",
                "width": im.width,
                "height": im.height
            })
    return pages


def _extract_json_dict(raw_text: str) -> dict | None:
    """Safely parse JSON from LLM response."""
    if not raw_text:
        return None
    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                return None
    return None


def _normalize_q_id(q_raw: str) -> str:
    """Normalizes question labels like 'Question 1', 'س١', 'q1', '1)', '1' -> 'Q1'."""
    s = str(q_raw or "").translate(_ARABIC_DIGITS_MAP).strip()
    m = re.search(r"(\d+)", s)
    if m:
        return f"Q{int(m.group(1))}"
    return s.upper()


def _q_sort_key(q_label: str):
    m = re.search(r"(\d+)", q_label)
    return (0, int(m.group(1)), q_label) if m else (1, 0, q_label)


def _sanitize_hints_no_math(hint_text: str) -> str:
    """Ensures student_hints contains zero mathematical equations or numbers."""
    if not hint_text:
        return ""
    cleaned = re.sub(r"[=+\-*/^<>≤≥≈≠√∫∑∏]+", " ", hint_text)
    cleaned = re.sub(r"\b\d+(?:\.\d+)?\b", "", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    return cleaned


# ---------------------------------------------------------------------------
# STAGE 1: MISTRAL NATIVE PDF MATH OCR (1 Single Call for Whole PDF!)
# ---------------------------------------------------------------------------
def extract_math_with_mistral_ocr(pdf_bytes: bytes, mistral_keys: list[str]) -> list[str] | None:
    """
    Sends the entire PDF in a single base64 data URI to Mistral's dedicated OCR API
    ([https://api.mistral.ai/v1/ocr](https://api.mistral.ai/v1/ocr)) using model 'mistral-ocr-latest'.
    Returns a list of markdown strings (one per page), or None if unavailable.
    """
    global _ROTATION_IDX
    if not mistral_keys:
        return None

    b64_pdf = base64.b64encode(pdf_bytes).decode("utf-8")
    payload = {
        "model": "mistral-ocr-latest",
        "document": {
            "type": "document_url",
            "document_url": f"data:application/pdf;base64,{b64_pdf}"
        },
        "include_image_base64": False
    }

    for offset in range(len(mistral_keys)):
        k = mistral_keys[(_ROTATION_IDX + offset) % len(mistral_keys)]
        try:
            resp = requests.post(
                "[https://api.mistral.ai/v1/ocr](https://api.mistral.ai/v1/ocr)",
                headers={
                    "Authorization": f"Bearer {k}",
                    "Content-Type": "application/json"
                },
                json=payload,
                timeout=60
            )
            if resp.status_code == 200:
                data = resp.json()
                pages_out = data.get("pages", [])
                if pages_out:
                    return [p.get("markdown", "") for p in pages_out]
            else:
                logger.warning(f"Mistral OCR HTTP {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            logger.warning(f"Mistral OCR error: {e}")
            continue
    return None


# ---------------------------------------------------------------------------
# STAGE 2: GROQ / MISTRAL FAST GRADING
# ---------------------------------------------------------------------------
def grade_ocr_text_with_groq_or_mistral(
    ocr_pages: list[str],
    assignment_title: str,
    rubric: str,
    groq_keys: list[str],
    mistral_keys: list[str]
) -> dict | None:
    """
    Grades the extracted Markdown/LaTeX transcript of the entire homework in a single fast call
    using Groq (llama-3.3-70b-versatile) or Mistral (mistral-large-latest).
    """
    global _ROTATION_IDX
    combined_transcript = []
    for idx, md in enumerate(ocr_pages):
        combined_transcript.append(f"=== PAGE {idx + 1} ===\n{md.strip() or '[Blank Page]'}")
    full_transcript_str = "\n\n".join(combined_transcript)

    has_rubric = bool(rubric and rubric.strip())
    rubric_block = (
        f"TEACHER RUBRIC / ANSWER KEY:\n{rubric.strip()}"
        if has_rubric
        else "No rubric provided — verify each question mathematically."
    )

    system_prompt = (
        "You are an expert math homework grading auditor. You receive an OCR transcript of a student's "
        "handwritten homework and compare every question against the Teacher's Rubric / Answer Key. "
        "Always respond with valid JSON only."
    )

    user_prompt = f"""Homework Title: "{assignment_title}"
Total Pages: {len(ocr_pages)}

{rubric_block}

STUDENT SUBMISSION TRANSCRIPT (FROM OCR):
{full_transcript_str}

GRADING RULES:
1. Examine every question that appears in the transcript or rubric.
2. STATUS (`status`):
   - `"answered"`: The student wrote a final answer or MCQ choice letter. Set `matches_key: true` if their final answer (or boxed/final step) is mathematically equivalent to the rubric answer, else `false`.
   - `"blank"`: The question number is written with no answer, `"?"`, `"??"`, or skipped (`matches_key: false`).
   - `"unreadable"`: Use ONLY if the OCR transcript for that question's final answer is completely corrupted/garbled and cannot be judged (`matches_key: false`).
3. CROSSED-OUT WORK & EQUIVALENT NOTATION:
   - If multiple steps are shown, judge the final answer written at the end of the question.
   - Reversed brackets `]3, ∞[` equal `(3, ∞)`, and `]a, b[` equals `(a, b)`.
   - For MCQs, accept the choice letter (`B`), the value (`-1`), or both (`B: -1`).
4. WORKING STEPS (`has_working`):
   - Set `has_working: true` if intermediate mathematical steps are shown for that question, or `false` if ONLY a bare MCQ letter (like `a`, `b`, `c`, `d`) or bare final number was written without steps.
5. HINTS (`student_hints`):
   - If any question has `matches_key: false`, write 2 short, encouraging plain-English sentences giving conceptual advice on what topics to review.
   - STRICTLY ZERO MATHEMATICAL EQUATIONS, FORMULAS, NUMBERS, OR SYMBOLS (no digits, no '=', '+', '-', '/', '^') in `student_hints`. Do NOT mention the word "teacher".

Return ONLY a JSON object in this exact format:
{{
  "per_question": [
    {{
      "q": "Q1",
      "student_final_answer": "...",
      "status": "answered",
      "has_working": true,
      "matches_key": true
    }}
  ],
  "student_hints": "..."
}}"""

    # 1. Try Groq first (llama-3.3-70b-versatile -> 500+ tokens/sec)
    for model_id in ["llama-3.3-70b-versatile", "qwen/qwen3-32b", "llama-3.1-8b-instant"]:
        for offset in range(len(groq_keys)):
            k = groq_keys[(_ROTATION_IDX + offset) % len(groq_keys)]
            _ROTATION_IDX += 1
            try:
                resp = requests.post(
                    "[https://api.groq.com/openai/v1/chat/completions](https://api.groq.com/openai/v1/chat/completions)",
                    headers={
                        "Authorization": f"Bearer {k}",
                        "Content-Type": "application/json"
                    },
                    json={
                        "model": model_id,
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_prompt}
                        ],
                        "response_format": {"type": "json_object"},
                        "temperature": 0.0,
                        "max_tokens": 4096
                    },
                    timeout=35
                )
                if resp.status_code == 200:
                    content = resp.json()["choices"][0]["message"]["content"]
                    parsed = _extract_json_dict(content)
                    if parsed and isinstance(parsed.get("per_question"), list):
                        return parsed
                else:
                    logger.warning(f"Groq ({model_id}) HTTP {resp.status_code}: {resp.text[:200]}")
            except Exception as e:
                logger.warning(f"Groq ({model_id}) error: {e}")

    # 2. Fallback to Mistral Text (mistral-large-latest / mistral-small-latest)
    for model_id in ["mistral-large-latest", "mistral-small-latest"]:
        for offset in range(len(mistral_keys)):
            k = mistral_keys[(_ROTATION_IDX + offset) % len(mistral_keys)]
            _ROTATION_IDX += 1
            try:
                resp = requests.post(
                    "[https://api.mistral.ai/v1/chat/completions](https://api.mistral.ai/v1/chat/completions)",
                    headers={
                        "Authorization": f"Bearer {k}",
                        "Content-Type": "application/json"
                    },
                    json={
                        "model": model_id,
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_prompt}
                        ],
                        "response_format": {"type": "json_object"},
                        "temperature": 0.0,
                        "max_tokens": 4096
                    },
                    timeout=45
                )
                if resp.status_code == 200:
                    content = resp.json()["choices"][0]["message"]["content"]
                    parsed = _extract_json_dict(content)
                    if parsed and isinstance(parsed.get("per_question"), list):
                        return parsed
            except Exception as e:
                logger.warning(f"Mistral chat ({model_id}) error: {e}")

    return None


# ---------------------------------------------------------------------------
# FALLBACK VISION GRADING (Groq Llama-4-Scout Vision / Mistral Pixtral Vision)
# ---------------------------------------------------------------------------
def grade_images_with_vision_batch(
    batch_pages: list[dict],
    total_pages: int,
    assignment_title: str,
    rubric: str,
    groq_keys: list[str],
    mistral_keys: list[str]
) -> dict | None:
    """
    Direct Vision fallback (up to 4 images per batch) using Groq's Llama-4-Scout
    or Mistral's Pixtral-Large if Mistral OCR was not used.
    """
    global _ROTATION_IDX
    start_p = batch_pages[0]["page_num"]
    end_p = batch_pages[-1]["page_num"]
    range_label = f"Page {start_p}" if start_p == end_p else f"Pages {start_p}–{end_p}"

    has_rubric = bool(rubric and rubric.strip())
    rubric_block = (
        f"TEACHER RUBRIC / ANSWER KEY:\n{rubric.strip()}"
        if has_rubric
        else "No rubric provided — verify each question mathematically."
    )

    prompt_text = f"""Grade {range_label} (of {total_pages}) for homework: "{assignment_title}".
{rubric_block}

RULES:
1. Only report questions visible on {range_label}.
2. `status`: `"answered"` (if readable, even if messy), `"blank"` (if empty or `"??"`), or `"unreadable"` (only if completely blurred/cut off).
3. Ignore crossed-out work; judge boxed/circled or final answers. `]3, ∞[` equals `(3, ∞)`.
4. `has_working`: `true` if steps are shown, `false` if only bare choice letters (a, b, c, d) or final numbers are written.
5. `student_hints`: 1-2 short plain-English sentences of conceptual advice with ZERO numbers or math symbols.

Return ONLY valid JSON:
{{
  "per_question": [
    {{"q": "Q1", "student_final_answer": "...", "status": "answered", "has_working": true, "matches_key": true}}
  ],
  "student_hints": "..."
}}"""

    user_content = []
    for p in batch_pages:
        b64_img = base64.b64encode(p["image_bytes"]).decode("utf-8")
        user_content.append({"type": "text", "text": f"[Page {p['page_num']}]"})
        user_content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{b64_img}"}
        })
    user_content.append({"type": "text", "text": prompt_text})

    # Try Groq Vision (Llama 4 Scout / Maverick)
    for model_id in [
        "meta-llama/llama-4-scout-17b-16e-instruct",
        "meta-llama/llama-4-maverick-17b-128e-instruct"
    ]:
        for offset in range(len(groq_keys)):
            k = groq_keys[(_ROTATION_IDX + offset) % len(groq_keys)]
            _ROTATION_IDX += 1
            try:
                resp = requests.post(
                    "[https://api.groq.com/openai/v1/chat/completions](https://api.groq.com/openai/v1/chat/completions)",
                    headers={"Authorization": f"Bearer {k}", "Content-Type": "application/json"},
                    json={
                        "model": model_id,
                        "messages": [{"role": "user", "content": user_content}],
                        "response_format": {"type": "json_object"},
                        "temperature": 0.0,
                        "max_tokens": 3000
                    },
                    timeout=45
                )
                if resp.status_code == 200:
                    content = resp.json()["choices"][0]["message"]["content"]
                    parsed = _extract_json_dict(content)
                    if parsed and isinstance(parsed.get("per_question"), list):
                        return parsed
            except Exception as e:
                logger.warning(f"Groq Vision ({model_id}) error: {e}")

    # Try Mistral Pixtral Vision
    for model_id in ["pixtral-large-latest", "pixtral-12b-2409"]:
        for offset in range(len(mistral_keys)):
            k = mistral_keys[(_ROTATION_IDX + offset) % len(mistral_keys)]
            _ROTATION_IDX += 1
            try:
                resp = requests.post(
                    "[https://api.mistral.ai/v1/chat/completions](https://api.mistral.ai/v1/chat/completions)",
                    headers={"Authorization": f"Bearer {k}", "Content-Type": "application/json"},
                    json={
                        "model": model_id,
                        "messages": [{"role": "user", "content": user_content}],
                        "response_format": {"type": "json_object"},
                        "temperature": 0.0,
                        "max_tokens": 3000
                    },
                    timeout=50
                )
                if resp.status_code == 200:
                    content = resp.json()["choices"][0]["message"]["content"]
                    parsed = _extract_json_dict(content)
                    if parsed and isinstance(parsed.get("per_question"), list):
                        return parsed
            except Exception as e:
                logger.warning(f"Mistral Vision ({model_id}) error: {e}")

    return None


# ---------------------------------------------------------------------------
# MAIN ENTRYPOINT CALLED BY app.py
# ---------------------------------------------------------------------------
def grade_full_homework(
    pdf_bytes: bytes,
    assignment_title: str,
    max_score: float,
    api_key: str | list[str] = None,
    rubric: str = "",
    model_name: str = "auto",
    chunk_size: int = 4,
    progress_callback=None
) -> dict:
    """
    2-Stage Mistral OCR + Groq LPU Grading Pipeline:
    1. Renders page images for the teacher's UI preview (5% -> 25%).
    2. Runs Mistral Native PDF OCR on the whole PDF in 1 call (25% -> 60%).
    3. Grades the extracted math transcript with Groq Llama-3.3-70B in ~1.5s (60% -> 95%).
       (Automatically falls back to Groq Llama-4-Scout / Pixtral Vision if needed).
    4. Computes exact score & equation-free feedback report in Python (100%).
    """
    import streamlit as st
    providers = get_provider_keys(st.secrets)
    groq_keys = providers["groq"]
    mistral_keys = providers["mistral"]

    # Also classify any keys passed directly via api_key argument
    for k in parse_api_keys(api_key):
        if k.startswith("gsk_") and k not in groq_keys:
            groq_keys.append(k)
        elif not k.startswith("AIza") and k not in mistral_keys and k not in groq_keys:
            mistral_keys.append(k)

    if not groq_keys and not mistral_keys:
        raise ValueError(
            "Please add GROQ_API_KEYS and/or MISTRAL_API_KEYS to your .streamlit/secrets.toml file."
        )

    # 1. Render page previews for UI & attachment (5% -> 25%)
    def _on_page(curr_p, tot_p):
        if progress_callback:
            prog = 5 + int(20 * (curr_p / max(1, tot_p)))
            progress_callback(prog, 100, f"Preparing page {curr_p} of {tot_p}...")

    pages = split_pdf_into_page_images(pdf_bytes, on_page_progress=_on_page)
    if not pages:
        raise ValueError("The provided PDF has 0 pages.")
    total_pages = len(pages)

    merged_questions: dict[str, dict] = {}
    all_hints: list[str] = []
    grading_failed_pages: list[str] = []

    # 2. Try Stage 1 (Mistral Native PDF OCR) + Stage 2 (Groq Fast Text Grading)
    used_ocr_pipeline = False
    if mistral_keys:
        if progress_callback:
            progress_callback(30, 100, f"Scanning all {total_pages} page(s) with Mistral Math OCR...")
        ocr_pages = extract_math_with_mistral_ocr(pdf_bytes, mistral_keys)

        if ocr_pages and any(p.strip() for p in ocr_pages):
            if progress_callback:
                progress_callback(65, 100, "Grading extracted math solutions with Groq...")
            graded_json = grade_ocr_text_with_groq_or_mistral(
                ocr_pages=ocr_pages,
                assignment_title=assignment_title,
                rubric=rubric,
                groq_keys=groq_keys,
                mistral_keys=mistral_keys,
            )
            if graded_json and isinstance(graded_json.get("per_question"), list) and len(graded_json["per_question"]) > 0:
                used_ocr_pipeline = True
                hint = _sanitize_hints_no_math(str(graded_json.get("student_hints", "") or ""))
                if hint:
                    all_hints.append(hint)
                for item in graded_json["per_question"]:
                    if not isinstance(item, dict):
                        continue
                    q_norm = _normalize_q_id(item.get("q", ""))
                    if not q_norm:
                        continue
                    merged_questions[q_norm] = {
                        "q": q_norm,
                        "status": str(item.get("status", "answered")).lower(),
                        "has_working": bool(item.get("has_working", False)),
                        "matches_key": bool(item.get("matches_key", False)),
                    }

    # 3. Fallback: Direct Vision Batch Grading (Groq Llama-4-Scout / Mistral Pixtral)
    if not used_ocr_pipeline:
        batches = [pages[i:i + 4] for i in range(0, total_pages, 4)]
        total_batches = len(batches)
        for b_idx, batch in enumerate(batches):
            start_p = batch[0]["page_num"]
            end_p = batch[-1]["page_num"]
            range_label = f"Page {start_p}" if start_p == end_p else f"Pages {start_p}–{end_p}"

            if progress_callback:
                prog = 30 + int(65 * (b_idx / max(1, total_batches)))
                progress_callback(prog, 100, f"Vision Grading {range_label} ({b_idx + 1}/{total_batches})...")

            parsed = grade_images_with_vision_batch(
                batch_pages=batch,
                total_pages=total_pages,
                assignment_title=assignment_title,
                rubric=rubric,
                groq_keys=groq_keys,
                mistral_keys=mistral_keys,
            )
            if parsed is not None:
                hint = _sanitize_hints_no_math(str(parsed.get("student_hints", "") or ""))
                if hint and hint not in all_hints:
                    all_hints.append(hint)
                for item in parsed.get("per_question", []):
                    if not isinstance(item, dict):
                        continue
                    q_norm = _normalize_q_id(item.get("q", ""))
                    if not q_norm:
                        continue
                    status = str(item.get("status", "answered")).lower()
                    has_working = bool(item.get("has_working", False))
                    matches_key = bool(item.get("matches_key", False))
                    if q_norm not in merged_questions:
                        merged_questions[q_norm] = {
                            "q": q_norm,
                            "status": status,
                            "has_working": has_working,
                            "matches_key": matches_key,
                        }
                    else:
                        prev = merged_questions[q_norm]
                        rank = {"answered": 2, "unreadable": 1, "blank": 0}
                        if rank.get(status, 0) >= rank.get(prev["status"], 0):
                            prev["status"] = status
                            prev["matches_key"] = prev["matches_key"] or matches_key
                        prev["has_working"] = prev["has_working"] or has_working
            else:
                grading_failed_pages.append(range_label)

    # 4. Compute Deterministic Score & Report in Python
    wrong_nums: list[str] = []
    not_sure_nums: list[str] = []
    correct_count = 0
    answered_count = 0
    answered_without_steps_count = 0

    for q_id in sorted(merged_questions.keys(), key=_q_sort_key):
        info = merged_questions[q_id]
        st_val = info["status"]

        if st_val == "unreadable":
            not_sure_nums.append(q_id)
        elif st_val == "blank":
            wrong_nums.append(q_id)
        else:
            answered_count += 1
            if not info["has_working"]:
                answered_without_steps_count += 1
            if info["matches_key"]:
                correct_count += 1
            else:
                wrong_nums.append(q_id)

    total_detected = len(merged_questions)
    total_denom = max(total_detected, int(max_score) if max_score <= 60 else total_detected)
    raw_score = (float(max_score) * (correct_count / total_denom)) if total_denom > 0 else 0.0
    final_score = max(0.0, min(float(max_score), round(raw_score, 1)))

    missing_steps_flag = (
        answered_count >= 3
        and (answered_without_steps_count / answered_count) >= MISSING_STEPS_THRESHOLD
    )

    report_lines = []
    if wrong_nums:
        report_lines.append(f"Wrong answers: {', '.join(wrong_nums)}")
        if all_hints:
            combined_hints = " ".join(all_hints[:2]).strip()
            if combined_hints:
                report_lines.append(f"\n{combined_hints}")
    elif not grading_failed_pages and total_detected > 0:
        report_lines.append("Wrong answers: None! Great job on all graded questions.")
    else:
        report_lines.append("Wrong answers: None detected.")

    if missing_steps_flag:
        report_lines.append(
            "\nNote on steps: The student generally misses working steps and only wrote direct choices, "
            "which suggests they could have copied the homework answers."
        )

    if not_sure_nums:
        report_lines.append(f"\nNot sure of questions: {', '.join(not_sure_nums)}")

    if grading_failed_pages:
        report_lines.append(f"\nGrading failed for: {', '.join(grading_failed_pages)}")

    if progress_callback:
        progress_callback(100, 100, "Grading complete!")

    return {
        "final_score": final_score,
        "max_score": max_score,
        "total_pages": total_pages,
        "consolidated_report": "\n".join(report_lines).strip(),
        "page_results": [{"page_num": p["page_num"], "image_bytes": p["image_bytes"]} for p in pages]
    }
