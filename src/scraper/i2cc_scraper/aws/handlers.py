"""Lambda handlers of the scraper state machine. One container image; each function runs one:

    crawl   {"input": {"url", "depth"?}, "run_id"}  crawl from url, storing each page as it's read
    plan    crawl's output                          dedupe the assets, expand Drive folders, dedupe
    ingest  {"run_id", "item"}                      download and store one item (a Distributed Map
                                                    runs it for every item in plan's items file)
    report  plan's output                           what happened: the state machine's output

Steps hand each other S3 keys, not data: a run's files are under <prefix>.runs/<run_id>/ (Step
Functions state is limited to 256 KB). ingest raises TransientError for a failure worth
retrying, which the state machine retries with backoff; everything else is a result.

Environment: SCRAPER_BUCKET, SCRAPER_PREFIX; PROCESSED_FILES_TABLE, OPENSEARCH_ENDPOINT and
INDEX_NAME (i2ccc's, so replaced files get ingested again; optional); GOOGLE_CREDENTIALS_SECRET
(a Secrets Manager secret holding a service-account key; optional: public links without it);
and I2CC_* settings (config.Config.from_env), e.g. I2CC_ALLOWED_START_DOMAINS=internet2.edu.
"""
import functools
import hashlib
import logging
import os
import re
from collections import Counter
from dataclasses import asdict
from typing import Optional

from .. import config as config_module
from ..config import Config
from ..downloader import (Context, DeadlineExceeded, DownloadError, DownloadResult, Reason,
                          Source, classify_error, crawl_into_store, dedupe, expand,
                          failed_result, ingest as ingest_one)
from ..downloader.models import FAILED
from ..logs import setup_logging
from ..progress import NULL_PROGRESS
from .progress import ProgressStore, RunStarter
from .invalidate import IngestInvalidator, opensearch_client
from .s3store import S3Store, read_json, write_json

log = logging.getLogger('i2cc_scraper.aws')

DEFAULT_PREFIX = 'i2cc-scraper/'
MAX_DEPTH = 3
MARGIN = 15.0     # seconds kept back from the invocation's time, to write the result and return
RUN_ID = re.compile(r'[A-Za-z0-9_-]{1,80}')


@functools.cache
def _setup() -> tuple[S3Store, Config]:
    """What every invocation in this container shares: the store and the settings."""
    setup_logging()
    config = Config.from_env()
    config_module.set_default(config)
    store = S3Store(os.environ['SCRAPER_BUCKET'], os.environ.get('SCRAPER_PREFIX', DEFAULT_PREFIX),
                    invalidator=_invalidator())
    return store, config


def _invalidator() -> Optional[IngestInvalidator]:
    table, endpoint = os.environ.get('PROCESSED_FILES_TABLE'), os.environ.get('OPENSEARCH_ENDPOINT')
    if not table and not endpoint:
        return None
    search = opensearch_client(endpoint, os.environ['AWS_REGION']) if endpoint else None
    return IngestInvalidator(table, search, os.environ.get('INDEX_NAME', ''))


@functools.cache
def _drive_client():
    """The Drive client, read from the secret on first use. Only the steps that touch Drive (plan,
    ingest) call it: they alone are allowed to read the secret."""
    from ..drive_api import DriveClient
    secret = os.environ.get('GOOGLE_CREDENTIALS_SECRET')
    if not secret:
        return DriveClient.from_env()
    import boto3
    client = boto3.client('secretsmanager')
    try:
        key = client.get_secret_value(SecretId=secret).get('SecretString')
    except client.exceptions.ResourceNotFoundException:
        key = None  # created by CDK, never given a value
    if not key or not key.lstrip().startswith('{'):
        log.warning('No Google service-account key in %s: using public Drive links', secret)
        return None
    return DriveClient(key)


def _progress_store():
    name = os.environ.get('SCRAPER_PROGRESS_TABLE')
    return ProgressStore(name) if name else None


def _context(lambda_context, drive: bool = False, run_id: str = '') -> Context:
    store, config = _setup()
    budget = lambda_context.get_remaining_time_in_millis() / 1000 - MARGIN
    progress_store = _progress_store()
    progress = progress_store.reporter(run_id) if progress_store and run_id else NULL_PROGRESS
    return Context(store, config=config, drive=_drive_client() if drive else None, budget=budget,
                   progress=progress,
                   reserve=min(60.0, budget * 0.1))


def _run_id(event: dict) -> str:
    run_id = event.get('run_id')
    if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
        raise ValueError(f'"run_id" must be 1-80 letters, digits, "-" or "_": {run_id!r}')
    return run_id


def _run_key(store: S3Store, run_id: str, name: str) -> str:
    return f'{store.prefix}.runs/{run_id}/{name}'


def _result_name(url: str) -> str:
    return hashlib.sha1(url.encode()).hexdigest() + '.json'


def _failure(url: str, kind: str, exc: Exception, title: str = '') -> DownloadResult:
    error = classify_error(exc)
    if error is None:
        log.exception('Unexpected error for %s', url)
        error = DownloadError(f'{type(exc).__name__}: {exc}', Reason.ERROR)
    return failed_result(url, kind, error, title)


# --- the steps ------------------------------------------------------------------------------------

def _request(request):
    url, depth = request.get('url'), request.get('depth', 1)
    if not isinstance(url, str) or not url.strip() or len(url) > 2048:
        raise ValueError('"url" must be the address of a page to crawl')
    if isinstance(depth, bool) or not isinstance(depth, int) or not 0 <= depth <= MAX_DEPTH:
        raise ValueError(f'"depth" must be a whole number from 0 to {MAX_DEPTH}')
    return url.strip(), depth


def start(event: dict, lambda_context) -> dict:
    """Invoke from the admin frontend's role. Returns the active run on repeated submissions."""
    url, depth = _request(event)
    from ..netguard import validate_start_url
    from ..url_classifier import classify_url
    if classify_url(url) is None:
        validate_start_url(url, Config.from_env())
    store = _progress_store()
    if store is None:
        raise RuntimeError('SCRAPER_PROGRESS_TABLE is required')
    return RunStarter(store, os.environ['SCRAPER_STATE_MACHINE_ARN']).start(url, depth)


def _step(phase):
    """Finalize an infrastructure failure; document failures are handled in the item task."""
    def decorate(fn):
        @functools.wraps(fn)
        def invoke(event, lambda_context):
            store = _progress_store()
            run_id = _run_id(event)
            if store and phase != 'crawling':
                store.phase(run_id, phase)
            try:
                return fn(event, lambda_context)
            except Exception as exc:
                if store:
                    store.finish(run_id, status='failed', error=str(exc), reason=phase)
                raise
        return invoke
    return decorate


@_step('crawling')
def crawl(event: dict, lambda_context) -> dict:
    url, depth = _request(event.get('input') or {})
    run_id = _run_id(event)
    progress_store = _progress_store()
    if progress_store:
        claim = progress_store.claim(url, depth, run_id=run_id)
        if claim.run_id != run_id:
            raise ValueError(f'Source already has an active run: {claim.run_id}')
        progress_store.phase(run_id, 'crawling')
        if event.get('execution_arn'):
            progress_store.bind_execution(run_id, event['execution_arn'])
    store = _setup()[0]
    with _context(lambda_context, run_id=run_id) as ctx:
        report = crawl_into_store(url.strip(), ctx, depth,
                                  time_limit=max(1.0, ctx.time_left() - ctx.reserve))
    key = _run_key(store, run_id, 'crawl.json')
    write_json(store.client, store.bucket, key, {
        'url': url, 'depth': depth, 'truncated': report.truncated,
        'pages': [asdict(r) for r in report.pages],
        'problems': [asdict(r) for r in report.problems],
        'assets': [asdict(r) for r in report.assets]})
    return {'run_id': run_id, 'url': url, 'bucket': store.bucket, 'crawl_key': key,
            'pages': len(report.pages), 'assets': len(report.assets)}


@_step('planning')
def plan(event: dict, lambda_context) -> dict:
    run_id = _run_id(event)
    store = _setup()[0]
    found = read_json(store.client, store.bucket, _run_key(store, run_id, 'crawl.json'))
    items, failures = [], []
    with _context(lambda_context, drive=True, run_id=run_id) as ctx:
        for asset in dedupe(found['assets'], ctx.options):
            try:
                expanded = expand(asset, ctx)
            except Exception as exc:  # one folder that can't be listed doesn't stop the run
                failures.append(_failure(asset.url, asset.kind, exc, asset.link_text))
                continue
            items += expanded.items
            failures += expanded.failures
        items = dedupe(items, ctx.options)
        for failure in failures:
            ctx.progress.content(failure.url, 'failed', result=asdict(failure),
                                 error=failure.error, reason=failure.reason)
    items_key = _run_key(store, run_id, 'items.json')
    write_json(store.client, store.bucket, items_key, [asdict(i) for i in items])
    write_json(store.client, store.bucket, _run_key(store, run_id, 'plan.json'),
               {'failures': [asdict(f) for f in failures]})
    progress_store = _progress_store()
    if progress_store:
        progress_store.phase(run_id, 'downloading', discovery_complete=not found['truncated'])
    return {'run_id': run_id, 'url': found['url'], 'bucket': store.bucket,
            'items_key': items_key, 'items': len(items), 'failures': len(failures)}


def ingest(event: dict, lambda_context) -> dict:
    run_id = _run_id(event)
    store = _setup()[0]
    item = Source.of(event['item'])
    with _context(lambda_context, drive=True, run_id=run_id) as ctx:
        try:
            result = ingest_one(item, ctx)    # TransientError: raised, for the retry
        except DeadlineExceeded as exc:       # a retry would run out of time the same way
            result = failed_result(item.url, item.kind, exc, item.link_text)
            ctx.progress.content(item.url, 'failed', result=asdict(result),
                                 error=result.error, reason=result.reason)
    write_json(store.client, store.bucket,
               _run_key(store, run_id, 'results/' + _result_name(item.url)), asdict(result))
    return {'status': result.status}         # small: a Map collects every item's output


@_step('reporting')
def report(event: dict, lambda_context) -> dict:
    run_id = _run_id(event)
    store = _setup()[0]

    def load(name):
        return read_json(store.client, store.bucket, _run_key(store, run_id, name))

    found, planned, items = load('crawl.json'), load('plan.json'), load('items.json')
    rows = found['pages'] + found['problems'] + planned['failures']
    for item in items:
        try:
            rows.append(load('results/' + _result_name(item['url'])))
        except store.client.exceptions.NoSuchKey:  # every retry failed, or the function died
            source = Source.of(item)
            rows.append(asdict(DownloadResult(
                url=source.url, type=source.kind, title=source.link_text, reason=Reason.UNAVAILABLE,
                error='Not stored: it kept failing temporarily (or timed out) after retries. '
                      'Run it again later.')))
    key = _run_key(store, run_id, 'report.json')
    write_json(store.client, store.bucket, key, {'url': found['url'], 'results': rows})
    failed = [{k: r[k] for k in ('url', 'title', 'reason', 'error')}
              for r in rows if r['status'] == FAILED]
    progress_store = _progress_store()
    if progress_store:
        progress_store.finish(run_id, report_key=key)
    return {'run_id': run_id, 'url': found['url'],
            'counts': dict(Counter(r['status'] for r in rows)), 'truncated': found['truncated'],
            'failed': failed[:50], 'report_key': key}


def execution_status(event: dict, lambda_context) -> None:
    """Reconcile terminal executions, including tasks killed before reporting a result.

    EventBridge events give prompt updates. A scheduled invocation checks active sources as
    a fallback for lost events and repairs a starter killed between claiming and starting.
    """
    store = _progress_store()
    if store is None:
        return
    if 'failure' in event:
        failure = event['failure']
        store.finish(_run_id(event), status='failed', error=failure.get('Cause', ''),
                     reason=failure.get('Error', 'workflow_error'))
        return
    import boto3
    client = boto3.client('stepfunctions')
    detail = event.get('detail')
    if detail:
        runs = [{'run_id': detail['name'], 'execution_arn': detail['executionArn']}]
    else:
        def active_runs():
            after = None
            while True:
                page = store.list_sources(after=after)
                for source in page['items']:
                    if source.get('active_run_id'):
                        run = store.get_run(source['active_run_id'])
                        if run:
                            yield run
                after = page['next_key']
                if not after:
                    break
        runs = active_runs()
    for run in runs:
        arn = run.get('execution_arn')
        if not arn:
            if run.get('status') == 'queued':
                RunStarter(store, os.environ['SCRAPER_STATE_MACHINE_ARN'], client=client).start(
                    run['url'], int(run['depth']))
            continue
        state = client.describe_execution(executionArn=arn)
        status = state['status']
        if status in ('FAILED', 'TIMED_OUT', 'ABORTED', 'SUCCEEDED'):
            store.finish(run['run_id'], status=(None if status == 'SUCCEEDED' else status.lower()),
                         error=state.get('cause', ''), reason=state.get('error', ''))
