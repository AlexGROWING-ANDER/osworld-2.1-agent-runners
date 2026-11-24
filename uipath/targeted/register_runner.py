"""Register a Docker runner for this dedicated GitLab guest."""
import json
import os
from pathlib import Path
import ssl
import subprocess
import urllib.parse
import urllib.request


BASE = "https://WINDOWS_HOST:18182"
TOKEN_FILE = Path("gitlab-admin.token")
RUNNER_CONFIG = Path("runner/config.toml")
CA = ssl.create_default_context(cafile="tls/ca.crt")


def api(method, endpoint, data=None):
    headers = {"PRIVATE-TOKEN": TOKEN_FILE.read_text().strip()}
    payload = urllib.parse.urlencode(data).encode() if data is not None else None
    req = urllib.request.Request(BASE + "/api/v4/" + endpoint, payload, headers, method=method)
    with urllib.request.urlopen(req, timeout=30, context=CA) as response:
        return json.load(response)


def main():
    if not api("GET", "user").get("is_admin"):
        raise RuntimeError("GitLab account is not an administrator")
    if not RUNNER_CONFIG.exists() or "[[runners]]" not in RUNNER_CONFIG.read_text():
        runner = api("POST", "user/runners", {
            "runner_type": "instance_type",
            "description": "osworld-local-docker",
            "run_untagged": "true",
            "locked": "false",
            "paused": "false",
        })
        # The runner reads its authentication token from CI_SERVER_TOKEN.
        subprocess.run([
            "docker-compose", "run", "--rm", "-T", "--no-deps",
            "-e", "CI_SERVER_TOKEN", "--entrypoint", "gitlab-runner", "runner",
            "register", "--non-interactive", "--url", BASE,
            "--tls-ca-file", "/etc/gitlab-runner/certs/ca.crt",
            "--executor", "docker", "--docker-image", "alpine:3.20",
            "--docker-volumes", "/opt/osworld-gitlab/tls/ca.crt:/etc/gitlab-runner/certs/ca.crt:ro",
        ], stdin=subprocess.DEVNULL, env={**os.environ, "CI_SERVER_TOKEN": runner["token"]},
            text=True, check=True, capture_output=True)
        Path("runner/instance-id.txt").write_text(str(runner["id"]) + "\n")


if __name__ == "__main__":
    main()
