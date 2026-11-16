"""Source crawler Lambda (stub). See src/crawler/crawler.py for what it does."""

from aws_cdk import BundlingOptions, Duration
from aws_cdk import aws_lambda as _lambda
from aws_cdk import aws_s3 as s3
from constructs import Construct

CRAWLER_TIMEOUT = Duration.minutes(5)


class SourceCrawler(Construct):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        function_name: str,
        admin_api_function_name: str,
        input_assets_bucket: s3.IBucket,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Both functions invoke each other, so each side gets the other's name as a
        # plain string. A CloudFormation reference in both directions would be a cycle.
        self.function = _lambda.Function(
            self,
            "Function",
            function_name=function_name,
            runtime=_lambda.Runtime.PYTHON_3_13,
            code=_lambda.Code.from_asset(
                "src/crawler",
                bundling=BundlingOptions(
                    image=_lambda.Runtime.PYTHON_3_13.bundling_image,
                    command=[
                        "bash",
                        "-c",
                        "pip install --platform manylinux2014_x86_64 --implementation cp --python-version 3.13 --only-binary=:all: --target /asset-output -r requirements.txt && cp -au . /asset-output",
                    ],
                ),
            ),
            handler="crawler.lambda_handler",
            timeout=CRAWLER_TIMEOUT,
            memory_size=256,
            retry_attempts=0,
            environment={"ADMIN_API_FUNCTION_NAME": admin_api_function_name},
        )

        # Uploads stay under the admin prefix, matching what cleanup is allowed to delete
        input_assets_bucket.grant_put(self.function, "web/*")
