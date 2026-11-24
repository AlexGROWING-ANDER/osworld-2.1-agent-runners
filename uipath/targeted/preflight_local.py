"""Verify the local deployment before using it for OSWorld #026 / #041."""
import argparse
import json
from datetime import date, timedelta
import secrets
from pathlib import Path
import subprocess
import time
from urllib.parse import quote

import requests


ROOT = Path(__file__).resolve().parent
KEYS = ROOT / "local_keys"


def ssh_args():
    return [
        "ssh.exe", "-p", "18722", "-i", str(KEYS / "id_ed25519"),
        "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
        "-o", "UserKnownHostsFile=" + str(KEYS / "known_hosts"),
        "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=10",
        "osworld@127.0.0.1",
    ]


class LocalGitlab:
    def __init__(self, gitlab, pages, token):
        self.gitlab = gitlab
        self.pages = pages
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.verify = str(ROOT / "requests-ca-bundle.pem")
        self.session.headers["PRIVATE-TOKEN"] = token

    def api(self, method, endpoint, payload=None, token=None):
        headers = {"PRIVATE-TOKEN": token} if token else None
        response = self.session.request(method, self.gitlab + "/api/v4/" + endpoint,
                                        json=payload, headers=headers, timeout=15, allow_redirects=False)
        if not response.ok:
            raise RuntimeError(f"{method} {endpoint} failed with HTTP {response.status_code}")
        return response.json() if response.content else None

    def check_readiness(self):
        if not self.api("GET", "user").get("is_admin"):
            raise RuntimeError("GitLab administrator credentials required")
        for _ in range(5):
            runners = self.api("GET", "runners/all?type=instance_type&status=online")
            if any(r["description"] == "osworld-local-docker" for r in runners):
                break
            time.sleep(4)
        else:
            raise RuntimeError("The expected instance runner has not reported online")
        print("GitLab administrator API and instance runner verified", flush=True)

    def check_cases(self):
        slug = "osworld-check-" + secrets.token_hex(4)
        user = self.api("POST", "users", {
            "username": slug, "name": "OSWorld deployment smoke test",
            "email": slug + "@example.test", "password": secrets.token_urlsafe(24),
            "skip_confirmation": True,
        })
        user_id = user["id"]
        user_token = self.api("POST", f"users/{user_id}/impersonation_tokens", {
            "name": "osworld smoke test", "scopes": ["api"],
            "expires_at": (date.today() + timedelta(days=1)).isoformat(),
        })["token"]
        project = self.api("POST", "projects", {
            "name": "pages-check", "visibility": "public", "initialize_with_readme": True,
        }, token=user_token)
        project_endpoint = f"projects/{project['id']}"
        branch = project["default_branch"]
        marker = slug + "-pages-ready"
        print(f"Temporary GitLab project {slug}/pages-check created", flush=True)
        self.api("POST", project_endpoint + "/repository/commits", {
            "branch": branch,
            "commit_message": "Add isolated Pages deployment smoke test",
            "actions": [
                {"action": "create", "file_path": "public/index.html", "content": marker + "\n"},
                {"action": "create", "file_path": ".gitlab-ci.yml", "content":
                    "pages:\n  image: alpine:3.20\n  stage: deploy\n"
                    "  script:\n    - test -f public/index.html\n"
                    "  artifacts:\n    paths:\n      - public\n"},
            ],
        }, token=user_token)
        filename = quote("public/index.html", safe="")
        self.api("GET", project_endpoint + f"/repository/files/{filename}?ref={quote(branch, safe='')}",
                 token=user_token)
        print("#026 infrastructure: temporary account, repository and commit verified", flush=True)

        page_url = self.pages + "/" + slug + "/pages-check/"
        deadline = time.monotonic() + 540
        while time.monotonic() < deadline:
            pipelines = self.api("GET", project_endpoint + "/pipelines?per_page=1", token=user_token)
            status = pipelines[0]["status"] if pipelines else "waiting"
            if status in {"canceled", "failed"}:
                raise RuntimeError(f"Pages smoke pipeline stopped with status {status}, project {slug}/pages-check")
            response = self.session.get(page_url, headers={"PRIVATE-TOKEN": None},
                                        allow_redirects=False, timeout=15)
            if status == "success" and response.ok and marker in response.text:
                (ROOT / "pages_smoke.json").write_text(json.dumps({"url": page_url, "marker": marker}), encoding="utf-8")
                print("#041 infrastructure: Docker CI and published Pages content verified", flush=True)
                return
            time.sleep(8)
        raise RuntimeError(f"Pages smoke test timed out, project {slug}/pages-check")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify-cases", action="store_true",
                        help="Create a disposable test account and run a real Pages pipeline")
    parser.add_argument("--write-runner-config", action="store_true",
                        help="Save private runner settings only after both infrastructure checks pass")
    args = parser.parse_args()
    if args.write_runner_config and not args.verify_cases:
        parser.error("--write-runner-config requires --verify-cases")
    gitlab_ip, pages_ip = (ROOT / "host-only-ip.txt").read_text().split()
    result = subprocess.run(ssh_args() + ["sudo", "cat", "/opt/osworld-gitlab/gitlab-admin.token"],
                            capture_output=True, text=True, timeout=30, check=False)
    if result.returncode or not result.stdout.strip():
        raise RuntimeError("Could not load credentials from the local GitLab guest")
    lab = LocalGitlab(f"https://{gitlab_ip}:18182", f"https://{pages_ip}:18183", result.stdout.strip())
    lab.check_readiness()
    if args.verify_cases:
        lab.check_cases()
    if args.write_runner_config:
        settings = KEYS / "osworld21_eval_gitlab.env"
        staging = settings.with_suffix(".env.pending")
        staging.write_text(f"GITLAB_URL={lab.gitlab}\nGITLAB_PRIVATE_TOKEN={result.stdout.strip()}\n",
                           encoding="utf-8")
        staging.replace(settings)
        print("Private evaluation config written after both checks passed", flush=True)


if __name__ == "__main__":
    main()
