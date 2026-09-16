"""
Job-tailored resume generator.

Creates '<nn>_<Job_Name>_<Company_Name>.pdf' by rewriting the text content of
the original resume PDF *in place* (pymupdf redaction + re-insertion), so the
original fonts, sizes, positions and margins are preserved. Replacement text
is tailored per job description via Gemini (same google-genai client already
used by ai_extractor.py), with a deterministic rule-based fallback (keyword
reordering of the competency sections) when no API key is available or the
API call fails.

CLI (handy for testing without the server):
    python resume_tailor.py --title "Senior Test Automation Engineer" \
        --company "Infosys" --skills "Playwright,Python,AWS"
"""

import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional

import pymupdf

from config import RESUME_BASE_PDF, RESUME_OUTPUT_DIR, RESUME_CACHE_DIR, DEFAULT_GEMINI_MODEL
from database import (
    build_resume_filename,
    get_tailored_resume,
    save_tailored_resume,
    get_base_resume_blob,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Fonts: the base resume uses Liberation Sans, which is metrically identical
# to Arial / Helvetica. We insert Base-14 Helvetica: same widths (layout is
# preserved) and its text extraction is clean (spaces/hyphens stay real ASCII
# characters, unlike some embedded TTF subsets) which matters for ATS parsers.
# ---------------------------------------------------------------------------

_STYLE_BASE14 = {
    "regular": "helv",
    "bold": "hebo",
    "italic": "heit",
    "bold_italic": "hebi",
}

_STYLE_FONTNAME = {"regular": "AR", "bold": "ARB", "italic": "ARI", "bold_italic": "ARBI"}

_STYLE_CACHE: Dict[str, Dict[str, Any]] = {}


def _get_style_font(style: str) -> Dict[str, Any]:
    """Resolves a text style to its Base-14 Helvetica insertion font."""
    if style in _STYLE_CACHE:
        return _STYLE_CACHE[style]
    entry: Dict[str, Any] = {"base14": _STYLE_BASE14[style]}
    _STYLE_CACHE[style] = entry
    return entry


def _classify_span_font(font_name: str) -> str:
    n = (font_name or "").lower()
    if "bold" in n and ("italic" in n or "oblique" in n):
        return "bold_italic"
    if "bold" in n:
        return "bold"
    if "italic" in n or "oblique" in n:
        return "italic"
    return "regular"


def _color_tuple(srgb_int: int):
    r = (srgb_int >> 16) & 255
    g = (srgb_int >> 8) & 255
    b = srgb_int & 255
    return (r / 255.0, g / 255.0, b / 255.0)


# ---------------------------------------------------------------------------
# Unicode sanitizing: the replacement fonts cover Latin-1, so map common
# typographic characters down and drop anything outside that range.
# ---------------------------------------------------------------------------

_CHAR_MAP = {
    "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
    "\u2013": "-", "\u2014": "-", "\u2022": "-", "\u00a0": " ",
    "\u2026": "...", "\u2192": "->", "\u00ae": "", "\u2122": "",
    "\u25a0": "-", "\uf0a7": "-", "\uf0b7": "-",
}


def _sanitize(text: str) -> str:
    text = text or ""
    for k, v in _CHAR_MAP.items():
        text = text.replace(k, v)
    return "".join(ch for ch in text if ord(ch) >= 32 and ord(ch) <= 255)


# ---------------------------------------------------------------------------
# Line extraction from the base PDF
# ---------------------------------------------------------------------------

def _extract_lines(page) -> List[Dict[str, Any]]:
    """Groups spans into visual lines (same baseline y), sorted top-down."""
    raw = page.get_text("dict")
    lines: Dict[int, Dict[str, Any]] = {}
    for block in raw["blocks"]:
        if block.get("type") != 0:
            continue
        for line in block["lines"]:
            for span in line["spans"]:
                if not span["text"].strip():
                    continue
                key = round(span["origin"][1])
                entry = lines.get(key)
                if entry is None:
                    entry = {"y": span["origin"][1], "x0": span["origin"][0],
                             "x1": span["bbox"][2], "spans": []}
                    lines[key] = entry
                entry["x1"] = max(entry["x1"], span["bbox"][2])
                entry["spans"].append(span)
    out: List[Dict[str, Any]] = []
    for key in sorted(lines):
        entry = lines[key]
        entry["spans"].sort(key=lambda s: s["origin"][0])
        entry["text"] = "".join(s["text"] for s in entry["spans"]).strip()
        dominant = max(entry["spans"], key=lambda s: len(s["text"].strip()))
        entry["style"] = _classify_span_font(dominant["font"])
        entry["size"] = dominant["size"]
        entry["color"] = dominant["color"]
        out.append(entry)
    return out


def _slot_from_line(line: Dict[str, Any], x_start: float, right_margin: float,
                    spans: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """Builds one rewrite slot. `spans` limits both the original text and the
    redaction rectangle to the content part of the line (label/bullet glyphs
    outside that range stay untouched in the PDF)."""
    use = [s for s in (spans or line["spans"]) if s["origin"][0] >= x_start - 1]
    orig_text = "".join(s["text"] for s in sorted(use, key=lambda s: s["origin"][0])).strip()
    if not use:
        use = line["spans"]
        orig_text = line["text"]
    return {
        "page": None,  # filled by caller
        "y": round(line["y"], 2),
        "x_start": round(x_start, 2),
        "right": round(right_margin, 2),
        "size": line["size"],
        "style": line["style"],
        "color": line["color"],
        "orig": orig_text,
        "bbox": (min(s["bbox"][0] for s in use) - 0.5,
                 min(s["bbox"][1] for s in use) - 0.5,
                 right_margin + 1.0,
                 max(s["bbox"][3] for s in use) + 0.5),
    }


# ---------------------------------------------------------------------------
# Rewrite-plan builder (structural, survives small base-resume edits)
# ---------------------------------------------------------------------------

def _build_line_plan(doc) -> Dict[str, Any]:
    """Scans the base resume and returns the rewrite plan skeleton with the
    original texts (used both for the AI prompt and the rule-based fallback)."""
    plan: Dict[str, Any] = {"headline": None, "summary_slots": [], "comp_groups": []}

    all_lines = {pno: _extract_lines(page) for pno, page in enumerate(doc)}

    def _find_header(pno: int, text: str) -> Optional[Dict[str, Any]]:
        for line in all_lines.get(pno, []):
            if line["text"].strip().upper().startswith(text.upper()):
                return line
        return None

    def _lines_until(pno: int, start_y: float, stop_text: Optional[str]) -> List[Dict[str, Any]]:
        result = []
        for line in all_lines.get(pno, []):
            if line["y"] <= start_y + 2:
                continue
            if stop_text and line["text"].strip().upper().startswith(stop_text.upper()):
                break
            result.append(line)
        return result

    # --- Headline: a size-10 line on page 1 above the PROFESSIONAL SUMMARY ---
    summary_header = _find_header(0, "PROFESSIONAL SUMMARY")
    if summary_header:
        candidates = [l for l in all_lines.get(0, [])
                      if l["y"] < summary_header["y"] - 4 and 9.6 <= l["size"] <= 11.5]
        if candidates:
            headline_line = candidates[0]
            slot = _slot_from_line(headline_line, headline_line["x0"],
                                   headline_line["x1"] + 6)
            slot["page"] = 0
            slot["bbox"] = (headline_line["x0"] - 0.5,
                            min(s["bbox"][1] for s in headline_line["spans"]) - 0.5,
                            headline_line["x1"] + 1.0,
                            max(s["bbox"][3] for s in headline_line["spans"]) + 0.5)
            plan["headline"] = slot

    # --- Summary slots: indented body lines between the two headers ---
    # The leading bullet glyph (x < 50) is deliberately excluded from the slot
    # so it survives redaction and the visual list style stays consistent.
    if summary_header:
        competencies_header = _find_header(0, "CORE COMPETENCIES")
        stop_y = competencies_header["y"] if competencies_header else 10 ** 6
        body = [l for l in _lines_until(0, summary_header["y"], "CORE COMPETENCIES")
                if l["x0"] < 10 ** 6 and l["x0"] > 48 and l["y"] < stop_y]
        for line in body[:9]:
            content_spans = [s for s in line["spans"] if s["origin"][0] > 50]
            if not content_spans:
                continue
            x_start = min(s["origin"][0] for s in content_spans)
            slot = _slot_from_line(line, x_start, 552.8, spans=content_spans)
            slot["page"] = 0
            plan["summary_slots"].append(slot)

    # --- Competency groups: labelled lines + continuation lines, page 1 & 2 ---
    for pno, header_text, stop_text in ((0, "CORE COMPETENCIES", "PROFESSIONAL EXPERIENCE"),
                                        (1, "TECHNICAL SKILLS", "EDUCATION & CERTIFICATIONS")):
        header = _find_header(pno, header_text)
        if not header:
            continue
        section_lines = _lines_until(pno, header["y"], stop_text)
        # Content indent: x0 of continuation lines (no label); default 175.2.
        cont_x0s = [round(l["x0"], 1) for l in section_lines if l["x0"] > 160]
        content_x0 = cont_x0s[0] if cont_x0s else 175.2

        current: Optional[Dict[str, Any]] = None
        for line in section_lines:
            if line["x0"] < 100:  # bullet + label line -> new group
                label_text = "".join(s["text"] for s in line["spans"]
                                     if s["origin"][0] < content_x0 - 2).strip()
                label_text = re.sub(r"^[-\u2022\u25a0\uf0a7\uf0b7]\s*", "", label_text)
                current = {"label": label_text, "slots": []}
                plan["comp_groups"].append(current)
            if current is None:
                continue
            # Content starts at the section's content indent; the bullet and
            # label glyphs (left of it) are preserved untouched in the PDF.
            x_start = content_x0 if line["x0"] < 100 else line["x0"]
            content_spans = ([s for s in line["spans"] if s["origin"][0] >= content_x0 - 2]
                             if line["x0"] < 100 else line["spans"])
            if not content_spans:
                continue
            slot = _slot_from_line(line, x_start, 552.8, spans=content_spans)
            slot["page"] = pno
            current["slots"].append(slot)

    return plan


# ---------------------------------------------------------------------------
# Text wrapping with real font metrics
# ---------------------------------------------------------------------------

def _text_length(text: str, style: str, size: float) -> float:
    entry = _get_style_font(style)
    return pymupdf.get_text_length(text, fontname=entry["base14"], fontsize=size)


def _wrap_to_slots(text: str, slots: List[Dict[str, Any]]) -> List[str]:
    """Greedy word-wrap of `text` into the slot widths; returns one string per
    slot (empty string when the text ran out). Never overflows a slot."""
    entry_style = slots[0]["style"] if slots else "regular"
    size = slots[0]["size"] if slots else 9.5
    words = _sanitize(text).split()
    out: List[str] = []
    current = ""
    for slot_idx, slot in enumerate(slots):
        avail = slot["right"] - slot["x_start"]
        line = current
        while words:
            trial = (line + " " + words[0]).strip()
            if _text_length(trial, slot["style"], slot["size"]) <= avail:
                line = trial
                words.pop(0)
            else:
                break
        out.append(line)
        current = ""
        if not words:
            # fill remaining slots with empties
            out.extend([""] * (len(slots) - slot_idx - 1))
            break
    if words:
        logger.warning("Tailored text did not fit %d slot(s); %d word(s) dropped",
                       len(slots), len(words))
    return out


# ---------------------------------------------------------------------------
# Tailoring backends
# ---------------------------------------------------------------------------

_STOPWORDS = {"the", "and", "for", "with", "a", "an", "of", "in", "to", "on",
              "at", "or", "is", "are", "be", "as", "by", "we", "you", "our"}


def _job_keywords(job: Dict[str, Any]) -> List[str]:
    keywords: List[str] = []
    skills = job.get("skills") or []
    if isinstance(skills, str):
        try:
            skills = json.loads(skills)
        except (ValueError, TypeError):
            skills = [s.strip() for s in skills.split(",") if s.strip()]
    for s in skills or []:
        keywords.append(str(s))
    for field in (job.get("job_title"), job.get("summary"), job.get("experience_level")):
        for w in re.findall(r"[A-Za-z][A-Za-z+#.]{2,}", str(field or "")):
            if w.lower() not in _STOPWORDS:
                keywords.append(w)
    seen, unique = set(), []
    for k in keywords:
        if k.lower() not in seen:
            seen.add(k.lower())
            unique.append(k)
    return unique[:40]


def _tailor_rule_based(job: Dict[str, Any], plan: Dict[str, Any]) -> Dict[str, Any]:
    """Deterministic fallback: keep headline/summary, reorder competency items
    so the ones matching the job's keywords come first."""
    keywords = [k.lower() for k in _job_keywords(job)]

    def reorder(content: str, slots: List[Dict[str, Any]]) -> str:
        items = [i.strip() for i in content.split(",") if i.strip()]
        scored = []
        for item in items:
            low = item.lower()
            score = sum(1 for k in keywords if k in low)
            scored.append((score, item))
        scored_sorted = sorted(scored, key=lambda t: -t[0])
        # Greedily keep items that still fit the available width.
        capacity = sum(s["right"] - s["x_start"] for s in slots) * 0.98
        kept: List[tuple] = []
        used = 0.0
        for score, item in scored_sorted:
            piece = _sanitize(item)
            width = _text_length(piece + ", ", slots[0]["style"] if slots else "regular",
                                 slots[0]["size"] if slots else 9.0)
            if used + width <= capacity or not kept:
                kept.append((score, item))
                used += width
        kept.sort(key=lambda t: 0)  # keep original resume order among the kept
        kept_items = [i for _, i in kept]
        # Preserve the original order of the selected items:
        kept_items = [i for i in items if i in kept_items]
        return ", ".join(kept_items)

    contents = []
    for group in plan["comp_groups"]:
        # Slot "orig" text is the CONTENT only (label/bullet glyphs were
        # excluded at plan time), so no label stripping is needed here.
        joined = " ".join(s["orig"] for s in group["slots"]).strip()
        contents.append(reorder(joined, group["slots"]))


    return {
        "headline": plan["headline"]["orig"] if plan["headline"] else "",
        "summary_p1": " ".join(s["orig"] for s in plan["summary_slots"][0:4]),
        "summary_p2": " ".join(s["orig"] for s in plan["summary_slots"][4:7]),
        "summary_p3": " ".join(s["orig"] for s in plan["summary_slots"][7:9]),
        "comp_contents": contents,
        "match_note": "Tailored with rule-based keyword reordering (no AI key configured).",
        "engine": "rules",
    }


def _tailor_with_gemini(job: Dict[str, Any], plan: Dict[str, Any], api_key: str) -> Optional[Dict[str, Any]]:
    """Gemini-powered rewrite of headline/summary/competency content."""
    if not api_key:
        return None
    try:
        from google import genai
        from google.genai import types
    except ImportError:
        logger.warning("google-genai not installed; falling back to rules")
        return None

    def _chars(slots: List[Dict[str, Any]]) -> int:
        if not slots:
            return 0
        avg_width = sum(s["right"] - s["x_start"] for s in slots) / len(slots)
        size = slots[0]["size"]
        return int(sum(s["right"] - s["x_start"] for s in slots) / (size * 0.47))

    summary_p1 = " ".join(s["orig"] for s in plan["summary_slots"][0:4]).strip()
    summary_p2 = " ".join(s["orig"] for s in plan["summary_slots"][4:7]).strip()
    summary_p3 = " ".join(s["orig"] for s in plan["summary_slots"][7:9]).strip()

    comp_lines = []
    for idx, group in enumerate(plan["comp_groups"], start=1):
        original = " ".join(s["orig"] for s in group["slots"]).strip()
        comp_lines.append(f"{idx}. {group['label']}: {original}")

    skills = job.get("skills") or []
    if isinstance(skills, str):
        try:
            skills = json.loads(skills)
        except (ValueError, TypeError):
            skills = [s.strip() for s in skills.split(",") if s.strip()]

    prompt = f"""You are an expert technical resume writer. Tailor sections of Deepak Kumar Behera's resume for the job below.

STRICT RULES:
- NEVER invent employers, job titles, dates, degrees or certifications.
- Only reframe the candidate's existing, truthful experience using the job's terminology.
- Naturally include the job's important keywords for ATS matching.
- Keep the same professional tone. Plain ASCII text only (no bullets, no markdown).
- Respect the maximum character limits (they map to fixed PDF line widths).

JOB: {job.get('job_title')} @ {job.get('company_name')} ({job.get('location')}, {job.get('job_type')}, experience: {job.get('experience_level')})
Job key skills: {', '.join(str(s) for s in skills) or 'not listed'}
Job summary: {str(job.get('summary') or '')[:400]}
Job posting snippet: {str(job.get('raw_email_snippet') or '')[:1200]}

ORIGINAL RESUME CONTENT:
HEADLINE (max {_chars([plan['headline']]) if plan['headline'] else 110} chars): {plan['headline']['orig'] if plan['headline'] else ''}
SUMMARY paragraph 1 (max {_chars(plan['summary_slots'][0:4])} chars): {summary_p1}
SUMMARY paragraph 2 (max {_chars(plan['summary_slots'][4:7])} chars): {summary_p2}
SUMMARY paragraph 3 (max {_chars(plan['summary_slots'][7:9])} chars): {summary_p3}
COMPETENCY SECTIONS (content only will be replaced; keep each under its char cap):
{chr(10).join(comp_lines)}

Return ONLY raw JSON (no markdown fences) with this exact schema:
{{
  "headline": "rewritten headline, max {_chars([plan['headline']]) if plan['headline'] else 110} chars, parts separated by ' | '",
  "summary_p1": "rewritten paragraph 1, max {_chars(plan['summary_slots'][0:4])} chars",
  "summary_p2": "rewritten paragraph 2, max {_chars(plan['summary_slots'][4:7])} chars",
  "summary_p3": "rewritten paragraph 3, max {_chars(plan['summary_slots'][7:9])} chars",
  "comp_contents": ["content for section 1 (no label, comma-separated items)", "... one string per section, same order ..."],
  "match_note": "one sentence (max 120 chars) on why this resume fits the job"
}}
The comp_contents array MUST have exactly {len(plan['comp_groups'])} strings, in the same order as the sections above."""

    try:
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model=DEFAULT_GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json"),
        )
        raw = (response.text or "").strip()
        raw = re.sub(r"^```(?:json)?|```$", "", raw).strip()
        data = json.loads(raw)
    except Exception as e:
        logger.warning("Gemini resume tailoring failed, falling back to rules: %s", e)
        return None

    comp_contents = data.get("comp_contents") or []
    if len(comp_contents) < len(plan["comp_groups"]):
        comp_contents += [""] * (len(plan["comp_groups"]) - len(comp_contents))
    comp_contents = comp_contents[: len(plan["comp_groups"])]

    return {
        "headline": str(data.get("headline") or (plan["headline"]["orig"] if plan["headline"] else "")),
        "summary_p1": str(data.get("summary_p1") or summary_p1),
        "summary_p2": str(data.get("summary_p2") or summary_p2),
        "summary_p3": str(data.get("summary_p3") or summary_p3),
        "comp_contents": [str(c) for c in comp_contents],
        "match_note": str(data.get("match_note") or "")[:200],
        "engine": "gemini",
    }


# ---------------------------------------------------------------------------
# PDF writing (redact + re-insert, keeping graphics)
# ---------------------------------------------------------------------------

def _capture_intersecting_drawings(page, rects) -> List[Dict[str, Any]]:
    items = []
    for drawing in page.get_drawings():
        for item in drawing.get("items", []):
            op = item[0]
            try:
                if op == "l":
                    rect = pymupdf.Rect(item[1], item[2])
                elif op == "re":
                    rect = pymupdf.Rect(item[1])
                elif op == "qu":
                    rect = item[1].rect
                elif op == "c":
                    rect = pymupdf.Rect(item[1], item[4])
                else:
                    continue
            except Exception:
                continue
            if any(rect.intersects(r) for r in rects):
                items.append({"op": op, "item": item, "color": drawing.get("color"),
                              "fill": drawing.get("fill"), "width": drawing.get("width") or 0.5})
    return items


def _redraw_items(page, items) -> None:
    for entry in items:
        item, op = entry["item"], entry["op"]
        color = entry["color"]
        width = entry["width"]
        try:
            if op == "l":
                page.draw_line(item[1], item[2], color=color, width=width)
            elif op == "re":
                page.draw_rect(item[1], color=color, fill=entry["fill"], width=width)
            elif op == "qu":
                page.draw_quad(item[1], color=color, width=width)
            elif op == "c":
                page.draw_bezier(item[1], item[2], item[3], item[4], color=color, width=width)
        except Exception as e:
            logger.debug("Could not redraw graphics item (%s): %s", op, e)


def _apply_redactions_keep_art(page, rects) -> None:
    """Applies text redactions while preserving vector graphics (underlines)."""
    try:
        graphics_none = getattr(pymupdf, "PDF_REDACT_LINE_ART_NONE", 0)
        page.apply_redactions(images=getattr(pymupdf, "PDF_REDACT_IMAGE_NONE", 0),
                              graphics=graphics_none)
    except (AttributeError, TypeError):
        protected = _capture_intersecting_drawings(page, rects)
        page.apply_redactions()
        _redraw_items(page, protected)


def _insert_line(page, slot: Dict[str, Any], text: str) -> None:
    if not text:
        return
    entry = _get_style_font(slot["style"])
    page.insert_text(pymupdf.Point(slot["x_start"], slot["y"]), text,
                     fontname=entry["base14"], fontsize=slot["size"],
                     color=_color_tuple(slot["color"]))


def _write_tailored_pdf(base_bytes: bytes, texts: Dict[str, Any], out_path) -> bytes:
    doc = pymupdf.open(stream=base_bytes, filetype="pdf")
    # The plan is rebuilt here (not reused) so every slot carries live page
    # references of THIS document instance.
    plan = _build_line_plan(doc)

    jobs_by_page: Dict[int, List] = {}
    for pno in range(len(doc)):
        jobs_by_page[pno] = []

    def _add(slot, new_text):
        if slot is not None and new_text:
            jobs_by_page.setdefault(slot["page"], []).append((slot, new_text))

    _add(plan["headline"], _sanitize(texts["headline"]))

    wrapped = []
    wrapped += _wrap_to_slots(texts.get("summary_p1", ""), plan["summary_slots"][0:4])
    wrapped += _wrap_to_slots(texts.get("summary_p2", ""), plan["summary_slots"][4:7])
    wrapped += _wrap_to_slots(texts.get("summary_p3", ""), plan["summary_slots"][7:9])
    for slot, line in zip(plan["summary_slots"], wrapped):
        _add(slot, line)

    for group, content in zip(plan["comp_groups"], texts.get("comp_contents", [])):
        for slot, line in zip(group["slots"], _wrap_to_slots(content, group["slots"])):
            _add(slot, line)

    for pno, page in enumerate(doc):
        page_jobs = jobs_by_page.get(pno, [])
        if not page_jobs:
            continue
        rects = [pymupdf.Rect(slot["bbox"]) for slot, _ in page_jobs]
        for rect in rects:
            page.add_redact_annot(rect)
        _apply_redactions_keep_art(page, rects)
        for slot, text in page_jobs:
            _insert_line(page, slot, text)

    doc.save(str(out_path), garbage=3, deflate=True)
    pdf_bytes = open(out_path, "rb").read()
    doc.close()
    return pdf_bytes


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def generate_tailored_resume(job: Dict[str, Any], force: bool = False,
                             gemini_api_key: str = "") -> Dict[str, Any]:
    """Generates (or returns the cached) tailored resume for a job.

    Returns dict with: file_name, view_url, download_url, engine, match_note,
    cached, output_paths (local copies), pdf_bytes (for API streaming)."""
    job_id = job.get("id")

    cached = get_tailored_resume(job)
    if cached and not force:
        return {
            "status": "success",
            "cached": True,
            "file_name": cached["file_name"],
            "engine": cached.get("engine") or "",
            "match_note": "",
            "generated_at": cached.get("generated_at") or "",
            "view_url": f"/api/jobs/{job_id}/resume/view",
            "download_url": f"/api/jobs/{job_id}/resume/download",
        }

    base_bytes = get_base_resume_blob()
    if not base_bytes:
        return {"status": "error", "error":
                "Base resume PDF not found (upload it with: python resume_tailor.py --seed-base)."}

    try:
        with pymupdf.open(stream=base_bytes, filetype="pdf") as doc:
            plan = _build_line_plan(doc)
    except Exception as e:
        return {"status": "error", "error": f"Could not parse base resume PDF: {e}"}

    if not plan["summary_slots"] or not plan["comp_groups"]:
        return {"status": "error", "error":
                "Base resume structure not recognized (summary/competency sections missing)."}

    texts = _tailor_with_gemini(job, plan, gemini_api_key) or _tailor_rule_based(job, plan)

    file_name = build_resume_filename(job, int(job_id or 0))
    now_iso = datetime.now(timezone.utc).isoformat()

    output_paths = []
    pdf_bytes = b""
    for folder in (RESUME_CACHE_DIR, RESUME_OUTPUT_DIR):
        try:
            folder.mkdir(parents=True, exist_ok=True)
            target = folder / file_name
            pdf_bytes = _write_tailored_pdf(base_bytes, texts, target)
            output_paths.append(str(target))
        except Exception as e:
            logger.warning("Could not write resume to %s: %s", folder, e)

    if not output_paths:
        # Fallback (e.g. read-only FS): build bytes in memory only.
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp_path = tmp.name
        pdf_bytes = _write_tailored_pdf(base_bytes, texts, tmp_path)
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        output_paths.append("(memory only)")

    try:
        save_tailored_resume(job, file_name, pdf_bytes, now_iso, texts["engine"],
                             match_summary=texts.get("match_note", ""))
    except Exception as e:
        logger.warning("Could not persist tailored resume to database: %s", e)

    return {
        "status": "success",
        "cached": False,
        "file_name": file_name,
        "engine": texts["engine"],
        "match_note": texts.get("match_note", ""),
        "generated_at": now_iso,
        "view_url": f"/api/jobs/{job_id}/resume/view",
        "download_url": f"/api/jobs/{job_id}/resume/download",
        "output_paths": output_paths,
        "pdf_bytes": pdf_bytes,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    import sys

    sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="Generate a job-tailored resume PDF")
    parser.add_argument("--title", required=False, default="Senior Test Automation Engineer")
    parser.add_argument("--company", required=False, default="Example Corp")
    parser.add_argument("--skills", required=False, default="")
    parser.add_argument("--job-id", type=int, default=1)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--seed-base", action="store_true",
                        help="Upload the base resume PDF into the database for serverless use")
    args = parser.parse_args()

    if args.seed_base:
        from database import ensure_base_resume_blob
        ok = ensure_base_resume_blob()
        print("Base resume upload:", "OK" if ok else "FAILED")
        sys.exit(0 if ok else 1)

    fake_job = {
        "id": args.job_id,
        "job_title": args.title,
        "company_name": args.company,
        "skills": [s.strip() for s in args.skills.split(",") if s.strip()] if args.skills else [],
        "location": "",
        "job_type": "",
        "experience_level": "",
        "summary": "",
        "raw_email_snippet": "",
    }

    from database import get_setting
    from config import GEMINI_API_KEY
    key = get_setting("gemini_api_key", "") or GEMINI_API_KEY

    result = generate_tailored_resume(fake_job, force=args.force, gemini_api_key=key)
    if result.get("status") != "success":
        print("FAILED:", result.get("error"))
        sys.exit(1)
    print(json.dumps({k: v for k, v in result.items() if k != "pdf_bytes"}, indent=2))
