"""The state machine's Lambda handlers, run in order the way Step Functions runs them, against
AWS Emulator (S3, DynamoDB) and a local web site."""
import functools
import json
import types

import pytest

from i2cc_scraper.aws import handlers

LAMBDA = types.SimpleNamespace(get_remaining_time_in_millis=lambda: 900_000)


@pytest.fixture
def env(emulator, bucket, monkeypatch, tmp_path):
    prefix = f'run-{tmp_path.name[-20:]}/'
    for name, value in {'SCRAPER_BUCKET': bucket, 'SCRAPER_PREFIX': prefix,
                        'I2CC_BLOCK_PRIVATE_ADDRESSES': '0', 'I2CC_CRAWL_DELAY': '0',
                        'I2CC_RETRY_BACKOFF': '0', 'I2CC_RETRY_JITTER': '0'}.items():
        monkeypatch.setenv(name, value)
    for name in ('PROCESSED_FILES_TABLE', 'OPENSEARCH_ENDPOINT', 'GOOGLE_CREDENTIALS_SECRET',
                 'I2CC_GOOGLE_CREDENTIALS_JSON', 'GOOGLE_APPLICATION_CREDENTIALS'):
        monkeypatch.delenv(name, raising=False)
    handlers._setup.cache_clear()
    handlers._drive_client.cache_clear()
    yield types.SimpleNamespace(s3=emulator.s3, dynamodb=emulator.dynamodb, bucket=bucket,
                                prefix=prefix)
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


def test_an_item_whose_retries_ran_out_is_reported(env, forum):
    crawled = handlers.crawl({'input': {'url': forum.url('/forum/')}, 'run_id': 'r'}, LAMBDA)
    planned = handlers.plan(crawled, LAMBDA)                   # ... and no item is ingested
    summary = handlers.report(planned, LAMBDA)
    assert summary['counts'] == {'new': 1, 'failed': 2}
    assert {f['reason'] for f in summary['failed']} == {'unavailable'}


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
