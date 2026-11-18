"""Shared test fixtures: a local web server for crawl/retry/download tests, and fast retries.

Tests never touch the network: real pages are saved snapshots in tests/fixtures/, and
everything that needs HTTP talks to the local server below.
"""
import collections
import json
import os
import re
import subprocess
import sys
import ipaddress
import socket
import types
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from i2cc_scraper import config
from i2cc_scraper.store import LocalStore

FIXTURES = Path(__file__).parent / 'fixtures'
PACKAGE_ROOT = Path(__file__).parents[2] / 'src' / 'scraper'  # what `import i2cc_scraper` needs


def fixture_html(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


class Site:
    """A local web server. Register responses with site.route(path, ...); site.hits counts
    requests per path (including the query string)."""

    def __init__(self):
        self.routes: dict[str, object] = {}
        self.hits: collections.Counter = collections.Counter()
        site = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 (http.server's naming)
                site.hits[self.path] += 1
                route = site.routes.get(self.path)
                if route is None:
                    self._send(404, {'Content-Type': 'text/html'}, b'<h1>Not found</h1>')
                elif callable(route):
                    route(self, site.hits[self.path])
                else:
                    self._send(*route)

            def _send(self, status, headers, body):
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):  # keep test output quiet
                pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.base = f'http://127.0.0.1:{self.server.server_address[1]}'
        self.thread = threading.Thread(target=self.server.serve_forever, args=(0.02,), daemon=True)
        self.thread.start()

    def url(self, path: str) -> str:
        return self.base + path

    def route(self, path: str, body: bytes | str = b'', status: int = 200,
              content_type: str = 'text/html; charset=utf-8', headers: dict | None = None):
        if isinstance(body, str):
            body = body.encode()
        self.routes[path] = (status, {'Content-Type': content_type, **(headers or {})}, body)

    def page(self, path: str, main: str, extra: str = ''):
        """Register an HTML page: `main` goes inside <main>, `extra` (e.g. nav/footer) outside."""
        self.route(path, f'<html><head><title>{path}</title></head><body>{extra}'
                         f'<main>{main}</main></body></html>')

    def close(self):
        self.server.shutdown()
        self.server.server_close()


# --- route builders for Site -------------------------------------------------------------------

OK_PAGE = b'<html><main>ok</main></html>'


def flaky(failures: int, status: int = 503, headers: dict | None = None):
    """A route that fails `failures` times with `status`, then serves OK_PAGE."""
    def handler(request, hit):
        if hit <= failures:
            request._send(status, {'Content-Type': 'text/html', **(headers or {})}, b'busy')
        else:
            request._send(200, {'Content-Type': 'text/html'}, OK_PAGE)
    return handler


def etag_route(etag: str, body: bytes, seen: list):
    """A file with an ETag that answers If-None-Match with 304. seen collects the requests'
    If-None-Match headers."""
    def handler(request, hit):
        seen.append(request.headers.get('If-None-Match'))
        if request.headers.get('If-None-Match') == etag:
            request.send_response(304)
            request.send_header('ETag', etag)
            request.end_headers()
        else:
            request._send(200, {'Content-Type': 'application/pdf', 'ETag': etag}, body)
    return handler


def run_cli(args: list[str], tmp_path: Path, env: dict | None = None) -> tuple[int, list, str]:
    """Run i2cc-download in a subprocess with a store in tmp_path, allowed to reach the local
    test server: (exit code, JSON results, stderr). env: more environment variables."""
    env = {'I2CC_BLOCK_PRIVATE_ADDRESSES': '0', 'I2CC_CRAWL_DELAY': '0', 'PATH': '/usr/bin:/bin',
           'HOME': str(tmp_path), 'PYTHONPATH': str(PACKAGE_ROOT), **(env or {})}
    run = subprocess.run([sys.executable, '-m', 'i2cc_scraper.downloader', *args,
                          '--out', str(tmp_path / 'store')],
                         capture_output=True, text=True, env=env, cwd=tmp_path, timeout=60)
    return run.returncode, json.loads(run.stdout or '[]'), run.stderr


@pytest.fixture
def site():
    s = Site()
    yield s
    s.close()


TEST_CONFIG = config.Config(retry_backoff=0, retry_jitter=0, crawl_delay=0,
                            block_private_addresses=False)  # the test server is on 127.0.0.1


@pytest.fixture(autouse=True)
def fast_and_local():
    """No waiting between retries or pages, and allow the local test server's address. Tests
    never use real Google credentials: Drive tests pass a fake Drive client explicitly."""
    config.set_default(TEST_CONFIG)
    yield
    config.set_default(None)


@pytest.fixture
def store(tmp_path):
    return LocalStore(tmp_path / 'store')


# --- AWS Emulator (S3, DynamoDB) ------------------------------------------------------------------

@pytest.fixture(scope='session')
def emulator():
    """boto3 clients for AWS Emulator, or skip: run the suite with its connection variables set,
    `( . ~/.aws/emulator/9297.env && pytest )`. Never real AWS: the endpoint must be local."""
    endpoint = os.environ.get('AWS_ENDPOINT_URL', '')
    if not re.match(r'https?://(127\.0\.0\.1|localhost)(:\d+)?/?$', endpoint):
        pytest.skip('needs AWS Emulator: ( . ~/.aws/emulator/9297.env && pytest )')
    import boto3
    return types.SimpleNamespace(s3=boto3.client('s3'), dynamodb=boto3.client('dynamodb'))


@pytest.fixture(scope='session')
def bucket(emulator):
    name = f'i2cc-test-{uuid.uuid4().hex[:12]}'
    emulator.s3.create_bucket(Bucket=name)
    yield name
    empty_bucket(emulator.s3, name)  # the emulator keeps everything in memory, and has a cap
    emulator.s3.delete_bucket(Bucket=name)


def empty_bucket(s3, name):
    for page in s3.get_paginator('list_objects_v2').paginate(Bucket=name):
        for obj in page.get('Contents', []):
            s3.delete_object(Bucket=name, Key=obj['Key'])
    for upload in s3.list_multipart_uploads(Bucket=name).get('Uploads', []):
        s3.abort_multipart_upload(Bucket=name, Key=upload['Key'], UploadId=upload['UploadId'])


@pytest.fixture
def s3_store(emulator, bucket):
    """An S3Store under a prefix of its own, in the session's emulator bucket."""
    from i2cc_scraper.aws import S3Store
    return S3Store(bucket, f'test-{uuid.uuid4().hex[:12]}/', client=emulator.s3)


@pytest.fixture(autouse=True)
def no_internet(monkeypatch):
    """Fail any test that tries to connect anywhere but this machine: tests must not depend on
    (or quietly reach) the network."""
    real_connect = socket.socket.connect

    def connect(sock, address):
        host = address[0] if isinstance(address, tuple) else address
        try:
            local = ipaddress.ip_address(host).is_loopback
        except ValueError:
            local = host == 'localhost'
        if not local:
            raise AssertionError(f'test tried to reach the network: {address}')
        return real_connect(sock, address)
    monkeypatch.setattr(socket.socket, 'connect', connect)


# --- a stand-in for yt-dlp ----------------------------------------------------------------------

class FakeYtDlpError(Exception):
    pass


def range_route(body: bytes):
    """A file served in parts when a request asks for a Range, as video hosts do."""
    def handler(request, hit):
        match = re.fullmatch(r'bytes=(\d+)-(\d+)', request.headers.get('Range', ''))
        if not match:
            request._send(200, {'Content-Type': 'video/mp4'}, body)
            return
        start, end = int(match[1]), min(int(match[2]), len(body) - 1)
        if start >= len(body):
            request._send(416, {'Content-Range': f'bytes */{len(body)}'}, b'')
            return
        request._send(206, {'Content-Type': 'video/mp4',
                            'Content-Range': f'bytes {start}-{end}/{len(body)}'},
                      body[start:end + 1])
    return handler


@pytest.fixture
def fake_yt_dlp(monkeypatch, site):
    """A stand-in for the yt_dlp module. Set .error to make downloads fail with that message,
    and .captions = False for a video that has no captions (yet). .options collects the
    options of each call. The format it picks is served by `site` at /videoplayback (in ranges);
    set .streamable = False for one that only yt-dlp itself can download (merged streams)."""
    module = types.ModuleType('yt_dlp')
    module.streamable = True
    site.routes['/videoplayback'] = range_route(b'video')
    module.utils = types.SimpleNamespace(DownloadError=FakeYtDlpError)
    module.error = None
    module.captions = True
    module.subtitle_text = 'WEBVTT\n'
    module.during_download = None          # a test can spend (fake) time in the middle of it
    module.calls = []
    module.options = []

    class YoutubeDL:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, url, download):
            module.calls.append(url)
            module.options.append(self.options)
            if module.error:
                raise FakeYtDlpError('ERROR: ' + module.error)
            template = self.options['outtmpl']['default']
            info = {'id': 'VIDEO1', 'title': 'Keynote recording', 'requested_downloads': [],
                    'requested_subtitles': {}}
            if self.options.get('skip_download'):
                if module.streamable and self.options.get('format'):  # a video, not captions only
                    if module.during_download:
                        module.during_download()       # the download takes time, streamed or not
                    info['requested_downloads'] = [{
                        'url': site.url('/videoplayback'), 'protocol': 'http', 'ext': 'mp4',
                        'http_headers': {'User-Agent': 'yt-dlp'}}]
            else:
                if module.during_download:
                    module.during_download()
                for hook in self.options.get('progress_hooks', []):
                    hook({'status': 'downloading'})      # yt-dlp reports progress like this
                video = template.replace('%(ext)s', 'mp4')
                open(video, 'wb').write(b'video')
                info['requested_downloads'] = [{'filepath': video}]
            if module.captions and self.options.get('writesubtitles'):
                subs = template.replace('%(ext)s', 'en.vtt')
                open(subs, 'w').write(module.subtitle_text)
                info['requested_subtitles'] = {'en': {'filepath': subs}}
            return info

    module.YoutubeDL = YoutubeDL
    monkeypatch.setitem(sys.modules, 'yt_dlp', module)
    return module
