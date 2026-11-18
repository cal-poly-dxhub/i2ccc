"""DynamoDB catalog, run history, and discovery graph. No HTTP or search-ingestion state.

Source records persist; run records, items, edges, and history expire after 30 days. All run
writes check the run is still active, and item updates use optimistic versions. Query helpers
hide logically expired records before DynamoDB's asynchronous TTL deletion catches up.
"""
import hashlib
import json
import re
import time
import uuid
from collections import Counter
from dataclasses import asdict, dataclass

import boto3
from boto3.dynamodb.conditions import Attr, Key

from ..scraper import url_key

RETENTION_SECONDS = 30 * 86400
ACTIVE = ('queued', 'running')
LOCKED = (*ACTIVE, 'finalizing')
TERMINAL_CONTENT = ('stored', 'failed', 'deferred', 'not_applicable', 'skipped')


def identity(url: str) -> str:
    """Stable before fetching, including URL variants of the same provider object."""
    return hashlib.sha256(url_key(url).encode()).hexdigest()


def table_definition(name: str) -> dict:
    """Shared schema for emulator setup. Production resources are owned by CDK."""
    return {
        'TableName': name, 'BillingMode': 'PAY_PER_REQUEST',
        'KeySchema': [{'AttributeName': 'pk', 'KeyType': 'HASH'},
                      {'AttributeName': 'sk', 'KeyType': 'RANGE'}],
        'AttributeDefinitions': [
            {'AttributeName': field, 'AttributeType': 'S'}
            for field in ('pk', 'sk', 'catalog_pk', 'catalog_sk')],
        'GlobalSecondaryIndexes': [{
            'IndexName': 'sources',
            'KeySchema': [{'AttributeName': 'catalog_pk', 'KeyType': 'HASH'},
                          {'AttributeName': 'catalog_sk', 'KeyType': 'RANGE'}],
            'Projection': {'ProjectionType': 'ALL'}}],
    }


@dataclass(frozen=True)
class Claim:
    source_id: str
    run_id: str
    created: bool


class ProgressStore:
    def __init__(self, table_name: str, *, table=None, clock=time.time):
        self.table = table if table is not None else boto3.resource('dynamodb').Table(table_name)
        self.client = self.table.meta.client
        self.clock = clock

    def _get(self, pk: str, sk: str) -> dict | None:
        row = self.table.get_item(Key={'pk': pk, 'sk': sk}, ConsistentRead=True).get('Item')
        if row and row.get('expires_at', self.clock() + 1) <= self.clock():
            return None
        return row

    def get_source(self, source_id: str) -> dict | None:
        return self._get('SOURCE#' + source_id, 'META')

    def get_run(self, run_id: str) -> dict | None:
        return self._get('RUN#' + run_id, 'META')

    def get_item(self, run_id: str, url: str) -> dict | None:
        return self._get('RUN#' + run_id, 'ITEM#' + identity(url))

    def _transaction(self, actions):
        """The resource's client marshals native Python values, including transactions."""
        self.client.transact_write_items(TransactItems=actions)

    def claim(self, url: str, depth: int = 1, *, run_id: str | None = None) -> Claim:
        run_id = run_id or str(uuid.uuid4())
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', run_id):
            raise ValueError('Invalid run_id')
        source_id = identity(url)
        for _ in range(12):
            now = int(self.clock())
            source = self.get_source(source_id)
            if source and source.get('active_run_id'):
                active = self.get_run(source['active_run_id'])
                if active and active['status'] in LOCKED:
                    # A timeout is reconciled against Step Functions, never inferred from TTL.
                    return Claim(source_id, active['run_id'], False)
            previous = source.get('revision', 0) if source else 0
            source = dict(source or {'pk': 'SOURCE#' + source_id, 'sk': 'META',
                                     'source_id': source_id, 'url': url, 'added_at': now,
                                     'catalog_pk': 'SOURCES',
                                     'catalog_sk': f'{now:020d}#{source_id}'})
            source.update(active_run_id=run_id, latest_run_id=run_id, latest_status='queued',
                          latest_phase='queued', counts={}, revision=previous + 1)
            run = {'pk': 'RUN#' + run_id, 'sk': 'META', 'run_id': run_id,
                   'source_id': source_id, 'url': url, 'depth': depth,
                   'status': 'queued', 'phase': 'queued', 'discovery_complete': False,
                   'started_at': now, 'expires_at': now + RETENTION_SECONDS, 'revision': 1}
            history = {**run, 'pk': source['pk'], 'sk': f'RUN#{now:020d}#{run_id}'}
            condition = ('attribute_not_exists(pk)' if previous == 0 else 'revision = :previous')
            put_source = {'TableName': self.table.name, 'Item': source,
                          'ConditionExpression': condition}
            if previous:
                put_source['ExpressionAttributeValues'] = {':previous': previous}
            try:
                self._transaction([
                    {'Put': put_source},
                    {'Put': {'TableName': self.table.name, 'Item': run,
                             'ConditionExpression': 'attribute_not_exists(pk)'}},
                    {'Put': {'TableName': self.table.name, 'Item': history}},
                ])
                return Claim(source_id, run_id, True)
            except self.client.exceptions.TransactionCanceledException as exc:
                if not self._conflict(exc):
                    raise
                existing = self.get_run(run_id)
                if existing:
                    if existing['source_id'] != source_id:
                        raise ValueError('run_id already belongs to another source') from exc
                    return Claim(source_id, run_id, False)
        raise RuntimeError('Source changed repeatedly while starting a run; retry the request')

    @staticmethod
    def _conflict(exc):
        reasons = exc.response.get('CancellationReasons', [])
        return bool(reasons) and all(r['Code'] in (
            'None', 'ConditionalCheckFailed', 'TransactionConflict') for r in reasons)

    def _edit(self, pk, sk, change, *, active_run: str = ''):
        for _ in range(12):
            old = self._get(pk, sk)
            row = dict(old or {'pk': pk, 'sk': sk, 'revision': 0})
            if change(row) is False:
                return old
            row['revision'] = row.get('revision', 0) + 1
            condition = ('attribute_not_exists(pk)' if old is None else 'revision = :previous')
            put = {'TableName': self.table.name, 'Item': row, 'ConditionExpression': condition}
            if old is not None:
                put['ExpressionAttributeValues'] = {':previous': old['revision']}
            actions = [{'Put': put}]
            if active_run:
                actions.insert(0, {'ConditionCheck': {
                    'TableName': self.table.name, 'Key': {'pk': 'RUN#' + active_run, 'sk': 'META'},
                    'ConditionExpression': '#s IN (:queued, :running) AND expires_at > :now',
                    'ExpressionAttributeNames': {'#s': 'status'},
                    'ExpressionAttributeValues': {':queued': 'queued', ':running': 'running',
                                                  ':now': int(self.clock())}}})
            try:
                self._transaction(actions)
                return row
            except self.client.exceptions.TransactionCanceledException as exc:
                if not self._conflict(exc):
                    raise
                if active_run:
                    run = self.get_run(active_run)
                    if not run or run['status'] not in ACTIVE:
                        return old  # a late worker cannot change a finalized/expired run
        raise RuntimeError('Progress changed repeatedly; retry the task')

    def phase(self, run_id: str, phase: str, **fields):
        def update(row):
            if row.get('status') not in ACTIVE:
                return False
            row.update(status='running', phase=phase, updated_at=int(self.clock()), **fields)
        run = self._edit('RUN#' + run_id, 'META', update)
        if run:
            def source_phase(source):
                if source.get('active_run_id') != run_id or run['status'] not in ACTIVE:
                    return False
                source.update(latest_status='running', latest_phase=phase)
            self._edit('SOURCE#' + run['source_id'], 'META', source_phase)
        return run

    def bind_execution(self, run_id, arn):
        def update(row):
            if row.get('status') not in ACTIVE:
                return False
            row['execution_arn'] = arn
        self._edit('RUN#' + run_id, 'META', update)

    def reporter(self, run_id: str):
        run = self.get_run(run_id)
        if not run:
            raise ValueError('Unknown or expired run')
        return RunProgress(self, run)

    def _page(self, pk, prefix, *, limit=100, after=None, index=None):
        if not 1 <= limit <= 1000:
            raise ValueError('limit must be between 1 and 1000')
        kwargs = {'KeyConditionExpression': Key('pk').eq(pk) & Key('sk').begins_with(prefix),
                  'Limit': limit, 'ConsistentRead': True,
                  'FilterExpression': Attr('expires_at').not_exists() | Attr('expires_at').gt(
                      int(self.clock()))}
        if index:
            kwargs.update(IndexName='sources', ConsistentRead=False, ScanIndexForward=False,
                          KeyConditionExpression=Key('catalog_pk').eq('SOURCES'))
        if after:
            kwargs['ExclusiveStartKey'] = after
        response = self.table.query(**kwargs)
        return {'items': response.get('Items', []), 'next_key': response.get('LastEvaluatedKey')}

    def list_sources(self, **paging):
        return self._page('', '', index='sources', **paging)

    def list_runs(self, source_id, **paging):
        return self._page('SOURCE#' + source_id, 'RUN#', **paging)

    def list_items(self, run_id, **paging):
        return self._page('RUN#' + run_id, 'ITEM#', **paging)

    def list_children(self, run_id, parent_url, **paging):
        """Edges to immediate children; item_id references the child's single status record."""
        return self._page('RUN#' + run_id, 'EDGE#' + identity(parent_url) + '#', **paging)

    def _items(self, run_id):
        paginator = self.client.get_paginator('query')
        for page in paginator.paginate(
                TableName=self.table.name, ConsistentRead=True,
                KeyConditionExpression='pk = :pk AND begins_with(sk, :prefix)',
                ExpressionAttributeValues={':pk': 'RUN#' + run_id, ':prefix': 'ITEM#'}):
            for item in page.get('Items', []):
                if item['expires_at'] > self.clock():
                    yield item

    def summarize(self, run_id):
        rows = list(self._items(run_id))
        return {'discovered': len(rows),
                'content': dict(Counter(r['content_status'] for r in rows)),
                'discovery': dict(Counter(r['discovery_status'] for r in rows))}

    def finish(self, run_id, *, status=None, error='', reason='', report_key=''):
        run = self.get_run(run_id)
        if not run:
            return
        if run['status'] in ACTIVE:
            def freeze(row):
                if row.get('status') not in ACTIVE:
                    return False
                row.update(status='finalizing', requested_status=status or '',
                           error=error[:2000], reason=reason, report_key=report_key)
            self._edit(run['pk'], 'META', freeze)
            run = self.get_run(run_id)
        if run['status'] == 'finalizing':
            # Any unfinished work is explicit, including a task killed before writing a result.
            # Freeze the run first: concurrent late workers cannot change the final counts.
            observer = RunProgress(self, run, finalizing=True)
            for item in self._items(run_id):
                if item['discovery_status'] in ('queued', 'running'):
                    observer.discovery(item['url'], 'deferred', error=run['error'] or 'Discovery did not finish',
                                       reason=run['reason'] or 'incomplete')
                if item['content_status'] not in TERMINAL_CONTENT:
                    observer.content(item['url'], 'failed', error=run['error'] or 'No result after retries',
                                     reason=run['reason'] or 'unavailable')
            counts = self.summarize(run_id)
            failed = counts['content'].get('failed', 0) + counts['discovery'].get('failed', 0)
            incomplete = counts['discovery'].get('deferred', 0)
            outcome = run['requested_status'] or (
                'partial_failure' if failed or incomplete else 'complete')

            def update(row):
                if row.get('status') != 'finalizing':
                    return False
                row.update(status=outcome, phase='finished', finished_at=int(self.clock()),
                           counts=counts, discovery_complete=not (
                               incomplete or counts['discovery'].get('failed', 0)))
                row.pop('requested_status', None)
            self._edit(run['pk'], 'META', update)
        final = self.get_run(run_id)
        def history(row):
            row.update({k: v for k, v in final.items() if k not in ('pk', 'sk', 'revision')})
        self._edit('SOURCE#' + run['source_id'], f'RUN#{int(run["started_at"]):020d}#{run_id}', history)
        # Retry-safe repair if a process died between finalizing the run and releasing its source.
        def release(source):
            if source.get('active_run_id') != run_id:
                return False
            source.pop('active_run_id', None)
            source.update(latest_status=final['status'], latest_phase='finished',
                          counts=final.get('counts', {}))
        self._edit('SOURCE#' + run['source_id'], 'META', release)


class RunProgress:
    def __init__(self, store: ProgressStore, run: dict, *, finalizing=False):
        self.store, self.run = store, run
        self.attempts = {}
        self.finalizing = finalizing

    def _edit(self, sk, change):
        return self.store._edit(self.run['pk'], sk, change,
                                active_run='' if self.finalizing else self.run['run_id'])

    def discovered(self, url, kind, *, parent='', title='', context=''):
        item_id = identity(url)
        now = int(self.store.clock())
        def update(row):
            if 'item_id' not in row:
                row.update(item_id=item_id, url=url, kind=kind, title=title,
                           discovered_at=now, expires_at=self.run['expires_at'],
                           content_status='queued', discovery_status=(
                               'queued' if kind in ('page', 'page_text', 'folder') else 'not_applicable'))
            if title:
                row['title'] = title
            if kind and row['kind'] != 'folder':
                row['kind'] = kind
            if kind == 'folder':
                row['kind'] = kind
                row['content_status'] = 'not_applicable'
                if row['discovery_status'] == 'not_applicable':
                    row['discovery_status'] = 'queued'
        item = self._edit('ITEM#' + item_id, update)
        if item is not None and item_id == self.run['source_id']:
            def source_summary(source):
                if source.get('active_run_id') != self.run['run_id']:
                    return False
                if title:
                    source['title'] = title
                source['kind'] = item['kind']
            self.store._edit('SOURCE#' + self.run['source_id'], 'META', source_summary)
        if parent:
            parent_id = identity(parent)
            def edge(row):
                if 'item_id' in row:
                    return False
                row.update(parent_id=parent_id, item_id=item_id, parent_url=parent,
                           url=url, link_text=title, context=context, discovered_at=now,
                           expires_at=self.run['expires_at'])
            self._edit(f'EDGE#{parent_id}#{item_id}', edge)

    def discovery(self, url, status, *, error='', reason=''):
        def update(row):
            if 'item_id' not in row:
                raise ValueError('Report discovery before progress')
            if row['discovery_status'] in ('complete', 'failed', 'deferred'):
                return False
            row.update(discovery_status=status, discovery_error=error[:2000], discovery_reason=reason,
                       updated_at=int(self.store.clock()))
        self._edit('ITEM#' + identity(url), update)

    def content(self, url, status, *, result=None, error='', reason=''):
        item_id = identity(url)
        def update(row):
            if 'item_id' not in row:
                raise ValueError('Report discovery before progress')
            if row['content_status'] in ('stored', 'not_applicable', 'skipped'):
                return False
            if status == 'checking':
                row['attempt'] = row.get('attempt', 0) + 1
                self.attempts[item_id] = row['attempt']
            elif item_id in self.attempts and row.get('attempt') != self.attempts[item_id]:
                return False  # a newer invocation owns this item's progress
            row.update(content_status=status, error=error[:2000], reason=reason,
                       updated_at=int(self.store.clock()))
            if result is not None:
                row.update(result={k: v for k, v in result.items() if k != 'children'},
                           title=result.get('title') or row['title'])
        self._edit('ITEM#' + item_id, update)
        if result is not None and item_id == self.run['source_id'] and result.get('title'):
            def source_title(source):
                if source.get('active_run_id') != self.run['run_id']:
                    return False
                source['title'] = result['title']
            self.store._edit('SOURCE#' + self.run['source_id'], 'META', source_title)


class RunStarter:
    """Claim a source before starting Standard Step Functions. Also repairs an uncertain start.

    Callers racing on a queued run submit exactly the same name and persisted input, which
    Standard StartExecution treats idempotently. Transport errors leave it queued for retry.
    """
    def __init__(self, store: ProgressStore, state_machine_arn: str, *, client=None):
        self.store, self.arn = store, state_machine_arn
        self.client = client if client is not None else boto3.client('stepfunctions')

    def start(self, url, depth=1):
        claim = self.store.claim(url, depth)
        run = self.store.get_run(claim.run_id)
        if run['status'] == 'queued' and not run.get('execution_arn'):
            payload = json.dumps({'url': run['url'], 'depth': int(run['depth'])}, sort_keys=True)
            try:
                execution = self.client.start_execution(stateMachineArn=self.arn,
                                                       name=claim.run_id, input=payload)
            except self.client.exceptions.ExecutionAlreadyExists:
                # A previous caller's start succeeded and completed before the retry arrived.
                arn = self.arn.split(':stateMachine:')[0] + ':execution:' + self.arn.split(
                    ':stateMachine:')[1].split(':')[0] + ':' + claim.run_id
                execution = self.client.describe_execution(executionArn=arn)
            self.store.bind_execution(claim.run_id, execution['executionArn'])
        return {**asdict(claim), 'execution_arn': self.store.get_run(claim.run_id).get('execution_arn', '')}
