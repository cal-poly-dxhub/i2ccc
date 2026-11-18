"""Admin API: routes under the proxy API's /api for sources added through the admin UI.

See BACKEND_PLAN.md. Everything here is new; the only existing resources it
touches are the proxy API (new routes) and, in RagBackend, its CORS origins.
"""

import hashlib
from pathlib import Path
import shutil

from aws_cdk import BundlingOptions, DockerVolume, Duration, RemovalPolicy
from aws_cdk import aws_apigateway as apigw
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as _lambda
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_stepfunctions as sfn
from constructs import Construct

# Prefixes the admin API may list and delete. Everything else in the bucket
# (Confluence and Drive content) is out of its reach.
ADMIN_PREFIXES = ("web/", "quarantine/web/")

# The crawler gets this long after its workers' timeout to report a failure
CRAWL_REPORT_MARGIN_MINUTES = 10
CLEANUP_WORKER_TIMEOUT = Duration.minutes(15)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
URL_FILE_NAME = "source_urls.yaml"
SHARED_DIRECTORY = PROJECT_ROOT / "shared"
ADMIN_API_PATH = PROJECT_ROOT / "src" / "admin_api"

class AdminApi(Construct):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        api_resource: apigw.IResource,
        authorizer: apigw.IAuthorizer,
        allowed_origins: list[str],
        input_assets_bucket: s3.IBucket,
        processed_files_table: dynamodb.ITable,
        ingestion_state_machine: sfn.IStateMachine,
        opensearch_endpoint: str,
        opensearch_index_name: str,
        opensearch_collection_arn: str,
        crawler_function: _lambda.IFunction = None,
        # How long a crawler worker may run before the crawler stops it and reports
        # the crawl failed. Pass the crawler's real setting once it exists.
        crawler_worker_timeout: Duration = Duration.hours(2),
        # Fixed name, so the crawler can be given it without a circular reference
        function_name: str = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # One row per source (sk = "SOURCE") and one per crawled file (sk = S3 key)
        documents_table = dynamodb.Table(
            self,
            "DocumentsTable",
            partition_key=dynamodb.Attribute(name="source_id", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="sk", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            # The only record of which S3 objects and chunks came from the admin UI
            removal_policy=RemovalPolicy.RETAIN,
        )
        documents_table.add_global_secondary_index(
            index_name="by_normalized_url",
            partition_key=dynamodb.Attribute(name="normalized_url", type=dynamodb.AttributeType.STRING),
            projection_type=dynamodb.ProjectionType.KEYS_ONLY,
        )

        # Copy the shared urls file into the deployment package
        shutil.copy(SHARED_DIRECTORY / URL_FILE_NAME, ADMIN_API_PATH / URL_FILE_NAME)
        code = _lambda.Code.from_asset(
            "src/admin_api",
            bundling=BundlingOptions(
                image=_lambda.Runtime.PYTHON_3_13.bundling_image,
                command=[
                    "bash",
                    "-c",
                    "pip install --platform manylinux2014_x86_64 --implementation cp --python-version 3.13 --only-binary=:all: --target /asset-output -r requirements.txt && cp -au . /asset-output",
                ],
            ),
        )
        common_env = {
            "DOCUMENTS_TABLE": documents_table.table_name,
            "PROCESSED_FILES_TABLE": processed_files_table.table_name,
            "BUCKET_NAME": input_assets_bucket.bucket_name,
            "STATE_MACHINE_ARN": ingestion_state_machine.state_machine_arn,
            "CRAWLER_FUNCTION_NAME": crawler_function.function_name if crawler_function else "",
        }

        # Removes a source's content for rerun and delete, which can take longer than
        # API Gateway's 29 seconds. Failures are recorded on the source row and retried
        # from the UI, so Lambda's own retries are off.
        worker = _lambda.Function(
            self,
            "AdminCleanupWorker",
            runtime=_lambda.Runtime.PYTHON_3_13,
            code=code,
            handler="worker.lambda_handler",
            timeout=CLEANUP_WORKER_TIMEOUT,
            memory_size=512,
            retry_attempts=0,
            environment={
                **common_env,
                "OPENSEARCH_ENDPOINT": opensearch_endpoint,
                "OPENSEARCH_INDEX": opensearch_index_name,
            },
        )

        function = _lambda.Function(
            self,
            "AdminApiHandler",
            function_name=function_name,
            runtime=_lambda.Runtime.PYTHON_3_13,
            code=code,
            handler="handler.lambda_handler",
            timeout=Duration.seconds(30),
            memory_size=512,
            environment={
                **common_env,
                "STATE_MACHINE_ARN": ingestion_state_machine.state_machine_arn,
                "CLEANUP_FUNCTION_NAME": worker.function_name,
                # Past these, the crawler's workers or the cleanup worker are known to
                # have stopped, so a source still marked busy counts as failed
                "CRAWL_TIMEOUT_MINUTES": str(int(crawler_worker_timeout.to_minutes()) + CRAWL_REPORT_MARGIN_MINUTES),
                "CLEANUP_TIMEOUT_MINUTES": str(int(CLEANUP_WORKER_TIMEOUT.to_minutes()) + 1),
                "ALLOWED_ORIGINS": ",".join(allowed_origins),
            },
        )

        for fn in (function, worker):
            documents_table.grant_read_write_data(fn)
            # Listing under the admin prefixes: status of chunked videos, and cleanup
            fn.add_to_role_policy(
                iam.PolicyStatement(
                    actions=["s3:ListBucket"],
                    resources=[input_assets_bucket.bucket_arn],
                    conditions={"StringLike": {"s3:prefix": [f"{p}*" for p in ADMIN_PREFIXES]}},
                )
            )
            if crawler_function:
                # The API starts crawls for new sources, the worker for reruns
                crawler_function.grant_invoke(fn)

        # API: read-only on ingest state, plus starting ingests and cleanups
        function.add_to_role_policy(
            iam.PolicyStatement(
                actions=["dynamodb:BatchGetItem", "dynamodb:GetItem"],
                resources=[processed_files_table.table_arn],
            )
        )
        function.add_to_role_policy(
            iam.PolicyStatement(
                actions=["states:StartExecution", "states:ListExecutions"],
                resources=[ingestion_state_machine.state_machine_arn],
            )
        )
        worker.grant_invoke(function)

        # Worker: the deletes, limited to content under the admin prefixes
        worker.add_to_role_policy(
            iam.PolicyStatement(
                actions=["dynamodb:BatchWriteItem", "dynamodb:DeleteItem"],
                resources=[processed_files_table.table_arn],
            )
        )
        worker.add_to_role_policy(
            iam.PolicyStatement(
                actions=["s3:DeleteObject"],
                resources=[input_assets_bucket.arn_for_objects(f"{p}*") for p in ADMIN_PREFIXES],
            )
        )
        # The collection's data access policy already grants the whole account,
        # so the IAM permission is all the role needs
        worker.add_to_role_policy(
            iam.PolicyStatement(actions=["aoss:APIAccessAll"], resources=[opensearch_collection_arn])
        )

        integration = apigw.LambdaIntegration(function, proxy=True)
        auth = {"authorizer": authorizer, "authorization_type": apigw.AuthorizationType.COGNITO}

        sources = api_resource.add_resource("sources")
        sources.add_method("GET", integration, **auth)
        sources.add_method("POST", integration, **auth)
        source = sources.add_resource("{id}")
        source.add_method("DELETE", integration, **auth)
        source.add_resource("rerun").add_method("POST", integration, **auth)
        source.add_resource("documents").add_method("GET", integration, **auth)
        api_resource.add_resource("ingest").add_method("POST", integration, **auth)

        self.documents_table = documents_table
        # The crawler reports results by invoking this function with {"crawl_result": ...}.
        # Grant it with admin_api.function.grant_invoke(crawler_role_or_function).
        self.function = function
        self.cleanup_worker = worker
