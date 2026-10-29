# claude-account-rotator

A macOS background program for Claude Code sessions that run in Orca terminal panes on Orca's folder-based
accounts. It reads each account's 5-hour and 7-day usage, and before a session hits the rate limit it restarts
that session on a less-used account (`/exit`, then `claude --resume` with the other account's folder): a soft
switch at `soft_pct` once the session is idle, a hard switch at `hard_pct` even while it works. It also keeps
Orca's global `selected-host` pointer on a usable account.

## Requirements

- macOS.
- Orca with folder-based Claude accounts under `~/Library/Application Support/orca/claude-profiles`.
- `/usr/bin/python3` 3.9 or newer.
- Standard library only; nothing to install with pip.

## Install / uninstall

```sh
bin/rotator install      # writes the launchd user agent and starts it
bin/rotator uninstall    # stops it and removes the plist
```

- `install` writes `~/Library/LaunchAgents/io.github.choiyounggi.claude-account-rotator.plist` and runs
  `launchctl bootstrap gui/<uid>` on it. Running it again replaces a loaded agent (`bootout`, then `bootstrap`).
- `uninstall` runs `launchctl bootout gui/<uid>/io.github.choiyounggi.claude-account-rotator` (the exact label,
  never a pattern) and removes the plist.
- The agent runs `/usr/bin/python3 <repo>/bin/rotator --config <abs path> daemon` with `RunAtLoad` and
  `KeepAlive`. Its `ExitTimeOut` is computed from `timeouts` at install time (440 s with the defaults: one full
  switch plus margin), so re-run `rotator install` after changing `timeouts`.
- Run `rotator uninstall` before running `rotator daemon` in the foreground. Otherwise the second copy finds the
  daemon lock held and exits 1, and with `KeepAlive` launchd restarts the agent copy every 10 s
  (`ThrottleInterval` default, `man launchd.plist`).

## Usage

```sh
rotator status [--ascii]             # one table
rotator status --watch [--interval S] [--ascii]
rotator status --json
rotator daemon [--dry-run]           # foreground
rotator switch --session <pid> --to <account-id> [--soft|--hard] [--dry-run]
```

- `status` draws one table of accounts and one of sessions (synthetic example):

```text
Claude 계정 사용량                                                                       21:00:00  데몬: 꺼짐
┌───┬───────┬───────┬──────────────────────────┬────────────────────┬───────────┬──────┬────────────────────┐
│   │ 계정  │ 플랜  │ 5시간                    │ 주간               │ 페이블    │ 세션 │ 갱신               │
├───┼───────┼───────┼──────────────────────────┼────────────────────┼───────────┼──────┼────────────────────┤
│ * │ alice │ Max5x │ ████████▒  89% ↻1h21m    │ ████▒▒▒▒▒  45% ↻6d │ █▒▒▒  25% │    1 │ 3초 전             │
│   │ bob   │ Team  │ █████████ 100% ↻21m      │ ███▒▒▒▒▒▒  37% ↻6d │ -         │    0 │ 3초 전             │
│   │ carol │ -     │ (캐시)      0%           │ (캐시)     28% ↻2d │ -         │    0 │ 3일 전 (호출 제한) │
│   │ dave  │ -     │ -  (토큰 만료·캐시 없음) │ -                  │ -         │    0 │ -                  │
└───┴───────┴───────┴──────────────────────────┴────────────────────┴───────────┴──────┴────────────────────┘
* = Orca 선택 계정   노랑 ≥85%  빨강 ≥95%

┌──────┬───────────┬──────┬─────────────────┬──────────────────────┐
│ PID  │ 계정      │ 상태 │ 모델            │ 최근 전환 결과       │
├──────┼───────────┼──────┼─────────────────┼──────────────────────┤
│ 4101 │ alice     │ busy │ claude-opus-5-5 │ decision soft -> bob │
│ 4202 │ (관리 밖) │ idle │ -               │ -                    │
└──────┴───────────┴──────┴─────────────────┴──────────────────────┘
q 또는 Ctrl-C 종료
```

- Accounts are named by their label: the local part of the account's email (before `@`), else its display name,
  else the first 8 characters of the account folder id, cut with `…` at 18 columns. `*` marks the account
  `selected-host` points to.
- 플랜 is the seat tier Orca stored for the profile (`rateLimitTier`): `Max5x`, `Max20x`, `Team` (`default_raven`),
  any other tier without its `default_` prefix cut to 8 characters, `-` when unknown. The same email logged in
  under two profiles shows as two rows told apart by 플랜; rows whose label and plan are both equal get ` #` and
  the first 4 characters of their account folder id. Rows are sorted by label, then plan, then folder id.
- Each usage cell is a 9-cell bar (4 for 페이블), the percent rounded down, and `↻` with the time left until the
  window resets (`21m`, `1h21m`, `6d`). A window already past its reset shows 0 % and no `↻`. `(캐시)` replaces
  the bar when the numbers come from Claude Code's cached copy; an `unavailable` account shows `-` and the reason
  (토큰 만료, 토큰 없음, 요청 실패, 호출 제한, 연결 실패, 캐시 없음, …). 세션 counts the account's live sessions,
  갱신 is how long ago its usage was fetched.
- Colours (only when stdout is a terminal and `NO_COLOR` is unset or empty): yellow at `soft_pct`, red at
  `hard_pct`, cache rows dimmed. Without colour the layout is the same.
- Every line is measured in terminal columns (Hangul counts 2). When the table does not fit the terminal it drops
  the bars, then the 플랜 column (accounts sharing a label then show their plan after it), then 페이블, then 갱신,
  then narrows 계정; rows are never wrapped. Hint lines below the sessions table wrap instead of being cut. A
  relaunch command gets a line of its own, printed whole and never wrapped by `status` (the terminal soft-wraps it),
  so copying the line gives the exact command.
- Box glyphs and `█▒` are "ambiguous width": terminals set to draw them double width (common in Korean setups)
  would break the borders. On a terminal `status` asks the terminal once (cursor-position report, 0.2 s) and
  measures them accordingly; `--ascii` draws with `+ - |` and `# .` instead and fits any terminal.
- Sessions table: unmanaged sessions (their `CLAUDE_CONFIG_DIR` is not an Orca account folder: shown as
  `(관리 밖)`, never switched), per-session decision and last switch result; under it each session's hint and
  relaunch line, then an attention list (주의) of recent touched switches whose process is gone.
- `status --watch` uses the terminal's alternate screen with the cursor hidden and restores both on exit, also
  after an error or SIGTERM. `q` or Ctrl-C quits (`q` needs stdin to be a terminal; otherwise Ctrl-C only).
- `switch` is a manual one-shot switch with the same checks and the same per-session lock as the daemon.
  `--dry-run` prints the policy verdict and the relaunch line and sends nothing. It ignores `auto_switch` and
  honours `dry_run`.
- Exit codes: `0` ok; `1` runtime failure, switch not ok, daemon already running, session busy or already
  touched; `2` usage or config error, unknown or unmanaged pid or account, `profiles_root` missing.

### Live usage cadence

- Usage is fetched per account at most once per `usage_poll_s` (default 30 s, minimum 15 s): by the daemon when
  it runs, otherwise by `status --watch` itself with the same per-account cache, never once per redraw.
- `status --watch` redraws every `--interval` seconds (default 1). It re-discovers sessions and re-reads the
  daemon snapshot, records and `selected-host` only every `session_poll_s` (default 10 s); the redraws in
  between re-render the last view with the current time, so the 갱신 age (`N초 전`, i.e. N seconds ago, since
  that account's usage was fetched) and the `↻1h12m` reset countdowns still tick every second.
- An account answered with HTTP 429 waits for the server's `Retry-After` when the answer carries one (seconds or
  an HTTP-date, RFC 9110 §10.2.3): next fetch after `max(usage_poll_s, min(900 s, Retry-After))`. Without the
  header it is fetched again only after twice its last interval, doubling up to 300 s.
  It is shown as `(캐시)` with `(호출 제한)` after the age (rate_limited), or `-  (호출 제한·캐시 없음)` when Claude
  Code has no cached usage; the age then shows how old the cached numbers are. Any other answer returns it to `usage_poll_s`.

## Fable usage

- The usage response's `limits[]` entry with `kind` `weekly_scoped` and model display name `Fable` is shown as the
  account's `fable` window (percent + reset time), from the live answer or Claude Code's cached copy. An account
  whose data has no such entry has `fable` unknown.
- A session is a Fable session when its `model` contains `fable` (any case). For a Fable session both the trigger
  (85 % soft / 95 % hard) and the target choice use `max(5h, 7d, fable)` (a window past its reset time counts 0),
  and an account with unknown Fable data is never chosen as its target (`fable unknown` in the reason). Every other
  session, and the `selected-host` pointer correction, still use `max(5h, 7d)` only.
- Where `model` comes from: the last assistant entry (ignoring Claude Code's `<synthetic>` error turns and subagent
  entries marked `isSidechain`) within the last 1 MiB of the session's transcript
  `<config dir>/projects/<cwd with every non-letter/digit as ->/<session id>.jsonl` (if that file is missing, the
  single matching `projects/*/<session id>.jsonl`; only a regular file is read); when the transcript has no model
  yet, the `--model` argument the session was started with, minus a trailing alias suffix such as `[1m]`. Only the
  model id (letters, digits, `._:@-`, at most 64 characters) is kept; the rest of the tail it reads is discarded, never
  stored or logged. This transcript layout is observed Claude Code behaviour (2.1.x), not a documented
  interface: if it changes, `model` becomes unknown and the session is treated as non-Fable.
- `rotator status` shows the `fable` window in the 페이블 column and each session's model id in the 모델 column.

## Status JSON (schema 1)

`rotator status --json` prints one object:

| key | meaning |
|-----|---------|
| `schema` | `1` |
| `generated_at` | when the document was built |
| `daemon` | `{running, pid, snapshot_age_s, flags}` from the daemon's last snapshot |
| `accounts` | one entry per account: `account_id, label, plan, source, error, fetched_at, five_hour, seven_day, fable` (each window `{utilization, resets_at}` or null), `pointer`, `sessions` (`[{pid, status, model}]`) |
| `pointer_account_id` | the account `selected-host` names, or null |
| `sessions` | managed live sessions: `pid, session_id, status, account_id, model, decision, would_switch, in_flight, in_flight_pid, stuck, unswitchable, hard_hold_until, excluded, record_state, last_result, hint, post_verify` |
| `unmanaged_sessions` | `[{pid, session_id, status, model}]` |
| `attention` | `[{pid, session_id, updated_at, record_state, last_result, hint}]`: touched, not-ok switches of gone processes from the last 24 h |

Times are ISO-8601 UTC strings. `relaunch_command` appears only inside `last_result`. `label` is the short account name shown in
the table (email local part, else display name, else folder id prefix) and `plan` its seat tier tag (or null);
no full email address, account UUID,
organization, token or environment value is included. The only argv or transcript value included is a session's
model id (`model`, from the transcript or its `--model` argument, validated as above). Changes within schema 1 are
additive only.
`model` is the session's model id or null.

## Configuration

One JSON object at `~/Library/Application Support/claude-account-rotator/config.json` (or `--config PATH`). An
absent file or key means the default; unknown keys are rejected; an invalid value exits 2 with a message that
starts with the key.

| key | default | constraint |
|-----|---------|------------|
| `soft_pct` | `85` | number 0-100, <= `hard_pct` |
| `hard_pct` | `95` | number 0-100, >= `soft_pct` |
| `target_headroom_pct` | `10` | number 0-100, <= `soft_pct`; a target must be at or below `soft_pct - target_headroom_pct` |
| `stale_max_age_s` | `900` | number >= 0; older usage is not trusted for a target |
| `cooldown_s` | `300` | number >= 0; minimum time between switches of one session, and the hard-mode hold after an aborted hard switch |
| `clock_skew_tolerance_s` | `300` | number >= 0 |
| `session_poll_s` | `10` | number > 0; daemon tick interval |
| `usage_poll_s` | `30` | number >= 15 and < `stale_max_age_s` |
| `timeouts` | see below | object with only the keys below |
| `timeouts.idle_s` | `30` | number >= 0 |
| `timeouts.exit_s` | `30` | number >= 0 |
| `timeouts.shell_s` | `5` | number >= 0 |
| `timeouts.ready_s` | `60` | number >= 0 |
| `timeouts.prompt_s` | `15` | number >= 0 |
| `timeouts.poll_s` | `0.5` | number > 0 |
| `continue_prompt` | `계정 전환으로 세션이 재시작됐어. 중단된 작업을 이어서 진행해줘.` | one non-empty line of at most 800 characters, no control characters, not starting with `-` or `/` |
| `dry_run` | `false` | `true` / `false` |
| `auto_switch` | `true` | `true` / `false` |
| `pointer_correction` | `true` | `true` / `false` |
| `profiles_root` | `~/Library/Application Support/orca/claude-profiles` | absolute path (`~` expanded) |

Numbers must be finite and not `true`/`false`.

## Safety model

- Every keystroke goes through `switcher.switch_session`, whose preflight fails closed: nothing is typed unless
  the pane, process, account and screen checks all pass.
- `relaunch_command` is shown in the status, the switch record and the log; the rotator never types it
  (`rotator.daemon` never calls a terminal driver method itself).
- A session whose switch touched the pane (any result state other than `untouched`) is never switched again
  until it reappears as a new process (`daemon.touched`, checked by the tick and again inside `run_switch`).
- A switch whose outcome is unknown (an intent record left by a crash, SIGKILL or OOM) is never re-sent and is
  shown as "outcome unknown": the intent record is written before every `switch_session` call.
- One switch at a time per session and per pane, across the daemon and the CLI (`flock` on
  `locks/s-*.lock` and `locks/h-*.lock`).
- A session's account is read only from its own process environment (`CLAUDE_CONFIG_DIR`), never from
  `selected-host` or the UI.
- `dry_run: true` / `--dry-run` and `auto_switch: false` never switch and never write `selected-host`.
- Usage is fetched with at most one request per account per interval; a 429 backs off.

## Files

- State dir `~/Library/Application Support/claude-account-rotator/`: `config.json` (the default config),
  `state.json` (daemon snapshot), `daemon.lock` (single instance, holds the daemon pid), `locks/` and
  `sessions/`. The state dir is fixed: `--config PATH` changes only which config file is read, so every daemon
  and `rotator switch` process uses the same locks and records whatever config it was started with.
- `locks/` holds one empty file per switched session or pane; `sessions/` holds one small JSON record per
  switched session identity. The rotator never prunes either; delete them only while no rotator process runs.
- Log dir `~/Library/Logs/claude-account-rotator/`: `rotator.log` (rotating, 1 MiB x 3), `launchd.out.log`,
  `launchd.err.log`.

## Known residual risks

1. Orca gives no tty binding: a process that re-exports a pane's `ORCA_TERMINAL_HANDLE` by hand outside the pane
   passes the pane-binding check.
2. The Orca process names are pinned to Orca 1.4.241 (`orca-terminal-host`); a rename makes every switch end as
   `pane_binding_unverified`, which the rotator marks unswitchable (fails closed).
3. If a pane respawns its shell before the session registry is written, the relaunch line could be typed into
   the respawned shell.
4. There is an unavoidable window between reading the screen and sending `/exit` or the continue prompt; a
   keystroke typed by the user in that window is mixed with the rotator's input.
5. Hard mode may restore the cancelled prompt into the input box after Ctrl-C, which ends the attempt as
   `interrupted` (touched), so that process is never retried.
6. Workspace trust inherited from a parent directory is not verified; sessions in a subdirectory may fail closed
   as `target_untrusted`, and the rotator then excludes that target for the session and tries the next one.
7. The shape of a real accepted Orca send receipt has not been sampled live.
8. Under launchd the daemon is expected to see the same `PATH` and Keychain access as an interactive shell;
   Keychain access from a launchd job context is not measured.
9. Logout or shutdown may SIGKILL an in-flight switch despite `ExitTimeOut`; its intent record keeps the session
   marked "outcome unknown".
10. Several sessions on one hot account may all pick the same lowest-used target in one tick (only a
    per-session cooldown applies).
11. `ExitTimeOut` is fixed at install time; re-run `rotator install` after changing `timeouts`.
12. With `KeepAlive`, a foreground `rotator daemon` makes the agent copy exit 1 and respawn every 10 s; run
    `rotator uninstall` first.
13. `locks/` and `sessions/` under the state dir are never pruned by the program.
