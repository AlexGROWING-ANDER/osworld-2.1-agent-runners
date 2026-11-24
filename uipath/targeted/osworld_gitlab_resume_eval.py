"""Restore the dedicated GitLab port forwarding and resume evaluation preflight."""

import argparse
from ipaddress import IPv4Address
import json
from pathlib import Path
import socket
import subprocess
import sys
import time

import requests


LOCAL = Path(r"H:\GUI_AGENT_OSWorld\gitlab_deploy\local_vm")
sys.path.insert(0, str(LOCAL))
from preflight_local import ssh_args


def wait_for_ssh() -> None:
    end = time.monotonic() + 180
    while time.monotonic() < end:
        result = subprocess.run(ssh_args() + ["true"], capture_output=True, timeout=20)
        if result.returncode == 0:
            return
        time.sleep(5)
    raise TimeoutError("The dedicated GitLab guest did not answer SSH")


def restore_forwarding(gitlab: str, pages: str) -> None:
    routes = ((gitlab, 18182), (pages, 18183), (gitlab, 18723))
    ssh = ssh_args()
    args = [*ssh[:-1], "-o", "ExitOnForwardFailure=yes", "-N"]
    for addr, port in routes:
        args.extend(("-L", f"{addr}:{port}:127.0.0.1:{port}"))
    args.append(ssh[-1])

    with socket.create_connection(("127.0.0.1", 18724), timeout=8) as sock, sock.makefile("rwb") as channel:
        if "QMP" not in json.loads(channel.readline()):
            raise RuntimeError("Expected the GitLab VM's QEMU monitor")

        def qmp(execute: str, **arguments):
            channel.write(json.dumps({"execute": execute, "arguments": arguments}).encode() + b"\n")
            channel.flush()
            while True:
                reply = json.loads(channel.readline())
                if "error" in reply:
                    raise RuntimeError(reply["error"])
                if "return" in reply:
                    return reply["return"]

        qmp("qmp_capabilities")
        original = qmp("human-monitor-command", **{"command-line": "info usernet"})
        if "(nic)" not in original or not any(
            "HOST_FORWARD" in line and "18722" in line.split()
            for line in original.splitlines()
        ):
            raise RuntimeError("Expected the dedicated GitLab SSH forwarding rule")
        existing = [(addr, port) for addr, port in routes if any(
            "HOST_FORWARD" in line and addr in line.split() and str(port) in line.split()
            for line in original.splitlines()
        )]
        changed = []
        proc = None
        try:
            for addr, port in existing:
                reply = qmp("human-monitor-command", **{"command-line": f"hostfwd_remove nic tcp:{addr}:{port}"})
                if "removed" not in reply:
                    raise RuntimeError(reply.strip())
                changed.append((addr, port))
            with (LOCAL / "gitlab_ssh_forward.err.log").open("ab") as errors:
                proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                        stderr=errors, creationflags=subprocess.CREATE_NO_WINDOW)
            time.sleep(2)
            if proc.poll() is not None:
                raise RuntimeError("SSH forwarding exited before binding the GitLab addresses")
            (LOCAL / "gitlab_ssh_forward.json").write_text(json.dumps({"pid": proc.pid, "gitlab": gitlab,
                                                                       "pages": pages}), encoding="utf-8")
            print(f"GitLab SSH forwarding restored, PID {proc.pid}", flush=True)
        except BaseException:
            if proc is not None and proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=8)
            for addr, port in changed:
                qmp("human-monitor-command", **{"command-line": f"hostfwd_add nic tcp:{addr}:{port}-:{port}"})
            raise


def wait_for_pages(gitlab: str, pages: str) -> None:
    smoke = json.loads((LOCAL / "pages_smoke.json").read_bytes())
    if not smoke["url"].startswith(f"https://{pages}:18183/"):
        raise RuntimeError("Unexpected saved Pages URL")
    with requests.Session() as session:
        session.trust_env = False
        session.verify = str(LOCAL / "requests-ca-bundle.pem")
        end = time.monotonic() + 300
        while time.monotonic() < end:
            try:
                login = session.get(f"https://{gitlab}:18182/users/sign_in", allow_redirects=False, timeout=10)
                page = session.get(smoke["url"], allow_redirects=False, timeout=10)
                if login.status_code == 200 and page.ok and smoke["marker"] in page.text:
                    print("GitLab login and the existing published Pages content are reachable again", flush=True)
                    return
            except requests.RequestException:
                pass
            time.sleep(8)
    raise TimeoutError("GitLab and the published smoke page were not both reachable after restart")


def wait_for_host_memory() -> None:
    from uipath_gitlab import CAMPAIGN

    sys.path.insert(0, str(CAMPAIGN))
    from run_reliability import memory_snapshot

    announced = False
    while True:
        snapshot = memory_snapshot()
        if snapshot["free_physical_mb"] >= 1200 and snapshot["free_commit_mb"] >= 4096 + 512:
            return
        if not announced:
            print("Waiting for memory before importing the official 4 GiB evaluation worker", flush=True)
            announced = True
        time.sleep(20)


def start_gitlab_when_ready(gitlab: str, pages: str) -> None:
    from uipath_gitlab import CAMPAIGN

    sys.path.insert(0, str(CAMPAIGN))
    from run_reliability import memory_snapshot

    announced = False
    while True:
        with socket.socket() as probe:
            probe.settimeout(1)
            if probe.connect_ex(("127.0.0.1", 18724)) == 0:
                return
        snapshot = memory_snapshot()
        if snapshot["free_physical_mb"] >= 900 and snapshot["free_commit_mb"] >= 2048 + 2600:
            result = subprocess.run([
                sys.executable, "-B", "-u", "-X", "utf8", str(LOCAL / "prepare_local.py"),
                "--host-only-ip", gitlab, "--pages-ip", pages, "--memory-mib", "2048", "--start",
            ], cwd=LOCAL, capture_output=True, text=True, encoding="utf-8", errors="replace")
            if result.returncode == 0:
                print(result.stdout.strip(), flush=True)
                return
            if "Host memory insufficient for GitLab VM" not in result.stderr:
                raise RuntimeError(result.stderr.strip() or "GitLab VM failed to start")
        if not announced:
            print("Waiting for host memory to restore the 2 GiB GitLab VM", flush=True)
            announced = True
        time.sleep(10)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway-config", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--existing-forwarding", action="store_true")
    mode.add_argument("--start-if-needed", action="store_true")
    parser.add_argument("--preflight-only", action="store_true")
    args = parser.parse_args()
    config = args.gateway_config.resolve(strict=True)
    gitlab, pages = (str(IPv4Address(ip)) for ip in (LOCAL / "host-only-ip.txt").read_text().split())
    if args.start_if_needed:
        start_gitlab_when_ready(gitlab, pages)
    wait_for_ssh()
    if not args.existing_forwarding:
        restore_forwarding(gitlab, pages)
    wait_for_pages(gitlab, pages)
    wait_for_host_memory()
    entry = [sys.executable, "-B", "-u", "-X", "utf8", str(LOCAL / "uipath_gitlab.py"),
             "--gateway-config", str(config)]
    subprocess.run([*entry, "preflight"], cwd=LOCAL, check=True)
    if not args.preflight_only:
        subprocess.run([*entry, "run"], cwd=LOCAL, check=True)


if __name__ == "__main__":
    main()
