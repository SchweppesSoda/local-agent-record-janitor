"""Install a hash-pinned test dependency into an explicitly supplied empty root."""
import argparse
import hashlib
import json
from pathlib import Path
import tempfile
import urllib.request
import zipfile


def install(destination):
    from local_agent_record_janitor.paseo_indexeddb import CODE, runtime
    manifest = json.loads((CODE / "paseo_electron.json").read_text(encoding="utf-8"))
    destination = Path(destination).absolute()
    if destination.exists():
        runtime(destination)
        return
    url = "https://github.com/electron/electron/releases/download/v44.2.0/electron-v44.2.0-win32-x64.zip"
    with tempfile.TemporaryDirectory(prefix="larj-electron-download-") as raw:
        archive = Path(raw) / "electron.zip"
        with urllib.request.urlopen(url, timeout=60) as response, archive.open("xb") as stream:
            while block := response.read(1024 * 1024):
                stream.write(block)
                if stream.tell() > 512 * 1024 * 1024:
                    raise ValueError("Electron archive exceeds dependency budget")
        if hashlib.sha256(archive.read_bytes()).hexdigest() != manifest["archive_sha256"]:
            raise ValueError("Electron dependency archive hash mismatch")
        with zipfile.ZipFile(archive) as bundle:
            expected = {item["path"]: item for item in manifest["files"]}
            members = [item for item in bundle.infolist() if not item.is_dir()]
            if len(members) != len(expected) or {item.filename for item in members} != expected.keys():
                raise ValueError("Electron dependency archive layout mismatch")
            destination.mkdir(parents=True)
            for item in members:
                body = bundle.read(item)
                proof = expected[item.filename]
                if len(body) != proof["size"] or hashlib.sha256(body).hexdigest() != proof["sha256"]:
                    raise ValueError("Electron dependency member hash mismatch")
                target = destination / item.filename
                target.relative_to(destination)
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("xb") as stream:
                    stream.write(body)
        runtime(destination)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    install(parser.parse_args().destination)
