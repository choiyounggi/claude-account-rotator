import dataclasses
import fnmatch
import io
import json
import os
import tempfile
import tokenize
import unittest

import rotator.switcher as sw
from rotator.models import Profile, Session
from rotator.switcher import (
    DEFAULT_CONTINUE_PROMPT,
    STATE_HINTS,
    OrcaCliDriver,
    OsProcessProbe,
    ProcEnv,
    ScreenReceipt,
    SendReceipt,
    SwitchResult,
    Timeouts,
    WaitReceipt,
    build_relaunch_command,
    check_pane_binding,
    classify_screen,
    switch_budget_s,
    switch_session,
)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def clock(self):
        return self.now

    def sleep(self, s):
        self.sleeps.append(s)
        self.now += s


# ---------------------------------------------------------------- Task 02


class TestVocabulary(unittest.TestCase):
    def test_reasons_unique_and_complete(self):
        self.assertEqual(len(sw.REASONS), 28)
        self.assertEqual(len(set(sw.REASONS)), len(sw.REASONS))

    def test_steps_and_states(self):
        self.assertEqual(
            sw.STEPS,
            ("preflight", "interrupt", "exit", "wait_exit", "relaunch", "wait_ready", "verify_account", "continue_prompt"),
        )
        self.assertEqual(
            sw.STATES, ("untouched", "interrupted", "exit_pending", "exited", "relaunched", "verified", "continued")
        )
        self.assertEqual(sw.MODES, ("soft", "hard"))

    def test_state_hints_cover_every_state(self):
        self.assertEqual(set(STATE_HINTS), set(sw.STATES))
        self.assertEqual(STATE_HINTS["continued"], "")
        for state, hint in STATE_HINTS.items():
            if state != "continued":
                self.assertTrue(hint, state)
            self.assertNotIn("/Users", hint)

    def test_exited_hint_forbids_automatic_relaunch(self):
        self.assertIn("never send relaunch_command automatically", STATE_HINTS["exited"])
        self.assertIn("not confirmed", STATE_HINTS["exited"])

    def test_interrupted_hint_says_may(self):
        self.assertIn("may have been cancelled", STATE_HINTS["interrupted"])

    def test_recoverable_and_relaunch_state_sets(self):
        self.assertEqual(sw._RECOVERABLE_STATES, {"exited", "relaunched", "verified"})
        self.assertEqual(sw._RELAUNCH_STATES, {"exit_pending", "exited", "relaunched", "verified", "continued"})
        self.assertTrue(sw._RECOVERABLE_STATES <= set(sw.STATES))
        self.assertTrue(sw._RELAUNCH_STATES <= set(sw.STATES))

    def test_default_prompt_constant(self):
        self.assertEqual(DEFAULT_CONTINUE_PROMPT, "계정 전환으로 세션이 재시작됐어. 중단된 작업을 이어서 진행해줘.")
        self.assertLessEqual(len(DEFAULT_CONTINUE_PROMPT), sw.MAX_PROMPT_CHARS)
        self.assertNotIn("\n", DEFAULT_CONTINUE_PROMPT)


class TestTimeouts(unittest.TestCase):
    def test_defaults(self):
        t = Timeouts()
        self.assertEqual(t.idle_s, 30.0)
        self.assertEqual(t.exit_s, 30.0)
        self.assertEqual(t.shell_s, 5.0)
        self.assertEqual(t.ready_s, 60.0)
        self.assertEqual(t.prompt_s, 15.0)
        self.assertEqual(t.poll_s, 0.5)

    def test_ints_accepted(self):
        t = Timeouts(exit_s=5, poll_s=1)
        self.assertEqual(t.exit_s, 5)
        self.assertIs(type(t.exit_s), int)

    def test_zero_budget_allowed(self):
        self.assertEqual(Timeouts(exit_s=0).exit_s, 0)

    def test_poll_zero_rejected(self):
        with self.assertRaises(ValueError):
            Timeouts(poll_s=0)

    def test_negative_rejected(self):
        with self.assertRaises(ValueError):
            Timeouts(exit_s=-1)

    def test_nan_rejected(self):
        with self.assertRaises(ValueError):
            Timeouts(idle_s=float("nan"))

    def test_inf_rejected(self):
        with self.assertRaises(ValueError):
            Timeouts(ready_s=float("inf"))

    def test_bool_rejected(self):
        with self.assertRaises(ValueError):
            Timeouts(ready_s=True)

    def test_string_rejected(self):
        with self.assertRaises(ValueError):
            Timeouts(prompt_s="1")

    def test_frozen(self):
        with self.assertRaises(dataclasses.FrozenInstanceError):
            Timeouts().exit_s = 1.0


class TestReceipts(unittest.TestCase):
    def test_send_receipt_defaults(self):
        r = SendReceipt("accepted")
        self.assertEqual(r.delivery, "accepted")
        self.assertIsNone(r.refused_reason)
        self.assertEqual(r.stages, ())
        self.assertIsNone(r.observation)
        self.assertIsNone(r.request_id)
        self.assertEqual(r.raw, "")

    def test_send_receipt_bad_delivery(self):
        with self.assertRaises(ValueError):
            SendReceipt("maybe")

    def test_send_receipt_repr_hides_raw(self):
        self.assertNotIn("SECRET-RAW", repr(SendReceipt("accepted", raw="SECRET-RAW")))

    def test_wait_receipt_defaults(self):
        w = WaitReceipt(True)
        self.assertIs(w.satisfied, True)
        self.assertIsNone(w.status)
        self.assertIsNone(w.blocked_reason)
        self.assertEqual(w.raw, "")

    def test_screen_receipt_repr_hides_tail_and_draft(self):
        s = ScreenReceipt(ok=True, tail=("TAILTEXT",), source="screen", draft="DRAFTTEXT", raw="RAWTEXT")
        text = repr(s)
        for hidden in ("TAILTEXT", "DRAFTTEXT", "RAWTEXT"):
            self.assertNotIn(hidden, text)
        self.assertIn("screen", text)
        self.assertIsNone(ScreenReceipt(ok=False).agent_wait)
        self.assertEqual(ScreenReceipt(ok=False).tail, ())

    def test_proc_env_defaults_and_repr(self):
        self.assertEqual(ProcEnv(), ProcEnv(config_dir=None, terminal_handle=None, present=frozenset()))
        env = ProcEnv(config_dir="/Users/user/h", terminal_handle="term_00000000", present=frozenset({"TMUX"}))
        text = repr(env)
        self.assertNotIn("/Users/user/h", text)
        self.assertNotIn("term_00000000", text)
        self.assertIn("TMUX", text)


class TestSwitchResultValue(unittest.TestCase):
    def test_fields_and_repr(self):
        r = SwitchResult(
            ok=False,
            step="wait_exit",
            reason="exit_timeout",
            state="exit_pending",
            new_pid=None,
            relaunch_command="cd /Users/user/p && x",
            recoverable=False,
            request_id="r1",
            other_account_id="acc",
        )
        self.assertIs(r.ok, False)
        self.assertEqual(r.step, "wait_exit")
        self.assertEqual(r.reason, "exit_timeout")
        self.assertEqual(r.state, "exit_pending")
        self.assertIsNone(r.new_pid)
        self.assertEqual(r.relaunch_command, "cd /Users/user/p && x")
        self.assertIs(r.recoverable, False)
        self.assertEqual(r.request_id, "r1")
        self.assertEqual(r.other_account_id, "acc")
        self.assertNotIn("/Users/user/p", repr(r))

    def test_relaunch_command_required(self):
        with self.assertRaises(TypeError):
            SwitchResult(ok=True, step="preflight", reason=None, state="untouched", new_pid=None, recoverable=False)


class TestWaitUntil(unittest.TestCase):
    def _counting(self, results):
        calls = []

        def probe():
            calls.append(1)
            i = len(calls) - 1
            return results[i] if i < len(results) else None

        return probe, calls

    def test_zero_budget_probes_once(self):
        c = FakeClock()
        probe, calls = self._counting([])
        self.assertIsNone(sw._wait_until(probe, 0, 0.5, c.clock, c.sleep))
        self.assertEqual(len(calls), 1)
        self.assertEqual(c.sleeps, [])

    def test_five_by_one_probes_five_times(self):
        c = FakeClock()
        probe, calls = self._counting([])
        self.assertIsNone(sw._wait_until(probe, 5, 1, c.clock, c.sleep))
        self.assertEqual(len(calls), 5)
        self.assertEqual(c.now, 5.0)

    def test_truthy_on_third_returns_value(self):
        c = FakeClock()
        probe, calls = self._counting([None, 0, "ok"])
        self.assertEqual(sw._wait_until(probe, 10, 1, c.clock, c.sleep), "ok")
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(c.sleeps), 2)

    def test_last_sleep_clipped_to_deadline(self):
        c = FakeClock()
        probe, calls = self._counting([])
        self.assertIsNone(sw._wait_until(probe, 2.5, 1, c.clock, c.sleep))
        self.assertEqual(c.sleeps, [1, 1, 0.5])
        self.assertEqual(len(calls), 3)


class TestSwitchBudget(unittest.TestCase):
    def test_default_budget_is_380(self):
        self.assertEqual(switch_budget_s(Timeouts()), 380.0)

    def test_budget_tracks_timeouts(self):
        t = Timeouts(idle_s=0, exit_s=0, shell_s=0, ready_s=0, prompt_s=0)
        self.assertEqual(switch_budget_s(t), 240.0)


# ---------------------------------------------------------------- Task 03

HOME_SP = "/Users/user/Profile Root/00000000-0000-0000-0000-000000000002/home"
SID1 = "00000000-0000-4000-8000-000000000001"


def _sess(argv, *, cwd="/Users/user/proj", sid=SID1):
    return Session(
        pid=1111,
        session_id=sid,
        cwd=cwd,
        status="idle",
        config_dir="/Users/user/old/home",
        argv=tuple(argv),
        terminal_handle="term_00000000",
    )


class TestRelaunchCommand(unittest.TestCase):
    def test_r6_example(self):
        s = _sess(
            (
                "/Users/user/.nvm/versions/node/v24/bin/claude",
                "--permission-mode",
                "bypassPermissions",
                "--resume",
                "OLD-ID",
                "--model",
                "claude-opus-5-5",
                "fix the tests",
            )
        )
        self.assertEqual(
            build_relaunch_command(s, HOME_SP),
            "cd /Users/user/proj && /usr/bin/env CLAUDE_CONFIG_DIR='/Users/user/Profile Root/"
            "00000000-0000-0000-0000-000000000002/home' ORCA_CLAUDE_INJECTED_CONFIG_DIR='/Users/user/Profile Root/"
            "00000000-0000-0000-0000-000000000002/home' /Users/user/.nvm/versions/node/v24/bin/claude --resume "
            "00000000-0000-4000-8000-000000000001 --permission-mode bypassPermissions --model claude-opus-5-5",
        )

    def test_bare_claude_only_resume(self):
        self.assertEqual(
            build_relaunch_command(_sess(("claude",)), "/Users/user/h"),
            "cd /Users/user/proj && /usr/bin/env CLAUDE_CONFIG_DIR=/Users/user/h "
            "ORCA_CLAUDE_INJECTED_CONFIG_DIR=/Users/user/h claude --resume 00000000-0000-4000-8000-000000000001",
        )

    def test_cwd_with_space_quoted(self):
        line = build_relaunch_command(_sess(("claude",), cwd="/Users/user/my proj"), "/Users/user/h")
        self.assertTrue(line.startswith("cd '/Users/user/my proj' && "))

    def test_argv0_with_space_quoted(self):
        line = build_relaunch_command(_sess(("/Users/user/my bin/claude",)), "/Users/user/h")
        self.assertIn(" ORCA_CLAUDE_INJECTED_CONFIG_DIR=/Users/user/h '/Users/user/my bin/claude' --resume ", line)

    def test_home_not_normalised(self):
        line = build_relaunch_command(_sess(("claude",)), "/Users/user/h/")
        self.assertIn("CLAUDE_CONFIG_DIR=/Users/user/h/ ", line)

    def test_equals_value_quoted(self):
        line = build_relaunch_command(_sess(("claude", "--model", "=x")), "/Users/user/h")
        self.assertTrue(line.endswith(" --model '=x'"))

    def test_dangerous_flag_kept(self):
        line = build_relaunch_command(_sess(("claude", "--dangerously-skip-permissions")), "/Users/user/h")
        self.assertTrue(line.endswith(" --resume " + SID1 + " --dangerously-skip-permissions"))

    def test_argv0_not_claude(self):
        with self.assertRaises(ValueError):
            build_relaunch_command(_sess(("node", "x")), "/Users/user/h")

    def test_empty_argv(self):
        with self.assertRaises(ValueError):
            build_relaunch_command(_sess(()), "/Users/user/h")

    def test_session_id_leading_dash(self):
        with self.assertRaises(ValueError):
            build_relaunch_command(_sess(("claude",), sid="-x"), "/Users/user/h")

    def test_session_id_with_space(self):
        with self.assertRaises(ValueError):
            build_relaunch_command(_sess(("claude",), sid="a b"), "/Users/user/h")

    def test_relative_cwd(self):
        with self.assertRaises(ValueError):
            build_relaunch_command(_sess(("claude",), cwd="proj"), "/Users/user/h")

    def test_relative_home(self):
        with self.assertRaises(ValueError):
            build_relaunch_command(_sess(("claude",)), "h")

    def test_newline_in_value(self):
        with self.assertRaises(ValueError):
            build_relaunch_command(_sess(("claude", "--model", "a\nb")), "/Users/user/h")

    def test_backslash_in_value(self):
        with self.assertRaises(ValueError):
            build_relaunch_command(_sess(("claude", "--model", "a\\b")), "/Users/user/h")


class TestPreservedArgs(unittest.TestCase):
    def _kept(self, *rest):
        return sw._preserved_args(("claude",) + rest)

    def test_brief_flags_kept_in_order(self):
        self.assertEqual(
            self._kept("--permission-mode", "X", "--model", "Y", "--dangerously-skip-permissions"),
            ["--permission-mode", "X", "--model", "Y", "--dangerously-skip-permissions"],
        )

    def test_resume_continue_and_session_flags_dropped(self):
        cases = [
            (("--resume", "OLD"), []),
            (("--resume=OLD",), []),
            (("-r",), []),
            (("-r", "--model", "Y"), ["--model", "Y"]),
            (("-c",), []),
            (("--continue",), []),
            (("--session-id", "U"), []),
            (("--fork-session",), []),
            (("-w",), []),
            (("-w", "name"), []),
            (("--worktree",), []),
            (("--worktree", "name"), []),
            (("--worktree=name",), []),
            (("--worktree", "name", "--model", "Y"), ["--model", "Y"]),
            (("fix it",), []),
        ]
        for rest, expected in cases:
            with self.subTest(rest=rest):
                self.assertEqual(self._kept(*rest), expected)

    def test_variadic_keeps_following_values(self):
        self.assertEqual(self._kept("--add-dir", "/a", "/b", "go"), ["--add-dir", "/a", "/b", "go"])

    def test_lone_dash_is_value(self):
        self.assertEqual(self._kept("--debug", "-"), ["--debug", "-"])

    def test_value_option_takes_dash_value(self):
        self.assertEqual(self._kept("--model", "-x"), ["--model", "-x"])

    def test_double_dash_ends_options(self):
        self.assertEqual(self._kept("--model", "Y", "--", "x"), ["--model", "Y"])

    def test_tokens_after_double_dash_not_reparsed(self):
        self.assertEqual(self._kept("--model", "Y", "--", "--model", "Z"), ["--model", "Y"])

    def test_equals_forms_kept(self):
        self.assertEqual(self._kept("--tmux=classic"), ["--tmux=classic"])
        self.assertEqual(self._kept("--model=Y"), ["--model=Y"])

    def test_unemittable_options_refused(self):
        for rest in (
            ("--zzz",),
            ("-cp",),
            ("-n=x",),
            ("--print",),
            ("-p",),
            ("--print=1",),
            ("--verbose=1",),
            ("--model",),
            ("--resume", "x", "--teleport"),
        ):
            with self.subTest(rest=rest):
                with self.assertRaises(ValueError):
                    self._kept(*rest)


class TestValidHandle(unittest.TestCase):
    def test_valid(self):
        self.assertTrue(sw._valid_handle("term_00000000"))

    def test_invalid(self):
        for h in ("", "-x", "term 0", "term\t0", "term\x00", "term\x7f", None, 5):
            with self.subTest(h=h):
                self.assertFalse(sw._valid_handle(h))


# ---------------------------------------------------------------- Task 04


class FakeRun:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def __call__(self, argv, timeout_s):
        self.calls.append((list(argv), timeout_s))
        return self.results.pop(0) if self.results else (0, "", "")


H = "term_00000000"
B = "/x/orca"


class TestOrcaCliDriver(unittest.TestCase):
    def _driver(self, *results):
        fake = FakeRun(*results)
        return OrcaCliDriver(binary=B, run=fake), fake

    def test_send_text_enter_argv_and_timeout(self):
        d, fake = self._driver((0, '{"send":{"accepted":true}}', ""))
        d.send(H, "/exit", enter=True, interrupt=False)
        self.assertEqual(fake.calls, [([B, "terminal", "send", "--terminal", H, "--text", "/exit", "--enter", "--json"], 50.0)])

    def test_interrupt_only_argv_and_timeout(self):
        d, fake = self._driver()
        d.send(H, None, enter=False, interrupt=True)
        self.assertEqual(fake.calls, [([B, "terminal", "send", "--terminal", H, "--interrupt", "--json"], 15.0)])

    def test_read_argv_and_timeout(self):
        d, fake = self._driver()
        d.read_screen(H)
        self.assertEqual(fake.calls, [([B, "terminal", "read", "--terminal", H, "--screen", "--json"], 15.0)])

    def test_wait_argv_and_timeout(self):
        d, fake = self._driver()
        d.wait_for(H, "exit", 5000)
        self.assertEqual(
            fake.calls,
            [([B, "terminal", "wait", "--terminal", H, "--for", "exit", "--timeout-ms", "5000", "--json"], 10.0)],
        )

    def test_accepted_with_prompt_receipt(self):
        d, _ = self._driver(
            (
                0,
                '{"ok":true,"result":{"send":{"accepted":true,"prompt":{"requestId":"r1",'
                '"stages":["input_accepted","turn_started"],"observation":"turn_started"}}}}',
                "",
            )
        )
        r = d.send(H, "go", enter=True, interrupt=False)
        self.assertEqual(r.delivery, "accepted")
        self.assertEqual(r.stages, ("input_accepted", "turn_started"))
        self.assertEqual(r.observation, "turn_started")
        self.assertEqual(r.request_id, "r1")
        self.assertIsNone(r.refused_reason)

    def test_send_object_at_top_level(self):
        d, _ = self._driver((0, '{"send":{"accepted":true}}', ""))
        r = d.send(H, "go", enter=True, interrupt=False)
        self.assertEqual(r.delivery, "accepted")
        self.assertEqual(r.stages, ())

    def test_refused_with_reason(self):
        d, _ = self._driver((1, '{"result":{"send":{"accepted":false,"refusedReason":"busy"}}}', ""))
        r = d.send(H, "go", enter=True, interrupt=False)
        self.assertEqual((r.delivery, r.refused_reason), ("refused", "busy"))

    def test_refused_without_reason(self):
        d, _ = self._driver((1, '{"result":{"send":{"accepted":false}}}', ""))
        r = d.send(H, "go", enter=True, interrupt=False)
        self.assertEqual((r.delivery, r.refused_reason), ("refused", "refused"))

    def test_not_executed_is_refused(self):
        d, _ = self._driver((127, "", "not executable"))
        r = d.send(H, "go", enter=True, interrupt=False)
        self.assertEqual((r.delivery, r.refused_reason), ("refused", "not_executed"))

    def test_timeout_is_unconfirmed(self):
        d, _ = self._driver((124, "", "timeout"))
        r = d.send(H, "go", enter=True, interrupt=False)
        self.assertEqual(r.delivery, "unconfirmed")
        self.assertIsNone(r.refused_reason)
        self.assertIn("[stderr] timeout", r.raw)

    def test_not_json_is_unconfirmed(self):
        d, _ = self._driver((1, "not json", ""))
        r = d.send(H, "go", enter=True, interrupt=False)
        self.assertEqual(r.delivery, "unconfirmed")
        self.assertEqual(r.raw, "not json")

    def test_error_envelope_is_unconfirmed(self):
        d, _ = self._driver((1, '{"ok":false,"error":{"code":"partial_write","bytesHandedToTransport":true}}', ""))
        self.assertEqual(d.send(H, "go", enter=True, interrupt=False).delivery, "unconfirmed")

    def test_string_accepted_is_unconfirmed(self):
        d, _ = self._driver((0, '{"result":{"send":{"accepted":"true"}}}', ""))
        self.assertEqual(d.send(H, "go", enter=True, interrupt=False).delivery, "unconfirmed")

    def test_rc0_ok_false_is_unconfirmed(self):
        d, _ = self._driver((0, '{"ok":false,"result":{"send":{"accepted":true}}}', ""))
        self.assertEqual(d.send(H, "go", enter=True, interrupt=False).delivery, "unconfirmed")

    def test_nonzero_rc_with_accepted_true_is_unconfirmed(self):
        d, _ = self._driver((1, '{"result":{"send":{"accepted":true}}}', ""))
        self.assertEqual(d.send(H, "go", enter=True, interrupt=False).delivery, "unconfirmed")

    def test_non_dict_json_is_unconfirmed(self):
        d, _ = self._driver((0, "[1,2]", ""))
        self.assertEqual(d.send(H, "go", enter=True, interrupt=False).delivery, "unconfirmed")

    def test_read_parses_tail_draft_agent_wait(self):
        d, _ = self._driver(
            (
                0,
                '{"result":{"terminal":{"tail":["❯ ", 5],"source":"screen","draft":"queued text",'
                '"agentWait":{"source":"prompt-text","reason":"r"}}}}',
                "",
            )
        )
        s = d.read_screen(H)
        self.assertIs(s.ok, True)
        self.assertEqual(s.tail, ("❯ ",))
        self.assertEqual(s.source, "screen")
        self.assertEqual(s.draft, "queued text")
        self.assertEqual(s.agent_wait, ("prompt-text", "r"))

    def test_read_failure_not_ok(self):
        d, _ = self._driver((1, "", "boom"))
        s = d.read_screen(H)
        self.assertIs(s.ok, False)
        self.assertEqual(s.tail, ())
        self.assertIn("[stderr] boom", s.raw)

    def test_wait_satisfied(self):
        d, _ = self._driver((0, '{"result":{"wait":{"satisfied":true,"status":"exited","blockedReason":null}}}', ""))
        w = d.wait_for(H, "exit", 5000)
        self.assertIs(w.satisfied, True)
        self.assertEqual(w.status, "exited")
        self.assertIsNone(w.blocked_reason)

    def test_wait_nonzero_rc_not_satisfied(self):
        d, _ = self._driver((1, '{"result":{"wait":{"satisfied":true,"status":"exited","blockedReason":null}}}', ""))
        self.assertIs(d.wait_for(H, "exit", 5000).satisfied, False)

    def test_invalid_handle_rejected_before_run(self):
        d, fake = self._driver()
        with self.assertRaises(ValueError):
            d.send("-x", "a", enter=True, interrupt=False)
        with self.assertRaises(ValueError):
            d.read_screen("a b")
        with self.assertRaises(ValueError):
            d.wait_for("", "exit", 1)
        self.assertEqual(fake.calls, [])

    def test_option_like_text_rejected(self):
        d, fake = self._driver()
        with self.assertRaises(ValueError):
            d.send(H, "-x", enter=True, interrupt=False)
        with self.assertRaises(ValueError):
            d.send(H, "", enter=True, interrupt=False)
        self.assertEqual(fake.calls, [])

    def test_default_wiring_by_identity(self):
        d = OrcaCliDriver()
        self.assertIs(d._run, sw._run_subprocess)
        self.assertEqual(d._binary, sw.ORCA_BINARY_DEFAULT)


# ---------------------------------------------------------------- Task 05

R12_LINE = (
    "claude --resume X CLAUDE_CONFIG_DIR=/Users/user/Library/Application Support/orca-test/0000/home "
    "ORCA_TERMINAL_HANDLE=term_0"
)


class TestParseEnv(unittest.TestCase):
    def test_space_path_and_handle(self):
        env = sw._parse_env(R12_LINE)
        self.assertEqual(env.config_dir, "/Users/user/Library/Application Support/orca-test/0000/home")
        self.assertEqual(env.terminal_handle, "term_0")
        self.assertEqual(env.present, frozenset())

    def test_no_variable(self):
        self.assertEqual(sw._parse_env("claude --resume X"), ProcEnv())

    def test_last_match_wins(self):
        self.assertEqual(sw._parse_env("claude CLAUDE_CONFIG_DIR=/a FOO=1 CLAUDE_CONFIG_DIR=/b").config_dir, "/b")

    def test_tmux_presence_value_not_captured(self):
        env = sw._parse_env("claude TMUX=/x,1,0 ORCA_TERMINAL_HANDLE=term_0")
        self.assertEqual(env.present, frozenset({"TMUX"}))
        self.assertNotIn("/x,1,0", env.config_dir or "")
        self.assertNotIn("/x,1,0", env.terminal_handle or "")
        self.assertNotIn("/x,1,0", repr(env))

    def test_tmux_pane_alone_not_presence(self):
        self.assertEqual(sw._parse_env("claude TMUX_PANE=%1").present, frozenset())

    def test_sty_presence(self):
        self.assertEqual(sw._parse_env("claude STY=1.x").present, frozenset({"STY"}))

    def test_empty_line(self):
        self.assertEqual(sw._parse_env(""), ProcEnv())


class TestParseProcessTable(unittest.TestCase):
    def test_three_rows_normalised(self):
        out = "  10     1 /sbin/launchd\n 20 10 -zsh\n 30 20 /x/Orca Helper (Renderer)\n"
        self.assertEqual(
            sw._parse_process_table(out),
            {10: (1, "launchd"), 20: (10, "zsh"), 30: (20, "orca helper (renderer)")},
        )

    def test_unparsable_lines_skipped(self):
        self.assertEqual(sw._parse_process_table("garbage\n 5 1 login\n"), {5: (1, "login")})

    def test_empty_stdout(self):
        self.assertEqual(sw._parse_process_table(""), {})


class TestOsProcessProbe(unittest.TestCase):
    def _kill(self, exc=None):
        calls = []

        def kill(pid, sig):
            calls.append((pid, sig))
            if exc is not None:
                raise exc

        return kill, calls

    def test_alive_when_kill_returns(self):
        kill, calls = self._kill()
        self.assertIs(OsProcessProbe(kill=kill).is_alive(42), True)
        self.assertEqual(calls, [(42, 0)])

    def test_dead_on_process_lookup_error(self):
        kill, _ = self._kill(ProcessLookupError())
        self.assertIs(OsProcessProbe(kill=kill).is_alive(42), False)

    def test_alive_on_permission_error(self):
        kill, _ = self._kill(PermissionError())
        self.assertIs(OsProcessProbe(kill=kill).is_alive(42), True)

    def test_other_error_propagates(self):
        kill, _ = self._kill(OSError(22, "x"))
        with self.assertRaises(OSError):
            OsProcessProbe(kill=kill).is_alive(42)

    def test_read_env_argv_and_parse(self):
        fake = FakeRun((0, R12_LINE + "\n", ""))
        env = OsProcessProbe(run=fake).read_env(42)
        self.assertEqual(fake.calls, [(["/bin/ps", "-E", "-ww", "-o", "command=", "-p", "42"], 5.0)])
        self.assertEqual(env.config_dir, "/Users/user/Library/Application Support/orca-test/0000/home")
        self.assertEqual(env.terminal_handle, "term_0")

    def test_read_env_nonzero_rc_is_none(self):
        self.assertIsNone(OsProcessProbe(run=FakeRun((1, "", ""))).read_env(42))

    def test_process_table_argv_and_parse(self):
        fake = FakeRun((0, " 20 10 zsh\n", ""))
        self.assertEqual(OsProcessProbe(run=fake).process_table(), {20: (10, "zsh")})
        self.assertEqual(fake.calls, [(["/bin/ps", "-A", "-o", "pid=,ppid=,comm="], 5.0)])

    def test_process_table_rc_raises(self):
        with self.assertRaises(OSError):
            OsProcessProbe(run=FakeRun((1, "", ""))).process_table()

    def test_process_table_empty_raises(self):
        with self.assertRaises(OSError):
            OsProcessProbe(run=FakeRun((0, "junk\n", ""))).process_table()

    def test_default_wiring_by_identity(self):
        p = OsProcessProbe()
        self.assertIs(p._kill, os.kill)
        self.assertIs(p._run, sw._run_subprocess)


# ---------------------------------------------------------------- Task 06

RULE = "─" * 40


def _screen(*inner, above=("● earlier answer",), below=("  ? for shortcuts",), ok=True, source="screen"):
    return ScreenReceipt(ok=ok, source=source, tail=tuple(above) + (RULE,) + tuple(inner) + (RULE,) + tuple(below))


EMPTY_SCREEN = _screen("❯ ")
TRUST_SCREEN = _screen("❯ 1. Yes, I trust this folder", above=("Trust this directory?",))

HOST, LOGIN, SH = 1108, 1109, 1110
ORCA_TABLE = {1111: (SH, "claude"), SH: (LOGIN, "zsh"), LOGIN: (HOST, "login"), HOST: (1, "orca-terminal-host")}
OK_ENV = ProcEnv(terminal_handle="term_00000000")


class TestClassifyScreen(unittest.TestCase):
    def test_empty_box_clear(self):
        self.assertIsNone(classify_screen(EMPTY_SCREEN))

    def test_placeholder_clear(self):
        self.assertIsNone(classify_screen(_screen('❯ Try "fix lint"')))

    def test_nbsp_and_box_chars_clear(self):
        self.assertIsNone(classify_screen(_screen("│ ❯  │")))

    def test_gt_prompt_clear(self):
        self.assertIsNone(classify_screen(_screen("> ")))

    def test_typed_text_not_empty(self):
        self.assertEqual(classify_screen(_screen("❯ wip")), "input_not_empty")

    def test_multiline_input_not_empty(self):
        self.assertEqual(classify_screen(_screen("❯ a", "  b")), "input_not_empty")

    def test_trust_marker_in_bottom_region(self):
        self.assertEqual(classify_screen(_screen("❯ ", below=("Yes, I trust this folder",))), "trust_dialog")

    def test_dialog_marker_in_bottom_region(self):
        self.assertEqual(classify_screen(_screen("❯ ", below=("Do you want to proceed?",))), "dialog_open")

    def test_marker_only_in_transcript_ignored(self):
        self.assertIsNone(classify_screen(_screen("❯ ", above=("Do you want to proceed?", "Trust this directory?"))))

    def test_one_rule_marker_in_last_lines(self):
        s = ScreenReceipt(ok=True, source="screen", tail=("x", RULE, "Do you want to proceed?"))
        self.assertEqual(classify_screen(s), "dialog_open")

    def test_one_rule_no_marker_unreadable(self):
        s = ScreenReceipt(ok=True, source="screen", tail=("x", RULE, "❯ "))
        self.assertEqual(classify_screen(s), "screen_unreadable")

    def test_marker_outside_bottom_window_one_rule(self):
        tail = ("Do you want to proceed?",) + tuple("line %d" % i for i in range(14)) + (RULE,)
        self.assertEqual(classify_screen(ScreenReceipt(ok=True, source="screen", tail=tail)), "screen_unreadable")

    def test_marker_inside_bottom_window_one_rule(self):
        tail = ("Do you want to proceed?",) + tuple("line %d" % i for i in range(13)) + (RULE,)
        self.assertEqual(classify_screen(ScreenReceipt(ok=True, source="screen", tail=tail)), "dialog_open")

    def test_short_rule_not_a_rule(self):
        short = "─" * 9
        s = ScreenReceipt(ok=True, source="screen", tail=("x", short, "❯ ", short))
        self.assertEqual(classify_screen(s), "screen_unreadable")

    def test_rule_of_min_length_is_a_rule(self):
        rule10 = "─" * 10
        s = ScreenReceipt(ok=True, source="screen", tail=("x", rule10, "❯ ", rule10))
        self.assertIsNone(classify_screen(s))

    def test_not_ok_unreadable(self):
        self.assertEqual(classify_screen(_screen("❯ ", ok=False)), "screen_unreadable")

    def test_screen_unavailable_source(self):
        self.assertEqual(classify_screen(_screen("❯ ", source="screen-unavailable")), "screen_unreadable")

    def test_trust_wins_over_dialog(self):
        s = _screen("❯ ", below=("Do you want to proceed?", "Trust this directory?"))
        self.assertEqual(classify_screen(s), "trust_dialog")

    def test_draft_ignored(self):
        self.assertIsNone(classify_screen(dataclasses.replace(EMPTY_SCREEN, draft="queued text")))


def _shell_chain(n_shells):
    table = {1111: (2000, "claude")}
    for i in range(n_shells):
        table[2000 + i] = (2000 + i + 1, "zsh")
    table[2000 + n_shells] = (1, "orca-terminal-host")
    return table


BINDING_TABLES = {
    "orca": ORCA_TABLE,
    "tmux": {1111: (SH, "claude"), SH: (50, "zsh"), 50: (1, "tmux")},
    "sshd": {1111: (SH, "claude"), SH: (50, "zsh"), 50: (1, "sshd")},
    "inherited": {
        1111: (60, "claude.exe"),
        60: (61, "sh"),
        61: (62, "zsh"),
        62: (63, "claude"),
        63: (LOGIN, "zsh"),
        LOGIN: (HOST, "login"),
        HOST: (1, "orca-terminal-host"),
    },
    "empty": {},
    "parent_absent": {1111: (999, "claude")},
    "launchd": {1111: (SH, "claude"), SH: (1, "zsh")},
    "direct_child": {1111: (HOST, "claude"), HOST: (1, "orca-terminal-host")},
    "login_only": {1111: (LOGIN, "claude"), LOGIN: (HOST, "login"), HOST: (1, "orca-terminal-host")},
    "fifteen": _shell_chain(15),
    "sixteen": _shell_chain(16),
}


class TestCheckPaneBinding(unittest.TestCase):
    def _check(self, table, env=OK_ENV, handle="term_00000000"):
        return check_pane_binding(1111, handle, table, env)

    def test_orca_chain_bound(self):
        self.assertIsNone(self._check(ORCA_TABLE))

    def test_login_shell_dash_normalised(self):
        self.assertIsNone(self._check({**ORCA_TABLE, SH: (LOGIN, "-zsh")}))

    def test_absolute_comm_normalised(self):
        self.assertIsNone(self._check({**ORCA_TABLE, SH: (LOGIN, "/bin/zsh")}))

    def test_tmux_ancestor(self):
        self.assertEqual(self._check(BINDING_TABLES["tmux"]), "multiplexer")

    def test_sshd_ancestor(self):
        self.assertEqual(self._check(BINDING_TABLES["sshd"]), "multiplexer")

    def test_claude_child_of_claude(self):
        self.assertEqual(self._check(BINDING_TABLES["inherited"]), "foreign_ancestor")

    def test_pid_absent(self):
        self.assertEqual(self._check({}), "chain_broken")

    def test_parent_absent(self):
        self.assertEqual(self._check(BINDING_TABLES["parent_absent"]), "chain_broken")

    def test_reaches_launchd(self):
        self.assertEqual(self._check(BINDING_TABLES["launchd"]), "chain_broken")

    def test_fifteen_shells_then_host_bound(self):
        self.assertIsNone(self._check(BINDING_TABLES["fifteen"]))

    def test_sixteen_shells_hop_limit(self):
        self.assertEqual(self._check(BINDING_TABLES["sixteen"]), "hop_limit")

    def test_no_shell_between_claude_and_host(self):
        self.assertEqual(self._check(BINDING_TABLES["direct_child"]), "no_shell")

    def test_login_without_shell_is_no_shell(self):
        self.assertEqual(self._check(BINDING_TABLES["login_only"]), "no_shell")

    def test_one_shell_is_enough(self):
        table = {1111: (SH, "claude"), SH: (HOST, "zsh"), HOST: (1, "orca-terminal-host")}
        self.assertIsNone(self._check(table))

    def test_env_none(self):
        self.assertEqual(self._check(ORCA_TABLE, env=None), "env_unreadable")

    def test_env_sty(self):
        env = ProcEnv(terminal_handle="term_00000000", present=frozenset({"STY"}))
        self.assertEqual(self._check(ORCA_TABLE, env=env), "env_multiplexer")

    def test_env_tmux(self):
        env = ProcEnv(terminal_handle="term_00000000", present=frozenset({"TMUX"}))
        self.assertEqual(self._check(ORCA_TABLE, env=env), "env_multiplexer")

    def test_other_handle(self):
        self.assertEqual(self._check(ORCA_TABLE, env=ProcEnv(terminal_handle="term_11111111")), "handle_mismatch")

    def test_handle_missing(self):
        self.assertEqual(self._check(ORCA_TABLE, env=ProcEnv()), "handle_mismatch")

    def test_never_returns_table_unavailable(self):
        envs = (OK_ENV, None, ProcEnv(), ProcEnv(terminal_handle="term_00000000", present=frozenset({"TMUX"})))
        for name, table in BINDING_TABLES.items():
            for env in envs:
                with self.subTest(table=name, env=env):
                    fault = self._check(table, env=env)
                    self.assertTrue(fault is None or (fault in sw.BINDING_FAULTS and fault != "table_unavailable"))


# ---------------------------------------------------------------- Task 07: shared fake world

ACC_OLD = "00000000-0000-0000-0000-000000000001"
ACC_NEW = "00000000-0000-0000-0000-000000000002"
ACC_OTHER = "00000000-0000-0000-0000-000000000003"
ROOT = "/Users/user/Profile Root"
OLD_HOME = ROOT + "/" + ACC_OLD + "/home"
NEW_HOME = ROOT + "/" + ACC_NEW + "/home"
OTHER_HOME = ROOT + "/" + ACC_OTHER + "/home"
SID = "00000000-0000-4000-8000-000000000001"
CWD = "/Users/user/proj/repo"
HANDLE = "term_00000000"
OLD_PID, NEW_PID = 1111, 4242
OLD_REG = OLD_HOME + "/sessions/1111.json"
NEW_REG = NEW_HOME + "/sessions/4242.json"
OTHER_REG = OTHER_HOME + "/sessions/4242.json"
TRANSCRIPT = NEW_HOME + "/projects/-Users-user-proj-repo/" + SID + ".jsonl"
TRUST_FILE = NEW_HOME + "/.claude.json"
FAST = Timeouts(idle_s=5.0, exit_s=5.0, shell_s=5.0, ready_s=5.0, prompt_s=5.0, poll_s=1.0)
LINE = (
    "cd /Users/user/proj/repo && /usr/bin/env CLAUDE_CONFIG_DIR='/Users/user/Profile Root/"
    "00000000-0000-0000-0000-000000000002/home' ORCA_CLAUDE_INJECTED_CONFIG_DIR='/Users/user/Profile Root/"
    "00000000-0000-0000-0000-000000000002/home' claude --resume 00000000-0000-4000-8000-000000000001 --model opus"
)


def reg(pid, status, sid=SID, started_at=1000):
    return json.dumps(
        {
            "pid": pid,
            "sessionId": sid,
            "cwd": CWD,
            "startedAt": started_at,
            "status": status,
            "statusUpdatedAt": started_at,
            "procStart": "0",
        }
    )


def trust(*dirs):
    return json.dumps({"projects": {d: {"hasTrustDialogAccepted": True} for d in dirs}})


class Seq:
    def __init__(self, *values):
        self.values = values
        self.i = 0

    def peek(self):
        return self.values[min(self.i, len(self.values) - 1)]

    def take(self):
        v = self.peek()
        self.i += 1
        return v


class FakeFs:
    def __init__(self, files, dirs):
        self.files = dict(files)
        self.dirs = set(dirs)
        self.reads = []

    def _get(self, path, advance):
        v = self.files.get(path)
        if isinstance(v, Seq):
            return v.take() if advance else v.peek()
        if callable(v):
            return v()
        return v

    def read_text(self, path):
        self.reads.append(path)
        v = self._get(path, True)
        if v is None:
            raise OSError("absent")
        return v

    def glob_paths(self, pattern):
        return sorted(k for k in self.files if self._get(k, False) is not None and fnmatch.fnmatchcase(k, pattern))

    def isdir(self, path):
        return path in self.dirs


def _raise_or_return(value):
    if isinstance(value, Exception):
        raise value
    return value


class FakeDriver:
    def __init__(self):
        self.calls = []
        self.script = {}
        self.screens = []
        self.waits = []

    @staticmethod
    def _kind(text, interrupt):
        if interrupt:
            return "interrupt"
        if text == "/exit":
            return "exit"
        if text.startswith("cd "):
            return "relaunch"
        return "prompt"

    def send(self, handle, text, *, enter, interrupt):
        self.calls.append(("send", handle, text, enter, interrupt))
        kind = self._kind(text, interrupt)
        queue = self.script.get(kind)
        if queue:
            return _raise_or_return(queue.pop(0))
        if kind == "prompt":
            return SendReceipt(
                "accepted", stages=("input_accepted", "turn_started"), observation="turn_started", request_id="req-1"
            )
        return SendReceipt("accepted")

    def read_screen(self, handle):
        self.calls.append(("read_screen", handle))
        return _raise_or_return(self.screens.pop(0)) if self.screens else EMPTY_SCREEN

    def wait_for(self, handle, condition, timeout_ms):
        self.calls.append(("wait_for", handle, condition, timeout_ms))
        return _raise_or_return(self.waits.pop(0)) if self.waits else WaitReceipt(True)

    def sends(self):
        return [c for c in self.calls if c[0] == "send"]

    def sent(self, kind):
        return any(self._kind(c[2], c[4]) == kind for c in self.sends())


class FakeProcs:
    def __init__(self, driver):
        self.driver = driver
        self.calls = []
        self.alive = {}
        self.envs = {
            OLD_PID: ProcEnv(config_dir=OLD_HOME, terminal_handle=HANDLE),
            NEW_PID: ProcEnv(config_dir=NEW_HOME, terminal_handle=HANDLE),
        }
        self.tables: list = [ORCA_TABLE]
        self._alive_i = {}
        self._table_i = 0

    def is_alive(self, pid):
        self.calls.append(("is_alive", pid))
        if pid in self.alive:
            seq = self.alive[pid]
            i = self._alive_i.get(pid, 0)
            self._alive_i[pid] = i + 1
            v = seq[min(i, len(seq) - 1)]
            if isinstance(v, Exception):
                raise v
            return v
        if pid == OLD_PID:
            return not self.driver.sent("exit")
        if pid == NEW_PID:
            return self.driver.sent("relaunch")
        return False

    def read_env(self, pid):
        self.calls.append(("read_env", pid))
        v = self.envs.get(pid)
        if isinstance(v, Exception):
            raise v
        return v

    def process_table(self):
        self.calls.append(("process_table",))
        v = self.tables[min(self._table_i, len(self.tables) - 1)]
        self._table_i += 1
        if isinstance(v, Exception):
            raise v
        return v


class World:
    def __init__(self, status="idle"):
        self.clock = FakeClock()
        self.driver = FakeDriver()
        self.procs = FakeProcs(self.driver)
        self.session = Session(
            pid=OLD_PID,
            session_id=SID,
            cwd=CWD,
            status="idle",
            config_dir=OLD_HOME,
            argv=("claude", "--model", "opus"),
            terminal_handle=HANDLE,
        )
        self.target = Profile(account_id=ACC_NEW, home=NEW_HOME, keychain_service="Claude Code-credentials-test2")
        if status == "busy":
            old = lambda: reg(OLD_PID, "idle" if self.driver.sent("interrupt") else "busy")  # noqa: E731
        else:
            old = reg(OLD_PID, status)
        self.fs = FakeFs(
            files={
                TRANSCRIPT: "",
                TRUST_FILE: trust(CWD),
                OLD_REG: old,
                NEW_REG: lambda: (
                    reg(NEW_PID, "busy" if self.driver.sent("prompt") else "idle", started_at=2000)
                    if self.driver.sent("relaunch")
                    else None
                ),
            },
            dirs={NEW_HOME, CWD},
        )

    def run(self, mode="soft", **kw):
        args = dict(
            driver=self.driver,
            procs=self.procs,
            read_text=self.fs.read_text,
            glob_paths=self.fs.glob_paths,
            isdir=self.fs.isdir,
            clock=self.clock.clock,
            sleep=self.clock.sleep,
            timeouts=FAST,
            profiles_root=ROOT,
        )
        args.update(kw)
        return switch_session(self.session, self.target, mode, **args)


def assert_untouched(tc, res, reason, world):
    tc.assertEqual(
        res,
        SwitchResult(
            ok=False,
            step="preflight",
            reason=reason,
            state="untouched",
            new_pid=None,
            relaunch_command="",
            recoverable=False,
            request_id=None,
            other_account_id=None,
        ),
    )
    tc.assertEqual(world.driver.calls, [])


def result(step, reason, state, *, new_pid=None, line=LINE, request_id=None, other_account_id=None):
    """Expected SwitchResult with the D3 invariants derived from ``state``."""
    return SwitchResult(
        ok=reason is None and state == "continued",
        step=step,
        reason=reason,
        state=state,
        new_pid=new_pid,
        relaunch_command=line if state in ("exit_pending", "exited", "relaunched", "verified", "continued") else "",
        recoverable=state in ("exited", "relaunched", "verified"),
        request_id=request_id,
        other_account_id=other_account_id,
    )


class TestSwitchArgs(unittest.TestCase):
    def test_driver_required(self):
        w = World()
        with self.assertRaises(TypeError):
            switch_session(w.session, w.target, "soft")

    def test_bad_mode(self):
        w = World()
        with self.assertRaises(ValueError):
            w.run("medium")
        self.assertEqual(w.driver.calls, [])
        self.assertEqual(w.procs.calls, [])

    def test_prompt_rules(self):
        for prompt in ("x" * 801, "a\nb", "a\rb", "/x", "-x", "", "   ", 5, "이어서\x00 진행", "a\tb", "\x1b[0m go", "a\x7fb", "a\x1fb"):
            with self.subTest(prompt=prompt):
                w = World()
                with self.assertRaises(ValueError):
                    w.run("soft", continue_prompt=prompt)
                self.assertEqual(w.driver.calls, [])
                self.assertEqual(w.procs.calls, [])

    def test_prompt_800_chars_passes_validation(self):
        w = World()
        w.session = dataclasses.replace(w.session, terminal_handle=None)
        assert_untouched(self, w.run("soft", continue_prompt="가" * 800), "no_terminal_handle", w)

    def test_bad_timeouts(self):
        w = World()
        with self.assertRaises(ValueError):
            w.run("soft", timeouts={"exit_s": 1})
        self.assertEqual(w.driver.calls, [])


class TestPreflight(unittest.TestCase):
    def _shell_registry(self, w):
        w.fs.files[OLD_REG] = reg(OLD_PID, "shell")

    def test_no_terminal_handle(self):
        w = World()
        w.session = dataclasses.replace(w.session, terminal_handle=None)
        assert_untouched(self, w.run(), "no_terminal_handle", w)
        self.assertEqual(w.procs.calls, [])

    def test_handle_invalid(self):
        for h in ("", "-x", "term 0", "term\t0"):
            with self.subTest(h=h):
                w = World()
                w.session = dataclasses.replace(w.session, terminal_handle=h)
                assert_untouched(self, w.run(), "terminal_handle_invalid", w)

    def test_no_argv(self):
        w = World()
        w.session = dataclasses.replace(w.session, argv=())
        assert_untouched(self, w.run(), "no_argv", w)

    def test_relaunch_unbuildable(self):
        w = World()
        w.session = dataclasses.replace(w.session, argv=("node", "x"))
        assert_untouched(self, w.run(), "relaunch_unbuildable", w)

    def test_same_account(self):
        w = World()
        w.target = Profile(account_id=ACC_OLD, home=OLD_HOME + "/", keychain_service="Claude Code-credentials-test1")
        assert_untouched(self, w.run(), "same_account", w)

    def test_target_home_missing(self):
        w = World()
        w.fs.dirs.discard(NEW_HOME)
        assert_untouched(self, w.run(), "target_home_missing", w)

    def test_cwd_missing(self):
        w = World()
        w.fs.dirs.discard(CWD)
        assert_untouched(self, w.run(), "cwd_missing", w)

    def test_transcript_not_visible(self):
        w = World()
        del w.fs.files[TRANSCRIPT]
        assert_untouched(self, w.run(), "transcript_not_visible", w)

    def test_transcript_under_other_project_dir_passes(self):
        w = World()
        del w.fs.files[TRANSCRIPT]
        w.fs.files[NEW_HOME + "/projects/-x-proj/" + SID + ".jsonl"] = ""
        self._shell_registry(w)
        assert_untouched(self, w.run(), "pane_not_claude", w)

    def test_bracket_in_home_matched_literally(self):
        w = World()
        home = ROOT + "/[a]/home"
        w.target = Profile(account_id="[a]", home=home, keychain_service="Claude Code-credentials-test3")
        w.fs.files[home + "/projects/-Users-user-proj-repo/" + SID + ".jsonl"] = ""
        w.fs.files[home + "/.claude.json"] = trust(CWD)
        w.fs.dirs.add(home)
        self._shell_registry(w)
        assert_untouched(self, w.run(), "pane_not_claude", w)

    def test_bracket_in_home_not_a_character_class(self):
        w = World()
        home = ROOT + "/[a]/home"
        w.target = Profile(account_id="[a]", home=home, keychain_service="Claude Code-credentials-test3")
        # Only a transcript under ".../a/home" exists: an unescaped "[a]" would match it.
        w.fs.files[ROOT + "/a/home/projects/-Users-user-proj-repo/" + SID + ".jsonl"] = ""
        w.fs.files[home + "/.claude.json"] = trust(CWD)
        w.fs.dirs.add(home)
        assert_untouched(self, w.run(), "transcript_not_visible", w)

    def test_target_untrusted_false_flag(self):
        w = World()
        w.fs.files[TRUST_FILE] = json.dumps({"projects": {CWD: {"hasTrustDialogAccepted": False}}})
        assert_untouched(self, w.run(), "target_untrusted", w)

    def test_trust_string_true_untrusted(self):
        w = World()
        w.fs.files[TRUST_FILE] = json.dumps({"projects": {CWD: {"hasTrustDialogAccepted": "true"}}})
        assert_untouched(self, w.run(), "target_untrusted", w)

    def test_trust_file_unreadable(self):
        w = World()
        del w.fs.files[TRUST_FILE]
        assert_untouched(self, w.run(), "target_untrusted", w)

    def _subdir_world(self, trusted, *, git=True):
        w = World()
        sub = "/Users/user/proj/repo/sub"
        w.session = dataclasses.replace(w.session, cwd=sub)
        w.fs.dirs.add(sub)
        if git:
            w.fs.files["/Users/user/proj/repo/.git"] = ""
        w.fs.files[TRUST_FILE] = trust(trusted)
        self._shell_registry(w)
        return w

    def test_trust_ancestor_up_to_git_root_passes(self):
        w = self._subdir_world("/Users/user/proj/repo")
        assert_untouched(self, w.run(), "pane_not_claude", w)

    def test_trust_above_git_root_untrusted(self):
        w = self._subdir_world("/Users/user/proj")
        assert_untouched(self, w.run(), "target_untrusted", w)

    def test_trust_parent_without_git_untrusted(self):
        w = self._subdir_world("/Users/user/proj/repo", git=False)
        assert_untouched(self, w.run(), "target_untrusted", w)

    def test_pid_not_alive(self):
        w = World()
        w.procs.alive[OLD_PID] = [False]
        assert_untouched(self, w.run(), "pid_not_alive", w)

    def test_is_alive_raises_fails_closed(self):
        w = World()
        w.procs.alive[OLD_PID] = [RuntimeError("x")]
        assert_untouched(self, w.run(), "pid_not_alive", w)

    def test_binding_tmux_ancestor(self):
        w = World()
        w.procs.tables = [{OLD_PID: (1110, "claude"), 1110: (50, "zsh"), 50: (1, "tmux")}]
        assert_untouched(self, w.run(), "pane_binding_unverified", w)

    def test_binding_without_shell(self):
        w = World()
        w.procs.tables = [{OLD_PID: (HOST, "claude"), HOST: (1, "orca-terminal-host")}]
        assert_untouched(self, w.run(), "pane_binding_unverified", w)

    def test_process_table_oserror(self):
        w = World()
        w.procs.tables = [OSError("ps")]
        assert_untouched(self, w.run(), "pane_binding_unverified", w)
        self.assertNotIn(("read_env", OLD_PID), w.procs.calls)

    def test_process_table_non_oserror_fails_closed(self):
        w = World()
        w.procs.tables = [RuntimeError("x")]
        assert_untouched(self, w.run(), "pane_binding_unverified", w)

    def test_read_env_raises_fails_closed(self):
        w = World()
        w.procs.envs[OLD_PID] = ValueError("x")
        assert_untouched(self, w.run(), "pane_binding_unverified", w)

    def test_env_other_handle(self):
        w = World()
        w.procs.envs[OLD_PID] = ProcEnv(terminal_handle="term_11111111")
        assert_untouched(self, w.run(), "pane_binding_unverified", w)

    def test_registry_unreadable(self):
        w = World()
        w.fs.files[OLD_REG] = None
        assert_untouched(self, w.run(), "pane_not_claude", w)

    def test_registry_not_object(self):
        w = World()
        w.fs.files[OLD_REG] = "[]"
        assert_untouched(self, w.run(), "pane_not_claude", w)

    def test_registry_shell(self):
        w = World()
        self._shell_registry(w)
        assert_untouched(self, w.run(), "pane_not_claude", w)

    def test_registry_unknown_status(self):
        w = World()
        w.fs.files[OLD_REG] = reg(OLD_PID, "compacting")
        assert_untouched(self, w.run(), "pane_not_claude", w)

    def test_registry_pid_mismatch(self):
        w = World()
        w.fs.files[OLD_REG] = reg(999, "idle")
        assert_untouched(self, w.run(), "registry_mismatch", w)

    def test_registry_session_mismatch(self):
        w = World()
        w.fs.files[OLD_REG] = reg(OLD_PID, "idle", sid="other")
        assert_untouched(self, w.run(), "registry_mismatch", w)

    def test_waiting_soft(self):
        w = World("waiting")
        assert_untouched(self, w.run("soft"), "dialog_open", w)

    def test_waiting_hard(self):
        w = World("waiting")
        assert_untouched(self, w.run("hard"), "dialog_open", w)

    def test_soft_busy_not_idle(self):
        w = World("busy")
        assert_untouched(self, w.run("soft"), "not_idle", w)

    def test_binding_before_registry(self):
        w = World("busy")
        w.procs.tables = [{OLD_PID: (1110, "claude"), 1110: (50, "zsh"), 50: (1, "tmux")}]
        assert_untouched(self, w.run("soft"), "pane_binding_unverified", w)

    def test_preflight_info_log(self):
        w = World()
        w.session = dataclasses.replace(w.session, terminal_handle=None)
        with self.assertLogs("rotator.switcher", "INFO") as cm:
            w.run()
        self.assertEqual(
            [r.getMessage() for r in cm.records],
            ["switch step=preflight pid=1111 reason=no_terminal_handle state=untouched"],
        )


# ---------------------------------------------------------------- Task 08

BUSY_REG = reg(OLD_PID, "busy")
TMUX_TABLE = {OLD_PID: (1110, "claude"), 1110: (50, "zsh"), 50: (1, "tmux")}


class TestInterrupt(unittest.TestCase):
    def test_screen_not_empty_before_ctrl_c(self):
        w = World("busy")
        w.driver.screens = [_screen("❯ wip")]
        self.assertEqual(w.run("hard"), result("interrupt", "input_not_empty", "untouched"))
        self.assertEqual(w.driver.calls, [("read_screen", HANDLE)])

    def test_screen_unreadable_before_ctrl_c(self):
        w = World("busy")
        w.driver.screens = [ScreenReceipt(ok=False)]
        self.assertEqual(w.run("hard"), result("interrupt", "screen_unreadable", "untouched"))
        self.assertEqual(w.driver.sends(), [])

    def test_interrupt_refused(self):
        w = World("busy")
        w.driver.script["interrupt"] = [SendReceipt("refused", refused_reason="busy")]
        self.assertEqual(w.run("hard"), result("interrupt", "send_refused", "untouched"))
        self.assertEqual(w.driver.calls, [("read_screen", HANDLE), ("send", HANDLE, None, False, True)])

    def test_interrupt_timeout(self):
        w = World("busy")
        w.fs.files[OLD_REG] = BUSY_REG
        self.assertEqual(w.run("hard"), result("interrupt", "interrupt_timeout", "interrupted"))
        self.assertEqual([c for c in w.driver.sends() if c[4]], [("send", HANDLE, None, False, True)])
        self.assertIs(w.driver.sent("exit"), False)
        self.assertEqual(w.procs.calls.count(("is_alive", OLD_PID)), 1 + 5)
        self.assertEqual(w.clock.now, 5.0)

    def test_interrupt_unconfirmed_counts_as_sent(self):
        w = World("busy")
        w.driver.script["interrupt"] = [SendReceipt("unconfirmed")]
        w.fs.files[OLD_REG] = BUSY_REG
        self.assertEqual(w.run("hard"), result("interrupt", "interrupt_timeout", "interrupted"))

    def test_waiting_mid_poll(self):
        w = World("busy")
        w.fs.files[OLD_REG] = lambda: reg(OLD_PID, "waiting" if w.driver.sent("interrupt") else "busy")
        self.assertEqual(w.run("hard"), result("interrupt", "dialog_open", "interrupted"))
        self.assertIs(w.driver.sent("exit"), False)

    def test_pid_dies_during_poll(self):
        w = World("busy")
        w.fs.files[OLD_REG] = BUSY_REG
        w.procs.alive[OLD_PID] = [True, True, False]
        self.assertEqual(w.run("hard"), result("interrupt", "pid_not_alive", "exited"))
        self.assertEqual(len(w.driver.sends()), 1)

    def test_is_alive_raises_in_poll(self):
        w = World("busy")
        w.procs.alive[OLD_PID] = [True, RuntimeError("x")]
        self.assertEqual(w.run("hard"), result("interrupt", "interrupt_timeout", "interrupted"))
        self.assertIs(w.driver.sent("exit"), False)


class TestPreExit(unittest.TestCase):
    def test_screen_guard_before_exit_soft(self):
        w = World()
        w.driver.screens = [_screen("❯ wip")]
        self.assertEqual(w.run("soft"), result("exit", "input_not_empty", "untouched"))
        self.assertEqual(w.driver.calls, [("read_screen", HANDLE)])

    def test_turn_submitted_between_preflight_and_exit(self):
        w = World()
        w.fs.files[OLD_REG] = Seq(reg(OLD_PID, "idle"), reg(OLD_PID, "busy"))
        self.assertEqual(w.run("soft"), result("exit", "not_idle", "untouched"))
        self.assertIs(w.driver.sent("exit"), False)

    def test_dialog_before_exit(self):
        w = World()
        w.fs.files[OLD_REG] = Seq(reg(OLD_PID, "idle"), reg(OLD_PID, "waiting"))
        self.assertEqual(w.run("soft"), result("exit", "dialog_open", "untouched"))
        self.assertIs(w.driver.sent("exit"), False)

    def test_registry_gone_before_exit(self):
        w = World()
        w.fs.files[OLD_REG] = Seq(reg(OLD_PID, "idle"), None)
        self.assertEqual(w.run("soft"), result("exit", "pane_not_claude", "untouched"))
        self.assertIs(w.driver.sent("exit"), False)

    def test_binding_changes_before_exit(self):
        w = World()
        w.procs.tables = [ORCA_TABLE, TMUX_TABLE]
        self.assertEqual(w.run("soft"), result("exit", "pane_binding_unverified", "untouched"))
        self.assertIs(w.driver.sent("exit"), False)
        self.assertEqual(w.procs.calls.count(("process_table",)), 2)

    def test_restored_prompt_after_interrupt(self):
        w = World("busy")
        w.driver.screens = [EMPTY_SCREEN, _screen("❯ restored prompt")]
        self.assertEqual(w.run("hard"), result("exit", "input_not_empty", "interrupted"))
        self.assertIs(w.driver.sent("exit"), False)

    def test_exit_refused(self):
        w = World()
        w.driver.script["exit"] = [SendReceipt("refused", refused_reason="busy")]
        self.assertEqual(w.run("soft"), result("exit", "send_refused", "untouched"))
        self.assertEqual(len(w.driver.sends()), 1)

    def test_hard_idle_skips_interrupt(self):
        w = World()
        w.driver.screens = [_screen("❯ wip")]
        self.assertEqual(w.run("hard"), result("exit", "input_not_empty", "untouched"))
        self.assertIs(w.driver.sent("interrupt"), False)


class TestWaitExit(unittest.TestCase):
    def test_exit_timeout_is_exit_pending(self):
        w = World()
        w.procs.alive[OLD_PID] = [True]
        self.assertEqual(w.run("soft"), result("wait_exit", "exit_timeout", "exit_pending"))
        self.assertEqual([c for c in w.driver.calls if c[0] == "wait_for"], [("wait_for", HANDLE, "exit", 5000)])
        self.assertIs(w.driver.sent("relaunch"), False)

    def test_unconfirmed_exit_alive_is_exit_pending(self):
        w = World()
        w.driver.script["exit"] = [SendReceipt("unconfirmed")]
        w.procs.alive[OLD_PID] = [True]
        self.assertEqual(w.run("soft"), result("wait_exit", "exit_timeout", "exit_pending"))
        self.assertEqual(len(w.driver.sends()), 1)

    def test_zero_budget_probe_counts(self):
        w = World()
        w.procs.alive[OLD_PID] = [True]
        res = w.run("soft", timeouts=dataclasses.replace(FAST, exit_s=0))
        self.assertEqual(res, result("wait_exit", "exit_timeout", "exit_pending"))
        self.assertEqual(w.procs.calls.count(("is_alive", OLD_PID)), 3)

    def test_five_by_one_probe_counts(self):
        w = World()
        w.procs.alive[OLD_PID] = [True]
        res = w.run("soft", timeouts=dataclasses.replace(FAST, exit_s=5, poll_s=1))
        self.assertEqual(res, result("wait_exit", "exit_timeout", "exit_pending"))
        self.assertEqual(w.procs.calls.count(("is_alive", OLD_PID)), 7)

    def test_is_alive_raising_counts_as_alive(self):
        w = World()
        w.procs.alive[OLD_PID] = [True, RuntimeError("x")]
        self.assertEqual(w.run("soft"), result("wait_exit", "exit_timeout", "exit_pending"))
        self.assertIs(w.driver.sent("relaunch"), False)


# ---------------------------------------------------------------- Task 09


class TestRelaunch(unittest.TestCase):
    def test_session_already_running_elsewhere(self):
        w = World()
        w.fs.files[OTHER_HOME + "/sessions/5555.json"] = reg(5555, "idle")
        w.procs.alive[5555] = [True]
        self.assertEqual(
            w.run(),
            result("relaunch", "session_already_running", "relaunched", new_pid=5555, other_account_id=ACC_OTHER),
        )
        self.assertIs(w.driver.sent("relaunch"), False)

    def test_session_already_running_under_target(self):
        w = World()
        w.fs.files[NEW_HOME + "/sessions/5555.json"] = reg(5555, "idle")
        w.procs.alive[5555] = [True]
        self.assertEqual(w.run(), result("relaunch", "session_already_running", "relaunched", new_pid=5555))
        self.assertIs(w.driver.sent("relaunch"), False)

    def test_already_running_alive_probe_raises(self):
        w = World()
        w.fs.files[OTHER_HOME + "/sessions/5555.json"] = reg(5555, "idle")
        w.procs.alive[5555] = [RuntimeError("x")]
        self.assertEqual(
            w.run(),
            result("relaunch", "session_already_running", "relaunched", new_pid=5555, other_account_id=ACC_OTHER),
        )
        self.assertIs(w.driver.sent("relaunch"), False)

    def test_already_running_two_entries_newest_wins(self):
        w = World()
        w.fs.files[OTHER_HOME + "/sessions/5555.json"] = reg(5555, "idle", started_at=1000)
        w.fs.files[OTHER_HOME + "/sessions/5556.json"] = reg(5556, "idle", started_at=3000)
        w.procs.alive[5555] = [True]
        w.procs.alive[5556] = [True]
        self.assertEqual(
            w.run(),
            result("relaunch", "session_already_running", "relaunched", new_pid=5556, other_account_id=ACC_OTHER),
        )

    def test_dead_same_session_entry_ignored(self):
        w = World()
        w.fs.files[OTHER_HOME + "/sessions/5555.json"] = reg(5555, "idle")
        w.procs.alive[5555] = [False]
        w.fs.files.pop(NEW_REG)
        self.assertEqual(w.run(), result("wait_ready", "ready_timeout", "exited"))
        self.assertEqual([c for c in w.driver.sends() if c[2] == LINE], [("send", HANDLE, LINE, True, False)])

    def test_relaunch_refused(self):
        w = World()
        w.driver.script["relaunch"] = [SendReceipt("refused", refused_reason="busy")]
        self.assertEqual(w.run(), result("relaunch", "send_refused", "exited"))
        self.assertIs(w.driver.sent("prompt"), False)


class TestWaitReady(unittest.TestCase):
    def _new_reg(self, w, status, sid=SID):
        w.fs.files[NEW_REG] = lambda: reg(NEW_PID, status, sid=sid, started_at=2000) if w.driver.sent("relaunch") else None

    def test_waiting_with_trust_screen(self):
        w = World()
        self._new_reg(w, "waiting")
        w.driver.screens = [EMPTY_SCREEN, TRUST_SCREEN]
        self.assertEqual(w.run(), result("wait_ready", "trust_dialog", "relaunched", new_pid=NEW_PID))
        self.assertIs(w.driver.sent("prompt"), False)

    def test_waiting_other_dialog(self):
        w = World()
        self._new_reg(w, "waiting")
        w.driver.screens = [EMPTY_SCREEN, _screen("❯ ", below=("Do you want to proceed?",))]
        self.assertEqual(w.run(), result("wait_ready", "dialog_open", "relaunched", new_pid=NEW_PID))

    def test_shell_status_classified(self):
        w = World()
        self._new_reg(w, "shell")
        w.driver.screens = [EMPTY_SCREEN, EMPTY_SCREEN]
        self.assertEqual(w.run(), result("wait_ready", "dialog_open", "relaunched", new_pid=NEW_PID))

    def test_no_entry_trust_screen_at_deadline(self):
        w = World()
        w.fs.files.pop(NEW_REG)
        w.driver.screens = [EMPTY_SCREEN, TRUST_SCREEN]
        self.assertEqual(w.run(), result("wait_ready", "trust_dialog", "relaunched"))

    def test_launched_elsewhere(self):
        w = World()
        w.fs.files.pop(NEW_REG)
        w.fs.files[OTHER_REG] = lambda: reg(NEW_PID, "idle", started_at=2000) if w.driver.sent("relaunch") else None
        self.assertEqual(
            w.run(),
            result("wait_ready", "launched_elsewhere", "relaunched", new_pid=NEW_PID, other_account_id=ACC_OTHER),
        )

    def test_launched_in_default_home(self):
        w = World()
        w.fs.files.pop(NEW_REG)
        path = os.path.join(os.path.expanduser("~/.claude"), "sessions", "4242.json")
        w.fs.files[path] = lambda: reg(NEW_PID, "idle", started_at=2000) if w.driver.sent("relaunch") else None
        self.assertEqual(
            w.run(),
            result("wait_ready", "launched_elsewhere", "relaunched", new_pid=NEW_PID, other_account_id="default"),
        )

    def test_nothing_ready_timeout_exited(self):
        w = World()
        w.fs.files.pop(NEW_REG)
        self.assertEqual(w.run(), result("wait_ready", "ready_timeout", "exited"))
        self.assertEqual(w.clock.now, 5.0)

    def test_busy_forever_ready_timeout_relaunched(self):
        w = World()
        self._new_reg(w, "busy")
        self.assertEqual(w.run(), result("wait_ready", "ready_timeout", "relaunched", new_pid=NEW_PID))

    def test_entry_ignored_wrong_session(self):
        w = World()
        self._new_reg(w, "idle", sid="other")
        self.assertEqual(w.run(), result("wait_ready", "ready_timeout", "exited"))

    def test_entry_ignored_snapshot_pid(self):
        w = World()
        w.fs.files[NEW_REG] = reg(NEW_PID, "idle")
        w.procs.alive[NEW_PID] = [False, True]
        self.assertEqual(w.run(), result("wait_ready", "ready_timeout", "exited"))
        self.assertEqual(w.procs.calls.count(("is_alive", NEW_PID)), 1)

    def test_entry_ignored_dead_pid(self):
        w = World()
        w.procs.alive[NEW_PID] = [False]
        self.assertEqual(w.run(), result("wait_ready", "ready_timeout", "exited"))

    def test_entry_alive_probe_raises_ignored(self):
        w = World()
        w.procs.alive[NEW_PID] = [RuntimeError("x")]
        self.assertEqual(w.run(), result("wait_ready", "ready_timeout", "exited"))

    def test_key_file_never_read(self):
        w = World()
        w.fs.files[NEW_HOME + "/sessions/4242.abc.key"] = "SECRET"
        w.procs.envs[NEW_PID] = None
        self.assertEqual(w.run(), result("verify_account", "account_unverified", "relaunched", new_pid=NEW_PID))
        self.assertEqual([p for p in w.fs.reads if p.endswith(".key")], [])


class TestVerifyAccount(unittest.TestCase):
    def _assert_stops(self, w, reason):
        self.assertEqual(w.run(), result("verify_account", reason, "relaunched", new_pid=NEW_PID))
        self.assertIs(w.driver.sent("prompt"), False)

    def test_wrong_account(self):
        w = World()
        w.procs.envs[NEW_PID] = ProcEnv(config_dir="/Users/user/elsewhere/home")
        self._assert_stops(w, "wrong_account")

    def test_env_unreadable(self):
        w = World()
        w.procs.envs[NEW_PID] = None
        self._assert_stops(w, "account_unverified")

    def test_env_without_config_dir(self):
        w = World()
        w.procs.envs[NEW_PID] = ProcEnv()
        self._assert_stops(w, "account_unverified")

    def test_read_env_raises(self):
        w = World()
        w.procs.envs[NEW_PID] = ValueError("x")
        self._assert_stops(w, "account_unverified")


# ---------------------------------------------------------------- Task 10

HAPPY = SwitchResult(
    ok=True,
    step="continue_prompt",
    reason=None,
    state="continued",
    new_pid=NEW_PID,
    relaunch_command=LINE,
    recoverable=False,
    request_id="req-1",
    other_account_id=None,
)
P = DEFAULT_CONTINUE_PROMPT
SOFT_LOG = [
    ("read_screen", HANDLE),
    ("send", HANDLE, "/exit", True, False),
    ("wait_for", HANDLE, "exit", 5000),
    ("send", HANDLE, LINE, True, False),
    ("read_screen", HANDLE),
    ("send", HANDLE, P, True, False),
]
PROCS_LOG = [("process_table",), ("read_env", OLD_PID), ("process_table",), ("read_env", OLD_PID), ("read_env", NEW_PID)]


def procs_log(w):
    return [c for c in w.procs.calls if c[0] != "is_alive"]


class TestHappyPath(unittest.TestCase):
    def test_soft(self):
        w = World()
        self.assertEqual(w.run("soft"), HAPPY)
        self.assertEqual(w.driver.calls, SOFT_LOG)
        self.assertEqual(procs_log(w), PROCS_LOG)

    def test_hard_idle(self):
        w = World()
        self.assertEqual(w.run("hard"), HAPPY)
        self.assertEqual(w.driver.calls, SOFT_LOG)
        self.assertEqual(procs_log(w), PROCS_LOG)

    def test_hard_busy(self):
        w = World("busy")
        self.assertEqual(w.run("hard"), HAPPY)
        self.assertEqual(w.driver.calls, [("read_screen", HANDLE), ("send", HANDLE, None, False, True)] + SOFT_LOG)
        self.assertEqual(procs_log(w), PROCS_LOG)

    def test_default_timeouts_wait_for_5000(self):
        w = World()
        self.assertEqual(w.run("soft", timeouts=Timeouts()), HAPPY)
        self.assertIn(("wait_for", HANDLE, "exit", 5000), w.driver.calls)

    def test_prompt_800_chars(self):
        w = World()
        res = w.run("soft", continue_prompt="가" * 800)
        self.assertEqual(res, HAPPY)
        self.assertEqual(w.driver.sends()[-1][2], "가" * 800)

    def test_state_hint_for_success_empty(self):
        self.assertEqual(STATE_HINTS[HAPPY.state], "")


class TestProceedPaths(unittest.TestCase):
    def test_exit_unconfirmed_pid_dies(self):
        w = World()
        w.driver.script["exit"] = [SendReceipt("unconfirmed")]
        self.assertEqual(w.run(), HAPPY)
        self.assertEqual(w.driver.calls, SOFT_LOG)

    def test_interrupt_unconfirmed_then_idle(self):
        w = World("busy")
        w.driver.script["interrupt"] = [SendReceipt("unconfirmed")]
        self.assertEqual(w.run("hard"), HAPPY)
        self.assertEqual(len([c for c in w.driver.sends() if c[4]]), 1)

    def test_pid_dies_on_third_probe(self):
        w = World()
        w.procs.alive[OLD_PID] = [True, True, True, False]
        self.assertEqual(w.run(), HAPPY)
        self.assertEqual(w.procs.calls.count(("is_alive", OLD_PID)), 4)

    def test_late_death_on_recheck(self):
        w = World()
        w.procs.alive[OLD_PID] = [True, True, False]
        self.assertEqual(w.run(timeouts=dataclasses.replace(FAST, exit_s=0)), HAPPY)
        self.assertEqual(len([c for c in w.driver.calls if c[0] == "wait_for"]), 1)

    def test_settle_unsatisfied_still_proceeds(self):
        w = World()
        w.driver.waits = [WaitReceipt(False)]
        self.assertEqual(w.run(), HAPPY)

    def test_relaunch_unconfirmed_still_polls(self):
        w = World()
        w.driver.script["relaunch"] = [SendReceipt("unconfirmed")]
        self.assertEqual(w.run(), HAPPY)

    def test_dead_same_session_entry_ignored(self):
        w = World()
        w.fs.files[ROOT + "/" + "00000000-0000-0000-0000-000000000003/home/sessions/5555.json"] = reg(5555, "idle")
        w.procs.alive[5555] = [False]
        self.assertEqual(w.run(), HAPPY)
        self.assertEqual(len([c for c in w.driver.sends() if c[2] == LINE]), 1)

    def test_busy_then_idle_ready(self):
        w = World()
        w.fs.files[NEW_REG] = lambda: (
            reg(NEW_PID, "idle" if w.clock.now >= 2.0 else "busy", started_at=2000) if w.driver.sent("relaunch") else None
        )
        self.assertEqual(w.run(), HAPPY)
        self.assertEqual(w.clock.now, 2.0)

    def test_config_dir_trailing_slash_verified(self):
        w = World()
        w.procs.envs[NEW_PID] = ProcEnv(config_dir=NEW_HOME + "/", terminal_handle=HANDLE)
        self.assertEqual(w.run(), HAPPY)


class TestContinuePrompt(unittest.TestCase):
    def _fail(self, reason, request_id=None):
        return result("continue_prompt", reason, "verified", new_pid=NEW_PID, request_id=request_id)

    def test_prompt_unconfirmed_then_busy_succeeds(self):
        w = World()
        w.driver.script["prompt"] = [SendReceipt("unconfirmed")]
        self.assertEqual(w.run(), dataclasses.replace(HAPPY, request_id=None))

    def test_unconfirmed_with_turn_started_still_polls_registry(self):
        w = World()
        w.driver.script["prompt"] = [
            SendReceipt("unconfirmed", stages=("input_accepted", "turn_started"), request_id="r7")
        ]
        w.fs.files[NEW_REG] = lambda: reg(NEW_PID, "idle", started_at=2000) if w.driver.sent("relaunch") else None
        self.assertEqual(w.run(), self._fail("prompt_unconfirmed", "r7"))
        self.assertEqual(w.clock.now, 5.0)

    def test_unconfirmed_with_turn_started_busy_succeeds(self):
        w = World()
        w.driver.script["prompt"] = [
            SendReceipt("unconfirmed", stages=("input_accepted", "turn_started"), request_id="r7")
        ]
        self.assertEqual(w.run(), dataclasses.replace(HAPPY, request_id="r7"))

    def test_accepted_without_turn_start_registry_busy(self):
        w = World()
        w.driver.script["prompt"] = [SendReceipt("accepted", stages=("input_accepted",), request_id="r9")]
        self.assertEqual(w.run(), dataclasses.replace(HAPPY, request_id="r9"))

    def test_accepted_without_turn_start_stays_idle(self):
        w = World()
        w.driver.script["prompt"] = [SendReceipt("accepted", stages=("input_accepted",), request_id="r9")]
        w.fs.files[NEW_REG] = lambda: reg(NEW_PID, "idle", started_at=2000) if w.driver.sent("relaunch") else None
        self.assertEqual(w.run(), self._fail("prompt_unconfirmed", "r9"))
        self.assertEqual(len([c for c in w.driver.sends() if c[2] == P]), 1)

    def test_permission_observation(self):
        w = World()
        w.driver.script["prompt"] = [
            SendReceipt("accepted", stages=("input_accepted",), observation="permission", request_id="r2")
        ]
        self.assertEqual(w.run(), self._fail("prompt_blocked_permission", "r2"))

    def test_incarnation_replaced(self):
        w = World()
        w.driver.script["prompt"] = [
            SendReceipt(
                "accepted",
                stages=("input_accepted", "turn_started"),
                observation="incarnation_replaced",
                request_id="r3",
            )
        ]
        self.assertEqual(w.run(), self._fail("prompt_unconfirmed", "r3"))

    def test_prompt_refused(self):
        w = World()
        w.driver.script["prompt"] = [SendReceipt("refused", refused_reason="busy")]
        self.assertEqual(w.run(), self._fail("send_refused"))

    def test_screen_guard_before_prompt(self):
        w = World()
        w.driver.screens = [EMPTY_SCREEN, _screen("❯ wip")]
        self.assertEqual(w.run(), self._fail("input_not_empty"))
        self.assertIs(w.driver.sent("prompt"), False)

    def test_dialog_before_prompt(self):
        w = World()
        w.driver.screens = [EMPTY_SCREEN, _screen("❯ ", below=("Do you want to proceed?",))]
        self.assertEqual(w.run(), self._fail("dialog_open"))
        self.assertIs(w.driver.sent("prompt"), False)


class TestLogging(unittest.TestCase):
    TAIL = [
        "switch step=wait_exit pid=1111 reason=ok state=exited",
        "switch step=relaunch pid=1111 reason=ok state=exited",
        "switch step=wait_ready pid=1111 reason=ok state=relaunched",
        "switch step=verify_account pid=1111 reason=ok state=verified",
        "switch step=continue_prompt pid=1111 reason=ok state=continued",
    ]

    def _info(self, w, mode):
        with self.assertLogs("rotator.switcher", "INFO") as cm:
            w.run(mode)
        return [r.getMessage() for r in cm.records]

    def test_info_lines_soft(self):
        self.assertEqual(
            self._info(World(), "soft"),
            [
                "switch step=preflight pid=1111 reason=ok state=untouched",
                "switch step=exit pid=1111 reason=ok state=untouched",
            ]
            + self.TAIL,
        )

    def test_info_lines_hard_busy(self):
        self.assertEqual(
            self._info(World("busy"), "hard"),
            [
                "switch step=preflight pid=1111 reason=ok state=untouched",
                "switch step=interrupt pid=1111 reason=ok state=interrupted",
                "switch step=exit pid=1111 reason=ok state=interrupted",
            ]
            + self.TAIL,
        )

    def test_no_secret_in_any_record(self):
        w = World("busy")
        w.driver.screens = [_screen("❯ ", above=("● SCREEN-SENTINEL answer",))]
        w.procs.envs[OLD_PID] = ProcEnv(config_dir=OLD_HOME, terminal_handle=HANDLE)
        with self.assertLogs("rotator.switcher", "DEBUG") as cm:
            res = w.run("hard", continue_prompt="PROMPT-SENTINEL 계속")
        self.assertIs(res.ok, True)
        self.assertGreater(len(cm.records), 0)
        for record in cm.records:
            m = record.getMessage()
            for secret in (
                "SCREEN-SENTINEL",
                "PROMPT-SENTINEL",
                NEW_HOME,
                OLD_HOME,
                "/Users/user",
                "token",
                "orca-terminal-host",
                "CLAUDE_CONFIG_DIR",
            ):
                self.assertNotIn(secret, m)
            if record.levelno >= 20:
                self.assertNotIn("term_", m)

    def test_no_secret_in_failure_records(self):
        seam = World()
        seam.procs.alive[OLD_PID] = [OSError("/Users/user/secret-path")]
        binding = World()
        binding.procs.tables = [ORCA_TABLE, TMUX_TABLE]
        messages = []
        for w in (seam, binding):
            with self.assertLogs("rotator.switcher", "DEBUG") as cm:
                w.run("soft")
            messages += [r.getMessage() for r in cm.records]
        self.assertIn("switch seam error=OSError", messages)
        self.assertIn("switch binding fault=multiplexer", messages)
        for m in messages:
            self.assertNotIn("/Users/user", m)
            self.assertNotIn("tmux", m)

    def test_result_repr_hides_paths(self):
        res = World().run()
        self.assertEqual(res, HAPPY)
        self.assertNotIn("/Users/user", repr(res))
        self.assertNotIn(NEW_HOME, repr(ProcEnv(config_dir=NEW_HOME)))


def _code(path):
    with open(path, encoding="utf-8") as f:
        src = f.read()
    lines = src.splitlines(keepends=True)
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type == tokenize.COMMENT:
            (row, col), (_, end) = tok.start, tok.end
            line = lines[row - 1]
            lines[row - 1] = line[:col] + " " * (end - col) + line[end:]
    return "".join(lines)


PKG = os.path.dirname(os.path.abspath(sw.__file__))
SWITCHER_SRC = os.path.join(PKG, "switcher.py")
POINTER_SRC = os.path.join(PKG, "pointer.py")
SRC_TMP = os.path.join(REPO, ".claude", "tmp", "test-switcher-src")


class TestSourceText(unittest.TestCase):
    def test_switcher_one_subprocess_run(self):
        code = _code(SWITCHER_SRC)
        self.assertEqual(code.count("import subprocess"), 1)
        self.assertEqual(code.count("subprocess.run("), 1)

    def test_switcher_no_shell_true(self):
        self.assertNotIn("shell=True", _code(SWITCHER_SRC))

    def test_switcher_no_os_kill_call(self):
        self.assertNotIn("os.kill(", _code(SWITCHER_SRC))

    def test_no_ctypes(self):
        self.assertNotIn("ctypes", _code(SWITCHER_SRC))
        self.assertNotIn("ctypes", _code(POINTER_SRC))

    def test_pointer_no_subprocess(self):
        self.assertNotIn("subprocess", _code(POINTER_SRC))

    def test_comment_is_ignored(self):
        os.makedirs(SRC_TMP, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=SRC_TMP) as d:
            path = os.path.join(d, "sample.py")
            with open(path, "w", encoding="utf-8") as f:
                f.write("x = 1  # shell=True\ny = 'shell=True'\n")
            code = _code(path)
        self.assertNotIn("# shell=True", code)
        self.assertEqual(code.count("shell=True"), 1)
        self.assertIn("y = 'shell=True'", code)


# ---------------------------------------------------------------- rework: no seam exception escapes


def _read_text_raising_for(w, path, exc):
    orig = w.fs.read_text

    def read_text(p):
        if p == path:
            w.fs.reads.append(p)
            raise exc
        return orig(p)

    return read_text


class TestSeamFailures(unittest.TestCase):
    def test_read_screen_raises_before_exit(self):
        w = World()
        w.driver.screens = [RuntimeError("x")]
        self.assertEqual(w.run(), result("exit", "screen_unreadable", "untouched"))
        self.assertEqual(w.driver.sends(), [])

    def test_read_screen_raises_before_ctrl_c(self):
        w = World("busy")
        w.driver.screens = [RuntimeError("x")]
        self.assertEqual(w.run("hard"), result("interrupt", "screen_unreadable", "untouched"))
        self.assertEqual(w.driver.sends(), [])

    def test_read_screen_returns_junk(self):
        w = World()
        w.driver.screens = [None]
        self.assertEqual(w.run(), result("exit", "screen_unreadable", "untouched"))

    def test_read_screen_raises_before_prompt(self):
        w = World()
        w.driver.screens = [EMPTY_SCREEN, RuntimeError("x")]
        self.assertEqual(w.run(), result("continue_prompt", "screen_unreadable", "verified", new_pid=NEW_PID))
        self.assertIs(w.driver.sent("prompt"), False)

    def test_interrupt_send_raises_reads_as_unconfirmed(self):
        w = World("busy")
        w.driver.script["interrupt"] = [RuntimeError("x")]
        self.assertEqual(w.run("hard"), HAPPY)

    def test_interrupt_send_raises_turn_continues_is_interrupted(self):
        w = World("busy")
        w.driver.script["interrupt"] = [RuntimeError("x")]
        w.fs.files[OLD_REG] = BUSY_REG
        self.assertEqual(w.run("hard"), result("interrupt", "interrupt_timeout", "interrupted"))

    def test_exit_send_raises_pid_dies_proceeds(self):
        w = World()
        w.driver.script["exit"] = [RuntimeError("x")]
        self.assertEqual(w.run(), HAPPY)
        self.assertEqual(w.driver.calls, SOFT_LOG)

    def test_exit_send_raises_pid_alive_is_exit_pending(self):
        w = World()
        w.driver.script["exit"] = [RuntimeError("x")]
        w.procs.alive[OLD_PID] = [True]
        self.assertEqual(w.run(), result("wait_exit", "exit_timeout", "exit_pending"))
        self.assertIs(w.driver.sent("relaunch"), False)

    def test_exit_send_returns_junk_reads_as_unconfirmed(self):
        w = World()
        w.driver.script["exit"] = [None]
        w.procs.alive[OLD_PID] = [True]
        self.assertEqual(w.run(), result("wait_exit", "exit_timeout", "exit_pending"))

    def test_wait_for_raises_never_gates(self):
        w = World()
        w.driver.waits = [RuntimeError("x")]
        self.assertEqual(w.run(), HAPPY)

    def test_relaunch_send_raises_nothing_ready(self):
        w = World()
        w.driver.script["relaunch"] = [RuntimeError("x")]
        w.fs.files.pop(NEW_REG)
        self.assertEqual(w.run(), result("wait_ready", "ready_timeout", "exited"))
        self.assertEqual(len([c for c in w.driver.sends() if c[2] == LINE]), 1)

    def test_prompt_send_raises_after_point_of_no_return(self):
        w = World()
        w.driver.script["prompt"] = [ValueError("embedded null byte")]
        w.fs.files[NEW_REG] = lambda: reg(NEW_PID, "idle", started_at=2000) if w.driver.sent("relaunch") else None
        self.assertEqual(w.run(), result("continue_prompt", "prompt_unconfirmed", "verified", new_pid=NEW_PID))
        self.assertEqual(len([c for c in w.driver.sends() if c[2] == P]), 1)

    def test_prompt_send_raises_registry_busy_proves_delivery(self):
        w = World()
        w.driver.script["prompt"] = [RuntimeError("x")]
        self.assertEqual(w.run(), dataclasses.replace(HAPPY, request_id=None))

    def test_read_text_non_oserror_at_preflight(self):
        w = World()
        res = w.run(read_text=_read_text_raising_for(w, OLD_REG, RuntimeError("x")))
        assert_untouched(self, res, "pane_not_claude", w)

    def test_read_text_non_oserror_before_exit(self):
        w = World()
        calls = []
        orig = w.fs.read_text

        def read_text(p):
            if p == OLD_REG:
                calls.append(p)
                if len(calls) > 1:
                    raise RuntimeError("x")
            return orig(p)

        self.assertEqual(w.run(read_text=read_text), result("exit", "pane_not_claude", "untouched"))
        self.assertIs(w.driver.sent("exit"), False)

    def test_read_text_returns_non_str(self):
        w = World()
        orig = w.fs.read_text
        res = w.run(read_text=lambda p: 5 if p == TRUST_FILE else orig(p))
        assert_untouched(self, res, "target_untrusted", w)

    def test_registry_nested_too_deep(self):
        w = World()
        w.fs.files[OLD_REG] = "[" * 100000 + "]" * 100000
        assert_untouched(self, w.run(), "pane_not_claude", w)

    def test_glob_raises_at_preflight(self):
        w = World()

        def glob_paths(pattern):
            raise RuntimeError("x")

        assert_untouched(self, w.run(glob_paths=glob_paths), "transcript_not_visible", w)

    def test_glob_raises_during_readiness(self):
        w = World()

        def glob_paths(pattern):
            if w.driver.sent("relaunch"):
                raise RuntimeError("x")
            return w.fs.glob_paths(pattern)

        self.assertEqual(w.run(glob_paths=glob_paths), result("wait_ready", "ready_timeout", "exited"))

    def test_isdir_raises(self):
        w = World()

        def isdir(path):
            raise RuntimeError("x")

        assert_untouched(self, w.run(isdir=isdir), "target_home_missing", w)

    def test_isdir_truthy_non_bool_is_not_a_dir(self):
        w = World()
        assert_untouched(self, w.run(isdir=lambda p: 1), "target_home_missing", w)

    def test_process_table_returns_none(self):
        w = World()
        w.procs.tables = [None]
        assert_untouched(self, w.run(), "pane_binding_unverified", w)

    def test_process_table_malformed_rows(self):
        w = World()
        w.procs.tables = [{OLD_PID: "garbage"}]
        assert_untouched(self, w.run(), "pane_binding_unverified", w)

    def test_read_env_junk_for_new_pid(self):
        w = World()
        w.procs.envs[NEW_PID] = "CLAUDE_CONFIG_DIR=" + NEW_HOME
        self.assertEqual(w.run(), result("verify_account", "account_unverified", "relaunched", new_pid=NEW_PID))
        self.assertIs(w.driver.sent("prompt"), False)

    def test_read_env_non_str_config_dir(self):
        w = World()
        w.procs.envs[NEW_PID] = ProcEnv(config_dir=5)
        self.assertEqual(w.run(), result("verify_account", "account_unverified", "relaunched", new_pid=NEW_PID))
        self.assertIs(w.driver.sent("prompt"), False)

    def test_prompt_receipt_malformed_stages_polls_registry(self):
        w = World()
        w.driver.script["prompt"] = [SendReceipt("accepted", stages=None, request_id="r8")]
        w.fs.files[NEW_REG] = lambda: reg(NEW_PID, "idle", started_at=2000) if w.driver.sent("relaunch") else None
        self.assertEqual(w.run(), result("continue_prompt", "prompt_unconfirmed", "verified", new_pid=NEW_PID, request_id="r8"))

    def test_seam_error_logs_class_name_only(self):
        w = World()
        w.driver.script["prompt"] = [ValueError("/Users/user/secret embedded null byte")]
        with self.assertLogs("rotator.switcher", "DEBUG") as cm:
            w.run()
        messages = [r.getMessage() for r in cm.records]
        self.assertIn("switch seam error=ValueError", messages)
        for m in messages:
            self.assertNotIn("/Users/user", m)
            self.assertNotIn("embedded", m)
