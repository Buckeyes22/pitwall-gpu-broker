"""Install wheel and sdist outside the checkout and exercise public contracts."""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path


def _run(command: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> None:
    subprocess.run(command, cwd=cwd, env=env, check=True)


def _free_loopback_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _request_json(
    url: str,
    *,
    token: str,
    payload: dict[str, object] | None = None,
) -> dict[str, object]:
    body = None if payload is None else json.dumps(payload).encode()
    headers = {"Authorization": f"Bearer {token}"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, headers=headers)
    with urllib.request.urlopen(request, timeout=3) as response:
        if response.status != 200:
            raise RuntimeError(f"artifact API smoke returned HTTP {response.status}")
        value = json.load(response)
    if not isinstance(value, dict):
        raise RuntimeError("artifact API smoke returned a non-object response")
    return value


def _smoke_api(environment: Path, root: Path, child_env: dict[str, str]) -> None:
    port = _free_loopback_port()
    token = "artifact-smoke-api-token-0001"
    api_env = child_env | {
        "PITWALL_API_HOST": "127.0.0.1",
        "PITWALL_API_PORT": str(port),
        "PITWALL_API_TOKEN": token,
        "PITWALL_ADMIN_SECRET": "artifact-smoke-admin-secret-0001",
    }
    log_path = root / "api.log"
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            [str(environment / "bin" / "pitwall-api")],
            cwd=root,
            env=api_env,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            ready: dict[str, object] | None = None
            for _ in range(60):
                if process.poll() is not None:
                    raise RuntimeError(f"installed API exited early; see {log_path}")
                try:
                    ready = _request_json(f"http://127.0.0.1:{port}/readyz", token=token)
                    break
                except (OSError, urllib.error.URLError):
                    time.sleep(0.25)
            if ready is None or ready.get("ok") is not True:
                raise RuntimeError("installed API did not become ready")
            capabilities = _request_json(f"http://127.0.0.1:{port}/v1/capabilities", token=token)
            if "items" not in capabilities:
                raise RuntimeError("installed API capability response is malformed")
            inference = _request_json(
                f"http://127.0.0.1:{port}/v1/inference",
                token=token,
                payload={"capability": "embedding.demo", "texts": ["hello"], "dry_run": True},
            )
            result = inference.get("result")
            if not isinstance(result, dict) or result.get("dry_run") is not True:
                raise RuntimeError("installed API dry-run request did not remain dry-run")
        finally:
            # SIGINT follows Uvicorn's graceful-shutdown path and therefore
            # verifies that application lifespan cleanup completes cleanly.
            process.send_signal(signal.SIGINT)
            try:
                return_code = process.wait(timeout=10)
            except subprocess.TimeoutExpired as exc:
                process.kill()
                process.wait(timeout=5)
                raise RuntimeError("installed API did not stop within 10 seconds") from exc
            if return_code != 0:
                raise RuntimeError(f"installed API shutdown returned {return_code}; see {log_path}")


def smoke(artifact: Path) -> None:
    uv = os.environ.get("UV", "uv")
    with tempfile.TemporaryDirectory(prefix="pitwall-artifact-smoke-") as temp:
        root = Path(temp)
        environment = root / "venv"
        _run([uv, "venv", str(environment)], cwd=root)
        python = environment / "bin" / "python"
        _run([uv, "pip", "install", "--python", str(python), str(artifact.resolve())], cwd=root)
        cli = environment / "bin" / "pitwall-gpu-broker"
        _run([str(cli), "--help"], cwd=root)
        _run([str(cli), "--version"], cwd=root)
        _run(
            [
                str(python),
                "-c",
                (
                    "import pitwall; from pitwall.migrations import discover_migrations; "
                    "items=discover_migrations(); assert len(items) >= 20; "
                    "assert all(item.sql for item in items); print(pitwall.__version__)"
                ),
            ],
            cwd=root,
        )
        database_url = os.environ.get("PITWALL_TEST_DATABASE_URL")
        if database_url:
            child_env = os.environ.copy()
            child_env["DATABASE_URL"] = database_url
            child_env["REDIS_URL"] = os.environ.get(
                "PITWALL_TEST_REDIS_URL", "redis://127.0.0.1:6380/0"
            )
            child_env["RUNPOD_API_KEY"] = "artifact-smoke-runpod-key"
            _run([str(cli), "db", "migrate"], cwd=root, env=child_env)
            _run([str(cli), "db", "status"], cwd=root, env=child_env)
            _run([str(cli), "config", "check"], cwd=root, env=child_env)
            _run([str(cli), "init", "--non-interactive", "--json"], cwd=root, env=child_env)
            _smoke_api(environment, root, child_env)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    artifacts = sorted(args.directory.glob("*.whl")) + sorted(args.directory.glob("*.tar.gz"))
    if len(artifacts) != 2:
        parser.error("exactly one wheel and one sdist are required")
    for artifact in artifacts:
        smoke(artifact)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
