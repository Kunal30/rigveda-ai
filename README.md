# Rigveda AI

Rigveda AI is a private assistant for the files on your Ubuntu machine. You choose the folders; it reads the documents, slides, spreadsheets, images, audio and video in them, indexes them into a local SQLite database, and answers questions with a locally running Ollama model. Every answer cites its source, down to the page, slide or timestamp.

Nothing is uploaded. Text extraction, OCR, transcription and answers all run on your computer. It only reads the folders you add, never your whole home directory.

See [docs/ROADMAP.md](docs/ROADMAP.md) for where the project is going, including browser form autofill.

## Quick start

Requires Python 3.11+, and [Ollama](https://ollama.com) for images and answers.

```bash
cd ~/github/rigveda-ai
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[all]'          # or pick extras: .[docs], .[media], .[watch]

ollama pull qwen3:14b            # answers (qwen3:4b for smaller GPUs; set `model` in the config)
ollama pull qwen2.5vl:7b         # reads images and scanned PDFs
ollama pull bge-m3               # meaning search

rigveda folders add ~/Documents  # choose what Rigveda knows about
rigveda index                    # index the chosen folders
rigveda search "passport renewal"
rigveda ask "When is my passport appointment?"
```

To keep the index current as files change, run `rigveda watch` instead of `rigveda index`.

## Commands

| Command | What it does |
|---|---|
| `rigveda folders add/remove/list PATH…` | Choose the folders to index (saved in `~/.config/rigveda/config.toml`) |
| `rigveda index [PATH…]` | Index the chosen folders, or the paths given; unchanged files are skipped and deleted files are dropped |
| `rigveda watch [PATH…]` | Index, then keep watching for new, changed and deleted files |
| `rigveda status` | Show how many files of each kind are indexed |
| `rigveda search "QUESTION"` | Search by meaning and keywords (`--keywords-only` works without Ollama) |
| `rigveda ask "QUESTION"` | Answer with the local model, citing sources |
| `rigveda serve` | Browser chat at [http://127.0.0.1:8000](http://127.0.0.1:8000) |

The index lives at `~/.local/share/rigveda/rigveda.db`, readable only by you. Use `--db PATH` before a command to use another file. Models and the Ollama address are set in the config file.

## How search works

Each file is split into chunks of about 1,200 characters. Every chunk is stored twice: as words, for exact matches such as names, ID numbers and amounts, and as an embedding from `bge-m3`, a list of numbers representing its meaning. A question is matched both ways, the two rankings are merged (meaning counts double), and duplicate copies of the same text are shown once. That is how "What is my monthly rent?" finds "$ 2605.00 per month for rent" although the words differ. `rigveda ask` passes the top 12 chunks to the answer model.

Filled-in PDF forms, such as e-signed leases, are read by word position, so typed-in values stay on the line with their labels.

## Supported files

| Kind | Extensions | How it's read | Cited as |
|---|---|---|---|
| Text and code | `.txt .md .csv .json .yaml .py …` | directly | chunk |
| PDF | `.pdf` | text layer; scanned pages are read by the vision model | page |
| Word | `.docx` | paragraphs and tables | chunk |
| PowerPoint | `.pptx` | slide text, tables and speaker notes | slide |
| Excel | `.xlsx` | each sheet as rows | sheet |
| Images | `.png .jpg .jpeg .webp .gif .bmp .tif` | vision model transcribes text and describes the picture | image |
| Audio | `.mp3 .wav .m4a .flac .ogg .opus .aac .wma` | faster-whisper (`large-v3`) on the GPU, CPU fallback | timestamp |
| Video | `.mp4 .mkv .mov .webm .avi .m4v` | soundtrack transcribed like audio | timestamp |

Audio and video need `ffmpeg` (`sudo apt install ffmpeg`). Whisper downloads its model (~3 GB) the first time it runs. Hidden folders, `node_modules`, virtual environments and Office lock files (`~$…`) are skipped.

If Ollama isn't running, images are skipped and retried on the next `index`. Everything else still works.

## Browser chat

```bash
rigveda serve
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000). Answers list their sources below them. The server only listens on your computer and has no login, so do not expose it to a network.

## Development

```bash
pip install -e '.[all,dev]'
pytest
```

## Safety notes

- Start with a small folder, such as `~/Documents/notes`.
- The index contains extracted text from your files, so keep it protected.
- Do not index credentials, private keys, or files you wouldn't want included in an AI prompt.
