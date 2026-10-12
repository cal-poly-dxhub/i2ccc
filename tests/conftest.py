import os
import sys

import boto3
import pytest
from moto import mock_aws

# Don't leave __pycache__ in Lambda source dirs - CDK image assets hash the
# whole directory, so stray bytecode would force image rebuilds
sys.dont_write_bytecode = True

# Set before test modules import Lambdas that create boto3 clients/tables at
# import time. Point botocore away from ~/.aws so tests never see real
# profiles or credentials.
os.environ["AWS_CONFIG_FILE"] = os.devnull
os.environ["AWS_SHARED_CREDENTIALS_FILE"] = os.devnull
os.environ.pop("AWS_PROFILE", None)
os.environ["AWS_DEFAULT_REGION"] = "us-west-2"
os.environ["AWS_ACCESS_KEY_ID"] = "testing"
os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
os.environ["AWS_SESSION_TOKEN"] = "testing"
os.environ.setdefault("PROCESSED_FILES_TABLE", "processed-files")

BUCKET = "test-bucket"


@pytest.fixture
def aws():
    with mock_aws():
        yield


@pytest.fixture
def s3(aws):
    client = boto3.client("s3")
    client.create_bucket(
        Bucket=BUCKET,
        CreateBucketConfiguration={"LocationConstraint": os.environ["AWS_DEFAULT_REGION"]},
    )
    return client
