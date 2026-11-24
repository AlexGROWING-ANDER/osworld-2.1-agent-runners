"""Complete the pinned upstream clone using individually verified Git blobs."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import hashlib
import os
import subprocess
from urllib.parse import quote

import requests


REPO = Path(__file__).resolve().parent / "osworld041_metadata_clone"
COMMIT = "4a1b993e30a4d4f8221e356fa307bcd494355cf8"
PROXY = "http://127.0.0.1:18081"


def git(*args, data=None):
    return subprocess.run(
        ["git", "-C", str(REPO), *args], input=data, stdout=subprocess.PIPE, check=True,
        env={**os.environ, "GIT_NO_LAZY_FETCH": "1"},
    ).stdout.strip()


def download(entry):
    oid, (commit, name) = entry
    url = f"https://raw.githubusercontent.com/ThisisXXZ/Paper-Site-Template/{commit}/{quote(name)}"
    with requests.Session() as session:
        session.trust_env = False
        response = session.get(url, proxies={"https": PROXY}, timeout=(12, 45))
        response.raise_for_status()
    data = response.content
    digest = hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest().encode()
    if digest != oid:
        raise ValueError(f"Git blob mismatch: {name}")
    if git("hash-object", "-w", "--stdin", data=data) != oid:
        raise ValueError(f"Git did not store the expected object: {name}")
    print(f"Verified {name} ({len(data)} bytes)", flush=True)


if __name__ == "__main__":
    if git("rev-parse", "HEAD").decode() != COMMIT:
        raise SystemExit("Unexpected upstream commit")
    missing = {line[1:] for line in git("rev-list", "--objects", "--missing=print", COMMIT).splitlines()
               if line.startswith(b"?")}
    entries = {}
    for commit in git("rev-list", COMMIT).decode().splitlines():
        for entry in git("ls-tree", "-rz", commit).rstrip(b"\0").split(b"\0"):
            metadata, path = entry.split(b"\t", 1)
            _, kind, oid = metadata.split()
            if kind == b"blob" and oid in missing:
                entries.setdefault(oid, (commit, path.decode()))
    if entries.keys() != missing:
        raise SystemExit("The upstream history has missing objects other than blobs")
    print(f"Fetching {len(entries)} missing blobs across the original history", flush=True)
    with ThreadPoolExecutor(max_workers=5) as workers:
        list(workers.map(download, entries.items()))
    if any(line.startswith(b"?") for line in git("rev-list", "--objects", "--missing=print", COMMIT).splitlines()):
        raise SystemExit("The upstream history still has missing objects")
    git("fsck", "--full")
    git("checkout", "--detach", COMMIT)
    print(f"Verified checkout of {COMMIT}", flush=True)
