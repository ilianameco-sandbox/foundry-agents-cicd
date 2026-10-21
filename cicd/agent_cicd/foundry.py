"""Shared plumbing: Foundry clients, polling, GitHub Actions outputs and reports."""

from __future__ import annotations

import json
import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

TERMINAL_STATUSES = ("completed", "failed", "canceled", "cancelled")

# Local Windows consoles may not be UTF-8; never let report printing break a gate.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")


@contextmanager
def project_client(endpoint: str) -> Iterator[Any]:
    from azure.ai.projects import AIProjectClient
    from azure.identity import AzureCliCredential

    # azure/login (OIDC) in GitHub Actions and `az login` locally both feed the Azure CLI credential.
    with AzureCliCredential(process_timeout=120) as credential, \
            AIProjectClient(endpoint=endpoint, credential=credential) as client:
        yield client


def to_plain(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {k: to_plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_plain(v) for v in value]
    for method in ("model_dump", "as_dict", "to_dict"):
        if hasattr(value, method):
            try:
                return to_plain(getattr(value, method)())
            except Exception:  # noqa: BLE001 - best effort serialisation for reports
                continue
    return str(value)


def wait_for_run(retrieve: Callable[[], Any], timeout_minutes: float, label: str) -> Any:
    deadline = time.monotonic() + timeout_minutes * 60
    delay = 10.0
    while True:
        run = retrieve()
        status = getattr(run, "status", None)
        print(f"[{label}] status={status}", flush=True)
        if status in TERMINAL_STATUSES:
            return run
        if time.monotonic() > deadline:
            print(f"[{label}] timed out after {timeout_minutes} minutes", flush=True)
            return run
        time.sleep(delay)
        delay = min(delay * 1.5, 60.0)


def set_outputs(**values: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    for key, value in values.items():
        print(f"{key}={value}")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            for key, value in values.items():
                handle.write(f"{key}={value}\n")


def add_summary(markdown: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    print(markdown)
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(markdown + "\n")


def write_report(path: Path | None, payload: dict[str, Any]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(to_plain(payload), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
