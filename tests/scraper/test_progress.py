"""Database behavior against AWS Emulator, including races, retries, graphs, and expiry."""
from concurrent.futures import ThreadPoolExecutor

import pytest

from i2cc_scraper.aws.progress import ProgressStore, RETENTION_SECONDS, identity

ROOT = 'https://internet2.edu/events/'
PAGE = 'https://internet2.edu/events/meeting/'
PDF = 'https://internet2.edu/slides.pdf'


def test_source_claim_is_atomic_and_reuses_active_run(progress_store):
    # Separate resources, as in independent Lambda invocations (resources aren't thread safe).
    def claim(_):
        return ProgressStore(progress_store.table.name).claim(ROOT)
    with ThreadPoolExecutor(max_workers=4) as workers:
        claims = list(workers.map(claim, range(8)))
    assert len({c.run_id for c in claims}) == 1
    assert sum(c.created for c in claims) == 1
    assert len(progress_store.list_sources()['items']) == 1
    assert len(progress_store.list_runs(identity(ROOT))['items']) == 1


def test_shared_children_have_multiple_edges_but_one_status(progress_store):
    run = progress_store.claim(ROOT)
    observer = progress_store.reporter(run.run_id)
    observer.discovered(ROOT, 'page')
    observer.discovered(PAGE, 'page', parent=ROOT)
    observer.discovered(PDF, 'file', parent=ROOT, title='Slides')
    observer.discovered(PDF, 'file', parent=PAGE, title='Slides')
    observer.discovered(ROOT, 'page', parent=PAGE)  # cycles preserve provenance, not extra work
    observer.content(PDF, 'checking')
    observer.content(PDF, 'stored', result={'status': 'new', 'files': ['slides.pdf']})
    observer.discovered(PDF, 'file', parent=PAGE)
    assert progress_store.summarize(run.run_id)['discovered'] == 3
    assert progress_store.get_item(run.run_id, PDF)['content_status'] == 'stored'
    assert len(progress_store.list_children(run.run_id, ROOT)['items']) == 2
    assert len(progress_store.list_children(run.run_id, PAGE)['items']) == 2


def test_retries_fence_old_attempts_and_do_not_reset_success(progress_store):
    run = progress_store.claim(ROOT)
    first = progress_store.reporter(run.run_id)
    second = progress_store.reporter(run.run_id)
    first.discovered(PDF, 'file')
    first.content(PDF, 'checking')
    first.content(PDF, 'retrying', error='rate limited', reason='unavailable')
    second.content(PDF, 'checking')
    first.content(PDF, 'failed', error='late failure')
    assert progress_store.get_item(run.run_id, PDF)['content_status'] == 'checking'
    second.content(PDF, 'stored', result={'status': 'unchanged'})
    first.content(PDF, 'checking')
    item = progress_store.get_item(run.run_id, PDF)
    assert item['content_status'] == 'stored' and item['attempt'] == 2
    assert item['result']['status'] == 'unchanged'


def test_discovery_refines_types_without_downgrading_a_folder(progress_store):
    run = progress_store.claim(ROOT)
    observer = progress_store.reporter(run.run_id)
    observer.discovered(ROOT, 'page')
    observer.discovered(ROOT, 'page_text', title='Events')
    assert progress_store.get_source(run.source_id)['kind'] == 'page_text'
    assert progress_store.get_source(run.source_id)['title'] == 'Events'
    observer.discovered(PDF, 'page')
    observer.discovered(PDF, 'file')
    assert progress_store.get_item(run.run_id, PDF)['kind'] == 'file'
    folder = 'https://drive.google.com/drive/folders/root'
    observer.discovered(folder, 'google_drive')
    observer.discovered(folder, 'folder')
    observer.discovered(folder, 'google_drive')
    assert progress_store.get_item(run.run_id, folder)['kind'] == 'folder'


def test_finalizer_records_missing_results_and_releases_source(progress_store):
    run = progress_store.claim(ROOT)
    observer = progress_store.reporter(run.run_id)
    observer.discovered(ROOT, 'page')
    observer.discovery(ROOT, 'complete')
    observer.content(ROOT, 'stored', result={'status': 'new'})
    observer.discovered(PDF, 'file', parent=ROOT)
    progress_store.finish(run.run_id)
    final = progress_store.get_run(run.run_id)
    assert final['status'] == 'partial_failure'
    assert final['counts']['content'] == {'stored': 1, 'failed': 1}
    assert progress_store.list_runs(run.source_id)['items'][0]['status'] == 'partial_failure'
    assert progress_store.get_item(run.run_id, PDF)['reason'] == 'unavailable'
    assert 'active_run_id' not in progress_store.get_source(run.source_id)
    observer.discovered(PAGE, 'page', parent=ROOT)  # late discovery cannot resurrect work
    observer.content(PDF, 'checking')
    assert progress_store.get_item(run.run_id, PAGE) is None
    assert progress_store.get_item(run.run_id, PDF)['content_status'] == 'failed'
    again = progress_store.claim(ROOT)
    assert again.created and again.run_id != run.run_id
    progress_store.finish(run.run_id)  # replaying old finalizer must not release the new lock
    assert progress_store.get_source(run.source_id)['active_run_id'] == again.run_id


def test_expired_rows_are_hidden_but_source_survives(progress_store):
    now = [1_800_000_000]
    progress_store.clock = lambda: now[0]
    run = progress_store.claim(ROOT)
    observer = progress_store.reporter(run.run_id)
    observer.discovered(ROOT, 'page')
    observer.discovered(PDF, 'file', parent=ROOT)
    row = progress_store.get_item(run.run_id, PDF)
    assert row['expires_at'] == now[0] + RETENTION_SECONDS
    now[0] += RETENTION_SECONDS + 1
    assert progress_store.get_run(run.run_id) is None
    assert progress_store.get_item(run.run_id, PDF) is None
    assert progress_store.list_items(run.run_id)['items'] == []
    assert progress_store.list_children(run.run_id, ROOT)['items'] == []
    assert progress_store.list_runs(run.source_id)['items'] == []
    assert progress_store.get_source(run.source_id) is not None


def test_query_pagination_and_provider_identity(progress_store):
    assert identity('https://youtu.be/VIDEO1') == identity('https://www.youtube.com/watch?v=VIDEO1')
    run = progress_store.claim(ROOT)
    observer = progress_store.reporter(run.run_id)
    for i in range(7):
        observer.discovered(f'https://internet2.edu/{i}.pdf', 'file', parent=ROOT)
    rows, after = [], None
    while True:
        page = progress_store.list_items(run.run_id, limit=2, after=after)
        rows += page['items']
        after = page['next_key']
        if not after:
            break
    assert len(rows) == 7
    assert progress_store.summarize(run.run_id)['discovered'] == 7
    with pytest.raises(ValueError):
        progress_store.list_items(run.run_id, limit=0)


def test_starter_reuses_run_and_recovers_uncertain_start(progress_store):
    import boto3
    from botocore.exceptions import EndpointConnectionError
    from i2cc_scraper.aws.progress import RunStarter
    class Starts:
        exceptions = boto3.client('stepfunctions').exceptions
        calls = []
        def start_execution(self, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                raise EndpointConnectionError(endpoint_url='https://127.0.0.1:9297')
            return {'executionArn': 'arn:aws:states:us-east-1:000000000000:execution:scraper:r'}
    client = Starts()
    starter = RunStarter(progress_store, 'arn:aws:states:us-east-1:000000000000:stateMachine:scraper',
                         client=client)
    with pytest.raises(EndpointConnectionError):
        starter.start(ROOT)
    recovered = starter.start(ROOT, depth=3)  # original request's depth is retained
    repeated = starter.start(ROOT)
    assert recovered['run_id'] == repeated['run_id']
    assert client.calls[0] == client.calls[1]
    assert len(client.calls) == 2
    assert not recovered['created']


def test_finalization_can_resume_after_its_worker_dies(progress_store):
    run = progress_store.claim(ROOT)
    observer = progress_store.reporter(run.run_id)
    observer.discovered(PDF, 'file')
    progress_store._edit('RUN#' + run.run_id, 'META', lambda row: row.update(
        status='finalizing', requested_status='timed_out', error='Execution timed out',
        reason='timeout', report_key=''))
    assert progress_store.claim(ROOT).run_id == run.run_id
    observer.content(PDF, 'stored', result={'status': 'new'})  # late worker is fenced out
    progress_store.finish(run.run_id)
    assert progress_store.get_run(run.run_id)['status'] == 'timed_out'
    assert progress_store.get_item(run.run_id, PDF)['content_status'] == 'failed'
    assert 'active_run_id' not in progress_store.get_source(run.source_id)
