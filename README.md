# OSWorld 2.1 Windows evaluation adapters

This repository records the Windows evaluation adapters for Agent-S2 and UiPath Screen Agent. Both GPT campaigns use `gpt-6.1-sol`, medium reasoning, a 150-step limit, and a 4096 MiB QEMU guest. The model and step settings in the historical Agent-S2 worker are overridden by its campaign launcher.

| Directory | Contents |
| --- | --- |
| `agents/` | Agent-S2 launchers, desktop worker, queue maintenance, and status window |
| `uipath/` | UiPath R3.1 worker, strict Planner schema, retry logic, targeted GitLab cases, and status window |
| `shared/` | Request transport, QEMU support, and reliability helpers used by the Agent-S2 desktop worker |
| `upstream/` | Changed files from the locally used OSWorld checkout, organized by upstream path |
| `source_manifest.json` | SHA-256 for the source files copied into this snapshot |

## Runtime setup

The archived entry points retain their original path defaults. Configure the following paths for the local machine or stage the files under the paths expected by the launchers.

| Source directory | Runtime location |
| --- | --- |
| `agents/launchers/` and `uipath/targeted/` | Shared `gitlab_deploy/local_vm` launch directory. `agent_s2_sol54.py` imports `uipath_gitlab.py` from this directory. |
| `agents/runtime/` | Agent-S2 launcher's `SOURCE` directory. Keep `attempt_integrity.py` next to `desktop_worker.py` so the resume logic can validate existing results. |
| `agents/framework/` | Agent-S2 `desktop_worker.py`'s `S2` directory. |
| `shared/agent_s2_runtime/` | Agent-S2 `desktop_worker.py`'s `SHARED` directory. |
| `uipath/runtime/` | Agent-S2 launcher's `UIPATH` directory and the targeted UiPath adapter's `CAMPAIGN` directory. Keep its `desktop_worker.py` separate from the Agent-S2 worker. |
| `upstream/agent_s2/` and `upstream/uipath/` | Apply each overlay to its corresponding OSWorld checkout at the pinned commit. |

Use OSWorld commit `3d778a3c9a34a079316f70df023b166700445792`, the official OSWorld 2.1 task files and matching guest image, a Windows QEMU setup, and a local gateway configuration. The desktop worker imports the Agent-S2 framework from the checkout at `1f1506260443340276342580fd8f0b5c20f2817f`. Its desktop framework modules and license are included in `agents/framework/`.

UiPath R3.1 verifies its historical `campaign.json` file inventory before running. Restore the files to their original relative paths when using that inventory to validate a runtime installation. The release image, benchmark assets, setup credentials, active queues, and evaluation trajectories are managed separately from the source repository.

Cases 026 and 041 use a local GitLab instance. The runtime requires a machine-local GitLab configuration, CA bundle, SSH keys and Pages smoke-test record. The Windows adapter installs the local CA in the guest and can use local mirrors for the original 026 attachment and 041 Git repository. Start these mirrors before selecting the mapped routes. Case 021 has a Windows date-format compatibility shim. The local GitLab runs are recorded as `formal_benchmark=false`.

## Entry points

- `agents/launchers/agent_s2_028_054.py` selects tasks 028–054 by default. `--local` selects tasks 001–027 and resumes the existing local queue. Pass `check` before `run` to validate the shard and gateway configuration. The historical `maintain_sol54.py` restarts the original 54-task runner and belongs to that campaign.
- `uipath/runtime/uipath_batch.py` is the R3.1 batch entry point. `uipath/targeted/osworld_uipath_retry_three.py` schedules the targeted reruns. Repeat `--task` to select individual cases. The targeted status window imports `agent_s2_status_window.py` from the launcher directory when deployed in the original layout.

## Verification scope

This snapshot is for source review. File hashes and Python syntax establish what was copied. A fresh-machine run also requires the local dependencies above and an environment preflight.

The review copy of the targeted retry launcher records a deferred case when its worker exits before creating an attempt folder. The rest of the deployed evaluation behavior and campaign settings are retained.
