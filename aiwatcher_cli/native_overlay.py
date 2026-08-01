"""Tiny native handoff companion for AIWatcher Local.

This is intentionally dependency-free. It gives `aiwatcher watch --overlay` a
real desktop window that can float above Claude, Codex, Cursor, or a browser
without claiming to inject UI into those apps.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import webbrowser


MACOS_SWIFT_OVERLAY = r'''
import Cocoa
import Foundation

let args = CommandLine.arguments
let urlString = args.count > 1 ? args[1] : ""
let titleText = args.count > 2 ? args[2] : "AIWatcher handoff recommended"
let bodyText = args.count > 3 ? args[3] : "AIWatcher found context pressure."
let severityText = args.count > 4 ? args[4] : "warning"

func apiBase(_ value: String) -> String {
    guard let url = URL(string: value) else { return "" }
    return "\(url.scheme ?? "http")://\(url.host ?? "127.0.0.1"):\(url.port ?? 8765)"
}

func sessionId(_ value: String) -> String {
    guard let comps = URLComponents(string: value) else { return "" }
    return comps.queryItems?.first(where: { $0.name == "session" })?.value ?? ""
}

let baseURL = apiBase(urlString)
let sid = sessionId(urlString)

func postDecision(_ decision: String) {
    guard let url = URL(string: "\(baseURL)/api/handoff-decision") else { return }
    var request = URLRequest(url: url)
    request.httpMethod = "POST"
    request.setValue("application/json", forHTTPHeaderField: "Content-Type")
    let payload: [String: Any] = ["session_id": sid, "decision": decision, "reason": bodyText]
    request.httpBody = try? JSONSerialization.data(withJSONObject: payload)
    let sem = DispatchSemaphore(value: 0)
    URLSession.shared.dataTask(with: request) { _, _, _ in sem.signal() }.resume()
    _ = sem.wait(timeout: .now() + 2)
}

func fetchHandoffBrief() -> String? {
    guard let encoded = sid.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed),
          let url = URL(string: "\(baseURL)/api/handoff?id=\(encoded)&target=generic&prompt=0") else { return nil }
    let sem = DispatchSemaphore(value: 0)
    var result: String?
    URLSession.shared.dataTask(with: url) { data, _, _ in
        defer { sem.signal() }
        guard let data = data,
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return }
        result = json["next_brief"] as? String
    }.resume()
    _ = sem.wait(timeout: .now() + 4)
    return result
}

func copyToClipboard(_ value: String) {
    NSPasteboard.general.clearContents()
    NSPasteboard.general.setString(value, forType: .string)
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    var window: NSPanel!
    var statusLabel: NSTextField!

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)

        let width: CGFloat = 760
        let height: CGFloat = 230
        let screen = NSScreen.main?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1200, height: 800)
        let frame = NSRect(x: screen.maxX - width - 28, y: screen.minY + 28, width: width, height: height)
        window = NSPanel(contentRect: frame, styleMask: [.titled, .closable], backing: .buffered, defer: false)
        window.title = "AIWatcher Handoff"
        window.level = .floating
        window.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        window.hidesOnDeactivate = false
        window.isReleasedWhenClosed = false

        let view = NSView(frame: NSRect(x: 0, y: 0, width: width, height: height))
        view.wantsLayer = true
        view.layer?.backgroundColor = NSColor(calibratedRed: 0.93, green: 0.96, blue: 1.0, alpha: 1).cgColor
        window.contentView = view

        let title = NSTextField(labelWithString: titleText)
        title.frame = NSRect(x: 24, y: 164, width: 560, height: 34)
        title.font = NSFont.boldSystemFont(ofSize: 22)
        title.textColor = NSColor(calibratedRed: 0.08, green: 0.32, blue: 0.65, alpha: 1)
        view.addSubview(title)

        let badge = NSTextField(labelWithString: severityText)
        badge.frame = NSRect(x: 620, y: 166, width: 110, height: 28)
        badge.alignment = .center
        badge.font = NSFont.systemFont(ofSize: 13, weight: .semibold)
        badge.textColor = NSColor(calibratedRed: 0.30, green: 0.36, blue: 0.48, alpha: 1)
        view.addSubview(badge)

        let body = NSTextField(wrappingLabelWithString: bodyText)
        body.frame = NSRect(x: 24, y: 104, width: 700, height: 52)
        body.font = NSFont.systemFont(ofSize: 15)
        body.textColor = NSColor(calibratedRed: 0.22, green: 0.28, blue: 0.40, alpha: 1)
        view.addSubview(body)

        let buttons: [(String, Selector)] = [
            ("New chat", #selector(newChat)),
            ("Copy handoff", #selector(copyHandoff)),
            ("Continue here", #selector(continueHere)),
            ("Inspect", #selector(inspectSession))
        ]
        var x: CGFloat = 24
        for item in buttons {
            let button = NSButton(title: item.0, target: self, action: item.1)
            button.frame = NSRect(x: x, y: 54, width: item.0 == "Continue here" ? 140 : 125, height: 34)
            button.bezelStyle = .rounded
            view.addSubview(button)
            x += button.frame.width + 12
        }

        statusLabel = NSTextField(labelWithString: "Local-only. Prompt/source content is not stored in this decision.")
        statusLabel.frame = NSRect(x: 24, y: 20, width: 700, height: 22)
        statusLabel.font = NSFont.systemFont(ofSize: 13)
        statusLabel.textColor = NSColor(calibratedRed: 0.36, green: 0.43, blue: 0.55, alpha: 1)
        view.addSubview(statusLabel)

        window.makeKeyAndOrderFront(nil)
        window.orderFrontRegardless()
        NSApp.activate(ignoringOtherApps: true)
    }

    func finish(_ message: String) {
        statusLabel.stringValue = message
        DispatchQueue.main.asyncAfter(deadline: .now() + 2.5) {
            NSApp.terminate(nil)
        }
    }

    @objc func newChat() {
        postDecision("new_chat")
        if let brief = fetchHandoffBrief() {
            copyToClipboard(brief)
            finish("Handoff copied. Paste it into a fresh Claude, Codex, or Cursor chat.")
        } else {
            finish("Could not copy. Open the dashboard to copy the handoff.")
        }
    }

    @objc func copyHandoff() {
        postDecision("copy_handoff")
        if let brief = fetchHandoffBrief() {
            copyToClipboard(brief)
            finish("Handoff copied. Paste it into a fresh Claude, Codex, or Cursor chat.")
        } else {
            finish("Could not copy. Open the dashboard to copy the handoff.")
        }
    }

    @objc func continueHere() {
        postDecision("continue_here")
        finish("Decision saved: continue here.")
    }

    @objc func inspectSession() {
        let inspect = urlString.replacingOccurrences(of: "/overlay", with: "/")
        if let url = URL(string: inspect) {
            NSWorkspace.shared.open(url)
        }
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.run()
'''


def _api_base(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def _session_id(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    return urllib.parse.parse_qs(parsed.query).get("session", [""])[0]


def _request_json(url: str, payload: dict[str, object] | None = None) -> dict[str, object]:
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method="POST" if payload is not None else "GET")
    with urllib.request.urlopen(request, timeout=8) as response:
        return json.loads(response.read().decode("utf-8"))


def _record_decision(base: str, session_id: str, decision: str, reason: str) -> None:
    try:
        _request_json(
            f"{base}/api/handoff-decision",
            {
                "session_id": session_id,
                "decision": decision,
                "reason": reason,
            },
        )
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        # Decision receipts are useful, but the nudge should never fail because
        # a local POST did not work.
        return


def _set_window_position(root: object, width: int, height: int) -> None:
    screen_width = int(root.winfo_screenwidth())  # type: ignore[attr-defined]
    screen_height = int(root.winfo_screenheight())  # type: ignore[attr-defined]
    x = max(16, screen_width - width - 28)
    y = max(16, screen_height - height - 92)
    root.geometry(f"{width}x{height}+{x}+{y}")  # type: ignore[attr-defined]


def _run_macos_swift_overlay(url: str, title: str, body: str, severity: str) -> int:
    swift = shutil.which("swift")
    if sys.platform != "darwin" or not swift:
        return 2
    script_path = os.path.join(tempfile.gettempdir(), "aiwatcher-native-overlay.swift")
    try:
        with open(script_path, "w", encoding="utf-8") as handle:
            handle.write(MACOS_SWIFT_OVERLAY)
        subprocess.Popen(
            [swift, script_path, url, title, body, severity],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return 0
    except OSError as exc:
        print(f"AIWatcher macOS overlay unavailable: {exc}", file=sys.stderr)
        return 2


def run_native_overlay(url: str, title: str, body: str, severity: str) -> int:
    try:
        import tkinter as tk
        from tkinter import ttk
    except Exception as exc:  # pragma: no cover - depends on host Python build
        if sys.platform == "darwin":
            return _run_macos_swift_overlay(url, title, body, severity)
        print(f"AIWatcher native overlay unavailable: {exc}", file=sys.stderr)
        return 2

    base = _api_base(url)
    session_id = _session_id(url)
    reason = body

    root = tk.Tk()
    root.title("AIWatcher Handoff")
    root.configure(bg="#edf4ff")
    root.attributes("-topmost", True)
    try:
        root.call("::tk::unsupported::MacWindowStyle", "style", root._w, "utility", "closeBox")
    except tk.TclError:
        pass

    width = 760
    height = 245
    _set_window_position(root, width, height)

    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    style.configure("AIW.TFrame", background="#edf4ff")
    style.configure("AIW.TLabel", background="#edf4ff", foreground="#1f2a44", font=("Helvetica", 13))
    style.configure("AIWTitle.TLabel", background="#edf4ff", foreground="#1d5dab", font=("Helvetica", 20, "bold"))
    style.configure("AIWMuted.TLabel", background="#edf4ff", foreground="#4f5f78", font=("Helvetica", 12))
    style.configure("AIW.TButton", font=("Helvetica", 13, "bold"), padding=(14, 8))

    frame = ttk.Frame(root, padding=22, style="AIW.TFrame")
    frame.pack(fill="both", expand=True)

    header = ttk.Frame(frame, style="AIW.TFrame")
    header.pack(fill="x")
    ttk.Label(header, text=title or "Start a fresh AI session", style="AIWTitle.TLabel", wraplength=600).pack(
        side="left", fill="x", expand=True, anchor="w"
    )
    ttk.Label(header, text=severity or "warning", style="AIWMuted.TLabel").pack(side="right", padx=(16, 0))

    ttk.Label(frame, text=body or "AIWatcher found context pressure that may waste your next turns.", style="AIW.TLabel", wraplength=700).pack(
        fill="x", pady=(12, 18), anchor="w"
    )

    status = tk.StringVar(value="Choose an action. Prompt/source content stays local.")
    button_row = ttk.Frame(frame, style="AIW.TFrame")
    button_row.pack(fill="x", pady=(0, 12))

    def show_saved(message: str) -> None:
        status.set(message)
        root.after(2500, root.destroy)

    def copy_handoff(decision: str) -> None:
        _record_decision(base, session_id, decision, reason)
        try:
            capsule = _request_json(f"{base}/api/handoff?id={urllib.parse.quote(session_id)}&target=generic&prompt=0")
            brief = str(capsule.get("next_brief") or "")
            root.clipboard_clear()
            root.clipboard_append(brief)
            root.update()
            show_saved("Handoff copied. Paste it into a fresh Claude, Codex, or Cursor chat.")
        except (OSError, urllib.error.URLError, json.JSONDecodeError, tk.TclError):
            status.set("Could not copy. Open the dashboard to copy the handoff manually.")

    def continue_here() -> None:
        _record_decision(base, session_id, "continue_here", reason)
        show_saved("Decision saved: continue here.")

    ttk.Button(button_row, text="New chat", style="AIW.TButton", command=lambda: copy_handoff("new_chat")).pack(
        side="left", padx=(0, 10)
    )
    ttk.Button(button_row, text="Copy handoff", style="AIW.TButton", command=lambda: copy_handoff("copy_handoff")).pack(
        side="left", padx=(0, 10)
    )
    ttk.Button(button_row, text="Continue here", style="AIW.TButton", command=continue_here).pack(side="left", padx=(0, 10))
    ttk.Button(button_row, text="Inspect", style="AIW.TButton", command=lambda: webbrowser.open(url.replace("/overlay", "/"))).pack(
        side="left"
    )

    ttk.Label(frame, textvariable=status, style="AIWMuted.TLabel", wraplength=700).pack(fill="x", anchor="w")

    root.lift()
    root.mainloop()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aiwatcher-native-overlay")
    parser.add_argument("--url", required=True)
    parser.add_argument("--title", default="Start a fresh AI session")
    parser.add_argument("--body", default="")
    parser.add_argument("--severity", default="warning")
    args = parser.parse_args(argv)
    return run_native_overlay(args.url, args.title, args.body, args.severity)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
