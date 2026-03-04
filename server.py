#!/usr/bin/env python3
"""
PDF Watermark Removal Backend
Core removal logic ported from app.py (proven working).
Wrapped in a Flask API with CORS, stats and log headers.
"""

import io
import os
import re
import json
from flask import Flask, request, send_file, jsonify, render_template
from flask_cors import CORS
import fitz  # PyMuPDF

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024  # 100 MB
CORS(app, expose_headers=["X-Stats", "X-Logs"])

# ─────────────────────────────────────────────────────────────
# WATERMARK VOCABULARY
# ─────────────────────────────────────────────────────────────

COMMON_WATERMARKS = [
    "DRAFT", "CONFIDENTIAL", "SAMPLE", "WATERMARK", "COPY",
    "DO NOT COPY", "FOR REVIEW", "INTERNAL", "RESTRICTED",
    "TOP SECRET", "CLASSIFIED", "NOT FOR DISTRIBUTION",
    "PREVIEW", "SPECIMEN", "VOID", "CANCELLED", "PAID",
    "RECEIVED", "APPROVED", "REJECTED", "PENDING", "DUPLICATE",
    "ILOVEPDF", "SMALLPDF", "SEJDA", "ADOBE",
    "PROCESSED BY", "EVALUATION", "TRIAL",
]

# Short companion strings that appear alongside watermarks
# (timestamps, document IDs, version numbers)
COMPANION_PATTERNS = [
    r"^\w{3}-\d{2}-\d{4}",          # Mar-01-2024
    r"^\d{1,2}-\d{2}-\d{4}",        # 01-01-2024
    r"^\d{1,2}:\d{2}(:\d{2})?",     # 14:30 or 14:30:00
    r"^\d{4,8}$",                    # 4-8 digit numeric IDs
    r"^[A-Z]+-\d{2}-\d{4}\s+\d{2}", # ABC-01-2024 12
    r"^\d{2}/\d{2}/\d{4}",          # 01/01/2024
    r"^\d{4}-\d{2}-\d{2}",          # 2024-01-01 (ISO date)
    r"^[A-Z]{2,6}\d{4,10}$",        # DOCREF00001234
    r"^Page\s+\d+",                  # Page 1
    r"^\d+\s*of\s*\d+",             # 1 of 10
]


def is_companion_text(text: str) -> bool:
    return any(re.match(p, text.strip(), re.IGNORECASE) for p in COMPANION_PATTERNS)


# ─────────────────────────────────────────────────────────────
# HTTP ENDPOINTS
# ─────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "pymupdf": fitz.version[0]})


@app.route("/remove-watermark", methods=["POST"])
def remove_watermark():
    if "file" not in request.files:
        return jsonify({"error": "No PDF file provided"}), 400

    f = request.files["file"]
    try:
        options = json.loads(request.form.get("options", "{}"))
    except Exception:
        options = {}

    pdf_bytes = f.read()
    if not pdf_bytes:
        return jsonify({"error": "Empty file"}), 400

    try:
        result_bytes, stats, logs = process_pdf(pdf_bytes, options)
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500

    buf = io.BytesIO(result_bytes)
    buf.seek(0)
    resp = send_file(buf, mimetype="application/pdf", download_name="cleaned.pdf")
    resp.headers["X-Stats"] = json.dumps(stats)
    resp.headers["X-Logs"] = json.dumps(logs[:60])
    return resp


# ─────────────────────────────────────────────────────────────
# MAIN PIPELINE
# ─────────────────────────────────────────────────────────────

def process_pdf(pdf_bytes: bytes, options: dict):
    # Build the full terms list: common vocabulary + user keywords
    user_kws = [k.strip().upper() for k in options.get("keywords", []) if k.strip()]
    terms = user_kws + [w for w in COMMON_WATERMARKS if w not in user_kws]

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")

    stats = {
        "streamsRemoved": 0,
        "redacted": 0,
        "annotationsRemoved": 0,
        "xobjectsRemoved": 0,
        "streamsProcessed": 0,
        # kept for UI compatibility
        "blocksRemoved": 0,
        "ocgRemoved": 0,
    }
    all_logs = []

    for page_idx in range(doc.page_count):
        page = doc[page_idx]
        lp = f"Page {page_idx + 1}:"

        # ── Strategy 1: Blank / partially strip content streams ──────────────
        for cxref in page.get_contents():
            try:
                stream = doc.xref_stream(cxref)
                if not stream:
                    continue
                stats["streamsProcessed"] += 1

                # 1a. Keyword/vocab match AND every literal is watermark → blank whole stream
                if _stream_contains_watermark(stream, terms) and \
                   _stream_is_only_watermark(stream, terms):
                    doc.update_stream(cxref, b"")
                    stats["streamsRemoved"] += 1
                    stats["blocksRemoved"] += 1
                    all_logs.append(f"{lp} Blanked watermark-only stream (xref {cxref})")
                    continue

                # 1b. Tiled personal watermark: 3+ BT blocks all with identical short text.
                #     Handles "" × 113 pattern on every page of 1.pdf.
                elif _is_tiled_watermark_stream(stream):
                    sample = _tiled_stream_sample_text(stream)
                    doc.update_stream(cxref, b"")
                    stats["streamsRemoved"] += 1
                    stats["blocksRemoved"] += 1
                    all_logs.append(f"{lp} Blanked tiled watermark stream (xref {cxref}): '{sample}'")
                    continue

                # 1c. Partial removal: strip standalone BT blocks with very light-grey
                #     color (≥ 0.78 on all channels). Handles 0.886 watermark in 2.PDF.
                elif options.get("removeTransparent", True):
                    modified, count = _remove_lightgrey_bt_blocks(stream)
                    if count > 0:
                        doc.update_stream(cxref, modified, compress=True)
                        stats["blocksRemoved"] += count
                        all_logs.append(f"{lp} Removed {count} light-grey BT block(s) (xref {cxref})")

            except Exception as e:
                all_logs.append(f"{lp} Stream error xref={cxref}: {e}")

        # ── Strategy 2: Redact matching text lines via PyMuPDF text layer ──
        # Only runs when removeAnnotations or a keyword is set, since
        # get_text() redaction targets visible text in the rendered layer.
        if options.get("removeAnnotations", True) or user_kws:
            try:
                raw = page.get_text("rawdict", flags=fitz.TEXT_PRESERVE_WHITESPACE)
                redact_rects = []
                for block in raw.get("blocks", []):
                    if block.get("type") != 0:
                        continue
                    for line in block.get("lines", []):
                        spans = line.get("spans", [])
                        combined = "".join(s.get("text", "") for s in spans).strip().upper()
                        if combined and any(t in combined for t in terms):
                            bboxes = [fitz.Rect(s["bbox"]) for s in spans if s.get("bbox")]
                            if bboxes:
                                lr = bboxes[0]
                                for r in bboxes[1:]:
                                    lr |= r
                                redact_rects.append(lr + (-1, -2, 1, 2))

                if redact_rects:
                    for rect in redact_rects:
                        page.add_redact_annot(rect, fill=None, cross_out=False)
                    page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)
                    stats["redacted"] += len(redact_rects)
                    all_logs.append(f"{lp} Redacted {len(redact_rects)} text line(s)")
            except Exception as e:
                all_logs.append(f"{lp} Text redaction error: {e}")

        # ── Strategy 3: Remove annotations whose content matches terms ──
        if options.get("removeAnnotations", True):
            to_delete = []
            for annot in page.annots():
                info = annot.info
                combined = (info.get("content", "") + info.get("title", "")).upper()
                if any(t in combined for t in terms):
                    to_delete.append(annot)
            for annot in to_delete:
                try:
                    page.delete_annot(annot)
                    stats["annotationsRemoved"] += 1
                    all_logs.append(f"{lp} Deleted annotation: '{combined[:60]}'")
                except Exception as e:
                    all_logs.append(f"{lp} Annotation delete error: {e}")

        # ── Strategy 4: Remove Form XObjects with low opacity or Multiply ──
        # Catches transparent image/graphic overlays used as watermarks.
        # Looks for /ca 0.0–0.3 (fill opacity) or /Multiply blendmode in
        # the XObject dictionary — exactly how app.py does it.
        if options.get("removeXObjects", True):
            try:
                for xref, name, *_ in page.get_xobjects():
                    try:
                        xobj_str = doc.xref_object(xref)
                        if "/Form" in xobj_str and (
                            re.search(r"/ca\s+0\.[0-3]", xobj_str, re.IGNORECASE) or
                            "/Multiply" in xobj_str
                        ):
                            doc.update_stream(xref, b"")
                            stats["xobjectsRemoved"] += 1
                            all_logs.append(
                                f"{lp} Cleared transparent Form XObject /{name} (xref {xref})"
                            )
                    except Exception:
                        pass
            except Exception as e:
                all_logs.append(f"{lp} XObject scan error: {e}")

    # Save with garbage collection + compression
    out = io.BytesIO()
    doc.save(out, garbage=4, deflate=True)
    doc.close()

    return out.getvalue(), stats, all_logs


# ─────────────────────────────────────────────────────────────
# STREAM ANALYSIS HELPERS
# ─────────────────────────────────────────────────────────────

# ── Tiled-watermark detection (Fix for 1.pdf) ────────────────

def _tiled_stream_sample_text(stream_bytes: bytes) -> str:
    """Return the repeated text from a tiled watermark stream."""
    try:
        text = stream_bytes.decode("latin-1", errors="replace")
        blocks = re.findall(r"BT\b(.*?)\bET", text, re.DOTALL)
        if blocks:
            lits = re.findall(r"\(((?:[^()\\]|\\.)*)\)", blocks[0])
            return re.sub(r"\\(.)", r"\1", "".join(lits)).strip()[:80]
    except Exception:
        pass
    return ""


def _is_tiled_watermark_stream(stream_bytes: bytes) -> bool:
    """True if the stream is a tiled personal-stamp watermark.

    Criteria (all must hold):
    - ≥ 3 BT blocks
    - A single dominant short text appears ≥ 3 times
    - Every non-empty, non-companion text is a substring or superset of
      that dominant text  (handles '')
    """
    try:
        text = stream_bytes.decode("latin-1", errors="replace")
    except Exception:
        return False

    bt_blocks = re.findall(r"BT\b(.*?)\bET", text, re.DOTALL)
    if len(bt_blocks) < 3:
        return False

    def block_text(block):
        lits = re.findall(r"\(((?:[^()\\]|\\.)*)\)", block)
        return re.sub(r"\\(.)", r"\1", "".join(lits)).strip()

    texts       = [block_text(b) for b in bt_blocks]
    non_empty   = [t for t in texts if t]
    non_companion = [t for t in non_empty if not is_companion_text(t)]

    if not non_companion:
        return False

    from collections import Counter
    dominant, dominant_count = Counter(non_companion).most_common(1)[0]

    if dominant_count < 3 or len(dominant) > 120:
        return False

    # Every non-companion text must be either the dominant text, or a
    # substring / superset of it (e.g. "" ⊂ "")
    return all(dominant in t or t in dominant for t in non_companion)


# ── Light-grey BT block removal (Fix for 2.PDF) ──────────────

_RG_RE   = re.compile(r"([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+rg")
_G_RE    = re.compile(r"([\d.]+)\s+g\b")
_LIT_RE  = re.compile(r"\(((?:[^()\\]|\\.)*)\)")
_GREY_THRESHOLD = 0.78   # captures 0.886; leaves chart greys (≤ 0.70) alone


def _remove_lightgrey_bt_blocks(stream_bytes: bytes):
    """Scan a content stream and remove BT...ET blocks whose fill colour is
    very light grey (all rg channels ≥ 0.78).  Only removes blocks that sit
    at the top level (not nested inside a q/Q graphics state), so chart labels
    inside q blocks are never touched."""
    try:
        text = stream_bytes.decode("latin-1", errors="replace")
    except Exception:
        return stream_bytes, 0

    # We need to track q-depth so we only remove top-level BT blocks.
    # Walk token-by-token using a simple regex split.
    token_re = re.compile(
        r"(BT\b|ET\b|\bq\b|\bQ\b|"          # structural operators
        r"[\d.+-]+\s+[\d.+-]+\s+[\d.+-]+\s+rg\b|"  # r g b rg
        r"[\d.+-]+\s+g\b)",                  # gray g
        re.DOTALL
    )

    removed = 0
    result_parts = []
    pos = 0
    q_depth = 0
    i = 0
    tokens = [(m.group(), m.start(), m.end()) for m in token_re.finditer(text)]

    # We process the stream as raw text, cutting out matching BT…ET spans.
    # Build a list of (start, end) ranges to delete.
    delete_ranges = []

    j = 0
    while j < len(tokens):
        tok, ts, te = tokens[j]

        if tok.strip() in ("q",):
            q_depth += 1
        elif tok.strip() in ("Q",):
            q_depth = max(0, q_depth - 1)
        elif tok.strip() == "BT" and q_depth == 0:
            # Find the matching ET
            bt_start = ts
            et_end = None
            current_color_grey = False
            k = j + 1
            while k < len(tokens):
                t2, ts2, te2 = tokens[k]
                if t2.strip() == "ET":
                    et_end = te2
                    break
                # Check rg colour inside this BT block
                if re.fullmatch(r"[\d.+-]+\s+[\d.+-]+\s+[\d.+-]+\s+rg", t2.strip()):
                    nums = t2.strip().split()
                    try:
                        r, g, b = float(nums[0]), float(nums[1]), float(nums[2])
                        if r >= _GREY_THRESHOLD and g >= _GREY_THRESHOLD and b >= _GREY_THRESHOLD:
                            current_color_grey = True
                    except ValueError:
                        pass
                elif re.fullmatch(r"[\d.+-]+\s+g", t2.strip()):
                    try:
                        gv = float(t2.strip().split()[0])
                        if gv >= _GREY_THRESHOLD:
                            current_color_grey = True
                    except ValueError:
                        pass
                k += 1

            if et_end and current_color_grey:
                # Verify there is actual text content (not an empty block)
                bt_text_raw = text[bt_start:et_end]
                has_text = bool(_LIT_RE.search(bt_text_raw))
                if has_text:
                    delete_ranges.append((bt_start, et_end))
                    removed += 1
            j = k + 1 if et_end else j + 1
            continue

        j += 1

    if not delete_ranges:
        return stream_bytes, 0

    # Reconstruct stream with deleted ranges removed
    out = []
    prev = 0
    for start, end in sorted(delete_ranges):
        out.append(text[prev:start])
        prev = end
    out.append(text[prev:])
    new_text = "".join(out)
    return new_text.encode("latin-1", errors="replace"), removed


def _stream_contains_watermark(stream_bytes: bytes, terms: list) -> bool:
    """True if any watermark term appears anywhere in the stream text."""
    try:
        text = stream_bytes.decode("latin-1", errors="replace").upper()
    except Exception:
        return False
    return any(t in text for t in terms)


def _stream_is_only_watermark(stream_bytes: bytes, terms: list) -> bool:
    """True only if EVERY literal string inside BT...ET blocks is either a
    recognised watermark term or a companion string (date, ID, etc.).
    This conservative check prevents blanking streams with real content."""
    try:
        text = stream_bytes.decode("latin-1", errors="replace")
    except Exception:
        return False

    bt_blocks = re.findall(r"BT\b(.*?)\bET", text, re.DOTALL)
    if not bt_blocks:
        return False

    terms_upper = [t.upper() for t in terms]

    for block in bt_blocks:
        # Extract all PDF literal strings: (...)
        literals = re.findall(r"\(((?:[^()\\]|\\.)*)\)", block)
        for lit in literals:
            lit_clean = re.sub(r"\\(.)", r"\1", lit).strip()
            if not lit_clean:
                continue
            lit_upper = lit_clean.upper()
            if any(t in lit_upper for t in terms_upper):
                continue          # It's a watermark term — OK
            if is_companion_text(lit_clean):
                continue          # It's a companion string — OK
            return False          # Found a non-watermark string — keep stream

    return True


# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    print(f"PDF Watermark Remover — http://localhost:{port}")
    print(f"PyMuPDF version: {fitz.version[0]}")
    app.run(host="0.0.0.0", port=port, debug=False)
