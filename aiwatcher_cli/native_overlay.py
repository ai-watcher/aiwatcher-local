"""Tiny native Fresh Start companion for AIWatcher Local.

This is intentionally dependency-free. It gives `aiwatcher watch --overlay` a
real desktop window that can float above Claude, Codex, Cursor, or a browser
without claiming to inject UI into those apps.
"""

from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

from . import brand_mark


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
    "waiting": "session_blocked",
    "blocked": "session_blocked",
    "session_waiting": "session_blocked",
}


def _normalize_signal_kind(value: str | None) -> str:
    normalized = (value or "generic").strip().lower().replace("-", "_").replace(" ", "_")
    normalized = _SIGNAL_ALIASES.get(normalized, normalized)
    return normalized if normalized in {"session_blocked", "critical_context", "loop", "velocity", "runway", "generic"} else "generic"


def _reduce_motion_requested() -> bool:
    """Honor the host preference in the Tk fallback, with an env override."""
    override = os.environ.get("AIWATCHER_REDUCE_MOTION")
    if override is not None:
        return override.strip().lower() in {"1", "true", "yes", "on"}
    try:
        if sys.platform == "darwin":
            result = subprocess.run(
                ["defaults", "read", "com.apple.universalaccess", "reduceMotion"],
                capture_output=True,
                text=True,
                timeout=0.5,
                check=False,
            )
            return result.stdout.strip() == "1"
        if sys.platform == "win32":
            import ctypes

            enabled = ctypes.c_int(1)
            ok = ctypes.windll.user32.SystemParametersInfoW(0x1042, 0, ctypes.byref(enabled), 0)
            return bool(ok) and not bool(enabled.value)
        gsettings = shutil.which("gsettings")
        if gsettings:
            result = subprocess.run(
                [gsettings, "get", "org.gnome.desktop.interface", "enable-animations"],
                capture_output=True,
                text=True,
                timeout=0.5,
                check=False,
            )
            return result.stdout.strip().lower() == "false"
    except (OSError, subprocess.SubprocessError, AttributeError):
        pass
    return False


def _companion_expression(
    state: str,
    *,
    working: bool,
    needs_attention: bool,
    phase: int,
    reduce_motion: bool,
) -> tuple[int, int, str]:
    """Return the Tk Companion's gaze and eye colour for observable state."""
    # "connecting" shares the grey, downcast face. Before the first good read
    # there is nothing to report, and grey is the absence of a claim -- the
    # resting face quietly asserts that all is well, which is not yet known.
    if state in {"offline", "connecting"}:
        return 0, 1, "#808f9f"
    if needs_attention:
        return 1, -1, "#ffd6ad"
    if state == "session_finished":
        return 0, -1, "#7fdbff"
    if working:
        gaze = (0, 0) if reduce_motion else ((-1, 0), (0, 1), (1, 0), (0, -1))[phase % 4]
        return gaze[0], gaze[1], "#78f0c2"
    return 0, 0, "#7fdbff"


def overlay_config(
    signal_kind: str | None,
    *,
    primary_label: str | None = None,
    action_endpoint_available: bool = False,
    runtime_action_available: bool = False,
) -> OverlayConfig:
    """Return truthful, signal-specific copy and behavior for the companion."""
    kind = _normalize_signal_kind(signal_kind)
    if kind == "session_blocked":
        # Not a handoff. The session is alive and stopped on a permission
        # prompt, so the useful act is to get back to it and answer -- a brief
        # pasted into a fresh chat abandons work that only needed a yes.
        config = OverlayConfig(
            kind,
            "A session is waiting for you",
            "Return to session",
            "return_to_session",
            "return",
            "This session stopped and cannot continue until you answer it.",
        )
    elif kind == "critical_context":
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

// The return-mode button has to report what happened, so unlike the call above
// this one waits and keeps the reason. Not gated on runtimeActionAvailable:
// the server decides, and its refusal is the message we want to show.
func requestRuntimeReturnResult() -> (ok: Bool, message: String) {
    guard let url = URL(string: "\(baseURL)/api/runtime-return") else {
        return (false, "no dashboard URL")
    }
    var request = URLRequest(url: url)
    request.httpMethod = "POST"
    request.setValue("application/json", forHTTPHeaderField: "Content-Type")
    request.httpBody = try? JSONSerialization.data(withJSONObject: ["session_id": sid])
    let sem = DispatchSemaphore(value: 0)
    var ok = false
    var message = ""
    URLSession.shared.dataTask(with: request) { data, _, _ in
        defer { sem.signal() }
        guard let data = data,
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return }
        ok = (json["ok"] as? Bool) ?? false
        message = (json["message"] as? String) ?? (json["error"] as? String) ?? ""
    }.resume()
    _ = sem.wait(timeout: .now() + 4)
    return (ok, message)
}

func fetchHandoffBrief() -> String? {
    if !briefFile.isEmpty,
       let value = try? String(contentsOfFile: briefFile, encoding: .utf8),
       !value.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
        return value
    }
    guard let encoded = sid.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed),
          let url = URL(string: "\(baseURL)/api/handoff?id=\(encoded)&target=generic") else { return nil }
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

        // The notification wears the brand the same way the collapsed bubble
        // does: the mark on a plain white ground, ink type, and severity
        // carried by the mark itself -- the blue ring turns orange when the
        // signal is critical, the job that ring does everywhere else. The
        // ground never floods.
        window.isOpaque = false
        window.backgroundColor = .clear
        // The ground is fixed white regardless of system theme, but without
        // this the window inherits Dark Mode from the OS and native buttons
        // draw their titles in the dark-mode label color -- near-white text
        // on this near-white ground, unreadable. Pin the appearance so
        // buttons render with the light-mode ink this ground was designed
        // for.
        window.appearance = NSAppearance(named: .aqua)
        let view = NSView(frame: NSRect(x: 0, y: 0, width: width, height: height))
        view.wantsLayer = true
        view.layer?.backgroundColor = NSColor(calibratedRed: 1.00, green: 1.00, blue: 1.00, alpha: 0.98).cgColor
        view.layer?.cornerRadius = 14
        view.layer?.borderWidth = 1
        view.layer?.borderColor = NSColor(calibratedRed: 0.08, green: 0.07, blue: 0.08, alpha: 0.14).cgColor
        window.contentView = view

        // The mark from logo/aiwatcher-mark.svg as layers, ink-on-light. Both
        // rings 300 wide, 40 stroke, 85 outer corner radius in a 429x349 box;
        // the ink ring (300x232 at offset 129,117) in front. The height
        // difference between the rings is the artwork's own.
        let markHeight: CGFloat = 30
        let markScale = markHeight / 349.0
        let markView = NSView(frame: NSRect(x: 22, y: 118, width: 429.0 * markScale, height: markHeight))
        markView.wantsLayer = true
        let blueRing = CALayer()
        blueRing.frame = CGRect(x: 0, y: markHeight - 260.0 * markScale, width: 300.0 * markScale, height: 260.0 * markScale)
        blueRing.borderWidth = 40.0 * markScale
        blueRing.cornerRadius = 85.0 * markScale
        blueRing.borderColor = (severityText == "critical"
            ? NSColor(calibratedRed: 0.93, green: 0.42, blue: 0.14, alpha: 1)
            : NSColor(calibratedRed: 0.00, green: 0.32, blue: 0.96, alpha: 1)).cgColor
        markView.layer?.addSublayer(blueRing)
        let inkRing = CALayer()
        inkRing.frame = CGRect(x: 129.0 * markScale, y: 0, width: 300.0 * markScale, height: 232.0 * markScale)
        inkRing.borderWidth = 40.0 * markScale
        inkRing.cornerRadius = 85.0 * markScale
        inkRing.borderColor = NSColor(calibratedRed: 0.08, green: 0.07, blue: 0.08, alpha: 1).cgColor
        markView.layer?.addSublayer(inkRing)
        view.addSubview(markView)

        let title = NSTextField(labelWithString: titleText)
        title.frame = NSRect(x: 70, y: 122, width: 452, height: 26)
        title.font = NSFont.boldSystemFont(ofSize: 18)
        title.textColor = NSColor(calibratedRed: 0.08, green: 0.07, blue: 0.08, alpha: 1)
        view.addSubview(title)

        let badge = NSTextField(labelWithString: severityText)
        badge.frame = NSRect(x: 550, y: 124, width: 92, height: 22)
        badge.alignment = .center
        badge.font = NSFont.systemFont(ofSize: 13, weight: .semibold)
        badge.textColor = severityText == "critical"
            ? NSColor(calibratedRed: 0.80, green: 0.33, blue: 0.09, alpha: 1)
            : NSColor(calibratedRed: 0.62, green: 0.42, blue: 0.07, alpha: 1)
        view.addSubview(badge)

        let body = NSTextField(wrappingLabelWithString: bodyText)
        body.frame = NSRect(x: 22, y: 78, width: 626, height: 38)
        body.font = NSFont.systemFont(ofSize: 14)
        body.textColor = NSColor(calibratedRed: 0.26, green: 0.25, blue: 0.26, alpha: 1)
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

        statusLabel = NSTextField(labelWithString: "Private by default. Prompt/source content is not stored in this decision.")
        statusLabel.frame = NSRect(x: 452, y: 44, width: 196, height: 18)
        statusLabel.alignment = .right
        statusLabel.font = NSFont.systemFont(ofSize: 11)
        statusLabel.textColor = NSColor(calibratedRed: 0.08, green: 0.07, blue: 0.08, alpha: 0.55)
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
        if primaryMode == "return" {
            postInterventionAction("acted")
            let result = requestRuntimeReturnResult()
            if result.ok {
                finish(result.message.isEmpty ? "Opened your AI tool. Answer it there to continue." : result.message)
            } else {
                // Say why, and leave the evidence one click away. Reporting a
                // return that did not happen is the failure this mode exists
                // to stop.
                openDashboardSession()
                let reason = result.message.isEmpty ? "" : " (\(result.message))"
                finish("Could not reach the tool from here\(reason). Opened the session in AIWatcher instead.")
            }
            return
        }
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
        openDashboardSession()
    }

    func openDashboardSession() {
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
let visibilityMode = args.count > 4 ? args[4] : "always"
let intervalSeconds = args.count > 5 ? args[5] : "30"
let restartRequestPath = args.count > 6 ? args[6] : ""

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

let foregroundAIHints = [
    "codex",
    "claude",
    "chatgpt",
    "openai",
    "cursor",
    "visual studio code",
    "vscode",
    "code",
    "terminal",
    "iterm",
    "warp",
    "wezterm",
    "hyper",
    "ghostty",
    "powershell"
]

func foregroundLooksLikeAIWork() -> Bool {
    guard let app = NSWorkspace.shared.frontmostApplication else { return false }
    let bundle = app.bundleIdentifier?.lowercased() ?? ""
    let name = app.localizedName?.lowercased() ?? ""
    let combined = "\(bundle) \(name)"
    return foregroundAIHints.contains { combined.contains($0) }
}

final class DragView: NSView {
    override var mouseDownCanMoveWindow: Bool { true }
}

// The collapsed bubble is one full-size button, so a plain NSButton would
// swallow every mouse-down and the bubble could not be moved. A press that
// travels more than a few points becomes a window drag; a press released in
// place stays a click.
final class DraggableButton: NSButton {
    override func mouseDown(with event: NSEvent) {
        let start = event.locationInWindow
        while true {
            guard let next = window?.nextEvent(matching: [.leftMouseUp, .leftMouseDragged]) else { return }
            if next.type == .leftMouseUp {
                performClick(nil)
                return
            }
            if abs(next.locationInWindow.x - start.x) + abs(next.locationInWindow.y - start.y) > 3 {
                window?.performDrag(with: next)
                return
            }
        }
    }
}

final class UsagePanelView: NSView {
    override func draw(_ dirtyRect: NSRect) {
        NSColor.windowBackgroundColor.setFill()
        dirtyRect.fill()
    }
}

final class UsageMeter: NSView {
    var doubleValue: Double = 0 { didSet { needsDisplay = true } }
    override func draw(_ dirtyRect: NSRect) {
        NSColor.quaternaryLabelColor.setFill()
        NSBezierPath(roundedRect: bounds, xRadius: 4, yRadius: 4).fill()
        let portion = min(max(doubleValue, 0), 100) / 100
        if portion > 0 {
            NSColor.systemBlue.setFill()
            NSBezierPath(roundedRect: NSRect(x: 0, y: 0, width: bounds.width * portion, height: bounds.height), xRadius: 4, yRadius: 4).fill()
        }
    }
}

final class PresenceDelegate: NSObject, NSApplicationDelegate {
    var window: NSPanel!
    var rootView: NSView!
    var titleLabel: NSTextField!
    var subtitleLabel: NSTextField!
    var primaryButton: NSButton!
    var continueButton: NSButton!
    var skipButton: NSButton!
    var planButton: NSButton!
    var askButton: NSButton!
    var scanButton: NSButton!
    var consoleButton: NSButton!
    var collapseButton: NSButton!
    var expandButton: NSButton!
    var dragHandle: NSTextField!
    var brandMarkView: NSView!
    var brandBlueRing: CALayer!
    var collapsedMarkView: NSView!
    var collapsedBlueRing: CALayer!
    var collapsedFacePanel: CALayer!
    var collapsedLeftEye: CALayer!
    var collapsedRightEye: CALayer!
    var rimLayer: CAShapeLayer!
    var collapsedBadge: NSTextField!
    var orbitLayer: CAShapeLayer!
    var primaryURL = dashboardURL
    var primaryAction = "open_url"
    var primarySessionID = ""
    var compactCommand = ""
    var compactSha = ""
    var primaryRuntimeAvailable = false
    var continueAction = ""
    var continueURL = ""
    var continueSessionID = ""
    var continueReason = ""
    var continueExpectedTokens = 0
    var skipState = ""
    var skipSessionID = ""
    var skipProject = ""
    var skipProjects: [String] = []
    var skipSessionIDs: [String] = []
    var collapsed = false
    var stateName = "watching"
    var pulseOn = false
    var expressionPhase = 0
    var expressionScheduleToken = 0
    var autoCollapseToken = 0
    var autoCollapseDeadline: Date? = nil
    var suppressPassiveRefreshUntil: Date? = nil
    var pendingClipboardOverrideSessionID = ""
    var didExpandOffline = false
    // See refreshState: a failed probe says "no answer yet", not "the
    // dashboard is down". Stopgap numbers, mirroring the Tk presence bar --
    // nothing local reports how long the dashboard's first scan will take.
    static let offlineAfterFailures = 3
    static let offlineAfterSeconds: Double = 45.0
    var probeFailures = 0
    var lastProbeOK: Date? = nil
    let probeSince = Date()
    var waitingRowTexts: [String] = []
    var waitingURLs: [String] = []
    var waitingSessionIDs: [String] = []
    var waitingReturnAvailable: [Bool] = []
    var waitingWants: [String] = []
    var waitingRowKinds: [String] = []
    var waitingRowSections: [String] = []
    var waitingRowActionLabels: [String] = []
    // Compaction rows: one window each, with its own command. A row whose
    // action is empty is a fact (compacting, compacted) and draws no button.
    var waitingRowTags: [String] = []
    var waitingRowActions: [String] = []
    var waitingRowCommands: [String] = []
    var waitingRowShas: [String] = []
    var waitingCount = 0
    var workingCount = 0
    var finishedCount = 0
    var reviewCount = 0
    var motionMode = "standard"
    var activityTone = ""
    var deliveryConfirmed = false
    var deliveryReceiptID = ""
    var detailText = ""
    var pressureAvailable = false
    var pressurePct = 0
    var pressureSeverity = "ok"
    var pressureLabel = ""
    var pressureStats = ""
    var pressureStatsDetail = ""
    var signalChipText = ""
    var signalURL = ""
    var rowDots: [NSView] = []
    var rowLabels: [NSTextField] = []
    var rowTags: [NSTextField] = []
    var rowButtons: [NSButton] = []
    var meterTrack: NSView!
    var meterFill: NSView!
    var meterLabel: NSTextField!
    var statsLabel: NSTextField!
    var signalButton: NSButton!
    var usageButton: NSButton!
    var usagePopover: NSPopover?
    var usagePicker: NSPopUpButton!
    var usageHeading: NSTextField!
    var usageIdentity: NSTextField!
    var usageContext: NSTextField!
    var usageNote: NSTextField!
    var usageTotals: NSTextField!
    var usageInput: NSTextField!
    var usageCache: NSTextField!
    var usageCost: NSTextField!
    var usageBoundary: NSTextField!
    var usageUpdated: NSTextField!
    var usageMeter: UsageMeter!
    var usageOpenButton: NSButton!
    var usageRows: [[String: Any]] = []
    var usageSessionID = ""
    var usageSessionURL = ""
    var usageRequest = 0

    // 46 characters of 9pt subtitle measure ~210pt ("claude-code ·
    // aiwatcher-local · waited 12m 30s" is 208), and at 560 the subtitle
    // had 150 beside the meter, so every compact stage read "...calibrat".
    // The extra 66 goes to the subtitle in every layout; everything to its
    // right shifts by the same 66.
    // Usage has its own slot; it never displaces a gate or queue action.
    let expandedWidth: CGFloat = 706
    let headerHeight: CGFloat = 58
    let rowHeight: CGFloat = 34
    let maxWaitingRows = 5
    // 52, not 44: the count badge sits at the circle's 45-degree corner, and
    // on a 44px bubble that corner has already curved away -- the badge was
    // half over transparent window and rendered cropped.
    let collapsedWidth: CGFloat = 52
    let collapsedHeight: CGFloat = 52
    let brandBlue = NSColor(calibratedRed: 0.00, green: 0.32, blue: 0.96, alpha: 1)      // #0052F5
    let brandInkOnDark = NSColor(calibratedRed: 0.86, green: 0.90, blue: 0.96, alpha: 1) // #DCE6F6
    let brandInkOnLight = NSColor(calibratedRed: 0.08, green: 0.07, blue: 0.08, alpha: 1) // #141314

    // The mark from logo/aiwatcher-mark.svg as layers: a CALayer with a border
    // and corner radius is a rounded-rect ring. Both rings are 300 wide with a
    // 40 stroke and an 85 outer corner radius in a 429x349 box; the ink ring
    // (300x232 at offset 129,117) draws in front. The rings' heights differ on
    // purpose -- equalising them breaks the fit to the original artwork.
    func makeBrandMark(height: CGFloat, ink: NSColor) -> (mark: CALayer, blueRing: CALayer, inkRing: CALayer) {
        let s = height / 349.0
        let mark = CALayer()
        mark.frame = CGRect(x: 0, y: 0, width: 429.0 * s, height: height)
        let blueRing = CALayer()
        blueRing.frame = CGRect(x: 0, y: height - 260.0 * s, width: 300.0 * s, height: 260.0 * s)
        blueRing.borderWidth = 40.0 * s
        blueRing.cornerRadius = 85.0 * s
        blueRing.borderColor = brandBlue.cgColor
        mark.addSublayer(blueRing)
        let inkRing = CALayer()
        inkRing.frame = CGRect(x: 129.0 * s, y: 0, width: 300.0 * s, height: 232.0 * s)
        inkRing.borderWidth = 40.0 * s
        inkRing.cornerRadius = 85.0 * s
        inkRing.borderColor = ink.cgColor
        mark.addSublayer(inkRing)
        return (mark, blueRing, inkRing)
    }

    // One row per actionable item. Waiting, context review, finished batches,
    // and away digests all reuse this row surface so the badge can explain
    // itself instead of dropping the user into an unlabeled count.
    var visibleWaitingRows: Int {
        if waitingRowTexts.isEmpty { return 0 }
        return min(waitingRowTexts.count, maxWaitingRows)
    }

    func reviewTitle() -> String {
        if ["control_review", "context_review"].contains(stateName) {
            if visibleWaitingRows > 0 && reviewCount > visibleWaitingRows {
                return "\(visibleWaitingRows) of \(reviewCount) projects need review"
            }
            return reviewCount == 1 ? "1 project needs review" : "\(reviewCount) projects need review"
        }
        if stateName == "session_finished" && visibleWaitingRows > 0 {
            if finishedCount > visibleWaitingRows {
                return "\(visibleWaitingRows) of \(finishedCount) ready to check"
            }
            return "\(finishedCount) ready to check"
        }
        return String((titleLabel.stringValue.isEmpty ? "AIWatcher" : titleLabel.stringValue).prefix(40))
    }

    func reviewSubtitle() -> String {
        if ["control_review", "context_review"].contains(stateName), visibleWaitingRows > 0 {
            let total = max(reviewCount, visibleWaitingRows)
            let hidden = max(total - visibleWaitingRows, 0)
            return hidden > 0 ? "\(hidden) more saved in Watch. Review all or pick a project." : "Pick a project to review its context."
        }
        if stateName == "session_finished", visibleWaitingRows > 0 {
            let count = stateName == "session_finished" ? finishedCount : reviewCount
            let total = max(count, visibleWaitingRows)
            let hidden = max(total - visibleWaitingRows, 0)
            return hidden > 0 ? "\(hidden) more in UI. Pick a row or open UI." : "Pick a row or open UI."
        }
        return String((subtitleLabel.stringValue.isEmpty ? "Watching quietly" : subtitleLabel.stringValue).prefix(46))
    }

    func rowDisplayText(_ row: [String: Any]) -> String {
        let kind = row["kind"] as? String ?? ""
        if kind == "compact" {
            return row["text"] as? String ?? ""
        }
        // A live chat's current prompt: the server writes the whole line
        // (chat name and figures, never prompt text).
        if kind.hasPrefix("prompt_") {
            return row["text"] as? String ?? ""
        }
        if let text = row["text"] as? String, !text.isEmpty {
            return text
        }
        if ["working", "delivery_confirmed"].contains(kind) {
            return row["text"] as? String ?? ""
        }
        let tool = row["tool"] as? String ?? "AI tool"
        let project = row["project"] as? String ?? ""
        let waited = row["waited_label"] as? String ?? ""
        let review = row["review_label"] as? String ?? waited
        let severity = row["severity_label"] as? String ?? ""
        let activity = row["activity_label"] as? String ?? ""
        let scope = row["scope_label"] as? String ?? ""
        let prefix: String
        if ["finished", "recently_observed"].contains(kind) {
            prefix = "Ready to check"
        } else if kind == "session_waiting" {
            prefix = "Waiting"
        } else if kind == "context_review" {
            prefix = "Context health"
        } else if kind.isEmpty {
            prefix = tool
        } else {
            prefix = "Signal"
        }
        if kind.isEmpty {
            return [tool, project, waited].filter { !$0.isEmpty }.joined(separator: " · ")
        }
        let details = kind == "context_review"
            ? [scope, project, tool, severity, activity, review]
            : [project, tool, waited]
        let detail = details.filter { !$0.isEmpty }.joined(separator: " · ")
        return detail.isEmpty ? prefix : "\(prefix): \(detail)"
    }

    func rowActionLabel(index: Int) -> String {
        if index < waitingRowActionLabels.count, !waitingRowActionLabels[index].isEmpty {
            return waitingRowActionLabels[index]
        }
        let kind = index < waitingRowKinds.count ? waitingRowKinds[index] : ""
        if kind == "compact" { return "Review" }
        if kind.hasPrefix("prompt_") { return "Open" }
        let canReturn = index < waitingReturnAvailable.count && waitingReturnAvailable[index]
        if canReturn { return "Return" }
        if ["context_review", "control_review"].contains(kind) { return "Review" }
        return kind.isEmpty ? "Open" : "Review"
    }

    func splitLines(_ value: String) -> [String] {
        return value.components(separatedBy: .newlines)
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
    }

    func shortSkipFailureMessage() -> String {
        if ["control_recommended_group", "control_recommended_project"].contains(skipState) {
            return "Could not save Later. Open UI."
        }
        if skipState == "session_finished_group" {
            return "Could not clear notices. Open UI."
        }
        return "Could not save Skip. Open UI."
    }

    func applyOfflineState() {
        self.stateName = "offline"
        self.waitingRowTexts = []
        self.waitingURLs = []
        self.waitingSessionIDs = []
        self.waitingReturnAvailable = []
        self.waitingWants = []
        self.waitingRowKinds = []
        self.waitingRowSections = []
        self.waitingRowActionLabels = []
        self.waitingRowTags = []
        self.waitingRowActions = []
        self.waitingRowCommands = []
        self.waitingRowShas = []
        self.waitingCount = 0
        self.workingCount = 0
        self.finishedCount = 0
        self.reviewCount = 0
        self.activityTone = ""
        self.skipState = ""
        self.skipSessionID = ""
        self.skipProject = ""
        self.skipProjects = []
        self.skipSessionIDs = []
        self.titleLabel.stringValue = "UI offline"
        self.subtitleLabel.stringValue = "Start dashboard + Companion"
        self.primaryButton.title = "Restart"
        self.primaryAction = "restart_aiwatcher"
        self.primaryURL = dashboardURL
        self.planButton.isEnabled = false
        self.askButton.isEnabled = false
        self.scanButton.isEnabled = false
        self.consoleButton.isEnabled = false
        if !didExpandOffline && collapsed {
            didExpandOffline = true
            setCollapsed(false)
        } else {
            self.updateAppearance()
        }
    }

    func applyConnectingState() {
        // Unreachable, but not for long enough to claim the dashboard is down.
        // After a good read the last rows stay put and only the window title
        // changes; a cold start has nothing to show and says so rather than
        // offering Restart for a dashboard that is still starting.
        if lastProbeOK == nil {
            self.stateName = "connecting"
            self.titleLabel.stringValue = "Connecting\u{2026}"
            self.subtitleLabel.stringValue = "Dashboard is starting. Its first scan can take a moment."
        }
        self.updateAppearance()
    }

    var isUnreachableState: Bool {
        return stateName == "offline" || stateName == "connecting"
    }

    var expandedHeight: CGFloat {
        return headerHeight + CGFloat(visibleWaitingRows) * rowHeight
    }

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
        // Without this the window's opaque backing shows as dark corners
        // behind the rounded root view -- a square drawn behind the bubble.
        window.isOpaque = false
        window.backgroundColor = .clear
        // The ground here is a fixed dark navy regardless of system theme --
        // the same reasoning as the notification window's .aqua pin above,
        // mirrored. Left unpinned, a Light Mode system draws native button
        // titles in the light-mode label color, near-black text on this
        // near-black ground.
        window.appearance = NSAppearance(named: .darkAqua)

        rootView = DragView(frame: NSRect(x: 0, y: 0, width: width, height: height))
        rootView.wantsLayer = true
        rootView.layer?.backgroundColor = NSColor(calibratedRed: 0.035, green: 0.052, blue: 0.078, alpha: 0.90).cgColor
        rootView.layer?.cornerRadius = 16
        rootView.layer?.borderWidth = 1
        rootView.layer?.borderColor = NSColor(calibratedRed: 0.34, green: 0.52, blue: 0.74, alpha: 0.72).cgColor
        window.contentView = rootView

        dragHandle = NSTextField(labelWithString: "::")
        dragHandle.frame = NSRect(x: 10, y: 21, width: 18, height: 16)
        dragHandle.font = NSFont.systemFont(ofSize: 12, weight: .bold)
        dragHandle.textColor = NSColor(calibratedRed: 0.48, green: 0.58, blue: 0.70, alpha: 1)
        dragHandle.toolTip = "Drag AIWatcher"
        rootView.addSubview(dragHandle)

        // The dashboard's brand mark, on the bar's dark ground so the ink ring
        // takes the dark-theme ink (see logo/README.md on the brand branch).
        brandMarkView = NSView(frame: NSRect(x: 30, y: 16, width: 32, height: 26))
        brandMarkView.wantsLayer = true
        let expandedMark = makeBrandMark(height: 26, ink: brandInkOnDark)
        brandBlueRing = expandedMark.blueRing
        brandMarkView.layer?.addSublayer(expandedMark.mark)
        rootView.addSubview(brandMarkView)

        titleLabel = NSTextField(labelWithString: "AIWatcher")
        titleLabel.frame = NSRect(x: 66, y: 31, width: 220, height: 17)
        titleLabel.font = NSFont.systemFont(ofSize: 12, weight: .semibold)
        // Truncate by pixel, not by character: the 18-character cap the bar
        // launched with cut "Continue only after confirmation" -- the label
        // every destructive prompt's gate wears -- to "Continue only afte"
        // while the frame still had room. The frame is the limit now, and
        // the title grows into it when a layout widens it.
        titleLabel.lineBreakMode = .byTruncatingTail
        titleLabel.textColor = NSColor.white
        rootView.addSubview(titleLabel)

        subtitleLabel = NSTextField(labelWithString: "Watching quietly")
        subtitleLabel.frame = NSRect(x: 66, y: 12, width: 304, height: 16)
        subtitleLabel.font = NSFont.systemFont(ofSize: 9)
        subtitleLabel.lineBreakMode = .byTruncatingTail
        subtitleLabel.textColor = NSColor(calibratedRed: 0.67, green: 0.74, blue: 0.84, alpha: 1)
        rootView.addSubview(subtitleLabel)

        planButton = NSButton(title: "Plan", target: self, action: #selector(openPrompt))
        planButton.frame = NSRect(x: 356, y: 15, width: 48, height: 28)
        planButton.bezelStyle = .rounded
        planButton.controlSize = .small
        rootView.addSubview(planButton)

        askButton = NSButton(title: "Ask", target: self, action: #selector(openAsk))
        askButton.frame = NSRect(x: 408, y: 15, width: 46, height: 28)
        askButton.bezelStyle = .rounded
        askButton.controlSize = .small
        askButton.toolTip = "Ask AIWatcher from local evidence"
        rootView.addSubview(askButton)

        scanButton = NSButton(title: "Scan", target: self, action: #selector(scanNow))
        scanButton.frame = NSRect(x: 458, y: 15, width: 52, height: 28)
        scanButton.bezelStyle = .rounded
        scanButton.controlSize = .small
        scanButton.toolTip = "Force a local scan for current AIWatcher actions"
        rootView.addSubview(scanButton)

        primaryButton = NSButton(title: "Watch", target: self, action: #selector(openPrimary))
        primaryButton.frame = NSRect(x: 262, y: 15, width: 94, height: 28)
        primaryButton.bezelStyle = .rounded
        primaryButton.controlSize = .small
        primaryButton.wantsLayer = true
        primaryButton.layer?.cornerRadius = 7
        primaryButton.toolTip = "Open current AIWatcher action"
        rootView.addSubview(primaryButton)

        continueButton = NSButton(title: "Continue", target: self, action: #selector(continueHere))
        continueButton.frame = NSRect(x: 360, y: 15, width: 70, height: 28)
        continueButton.bezelStyle = .rounded
        continueButton.controlSize = .small
        continueButton.toolTip = "Continue in this session and quiet this Fresh Start nudge"
        continueButton.isHidden = true
        rootView.addSubview(continueButton)

        skipButton = NSButton(title: "Skip", target: self, action: #selector(skipCurrent))
        skipButton.frame = NSRect(x: 434, y: 15, width: 64, height: 28)
        skipButton.bezelStyle = .rounded
        skipButton.controlSize = .small
        skipButton.toolTip = "Quiet this Companion nudge without deleting the evidence"
        skipButton.isHidden = true
        rootView.addSubview(skipButton)

        consoleButton = NSButton(title: "UI", target: self, action: #selector(openDashboard))
        consoleButton.frame = NSRect(x: 464, y: 15, width: 38, height: 28)
        consoleButton.bezelStyle = .rounded
        consoleButton.controlSize = .small
        consoleButton.toolTip = "Open AIWatcher Console"
        rootView.addSubview(consoleButton)

        collapseButton = NSButton(title: "-", target: self, action: #selector(toggleCollapsed))
        collapseButton.frame = NSRect(x: 604, y: 37, width: 18, height: 18)
        collapseButton.bezelStyle = .rounded
        collapseButton.controlSize = .mini
        collapseButton.toolTip = "Minimize AIWatcher"
        rootView.addSubview(collapseButton)

        // Collapsed, the companion is a Loom-style bubble: the mark alone on a
        // white circle, no ring border and no other chrome. The mark is hosted
        // in a plain NSView rather than the button's layer -- NSButton uses
        // flipped layer geometry, which drew the mark upside down.
        collapsedMarkView = NSView(frame: NSRect(x: 12.5, y: 15, width: 27.1, height: 22))
        collapsedMarkView.wantsLayer = true
        let collapsedMark = makeBrandMark(height: 22, ink: brandInkOnLight)
        collapsedMarkView.layer?.addSublayer(collapsedMark.mark)
        collapsedBlueRing = collapsedMark.blueRing
        // Companion-only expression: the official mark stays intact everywhere
        // else, while its front ring becomes a tiny watchful viewport here.
        // The panel remains inside the ring stroke, so the silhouette and both
        // brand rings are still recognizable at 52pt.
        collapsedFacePanel = CALayer()
        collapsedFacePanel.frame = CGRect(x: 10.2, y: 3.2, width: 14.3, height: 8.2)
        collapsedFacePanel.cornerRadius = 3.0
        collapsedFacePanel.backgroundColor = brandInkOnLight.cgColor
        collapsedLeftEye = CALayer()
        collapsedLeftEye.frame = CGRect(x: 2.5, y: 4.0, width: 2.8, height: 1.45)
        collapsedLeftEye.cornerRadius = 0.72
        collapsedRightEye = CALayer()
        collapsedRightEye.frame = CGRect(x: 9.0, y: 4.0, width: 2.8, height: 1.45)
        collapsedRightEye.cornerRadius = 0.72
        collapsedFacePanel.addSublayer(collapsedLeftEye)
        collapsedFacePanel.addSublayer(collapsedRightEye)
        collapsedMarkView.layer?.addSublayer(collapsedFacePanel)
        collapsedMarkView.isHidden = true
        rootView.addSubview(collapsedMarkView)

        // The running indicator: a short mint arc orbiting the bubble's rim
        // while any session is working -- the owner's pick over the quieter
        // candidates, and the one place the bar uses continuous motion.
        // Attention still outranks it, and Reduce Motion suppresses it
        // entirely (the mint ring alone says running then).
        orbitLayer = CAShapeLayer()
        orbitLayer.bounds = CGRect(x: 0, y: 0, width: 52, height: 52)
        orbitLayer.position = CGPoint(x: 26, y: 26)
        // Inset by half the stroke so the outer edge sits on the bubble's
        // rim: a 52-point window clips anything past radius 26.
        orbitLayer.path = CGPath(ellipseIn: CGRect(x: 2.5, y: 2.5, width: 47, height: 47), transform: nil)
        orbitLayer.fillColor = NSColor.clear.cgColor
        orbitLayer.strokeColor = NSColor(calibratedRed: 0.26, green: 0.85, blue: 0.64, alpha: 1).cgColor
        // 5 wide and a third of the circle: the 2.5 x 17% arc the bar first
        // shipped with was too slight to read as "in progress" at a glance
        // (the review of the collapsed bubble asked for a bigger ring, and
        // the owner asked for a quarter more on top of the first 4).
        orbitLayer.lineWidth = 5
        orbitLayer.lineCap = .round
        orbitLayer.strokeEnd = 0.33
        orbitLayer.isHidden = true
        rootView.layer?.addSublayer(orbitLayer)

        // The attention rim: a full orange ring on the bubble's edge while
        // something needs the user. The mark's own ring is a 2.5-point stroke
        // on a 19-point box, and turning that orange was the whole of the
        // collapsed attention treatment -- a reviewer sat with a prompt held
        // at the gate and did not notice. The rim is the same 5-point circle
        // the orbit arc travels, drawn whole; hard gates pulse it, softer
        // attention holds it steady.
        rimLayer = CAShapeLayer()
        rimLayer.bounds = CGRect(x: 0, y: 0, width: 52, height: 52)
        rimLayer.position = CGPoint(x: 26, y: 26)
        rimLayer.path = CGPath(ellipseIn: CGRect(x: 2.5, y: 2.5, width: 47, height: 47), transform: nil)
        rimLayer.fillColor = NSColor.clear.cgColor
        rimLayer.strokeColor = NSColor(calibratedRed: 0.93, green: 0.42, blue: 0.14, alpha: 1).cgColor
        rimLayer.lineWidth = 5
        rimLayer.isHidden = true
        rootView.layer?.addSublayer(rimLayer)

        // Count badge at the bubble's top-right, kept fully inside the white
        // circle: at 45 degrees a 16px badge fits a radius-26 circle only if
        // its centre is within 18 of the bubble's -- hence (31, 31), not the
        // window corner. The white ground never floods; the count is the only
        // added chrome.
        collapsedBadge = NSTextField(labelWithString: "")
        collapsedBadge.frame = NSRect(x: 31, y: 31, width: 16, height: 16)
        collapsedBadge.font = NSFont.systemFont(ofSize: 9, weight: .bold)
        collapsedBadge.textColor = NSColor.white
        collapsedBadge.alignment = .center
        collapsedBadge.wantsLayer = true
        collapsedBadge.layer?.cornerRadius = 8
        collapsedBadge.isHidden = true
        rootView.addSubview(collapsedBadge)

        expandButton = DraggableButton(title: "", target: self, action: #selector(toggleCollapsed))
        expandButton.frame = NSRect(x: 0, y: 0, width: 52, height: 52)
        expandButton.isBordered = false
        expandButton.toolTip = "Open AIWatcher Companion"
        expandButton.isHidden = true
        rootView.addSubview(expandButton)

        usageButton = NSButton(title: "Usage", target: self, action: #selector(openUsage))
        usageButton.bezelStyle = .rounded
        usageButton.controlSize = .small
        usageButton.toolTip = "Inspect usage for a specific session without opening the dashboard"
        rootView.addSubview(usageButton)

        // Pre-allocated queue rows for the multi-session waiting state. Built
        // once and toggled by isHidden, matching how every other control here
        // is managed, rather than creating views per poll.
        for index in 0..<maxWaitingRows {
            let dot = NSView(frame: .zero)
            dot.wantsLayer = true
            dot.layer?.cornerRadius = 3.5
            dot.isHidden = true
            rootView.addSubview(dot)
            rowDots.append(dot)

            let label = NSTextField(labelWithString: "")
            label.font = NSFont.systemFont(ofSize: 11, weight: .medium)
            label.textColor = NSColor(calibratedRed: 0.90, green: 0.94, blue: 0.98, alpha: 1)
            label.lineBreakMode = .byTruncatingTail
            label.isHidden = true
            rootView.addSubview(label)
            rowLabels.append(label)

            // What the session is asking for, from the hook's closed
            // vocabulary -- "wants: run Bash" -- so the row says how big an
            // interruption answering is.
            let tag = NSTextField(labelWithString: "")
            tag.font = NSFont.systemFont(ofSize: 9)
            tag.textColor = NSColor(calibratedRed: 0.67, green: 0.74, blue: 0.84, alpha: 1)
            tag.alignment = .right
            tag.lineBreakMode = .byTruncatingTail
            tag.isHidden = true
            rootView.addSubview(tag)
            rowTags.append(tag)

            let button = NSButton(title: "Open", target: self, action: #selector(openWaitingRow(_:)))
            button.bezelStyle = .rounded
            button.controlSize = .small
            button.tag = index
            button.toolTip = "Open this waiting session"
            button.isHidden = true
            rootView.addSubview(button)
            rowButtons.append(button)
        }

        // Context-pressure meter for the resting state: the one graphic the
        // bar carries. A meter, not a chart -- position against the per-turn
        // limit is readable at a glance where a trend line is not.
        meterTrack = NSView(frame: .zero)
        meterTrack.wantsLayer = true
        meterTrack.layer?.cornerRadius = 2.5
        meterTrack.layer?.backgroundColor = NSColor(calibratedRed: 0.16, green: 0.20, blue: 0.27, alpha: 1).cgColor
        meterTrack.isHidden = true
        rootView.addSubview(meterTrack)
        meterFill = NSView(frame: .zero)
        meterFill.wantsLayer = true
        meterFill.layer?.cornerRadius = 2.5
        meterTrack.addSubview(meterFill)
        meterLabel = NSTextField(labelWithString: "")
        meterLabel.font = NSFont.systemFont(ofSize: 8, weight: .semibold)
        meterLabel.isHidden = true
        rootView.addSubview(meterLabel)

        // Running totals for the working session -- the absolute anchor for
        // the meter's percent. Plain muted text, no status colour: a total
        // is not a verdict.
        statsLabel = NSTextField(labelWithString: "")
        statsLabel.font = NSFont.systemFont(ofSize: 9)
        statsLabel.textColor = NSColor(calibratedRed: 0.67, green: 0.74, blue: 0.84, alpha: 1)
        statsLabel.alignment = .right
        statsLabel.isHidden = true
        rootView.addSubview(statsLabel)

        // Missed-nudge chip: the overlay-only signals (loop, velocity, runway,
        // usage pressure) vanish with the 20-second panel; the chip is where a
        // missed one lands. It borrows Plan/Ask's slot -- both remain a click
        // away in the Console, a recent signal is not.
        signalButton = NSButton(title: "", target: self, action: #selector(openSignal))
        signalButton.bezelStyle = .rounded
        signalButton.controlSize = .small
        signalButton.contentTintColor = NSColor(calibratedRed: 0.94, green: 0.62, blue: 0.15, alpha: 1)
        signalButton.isHidden = true
        rootView.addSubview(signalButton)

        setCollapsed(true)
        window.orderFrontRegardless()
        updateAppearance()
        schedulePulse()
        scheduleExpression()
        refreshState()
    }

    func usageText(_ parent: NSView, y: CGFloat, height: CGFloat = 22, size: CGFloat = 12, bold: Bool = false) -> NSTextField {
        let field = NSTextField(wrappingLabelWithString: "")
        field.frame = NSRect(x: 20, y: y, width: 440, height: height)
        field.font = NSFont.systemFont(ofSize: size, weight: bold ? .semibold : .regular)
        field.textColor = NSColor.labelColor
        parent.addSubview(field)
        return field
    }

    @objc func openUsage() {
        if usagePopover?.isShown == true {
            usagePopover?.performClose(nil)
            return
        }
        autoCollapseToken += 1
        autoCollapseDeadline = nil
        let content = NSViewController()
        content.view = UsagePanelView(frame: NSRect(x: 0, y: 0, width: 480, height: 550))
        usageHeading = usageText(content.view, y: 512, size: 18, bold: true)
        usageHeading.stringValue = "Session usage"
        usagePicker = NSPopUpButton(frame: NSRect(x: 16, y: 478, width: 448, height: 28), pullsDown: false)
        usagePicker.target = self
        usagePicker.action = #selector(selectUsageSession)
        usagePicker.addItem(withTitle: "Loading recent sessions…")
        content.view.addSubview(usagePicker)
        usageIdentity = usageText(content.view, y: 432, height: 40)
        usageContext = usageText(content.view, y: 390, height: 36, size: 24, bold: true)
        usageMeter = UsageMeter(frame: NSRect(x: 20, y: 374, width: 440, height: 10))
        content.view.addSubview(usageMeter)
        usageNote = usageText(content.view, y: 312, height: 54, size: 11)
        usageTotals = usageText(content.view, y: 282, bold: true)
        usageInput = usageText(content.view, y: 256)
        usageCache = usageText(content.view, y: 224, height: 30, size: 11)
        usageCost = usageText(content.view, y: 192, height: 30, size: 11)
        usageBoundary = usageText(content.view, y: 122, height: 60, size: 11)
        usageUpdated = usageText(content.view, y: 56, height: 60, size: 11)
        usageOpenButton = NSButton(title: "Open session in UI", target: self, action: #selector(openUsageSession))
        usageOpenButton.frame = NSRect(x: 16, y: 16, width: 170, height: 28)
        usageOpenButton.bezelStyle = .rounded
        usageOpenButton.isEnabled = false
        content.view.addSubview(usageOpenButton)
        let refresh = NSButton(title: "Refresh", target: self, action: #selector(refreshUsage))
        refresh.frame = NSRect(x: 372, y: 16, width: 92, height: 28)
        refresh.bezelStyle = .rounded
        content.view.addSubview(refresh)
        let popover = NSPopover()
        popover.behavior = .transient
        popover.contentViewController = content
        usagePopover = popover
        popover.show(relativeTo: usageButton.bounds, of: usageButton, preferredEdge: .minY)
        // Keep a user selection stable; never silently switch to another run.
        if usageSessionID.isEmpty { usageSessionID = primarySessionID }
        refreshUsage()
    }

    @objc func selectUsageSession() {
        let index = usagePicker.indexOfSelectedItem
        guard index >= 0 && index < usageRows.count else { return }
        usageSessionID = usageRows[index]["session_id"] as? String ?? ""
        refreshUsage()
    }

    @objc func openUsageSession() {
        if !usageSessionURL.isEmpty { openURL(usageSessionURL) }
    }

    @objc func refreshUsage() {
        usageRequest += 1
        let requestID = usageRequest
        var components = URLComponents(string: "\(dashboardBaseURL)/api/companion-usage")!
        components.queryItems = [URLQueryItem(name: "session_id", value: usageSessionID)]
        guard let url = components.url else { return }
        usageContext.stringValue = "Loading…"
        usageMeter.isHidden = true
        usageOpenButton.isEnabled = false
        for field in [usageIdentity, usageNote, usageTotals, usageInput, usageCache, usageCost, usageBoundary, usageUpdated] {
            field?.stringValue = ""
        }
        var request = URLRequest(url: url)
        request.timeoutInterval = 5
        URLSession.shared.dataTask(with: request) { data, response, _ in
            var payload: [String: Any]? = nil
            if let data = data { payload = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] }
            let json = payload
            DispatchQueue.main.async {
                guard self.usageRequest == requestID else { return }
                guard (response as? HTTPURLResponse)?.statusCode == 200, let json = json else {
                    self.usageContext.stringValue = "Usage unavailable"
                    self.usageNote.stringValue = "Could not reach the local dashboard. Retry with Refresh; monitoring continues independently."
                    return
                }
                self.usageRows = json["sessions"] as? [[String: Any]] ?? []
                self.usagePicker.removeAllItems()
                self.usagePicker.addItems(withTitles: self.usageRows.map { $0["label"] as? String ?? "Unknown session" })
                if self.usageRows.isEmpty { self.usagePicker.addItem(withTitle: "No recent sessions") }
                guard let selected = json["selected"] as? [String: Any] else {
                    self.usageContext.stringValue = "Usage unavailable"
                    self.usageNote.stringValue = json["reason"] as? String ?? "Session not available."
                    return
                }
                self.usageSessionID = selected["session_id"] as? String ?? ""
                if let index = self.usageRows.firstIndex(where: { $0["session_id"] as? String == self.usageSessionID }) {
                    self.usagePicker.selectItem(at: index)
                }
                let model = selected["model"] as? String ?? "Unknown model"
                let state = selected["state"] as? String ?? "unknown"
                self.usageIdentity.stringValue = "\(model) · \(state)\nSession \(self.usageSessionID)"
                self.usageIdentity.toolTip = selected["project_path"] as? String
                let context = selected["context"] as? [String: Any] ?? [:]
                let available = context["available"] as? Bool ?? false
                self.usageContext.stringValue = available ? (selected["context_label"] as? String ?? "") : "Context unavailable"
                self.usageContext.font = NSFont.systemFont(ofSize: available ? 20 : 24, weight: .semibold)
                self.usageMeter.isHidden = !available
                self.usageMeter.doubleValue = min(max((context["pct_of_turn_limit"] as? NSNumber)?.doubleValue ?? 0, 0), 100)
                self.usageNote.stringValue = available ? (selected["context_note"] as? String ?? "") : (selected["context_label"] as? String ?? "")
                self.usageTotals.stringValue = selected["usage_label"] as? String ?? ""
                self.usageInput.stringValue = selected["input_label"] as? String ?? ""
                self.usageCache.stringValue = selected["cache_label"] as? String ?? ""
                self.usageCost.stringValue = selected["cost_label"] as? String ?? ""
                self.usageBoundary.stringValue = selected["breakdown_note"] as? String ?? ""
                let updated = selected["updated_at"] as? String ?? "unknown"
                let totalsStamp = selected["totals_updated_at"] as? String ?? "unknown"
                let total = json["total_recent_sessions"] as? Int ?? 0
                self.usageUpdated.stringValue = "Last activity: \(updated)\nTotals through: \(totalsStamp)\n\(total) recent sessions · picker limited to 20"
                self.usageSessionURL = absoluteURL(selected["url"] as? String ?? "/")
                self.usageOpenButton.isEnabled = true
            }
        }.resume()
    }

    @objc func openDashboard() {
        openURL(dashboardURL)
    }

    @objc func openWaitingRow(_ sender: NSButton) {
        let index = sender.tag
        guard index >= 0, index < waitingURLs.count else { return }
        if index < waitingRowActions.count, waitingRowActions[index] == "copy_compact",
           index < waitingRowCommands.count, !waitingRowCommands[index].isEmpty {
            copyCompactRow(index: index)
            return
        }
        if index < waitingRowActions.count, waitingRowActions[index] == "copy_fresh_start",
           index < waitingSessionIDs.count, !waitingSessionIDs[index].isEmpty {
            primarySessionID = waitingSessionIDs[index]
            primaryURL = waitingURLs[index]
            copyFreshStartFromCompanion()
            return
        }
        if index < waitingReturnAvailable.count, waitingReturnAvailable[index],
           index < waitingSessionIDs.count, !waitingSessionIDs[index].isEmpty {
            requestRuntimeReturn(sessionID: waitingSessionIDs[index], fallbackURL: waitingURLs[index])
            return
        }
        openURL(waitingURLs[index])
        scheduleAutoCollapse(after: 1.5)
    }

    // Asks the dashboard to focus the blocked tool. The honesty rule from the
    // transient overlay applies here too: wait for the result, and on failure
    // say why and open the session in AIWatcher -- never report a return that
    // did not happen.
    func requestRuntimeReturn(sessionID: String, fallbackURL: String) {
        guard let url = URL(string: dashboardBaseURL + "/api/runtime-return") else {
            openURL(fallbackURL)
            return
        }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.timeoutInterval = 4
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try? JSONSerialization.data(withJSONObject: ["session_id": sessionID])
        subtitleLabel.stringValue = "Returning..."
        URLSession.shared.dataTask(with: request) { data, _, _ in
            var ok = false
            var message = ""
            if let data = data,
               let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                ok = json["ok"] as? Bool ?? false
                message = json["message"] as? String ?? ""
            }
            DispatchQueue.main.async {
                if ok {
                    self.subtitleLabel.stringValue = String((message.isEmpty ? "Opened your AI tool. Answer it there." : message).prefix(46))
                } else {
                    openURL(fallbackURL)
                    self.subtitleLabel.stringValue = "No live return. Opened in AIWatcher."
                }
                self.scheduleAutoCollapse(after: 2.5)
            }
        }.resume()
    }

    @objc func openSignal() {
        guard !signalURL.isEmpty else { return }
        openURL(signalURL)
        scheduleAutoCollapse(after: 1.5)
    }

    @objc func openPrompt() {
        openURL(promptURL)
    }

    @objc func openAsk() {
        openURL(dashboardBaseURL + "/?ask=1")
        scheduleAutoCollapse(after: 1.5)
    }

    @objc func scanNow() {
        titleLabel.stringValue = "Scanning"
        subtitleLabel.stringValue = "Checking local AI work..."
        guard let url = URL(string: dashboardBaseURL + "/api/companion-scan") else {
            return
        }
        URLSession.shared.dataTask(with: url) { data, _, _ in
            DispatchQueue.main.async {
                if let data = data,
                   let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                   let state = json["state"] as? [String: Any] {
                    self.applyState(state)
                } else {
                    self.titleLabel.stringValue = "Scan failed"
                    self.subtitleLabel.stringValue = "Open UI for details"
                    self.updateAppearance()
                }
                self.scheduleRefresh(after: 1.0)
            }
        }.resume()
    }

    @objc func openPrimary() {
        if primaryAction == "restart_aiwatcher" {
            requestRestart()
            return
        }
        if stateName == "proof_pending" {
            acknowledgeFreshStartReceipts()
            openURL(primaryURL)
            return
        }
        if primaryAction == "copy_fresh_start" && !primarySessionID.isEmpty {
            copyFreshStartFromCompanion()
            return
        }
        if primaryAction == "copy_compact" && !compactCommand.isEmpty {
            copyCompactFromCompanion()
            return
        }
        if primaryAction == "runtime_return" && !primarySessionID.isEmpty {
            requestRuntimeReturn(sessionID: primarySessionID, fallbackURL: primaryURL)
            return
        }
        if primaryAction == "open_prompt_gate" {
            scheduleAutoCollapse(after: 1.5)
        }
        openURL(primaryURL)
    }

    func requestRestart() {
        guard !restartRequestPath.isEmpty else { return }
        self.primaryButton.isEnabled = false
        self.titleLabel.stringValue = "Restarting AIWatcher"
        self.subtitleLabel.stringValue = "Dashboard and Companion will reopen"
        self.updateAppearance()
        do {
            try Data("restart\n".utf8).write(to: URL(fileURLWithPath: restartRequestPath), options: .atomic)
        } catch {
            self.titleLabel.stringValue = "Restart could not start"
            self.subtitleLabel.stringValue = "Run aiwatcher restart"
            self.primaryButton.isEnabled = true
            self.updateAppearance()
            return
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.08) {
            NSApp.terminate(nil)
        }
    }

    // The command travels in the state payload, so this is a pasteboard write
    // and a receipt -- no fetch, unlike the Fresh Start brief. The bar says
    // "Copied" only after the pasteboard reads back the exact command; the
    // dashboard takes over from the next poll, having read the click from the
    // receipt, and moves on only when the session's own log shows the /compact.
    func copyCompactFromCompanion() {
        if !writeClipboard(compactCommand) {
            self.titleLabel.stringValue = "Copy failed"
            self.subtitleLabel.stringValue = "Open UI to copy /compact"
            self.updateAppearance()
            openURL(primaryURL)
            return
        }
        recordCompactDecision("copied", sessionID: primarySessionID, sha: compactSha)
        self.stateName = "compact_copied"
        self.skipState = ""
        self.primaryURL = dashboardURL
        self.suppressPassiveRefreshUntil = Date().addingTimeInterval(3.0)
        self.titleLabel.stringValue = "Copied"
        self.subtitleLabel.stringValue = "Paste /compact into that session"
        self.updateAppearance()
        self.scheduleAutoCollapse(after: 12.0)
        self.scheduleRefresh(after: 3.0)
    }

    // One row's Copy, when several windows qualify. Only that row changes:
    // the receipt notes the click against the session the row names, and the
    // others keep their buttons.
    func copyCompactRow(index: Int) {
        let command = index < waitingRowCommands.count ? waitingRowCommands[index] : ""
        if !writeClipboard(command) {
            self.titleLabel.stringValue = "Copy failed"
            self.subtitleLabel.stringValue = "Open UI to copy /compact"
            self.updateAppearance()
            return
        }
        let sessionID = index < waitingSessionIDs.count ? waitingSessionIDs[index] : ""
        let sha = index < waitingRowShas.count ? waitingRowShas[index] : ""
        recordCompactDecision("copied", sessionID: sessionID, sha: sha)
        if index < waitingRowTags.count { waitingRowTags[index] = "copied" }
        self.suppressPassiveRefreshUntil = Date().addingTimeInterval(3.0)
        self.updateAppearance()
        self.scheduleAutoCollapse(after: 12.0)
        self.scheduleRefresh(after: 3.0)
    }

    func recordCompactDecision(_ decision: String, sessionID: String, sha: String) {
        guard let url = URL(string: dashboardBaseURL + "/api/compact-decision") else {
            return
        }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        let payload: [String: Any] = [
            "session_id": sessionID,
            "sha": sha,
            "decision": decision,
            "action_channel": "companion"
        ]
        request.httpBody = try? JSONSerialization.data(withJSONObject: payload)
        URLSession.shared.dataTask(with: request).resume()
    }

    func clipboardNeedsConfirmation(for brief: String) -> Bool {
        let current = NSPasteboard.general.string(forType: .string)?
            .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        if current.isEmpty { return false }
        if current == brief { return false }
        if current.hasPrefix("AIWatcher Fresh Start brief") { return false }
        if pendingClipboardOverrideSessionID == primarySessionID { return false }
        return true
    }

    func writeClipboard(_ value: String) -> Bool {
        let text = value.trimmingCharacters(in: .whitespacesAndNewlines)
        if text.isEmpty { return false }
        NSPasteboard.general.clearContents()
        if !NSPasteboard.general.setString(value, forType: .string) {
            return false
        }
        let current = NSPasteboard.general.string(forType: .string)?
            .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        return current == text
    }

    func copyFreshStartFromCompanion() {
        titleLabel.stringValue = "Copying brief"
        subtitleLabel.stringValue = "Preparing Fresh Start..."
        guard let encoded = primarySessionID.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed),
              let briefURL = URL(string: dashboardBaseURL + "/api/handoff?id=" + encoded + "&target=generic") else {
            openURL(primaryURL)
            return
        }
        URLSession.shared.dataTask(with: briefURL) { data, _, _ in
            DispatchQueue.main.async {
                guard let data = data,
                      let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                      let brief = json["next_brief"] as? String,
                      !brief.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
                    self.titleLabel.stringValue = "Fresh Start"
                    self.subtitleLabel.stringValue = "Open UI to copy the brief"
                    self.updateAppearance()
                    openURL(self.primaryURL)
                    return
                }
                if self.clipboardNeedsConfirmation(for: brief) {
                    self.pendingClipboardOverrideSessionID = self.primarySessionID
                    self.stateName = "clipboard_confirm"
                    self.suppressPassiveRefreshUntil = Date().addingTimeInterval(30.0)
                    self.titleLabel.stringValue = "Clipboard has text"
                    self.subtitleLabel.stringValue = "Click Replace to copy Fresh Start"
                    self.primaryButton.title = "Replace"
                    self.updateAppearance()
                    self.scheduleAutoCollapse(after: 30.0)
                    return
                }
                self.pendingClipboardOverrideSessionID = ""
                if !self.writeClipboard(brief) {
                    self.titleLabel.stringValue = "Copy failed"
                    self.subtitleLabel.stringValue = "Open UI to copy the brief"
                    self.updateAppearance()
                    openURL(self.primaryURL)
                    return
                }
                self.recordFreshStartCopied()
                if self.primaryRuntimeAvailable {
                    self.requestRuntimeReturnForPrimary()
                } else {
                    openURL(self.primaryURL)
                }
                self.stateName = "fresh_start_copied"
                self.skipState = ""
                self.continueSessionID = ""
                self.primaryURL = dashboardURL
                self.suppressPassiveRefreshUntil = Date().addingTimeInterval(12.0)
                self.titleLabel.stringValue = "Fresh Start copied"
                self.subtitleLabel.stringValue = "Paste it into a fresh chat"
                self.updateAppearance()
                self.scheduleAutoCollapse(after: 12.0)
                self.scheduleRefresh(after: 12.0)
            }
        }.resume()
    }

    func recordFreshStartCopied() {
        guard let url = URL(string: dashboardBaseURL + "/api/handoff-decision") else {
            return
        }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        let payload: [String: Any] = [
            "session_id": primarySessionID,
            "decision": "copy_handoff",
            "reason": continueReason.isEmpty ? "Fresh Start brief copied from Companion." : continueReason,
            "action_channel": "companion"
        ]
        request.httpBody = try? JSONSerialization.data(withJSONObject: payload)
        URLSession.shared.dataTask(with: request).resume()
    }

    func requestRuntimeReturnForPrimary() {
        guard let url = URL(string: dashboardBaseURL + "/api/runtime-return") else {
            return
        }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try? JSONSerialization.data(withJSONObject: ["session_id": primarySessionID])
        URLSession.shared.dataTask(with: request).resume()
    }

    func acknowledgeFreshStartReceipts() {
        guard let url = URL(string: dashboardBaseURL + "/api/handoff-receipts-viewed") else {
            return
        }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        URLSession.shared.dataTask(with: request) { _, response, error in
            DispatchQueue.main.async {
                let ok = error == nil && ((response as? HTTPURLResponse)?.statusCode ?? 500) >= 200 && ((response as? HTTPURLResponse)?.statusCode ?? 500) < 300
                if ok {
                    self.stateName = "watching"
                    self.skipState = ""
                    self.titleLabel.stringValue = "Watching quietly"
                    self.subtitleLabel.stringValue = "Fresh Start receipt opened"
                } else {
                    self.titleLabel.stringValue = "Still pending"
                    self.subtitleLabel.stringValue = "Could not save receipt view"
                }
                self.updateAppearance()
                if ok { self.scheduleAutoCollapse(after: 1.2) }
            }
        }.resume()
    }

    @objc func continueHere() {
        if stateName == "prompt_gate" && continueAction == "run_original_prompt" && !continueURL.isEmpty {
            continuePromptGate()
            return
        }
        guard !continueSessionID.isEmpty,
              let url = URL(string: dashboardBaseURL + "/api/handoff-decision") else {
            return
        }
        var payload: [String: Any] = [
            "session_id": continueSessionID,
            "decision": "continue_here",
            "reason": continueReason.isEmpty ? "User chose to keep working in the current session." : continueReason,
            "action_channel": "companion"
        ]
        if continueExpectedTokens > 0 {
            payload["expected_saved_context_tokens"] = continueExpectedTokens
        }
        guard let body = try? JSONSerialization.data(withJSONObject: payload) else {
            return
        }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = body
        URLSession.shared.dataTask(with: request) { _, response, error in
            DispatchQueue.main.async {
                let ok = error == nil && ((response as? HTTPURLResponse)?.statusCode ?? 500) >= 200 && ((response as? HTTPURLResponse)?.statusCode ?? 500) < 300
                if ok {
                    self.continueSessionID = ""
                    self.stateName = "watching"
                    self.titleLabel.stringValue = "Watching quietly"
                    self.subtitleLabel.stringValue = "Fresh Start decision saved"
                    self.primaryURL = dashboardURL
                } else {
                    self.titleLabel.stringValue = "Still pending"
                    self.subtitleLabel.stringValue = "Could not save Continue"
                }
                self.updateAppearance()
                if ok { self.scheduleAutoCollapse(after: 1.2) }
            }
        }.resume()
    }

    func continuePromptGate() {
        guard var components = URLComponents(string: continueURL) else {
            openURL(primaryURL)
            return
        }
        let basePath = components.path.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        components.path = basePath.isEmpty ? "/decision" : "/" + basePath + "/decision"
        guard let url = components.url else {
            openURL(primaryURL)
            return
        }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try? JSONSerialization.data(withJSONObject: ["decision": "run_original"])
        URLSession.shared.dataTask(with: request) { _, response, error in
            DispatchQueue.main.async {
                let status = (response as? HTTPURLResponse)?.statusCode ?? 500
                let ok = error == nil && status >= 200 && status < 300
                if ok {
                    self.stateName = "watching"
                    self.titleLabel.stringValue = "Continuing"
                    self.subtitleLabel.stringValue = "Original prompt released"
                    self.primaryURL = dashboardURL
                    self.continueAction = ""
                    self.continueURL = ""
                } else {
                    self.titleLabel.stringValue = "Still paused"
                    self.subtitleLabel.stringValue = "Open Review Gate to continue"
                }
                self.updateAppearance()
                if ok { self.scheduleAutoCollapse(after: 1.2) }
            }
        }.resume()
    }

    @objc func skipCurrent() {
        guard !skipState.isEmpty,
              let url = URL(string: dashboardBaseURL + "/api/companion-skip") else {
            return
        }
        var payload: [String: Any] = [
            "state": skipState,
            "session_id": skipSessionID,
            "project": skipProject,
            "projects": skipProjects.isEmpty ? splitLines(skipProject) : skipProjects,
            "session_ids": skipSessionIDs.isEmpty ? splitLines(skipSessionID) : skipSessionIDs
        ]
        guard let body = try? JSONSerialization.data(withJSONObject: payload) else {
            return
        }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = body
        URLSession.shared.dataTask(with: request) { _, response, error in
            DispatchQueue.main.async {
                let ok = error == nil && ((response as? HTTPURLResponse)?.statusCode ?? 500) >= 200 && ((response as? HTTPURLResponse)?.statusCode ?? 500) < 300
                if ok {
                    self.skipState = ""
                    self.skipSessionID = ""
                    self.skipProject = ""
                    self.skipProjects = []
                    self.skipSessionIDs = []
                    self.continueSessionID = ""
                    self.stateName = "watching"
                    self.titleLabel.stringValue = "Watching quietly"
                    self.subtitleLabel.stringValue = "Companion nudge skipped"
                    self.primaryURL = dashboardURL
                } else {
                    self.titleLabel.stringValue = "Still pending"
                    self.subtitleLabel.stringValue = self.shortSkipFailureMessage()
                }
                self.updateAppearance()
                if ok { self.scheduleAutoCollapse(after: 1.2) }
            }
        }.resume()
    }

    @objc func toggleCollapsed() {
        setCollapsed(!collapsed)
        if !collapsed {
            scheduleAutoCollapse(after: 10.0)
        }
    }

    func setCollapsed(_ value: Bool) {
        if collapsed == value {
            applyVisibility()
            updateAppearance()
            return
        }
        collapsed = value
        if collapsed {
            usagePopover?.performClose(nil)
            autoCollapseDeadline = nil
        }
        let targetWidth = collapsed ? collapsedWidth : expandedWidth
        let targetHeight = collapsed ? collapsedHeight : expandedHeight
        window.setFrame(anchoredFrame(width: targetWidth, height: targetHeight), display: true, animate: true)
        rootView.frame = NSRect(x: 0, y: 0, width: targetWidth, height: targetHeight)
        rootView.layer?.cornerRadius = collapsed ? targetHeight / 2 : 16
        applyVisibility()
        updateAppearance()
        scheduleExpression(after: 0.1)
    }

    // session_waiting belongs in both lists. Left out, the widget renders the
    // words "Waiting on you" in its calm style with no button -- saying
    // something urgent while looking like nothing is happening.
    // session_finished and away_digest are deliberately in the first list and
    // not the second: they earn the primary and the compact layout, but
    // "review when ready" must not wear the orange "blocked on you" treatment.
    func hasPrimaryAction() -> Bool {
        let states = ["offline", "prompt_gate", "command_gate", "control_recommended", "control_review", "context_review", "optimize_available", "clipboard_confirm", "session_waiting", "session_finished", "away_digest", "compact_recommended", "delivery_review_ready"]
        // Only the nudge step of a compaction carries a button. The later
        // steps -- copied, compacting, compacted -- are facts about a thing
        // already done, and a button would ask for it again.
        let compactWithoutButton = stateName == "compact_recommended" && primaryAction == "none"
        return states.contains(stateName) && !compactWithoutButton
    }

    func needsAttentionState() -> Bool {
        if !activityTone.isEmpty {
            return activityTone == "attention"
        }
        return ["prompt_gate", "command_gate", "control_recommended", "control_review", "optimize_available", "clipboard_confirm", "session_waiting"].contains(stateName)
    }

    // A hard gate is the one kind of attention where the user's own prompt is
    // held until they act: a hook has paused Claude Code and nothing happens
    // in the terminal until the Companion gate is reviewed. Softer attention
    // (a control to review, a session waiting) blocks nothing, so it keeps
    // the calmer treatment.
    func isHardGate() -> Bool {
        return ["prompt_gate", "command_gate"].contains(stateName)
    }

    func shouldShowWindow() -> Bool {
        if stateName == "offline" {
            return true
        }
        if needsAttentionState() {
            return true
        }
        // Soft attention: recently quiet work and the away digest force the window
        // visible even in nudges-only mode, without the orange treatment.
        if ["session_finished", "away_digest", "delivery_review_ready"].contains(stateName) {
            return true
        }
        // A commit just landed in the session being typed into: same soft
        // attention as finished work, and the same calm treatment.
        if stateName == "compact_recommended" {
            return true
        }
        if visibilityMode == "nudges-only" {
            return false
        }
        if visibilityMode == "ai-apps" {
            return foregroundLooksLikeAIWork()
        }
        return true
    }

    func applyWindowVisibility() {
        if shouldShowWindow() {
            if !window.isVisible {
                window.orderFrontRegardless()
            }
        } else if window.isVisible {
            window.orderOut(nil)
        }
    }

    func applyVisibility() {
        if collapsed {
            for view in [dragHandle, brandMarkView, titleLabel, subtitleLabel, primaryButton, continueButton, skipButton, planButton, askButton, scanButton, consoleButton, usageButton, collapseButton] {
                view?.isHidden = true
            }
            for view in rowDots { view.isHidden = true }
            for label in rowLabels { label.isHidden = true }
            for tag in rowTags { tag.isHidden = true }
            for button in rowButtons { button.isHidden = true }
            meterTrack.isHidden = true
            meterLabel.isHidden = true
            statsLabel.isHidden = true
            signalButton.isHidden = true
            collapsedMarkView.isHidden = false
            collapsedBadge.isHidden = waitingCount <= 0 && reviewCount <= 0
            expandButton.isHidden = false
            return
        }
        collapsedMarkView.isHidden = true
        collapsedBadge.isHidden = true
        // The header band keeps its one-line layout; with queue rows drawn
        // below it, every header frame shifts up by the rows area (AppKit's
        // origin is the bottom-left corner).
        let rowsShown = visibleWaitingRows
        let yOff = CGFloat(rowsShown) * rowHeight
        dragHandle.isHidden = false
        brandMarkView.isHidden = false
        titleLabel.isHidden = false
        subtitleLabel.isHidden = false
        dragHandle.frame = NSRect(x: 10, y: 21 + yOff, width: 18, height: 16)
        brandMarkView.frame = NSRect(x: 30, y: 16 + yOff, width: 32, height: 26)
        collapseButton.frame = NSRect(x: 684, y: 37 + yOff, width: 18, height: 18)
        let attention = needsAttentionState()
        // Context review queues need both scopes: a row-level Watch button and
        // a Review all button for the full Context Health list.
        let showPrimary = hasPrimaryAction() && (rowsShown == 0 || ["control_review", "context_review"].contains(stateName))
        let showChip = !signalChipText.isEmpty && !attention && !showPrimary
        planButton.isHidden = attention || showChip || showPrimary
        askButton.isHidden = attention || showChip || showPrimary
        scanButton.isHidden = attention || showPrimary
        consoleButton.isHidden = false
        usageButton.isHidden = false
        usageButton.frame = NSRect(x: 642, y: 10 + yOff, width: 58, height: 24)
        collapseButton.isHidden = false
        expandButton.isHidden = true
        let showContinue = (
            (!continueSessionID.isEmpty && stateName == "control_recommended") ||
            (stateName == "prompt_gate" && continueAction == "run_original_prompt" && !continueURL.isEmpty)
        )
        let showSkip = !skipState.isEmpty && stateName != "prompt_gate"
        primaryButton.isHidden = !showPrimary
        continueButton.isHidden = !showContinue
        skipButton.isHidden = !showSkip
        if attention || showPrimary {
            meterTrack.isHidden = true
            meterLabel.isHidden = true
            statsLabel.isHidden = true
            signalButton.isHidden = true
            titleLabel.frame = NSRect(x: 66, y: 31 + yOff, width: 236, height: 17)
            subtitleLabel.frame = NSRect(x: 66, y: 12 + yOff, width: 236, height: 16)
            primaryButton.frame = NSRect(x: 306, y: 15 + yOff, width: 100, height: 28)
            if showContinue {
                continueButton.frame = NSRect(x: 412, y: 15 + yOff, width: 70, height: 28)
                if showSkip {
                    skipButton.frame = NSRect(x: 490, y: 15 + yOff, width: 68, height: 28)
                    consoleButton.frame = NSRect(x: 564, y: 15 + yOff, width: 38, height: 28)
                } else {
                    consoleButton.frame = NSRect(x: 488, y: 15 + yOff, width: 38, height: 28)
                }
            } else {
                if showSkip {
                    skipButton.frame = NSRect(x: 434, y: 15 + yOff, width: 68, height: 28)
                    consoleButton.frame = NSRect(x: 508, y: 15 + yOff, width: 38, height: 28)
                } else {
                    consoleButton.frame = NSRect(x: 434, y: 15 + yOff, width: 38, height: 28)
                }
            }
        } else {
            let showMeter = pressureAvailable
            let showStats = showMeter && !pressureStats.isEmpty
            statsLabel.isHidden = !showStats
            if showStats {
                // Right-aligned on the title line's tail; the title is capped
                // at 18 characters, so the two never meet.
                statsLabel.frame = NSRect(x: 270, y: 33 + yOff, width: 112, height: 13)
                statsLabel.stringValue = pressureStats
                statsLabel.toolTip = pressureStatsDetail.isEmpty ? nil : pressureStatsDetail
            }
            titleLabel.frame = NSRect(x: 66, y: 31 + yOff, width: 286, height: 17)
            subtitleLabel.frame = NSRect(x: 66, y: 12 + yOff, width: showMeter ? 216 : 304, height: 16)
            meterTrack.isHidden = !showMeter
            meterLabel.isHidden = !showMeter
            if showMeter {
                // Fill clamps at 100%; the percent label does not. A 250K turn
                // is past the limit, and the number should say so.
                meterTrack.frame = NSRect(x: 288, y: 17 + yOff, width: 62, height: 5)
                let clamped = min(max(pressurePct, 0), 100)
                meterFill.frame = NSRect(x: 0, y: 0, width: 62.0 * CGFloat(clamped) / 100.0, height: 5)
                let meterColor = pressureSeverity == "critical"
                    ? NSColor(calibratedRed: 0.93, green: 0.42, blue: 0.14, alpha: 1)
                    : (pressureSeverity == "warning"
                        ? NSColor(calibratedRed: 0.94, green: 0.62, blue: 0.15, alpha: 1)
                        : NSColor(calibratedRed: 0.35, green: 0.55, blue: 0.98, alpha: 1))
                meterFill.layer?.backgroundColor = meterColor.cgColor
                meterLabel.frame = NSRect(x: 354, y: 12 + yOff, width: 28, height: 14)
                meterLabel.stringValue = "\(pressurePct)%"
                meterLabel.textColor = meterColor
                meterTrack.toolTip = pressureLabel
                meterLabel.toolTip = pressureLabel
            }
            signalButton.isHidden = !showChip
            if showChip {
                signalButton.title = signalChipText
                signalButton.frame = NSRect(x: 384, y: 15 + yOff, width: 100, height: 28)
            }
            let utilityY = 15 + yOff
            planButton.frame = NSRect(x: 384, y: utilityY, width: 48, height: 28)
            askButton.frame = NSRect(x: 436, y: utilityY, width: 46, height: 28)
            scanButton.frame = NSRect(x: 486, y: utilityY, width: 52, height: 28)
            if showSkip {
                skipButton.frame = NSRect(x: 542, y: utilityY, width: 50, height: 28)
                consoleButton.frame = NSRect(x: 596, y: utilityY, width: 38, height: 28)
            } else {
                consoleButton.frame = NSRect(x: 542, y: utilityY, width: 38, height: 28)
            }
        }
        for index in 0..<maxWaitingRows {
            let visible = index < rowsShown
            let kind = index < waitingRowKinds.count ? waitingRowKinds[index] : ""
            let action = index < waitingRowActions.count ? waitingRowActions[index] : ""
            rowDots[index].isHidden = !visible
            rowLabels[index].isHidden = !visible
            rowButtons[index].isHidden = !visible || action == "none" || (kind == "compact" && action.isEmpty)
            // Compaction rows carry their stage where waiting rows carry
            // what the session wants.
            // Prompt rows carry "working 3 min" or "done" in the same place.
            let tagged = kind == "compact" || kind.hasPrefix("prompt_")
            let tag = tagged && index < waitingRowTags.count ? waitingRowTags[index] : ""
            let wants = tagged ? "" : (index < waitingWants.count ? waitingWants[index] : "")
            rowTags[index].isHidden = !visible || (wants.isEmpty && tag.isEmpty)
            if !visible { continue }
            let rowY = yOff - rowHeight * CGFloat(index + 1)
            rowDots[index].frame = NSRect(x: 30, y: rowY + 14, width: 7, height: 7)
            // Digest rows color by kind -- mint for recently quiet work and for a
            // compaction (a good moment, not an alarm), amber for signals;
            // waiting rows keep hottest-first by position.
            let dotColor: NSColor
            if kind == "prompt_working" {
                // Work in progress is neither done nor a signal: brand blue.
                dotColor = NSColor(calibratedRed: 0.23, green: 0.51, blue: 0.96, alpha: 1)
            } else if ["finished", "recently_observed", "delivery_confirmed", "compact", "prompt_done"].contains(kind) {
                dotColor = NSColor(calibratedRed: 0.36, green: 0.79, blue: 0.65, alpha: 1)
            } else if !kind.isEmpty {
                dotColor = NSColor(calibratedRed: 0.94, green: 0.62, blue: 0.15, alpha: 1)
            } else {
                dotColor = index == 0
                    ? NSColor(calibratedRed: 0.89, green: 0.29, blue: 0.29, alpha: 1)
                    : NSColor(calibratedRed: 0.94, green: 0.62, blue: 0.15, alpha: 1)
            }
            rowDots[index].layer?.backgroundColor = dotColor.cgColor
            rowLabels[index].frame = NSRect(x: 46, y: rowY + 9, width: wants.isEmpty && !kind.hasPrefix("prompt_") ? 462 : 350, height: 17)
            let section = index < waitingRowSections.count ? waitingRowSections[index] : ""
            let text = index < waitingRowTexts.count ? waitingRowTexts[index] : ""
            rowLabels[index].stringValue = section.isEmpty ? text : "\(section.uppercased())   \(text)"
            rowTags[index].frame = NSRect(x: 400, y: rowY + 10, width: 112, height: 14)
            rowTags[index].stringValue = kind == "compact" ? tag : (kind.hasPrefix("prompt_") ? tag : (wants.isEmpty ? "" : "wants: \(wants)"))
            let canReturn = index < waitingReturnAvailable.count && waitingReturnAvailable[index]
            rowButtons[index].title = rowActionLabel(index: index)
            rowButtons[index].toolTip = kind == "compact"
                ? "Review this chat before compacting"
                : kind.hasPrefix("prompt_")
                ? "Open this chat's session review"
                : (canReturn
                    ? "Focus the blocked tool directly"
                    : (kind.isEmpty
                        ? "Tool window not reachable from here -- opens the session in AIWatcher"
                        : "Open this in AIWatcher"))
            rowButtons[index].frame = NSRect(x: 518, y: rowY + 4, width: 64, height: 26)
        }
    }

    // Grows away from the screen edge the window sits nearest -- judged from
    // where the user dragged it, not the configured start position -- then
    // clamps onto that screen. A bubble parked on the left opens rightward,
    // one on the right opens leftward, and collapsing lands it back in place.
    func anchoredFrame(width: CGFloat, height: CGFloat) -> NSRect {
        let current = window.frame
        let screen = (window.screen ?? NSScreen.main)?.visibleFrame ?? current
        let margin: CGFloat = 8
        var x = current.midX > screen.midX ? current.maxX - width : current.minX
        var y = current.midY > screen.midY ? current.maxY - height : current.minY
        x = min(max(x, screen.minX + margin), max(screen.minX + margin, screen.maxX - width - margin))
        y = min(max(y, screen.minY + margin), max(screen.minY + margin, screen.maxY - height - margin))
        return NSRect(x: x, y: y, width: width, height: height)
    }

    // The queue can change the bar's height between polls. Anchored the same
    // way setCollapsed anchors, so the corner the user parked it in stays put.
    func applyWindowSize() {
        if collapsed { return }
        let target = expandedHeight
        if abs(window.frame.height - target) < 0.5 { return }
        window.setFrame(anchoredFrame(width: expandedWidth, height: target), display: true, animate: true)
        rootView.frame = NSRect(x: 0, y: 0, width: expandedWidth, height: target)
    }

    func updateWatcherExpression(blink: Bool = false) {
        let reduceMotion = NSWorkspace.shared.accessibilityDisplayShouldReduceMotion
        let needsAttention = needsAttentionState()
        var gaze = CGPoint.zero
        if isUnreachableState {
            gaze = CGPoint(x: 0, y: -0.9)
        } else if needsAttention {
            // The badge and action surface live above and to the right of the
            // viewport. Looking there makes the motion directional, not merely
            // decorative.
            gaze = CGPoint(x: 1.0, y: 0.8)
        } else if stateName == "session_finished" {
            gaze = CGPoint(x: 0, y: 0.7)
        } else if workingCount > 0 && !reduceMotion && motionMode == "expressive" {
            // A calm left/down/right scan communicates active observation more
            // specifically than another loading spinner.
            let scan = [
                CGPoint(x: -1.0, y: 0.2),
                CGPoint(x: 0.0, y: -0.8),
                CGPoint(x: 1.0, y: 0.1),
                CGPoint(x: 0.0, y: 0.5),
            ]
            gaze = scan[expressionPhase % scan.count]
        }

        let eyeColor: NSColor
        if isUnreachableState {
            eyeColor = NSColor(calibratedRed: 0.50, green: 0.56, blue: 0.64, alpha: 1)
        } else if needsAttention {
            eyeColor = NSColor(calibratedRed: 1.00, green: 0.84, blue: 0.68, alpha: 1)
        } else if stateName == "session_finished" {
            eyeColor = NSColor(calibratedRed: 0.50, green: 0.86, blue: 1.00, alpha: 1)
        } else if workingCount > 0 {
            eyeColor = NSColor(calibratedRed: 0.47, green: 0.94, blue: 0.76, alpha: 1)
        } else {
            eyeColor = NSColor(calibratedRed: 0.50, green: 0.86, blue: 1.00, alpha: 1)
        }

        CATransaction.begin()
        CATransaction.setAnimationDuration((reduceMotion || motionMode == "quiet") ? 0 : 0.22)
        collapsedFacePanel.opacity = isUnreachableState ? 0.62 : 1.0
        collapsedLeftEye.backgroundColor = eyeColor.cgColor
        collapsedRightEye.backgroundColor = eyeColor.cgColor
        collapsedLeftEye.frame = CGRect(x: 2.5 + gaze.x, y: 4.0 + gaze.y, width: 2.8, height: 1.45)
        collapsedRightEye.frame = CGRect(x: 9.0 + gaze.x, y: 4.0 + gaze.y, width: 2.8, height: 1.45)
        CATransaction.commit()

        if blink && !reduceMotion && motionMode != "quiet" && workingCount == 0 && !needsAttention && !isUnreachableState {
            let close = CAKeyframeAnimation(keyPath: "transform.scale.y")
            close.values = [1.0, 0.12, 1.0]
            close.keyTimes = [0.0, 0.45, 1.0]
            close.duration = 0.22
            collapsedLeftEye.add(close, forKey: "blink")
            collapsedRightEye.add(close, forKey: "blink")
        }
    }

    func playWatcherTransition(previousState: String, previousWorkingCount: Int, previousDeliveryReceiptID: String, previousNeedsAttention: Bool) {
        if NSWorkspace.shared.accessibilityDisplayShouldReduceMotion || motionMode == "quiet" { return }
        // Keep the expanded product mark completely canonical. Expressive
        // motion belongs only to the compact Companion surface.
        if !collapsed { return }
        let enteredAttention = needsAttentionState() && !previousNeedsAttention
        let activityChanged = (previousWorkingCount > 0) != (workingCount > 0)
        let delivered = !deliveryReceiptID.isEmpty && deliveryReceiptID != previousDeliveryReceiptID
        let recovered = previousState == "offline" && stateName != "offline"
        if !enteredAttention && !activityChanged && !delivered && !recovered { return }

        let focus = CAKeyframeAnimation(keyPath: "transform.scale")
        focus.values = [1.0, 1.13, 0.97, 1.0]
        focus.keyTimes = [0.0, 0.38, 0.72, 1.0]
        focus.duration = 0.46
        focus.timingFunction = CAMediaTimingFunction(name: .easeInEaseOut)
        collapsedMarkView.layer?.add(focus, forKey: "watcher-focus-lock")
    }

    func scheduleExpression(after delay: Double = 1.1) {
        expressionScheduleToken += 1
        let token = expressionScheduleToken
        DispatchQueue.main.asyncAfter(deadline: .now() + delay) {
            if token != self.expressionScheduleToken { return }
            let reduceMotion = NSWorkspace.shared.accessibilityDisplayShouldReduceMotion
            let expressive = self.collapsed && self.window.isVisible && !reduceMotion && self.motionMode == "expressive"
            if expressive {
                self.expressionPhase += 1
                let resting = self.workingCount == 0 && !self.needsAttentionState() && self.stateName != "offline"
                self.updateWatcherExpression(blink: resting)
            }
            let activelyScanning = expressive && self.workingCount > 0 && self.stateName != "session_finished"
            let dormant = !self.collapsed || !self.window.isVisible || reduceMotion || self.motionMode == "quiet"
            self.scheduleExpression(after: activelyScanning ? 1.1 : (dormant ? 30.0 : 8.0))
        }
    }

    func updateAppearance() {
        applyWindowSize()
        applyVisibility()
        let needsAttention = needsAttentionState()
        let dark = NSColor(calibratedRed: 0.035, green: 0.052, blue: 0.078, alpha: 0.94).cgColor
        let orangeColor = pulseOn
            ? NSColor(calibratedRed: 0.93, green: 0.42, blue: 0.14, alpha: 0.96)
            : NSColor(calibratedRed: 0.72, green: 0.27, blue: 0.09, alpha: 0.94)
        // Collapsed, the bubble stays white in every state. Attention is
        // carried by the mark itself -- the blue ring is the one that turns
        // orange and pulses, the same job it does in the dashboard favicon --
        // rather than by flooding the ground.
        rootView.layer?.backgroundColor = collapsed
            ? NSColor(calibratedRed: 1.00, green: 1.00, blue: 1.00, alpha: 0.97).cgColor
            : dark
        rootView.layer?.borderWidth = collapsed ? 0 : 1
        rootView.layer?.borderColor = needsAttention
            ? NSColor(calibratedRed: 1.00, green: 0.63, blue: 0.24, alpha: 0.95).cgColor
            : NSColor(calibratedRed: 0.34, green: 0.52, blue: 0.74, alpha: 0.72).cgColor
        primaryButton.layer?.backgroundColor = needsAttention ? orangeColor.cgColor : NSColor.clear.cgColor
        primaryButton.contentTintColor = needsAttention ? NSColor.white : NSColor.controlTextColor
        // The blue ring is the mark's state channel, speaking the dashboard
        // favicon's vocabulary: orange when something needs you, mint while a
        // session is actively working (#43d9a3, the favicon's healthy-live
        // colour), brand blue at rest. Mint does not pulse -- running is a
        // status, not an alarm.
        let runningMint = NSColor(calibratedRed: 0.26, green: 0.85, blue: 0.64, alpha: 1)
        let ringColor = needsAttention ? orangeColor : (workingCount > 0 ? runningMint : brandBlue)
        collapsedBlueRing.borderColor = ringColor.cgColor
        brandBlueRing.borderColor = ringColor.cgColor
        let reduceMotion = NSWorkspace.shared.accessibilityDisplayShouldReduceMotion
        let showOrbit = collapsed && window.isVisible && workingCount > 0 && !needsAttention
        let animateOrbit = showOrbit && !reduceMotion && motionMode != "quiet"
        orbitLayer.isHidden = !showOrbit
        if animateOrbit && orbitLayer.animation(forKey: "orbit") == nil {
            let spin = CABasicAnimation(keyPath: "transform.rotation.z")
            spin.fromValue = 0
            spin.toValue = -2 * Double.pi
            spin.duration = 2.8
            spin.repeatCount = .infinity
            orbitLayer.add(spin, forKey: "orbit")
        } else if !animateOrbit {
            orbitLayer.removeAnimation(forKey: "orbit")
        }
        // The rim outranks the orbit arc the way attention outranks running.
        // A hard gate pulses it between full and faint on the same tick as
        // the button; with Reduce Motion on it holds at full, still orange,
        // still a whole ring.
        let hardGate = isHardGate()
        rimLayer.isHidden = !collapsed || !needsAttention
        rimLayer.opacity = (hardGate && !reduceMotion && motionMode != "quiet" && !pulseOn) ? 0.3 : 1.0
        // The collapsed bubble reserves numbers for things needing attention.
        // Completed work is reward/status text after expansion, not a mystery
        // count sitting on screen all day. A hard gate carries no count, and
        // was the one attention state with nothing on the bubble but the
        // mark's ring: it gets a "!" so the blocked state is never the
        // quietest one.
        collapsedBadge.stringValue = waitingCount > 0
            ? String(waitingCount)
            : (reviewCount > 0 ? String(reviewCount) : (hardGate ? "!" : ""))
        collapsedBadge.layer?.backgroundColor = ((waitingCount > 0 || needsAttention) ? orangeColor : brandBlue).cgColor
        if collapsed {
            collapsedBadge.isHidden = waitingCount <= 0 && reviewCount <= 0 && !hardGate
        }
        updateWatcherExpression()
        applyWindowVisibility()
    }

    func schedulePulse(after delay: Double = 0.8) {
        DispatchQueue.main.asyncAfter(deadline: .now() + delay) {
            let reduceMotion = NSWorkspace.shared.accessibilityDisplayShouldReduceMotion
            let animate = self.window.isVisible && self.isHardGate() && !reduceMotion && self.motionMode != "quiet"
            if animate {
                self.pulseOn.toggle()
                self.updateAppearance()
            } else if self.pulseOn {
                self.pulseOn = false
                self.updateAppearance()
            }
            let dormant = !self.window.isVisible || reduceMotion || self.motionMode == "quiet"
            self.schedulePulse(after: animate ? 0.8 : (dormant ? 30.0 : 10.0))
        }
    }

    func refreshState() {
        guard let url = URL(string: stateURL) else {
            scheduleRefresh()
            return
        }
        URLSession.shared.dataTask(with: url) { data, _, _ in
            guard let data = data,
                  let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
                DispatchQueue.main.async {
                    self.probeFailures += 1
                    let unreachableSince = self.lastProbeOK ?? self.probeSince
                    let budgetSpent = self.probeFailures >= Self.offlineAfterFailures
                    let outlivedScan = Date().timeIntervalSince(unreachableSince) >= Self.offlineAfterSeconds
                    if budgetSpent && outlivedScan {
                        self.applyOfflineState()
                    } else {
                        self.applyConnectingState()
                    }
                    self.scheduleRefresh(after: 3.0)
                }
                return
            }
            DispatchQueue.main.async {
                self.probeFailures = 0
                self.lastProbeOK = Date()
                self.applyState(json)
                self.scheduleAutoCollapse(after: self.hasPrimaryAction() ? 10.0 : 4.0)
                self.scheduleRefresh(after: self.hasPrimaryAction() ? 2.0 : 3.0)
            }
        }.resume()
    }

    func applyState(_ json: [String: Any]) {
        self.didExpandOffline = false
        self.planButton.isEnabled = true
        self.askButton.isEnabled = true
        self.scanButton.isEnabled = true
        self.consoleButton.isEnabled = true
        self.primaryButton.isEnabled = true
        let incomingState = json["state"] as? String ?? "watching"
        if let hold = suppressPassiveRefreshUntil,
           hold > Date(),
           !["prompt_gate", "command_gate"].contains(incomingState),
           ["fresh_start_copied", "clipboard_confirm", "compact_copied"].contains(self.stateName) {
            self.updateAppearance()
            return
        }
        if let hold = suppressPassiveRefreshUntil, hold <= Date() {
            suppressPassiveRefreshUntil = nil
        }
        let previousState = self.stateName
        let previousWorkingCount = self.workingCount
        let previousDeliveryReceiptID = self.deliveryReceiptID
        let previousNeedsAttention = needsAttentionState()
        let wasHardGate = isHardGate()
        self.stateName = incomingState
        self.detailText = json["detail"] as? String ?? ""
        // New payloads carry all live categories at once. Older dashboards
        // still fall back to the winning state's single row collection.
        var waiting = json["waiting_sessions"] as? [[String: Any]] ?? []
        if waiting.isEmpty {
            waiting = json["digest_rows"] as? [[String: Any]] ?? []
        }
        if incomingState == "compact_recommended" {
            waiting = json["compact_rows"] as? [[String: Any]] ?? []
        }
        var sectionLabels: [String] = []
        var activitySectionCount = 0
        if let sections = json["activity_sections"] as? [[String: Any]] {
            let activeSections = sections.filter { (($0["rows"] as? [[String: Any]])?.isEmpty == false) }
            activitySectionCount = activeSections.count
            var grouped: [[String: Any]] = []
            for (sectionIndex, section) in activeSections.enumerated() {
                let label = section["label"] as? String ?? ""
                let rows = section["rows"] as? [[String: Any]] ?? []
                let remainingSections = activeSections.count - sectionIndex - 1
                let allowance = max(1, maxWaitingRows - grouped.count - remainingSections)
                for (index, source) in rows.prefix(allowance).enumerated() {
                    var row = source
                    row["_section_label"] = index == 0 ? label : ""
                    grouped.append(row)
                }
            }
            if !grouped.isEmpty { waiting = grouped }
        }
        sectionLabels = waiting.prefix(maxWaitingRows).map { row in
            (row["_section_label"] as? String) ?? ""
        }
        self.waitingRowSections = sectionLabels
        self.waitingRowTags = waiting.prefix(maxWaitingRows).map { row in
            String(((row["tag"] as? String) ?? "").prefix(16))
        }
        self.waitingRowActions = waiting.prefix(maxWaitingRows).map { row in
            let normalized = row["normalized_action"] as? [String: Any]
            return (normalized?["type"] as? String) ?? (row["action"] as? String) ?? ""
        }
        self.waitingRowActionLabels = waiting.prefix(maxWaitingRows).map { row in
            let normalized = row["normalized_action"] as? [String: Any]
            return (normalized?["label"] as? String) ?? ""
        }
        self.waitingRowCommands = waiting.prefix(maxWaitingRows).map { row in
            (row["command"] as? String) ?? ""
        }
        self.waitingRowShas = waiting.prefix(maxWaitingRows).map { row in
            (row["sha"] as? String) ?? ""
        }
        self.waitingURLs = waiting.prefix(maxWaitingRows).map { row in
            let normalized = row["normalized_action"] as? [String: Any]
            return absoluteURL((normalized?["url"] as? String) ?? (row["url"] as? String) ?? "/")
        }
        self.waitingSessionIDs = waiting.prefix(maxWaitingRows).map { row in
            let normalized = row["normalized_action"] as? [String: Any]
            return (normalized?["session_id"] as? String) ?? (row["session_id"] as? String) ?? ""
        }
        self.waitingReturnAvailable = waiting.prefix(maxWaitingRows).map { row in
            let normalized = row["normalized_action"] as? [String: Any]
            return ((normalized?["type"] as? String) == "runtime_return") || ((row["return_available"] as? Bool) ?? false)
        }
        self.waitingWants = waiting.prefix(maxWaitingRows).map { row in
            String(((row["wants"] as? String) ?? "").prefix(16))
        }
        self.waitingRowKinds = waiting.prefix(maxWaitingRows).map { row in
            (row["kind"] as? String) ?? ""
        }
        let reviewKinds = ["control_review", "context_review", "session_finished"]
        self.waitingRowTexts = waiting.prefix(maxWaitingRows).map { row in
            rowDisplayText(row)
        }
        let preferences = json["companion_preferences"] as? [String: Any]
        self.motionMode = preferences?["motion_mode"] as? String ?? "standard"
        self.activityTone = json["tone"] as? String ?? ""
        self.deliveryConfirmed = json["delivery_confirmed"] as? Bool ?? false
        self.deliveryReceiptID = json["delivery_receipt_id"] as? String ?? ""
        let presence = json["presence"] as? [String: Any]
        self.workingCount = presence?["working"] as? Int ?? 0
        let finishedList = json["finished_sessions"] as? [[String: Any]] ?? []
        let digestList = json["digest_rows"] as? [[String: Any]] ?? []
        if json.keys.contains("badge") {
            if let badge = json["badge"] as? [String: Any] {
                let count = badge["count"] as? Int ?? 0
                let tone = badge["tone"] as? String ?? "info"
                // A compact nudge is something to do, so it earns the calm
                // blue count on the collapsed bubble the review states get;
                // finished work never does.
                if ["control_review", "context_review", "compact_recommended"].contains(incomingState) {
                    self.waitingCount = 0
                    self.finishedCount = 0
                    self.reviewCount = count
                } else {
                    self.waitingCount = tone == "attention" ? count : 0
                    self.finishedCount = tone == "attention" ? 0 : count
                    self.reviewCount = 0
                }
            } else {
                self.waitingCount = 0
                self.finishedCount = 0
                self.reviewCount = 0
            }
        } else {
            self.waitingCount = presence?["waiting"] as? Int ?? self.waitingRowTexts.count
            self.finishedCount = finishedList.isEmpty ? digestList.count : finishedList.count
            self.reviewCount = 0
        }
        let pressure = json["pressure"] as? [String: Any]
        self.pressureAvailable = pressure?["available"] as? Bool ?? false
        self.pressurePct = pressure?["pct_of_turn_limit"] as? Int ?? 0
        self.pressureSeverity = pressure?["severity"] as? String ?? "ok"
        self.pressureLabel = pressure?["label"] as? String ?? ""
        self.pressureStats = String((pressure?["stats_label"] as? String ?? "").prefix(20))
        self.pressureStatsDetail = pressure?["stats_detail"] as? String ?? ""
        if let signal = json["recent_signal"] as? [String: Any] {
            self.signalChipText = String((signal["chip"] as? String ?? "").prefix(14))
            self.signalURL = absoluteURL((signal["url"] as? String) ?? "/")
        } else {
            self.signalChipText = ""
            self.signalURL = ""
        }
        // 40 is a sanity bound, not the fit; the frame and the ellipsis are.
        // The longest label the preflight planner emits is 32.
        self.titleLabel.stringValue = String((json["label"] as? String ?? "AIWatcher").prefix(40))
        var subtitleText = String((json["subtitle"] as? String ?? "Watching quietly").prefix(46))
        if activitySectionCount > 1 {
            subtitleText = String((json["activity_line"] as? String ?? subtitleText).prefix(46))
        }
        if ["prompt_gate", "command_gate"].contains(incomingState), let remaining = json["expires_in_seconds"] as? Int, remaining >= 0 {
            subtitleText += " · \(remaining)s"
        }
        self.subtitleLabel.stringValue = subtitleText
        self.expandButton.setAccessibilityLabel("AIWatcher Companion")
        self.expandButton.setAccessibilityValue(String((json["activity_line"] as? String ?? subtitleText).prefix(120)))
        if reviewKinds.contains(incomingState), visibleWaitingRows > 0 {
            self.titleLabel.stringValue = reviewTitle()
            self.subtitleLabel.stringValue = reviewSubtitle()
        }
        // The payload has always shipped a second explanatory sentence; the
        // bar never drew it. A tooltip costs no pixels.
        let tip = self.detailText.isEmpty ? nil : self.detailText
        self.titleLabel.toolTip = tip
        self.subtitleLabel.toolTip = tip
        self.primaryButton.title = String((json["primary_label"] as? String ?? "Watch").prefix(12))
        self.primaryAction = json["primary_action"] as? String ?? "open_url"
        self.primarySessionID = json["primary_session_id"] as? String ?? ""
        self.compactCommand = json["compact_command"] as? String ?? ""
        self.compactSha = json["compact_sha"] as? String ?? ""
        self.primaryRuntimeAvailable = json["primary_runtime_available"] as? Bool ?? false
        self.primaryURL = absoluteURL(json["primary_url"] as? String ?? "/")
        self.continueButton.title = String((json["continue_label"] as? String ?? "Continue").prefix(10))
        self.continueAction = json["continue_action"] as? String ?? ""
        self.continueURL = absoluteURL(json["continue_url"] as? String ?? "")
        self.continueSessionID = json["continue_session_id"] as? String ?? ""
        self.continueReason = json["continue_reason"] as? String ?? ""
        self.continueExpectedTokens = json["continue_expected_saved_context_tokens"] as? Int ?? 0
        self.skipButton.title = String((json["skip_label"] as? String ?? "Skip").prefix(10))
        self.skipState = json["skip_state"] as? String ?? ""
        self.skipSessionID = json["skip_session_id"] as? String ?? ""
        self.skipProject = json["skip_project"] as? String ?? ""
        self.skipProjects = (json["skip_projects"] as? [String]) ?? []
        self.skipSessionIDs = (json["skip_session_ids"] as? [String]) ?? []
        // Entering a hard gate opens the bar itself: the reason and the
        // Review button are what tell the user why their prompt is hanging,
        // and the bubble alone could only say "something". Once per gate --
        // a user who collapses it again is not fought, and the rim and badge
        // keep saying it.
        if isHardGate() && !wasHardGate && collapsed {
            setCollapsed(false)
        } else {
            self.updateAppearance()
        }
        playWatcherTransition(
            previousState: previousState,
            previousWorkingCount: previousWorkingCount,
            previousDeliveryReceiptID: previousDeliveryReceiptID,
            previousNeedsAttention: previousNeedsAttention
        )
    }

    func scheduleAutoCollapse(after delay: Double) {
        if collapsed || usagePopover?.isShown == true {
            return
        }
        let deadline = Date().addingTimeInterval(delay)
        if let existing = autoCollapseDeadline, existing <= deadline {
            return
        }
        autoCollapseDeadline = deadline
        autoCollapseToken += 1
        let token = autoCollapseToken
        DispatchQueue.main.asyncAfter(deadline: .now() + delay) {
            if token == self.autoCollapseToken && !self.collapsed {
                self.autoCollapseDeadline = nil
                // A pending hard gate keeps the bar open: folding the reason
                // away ten seconds after showing it is how the gate got
                // missed. Clearing the deadline lets the next refresh
                // schedule again once the gate resolves.
                if self.isHardGate() {
                    return
                }
                self.setCollapsed(true)
            }
        }
    }

    func scheduleRefresh(after delay: Double = 3.0) {
        DispatchQueue.main.asyncAfter(deadline: .now() + delay) {
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


def _runtime_return_result(base: str, session_id: str) -> dict[str, object]:
    """Like _request_runtime_return, but keeps the reason.

    A button whose whole job is returning you to the session has to be able to
    say why it could not, so the bool is not enough here.
    """
    if not session_id:
        return {"ok": False, "message": "no session id"}
    try:
        return _request_json(f"{base}/api/runtime-return", {"session_id": session_id})
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        return {"ok": False, "message": str(exc)}


def _request_runtime_return(base: str, session_id: str) -> bool:
    if not session_id:
        return False
    try:
        result = _request_json(f"{base}/api/runtime-return", {"session_id": session_id})
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        return False
    return bool(result.get("ok"))


def _anchored_origin(
    x: int,
    y: int,
    width: int,
    height: int,
    target_width: int,
    target_height: int,
    screen_width: int,
    screen_height: int,
    margin: int = 8,
) -> tuple[int, int]:
    """Top-left corner for resizing a window in place, in Tk's y-down space.

    The window grows away from the screen edge it sits nearest -- a bubble
    dragged to the right expands leftward, one on the left expands rightward --
    judged from where it is now, not the configured start position, so a
    dragged bubble never opens off screen. The result is then clamped on screen.
    """
    new_x = x + width - target_width if x + width / 2 > screen_width / 2 else x
    new_y = y + height - target_height if y + height / 2 > screen_height / 2 else y
    new_x = min(max(new_x, margin), max(margin, screen_width - target_width - margin))
    new_y = min(max(new_y, margin), max(margin, screen_height - target_height - margin))
    return int(new_x), int(new_y)


def _set_window_position(root: object, width: int, height: int) -> None:
    screen_width = int(root.winfo_screenwidth())  # type: ignore[attr-defined]
    screen_height = int(root.winfo_screenheight())  # type: ignore[attr-defined]
    x = max(16, screen_width - width - 28)
    y = max(16, screen_height - height - 92)
    root.geometry(f"{width}x{height}+{x}+{y}")  # type: ignore[attr-defined]


MACOS_SWIFT_TRAY = r'''
import Cocoa
import Foundation

let dashboardURL = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "http://127.0.0.1:8765"
let promptURL = CommandLine.arguments.count > 2 ? CommandLine.arguments[2] : dashboardURL + "/?view=prompt"

func openURL(_ value: String) {
    if let url = URL(string: value) {
        NSWorkspace.shared.open(url)
    }
}

func postScan() {
    guard let url = URL(string: dashboardURL.trimmingCharacters(in: CharacterSet(charactersIn: "/")) + "/api/companion-scan") else { return }
    var request = URLRequest(url: url)
    request.httpMethod = "GET"
    URLSession.shared.dataTask(with: request).resume()
}

final class TrayDelegate: NSObject, NSApplicationDelegate {
    var item: NSStatusItem!

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        item.button?.title = "AIW"
        item.button?.toolTip = "AIWatcher Local"

        let menu = NSMenu()
        menu.addItem(NSMenuItem(title: "Open Console", action: #selector(openConsole), keyEquivalent: ""))
        menu.addItem(NSMenuItem(title: "Plan Prompt", action: #selector(openPrompt), keyEquivalent: ""))
        menu.addItem(NSMenuItem(title: "Scan Now", action: #selector(scanNow), keyEquivalent: ""))
        menu.addItem(NSMenuItem.separator())
        menu.addItem(NSMenuItem(title: "Quit AIWatcher Menu", action: #selector(quit), keyEquivalent: "q"))
        for entry in menu.items { entry.target = self }
        item.menu = menu
    }

    @objc func openConsole() { openURL(dashboardURL) }
    @objc func openPrompt() { openURL(promptURL) }
    @objc func scanNow() { postScan() }
    @objc func quit() { NSApp.terminate(nil) }
}

let app = NSApplication.shared
let delegate = TrayDelegate()
app.delegate = delegate
app.run()
'''


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


def _restart_command(url: str, position: str, visibility: str, interval: int = 30) -> list[str]:
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname or "127.0.0.1"
    if host in {"0.0.0.0", "::"}:
        host = "127.0.0.1"
    port = parsed.port or 8765
    return [
        sys.executable,
        "-m",
        "aiwatcher_cli",
        "restart",
        "--from-companion",
        "--interval",
        str(max(15, int(interval))),
        "--ui-host",
        host,
        "--ui-port",
        str(port),
        "--presence-position",
        position,
        "--presence-visibility",
        visibility,
    ]


def _launch_aiwatcher_restart(url: str, position: str, visibility: str, interval: int = 30) -> bool:
    package_root = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
    env = os.environ.copy()
    env["PYTHONPATH"] = package_root + os.pathsep + env.get("PYTHONPATH", "")
    kwargs: dict[str, object] = {"start_new_session": sys.platform != "win32"}
    if sys.platform == "win32":
        kwargs["creationflags"] = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
    log_path = os.path.join(tempfile.gettempdir(), "aiwatcher-restart.log")
    try:
        with open(log_path, "ab") as log:
            subprocess.Popen(
                _restart_command(url, position, visibility, interval),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                cwd=package_root,
                env=env,
                **kwargs,
            )
        return True
    except OSError as exc:
        print(f"AIWatcher restart unavailable: {exc}", file=sys.stderr)
        return False


def _consume_restart_request(path: str, url: str, position: str, visibility: str, interval: int = 30) -> bool:
    try:
        with open(path, encoding="utf-8") as handle:
            requested = handle.read().strip() == "restart"
    except OSError:
        requested = False
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    return requested and _launch_aiwatcher_restart(url, position, visibility, interval)


def _run_macos_swift_presence(url: str, prompt_url: str, position: str, visibility: str, interval: int = 30) -> int:
    swift = shutil.which("swift")
    if sys.platform != "darwin" or not swift:
        return 2
    script_path = os.path.join(tempfile.gettempdir(), "aiwatcher-native-presence.swift")
    request_path = os.path.join(
        tempfile.gettempdir(),
        f"aiwatcher-restart-{os.getpid()}-{time.time_ns()}.request",
    )
    try:
        with open(script_path, "w", encoding="utf-8") as handle:
            handle.write(MACOS_SWIFT_PRESENCE)
        completed = subprocess.run(
            [swift, script_path, url, prompt_url, position, visibility, str(max(15, int(interval))), request_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if _consume_restart_request(request_path, url, position, visibility, interval):
            return 0
        return int(completed.returncode)
    except OSError as exc:
        print(f"AIWatcher presence unavailable: {exc}", file=sys.stderr)
        return 2
    finally:
        try:
            os.unlink(request_path)
        except OSError:
            pass


def _run_macos_swift_tray(url: str, prompt_url: str) -> int:
    swift = shutil.which("swift")
    if sys.platform != "darwin" or not swift:
        return 2
    script_path = os.path.join(tempfile.gettempdir(), "aiwatcher-native-tray.swift")
    try:
        with open(script_path, "w", encoding="utf-8") as handle:
            handle.write(MACOS_SWIFT_TRAY)
        completed = subprocess.run(
            [swift, script_path, url, prompt_url],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return int(completed.returncode)
    except OSError as exc:
        print(f"AIWatcher menu bar unavailable: {exc}", file=sys.stderr)
        return 2


def _run_windows_tray(url: str, prompt_url: str) -> int:
    if sys.platform != "win32":
        return 2
    try:
        import ctypes
        from ctypes import wintypes
    except Exception as exc:  # pragma: no cover - platform dependent
        print(f"AIWatcher tray unavailable: {exc}", file=sys.stderr)
        return 2

    user32 = ctypes.windll.user32
    shell32 = ctypes.windll.shell32
    kernel32 = ctypes.windll.kernel32

    WM_DESTROY = 0x0002
    WM_COMMAND = 0x0111
    WM_USER = 0x0400
    WM_TRAY = WM_USER + 20
    WM_RBUTTONUP = 0x0205
    WM_LBUTTONUP = 0x0202
    NIF_MESSAGE = 0x00000001
    NIF_ICON = 0x00000002
    NIF_TIP = 0x00000004
    NIM_ADD = 0x00000000
    NIM_DELETE = 0x00000002
    TPM_RIGHTBUTTON = 0x0002
    MF_STRING = 0x0000
    IDI_APPLICATION = 32512
    SW_SHOWNORMAL = 1
    IDM_CONSOLE = 1001
    IDM_PROMPT = 1002
    IDM_SCAN = 1003
    IDM_QUIT = 1004

    class WNDCLASS(ctypes.Structure):
        _fields_ = [
            ("style", wintypes.UINT),
            ("lpfnWndProc", ctypes.c_void_p),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", wintypes.HINSTANCE),
            ("hIcon", wintypes.HICON),
            ("hCursor", wintypes.HCURSOR),
            ("hbrBackground", wintypes.HBRUSH),
            ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR),
        ]

    class NOTIFYICONDATA(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("hWnd", wintypes.HWND),
            ("uID", wintypes.UINT),
            ("uFlags", wintypes.UINT),
            ("uCallbackMessage", wintypes.UINT),
            ("hIcon", wintypes.HICON),
            ("szTip", wintypes.WCHAR * 128),
        ]

    WNDPROC = ctypes.WINFUNCTYPE(wintypes.LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

    def open_url(value: str) -> None:
        shell32.ShellExecuteW(None, "open", value, None, None, SW_SHOWNORMAL)

    def scan_now() -> None:
        try:
            _request_json(f"{url.rstrip('/')}/api/companion-scan")
        except (OSError, urllib.error.URLError, json.JSONDecodeError):
            pass

    def wndproc(hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        if msg == WM_TRAY and lparam in {WM_RBUTTONUP, WM_LBUTTONUP}:
            menu = user32.CreatePopupMenu()
            user32.AppendMenuW(menu, MF_STRING, IDM_CONSOLE, "Open Console")
            user32.AppendMenuW(menu, MF_STRING, IDM_PROMPT, "Plan Prompt")
            user32.AppendMenuW(menu, MF_STRING, IDM_SCAN, "Scan Now")
            user32.AppendMenuW(menu, MF_STRING, IDM_QUIT, "Quit AIWatcher Tray")
            point = wintypes.POINT()
            user32.GetCursorPos(ctypes.byref(point))
            user32.SetForegroundWindow(hwnd)
            user32.TrackPopupMenu(menu, TPM_RIGHTBUTTON, point.x, point.y, 0, hwnd, None)
            user32.DestroyMenu(menu)
            return 0
        if msg == WM_COMMAND:
            command = int(wparam) & 0xFFFF
            if command == IDM_CONSOLE:
                open_url(url)
            elif command == IDM_PROMPT:
                open_url(prompt_url)
            elif command == IDM_SCAN:
                scan_now()
            elif command == IDM_QUIT:
                user32.DestroyWindow(hwnd)
            return 0
        if msg == WM_DESTROY:
            nid = NOTIFYICONDATA()
            nid.cbSize = ctypes.sizeof(NOTIFYICONDATA)
            nid.hWnd = hwnd
            nid.uID = 1
            shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    proc = WNDPROC(wndproc)
    instance = kernel32.GetModuleHandleW(None)
    class_name = "AIWatcherTrayWindow"
    wc = WNDCLASS()
    wc.lpfnWndProc = ctypes.cast(proc, ctypes.c_void_p).value
    wc.hInstance = instance
    wc.lpszClassName = class_name
    if not user32.RegisterClassW(ctypes.byref(wc)):
        # The class may already exist after a restart in the same process.
        pass
    hwnd = user32.CreateWindowExW(0, class_name, "AIWatcher Local", 0, 0, 0, 0, 0, None, None, instance, None)
    if not hwnd:
        print("AIWatcher tray unavailable: could not create hidden tray window", file=sys.stderr)
        return 2

    nid = NOTIFYICONDATA()
    nid.cbSize = ctypes.sizeof(NOTIFYICONDATA)
    nid.hWnd = hwnd
    nid.uID = 1
    nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
    nid.uCallbackMessage = WM_TRAY
    nid.hIcon = user32.LoadIconW(None, IDI_APPLICATION)
    nid.szTip = "AIWatcher Local"
    if not shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)):
        print("AIWatcher tray unavailable: Shell_NotifyIcon failed", file=sys.stderr)
        return 2

    message = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(message))
        user32.DispatchMessageW(ctypes.byref(message))
    return 0


def run_native_tray(url: str, *, prompt_url: str | None = None) -> int:
    """Run a real OS menu-bar/system-tray entry point."""
    prompt_url = prompt_url or f"{url.rstrip('/')}/?view=prompt"
    if sys.platform == "darwin" and shutil.which("swift"):
        return _run_macos_swift_tray(url, prompt_url)
    if sys.platform == "win32":
        return _run_windows_tray(url, prompt_url)
    print("AIWatcher tray unavailable: native tray is implemented for macOS and Windows.", file=sys.stderr)
    return 2


_AI_WORK_TEXT_HINTS = (
    "codex",
    "claude",
    "chatgpt",
    "openai",
    "cursor",
    "visual studio code",
    "vscode",
    "code.exe",
    "terminal",
    "iterm",
    "warp",
    "wezterm",
    "hyper",
    "ghostty",
    "powershell",
    "pwsh",
    "cmd.exe",
    "windowsterminal",
    "claude.ai",
    "chat.openai.com",
)


def _windows_foreground_text() -> str:
    try:
        import ctypes

        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return ""
        title = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, title, 512)
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        process = ""
        if pid.value:
            try:
                output = subprocess.check_output(
                    ["tasklist", "/FI", f"PID eq {int(pid.value)}", "/FO", "CSV", "/NH"],
                    text=True,
                    stderr=subprocess.DEVNULL,
                    timeout=0.6,
                    # The presence widget has no console; see runtime_nudge.
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                process = output.split(",", 1)[0].strip().strip('"')
            except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
                process = ""
        return f"{title.value} {process}"
    except (AttributeError, OSError, ValueError):
        return ""


def _foreground_looks_like_ai_work() -> bool:
    if sys.platform == "win32":
        haystack = _windows_foreground_text().lower()
        return bool(haystack) and any(hint in haystack for hint in _AI_WORK_TEXT_HINTS)
    return True


def _draw_brand_mark(canvas: object, height: float, blue: str, ink: str) -> None:
    """Draw the two-ring mark from logo/aiwatcher-mark.svg on a Tk canvas.

    Tk has no rounded-rectangle primitive, so each ring is four lines and four
    arcs on the stroke centerline. The geometry is the mark's own: both rings
    300 wide with a 40 stroke and an 85 outer corner radius in a 429x349 box,
    the ink ring (300x232 at offset 129,117) drawn in front. The rings' heights
    differ on purpose -- equalising them breaks the fit to the original artwork.
    """
    s = height / brand_mark.VIEW_H
    stroke = max(2.0, 40.0 * s)

    def ring(x0: float, y0: float, x1: float, y1: float, r: float, colour: str) -> None:
        for ax, ay, bx, by in (
            (x0 + r, y0, x1 - r, y0),
            (x0 + r, y1, x1 - r, y1),
            (x0, y0 + r, x0, y1 - r),
            (x1, y0 + r, x1, y1 - r),
        ):
            canvas.create_line(ax, ay, bx, by, fill=colour, width=stroke)
        for cx, cy, start in (
            (x0, y0, 90.0),
            (x1 - 2.0 * r, y0, 0.0),
            (x0, y1 - 2.0 * r, 180.0),
            (x1 - 2.0 * r, y1 - 2.0 * r, 270.0),
        ):
            canvas.create_arc(
                cx, cy, cx + 2.0 * r, cy + 2.0 * r,
                start=start, extent=90.0, style="arc", outline=colour, width=stroke,
            )

    # brand_mark.RINGS states the centerline, which is what `ring` draws on.
    for (x, y, w, h, radius, _stroke), colour in zip(brand_mark.RINGS, (blue, ink)):
        ring(x * s, y * s, (x + w) * s, (y + h) * s, radius * s, colour)


# PhotoImage pixels are owned by Tk, and a canvas only holds a reference by
# name: let the Python object fall out of scope and the image blanks. So the
# cache keeps them for the life of the process, keyed by everything that
# changes the pixels. There are four ring colours and three sizes in practice.
_MARK_PHOTOS: dict[tuple[float, str, str], object] = {}
_MARK_PHOTOS_UNAVAILABLE = False


def _brand_mark_photo(tk_module: object, height: float, blue: str, ink: str) -> object | None:
    """Return a cached anti-aliased PhotoImage of the mark, or None.

    None means the caller should draw the vector fallback instead: Tk before
    8.6 cannot read PNG at all, and a bar that renders its mark the old way is
    better than one that fails to start.
    """
    global _MARK_PHOTOS_UNAVAILABLE
    if _MARK_PHOTOS_UNAVAILABLE:
        return None
    key = (round(float(height), 2), blue, ink)
    photo = _MARK_PHOTOS.get(key)
    if photo is not None:
        return photo
    try:
        png = brand_mark.render_mark_png(height, blue, ink)
    except (ValueError, OverflowError):
        return None  # a colour this build does not understand; not fatal
    try:
        photo = tk_module.PhotoImage(data=base64.b64encode(png).decode("ascii"))
    except Exception:  # pragma: no cover - depends on the host Tk build
        _MARK_PHOTOS_UNAVAILABLE = True
        return None
    _MARK_PHOTOS[key] = photo
    return photo


def _paint_brand_mark(tk_module: object, canvas: object, height: float, blue: str, ink: str) -> None:
    """Draw the mark at the canvas origin, anti-aliased where Tk can manage it."""
    photo = _brand_mark_photo(tk_module, height, blue, ink)
    if photo is None:
        _draw_brand_mark(canvas, height=height, blue=blue, ink=ink)
        return
    try:
        canvas.create_image(0, 0, image=photo, anchor="nw")
    except tk_module.TclError:  # pragma: no cover - a torn-down interpreter
        _draw_brand_mark(canvas, height=height, blue=blue, ink=ink)


def run_native_presence(
    url: str,
    *,
    prompt_url: str | None = None,
    position: str = "bottom-right",
    visibility: str = "always",
    interval: int = 30,
) -> int:
    """Run the collapsed always-available local companion entry point."""
    prompt_url = prompt_url or f"{url.rstrip('/')}/?view=prompt"
    visibility = visibility if visibility in {"always", "ai-apps", "nudges-only"} else "always"
    if sys.platform == "darwin" and shutil.which("swift"):
        return _run_macos_swift_presence(url, prompt_url, position, visibility, interval)
    restart_requested = False
    offline_expanded = False
    try:
        import tkinter as tk
        from tkinter import ttk
    except Exception as exc:  # pragma: no cover - depends on host Python build
        print(f"AIWatcher companion presence unavailable: {exc}", file=sys.stderr)
        return 2

    root = tk.Tk()
    reduce_motion_state = {"value": _reduce_motion_requested(), "checked_at": time.monotonic()}

    def reduce_motion_active() -> bool:
        now = time.monotonic()
        if now - float(reduce_motion_state["checked_at"]) >= 5.0:
            reduce_motion_state["value"] = _reduce_motion_requested()
            reduce_motion_state["checked_at"] = now
        return bool(reduce_motion_state["value"])
    root.title("AIWatcher Companion")
    root.configure(bg="#0d141f")
    root.attributes("-topmost", True)
    if sys.platform == "win32":
        root.overrideredirect(True)
    try:
        root.call("::tk::unsupported::MacWindowStyle", "style", root._w, "utility", "closeBox")
    except tk.TclError:
        pass

    expanded_width = 640
    expanded_height = 58
    # 52, not 44: the count badge needs room inside the bubble -- see the
    # matching constant in the Swift bar.
    # The one number that sets how large the bubble is. Everything below --
    # the canvas, the mark, the face, the eyes, the rim, the badge and the
    # badge's type size -- is derived from it, so this is the only edit a size
    # change needs. It was 52, which on a 2560-wide display reads as a speck.
    collapsed_width = 72
    collapsed_height = 72
    # The canvas is what is left of the window after the frame's padding. It
    # used to be typed as 36x40 beside a 52px window and happened to agree;
    # that is the same trap as everything else in this bubble.
    collapsed_padding = (8, 6)
    collapsed_canvas_width = collapsed_width - 2 * collapsed_padding[0]
    collapsed_canvas_height = collapsed_height - 2 * collapsed_padding[1]
    bubble = brand_mark.bubble_layout(collapsed_canvas_width, collapsed_canvas_height)
    row_height = 30
    max_waiting_rows = 5
    screen_width = int(root.winfo_screenwidth())
    screen_height = int(root.winfo_screenheight())
    x = 24 if "left" in position else max(16, screen_width - expanded_width - 24)
    y = 24 if "top" in position else max(16, screen_height - expanded_height - 92)
    root.geometry(f"{expanded_width}x{expanded_height}+{x}+{y}")
    # Tracked here rather than read back through winfo_x/winfo_width, which
    # report a 1x1 window at 0,0 until Tk maps it.
    window_box = {"x": x, "y": y, "width": expanded_width, "height": expanded_height}

    def resize_window(width: int, height: int) -> None:
        new_x, new_y = _anchored_origin(
            window_box["x"],
            window_box["y"],
            window_box["width"],
            window_box["height"],
            width,
            height,
            screen_width,
            screen_height,
        )
        root.geometry(f"{width}x{height}+{new_x}+{new_y}")
        window_box.update(x=new_x, y=new_y, width=width, height=height)

    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    style.configure("Presence.TFrame", background="#090d14")
    style.configure("PresenceTitle.TLabel", background="#090d14", foreground="#f7fbff", font=("Helvetica", 11, "bold"))
    style.configure("PresenceMuted.TLabel", background="#090d14", foreground="#a8b6ca", font=("Helvetica", 9))
    style.configure("PresenceDrag.TLabel", background="#090d14", foreground="#8ea3bd", font=("Helvetica", 10, "bold"))
    # clam's stock button is a light grey chip with a bevel, which on this
    # dark bar read as a dialog from another decade. These are flat, dark and
    # borderless, and they carry hover and press in the fill rather than in a
    # raised edge. Padding stays close to the old (6, 3): the bar is a fixed
    # 640 wide and the busy layouts fill most of it, so there is no room to
    # spend on breathing space.
    #
    # Only the buttons change typeface. Segoe UI is the native Windows face,
    # but the title and subtitle widths are tuned to the current label metrics
    # -- the subtitle's 46-character cap is measured against them -- so the
    # labels keep theirs.
    button_face = "Segoe UI" if sys.platform == "win32" else "Helvetica"
    button_base: dict[str, object] = {
        "font": (button_face, 9, "bold"),
        "background": "#1b2636",
        "foreground": "#dce7f7",
        "borderwidth": 0,
        "relief": "flat",
        "focuscolor": "",
    }
    button_states: dict[str, object] = {
        "background": [("disabled", "#141c28"), ("pressed", "#2c3c57"), ("active", "#22304a")],
        "foreground": [("disabled", "#4a586c")],
    }
    style.configure("Presence.TButton", padding=(8, 4), **button_base)
    style.map("Presence.TButton", **button_states)
    style.configure("PresenceAttention.TButton", padding=(8, 4), **button_base)
    style.map(
        "PresenceAttention.TButton",
        background=[("disabled", "#141c28"), ("pressed", "#b84816"), ("active", "#f07a3a")],
        foreground=[("disabled", "#f3d8c8")],
    )
    style.configure("PresenceMini.TButton", padding=(5, 3), **button_base)
    style.map("PresenceMini.TButton", **button_states)

    frame = ttk.Frame(root, padding=(8, 6), style="Presence.TFrame")
    frame.pack(fill="both", expand=True)
    collapsed_frame = ttk.Frame(root, padding=collapsed_padding, style="Presence.TFrame")
    left = ttk.Frame(frame, style="Presence.TFrame")
    left.pack(side="left", fill="both", expand=True)
    title_var = tk.StringVar(value="AIWatcher")
    subtitle_var = tk.StringVar(value="Watching quietly")
    primary_label_var = tk.StringVar(value="Watch")
    primary_action_var = tk.StringVar(value="open_url")
    primary_session_id_var = tk.StringVar(value="")
    compact_command_var = tk.StringVar(value="")
    compact_sha_var = tk.StringVar(value="")
    primary_runtime_available_var = tk.BooleanVar(value=False)
    primary_url_var = tk.StringVar(value=url)
    continue_label_var = tk.StringVar(value="Continue")
    continue_action_var = tk.StringVar(value="")
    continue_url_var = tk.StringVar(value="")
    continue_session_id_var = tk.StringVar(value="")
    continue_reason_var = tk.StringVar(value="")
    continue_expected_tokens_var = tk.IntVar(value=0)
    skip_label_var = tk.StringVar(value="Skip")
    skip_state_var = tk.StringVar(value="")
    skip_session_id_var = tk.StringVar(value="")
    skip_project_var = tk.StringVar(value="")
    skip_projects_var = tk.StringVar(value="")
    skip_session_ids_var = tk.StringVar(value="")
    state_var = tk.StringVar(value="watching")
    pulse_var = tk.BooleanVar(value=False)
    suppress_passive_refresh_until = tk.DoubleVar(value=0.0)
    pending_clipboard_override_session_var = tk.StringVar(value="")
    waiting_row_texts: list[str] = []
    waiting_row_urls: list[str] = []
    waiting_row_session_ids: list[str] = []
    waiting_row_return: list[bool] = []
    waiting_row_wants: list[str] = []
    waiting_row_kinds: list[str] = []
    waiting_row_sections: list[str] = []
    waiting_row_action_labels: list[str] = []
    # Compaction rows: one window each, with its own command; an empty action
    # is a fact (compacting, compacted) and draws no button.
    waiting_row_tags: list[str] = []
    waiting_row_actions: list[str] = []
    waiting_row_commands: list[str] = []
    waiting_row_shas: list[str] = []
    waiting_count_var = tk.IntVar(value=0)
    working_count_var = tk.IntVar(value=0)
    finished_count_var = tk.IntVar(value=0)
    review_count_var = tk.IntVar(value=0)
    motion_mode_var = tk.StringVar(value="standard")
    activity_tone_var = tk.StringVar(value="")
    delivery_confirmed_var = tk.BooleanVar(value=False)
    delivery_receipt_id_var = tk.StringVar(value="")
    orbit_angle_var = tk.IntVar(value=0)
    # The running arc's canvas id, so its angle can be nudged on its own frame
    # clock without re-running update_attention_style's full redraw. Cleared
    # whenever the canvas is torn down, and None whenever no arc is drawn.
    # "running" says whether a frame timer is already in flight, so the redraw
    # can start one without ever stacking two.
    orbit_item: dict[str, object] = {"id": None, "running": False}
    expression_phase_var = tk.IntVar(value=0)
    focus_lock_ticks_var = tk.IntVar(value=0)
    pressure_available_var = tk.BooleanVar(value=False)
    pressure_pct_var = tk.IntVar(value=0)
    pressure_severity_var = tk.StringVar(value="ok")
    pressure_stats_var = tk.StringVar(value="")
    signal_chip_var = tk.StringVar(value="")
    signal_url_var = tk.StringVar(value="")
    drag = ttk.Label(frame, text="::", style="PresenceDrag.TLabel", cursor="fleur")
    drag.pack(side="left", padx=(0, 6))
    # The brand mark sits beside both lines of copy, as on the dashboard. The
    # expanded bar is always the dark ground, so the ink ring takes the
    # dark-theme ink.
    mark_canvas = tk.Canvas(frame, width=32, height=26, bg="#090d14", highlightthickness=0, bd=0)
    _paint_brand_mark(tk, mark_canvas, 26.0, "#0052F5", "#DCE6F6")
    mark_canvas.pack(side="left", padx=(0, 8), before=left)
    title_stack = ttk.Frame(left, style="Presence.TFrame")
    title_stack.pack(anchor="w")
    ttk.Label(title_stack, textvariable=title_var, style="PresenceTitle.TLabel").pack(side="left")
    # Running totals for the working session -- the absolute anchor for the
    # meter's percent. Plain muted text, no status colour: a total is not a
    # verdict. Empty when unmeasurable, which packs to nothing.
    ttk.Label(title_stack, textvariable=pressure_stats_var, style="PresenceMuted.TLabel").pack(side="left", padx=(10, 0))
    subtitle_row = ttk.Frame(left, style="Presence.TFrame")
    subtitle_row.pack(anchor="w", fill="x")
    ttk.Label(subtitle_row, textvariable=subtitle_var, style="PresenceMuted.TLabel", wraplength=250).pack(side="left")
    # Context-pressure meter for the resting state: the one graphic the bar
    # carries. Packed only while the payload says the number is measurable.
    meter_canvas = tk.Canvas(subtitle_row, width=70, height=9, bg="#090d14", highlightthickness=0, bd=0)

    drag_start: dict[str, int] = {"x": 0, "y": 0}

    def begin_drag(event: tk.Event) -> None:
        drag_start["x"] = int(event.x)
        drag_start["y"] = int(event.y)

    def move_window(event: tk.Event) -> None:
        new_x = int(event.x_root) - drag_start["x"]
        new_y = int(event.y_root) - drag_start["y"]
        root.geometry(f"+{new_x}+{new_y}")
        window_box.update(x=new_x, y=new_y)

    for draggable in (root, frame, collapsed_frame, drag, left):
        draggable.bind("<ButtonPress-1>", begin_drag)
        draggable.bind("<B1-Motion>", move_window)

    def open_dashboard() -> None:
        webbrowser.open(url)

    def open_prompt() -> None:
        webbrowser.open(prompt_url or url)

    def open_ask() -> None:
        webbrowser.open(f"{url.rstrip('/')}/?ask=1")
        schedule_auto_collapse(1500)

    def split_lines(value: str) -> list[str]:
        return [part.strip() for part in value.splitlines() if part.strip()]

    def row_display_text(row: dict[str, object]) -> str:
        kind = str(row.get("kind") or "")
        if kind == "compact":
            return str(row.get("text") or "")
        # A live chat's current prompt: the server writes the whole line
        # (chat name and figures, never prompt text).
        if kind.startswith("prompt_"):
            return str(row.get("text") or "")
        if row.get("text"):
            return str(row.get("text"))
        if kind in {"working", "delivery_confirmed"}:
            return str(row.get("text") or "")
        tool = str(row.get("tool") or "AI tool")
        project = str(row.get("project") or "")
        waited = str(row.get("waited_label") or "")
        review = str(row.get("review_label") or waited)
        severity = str(row.get("severity_label") or "")
        activity = str(row.get("activity_label") or "")
        scope = str(row.get("scope_label") or "")
        if kind in {"finished", "recently_observed"}:
            parts = ["Ready to check", project, tool, waited]
        elif kind == "session_waiting":
            parts = ["Waiting", project, tool, waited]
        elif kind == "context_review":
            parts = ["Context health", scope, project, tool, severity, activity, review]
        elif kind:
            parts = ["Signal", project, tool, waited]
        else:
            parts = [tool, project, waited]
        return " · ".join(part for part in parts if part)

    def review_title() -> str:
        if state_var.get() in {"control_review", "context_review"}:
            count = int(review_count_var.get() or 0)
            shown = visible_waiting_rows()
            if shown and count > shown:
                return f"{shown} of {count} projects need review"
            return "1 project needs review" if count == 1 else f"{count} projects need review"
        if state_var.get() == "session_finished" and visible_waiting_rows() > 0:
            count = int(finished_count_var.get() or 0)
            shown = visible_waiting_rows()
            if shown and count > shown:
                return f"{shown} of {count} ready to check"
            return f"{count} ready to check"
        return title_var.get()[:34]

    def review_subtitle() -> str:
        if state_var.get() in {"control_review", "context_review"} and visible_waiting_rows() > 0:
            count = max(int(review_count_var.get() or 0), visible_waiting_rows())
            hidden = max(count - visible_waiting_rows(), 0)
            return (
                f"{hidden} more saved in Watch. Review all or pick a project."
                if hidden > 0 else "Pick a project to review its context."
            )
        if state_var.get() == "session_finished" and visible_waiting_rows() > 0:
            base_count = int(finished_count_var.get() or 0) if state_var.get() == "session_finished" else int(review_count_var.get() or 0)
            count = max(base_count, visible_waiting_rows())
            hidden = max(count - visible_waiting_rows(), 0)
            return (
                f"{hidden} more in UI. Pick a row or open UI."
                if hidden > 0 else "Pick a row or open UI."
            )
        return subtitle_var.get()[:46]

    def row_action_label(index: int) -> str:
        if index < len(waiting_row_action_labels) and waiting_row_action_labels[index]:
            return waiting_row_action_labels[index]
        kind = waiting_row_kinds[index] if index < len(waiting_row_kinds) else ""
        if kind == "compact":
            return "Review"
        if kind.startswith("prompt_"):
            return "Open"
        can_return = index < len(waiting_row_return) and waiting_row_return[index]
        if can_return:
            return "Return"
        if kind in {"context_review", "control_review"}:
            return "Review"
        return "Review" if kind else "Open"

    def short_skip_failure_message() -> str:
        state = skip_state_var.get()
        if state in {"control_recommended_group", "control_recommended_project"}:
            return "Could not save Later. Open UI."
        if state == "session_finished_group":
            return "Could not clear notices. Open UI."
        return "Could not save Skip. Open UI."

    def apply_offline_state() -> None:
        nonlocal offline_expanded
        root.title("AIWatcher Companion: UI offline")
        waiting_row_texts.clear()
        waiting_row_urls.clear()
        waiting_row_session_ids.clear()
        waiting_row_return.clear()
        waiting_row_wants.clear()
        waiting_row_kinds.clear()
        waiting_row_sections.clear()
        waiting_row_action_labels.clear()
        waiting_row_tags.clear()
        waiting_row_actions.clear()
        waiting_row_commands.clear()
        waiting_row_shas.clear()
        waiting_count_var.set(0)
        working_count_var.set(0)
        finished_count_var.set(0)
        review_count_var.set(0)
        activity_tone_var.set("")
        skip_state_var.set("")
        skip_session_id_var.set("")
        skip_project_var.set("")
        skip_projects_var.set("")
        skip_session_ids_var.set("")
        state_var.set("offline")
        title_var.set("UI offline")
        subtitle_var.set("Start dashboard + Companion")
        primary_label_var.set("Restart")
        primary_action_var.set("restart_aiwatcher")
        primary_url_var.set(url)
        if not offline_expanded and collapsed.get():
            offline_expanded = True
            set_collapsed(False)
        update_attention_style()

    def apply_state(payload: dict[str, object]) -> None:
        nonlocal offline_expanded
        offline_expanded = False
        incoming_state = str(payload.get("state") or "watching")
        if (
            suppress_passive_refresh_until.get() > time.time()
            and state_var.get() in {"fresh_start_copied", "clipboard_confirm", "compact_copied"}
            and incoming_state not in {"prompt_gate", "command_gate"}
        ):
            return
        if suppress_passive_refresh_until.get() <= time.time():
            suppress_passive_refresh_until.set(0.0)
        previous_state = state_var.get()
        previous_working_count = int(working_count_var.get() or 0)
        previous_delivery_receipt_id = delivery_receipt_id_var.get()
        previous_needs_attention = (
            activity_tone_var.get() == "attention"
            if activity_tone_var.get()
            else previous_state in {
                "prompt_gate", "command_gate", "control_recommended", "control_review",
                "optimize_available", "clipboard_confirm", "session_waiting",
            }
        )
        was_hard_gate = previous_state in {"prompt_gate", "command_gate"}
        state_var.set(incoming_state)
        waiting_row_texts.clear()
        waiting_row_urls.clear()
        waiting_row_session_ids.clear()
        waiting_row_return.clear()
        waiting_row_wants.clear()
        waiting_row_kinds.clear()
        waiting_row_sections.clear()
        waiting_row_action_labels.clear()
        waiting_row_tags.clear()
        waiting_row_actions.clear()
        waiting_row_commands.clear()
        waiting_row_shas.clear()
        # The away digest's history entries ride the same row machinery as
        # the waiting queue; a payload carries one list or the other. A
        # compaction with several windows brings its own rows.
        waiting_sessions = payload.get("waiting_sessions")
        if not isinstance(waiting_sessions, list) or not waiting_sessions:
            waiting_sessions = payload.get("digest_rows")
        if incoming_state == "compact_recommended":
            waiting_sessions = payload.get("compact_rows")
        activity_sections = payload.get("activity_sections")
        grouped_rows: list[dict[str, object]] = []
        if isinstance(activity_sections, list):
            active_sections: list[tuple[str, list[object]]] = []
            for section in activity_sections:
                if not isinstance(section, dict):
                    continue
                label = str(section.get("label") or "")
                rows = section.get("rows")
                if not isinstance(rows, list) or not rows:
                    continue
                active_sections.append((label, rows))
            for active_index, (label, rows) in enumerate(active_sections):
                remaining_sections = len(active_sections) - active_index - 1
                allowance = max(1, max_waiting_rows - len(grouped_rows) - remaining_sections)
                for section_index, source in enumerate(rows[:allowance]):
                    if not isinstance(source, dict):
                        continue
                    grouped_rows.append({**source, "_section_label": label if section_index == 0 else ""})
        if grouped_rows:
            waiting_sessions = grouped_rows
        if isinstance(waiting_sessions, list):
            for row in waiting_sessions[:max_waiting_rows]:
                if not isinstance(row, dict):
                    continue
                waiting_row_texts.append(row_display_text(row))
                normalized_action = row.get("normalized_action")
                normalized_action = normalized_action if isinstance(normalized_action, dict) else {}
                path = str(normalized_action.get("url") or row.get("url") or "/")
                waiting_row_urls.append(path if path.startswith("http") else f"{url.rstrip('/')}{path}")
                waiting_row_session_ids.append(str(normalized_action.get("session_id") or row.get("session_id") or ""))
                waiting_row_return.append(normalized_action.get("type") == "runtime_return" or bool(row.get("return_available")))
                waiting_row_wants.append(str(row.get("wants") or "")[:16])
                waiting_row_kinds.append(str(row.get("kind") or ""))
                waiting_row_sections.append(str(row.get("_section_label") or ""))
                waiting_row_tags.append(str(row.get("tag") or "")[:16])
                waiting_row_actions.append(str(normalized_action.get("type") or row.get("action") or ""))
                waiting_row_action_labels.append(str(normalized_action.get("label") or ""))
                waiting_row_commands.append(str(row.get("command") or ""))
                waiting_row_shas.append(str(row.get("sha") or ""))
        preferences = payload.get("companion_preferences")
        if isinstance(preferences, dict):
            motion_mode_var.set(str(preferences.get("motion_mode") or "standard"))
        activity_tone_var.set(str(payload.get("tone") or ""))
        delivery_confirmed_var.set(bool(payload.get("delivery_confirmed")))
        delivery_receipt_id_var.set(str(payload.get("delivery_receipt_id") or ""))
        presence = payload.get("presence")
        try:
            presence_waiting_count = int(presence.get("waiting") or 0) if isinstance(presence, dict) else 0
        except (TypeError, ValueError):
            presence_waiting_count = 0
        try:
            working_count_var.set(int(presence.get("working") or 0) if isinstance(presence, dict) else 0)
        except (TypeError, ValueError, tk.TclError):
            working_count_var.set(0)
        current_needs_attention = (
            activity_tone_var.get() == "attention"
            if activity_tone_var.get()
            else incoming_state in {
                "prompt_gate", "command_gate", "control_recommended", "control_review",
                "optimize_available", "clipboard_confirm", "session_waiting",
            }
        )
        entered_attention = current_needs_attention and not previous_needs_attention
        activity_changed = (previous_working_count > 0) != (int(working_count_var.get() or 0) > 0)
        delivered = bool(delivery_receipt_id_var.get()) and delivery_receipt_id_var.get() != previous_delivery_receipt_id
        recovered = previous_state == "offline" and incoming_state != "offline"
        if not reduce_motion_active() and motion_mode_var.get() != "quiet" and (entered_attention or activity_changed or delivered or recovered):
            focus_lock_ticks_var.set(2)
        finished_sessions = payload.get("finished_sessions")
        digest_rows = payload.get("digest_rows")
        badge = payload.get("badge")
        if "badge" in payload and isinstance(badge, dict):
            try:
                badge_count = int(badge.get("count") or 0)
            except (TypeError, ValueError):
                badge_count = 0
            if incoming_state in {"control_review", "context_review", "compact_recommended"}:
                waiting_count_var.set(0)
                finished_count_var.set(0)
                review_count_var.set(badge_count)
            elif badge.get("tone") == "attention":
                waiting_count_var.set(badge_count)
                finished_count_var.set(0)
                review_count_var.set(0)
            else:
                waiting_count_var.set(0)
                finished_count_var.set(badge_count)
                review_count_var.set(0)
        elif "badge" in payload:
            waiting_count_var.set(0)
            finished_count_var.set(0)
            review_count_var.set(0)
        else:
            waiting_count_var.set(presence_waiting_count or len(waiting_row_texts))
            if isinstance(finished_sessions, list) and finished_sessions:
                finished_count_var.set(len(finished_sessions))
            elif isinstance(digest_rows, list):
                finished_count_var.set(len(digest_rows))
            else:
                finished_count_var.set(0)
            review_count_var.set(0)
        pressure = payload.get("pressure")
        if isinstance(pressure, dict) and pressure.get("available"):
            pressure_available_var.set(True)
            try:
                pressure_pct_var.set(int(pressure.get("pct_of_turn_limit") or 0))
            except (TypeError, ValueError, tk.TclError):
                pressure_pct_var.set(0)
            pressure_severity_var.set(str(pressure.get("severity") or "ok"))
            # The prompt status is the resting bar with better words; it keeps the meter's stats.
            if incoming_state in {"watching", "prompt_status"}:
                pressure_stats_var.set(str(pressure.get("stats_label") or "")[:20])
            else:
                pressure_stats_var.set("")
        else:
            pressure_available_var.set(False)
            pressure_stats_var.set("")
        signal = payload.get("recent_signal")
        if isinstance(signal, dict) and signal.get("chip"):
            signal_chip_var.set(str(signal.get("chip"))[:14])
            signal_path = str(signal.get("url") or "/")
            signal_url_var.set(signal_path if signal_path.startswith("http") else f"{url.rstrip('/')}{signal_path}")
        else:
            signal_chip_var.set("")
            signal_url_var.set("")
        # Tk has no tail ellipsis, so a character cap stands in for the
        # frame: 34 holds the planner's longest label (32) on the 560-wide
        # bar, where 18 cut it to "Continue only afte".
        title_var.set(str(payload.get("label") or "AIWatcher")[:34])
        subtitle_text = str(payload.get("subtitle") or "Watching quietly")[:46]
        if isinstance(activity_sections, list) and len(activity_sections) > 1:
            subtitle_text = str(payload.get("activity_line") or subtitle_text)[:46]
        remaining = payload.get("expires_in_seconds")
        if incoming_state in {"prompt_gate", "command_gate"} and isinstance(remaining, int) and remaining >= 0:
            subtitle_text += f" · {remaining}s"
        subtitle_var.set(subtitle_text)
        root.title(f"AIWatcher Companion: {str(payload.get('activity_line') or subtitle_text)[:120]}")
        if incoming_state in {"control_review", "context_review", "session_finished"} and visible_waiting_rows() > 0:
            title_var.set(review_title())
            subtitle_var.set(review_subtitle())
        primary_label_var.set(str(payload.get("primary_label") or "Watch")[:12])
        primary_action_var.set(str(payload.get("primary_action") or "open_url"))
        primary_session_id_var.set(str(payload.get("primary_session_id") or ""))
        compact_command_var.set(str(payload.get("compact_command") or ""))
        compact_sha_var.set(str(payload.get("compact_sha") or ""))
        primary_runtime_available_var.set(bool(payload.get("primary_runtime_available")))
        primary_path = str(payload.get("primary_url") or "/")
        primary_url_var.set(primary_path if primary_path.startswith("http") else f"{url.rstrip('/')}{primary_path}")
        continue_label_var.set(str(payload.get("continue_label") or "Continue")[:10])
        continue_action_var.set(str(payload.get("continue_action") or ""))
        continue_path = str(payload.get("continue_url") or "")
        continue_url_var.set(
            continue_path
            if continue_path.startswith("http")
            else (f"{url.rstrip('/')}{continue_path}" if continue_path else "")
        )
        continue_session_id_var.set(str(payload.get("continue_session_id") or ""))
        continue_reason_var.set(str(payload.get("continue_reason") or ""))
        skip_label_var.set(str(payload.get("skip_label") or "Skip")[:10])
        skip_state_var.set(str(payload.get("skip_state") or ""))
        skip_session_id_var.set(str(payload.get("skip_session_id") or ""))
        skip_project_var.set(str(payload.get("skip_project") or ""))
        projects = payload.get("skip_projects")
        skip_projects_var.set("\n".join(str(item).strip() for item in projects if str(item).strip()) if isinstance(projects, list) else "")
        session_ids = payload.get("skip_session_ids")
        skip_session_ids_var.set("\n".join(str(item).strip() for item in session_ids if str(item).strip()) if isinstance(session_ids, list) else "")
        try:
            continue_expected_tokens_var.set(int(payload.get("continue_expected_saved_context_tokens") or 0))
        except (TypeError, ValueError, tk.TclError):
            continue_expected_tokens_var.set(0)
        # Entering a hard gate opens the bar itself, once per gate: the
        # reason and the Review button are what say why the prompt hangs.
        if incoming_state in {"prompt_gate", "command_gate"} and not was_hard_gate and collapsed.get():
            set_collapsed(False)

    def scan_now() -> None:
        title_var.set("Scanning")
        subtitle_var.set("Checking local AI work...")
        try:
            with urllib.request.urlopen(f"{url.rstrip('/')}/api/companion-scan", timeout=12.0) as response:
                payload = json.loads(response.read().decode("utf-8"))
            state_payload = payload.get("state") if isinstance(payload, dict) else None
            if isinstance(state_payload, dict):
                apply_state(state_payload)
            else:
                title_var.set("Scan done")
                subtitle_var.set("Open UI for details")
        except (OSError, urllib.error.URLError, json.JSONDecodeError, tk.TclError):
            title_var.set("Scan failed")
            subtitle_var.set("Open UI for details")
        update_attention_style()

    def open_primary() -> None:
        nonlocal restart_requested
        if primary_action_var.get() == "restart_aiwatcher":
            restart_requested = True
            title_var.set("Restarting AIWatcher")
            subtitle_var.set("Dashboard and Companion will reopen")
            primary_button.state(["disabled"])
            root.after(80, root.destroy)
            return
        if state_var.get() == "proof_pending":
            request = urllib.request.Request(f"{url.rstrip('/')}/api/handoff-receipts-viewed", method="POST")
            saved = False
            try:
                with urllib.request.urlopen(request, timeout=1.5):
                    saved = True
            except (OSError, urllib.error.URLError):
                pass
            if not saved:
                title_var.set("Still pending")
                subtitle_var.set("Could not save receipt view")
                update_attention_style()
                return
            state_var.set("watching")
            title_var.set("Watching quietly")
            subtitle_var.set("Fresh Start receipt opened")
            skip_state_var.set("")
            update_attention_style()
            schedule_auto_collapse(1200)
            webbrowser.open(primary_url_var.get() or url)
            return
        if primary_action_var.get() == "copy_fresh_start" and primary_session_id_var.get().strip():
            copy_fresh_start_from_companion()
            return
        if primary_action_var.get() == "copy_compact" and compact_command_var.get().strip():
            copy_compact_from_companion()
            return
        if primary_action_var.get() == "runtime_return" and primary_session_id_var.get().strip():
            request_runtime_return(primary_session_id_var.get().strip(), primary_url_var.get() or url)
            return
        if primary_action_var.get() == "open_prompt_gate":
            schedule_auto_collapse(1500)
        webbrowser.open(primary_url_var.get() or url)

    def set_clipboard_text(text: str) -> bool:
        value = text.strip()
        if not value:
            return False
        try:
            root.clipboard_clear()
            root.clipboard_append(text)
            root.update()
            return root.clipboard_get().strip() == value
        except tk.TclError:
            return False

    def copy_fresh_start_from_companion() -> None:
        session_id = primary_session_id_var.get().strip()
        title_var.set("Copying brief")
        subtitle_var.set("Preparing Fresh Start...")
        try:
            encoded = urllib.parse.quote(session_id)
            with urllib.request.urlopen(f"{url.rstrip('/')}/api/handoff?id={encoded}&target=generic", timeout=8.0) as response:
                capsule = json.loads(response.read().decode("utf-8"))
            brief = str(capsule.get("next_brief") or "").strip() if isinstance(capsule, dict) else ""
            if not brief:
                raise ValueError("empty brief")
            try:
                current_clipboard = root.clipboard_get().strip()
            except tk.TclError:
                current_clipboard = ""
            if (
                current_clipboard
                and current_clipboard != brief
                and not current_clipboard.startswith("AIWatcher Fresh Start brief")
                and pending_clipboard_override_session_var.get() != session_id
            ):
                pending_clipboard_override_session_var.set(session_id)
                state_var.set("clipboard_confirm")
                suppress_passive_refresh_until.set(time.time() + 30.0)
                title_var.set("Clipboard has text")
                subtitle_var.set("Click Replace to copy Fresh Start")
                primary_label_var.set("Replace")
                update_attention_style()
                schedule_auto_collapse(30000)
                return
            pending_clipboard_override_session_var.set("")
            if not set_clipboard_text(brief):
                title_var.set("Copy failed")
                subtitle_var.set("Open UI to copy the brief")
                update_attention_style()
                webbrowser.open(primary_url_var.get() or url)
                return
            payload = {
                "session_id": session_id,
                "decision": "copy_handoff",
                "reason": continue_reason_var.get() or "Fresh Start brief copied from Companion.",
                "action_channel": "companion",
            }
            request = urllib.request.Request(
                f"{url.rstrip('/')}/api/handoff-decision",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=1.5):
                    pass
            except (OSError, urllib.error.URLError):
                pass
            if primary_runtime_available_var.get():
                request = urllib.request.Request(
                    f"{url.rstrip('/')}/api/runtime-return",
                    data=json.dumps({"session_id": session_id}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                try:
                    with urllib.request.urlopen(request, timeout=1.5):
                        pass
                except (OSError, urllib.error.URLError):
                    pass
            else:
                webbrowser.open(primary_url_var.get() or url)
            continue_session_id_var.set("")
            skip_state_var.set("")
            state_var.set("fresh_start_copied")
            suppress_passive_refresh_until.set(time.time() + 12.0)
            title_var.set("Fresh Start copied")
            subtitle_var.set("Paste it into a fresh chat")
            primary_url_var.set(url)
            update_attention_style()
            schedule_auto_collapse(12000)
        except (OSError, urllib.error.URLError, json.JSONDecodeError, ValueError, tk.TclError):
            title_var.set("Fresh Start")
            subtitle_var.set("Open UI to copy the brief")
            update_attention_style()
            webbrowser.open(primary_url_var.get() or url)

    def record_compact_decision(session_id: str, sha: str, decision: str) -> None:
        request = urllib.request.Request(
            f"{url.rstrip('/')}/api/compact-decision",
            data=json.dumps({
                "session_id": session_id,
                "sha": sha,
                "decision": decision,
                "action_channel": "companion",
            }).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=1.5):
                pass
        except (OSError, urllib.error.URLError):
            pass

    # The bar says "Copied" only after the clipboard reads back the exact
    # command; the dashboard takes over from the next poll, having read the
    # click from the receipt, and moves on only when the session's own log
    # shows the /compact.
    def copy_compact_from_companion() -> None:
        command = compact_command_var.get().strip()
        session_id = primary_session_id_var.get().strip()
        if not set_clipboard_text(command):
            title_var.set("Copy failed")
            subtitle_var.set("Open UI to copy /compact")
            update_attention_style()
            webbrowser.open(primary_url_var.get() or url)
            return
        record_compact_decision(session_id, compact_sha_var.get(), "copied")
        skip_state_var.set("")
        state_var.set("compact_copied")
        suppress_passive_refresh_until.set(time.time() + 3.0)
        title_var.set("Copied")
        subtitle_var.set("Paste /compact into that session")
        primary_url_var.set(url)
        update_attention_style()
        schedule_auto_collapse(12000)

    # One row's Copy, when several windows qualify. Only that row changes:
    # the receipt notes the click against the session the row names.
    def copy_compact_row(index: int) -> None:
        command = waiting_row_commands[index] if index < len(waiting_row_commands) else ""
        if not set_clipboard_text(command):
            title_var.set("Copy failed")
            subtitle_var.set("Open UI to copy /compact")
            update_attention_style()
            webbrowser.open(waiting_row_urls[index] if index < len(waiting_row_urls) else url)
            return
        session_id = waiting_row_session_ids[index] if index < len(waiting_row_session_ids) else ""
        sha = waiting_row_shas[index] if index < len(waiting_row_shas) else ""
        record_compact_decision(session_id, sha, "copied")
        if index < len(waiting_row_tags):
            waiting_row_tags[index] = "copied"
        suppress_passive_refresh_until.set(time.time() + 3.0)
        apply_waiting_rows()
        schedule_auto_collapse(12000)

    def continue_here() -> None:
        if (
            state_var.get() == "prompt_gate"
            and continue_action_var.get() == "run_original_prompt"
            and continue_url_var.get().strip()
        ):
            gate_url = continue_url_var.get().strip().rstrip("/")
            request = urllib.request.Request(
                f"{gate_url}/decision",
                data=json.dumps({"decision": "run_original"}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            released = False
            try:
                with urllib.request.urlopen(request, timeout=1.5):
                    released = True
            except (OSError, urllib.error.URLError):
                pass
            if not released:
                title_var.set("Still paused")
                subtitle_var.set("Open Review Gate to continue")
                update_attention_style()
                return
            state_var.set("watching")
            title_var.set("Continuing")
            subtitle_var.set("Original prompt released")
            continue_action_var.set("")
            continue_url_var.set("")
            primary_url_var.set(url)
            update_attention_style()
            schedule_auto_collapse(1200)
            return
        session_id = continue_session_id_var.get().strip()
        if not session_id:
            return
        payload = {
            "session_id": session_id,
            "decision": "continue_here",
            "reason": continue_reason_var.get() or "User chose to keep working in the current session.",
            "action_channel": "companion",
        }
        expected = int(continue_expected_tokens_var.get() or 0)
        if expected > 0:
            payload["expected_saved_context_tokens"] = expected
        request = urllib.request.Request(
            f"{url.rstrip('/')}/api/handoff-decision",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        saved = False
        try:
            with urllib.request.urlopen(request, timeout=1.5):
                saved = True
        except (OSError, urllib.error.URLError):
            pass
        if not saved:
            title_var.set("Still pending")
            subtitle_var.set("Could not save Continue")
            update_attention_style()
            return
        continue_session_id_var.set("")
        state_var.set("watching")
        title_var.set("Watching quietly")
        subtitle_var.set("Fresh Start decision saved")
        primary_url_var.set(url)
        update_attention_style()
        schedule_auto_collapse(1200)

    def skip_current() -> None:
        state = skip_state_var.get().strip()
        if not state:
            return
        payload = {
            "state": state,
            "session_id": skip_session_id_var.get().strip(),
            "project": skip_project_var.get().strip(),
            "projects": split_lines(skip_projects_var.get()) or split_lines(skip_project_var.get()),
            "session_ids": split_lines(skip_session_ids_var.get()) or split_lines(skip_session_id_var.get()),
        }
        request = urllib.request.Request(
            f"{url.rstrip('/')}/api/companion-skip",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        saved = False
        try:
            with urllib.request.urlopen(request, timeout=1.5):
                saved = True
        except (OSError, urllib.error.URLError):
            pass
        if not saved:
            title_var.set("Still pending")
            subtitle_var.set(short_skip_failure_message())
            update_attention_style()
            return
        skip_state_var.set("")
        skip_session_id_var.set("")
        skip_project_var.set("")
        skip_projects_var.set("")
        skip_session_ids_var.set("")
        continue_session_id_var.set("")
        state_var.set("watching")
        title_var.set("Watching quietly")
        subtitle_var.set("Companion nudge skipped")
        primary_url_var.set(url)
        update_attention_style()
        schedule_auto_collapse(1200)

    collapsed = tk.BooleanVar(value=False)
    plan_packed = tk.BooleanVar(value=True)
    ask_packed = tk.BooleanVar(value=True)
    continue_packed = tk.BooleanVar(value=False)
    skip_packed = tk.BooleanVar(value=False)
    scan_packed = tk.BooleanVar(value=True)
    primary_packed = tk.BooleanVar(value=True)
    rows_packed = tk.BooleanVar(value=False)
    rows_shown = tk.IntVar(value=0)
    meter_packed = tk.BooleanVar(value=False)
    signal_packed = tk.BooleanVar(value=False)
    auto_collapse_token = tk.IntVar(value=0)
    auto_collapse_deadline = tk.DoubleVar(value=0.0)

    def set_collapsed(value: bool) -> None:
        if collapsed.get() == value:
            update_attention_style()
            return
        collapsed.set(value)
        if collapsed.get():
            auto_collapse_deadline.set(0.0)
        if collapsed.get():
            frame.pack_forget()
            collapsed_frame.pack(fill="both", expand=True)
            resize_window(collapsed_width, collapsed_height)
        else:
            collapsed_frame.pack_forget()
            frame.pack(fill="both", expand=True)
            resize_window(expanded_width, expanded_height + row_height * visible_waiting_rows())
        update_attention_style()

    def toggle_collapsed() -> None:
        set_collapsed(not collapsed.get())
        if not collapsed.get():
            schedule_auto_collapse(10000)

    def has_primary_action() -> bool:
        # session_finished earns the primary (Review) and the compact layout;
        # update_attention_style's needs_attention set deliberately excludes
        # it, so "review when ready" never wears the orange treatment.
        states = {
            "offline", "prompt_gate", "command_gate", "control_recommended", "control_review", "context_review", "optimize_available", "clipboard_confirm",
            "session_waiting", "session_finished", "away_digest", "compact_recommended", "delivery_review_ready",
        }
        # Only the nudge step of a compaction carries a button; the later
        # steps are facts about a thing already done.
        compact_without_button = state_var.get() == "compact_recommended" and primary_action_var.get() == "none"
        return state_var.get() in states and not compact_without_button

    def visible_waiting_rows() -> int:
        # One row per actionable item. Waiting, context review, finished
        # batches, and away digests all reuse this row surface so the badge can
        # explain itself instead of dropping the user into an unlabeled count.
        if not waiting_row_texts:
            return 0
        return min(len(waiting_row_texts), max_waiting_rows)

    def request_runtime_return(session_id: str, fallback_url: str) -> None:
        # The honesty rule from the transient overlay applies here too: wait
        # for the result, and on failure say why and open the session in
        # AIWatcher -- never report a return that did not happen.
        subtitle_var.set("Returning...")
        request = urllib.request.Request(
            f"{url.rstrip('/')}/api/runtime-return",
            data=json.dumps({"session_id": session_id}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        ok = False
        message = ""
        try:
            with urllib.request.urlopen(request, timeout=4.0) as response:
                result = json.loads(response.read().decode("utf-8"))
            if isinstance(result, dict):
                ok = bool(result.get("ok"))
                message = str(result.get("message") or "")
        except (OSError, urllib.error.URLError, json.JSONDecodeError):
            pass
        if ok:
            subtitle_var.set((message or "Opened your AI tool. Answer it there.")[:46])
        else:
            webbrowser.open(fallback_url)
            subtitle_var.set("No live return. Opened in AIWatcher.")
        schedule_auto_collapse(2500)

    def open_waiting_row(index: int) -> None:
        if not 0 <= index < len(waiting_row_urls):
            return
        if (
            index < len(waiting_row_actions)
            and waiting_row_actions[index] == "copy_compact"
            and index < len(waiting_row_commands)
            and waiting_row_commands[index]
        ):
            copy_compact_row(index)
            return
        if (
            index < len(waiting_row_actions)
            and waiting_row_actions[index] == "copy_fresh_start"
            and index < len(waiting_row_session_ids)
            and waiting_row_session_ids[index]
        ):
            primary_session_id_var.set(waiting_row_session_ids[index])
            primary_url_var.set(waiting_row_urls[index])
            copy_fresh_start_from_companion()
            return
        if (
            index < len(waiting_row_return)
            and waiting_row_return[index]
            and index < len(waiting_row_session_ids)
            and waiting_row_session_ids[index]
        ):
            request_runtime_return(waiting_row_session_ids[index], waiting_row_urls[index])
            return
        webbrowser.open(waiting_row_urls[index])
        schedule_auto_collapse(1500)

    def open_signal() -> None:
        target = signal_url_var.get().strip()
        if target:
            webbrowser.open(target)
            schedule_auto_collapse(1500)

    def apply_waiting_rows() -> None:
        rows = 0 if collapsed.get() else visible_waiting_rows()
        for index in range(min(rows, len(waiting_row_texts))):
            section = waiting_row_sections[index] if index < len(waiting_row_sections) else ""
            label = waiting_row_texts[index]
            row_widgets[index][1].configure(text=f"{section.upper()}   {label}" if section else label)
            row_widgets[index][2].configure(
                text=row_action_label(index),
            )
            kind = waiting_row_kinds[index] if index < len(waiting_row_kinds) else ""
            action = waiting_row_actions[index] if index < len(waiting_row_actions) else ""
            if action == "none":
                row_widgets[index][2].pack_forget()
                row_widgets[index][3].configure(text="")
            elif kind == "compact":
                # The stage where a waiting row shows what the session wants;
                # no button once the compaction is under way.
                tag = waiting_row_tags[index] if index < len(waiting_row_tags) else ""
                row_widgets[index][3].configure(text=tag)
                if action:
                    row_widgets[index][2].pack(side="right")
                else:
                    row_widgets[index][2].pack_forget()
            elif kind.startswith("prompt_"):
                # "working 3 min" or "done" where a waiting row says what it wants.
                tag = waiting_row_tags[index] if index < len(waiting_row_tags) else ""
                row_widgets[index][3].configure(text=tag)
                row_widgets[index][2].pack(side="right")
            else:
                row_widgets[index][2].pack(side="right")
                wants = waiting_row_wants[index] if index < len(waiting_row_wants) else ""
                row_widgets[index][3].configure(text=f"wants: {wants}" if wants else "")
        if rows == rows_shown.get():
            return
        for row_frame, _row_label, _row_button, _row_wants in row_widgets:
            row_frame.pack_forget()
        if rows_packed.get():
            rows_frame.pack_forget()
            rows_packed.set(False)
        if rows:
            rows_frame.pack(fill="both", expand=True)
            rows_packed.set(True)
            for index in range(rows):
                row_widgets[index][0].pack(fill="x")
        rows_shown.set(rows)
        if not collapsed.get():
            resize_window(expanded_width, expanded_height + row_height * rows)

    def should_show_window() -> bool:
        if state_var.get() == "offline":
            return True
        if state_var.get() in {
            "prompt_gate", "command_gate", "control_recommended", "control_review", "optimize_available",
            "clipboard_confirm", "session_waiting",
        }:
            return True
        if state_var.get() in {"session_finished", "away_digest", "compact_recommended", "delivery_review_ready"}:
            return True
        if visibility == "nudges-only":
            return False
        if visibility == "ai-apps":
            return _foreground_looks_like_ai_work()
        return True

    def apply_window_visibility() -> None:
        try:
            if should_show_window():
                root.deiconify()
                root.attributes("-topmost", True)
            else:
                root.withdraw()
        except tk.TclError:
            pass

    def schedule_auto_collapse(delay_ms: int = 6000) -> None:
        if collapsed.get():
            return
        deadline = time.monotonic() + (delay_ms / 1000.0)
        existing_deadline = float(auto_collapse_deadline.get() or 0.0)
        if existing_deadline and existing_deadline <= deadline:
            return
        auto_collapse_deadline.set(deadline)
        auto_collapse_token.set(auto_collapse_token.get() + 1)
        token = auto_collapse_token.get()

        def collapse_if_current() -> None:
            try:
                if token == auto_collapse_token.get() and not collapsed.get():
                    auto_collapse_deadline.set(0.0)
                    # A pending hard gate keeps the bar open; the cleared
                    # deadline lets the next refresh schedule again once it
                    # resolves.
                    if state_var.get() in {"prompt_gate", "command_gate"}:
                        return
                    set_collapsed(True)
            except tk.TclError:
                pass

        try:
            root.after(delay_ms, collapse_if_current)
        except tk.TclError:
            pass

    def update_attention_style() -> None:
        needs_attention = (
            activity_tone_var.get() == "attention"
            if activity_tone_var.get()
            else state_var.get() in {
                "prompt_gate", "command_gate", "control_recommended", "control_review", "optimize_available",
                "clipboard_confirm", "session_waiting",
            }
        )
        # A hard gate holds the user's own prompt until they act; softer
        # attention blocks nothing. Mirrors the Swift bar's isHardGate.
        hard_gate = state_var.get() in {"prompt_gate", "command_gate"}
        attention_bg = "#ed6a24" if pulse_var.get() else "#b84816"
        # Collapsed, the bubble stays white in every state; attention is
        # carried by an orange rim on the bubble's edge and the mark's blue
        # ring turning orange, the same job it does in the dashboard favicon
        # -- not by flooding the ground.
        shell_bg = "#ffffff" if collapsed.get() else "#090d14"
        root.configure(bg=shell_bg)
        style.configure("Presence.TFrame", background=shell_bg)
        style.configure("PresenceTitle.TLabel", background=shell_bg)
        style.configure("PresenceMuted.TLabel", background=shell_bg)
        style.configure("PresenceDrag.TLabel", background=shell_bg)
        # The button swaps back to Presence.TButton when nothing needs the
        # user, so this style is only ever on screen while it does: it is
        # always the orange chip, pulsing between the two oranges.
        style.configure("PresenceAttention.TButton", background=attention_bg, foreground="#ffffff")
        primary_button.configure(style="PresenceAttention.TButton" if needs_attention else "Presence.TButton")
        # The blue ring is the mark's state channel, speaking the dashboard
        # favicon's vocabulary: orange when something needs you, mint while a
        # session is actively working (#43d9a3, the favicon's healthy-live
        # colour), brand blue at rest.
        ring_colour = (
            attention_bg if needs_attention
            else ("#43d9a3" if int(working_count_var.get() or 0) > 0 else "#0052F5")
        )
        mark_canvas.delete("all")
        _paint_brand_mark(tk, mark_canvas, 26.0, ring_colour, "#DCE6F6")
        collapsed_canvas.configure(bg="#ffffff")
        collapsed_canvas.delete("all")
        orbit_item["id"] = None
        _paint_brand_mark(tk, collapsed_canvas, bubble.mark_height, ring_colour, "#141314")
        collapsed_canvas.move("all", bubble.mark.x0, bubble.mark.y0)
        # Companion-only expression inside the front ring. The dashboard and
        # expanded bar keep the official mark untouched; this small viewport
        # makes the persistent bubble feel watchful without introducing an
        # unrelated mascot or another asset format.
        working = int(working_count_var.get() or 0) > 0
        offline = state_var.get() == "offline"
        reduce_motion = reduce_motion_active()
        gaze_x, gaze_y, eye_colour = _companion_expression(
            state_var.get(),
            working=working,
            needs_attention=needs_attention,
            phase=int(expression_phase_var.get()),
            reduce_motion=reduce_motion or motion_mode_var.get() != "expressive",
        )
        face = bubble.face
        face_radius = face.height / 2.0
        collapsed_canvas.create_oval(face.x0, face.y0, face.x0 + face.height, face.y1, fill="#141314", outline="")
        collapsed_canvas.create_oval(face.x1 - face.height, face.y0, face.x1, face.y1, fill="#141314", outline="")
        collapsed_canvas.create_rectangle(
            face.x0 + face_radius, face.y0, face.x1 - face_radius, face.y1, fill="#141314", outline="",
        )
        # A focus lock thickens the gaze; both come from the face's own size.
        eye_width = bubble.eye_thickness * (1.4 if int(focus_lock_ticks_var.get() or 0) > 0 else 1.0)
        # Gaze is a nudge in units of the face, not a fixed pixel, so it stays
        # proportionate if the bubble is ever drawn larger.
        gaze_step = max(1.0, face.height * 0.12)
        for eye in bubble.eyes:
            collapsed_canvas.create_line(
                eye.x0 + gaze_x * gaze_step, eye.y0 + gaze_y * gaze_step,
                eye.x1 + gaze_x * gaze_step, eye.y1 + gaze_y * gaze_step,
                fill=eye_colour, width=eye_width, capstyle=tk.ROUND,
            )
        if int(focus_lock_ticks_var.get() or 0) > 0 and not needs_attention:
            collapsed_canvas.create_oval(*bubble.focus_rim, outline=ring_colour, width=2)
        # The running indicator: a mint arc travelling around the bubble's rim
        # while any session is working. Attention outranks it. A third of the
        # circle at 4 wide, the Swift bar's 5 x 33% scaled to this canvas.
        # spin_orbit owns the angle -- drawing only places the arc where that
        # clock has already put it.
        if int(working_count_var.get() or 0) > 0 and not needs_attention:
            orbit_item["id"] = collapsed_canvas.create_arc(
                *bubble.rim, start=orbit_angle_var.get(), extent=120,
                style="arc", outline="#43d9a3", width=bubble.orbit_stroke,
            )
            # The frame timer only exists while there is an arc to move, so
            # drawing one is what starts it.
            if not orbit_item["running"]:
                orbit_item["running"] = True
                try:
                    root.after(orbit_frame_ms, spin_orbit)
                except tk.TclError:
                    orbit_item["running"] = False
        # The attention rim: the whole circle in orange while something needs
        # the user. Tk has no layer opacity, so a hard gate pulses between
        # orange and a pale tint of it rather than between two oranges --
        # the latter reads as flicker, not as a signal. Softer attention
        # holds steady.
        if needs_attention:
            rim_colour = "#ed6a24"
            if hard_gate and not reduce_motion and motion_mode_var.get() != "quiet" and not pulse_var.get():
                rim_colour = "#f8cdb5"
            collapsed_canvas.create_oval(*bubble.rim, outline=rim_colour, width=bubble.orbit_stroke)
        # The collapsed bubble reserves numbers for things needing attention.
        # Completed work is reward/status text after expansion, not a mystery
        # count sitting on screen all day.
        waiting_count = int(waiting_count_var.get() or 0)
        finished_count = int(finished_count_var.get() or 0)
        review_count = int(review_count_var.get() or 0)
        badge_count = waiting_count or review_count
        # A hard gate carries no count and was the one attention state with
        # nothing on the bubble but the mark's ring; "!" keeps the blocked
        # state from being the quietest one.
        badge_text = str(badge_count) if badge_count > 0 else ("!" if hard_gate else "")
        if badge_text:
            collapsed_canvas.create_oval(
                *bubble.badge,
                fill=attention_bg if waiting_count > 0 or needs_attention else "#0052F5", outline="",
            )
            badge_x, badge_y = bubble.badge.centre
            collapsed_canvas.create_text(
                badge_x, badge_y, text=badge_text, fill="#ffffff",
                font=("Helvetica", bubble.badge_font_size, "bold"),
            )
        for button in (plan_button, ask_button, scan_button, console_button):
            button.state(["disabled"] if offline else ["!disabled"])
        if not restart_requested:
            primary_button.state(["!disabled"])
        attention_layout = has_primary_action() and not collapsed.get()
        # Context review queues need both scopes: a row-level Watch button and
        # a Review all button for the full Context Health list.
        should_show_primary = attention_layout and (
            visible_waiting_rows() == 0 or state_var.get() in {"control_review", "context_review"}
        )
        # The missed-nudge chip borrows Plan/Ask's slot -- both remain a click
        # away in the Console, a recent signal is not.
        show_chip = bool(signal_chip_var.get()) and not attention_layout and not collapsed.get()
        hide_plan_ask = attention_layout or show_chip
        if show_chip and not signal_packed.get():
            signal_button.pack(side="left", padx=(8, 4), before=scan_button)
            signal_packed.set(True)
        elif not show_chip and signal_packed.get():
            signal_button.pack_forget()
            signal_packed.set(False)
        if pressure_available_var.get() and not collapsed.get() and not attention_layout:
            if not meter_packed.get():
                meter_canvas.pack(side="left", padx=(8, 0))
                meter_packed.set(True)
            meter_canvas.configure(bg="#090d14")
            meter_canvas.delete("all")
            pct = max(0, min(100, int(pressure_pct_var.get() or 0)))
            meter_colour = {"critical": "#ed6a24", "warning": "#ef9f27"}.get(
                pressure_severity_var.get(), "#4f83f0",
            )
            meter_canvas.create_rectangle(0, 2, 62, 7, fill="#2a3342", outline="")
            if pct:
                meter_canvas.create_rectangle(0, 2, int(62 * pct / 100), 7, fill=meter_colour, outline="")
        elif meter_packed.get():
            meter_canvas.pack_forget()
            meter_packed.set(False)
        # Repacked right-to-left, each anchored to the nearest button that is
        # actually packed: `before=` a forgotten widget is a TclError, and both
        # the chip and the attention layout can leave ask or scan unpacked.
        if hide_plan_ask:
            if ask_packed.get():
                ask_button.pack_forget()
                ask_packed.set(False)
        elif not collapsed.get() and not ask_packed.get():
            ask_button.pack(side="left", padx=(0, 4), before=scan_button if scan_packed.get() else console_button)
            ask_packed.set(True)
        if hide_plan_ask:
            if plan_packed.get():
                plan_button.pack_forget()
                plan_packed.set(False)
        elif not collapsed.get() and not plan_packed.get():
            plan_button.pack(side="left", padx=(8, 4), before=ask_button)
            plan_packed.set(True)
        if attention_layout:
            if scan_packed.get():
                scan_button.pack_forget()
                scan_packed.set(False)
        elif not collapsed.get() and not scan_packed.get():
            scan_button.pack(side="left", padx=(0, 4), before=console_button)
            scan_packed.set(True)
        if should_show_primary and not primary_packed.get():
            primary_button.pack(side="left", padx=(0, 4), before=continue_button if continue_packed.get() else console_button)
            primary_packed.set(True)
        elif not should_show_primary and primary_packed.get():
            primary_button.pack_forget()
            primary_packed.set(False)
        should_show_continue = (
            (
                bool(continue_session_id_var.get().strip())
                or (
                    state_var.get() == "prompt_gate"
                    and continue_action_var.get() == "run_original_prompt"
                    and bool(continue_url_var.get().strip())
                )
            )
            and not collapsed.get()
        )
        if should_show_continue and not continue_packed.get():
            continue_button.pack(side="left", padx=(0, 4), before=skip_button if skip_packed.get() else console_button)
            continue_packed.set(True)
        elif not should_show_continue and continue_packed.get():
            continue_button.pack_forget()
            continue_packed.set(False)
        should_show_skip = bool(skip_state_var.get().strip()) and state_var.get() != "prompt_gate" and not collapsed.get()
        if should_show_skip and not skip_packed.get():
            skip_button.pack(side="left", padx=(0, 4), before=console_button)
            skip_packed.set(True)
        elif not should_show_skip and skip_packed.get():
            skip_button.pack_forget()
            skip_packed.set(False)
        apply_waiting_rows()
        apply_window_visibility()

    # The arc used to advance 103 degrees on each 800ms pulse tick: three
    # frames to a revolution, which reads as a stutter rather than a spin. It
    # now has its own frame clock, and moves only its own canvas item --
    # update_attention_style tears down and rebuilds both canvases, which is
    # far too heavy to run at frame rate.
    #
    # 9 degrees per 50ms is the same two-second revolution at 20fps. 30fps
    # measured 2.2% of a core against 0.4% for the old stutter, which is too
    # much to ask of a bar that is meant to sit open all day; 20fps is a third
    # cheaper and a two-second rotation does not read as choppy at it.
    orbit_frame_ms = 50
    orbit_step_degrees = 9

    def spin_orbit() -> None:
        item = orbit_item["id"]
        spinning = (
            item is not None
            and collapsed.get()
            # winfo_viewable is a cheap mapped-state check: a withdrawn bubble
            # (nudges-only, or an ai-apps window with no AI app in front) has
            # nothing on screen to animate.
            and bool(root.winfo_viewable())
            and not reduce_motion_active()
            and motion_mode_var.get() != "quiet"
        )
        if not spinning:
            # Stop rather than idle at a slower tick. A bar that sits open all
            # day should cost nothing while nothing is running, and the next
            # redraw that draws an arc starts the timer again.
            orbit_item["running"] = False
            return
        orbit_angle_var.set((int(orbit_angle_var.get()) - orbit_step_degrees) % 360)
        try:
            collapsed_canvas.itemconfigure(item, start=orbit_angle_var.get())
        except tk.TclError:
            orbit_item["id"] = None
        try:
            root.after(orbit_frame_ms, spin_orbit)
        except tk.TclError:
            orbit_item["running"] = False

    def pulse_attention() -> None:
        reduce_motion = reduce_motion_active()
        visible_surface = should_show_window()
        animate_surface = (
            not reduce_motion
            and motion_mode_var.get() != "quiet"
            and collapsed.get()
            and visible_surface
            and (int(working_count_var.get() or 0) > 0 or state_var.get() in {"prompt_gate", "command_gate"})
        )
        if animate_surface:
            pulse_var.set(not pulse_var.get())
            if motion_mode_var.get() == "expressive" and int(working_count_var.get() or 0) > 0:
                expression_phase_var.set((int(expression_phase_var.get()) + 1) % 4)
            if int(focus_lock_ticks_var.get() or 0) > 0:
                focus_lock_ticks_var.set(int(focus_lock_ticks_var.get()) - 1)
        if visible_surface:
            update_attention_style()
        try:
            root.after(800 if animate_surface else (30000 if not visible_surface else 8000), pulse_attention)
        except tk.TclError:
            pass

    # Reaching the dashboard is a measurement, and a failed probe only says
    # "no answer inside the budget": the dashboard's first scan over a large
    # local history takes tens of seconds, and every probe during it times out.
    # Painting "UI offline" there pointed the user at Restart, which starts
    # that scan again, so the false alarm renewed itself. Unreachable therefore
    # carries its own state until it has outlived that scan window, and only
    # then becomes a claim about the dashboard.
    #
    # Both numbers are stopgaps: nothing local reports how long a startup scan
    # will take, so there is nothing to derive them from. 45s covers the "tens
    # of seconds" a first scan is documented to take, and three failures keep a
    # single slow response from changing the surface at all.
    offline_after_failures = 3
    offline_after_seconds = 45.0
    probe = {"failures": 0.0, "last_ok": 0.0, "since": time.monotonic()}

    def apply_connecting_state() -> None:
        # Unreachable, but not for long enough to claim the dashboard is down,
        # so this reports which of the two it cannot tell instead of offering
        # Restart for a dashboard that is still starting. After a good read the
        # last rows stay put and only the window title changes: a three-second
        # gap should not rewrite the surface or pop the bubble open.
        root.title("AIWatcher Companion: connecting")
        if float(probe["last_ok"]) <= 0.0:
            state_var.set("connecting")
            title_var.set("Connecting\u2026")
            subtitle_var.set("Dashboard is starting. Its first scan can take a moment.")
        update_attention_style()

    def refresh_state() -> None:
        # The request runs off the Tk main thread. Blocking it here froze the
        # whole bar for the length of the timeout on every tick -- dragging,
        # hover and the running arc all stopped -- and it was worst exactly
        # when the dashboard was slow or down and every probe spent its full
        # budget, which is when the bar most needs to look alive. Off the main
        # thread the budget can also be generous enough to ride out a slow
        # answer instead of counting it as a failure.
        #
        # The worker only writes to a dict and the main thread picks the result
        # up, because Tk calls are not safe from another thread. Same shape as
        # the usage panel's fetch below.
        request_url = f"{url.rstrip('/')}/api/companion-state"
        box: dict[str, object] = {}

        def fetch() -> None:
            try:
                with urllib.request.urlopen(request_url, timeout=2.5) as response:
                    box["payload"] = json.loads(response.read().decode("utf-8"))
            except (OSError, ValueError):
                box["error"] = True
            finally:
                box["done"] = True

        def settle() -> None:
            if not box.get("done"):
                try:
                    root.after(60, settle)
                except tk.TclError:
                    pass
                return
            payload = box.get("payload")
            try:
                if isinstance(payload, dict):
                    probe["failures"] = 0.0
                    probe["last_ok"] = time.monotonic()
                    apply_state(payload)
                else:
                    probe["failures"] = float(probe["failures"]) + 1.0
                    unreachable_since = float(probe["last_ok"]) or float(probe["since"])
                    if (
                        float(probe["failures"]) >= offline_after_failures
                        and time.monotonic() - unreachable_since >= offline_after_seconds
                    ):
                        apply_offline_state()
                    else:
                        apply_connecting_state()
                update_attention_style()
                schedule_auto_collapse(10000 if has_primary_action() else 4000)
            except tk.TclError:
                pass
            try:
                root.after(3000, refresh_state)
            except tk.TclError:
                pass

        threading.Thread(target=fetch, daemon=True).start()
        settle()

    usage_state: dict[str, object] = {"window": None, "session_id": "", "request": 0}

    def open_usage() -> None:
        existing = usage_state["window"]
        if existing is not None and existing.winfo_exists():
            existing.lift()
            return
        panel = tk.Toplevel(root)
        panel.title("AIWatcher · Session usage")
        panel.geometry("560x610")
        panel.attributes("-topmost", True)
        usage_state["window"] = panel
        body = ttk.Frame(panel, padding=16)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="Session usage", font=("TkDefaultFont", 16, "bold")).pack(anchor="w", pady=(0, 10))
        picker = ttk.Combobox(body, state="readonly")
        picker.pack(fill="x", pady=(0, 10))
        text_vars = {key: tk.StringVar(value="") for key in (
            "identity", "context", "note", "totals", "input", "cache", "cost", "boundary", "updated",
        )}
        ttk.Label(body, textvariable=text_vars["identity"], wraplength=520).pack(anchor="w")
        ttk.Label(body, textvariable=text_vars["context"], font=("TkDefaultFont", 18, "bold"), wraplength=520).pack(anchor="w", pady=(12, 6))
        meter_slot = ttk.Frame(body)
        meter_slot.pack(fill="x")
        meter = ttk.Progressbar(meter_slot, maximum=100)
        for key in ("note", "totals", "input", "cache", "cost", "boundary", "updated"):
            ttk.Label(body, textvariable=text_vars[key], wraplength=520).pack(anchor="w", pady=(5, 2))
        footer = ttk.Frame(body)
        footer.pack(side="bottom", fill="x", pady=(12, 0))
        selected_url = {"value": ""}
        open_button = ttk.Button(footer, text="Open session in UI", command=lambda: webbrowser.open(selected_url["value"]))
        open_button.pack(side="left")
        choices: list[dict[str, object]] = []

        def refresh_usage() -> None:
            usage_state["request"] = int(usage_state["request"]) + 1
            request_id = usage_state["request"]
            query = urllib.parse.urlencode({"session_id": usage_state["session_id"]})
            request_url = f"{_api_base(url)}/api/companion-usage?{query}"
            for var in text_vars.values():
                var.set("")
            text_vars["context"].set("Loading…")
            meter.pack_forget()
            open_button.state(["disabled"])
            result: dict[str, object] = {}

            def fetch() -> None:
                try:
                    with urllib.request.urlopen(request_url, timeout=5) as response:
                        result["payload"] = json.loads(response.read().decode("utf-8"))
                except (OSError, ValueError):
                    result["error"] = True
                finally:
                    result["done"] = True

            def render_result() -> None:
                if not panel.winfo_exists() or request_id != usage_state["request"]:
                    return
                if not result.get("done"):
                    root.after(80, render_result)
                    return
                payload = result.get("payload")
                if not isinstance(payload, dict):
                    text_vars["context"].set("Usage unavailable")
                    text_vars["note"].set("Could not reach the local dashboard. Retry with Refresh; monitoring continues independently.")
                    return
                choices[:] = payload.get("sessions") or []
                picker["values"] = [row["label"] for row in choices]
                selected = payload.get("selected")
                if not isinstance(selected, dict):
                    text_vars["context"].set("Usage unavailable")
                    text_vars["note"].set(str(payload.get("reason") or "Session not available."))
                    return
                sid = str(selected["session_id"])
                usage_state["session_id"] = sid
                for index, choice in enumerate(choices):
                    if choice["session_id"] == sid:
                        picker.current(index)
                        break
                context = selected.get("context") or {}
                available = context.get("available")
                text_vars["identity"].set(f"{selected['model']} · {selected['state']}\nSession {sid}")
                text_vars["context"].set(str(selected["context_label"]) if available else "Context unavailable")
                if available:
                    meter["value"] = min(100, max(0, context.get("pct_of_turn_limit") or 0))
                    meter.pack(fill="x", pady=8)
                text_vars["note"].set(str(selected["context_note"] if available else selected["context_label"]))
                for key, value in (("totals", "usage_label"), ("input", "input_label"), ("cache", "cache_label"), ("cost", "cost_label"), ("boundary", "breakdown_note")):
                    text_vars[key].set(str(selected[value]))
                text_vars["updated"].set(f"Last activity: {selected.get('updated_at') or 'unknown'}\nTotals through: {selected.get('totals_updated_at') or 'unknown'}\n{payload['total_recent_sessions']} recent sessions · picker limited to 20")
                selected_url["value"] = f"{_api_base(url)}{selected['url']}"
                open_button.state(["!disabled"])

            threading.Thread(target=fetch, daemon=True).start()
            root.after(80, render_result)

        def select_session(_event: object) -> None:
            index = picker.current()
            if 0 <= index < len(choices):
                usage_state["session_id"] = choices[index]["session_id"]
                refresh_usage()

        picker.bind("<<ComboboxSelected>>", select_session)
        ttk.Button(footer, text="Refresh", command=refresh_usage).pack(side="right")
        refresh_usage()

    plan_button = ttk.Button(frame, text="Plan", width=6, style="Presence.TButton", command=open_prompt)
    plan_button.pack(side="left", padx=(8, 4))
    ask_button = ttk.Button(frame, text="Ask", width=5, style="Presence.TButton", command=open_ask)
    ask_button.pack(side="left", padx=(0, 4))
    scan_button = ttk.Button(frame, text="Scan", width=6, style="Presence.TButton", command=scan_now)
    scan_button.pack(side="left", padx=(0, 4))
    primary_button = ttk.Button(frame, textvariable=primary_label_var, width=11, style="Presence.TButton", command=open_primary)
    primary_button.pack(side="left", padx=(0, 4))
    continue_button = ttk.Button(frame, textvariable=continue_label_var, width=9, style="Presence.TButton", command=continue_here)
    skip_button = ttk.Button(frame, textvariable=skip_label_var, width=9, style="Presence.TButton", command=skip_current)
    console_button = ttk.Button(frame, text="UI", width=4, style="Presence.TButton", command=open_dashboard)
    console_button.pack(side="left")
    usage_button = ttk.Button(frame, text="Usage", width=7, style="Presence.TButton", command=open_usage)
    usage_button.pack(side="left", padx=(4, 0))
    ttk.Button(frame, text="-", width=2, style="PresenceMini.TButton", command=toggle_collapsed).pack(side="left", padx=(4, 0))
    signal_button = ttk.Button(frame, textvariable=signal_chip_var, width=10, style="Presence.TButton", command=open_signal)
    rows_frame = ttk.Frame(root, style="Presence.TFrame")
    row_widgets: list[tuple[ttk.Frame, ttk.Label, ttk.Button, ttk.Label]] = []
    for row_index in range(max_waiting_rows):
        row_frame = ttk.Frame(rows_frame, padding=(14, 2), style="Presence.TFrame")
        ttk.Label(row_frame, text="●", style="PresenceDot.TLabel").pack(side="left", padx=(0, 6))
        row_label = ttk.Label(row_frame, text="", style="PresenceMuted.TLabel")
        row_label.pack(side="left", fill="x", expand=True)
        row_button = ttk.Button(
            row_frame, text="Open", width=7, style="Presence.TButton",
            command=lambda index=row_index: open_waiting_row(index),
        )
        row_button.pack(side="right")
        row_wants = ttk.Label(row_frame, text="", style="PresenceMuted.TLabel")
        row_wants.pack(side="right", padx=(0, 8))
        row_widgets.append((row_frame, row_label, row_button, row_wants))
    # The collapsed state is a Loom-style bubble: the mark alone on a plain
    # white ground, no lettering and no border chrome. update_attention_style
    # repaints it, turning the mark's blue ring orange when attention is due.
    collapsed_canvas = tk.Canvas(
        collapsed_frame,
        width=collapsed_canvas_width,
        height=collapsed_canvas_height,
        bg="#ffffff",
        highlightthickness=1,
        highlightcolor="#0052F5",
        highlightbackground="#ffffff",
        bd=0,
        takefocus=True,
    )
    collapsed_canvas.pack(fill="both", expand=True)

    # The canvas covers the whole bubble, so it has to carry the move as well
    # as the click: a press that travels more than a few pixels drags the
    # window, and a press released in place expands the bar.
    collapsed_press = {"x": 0, "y": 0, "moved": False}

    def collapsed_button_press(event: tk.Event) -> None:
        collapsed_press["x"] = int(event.x_root)
        collapsed_press["y"] = int(event.y_root)
        collapsed_press["moved"] = False
        drag_start["x"] = int(event.x_root) - int(root.winfo_rootx())
        drag_start["y"] = int(event.y_root) - int(root.winfo_rooty())

    def collapsed_button_motion(event: tk.Event) -> None:
        if abs(int(event.x_root) - collapsed_press["x"]) + abs(int(event.y_root) - collapsed_press["y"]) > 3:
            collapsed_press["moved"] = True
        if collapsed_press["moved"]:
            move_window(event)

    def collapsed_button_release(_event: tk.Event) -> None:
        if not collapsed_press["moved"]:
            toggle_collapsed()

    collapsed_canvas.bind("<ButtonPress-1>", collapsed_button_press)
    collapsed_canvas.bind("<B1-Motion>", collapsed_button_motion)
    collapsed_canvas.bind("<ButtonRelease-1>", collapsed_button_release)
    collapsed_canvas.bind("<Return>", lambda _event: toggle_collapsed())
    collapsed_canvas.bind("<space>", lambda _event: toggle_collapsed())
    set_collapsed(True)
    refresh_state()
    pulse_attention()
    spin_orbit()
    root.mainloop()
    if restart_requested:
        return 0 if _launch_aiwatcher_restart(url, position, visibility, interval) else 2
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

    # The notification wears the brand the same way the collapsed bubble does:
    # the mark on a plain white ground, ink type, and severity carried by the
    # mark itself -- the blue ring turns orange when the signal is critical.
    # The ground never floods.
    shell = "#ffffff"
    ink = "#141314"
    root = tk.Tk()
    root.title("AIWatcher")
    root.configure(bg=shell)
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
    style.configure("AIW.TFrame", background=shell)
    style.configure("AIW.TLabel", background=shell, foreground="#413f42", font=("Helvetica", 13))
    style.configure("AIWTitle.TLabel", background=shell, foreground=ink, font=("Helvetica", 20, "bold"))
    style.configure("AIWMuted.TLabel", background=shell, foreground="#6d6a6e", font=("Helvetica", 12))
    style.configure(
        "AIWSeverity.TLabel",
        background=shell,
        foreground="#cc5417" if (severity or "").lower() == "critical" else "#9d6b12",
        font=("Helvetica", 12, "bold"),
    )
    style.configure("AIW.TButton", font=("Helvetica", 13, "bold"), padding=(14, 8))

    frame = ttk.Frame(root, padding=22, style="AIW.TFrame")
    frame.pack(fill="both", expand=True)

    header = ttk.Frame(frame, style="AIW.TFrame")
    header.pack(fill="x")
    mark_canvas = tk.Canvas(header, width=38, height=30, bg=shell, highlightthickness=0, bd=0)
    _paint_brand_mark(
        tk, mark_canvas, 30.0,
        "#ed6a24" if (severity or "").lower() == "critical" else "#0052F5",
        ink,
    )
    mark_canvas.pack(side="left", padx=(0, 10))
    ttk.Label(header, text=title or "Start a fresh AI session", style="AIWTitle.TLabel", wraplength=560).pack(
        side="left", fill="x", expand=True, anchor="w"
    )
    ttk.Label(header, text=severity or "warning", style="AIWSeverity.TLabel").pack(side="right", padx=(16, 0))

    ttk.Label(frame, text=body or "AIWatcher found context pressure that may waste your next turns.", style="AIW.TLabel", wraplength=700).pack(
        fill="x", pady=(12, 18), anchor="w"
    )

    status = tk.StringVar(value="Private by default; auto-hides in 20 seconds")
    button_row = ttk.Frame(frame, style="AIW.TFrame")
    button_row.pack(fill="x", pady=(0, 12))

    def show_saved(message: str) -> None:
        status.set(message)
        root.after(2500, root.destroy)

    def inspect_session() -> None:
        _record_intervention_action(base, intervention_fingerprint, "acted")
        webbrowser.open(f"{base}/?session={urllib.parse.quote(session_id)}")
        show_saved("Opened the evidence. Review it before continuing the run.")

    def return_to_session() -> None:
        """Focus the tool the session is already in.

        No clipboard: this signal is not a handoff. When there is nothing to
        attach to, say why and leave the dashboard one click away rather than
        reporting a return that did not happen.
        """
        _record_intervention_action(base, intervention_fingerprint, "acted")
        result = _runtime_return_result(base, session_id)
        if result.get("ok"):
            show_saved(str(result.get("message") or "Opened your AI tool. Answer it there to continue."))
            return
        reason = str(result.get("message") or result.get("error") or "").strip()
        webbrowser.open(f"{base}/?session={urllib.parse.quote(session_id)}")
        show_saved(
            f"Could not reach the tool from here ({reason}). Opened the session in AIWatcher instead."
            if reason
            else "Could not reach the tool from here. Opened the session in AIWatcher instead."
        )

    def primary_action() -> None:
        if primary_mode == "return":
            return_to_session()
            return
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
    parser.add_argument("--tray", action="store_true", help="Run the OS menu-bar/system-tray entry point")
    parser.add_argument("--url", required=True)
    parser.add_argument("--prompt-url")
    parser.add_argument(
        "--position",
        choices=("bottom-right", "bottom-left", "top-right", "top-left"),
        default="bottom-right",
    )
    parser.add_argument(
        "--visibility",
        choices=("always", "ai-apps", "nudges-only"),
        default="always",
        help="When the Companion presence is visible",
    )
    parser.add_argument("--interval", type=int, default=30, help="Seconds between Companion scans")
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
    if args.tray:
        return run_native_tray(args.url, prompt_url=args.prompt_url)
    if args.presence:
        return run_native_presence(
            args.url,
            prompt_url=args.prompt_url,
            position=args.position,
            visibility=args.visibility,
            interval=args.interval,
        )
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
