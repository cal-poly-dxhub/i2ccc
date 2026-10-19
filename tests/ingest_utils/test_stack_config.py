import pytest

import stack_config as sc

OUTPUTS = [
    {"OutputKey": "RagIngestInputBucketName96FB6D98", "OutputValue": "the-bucket"},
    {"OutputKey": "RagIngestStepFunctionArn869D1E3C", "OutputValue": "arn:sfn"},
    {"OutputKey": "RagIngestProcessedFilesTableNameCA03F9C1", "OutputValue": "the-table"},
    {"OutputKey": "RagBackendOpensearchAPIEndpointF7127769", "OutputValue": "os.example.com"},
    {"OutputKey": "RagBackendRagAPIEndpointD787D973", "OutputValue": "https://rag/prod/"},
    # CDK emits ProxyAPIEndpoint twice (automatic and explicit) with one value
    {"OutputKey": "RagBackendProxyAPIEndpoint751EBD3B", "OutputValue": "https://proxy/prod/"},
    {"OutputKey": "RagBackendProxyAPIEndpoint086820D5", "OutputValue": "https://proxy/prod/"},
    {"OutputKey": "RagFrontendCloudFrontURLECCDE56F", "OutputValue": "https://cf"},
]


class FakeCloudFormation:
    def __init__(self, outputs, error=None):
        self.outputs, self.error, self.calls = outputs, error, []

    def describe_stacks(self, StackName):
        self.calls.append(StackName)
        if self.error:
            raise self.error
        return {"Stacks": [{"Outputs": self.outputs}]}


@pytest.fixture
def cfn(monkeypatch):
    fake = FakeCloudFormation(OUTPUTS)
    monkeypatch.setattr(sc.boto3, "client", lambda service, region_name=None: fake)
    sc._stack_outputs.cache_clear()
    for key in sc.STACK_OUTPUTS:
        monkeypatch.delenv(key.upper(), raising=False)
    yield fake
    sc._stack_outputs.cache_clear()


@pytest.fixture
def write_config(tmp_path):
    def write(text="opensearch_index_name: idx\n"):
        path = tmp_path / "config.yaml"
        path.write_text(text)
        return str(path)

    return write


def test_resolves_every_key_from_stack_outputs(cfn, write_config):
    config = sc.load_config(config_path=write_config())

    assert config["s3_bucket_name"] == "the-bucket"
    assert config["step_function_arn"] == "arn:sfn"
    assert config["processed_files_table"] == "the-table"
    assert config["opensearch_endpoint"] == "os.example.com"
    assert config["rag_api_endpoint"] == "https://rag/prod/"
    assert config["proxy_api_endpoint"] == "https://proxy/prod/"
    assert config["opensearch_index_name"] == "idx"


def test_rag_and_proxy_api_outputs_are_not_confused(cfn, write_config):
    config = sc.load_config(needs=("rag_api_endpoint",), config_path=write_config())

    assert config["rag_api_endpoint"] == "https://rag/prod/"


def test_stale_values_in_config_yaml_are_ignored(cfn, write_config):
    path = write_config("s3_bucket_name: stale\nstep_function_arn: stale\n")

    config = sc.load_config(needs=("s3_bucket_name",), config_path=path)

    assert config["s3_bucket_name"] == "the-bucket"
    assert "step_function_arn" not in config


def test_environment_variable_wins_and_skips_cloudformation(cfn, write_config, monkeypatch):
    monkeypatch.setenv("S3_BUCKET_NAME", "env-bucket")

    config = sc.load_config(needs=("s3_bucket_name",), config_path=write_config())

    assert config["s3_bucket_name"] == "env-bucket"
    assert cfn.calls == []


def test_only_missing_keys_trigger_a_stack_lookup(cfn, write_config, monkeypatch):
    monkeypatch.setenv("S3_BUCKET_NAME", "env-bucket")

    config = sc.load_config(
        needs=("s3_bucket_name", "step_function_arn"), config_path=write_config()
    )

    assert config["s3_bucket_name"] == "env-bucket"
    assert config["step_function_arn"] == "arn:sfn"
    assert len(cfn.calls) == 1


def test_stack_is_described_once_across_calls(cfn, write_config):
    path = write_config()

    sc.load_config(needs=("s3_bucket_name",), config_path=path)
    sc.load_config(needs=("step_function_arn",), config_path=path)

    assert len(cfn.calls) == 1


def test_defaults_and_overrides_for_stack_name_and_region(cfn, write_config):
    default = sc.load_config(needs=("s3_bucket_name",), config_path=write_config())
    custom = sc.load_config(
        needs=("s3_bucket_name",),
        config_path=write_config("stack_name: Other\naws_region: eu-west-1\n"),
    )

    assert default["stack_name"] == "RagChatbotStack"
    assert default["aws_region"] == "us-west-2"
    assert custom["stack_name"] == "Other"
    assert custom["aws_region"] == "eu-west-1"
    assert cfn.calls == ["RagChatbotStack", "Other"]


def test_missing_output_names_the_stack_and_output(cfn, write_config):
    cfn.outputs = [o for o in OUTPUTS if "StepFunctionArn" not in o["OutputKey"]]

    with pytest.raises(sc.StackConfigError, match="StepFunctionArn.*step_function_arn"):
        sc.load_config(needs=("step_function_arn",), config_path=write_config())


def test_conflicting_outputs_are_an_error(cfn, write_config):
    cfn.outputs = OUTPUTS + [
        {"OutputKey": "OtherInputBucketName12345678", "OutputValue": "other-bucket"}
    ]

    with pytest.raises(sc.StackConfigError, match="several different outputs"):
        sc.load_config(needs=("s3_bucket_name",), config_path=write_config())


def test_unreachable_stack_error_says_how_to_fix_it(cfn, write_config):
    cfn.error = RuntimeError("does not exist")

    with pytest.raises(sc.StackConfigError, match="RagChatbotStack.*us-west-2.*stack_name"):
        sc.load_config(needs=("s3_bucket_name",), config_path=write_config())


def test_unknown_key_is_rejected(cfn, write_config):
    with pytest.raises(ValueError, match="bogus"):
        sc.load_config(needs=("bogus",), config_path=write_config())


def test_missing_config_file_is_a_clear_error(cfn, tmp_path):
    with pytest.raises(sc.StackConfigError, match="config.yaml not found"):
        sc.load_config(config_path=str(tmp_path / "nope.yaml"))
