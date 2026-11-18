"""Admin API for the admin frontend: sources, their documents, rerun, delete and ingest."""

import json
import logging
from typing import Any, Annotated
import boto3
from botocore.exceptions import BotoCoreError, ClientError

from aws_lambda_powertools.event_handler.openapi.params import Query
from aws_lambda_powertools.event_handler import (
    APIGatewayRestResolver,
    CORSConfig,
    Response,
    content_types,
)
from aws_lambda_powertools.utilities.typing import LambdaContext
import os

logger = logging.getLogger()
logger.setLevel(logging.INFO)

sfn = boto3.client("stepfunctions")

STATE_MACHINE_ARN = os.environ["STATE_MACHINE_ARN"]

# TODO: build from the ALLOWED_ORIGINS environment variable
cors_config = CORSConfig(allow_origin="*")

app = APIGatewayRestResolver(
    cors=cors_config, serializer=lambda body: json.dumps(body, default=str)
)

@app.post("/api/ingest")
def start_ingest():
    """Starts ingestion of all pending documents."""
    try:
        is_running = sfn.list_executions(
            stateMachineArn=STATE_MACHINE_ARN, statusFilter="RUNNING", maxResults=1
        )["executions"]

        if is_running:
            return {"message": "Ingestion already running"}, 409

        response = sfn.start_execution(stateMachineArn=STATE_MACHINE_ARN)
    except (ClientError, BotoCoreError):
        logger.exception(
            "Failed to start ingestion state machine %s", STATE_MACHINE_ARN
        )
        return {"message": "Failed to start ingestion"}, 500

    return {
        "executionArn": response["executionArn"],
        "message": "Ingestion started successfully",
    }, 202


def lambda_handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    return app.resolve(event, context)
