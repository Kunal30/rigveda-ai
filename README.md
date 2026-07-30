# Rigveda AI

Rigveda AI is a private, local-first assistant for asking questions about files you choose on your Ubuntu machine. It indexes content into local SQLite, searches it locally, and can use a locally running Ollama model to write answers with source paths.

Nothing is uploaded by this project. You choose every directory that is indexed; it never scans your home directory by default.

## Quick start

Requires Python 3.10+.

```bash
cd ~/github/rigveda-ai
python3 -m venv .venv
source .venv/bin/activate
pip install -e .

# Index only the folder you want the assistant to know.
rigveda index ~/Documents

# Full-text search works without an AI model.
rigveda search "Where is my project proposal?"
```

The index is stored in `.rigveda/rigveda.db`. To use another location, pass `--db /path/to/rigveda.db` before the command.

## Add local AI answers with Ollama

Install and start [Ollama](https://ollama.com), then download a model:

```bash
ollama pull qwen3:4b
rigveda ask "Summarize the project proposal and cite the source file."
```

`ask` sends retrieved excerpts only to Ollama at `http://localhost:11434`; with normal Ollama setup this remains on your computer. Select another local model with `--model MODEL`.

## Browser chat interface

With files indexed and Ollama running, start the local chat server:

```bash
rigveda serve
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000) in your browser. The interface shows source file paths below each answer. The server binds to your computer only by default; it has no login, so do not expose it to an untrusted network. Use `--port 8080` to choose a different port.

## Supported files

Text, Markdown, JSON, YAML, CSV, and common source-code files work with no extra packages. Hidden directories plus dependency/build folders are skipped.

For PDFs or DOCX files, install optional readers in the virtual environment:

```bash
pip install pypdf python-docx
```

Scanned PDFs need OCR before their words can be searched. Re-run `rigveda index PATH` after changes; unchanged files are skipped automatically.

## Safety notes

- Start with a small folder, such as `~/Documents/notes`.
- The index contains extracted file text, so keep its database protected.
- Do not index credentials, private keys, or files you would not want included in an AI prompt.
