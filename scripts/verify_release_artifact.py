#!/usr/bin/env python3
"""Exercise a built AIWatcher wheel without importing the source checkout."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import venv
import zipfile
from pathlib import Path


REQUIRED_ASSETS = {
    "aiwatcher_cli/web/index.html",
    "aiwatcher_cli/web/index.css",
    "aiwatcher_cli/web/index.js",
}


def _run(command: list[str], *, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True, check=False)
    if result.returncode:
        raise RuntimeError(
            f"Command failed ({result.returncode}): {' '.join(command)}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def _venv_paths(root: Path) -> tuple[Path, Path]:
    scripts = root / ("Scripts" if os.name == "nt" else "bin")
    python = scripts / ("python.exe" if os.name == "nt" else "python")
    aiwatcher = scripts / ("aiwatcher.exe" if os.name == "nt" else "aiwatcher")
    return python, aiwatcher


def _wheel_from(directory: Path) -> Path:
    wheels = sorted(directory.glob("*.whl"))
    if len(wheels) != 1:
        raise RuntimeError(f"Expected exactly one wheel in {directory}, found {len(wheels)}")
    return wheels[0].resolve()


def verify(wheel: Path) -> dict[str, object]:
    source_root = Path(__file__).resolve().parents[1]
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
    missing = sorted(REQUIRED_ASSETS - names)
    if missing:
        raise RuntimeError(f"Wheel is missing dashboard assets: {', '.join(missing)}")

    with tempfile.TemporaryDirectory(prefix="aiwatcher-wheel-acceptance-") as temp_value:
        root = Path(temp_value)
        environment = root / "venv"
        work = root / "work"
        state = root / "state"
        work.mkdir()
        state.mkdir()
        venv.EnvBuilder(with_pip=True, clear=True).create(environment)
        python, aiwatcher = _venv_paths(environment)
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        env.update({
            "AIWATCHER_HOME": str(state),
            "HOME": str(root / "home"),
            "USERPROFILE": str(root / "home"),
            "PYTHONNOUSERSITE": "1",
        })
        Path(env["HOME"]).mkdir()

        _run(
            [str(python), "-m", "pip", "install", "--no-deps", "--disable-pip-version-check", str(wheel)],
            cwd=work,
            env=env,
        )
        imported = _run(
            [
                str(python),
                "-I",
                "-c",
                (
                    "import json, pathlib, aiwatcher_cli; "
                    "print(json.dumps({'module': str(pathlib.Path(aiwatcher_cli.__file__).resolve()), "
                    "'version': aiwatcher_cli.__version__}))"
                ),
            ],
            cwd=work,
            env=env,
        )
        identity = json.loads(imported.stdout)
        module_path = Path(identity["module"]).resolve()
        resolved_environment = environment.resolve()
        if source_root == module_path or source_root in module_path.parents:
            raise RuntimeError(f"Acceptance imported the source checkout: {module_path}")
        if resolved_environment not in module_path.parents:
            raise RuntimeError(f"Acceptance did not import the disposable wheel environment: {module_path}")

        help_result = _run([str(aiwatcher), "--help"], cwd=work, env=env)
        if "AIWatcher Local" not in help_result.stdout or "preflight" not in help_result.stdout:
            raise RuntimeError("Installed CLI help is incomplete")
        _run(
            [str(aiwatcher), "preflight", "--text", "Add one focused unit test.", "--cwd", str(work)],
            cwd=work,
            env=env,
        )

        no_external_network = textwrap.dedent(
            """
            import ipaddress
            import socket
            from unittest.mock import patch
            from aiwatcher_cli import cli

            original = socket.socket.connect
            external = []

            def guarded(sock, address):
                if isinstance(address, tuple) and address:
                    host = str(address[0]).split('%', 1)[0]
                    try:
                        allowed = ipaddress.ip_address(host).is_loopback
                    except ValueError:
                        allowed = host.lower() == 'localhost'
                    if not allowed:
                        external.append(address)
                        raise AssertionError(f'unexpected external connection: {address}')
                return original(sock, address)

            with patch.object(socket.socket, 'connect', guarded):
                assert cli.main(['setup']) == 0
                assert cli.main(['status']) == 0
            assert not external, external
            """
        )
        _run([str(python), "-I", "-c", no_external_network], cwd=work, env=env)

        http_smoke = textwrap.dedent(
            """
            import http.client
            import json
            import threading
            from aiwatcher_cli import ui

            server = ui.ThreadingHTTPServer(('127.0.0.1', 0), ui.UIHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            port = server.server_address[1]
            try:
                client = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
                client.request('GET', '/api/health')
                response = client.getresponse()
                assert response.status == 200, response.status
                assert json.loads(response.read())['service'] == 'aiwatcher-local'
                client.close()

                client = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
                client.request('GET', '/')
                response = client.getresponse()
                body = response.read().decode('utf-8')
                assert response.status == 200, response.status
                assert 'AIWatcher Local' in body
                client.close()

                client = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
                client.request('GET', '/api/health', headers={'Host': 'review.invalid'})
                response = client.getresponse()
                assert response.status == 421, response.status
                response.read()
                client.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=10)
            """
        )
        _run([str(python), "-I", "-c", http_smoke], cwd=work, env=env)

        return {
            "wheel": wheel.name,
            "version": identity["version"],
            "module": str(module_path),
            "assets": sorted(REQUIRED_ASSETS),
            "checks": [
                "isolated wheel import",
                "CLI entry point",
                "preflight",
                "default no-external-network setup/status",
                "dashboard assets",
                "loopback HTTP service",
                "invalid Host rejection",
            ],
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dist-dir", type=Path, default=Path("dist"))
    parser.add_argument("--json-output", type=Path)
    args = parser.parse_args()
    result = verify(_wheel_from(args.dist_dir))
    encoded = json.dumps(result, indent=2, sort_keys=True)
    print(encoded)
    if args.json_output:
        args.json_output.write_text(encoded + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
