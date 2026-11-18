"""CDK construct for the i2ccc stack: a state machine that takes a staff-submitted URL, crawls it,
and downloads what it links to into the ingest bucket, ready for DataIngestionStateMachine.

    Crawl -> Plan -> IngestEachItem (Distributed Map, one Lambda per item) -> Report

Invoke start_function with {"url": "https://internet2.edu/...", "depth": 1} (depth optional, 0-3).
It returns the existing active run for repeated submissions of the same source. The workflow's
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
from aws_cdk import ArnFormat, CfnOutput, Duration, RemovalPolicy, Size, Stack
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_ecr_assets as ecr_assets
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as event_targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_opensearchserverless as aoss
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_secretsmanager as secretsmanager
from aws_cdk import aws_stepfunctions as sfn
from aws_cdk import aws_stepfunctions_tasks as tasks
from constructs import Construct


class _RunItemsReader(sfn.S3JsonItemReader):
    """The dynamic key always resolves to a run's plan, never an arbitrary bucket object."""
    def __init__(self, *, bucket, prefix):
        super().__init__(bucket=bucket, key=sfn.JsonPath.string_at("$.items_key"))
        self._items_arn = bucket.arn_for_objects(f"{prefix}.runs/*/items.json")

    def provide_policy_statements(self):
        return [iam.PolicyStatement(actions=["s3:GetObject"], resources=[self._items_arn])]


class _MapResultsWriter(sfn.ResultWriter):
    def provide_policy_statements(self):
        return [iam.PolicyStatement(
            actions=["s3:GetObject", "s3:PutObject", "s3:ListMultipartUploadParts",
                     "s3:AbortMultipartUpload"],
            resources=[self.bucket.arn_for_objects(f"{self.prefix}/*")],
        )]


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
        opensearch_collection_name: str,
        code_directory: str = "src/scraper",   # the Lambda image's build context
        google_credentials_secret: secretsmanager.ISecret | None = None,
        key_prefix: str = "i2cc-scraper/",
        allowed_start_domains: tuple[str, ...] = ("internet2.edu",),
        max_concurrency: int = 4,
        frontend_role: iam.IGrantable | None = None,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        if not key_prefix or key_prefix.startswith("/") or any(c in key_prefix for c in "*?"):
            raise ValueError("key_prefix must be a nonempty, literal S3 prefix")
        key_prefix = key_prefix.rstrip("/") + "/"
        for name in (opensearch_collection_name, opensearch_index_name):
            if not name or name == "_all" or any(c in name for c in "*?/, "):
                raise ValueError("OpenSearch collection/index must be a single literal name")
        bucket = input_assets_bucket
        self.progress_table = dynamodb.Table(
            self, "ProgressTable",
            partition_key=dynamodb.Attribute(name="pk", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="sk", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            encryption=dynamodb.TableEncryption.AWS_MANAGED,
            time_to_live_attribute="expires_at",
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True),
            removal_policy=RemovalPolicy.RETAIN,
        )
        self.progress_table.add_global_secondary_index(
            index_name="sources",
            partition_key=dynamodb.Attribute(name="catalog_pk", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="catalog_sk", type=dynamodb.AttributeType.STRING),
            projection_type=dynamodb.ProjectionType.ALL,
        )

        # Downloads in progress, and each run's working files, don't outlive their use.
        bucket.add_lifecycle_rule(
            prefix=f"{key_prefix}.staging/",
            expiration=Duration.days(2),
            abort_incomplete_multipart_upload_after=Duration.days(1),
        )
        bucket.add_lifecycle_rule(prefix=f"{key_prefix}.runs/", expiration=Duration.days(30))

        storage_environment = {
            "SCRAPER_BUCKET": bucket.bucket_name,
            "SCRAPER_PREFIX": key_prefix,
            "SCRAPER_PROGRESS_TABLE": self.progress_table.table_name,
            "I2CC_ALLOWED_START_DOMAINS": ",".join(allowed_start_domains),
        }
        def function(name: str, step: str, memory: int, *, storage=True,
                     **options) -> lambda_.DockerImageFunction:
            environment = (dict(storage_environment) if storage else {
                "SCRAPER_PROGRESS_TABLE": self.progress_table.table_name,
                "I2CC_ALLOWED_START_DOMAINS": ",".join(allowed_start_domains),
            })
            if step in ("crawl", "ingest"):
                environment.update(PROCESSED_FILES_TABLE=processed_files_table.table_name,
                                   OPENSEARCH_ENDPOINT=opensearch_endpoint,
                                   INDEX_NAME=opensearch_index_name)
            if step in ("plan", "ingest") and google_credentials_secret is not None:
                environment["GOOGLE_CREDENTIALS_SECRET"] = google_credentials_secret.secret_arn
            log_group = logs.LogGroup(
                self, f"{name}Logs", retention=logs.RetentionDays.ONE_MONTH,
                removal_policy=RemovalPolicy.DESTROY,
            )
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
                log_group=log_group,
                **options,
            )
            # CDK creates these roles at their existing paths. Replace the default
            # account-wide logging policy with writes to this function's own log group.
            fn.role.node.default_child.add_property_deletion_override("ManagedPolicyArns")
            log_group.grant_write(fn)
            return fn

        crawl_fn = function("CrawlFunction", "crawl", 1024)
        plan_fn = function("PlanFunction", "plan", 1024)
        # Streaming keeps memory flat (8 MB upload parts); /tmp is for a YouTube format that
        # yt-dlp must download itself.
        ingest_fn = function("IngestFunction", "ingest", 2048,
                             ephemeral_storage_size=Size.gibibytes(10))
        report_fn = function("ReportFunction", "report", 512)
        failure_fn = function("FailureFunction", "execution_status", 512, storage=False)

        # Runtime policy baseline: IAM Policy Autopilot; resources and optional API
        # permissions narrowed per handler (see the README's permissions section).
        families = {"ForAllValues:StringLike": {"dynamodb:LeadingKeys": ["SOURCE#*", "RUN#*"]},
                    "Null": {"dynamodb:LeadingKeys": "false"}}

        def progress_access(fn, *, query=False, checks=False, catalog=False):
            fn.add_to_role_policy(iam.PolicyStatement(
                actions=["dynamodb:GetItem"] + (["dynamodb:Query"] if query else []),
                resources=[self.progress_table.table_arn], conditions=families,
            ))
            fn.add_to_role_policy(iam.PolicyStatement(
                actions=["dynamodb:PutItem"] + (["dynamodb:ConditionCheckItem"] if checks else []),
                resources=[self.progress_table.table_arn],
                conditions={**families, "StringEquals": {
                    "dynamodb:EnclosingOperation": "TransactWriteItems"}},
            ))
            if catalog:
                fn.add_to_role_policy(iam.PolicyStatement(
                    actions=["dynamodb:Query"],
                    resources=[f"{self.progress_table.table_arn}/index/sources"],
                ))

        progress_access(crawl_fn, query=True, checks=True)
        progress_access(plan_fn, query=True, checks=True)
        progress_access(ingest_fn, checks=True)
        progress_access(report_fn, query=True)
        progress_access(failure_fn, query=True)

        def objects(fn, actions, patterns):
            fn.add_to_role_policy(iam.PolicyStatement(
                actions=actions, resources=[bucket.arn_for_objects(p) for p in patterns],
            ))

        def list_prefixes(fn, patterns):
            fn.add_to_role_policy(iam.PolicyStatement(
                actions=["s3:ListBucket"], resources=[bucket.bucket_arn],
                conditions={"StringLike": {"s3:prefix": patterns}},
            ))

        content = [f"{key_prefix}{kind}/*" for kind in (
            "pages", "files", "google_drive", "youtube", "panopto")]
        runs = f"{key_prefix}.runs/*/"
        for fn, staging in ((crawl_fn, []), (ingest_fn, [f"{key_prefix}.staging/*"])):
            objects(fn, ["s3:GetObject", "s3:PutObject", "s3:DeleteObject",
                         "s3:AbortMultipartUpload"], content + staging)
            list_prefixes(fn, content)
            if bucket.encryption_key is not None:
                bucket.encryption_key.grant(fn, "kms:Decrypt", "kms:GenerateDataKey")
        objects(crawl_fn, ["s3:PutObject"], [runs + "crawl.json"])
        objects(ingest_fn, ["s3:PutObject"], [runs + "results/*.json"])
        objects(plan_fn, ["s3:GetObject"], [runs + "crawl.json"])
        objects(plan_fn, ["s3:PutObject"], [runs + "items.json", runs + "plan.json"])
        list_prefixes(plan_fn, [runs + "crawl.json"])
        report_inputs = [runs + name for name in (
            "crawl.json", "items.json", "plan.json", "results/*.json")]
        objects(report_fn, ["s3:GetObject"], report_inputs)
        objects(report_fn, ["s3:PutObject"], [runs + "report.json"])
        list_prefixes(report_fn, report_inputs)
        if bucket.encryption_key is not None:
            for fn in (plan_fn, report_fn):
                bucket.encryption_key.grant(fn, "kms:Decrypt", "kms:GenerateDataKey")

        # Pages (crawl) and downloads (ingest) can replace stored files: they clear the
        # replaced file's processed-files entry and its vectors.
        for fn in (crawl_fn, ingest_fn):
            fn.add_to_role_policy(iam.PolicyStatement(
                actions=["dynamodb:DeleteItem"], resources=[processed_files_table.table_arn],
                conditions={
                    "ForAllValues:StringLike": {"dynamodb:LeadingKeys": [
                        f"s3://{bucket.bucket_name}/{p}" for p in content]},
                    "Null": {"dynamodb:LeadingKeys": "false"},
                },
            ))
            fn.add_to_role_policy(
                iam.PolicyStatement(actions=["aoss:APIAccessAll"],
                                    resources=[opensearch_collection_arn])
            )
        if google_credentials_secret is not None:
            for fn in (plan_fn, ingest_fn):   # listing Drive folders, downloading Drive files
                fn.add_to_role_policy(iam.PolicyStatement(
                    actions=["secretsmanager:GetSecretValue"],
                    resources=[google_credentials_secret.secret_arn],
                ))
                if google_credentials_secret.encryption_key is not None:
                    google_credentials_secret.encryption_key.grant_decrypt(fn)

        # IAM APIAccessAll is a required collection gate, not an index permission.
        # The separate data policy grants only search + document cleanup on this index.
        aoss.CfnAccessPolicy(
            self, "IndexCleanupAccess", type="data", name=f"scraper-{self.node.addr[-16:]}",
            policy=Stack.of(self).to_json_string([{
                "Rules": [{"ResourceType": "index",
                           "Resource": [f"index/{opensearch_collection_name}/{opensearch_index_name}"],
                           "Permission": ["aoss:ReadDocument", "aoss:WriteDocument"]}],
                "Principal": [crawl_fn.role.role_arn, ingest_fn.role.role_arn],
            }]),
        )

        crawl = tasks.LambdaInvoke(
            self,
            "Crawl",
            lambda_function=crawl_fn,
            payload=sfn.TaskInput.from_object({
                "input": sfn.JsonPath.object_at("$"),
                "run_id": sfn.JsonPath.string_at("$$.Execution.Name"),
                "execution_arn": sfn.JsonPath.string_at("$$.Execution.Id"),
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
            item_reader=_RunItemsReader(bucket=bucket, prefix=key_prefix),
            item_selector={
                "run_id": sfn.JsonPath.string_at("$.run_id"),
                "item": sfn.JsonPath.object_at("$$.Map.Item.Value"),
            },
            max_concurrency=max_concurrency,
            tolerated_failure_percentage=100,   # Report lists what didn't make it
            # Item outputs go to S3, not into the state (limited to 256 KB).
            result_writer=_MapResultsWriter(bucket=bucket, prefix=f"{key_prefix}.runs/map-results"),
            result_path=sfn.JsonPath.DISCARD,
        )
        each_item.item_processor(ingest)
        report = tasks.LambdaInvoke(self, "Report", lambda_function=report_fn,
                                    payload_response_only=True)
        record_failure = tasks.LambdaInvoke(
            self, "RecordFailure", lambda_function=failure_fn, payload_response_only=True,
            payload=sfn.TaskInput.from_object({
                "run_id": sfn.JsonPath.string_at("$$.Execution.Name"),
                "failure": sfn.JsonPath.object_at("$.failure"),
            }),
        )
        record_failure.next(sfn.Fail(self, "ScraperFailed"))
        for task in (crawl, plan, each_item, report):
            task.add_catch(record_failure, result_path="$.failure")

        self.state_machine = sfn.StateMachine(
            self,
            "ScraperStateMachine",
            definition_body=sfn.DefinitionBody.from_chainable(
                crawl.next(plan).next(each_item).next(report)
            ),
            timeout=Duration.hours(6),
        )
        # Exact physical name is generated by CloudFormation. Using its Ref here
        # would create a role <-> state machine cycle, so bind trust to this account/region.
        self.state_machine.role.node.default_child.assume_role_policy_document = {
            "Version": "2012-10-17", "Statement": [{
                "Effect": "Allow", "Action": "sts:AssumeRole",
                "Principal": {"Service": "states.amazonaws.com"},
                "Condition": {
                    "StringEquals": {"aws:SourceAccount": Stack.of(self).account},
                    "ArnLike": {"aws:SourceArn": Stack.of(self).format_arn(
                        service="states", resource="stateMachine", resource_name="*",
                        arn_format=ArnFormat.COLON_RESOURCE_NAME)},
                },
            }],
        }

        # The caller invokes this function, so the active-run claim cannot be bypassed.
        # Keeping it outside the workflow avoids an environment/definition dependency cycle.
        self.start_function = function("StartFunction", "start", 512, storage=False)
        progress_access(self.start_function)
        self.start_function.add_environment("SCRAPER_STATE_MACHINE_ARN",
                                            self.state_machine.state_machine_arn)
        self.state_machine.grant_start_execution(self.start_function)
        execution_arn = Stack.of(self).format_arn(
            service="states", resource="execution",
            resource_name=f"{self.state_machine.state_machine_name}:*",
            arn_format=ArnFormat.COLON_RESOURCE_NAME,
        )
        self.start_function.add_to_role_policy(iam.PolicyStatement(
            actions=["states:DescribeExecution"], resources=[execution_arn],
        ))

        reconcile_fn = function("ReconcileFunction", "execution_status", 512, storage=False)
        progress_access(reconcile_fn, query=True, catalog=True)
        reconcile_fn.add_environment("SCRAPER_STATE_MACHINE_ARN",
                                     self.state_machine.state_machine_arn)
        reconcile_fn.add_to_role_policy(iam.PolicyStatement(
            actions=["states:DescribeExecution"], resources=[execution_arn],
        ))
        self.state_machine.grant_start_execution(reconcile_fn)
        events.Rule(
            self, "ExecutionStatusChanges",
            event_pattern=events.EventPattern(
                source=["aws.states"], detail_type=["Step Functions Execution Status Change"],
                detail={"stateMachineArn": [self.state_machine.state_machine_arn],
                        "status": ["SUCCEEDED", "FAILED", "TIMED_OUT", "ABORTED"]},
            ),
            targets=[event_targets.LambdaFunction(reconcile_fn)],
        )
        events.Rule(self, "ReconcileActiveRuns", schedule=events.Schedule.rate(Duration.minutes(5)),
                    targets=[event_targets.LambdaFunction(reconcile_fn)])
        if frontend_role is not None:
            self.grant_frontend_access(frontend_role)

        CfnOutput(self, "ScraperProgressTableName", value=self.progress_table.table_name)
        CfnOutput(self, "ScraperStartFunctionArn", value=self.start_function.function_arn)

        CfnOutput(
            self,
            "ScraperStateMachineArn",
            value=self.state_machine.state_machine_arn,
            description='Start with {"url": "<page>", "depth": 1} to scrape a page into the '
                        "ingest bucket",
        )

    def grant_frontend_access(self, grantee: iam.IGrantable) -> None:
        """Only the designated admin frontend can read progress and submit scraper work.

        Progress writes belong to the workers; no frontend table writes, raw StartExecution
        grant, public function URL, or HTTP endpoint is created.
        """
        iam.Grant.add_to_principal(
            grantee=grantee, actions=["dynamodb:GetItem", "dynamodb:Query"],
            resource_arns=[self.progress_table.table_arn],
        )
        iam.Grant.add_to_principal(
            grantee=grantee, actions=["dynamodb:Query"],
            resource_arns=[f"{self.progress_table.table_arn}/index/sources"],
        )
        self.start_function.grant_invoke(grantee)
