# i2cc-scraper

Crawl Internet2 pages and download what they link to — page text, Google Drive/Slides decks,
website files, YouTube and Panopto recordings — into a store, with a metadata record for each,
for a RAG ingestion pipeline to pick up. Re-runs write only what changed.

**How it's built:** see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — layers, data model,
change detection, failure model, and how it maps onto Step Functions.

## Install

```sh
python -m venv .venv && .venv/bin/pip install -e '.[dev,youtube,aws]'   # development
pip install -r requirements.txt && pip install --no-deps .         # deployment: exact versions
```

YouTube needs the `youtube` extra and a JavaScript runtime (`deno` or `node`) on `PATH`.
Give it a link to a single video (any usual form; a `&list=…` or `&t=…` is ignored) and it
downloads just that video without crawling anything; playlists, channels and search pages are
refused.

## Use

```sh
i2cc-scrape --depth 1 https://internet2.edu/cloud-forum/... > crawl.json   # find pages + links
i2cc-download --from-json crawl.json --out downloads                       # fetch into a store
```

`i2cc-download` prints a JSON result per item (`status`: `new` / `updated` / `unchanged` /
`failed` / `deferred`, with a `reason` for failures) and exits 1 if anything failed. `--force`
downloads everything again.

**Time budget.** `--budget SECONDS` (with `--reserve`) caps a whole run, crawl and downloads
together: it stops starting new items near the end, cuts off any transfer still running when the
time is up (nothing half-written), and reports the items it didn't reach as `deferred`. Exit code
`3` means "nothing failed, but not everything was reached: run it again" -- what is already
stored is skipped. One item failing, whatever the reason, never ends the run.

From Python, the same thing is `scraper.crawl_for_ingest()` then `downloader.download_all()`.
An orchestrator (e.g. Step Functions) runs the same steps one at a time, each taking and
returning JSON-serializable items, with a `downloader.Context`:

```
crawl_into_store(url, ctx)   pages stored as they're read; report.assets = what they link to
dedupe(assets)               one item per stored item (merges found_on, keeps both contexts)
expand(asset, ctx)           a Drive folder -> its files (per asset, can run in parallel)
dedupe(all expanded items)   a file may be linked directly and listed in a folder
ingest(item, ctx)            download + store, unless unchanged (per item, in parallel)
```

`ingest()` raises `TransientError` for failures worth retrying; everything else is a result.

## What gets stored

Each item is stored under a key built from its URL or ID (never its title), with a
`<key>.metadata.json` record next to its files. Downloads are streamed into the store as they
arrive — a video never needs local disk. In the local store files are `<key><suffix>`; in
S3 (below) they are named by title.

| key | content |
|---|---|
| `pages/<url hash>` | `.txt` (title, URL, extracted text) |
| `files/<url hash>` | the file (`.pdf`, `.pptx`, ...) |
| `google_drive/<file id>` | the file or export (`.pdf` by default) |
| `youtube/<video id>` | `.mp4` + `.en.vtt` captions |
| `panopto/<session id>` | `.mp4` + `.en.vtt` captions (converted from Panopto's SRT) |

The record has `source_url`, `found_on` (every page or Drive folder linking to it), `title`,
`link_text`, `context` (the heading above a page's link, e.g. the session name),
`folder_path` (the Drive folder it's in, e.g. `Slides 2026 / Day 2` — an item found both ways
has both), `content_type`, `content_sha256`, `source_version`, and `fetched_at`. Pages also list
the documents/videos they `links` to; an item no page links to any more gets `unlinked_at`
(nothing is deleted). Metadata for the RAG system includes `is_subscriber_content` (controls badge
display), `parent_folder_name` and `parent_folder_url` (for context about the parent collection),
and `relative_start_time` (for video chunks). See `i2cc_scraper/store.py`.

**Change detection.** Drive (with a service account) and website files with an `ETag` /
`Last-Modified` are skipped *without downloading* when unchanged. Everything else is downloaded
and compared by SHA-256; unchanged content isn't rewritten. Crawled pages are compared by their
extracted text, so markup-only changes don't count. A page whose text is unchanged but whose
document links changed is stored again, and items it stopped linking are flagged. Stored
Panopto recordings aren't fetched again; YouTube videos only if the quality setting changed
(default: at most 360p, which is enough for ingestion; a ready-made mp4 is preferred, so ffmpeg
is optional). A recording stored without captions is checked again for them (captions only) for
14 days. YouTube's automatic captions are stored with each line once (they arrive with every
line repeated three times).

**Failures.** Permanent failures (`not_found`, `access_denied`, `not_downloadable`, `blocked`,
`unsupported`, ...) are results. Temporary ones (timeouts, HTTP 429/5xx after retries) make
`ingest()` raise `TransientError`, so a caller can retry later; `download()`/`download_all()`
report them as `failed` with reason `unavailable`.

Several runs may share one store at once: Record updates are conditional (a writer that finds the
Record changed re-reads and merges), so overlapping runs don't lose each other's `found_on`
entries.

## Run in AWS (for i2ccc)

`i2cc_scraper.aws` (the `aws` extra) runs the scraper as a Step Functions state machine of Lambda
functions — crawl, plan, one Lambda per item (Distributed Map), report — storing into i2ccc's
ingest bucket under `i2cc-scraper/`:

```
i2cc-scraper/<key>/<hash>/<Title>.pdf|.txt|.mp4|.en.vtt   what i2ccc ingests, with the S3
                                                          metadata its ingest functions read
i2cc-scraper/<key>.metadata.json                          the Record (ignored by ingestion)
```

The file name is the title because i2ccc shows it as the citation. When a stored file is
replaced, its entry in i2ccc's processed-files table and its OpenSearch vectors are deleted, so
the next ingestion run picks up the new version. The state machine doesn't start ingestion.

* `src/scraper/Dockerfile` — the Lambda image (this directory is the build context).
* `cdk/scraper.py` — the CDK construct, `ScraperPipeline` (its docstring shows how to wire it to
  `RagIngest`). Output: the state machine's ARN.
* Invoke the construct's `start_function` with `{"url": "https://internet2.edu/...", "depth": 1}`.
  It returns `source_id`, `run_id`, `execution_arn`, and `created`. Submitting the same source
  while active returns that run (including its original depth). The execution's output is the summary, and
  `i2cc-scraper/.runs/<execution name>/report.json` has every item's result.

### Discovery and download progress

`SCRAPER_PROGRESS_TABLE` enables incremental DynamoDB reporting in the Lambda handlers.
Without it, the library, CLI, and existing S3 reporting continue to work. This tracks discovery
and downloading only: `stored` means content reached S3, not that search ingestion completed.
No frontend HTTP endpoints are included.

The table has string `pk`/`sk` keys and a `sources` GSI (`catalog_pk`/`catalog_sk`). The schema
used by emulator tests is `i2cc_scraper.aws.progress.table_definition(name)`:

| pk | sk | Record |
| --- | --- | --- |
| `SOURCE#<source_id>` | `META` | Persistent source, title, latest/active run, latest phase and final counts |
| `SOURCE#<source_id>` | `RUN#<timestamp>#<run_id>` | Run history, with final status and counts |
| `RUN#<run_id>` | `META` | Execution ARN, phase, outcome, discovery completeness, timestamps |
| `RUN#<run_id>` | `ITEM#<item_id>` | One discovered page, folder, or document and its progress |
| `RUN#<run_id>` | `EDGE#<parent_id>#<child_id>` | Where an item was found, link text, and context |

IDs are hashes of the existing normalized URL/provider identity, so link variants share an
item. Every parent relationship is recorded before deduplicating work. Pages and folders can
have multiple parents and cycles; clients traversing the graph must keep a visited set.
Folder nodes survive expansion, including empty folders and failed listings. Run counts count
unique items, not relationships. An edge does not mean a second download is queued.

Each item has independent `discovery_status` (`queued`, `running`, `complete`, `failed`,
`deferred`, `not_applicable`) and `content_status` (`queued`, `checking`, `downloading`,
`retrying`, `stored`, `failed`, `deferred`, `skipped`, `not_applicable`). Stored results retain
`new`/`updated`/`unchanged`, file references, and the store key. Errors include a reason and
message. A page finishing discovery does not imply its children have finished downloading.

Use the Python query methods on `ProgressStore(table_name)`: `get_source`, `get_run`,
`get_item(run_id, url)`, `list_sources`, `list_runs(source_id)`, `list_items(run_id)`, and
`list_children(run_id, parent_url)`. List methods return `items` and `next_key`; pass that key
back as `after`, even if a filtered page was empty. `limit` defaults to 100. Child results are
edges referencing item IDs. `summarize(run_id)` queries all item pages to calculate live
counts; final counts are stored on the run and source when it finishes. GSI catalog reads are
eventually consistent; run/item reads use the base table with strong consistency.

Run metadata, history, items, and edges have `expires_at` set to 30 days after run creation.
Enable DynamoDB TTL on that numeric epoch-seconds attribute. Query methods hide expired rows
while AWS asynchronously deletes them. Source records and their latest summary persist.

Source claims are transactional, so simultaneous submissions return one active run. The
starter persists the original request and uses the run UUID as the Standard execution name;
retrying an uncertain start uses exactly the same name and input. Each download attempt has
a version that fences out late updates from older attempts. Finalization freezes run writes,
marks unfinished work explicitly, records final counts, and releases the source for another run.
EventBridge terminal execution events and a five-minute reconciliation job handle timeouts,
stopped executions, missed events, and interrupted starts/finalizers.

The CDK construct exposes `progress_table`, `start_function`, and
`grant_frontend_access(admin_role)` (or the `frontend_role` constructor argument). The designated
frontend role gets table reads and permission to invoke the starter; workers own progress
writes. The construct creates no public function URL or frontend API. Integrate it into the
existing `RagChatbotStack` through `stack.scraper.grant_frontend_access(admin_role)`
when the admin frontend supplies its IAM role. Deployment still uses `app.py` and
the normal `cdk deploy` flow in `us-east-1`; no separate app or stack is needed.
CDK retains the table on stack removal and enables point-in-time recovery.

Locally, against [AWS Emulator](https://github.com/aws/aws-emulator):
`( . ~/.aws/emulator/9297.env && .venv/bin/python -m pytest )` also runs the S3 store and the
handlers end to end (`tests/scraper/test_s3store.py`, `tests/scraper/test_handlers.py`), plus
transaction races, retry ordering, pagination, and retention (`tests/scraper/test_progress.py`).
These tests create uniquely named tables and remove them afterward. The endpoint fixture
only accepts loopback addresses. `tests/scraper/test_cdk_scraper.py` synthesizes the construct
locally when `aws-cdk-lib` is installed; it does not build images or deploy resources.

## Configuration

Settings live in `i2cc_scraper.config.Config`, read from `I2CC_*` environment variables (e.g.
`I2CC_TIMEOUT=60`, `I2CC_MAX_PAGES=500`) or passed in code.

* `I2CC_ALLOWED_START_DOMAINS=internet2.edu,...` — only these domains (and subdomains) may be
  crawled. Unset means any; **set it wherever URLs come from users.**
* Requests to private/internal addresses (`10.*`, `127.*`, `169.254.169.254`, ...) are refused,
  including after redirects. `I2CC_BLOCK_PRIVATE_ADDRESSES=0` turns that off (local testing only).

Google Drive uses a service account if one is configured, else public links:
`I2CC_GOOGLE_CREDENTIALS_JSON` (the key JSON itself — e.g. injected from a secrets manager),
`GOOGLE_APPLICATION_CREDENTIALS` (a key file), `--credentials KEY.json`, or `./credentials.json`
(command line only). Share private Drive folders with the service account's email.

## Develop

```sh
.venv/bin/python -m pytest
```

Tests use a local HTTP server and fakes for Drive and yt-dlp — no network, no credentials.
With AWS Emulator's variables set they also run against its S3 and DynamoDB; without, those
tests are skipped. `tests/scraper/test_store.py` is the contract every `Store` passes.
`ingest_utils/scraper/tui.py` is a terminal UI for trying things out (`pip install -e
'src/scraper[tui]'`, `python ingest_utils/scraper/tui.py --out downloads`); it
isn't part of the package. It uses the same store, so a run reports what the cache did: each
document goes `Queued → Checking → Downloading` (only if bytes are really fetched) and ends as
`New`, `Updated` or `Unchanged`, with a line saying whether anything was downloaded. Use the same
`--out` folder to see cache hits across sessions.

**Never commit secrets.** `credentials.json`, `.env`, and the `.kiro/` / `.codex/` MCP configs
are git-ignored.
