"""A Store in an S3 bucket, laid out for the i2ccc ingestion pipeline:

    <prefix><key>.metadata.json               the Record
    <prefix><key>/<hash>/<Title><suffix>      its content files
    <prefix>.staging/<uuid>                   downloads in progress (stage())

i2ccc ingests every .txt/.vtt/.pdf/.mp4 in the bucket, reads the S3 object metadata that its own
uploader writes (source-url, member-content, parent-folder-name/url, relative-start-time), and
shows the file name as the citation title -- hence the title as the file name. <hash> is the
first 12 hex digits of the main file's SHA-256: new content always gets new object names, so
two writers racing on one key never overwrite each other's files, and unchanged content keeps
its names. Records and staging objects have extensions ingestion ignores.

Each content object also carries scraper-key and scraper-file-id metadata, which ingestion copies
into every vector it makes from the file. When a file is replaced or removed, the Invalidator
(if given) deletes those vectors and the file's processed-files entry, so the next ingestion run
picks up the new version.

Records are written with S3 conditional writes (If-Match / If-None-Match on the ETag), which is
what makes `expected=` work across Lambdas. Unlike LocalStore, a put() refused by a conflict may
already have placed its new objects; it removes the ones the winning Record doesn't list.
"""
import hashlib
import json
import logging
import mimetypes
import re
import unicodedata
import uuid
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional, Protocol

from botocore.exceptions import ClientError
from requests.utils import requote_uri

from ..downloader.errors import TransientError
from ..store import (METADATA_SUFFIX, STAGING_DIR, UNCHECKED, ConflictError, Content, Record,
                     Staged, check_expected, check_key, check_suffix)

log = logging.getLogger('i2cc_scraper.aws')

PART_SIZE = 8 << 20                  # multipart upload part size (S3's minimum is 5 MiB)
LEFTOVER_AGE = timedelta(minutes=30)  # an unlisted object younger than this may be another
                                      # writer's, placed but not yet listed in its Record
MEDIA_SUFFIXES = ('.mp4', '.webm', '.m4a', '.mp3', '.wav', '.flac')
_CONFLICT_CODES = {'PreconditionFailed', 'ConditionalRequestConflict', 'NoSuchKey', '412', '409'}


class Invalidator(Protocol):
    def invalidate(self, s3_uri: str, file_id: str) -> None:
        """Make ingestion forget the object: what it indexed from it, and that it was done."""


def file_id(object_key: str) -> str:
    """The scraper-file-id of the object at object_key: how its vectors are found again."""
    return hashlib.sha1(object_key.encode()).hexdigest()[:20]


def file_title(title: str, key: str) -> str:
    """title as an ASCII file name (no path or URL syntax), else the key's ID."""
    text = unicodedata.normalize('NFKD', title).encode('ascii', 'ignore').decode()
    text = re.sub(r"[^A-Za-z0-9 ._,()&+'-]+", ' ', text)
    text = re.sub(r'\s+', ' ', text).strip(' .')[:100].strip(' .')
    return text or key.rsplit('/', 1)[1]


def _ascii(text: str, limit: int = 512) -> str:
    """A metadata value: S3 user metadata must be ASCII, and is limited to 2 KB in all."""
    text = unicodedata.normalize('NFKD', text).encode('ascii', 'ignore').decode()
    return re.sub(r'[\x00-\x1f\x7f]', '', text)[:limit]


def object_metadata(record: Record, object_key: str) -> dict[str, str]:
    """The S3 metadata of one content object: i2ccc's S3Uploader fields, plus the scraper's."""
    metadata = {
        'member-content': 'true' if record.is_subscriber_content else 'false',
        'source-url': _ascii(requote_uri(record.source_url)),
        'scraper-key': record.key,
        'scraper-file-id': file_id(object_key),
    }
    if record.parent_folder_name:
        metadata['parent-folder-name'] = _ascii(record.parent_folder_name)
    if record.parent_folder_url:
        metadata['parent-folder-url'] = _ascii(requote_uri(record.parent_folder_url))
    if object_key.lower().endswith(MEDIA_SUFFIXES):  # i2ccc offsets video timestamps by it
        metadata['relative-start-time'] = str(record.relative_start_time or 0)
    return metadata


class S3Store:
    def __init__(self, bucket: str, prefix: str = '', client=None,
                 invalidator: Optional[Invalidator] = None):
        import boto3
        if prefix and not prefix.endswith('/'):
            prefix += '/'
        self.bucket, self.prefix = bucket, prefix
        self.client = client or boto3.client('s3')
        self.invalidator = invalidator

    # -- names -----------------------------------------------------------------------------

    def file_name(self, record: Record, suffix: str) -> str:
        """The store name (as in Record.files) of record's file with this suffix."""
        return (f'{record.key}/{record.content_sha256[:12]}/'
                f'{file_title(record.title, record.key)}{check_suffix(suffix)}')

    def suffix_of(self, key: str, name: str) -> str:
        """From the end of the file name. Only captions have two-part suffixes ('.en.vtt'), so
        a title ending in e.g. 'v1.2' doesn't make '.2.pdf' a suffix."""
        match = (re.search(r'\.[a-z]{2,10}\.(?:vtt|srt)$', name)
                 or re.search(r'\.[a-z0-9]{1,10}$', name))
        return match[0] if match else ''

    def object_key(self, name: str) -> str:
        return self.prefix + name

    def uri(self, name: str) -> str:
        return f's3://{self.bucket}/{self.object_key(name)}'

    def _record_key(self, key: str) -> str:
        return self.prefix + check_key(key) + METADATA_SUFFIX

    # -- Store -----------------------------------------------------------------------------

    def get(self, key: str) -> Optional[Record]:
        try:
            response = get_object(self.client, self.bucket, self._record_key(key))
        except ClientError as exc:
            if exc.response['Error']['Code'] in ('NoSuchKey', '404'):
                return None
            raise
        with response['Body'] as body:
            record = Record.from_json(body.read().decode('utf-8'))
        record.etag = response['ETag']
        return record

    def read(self, name: str) -> bytes:
        """A stored file's content (from Record.files)."""
        return self.client.get_object(Bucket=self.bucket,
                                      Key=self.object_key(name))['Body'].read()

    def stage(self, chunks: Iterable[bytes]) -> Staged:
        key = f'{self.prefix}{STAGING_DIR}/{uuid.uuid4().hex}'
        digest, size, buffer = hashlib.sha256(), 0, bytearray()
        upload_id, parts = None, []
        try:
            for chunk in chunks:
                digest.update(chunk)
                size += len(chunk)
                buffer += chunk
                while len(buffer) >= PART_SIZE:
                    if upload_id is None:
                        upload_id = self.client.create_multipart_upload(
                            Bucket=self.bucket, Key=key)['UploadId']
                    parts.append(self._upload_part(key, upload_id, len(parts) + 1,
                                                   bytes(buffer[:PART_SIZE])))
                    del buffer[:PART_SIZE]
            if upload_id is None:
                self.client.put_object(Bucket=self.bucket, Key=key, Body=bytes(buffer))
            else:
                if buffer:
                    parts.append(self._upload_part(key, upload_id, len(parts) + 1, bytes(buffer)))
                self.client.complete_multipart_upload(
                    Bucket=self.bucket, Key=key, UploadId=upload_id,
                    MultipartUpload={'Parts': parts})
        except BaseException:
            if upload_id is not None:
                try:
                    self.client.abort_multipart_upload(Bucket=self.bucket, Key=key,
                                                       UploadId=upload_id)
                except Exception:  # the bucket's lifecycle rule aborts it later
                    log.warning('Could not abort the upload of %s', key, exc_info=True)
            raise
        return Staged(key, digest.hexdigest(), size)

    def _upload_part(self, key: str, upload_id: str, number: int, data: bytes) -> dict:
        response = self.client.upload_part(Bucket=self.bucket, Key=key, UploadId=upload_id,
                                           PartNumber=number, Body=data)
        return {'PartNumber': number, 'ETag': response['ETag']}

    def discard(self, staged: Staged) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=staged.ref)

    def put(self, record: Record, files: list[tuple[str, Content]],
            keep_other_files: bool = False, expected: object = UNCHECKED) -> Record:
        check_key(record.key)
        current = self.get(record.key)
        check_expected(record.key, current, expected)   # before anything is placed
        names = [self.file_name(record, suffix) for suffix, _ in files]
        old = list(current.files) if current else []
        listed = old + [n for n in names if n not in old] if keep_other_files else names
        dropped = [n for n in old if n not in listed]
        # Same name means same content for the main file (its hash is in the name), not for
        # the others (e.g. captions fetched again).
        rewritten = [n for i, n in enumerate(names)
                     if n in old and (i > 0 or keep_other_files)]
        self._invalidate(dropped + rewritten, strict=True)

        placed = []
        try:
            for i, (name, (_, content)) in enumerate(zip(names, files)):
                main = i == 0 and not keep_other_files
                self._place(name, content, record, record.content_type if main else '')
                placed.append(name)
            record.files = listed
            self._write_record(record, expected)
        except ConflictError:
            winner = self.get(record.key)
            keep = set(old) | set(winner.files if winner else [])
            self._delete([n for n in placed if n not in keep])
            raise
        self._delete(dropped)
        for _, content in files:
            if isinstance(content, Staged):
                self.discard(content)
        self._remove_leftovers(record)
        self._invalidate(dropped + rewritten, strict=False)  # in case ingestion ran meanwhile
        return record

    def put_record(self, record: Record, expected: object = UNCHECKED) -> None:
        check_key(record.key)
        current = self.get(record.key)
        check_expected(record.key, current, expected)
        self._write_record(record, expected)
        if current is not None and current.files == record.files:
            self._refresh_metadata(current, record)
        self._remove_leftovers(record)

    def delete(self, key: str) -> None:
        """The Record goes first, so a crash in between leaves unlisted objects (removed by the
        next write to the key), never a Record listing missing files."""
        current = self.get(key)
        if current is not None:
            self._invalidate(current.files, strict=True)
        self.client.delete_object(Bucket=self.bucket, Key=self._record_key(key))
        self._delete([obj['Key'][len(self.prefix):] for obj in self._objects_under(key)])

    # -- internals -------------------------------------------------------------------------

    def _write_record(self, record: Record, expected: object) -> None:
        condition = {}
        if expected is None:
            condition['IfNoneMatch'] = '*'
        elif isinstance(expected, Record):
            condition['IfMatch'] = expected.etag
        try:
            response = self.client.put_object(
                Bucket=self.bucket, Key=self._record_key(record.key),
                Body=record.to_json().encode('utf-8'), ContentType='application/json',
                **condition)
        except ClientError as exc:
            if condition and exc.response['Error']['Code'] in _CONFLICT_CODES:
                raise ConflictError(f'{record.key} was changed by someone else since it was '
                                    'read') from exc
            raise
        record.etag = response['ETag']

    def _place(self, name: str, content: Content, record: Record, content_type: str) -> None:
        key = self.object_key(name)
        extra = {'Metadata': object_metadata(record, key),
                 'ContentType': (content_type or mimetypes.guess_type(name)[0]
                                 or 'application/octet-stream')}
        if isinstance(content, Staged):  # server-side; multipart for big objects
            self.client.copy({'Bucket': self.bucket, 'Key': content.ref}, self.bucket, key,
                             ExtraArgs={**extra, 'MetadataDirective': 'REPLACE'})
        else:
            self.client.upload_file(str(content), self.bucket, key, ExtraArgs=extra)

    def _refresh_metadata(self, before: Record, after: Record) -> None:
        """Rewrite the content objects' metadata if a metadata-only change altered it (e.g.
        the item turned up in a Drive folder). What was already indexed keeps the old values."""
        for name in after.files:
            key = self.object_key(name)
            new = object_metadata(after, key)
            if new == object_metadata(before, key):
                continue
            head = self.client.head_object(Bucket=self.bucket, Key=key)
            self.client.copy({'Bucket': self.bucket, 'Key': key}, self.bucket, key,
                             ExtraArgs={'Metadata': new, 'MetadataDirective': 'REPLACE',
                                        'ContentType': head.get('ContentType',
                                                                'application/octet-stream')})

    def _objects_under(self, key: str) -> list[dict]:
        objects = []
        paginator = self.client.get_paginator('list_objects_v2')
        for page in paginator.paginate(Bucket=self.bucket,
                                       Prefix=f'{self.prefix}{check_key(key)}/'):
            objects += page.get('Contents', [])
        return objects

    def _remove_leftovers(self, record: Record) -> None:
        """Delete objects under the key that the Record doesn't list and that are too old to be
        another writer's work in progress: what a write killed partway leaves behind."""
        cutoff = datetime.now(timezone.utc) - LEFTOVER_AGE
        listed = set(record.files)
        stale = [obj['Key'][len(self.prefix):] for obj in self._objects_under(record.key)
                 if obj['Key'][len(self.prefix):] not in listed and obj['LastModified'] < cutoff]
        if stale:
            self._invalidate(stale, strict=False)
            self._delete(stale)

    def _delete(self, names: list[str]) -> None:
        for name in names:
            self.client.delete_object(Bucket=self.bucket, Key=self.object_key(name))

    def _invalidate(self, names: list[str], strict: bool) -> None:
        """Tell ingestion these objects are gone or replaced. strict: a failure is a
        TransientError (nothing has changed yet, so the write can simply be retried)."""
        if self.invalidator is None:
            return
        for name in dict.fromkeys(names):
            try:
                self.invalidator.invalidate(self.uri(name), file_id(self.object_key(name)))
            except Exception as exc:
                if strict:
                    raise TransientError(f'Could not clear ingestion state for {name}: '
                                         f'{exc}') from exc
                log.warning('Could not clear ingestion state for %s: %s', name, exc)


def get_object(client, bucket: str, key: str):
    """Preserve missing-key behavior with prefix-scoped ListBucket permissions.

    S3 may return 403 for a missing object when the caller lacks unrestricted ListBucket.
    Check only that key's prefix before translating to NoSuchKey; a real denied object or
    denied listing must remain an authorization error, never a cache miss.
    """
    try:
        return client.get_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        if exc.response['Error']['Code'] not in ('AccessDenied', '403'):
            raise
        found = client.list_objects_v2(Bucket=bucket, Prefix=key, MaxKeys=1)
        if any(item['Key'] == key for item in found.get('Contents', [])):
            raise
        raise client.exceptions.NoSuchKey({
            'Error': {'Code': 'NoSuchKey', 'Message': 'The specified key does not exist'},
            'ResponseMetadata': {'HTTPStatusCode': 404},
        }, 'GetObject') from exc


def read_json(client, bucket: str, key: str):
    with get_object(client, bucket, key)['Body'] as body:
        return json.loads(body.read())


def write_json(client, bucket: str, key: str, data) -> None:
    client.put_object(Bucket=bucket, Key=key, ContentType='application/json',
                      Body=json.dumps(data, ensure_ascii=False).encode('utf-8'))
