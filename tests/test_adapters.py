from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class AdapterContractTests(unittest.TestCase):
    def test_local_broker_has_health_limit_and_no_wildcard_cors(self) -> None:
        source = (ROOT / "aiwatcher_cli" / "ui.py").read_text(encoding="utf-8")
        self.assertIn('parsed.path == "/api/health"', source)
        self.assertIn("MAX_REQUEST_BYTES = 64 * 1024", source)
        self.assertIn("length > MAX_REQUEST_BYTES", source)
        self.assertIn('content_type != "application/json"', source)
        self.assertNotIn("Access-Control-Allow-Origin", source)

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
