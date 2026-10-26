<p align="center"><img src="assets/logo-wordmark.svg" alt="Rigveda AI" width="420"></p>

# Rigveda AI

Rigveda AI is a private assistant for the files on your Ubuntu machine. You choose the folders; it reads the documents, slides, spreadsheets, images, audio and video in them, indexes them into a local SQLite database, and answers questions with a locally running Ollama model. Every answer cites its source, down to the page, slide or timestamp.

Nothing is uploaded. Text extraction, OCR, transcription and answers all run on your computer. It only reads the folders you add, never your whole home directory.

For how it fits together, read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). For where it is going, see [docs/ROADMAP.md](docs/ROADMAP.md).

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
| `rigveda facts owner "NAME"` | Say whose personal facts to collect (your full name) |
| `rigveda facts scan` | Look for personal facts in indexed files (also runs after every index) |
| `rigveda facts list` | Show facts and where they came from (`--status pending`, `--show` for full ID numbers) |
| `rigveda facts approve/reject ID…` | Decide on facts; `rigveda facts add KEY VALUE` types one in |
| `rigveda token [--new]` | Show (or replace) the secret the browser extension needs |
| `rigveda write ["WHAT"]` | Draft text from your files; without text, open the writing window |
| `rigveda hotkey [--remove]` | A desktop shortcut (Super+Alt+R on GNOME) that opens the writing window |
| `rigveda drive setup FILE.json / connect / survey / index / status / retry / disconnect` | Connect Google Drive read-only, see what's in it, and index it in place ([guide](docs/GOOGLE_DRIVE.md)) |
| `rigveda listen` | Voice commands: hold Right Ctrl (or tap Super+R) and say e.g. "open Downloads"; it answers aloud (`--autostart` sets it up for every login) |
| `rigveda orb` | A floating assistant on your screen: live stats and a chat (`--autostart` keeps it there after every login) |
| `rigveda serve` | Browser chat at [http://127.0.0.1:8765](http://127.0.0.1:8765), facts review at `/facts`, live status at [`/status`](http://127.0.0.1:8765/status) |

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

## Your facts profile

To fill in forms (Phase 4), Rigveda keeps a profile of your personal details: name, date of birth, place of birth, phone, email, addresses, passport, driver's license and Social Security numbers, employer and so on.

```bash
rigveda facts owner "Your Full Name"   # only passages that mention you are read
rigveda facts scan                      # a couple of minutes for a few hundred files
rigveda serve                           # then open http://127.0.0.1:8765/facts
```

The answer model reads each passage that mentions you and lists the facts it states, with the exact words they came from. A fact is kept only if those words and the value really appear in the file, so the model cannot invent one. Redacted numbers (`***-**-1234`), other people's names (a passport also names your parents) and birth dates in the last ten years are dropped. Spelling variants such as `Rd.` and `RD`, or a phone number with and without `+1`, count as one fact.

Facts are **suggested in forms as soon as they're found**; you don't have to approve anything. When a field has several candidates, the one in the most files (and, for addresses, the newest) comes first. The review page is optional: each fact shows how many files agree and the words it came from, and you can approve one to rank it first, correct it, or reject it so it's never suggested. ID numbers are masked until you click *show*. Your decisions are kept when files change; a rejected fact stays rejected if it's found again. Audio and video are not scanned.

## Form autofill (Chrome extension)

The extension in [`extension/`](extension) suggests facts from your files as you fill in web forms. Click into a field, and a suggestion appears under it with the file it came from: **Tab** fills it and moves to the next field, **↑ ↓** choose between suggestions, **Esc** closes. *Fill this page* in the extension's menu fills every empty field it recognises at once and outlines them in orange for you to check.

It never submits a form, and never fills passwords, one-time codes, payment cards, or fields asking for someone else's details (father's name, emergency contact). Dates follow the field's format (`MM/DD/YYYY`, `DD/MM/YYYY`, date pickers, separate day/month/year boxes), dropdowns get the matching option (`WA` or `Washington`, `IN` for an Indian nationality), and all parts of an address come from the same, newest home.

Install it once:

1. Keep `rigveda serve` running (see *Run in the background* below).
2. In Chrome, open `chrome://extensions`, turn on **Developer mode**, click **Load unpacked**, and choose the `extension` folder.
3. Run `rigveda token`, open the extension's **Options**, paste the token, and click **Save and test**.
4. Try it on [http://127.0.0.1:8765/test-form](http://127.0.0.1:8765/test-form).

The token keeps other programs and web sites from asking for your details: only the extension holds it, and only its background worker talks to the server. Turn suggestions off for a site from the extension's menu. To change the token, run `rigveda token --new`, restart the server, and paste the new one.

## Writing help

In a longer text box (an application question, a cover letter, an email) press **Alt+Shift+R**, or right-click and choose **Write with Rigveda**. In an empty box you get a draft that answers the box's question; with text already there, you get the next sentences from where your cursor is. **Tab** inserts it, **Alt+Shift+R** again gives another, **Esc** discards. The suggestion says which files it drew on. Change the shortcut at `chrome://extensions/shortcuts`.

Drafts use only what your files say. When your name is set (`rigveda facts owner`), only passages that mention you are used, so a textbook or a paper in your folders is never written up as your own work, and film and audio transcripts are never used. Where a detail is missing, the draft leaves a placeholder such as `[your role]` for you to fill in. ID numbers are never included. Add a folder with your résumé, project notes or past letters to get drafts about your work. Always read a draft before sending it.

Outside Chrome, `rigveda hotkey` adds a GNOME shortcut, **Super+Alt+R**, that opens a small writing window: say what you need, then copy the draft and paste it where you were. (GNOME on Wayland doesn't let one app type into another app's text boxes, so a window with Copy is the most it can do.) `rigveda write "a two-line bio"` prints a draft in the terminal.

## Google Drive

Rigveda reads Google Drive in place and read-only, without copying it to this computer. Follow [docs/GOOGLE_DRIVE.md](docs/GOOGLE_DRIVE.md) to create your own free Google key (about ten minutes), then `rigveda drive connect` to sign in and `rigveda drive survey` to see your Drive grouped by type and size, with what indexing would read and skip. The survey fetches names, types and sizes only. `rigveda drive index --background` then reads Drive in place, keeping only the text and a link to each file; documents and emails first, then archives and other files, then recordings, then images.

## Voice

Hold **Right Ctrl** while you speak and let go when you're done, or tap **Super+R** and just talk (it stops when you pause). Say what you want: "open Downloads", "open Chrome and open Fidelity", "open my 2025 tax document", "find my passport", "search for the weather in San Jose", "what time is it", "turn it up", "pause the music", "when does my passport expire?", or just talk. A soft blip and the orb popping mean it's listening; the orb swells and glows with your voice, spins while it thinks and pulses while it answers. Use either key again to cut a reply short and ask something else. It answers aloud with dry, confident humour ("Google Chrome, up and running."), or as a calm Indian sage with `--persona sage` ("Tathaastu. Google Chrome awakens."). Everything you said and heard is in the orb's **Voice** tab.

The microphone is closed until you use a key, and closes again when you let go (or, after Super+R, when you stop talking). Holding Right Ctrl on its own for a quarter of a second counts as talking; with another key, as in Ctrl+C, it's an ordinary shortcut. GNOME never tells apps a key was let go, so hold-to-talk reads the keyboard directly, which needs you in the `input` group: `sudo usermod -aG input $USER`, then log out and back in. That access could read everything you type, so Rigveda only looks for its one key and drops every other key event unseen; nothing is stored or logged. A different key: `talk_key` in the config (`KEY_RIGHTALT`, `KEY_PAUSE`, `KEY_SCROLLLOCK`, `KEY_F13`…). Prefer it always listening? Set `listen = "wake"` in the config (or `rigveda listen --mode wake`), and it hears you hands-free with no name needed: it learns your voice, ignores the TV and other people, ignores what you say while holding Space (dictation for Claude Code) or while another app is recording, and answers only what's said to it. `need_name = true` makes it wait for "Rigved" again. The orb's microphone button mutes it. `rigveda listen --label` and `rigveda listen --train` teach it your voice better ([how it works](docs/ARCHITECTURE.md#listening)). Listening runs on this computer: Silero spots speech, smart-turn hears when you've finished, and Whisper writes it down. "Volume to 60%" sets the apps playing sound, not the speakers, so Rigved is always at full volume.

**Claude for the world, your computer for you.** Set `claude_model = "claude-sonnet-5-5"` and questions about the world, the news and plain conversation go to Claude through your Claude Code login, with web search and no other tool. Anything about you ("my lease", "my passport") is still answered here from your files, and turns answered from your files are never sent to Claude ([details](docs/ARCHITECTURE.md#two-minds)). Simple rules understand common phrasings; for anything else the local model picks from a fixed list of actions (open, find, search, ask your files, talk, time, date, volume, music), so it can never run a command of its own, and it only opens a film or song whose name you said. Your conversations are kept in `~/.local/share/rigveda/voice-history.jsonl` (readable only by you, the last 1,000). The voice is [Kokoro](https://github.com/thewh1teagle/kokoro-onnx), with American, British and Indian voices to choose from; its model (about 340 MB) downloads on first use.

```bash
pip install -e '.[voice]'
rigveda listen                       # listen in this terminal; Ctrl+C stops
rigveda listen --text "open chrome"  # one command, typed instead of spoken (add --dry-run to only see what it would do)
rigveda listen --voices              # hear every voice; choose with --voice am_fenrir, and --persona witty or sage
rigveda listen --autostart           # from every login: the listener (a systemd user service) and the Super+R shortcut
rigveda listen --no-autostart        # remove both
```

## Floating orb

`rigveda orb` puts a small glowing orb in the bottom-right corner of your screen. It stays above other windows and on every workspace. Its three outer arcs turn from green to amber or red if the file watcher, the server or the local models stop. Drag it anywhere (it remembers where), scroll over it to make it bigger or smaller, and click it to open a translucent panel with your index stats, 30 days of activity, any problems, and a chat for asking about your files. Drag the panel's edges or corners to resize it. **Esc** or **–** shrinks it back. Both sizes are remembered. `rigveda orb --autostart` keeps it on screen from every login on: it installs and enables the `rigveda-orb.service` systemd user service, which restarts the orb if it crashes. *Quit* in the panel closes it until your next login. `rigveda orb --no-autostart` removes the service. The same panel opens in a browser at [http://127.0.0.1:8765/orb](http://127.0.0.1:8765/orb).

It needs GTK and WebKitGTK for the system Python (`sudo apt install python3-gi gir1.2-webkit2-4.1`, already on Ubuntu desktops). GNOME on Wayland doesn't let an app stay on top or choose its position, so the orb runs through XWayland, where both work.

## Run in the background

Two systemd user services keep Rigveda running after you log in. The files are in [`contrib/systemd`](contrib/systemd):

```bash
ln -s ~/github/rigveda-ai/.venv/bin/rigveda ~/.local/bin/rigveda
cp contrib/systemd/*.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now rigveda.service rigveda-serve.service
```

`rigveda.service` keeps the index up to date (`rigveda watch`) and `rigveda-serve.service` runs the server for the chat, the facts page and the extension. Logs: `journalctl --user -u rigveda -u rigveda-serve`.

## Browser chat

```bash
rigveda serve
```

Open [http://127.0.0.1:8765](http://127.0.0.1:8765). Answers list their sources below them. The server only listens on your computer and has no login yet, so do not expose it to a network. It refuses requests addressed to any other host name and form posts from other web sites, so a web page you visit cannot read or change your data through it.

## Development

```bash
pip install -e '.[all,dev]'
pytest
```

## Safety notes

- Start with a small folder, such as `~/Documents/notes`.
- The index contains extracted text from your files, so keep it protected.
- Do not index credentials, private keys, or files you wouldn't want included in an AI prompt.
