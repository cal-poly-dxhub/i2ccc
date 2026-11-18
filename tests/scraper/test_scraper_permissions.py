"""IAM regression checks against synthesized policies; no emulator IAM claims or AWS calls."""
import fnmatch
import json

import pytest

cdk = pytest.importorskip('aws_cdk')
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_secretsmanager as secretsmanager
from aws_cdk.assertions import Template

from cdk.scraper import ScraperPipeline


@pytest.fixture
def policies(tmp_path):
    app = cdk.App(outdir=str(tmp_path / 'cdk.out'))
    stack = cdk.Stack(app, 'Permissions', env=cdk.Environment(
        account='000000000000', region='us-east-1'))
    bucket = s3.Bucket(stack, 'Input', bucket_name='scraper-input-bucket')
    cache = dynamodb.Table(stack, 'Processed', partition_key=dynamodb.Attribute(
        name='s3_uri', type=dynamodb.AttributeType.STRING))
    secret = secretsmanager.Secret(stack, 'Google')
    admin = iam.Role(stack, 'Admin', assumed_by=iam.ServicePrincipal('lambda.amazonaws.com'))
    scraper = ScraperPipeline(
        stack, 'Scraper', input_assets_bucket=bucket, processed_files_table=cache,
        opensearch_endpoint='https://test.us-east-1.aoss.amazonaws.com',
        opensearch_index_name='test-index', opensearch_collection_name='test-collection',
        opensearch_collection_arn='arn:aws:aoss:us-east-1:000000000000:collection/test',
        google_credentials_secret=secret, frontend_role=admin,
    )
    resources = Template.from_stack(stack).to_json()['Resources']
    app.synth()
    return resources, stack, scraper


def as_list(value):
    return value if isinstance(value, list) else [value]


def role_statements(resources, role_id):
    return [s for row in resources.values() if row['Type'] == 'AWS::IAM::Policy'
            and {'Ref': role_id} in row['Properties']['Roles']
            for s in row['Properties']['PolicyDocument']['Statement']]


def literal(value, resources):
    """Resolve the literal bucket name through CloudFormation references in conditions."""
    if isinstance(value, str):
        return value
    if 'Ref' in value:
        if value['Ref'] == 'AWS::Partition':
            return 'aws'
        return resources[value['Ref']]['Properties']['BucketName']
    separator, parts = value['Fn::Join']
    return separator.join(literal(part, resources) for part in parts)


def function_role(resources, label):
    name = next(name for name, row in resources.items() if row['Type'] == 'AWS::Lambda::Function'
                and name.startswith('Scraper' + label + 'Function'))
    role_id = resources[name]['Properties']['Role']['Fn::GetAtt'][0]
    return role_id, role_statements(resources, role_id)


def test_each_function_has_only_its_required_actions(policies):
    resources, _, _ = policies
    logs = {'logs:CreateLogStream', 'logs:PutLogEvents'}
    read_write = {'dynamodb:GetItem', 'dynamodb:PutItem'}
    query = {'dynamodb:Query'}
    check = {'dynamodb:ConditionCheckItem'}
    objects = {'s3:GetObject', 's3:PutObject', 's3:ListBucket'}
    cleanup = {'s3:DeleteObject', 's3:AbortMultipartUpload', 'dynamodb:DeleteItem',
               'aoss:APIAccessAll'}
    secret = {'secretsmanager:GetSecretValue'}
    execution = {'states:StartExecution', 'states:DescribeExecution'}
    expected = {
        'Crawl': logs | read_write | query | check | objects | cleanup,
        'Plan': logs | read_write | query | check | objects | secret,
        'Ingest': logs | read_write | check | objects | cleanup | secret,
        'Report': logs | read_write | query | objects,
        'Failure': logs | read_write | query,
        'Start': logs | read_write | execution,
        'Reconcile': logs | read_write | query | execution,
    }
    for label, allowed in expected.items():
        role_id, statements = function_role(resources, label)
        assert not resources[role_id]['Properties'].get('ManagedPolicyArns')
        assert {a for s in statements for a in as_list(s['Action'])} == allowed, label
        assert all(r != '*' for s in statements for r in as_list(s['Resource'])), label
        for statement in statements:
            if 'logs:PutLogEvents' in as_list(statement['Action']):
                assert f'Scraper{label}FunctionLogs' in json.dumps(statement['Resource'])


def test_s3_grants_cannot_reach_other_ingestion_data(policies):
    resources, _, _ = policies
    for label in ('Crawl', 'Plan', 'Ingest', 'Report'):
        _, statements = function_role(resources, label)
        for statement in statements:
            actions = as_list(statement['Action'])
            if 's3:ListBucket' in actions:
                prefixes = statement['Condition']['StringLike']['s3:prefix']
                assert all(p.startswith('i2cc-scraper/') for p in as_list(prefixes))
                assert not any(fnmatch.fnmatchcase('files-to-process/private.pdf', p)
                               for p in as_list(prefixes))
            elif any(a.startswith('s3:') for a in actions):
                for arn in as_list(statement['Resource']):
                    assert '/i2cc-scraper/' in json.dumps(arn)
                    if label in ('Plan', 'Report'):
                        assert '.runs/' in json.dumps(arn)
                        assert 's3:DeleteObject' not in actions


def test_transactions_cache_deletion_and_index_access_are_bounded(policies):
    resources, stack, scraper = policies
    table = stack.resolve(scraper.progress_table.table_arn)
    for label in ('Crawl', 'Plan', 'Ingest', 'Report', 'Failure', 'Start', 'Reconcile'):
        _, statements = function_role(resources, label)
        for statement in statements:
            actions = as_list(statement['Action'])
            if 'dynamodb:PutItem' in actions:
                assert statement['Resource'] in (table, [table])
                conditions = statement['Condition']
                assert conditions['StringEquals']['dynamodb:EnclosingOperation'] == 'TransactWriteItems'
                assert conditions['Null']['dynamodb:LeadingKeys'] == 'false'
            if 'dynamodb:DeleteItem' in actions:
                assert label in ('Crawl', 'Ingest')
                conditions = statement['Condition']
                assert conditions['Null']['dynamodb:LeadingKeys'] == 'false'
                patterns = [literal(p, resources) for p in conditions[
                    'ForAllValues:StringLike']['dynamodb:LeadingKeys']]
                assert any(fnmatch.fnmatchcase(
                    's3://scraper-input-bucket/i2cc-scraper/files/id/content.pdf', p) for p in patterns)
                assert not any(fnmatch.fnmatchcase(
                    's3://scraper-input-bucket/files-to-process/private.pdf', p) for p in patterns)
            if '/index/' in json.dumps(statement['Resource']):
                assert label == 'Reconcile'
                assert actions == ['dynamodb:Query']
                assert '/index/sources' in json.dumps(statement['Resource'])


def test_workflow_and_admin_are_scoped_to_scraper_resources(policies):
    resources, stack, scraper = policies
    role_id = stack.resolve(scraper.state_machine.role.role_arn)['Fn::GetAtt'][0]
    statements = role_statements(resources, role_id)
    for statement in statements:
        actions = as_list(statement['Action'])
        assert all(r != '*' for r in as_list(statement['Resource']))
        if any(a.startswith('s3:') for a in actions):
            text = json.dumps(statement['Resource'])
            assert '/i2cc-scraper/.runs/' in text
            assert 'items.json' in text or 'map-results/' in text
    trust = resources[role_id]['Properties']['AssumeRolePolicyDocument']['Statement'][0]
    assert trust['Condition']['StringEquals'] == {'aws:SourceAccount': '000000000000'}
    assert literal(trust['Condition']['ArnLike']['aws:SourceArn'], resources) == (
        'arn:aws:states:us-east-1:000000000000:stateMachine:*')
    for label in ('Start', 'Reconcile'):
        _, permissions = function_role(resources, label)
        for statement in permissions:
            if 'states:DescribeExecution' in as_list(statement['Action']):
                assert ':execution:' in json.dumps(statement['Resource'])
                assert 'ScraperScraperStateMachine' in json.dumps(statement['Resource'])
            if 'states:StartExecution' in as_list(statement['Action']):
                assert statement['Resource'] == stack.resolve(scraper.state_machine.state_machine_arn)
    admin_id = next(name for name, row in resources.items()
                    if name.startswith('Admin') and row['Type'] == 'AWS::IAM::Role')
    admin_statements = role_statements(resources, admin_id)
    assert {a for s in admin_statements for a in as_list(s['Action'])} == {
        'dynamodb:GetItem', 'dynamodb:Query', 'lambda:InvokeFunction'}


def test_index_cleanup_has_no_index_management_or_unrelated_principals(policies):
    resources, stack, _ = policies
    policy = next(row['Properties']['Policy'] for row in resources.values()
                  if row['Type'] == 'AWS::OpenSearchServerless::AccessPolicy')
    text = json.dumps(policy)
    assert 'index/test-collection/test-index' in text
    assert 'aoss:ReadDocument' in text and 'aoss:WriteDocument' in text
    assert 'CreateIndex' not in text and 'DeleteIndex' not in text and 'index/test-collection/*' not in text
    assert 'ScraperCrawlFunctionServiceRole' in text and 'ScraperIngestFunctionServiceRole' in text
    for label in ('Plan', 'Report', 'Failure', 'Start', 'Reconcile'):
        assert f'Scraper{label}FunctionServiceRole' not in text
    for name, row in resources.items():
        if row['Type'] == 'AWS::Lambda::Function':
            environment = row['Properties']['Environment']['Variables']
            assert ('GOOGLE_CREDENTIALS_SECRET' in environment) == any(
                name.startswith(f'Scraper{label}Function') for label in ('Plan', 'Ingest'))


@pytest.mark.parametrize('prefix', ['', '/', '*', 'scraper?/', '/other/'])
def test_policy_prefix_cannot_expand_to_other_bucket_keys(tmp_path, prefix):
    app = cdk.App(outdir=str(tmp_path / 'cdk.out'))
    stack = cdk.Stack(app, 'BadPrefix')
    bucket = s3.Bucket(stack, 'Input')
    cache = dynamodb.Table(stack, 'Processed', partition_key=dynamodb.Attribute(
        name='s3_uri', type=dynamodb.AttributeType.STRING))
    with pytest.raises(ValueError, match='literal S3 prefix'):
        ScraperPipeline(
            stack, 'Scraper', input_assets_bucket=bucket, processed_files_table=cache,
            opensearch_endpoint='https://test.us-east-1.aoss.amazonaws.com',
            opensearch_index_name='test-index', opensearch_collection_name='test-collection',
            opensearch_collection_arn='arn:aws:aoss:us-east-1:000000000000:collection/test',
            key_prefix=prefix,
        )
