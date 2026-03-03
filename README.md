# PDF Watermark Remover

A Flask + PyMuPDF web app that automatically detects and removes watermarks from PDF files — no configuration needed.

**Live demo:** *(add Railway URL after deployment)*
**GitHub:** https://github.com/avikmodak01/pdf-watermark-remover

---

## Features

- Auto-detects tiled personal watermarks (e.g. repeated name/ID stamps)
- Removes light-grey embedded watermark blocks
- Removes standard keyword watermarks (DRAFT, CONFIDENTIAL, etc.)
- Removes transparent XObject overlays
- Removes annotation-based watermarks
- Preserves all legitimate document content
- Clean dark-themed UI with before/after PDF preview

---

## Project Structure

```
pdf-watermark-remover/
├── server.py              # Flask backend — all removal logic lives here
├── templates/
│   └── index.html         # Frontend UI (served by Flask)
├── requirements.txt       # Python dependencies
├── Procfile               # Railway/Heroku startup command
└── .gitignore
```

---

## How It Works — Detection Strategies

The backend runs 4 strategies on every page, in order:

### Strategy 1a — Keyword stream blanking
Blanks an entire content stream if:
- It contains a known watermark keyword (DRAFT, CONFIDENTIAL, SAMPLE, etc.), **and**
- Every PDF string literal inside the stream is either a keyword or a companion string (date, ID)

This is the most conservative check — it will never blank a stream that has real text in it.

### Strategy 1b — Tiled personal watermark (`_is_tiled_watermark_stream`)
Handles patterns like `AVIKMODAK (204946)` repeated 100+ times across the page.

Detection criteria (all must hold):
- Stream has ≥ 3 BT blocks
- One dominant short text appears ≥ 3 times
- Every other non-empty, non-companion text is a substring or superset of the dominant text

This is **name/number agnostic** — works for any person's watermark, not just a hardcoded name.

```
Example stream pattern detected:
  BT ... (JOHN DOE \(789012\)) Tj ET   ← repeated 113 times
  BT ... (JOHN DOE) Tj ET              ← partial (substring match)
  BT ... (Mar-03-2026 18:07:17) Tj ET  ← companion timestamp → ignored
```

### Strategy 1c — Light-grey BT block removal (`_remove_lightgrey_bt_blocks`)
Handles watermarks embedded as very light grey text inside the main content stream (not a separate stream).

Detection criteria:
- BT block is at top level (not inside a `q`/`Q` graphics state save)
- Fill colour (`rg` or `g` operator) has all channels ≥ 0.78
- Block contains at least one text literal

This is **text-content agnostic** — works for any light-grey watermark regardless of what it says.
Threshold 0.78 is chosen to capture near-white grey (e.g. `0.886 rg`) while leaving chart/graph labels (typically ≤ 0.70) untouched.

### Strategy 2 — Text layer redaction
Uses PyMuPDF's `page.get_text("rawdict")` to find rendered text lines matching the keyword vocabulary, then redacts them with `page.add_redact_annot()`.

### Strategy 3 — Annotation deletion
Iterates `page.annots()` and deletes any annotation whose `content` or `title` matches the keyword vocabulary.

### Strategy 4 — Transparent XObject removal
Scans Form XObjects referenced from the page. Blanks any that have:
- `/ca 0.0` – `0.3` (low fill opacity), or
- `/Multiply` blend mode

---

## Companion Text Patterns

These patterns appear alongside personal watermarks (timestamps, doc IDs) and are **ignored** during stream analysis so they don't prevent watermark detection:

| Pattern | Example |
|---------|---------|
| `^\w{3}-\d{2}-\d{4}` | `Mar-03-2026` |
| `^\d{1,2}-\d{2}-\d{4}` | `03-03-2026` |
| `^\d{1,2}:\d{2}(:\d{2})?` | `18:07:17` |
| `^\d{4,8}$` | `204946` |
| `^\d{2}/\d{2}/\d{4}` | `03/03/2026` |
| `^\d{4}-\d{2}-\d{2}` | `2026-03-03` |
| `^Page\s+\d+` | `Page 1` |
| `^\d+\s*of\s*\d+` | `1 of 10` |

---

## Real-World Test Cases

### 1.pdf — RBI exam document (4 pages, 406 KB)
- **Watermark type:** Tiled personal stamp
- **Pattern:** Separate content streams (xref 33/50/57/70, ~9 KB each) with 113 BT blocks per page
- **Content:** `(AVIKMODAK \(204946\))` at 45° rotation matrix `0.70711 0.70711 -0.70711 0.70711`
- **Strategy used:** 1b (tiled stream detection)
- **Result:** 4 streams blanked, 0 false positives

### 2.PDF — Salary slip (1 page, 19 KB)
- **Watermark type:** Light-grey embedded BT block
- **Pattern:** First BT block in main stream uses `0.886 0.886 0.886 rg` with text `(00204946)Tj`
- **Strategy used:** 1c (light-grey block removal)
- **Result:** 23 watermark occurrences removed, 1 legitimate account number preserved

---

## API

### `GET /health`
Returns backend status and PyMuPDF version.
```json
{ "status": "ok", "pymupdf": "1.27.1" }
```

### `POST /remove-watermark`
**Request:** `multipart/form-data`
- `file` — PDF file
- `options` — JSON string with flags:
  ```json
  {
    "removeAnnotations": true,
    "removeXObjects": true,
    "removeTransparent": true,
    "keywords": ["CUSTOM TERM"]
  }
  ```

**Response:** Cleaned PDF binary
**Headers:**
- `X-Stats` — JSON with removal counts:
  ```json
  {
    "streamsRemoved": 4,
    "blocksRemoved": 4,
    "redacted": 0,
    "annotationsRemoved": 0,
    "xobjectsRemoved": 0
  }
  ```
- `X-Logs` — JSON array of per-page log messages

---

## Local Setup

```bash
# Install dependencies
pip install -r requirements.txt

# Run the server
python server.py

# Open in browser
http://localhost:5001
```

---

## Deployment (Railway)

1. Push to GitHub (already done)
2. Go to [railway.app](https://railway.app) → **New Project** → **Deploy from GitHub repo**
3. Select `avikmodak01/pdf-watermark-remover`
4. Railway auto-installs `requirements.txt` and runs the `Procfile`
5. Go to **Settings** → **Generate Domain** to get your public HTTPS URL

The `PORT` environment variable is automatically set by Railway and picked up by `server.py`.

---

## Dependencies

| Package | Purpose |
|---------|---------|
| `PyMuPDF >= 1.24.0` | PDF parsing, content stream access, redaction |
| `flask >= 3.0.0` | Web framework |
| `flask-cors >= 4.0.0` | CORS headers for `X-Stats` / `X-Logs` |

---

## Frontend

The UI is a single-file dark-themed app (`templates/index.html`) served by Flask. It:
- Calls `GET /health` on load to confirm backend availability
- Falls back to a client-side JS implementation if the backend is unreachable
- Shows before/after PDF preview using PDF.js
- Displays per-page removal logs from `X-Logs` header
- Supports drag-and-drop and custom keyword input
