from __future__ import annotations

import unittest
from unittest import mock
import inspect

from aiwatcher_cli import native_overlay
from aiwatcher_cli.native_overlay import overlay_config


class NativeOverlayConfigTests(unittest.TestCase):
    def test_velocity_recommends_focus_not_a_fresh_chat(self) -> None:
        config = overlay_config("velocity")

        self.assertEqual(config.primary_mode, "copy")
        self.assertEqual(config.primary_label, "Copy focused brief")
        self.assertNotIn("fresh", config.title.lower())

    def test_critical_context_offers_fresh_session_brief(self) -> None:
        config = overlay_config("critical_context")

        self.assertEqual(config.primary_mode, "copy")
        self.assertEqual(config.primary_label, "Copy fresh-session brief")

    def test_blocked_session_returns_rather_than_handing_off(self) -> None:
        config = overlay_config("session_blocked")

        self.assertEqual(config.primary_mode, "return")
        self.assertEqual(config.primary_label, "Return to session")
        # Not a handoff: the session is waiting, not heavy.
        self.assertNotIn("fresh", config.title.lower())
        self.assertNotIn("brief", config.primary_label.lower())

    def test_waiting_aliases_reach_the_blocked_session_config(self) -> None:
        for alias in ("waiting", "session_waiting", "blocked"):
            with self.subTest(alias=alias):
                self.assertEqual(native_overlay._normalize_signal_kind(alias), "session_blocked")

    def test_loop_prioritizes_inspection(self) -> None:
        config = overlay_config("loop", action_endpoint_available=True)

        self.assertEqual(config.primary_mode, "inspect")
        self.assertEqual(config.primary_label, "Stop and inspect")

    def test_primary_label_can_be_overridden_by_watch_presentation(self) -> None:
        config = overlay_config("runway", primary_label="Copy cross-tool Fresh Start")

        self.assertEqual(config.primary_label, "Copy cross-tool Fresh Start")

    def test_generic_fresh_start_title_infers_critical_context(self) -> None:
        self.assertEqual(
            native_overlay._infer_signal_kind_from_title("generic", "AIWatcher: start a fresh chat"),
            "critical_context",
        )

    def test_runtime_return_uses_post_body(self) -> None:
        with mock.patch.object(native_overlay, "_request_json", return_value={"ok": True}) as request_json:
            self.assertTrue(native_overlay._request_runtime_return("http://127.0.0.1:8765", "sess-1"))

        request_json.assert_called_once_with("http://127.0.0.1:8765/api/runtime-return", {"session_id": "sess-1"})

    def test_macos_overlay_records_handoff_only_for_critical_context_copy(self) -> None:
        self.assertIn('if signalKind == "critical_context"', native_overlay.MACOS_SWIFT_OVERLAY)
        self.assertIn("/api/handoff-basic", native_overlay.MACOS_SWIFT_OVERLAY)
        self.assertIn('postDecision("copy_handoff")', native_overlay.MACOS_SWIFT_OVERLAY)
        self.assertIn('if action == "snooze"', native_overlay.MACOS_SWIFT_OVERLAY)
        self.assertNotIn('action == "dismiss" ? "dismissed" : "copy_handoff"', native_overlay.MACOS_SWIFT_OVERLAY)

    def test_macos_overlay_is_nonactivating_and_has_one_primary_action(self) -> None:
        source = native_overlay.MACOS_SWIFT_OVERLAY

        self.assertIn(".nonactivatingPanel", source)
        self.assertNotIn("NSApp.activate", source)
        self.assertIn('NSButton(title: "Continue here"', source)
        self.assertIn('NSButton(title: "..."', source)

    def test_tk_overlay_records_handoff_only_for_critical_context_copy(self) -> None:
        source = inspect.getsource(native_overlay.run_native_overlay)

        self.assertIn('_normalize_signal_kind(signal_kind) == "critical_context"', source)
        self.assertIn('_record_decision(base, session_id, "copy_handoff"', source)

    def test_tk_overlay_returns_without_touching_the_clipboard(self) -> None:
        source = inspect.getsource(native_overlay.run_native_overlay)

        self.assertIn('if primary_mode == "return":', source)
        self.assertIn("_runtime_return_result(base, session_id)", source)
        # The handler itself never touches the clipboard, and the dispatch
        # reaches it before the copy path. A brief on the clipboard is the
        # thing this mode exists to stop.
        handler = source.split("def return_to_session()")[1].split("def primary_action()")[0]
        self.assertNotIn("clipboard_append", handler)
        self.assertNotIn("clipboard_clear", handler)
        dispatch = source.split("def primary_action()")[1]
        self.assertLess(
            dispatch.index('if primary_mode == "return":'),
            dispatch.index("clipboard_append"),
        )

    def test_swift_overlay_returns_and_reports_why_it_could_not(self) -> None:
        source = native_overlay.MACOS_SWIFT_OVERLAY

        self.assertIn('if primaryMode == "return" {', source)
        self.assertIn("func requestRuntimeReturnResult()", source)
        self.assertIn("Could not reach the tool from here", source)
        # Unlike the fire-and-forget call, this one waits: a button that
        # reports a return has to know whether one happened.
        self.assertIn("sem.wait(timeout: .now() + 4)", source)

    def test_macos_presence_is_collapsed_nonactivating_companion(self) -> None:
        source = native_overlay.MACOS_SWIFT_PRESENCE

        self.assertIn(".nonactivatingPanel", source)
        self.assertIn(".canJoinAllSpaces", source)
        self.assertIn('"AIWatcher"', source)
        self.assertIn('"Watching quietly"', source)
        self.assertIn('@objc func openDashboard', source)
        self.assertIn('@objc func openPrompt', source)
        self.assertIn('@objc func scanNow', source)
        self.assertIn('@objc func openPrimary', source)
        self.assertIn('@objc func toggleCollapsed', source)
        self.assertIn('final class DragView', source)
        self.assertIn('window.isMovableByWindowBackground = true', source)
        # The collapsed bubble drags as well as clicks: the full-size button
        # hands a moving press to the window and keeps a stationary one.
        self.assertIn('final class DraggableButton', source)
        self.assertIn('window?.performDrag(with: next)', source)
        self.assertIn('brandMarkView', source)
        self.assertIn('stateName', source)
        self.assertIn('pulseOn', source)
        self.assertIn('updateAppearance()', source)
        self.assertIn('schedulePulse()', source)
        self.assertIn('primaryButton.layer?.backgroundColor', source)
        self.assertIn('continueButton', source)
        self.assertIn('continueHere', source)
        self.assertIn('continuePromptGate', source)
        self.assertIn('"run_original_prompt"', source)
        self.assertIn('"decision": "run_original"', source)
        self.assertIn('skipButton', source)
        self.assertIn('skipCurrent', source)
        self.assertIn('/api/handoff-decision', source)
        self.assertIn('/api/companion-skip', source)
        self.assertIn('/api/companion-scan', source)
        self.assertIn('/api/handoff-receipts-viewed', source)
        self.assertIn('"prompt_gate"', source)
        self.assertIn('"control_review"', source)
        self.assertNotIn('"proof_pending", "needs_review"', source)
        self.assertIn('0.93, green: 0.42, blue: 0.14', source)
        # The collapsed bubble: a transparent window (no square corners behind
        # the circle), the mark hosted outside the flipped NSButton layer, and
        # attention carried by the blue ring, not an orange ground.
        self.assertIn('window.isOpaque = false', source)
        self.assertIn('collapsedMarkView', source)
        self.assertIn('collapsedBlueRing.borderColor = (needsAttention ? orangeColor : brandBlue).cgColor', source)
        self.assertIn('"/api/companion-state"', source)
        self.assertIn('"Plan"', source)
        self.assertIn('"Scan"', source)
        self.assertIn('"UI"', source)
        self.assertIn("targetHeight / 2", source)
        # The brand mark: brand blue, and the two ring heights that differ on
        # purpose -- equalising them breaks the fit to the original artwork.
        self.assertIn('makeBrandMark', source)
        self.assertIn('0.00, green: 0.32, blue: 0.96', source)
        self.assertIn('260.0 * s', source)
        self.assertIn('232.0 * s', source)
        self.assertIn('85.0 * s', source)
        self.assertIn('rootView.layer?.borderWidth = collapsed ? 0 : 1', source)
        self.assertIn("setCollapsed(true)", source)
        self.assertIn("current.maxX - targetWidth", source)
        self.assertIn("hasPrimaryAction", source)
        self.assertIn("scheduleAutoCollapse", source)

    def test_presence_bars_draw_the_waiting_queue_and_countdown(self) -> None:
        # Both bars render the additive companion-state fields: waiting_sessions
        # queue rows, the presence waiting count on the collapsed pill, the
        # prompt-gate countdown, and `detail` as a tooltip where the toolkit
        # has one.
        mac = native_overlay.MACOS_SWIFT_PRESENCE
        self.assertIn('json["waiting_sessions"]', mac)
        self.assertIn('json["expires_in_seconds"]', mac)
        self.assertIn('json["presence"]', mac)
        self.assertIn("@objc func openWaitingRow", mac)
        self.assertIn("visibleWaitingRows", mac)
        # Height follows the queue, and the resize keeps the parked corner
        # fixed the same way setCollapsed does.
        self.assertIn("func applyWindowSize()", mac)
        self.assertIn("CGFloat(visibleWaitingRows) * rowHeight", mac)
        # The waiting count rides the white bubble as a badge; the ground
        # itself never floods, per the brand rule that attention is carried by
        # the mark's blue ring turning orange.
        self.assertIn("collapsedBadge", mac)
        self.assertIn("collapsedBadge.stringValue = waitingCount > 0", mac)
        self.assertIn("titleLabel.toolTip", mac)
        # A queue means per-row Open buttons, not a duplicated primary.
        self.assertIn("hasPrimaryAction() && rowsShown == 0", mac)

        tk_source = inspect.getsource(native_overlay.run_native_presence)
        self.assertIn("waiting_sessions", tk_source)
        self.assertIn("expires_in_seconds", tk_source)
        self.assertIn("def visible_waiting_rows", tk_source)
        self.assertIn("def open_waiting_row", tk_source)
        self.assertIn("def apply_waiting_rows", tk_source)
        self.assertIn("create_text(\n                27, 10, text=str(badge_count)", tk_source)
        self.assertIn("visible_waiting_rows() == 0", tk_source)

    def test_presence_bars_draw_the_meter_and_missed_signal_chip(self) -> None:
        # The meter draws only when the payload says the number is measurable
        # -- unmeasurable must not render as an empty bar -- and the chip
        # borrows Plan/Ask's slot only outside attention states.
        mac = native_overlay.MACOS_SWIFT_PRESENCE
        self.assertIn('json["pressure"]', mac)
        self.assertIn('json["recent_signal"]', mac)
        self.assertIn("let showMeter = pressureAvailable", mac)
        self.assertIn("@objc func openSignal", mac)
        self.assertIn("!signalChipText.isEmpty && !attention", mac)
        # The fill clamps at 100%; the percent label does not: a 250K turn is
        # past the limit and the number should say so.
        self.assertIn("min(max(pressurePct, 0), 100)", mac)

        # The running-totals label rides the pressure block as plain muted
        # text -- a total is not a verdict, so no status colour touches it.
        self.assertIn('pressure?["stats_label"]', mac)
        self.assertIn("statsLabel", mac)

        tk_source = inspect.getsource(native_overlay.run_native_presence)
        self.assertIn("pressure_stats_var", tk_source)
        self.assertIn('payload.get("pressure")', tk_source)
        self.assertIn('payload.get("recent_signal")', tk_source)
        self.assertIn("def open_signal", tk_source)
        self.assertIn("pressure_available_var.get() and not collapsed.get()", tk_source)

    def test_presence_bars_return_to_the_blocked_tool_honestly(self) -> None:
        # A queue row (or a lone session's primary) whose runtime attachment is
        # reachable posts /api/runtime-return and reports the result; failure
        # says so and opens the session in AIWatcher instead. Never a claimed
        # jump that did not happen.
        mac = native_overlay.MACOS_SWIFT_PRESENCE
        self.assertIn('"return_available"', mac)
        self.assertIn("func requestRuntimeReturn(sessionID:", mac)
        self.assertIn('primaryAction == "runtime_return"', mac)
        self.assertIn('canReturn ? "Return" : (kind.isEmpty ? "Open" : "Review")', mac)
        self.assertIn('"No live return. Opened in AIWatcher."', mac)
        # The result is awaited, with a bounded timeout, off the main thread.
        self.assertIn("request.timeoutInterval = 4", mac)
        self.assertIn("DispatchQueue.main.async", mac)

        tk_source = inspect.getsource(native_overlay.run_native_presence)
        self.assertIn("def request_runtime_return", tk_source)
        self.assertIn('"runtime_return"', tk_source)
        self.assertIn("return_available", tk_source)
        self.assertIn('"Return" if can_return else ("Review" if kind else "Open")', tk_source)
        self.assertIn("No live return. Opened in AIWatcher.", tk_source)

    def test_presence_rows_say_what_the_session_wants(self) -> None:
        # The "wants" tag is the hook's closed-vocabulary phrase; the row only
        # ever renders "wants: <phrase>" or nothing at all.
        mac = native_overlay.MACOS_SWIFT_PRESENCE
        self.assertIn('row["wants"]', mac)
        self.assertIn("rowTags", mac)
        self.assertIn('wants.isEmpty ? "" : "wants: \\(wants)"', mac)

        tk_source = inspect.getsource(native_overlay.run_native_presence)
        self.assertIn("waiting_row_wants", tk_source)
        self.assertIn('f"wants: {wants}" if wants else ""', tk_source)

    def test_finished_earns_the_primary_but_never_the_orange(self) -> None:
        # session_finished sits in hasPrimaryAction (Review, compact layout)
        # and deliberately not in needsAttentionState: "review when ready"
        # must not wear the "blocked on you" treatment. The bubble badge goes
        # brand blue for the same reason, and the window still shows in
        # nudges-only mode via the soft-attention check.
        mac = native_overlay.MACOS_SWIFT_PRESENCE
        self.assertIn("session_finished", mac.split("func hasPrimaryAction")[1][:400])
        self.assertNotIn("session_finished", mac.split("func needsAttentionState")[1][:400])
        self.assertIn('["session_finished", "away_digest"].contains(stateName)', mac)
        self.assertIn('json["finished_sessions"]', mac)
        self.assertIn("finishedCount > 0 ? String(finishedCount)", mac)

        tk_source = inspect.getsource(native_overlay.run_native_presence)
        self.assertIn("session_finished", tk_source.split("def has_primary_action()")[1][:600])
        self.assertNotIn(
            "session_finished",
            tk_source.split("needs_attention = state_var.get() in")[1][:200],
        )
        self.assertIn('fill=attention_bg if waiting_count > 0 else "#0052F5"', tk_source)

    def test_the_away_digest_rides_the_queue_rows(self) -> None:
        # History entries reuse the waiting-row machinery: mint/amber dots by
        # kind, Review buttons, and the calm treatment -- away_digest earns
        # the compact layout without joining the attention lists.
        mac = native_overlay.MACOS_SWIFT_PRESENCE
        self.assertIn('json["digest_rows"]', mac)
        self.assertIn('"away_digest"', mac)
        self.assertNotIn("away_digest", mac.split("func needsAttentionState")[1][:400])
        self.assertIn('kind.isEmpty ? "Open" : "Review"', mac)

        tk_source = inspect.getsource(native_overlay.run_native_presence)
        self.assertIn('payload.get("digest_rows")', tk_source)
        self.assertIn('"away_digest"', tk_source)
        self.assertNotIn(
            "away_digest",
            tk_source.split("needs_attention = state_var.get() in")[1][:200],
        )
        self.assertIn('"Review" if kind else "Open"', tk_source)

    def test_the_marks_ring_says_running(self) -> None:
        # The ring speaks the dashboard favicon's vocabulary: orange for
        # attention, mint #43d9a3 (the favicon's healthy-live colour) while a
        # session is working, brand blue at rest. Attention keeps priority.
        mac = native_overlay.MACOS_SWIFT_PRESENCE
        self.assertIn("workingCount > 0 ? runningMint : brandBlue", mac)
        self.assertIn("0.26, green: 0.85, blue: 0.64", mac)

        tk_source = inspect.getsource(native_overlay.run_native_presence)
        self.assertIn('"#43d9a3" if int(working_count_var.get() or 0) > 0 else "#0052F5"', tk_source)

    def test_tk_presence_opens_dashboard_and_prompt_without_session_claim(self) -> None:
        source = inspect.getsource(native_overlay.run_native_presence)

        self.assertIn("Watching quietly", source)
        self.assertIn("toggle_collapsed", source)
        self.assertIn("state_var", source)
        self.assertIn("pulse_var", source)
        self.assertIn("#ed6a24", source)
        self.assertIn('"#ffffff" if collapsed.get() else "#090d14"', source)
        self.assertIn("PresenceAttention.TButton", source)
        self.assertNotIn('"proof_pending", "needs_review"', source)
        self.assertIn("continue_here", source)
        self.assertIn("run_original_prompt", source)
        self.assertIn("\"decision\": \"run_original\"", source)
        self.assertIn("skip_current", source)
        self.assertIn("/api/handoff-decision", source)
        self.assertIn("/api/companion-skip", source)
        self.assertIn("/api/handoff-receipts-viewed", source)
        self.assertIn("control_review", source)
        self.assertIn("cursor=\"fleur\"", source)
        self.assertIn("webbrowser.open(url)", source)
        self.assertIn("webbrowser.open(prompt_url or url)", source)
        self.assertIn("scan_now", source)
        self.assertIn("/api/companion-state", source)
        self.assertIn("/api/companion-scan", source)
        self.assertIn("text=\"Plan\"", source)
        self.assertIn("text=\"Scan\"", source)
        self.assertIn("text=\"UI\"", source)
        self.assertIn("_draw_brand_mark", source)
        self.assertIn("#0052F5", source)
        self.assertIn("collapsed_button_motion", source)
        self.assertIn("collapsed_button_release", source)
        self.assertIn("set_collapsed(True)", source)
        self.assertIn("root.after(3000, refresh_state)", source)
        self.assertIn("has_primary_action", source)
        self.assertIn("schedule_auto_collapse", source)
        self.assertIn("textvariable=primary_label_var", source)
        self.assertIn("primary_runtime_available_var", source)
        self.assertIn("/api/runtime-return", source)
        self.assertIn("if primary_runtime_available_var.get()", source)
        self.assertIn("Clipboard has text", source)
        self.assertIn("Click Replace to copy Fresh Start", source)
        self.assertIn("pending_clipboard_override_session_var", source)
        # Asserted per state rather than as one literal set: the previous form
        # pinned the exact spelling of the line, so adding a state broke it for
        # formatting reasons rather than behavioural ones.
        primary_states = source[source.index("def has_primary_action()"):][:600]
        for state in ("prompt_gate", "control_recommended", "optimize_available",
                      "clipboard_confirm", "session_waiting"):
            with self.subTest(state=state):
                self.assertIn(state, primary_states)
        self.assertIn("visibility: str = \"always\"", source)
        self.assertIn("_foreground_looks_like_ai_work", source)
        self.assertIn("root.withdraw()", source)

        mac_source = native_overlay.MACOS_SWIFT_PRESENCE
        self.assertIn("visibilityMode", mac_source)
        self.assertIn("NSWorkspace.shared.frontmostApplication", mac_source)
        self.assertIn("foregroundLooksLikeAIWork", mac_source)
        self.assertIn('visibilityMode == "ai-apps"', mac_source)

    def test_presence_visibility_uses_windows_foreground_api(self) -> None:
        helper_source = inspect.getsource(native_overlay._windows_foreground_text)
        main_source = inspect.getsource(native_overlay.main)

        self.assertIn("GetForegroundWindow", helper_source)
        self.assertIn("GetWindowTextW", helper_source)
        self.assertIn("tasklist", helper_source)
        self.assertIn("--visibility", main_source)
        self.assertIn("visibility=args.visibility", main_source)

    def test_macos_tray_has_console_prompt_scan_and_quit(self) -> None:
        source = native_overlay.MACOS_SWIFT_TRAY

        self.assertIn("NSStatusBar.system.statusItem", source)
        self.assertIn('"AIW"', source)
        self.assertIn("Open Console", source)
        self.assertIn("Plan Prompt", source)
        self.assertIn("Scan Now", source)
        self.assertIn("Quit AIWatcher Menu", source)
        self.assertIn("/api/companion-scan", source)

    def test_windows_tray_uses_shell_notify_icon(self) -> None:
        source = inspect.getsource(native_overlay._run_windows_tray)

        self.assertIn("Shell_NotifyIconW", source)
        self.assertIn("CreatePopupMenu", source)
        self.assertIn("Open Console", source)
        self.assertIn("Plan Prompt", source)
        self.assertIn("Scan Now", source)

    def test_main_routes_tray_mode_separately_from_presence(self) -> None:
        source = inspect.getsource(native_overlay.main)

        self.assertIn("--tray", source)
        self.assertIn("run_native_tray", source)
        self.assertIn("run_native_presence", source)


if __name__ == "__main__":
    unittest.main()


class MacosSwiftOverlaySourceTests(unittest.TestCase):
    def test_swift_overlay_source_typechecks(self) -> None:
        """The Swift companion is the macOS delivery path and is stored as a
        Python string, so nothing parses it until a developer on a Mac triggers
        an overlay. A typo here is a window that never opens, reported as
        "Overlay: opened".

        Gated on macOS, not merely on swiftc: the Linux runners ship a Swift
        toolchain too, and it typechecks this file right up to `import Cocoa`,
        which is macOS-only. Checking for the compiler alone turned a
        platform-specific source file into a failure on every Linux job.

        In CI one macOS job owns it (see AIWATCHER_SWIFT_TYPECHECK in
        ci.yml): this source does not vary by Python version, so four macOS
        jobs would pay for the same answer. The gate is on the flag rather
        than on a version number so that a developer on a Mac still gets the
        check whatever Python they run -- the system one here is 3.9, which
        the macOS matrix does not even cover.
        """
        import os
        import shutil as _shutil
        import subprocess
        import sys as _sys
        import tempfile

        swiftc = _shutil.which("swiftc")
        if _sys.platform != "darwin" or not swiftc:
            self.skipTest("the macOS SDK is needed to typecheck a Cocoa source file")
        if os.environ.get("CI") and os.environ.get("AIWATCHER_SWIFT_TYPECHECK") != "1":
            self.skipTest("another macOS job owns the swiftc typecheck in CI")
        # Both embedded Cocoa sources, not just the nudge overlay: the presence
        # bar is the one that changes most and a typo there is a Companion that
        # silently never draws.
        sources = {
            "overlay": native_overlay.MACOS_SWIFT_OVERLAY,
            "presence": native_overlay.MACOS_SWIFT_PRESENCE,
        }
        for name, source in sources.items():
            with self.subTest(source=name):
                with tempfile.NamedTemporaryFile("w", suffix=".swift", delete=False, encoding="utf-8") as handle:
                    handle.write(source)
                    path = handle.name
                try:
                    completed = subprocess.run([swiftc, "-typecheck", path], capture_output=True, text=True)
                finally:
                    os.unlink(path)
                self.assertEqual(completed.returncode, 0, f"Swift {name} source does not typecheck:\n{completed.stderr}")


if __name__ == "__main__":
    unittest.main()
