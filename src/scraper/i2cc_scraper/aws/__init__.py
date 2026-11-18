"""Running the scraper in AWS for the i2ccc RAG stack: an S3 Store, ingestion invalidation, and
the Lambda handlers of the scraper state machine. Needs the `aws` extra (boto3, opensearch-py)."""
from .invalidate import IngestInvalidator, opensearch_client  # noqa: F401
from .s3store import S3Store, file_id, file_title, object_metadata  # noqa: F401
