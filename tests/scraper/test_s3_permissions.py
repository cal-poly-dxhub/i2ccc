"""Missing objects under prefix-scoped IAM must not hide genuine authorization failures."""
import boto3
import pytest
from botocore.exceptions import ClientError
from botocore.stub import Stubber

from i2cc_scraper.aws.s3store import get_object


@pytest.fixture
def client():
    return boto3.client('s3', region_name='us-east-1',
                        aws_access_key_id='testing', aws_secret_access_key='testing')


@pytest.mark.parametrize('contents', [[], [{'Key': 'scraper/missing.json.other'}]])
def test_missing_key_403_is_confirmed_by_listing_only_its_prefix(client, contents):
    with Stubber(client) as stub:
        stub.add_client_error('get_object', service_error_code='AccessDenied', http_status_code=403,
                              expected_params={'Bucket': 'bucket', 'Key': 'scraper/missing.json'})
        stub.add_response('list_objects_v2', {'Contents': contents},
                          {'Bucket': 'bucket', 'Prefix': 'scraper/missing.json', 'MaxKeys': 1})
        with pytest.raises(client.exceptions.NoSuchKey):
            get_object(client, 'bucket', 'scraper/missing.json')
        stub.assert_no_pending_responses()


def test_denied_existing_object_stays_denied(client):
    with Stubber(client) as stub:
        stub.add_client_error('get_object', service_error_code='AccessDenied', http_status_code=403)
        stub.add_response('list_objects_v2', {'Contents': [{'Key': 'scraper/private.json'}]},
                          {'Bucket': 'bucket', 'Prefix': 'scraper/private.json', 'MaxKeys': 1})
        with pytest.raises(ClientError) as exc:
            get_object(client, 'bucket', 'scraper/private.json')
        assert exc.value.response['Error']['Code'] == 'AccessDenied'


def test_denied_listing_is_not_interpreted_as_a_missing_object(client):
    with Stubber(client) as stub:
        stub.add_client_error('get_object', service_error_code='AccessDenied', http_status_code=403)
        stub.add_client_error('list_objects_v2', service_error_code='AccessDenied', http_status_code=403)
        with pytest.raises(ClientError) as exc:
            get_object(client, 'bucket', 'other/private.json')
        assert exc.value.response['Error']['Code'] == 'AccessDenied'
