"""The state machine's Lambda handlers, run in order the way Step Functions runs them, against
AWS Emulator (S3, DynamoDB) and a local web site."""
import functools
import json
import types

import pytest

from i2cc_scraper.aws import handlers

LAMBDA = types.SimpleNamespace(get_remaining_time_in_millis=lambda: 900_000)


@pytest.fixture
def env(emulator, bucket, progress_store, monkeypatch, tmp_path):
    prefix = f'run-{tmp_path.name[-20:]}/'
    for name, value in {'SCRAPER_BUCKET': bucket, 'SCRAPER_PREFIX': prefix,
                        'I2CC_BLOCK_PRIVATE_ADDRESSES': '0', 'I2CC_CRAWL_DELAY': '0',
                        'I2CC_RETRY_BACKOFF': '0', 'I2CC_RETRY_JITTER': '0',
                        'SCRAPER_PROGRESS_TABLE': progress_store.table.name}.items():
        monkeypatch.setenv(name, value)
    for name in ('PROCESSED_FILES_TABLE', 'OPENSEARCH_ENDPOINT', 'GOOGLE_CREDENTIALS_SECRET',
                 'I2CC_GOOGLE_CREDENTIALS_JSON', 'GOOGLE_APPLICATION_CREDENTIALS'):
        monkeypatch.delenv(name, raising=False)
    handlers._setup.cache_clear()
    handlers._drive_client.cache_clear()
    yield types.SimpleNamespace(s3=emulator.s3, dynamodb=emulator.dynamodb, bucket=bucket,
                                prefix=prefix, progress=progress_store)
    handlers._setup.cache_clear()
    handlers._drive_client.cache_clear()


def run(url, run_id='run-1', depth=1):
    """What the state machine does: crawl, plan, ingest every item (the Map), report."""
    crawled = handlers.crawl({'input': {'url': url, 'depth': depth}, 'run_id': run_id}, LAMBDA)
    planned = handlers.plan(crawled, LAMBDA)
    store = handlers._setup()[0]
    items = json.loads(store.client.get_object(Bucket=planned['bucket'],
                                               Key=planned['items_key'])['Body'].read())
    for item in items:
        handlers.ingest({'run_id': run_id, 'item': item}, LAMBDA)
    return handlers.report(planned, LAMBDA)


def objects(env):
    response = env.s3.list_objects_v2(Bucket=env.bucket, Prefix=env.prefix)
    return sorted(o['Key'][len(env.prefix):] for o in response.get('Contents', []))


@pytest.fixture
def forum(site, fake_yt_dlp):
    site.route('/files/Agenda.pdf', b'%PDF agenda', content_type='application/pdf')
    site.page('/forum/', '<h2>Day 1</h2><a href="/files/Agenda.pdf">Agenda</a> '
                         '<a href="https://www.youtube.com/watch?v=VIDEO1">Keynote</a>')
    return site


def test_a_submitted_url_ends_up_in_s3_ready_for_ingestion(env, forum):
    summary = run(forum.url('/forum/'))
    assert summary['counts'] == {'new': 3} and summary['failed'] == []
    content = [k for k in objects(env) if not k.startswith('.runs/')
               and not k.endswith('.metadata.json')]
    assert sorted(k.rsplit('/', 1)[1] for k in content) == [
        'Agenda.pdf', 'Keynote recording.en.vtt', 'Keynote recording.mp4', 'forum.txt']
    assert not [k for k in objects(env) if k.startswith('.staging/')]
    report = json.loads(env.s3.get_object(Bucket=env.bucket, Key=env.prefix + summary['report_key']
                                          [len(env.prefix):])['Body'].read())
    assert {r['status'] for r in report['results']} == {'new'}
    assert env.progress.get_run('run-1')['status'] == 'complete'
    assert env.progress.summarize('run-1')['content'] == {'stored': 3}


def test_running_it_again_changes_nothing(env, forum):
    run(forum.url('/forum/'), 'run-1')
    before = [k for k in objects(env) if not k.startswith('.runs/')]
    summary = run(forum.url('/forum/'), 'run-2')
    assert summary['counts'] == {'unchanged': 3}
    assert [k for k in objects(env) if not k.startswith('.runs/')] == before


def test_a_changed_file_is_replaced_and_ingestion_told(env, forum, monkeypatch):
    table = 'processed-files-handlers'
    try:
        env.dynamodb.create_table(
            TableName=table, BillingMode='PAY_PER_REQUEST',
            KeySchema=[{'AttributeName': 's3_uri', 'KeyType': 'HASH'}],
            AttributeDefinitions=[{'AttributeName': 's3_uri', 'AttributeType': 'S'}])
        env.dynamodb.get_waiter('table_exists').wait(TableName=table)
    except env.dynamodb.exceptions.ResourceInUseException:
        pass
    monkeypatch.setenv('PROCESSED_FILES_TABLE', table)
    handlers._setup.cache_clear()
    run(forum.url('/forum/'), 'run-1')
    old = next(k for k in objects(env) if k.endswith('/Agenda.pdf'))
    old_uri = f's3://{env.bucket}/{env.prefix}{old}'
    env.dynamodb.put_item(TableName=table, Item={'s3_uri': {'S': old_uri}})   # ingested

    forum.route('/files/Agenda.pdf', b'%PDF agenda, revised', content_type='application/pdf')
    summary = run(forum.url('/forum/'), 'run-2')
    assert summary['counts'] == {'unchanged': 2, 'updated': 1}
    new = next(k for k in objects(env) if k.endswith('/Agenda.pdf'))
    assert new != old and old not in objects(env)
    assert 'Item' not in env.dynamodb.get_item(TableName=table, Key={'s3_uri': {'S': old_uri}})


def test_failures_are_reported_not_raised(env, site, fake_yt_dlp):
    site.page('/forum/', '<a href="/files/gone.pdf">Gone</a>')
    summary = run(site.url('/forum/'))
    assert summary['counts'] == {'new': 1, 'failed': 1}
    assert [(f['reason'], f['url']) for f in summary['failed']] == [
        ('not_found', site.url('/files/gone.pdf'))]
    assert env.progress.get_run('run-1')['status'] == 'partial_failure'
    assert env.progress.get_item('run-1', site.url('/files/gone.pdf'))['reason'] == 'not_found'


def test_an_item_whose_retries_ran_out_is_reported(env, forum):
    crawled = handlers.crawl({'input': {'url': forum.url('/forum/')}, 'run_id': 'r'}, LAMBDA)
    planned = handlers.plan(crawled, LAMBDA)                   # ... and no item is ingested
    summary = handlers.report(planned, LAMBDA)
    assert summary['counts'] == {'new': 1, 'failed': 2}
    assert {f['reason'] for f in summary['failed']} == {'unavailable'}
    assert env.progress.summarize('r')['content'] == {'stored': 1, 'failed': 2}


def test_nested_pages_preserve_shared_links_before_downloads_start(env, site):
    site.page('/events/', '<a href="/meeting/">Meeting</a><a href="/slides.pdf">Slides</a>')
    site.page('/meeting/', '<a href="/slides.pdf">Same slides</a><a href="/events/">Back</a>')
    handlers.crawl({'input': {'url': site.url('/events/')}, 'run_id': 'r'}, LAMBDA)
    db = env.progress
    assert db.summarize('r')['discovered'] == 3
    assert db.get_item('r', site.url('/slides.pdf'))['content_status'] == 'queued'
    assert db.get_item('r', site.url('/meeting/'))['content_status'] == 'stored'
    for parent in ('/events/', '/meeting/'):
        assert site.url('/slides.pdf') in {
            e['url'] for e in db.list_children('r', site.url(parent))['items']}


def test_drive_subfolders_empty_folders_and_cycles_are_preserved(env, site, monkeypatch):
    from test_downloader import FakeDrive, ROOT_URL, G
    drive = FakeDrive()
    original = drive.list_folder
    def listing(folder_id):
        if folder_id == 'empty':
            return []
        rows = list(original(folder_id))
        if folder_id == 'root':
            rows.append({'id': 'empty', 'name': 'Empty folder', 'mimeType': G + 'folder'})
        if folder_id == 'sub':
            rows.append(drive.files['deck'])  # same file through another parent
        return rows
    drive.list_folder = listing
    monkeypatch.setattr(handlers, '_drive_client', functools.cache(lambda: drive))
    site.page('/events/', f'<a href="{ROOT_URL}">Slides folder</a>')
    crawled = handlers.crawl({'input': {'url': site.url('/events/')}, 'run_id': 'r'}, LAMBDA)
    handlers.plan(crawled, LAMBDA)
    db = env.progress
    sub = 'https://drive.google.com/drive/folders/sub'
    empty = 'https://drive.google.com/drive/folders/empty'
    assert db.get_item('r', empty)['discovery_status'] == 'complete'
    assert db.get_item('r', empty)['content_status'] == 'not_applicable'
    assert ROOT_URL in {e['url'] for e in db.list_children('r', sub)['items']}
    root_children = {e['item_id'] for e in db.list_children('r', ROOT_URL)['items']}
    sub_children = {e['item_id'] for e in db.list_children('r', sub)['items']}
    assert root_children & sub_children  # the shared deck has one identity
    assert db.get_item('r', sub)['kind'] == 'folder'


def test_truncated_discovery_is_distinct_from_an_empty_page(env, site, monkeypatch):
    monkeypatch.setenv('I2CC_MAX_PAGES', '1')
    handlers._setup.cache_clear()
    site.page('/events/', '<a href="/meeting/">Meeting</a>')
    site.page('/meeting/', '<p>Not read</p>')
    summary = run(site.url('/events/'))
    assert summary['truncated']
    assert env.progress.get_item('run-1', site.url('/meeting/'))['discovery_status'] == 'deferred'
    assert not env.progress.get_run('run-1')['discovery_complete']


@pytest.mark.parametrize('status', ['TIMED_OUT', 'ABORTED', 'FAILED'])
def test_terminal_execution_events_finalize_unfinished_work(env, monkeypatch, status):
    import boto3
    url = 'https://internet2.edu/slides.pdf'
    claim = env.progress.claim(url)
    arn = 'arn:aws:states:us-east-1:000000000000:execution:scraper:' + claim.run_id
    env.progress.bind_execution(claim.run_id, arn)
    progress = env.progress.reporter(claim.run_id)
    progress.discovered(url, 'file')
    progress.content(url, 'checking')
    original_client = boto3.client
    class Executions:
        def describe_execution(self, *, executionArn):
            assert executionArn == arn
            return {'status': status, 'cause': 'Execution stopped', 'error': 'Stopped'}
    monkeypatch.setattr(boto3, 'client', lambda service, **kw: (
        Executions() if service == 'stepfunctions' else original_client(service, **kw)))
    handlers.execution_status({'detail': {'name': claim.run_id, 'executionArn': arn}}, LAMBDA)
    assert env.progress.get_run(claim.run_id)['status'] == status.lower()
    assert env.progress.get_item(claim.run_id, url)['content_status'] == 'failed'
    assert 'active_run_id' not in env.progress.get_source(claim.source_id)


def test_download_status_is_visible_while_bytes_are_being_fetched(env, monkeypatch):
    from test_downloader import FakeDrive
    url = 'https://drive.google.com/file/d/pdf/view'
    drive = FakeDrive()
    observed = []
    original = drive.download
    def download(file_id):
        observed.append(env.progress.get_item('r', url)['content_status'])
        return original(file_id)
    drive.download = download
    monkeypatch.setattr(handlers, '_drive_client', functools.cache(lambda: drive))
    summary = run(url, run_id='r')
    assert summary['counts'] == {'new': 1}
    assert observed == ['downloading']
    assert env.progress.get_item('r', url)['content_status'] == 'stored'


def test_a_broken_workflow_step_releases_its_source(env):
    claim = env.progress.claim('https://internet2.edu/events/')
    with pytest.raises(env.s3.exceptions.NoSuchKey):
        handlers.plan({'run_id': claim.run_id}, LAMBDA)
    assert env.progress.get_run(claim.run_id)['status'] == 'failed'
    assert 'active_run_id' not in env.progress.get_source(claim.source_id)


@pytest.mark.parametrize('request_input, run_id', [
    ({}, 'r'), ({'url': 42}, 'r'), ({'url': 'https://x.org/', 'depth': 9}, 'r'),
    ({'url': 'https://x.org/', 'depth': True}, 'r'), ({'url': 'https://x.org/'}, '../x'),
])
def test_bad_input_is_refused(env, request_input, run_id):
    with pytest.raises(ValueError):
        handlers.crawl({'input': request_input, 'run_id': run_id}, LAMBDA)


def test_a_url_outside_the_allowed_domains_is_refused(env, monkeypatch):
    from i2cc_scraper.netguard import UnsafeURLError
    monkeypatch.setenv('I2CC_ALLOWED_START_DOMAINS', 'internet2.edu')
    handlers._setup.cache_clear()
    with pytest.raises(UnsafeURLError):
        handlers.crawl({'input': {'url': 'https://example.org/'}, 'run_id': 'r'}, LAMBDA)


def test_only_the_steps_that_use_drive_read_its_credentials(env, forum, monkeypatch):
    """The Google key is readable by plan and ingest alone (cdk/scraper.py grants nothing else),
    so crawl and report must not even ask for it."""
    asked = []
    monkeypatch.setattr(handlers, '_drive_client', functools.cache(lambda: asked.append(1)))
    crawled = handlers.crawl({'input': {'url': forum.url('/forum/')}, 'run_id': 'r'}, LAMBDA)
    assert asked == []
    planned = handlers.plan(crawled, LAMBDA)
    assert asked == [1]                                    # once; the client is then kept
    store = handlers._setup()[0]
    items = json.loads(store.client.get_object(Bucket=planned['bucket'],
                                               Key=planned['items_key'])['Body'].read())
    handlers.ingest({'run_id': 'r', 'item': items[0]}, LAMBDA)
    assert asked == [1]
    handlers.report(planned, LAMBDA)
    assert asked == [1]
