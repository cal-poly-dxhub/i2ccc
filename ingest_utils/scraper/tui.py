"""Terminal UI for trying out the scraper and downloader -- a local stand-in for the admin UI
mockup in the design doc. Testing only: the i2cc_scraper package doesn't import this file.

    .venv/bin/python ingest_utils/scraper/tui.py [--out downloads] [--depth 1]

Add a page URL: it's crawled (Crawling -> Crawled / Error), and every document found becomes a
child row that is queued, downloaded, and marked Ingested (or Failed -- retry). Pages without
documents are taken as page text. A direct YouTube/Panopto/Drive link skips straight to download.
Every crawled web page's HTML (and text) is saved in <out>/pages/.

Keys: Enter expand/collapse   r retry   x remove   l toggle log   a add URL   q quit
"""
import argparse
import itertools
import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, DataTable, Footer, Header, Input, Label, Log, Static

from i2cc_scraper import downloader, scraper
from i2cc_scraper.drive_api import cli_drive_client
from i2cc_scraper.store import LocalStore

DOWNLOAD_WORKERS = 2
LABEL_WIDTH = 72  # width of the "Source / Document" column

# Row statuses
CRAWLING, CRAWLED, ERROR = 'crawling', 'crawled', 'error'          # pages
# documents: waiting -> checking (is it stored and current?) -> downloading (only if bytes are
# really being fetched) -> what ingesting did: new / updated / unchanged
QUEUED, CHECKING, DOWNLOADING = 'queued', 'checking', 'downloading'
INGESTED, FAILED = 'ingested', 'failed'
NEW, UPDATED, UNCHANGED = downloader.NEW, downloader.UPDATED, downloader.UNCHANGED

STATUS_STYLES = {
    CRAWLING: ('⟳ Crawling', 'yellow'),
    CRAWLED: ('✓ Crawled', 'green'),
    ERROR: ('⚠ Error — retry', 'bold red'),
    QUEUED: ('Queued', 'dim'),
    CHECKING: ('⟳ Checking', 'yellow'),
    DOWNLOADING: ('↓ Downloading', 'cyan'),
    INGESTED: ('✓ Ingested', 'green'),
    NEW: ('✓ New', 'bold green'),
    UPDATED: ('✓ Updated', 'bold green'),
    UNCHANGED: ('✓ Unchanged', 'green'),
    FAILED: ('⚠ Failed — retry', 'bold red'),
}

TYPE_LABELS = {
    scraper.PAGE: 'Page',
    scraper.PAGE_TEXT: 'Page text',
    scraper.YOUTUBE: 'YouTube video',
    scraper.PANOPTO: 'Panopto',
    scraper.GOOGLE_DRIVE: 'Google Drive',
    scraper.FILE: 'File',
    scraper.FAILED: 'Page',
    scraper.NOT_FETCHED: 'Page (not read)',
}

ASSET_TYPES = {scraper.YOUTUBE, scraper.PANOPTO, scraper.GOOGLE_DRIVE, scraper.FILE}


@dataclass
class Item:
    """One row: a source the user added, or something found under it."""
    id: str
    url: str
    kind: str                         # a scraper type: page, page_text, youtube, ...
    status: str
    parent: Optional['Item'] = None
    title: str = ''
    context: str = ''
    added: float = field(default_factory=time.time)
    children: list['Item'] = field(default_factory=list)
    expanded: bool = True
    crawl_depth: int = 1              # depth used when (re)crawling this page
    files: list[str] = field(default_factory=list)
    text: str = ''
    error: str = ''
    link_text: str = ''               # the text of the link it was found through
    outcome: str = ''                 # what ingesting did: NEW, UPDATED or UNCHANGED ('' = not yet)
    fetched: bool = False             # whether file bytes were actually downloaded this time

    def outcome_note(self) -> str:
        """One line saying what ingesting did -- in particular whether anything was downloaded."""
        if self.kind in ASSET_TYPES:
            if self.outcome == NEW:
                return 'downloaded and stored'
            if self.outcome == UPDATED:
                return 'changed — new version stored'
            if self.outcome == UNCHANGED:
                return ('downloaded to compare — identical, nothing written' if self.fetched
                        else 'already stored — nothing downloaded')
        elif self.outcome:
            return {NEW: 'page text saved', UPDATED: 'page changed — saved again',
                    UNCHANGED: 'page unchanged — nothing written'}[self.outcome]
        return ''

    @property
    def depth(self) -> int:
        return 0 if self.parent is None else self.parent.depth + 1

    def label(self) -> str:
        if self.context and self.title:
            return f'{self.context} — {self.title}'
        return self.title or self.context or _short_url(self.url)

    def walk(self):
        yield self
        for child in self.children:
            yield from child.walk()


class TuiLogHandler(logging.Handler):
    """Sends scraper/downloader log lines to the app's log panel (from any thread)."""

    def __init__(self, app: 'IngestApp'):
        super().__init__()
        self.app = app

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
            if threading.current_thread() is threading.main_thread():
                self.app.write_log(line)
            else:
                self.app.call_from_thread(self.app.write_log, line)
        except Exception:  # never let logging crash a worker
            pass


class IngestApp(App):
    TITLE = 'CAI Content Admin — ingest test'
    CSS = '''
    #add-bar { height: 3; }
    #url { width: 1fr; }
    #depth-label { padding: 1 1 0 2; color: $text-muted; }
    #depth { width: 8; }
    #add { width: 18; }
    #hint { color: $text-muted; padding: 0 1; }
    #table { height: 1fr; }
    #details { height: auto; max-height: 12; border: round $primary; padding: 0 1; }
    #log { height: 10; border: round $secondary; }
    #log.hidden { display: none; }
    '''
    BINDINGS = [
        Binding('a', 'focus_url', 'Add URL'),
        Binding('r', 'retry', 'Retry'),
        Binding('x', 'remove', 'Remove'),
        Binding('delete', 'remove', 'Remove', show=False),
        Binding('l', 'toggle_log', 'Log'),
        Binding('q', 'quit', 'Quit'),
    ]

    def __init__(self, out_dir: Path, depth: int, initial_urls: Optional[list[str]] = None):
        super().__init__()
        self.out_dir = out_dir
        self.store = LocalStore(out_dir)
        self.default_depth = depth
        self.initial_urls = initial_urls or []
        self.sources: list[Item] = []
        self.items: dict[str, Item] = {}
        self._ids = itertools.count(1)
        self.downloads: queue.Queue[Item] = queue.Queue()

    # -- layout --------------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical():
            yield Static('We crawl top-level pages to find linked documents/videos, then download '
                         'each one independently. Direct video links skip straight to download.',
                         id='hint')
            with Horizontal(id='add-bar'):
                yield Input(placeholder='Drop in a page URL…', id='url')
                yield Label('Depth', id='depth-label')
                yield Input(value=str(self.default_depth), placeholder='depth', id='depth',
                            type='integer')
                yield Button('+ Add source', id='add', variant='primary')
            yield DataTable(id='table', cursor_type='row', zebra_stripes=True)
            yield Static('Select a row to see details.', id='details')
            yield Log(id='log', classes='hidden')
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_column('Source / Document', key='label', width=LABEL_WIDTH)
        table.add_column('Type', key='type', width=14)
        table.add_column('Added', key='added', width=10)
        table.add_column('Status', key='status', width=18)
        self.set_interval(30, self._refresh_times)

        handler = TuiLogHandler(self)
        handler.setFormatter(logging.Formatter('%(levelname)s %(name)s: %(message)s'))
        root = logging.getLogger()
        root.handlers[:] = [handler]  # keep log output from drawing over the UI
        root.setLevel(logging.INFO)
        for name in ('urllib3', 'google', 'yt_dlp'):
            logging.getLogger(name).setLevel(logging.WARNING)

        for _ in range(DOWNLOAD_WORKERS):
            threading.Thread(target=self._download_worker, daemon=True).start()
        self.query_one('#url', Input).focus()
        for url in self.initial_urls:
            self.add_source(url, self.default_depth)

    # -- adding sources ------------------------------------------------------------------

    @on(Button.Pressed, '#add')
    @on(Input.Submitted, '#url')
    def add_from_input(self) -> None:
        url_input = self.query_one('#url', Input)
        url = url_input.value.strip()
        if not url:
            return
        if '://' not in url:
            url = 'https://' + url
        try:
            depth = max(0, int(self.query_one('#depth', Input).value or self.default_depth))
        except ValueError:
            depth = self.default_depth
        url_input.value = ''
        self.add_source(url, depth)

    def add_source(self, url: str, depth: int) -> None:
        kind = scraper.classify_url(url)
        item = self._new_item(url, kind or scraper.PAGE, QUEUED if kind else CRAWLING,
                              crawl_depth=depth)
        self.sources.insert(0, item)
        self.render_table(select=item)
        if kind:
            self.downloads.put(item)  # a direct video/document link: no crawl needed
        else:
            self.crawl(item, depth)

    # -- crawling (background thread) ----------------------------------------------------

    @work(thread=True)
    def crawl(self, item: Item, depth: int) -> None:
        saved: dict[str, tuple[list[str], str]] = {}  # page URL -> (saved files, new/updated/unchanged)
        try:
            results = scraper.crawl_for_ingest(item.url, depth)
            with downloader.Context(self.store) as ctx:
                for r in results:
                    if r.type in (scraper.PAGE, scraper.PAGE_TEXT) and (r.html or r.text):
                        stored = downloader.save_page(r, ctx)
                        saved[r.url] = (self._paths(stored.files), stored.status)
                        r.html = ''  # on disk now; no need to keep it in memory
        except Exception as exc:  # anything unexpected becomes a row error, not a crash
            results, error = [], f'{type(exc).__name__}: {exc}'
        else:
            error = ''
        self.call_from_thread(self._crawl_done, item, results, error, saved)

    def _crawl_done(self, item: Item, results: list, error: str,
                    saved: dict[str, tuple[list[str], str]]) -> None:
        if item.id not in self.items:
            return  # removed while crawling
        item.children.clear()
        if error or not results:
            item.status, item.error = ERROR, error or 'Nothing found'
            self.render_table()
            return

        root, found = results[0], results[1:]
        item.kind, item.title, item.text, item.error = root.type, root.title, root.text, root.error
        item.files, item.outcome = saved.get(root.url, ([], ''))
        item.status = {scraper.PAGE: CRAWLED,
                       scraper.PAGE_TEXT: item.outcome or INGESTED}.get(root.type, ERROR)

        by_url = {root.url: item}
        for result in found:
            parent = by_url.get(result.found_on, item)
            child = self._new_item(result.url, result.type, self._initial_status(result.type),
                                   parent=parent, title=result.title, context=result.context,
                                   link_text=result.title)
            child.text, child.error = result.text, result.error
            child.files, child.outcome = saved.get(result.url, ([], ''))
            if result.type == scraper.PAGE_TEXT and child.outcome:
                child.status = child.outcome
            parent.children.append(child)
            by_url[result.url] = child
            if result.type in ASSET_TYPES:
                self.downloads.put(child)

        # A page with documents also has its own text ingested: show that as its first child,
        # matching the design doc's flow (page -> "ingest page text" + "scrape URLs").
        for page in [i for i in item.walk() if i.kind == scraper.PAGE and i.text]:
            text_row = self._new_item(page.url, scraper.PAGE_TEXT, page.outcome or INGESTED,
                                      parent=page, title='Page text')
            text_row.text, text_row.files, text_row.outcome = page.text, page.files, page.outcome
            page.children.insert(0, text_row)
        self.render_table()

    @staticmethod
    def _initial_status(kind: str) -> str:
        if kind in ASSET_TYPES:
            return QUEUED
        return {scraper.PAGE: CRAWLED, scraper.PAGE_TEXT: INGESTED}.get(kind, FAILED)

    # -- downloading (worker threads) ----------------------------------------------------

    def _download_worker(self) -> None:
        drive = cli_drive_client()  # one per thread: a DriveClient's session isn't shareable
        while True:
            item = self.downloads.get()
            if item.id not in self.items:
                continue  # removed while queued
            item.fetched, item.outcome = False, ''
            self.call_from_thread(self._set_status, item, CHECKING)

            def started() -> None:
                """File bytes are about to be fetched: only now is it "Downloading"."""
                item.fetched = True
                self.call_from_thread(self._set_status, item, DOWNLOADING)

            # Tell the downloader where the item was found, as the command line does, so its
            # stored record says which page linked it and under which heading.
            source = downloader.Source(
                url=item.url, type=item.kind, link_text=item.link_text, context=item.context,
                found_on=[item.parent.url] if item.parent is not None else [])
            try:
                with downloader.Context(self.store, drive=drive, on_download=started) as ctx:
                    result = downloader.ingest(source, ctx)
                error, files, title = result.error, self._paths(result.files), result.title
                status = result.status
            except downloader.TransientError as exc:
                error, files, title, status = f'Temporary failure — retry later: {exc}', [], '', ''
            except Exception as exc:
                error, files, title, status = f'{type(exc).__name__}: {exc}', [], '', ''
            self.call_from_thread(self._download_done, item, files, title, error, status)

    def _paths(self, names: list[str]) -> list[str]:
        """Store names (e.g. "google_drive/ID.pdf") as local file paths, for display."""
        return [str(self.store.path(name)) for name in names]

    def _download_done(self, item: Item, files: list[str], title: str, error: str,
                       status: str = '') -> None:
        if item.id not in self.items:
            return
        item.files, item.error = files, error
        generic = {'', 'slides', 'slide', 'recording', 'video', 'link', 'here'}
        if title and item.title.lower() in generic:
            item.title = title  # e.g. "Slides" -> the downloaded file's name
        if error and not files:
            item.status = FAILED
        else:
            item.outcome = status if status in (NEW, UPDATED, UNCHANGED) else ''
            item.status = item.outcome or INGESTED
        self.render_table()

    # -- actions -------------------------------------------------------------------------

    def action_focus_url(self) -> None:
        self.query_one('#url', Input).focus()

    def action_toggle_log(self) -> None:
        self.query_one('#log', Log).toggle_class('hidden')

    def action_retry(self) -> None:
        item = self._selected()
        if item is None or item.status not in (ERROR, FAILED):
            return
        item.error = ''
        if item.kind in ASSET_TYPES:
            self._set_status(item, QUEUED)
            self.downloads.put(item)
        else:  # a page: crawl it again, one level down from here
            item.status = CRAWLING
            self.render_table()
            self.crawl(item, item.crawl_depth)

    def action_remove(self) -> None:
        item = self._selected()
        if item is None:
            return
        for gone in item.walk():
            self.items.pop(gone.id, None)
        if item.parent is None:
            self.sources.remove(item)
        else:
            item.parent.children.remove(item)
        self.render_table()

    @on(DataTable.RowSelected)
    def toggle_expanded(self, event: DataTable.RowSelected) -> None:
        item = self.items.get(str(event.row_key.value))
        if item is not None and item.children:
            item.expanded = not item.expanded
            self.render_table(select=item)

    @on(DataTable.RowHighlighted)
    def show_details(self, event: DataTable.RowHighlighted) -> None:
        item = self.items.get(str(event.row_key.value)) if event.row_key else None
        self._show_details(item)

    # -- rendering -----------------------------------------------------------------------

    def render_table(self, select: Optional[Item] = None) -> None:
        table = self.query_one(DataTable)
        keep = select or self._selected()
        table.clear()
        rows: list[Item] = []
        for source in self.sources:
            self._collect_rows(source, rows)
        for item in rows:
            table.add_row(*self._cells(item), key=item.id, height=2)
        if keep is not None and keep in rows:
            table.move_cursor(row=rows.index(keep))
        self._show_details(self._selected())

    def _collect_rows(self, item: Item, rows: list[Item]) -> None:
        rows.append(item)
        if item.expanded:
            for child in item.children:
                self._collect_rows(child, rows)

    def _cells(self, item: Item) -> tuple:
        indent = '\u00a0' * 4 * item.depth  # non-breaking: the table strips leading spaces
        if item.children:
            marker = '▾ ' if item.expanded else '▸ '
        else:
            marker = '  ' if item.depth == 0 else '└ '
        title = _fit(item.label(), LABEL_WIDTH - len(indent) - 2)
        subtitle = _fit(self._subtitle(item), LABEL_WIDTH - len(indent) - 2)
        label = Text(indent + marker)
        label.append(title, style='bold' if item.depth == 0 else '')
        label.append('\n' + indent + '  ' + subtitle, style='dim')
        text, style = STATUS_STYLES[item.status]
        return (label, TYPE_LABELS.get(item.kind, item.kind), _ago(item.added),
                Text(text, style=style))

    def _subtitle(self, item: Item) -> str:
        documents = sum(1 for i in item.walk() if i is not item and i.kind in ASSET_TYPES)
        if item.status == CRAWLING:
            return f'{_short_url(item.url)} · crawling…'
        note = item.outcome_note()
        if item.kind == scraper.PAGE:
            line = f'{documents} document(s) found · {_short_url(item.url)}'
        elif item.kind == scraper.PAGE_TEXT:
            line = f'page text only · {len(item.text.split())} words · {_short_url(item.url)}'
        else:
            line = _short_url(item.url)
        return f'{line} · {note}' if note else line

    def _set_status(self, item: Item, status: str) -> None:
        if item.id not in self.items:
            return
        item.status = status
        text, style = STATUS_STYLES[status]
        try:
            self.query_one(DataTable).update_cell(item.id, 'status', Text(text, style=style))
        except Exception:  # row collapsed out of view
            pass

    def _refresh_times(self) -> None:
        table = self.query_one(DataTable)
        for row_key in list(table.rows):
            item = self.items.get(str(row_key.value))
            if item is not None:
                table.update_cell(row_key, 'added', _ago(item.added))

    def _show_details(self, item: Optional[Item]) -> None:
        details = self.query_one('#details', Static)
        if item is None:
            details.update('Select a row to see details.')
            return
        out = Text()
        out.append(item.label(), style='bold')
        out.append(f'\n{item.url}\n', style='dim')
        out.append(f'{TYPE_LABELS.get(item.kind, item.kind)} · {STATUS_STYLES[item.status][0]}')
        if item.outcome_note():
            out.append(f'\n  {item.outcome_note()}', style='dim')
        for path in item.files:
            out.append(f'\n  saved: {path}', style='green')
        if item.error:
            out.append(f'\n  error: {item.error}', style='red')
        if item.text:
            preview = ' '.join(item.text.split())[:300]
            out.append(f'\n  text: {preview}…', style='italic')
        details.update(out)

    def write_log(self, line: str) -> None:
        self.query_one('#log', Log).write_line(line)

    # -- helpers -------------------------------------------------------------------------

    def _new_item(self, url: str, kind: str, status: str, **fields) -> Item:
        item = Item(id=f'i{next(self._ids)}', url=url, kind=kind, status=status, **fields)
        self.items[item.id] = item
        return item

    def _selected(self) -> Optional[Item]:
        table = self.query_one(DataTable)
        if not table.row_count:
            return None
        try:
            row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        except Exception:
            return None
        return self.items.get(str(row_key.value))


def _short_url(url: str, limit: int = 80) -> str:
    url = url.split('://', 1)[-1].removeprefix('www.')
    return url if len(url) <= limit else url[:limit - 1] + '…'


def _fit(text: str, width: int) -> str:
    """Shorten text to one line of at most width characters."""
    text = ' '.join(text.split())
    return text if len(text) <= width else text[:max(width - 1, 0)] + '…'


def _ago(timestamp: float) -> str:
    seconds = int(time.time() - timestamp)
    if seconds < 60:
        return 'just now'
    if seconds < 3600:
        return f'{seconds // 60} min ago'
    if seconds < 86400:
        return f'{seconds // 3600} h ago'
    return f'{seconds // 86400} d ago'


def main() -> None:
    parser = argparse.ArgumentParser(description='Terminal UI for testing the scraper/downloader.')
    parser.add_argument('--out', type=Path, default=Path('downloads'),
                        help='Directory downloads are saved in (default: downloads)')
    parser.add_argument('--depth', type=int, default=1, help='Default crawl depth (default: 1)')
    parser.add_argument('urls', nargs='*', help='Sources to add at startup')
    args = parser.parse_args()

    IngestApp(args.out, args.depth, args.urls).run()


if __name__ == '__main__':
    main()
