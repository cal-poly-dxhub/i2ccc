"""The i2cc-download command."""
import argparse
import json
import logging
import sys
from dataclasses import asdict
from pathlib import Path

from .errors import Reason
from .models import DEFERRED, FAILED, Context, DownloadResult, Options
from .plan import crawl_into_store, download_all
from ..logs import setup_logging
from ..store import LocalStore

log = logging.getLogger('i2cc_scraper.downloader')  # one logger, as before


def _needs_another_run(result: DownloadResult) -> bool:
    """True for a result that isn't a failure of the item but of the run's time, disk or crawl
    limits: it was not reached, or was cut off. Running again (or with a higher limit)
    gets to it."""
    return result.status == DEFERRED or result.reason in (Reason.TIMEOUT, Reason.DISK_FULL,
                                                          Reason.NOT_FETCHED)


def main() -> None:
    parser = argparse.ArgumentParser(
        description='Download crawled pages, YouTube videos, Panopto sessions, Google '
                    'Drive/Docs files, and website files into a local store, with metadata.',
        epilog='Example: i2cc-scrape --depth 1 URL > crawl.json && '
               'i2cc-download --from-json crawl.json')
    parser.add_argument('urls', nargs='*', help='YouTube, Panopto, Google Drive/Docs, or file URLs')
    parser.add_argument('--crawl', metavar='URL',
                        help='Crawl from this page first (storing each page as it is read), '
                             'then download everything it links to')
    parser.add_argument('--depth', type=int, default=1,
                        help='--crawl: depth to crawl to (start page = 0; default: 1)')
    parser.add_argument('--budget', type=float, metavar='SECONDS',
                        help='Total time the run may take, crawling and downloading together. '
                             'Near the end it stops starting new items and reports the rest as '
                             '"deferred": run again to carry on (exit code 3)')
    parser.add_argument('--reserve', type=float, metavar='SECONDS',
                        help='--budget: stop starting new items when this much time is left '
                             '(default: 10%% of the budget, at most 30 s) -- about as long as '
                             'your slowest item takes')
    parser.add_argument('--time-limit', type=float, metavar='SECONDS',
                        help='--crawl: stop crawling after this long (default: '
                             '$I2CC_CRAWL_TIME_LIMIT, else none)')
    parser.add_argument('--from-json', type=Path, metavar='FILE',
                        help="Download the documents, and save the web pages, in i2cc-scrape's "
                             "--depth JSON output ('-' for stdin)")
    parser.add_argument('--out', type=Path, default=Path('downloads'),
                        help='Store directory (default: downloads)')
    parser.add_argument('--force', action='store_true',
                        help='Download everything again, even if it looks unchanged')
    parser.add_argument('--max-height', type=int, default=Options.max_height,
                        help='YouTube: maximum video height in pixels (default: '
                             f'{Options.max_height})')
    parser.add_argument('--audio-only', action='store_true', help='YouTube: download audio only')
    parser.add_argument('--no-captions', action='store_true',
                        help="YouTube/Panopto: don't save captions")
    parser.add_argument('--export-format',
                        help='Google Slides/Docs/Sheets: export format (default: pdf; xlsx for '
                             'Sheets), e.g. pptx, docx, txt, csv')
    parser.add_argument('--credentials', type=Path, metavar='KEY.json',
                        help='Google service-account key file, to use the Drive API (default: '
                             '$I2CC_GOOGLE_CREDENTIALS_JSON, $GOOGLE_APPLICATION_CREDENTIALS, '
                             'else ./credentials.json; with none, public links)')
    parser.add_argument('--no-pages', action='store_true',
                        help="--from-json: don't save crawled web pages, only download documents")
    parser.add_argument('--original', action='store_true',
                        help='Google: download the originally uploaded file (e.g. .pptx) where '
                             'there is one, instead of exporting')
    args = parser.parse_args()
    setup_logging()

    entries: list[dict | str] = list(args.urls)
    if args.from_json:
        text = sys.stdin.read() if str(args.from_json) == '-' else args.from_json.read_text()
        entries += json.loads(text)
    if not entries and not args.crawl:
        parser.error('give one or more URLs, --from-json FILE, or --crawl URL')

    from ..drive_api import DriveClient, cli_drive_client
    try:
        drive = DriveClient(args.credentials) if args.credentials else cli_drive_client()
    except (OSError, ValueError) as exc:
        parser.error(f'cannot use the Google credentials: {exc}')
    if drive is not None:
        log.info('Google Drive: using service account %s', drive.email)

    options = Options(export_format=args.export_format, original=args.original,
                      max_height=args.max_height, audio_only=args.audio_only,
                      captions=not args.no_captions, force=args.force)
    results: list[DownloadResult] = []
    reserve = args.reserve if args.reserve is not None else (
        30.0 if args.budget is None else min(30.0, args.budget * 0.1))
    with Context(LocalStore(args.out), options, drive=drive, budget=args.budget,
                 reserve=reserve) as ctx:  # one budget for the crawl and the downloads
        if args.crawl:
            from ..netguard import UnsafeURLError
            try:
                report = crawl_into_store(args.crawl, ctx, args.depth,
                                          time_limit=args.time_limit)
            except UnsafeURLError as exc:
                parser.error(str(exc))
            results += report.pages + report.problems
            entries += report.assets
        results += download_all(entries, ctx=ctx, save_pages=not args.no_pages)
    print(json.dumps([asdict(r) for r in results], indent=2, ensure_ascii=False))
    counts: dict[str, int] = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    log.info('Done: %s', ', '.join(f'{n} {s}' for s, n in sorted(counts.items())) or 'nothing')
    # Exit codes: 1 = something really failed; 3 = nothing did, but the run ran out of time or
    # disk first (items deferred, or cut off mid-transfer): run it again to carry on.
    if any(r.status == FAILED or r.error for r in results if not _needs_another_run(r)):
        sys.exit(1)
    if any(_needs_another_run(r) for r in results):
        log.warning('Not everything was reached: run again to carry on (a crawl stopped by '
                    'max_pages or a time limit needs a higher limit to get further)')
        sys.exit(3)
