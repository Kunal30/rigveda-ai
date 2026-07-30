"""Private local filesystem indexing and Ollama-backed answers."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

TEXT = {".c", ".cc", ".cpp", ".css", ".csv", ".go", ".h", ".html", ".java", ".js", ".json", ".md", ".py", ".rb", ".rs", ".rst", ".sh", ".sql", ".toml", ".ts", ".txt", ".xml", ".yaml", ".yml"}
OPTIONAL = {".pdf", ".docx"}
SKIP = {".git", ".hg", ".svn", ".venv", "venv", "node_modules", "__pycache__"}


def db_path(value: str | None) -> Path:
    return Path(value or ".rigveda/rigveda.db").expanduser().resolve()


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript("""
        CREATE TABLE IF NOT EXISTS documents (path TEXT PRIMARY KEY, modified_ns INTEGER NOT NULL, size INTEGER NOT NULL, indexed_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS chunks (id INTEGER PRIMARY KEY, path TEXT NOT NULL REFERENCES documents(path) ON DELETE CASCADE, ordinal INTEGER NOT NULL, text TEXT NOT NULL);
        CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(text);
        CREATE INDEX IF NOT EXISTS chunks_path_index ON chunks(path);
    """)
    return con


def files(root: Path):
    if root.is_file():
        if root.suffix.lower() in TEXT | OPTIONAL:
            yield root
        return
    for path in root.rglob("*"):
        if any(part.startswith(".") or part in SKIP for part in path.parts):
            continue
        if path.is_file() and not path.is_symlink() and path.suffix.lower() in TEXT | OPTIONAL:
            yield path


def extract(path: Path) -> str | None:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError:
            print(f"Skipping {path}: install pypdf to index PDFs.", file=sys.stderr)
            return None
        return "\n".join(page.extract_text() or "" for page in PdfReader(path).pages)
    if suffix == ".docx":
        try:
            from docx import Document
        except ImportError:
            print(f"Skipping {path}: install python-docx to index DOCX files.", file=sys.stderr)
            return None
        return "\n".join(p.text for p in Document(path).paragraphs)
    return path.read_text(encoding="utf-8", errors="replace")


def chunks(text: str, size: int = 1200, overlap: int = 200) -> list[str]:
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    result, start = [], 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            breakpoint = max(text.rfind("\n", start, end), text.rfind(" ", start, end))
            end = breakpoint if breakpoint > start + size // 2 else end
        result.append(text[start:end].strip())
        start = end if end == len(text) else max(end - overlap, start + 1)
    return [item for item in result if item]


def index(db: Path, roots: list[str]) -> tuple[int, int]:
    con, changed, skipped = connect(db), 0, 0
    for raw_root in roots:
        root = Path(raw_root).expanduser().resolve()
        if not root.exists():
            print(f"Not found: {root}", file=sys.stderr); skipped += 1; continue
        for path in files(root):
            try:
                stat = path.stat()
                known = con.execute("SELECT modified_ns, size FROM documents WHERE path=?", (str(path),)).fetchone()
                if known and tuple(known) == (stat.st_mtime_ns, stat.st_size):
                    skipped += 1; continue
                text = extract(path)
                if text is None:
                    skipped += 1; continue
                with con:
                    for row in con.execute("SELECT id FROM chunks WHERE path=?", (str(path),)):
                        con.execute("DELETE FROM chunks_fts WHERE rowid=?", (row[0],))
                    con.execute("DELETE FROM documents WHERE path=?", (str(path),))
                    con.execute("INSERT INTO documents VALUES (?, ?, ?, ?)", (str(path), stat.st_mtime_ns, stat.st_size, int(time.time())))
                    for ordinal, chunk in enumerate(chunks(text)):
                        cursor = con.execute("INSERT INTO chunks(path, ordinal, text) VALUES (?, ?, ?)", (str(path), ordinal, chunk))
                        con.execute("INSERT INTO chunks_fts(rowid, text) VALUES (?, ?)", (cursor.lastrowid, chunk))
                changed += 1
            except (OSError, ValueError) as error:
                print(f"Skipping {path}: {error}", file=sys.stderr); skipped += 1
    con.close()
    return changed, skipped


def search(db: Path, question: str, limit: int) -> list[sqlite3.Row]:
    terms = re.findall(r"[\w'-]+", question)
    if not terms:
        return []
    query = " OR ".join(f'"{term}"' for term in terms)
    con = connect(db)
    rows = con.execute("""SELECT chunks.path, chunks.ordinal, chunks.text, bm25(chunks_fts) score
        FROM chunks_fts JOIN chunks ON chunks.id=chunks_fts.rowid
        WHERE chunks_fts MATCH ? ORDER BY score LIMIT ?""", (query, limit)).fetchall()
    con.close()
    return rows


def ask(question: str, sources: list[sqlite3.Row], model: str, host: str) -> str:
    context = "\n\n".join(f"SOURCE: {r['path']} (chunk {r['ordinal'] + 1})\n{r['text']}" for r in sources)
    prompt = "Answer only from this local-file context; say when it is insufficient and cite paths.\n\n" + context + "\n\nQUESTION: " + question
    data = json.dumps({"model": model, "stream": False, "messages": [{"role": "user", "content": prompt}]}).encode()
    request = Request(host.rstrip("/") + "/api/chat", data=data, headers={"Content-Type": "application/json"})
    try:
        with urlopen(request, timeout=120) as response:
            return json.load(response)["message"]["content"]
    except (URLError, TimeoutError, KeyError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Could not reach Ollama at {host}: {error}") from error


def main() -> None:
    parser = argparse.ArgumentParser(description="Private, local-first filesystem assistant.")
    parser.add_argument("--db", help="Index location (default: .rigveda/rigveda.db)")
    commands = parser.add_subparsers(dest="command", required=True)
    cmd_index = commands.add_parser("index", help="Index selected files or directories.")
    cmd_index.add_argument("paths", nargs="+")
    cmd_search = commands.add_parser("search", help="Search indexed files.")
    cmd_search.add_argument("question"); cmd_search.add_argument("--limit", type=int, default=5)
    cmd_ask = commands.add_parser("ask", help="Ask a local Ollama model about indexed files.")
    cmd_ask.add_argument("question"); cmd_ask.add_argument("--limit", type=int, default=5)
    cmd_ask.add_argument("--model", default="qwen3:4b"); cmd_ask.add_argument("--ollama", default="http://localhost:11434")
    args, db = parser.parse_args(), db_path(parser.parse_args().db)
    if args.command == "index":
        updated, skipped = index(db, args.paths)
        print(f"Index complete: {updated} updated, {skipped} unchanged or skipped. Database: {db}")
        return
    sources = search(db, args.question, args.limit)
    if not sources:
        print("No matching indexed text. Run `rigveda index PATH` first."); return
    if args.command == "search":
        for row in sources:
            print(f"{row['path']} (chunk {row['ordinal'] + 1})\n  {row['text'].replace(chr(10), ' ')[:320]}\n")
        return
    try:
        print(ask(args.question, sources, args.model, args.ollama))
    except RuntimeError as error:
        print(error, file=sys.stderr)
        print("Search still works: `rigveda search \"your question\"`", file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
