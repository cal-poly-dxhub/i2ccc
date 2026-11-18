# Architecture

i2cc-scraper turns a page that Internet2 staff submit into stored, change-tracked content for a
RAG pipeline: the page's text, plus every document and recording it links to, each with a small
metadata record saying where it came from. This document explains how the code is organised and
why. For usage, see the [README](../README.md).

Contents

1. [The shape of the system](#1-the-shape-of-the-system)
2. [Data model](#2-data-model)
3. [Finding things: the scraper](#3-finding-things-the-scraper)
4. [Fetching things: the downloader](#4-fetching-things-the-downloader)
5. [Storing things: keys, records, change detection](#5-storing-things-keys-records-change-detection)
6. [Failure model](#6-failure-model)
7. [Network safety](#7-network-safety)
8. [Configuration](#8-configuration)
9. [Running it distributed (Step Functions)](#9-running-it-distributed-step-functions)
10. [Testing](#10-testing)
11. [Extending it](#11-extending-it)
12. [Known gaps and decisions](#12-known-gaps-and-decisions)

---

## 1. The shape of the system

Two halves, joined by plain data:

```
 staff-submitted URL
        │
        ▼
 ┌───────────────┐   CrawlResult list      ┌───────────────────────────────────────────────┐
 │  scraper.py   │ ──────────────────────► │ downloader/                                   │
 │  FIND         │   pages (text)          │ FETCH + STORE                                 │
 │  crawl pages, │   assets: Drive,        │ plan → dedupe → expand → ingest, per item     │
 │  classify     │   YouTube, Panopto,     └─────────────────────┬─────────────────────────┘
 │  links        │   website files                               │ files + Record
 └───────────────┘                                               ▼
                                                    ┌──────────────────────┐
                                                    │ store.py  (Store)    │  LocalStore now;
                                                    │ key → files          │  an S3 store
                                                    │     + .metadata.json │  implements the same
                                                    └──────────────────────┘  four methods
```

### Layers (imports point downward)

```
 entry points    i2cc-scrape (scraper.main)    i2cc-download (downloader/cli.py)    ingest_utils/scraper/tui.py (dev only)
                        │                                   │
 orchestration          │                   downloader/plan.py   crawl_into_store, dedupe, download_all
                        │                   downloader/api.py    ingest, save_page, download, FETCHERS
                        │                                   │
 fetchers               │               youtube.py   panopto.py   files.py   drive.py (+ expand)
                        │                                   │
 download machinery     │               commit.py  (keys, new/updated/unchanged, fetch_versioned)
                        │               http.py    (streaming, conditional requests, filenames)
                        │               errors.py  (Reason, TransientError, classify_error)
                        │               models.py  (Options, Source, DownloadResult, Context)
                        │               google_urls.py
                        │                                   │
 foundations     scraper.py (fetch, new_session, crawl)   store.py   drive_api.py
                 url_classifier.py   config.py   netguard.py   fileio.py   logs.py
```

Two import cycles exist on purpose and are broken with imports inside functions:
`drive.py → api.py` (a folder fetch calls `ingest()`), and `models.Source.kind → api.FETCHERS`.

### Design principles

- **Plain data between steps.** `CrawlResult`, `Source`, `DownloadResult` and `Record` are
  dataclasses that survive `dataclasses.asdict` → JSON → `Source.of()`. Any step can therefore run
  in a separate process or Lambda.
- **Identity comes from the URL or ID, never from a title.** Titles change; IDs don't. This
  makes "have we seen this?" a single lookup and prevents duplicate content.
- **Cheapest check first.** Use the source's own version marker if it has one; otherwise
  download and compare a hash; never rewrite what hasn't changed.
- **Failures are data.** Permanent failures come back as results with a machine-readable reason.
  Only temporary ones raise, so an orchestrator can retry exactly those.
- **Everything configurable is passed in.** No module searches the disk for settings or
  credentials; callers build a `Config`, a `DriveClient` and a `Context`.

---

## 2. Data model

| Type | Defined in | Produced by | Meaning |
|---|---|---|---|
| `CrawlResult` | `scraper.py` | the crawl | One URL found: `url`, `type`, `depth`, `found_on` (a single page), `title`, `context`, `text`, `html`, `error`, `key` |
| `Source` | `downloader/models.py` | `Source.of(...)`, `expand()`, `dedupe()` | One *work item* for the downloader: `url`, `type`, `found_on` (a **list**), `link_text`, `context`, `folder_path` |
| `DownloadResult` | `downloader/models.py` | every fetcher | `status` (`new`/`updated`/`unchanged`/`failed`), `key`, `files`, `title`, `error`, `reason`, `children` |
| `Record` | `store.py` | `_commit`/`_unchanged` | The stored metadata (see below) |
| `Options` | `downloader/models.py` | CLI flags / caller | `export_format`, `original`, `max_height` (360), `audio_only`, `captions`, `force` |
| `Context` | `downloader/models.py` | one per run | Store, options, config, HTTP session, optional Drive client, temp work directory, optional `on_download` callback (see below) |

**`Context.on_download`** is called just before file bytes are fetched from a source (never for a
metadata check or a "not modified" answer), so a caller can tell a real download from a skip. The
TUI uses it to switch a row from "Checking" to "Downloading".

**`CrawlResult.type`** is one of the asset types (`youtube`, `panopto`, `google_drive`, `file`),
or `page` (has document links), `page_text` (no document links), `failed` (couldn't be loaded),
or `not_fetched` (the crawl stopped before reaching it).

**`Source.of(x)`** accepts a URL string, a `CrawlResult` or its dict, or a `Source`'s own dict.
It normalises `found_on` (a string becomes a one-item list) and reads a `CrawlResult`'s `title`
as `link_text`. **`Source.kind`** says which fetcher handles it: the `type` if a fetcher is
registered for it in `api.FETCHERS`, else whatever `classify_url()` makes of the URL.

**`Record`** fields:

| Field | Meaning |
|---|---|
| `key` | Where it lives in the store, e.g. `google_drive/1epNAa9…` |
| `source_url` | The URL it was *first* stored from |
| `type` | `page`, `page_text`, `google_drive`, `youtube`, `panopto`, `file` |
| `files` | Store names of the content files, main file first |
| `content_sha256` | SHA-256 of the main file — changes iff the content does |
| `title` | The item's own title (page title, Drive name, video title) |
| `link_text` | The text of the link it was found through |
| `context` | The heading above a *page's* link to it (e.g. the session name) |
| `folder_path` | The Drive folder it was found in (e.g. `Slides 2026 / Day 2`) |
| `found_on` | Every page or folder linking to it, in discovery order |
| `content_type` | MIME type of the main file |
| `source_version` | The source's cheap change marker (see §5) |
| `fetched_at` | When the content was downloaded (UTC) |
| `captions_checked_at` | Recordings: when captions were last looked for after the download (`''` = not since) |
| `links` | Pages: store keys of the documents/videos it linked to when last saved (`null` = not recorded) |
| `unlinked_at` | When the last page linking to this item stopped linking to it (`''` = still linked, or never linked from a page) |
| `is_subscriber_content` | Controls badge display in RAG UI: `true` for subscriber content (blue "CICP"), `false` for public (gray "Public") |
| `parent_folder_name` | Name of parent collection (e.g. `Day 2` from `Slides 2026 / Day 2`), stored for S3 metadata |
| `parent_folder_url` | Link to parent folder (e.g. Drive folder URL), stored for S3 metadata |
| `relative_start_time` | Video chunks: offset in seconds from original video start; `null` for non-chunked content |
| `schema_version` | Currently 1; unknown fields in a newer record are ignored when reading |

Fields a source doesn't use keep their defaults (`null` / `''`). Records written before a field
existed read back with the default, so adding fields never forces a rewrite.

`context` and `folder_path` are separate on purpose: an item found both ways (linked from the
schedule page *and* listed in a Drive folder) keeps both.

---

## 3. Finding things: the scraper

`scraper.py` is the "find" half. It reads HTML; it never downloads documents.

### Three levels of API

| Function | Does |
|---|---|
| `scrape(url)` | One page → `ScrapeResult` (text, html, and the assets it links to) |
| `find_urls(url)` / `extract_urls(html, url)` | One page → the important URLs it links to |
| `crawl(url, depth)` / `crawl_for_ingest(url, depth)` | Breadth-first crawl. `crawl` returns URLs; `crawl_for_ingest` returns a `CrawlResult` for each, saying what ingestion it needs |

### What counts as an asset

`url_classifier.classify_url(url)` is a pure function that returns one of four types or `None`:

| Type | Recognised as |
|---|---|
| `youtube` | a single video on any YouTube host (`youtube.com` and subdomains such as `m.`/`music.`, `youtube-nocookie.com`, `youtu.be`): `/watch?v=…`, `/embed/`, `/shorts/`, `/live/`, `/v/`. Playlists, channels, search and home pages are **not** videos |
| `panopto` | `*.panopto.com` / `.panopto.eu` `…/Pages/Viewer.aspx?id=…` or `Embed.aspx` |
| `google_drive` | `docs.google.com/{presentation,document,spreadsheets,file}/d/…`, `drive.google.com/file/d/…`, folders, `/open?id=`, `/uc?id=` |
| `file` | any other URL whose path ends in a known extension (pdf, pptx, docx, mp4, …) — **or** whose server answered with a document `Content-Type` during a crawl |

`asset_id(url)` gives a stable identity (`youtube:<id>`, `panopto:<uuid>`, `google_drive:<id>`),
so different spellings of one link compare equal. `url_key(url)` is the equivalent for ordinary
pages (lowercase host, no `www.`, no trailing slash, no fragment).

### The crawl loop (`_crawl`)

```
queue = [start]                          found = {url_key of everything seen}
while queue:
    page = next
    stop if pages_fetched >= max_pages  or  elapsed (+delay) >= time_limit
        → mark this page and all still-queued pages NOT_FETCHED, forget them in `found`, stop
    sleep(delay) between requests
    fetch(page, html_only=True)
        NotHTML + a document MIME type → page.type = FILE      (e.g. /download?id=7 is a PDF)
        NotHTML otherwise / HTTP error / WAF challenge → page.type = FAILED
    parse → page.type = PAGE (has assets) or PAGE_TEXT; page.text/title filled in
    on_page(page) if given, then page.text = page.html = ''    (stored by the callback)
    links  = important links in the main content   (none if page.depth >= max_depth)
    assets = every video/document link anywhere on the page
    add each as a new CrawlResult(depth + 1, found_on = page.url); pages join the queue
```

Details that matter:

- **Depth.** The start page is depth 0. Pages at depth < `max_depth` are fetched and their links
  followed. `crawl_for_ingest` also fetches pages *at* `max_depth` (a page's type is only known
  once read) but doesn't follow their page links; it still reports their documents.
  `crawl()` (URL mode) doesn't fetch the last level at all.
- **`found`.** A set of `url_key`/`asset_id` strings. Pass the same set to several crawls to
  skip repeats. Pages marked `NOT_FETCHED` are removed from it so a later crawl picks them up.
- **Domains.** Only same-domain *pages* are fetched (default: the start URL's domain, plus
  subdomains; override with `allowed_domains`). Assets are returned whatever their domain but
  never fetched here.
- **Limits.** `max_pages` and `time_limit` are checked *before* each request, so one slow
  request (timeout × retries) can overrun a deadline — leave a margin.
- **`on_page`.** Called with each page as soon as it's parsed; the callback stores it. Used by
  `downloader.crawl_into_store` so page content never accumulates in memory. An exception in the
  callback propagates and ends the crawl (pages stored before it stay stored).

### What "important links" means

When following links, only the main content area is searched, and navigation is dropped:

- Content root: the first of `main`, `[role=main]`, `article`, `#main-content`, `#content`;
  site chrome (`nav`, `header`, `footer`, `aside`, ARIA landmarks) is removed from it.
- `is_unimportant(url, page_url)` rejects: the homepage of any site; static files and utility
  pages (`SKIP_EXTENSIONS`, `SKIP_PATHS`: login, search, feeds, tag/category/author archives, …);
  the page linking to itself; and breadcrumbs (ancestors of the current path). Descendants and
  siblings are kept. Assets are never unimportant.

Asset extraction (`extract_assets`) is deliberately broader: it searches the **whole page**, so
no recording in a sidebar is missed, and merges repeated links to the same asset by `asset_id`.
Each asset gets the link text as `title` and, as `context`, the nearest heading above it (up to
200 characters) — for the Cloud Forum abstracts page that's the session name.

### HTTP

`new_session(config)` builds the one HTTP session type used everywhere: automatic retries
(`Config.retries`, exponential backoff with jitter, `Retry-After` honoured but capped) on
connection errors and HTTP 429/500/502/503/504 for GET/HEAD only, the configured User-Agent,
and — unless disabled — the `GuardedAdapter` from `netguard` (§7). `fetch(url, session,
html_only=True)` streams just the headers first, so a link that turns out to be a 500 MB video
isn't downloaded to find out. It also detects AWS WAF browser challenges and raises a clear
error instead of returning an empty page.

---

## 4. Fetching things: the downloader

### Entry points (`downloader/__init__.py` re-exports everything)

| Function | Use |
|---|---|
| `ingest(entry, ctx)` | **The unit of work.** One item → stored, or reported unchanged. Raises `TransientError` for retryable failures |
| `save_page(entry, ctx)` | Store a crawled page (`.txt` with title, URL, extracted text) |
| `expand(entry, ctx)` | A Drive folder → one `Source` per file inside; anything else → itself, no network |
| `dedupe(items, options)` | Collapse items that would share a store key. Pure, no network |
| `crawl_into_store(url, ctx, depth)` | Crawl, storing each page as it's read → `CrawlReport` |
| `download_all(entries, out)` | The whole pipeline in one process |
| `download(url, out)` | Convenience: one URL, never raises for a failed download |

### The dispatch

`api.FETCHERS` maps a type to a function `fetch_x(source, ctx) -> DownloadResult`:

```python
FETCHERS = {YOUTUBE: fetch_youtube, PANOPTO: fetch_panopto,
            GOOGLE_DRIVE: fetch_google_drive, FILE: fetch_file}
```

`ingest()` looks up `Source.kind`, runs the fetcher, and turns exceptions into results or
`TransientError` (§6). Registering another type in `FETCHERS` is enough to support it.

### The shape every fetcher shares: `fetch_versioned`

```
fetch_versioned(ctx, source, kind, key, previous, download, version, title)

  stored Record already has this `version`?  ── yes ──►  _unchanged   (no download at all)
        │ no
  work_dir() ─► download(work)  returns one of
        Downloaded(files, title, content_type, version?)  ─► _commit
        NOT_MODIFIED   (server said 304)                  ─► _unchanged
        None           (couldn't; caller tries another way)
```

A fetcher therefore only states *how to download* and *what version marker applies*. The temp
work directory is deleted afterwards, whatever happens.

### Per-source behaviour

**Website files (`files.py`)** — Stored as `files/<hash of url_key>`. The stored `ETag` (or
`Last-Modified`) is sent back as `If-None-Match` / `If-Modified-Since`; a 304 — or a response
carrying the same validator — means unchanged without downloading. With no validators the file
is downloaded and its hash compared. The title comes from `Content-Disposition`, else the URL;
the extension from the name if valid, else from the MIME type, else `.bin`.

**Google Drive (`drive.py`)** — Two access paths, chosen by whether `ctx.drive` is set:

| | Service account (Drive API) | Public links |
|---|---|---|
| Reaches | public files **and** files shared with the account | only "anyone with the link" |
| Change check | `modifiedTime` (native files) or `md5Checksum` (uploads) → `source_version`; **no download when unchanged** | none; downloads, compares SHA-256 |
| Folder listing | `files.list` | Drive's embeddable folder view (HTML) |

What gets downloaded for a file:

- *Native* Slides/Docs/Sheets/Drawings → exported (`pdf` by default, `xlsx` for Sheets,
  `Options.export_format` overrides).
- *Uploaded* file linked in the Slides/Docs editor (a `.pptx` "opened in Slides", which is most
  Cloud Forum decks) → the public export link first (PDF); if that fails *permanently* (e.g.
  Google's HTTP 432 for oversized exports), fall back to the original file. The fallback is
  remembered in the stored version (`<md5>;format=pptx;export_refused=pdf`), so while the file is
  unchanged the refused export is **not requested again** on every run; a changed file, or
  `--force`, asks again.
- *Plain file link* (`/file/d/…`) or `Options.original` → the original file.
- Forms and Sites have no file form: skipped, not failures.
- `source_version` is `<marker>;format=<fmt>`, so changing `export_format` re-downloads.

**Folders.** `fetch_google_drive` on a folder = `expand()` then `ingest()` each file, summarised
by `folder_result()` (status, `partial`/`error` reason, `children`). `expand()` walks the tree,
follows shortcuts, skips loops and duplicates, and tags each file with `found_on = [its folder]`
and `folder_path`. Listing failures become `failures`; a *temporary* listing failure raises.

**YouTube (`youtube.py`)** — a YouTube link is never crawled: a video link goes straight to the
downloader, and any other YouTube page (playlist, channel, search, home) is refused —
`validate_start_url` raises for a start URL, a link to one found on a page is not followed even
if `youtube.com` is an allowed domain, and `ingest()` reports it as `unsupported` ("must be a
single video"). The video is fetched through yt-dlp, which is given the plain
`https://www.youtube.com/watch?v=<id>` built from the video ID (never the link as typed) with
`noplaylist` set: a link such as `watch?v=X&list=PL…&t=40s` otherwise makes yt-dlp download the
whole playlist. The Record's `source_url` keeps the link as given. Videos are fetched via `yt-dlp`,
at most `Options.max_height` pixels (default **360**).
The format selector prefers a ready-made mp4 (video + audio in one file, needs no ffmpeg), then
separate streams merged by ffmpeg *if installed*, then the lowest quality available. English
captions (`.en.vtt`) are saved too. **YouTube's automatic captions** come in a "rolling" layout that
says every line three times (word-timed in one cue, alone in a 10 ms filler cue, and again at the
top of the next); `captions.clean_vtt()` keeps each line once, with the cue it was spoken in, and
drops the word-timing markup. Checked against YouTube's own structured (`json3`) transcript of a
real Internet2 video: the same 387 words in the same order, from 1,127 in the raw file. Uploaded
(manual) captions have no such tags and are stored exactly as received. `source_version` is `video<=360p` (or `audio`): a stored
video is re-downloaded only if the quality setting changes or `force` is set. **Captions that
appear later** (YouTube's automatic ones take hours) are picked up: a video stored without them
is checked again — captions only, `skip_download`, never the video — at most every
`caption_recheck_hours` (24) and until `caption_recheck_days` (14) after it was downloaded, then
left alone. A failed re-check is logged, not an error (the stored video is fine) and counts as a
check, so a rate-limited or private video is tried once per interval, not on every run. Needs a JavaScript
runtime (`deno`/`node`/`bun`) on `PATH` for YouTube's format list. yt-dlp errors are mapped to
reasons (`blocked`, `access_denied`, `not_found`) or `TransientError` for 429/5xx/timeouts.

**Panopto (`panopto.py`)** — Public sessions only, through the podcast download link
(`/Panopto/Podcast/Download/<uuid>.mp4`). Captions come from `GenerateSRT.ashx` and are stored as WebVTT (`.en.vtt`); a
caption failure is logged but doesn't fail the video. A stored session is never
re-downloaded (unless `force`), but one stored without captions gets the same captions-only
re-check as YouTube. Quality is whatever Panopto serves.

**Pages (`api.save_page`)** — The text file is the *main* file: `Title: …\nURL: …\n\n<text>`.
Because change detection hashes the main file, a page whose HTML changes but whose extracted
text doesn't (a new nonce, a rotated widget) counts as unchanged. The `URL:` header uses the
URL the page was *first* stored from, so reaching it via a `www.` or trailing-slash variant
doesn't register as a change.

*What the page links to* is tracked separately, because the text can stay the same while the
links change (a label "Slides" re-pointed at another deck). The page's Record keeps `links`: the
store keys of the documents and videos on it. Then:

- **Links changed, text didn't** → the page is stored again (status `updated`), so the saved
  HTML isn't stale. The order of links doesn't matter.
- **A link was dropped** → for each item no longer linked, this page is removed from its
  `found_on` (whatever the spelling of the page URL). If no page is left, `unlinked_at` is set;
  it is cleared again when a page links the item. **Nothing is deleted** — whether to remove
  unlinked items is for whoever consumes the store.
- **Only documents and videos count,** not page-to-page links, so a rotating "related posts"
  widget can't make a page look changed on every run.
- **Records from before `links` existed** are backfilled quietly (metadata only, status
  `unchanged`) instead of being rewritten and re-ingested.
- A crawl without link information (an old crawl JSON, or `text_with_links=False`) tracks and
  unlinks nothing.

### Planning (`plan.py`)

`dedupe()` groups items by store key and merges each group: `found_on` is the union in order;
`link_text` and `context` prefer an item found on a page over one from a folder listing;
`folder_path` keeps the longest; and for a Drive file linked both in the editor
(`/presentation/d/X`, exported) and as a file (`/file/d/X`, original), the URL matching
`Options.original` is kept, so the stored format never depends on link order. It runs twice —
before `expand()` and after (folder contents are only known then).

`download_all()` = save pages → `dedupe` assets → `expand` each → `dedupe` again → `ingest` each,
then one summary row per linked folder. A distributed runner performs the same steps itself.

---

## 5. Storing things: keys, records, change detection

### Keys

| Key | Built from |
|---|---|
| `pages/<16 hex>` | SHA-1 of `url_key(page url)` |
| `files/<16 hex>` | SHA-1 of `url_key(file url)` |
| `google_drive/<file id>` | the Drive file ID |
| `youtube/<video id>` | the video ID |
| `panopto/<session uuid>` | the lowercase session ID |

`make_key` validates against `[a-z_]+/[A-Za-z0-9_-]{1,128}`; an ID that doesn't fit (a mangled
YouTube link) becomes an `invalid_url` failure instead of crashing the run. File suffixes must
match `(\.[a-z0-9]{1,10}){1,2}`. Store paths therefore can't contain anything a server or
a user typed.

In a `LocalStore`, content files are `<key><suffix>` (`google_drive/1epNAa9….pdf`,
`youtube/jNQ….en.vtt`); in the `S3Store` they are `<key>/<hash>/<Title><suffix>` (see below). The
record is `<key>.metadata.json`, written after the files so a record only exists once its files do.

### The `Store` interface (`store.py`)

```
get(key)                                  -> Record | None     (record.etag = which version it is)
stage(chunks)                             -> Staged            stream a download in, hashing it
discard(staged)                           -> None              staged content that wasn't put()
put(record, files, keep_other_files=False, expected=UNCHECKED)  -> Record
put_record(record, expected=UNCHECKED)    -> None              replace only the metadata
delete(key)                               -> None
suffix_of(key, name)                      -> str               which suffix a stored name has
```

**Streaming.** Fetchers never write a download to local disk: the HTTP response (or Drive
download, or a YouTube format served as one file) goes straight into `store.stage()`, which
hashes it on the way and returns a `Staged` handle; `put()` accepts it in place of a local path.
`fetch_versioned()` discards staged content that ends up not being stored (unchanged, or a
failed write). Small files the code writes itself (page text, captions) are still local paths.

**Write order.** `put` places the new files, then writes the Record, and only then removes files
the Record no longer lists. A crash can leave an unlisted leftover (removed by the next write to
that key), but never a Record that lists a file that is gone. (The first version deleted the old
file *before* writing the Record: a kill in between left a Record pointing at nothing, which the
next run then reported as `unchanged`, forever.) `delete` follows the same rule in reverse: the
Record first, then the files. All new files are staged beside their destinations before any
existing file is replaced, so a move that fails partway (a full disk) changes nothing and leaves
no partial files.

**Conditional writes.** Several writers may share a store -- overlapping runs, threads. `get()`
tags each Record with a version (`etag`); `put`/`put_record` take `expected=<the Record you
read>` (or `None`: "there must be none yet") and raise `ConflictError`, changing nothing -- not
even moving your files -- if someone wrote it since. The downloader's writes are all of the form
read, merge, write-if-unchanged; on a conflict it re-reads and merges again on top of the other
writer's version (`commit._retry_on_conflict`, up to 8 times, then a `TransientError`). So two
runs adding different pages to one deck's `found_on` both end up in it, and of several runs
creating the same item exactly one creates it and the others find it `unchanged`. `etag` is not
part of a Record's content: it is never written to the metadata file and ignored when comparing.

`LocalStore` implements it on a directory: files move into place via a uniquely named `.part`
file and an atomic rename, and each write (the check, the moves and the Record) happens under a
lock file in the directory, so threads and processes can share it. `tests/scraper/test_store.py` is the
contract every implementation passes (`S3Store` against AWS Emulator): conflicts, the 8-writer
read-merge-write race, one winner among several creators.

### The S3 store (`aws/s3store.py`)

Laid out for i2ccc's ingestion, which ingests every `.txt`/`.vtt`/`.pdf`/`.mp4` in its bucket,
reads the S3 object metadata i2ccc's own uploader writes, and shows the file name as the
citation title:

```
<prefix><key>.metadata.json               the Record (ingestion ignores .json)
<prefix><key>/<hash>/<Title><suffix>      content; <hash> = 12 hex of the main file's SHA-256
<prefix>.staging/<uuid>                   stage(): a multipart upload, 8 MiB parts
<prefix>.runs/<run id>/...                the state machine's working files (§9)
```

- **Names.** The title, folded to safe ASCII (`file_title`), so citations read well; the hash
  folder means new content always gets new object names (racing writers never overwrite each
  other's files; unchanged content keeps its names). A title change alone doesn't rename.
- **Object metadata:** `member-content`, `source-url`, `parent-folder-name`/`-url` (when the
  Record has them), `relative-start-time` (media), plus `scraper-key` and `scraper-file-id`.
  Ingestion copies all of it into every vector it makes from the file.
- **Placing** a `Staged` is a server-side copy from `.staging/` with the metadata; the Record
  is then written with `If-Match` (or `If-None-Match: *` for a new one). A put that loses that
  race deletes what it placed and raises `ConflictError` -- unlike `LocalStore`, which checks
  under a lock before placing anything.
- **Telling ingestion.** Before a write replaces or removes files, the `IngestInvalidator`
  deletes each one's processed-files entry and its vectors (search on `scraper-file-id`, bulk
  delete: OpenSearch Serverless has no delete-by-query). If that fails, nothing has changed
  (`TransientError`: retry). It runs once more after the write, in case ingestion ran meanwhile.
- **Leftovers** of a killed write -- unlisted objects under the key -- are removed by the next
  write to the key once they are older than 30 minutes (younger ones may be another writer's,
  placed but not yet listed). Abandoned `.staging/` uploads expire through a bucket lifecycle
  rule.

### The decision procedure

```
fetcher computes `version` (or '' if the source has no cheap marker)
        │
        ▼
 Record exists, version matches, not --force ───────────────► UNCHANGED  (no download)
        │ otherwise: download into a temp dir
        ▼
 sha256(main file) == Record.content_sha256  and same file suffixes ───► UNCHANGED  (nothing written)
        │ otherwise
        ▼
 store.put(files, new Record)  ───────────────────────────────► NEW (no previous) / UPDATED
```

In both UNCHANGED paths, `_unchanged()` still merges new metadata (a new `found_on` page, a
better `context`, a changed `type`) and rewrites **only the metadata file**, and only if it
actually differs. Rerunning an unchanged crawl therefore writes nothing.

| Source | Cheap marker (`source_version`) | Otherwise |
|---|---|---|
| Drive, API mode | `modifiedTime` / `md5Checksum` + `;format=` | — |
| Website file | `etag=…` / `last-modified=…` | hash compare |
| YouTube | `video<=360p` / `audio` | — |
| Panopto | none (never re-fetched; captions re-checked, see §4) | — |
| Drive, public link | none | hash compare (Google's PDF exports are byte-identical between runs) |
| Crawled page | none (internet2.edu sends no ETag and ignores conditional requests) | hash of extracted text **and** the set of documents/videos it links to |

---

## 6. Failure model

Two exception families (`downloader/errors.py`):

- **`DownloadError(message, Reason)`** — permanent; retrying soon won't help.
- **`TransientError`** — a temporary failure that outlasted the in-process retries.

`classify_error(exc)` maps raw exceptions: HTTP 401/403 → `access_denied`, 404/410 →
`not_found`, 429/5xx/timeouts/connection errors → `TransientError`, TLS errors → permanent,
`UnsafeURLError`/bad URLs → `invalid_url`, Drive API errors by status (Drive reports rate
limits as 403, recognised by message), Google auth errors → `auth`, a full disk (`ENOSPC`) →
`DiskFullError`, and the run running out of time → `DeadlineExceeded` (both `TransientError`s).
Anything else it can't classify (`ValueError`, a permissions error) is a bug or an environment
problem and **propagates out of `ingest()`** -- `download_all()` catches it per item (below),
classifying first, so a full disk or an expired budget from *any* unit (a page save, an
expansion, a fetch) still halts the run.

`Reason`: `unsupported`, `invalid_url`, `not_found`, `access_denied`, `not_downloadable`,
`blocked`, `auth`, `partial`, `unavailable`, `not_fetched`, `timeout`, `disk_full`, `error`.

Result statuses are `new`, `updated`, `unchanged`, `failed` and `deferred`: *not attempted* --
the run ran out of time or disk first, and nothing was done for the item.

What each entry point does with them:

| Call | Permanent failure | Temporary failure |
|---|---|---|
| `ingest()` | `FAILED` result with `reason` | **raises** `TransientError` |
| `download()` | `FAILED` result | `FAILED` result, reason `unavailable` |
| `download_all()` | `FAILED` result | `FAILED` result, reason `unavailable`; **anything unexpected** (a bug, an odd OS error) is a `FAILED` result with reason `error` and a logged traceback |
| `expand()` | item in `Expanded.failures` | **raises** |
| Drive folder fetch | `partial` result listing each failure | stores the rest, then **raises** (a retry finds those unchanged) |

Retry layers, innermost first: the session retries failed requests (`Config.retries`); a download
interrupted mid-stream restarts (`Config.download_attempts`); then `TransientError` surfaces for
the orchestrator (Step Functions `Retry`).

### One item can't end a run

`download_all()` runs every page, expansion and item as an isolated unit: whatever goes wrong
with one becomes its `FAILED` result and the rest carry on. Without this, one item raising an
unexpected error took the whole run down, discarded the results so far, and -- because a
scheduled run starts in the same order -- hit the same item first every time, so everything
after it was never processed. A full disk is the exception: every later item would fail the same
way, so it stops the run (that item `FAILED`/`disk_full`, the rest `deferred`/`disk_full`).
`crawl_into_store()` treats a page it can't store the same way (a `FAILED` entry in
`report.pages`; the crawl goes on).

### Time budget

`Context(budget=<seconds>, reserve=<seconds>)` limits how long a run takes: in a Lambda, the
invocation's remaining time less a margin for reporting. It is enforced where time is actually
spent:

- **Between items.** Once less than `reserve` seconds are left (`ctx.can_start()` is false),
  `download_all()` starts nothing new and returns the rest as `deferred` (`timeout`). Set
  `reserve` to about your slowest item.
- **In flight.** Streamed transfers (website files, Panopto, Drive, YouTube single-file formats)
  check the clock between chunks, and yt-dlp's own downloads from its progress hook (verified
  against the real yt-dlp). At zero the item stops with `DeadlineExceeded`: what it staged is
  removed, nothing is stored, and it comes back `FAILED`/`timeout`. The process is never killed
  in the middle of a write.
- **Waiting.** Retry sleeps (including `Retry-After`) and request timeouts are capped by the time
  left, so a server saying "try again in a minute" can't outlast the budget.
- **The crawl** may use `crawl_share` (40%) of what is left after the reserve, unless
  `time_limit` is given; pages it doesn't reach are `not_fetched`.

Because every stored item is skipped next time (`unchanged`), running again simply carries on.
`ctx` can be passed to `download_all(entries, ctx=ctx)` so one budget covers a crawl and its
downloads. `fileio.sweep_stale_dirs()` removes work directories a killed run left behind: call
it once when a long-lived process (a reused Lambda container) starts.

### Exit codes (`i2cc-download`)

`0` all done; `1` something really failed (a 404, access denied, a bug); `3` nothing failed but
the run ran out of time or disk, or a crawl was stopped by a limit (`not_fetched` pages) -- items
were deferred or cut off: run it again (a crawl stopped by `max_pages` or `--time-limit` needs a
higher limit to get further). If both happen, `1`. `--budget SECONDS` and `--reserve SECONDS` set the budget.

---

## 7. Network safety

URLs come from staff, so the scraper must not become a way to reach internal services (SSRF).

- **`validate_start_url`** — http(s) only; host must be in `Config.allowed_start_domains` (if
  set) or any subdomain of it; and the host must not resolve to a non-public address. Applied to
  the start of every crawl or scrape that will fetch a page. Direct asset links (a YouTube video
  or Drive URL) aren't crawled, so they skip the allowlist. **YouTube pages are never crawled**,
  even if `youtube.com` is allowed: they are built by JavaScript, so there is nothing to read.
- **`GuardedAdapter`** — mounted on every session from `new_session`. Before *each* request,
  including every redirect hop, it resolves the host and refuses loopback, private, link-local
  (`169.254.169.254`), and IPv4-mapped IPv6 addresses.
- **Untrusted names.** Filenames from `Content-Disposition` are cut to their last path
  component and used only as titles; extensions are limited to 1–10 letters/digits; stored
  paths are built from keys alone.

Not covered: DNS rebinding (the name changes between the check and the connection) — the real
fix is egress rules in the VPC; `yt-dlp` makes its own requests (to YouTube hosts only); and
`DriveClient` uses a plain adapter (it talks to Google's API).

---

## 8. Configuration

`config.Config` is a frozen dataclass. `Config.from_env()` reads `I2CC_<FIELD>` variables
(types follow each field's declaration; booleans accept `1/0/true/false/yes/no/on/off`; lists
are comma-separated). `config.default()` is read once and cached; `set_default()` replaces it.

| Field | Default | Meaning |
|---|---|---|
| `user_agent` | `Mozilla/5.0 (compatible; i2cc-scraper/1.0)` | |
| `timeout` | 30 | seconds per request (connect, and each read) |
| `retries`, `retry_backoff`, `retry_jitter`, `retry_after_max` | 3, 3.0, 1.5, 60 | session retry policy |
| `download_attempts` | 3 | restarts of a stream that breaks mid-download |
| `crawl_delay` | 0.5 | seconds between page requests |
| `max_pages` | 200 | pages fetched per crawl |
| `crawl_time_limit` | 0 | seconds before a crawl stops cleanly (0 = none) |
| `caption_recheck_hours`, `caption_recheck_days` | 24, 14 | how often, and for how long after download, a recording stored without captions is checked for them (0 days = never) |
| `allowed_start_domains` | `()` | empty = any; **set this in production** |
| `block_private_addresses` | true | turn off only for local testing |

Google credentials are separate: `DriveClient(key)` takes a dict, a JSON string or a file path;
`DriveClient.from_env()` reads `I2CC_GOOGLE_CREDENTIALS_JSON` or `GOOGLE_APPLICATION_CREDENTIALS`.
Only the command-line tools also look for `./credentials.json`.

---

## 9. Running it in AWS

`i2cc_scraper/aws/` (the `aws` extra) runs it as a Step Functions state machine of Lambda
functions, storing into i2ccc's ingest bucket. `src/scraper/Dockerfile` is the Lambda image (one
image; each function's CMD picks its handler) and `cdk/scraper.py` the CDK construct.

```
 {"url", "depth"?}                                          (a staff member submits a page)
   Crawl     crawl_into_store(url, ctx, depth)       pages stored as read -> .runs/<id>/crawl.json
   Plan      dedupe, expand() every asset, dedupe    -> .runs/<id>/items.json (+ plan.json)
   IngestEachItem   Distributed Map over items.json, one Lambda per item:
             ingest(item, ctx)                       -> .runs/<id>/results/<sha1(url)>.json
               TransientError -> Retry (1 min, x3 backoff, 3 attempts); failures tolerated
   Report    counts + failures                       -> .runs/<id>/report.json; the run's output
```

- **Plain data between steps.** Steps hand each other S3 keys (state is limited to 256 KB);
  items are `asdict(Source)`, read back with `Source.of()`. The run ID is the execution name.
- **One container, many invocations.** `handlers._setup()` builds the store and `Config` once
  per container; each invocation gets a `Context` whose budget is the Lambda's remaining time
  minus 15 s. The crawl gets its whole invocation (it has its own step).
- **The Google key** is a Secrets Manager secret (`GOOGLE_CREDENTIALS_SECRET` holds only its
  ARN; without a usable key, public Drive links are used). Only plan and ingest touch Drive, so
  only they are granted the secret and only they load it (`_drive_client()`, once per container,
  so a rotated key is picked up by new containers).
- **Failures are data here too.** Only `TransientError` leaves a handler (for the state
  machine's retry); `DeadlineExceeded` becomes a `timeout` result, since a retry would run out
  of time the same way. An item whose retries all failed has no result file; Report lists it
  as `unavailable`.
- **Input is checked at the boundary:** `url` a string, `depth` 0-3, the run ID safe for an S3
  key; `netguard` refuses start URLs outside `I2CC_ALLOWED_START_DOMAINS` (the construct sets
  `internet2.edu`) and private addresses.
- **It doesn't start ingestion.** Stored files are picked up by i2ccc's next ingestion run;
  replaced ones have been made ready for it (§5, the S3 store).
- **Tested locally:** the handlers run in order against AWS Emulator in
  `tests/scraper/test_handlers.py`; the same was done with the built image under the Lambda Runtime
  Interface Emulator. The state machine itself is checked by `cdk synth` only.

## 10. Testing

`tests/` has 492 tests and needs neither network nor credentials. 61 of them use AWS Emulator
(S3, DynamoDB) and are skipped unless its connection variables are set:
`( . ~/.aws/emulator/9297.env && pytest )`; the `emulator` fixture refuses any endpoint that
isn't on this machine.

- **`conftest.py`** provides `Site`, a real HTTP server on `127.0.0.1` where tests register
  routes (including `flaky()` and `etag_route()` builders); `run_cli()` for subprocess CLI tests;
  a `store` fixture; and two autouse fixtures: fast config (no sleeping, local addresses
  allowed) and a **no-internet guard** that fails any test connecting to a non-loopback address.
- **Fakes** in `test_downloader.py`: `FakeDrive` (a Drive API client with a small file tree,
  shortcuts, Forms, injectable failures) and a fake `yt_dlp` module whose chosen format is
  served by the test server in ranges.
- **Per area:** `test_url_classifier` (pure rules), `test_scraper_html` (saved real pages in
  `tests/scraper/fixtures/`), `test_crawl` (a local multi-page site: depth, limits, filtering),
  `test_retries`, `test_store` (the `Store` contract, run on `LocalStore` and `S3Store`),
  `test_s3store` (layout, object metadata, streaming, invalidation, races), `test_handlers`
  (the state machine's steps end to end),
  `test_netguard_config`, `test_fileio` (atomic writes, the stale work-dir sweep),
  `test_run_limits` (failure isolation, the time budget, the CLI's exit codes), `test_captions` (the cleaner, on a real caption excerpt), `test_downloader` (change detection, every fetcher,
  folders, dedupe, error reasons, the CLI), and `test_tui` (the terminal UI driven headlessly with
  Textual's test pilot: what each row shows while it works and when it's done).

---

## 11. Extending it

**A new source** (say, Vimeo): add the type constant and recognition rule to `url_classifier`,
write `fetch_vimeo(source, ctx)` using `fetch_versioned`, and register it in `api.FETCHERS`. Add
a store key prefix via `make_key`.

**A new store:** implement the `Store` methods (§5), add it to `STORES` in
`tests/scraper/test_store.py`, and it must pass the same tests. `S3Store` is the example.
`Record.files` holds store names, so only `LocalStore.path()` (used by the TUI) is
local-specific.

**A Lambda handler:** a thin function that builds `Config.from_env()`, a `Context`, and calls
one of `crawl_into_store`, `expand`, `ingest`, returning `asdict(...)`.

---

## 12. Known gaps and decisions

**Decided**
- Videos are downloaded (not just captions), at low quality (360p default).
- No download size limit for now. The largest real item so far is a 109 MB `.pptx`.
- This repo stops at "content + metadata in the store"; a separate pipeline does ingestion.

**Open**
- **Overlapping runs and the content itself.** Record updates are conditional (§5), so no
  `found_on` entry is lost, and in the S3 store new content gets new object names, so racing
  writers don't overwrite each other's files. Duplicate downloads of one item by two runs are
  possible (a per-item lease would stop them; worth it for videos).
- **No pruning.** Content that disappears from the site stays in the store. An item no page
  links to any more is *flagged* (`unlinked_at`, empty `found_on`) but not removed. Two limits:
  page-to-page links aren't tracked, a Drive folder's files list the *folder* in `found_on` (so a
  page that drops the folder link doesn't flag the files), and extensionless files recognised
  only by their `Content-Type` during a crawl aren't in a page's `links`, so dropping such a
  link doesn't unlink the item.
- **Content/Record window.** The content files and the Record are written in a safe order (§5)
  but not atomically together. A crash after a same-named file was replaced and before the Record
  is rewritten leaves the old Record over new bytes; the next run sees the source's version differs
  from the Record's and fetches again.
- **i2ccc ingestion, as it is today.** Its routing Lambda reads only the first page of
  `ListObjectsV2` (1000 keys) and its video chunker uploads chunks without object metadata, so
  vectors of a video over 1.8 GB can't be found by `scraper-file-id`. Vectors indexed before a
  metadata-only change (e.g. a newly known parent folder) keep the old values. OpenSearch
  Serverless search lags indexing by up to a minute, so vectors indexed in that window can be
  missed by invalidation.
- **Captions after the window.** A recording still without captions 14 days after download
  is no longer checked (`--force` re-fetches it).
- **Pages are always re-fetched** (internet2.edu offers no cheap change check); only the
  *write* is avoided.
- **Panopto quality** can't be lowered (the podcast link serves one quality).
- **YouTube from cloud IPs** is untested; YouTube may block AWS address ranges.
- **Time budget limits.** yt-dlp's extraction, and a download yt-dlp makes itself (a format
  that needs ffmpeg to merge), are only checked when yt-dlp reports progress; streamed
  downloads are checked between chunks. The crawl's own page requests use a session without the
  budget's caps, so the crawl can overrun its share by one request's timeout and retries.
- **`scraper.py` (about 730 lines)** still mixes crawling, link filtering, text extraction and
  HTTP; **`downloader/drive.py` (about 410 lines)** is the largest downloader module. Both are
  candidates for a further split.
- **Not done from the refactor list:** building Drive's retry policy from `Config`, and a
  `CrawlLimits` object to shorten the crawl function signatures — both change behaviour or API.
