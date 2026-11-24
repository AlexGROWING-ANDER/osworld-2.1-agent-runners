"""Run targeted OSWorld 2.1 UiPath R3.1 attempts."""
import argparse
import base64
from datetime import datetime
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shlex
import shutil
import sys
import time


ROOT = Path(__file__).resolve().parent
BENCH = Path("H:/GUI_AGENT_OSWorld")
CAMPAIGN = Path("H:/GUI_AGENT_runs/OSWORLD21_UNIPATH_SOL_SMOKE_20261119/campaign_r31_json_retry/UiPath_OSWorld21_GPT61Sol")
RESULTS = BENCH / "uipath_gitlab_r31_targeted"
GUEST_STATUS = ROOT / "guest_preflight.json"
CA = ROOT / "local-ca.crt"
SETTINGS = ROOT / "local_keys" / "osworld21_eval_gitlab.env"
PUBLIC_EVAL_VM_PASSWORD = "osworld-public-evaluation"
GUEST_ASSET_PROXY = "http://10.0.2.2:18081"
TEMPLATE_URL = "https://github.com/ThisisXXZ/Paper-Site-Template"
TEMPLATE_COMMIT = "4a1b993e30a4d4f8221e356fa307bcd494355cf8"
TEMPLATE_MIRROR = "git://10.0.2.2:19418/osworld041.git"
ATTACHMENT_NAME = "AI-Assisted_Healthcare.zip"
ATTACHMENT_SOURCE = f"https://huggingface.co/datasets/xlangai/osworld_v2_assets/resolve/main/task_026/{ATTACHMENT_NAME}"
ATTACHMENT_MIRROR = f"https://10.0.2.2:19426/{ATTACHMENT_NAME}"
ATTACHMENT_CA = ROOT / "osworld026_attachment.crt"


def configure_worker(gateway_config):
    from dotenv import dotenv_values

    values = dotenv_values(SETTINGS, encoding="utf-8", interpolate=False)
    gitlab_ip, pages_ip = (ROOT / "host-only-ip.txt").read_text().split()
    if values.get("GITLAB_URL") != f"https://{gitlab_ip}:18182" or not values.get("GITLAB_PRIVATE_TOKEN"):
        raise RuntimeError("Verified local GitLab config is missing")
    manifest = CAMPAIGN / "campaign.json"
    expected = "094267855fb1077bae0fef15dbe2931b88feb58bd57db505ce8879c17cdb161d"
    if hashlib.sha256(manifest.read_bytes()).hexdigest() != expected:
        raise RuntimeError("R3.1 campaign fingerprint mismatch")
    for rel, digest in json.loads(manifest.read_text(encoding="utf-8"))["code_sha256"].items():
        if hashlib.sha256((CAMPAIGN / rel).read_bytes()).hexdigest() != digest:
            raise RuntimeError(f"R3.1 file fingerprint mismatch: {rel}")
    os.environ.update(
        OSWORLD21_DATA=str(BENCH), OSWORLD21_CODE=str(CAMPAIGN / "OSWorld-v2.1"),
        OSWORLD21_RESULTS=str(RESULTS), OSWORLD21_SOL_MODEL="gpt-6.1-sol",
        OSWORLD21_ENV=str(gateway_config.resolve(strict=True)),
        OSWORLD21_GATEWAY_PROFILE="openlux_existing", WEBSITE_HOST_SUFFIX="site.hku.icu",
    )
    sys.path.insert(0, str(CAMPAIGN))
    from gateway_settings import gateway_settings
    gateway_settings(gateway_config.resolve(strict=True), "openlux_existing")
    import desktop_worker as worker
    if (worker.MODEL, worker.REASONING_EFFORT, worker.STEPS, worker.GUEST_MB) != ("gpt-6.1-sol", "medium", 150, 4096):
        raise RuntimeError("Unexpected existing UiPath model or run settings")
    worker.environment()
    os.environ.update(
        GITLAB_URL=values["GITLAB_URL"],
        GITLAB_PRIVATE_TOKEN=values["GITLAB_PRIVATE_TOKEN"],
        NO_PROXY=f"localhost,127.0.0.1,{gitlab_ip},{pages_ip}",
        no_proxy=f"localhost,127.0.0.1,{gitlab_ip},{pages_ip}",
        REQUESTS_CA_BUNDLE=str(ROOT / "requests-ca-bundle.pem"),
        OSWORLD_STRICT_SETUP_TRANSPORT="1",
    )
    return worker


def install_guest_ca(controller, pages=False):
    import requests

    password = controller.client_password or PUBLIC_EVAL_VM_PASSWORD
    public_cert = base64.b64encode(CA.read_bytes()).decode()
    gitlab_ip, pages_ip = (ROOT / "host-only-ip.txt").read_text().split()
    chrome_policy = base64.b64encode(json.dumps({
        "ProxySettings": {
            "ProxyMode": "fixed_servers",
            "ProxyServer": GUEST_ASSET_PROXY,
            "ProxyBypassList": f"localhost;127.0.0.1;10.0.2.2;{gitlab_ip};{pages_ip}",
        },
    }).encode()).decode()
    curl = "curl --noproxy '*' -fsS --max-time 12"
    commands = [
        "set -e",
        "tmp=$(mktemp)",
        "trap 'rm -f \"$tmp\"' EXIT",
        f"printf '%s' {shlex.quote(public_cert)} | base64 -d > \"$tmp\"",
        f"vm_sudo() {{ printf '%s\\n' {shlex.quote(password)} | sudo -S -p '' -- \"$@\"; }}",
        'vm_sudo install -m 644 "$tmp" /usr/local/share/ca-certificates/osworld-gitlab-local.crt',
        "vm_sudo update-ca-certificates >/dev/null",
        "command -v certutil >/dev/null || { vm_sudo apt-get update -o Acquire::Retries=1 >/dev/null "
        "&& vm_sudo apt-get install -y libnss3-tools >/dev/null; }",
        'for db in "$HOME/.pki/nssdb" "$HOME/.local/share/pki/nssdb"; do '
        'mkdir -p "$db"; '
        'test -f "$db/cert9.db" || certutil -N --empty-password -d "sql:$db"; '
        'certutil -A -d "sql:$db" -n osworld-local-gitlab -t "C,," -i "$tmp"; done',
        f"printf '%s' {shlex.quote(chrome_policy)} | base64 -d > \"$tmp\"",
        'vm_sudo install -D -m 644 "$tmp" /etc/opt/chrome/policies/managed/osworld-asset-proxy.json',
        f"vm_sudo git config --system http.https://github.com/.proxy {shlex.quote(GUEST_ASSET_PROXY)}",
        'vm_sudo git config --system http.https://github.com/.version HTTP/1.1',
        'vm_sudo git config --system protocol.version 0',
        f"{curl} -o /dev/null {shlex.quote(os.environ['GITLAB_URL'] + '/users/sign_in')}",
    ]
    if ATTACHMENT_CA.is_file():
        encoded_ca = base64.b64encode(ATTACHMENT_CA.read_bytes()).decode()
        commands.extend([
            f"printf '%s' {shlex.quote(encoded_ca)} | base64 -d > \"$tmp\"",
            'vm_sudo install -m 644 "$tmp" /usr/local/share/ca-certificates/osworld-attachment.crt',
            'vm_sudo update-ca-certificates >/dev/null',
            'for db in "$HOME/.pki/nssdb" "$HOME/.local/share/pki/nssdb"; do '
            'certutil -A -d "sql:$db" -n osworld-local-attachment -t "C,," -i "$tmp"; done',
        ])
    if (ROOT / "osworld041_mirror_ready").is_file():
        mirror_key = shlex.quote(f"url.{TEMPLATE_MIRROR}.insteadOf")
        for source_url in (TEMPLATE_URL, TEMPLATE_URL + ".git"):
            origin = shlex.quote(source_url)
            commands.append(
                f"vm_sudo git config --system --fixed-value --get-all {mirror_key} {origin} >/dev/null "
                f"|| vm_sudo git config --system --add {mirror_key} {origin}"
            )
    if pages:
        page = json.loads((ROOT / "pages_smoke.json").read_text())
        commands.append(f"{curl} {shlex.quote(page['url'])} | grep -Fq {shlex.quote(page['marker'])}")
        commands.extend([
            'profile=$(mktemp -d)',
            'trap \'rm -f "$tmp"; rm -rf -- "$profile"\' EXIT',
            'chrome_check() { timeout 70 google-chrome --headless --no-first-run --timeout=45000 '
            '--user-data-dir="$profile" --dump-dom "$1" 2>"$profile/chrome.log" | grep -Fqi -- "$2"; }',
            f"chrome_check {shlex.quote(os.environ['GITLAB_URL'] + '/users/sign_in')} user_login "
            "|| { echo 'Chrome failed GitLab sign-in page validation' >&2; exit 1; }",
            f"chrome_check {shlex.quote(page['url'])} {shlex.quote(page['marker'])} "
            "|| { echo 'Chrome failed GitLab Pages validation' >&2; exit 1; }",
            'assets_ok=1',
            "chrome_check https://github.com/ThisisXXZ/Paper-Site-Template Paper-Site-Template "
            "|| { echo 'Chrome failed to load the GitHub template page' >&2; assets_ok=0; }",
            'download_profile="$profile/download-profile"; mkdir -p "$download_profile/Default" "$profile/downloads"',
            'printf \'{"download":{"default_directory":"%s","prompt_for_download":false}}\' "$profile/downloads" '
            '> "$download_profile/Default/Preferences"',
            'printf \'<a id="attachment" href="%s">Official attachment</a><script>document.getElementById("attachment").click()</script>\' '
            f'{shlex.quote(ATTACHMENT_MIRROR if ATTACHMENT_CA.is_file() else ATTACHMENT_SOURCE)} '
            '> "$profile/attachment.html"',
            'timeout 660 google-chrome --headless --no-first-run --user-data-dir="$download_profile" '
            '"file://$profile/attachment.html" >"$profile/download.log" 2>&1 & download_pid=$!',
            'zip="$profile/downloads/AI-Assisted_Healthcare.zip"; '
            'for second in $(seq 1 650); do test ! -f "$zip" || break; sleep 1; done; '
            'kill "$download_pid" 2>/dev/null || true; wait "$download_pid" 2>/dev/null || true',
            'if echo "c9d83c187b7f6d055108d9838d0f65fb64401fe0755274d190b9af816802e951  $zip" '
            '| sha256sum --check --status && unzip -tq "$zip" >/dev/null; then '
            "echo 'Chrome attachment download and SHA-256 passed'; else echo 'Task 026 Chrome attachment download failed' >&2; assets_ok=0; fi",
            'timeout 480 env GIT_TERMINAL_PROMPT=0 git clone --progress '
            f'{shlex.quote(TEMPLATE_URL + ".git")} "$profile/template" '
            f'&& test "$(git -C "$profile/template" rev-parse HEAD)" = {TEMPLATE_COMMIT} '
            '&& git -C "$profile/template" fsck --full '
            "&& echo 'GitHub template commit and full clone passed' "
            "|| { echo 'Task 041 template clone failed' >&2; assets_ok=0; }",
            'test "$assets_ok" = 1',
        ])
    # The normal setup logger prints commands, including their expanded arguments.
    with requests.Session() as session:
        session.trust_env = False
        response = session.post(controller.http_server + "/setup/execute", json={
            "command": ["bash", "-lc", "\n".join(commands)], "shell": False, "timeout": 1500,
        }, timeout=1510)
        response.raise_for_status()
        result = response.json()
    if pages and result.get("output"):
        print(str(result["output"]).replace(password, "[redacted]").strip(), flush=True)
    if str(result.get("returncode")) != "0":
        message = str(result.get("error", "")).replace(password, "[redacted]").strip()
        raise RuntimeError(f"Official guest GitLab setup failed: {message or result.get('status')}")


def wait_for_guest_memory(worker):
    from run_reliability import ResourceBlocked, check_memory, memory_snapshot

    deadline = time.monotonic() + 30 * 60
    waiting = False
    while True:
        if shutil.disk_usage(BENCH).free < 8 * 1024**3:
            if not waiting:
                print("Waiting for the existing 8 GiB UiPath disk reserve on H:", flush=True)
                waiting = True
            if time.monotonic() >= deadline:
                raise RuntimeError("The existing UiPath runner requires at least 8 GiB free on H:")
            time.sleep(15)
            continue
        try:
            check_memory(memory_snapshot(), worker.GUEST_MB, 1200)
        except ResourceBlocked:
            if time.monotonic() >= deadline:
                raise
            if not waiting:
                print("Waiting for memory for the unchanged 4 GiB UiPath guest", flush=True)
                waiting = True
            time.sleep(20)
        else:
            return


def test_guest(worker, launch):
    from desktop_env.controllers.setup import SetupController
    from native_qemu_v2 import NativeQemuProvider
    from run_reliability import RuntimeGuard

    guard = RuntimeGuard(worker.BENCH / "experiment_runtime.lock")
    provider = None
    wait_for_guest_memory(worker)
    try:
        guard.acquire()
        provider = NativeQemuProvider(ROOT / "guest_access_test" / launch)
        provider.start_emulator(provider.base, True, "Ubuntu")
        provider.get_ip_address(provider.base)
        controller = SetupController("127.0.0.1", server_port=5002, chromium_port=9223)
        install_guest_ca(controller, pages=True)
        GUEST_STATUS.write_text(json.dumps({
            "gitlab": os.environ["GITLAB_URL"], "guest_mib": worker.GUEST_MB, "checked": launch,
            "adapter_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        }), encoding="utf-8")
        print("Official VM reached GitLab and Pages, downloaded the #026 ZIP and cloned the #041 template", flush=True)
    finally:
        if provider:
            provider.stop_emulator(provider.base)
        guard.close()


def unwrap_planner_json(content, validator):
    """Remove a complete presentation wrapper without interpreting action text."""
    clean = content.lstrip().removeprefix("\ufeff").strip()
    fence = re.fullmatch(r"```(?:json)?[ \t]*\r?\n(.*?)\r?\n[ \t]*```", clean, re.I | re.S)
    body = fence.group(1).strip() if fence else clean
    if not fence and not content.lstrip().startswith("\ufeff"):
        return content
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return content
    return body if validator.is_valid(parsed) else content


def planner_reply_shape(content):
    text = content.lstrip()
    if not text:
        return "empty"
    if text.startswith("\ufeff"):
        return "bom"
    if text.startswith("```"):
        return "code_fence"
    if text.startswith("{"):
        return "object"
    if text.startswith("["):
        return "array"
    if text.startswith("<"):
        return "markup"
    return "other_prefix"


def run_cases(worker, launch, tasks):
    status = json.loads(GUEST_STATUS.read_text()) if GUEST_STATUS.is_file() else {}
    if any(task in ("026", "041") for task in tasks) and (
            status.get("gitlab") != os.environ["GITLAB_URL"] or
            status.get("adapter_sha256") != hashlib.sha256(Path(__file__).read_bytes()).hexdigest()):
        raise RuntimeError("The official VM has not passed local GitLab preflight")
    from desktop_env.desktop_env import DesktopEnv
    import run_reliability

    original = DesktopEnv._call_task_setup
    original_grounding = worker.grounded_point
    original_connect_api = worker.connect_api
    original_audit = run_reliability.StepTimeoutAudit

    class ConnectionRetryAudit(original_audit):
        def can_retry(self, category, attempt):
            if category != "api_connection":
                return super().can_retry(category, attempt)
            if attempt != 1 or self.retry_used or self.action_submitted:
                return False
            self.retry_used = True
            logging.warning("Model connection failed at step %s before action submission. Retrying once.", self.step)
            return True

    def connect_api(transport, audit):
        class PlannerTransport:
            correction_step = None

            def create(self, **params):
                planner = audit.module == "unipath_planner"
                if planner and self.correction_step == audit.step:
                    params["messages"] = [*params["messages"], {"role": "user", "content": (
                        "The previous reply could not be parsed as JSON. Return exactly one object matching "
                        "the existing schema. Start with { and end with }. Do not add Markdown fences or prose. "
                        "Escape embedded quotes, backslashes, and line breaks in strings."
                    )}]
                response = transport.create(**params)
                if planner and response.choices:
                    choice = response.choices[0]
                    content = choice.message.content
                    if isinstance(content, str):
                        shape = planner_reply_shape(content)
                        if choice.finish_reason == "stop" and not getattr(choice.message, "refusal", None):
                            normalized = unwrap_planner_json(content, worker.VALIDATOR)
                            if normalized != content:
                                choice.message.content = normalized
                                content = normalized
                                logging.info("Planner JSON wrapper removed at step %s: %s", audit.step, shape)
                        try:
                            json.loads(content)
                        except json.JSONDecodeError as exc:
                            self.correction_step = audit.step
                            logging.warning("Planner JSON rejected at step %s, column %s: %s (shape=%s, chars=%s)",
                                            audit.step, exc.colno, exc.msg, shape, len(content))
                return response

        return original_connect_api(PlannerTransport(), audit)

    def grounded_point(invoke, data_url, description):
        try:
            return original_grounding(invoke, data_url, description)
        except ValueError as exc:
            if str(exc) not in {"Invalid grounding coordinate", "Empty model content"}:
                raise
            logging.warning("Grounding response rejected before action submission: %s. Retrying once.", exc)
            return original_grounding(invoke, data_url, description)

    def setup(env, task, use_proxy):
        if task.id == "021" and sys.platform == "win32":
            task_module = sys.modules[type(task).__module__]
            original_datetime = task_module.datetime

            class WindowsTaskDatetime(original_datetime):
                def strftime(self, fmt):
                    return super().strftime(fmt.replace("%-d", str(self.day)))

            task_module.datetime = WindowsTaskDatetime
            try:
                return original(env, task, use_proxy)
            finally:
                task_module.datetime = original_datetime
        if task.id in ("026", "041"):
            install_guest_ca(env.setup_controller)
        if task.id != "026" or not ATTACHMENT_CA.is_file():
            return original(env, task, use_proxy)
        task_module = sys.modules[type(task).__module__]
        prepare_urls = task_module.prepare_stateful_website_urls

        def prepare_mirrored_urls(*, app, state, cache_dir):
            serialized = json.dumps(state, ensure_ascii=False)
            source = json.dumps(ATTACHMENT_SOURCE)
            if app != "teamchat" or serialized.count(source) != 1:
                raise RuntimeError("Unexpected #026 attachment fixture")
            mirrored = json.loads(serialized.replace(source, json.dumps(ATTACHMENT_MIRROR)))
            return prepare_urls(app=app, state=mirrored, cache_dir=cache_dir)

        task_module.prepare_stateful_website_urls = prepare_mirrored_urls
        try:
            return original(env, task, use_proxy)
        finally:
            task_module.prepare_stateful_website_urls = prepare_urls

    DesktopEnv._call_task_setup = setup
    worker.grounded_point = grounded_point
    worker.connect_api = connect_api
    try:
        for task in tasks:
            run_reliability.StepTimeoutAudit = ConnectionRetryAudit if task == "021" else original_audit
            wait_for_guest_memory(worker)
            out = RESULTS / task / ("attempt_" + launch)
            out.parent.mkdir(parents=True, exist_ok=True)
            state = worker.run_one("ui_path_screen_agent", task, out)
            print(f"UiPath #{task}: {state}. {out}", flush=True)
    finally:
        DesktopEnv._call_task_setup = original
        worker.grounded_point = original_grounding
        worker.connect_api = original_connect_api
        run_reliability.StepTimeoutAudit = original_audit


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("preflight", "run"))
    parser.add_argument("--gateway-config", type=Path, required=True)
    parser.add_argument("--task", choices=("021", "026", "030", "041", "048"), action="append")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    worker = configure_worker(args.gateway_config)
    launch = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    if args.action == "preflight":
        test_guest(worker, launch)
    else:
        run_cases(worker, launch, args.task or ("021", "026", "041"))


if __name__ == "__main__":
    main()
