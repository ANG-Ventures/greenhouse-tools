from __future__ import annotations

import json
import subprocess
from typing import Iterable, Mapping

REWRITE_LIST_CMD = (
    "ssh",
    "-o",
    "ConnectTimeout=10",
    "-i",
    "~/.ssh/id_ed25519",
    "hassio@192.168.1.208",
    "sudo docker exec addon_a0d7b954_adguard wget -qO- http://127.0.0.1:45158/control/rewrite/list",
)


def _run_remote(remote_argv: list[str]) -> str:
    proc = subprocess.run(remote_argv, text=True, capture_output=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"remote command failed: {proc.returncode}")
    return proc.stdout


def parse_rewrite_list(text: str) -> dict[str, tuple[str, bool]]:
    raw = json.loads(text)
    if not isinstance(raw, list):
        raise ValueError("AGH rewrite list is not a JSON list")
    live: dict[str, tuple[str, bool]] = {}
    for row in raw:
        if not isinstance(row, Mapping):
            raise ValueError("AGH rewrite row is not an object")
        domain = row.get("domain")
        answer = row.get("answer")
        if not isinstance(domain, str) or not isinstance(answer, str):
            raise ValueError("AGH rewrite row missing domain/answer")
        enabled = bool(row.get("enabled", True))
        live[domain] = (answer, enabled)
    return live


def read_live() -> dict[str, tuple[str, bool]]:
    return parse_rewrite_list(_run_remote(list(REWRITE_LIST_CMD)))
