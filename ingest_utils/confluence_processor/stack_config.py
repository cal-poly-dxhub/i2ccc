"""
Loads config.yaml and fills in the values that belong to the deployed stack.

The bucket, state machine, table and API URLs are created by CDK, so they are
read from the stack's CloudFormation outputs instead of being copied into
config.yaml after every deploy. Anything the stack owns that is still in
config.yaml is ignored, so a stale pasted value can never win.

Resolution order for each stack-owned key:
  1. an environment variable named like the key, upper-cased
     (S3_BUCKET_NAME, STEP_FUNCTION_ARN, ...). The collector container uses
     this because its task role cannot call CloudFormation.
  2. the stack's CloudFormation output.

Usage:
    from stack_config import load_config
    config = load_config(needs=("s3_bucket_name",))
"""

import os
import re
from functools import lru_cache

import boto3  # type: ignore
import yaml  # type: ignore

DEFAULT_STACK_NAME = "RagChatbotStack"
DEFAULT_REGION = "us-west-2"

CONFIG_PATH = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "config.yaml")
)

# config key -> CloudFormation output name. CDK prefixes explicit outputs with
# the construct path and appends an 8-character hash (RagIngestInputBucketName96FB6D98),
# so outputs are matched on the name with that hash stripped.
STACK_OUTPUTS = {
    "s3_bucket_name": "InputBucketName",
    "step_function_arn": "StepFunctionArn",
    "processed_files_table": "ProcessedFilesTableName",
    "opensearch_endpoint": "OpensearchAPIEndpoint",
    "rag_api_endpoint": "RagAPIEndpoint",
    "proxy_api_endpoint": "ProxyAPIEndpoint",
}

_HASH_SUFFIX = re.compile(r"[0-9A-F]{8}$")


class StackConfigError(RuntimeError):
    """The config file or the stack outputs could not be resolved."""


@lru_cache(maxsize=None)
def _stack_outputs(stack_name: str, region: str) -> dict:
    """Returns {output name without hash suffix: [values]} for a stack."""
    cfn = boto3.client("cloudformation", region_name=region)
    try:
        stack = cfn.describe_stacks(StackName=stack_name)["Stacks"][0]
    except Exception as e:
        raise StackConfigError(
            f"Could not read CloudFormation stack '{stack_name}' in {region}: {e}. "
            "Check your AWS credentials and set stack_name / aws_region in config.yaml "
            "if they differ from the defaults."
        ) from e

    outputs: dict = {}
    for output in stack.get("Outputs", []):
        outputs.setdefault(_HASH_SUFFIX.sub("", output["OutputKey"]), []).append(
            output["OutputValue"]
        )
    return outputs


def _from_outputs(outputs: dict, key: str, stack_name: str) -> str:
    name = STACK_OUTPUTS[key]
    values = {v for k, vs in outputs.items() if k.endswith(name) for v in vs}
    if not values:
        raise StackConfigError(
            f"Stack '{stack_name}' has no output ending in '{name}' (needed for '{key}'). "
            "Redeploy the stack with the current code to publish it."
        )
    if len(values) > 1:
        raise StackConfigError(
            f"Stack '{stack_name}' has several different outputs ending in '{name}': "
            f"{sorted(values)}"
        )
    return values.pop()


def load_config(needs=tuple(STACK_OUTPUTS), config_path: str = CONFIG_PATH) -> dict:
    """Returns config.yaml with the requested stack-owned keys filled in.

    needs: the stack-owned keys the caller uses (any of STACK_OUTPUTS). Only
    those are resolved, so CloudFormation is not called when every one of them
    is available from the environment.
    """
    unknown = set(needs) - set(STACK_OUTPUTS)
    if unknown:
        raise ValueError(f"Not stack-owned config keys: {sorted(unknown)}")

    try:
        with open(config_path, "r") as f:
            config = yaml.safe_load(f) or {}
    except FileNotFoundError:
        raise StackConfigError(f"config.yaml not found at {config_path}") from None

    # Drop stale copies of anything the stack owns
    for key in STACK_OUTPUTS:
        config.pop(key, None)

    config.setdefault("aws_region", DEFAULT_REGION)
    stack_name = config.setdefault("stack_name", DEFAULT_STACK_NAME)

    missing = []
    for key in needs:
        value = os.environ.get(key.upper())
        if value:
            config[key] = value
        else:
            missing.append(key)

    if missing:
        outputs = _stack_outputs(stack_name, config["aws_region"])
        for key in missing:
            config[key] = _from_outputs(outputs, key, stack_name)

    return config
