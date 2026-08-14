from __future__ import annotations

import json
import socket
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from aiwatcher_cli import ui
from aiwatcher_cli.ui import UIHandler


ROOT = Path(__file__).resolve().parents[1]


class AdapterContractTests(unittest.TestCase):
    def test_local_broker_has_health_limit_and_no_wildcard_cors(self) -> None:
        source = (ROOT / "aiwatcher_cli" / "ui.py").read_text(encoding="utf-8")
        self.assertIn('parsed.path == "/api/health"', source)
        self.assertIn("MAX_REQUEST_BYTES = 64 * 1024", source)
        self.assertIn("length > MAX_REQUEST_BYTES", source)
        self.assertIn('content_type != "application/json"', source)
        # A bare wildcard would let any open tab read this loopback server's
        # responses (prompt risk, cost, session metadata) cross-origin, not
        # just the AIWatcher extension. Origin-scoped CORS is fine; "*" is not.
        self.assertNotIn('Access-Control-Allow-Origin", "*"', source)

    def test_local_broker_scopes_cors_to_trusted_origins_only(self) -> None:
        try:
            probe = socket.socket()
            probe.bind(("127.0.0.1", 0))
            probe.close()
        except PermissionError:
            self.skipTest("loopback sockets are unavailable in this test sandbox")

        server = ThreadingHTTPServer(("127.0.0.1", 0), UIHandler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            trusted_cases = [
                "chrome-extension://abcdefghijklmnopabcdefghijklmnop",
                "http://127.0.0.1:5173",
                "http://localhost:5173",
            ]
            with (
                patch.object(ui, "scan_all", return_value=[]),
                patch.object(ui, "scan_all_events", return_value=[]),
            ):
                for origin in trusted_cases:
                    request = urllib.request.Request(
                        f"http://127.0.0.1:{port}/api/context-health?tool=claude",
                        headers={"Origin": origin},
                    )
                    with urllib.request.urlopen(request, timeout=20) as response:
                        self.assertEqual(response.headers.get("Access-Control-Allow-Origin"), origin)

                # Attacker-registerable domains that a naive str.startswith("http://127.0.0.1")
                # or str.startswith("http://localhost") prefix check would incorrectly trust —
                # both are real, ownable DNS names, not spoofed headers.
                untrusted_cases = [
                    "https://evil.example.com",
                    "http://127.0.0.1.evil.com",
                    "http://localhost.evil.com",
                    "https://127.0.0.1",  # right host, wrong scheme
                ]
                for origin in untrusted_cases:
                    untrusted = urllib.request.Request(
                        f"http://127.0.0.1:{port}/api/context-health?tool=claude",
                        headers={"Origin": origin},
                    )
                    with urllib.request.urlopen(untrusted, timeout=20) as response:
                        self.assertIsNone(
                            response.headers.get("Access-Control-Allow-Origin"),
                            f"Origin {origin!r} must not be trusted",
                        )

                no_origin = urllib.request.Request(f"http://127.0.0.1:{port}/api/context-health?tool=claude")
                with urllib.request.urlopen(no_origin, timeout=20) as response:
                    self.assertIsNone(response.headers.get("Access-Control-Allow-Origin"))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_browser_adapter_uses_background_transport_and_dynamic_ports(self) -> None:
        manifest = json.loads((ROOT / "browser-extension" / "manifest.json").read_text(encoding="utf-8"))
        background = (ROOT / "browser-extension" / "background.js").read_text(encoding="utf-8")
        content = (ROOT / "browser-extension" / "content.js").read_text(encoding="utf-8")

        self.assertEqual(manifest["background"]["service_worker"], "background.js")
        self.assertIn("http://127.0.0.1/*", manifest["host_permissions"])
        self.assertNotIn("Access-Control-Allow-Origin", background)
        self.assertIn("PORT_START = 8765", background)
        self.assertIn("PORT_END = 8784", background)
        self.assertIn('chrome.runtime.sendMessage({', content)
        self.assertNotIn("fetch(PREFLIGHT_URL", content)

    def test_vscode_adapter_defaults_to_server_discovery(self) -> None:
        package = json.loads((ROOT / "vscode-extension" / "package.json").read_text(encoding="utf-8"))
        setting = package["contributes"]["configuration"]["properties"]["aiwatcher.serverUrl"]
        self.assertEqual(setting["default"], "auto")


if __name__ == "__main__":
    unittest.main()
