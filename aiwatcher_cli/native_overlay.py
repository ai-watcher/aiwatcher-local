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
import time
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
    "waiting": "session_blocked",
    "blocked": "session_blocked",
    "session_waiting": "session_blocked",
}


def _normalize_signal_kind(value: str | None) -> str:
    normalized = (value or "generic").strip().lower().replace("-", "_").replace(" ", "_")
    normalized = _SIGNAL_ALIASES.get(normalized, normalized)
    return normalized if normalized in {"session_blocked", "critical_context", "loop", "velocity", "runway", "generic"} else "generic"


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

        // The notification wears the brand the same way the collapsed bubble
        // does: the mark on a plain white ground, ink type, and severity
        // carried by the mark itself -- the blue ring turns orange when the
        // signal is critical, the job that ring does everywhere else. The
        // ground never floods.
        window.isOpaque = false
        window.backgroundColor = .clear
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

        statusLabel = NSTextField(labelWithString: "Local-only. Prompt/source content is not stored in this decision.")
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
    var collapsedMarkView: NSView!
    var collapsedBlueRing: CALayer!
    var collapsedBadge: NSTextField!
    var primaryURL = dashboardURL
    var primaryAction = "open_url"
    var primarySessionID = ""
    var primaryRuntimeAvailable = false
    var continueAction = ""
    var continueURL = ""
    var continueSessionID = ""
    var continueReason = ""
    var continueExpectedTokens = 0
    var skipState = ""
    var skipSessionID = ""
    var skipProject = ""
    var collapsed = false
    var stateName = "watching"
    var pulseOn = false
    var autoCollapseToken = 0
    var autoCollapseDeadline: Date? = nil
    var suppressPassiveRefreshUntil: Date? = nil
    var pendingClipboardOverrideSessionID = ""
    var waitingRowTexts: [String] = []
    var waitingURLs: [String] = []
    var waitingCount = 0
    var detailText = ""
    var rowDots: [NSView] = []
    var rowLabels: [NSTextField] = []
    var rowButtons: [NSButton] = []
    let expandedWidth: CGFloat = 560
    let headerHeight: CGFloat = 58
    let rowHeight: CGFloat = 34
    let maxWaitingRows = 3
    let collapsedWidth: CGFloat = 44
    let collapsedHeight: CGFloat = 44
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

    // One row per waiting session, but only once there is a queue to draw:
    // a single waiting session keeps the original one-line layout.
    var visibleWaitingRows: Int {
        if stateName != "session_waiting" || waitingRowTexts.count < 2 {
            return 0
        }
        return min(waitingRowTexts.count, maxWaitingRows)
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
        brandMarkView.layer?.addSublayer(makeBrandMark(height: 26, ink: brandInkOnDark).mark)
        rootView.addSubview(brandMarkView)

        titleLabel = NSTextField(labelWithString: "AIWatcher")
        titleLabel.frame = NSRect(x: 66, y: 31, width: 220, height: 17)
        titleLabel.font = NSFont.systemFont(ofSize: 12, weight: .semibold)
        titleLabel.textColor = NSColor.white
        rootView.addSubview(titleLabel)

        subtitleLabel = NSTextField(labelWithString: "Watching quietly")
        subtitleLabel.frame = NSRect(x: 66, y: 12, width: 250, height: 16)
        subtitleLabel.font = NSFont.systemFont(ofSize: 9)
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
        skipButton.frame = NSRect(x: 434, y: 15, width: 48, height: 28)
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
        collapseButton.frame = NSRect(x: 538, y: 37, width: 18, height: 18)
        collapseButton.bezelStyle = .rounded
        collapseButton.controlSize = .mini
        collapseButton.toolTip = "Minimize AIWatcher"
        rootView.addSubview(collapseButton)

        // Collapsed, the companion is a Loom-style bubble: the mark alone on a
        // white circle, no ring border and no other chrome. The mark is hosted
        // in a plain NSView rather than the button's layer -- NSButton uses
        // flipped layer geometry, which drew the mark upside down.
        collapsedMarkView = NSView(frame: NSRect(x: 9.7, y: 12, width: 24.6, height: 20))
        collapsedMarkView.wantsLayer = true
        let collapsedMark = makeBrandMark(height: 20, ink: brandInkOnLight)
        collapsedMarkView.layer?.addSublayer(collapsedMark.mark)
        collapsedBlueRing = collapsedMark.blueRing
        collapsedMarkView.isHidden = true
        rootView.addSubview(collapsedMarkView)

        // Waiting-count badge riding the bubble's top-right edge. The white
        // ground never floods; the count is the only added chrome.
        collapsedBadge = NSTextField(labelWithString: "")
        collapsedBadge.frame = NSRect(x: 27, y: 27, width: 16, height: 16)
        collapsedBadge.font = NSFont.systemFont(ofSize: 9, weight: .bold)
        collapsedBadge.textColor = NSColor.white
        collapsedBadge.alignment = .center
        collapsedBadge.wantsLayer = true
        collapsedBadge.layer?.cornerRadius = 8
        collapsedBadge.isHidden = true
        rootView.addSubview(collapsedBadge)

        expandButton = DraggableButton(title: "", target: self, action: #selector(toggleCollapsed))
        expandButton.frame = NSRect(x: 0, y: 0, width: 44, height: 44)
        expandButton.isBordered = false
        expandButton.toolTip = "Open AIWatcher Companion"
        expandButton.isHidden = true
        rootView.addSubview(expandButton)

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

            let button = NSButton(title: "Open", target: self, action: #selector(openWaitingRow(_:)))
            button.bezelStyle = .rounded
            button.controlSize = .small
            button.tag = index
            button.toolTip = "Open this waiting session"
            button.isHidden = true
            rootView.addSubview(button)
            rowButtons.append(button)
        }

        setCollapsed(true)
        window.orderFrontRegardless()
        updateAppearance()
        schedulePulse()
        refreshState()
    }

    @objc func openDashboard() {
        openURL(dashboardURL)
    }

    @objc func openWaitingRow(_ sender: NSButton) {
        let index = sender.tag
        guard index >= 0, index < waitingURLs.count else { return }
        openURL(waitingURLs[index])
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
        subtitleLabel.stringValue = "Checking local AI sessions..."
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
        if stateName == "proof_pending" {
            acknowledgeFreshStartReceipts()
            openURL(primaryURL)
            return
        }
        if primaryAction == "copy_fresh_start" && !primarySessionID.isEmpty {
            copyFreshStartFromCompanion()
            return
        }
        if primaryAction == "open_prompt_gate" {
            scheduleAutoCollapse(after: 1.5)
        }
        openURL(primaryURL)
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

    func copyFreshStartFromCompanion() {
        titleLabel.stringValue = "Copying brief"
        subtitleLabel.stringValue = "Preparing Fresh Start..."
        guard let encoded = primarySessionID.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed),
              let briefURL = URL(string: dashboardBaseURL + "/api/handoff-basic?id=" + encoded + "&target=generic") else {
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
                NSPasteboard.general.clearContents()
                NSPasteboard.general.setString(brief, forType: .string)
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
            "project": skipProject
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
                    self.continueSessionID = ""
                    self.stateName = "watching"
                    self.titleLabel.stringValue = "Watching quietly"
                    self.subtitleLabel.stringValue = "Companion nudge skipped"
                    self.primaryURL = dashboardURL
                } else {
                    self.titleLabel.stringValue = "Still pending"
                    self.subtitleLabel.stringValue = "Could not save Skip"
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
            autoCollapseDeadline = nil
        }
        let current = window.frame
        let targetWidth = collapsed ? collapsedWidth : expandedWidth
        let targetHeight = collapsed ? collapsedHeight : expandedHeight
        let targetX = position.contains("right") ? current.maxX - targetWidth : current.minX
        let targetY = position.contains("top") ? current.maxY - targetHeight : current.minY
        let target = NSRect(x: targetX, y: targetY, width: targetWidth, height: targetHeight)
        window.setFrame(target, display: true, animate: true)
        rootView.frame = NSRect(x: 0, y: 0, width: targetWidth, height: targetHeight)
        rootView.layer?.cornerRadius = collapsed ? targetHeight / 2 : 16
        applyVisibility()
        updateAppearance()
    }

    // session_waiting belongs in both lists. Left out, the widget renders the
    // words "Waiting on you" in its calm style with no button -- saying
    // something urgent while looking like nothing is happening.
    func hasPrimaryAction() -> Bool {
        return ["prompt_gate", "control_recommended", "control_review", "optimize_available", "clipboard_confirm", "session_waiting"].contains(stateName)
    }

    func needsAttentionState() -> Bool {
        return ["prompt_gate", "control_recommended", "control_review", "optimize_available", "clipboard_confirm", "session_waiting"].contains(stateName)
    }

    func shouldShowWindow() -> Bool {
        if needsAttentionState() {
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
            for view in [dragHandle, brandMarkView, titleLabel, subtitleLabel, primaryButton, continueButton, skipButton, planButton, askButton, scanButton, consoleButton, collapseButton] {
                view?.isHidden = true
            }
            for view in rowDots { view.isHidden = true }
            for label in rowLabels { label.isHidden = true }
            for button in rowButtons { button.isHidden = true }
            collapsedMarkView.isHidden = false
            collapsedBadge.isHidden = waitingCount <= 0
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
        collapseButton.frame = NSRect(x: 538, y: 37 + yOff, width: 18, height: 18)
        let attention = needsAttentionState()
        // With a queue on screen each row carries its own Open button, so the
        // single primary would only duplicate the first row's.
        let showPrimary = hasPrimaryAction() && rowsShown == 0
        planButton.isHidden = attention
        askButton.isHidden = attention
        scanButton.isHidden = attention
        consoleButton.isHidden = false
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
        if attention {
            titleLabel.frame = NSRect(x: 66, y: 31 + yOff, width: 170, height: 17)
            subtitleLabel.frame = NSRect(x: 66, y: 12 + yOff, width: 170, height: 16)
            primaryButton.frame = NSRect(x: 250, y: 15 + yOff, width: 112, height: 28)
            if showContinue {
                continueButton.frame = NSRect(x: 368, y: 15 + yOff, width: 70, height: 28)
                if showSkip {
                    skipButton.frame = NSRect(x: 444, y: 15 + yOff, width: 48, height: 28)
                    consoleButton.frame = NSRect(x: 498, y: 15 + yOff, width: 38, height: 28)
                } else {
                    consoleButton.frame = NSRect(x: 444, y: 15 + yOff, width: 38, height: 28)
                }
            } else {
                if showSkip {
                    skipButton.frame = NSRect(x: 368, y: 15 + yOff, width: 48, height: 28)
                    consoleButton.frame = NSRect(x: 422, y: 15 + yOff, width: 38, height: 28)
                } else {
                    consoleButton.frame = NSRect(x: 368, y: 15 + yOff, width: 38, height: 28)
                }
            }
        } else {
            titleLabel.frame = NSRect(x: 66, y: 31 + yOff, width: 220, height: 17)
            subtitleLabel.frame = NSRect(x: 66, y: 12 + yOff, width: 238, height: 16)
            planButton.frame = NSRect(x: 318, y: 15 + yOff, width: 48, height: 28)
            askButton.frame = NSRect(x: 370, y: 15 + yOff, width: 46, height: 28)
            scanButton.frame = NSRect(x: 420, y: 15 + yOff, width: 52, height: 28)
            consoleButton.frame = NSRect(x: 476, y: 15 + yOff, width: 38, height: 28)
        }
        for index in 0..<maxWaitingRows {
            let visible = index < rowsShown
            rowDots[index].isHidden = !visible
            rowLabels[index].isHidden = !visible
            rowButtons[index].isHidden = !visible
            if !visible { continue }
            let rowY = yOff - rowHeight * CGFloat(index + 1)
            rowDots[index].frame = NSRect(x: 30, y: rowY + 14, width: 7, height: 7)
            rowDots[index].layer?.backgroundColor = (index == 0
                ? NSColor(calibratedRed: 0.89, green: 0.29, blue: 0.29, alpha: 1)
                : NSColor(calibratedRed: 0.94, green: 0.62, blue: 0.15, alpha: 1)).cgColor
            rowLabels[index].frame = NSRect(x: 46, y: rowY + 9, width: 400, height: 17)
            rowLabels[index].stringValue = index < waitingRowTexts.count ? waitingRowTexts[index] : ""
            rowButtons[index].frame = NSRect(x: 458, y: rowY + 4, width: 58, height: 26)
        }
    }

    // The queue can change the bar's height between polls. Anchored the same
    // way setCollapsed anchors: a bottom-positioned bar grows upward, a
    // top-positioned one downward, so the corner the user parked it in stays
    // put.
    func applyWindowSize() {
        if collapsed { return }
        let target = expandedHeight
        let current = window.frame
        if abs(current.height - target) < 0.5 { return }
        let targetY = position.contains("top") ? current.maxY - target : current.minY
        let frame = NSRect(x: current.minX, y: targetY, width: expandedWidth, height: target)
        window.setFrame(frame, display: true, animate: true)
        rootView.frame = NSRect(x: 0, y: 0, width: expandedWidth, height: target)
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
        collapsedBlueRing.borderColor = (needsAttention ? orangeColor : brandBlue).cgColor
        // The bubble is what is on screen all day; a waiting count is the one
        // number worth carrying there. It rides as a small badge on the white
        // ground -- the ground itself never floods, per the brand rule that
        // attention is carried by the mark's blue ring turning orange.
        collapsedBadge.stringValue = waitingCount > 0 ? String(waitingCount) : ""
        collapsedBadge.layer?.backgroundColor = orangeColor.cgColor
        if collapsed {
            collapsedBadge.isHidden = waitingCount <= 0
        }
        applyWindowVisibility()
    }

    func schedulePulse() {
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.8) {
            self.pulseOn.toggle()
            self.updateAppearance()
            self.schedulePulse()
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
                self.scheduleRefresh()
                return
            }
            DispatchQueue.main.async {
                self.applyState(json)
                self.scheduleAutoCollapse(after: self.hasPrimaryAction() ? 10.0 : 4.0)
                self.scheduleRefresh(after: self.hasPrimaryAction() ? 2.0 : 3.0)
            }
        }.resume()
    }

    func applyState(_ json: [String: Any]) {
        let incomingState = json["state"] as? String ?? "watching"
        if let hold = suppressPassiveRefreshUntil,
           hold > Date(),
           incomingState != "prompt_gate",
           ["fresh_start_copied", "clipboard_confirm"].contains(self.stateName) {
            self.updateAppearance()
            return
        }
        if let hold = suppressPassiveRefreshUntil, hold <= Date() {
            suppressPassiveRefreshUntil = nil
        }
        self.stateName = incomingState
        self.detailText = json["detail"] as? String ?? ""
        let waiting = json["waiting_sessions"] as? [[String: Any]] ?? []
        self.waitingRowTexts = waiting.prefix(maxWaitingRows).map { row in
            let tool = row["tool"] as? String ?? "AI tool"
            let project = row["project"] as? String ?? ""
            let waited = row["waited_label"] as? String ?? ""
            return [tool, project, waited].filter { !$0.isEmpty }.joined(separator: " · ")
        }
        self.waitingURLs = waiting.prefix(maxWaitingRows).map { row in
            absoluteURL((row["url"] as? String) ?? "/")
        }
        let presence = json["presence"] as? [String: Any]
        self.waitingCount = presence?["waiting"] as? Int ?? self.waitingRowTexts.count
        self.titleLabel.stringValue = String((json["label"] as? String ?? "AIWatcher").prefix(18))
        var subtitleText = String((json["subtitle"] as? String ?? "Watching quietly").prefix(46))
        if incomingState == "prompt_gate", let remaining = json["expires_in_seconds"] as? Int, remaining >= 0 {
            subtitleText += " · \(remaining)s"
        }
        self.subtitleLabel.stringValue = subtitleText
        // The payload has always shipped a second explanatory sentence; the
        // bar never drew it. A tooltip costs no pixels.
        let tip = self.detailText.isEmpty ? nil : self.detailText
        self.titleLabel.toolTip = tip
        self.subtitleLabel.toolTip = tip
        self.primaryButton.title = String((json["primary_label"] as? String ?? "Watch").prefix(12))
        self.primaryAction = json["primary_action"] as? String ?? "open_url"
        self.primarySessionID = json["primary_session_id"] as? String ?? ""
        self.primaryRuntimeAvailable = json["primary_runtime_available"] as? Bool ?? false
        self.primaryURL = absoluteURL(json["primary_url"] as? String ?? "/")
        self.continueButton.title = String((json["continue_label"] as? String ?? "Continue").prefix(10))
        self.continueAction = json["continue_action"] as? String ?? ""
        self.continueURL = absoluteURL(json["continue_url"] as? String ?? "")
        self.continueSessionID = json["continue_session_id"] as? String ?? ""
        self.continueReason = json["continue_reason"] as? String ?? ""
        self.continueExpectedTokens = json["continue_expected_saved_context_tokens"] as? Int ?? 0
        self.skipButton.title = String((json["skip_label"] as? String ?? "Skip").prefix(8))
        self.skipState = json["skip_state"] as? String ?? ""
        self.skipSessionID = json["skip_session_id"] as? String ?? ""
        self.skipProject = json["skip_project"] as? String ?? ""
        self.updateAppearance()
    }

    func scheduleAutoCollapse(after delay: Double) {
        if collapsed {
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


def _run_macos_swift_presence(url: str, prompt_url: str, position: str, visibility: str) -> int:
    swift = shutil.which("swift")
    if sys.platform != "darwin" or not swift:
        return 2
    script_path = os.path.join(tempfile.gettempdir(), "aiwatcher-native-presence.swift")
    try:
        with open(script_path, "w", encoding="utf-8") as handle:
            handle.write(MACOS_SWIFT_PRESENCE)
        completed = subprocess.run(
            [swift, script_path, url, prompt_url, position, visibility],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return int(completed.returncode)
    except OSError as exc:
        print(f"AIWatcher presence unavailable: {exc}", file=sys.stderr)
        return 2


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
    s = height / 349.0
    stroke = max(2.0, 40.0 * s)

    def ring(x: float, y: float, w: float, h: float, colour: str) -> None:
        r = 85.0 * s - stroke / 2.0
        x0, y0 = x + stroke / 2.0, y + stroke / 2.0
        x1, y1 = x + w - stroke / 2.0, y + h - stroke / 2.0
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

    ring(0.0, 0.0, 300.0 * s, 260.0 * s, blue)
    ring(129.0 * s, 117.0 * s, 300.0 * s, 232.0 * s, ink)


def run_native_presence(
    url: str,
    *,
    prompt_url: str | None = None,
    position: str = "bottom-right",
    visibility: str = "always",
) -> int:
    """Run the collapsed always-available local companion entry point."""
    prompt_url = prompt_url or f"{url.rstrip('/')}/?view=prompt"
    visibility = visibility if visibility in {"always", "ai-apps", "nudges-only"} else "always"
    if sys.platform == "darwin" and shutil.which("swift"):
        return _run_macos_swift_presence(url, prompt_url, position, visibility)
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

    expanded_width = 560
    expanded_height = 58
    collapsed_width = 44
    collapsed_height = 44
    row_height = 30
    max_waiting_rows = 3
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
    style.configure("Presence.TFrame", background="#090d14")
    style.configure("PresenceTitle.TLabel", background="#090d14", foreground="#f7fbff", font=("Helvetica", 11, "bold"))
    style.configure("PresenceMuted.TLabel", background="#090d14", foreground="#a8b6ca", font=("Helvetica", 9))
    style.configure("PresenceDrag.TLabel", background="#090d14", foreground="#8ea3bd", font=("Helvetica", 10, "bold"))
    style.configure("Presence.TButton", font=("Helvetica", 9, "bold"), padding=(6, 3))
    style.configure("PresenceAttention.TButton", font=("Helvetica", 9, "bold"), padding=(6, 3))
    style.configure("PresenceMini.TButton", font=("Helvetica", 9, "bold"), padding=(4, 2))

    frame = ttk.Frame(root, padding=(8, 6), style="Presence.TFrame")
    frame.pack(fill="both", expand=True)
    collapsed_frame = ttk.Frame(root, padding=(8, 6), style="Presence.TFrame")
    left = ttk.Frame(frame, style="Presence.TFrame")
    left.pack(side="left", fill="both", expand=True)
    title_var = tk.StringVar(value="AIWatcher")
    subtitle_var = tk.StringVar(value="Watching quietly")
    primary_label_var = tk.StringVar(value="Watch")
    primary_action_var = tk.StringVar(value="open_url")
    primary_session_id_var = tk.StringVar(value="")
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
    state_var = tk.StringVar(value="watching")
    pulse_var = tk.BooleanVar(value=False)
    suppress_passive_refresh_until = tk.DoubleVar(value=0.0)
    pending_clipboard_override_session_var = tk.StringVar(value="")
    waiting_row_texts: list[str] = []
    waiting_row_urls: list[str] = []
    waiting_count_var = tk.IntVar(value=0)
    drag = ttk.Label(frame, text="::", style="PresenceDrag.TLabel", cursor="fleur")
    drag.pack(side="left", padx=(0, 6))
    # The brand mark sits beside both lines of copy, as on the dashboard. The
    # expanded bar is always the dark ground, so the ink ring takes the
    # dark-theme ink.
    mark_canvas = tk.Canvas(frame, width=32, height=26, bg="#090d14", highlightthickness=0, bd=0)
    _draw_brand_mark(mark_canvas, height=26.0, blue="#0052F5", ink="#DCE6F6")
    mark_canvas.pack(side="left", padx=(0, 8), before=left)
    title_stack = ttk.Frame(left, style="Presence.TFrame")
    title_stack.pack(anchor="w")
    ttk.Label(title_stack, textvariable=title_var, style="PresenceTitle.TLabel").pack(side="left")
    ttk.Label(left, textvariable=subtitle_var, style="PresenceMuted.TLabel", wraplength=250).pack(anchor="w")

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

    def open_ask() -> None:
        webbrowser.open(f"{url.rstrip('/')}/?ask=1")
        schedule_auto_collapse(1500)

    def apply_state(payload: dict[str, object]) -> None:
        incoming_state = str(payload.get("state") or "watching")
        if (
            suppress_passive_refresh_until.get() > time.time()
            and state_var.get() in {"fresh_start_copied", "clipboard_confirm"}
            and incoming_state != "prompt_gate"
        ):
            return
        if suppress_passive_refresh_until.get() <= time.time():
            suppress_passive_refresh_until.set(0.0)
        state_var.set(incoming_state)
        waiting_row_texts.clear()
        waiting_row_urls.clear()
        waiting_sessions = payload.get("waiting_sessions")
        if isinstance(waiting_sessions, list):
            for row in waiting_sessions[:max_waiting_rows]:
                if not isinstance(row, dict):
                    continue
                parts = [
                    str(row.get("tool") or "AI tool"),
                    str(row.get("project") or ""),
                    str(row.get("waited_label") or ""),
                ]
                waiting_row_texts.append(" · ".join(part for part in parts if part))
                path = str(row.get("url") or "/")
                waiting_row_urls.append(path if path.startswith("http") else f"{url.rstrip('/')}{path}")
        presence = payload.get("presence")
        try:
            waiting_count = int(presence.get("waiting") or 0) if isinstance(presence, dict) else 0
        except (TypeError, ValueError):
            waiting_count = 0
        waiting_count_var.set(waiting_count or len(waiting_row_texts))
        title_var.set(str(payload.get("label") or "AIWatcher")[:18])
        subtitle_text = str(payload.get("subtitle") or "Watching quietly")[:46]
        remaining = payload.get("expires_in_seconds")
        if incoming_state == "prompt_gate" and isinstance(remaining, int) and remaining >= 0:
            subtitle_text += f" · {remaining}s"
        subtitle_var.set(subtitle_text)
        primary_label_var.set(str(payload.get("primary_label") or "Watch")[:12])
        primary_action_var.set(str(payload.get("primary_action") or "open_url"))
        primary_session_id_var.set(str(payload.get("primary_session_id") or ""))
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
        skip_label_var.set(str(payload.get("skip_label") or "Skip")[:8])
        skip_state_var.set(str(payload.get("skip_state") or ""))
        skip_session_id_var.set(str(payload.get("skip_session_id") or ""))
        skip_project_var.set(str(payload.get("skip_project") or ""))
        try:
            continue_expected_tokens_var.set(int(payload.get("continue_expected_saved_context_tokens") or 0))
        except (TypeError, ValueError, tk.TclError):
            continue_expected_tokens_var.set(0)

    def scan_now() -> None:
        title_var.set("Scanning")
        subtitle_var.set("Checking local AI sessions...")
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
        if primary_action_var.get() == "open_prompt_gate":
            schedule_auto_collapse(1500)
        webbrowser.open(primary_url_var.get() or url)

    def copy_fresh_start_from_companion() -> None:
        session_id = primary_session_id_var.get().strip()
        title_var.set("Copying brief")
        subtitle_var.set("Preparing Fresh Start...")
        try:
            encoded = urllib.parse.quote(session_id)
            with urllib.request.urlopen(f"{url.rstrip('/')}/api/handoff-basic?id={encoded}&target=generic", timeout=8.0) as response:
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
            root.clipboard_clear()
            root.clipboard_append(brief)
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
            subtitle_var.set("Could not save Skip")
            update_attention_style()
            return
        skip_state_var.set("")
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
            root.geometry(f"{collapsed_width}x{collapsed_height}")
        else:
            collapsed_frame.pack_forget()
            frame.pack(fill="both", expand=True)
            root.geometry(f"{expanded_width}x{expanded_height + row_height * visible_waiting_rows()}")
        update_attention_style()

    def toggle_collapsed() -> None:
        set_collapsed(not collapsed.get())
        if not collapsed.get():
            schedule_auto_collapse(10000)

    def has_primary_action() -> bool:
        return state_var.get() in {
            "prompt_gate", "control_recommended", "optimize_available", "clipboard_confirm",
            "session_waiting",
        }

    def visible_waiting_rows() -> int:
        # One row per waiting session, but only once there is a queue to draw:
        # a single waiting session keeps the original one-line layout.
        if state_var.get() != "session_waiting" or len(waiting_row_texts) < 2:
            return 0
        return min(len(waiting_row_texts), max_waiting_rows)

    def open_waiting_row(index: int) -> None:
        if 0 <= index < len(waiting_row_urls):
            webbrowser.open(waiting_row_urls[index])
            schedule_auto_collapse(1500)

    def apply_waiting_rows() -> None:
        rows = 0 if collapsed.get() else visible_waiting_rows()
        for index in range(min(rows, len(waiting_row_texts))):
            row_widgets[index][1].configure(text=waiting_row_texts[index])
        if rows == rows_shown.get():
            return
        for row_frame, _row_label in row_widgets:
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
            root.geometry(f"{expanded_width}x{expanded_height + row_height * rows}")

    def should_show_window() -> bool:
        if has_primary_action():
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
                    set_collapsed(True)
            except tk.TclError:
                pass

        try:
            root.after(delay_ms, collapse_if_current)
        except tk.TclError:
            pass

    def update_attention_style() -> None:
        needs_attention = state_var.get() in {
            "prompt_gate", "control_recommended", "control_review", "optimize_available",
            "clipboard_confirm", "session_waiting",
        }
        attention_bg = "#ed6a24" if pulse_var.get() else "#b84816"
        # Collapsed, the bubble stays white in every state; attention is
        # carried by the mark's blue ring turning orange, the same job it does
        # in the dashboard favicon -- not by flooding the ground.
        shell_bg = "#ffffff" if collapsed.get() else "#090d14"
        root.configure(bg=shell_bg)
        style.configure("Presence.TFrame", background=shell_bg)
        style.configure("PresenceTitle.TLabel", background=shell_bg)
        style.configure("PresenceMuted.TLabel", background=shell_bg)
        style.configure("PresenceDrag.TLabel", background=shell_bg)
        style.configure(
            "PresenceAttention.TButton",
            background=attention_bg if needs_attention else "#f2f6fb",
            foreground="#ffffff" if needs_attention else "#111827",
        )
        primary_button.configure(style="PresenceAttention.TButton" if needs_attention else "Presence.TButton")
        collapsed_canvas.configure(bg="#ffffff")
        collapsed_canvas.delete("all")
        _draw_brand_mark(
            collapsed_canvas, height=18.0,
            blue=attention_bg if needs_attention else "#0052F5",
            ink="#141314",
        )
        collapsed_canvas.move("all", 3.0, 7.0)
        # The bubble is what is on screen all day; a waiting count is the one
        # number worth carrying there. It rides as a small badge on the white
        # ground -- the ground itself never floods, per the brand rule that
        # attention is carried by the mark's blue ring turning orange.
        waiting_count = int(waiting_count_var.get() or 0)
        if waiting_count > 0:
            collapsed_canvas.create_oval(26, 2, 42, 18, fill=attention_bg, outline="")
            collapsed_canvas.create_text(
                34, 10, text=str(waiting_count), fill="#ffffff", font=("Helvetica", 8, "bold"),
            )
        attention_layout = has_primary_action() and not collapsed.get()
        # With a queue on screen each row carries its own Open button, so the
        # single primary would only duplicate the first row's.
        should_show_primary = attention_layout and visible_waiting_rows() == 0
        if attention_layout:
            if plan_packed.get():
                plan_button.pack_forget()
                plan_packed.set(False)
        elif not collapsed.get() and not plan_packed.get():
            plan_button.pack(side="left", padx=(8, 4), before=ask_button)
            plan_packed.set(True)
        if attention_layout:
            if ask_packed.get():
                ask_button.pack_forget()
                ask_packed.set(False)
        elif not collapsed.get() and not ask_packed.get():
            ask_button.pack(side="left", padx=(0, 4), before=scan_button)
            ask_packed.set(True)
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

    def pulse_attention() -> None:
        pulse_var.set(not pulse_var.get())
        update_attention_style()
        try:
            root.after(800, pulse_attention)
        except tk.TclError:
            pass

    def refresh_state() -> None:
        try:
            request_url = f"{url.rstrip('/')}/api/companion-state"
            with urllib.request.urlopen(request_url, timeout=0.8) as response:
                payload = json.loads(response.read().decode("utf-8"))
            apply_state(payload)
        except (OSError, urllib.error.URLError, json.JSONDecodeError, tk.TclError):
            state_var.set("watching")
            title_var.set("AIWatcher")
            subtitle_var.set("Watching quietly")
            primary_label_var.set("Watch")
            primary_action_var.set("open_url")
            primary_session_id_var.set("")
            primary_runtime_available_var.set(False)
            primary_url_var.set(url)
            continue_session_id_var.set("")
            skip_state_var.set("")
            skip_project_var.set("")
            waiting_row_texts.clear()
            waiting_row_urls.clear()
            waiting_count_var.set(0)
        finally:
            update_attention_style()
            schedule_auto_collapse(10000 if has_primary_action() else 4000)
            try:
                root.after(3000, refresh_state)
            except tk.TclError:
                pass

    plan_button = ttk.Button(frame, text="Plan", width=6, style="Presence.TButton", command=open_prompt)
    plan_button.pack(side="left", padx=(8, 4))
    ask_button = ttk.Button(frame, text="Ask", width=5, style="Presence.TButton", command=open_ask)
    ask_button.pack(side="left", padx=(0, 4))
    scan_button = ttk.Button(frame, text="Scan", width=6, style="Presence.TButton", command=scan_now)
    scan_button.pack(side="left", padx=(0, 4))
    primary_button = ttk.Button(frame, textvariable=primary_label_var, width=11, style="Presence.TButton", command=open_primary)
    primary_button.pack(side="left", padx=(0, 4))
    continue_button = ttk.Button(frame, textvariable=continue_label_var, width=9, style="Presence.TButton", command=continue_here)
    skip_button = ttk.Button(frame, textvariable=skip_label_var, width=6, style="Presence.TButton", command=skip_current)
    console_button = ttk.Button(frame, text="UI", width=4, style="Presence.TButton", command=open_dashboard)
    console_button.pack(side="left")
    ttk.Button(frame, text="-", width=2, style="PresenceMini.TButton", command=toggle_collapsed).pack(side="left", padx=(4, 0))
    rows_frame = ttk.Frame(root, style="Presence.TFrame")
    row_widgets: list[tuple[ttk.Frame, ttk.Label]] = []
    for row_index in range(max_waiting_rows):
        row_frame = ttk.Frame(rows_frame, padding=(14, 2), style="Presence.TFrame")
        ttk.Label(row_frame, text="●", style="PresenceDot.TLabel").pack(side="left", padx=(0, 6))
        row_label = ttk.Label(row_frame, text="", style="PresenceMuted.TLabel")
        row_label.pack(side="left", fill="x", expand=True)
        ttk.Button(
            row_frame, text="Open", width=6, style="Presence.TButton",
            command=lambda index=row_index: open_waiting_row(index),
        ).pack(side="right")
        row_widgets.append((row_frame, row_label))
    # The collapsed state is a Loom-style bubble: the mark alone on a plain
    # white ground, no lettering and no border chrome. update_attention_style
    # repaints it, turning the mark's blue ring orange when attention is due.
    collapsed_canvas = tk.Canvas(collapsed_frame, width=28, height=32, bg="#ffffff", highlightthickness=0, bd=0)
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
    set_collapsed(True)
    refresh_state()
    pulse_attention()
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
    _draw_brand_mark(
        mark_canvas, height=30.0,
        blue="#ed6a24" if (severity or "").lower() == "critical" else "#0052F5",
        ink=ink,
    )
    mark_canvas.pack(side="left", padx=(0, 10))
    ttk.Label(header, text=title or "Start a fresh AI session", style="AIWTitle.TLabel", wraplength=560).pack(
        side="left", fill="x", expand=True, anchor="w"
    )
    ttk.Label(header, text=severity or "warning", style="AIWSeverity.TLabel").pack(side="right", padx=(16, 0))

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
        return run_native_presence(args.url, prompt_url=args.prompt_url, position=args.position, visibility=args.visibility)
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
