# Rigveda AI roadmap

Goal: a personal AI whose knowledge base is a folder you choose. It understands
documents, slides, images, audio and video in that folder, answers questions about
them, and suggests values while you fill in forms in the browser.

Decisions so far:

- **Fully local.** Parsing, transcription, embeddings and the language model run on
  this machine (RTX 5090, 32 GB VRAM) through Ollama and Whisper. No document text
  leaves the computer.
- **Browser forms first.** Suggestions appear through a Chrome extension. A desktop-wide
  hotkey popup may come later; GNOME on Wayland does not let one app read another
  app's text fields, so inline suggestions everywhere are not practical.

## How it fits together

```
 watched folder ──► ingest ──► extract text ──► chunk ──► embed ──┐
 (PDF, PPTX, video…)  │                                           ▼
                      └──► extract personal facts ──►  SQLite: documents, chunks,
                                                       FTS5, vectors, facts
                                                              │
        Chrome extension ──► /suggest ──► facts lookup ───────┤
        browser chat UI  ──► /ask     ──► hybrid search ──► local LLM (Ollama)
```

This is retrieval-augmented generation (RAG) for questions, plus a structured
**facts profile** for forms. Form fields need answers that are fast (under ~300 ms)
and exact (a passport number cannot be paraphrased), which a full RAG round trip
does poorly. Facts are pulled out of documents once at indexing time, you review
them, and form suggestions mostly become a lookup.

## Components

### 1. Ingestion: every file becomes text

| Type | How | Notes |
|---|---|---|
| Text, Markdown, code, CSV, JSON | read directly | already works |
| PDF | PyMuPDF | per-page text; OCR fallback for scanned pages |
| Scanned PDF, images | `qwen2.5vl` via Ollama | OCR plus a description of photos and diagrams |
| DOCX | python-docx | paragraphs and tables |
| PPTX | python-pptx | slide text **and speaker notes**, per slide |
| XLSX | openpyxl | sheet by sheet, as rows |
| Audio, video | ffmpeg → faster-whisper `large-v3` on GPU | timestamped transcript; citations point to `video.mp4 @ 12:34` |
| Video frames (later) | keyframes → vision model | for slides or screens shown in recordings |

Each chunk keeps its location (page, slide, timestamp) so answers can cite precisely.
A file watcher (`watchdog`) re-indexes changed files in the background.

### 2. Search: keyword plus meaning

- Keep SQLite FTS5 (BM25) for exact terms such as names and ID numbers.
- Add embeddings (`bge-m3` via Ollama, multilingual) stored with `sqlite-vec`.
- Merge both result lists with reciprocal rank fusion.
- Everything stays in a single SQLite file, with no separate database server.

### 3. Answers

- `qwen3:14b` (or `qwen3:32b`, which fits in 32 GB VRAM) for chat answers with citations.
- `qwen3:4b` for low-latency jobs such as matching form fields.

### 4. Personal facts profile

- At index time, a model scans likely documents (IDs, resumes, bills, letters) and
  extracts facts as `(key, value, source file, page, confidence)`. Examples:
  `full_name`, `date_of_birth`, `address.current`, `passport.number`, `employer`.
- Facts stay **pending** until you approve or edit them in a review page. Only
  approved facts are suggested in forms.
- Conflicts, such as two addresses, are kept with dates, so the newest can be
  preferred and older ones are still shown.

### 5. `/suggest` API and Chrome extension

- The content script notices focus on `input`, `textarea` and `select`, and builds a
  field description from `<label>`, `aria-label`, `placeholder`, `name`,
  `autocomplete` and nearby text.
- The server matches it against facts (exact key, then synonyms, then a small model)
  and returns up to 3 suggestions, each with its source.
- The suggestion shows as a dropdown under the field: **Tab** accepts it, **Esc**
  dismisses it. A "Fill this form" button fills all matched fields for review, and
  **never submits** the form.
- Later: grounded completions inside long text boxes, such as cover letters or
  "describe your experience".

### Security, because a local server is reachable from any web page

- Every request must carry a secret token, generated on first run and pasted into
  the extension. Without it, any website's JavaScript could query
  `127.0.0.1:8000` and read your documents.
- CORS allows only the extension's origin. The server binds to `127.0.0.1` only.
- Never suggest into password fields or card-number/CVV fields; per-site off switch.
- The index database holds sensitive text: keep it under `~/.local/share/rigveda`
  with `0600` permissions, and optionally encrypt it with SQLCipher later.

## Phases

| Phase | Deliverable | Done when |
|---|---|---|
| 0. Foundation ✅ | split `cli.py` into modules (`extract/`, `store`, `search`, `llm`); pytest; fix `.gitignore`; config file for the watched folder | tests pass, `rigveda index` behaves as before |
| 1. Multimedia ingestion ✅ | PDF, PPTX, XLSX, images, audio and video extractors; file watcher | a video and a slide deck are searchable, with timestamp and slide citations |
| 2. Semantic search | embeddings + hybrid ranking; better chat answers | finds the right file for a paraphrased question |
| 3. Facts profile | extraction + review page | name, DOB, address, IDs appear for approval with their source |
| 4. Form autofill | `/suggest` with token auth + Chrome extension | a test form gets correct suggestions accepted with Tab |
| 5. Writing help | grounded completions in text areas; optional desktop hotkey popup | |

## Models to pull

```bash
ollama pull qwen3:14b      # answers
ollama pull bge-m3         # embeddings
ollama pull qwen2.5vl:7b   # OCR and image understanding
# faster-whisper downloads large-v3 on first use (~3 GB)
```
