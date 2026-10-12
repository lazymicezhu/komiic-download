# Komiic Manga Downloader

[简体中文](README.md) | English

A Komiic manga downloader with a local web interface and a command-line interface. It supports single and batch downloads, resuming interrupted downloads, and CBZ export for readers such as Panels. Chapter and image information comes from the APIs used by Komiic, without scrolling or scraping rendered pages.

## Features

- **Single and batch downloads**: Select chapters, estimate image quota, and append links to a running batch queue.
- **Resumable downloads**: Skip existing images, complete unfinished chapters, check completeness after downloading, and retry failed items.
- **Accounts and quota**: Manage login Cookies, switch accounts, and view remaining quota and its history.
- **Manga and task history**: View local manga previews, mark favorites, and check download task status.
- **CBZ export and copying**: Automatically package complete chapters, then copy them manually or after downloading to a chosen directory.

Supports `komiic.com` and `komiic.cc`. Download only content you are authorized to access, and follow the site's usage rules and access restrictions.

## Quick start

```bash
git clone https://github.com/lazymicezhu/komiic-download.git
cd komiic-download
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python gui_server.py
```

Open [http://127.0.0.1:8787](http://127.0.0.1:8787) in your browser. Keep the server running while using the interface. For later sessions, activate the virtual environment in the project directory and run `python gui_server.py`.

To obtain a Cookie through browser login, install the Playwright browser:

```bash
python -m playwright install chromium
```

Manually pasted Cookies and command-line downloads do not require this browser installation. If the default port is in use, select another with `PORT=8788 python gui_server.py`.

## Web interface

1. In **账号池** (Account pool), add your login Cookie or use browser login to obtain one.
2. Select the site and output directory, enter a manga URL, and fetch its chapters.
3. Select the chapters to download, start the task, and check the log for progress and errors.

For batch downloads, enter one manga URL per line. You can estimate quota before starting and append links while the batch is running. The application processes manga in order, skips complete chapters, and checks for missing chapters afterward.

The account pool is shared between the two sites. When the current account's quota runs out, the application tries other enabled accounts. Quota records update periodically while the server is running.

**历史记录** (History) provides manga previews, chapter fetching, and favorite markers; **下载任务** (Download tasks) shows task status. Favorites help filter the list and do not automatically download updates. History previews use the default download directory, so keep it aligned with your actual output directory.

## Download folders and CBZ

Downloads go into the project's `downloads` directory by default, with images organized by manga and chapter:

```text
downloads/
└── Manga title/
    ├── Manga title_01_19p/
    │   ├── 001.jpg
    │   └── ...
    └── Manga title_panels_cbz/
        └── Manga title_01_19p.cbz
```

A CBZ is generated only when all images in a chapter are complete. Running the same task again skips existing images, so use the same output directory when resuming.

To change the default download location or copy CBZ files to a reader's import directory, create `.komiic_local_paths.json` in the project root:

```json
{
  "downloadDir": "downloads",
  "cbzCopyTarget": "/path/to/CBZ-Inbox"
}
```

| Setting | Description |
| --- | --- |
| `downloadDir` | Default GUI download directory. Relative paths are resolved from the project root; absolute paths are also supported. |
| `cbzCopyTarget` | CBZ copy destination. Replace this with your own absolute path and create the directory first. |

Restart the server after editing this file. Use **复制 CBZ** (Copy CBZ) or the after-download copy option to copy new archives to the target directory. Source files are kept, and disabling copying does not affect automatic chapter packaging.

## Command line

Download an entire manga:

```bash
python komiic_downloader.py "https://komiic.com/comic/1088" -o downloads
```

Download selected chapters. Chapter IDs come from the `/chapter/{id}/` part of chapter URLs:

```bash
python komiic_downloader.py "https://komiic.com/comic/1088" \
  -o downloads --chapter-ids 19836,19837,19838
```

Download a single chapter, or export existing downloads to CBZ:

```bash
python komiic_downloader.py "CHAPTER_URL" -o downloads --single-chapter
python komiic_downloader.py --export-existing-cbz downloads
```

Pass login credentials through an environment variable:

```bash
KOMIIC_COOKIE='PASTE_COOKIE_HEADER_VALUE_HERE' \
python komiic_downloader.py "https://komiic.com/comic/1088" -o downloads
```

| Common option | Description |
| --- | --- |
| `--base-url` | Select `https://komiic.com` or `https://komiic.cc`. When using a `.cc` URL, also specify that base URL. |
| `--max-chapters` | Maximum chapters to download; the default `0` means all chapters. |
| `--chapter-delay` | Delay between chapters in seconds; default `5`. |
| `--image-delay` | Delay between images in seconds; default `1`. |
| `--image-retries` | Immediate attempts per image; default `2`. |
| `--retry-delay` | Base delay for immediate retries in seconds; default `2`. |

Run `python komiic_downloader.py --help` for all options. The standalone CLI does not read the GUI account pool or path settings; specify `KOMIIC_COOKIE` and `-o` explicitly.

## Troubleshooting

- **Download failed or connection reset**: Check the log first. The downloader retries failed images. If errors persist, rerun later with the same directory to resume.
- **HTTP 429 or insufficient quota**: Wait for the rate-limit cooldown or quota reset before continuing. Existing files are kept.
- **CBZ not generated**: Check whether the chapter still has missing images.
- **CBZ copy failed**: Check that the destination is configured, exists, and is writable. Restart the server after changing path settings.

## Local data

Account Cookies, quota records, and download history are stored in `.komiic_gui_config.json`, with Cookies stored in plaintext. Browser login data is stored in `.komiic_playwright_profile/`. These files, local path settings, and downloaded content are excluded from Git. Keep them local, and do not upload or share login credentials.
