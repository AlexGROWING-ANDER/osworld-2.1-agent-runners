"""Fetch official OSWorld 2.1 data for the portable UniPath runner."""
import argparse
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
from zipfile import ZipFile

from desktop_worker import BENCH, REPO, RELEASE, read, tasks


def get_image(artifact):
    from huggingface_hub import hf_hub_download

    destination = Path(os.environ.get("OSWORLD21_QCOW2",
                       BENCH / "v2_official_image/osworld-v2-ubuntu-x86.qcow2")).resolve()
    if destination.exists():
        with destination.open("rb") as image:
            if image.read(4) != b"QFI\xfb":
                raise ValueError(f"Existing image is not QCOW2: {destination}")
        print(f"Existing QCOW2 retained: {destination}", flush=True)
        return
    downloads = BENCH / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    archive = Path(hf_hub_download(repo_id=artifact["artifact_repository"], repo_type=artifact["repo_type"],
                   revision=artifact["artifact_revision"], filename=artifact["artifact_path"],
                   local_dir=downloads, token=False))
    with archive.open("rb") as source:
        sha256 = hashlib.file_digest(source, "sha256").hexdigest()
    if archive.stat().st_size != artifact["artifact_size"] or ("sha256:" + sha256) != artifact["artifact_sha256"]:
        raise ValueError("Official VM archive does not match the release size and SHA256")
    print("Official VM archive SHA256 verified; extracting QCOW2.", flush=True)
    temporary = destination.with_name(destination.name + ".extracting")
    with ZipFile(archive) as zipped:
        member = zipped.getinfo("osworld-v2-ubuntu-x86.qcow2")
        if shutil.disk_usage(destination.parent).free < member.file_size + 8 * 1024**3:
            raise RuntimeError("Not enough disk space to extract the VM and keep 8 GiB free")
        with zipped.open(member) as source, temporary.open("wb") as output:
            shutil.copyfileobj(source, output, 16 * 1024**2)
    temporary.replace(destination)
    print(f"Pinned QCOW2 ready: {destination}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", choices=["tasks", "assets", "image", "all"], default="all")
    args = parser.parse_args()
    BENCH.mkdir(parents=True, exist_ok=True)
    release = REPO / f"benchmark_releases/{RELEASE}.json"
    manifest = read(release)
    if args.only in {"tasks", "all"}:
        subprocess.run([sys.executable, "scripts/tools/download_osworld_v2_tasks.py",
                        "--benchmark-release", str(release), "--revision", manifest["tasks"]["commit"],
                        "--keep-stale"], cwd=REPO, check=True)
        print(f"Official task hashes verified: {len(tasks())}", flush=True)
    if args.only in {"assets", "all"}:
        subprocess.run([sys.executable, "scripts/tools/download_osworld_v2_assets.py",
                        "--benchmark-release", str(release), "--revision", manifest["assets"]["commit"],
                        "--target-dir", str(BENCH / "v2_assets")], cwd=REPO, check=True)
    if args.only in {"image", "all"}:
        get_image(manifest["provider_images"]["docker"]["ubuntu"])


if __name__ == "__main__":
    main()
