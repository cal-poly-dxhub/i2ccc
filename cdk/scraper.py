"""CDK construct for the i2ccc stack: a state machine that takes a staff-submitted URL, crawls it,
and downloads what it links to into the ingest bucket, ready for DataIngestionStateMachine.

    Crawl -> Plan -> IngestEachItem (Distributed Map, one Lambda per item) -> Report

Start it with {"url": "https://internet2.edu/...", "depth": 1} (depth optional, 0-3); its
output is the run's summary (counts, failures, the S3 key of the full report). It doesn't start
ingestion: what it stores is picked up by the next ingestion run.

Lives in i2ccc's cdk/; create it next to RagIngest, e.g.:

    ScraperPipeline(self, "Scraper",
        input_assets_bucket=ingest.input_assets_bucket,
        processed_files_table=ingest.processed_files_table,
        opensearch_endpoint=ingest.opensearch_endpoint, opensearch_index_name=index_name,
        opensearch_collection_arn=ingest.collection_arn,
        google_credentials_secret=content_sync_google_secret)
"""
from aws_cdk import CfnOutput, Duration, RemovalPolicy, Size
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_ecr_assets as ecr_assets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_secretsmanager as secretsmanager
from aws_cdk import aws_stepfunctions as sfn
from aws_cdk import aws_stepfunctions_tasks as tasks
from constructs import Construct


class ScraperPipeline(Construct):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        input_assets_bucket: s3.Bucket,
        processed_files_table: dynamodb.ITable,
        opensearch_endpoint: str,
        opensearch_index_name: str,
        opensearch_collection_arn: str,
        code_directory: str = "src/scraper",   # the Lambda image's build context
        google_credentials_secret: secretsmanager.ISecret | None = None,
        key_prefix: str = "i2cc-scraper/",
        allowed_start_domains: tuple[str, ...] = ("internet2.edu",),
        max_concurrency: int = 4,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        bucket = input_assets_bucket

        # Downloads in progress, and each run's working files, don't outlive their use.
        bucket.add_lifecycle_rule(
            prefix=f"{key_prefix}.staging/",
            expiration=Duration.days(2),
            abort_incomplete_multipart_upload_after=Duration.days(1),
        )
        bucket.add_lifecycle_rule(prefix=f"{key_prefix}.runs/", expiration=Duration.days(30))

        environment = {
            "SCRAPER_BUCKET": bucket.bucket_name,
            "SCRAPER_PREFIX": key_prefix,
            "PROCESSED_FILES_TABLE": processed_files_table.table_name,
            "OPENSEARCH_ENDPOINT": opensearch_endpoint,
            "INDEX_NAME": opensearch_index_name,
            "I2CC_ALLOWED_START_DOMAINS": ",".join(allowed_start_domains),
        }
        if google_credentials_secret is not None:
            environment["GOOGLE_CREDENTIALS_SECRET"] = google_credentials_secret.secret_arn

        def function(name: str, step: str, memory: int, **options) -> lambda_.DockerImageFunction:
            fn = lambda_.DockerImageFunction(
                self,
                name,
                code=lambda_.DockerImageCode.from_image_asset(
                    directory=code_directory,
                    file="Dockerfile",
                    platform=ecr_assets.Platform.LINUX_AMD64,
                    cmd=[f"i2cc_scraper.aws.handlers.{step}"],
                ),
                memory_size=memory,
                timeout=Duration.minutes(15),
                environment=environment,
                log_group=logs.LogGroup(
                    self,
                    f"{name}Logs",
                    retention=logs.RetentionDays.ONE_MONTH,
                    removal_policy=RemovalPolicy.DESTROY,
                ),
                **options,
            )
            bucket.grant_read_write(fn, f"{key_prefix}*")
            return fn

        crawl_fn = function("CrawlFunction", "crawl", 1024)
        plan_fn = function("PlanFunction", "plan", 1024)
        # Streaming keeps memory flat (8 MB upload parts); /tmp is for a YouTube format that
        # yt-dlp must download itself.
        ingest_fn = function("IngestFunction", "ingest", 2048,
                             ephemeral_storage_size=Size.gibibytes(10))
        report_fn = function("ReportFunction", "report", 512)

        # Pages (crawl) and downloads (ingest) can replace stored files: they clear the
        # replaced file's processed-files entry and its vectors.
        for fn in (crawl_fn, ingest_fn):
            processed_files_table.grant(fn, "dynamodb:DeleteItem")
            fn.add_to_role_policy(
                iam.PolicyStatement(actions=["aoss:APIAccessAll"],
                                    resources=[opensearch_collection_arn])
            )
        if google_credentials_secret is not None:
            for fn in (plan_fn, ingest_fn):   # listing Drive folders, downloading Drive files
                google_credentials_secret.grant_read(fn)

        crawl = tasks.LambdaInvoke(
            self,
            "Crawl",
            lambda_function=crawl_fn,
            payload=sfn.TaskInput.from_object({
                "input": sfn.JsonPath.object_at("$"),
                "run_id": sfn.JsonPath.string_at("$$.Execution.Name"),
            }),
            payload_response_only=True,
        )
        plan = tasks.LambdaInvoke(self, "Plan", lambda_function=plan_fn,
                                  payload_response_only=True)
        ingest = tasks.LambdaInvoke(self, "Ingest", lambda_function=ingest_fn,
                                    payload_response_only=True)
        # A temporary failure (rate limit, 5xx, a conflicting writer) is tried again later.
        ingest.add_retry(
            errors=["TransientError"],
            interval=Duration.minutes(1),
            max_attempts=3,
            backoff_rate=3,
            jitter_strategy=sfn.JitterType.FULL,
        )
        each_item = sfn.DistributedMap(
            self,
            "IngestEachItem",
            item_reader=sfn.S3JsonItemReader(
                bucket=bucket, key=sfn.JsonPath.string_at("$.items_key")
            ),
            item_selector={
                "run_id": sfn.JsonPath.string_at("$.run_id"),
                "item": sfn.JsonPath.object_at("$$.Map.Item.Value"),
            },
            max_concurrency=max_concurrency,
            tolerated_failure_percentage=100,   # Report lists what didn't make it
            # Item outputs go to S3, not into the state (limited to 256 KB).
            result_writer=sfn.ResultWriter(bucket=bucket, prefix=f"{key_prefix}.runs/map-results"),
            result_path=sfn.JsonPath.DISCARD,
        )
        each_item.item_processor(ingest)
        report = tasks.LambdaInvoke(self, "Report", lambda_function=report_fn,
                                    payload_response_only=True)

        self.state_machine = sfn.StateMachine(
            self,
            "ScraperStateMachine",
            definition_body=sfn.DefinitionBody.from_chainable(
                crawl.next(plan).next(each_item).next(report)
            ),
            timeout=Duration.hours(6),
        )

        CfnOutput(
            self,
            "ScraperStateMachineArn",
            value=self.state_machine.state_machine_arn,
            description='Start with {"url": "<page>", "depth": 1} to scrape a page into the '
                        "ingest bucket",
        )
