"""
Fakes for the collector's external dependencies, used by
test_collector_golden.py. Everything here stands in for a third-party library
or binary, never for collector code, so the tests survive a refactor.
"""
import json
import stat
import sys
from pathlib import Path

import httplib2
from googleapiclient.errors import HttpError

FIXTURES = Path(__file__).parent / "fixtures"


def media_bytes(file_id: str) -> bytes:
    return f"drive-media:{file_id}\n".encode()


def export_bytes(file_id: str, mime_type: str) -> bytes:
    return f"drive-export:{file_id}:{mime_type}\n".encode()


def http_error(status: int, message: str) -> HttpError:
    content = json.dumps({"error": {"code": status, "message": message}}).encode()
    return HttpError(httplib2.Response({"status": status}), content)


def load_drive_tree() -> dict:
    return json.loads((FIXTURES / "drive_tree.json").read_text())


class _Request:
    def __init__(self, fn):
        self._fn = fn

    def execute(self):
        return self._fn()


class FakeDrive:
    """
    Serves fixtures/drive_tree.json through the subset of the Drive v3 client
    the collector calls: files().list/get/get_media/export and
    permissions().list. Folders not in the tree list as empty.

    Failure modes, all keyed by id in the tree:
      folders[id].name_error / list_error  HTTP status for files.get / files.list
      permissions[id] = {"error": status}  permissions.list fails
      download_errors[id]                  files.get_media fails
      export_too_large                     files.export fails with exportSizeLimitExceeded
    """

    def __init__(self, tree: dict):
        self.tree = tree
        self.calls = []

    def files(self):
        return _Files(self)

    def permissions(self):
        return _Permissions(self)


class _Files:
    def __init__(self, drive: FakeDrive):
        self.d = drive

    def list(self, q, **kwargs):
        folder_id = q.split("'")[1]
        self.d.calls.append(("files.list", folder_id))
        folder = self.d.tree["folders"].get(folder_id, {})

        def run():
            if "list_error" in folder:
                raise http_error(folder["list_error"], "Internal error listing folder")
            return {"files": [dict(f) for f in folder.get("files", [])]}

        return _Request(run)

    def get(self, fileId, fields=None, **kwargs):
        self.d.calls.append(("files.get", fileId))
        folder = self.d.tree["folders"].get(fileId)

        def run():
            if folder and "name_error" in folder:
                raise http_error(folder["name_error"], "File not found")
            return {"name": folder["name"] if folder else f"unlisted-folder-{fileId}"}

        return _Request(run)

    def get_media(self, fileId, **kwargs):
        self.d.calls.append(("files.get_media", fileId))
        status = self.d.tree.get("download_errors", {}).get(fileId)

        def run():
            if status:
                raise http_error(status, "Backend error")
            return media_bytes(fileId)

        return _Request(run)

    def export(self, fileId, mimeType, **kwargs):
        self.d.calls.append(("files.export", fileId))

        def run():
            if fileId in self.d.tree.get("export_too_large", []):
                raise http_error(403, "This file is too large to be exported. exportSizeLimitExceeded")
            return export_bytes(fileId, mimeType)

        return _Request(run)


class _Permissions:
    def __init__(self, drive: FakeDrive):
        self.d = drive

    def list(self, fileId, **kwargs):
        self.d.calls.append(("permissions.list", fileId))
        perms = self.d.tree["permissions"].get(fileId, [])

        def run():
            if isinstance(perms, dict) and "error" in perms:
                raise http_error(perms["error"], "The caller does not have permission")
            return {"permissions": perms}

        return _Request(run)


class FakeMediaInfo:
    """Stands in for pymediainfo.MediaInfo.parse; reads the file id out of
    the fake media bytes and returns the duration from the tree, if any."""

    def __init__(self, durations_ms: dict):
        self.durations_ms = durations_ms

    def parse(self, file_path, *args, **kwargs):
        content = Path(file_path).read_bytes()
        duration = None
        if content.startswith(b"drive-media:"):
            file_id = content.decode().strip().split(":", 1)[1]
            duration = self.durations_ms.get(file_id)
        track = type("Track", (), {"track_type": "Video", "duration": duration})()
        return type("Info", (), {"tracks": [track]})()


_SOFFICE = '''#!{python}
# Fake LibreOffice: "converts" by copying the committed sample PDF, and fails
# for inputs exported from the file ids listed in the tree's convert_fails.
import os, sys
args = sys.argv[1:]
outdir = args[args.index("--outdir") + 1]
src = args[-1]
with open(src, "rb") as f:
    content = f.read()
if any(f"drive-export:{{i}}:".encode() in content for i in {fail_ids!r}):
    sys.stderr.write("Error: source file could not be loaded\\n")
    sys.exit(1)
out = os.path.join(outdir, os.path.splitext(os.path.basename(src))[0] + ".pdf")
with open({sample!r}, "rb") as f:
    data = f.read()
with open(out, "wb") as f:
    f.write(data + ("% converted-from: " + os.path.basename(src) + "\\n").encode())
'''

_FFMPEG = '''#!{python}
# Fake ffmpeg: writes a marker recording the slice it was asked for.
import os, sys
args = sys.argv[1:]
src = args[args.index("-i") + 1]
ss = args[args.index("-ss") + 1]
t = args[args.index("-t") + 1]
with open(args[-1], "wb") as f:
    f.write(f"ffmpeg-chunk:{{os.path.basename(src)}}:ss={{ss}}:t={{t}}\\n".encode())
'''


def install_fake_binaries(bin_dir: Path, convert_fail_ids=()) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    scripts = {
        "soffice": _SOFFICE.format(
            python=sys.executable,
            sample=str(FIXTURES / "sample.pdf"),
            fail_ids=list(convert_fail_ids),
        ),
        "ffmpeg": _FFMPEG.format(python=sys.executable),
    }
    for name, body in scripts.items():
        path = bin_dir / name
        path.write_text(body)
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
