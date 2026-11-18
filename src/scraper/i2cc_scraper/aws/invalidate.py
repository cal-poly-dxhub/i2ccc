"""Making the i2ccc ingestion pipeline forget a file that the scraper replaced or removed.

i2ccc skips any S3 URI listed in its processed-files DynamoDB table, and its OpenSearch vectors
carry the S3 object's metadata -- including the scraper-file-id the S3Store sets. So forgetting a
file is: delete its table entry, and delete the vectors with its scraper-file-id. (OpenSearch
Serverless has no delete-by-query: the vectors are found by search, then deleted in bulk.)
"""
import logging
from typing import Optional

log = logging.getLogger('i2cc_scraper.aws')

FILE_ID_FIELD = 'metadata.scraper-file-id.keyword'   # dynamic mapping: text + .keyword
PAGE = 1000


class IngestInvalidator:
    def __init__(self, table: Optional[str] = None, opensearch=None, index: str = '',
                 dynamodb=None):
        """table: the processed-files table. opensearch: an opensearchpy.OpenSearch client for
        the collection holding `index` (see opensearch_client()). Either may be left out."""
        import boto3
        self.table = table
        self.dynamodb = dynamodb or (boto3.client('dynamodb') if table else None)
        self.opensearch, self.index = opensearch, index

    def invalidate(self, s3_uri: str, file_id: str) -> None:
        if self.table:
            self.dynamodb.delete_item(TableName=self.table, Key={'s3_uri': {'S': s3_uri}})
        if self.opensearch is not None:
            removed = self._delete_vectors(file_id)
            if removed:
                log.info('Removed %d indexed chunks of %s', removed, s3_uri)

    def _delete_vectors(self, file_id: str) -> int:
        ids, after = [], None
        while True:
            body = {'query': {'term': {FILE_ID_FIELD: file_id}}, 'size': PAGE,
                    'sort': [{'_id': 'asc'}], '_source': False}
            if after is not None:
                body['search_after'] = after
            hits = self.opensearch.search(index=self.index, body=body)['hits']['hits']
            ids += [hit['_id'] for hit in hits]
            if len(hits) < PAGE:
                break
            after = hits[-1]['sort']
        if ids:
            response = self.opensearch.bulk(
                body=[{'delete': {'_index': self.index, '_id': i}} for i in ids])
            failed = [item for item in response.get('items', [])
                      if item.get('delete', {}).get('status') not in (200, 404)]
            if failed:
                raise RuntimeError(f'OpenSearch refused to delete {len(failed)} chunk(s): '
                                   f'{failed[0]}')
        return len(ids)


def opensearch_client(endpoint: str, region: str):
    """A client for an OpenSearch Serverless collection (endpoint without https://), signed
    with the Lambda's credentials -- the way i2ccc's ingest functions connect."""
    import boto3
    from opensearchpy import AWSV4SignerAuth, OpenSearch, RequestsHttpConnection

    auth = AWSV4SignerAuth(boto3.Session().get_credentials(), region, 'aoss')
    return OpenSearch(hosts=[{'host': endpoint.removeprefix('https://'), 'port': 443}],
                      http_auth=auth, use_ssl=True, verify_certs=True,
                      connection_class=RequestsHttpConnection, timeout=30)
