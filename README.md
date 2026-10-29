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

## Idle-account token refresh

Claude Code refreshes an account's OAuth token only while a session runs on it, so an idle account's token
expires and its usage falls back to the `.claude.json` cache. The daemon closes that gap.

**What.** Only the daemon refreshes. It refreshes the OAuth token of an account that has no live Claude session,
with the same request Claude Code 2.1.296 sends (`POST https://platform.claude.com/v1/oauth/token`, JSON body with
`grant_type=refresh_token`, the refresh token, the stored client id or Claude Code's default one, and Claude
Code's scope rule), and writes the new tokens back to the same Keychain item Claude Code reads
(`-a <Claude Code's account name> -s "Claude Code-credentials-<hash>"`), keeping every other field of the stored
JSON (subscription, rate-limit tier, MCP OAuth entries and anything unknown).

**When.** The access token is expired or has less than 5 minutes left; at most once per usage poll per account; at
most one account's network refresh per daemon tick, after that tick's switch decisions; never for the source or
target of an in-flight switch; never in dry-run (`--dry-run` or `"dry_run": true`, shown as "드라이런"); not while
the daemon is stopping. `rotator status`, `status --json`, `status --watch` and `rotator switch` never refresh:
with the daemon running they show its records; with it stopped the status JSON records say `daemon_off`
("데몬 꺼짐", which the table shows only in its header) and usage falls back to the cache. After a
successful refresh the next poll fetches that account's live usage.

**How to disable.** Set `"token_refresh": false` in `config.json`; every account then shows "자동 갱신 꺼짐".

**Safety rules.**

- Locks: the rotator's own `locks/r-*.lock` (so two rotator processes never refresh one account) plus Claude
  Code's own `<home>/.oauth_refresh.lock` and legacy `<profile>/home.lock` dirs. Claude Code's dirs are taken
  without waiting and never taken over: a busy one is "갱신 중", one older than 60 s is "잠금 남음".
- Under the locks the Keychain item is read again and compared with what the gate saw; any change skips the
  account ("갱신 중").
- A probe write of the unchanged item (and a read-back) runs before the request, so a Keychain that refuses
  writes is found before the refresh token is spent ("키체인 쓰기 불가", retried after a back-off).
- The live-session check runs three times: in the gate, under the locks, and right before the request. A
  session-discovery failure counts as live ("세션 확인 실패").
- The secret goes only to `security -i` on stdin, never on the command line; `security` output is discarded. An
  item whose write line would exceed 4032 characters is not refreshed ("갱신 불가"), and neither is a profile that
  stores its credentials in a plaintext `.credentials.json`.
- After `invalid_grant`, an unknown outcome (a timeout or dropped connection after the request was sent), a
  malformed response or another rejection, the account is not retried until its Keychain item changes (a
  re-login in Orca or a refresh by Claude Code). Server errors, network errors before sending and 429 back off
  5 min doubling to 1 h (429 honours `Retry-After`).
- Stop and back-off state lives in memory only, so a daemon restart retries each stopped account once.
- No token is ever logged, printed, put in an exception message or written to a file; the status JSON carries
  only the record below.

**The write-failure window.** After a successful refresh the old refresh token is dead. Each write is a re-read,
a write and a read-back; if all 3 writes fail the daemon keeps only the new token set in memory, keeps the
account's rotator lock, and retries the write with back-off 30 s doubling to 10 min, alternating with other
accounts ("키체인 저장 실패"). If the item turns out to hold a different refresh token (or is gone on two
consecutive lookups) the set is dropped ("갱신 충돌"). The held set is lost when the daemon stops — the account
then needs a re-login in Orca; on a normal stop or SIGTERM the daemon log names the affected account ids at
ERROR (`event=token_refresh_held_at_exit`) before waiting for in-flight switches. A SIGKILL or a crash — including
launchd's kill after its exit timeout while a long write window or switch drain is still running — loses a held
set with no log line at all.

**`cc_lock_stale` ("잠금 남음").** Remove `<home>/.oauth_refresh.lock` (and `<profile>/home.lock`) by hand only
when no Claude session runs on that account. A stale `.oauth_refresh.lock.owner` file is left alone: Claude Code
answers `record_mismatch` for it and does not take the lock over.

**Reasons** (the `reason` of a record; for a cached or unavailable account the table shows the Korean text first,
then the usage error, clipped to the cell width; `daemon_off` appears only in the JSON):

| reason | text | meaning |
|--------|------|---------|
| `disabled` | 자동 갱신 꺼짐 | `token_refresh` is false |
| `dry_run` | 드라이런 | dry-run: nothing is refreshed |
| `daemon_off` | 데몬 꺼짐 | the daemon is not running, so nothing refreshes (JSON only; the header says so) |
| `no_credential` | 토큰 없음 | no readable Claude Code credentials item |
| `no_refresh_token` | 재로그인 필요 | the item has no usable refresh token |
| `plaintext_store` | 갱신 불가 | the profile keeps a plaintext `.credentials.json` |
| `keychain_item_unclear` | 갱신 불가 | the item was not found under Claude Code's account name |
| `payload_too_large` | 갱신 불가 | the write line would exceed 4032 characters |
| `live_session` | 사용 중 | a Claude session runs on the account (Claude Code refreshes it) |
| `discovery_failed` | 세션 확인 실패 | sessions could not be listed, so nothing is refreshed |
| `deferred` | 갱신 대기 | waiting for its turn (one network refresh per tick) |
| `locked` | 갱신 중 | another rotator process holds the account's lock |
| `cc_lock_busy` | 갱신 중 | Claude Code holds its refresh lock |
| `cc_lock_stale` | 잠금 남음 | Claude Code's lock dir is older than 60 s (see above) |
| `changed_under_lock` | 갱신 중 | the item changed while the lock was taken |
| `keychain_write_unavailable` | 키체인 쓰기 불가 | the probe write failed; retried after a back-off |
| `invalid_grant` | 재로그인 필요 | the server rejected the refresh token |
| `invalid_scope` | 재로그인 필요 | the server rejected the scopes |
| `rejected` | 갱신 거부 | another 4xx or an unexpected status |
| `malformed_response` | 응답 이상 | a 200 whose body is not a valid token set |
| `outcome_unknown` | 갱신 결과 불명 | the request may have reached the server; not retried |
| `network_error` | 연결 실패 | DNS or connection refused before sending; backed off |
| `server_error` | 갱신 실패 | a 5xx; backed off |
| `rate_limited` | 호출 제한 | a 429; backed off |
| `write_failed` | 키체인 저장 실패 | the new tokens are held in memory, write retried |
| `conflict` | 갱신 충돌 | the item holds another refresh token; the new set was dropped |
| `internal_error` | 갱신 실패 | an unexpected error; backed off |

**Version note.** Every statement about Claude Code and Orca above was measured on Claude Code 2.1.296 and Orca
1.4.241 (request, scopes, Keychain item and write, lock protocol, stored JSON shape); re-verify after either
updates.

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
Every `accounts[]` entry also has `refresh`: `{state, reason, at, last_refreshed_at}` (`state` one of
`not_needed`, `skipped`, `refreshed`, `failed`, `stopped`; see "Idle-account token refresh") or null; the schema
stays 1.

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
| `token_refresh` | `true` | `true` / `false`; `false` turns idle-account token refresh off (see "Idle-account token refresh") |
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
   Keychain access from a launchd job context is not measured; token refresh probes the write path first and
   reports "키체인 쓰기 불가" instead of refreshing when it fails.
9. Logout or shutdown may SIGKILL an in-flight switch despite `ExitTimeOut`; its intent record keeps the session
   marked "outcome unknown".
10. Several sessions on one hot account may all pick the same lowest-used target in one tick (only a
    per-session cooldown applies).
11. `ExitTimeOut` is fixed at install time; re-run `rotator install` after changing `timeouts`.
12. With `KeepAlive`, a foreground `rotator daemon` makes the agent copy exit 1 and respawn every 10 s; run
    `rotator uninstall` first.
13. `locks/` and `sessions/` under the state dir are never pruned by the program.
14. A Claude Code session that starts on an account during a refresh section and needs a refresh itself sees
    `lock_changed` while the rotator touches the lock dirs and waits about 2 × (5-10 s) + 7.5 s before giving up
    with `lock_busy`; in the worst case (every step hitting its timeout, about 2 minutes) its first request can
    fail.
15. While a write-failed token set is held, a Claude Code session refreshing that account posts the dead refresh
    token; the server's refresh-token reuse detection may then revoke the whole token family, and the account
    needs a re-login in Orca.
16. `security` exit code 44 ("item not found") cannot be told apart from an item that exists in a keychain
    missing from the search list; the rotator drops a held token set as "item gone" only after two consecutive
    code-44 lookups.
17. Stop and back-off state is not persisted: a daemon restart retries each stopped account once (for example
    one more `invalid_grant`).
18. The refresh request, scope rule, Keychain write and lock protocol mirror Claude Code 2.1.296; a future Claude
    Code build that refreshes without either lock, or changes the stored JSON shape, is not covered until
    re-verified.
19. A step inside a refresh section that stalls longer than 60 s without a lock-dir touch (for example a DNS
    lookup, which the urllib timeout does not bound) lets Claude Code treat the lock as stale and refresh the
    same account at the same time; on release the rotator then leaves any lock dir that is no longer its own
    (different inode or birth time) in place.
