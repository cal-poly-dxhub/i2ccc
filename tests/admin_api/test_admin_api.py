from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError, ConnectTimeoutError
import sys
from pathlib import Path
import os
import json

os.environ.setdefault("STATE_MACHINE_ARN", "arn:aws:states:us-east-1:123456789012:stateMachine:test")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

# Make src/admin_api importable so `import handler` works
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src" / "admin_api"))
import handler


def post_ingest():
    event = {
        "httpMethod": "POST",
        "path": "/api/ingest",
        "headers": {},
        "requestContext": {"stage": "prod"},
        "body": None,
        "isBase64Encoded": False,
    }
    return handler.lambda_handler(event, None)

def test_202_on_success():
    with patch.object(handler, "sfn") as sfn:
        sfn.list_executions.return_value = {"executions": []}
        sfn.start_execution.return_value = {"executionArn": "arn:aws:states:us-east-1:123456789012:execution:test:abc"}

        resp = post_ingest()

    assert resp["statusCode"] == 202
    assert json.loads(resp["body"])["executionArn"] == "arn:aws:states:us-east-1:123456789012:execution:test:abc"
    sfn.start_execution.assert_called_once_with(stateMachineArn=handler.STATE_MACHINE_ARN)
