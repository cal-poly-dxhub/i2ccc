"""Synthesize locally: no credentials, Docker builds, AWS lookups, or deployment."""
import json

import pytest

cdk = pytest.importorskip('aws_cdk', reason='CDK validation requires aws-cdk-lib')
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_s3 as s3
from aws_cdk.assertions import Template

from cdk.scraper import ScraperPipeline


def test_progress_table_workflow_and_frontend_grants_synthesize(tmp_path):
    app = cdk.App(outdir=str(tmp_path / 'cdk.out'))
    stack = cdk.Stack(app, 'ScraperTest', env=cdk.Environment(
        account='000000000000', region='us-east-1'))
    bucket = s3.Bucket(stack, 'Input')
    processed = dynamodb.Table(stack, 'Processed', partition_key=dynamodb.Attribute(
        name='s3_uri', type=dynamodb.AttributeType.STRING))
    frontend = iam.Role(stack, 'AdminFrontend', assumed_by=iam.ServicePrincipal('lambda.amazonaws.com'))
    scraper = ScraperPipeline(
        stack, 'Scraper', input_assets_bucket=bucket, processed_files_table=processed,
        opensearch_endpoint='https://test.us-east-1.aoss.amazonaws.com',
        opensearch_index_name='test',
        opensearch_collection_arn='arn:aws:aoss:us-east-1:000000000000:collection/test',
        frontend_role=frontend,
    )
    template = Template.from_stack(stack)
    template.has_resource_properties('AWS::DynamoDB::Table', {
        'BillingMode': 'PAY_PER_REQUEST',
        'TimeToLiveSpecification': {'AttributeName': 'expires_at', 'Enabled': True},
        'PointInTimeRecoverySpecification': {'PointInTimeRecoveryEnabled': True},
        'GlobalSecondaryIndexes': [{
            'IndexName': 'sources',
            'KeySchema': [{'AttributeName': 'catalog_pk', 'KeyType': 'HASH'},
                          {'AttributeName': 'catalog_sk', 'KeyType': 'RANGE'}],
            'Projection': {'ProjectionType': 'ALL'},
        }],
    })
    resources = template.to_json()['Resources']
    progress = next(r for r in resources.values() if r['Type'] == 'AWS::DynamoDB::Table'
                    and 'TimeToLiveSpecification' in r['Properties'])
    assert progress['DeletionPolicy'] == progress['UpdateReplacePolicy'] == 'Retain'
    functions = {r['Properties']['ImageConfig']['Command'][0].rsplit('.', 1)[-1]: r
                 for r in resources.values() if r['Type'] == 'AWS::Lambda::Function'}
    assert 'start' in functions
    assert 'SCRAPER_STATE_MACHINE_ARN' in functions['start']['Properties']['Environment']['Variables']
    assert not any(r['Type'] in ('AWS::ApiGateway::RestApi', 'AWS::Lambda::Url')
                   for r in resources.values())
    policy = next(r['Properties']['PolicyDocument'] for name, r in resources.items()
                  if name.startswith('AdminFrontendDefaultPolicy'))
    actions = {action for statement in policy['Statement']
               for action in (statement['Action'] if isinstance(statement['Action'], list)
                              else [statement['Action']])}
    assert 'lambda:InvokeFunction' in actions and 'dynamodb:Query' in actions
    assert not actions & {'states:StartExecution', 'dynamodb:PutItem', 'dynamodb:UpdateItem',
                          'dynamodb:DeleteItem', 's3:GetObject'}
    template.resource_count_is('AWS::Events::Rule', 2)
    definition = json.dumps(next(r['Properties']['DefinitionString'] for r in resources.values()
                                 if r['Type'] == 'AWS::StepFunctions::StateMachine'))
    assert 'RecordFailure' in definition and 'execution_arn' in definition
    assert scraper.start_function is not None
    app.synth()  # also checks dependencies and produces an assembly only in the temp directory



def test_scraper_is_integrated_into_the_existing_stack(tmp_path, monkeypatch):
    from aws_cdk import aws_lambda as lambda_
    from cdk.main import RagChatbotStack

    # Skip dependency installation inside Docker for template assertions only.
    # Deployment still uses the original bundling and image definitions.
    from_asset = lambda_.Code.from_asset
    def unbundled(path, **options):
        options.pop('bundling', None)
        return from_asset(path, **options)
    monkeypatch.setattr(lambda_.Code, 'from_asset', unbundled)
    app = cdk.App(outdir=str(tmp_path / 'cdk.out'))
    stack = RagChatbotStack(
        app, 'RagChatbotStack',
        embeddings_model_id='amazon.titan-embed-text-v2:0',
        video_text_model_id='test-model',
        opensearch_collection_name='test-collection', opensearch_index_name='test-index',
        chat_model='test-model', embedding_model='amazon.titan-embed-text-v2:0',
        chat_prompt='Test prompt', classifier_model='test-model', document_filter_model='test-model',
        platform_classifier_prompt='Test prompt', document_filter_prompt='Test prompt',
        config_path='config.yaml.example', max_concurrency=3, step_function_timeout_hours=24,
        chunk_size='20000', overlap='0.1', docs_retrieved=20, docs_after_falloff=20,
        cognito_domain_prefix='i2ccc-synth-test', dev_user_email='synth@example.invalid',
        env=cdk.Environment(account='000000000000', region='us-east-1'),
    )
    assert stack.scraper is stack.node.find_child('Scraper')
    ingest = stack.node.find_child('RagIngest')
    content_sync = stack.node.find_child('ContentSync')
    template = Template.from_stack(stack)
    template.has_resource_properties('AWS::Lambda::Function', {
        'ImageConfig': {'Command': ['i2cc_scraper.aws.handlers.crawl']},
        'Environment': {'Variables': {
            'SCRAPER_BUCKET': stack.resolve(ingest.input_assets_bucket.bucket_name),
            'PROCESSED_FILES_TABLE': stack.resolve(ingest.processed_files_table.table_name),
            'GOOGLE_CREDENTIALS_SECRET': stack.resolve(content_sync.google_secret.secret_arn),
            'SCRAPER_PROGRESS_TABLE': stack.resolve(stack.scraper.progress_table.table_name),
        }},
    })
    resources = template.to_json()['Resources']
    for name, resource in resources.items():
        if resource['Type'] == 'AWS::IAM::Policy' and not name.startswith('Scraper'):
            assert 'ScraperProgressTable' not in json.dumps(resource['Properties'])
            assert 'ScraperStartFunction' not in json.dumps(resource['Properties'])
    assert len(app.synth().stacks) == 1
