"""Tiny native Fresh Start companion for AIWatcher Local.

This is intentionally dependency-free. It gives `aiwatcher watch --overlay` a
real desktop window that can float above Claude, Codex, Cursor, or a browser
without claiming to inject UI into those apps.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
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


@dataclass(frozen=True)
class OverlayConfig:
    signal_kind: str
    title: str
    primary_label: str
    primary_action: str
    primary_mode: str
    guidance: str


_SIGNAL_ALIASES = {
    "context": "critical_context",
    "context_pressure": "critical_context",
    "context_critical": "critical_context",
    "critical": "critical_context",
    "runaway": "loop",
    "runaway_loop": "loop",
    "high_velocity": "velocity",
    "usage_velocity": "velocity",
    "low_runway": "runway",
    "quota": "runway",
    "insight": "generic",
}


def _normalize_signal_kind(value: str | None) -> str:
    normalized = (value or "generic").strip().lower().replace("-", "_").replace(" ", "_")
    normalized = _SIGNAL_ALIASES.get(normalized, normalized)
    return normalized if normalized in {"critical_context", "loop", "velocity", "runway", "generic"} else "generic"


def overlay_config(
    signal_kind: str | None,
    *,
    primary_label: str | None = None,
    action_endpoint_available: bool = False,
    runtime_action_available: bool = False,
) -> OverlayConfig:
    """Return truthful, signal-specific copy and behavior for the companion."""
    kind = _normalize_signal_kind(signal_kind)
    if kind == "critical_context":
        action = "copy_brief_and_open_runtime" if action_endpoint_available and runtime_action_available else "copy_fresh_brief"
        default_label = "Open tool + copy brief" if action == "copy_brief_and_open_runtime" else "Copy fresh-session brief"
        config = OverlayConfig(
            kind,
            "Context is getting expensive",
            default_label,
            action,
            "copy",
            "Start a fresh session in the same workspace without replaying the full conversation.",
        )
    elif kind == "loop":
        action = "stop_and_inspect" if action_endpoint_available else "inspect_loop"
        config = OverlayConfig(
            kind,
            "Possible loop detected",
            "Stop and inspect" if action_endpoint_available else "Inspect loop",
            action,
            "inspect",
            "Pause repeated work and inspect the evidence before allowing more tool calls.",
        )
    elif kind == "velocity":
        config = OverlayConfig(
            kind,
            "AI work is moving unusually fast",
            "Copy focused brief",
            "copy_focused_brief",
            "copy",
            "Narrow the current task to one checkpoint before more context and tool calls accumulate.",
        )
    elif kind == "runway":
        config = OverlayConfig(
            kind,
            "Usage runway is getting low",
            "Review switch options",
            "review_switch_options",
            "inspect",
            "Review the current workload before switching tools or continuing under limited capacity.",
        )
    else:
        config = OverlayConfig(
            kind,
            "AIWatcher found something to review",
            "Review insight",
            "review_insight",
            "inspect",
            "Inspect the evidence and choose whether the current work needs an intervention.",
        )
    if primary_label and primary_label.strip():
        return OverlayConfig(
            config.signal_kind,
            config.title,
            primary_label.strip(),
            config.primary_action,
            config.primary_mode,
            config.guidance,
        )
    return config


def _infer_signal_kind_from_title(signal_kind: str | None, title: str | None, body: str | None = None) -> str:
    normalized = _normalize_signal_kind(signal_kind)
    if normalized != "generic":
        return normalized
    text = f"{title or ''} {body or ''}".lower()
    if "fresh" in text or "handoff" in text or "context is getting expensive" in text:
        return "critical_context"
    if "loop" in text or "repeated" in text:
        return "loop"
    if "velocity" in text or "moving unusually fast" in text:
        return "velocity"
    if "runway" in text or "switch lane" in text or "usage pressure" in text:
        return "runway"
    return normalized


MACOS_SWIFT_OVERLAY = r'''
import Cocoa
import Foundation

let args = CommandLine.arguments
let urlString = args.count > 1 ? args[1] : ""
let titleText = args.count > 2 ? args[2] : "AIWatcher Fresh Start recommended"
let bodyText = args.count > 3 ? args[3] : "AIWatcher found context pressure."
let severityText = args.count > 4 ? args[4] : "warning"
let briefFile = args.count > 5 ? args[5] : ""
let interventionFingerprint = args.count > 6 ? args[6] : ""
let signalKind = args.count > 7 ? args[7] : "generic"
let primaryLabel = args.count > 8 ? args[8] : "Review insight"
let primaryMode = args.count > 9 ? args[9] : "inspect"
let runtimeActionAvailable = args.count > 10 ? args[10] == "1" : false

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
    let payload: [String: Any] = ["session_id": sid, "decision": decision, "reason": bodyText, "action_channel": "native_overlay"]
    request.httpBody = try? JSONSerialization.data(withJSONObject: payload)
    let sem = DispatchSemaphore(value: 0)
    URLSession.shared.dataTask(with: request) { _, _, _ in sem.signal() }.resume()
    _ = sem.wait(timeout: .now() + 2)
}

func postInterventionAction(_ action: String, snoozeMinutes: Int? = nil) {
    guard !interventionFingerprint.isEmpty,
          let url = URL(string: "\(baseURL)/api/ambient-intervention-action") else {
        if action == "snooze" {
            postDecision("continue_here")
        } else if action == "dismiss" {
            postDecision("dismissed")
        }
        return
    }
    var request = URLRequest(url: url)
    request.httpMethod = "POST"
    request.setValue("application/json", forHTTPHeaderField: "Content-Type")
    var payload: [String: Any] = [
        "fingerprint": interventionFingerprint,
        "action": action,
        "channel": "overlay"
    ]
    if let minutes = snoozeMinutes { payload["snooze_minutes"] = minutes }
    request.httpBody = try? JSONSerialization.data(withJSONObject: payload)
    let sem = DispatchSemaphore(value: 0)
    URLSession.shared.dataTask(with: request) { _, _, _ in sem.signal() }.resume()
    _ = sem.wait(timeout: .now() + 2)
}

func requestRuntimeReturn() {
    guard runtimeActionAvailable,
          let url = URL(string: "\(baseURL)/api/runtime-return") else { return }
    var request = URLRequest(url: url)
    request.httpMethod = "POST"
    request.setValue("application/json", forHTTPHeaderField: "Content-Type")
    request.httpBody = try? JSONSerialization.data(withJSONObject: ["session_id": sid])
    URLSession.shared.dataTask(with: request).resume()
}

func fetchHandoffBrief() -> String? {
    if !briefFile.isEmpty,
       let value = try? String(contentsOfFile: briefFile, encoding: .utf8),
       !value.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
        return value
    }
    guard let encoded = sid.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed),
          let url = URL(string: "\(baseURL)/api/handoff-basic?id=\(encoded)&target=generic") else { return nil }
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
    var completed = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)

        let width: CGFloat = 680
        let height: CGFloat = 168
        let screen = NSScreen.main?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1200, height: 800)
        let frame = NSRect(x: screen.maxX - width - 28, y: screen.minY + 28, width: width, height: height)
        window = NSPanel(contentRect: frame, styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        window.level = .floating
        window.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        window.hidesOnDeactivate = false
        window.isReleasedWhenClosed = false
        window.becomesKeyOnlyIfNeeded = true
        window.hasShadow = true

        let view = NSView(frame: NSRect(x: 0, y: 0, width: width, height: height))
        view.wantsLayer = true
        view.layer?.backgroundColor = NSColor(calibratedRed: 0.93, green: 0.96, blue: 1.0, alpha: 1).cgColor
        view.layer?.cornerRadius = 12
        view.layer?.borderWidth = 1
        view.layer?.borderColor = NSColor(calibratedRed: 0.65, green: 0.76, blue: 0.91, alpha: 1).cgColor
        window.contentView = view

        let title = NSTextField(labelWithString: titleText)
        title.frame = NSRect(x: 22, y: 122, width: 500, height: 26)
        title.font = NSFont.boldSystemFont(ofSize: 18)
        title.textColor = NSColor(calibratedRed: 0.08, green: 0.32, blue: 0.65, alpha: 1)
        view.addSubview(title)

        let badge = NSTextField(labelWithString: severityText)
        badge.frame = NSRect(x: 550, y: 124, width: 92, height: 22)
        badge.alignment = .center
        badge.font = NSFont.systemFont(ofSize: 13, weight: .semibold)
        badge.textColor = NSColor(calibratedRed: 0.30, green: 0.36, blue: 0.48, alpha: 1)
        view.addSubview(badge)

        let body = NSTextField(wrappingLabelWithString: bodyText)
        body.frame = NSRect(x: 22, y: 78, width: 626, height: 38)
        body.font = NSFont.systemFont(ofSize: 14)
        body.textColor = NSColor(calibratedRed: 0.22, green: 0.28, blue: 0.40, alpha: 1)
        view.addSubview(body)

        let primary = NSButton(title: primaryLabel, target: self, action: #selector(primaryAction))
        primary.frame = NSRect(x: 22, y: 38, width: 210, height: 32)
        primary.bezelStyle = .rounded
        primary.keyEquivalent = "\r"
        view.addSubview(primary)

        let continueButton = NSButton(title: "Continue here", target: self, action: #selector(continueHere))
        continueButton.frame = NSRect(x: 244, y: 38, width: 130, height: 32)
        continueButton.bezelStyle = .rounded
        view.addSubview(continueButton)

        let moreButton = NSButton(title: "...", target: self, action: #selector(showMore(_:)))
        moreButton.frame = NSRect(x: 386, y: 38, width: 52, height: 32)
        moreButton.bezelStyle = .rounded
        view.addSubview(moreButton)

        statusLabel = NSTextField(labelWithString: "Local-only. Prompt/source content is not stored in this decision.")
        statusLabel.frame = NSRect(x: 452, y: 44, width: 196, height: 18)
        statusLabel.alignment = .right
        statusLabel.font = NSFont.systemFont(ofSize: 11)
        statusLabel.textColor = NSColor(calibratedRed: 0.36, green: 0.43, blue: 0.55, alpha: 1)
        view.addSubview(statusLabel)

        window.orderFrontRegardless()
        postInterventionAction("displayed")
        DispatchQueue.main.asyncAfter(deadline: .now() + 20) {
            if !self.completed {
                postInterventionAction("snooze", snoozeMinutes: 15)
                NSApp.terminate(nil)
            }
        }
    }

    func finish(_ message: String) {
        completed = true
        statusLabel.stringValue = message
        DispatchQueue.main.asyncAfter(deadline: .now() + 2.5) {
            NSApp.terminate(nil)
        }
    }

    @objc func continueHere() {
        postDecision("continue_here")
        postInterventionAction("snooze", snoozeMinutes: 15)
        finish("Continuing. Quiet for 15 min.")
    }

    @objc func showMore(_ sender: NSButton) {
        let menu = NSMenu()
        menu.addItem(withTitle: "Inspect evidence", action: #selector(inspectSession), keyEquivalent: "")
        menu.addItem(withTitle: "Snooze 15 minutes", action: #selector(snooze), keyEquivalent: "")
        menu.addItem(withTitle: "Dismiss for this session", action: #selector(dismissSession), keyEquivalent: "")
        for item in menu.items { item.target = self }
        menu.popUp(positioning: nil, at: NSPoint(x: sender.frame.minX, y: sender.frame.maxY), in: sender.superview)
    }

    @objc func primaryAction() {
        if primaryMode == "inspect" {
            inspectSession()
            finish("Opened the evidence. Review it before continuing the run.")
            return
        }
        if let brief = fetchHandoffBrief() {
            copyToClipboard(brief)
            postInterventionAction("acted")
            if signalKind == "critical_context" {
                postDecision("copy_handoff")
            }
            requestRuntimeReturn()
            let destination = runtimeActionAvailable ? "The return target was opened." : "Return to your AI tool."
            finish("Brief copied. \(destination) Paste it to continue.")
        } else {
            finish("Could not copy. Open the dashboard to copy the Fresh Start brief.")
        }
    }

    @objc func snooze() {
        postInterventionAction("snooze", snoozeMinutes: 15)
        finish("Snoozed for 15 minutes. AIWatcher will stay quiet unless severity worsens.")
    }

    @objc func dismissSession() {
        postInterventionAction("dismiss")
        finish("Dismissed for this session state.")
    }

    @objc func inspectSession() {
        postInterventionAction("acted")
        let encoded = sid.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? ""
        let inspect = "\(baseURL)/?session=\(encoded)"
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

MACOS_SWIFT_PRESENCE = r'''
import Cocoa
import Foundation

let args = CommandLine.arguments
let dashboardURL = args.count > 1 ? args[1] : "http://127.0.0.1:8765"
let promptURL = args.count > 2 ? args[2] : dashboardURL
let position = args.count > 3 ? args[3] : "bottom-right"

func withoutTrailingSlash(_ value: String) -> String {
    var output = value
    while output.hasSuffix("/") {
        output.removeLast()
    }
    return output
}

let dashboardBaseURL = withoutTrailingSlash(dashboardURL)
let stateURL = dashboardBaseURL + "/api/companion-state"

func openURL(_ value: String) {
    guard let url = URL(string: value) else { return }
    NSWorkspace.shared.open(url)
}

func absoluteURL(_ value: String) -> String {
    if value.hasPrefix("http://") || value.hasPrefix("https://") { return value }
    return dashboardBaseURL + value
}

final class DragView: NSView {
    override var mouseDownCanMoveWindow: Bool { true }
}

final class PresenceDelegate: NSObject, NSApplicationDelegate {
    var window: NSPanel!
    var rootView: NSView!
    var titleLabel: NSTextField!
    var subtitleLabel: NSTextField!
    var primaryButton: NSButton!
    var planButton: NSButton!
    var consoleButton: NSButton!
    var collapseButton: NSButton!
    var expandButton: NSButton!
    var dragHandle: NSTextField!
    var dotLabel: NSTextField!
    var primaryURL = dashboardURL
    var collapsed = false
    var stateName = "watching"
    let expandedWidth: CGFloat = 386
    let expandedHeight: CGFloat = 58
    let collapsedWidth: CGFloat = 64
    let collapsedHeight: CGFloat = 42

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)

        let width: CGFloat = expandedWidth
        let height: CGFloat = expandedHeight
        let screen = NSScreen.main?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1200, height: 800)
        let margin: CGFloat = 24
        let x = position.contains("left") ? screen.minX + margin : screen.maxX - width - margin
        let y = position.contains("top") ? screen.maxY - height - margin : screen.minY + margin
        window = NSPanel(contentRect: NSRect(x: x, y: y, width: width, height: height),
                         styleMask: [.borderless, .nonactivatingPanel],
                         backing: .buffered,
                         defer: false)
        window.level = .floating
        window.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        window.hidesOnDeactivate = false
        window.isReleasedWhenClosed = false
        window.becomesKeyOnlyIfNeeded = true
        window.isMovableByWindowBackground = true
        window.hasShadow = true

        rootView = DragView(frame: NSRect(x: 0, y: 0, width: width, height: height))
        rootView.wantsLayer = true
        rootView.layer?.backgroundColor = NSColor(calibratedRed: 0.05, green: 0.07, blue: 0.10, alpha: 0.96).cgColor
        rootView.layer?.cornerRadius = 16
        rootView.layer?.borderWidth = 1
        rootView.layer?.borderColor = NSColor(calibratedRed: 0.25, green: 0.34, blue: 0.48, alpha: 0.90).cgColor
        window.contentView = rootView

        dragHandle = NSTextField(labelWithString: "::")
        dragHandle.frame = NSRect(x: 10, y: 21, width: 18, height: 16)
        dragHandle.font = NSFont.systemFont(ofSize: 12, weight: .bold)
        dragHandle.textColor = NSColor(calibratedRed: 0.48, green: 0.58, blue: 0.70, alpha: 1)
        dragHandle.toolTip = "Drag AIWatcher"
        rootView.addSubview(dragHandle)

        dotLabel = NSTextField(labelWithString: "on")
        dotLabel.frame = NSRect(x: 30, y: 31, width: 22, height: 16)
        dotLabel.font = NSFont.systemFont(ofSize: 10, weight: .bold)
        dotLabel.textColor = NSColor(calibratedRed: 0.26, green: 0.80, blue: 0.55, alpha: 1)
        rootView.addSubview(dotLabel)

        titleLabel = NSTextField(labelWithString: "AIWatcher")
        titleLabel.frame = NSRect(x: 56, y: 31, width: 88, height: 17)
        titleLabel.font = NSFont.systemFont(ofSize: 12, weight: .semibold)
        titleLabel.textColor = NSColor.white
        rootView.addSubview(titleLabel)

        subtitleLabel = NSTextField(labelWithString: "Watching quietly")
        subtitleLabel.frame = NSRect(x: 30, y: 12, width: 116, height: 16)
        subtitleLabel.font = NSFont.systemFont(ofSize: 9)
        subtitleLabel.textColor = NSColor(calibratedRed: 0.67, green: 0.74, blue: 0.84, alpha: 1)
        rootView.addSubview(subtitleLabel)

        planButton = NSButton(title: "Plan", target: self, action: #selector(openPrompt))
        planButton.frame = NSRect(x: 152, y: 15, width: 50, height: 28)
        planButton.bezelStyle = .rounded
        planButton.controlSize = .small
        rootView.addSubview(planButton)

        primaryButton = NSButton(title: "Watch", target: self, action: #selector(openPrimary))
        primaryButton.frame = NSRect(x: 206, y: 15, width: 88, height: 28)
        primaryButton.bezelStyle = .rounded
        primaryButton.controlSize = .small
        rootView.addSubview(primaryButton)

        consoleButton = NSButton(title: "Console", target: self, action: #selector(openDashboard))
        consoleButton.frame = NSRect(x: 298, y: 15, width: 64, height: 28)
        consoleButton.bezelStyle = .rounded
        consoleButton.controlSize = .small
        rootView.addSubview(consoleButton)

        collapseButton = NSButton(title: "-", target: self, action: #selector(toggleCollapsed))
        collapseButton.frame = NSRect(x: 364, y: 37, width: 18, height: 18)
        collapseButton.bezelStyle = .rounded
        collapseButton.controlSize = .mini
        collapseButton.toolTip = "Minimize AIWatcher"
        rootView.addSubview(collapseButton)

        expandButton = NSButton(title: "AIW", target: self, action: #selector(toggleCollapsed))
        expandButton.frame = NSRect(x: 8, y: 7, width: 48, height: 28)
        expandButton.bezelStyle = .rounded
        expandButton.controlSize = .small
        expandButton.toolTip = "Open AIWatcher Companion"
        expandButton.isHidden = true
        rootView.addSubview(expandButton)

        window.orderFrontRegardless()
        updateAppearance()
        refreshState()
    }

    @objc func openDashboard() {
        openURL(dashboardURL)
    }

    @objc func openPrompt() {
        openURL(promptURL)
    }

    @objc func openPrimary() {
        openURL(primaryURL)
    }

    @objc func toggleCollapsed() {
        collapsed.toggle()
        let current = window.frame
        let targetWidth = collapsed ? collapsedWidth : expandedWidth
        let targetHeight = collapsed ? collapsedHeight : expandedHeight
        let target = NSRect(
            x: current.minX,
            y: current.maxY - targetHeight,
            width: targetWidth,
            height: targetHeight
        )
        window.setFrame(target, display: true, animate: true)
        rootView.frame = NSRect(x: 0, y: 0, width: targetWidth, height: targetHeight)
        rootView.layer?.cornerRadius = collapsed ? 14 : 16
        for view in [dragHandle, dotLabel, titleLabel, subtitleLabel, primaryButton, planButton, consoleButton, collapseButton] {
            view?.isHidden = collapsed
        }
        expandButton.isHidden = !collapsed
        updateAppearance()
    }

    func updateAppearance() {
        let needsAttention = ["prompt_gate", "control_recommended", "proof_pending", "needs_review"].contains(stateName)
        let dark = NSColor(calibratedRed: 0.05, green: 0.07, blue: 0.10, alpha: 0.96).cgColor
        let orange = NSColor(calibratedRed: 0.88, green: 0.36, blue: 0.12, alpha: 0.96).cgColor
        rootView.layer?.backgroundColor = (collapsed && needsAttention) ? orange : dark
        rootView.layer?.borderColor = needsAttention
            ? NSColor(calibratedRed: 1.00, green: 0.63, blue: 0.24, alpha: 0.95).cgColor
            : NSColor(calibratedRed: 0.25, green: 0.34, blue: 0.48, alpha: 0.90).cgColor
    }

    func refreshState() {
        guard let url = URL(string: stateURL) else {
            scheduleRefresh()
            return
        }
        URLSession.shared.dataTask(with: url) { data, _, _ in
            guard let data = data,
                  let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
                self.scheduleRefresh()
                return
            }
            DispatchQueue.main.async {
                self.stateName = json["state"] as? String ?? "watching"
                self.titleLabel.stringValue = String((json["label"] as? String ?? "AIWatcher").prefix(18))
                self.subtitleLabel.stringValue = String((json["subtitle"] as? String ?? "Watching quietly").prefix(42))
                self.primaryButton.title = String((json["primary_label"] as? String ?? "Watch").prefix(12))
                self.primaryURL = absoluteURL(json["primary_url"] as? String ?? "/")
                self.updateAppearance()
                self.scheduleRefresh()
            }
        }.resume()
    }

    func scheduleRefresh() {
        DispatchQueue.main.asyncAfter(deadline: .now() + 8) {
            self.refreshState()
        }
    }
}

let app = NSApplication.shared
let delegate = PresenceDelegate()
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
                "action_channel": "native_overlay",
            },
        )
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        # Decision receipts are useful, but the nudge should never fail because
        # a local POST did not work.
        return


def _record_intervention_action(
    base: str,
    fingerprint: str,
    action: str,
    *,
    snooze_minutes: int | None = None,
) -> None:
    if not fingerprint:
        return
    payload: dict[str, object] = {
        "fingerprint": fingerprint,
        "action": action,
        "channel": "overlay",
    }
    if snooze_minutes is not None:
        payload["snooze_minutes"] = snooze_minutes
    try:
        _request_json(f"{base}/api/ambient-intervention-action", payload)
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        return


def _request_runtime_return(base: str, session_id: str) -> bool:
    if not session_id:
        return False
    try:
        result = _request_json(f"{base}/api/runtime-return", {"session_id": session_id})
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        return False
    return bool(result.get("ok"))


def _set_window_position(root: object, width: int, height: int) -> None:
    screen_width = int(root.winfo_screenwidth())  # type: ignore[attr-defined]
    screen_height = int(root.winfo_screenheight())  # type: ignore[attr-defined]
    x = max(16, screen_width - width - 28)
    y = max(16, screen_height - height - 92)
    root.geometry(f"{width}x{height}+{x}+{y}")  # type: ignore[attr-defined]


def _read_brief_file(brief_file: str | None) -> str | None:
    if not brief_file:
        return None
    try:
        with open(brief_file, encoding="utf-8") as handle:
            value = handle.read().strip()
    except OSError:
        return None
    return value or None


def _run_macos_swift_overlay(
    url: str,
    title: str,
    body: str,
    severity: str,
    brief_file: str | None = None,
    *,
    intervention_fingerprint: str = "",
    signal_kind: str = "generic",
    primary_label: str | None = None,
    primary_mode: str | None = None,
    runtime_action_available: bool = False,
) -> int:
    swift = shutil.which("swift")
    if sys.platform != "darwin" or not swift:
        return 2
    script_path = os.path.join(tempfile.gettempdir(), "aiwatcher-native-overlay.swift")
    try:
        with open(script_path, "w", encoding="utf-8") as handle:
            handle.write(MACOS_SWIFT_OVERLAY)
        inferred_signal_kind = _infer_signal_kind_from_title(signal_kind, title, body)
        config = overlay_config(inferred_signal_kind, primary_label=primary_label)
        subprocess.Popen(
            [
                swift,
                script_path,
                url,
                title or config.title,
                body,
                severity,
                brief_file or "",
                intervention_fingerprint,
                config.signal_kind,
                config.primary_label,
                primary_mode or config.primary_mode,
                "1" if runtime_action_available else "0",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return 0
    except OSError as exc:
        print(f"AIWatcher macOS overlay unavailable: {exc}", file=sys.stderr)
        return 2


def _run_macos_swift_presence(url: str, prompt_url: str, position: str) -> int:
    swift = shutil.which("swift")
    if sys.platform != "darwin" or not swift:
        return 2
    script_path = os.path.join(tempfile.gettempdir(), "aiwatcher-native-presence.swift")
    try:
        with open(script_path, "w", encoding="utf-8") as handle:
            handle.write(MACOS_SWIFT_PRESENCE)
        completed = subprocess.run(
            [swift, script_path, url, prompt_url, position],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return int(completed.returncode)
    except OSError as exc:
        print(f"AIWatcher presence unavailable: {exc}", file=sys.stderr)
        return 2


def run_native_presence(
    url: str,
    *,
    prompt_url: str | None = None,
    position: str = "bottom-right",
) -> int:
    """Run the collapsed always-available local companion entry point."""
    prompt_url = prompt_url or f"{url.rstrip('/')}/?view=prompt"
    if sys.platform == "darwin" and shutil.which("swift"):
        return _run_macos_swift_presence(url, prompt_url, position)
    try:
        import tkinter as tk
        from tkinter import ttk
    except Exception as exc:  # pragma: no cover - depends on host Python build
        print(f"AIWatcher companion presence unavailable: {exc}", file=sys.stderr)
        return 2

    root = tk.Tk()
    root.title("AIWatcher Companion")
    root.configure(bg="#0d141f")
    root.attributes("-topmost", True)
    if sys.platform == "win32":
        root.overrideredirect(True)
    try:
        root.call("::tk::unsupported::MacWindowStyle", "style", root._w, "utility", "closeBox")
    except tk.TclError:
        pass

    expanded_width = 386
    expanded_height = 58
    collapsed_width = 64
    collapsed_height = 42
    screen_width = int(root.winfo_screenwidth())
    screen_height = int(root.winfo_screenheight())
    x = 24 if "left" in position else max(16, screen_width - expanded_width - 24)
    y = 24 if "top" in position else max(16, screen_height - expanded_height - 92)
    root.geometry(f"{expanded_width}x{expanded_height}+{x}+{y}")

    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    style.configure("Presence.TFrame", background="#0d141f")
    style.configure("PresenceTitle.TLabel", background="#0d141f", foreground="#f7fbff", font=("Helvetica", 11, "bold"))
    style.configure("PresenceMuted.TLabel", background="#0d141f", foreground="#a8b6ca", font=("Helvetica", 9))
    style.configure("PresenceDot.TLabel", background="#0d141f", foreground="#45d486", font=("Helvetica", 10, "bold"))
    style.configure("PresenceDrag.TLabel", background="#0d141f", foreground="#7b8ba3", font=("Helvetica", 10, "bold"))
    style.configure("Presence.TButton", font=("Helvetica", 9, "bold"), padding=(6, 3))
    style.configure("PresenceMini.TButton", font=("Helvetica", 9, "bold"), padding=(4, 2))

    frame = ttk.Frame(root, padding=(8, 6), style="Presence.TFrame")
    frame.pack(fill="both", expand=True)
    collapsed_frame = ttk.Frame(root, padding=(8, 6), style="Presence.TFrame")
    left = ttk.Frame(frame, style="Presence.TFrame")
    left.pack(side="left", fill="both", expand=True)
    title_var = tk.StringVar(value="AIWatcher")
    subtitle_var = tk.StringVar(value="Watching quietly")
    primary_label_var = tk.StringVar(value="Watch")
    primary_url_var = tk.StringVar(value=url)
    state_var = tk.StringVar(value="watching")
    drag = ttk.Label(frame, text="::", style="PresenceDrag.TLabel", cursor="fleur")
    drag.pack(side="left", padx=(0, 6))
    title_stack = ttk.Frame(left, style="Presence.TFrame")
    title_stack.pack(anchor="w")
    ttk.Label(title_stack, text="on", style="PresenceDot.TLabel").pack(side="left", padx=(0, 6))
    ttk.Label(title_stack, textvariable=title_var, style="PresenceTitle.TLabel").pack(side="left")
    ttk.Label(left, textvariable=subtitle_var, style="PresenceMuted.TLabel", wraplength=112).pack(anchor="w")

    drag_start: dict[str, int] = {"x": 0, "y": 0}

    def begin_drag(event: tk.Event) -> None:
        drag_start["x"] = int(event.x)
        drag_start["y"] = int(event.y)

    def move_window(event: tk.Event) -> None:
        root.geometry(f"+{int(event.x_root) - drag_start['x']}+{int(event.y_root) - drag_start['y']}")

    for draggable in (root, frame, collapsed_frame, drag, left):
        draggable.bind("<ButtonPress-1>", begin_drag)
        draggable.bind("<B1-Motion>", move_window)

    def open_dashboard() -> None:
        webbrowser.open(url)

    def open_prompt() -> None:
        webbrowser.open(prompt_url or url)

    def open_primary() -> None:
        webbrowser.open(primary_url_var.get() or url)

    collapsed = tk.BooleanVar(value=False)

    def toggle_collapsed() -> None:
        collapsed.set(not collapsed.get())
        if collapsed.get():
            frame.pack_forget()
            collapsed_frame.pack(fill="both", expand=True)
            root.geometry(f"{collapsed_width}x{collapsed_height}")
        else:
            collapsed_frame.pack_forget()
            frame.pack(fill="both", expand=True)
            root.geometry(f"{expanded_width}x{expanded_height}")
        update_attention_style()

    def update_attention_style() -> None:
        needs_attention = state_var.get() in {"prompt_gate", "control_recommended", "proof_pending", "needs_review"}
        shell_bg = "#c6531e" if collapsed.get() and needs_attention else "#0d141f"
        root.configure(bg=shell_bg)
        style.configure("Presence.TFrame", background=shell_bg)
        style.configure("PresenceTitle.TLabel", background=shell_bg)
        style.configure("PresenceMuted.TLabel", background=shell_bg)
        style.configure("PresenceDot.TLabel", background=shell_bg)
        style.configure("PresenceDrag.TLabel", background=shell_bg)

    def refresh_state() -> None:
        try:
            request_url = f"{url.rstrip('/')}/api/companion-state"
            with urllib.request.urlopen(request_url, timeout=0.8) as response:
                payload = json.loads(response.read().decode("utf-8"))
            state_var.set(str(payload.get("state") or "watching"))
            title_var.set(str(payload.get("label") or "AIWatcher")[:18])
            subtitle_var.set(str(payload.get("subtitle") or "Watching quietly")[:42])
            primary_label_var.set(str(payload.get("primary_label") or "Watch")[:12])
            primary_path = str(payload.get("primary_url") or "/")
            primary_url_var.set(primary_path if primary_path.startswith("http") else f"{url.rstrip('/')}{primary_path}")
        except (OSError, urllib.error.URLError, json.JSONDecodeError, tk.TclError):
            state_var.set("watching")
            title_var.set("AIWatcher")
            subtitle_var.set("Watching quietly")
            primary_label_var.set("Watch")
            primary_url_var.set(url)
        finally:
            update_attention_style()
            try:
                root.after(8000, refresh_state)
            except tk.TclError:
                pass

    ttk.Button(frame, text="Plan", width=6, style="Presence.TButton", command=open_prompt).pack(side="left", padx=(8, 4))
    ttk.Button(frame, textvariable=primary_label_var, width=11, style="Presence.TButton", command=open_primary).pack(side="left", padx=(0, 4))
    ttk.Button(frame, text="Console", width=8, style="Presence.TButton", command=open_dashboard).pack(side="left")
    ttk.Button(frame, text="-", width=2, style="PresenceMini.TButton", command=toggle_collapsed).pack(side="left", padx=(4, 0))
    ttk.Button(collapsed_frame, text="AIW", width=5, style="Presence.TButton", command=toggle_collapsed).pack(fill="both", expand=True)
    refresh_state()
    root.mainloop()
    return 0


def run_native_overlay(
    url: str,
    title: str,
    body: str,
    severity: str,
    brief_file: str | None = None,
    *,
    intervention_fingerprint: str = "",
    signal_kind: str = "generic",
    primary_label: str | None = None,
    primary_mode: str | None = None,
    runtime_action_available: bool = False,
) -> int:
    signal_kind = _infer_signal_kind_from_title(signal_kind, title, body)
    config = overlay_config(signal_kind, primary_label=primary_label)
    title = title or config.title
    primary_mode = primary_mode or config.primary_mode
    if sys.platform == "darwin" and shutil.which("swift"):
        return _run_macos_swift_overlay(
            url,
            title,
            body,
            severity,
            brief_file,
            intervention_fingerprint=intervention_fingerprint,
            signal_kind=config.signal_kind,
            primary_label=config.primary_label,
            primary_mode=primary_mode,
            runtime_action_available=runtime_action_available,
        )
    try:
        import tkinter as tk
        from tkinter import ttk
    except Exception as exc:  # pragma: no cover - depends on host Python build
        if sys.platform == "darwin":
            return _run_macos_swift_overlay(
                url,
                title,
                body,
                severity,
                brief_file,
                intervention_fingerprint=intervention_fingerprint,
                signal_kind=config.signal_kind,
                primary_label=config.primary_label,
                primary_mode=primary_mode,
                runtime_action_available=runtime_action_available,
            )
        print(f"AIWatcher native overlay unavailable: {exc}", file=sys.stderr)
        return 2

    base = _api_base(url)
    session_id = _session_id(url)
    reason = body
    local_brief = _read_brief_file(brief_file)

    root = tk.Tk()
    root.title("AIWatcher")
    root.configure(bg="#edf4ff")
    root.attributes("-topmost", True)
    if sys.platform == "win32":
        root.overrideredirect(True)
    try:
        root.call("::tk::unsupported::MacWindowStyle", "style", root._w, "utility", "closeBox")
    except tk.TclError:
        pass

    width = 680
    height = 180
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

    status = tk.StringVar(value="Local-only; auto-hides in 20 seconds")
    button_row = ttk.Frame(frame, style="AIW.TFrame")
    button_row.pack(fill="x", pady=(0, 12))

    def show_saved(message: str) -> None:
        status.set(message)
        root.after(2500, root.destroy)

    def inspect_session() -> None:
        _record_intervention_action(base, intervention_fingerprint, "acted")
        webbrowser.open(f"{base}/?session={urllib.parse.quote(session_id)}")
        show_saved("Opened the evidence. Review it before continuing the run.")

    def primary_action() -> None:
        if primary_mode == "inspect":
            inspect_session()
            return
        try:
            if local_brief:
                brief = local_brief
            else:
                capsule = _request_json(f"{base}/api/handoff?id={urllib.parse.quote(session_id)}&target=generic&prompt=0")
                brief = str(capsule.get("next_brief") or "")
            root.clipboard_clear()
            root.clipboard_append(brief)
            root.update()
            _record_intervention_action(base, intervention_fingerprint, "acted")
            if _normalize_signal_kind(signal_kind) == "critical_context":
                _record_decision(base, session_id, "copy_handoff", body or "Fresh Start brief copied.")
            opened = runtime_action_available and _request_runtime_return(base, session_id)
            destination = "The return target was opened." if opened else "Return to your AI tool."
            show_saved(f"Brief copied. {destination} Paste it to continue.")
        except (OSError, urllib.error.URLError, json.JSONDecodeError, tk.TclError):
            status.set("Could not copy. Open the dashboard to copy the Fresh Start brief manually.")

    def snooze() -> None:
        _record_intervention_action(base, intervention_fingerprint, "snooze", snooze_minutes=15)
        show_saved("Snoozed for 15 minutes. AIWatcher will stay quiet unless severity worsens.")

    def continue_here() -> None:
        _record_decision(base, session_id, "continue_here", body or "Continue in current session.")
        _record_intervention_action(base, intervention_fingerprint, "snooze", snooze_minutes=15)
        show_saved("Continuing here. AIWatcher will stay quiet for 15 minutes.")

    def dismiss_session() -> None:
        _record_intervention_action(base, intervention_fingerprint, "dismiss")
        show_saved("Dismissed for this session state.")

    ttk.Button(button_row, text=config.primary_label, style="AIW.TButton", command=primary_action).pack(
        side="left", padx=(0, 10)
    )
    ttk.Button(button_row, text="Continue here", style="AIW.TButton", command=continue_here).pack(
        side="left", padx=(0, 10)
    )
    more_menu = tk.Menu(root, tearoff=False)
    more_menu.add_command(label="Inspect evidence", command=inspect_session)
    more_menu.add_command(label="Snooze 15 minutes", command=snooze)
    more_menu.add_command(label="Dismiss for this session", command=dismiss_session)

    def show_more() -> None:
        more_menu.tk_popup(root.winfo_pointerx(), root.winfo_pointery())

    ttk.Button(button_row, text="...", style="AIW.TButton", command=show_more).pack(side="left")

    ttk.Label(frame, textvariable=status, style="AIWMuted.TLabel", wraplength=700).pack(fill="x", anchor="w")

    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.user32.SetWindowPos(  # type: ignore[attr-defined]
                root.winfo_id(), -1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010 | 0x0040
            )
        except (AttributeError, OSError, tk.TclError):
            pass
    else:
        root.lift()
    _record_intervention_action(base, intervention_fingerprint, "displayed")
    root.after(
        20_000,
        lambda: (
            _record_intervention_action(base, intervention_fingerprint, "snooze", snooze_minutes=15),
            root.destroy(),
        ),
    )
    root.mainloop()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aiwatcher-native-overlay")
    parser.add_argument("--presence", action="store_true", help="Run the collapsed always-available companion")
    parser.add_argument("--url", required=True)
    parser.add_argument("--prompt-url")
    parser.add_argument(
        "--position",
        choices=("bottom-right", "bottom-left", "top-right", "top-left"),
        default="bottom-right",
    )
    parser.add_argument("--title", default="Start a fresh AI session")
    parser.add_argument("--body", default="")
    parser.add_argument("--severity", default="warning")
    parser.add_argument("--brief-file")
    parser.add_argument("--intervention-fingerprint", default="")
    parser.add_argument("--signal-kind", default="generic")
    parser.add_argument("--primary-label")
    parser.add_argument("--primary-mode", choices=("copy", "inspect"))
    parser.add_argument("--runtime-action-available", action="store_true")
    args = parser.parse_args(argv)
    if args.presence:
        return run_native_presence(args.url, prompt_url=args.prompt_url, position=args.position)
    return run_native_overlay(
        args.url,
        args.title,
        args.body,
        args.severity,
        args.brief_file,
        intervention_fingerprint=args.intervention_fingerprint,
        signal_kind=args.signal_kind,
        primary_label=args.primary_label,
        primary_mode=args.primary_mode,
        runtime_action_available=args.runtime_action_available,
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
