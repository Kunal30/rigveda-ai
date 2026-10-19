# Connect Google Drive

Rigveda reads your Google Drive **in place and read-only**. It never copies your Drive to this computer and can't change, share or delete anything in it. This first step only *looks*: it lists file names, types and sizes so you can see what's there before anything is read.

Google requires every app that reads Drive to have its own sign-in key, and only you can create one for your account. It takes about ten minutes and costs nothing.

## 1. Create a project

1. Open [console.cloud.google.com/projectcreate](https://console.cloud.google.com/projectcreate) and sign in with the Google account whose Drive you want to connect.
2. **Project name:** `Rigveda AI`. Leave *Location* as *No organization*.
3. Click **Create**, and make sure the new project is selected in the project picker at the top of the page.

## 2. Turn on the Drive API

1. Open [the Google Drive API page](https://console.cloud.google.com/apis/library/drive.googleapis.com).
2. Click **Enable**.

## 3. Describe the app

1. Open [Google Auth Platform](https://console.cloud.google.com/auth/overview) and click **Get started**.
2. **App name:** `Rigveda AI`. **User support email:** your email. Click **Next**.
3. **Audience:** choose **External**. Click **Next**.
4. **Contact information:** your email. Click **Next**, tick the agreement, and click **Create**.

## 4. Publish it, so Google doesn't sign you out every week

1. Open [Audience](https://console.cloud.google.com/auth/audience).
2. Under *Publishing status*, click **Publish app**, then **Confirm**. It should say **In production**.

While an app is in *Testing*, Google ends its sign-ins after seven days. Publishing doesn't make the app public or list it anywhere; nobody can use it without your key. Google may say the app needs verification. That's for apps offered to other people, and you can ignore it.

## 5. Create the key

1. Open [Clients](https://console.cloud.google.com/auth/clients) and click **Create client**.
2. **Application type:** **Desktop app**. **Name:** `Rigveda desktop`. Click **Create**.
3. In the window that appears, click **Download JSON**. The file is named like `client_secret_….json`.

## 6. Connect Rigveda

```bash
rigveda drive setup ~/Downloads/client_secret_*.json   # moves the key somewhere only you can read
rigveda drive connect                                    # opens Google sign-in in your browser
```

In the browser:

1. Choose your account.
2. Google says **"Google hasn't verified this app"**. That's expected for an app only you use. Click **Advanced**, then **Go to Rigveda AI (unsafe)**.
3. Tick **See and download all your Google Drive files** and click **Continue**. Without the tick, Rigveda gets no access.
4. The tab says *Rigveda AI is connected*. Close it.

## 7. See what's in your Drive

```bash
rigveda drive survey
```

This lists every file you can see in Drive (yours and shared with you, not the bin) and groups them by type, with counts and sizes and what indexing would do with each group: **read** (documents Rigveda can index today), **optional** (images, if you want text read from scans), **not yet** (old `.doc/.xls/.ppt`) or **skipped** (video, audio, archives). Only names, types and sizes are fetched. Nothing is downloaded, and nothing is stored.

## 8. Index it

```bash
rigveda drive index --background
```

Rigveda reads your Drive **in place**: each file is downloaded to a private temporary folder (`~/.cache/rigveda/drive`, readable only by you), read, and deleted straight away. Only the text and a link back to Drive are kept, in Rigveda's index. Answers cite Drive files as `Google Drive: name`.

Files are read in this order, so the most useful ones are searchable first:

| Order | What | How it's read |
|---|---|---|
| 1 | Google Docs, Sheets and Slides; PDFs; Word, Excel and PowerPoint; text; emails; subtitles | Docs are exported as Word/Excel/PowerPoint (as plain text if Google's 10 MB export limit is hit); emails with their attachments |
| 2 | Archives, old Office files, everything else | zip/jar/apk/tar/gz are opened and each file inside is read by type (two levels deep at most); `.doc/.xls/.ppt` go through LibreOffice; files of unknown type give up whatever readable text they hold |
| 3 | Audio and video | speech transcribed by Whisper on the GPU |
| 4 | Images | text and contents described by the vision model |

It keeps going after you close the terminal and can be stopped at any time with `systemctl --user stop rigveda-drive-index`. The next `rigveda drive index` carries on where it stopped and also picks up files added, changed or deleted in Drive since. `rigveda drive status` shows progress, and `rigveda drive retry` tries failed files again. Use `--only documents` (or `other`, `media`, `images`) to read one group.

Files Google won't let anyone download (some shared files) and Google Forms can't be read; they're listed as failed or skipped.

## What's kept, and how to undo it

| File | Holds |
|---|---|
| `~/.config/rigveda/google-client.json` | your key from step 5, moved out of Downloads so it isn't indexed (readable only by you) |
| `~/.config/rigveda/google-token.json` | the read-only sign-in, renewed automatically (readable only by you) |
| `~/.local/share/rigveda/rigveda.db` | the text of indexed Drive files and each file's progress (`drive_files`) |

- `rigveda drive status` shows which account is connected.
- `rigveda drive disconnect` revokes Rigveda's access at Google and deletes the sign-in. You can also remove it at [myaccount.google.com/permissions](https://myaccount.google.com/permissions).
- Deleting the project in the Cloud console ends everything for good.

Rigveda asks only for `drive.readonly`. Google's servers see the requests for the file list, as they would for any Drive app. Your questions and Rigveda's answers never go to Google.
