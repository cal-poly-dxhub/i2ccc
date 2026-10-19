#!/usr/bin/env python3
"""
End-to-end ingest test: Confluence calendar rows + pinned Google Drive files
-> S3 -> ingest state machine -> OpenSearch, compared against a golden file.

Runs against the stack named by the values in config.yaml (bucket, state
machine, table, OpenSearch endpoint and index), using your current AWS
credentials, and makes real calls: Drive downloads, S3 uploads, the deployed
state machine (Transcribe, Textract, Bedrock) and OpenSearch.

Run from the repo root (needs config.yaml there, and names.env for Drive):

    # 1. With the original (main) code deployed, record the expected output
    python tests/e2e/confluence_e2e_test.py --record-golden

    # 2. Deploy the code under test, then compare against the golden
    python tests/e2e/confluence_e2e_test.py

    # Remove leftovers from an interrupted run
    python tests/e2e/confluence_e2e_test.py --cleanup

Options: --keep (skip cleanup), --timeout-minutes N, --env-file PATH.

Inputs, all uploaded under PREFIX so the state machine runs on just them:
  - the two calendar rows in fixtures/calendar_rows.html, uploaded as event
    description .txt files by the real confluence_event_descriptions_to_s3
    (the page fetch is served from the fixture: the live page is behind a
    WAF challenge)
  - three files from those events' Drive folders (video, chat txt, pdf),
    uploaded by the real GoogleDriveProcessor with the same arguments its
    folder walk passes

No audio input: in practice the Drive collector doesn't collect audio. It
skips .m4a files Drive labels audio/x-m4a (not in its blob_types), and skips
audio/mp4 and audio/mpeg files when an .mp4 with the same name is in the
folder. The pipeline itself still routes .mp3/.wav/.flac/.m4a to the audio
processor if such a file reaches the bucket some other way.

Every run writes its snapshot to tests/e2e/last_run.json. Exit codes:
0 = passed (or golden recorded), 1 = output differs from the golden,
2 = the run itself failed. Plain `pytest` does not collect this file.
"""

import argparse
import datetime as dt
import json
import logging
import os
import sys
import tempfile
import time
import types
from collections import Counter
from unittest import mock

import boto3
import yaml
from dotenv import dotenv_values

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.normpath(os.path.join(HERE, "..", ".."))
UTILS_DIR = os.path.join(REPO_ROOT, "ingest_utils")
COLLECTOR_DIR = os.path.join(UTILS_DIR, "confluence_processor")
sys.path.insert(0, COLLECTOR_DIR)

from s3_uploader import S3Uploader  # noqa: E402

CONFIG_PATH = os.path.join(REPO_ROOT, "config.yaml")
REQUIRED_CONFIG = (
    "aws_region",
    "s3_bucket_name",
    "step_function_arn",
    "processed_files_table",
    "opensearch_endpoint",
    "opensearch_index_name",
)
PREFIX = "e2e-confluence-test/"
FIXTURE = os.path.join(HERE, "fixtures", "calendar_rows.html")
GOLDEN = os.path.join(HERE, "golden", "confluence_e2e.json")
LAST_RUN = os.path.join(HERE, "last_run.json")
DEFAULT_ENV_FILE = os.path.join(COLLECTOR_DIR, "names.env")
PAGE_URL = (
    "https://spaces.at.internet2.edu/spaces/cicp/pages/289113857/"
    "Cloud+Infrastructure+Community+Program+Calendar"
)

# Both events are "(Open)" on the calendar, so their files are non-member content
RD_STUDIO_FOLDER = "16K4NcDG5FnANTY3ZUs47r3DpsDbyRxXa"  # 04/17/2024 Research and Engineering Studio and HPC on AWS
NIH_FOLDER = "13CiH9pKBxAI5zl-LrgjY-ZiV_zC5FxmX"  # 1/15/2025 NIH Genomic Data Sharing requirement
_RD = "April 24 - NET+ AWS Town Hall - Research and Engineering Studio and HPC on AWS"

# (Drive file id, folder id, file name, md5). The md5 pins the exact bytes the
# golden was recorded from; the test refuses to run if Drive has changed them.
DRIVE_FILES = [
    ("1Fph-gwvwx-CWzYsK3fPkWE9Q5BrKxBP7", RD_STUDIO_FOLDER, f"{_RD}_recording.mp4", "2d16f084253444161fb9c22f136354c2"),  # 94.6 MB
    ("1QH_O36h16YYtyRnyaeKpxqqBrruBkkxG", RD_STUDIO_FOLDER, f"{_RD}_chat.txt", "00845b7e125e41c9471dc44ba12d933e"),
    ("1qbRKOHY-7FoDTLMmxG3_X-L3AtxRaulp", NIH_FOLDER, "What is Internet2 NET+ AWS.pdf", "c0fde7607dd79e13452c99a3c0d0754e"),
]

# Passages from these input types involve no transcription or LLM, so their
# text is part of the golden. Video and audio are compared on counts and metadata.
TEXT_COMPARED_EXTENSIONS = {"txt", "vtt", "pdf"}
# Fields that legitimately change between runs. Extend if a diff shows more.
IGNORED_METADATA_KEYS = set()
IGNORED_TABLE_FIELDS = {"timestamp"}

POLL_SECONDS = 30
OS_STEADY_POLLS = 3  # OpenSearch counts must hold for this many polls in a row
OS_SETTLE_TIMEOUT = 15 * 60
OS_DELETE_TIMEOUT = 3 * 60


class E2EError(Exception):
    """The run could not complete (as opposed to a mismatch with the golden)."""


class PrefixedUploader(S3Uploader):
    """The collector's real uploader, with every key placed under PREFIX."""

    def upload_file(self, file_path, s3_object_name_relative_to_subfolder, *args, **kwargs):
        return super().upload_file(
            file_path, PREFIX + s3_object_name_relative_to_subfolder, *args, **kwargs
        )


def load_config():
    try:
        with open(CONFIG_PATH) as f:
            config = yaml.safe_load(f) or {}
    except FileNotFoundError:
        raise E2EError(f"config.yaml not found at {CONFIG_PATH}") from None
    missing = [key for key in REQUIRED_CONFIG if not config.get(key)]
    if missing:
        raise E2EError(f"config.yaml is missing {missing}; copy them from the stack outputs")
    return config


class Stack:
    def __init__(self):
        self.config = load_config()
        self.region = self.config["aws_region"]
        self.bucket = self.config["s3_bucket_name"]
        self.index = self.config["opensearch_index_name"]
        self.state_machine = self.config["step_function_arn"]
        self.s3 = boto3.client("s3", region_name=self.region)
        self.sfn = boto3.client("stepfunctions", region_name=self.region)
        self.table = boto3.resource("dynamodb", region_name=self.region).Table(
            self.config["processed_files_table"]
        )
        self.os = _opensearch_client(self.config["opensearch_endpoint"], self.region)
        self.execution_arn = None
        account = boto3.client("sts", region_name=self.region).get_caller_identity()["Account"]
        print(f"Account {account} | region {self.region}")
        print(f"State machine {self.state_machine}")
        print(f"Bucket {self.bucket} | index {self.index}\n")


def _opensearch_client(host, region):
    from opensearchpy import AWSV4SignerAuth, OpenSearch, RequestsHttpConnection

    return OpenSearch(
        hosts=[{"host": host, "port": 443}],
        http_auth=AWSV4SignerAuth(boto3.Session().get_credentials(), region, "aoss"),
        use_ssl=True,
        verify_certs=True,
        timeout=120,
        connection_class=RequestsHttpConnection,
    )


# ---------------------------------------------------------------- naming

def stem_of(key):
    base = key.rsplit("/", 1)[-1]
    return base.rsplit(".", 1)[0] if "." in base else base


def relative(key):
    return key[len(PREFIX):] if key.startswith(PREFIX) else key


def expected_input_keys(ced):
    """The S3 keys the collectors will create, known before anything is uploaded."""
    from bs4 import BeautifulSoup

    with open(FIXTURE, "rb") as f:
        soup = BeautifulSoup(f.read(), "html.parser")
    rows = [r for r in soup.find("tbody").find_all("tr") if len(r.find_all("td")) >= 5]
    descriptions = [
        f"{PREFIX}{ced.sanitize_filename(r.find_all('td')[1].get_text(strip=True))}.txt" for r in rows
    ]
    drive = [PREFIX + name.replace("/", "_").replace("\\", "_") for _, _, name, _ in DRIVE_FILES]
    return sorted(descriptions + drive)


def attribute(doc_id, inputs):
    """The input key a doc came from: the longest input stem its doc_id contains."""
    best = None
    for key in inputs:
        stem = stem_of(key)
        if stem in (doc_id or "") and (best is None or len(stem) > len(stem_of(best))):
            best = key
    return best


# ---------------------------------------------------------------- lookups

def find_s3_keys(stack, stems):
    """Every object that is a test input or was derived from one."""
    found = []
    for page in stack.s3.get_paginator("list_objects_v2").paginate(Bucket=stack.bucket):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if key.startswith(PREFIX) or any(stem in key for stem in stems):
                found.append(key)
    return sorted(found)


def find_table_rows(stack, stems):
    uris, kwargs = [], {"ProjectionExpression": "s3_uri"}
    while True:
        page = stack.table.scan(**kwargs)
        for item in page.get("Items", []):
            uri = item["s3_uri"]
            if f"/{PREFIX}" in uri or any(stem in uri for stem in stems):
                uris.append(uri)
        if "LastEvaluatedKey" not in page:
            return sorted(uris)
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def _wildcard(text):
    return "*" + text.replace("\\", "\\\\").replace("*", "\\*").replace("?", "\\?") + "*"


def find_docs(stack, stems, with_embedding=False):
    if not stack.os.indices.exists(index=stack.index):
        return []
    body = {
        "size": 10000,
        "query": {
            "bool": {
                "should": [{"wildcard": {"metadata.doc_id.keyword": {"value": _wildcard(s)}}} for s in stems],
                "minimum_should_match": 1,
            }
        },
    }
    if not with_embedding:
        body["_source"] = {"excludes": ["embedding"]}
    return stack.os.search(index=stack.index, body=body)["hits"]["hits"]


def all_doc_ids(stack):
    """{_id: doc_id} for the whole index, or None if it is too big to list."""
    if not stack.os.indices.exists(index=stack.index):
        return {}
    result = stack.os.search(
        index=stack.index,
        body={"size": 10000, "track_total_hits": True, "_source": ["metadata.doc_id"], "query": {"match_all": {}}},
    )
    if result["hits"]["total"]["value"] > 10000:
        return None
    return {h["_id"]: (h["_source"].get("metadata") or {}).get("doc_id") for h in result["hits"]["hits"]}


# ---------------------------------------------------------------- steps

def preflight(stack, stems):
    leftovers = {
        "S3 objects": find_s3_keys(stack, stems),
        "processed-table rows": find_table_rows(stack, stems),
        "OpenSearch docs": [h["_source"].get("metadata", {}).get("doc_id") for h in find_docs(stack, stems)],
    }
    found = {k: v for k, v in leftovers.items() if v}
    if found:
        lines = [f"  {k}: {len(v)}, e.g. {v[:3]}" for k, v in found.items()]
        raise E2EError(
            "the sample is already in the stack, so counts would be wrong:\n"
            + "\n".join(lines)
            + "\nRemove it with --cleanup (it lists everything before deleting)."
        )


def service_account_json(env):
    content = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    if content:
        return content
    path = os.getenv("GOOGLE_DRIVE_CREDENTIALS") or env.get("GOOGLE_DRIVE_CREDENTIALS")
    if not path:
        raise E2EError("no Drive credentials: set GOOGLE_DRIVE_CREDENTIALS in names.env (or --env-file)")
    with open(path) as f:
        return json.dumps(json.load(f))


def pinned_drive_files(env):
    """Drive metadata for the pinned files, as the folder walk would list them."""
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    creds = service_account.Credentials.from_service_account_info(
        json.loads(service_account_json(env)),
        scopes=["https://www.googleapis.com/auth/drive.readonly"],
    )
    drive = build("drive", "v3", credentials=creds)
    files = []
    for file_id, folder_id, name, md5 in DRIVE_FILES:
        f = drive.files().get(
            fileId=file_id,
            fields="id, name, mimeType, webViewLink, webContentLink, md5Checksum",
            supportsAllDrives=True,
        ).execute()
        if f["name"] != name or f.get("md5Checksum") != md5:
            raise E2EError(
                f"Drive file {file_id} changed (now {f['name']!r}, md5 {f.get('md5Checksum')}); "
                "the golden no longer applies"
            )
        del f["md5Checksum"]
        files.append((f, folder_id))
    return files


def upload_inputs(stack, ced, env, drive_files):
    import google_drive_processor as gdp

    keys = []
    with tempfile.TemporaryDirectory() as tmp:
        print("Uploading event descriptions from the calendar fixture")
        with open(FIXTURE, "rb") as f:
            page = types.SimpleNamespace(content=f.read(), raise_for_status=lambda: None)
        with mock.patch.object(ced.requests, "get", return_value=page), mock.patch.multiple(
            ced,
            confluence_urls=[PAGE_URL],
            S3_SUBFOLDER=PREFIX.rstrip("/"),
            SKIP_EXISTING=False,
            DOWNLOAD_DIR=tmp,
        ):
            keys += ced.main()["uploaded_files"]

        print("\nUploading pinned Drive files")
        uploader = PrefixedUploader(bucket_name=stack.bucket, region_name=stack.region)
        processor = gdp.GoogleDriveProcessor(service_account_json(env), uploader, tmp, skip_existing=False)
        for f, folder_id in drive_files:
            uploaded = processor._download_and_upload_file(
                f,
                False,
                source_url=f.get("webViewLink") or f.get("webContentLink"),
                parent_folder_name=processor._get_folder_name(folder_id),
                parent_folder_url=f"https://drive.google.com/drive/folders/{folder_id}",
            )
            if not uploaded:
                raise E2EError(f"Drive upload failed for {f['name']}")
            keys.append(PREFIX + f["name"].replace("/", "_").replace("\\", "_"))
    return sorted(keys)


def run_state_machine(stack, timeout_minutes):
    name = "e2e-confluence-" + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
    stack.execution_arn = stack.sfn.start_execution(
        stateMachineArn=stack.state_machine,
        name=name,
        input=json.dumps({"Bucket": stack.bucket, "Prefix": PREFIX}),
    )["executionArn"]
    print(f"\nStarted {name}")
    print(
        f"  https://{stack.region}.console.aws.amazon.com/states/home?region={stack.region}"
        f"#/v2/executions/details/{stack.execution_arn}"
    )
    started = time.time()
    while True:
        status = stack.sfn.describe_execution(executionArn=stack.execution_arn)["status"]
        if status != "RUNNING":
            print(f"  {status} after {(time.time() - started) / 60:.0f} min")
            return status
        if time.time() - started > timeout_minutes * 60:
            raise E2EError(f"state machine still running after {timeout_minutes} min")
        print(f"  RUNNING ({(time.time() - started) / 60:.0f} min)", flush=True)
        time.sleep(POLL_SECONDS)


def stop_execution_if_running(stack):
    if not stack.execution_arn:
        return
    if stack.sfn.describe_execution(executionArn=stack.execution_arn)["status"] == "RUNNING":
        print(f"Stopping {stack.execution_arn}")
        stack.sfn.stop_execution(executionArn=stack.execution_arn, cause="e2e test cleanup")


def wait_for_index(stack, inputs):
    """AOSS shows new docs with a lag; wait until every input's count is steady."""
    stems = [stem_of(k) for k in inputs]
    print("\nWaiting for OpenSearch counts to settle")
    last, steady, deadline = None, 0, time.time() + OS_SETTLE_TIMEOUT
    while time.time() < deadline:
        counts = Counter(attribute(h["_source"].get("metadata", {}).get("doc_id"), inputs) for h in find_docs(stack, stems))
        current = {relative(k): counts.get(k, 0) for k in inputs}
        steady = steady + 1 if current == last and all(current.values()) else 0
        print(f"  {sum(current.values())} docs: {current}", flush=True)
        if steady >= OS_STEADY_POLLS - 1:
            return
        last = current
        time.sleep(POLL_SECONDS)
    print("  counts did not settle; snapshotting what is there")


def snapshot(stack, inputs, status, ids_before):
    stems = [stem_of(k) for k in inputs]
    snap = {
        "execution_status": status,
        "inputs": {},
        "processed_table": {},
        "prefix_keys": [],
        "quarantined": [],
        "derived_s3_objects": {},
        "opensearch": {},
        "unattributed_new_docs": [],
    }

    for key in inputs:
        entry = {"location": "missing"}
        for location, candidate in (("input", key), ("quarantine", f"quarantine/{key}")):
            try:
                head = stack.s3.head_object(Bucket=stack.bucket, Key=candidate)
            except stack.s3.exceptions.ClientError:
                continue
            entry = {"location": location, "size": head["ContentLength"], "metadata": head.get("Metadata", {})}
            break
        snap["inputs"][relative(key)] = entry

        item = stack.table.get_item(Key={"s3_uri": f"s3://{stack.bucket}/{key}"}).get("Item")
        if item is not None:
            item = {k: v for k, v in item.items() if k not in IGNORED_TABLE_FIELDS}
            item = json.loads(json.dumps(item, default=str))
        snap["processed_table"][relative(key)] = item

    derived = Counter()
    for key in find_s3_keys(stack, stems):
        if key.startswith(PREFIX):
            snap["prefix_keys"].append(relative(key))
        elif key.startswith(f"quarantine/{PREFIX}"):
            snap["quarantined"].append(relative(key[len("quarantine/"):]))
        else:
            derived[key.split("/", 1)[0] if "/" in key else "(bucket root)"] += 1
    snap["derived_s3_objects"] = dict(sorted(derived.items()))

    groups = {key: [] for key in inputs}
    for hit in find_docs(stack, stems, with_embedding=True):
        key = attribute(hit["_source"].get("metadata", {}).get("doc_id"), inputs)
        groups[key].append(hit["_source"])
    for key, docs in groups.items():
        snap["opensearch"][relative(key)] = summarize_docs(key, docs)

    if ids_before is not None:
        ids_now = all_doc_ids(stack) or {}
        snap["unattributed_new_docs"] = sorted(
            str(doc_id) for _id, doc_id in ids_now.items()
            if _id not in ids_before and attribute(doc_id, inputs) is None
        )
    return snap


def summarize_docs(key, docs):
    metadata = {}
    for doc in docs:
        for name, value in (doc.get("metadata") or {}).items():
            if name == "doc_id" or name in IGNORED_METADATA_KEYS:
                continue
            metadata.setdefault(name, set()).add(json.dumps(value, sort_keys=True, default=str))
    summary = {
        "doc_count": len(docs),
        "doc_ids": sorted(relative(str((d.get("metadata") or {}).get("doc_id"))) for d in docs),
        "types": dict(sorted(Counter(str(d.get("type")) for d in docs).items())),
        "fields": sorted({field for d in docs for field in d}),
        "embedding_dims": sorted({len(d.get("embedding") or []) for d in docs}),
        "metadata": {name: sorted(values) for name, values in sorted(metadata.items())},
    }
    if key.rsplit(".", 1)[-1].lower() in TEXT_COMPARED_EXTENSIONS:
        summary["passages"] = sorted(str(d.get("passage")) for d in docs)
    return summary


def cleanup(stack, stems, confirm=False):
    stop_execution_if_running(stack)
    keys = find_s3_keys(stack, stems)
    uris = find_table_rows(stack, stems)
    docs = find_docs(stack, stems)
    print(f"\nCleanup: {len(keys)} S3 objects, {len(uris)} processed-table rows, {len(docs)} OpenSearch docs")
    if not (keys or uris or docs):
        return
    if confirm:
        for key in keys:
            print(f"  s3://{stack.bucket}/{key}")
        for uri in uris:
            print(f"  table row {uri}")
        for doc in docs:
            print(f"  doc {doc['_source'].get('metadata', {}).get('doc_id')}")
        if input("Delete all of the above? [y/N] ").strip().lower() != "y":
            print("Nothing deleted")
            return

    for i in range(0, len(keys), 1000):
        stack.s3.delete_objects(
            Bucket=stack.bucket, Delete={"Objects": [{"Key": k} for k in keys[i:i + 1000]], "Quiet": True}
        )
    with stack.table.batch_writer() as batch:
        for uri in uris:
            batch.delete_item(Key={"s3_uri": uri})
    from opensearchpy.exceptions import NotFoundError

    for doc in docs:
        try:
            stack.os.delete(index=stack.index, id=doc["_id"])
        except NotFoundError:
            pass

    deadline = time.time() + OS_DELETE_TIMEOUT
    while (remaining := len(find_docs(stack, stems))) and time.time() < deadline:
        time.sleep(10)
    if remaining:
        print(f"  {remaining} docs still visible after {OS_DELETE_TIMEOUT // 60} min; rerun --cleanup later")
    else:
        print("  done")


# ---------------------------------------------------------------- comparison

def _short(value):
    text = json.dumps(value, default=str)
    return text if len(text) <= 300 else text[:300] + "..."


def diff(expected, actual, path="snapshot"):
    out = []
    if isinstance(expected, dict) and isinstance(actual, dict):
        for k in sorted(set(expected) | set(actual), key=str):
            p = f"{path} > {k}"
            if k not in actual:
                out.append(f"{p}: missing (expected {_short(expected[k])})")
            elif k not in expected:
                out.append(f"{p}: unexpected {_short(actual[k])}")
            else:
                out += diff(expected[k], actual[k], p)
    elif isinstance(expected, list) and isinstance(actual, list):
        if expected != actual:
            e = Counter(json.dumps(x, sort_keys=True, default=str) for x in expected)
            a = Counter(json.dumps(x, sort_keys=True, default=str) for x in actual)
            removed = [json.loads(x) for x in (e - a).elements()]
            added = [json.loads(x) for x in (a - e).elements()]
            out.append(f"{path}: {len(expected)} -> {len(actual)} items; missing {_short(removed)}; extra {_short(added)}")
    elif expected != actual:
        out.append(f"{path}: expected {_short(expected)}, got {_short(actual)}")
    return out


def golden_problems(snap):
    problems = []
    if snap["execution_status"] != "SUCCEEDED":
        problems.append(f"execution {snap['execution_status']}")
    problems += [f"{k} produced no docs" for k, v in snap["opensearch"].items() if not v["doc_count"]]
    problems += [f"{k} was quarantined" for k in snap["quarantined"]]
    return problems


# ---------------------------------------------------------------- main

def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--record-golden", action="store_true", help="save this run's output as the expected output")
    mode.add_argument("--cleanup", action="store_true", help="only remove leftovers from an earlier run")
    parser.add_argument("--keep", action="store_true", help="leave inputs, outputs and docs in place")
    parser.add_argument("--timeout-minutes", type=int, default=180, help="state machine timeout (default 180)")
    parser.add_argument("--env-file", default=DEFAULT_ENV_FILE, help="dotenv file with GOOGLE_DRIVE_CREDENTIALS")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    try:
        stack = Stack()
        import confluence_event_descriptions_to_s3 as ced

        expected = expected_input_keys(ced)
        stems = [stem_of(k) for k in expected]
        if args.cleanup:
            cleanup(stack, stems, confirm=True)
            return 0
        if not args.record_golden and not os.path.exists(GOLDEN):
            raise E2EError(f"no golden at {GOLDEN}; record one with --record-golden while the original code is deployed")

        preflight(stack, stems)
        env = dotenv_values(args.env_file) if os.path.exists(args.env_file) else {}
        drive_files = pinned_drive_files(env)

        # os_index_creator creates the index if it's missing, as run_step_function
        # does. It reads ../config.yaml relative to the working directory.
        sys.path.insert(0, UTILS_DIR)
        cwd = os.getcwd()
        os.chdir(UTILS_DIR)
        try:
            import os_index_creator  # noqa: F401
        finally:
            os.chdir(cwd)

        ids_before = all_doc_ids(stack)
        try:
            inputs = upload_inputs(stack, ced, env, drive_files)
            if inputs != expected:
                raise E2EError(f"uploaded {[relative(k) for k in inputs]}, expected {[relative(k) for k in expected]}")
            status = run_state_machine(stack, args.timeout_minutes)
            wait_for_index(stack, inputs)
            snap = snapshot(stack, inputs, status, ids_before)
        finally:
            if args.keep:
                stop_execution_if_running(stack)
                print("\n--keep: left everything in place; remove it later with --cleanup")
            else:
                cleanup(stack, stems)

        with open(LAST_RUN, "w") as f:
            json.dump(snap, f, indent=2, sort_keys=True)
        print(f"\nSnapshot written to {LAST_RUN}")

        if args.record_golden:
            problems = golden_problems(snap)
            if problems:
                raise E2EError("not recording a golden from a broken run: " + "; ".join(problems))
            os.makedirs(os.path.dirname(GOLDEN), exist_ok=True)
            with open(GOLDEN, "w") as f:
                json.dump(snap, f, indent=2, sort_keys=True)
            print(f"Golden recorded at {GOLDEN}")
            return 0

        with open(GOLDEN) as f:
            differences = diff(json.load(f), snap)
        if differences:
            print(f"\nFAIL: {len(differences)} differences from the golden")
            for line in differences:
                print(f"  {line}")
            return 1
        print("\nPASS: output matches the golden")
        return 0
    except E2EError as e:
        print(f"\nE2E ERROR: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
