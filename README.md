# claude-account-rotator

A macOS background program for Claude Code sessions that run in Orca terminal panes on Orca's folder-based
accounts. It reads each account's 5-hour and 7-day usage, and before a session hits the rate limit it restarts
that session on a less-used account (`/exit`, then `claude --resume` with the other account's folder): a soft
switch at `soft_pct` once the session is idle, a hard switch at `hard_pct` even while it works. It also keeps
Orca's global `selected-host` pointer on a usable account.

## Features

- **Live usage dashboard** — `rotator status --watch` shows every account's 5-hour, weekly and Fable usage as an
  aligned terminal table (group, plan, bars, time until reset, token state) that redraws every second and widens
  with the terminal.
- **Automatic per-session rotation** — soft switch when idle, hard switch near the limit, then the session
  continues on the new account with `--resume`.
- **Fable-aware** — Fable sessions are judged by the highest of the 5-hour, weekly and Fable windows.
- **Idle-account token refresh** — the daemon refreshes OAuth tokens of accounts with no live session, the same
  way Claude Code does, and writes them back to the same Keychain item.
- **Shared, rate-limit friendly polling** — one usage request per account per interval across all rotator
  processes; the last good reading stays on screen through transient failures.
- **Python standard library only**, run by a launchd user agent.

## Quick start

```sh
git clone https://github.com/choiyounggi/claude-account-rotator.git
cd claude-account-rotator
bin/rotator status --watch     # dashboard only, changes nothing
bin/rotator daemon --dry-run   # see what the daemon would do, sends nothing
bin/rotator install            # run the daemon in the background (automatic switching on)
```

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
Claude 계정 사용량                                                                                                        21:00:00  데몬: 꺼짐
┌───┬───────┬────────┬───────┬────────────────────────────────┬───────────────────────────────┬───────────┬──────┬───────────────────────────┐
│   │ 계정  │ 그룹   │ 플랜  │ 5시간                          │ 주간                          │ 페이블    │ 세션 │ 갱신                      │
├───┼───────┼────────┼───────┼────────────────────────────────┼───────────────────────────────┼───────────┼──────┼───────────────────────────┤
│ * │ alice │ TEAM 1 │ Max5x │ ████████▒  89% · 1시간 21분 뒤 │ ████▒▒▒▒▒  45% · 6일 2시간 뒤 │ █▒▒▒  25% │    1 │ 3초 전                    │
│   │ carol │ TEAM 1 │ -     │ ▒▒▒▒▒▒▒▒▒   0%                 │ ██▒▒▒▒▒▒▒  28% · 2일 5시간 뒤 │ -         │    0 │ 갱신 필요 · 3일 전 · 캐시 │
│   │ bob   │ TEAM 2 │ Team  │ █████████ 100% · 21분 뒤       │ ███▒▒▒▒▒▒  37% · 6일 뒤       │ -         │    0 │ 3초 전                    │
│   │ dave  │ TEAM 2 │ -     │ -  (토큰 만료·캐시 없음)       │ -                             │ -         │    0 │ 갱신 필요                 │
└───┴───────┴────────┴───────┴────────────────────────────────┴───────────────────────────────┴───────────┴──────┴───────────────────────────┘
* = Orca 선택 계정   노랑 ≥85%  빨강 ≥95%
토큰 만료 계정은 데몬이 자동 갱신해: rotator install (또는 rotator daemon)

┌──────┬────────────────────┬──────┬─────────────────┬──────────────────────────────────┐
│ PID  │ 계정               │ 상태 │ 모델            │ 최근 전환 결과                   │
├──────┼────────────────────┼──────┼─────────────────┼──────────────────────────────────┤
│ 4101 │ alice TEAM 1 Max5x │ busy │ claude-opus-5-5 │ decision soft -> bob TEAM 2 Team │
│ 4202 │ (관리 밖)          │ idle │ -               │ -                                │
└──────┴────────────────────┴──────┴─────────────────┴──────────────────────────────────┘
q 또는 Ctrl-C 종료
```

- Accounts are named by their label: the local part of the account's email (before `@`), else its display name,
  else the first 8 characters of the account folder id. `*` marks the account `selected-host` points to.
- 그룹 is the organization the profile is logged in to (`oauthAccount.organizationName`). When two or more
  different names all start with the same word(s), that shared start is left out (`Example Org TEAM 1` shows as
  `TEAM 1`; a name is never cut down to a bare number). It is shown whole while the table fits and cut with `…`
  at 10 columns only when it does not; `-` when unknown; the column is left out when no account has one.
- 플랜 is the seat tier Orca stored for the profile (`rateLimitTier`): `Max5x`, `Max20x`, `Team` (`default_raven`),
  any other tier without its `default_` prefix cut to 8 characters, `-` when unknown. The same email logged in
  under several profiles shows as several rows told apart by 그룹 and 플랜; rows whose label, group and plan are
  all equal get ` #` and the first 4 characters of their account folder id. Rows are sorted by group, then label,
  then plan, then folder id. The sessions table's 계정 shows `<label> <group> <plan>` (26 columns at most).
- In a narrow 계정 cell the label is kept and what follows it gives way first: the group is cut, then the group
  and then the plan are left out, with ` #<id4>` taking over whenever the row would otherwise read like another;
  only then is the label itself cut (`team.membe… #a001`).
- Each usage cell is a 9-cell bar (4 for 페이블), the percent rounded down, and the time left until the window
  resets, rounded down: `· 1분 안`, `· 21분 뒤`, `· 1시간 21분 뒤`, `· 6일 2시간 뒤` (zero parts are left out:
  `· 1시간 뒤`, `· 6일 뒤`). A window already past its reset shows 0 % and no reset time. Cached numbers (Claude
  Code's copy, used when the live fetch fails) are drawn the same way, dimmed; an `unavailable` account shows `-`
  and the reason (토큰 만료, 토큰 없음, 요청 실패, 호출 제한, 연결 실패, 캐시 없음, …). 세션 counts the account's
  live sessions.
- 갱신 is how long ago the usage was fetched, followed by `· 캐시` for cached numbers. An account whose token is
  not usable shows what it needs first: `갱신 필요` (expired or expiring within 5 minutes, the daemon is off),
  `갱신 대기` (the same, the daemon will refresh it), `세션이 갱신` (a Claude session runs on the account; Claude Code refreshes the token on that
  session's next request and the rotator leaves it alone), `재로그인 필요` (the server refused the refresh token,
  or the daemon stopped trying until the Keychain item changes), `키체인 저장 실패`, or another refresh reason
  below. A cached row without a token problem shows its usage error there instead (`10분 전 · 캐시 · 호출 제한`).
  With the daemon off and at least one `갱신 필요` row, a footer says `토큰 만료 계정은 데몬이 자동 갱신해: rotator
  install (또는 rotator daemon)`; only the daemon refreshes tokens.
- Colours (only when stdout is a terminal and `NO_COLOR` is unset or empty): yellow at `soft_pct`, red at
  `hard_pct`, cache rows dimmed. Without colour the layout is the same.
- Every line is measured in terminal columns (Hangul counts 2). Columns are as wide as their widest cell, so on
  a wide enough terminal nothing is cut. When the table does not fit, the label, the 갱신 text and the
  unavailable reason are cut step by step down to 18, 20 and 24 columns; then it drops the bars, then the 플랜
  column, then 그룹 (accounts sharing a label then show their group and plan after it), then 페이블, then 갱신,
  then the reset times, then cuts an unavailable account's reason down to 4 columns, then narrows 계정; rows are
  never wrapped. `status --watch` measures the terminal on every redraw, so resizing it widens or narrows the
  table within a second. The sessions table cuts its 최근 전환 결과 text first, then its 계정 cell. Hint lines and
  the footer wrap at spaces instead of being cut. A
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

- Usage is fetched per account at most once per `usage_poll_s` (default 30 s, minimum 15 s) across every rotator
  process together: the daemon, `status --watch` and one-shot `status` share `usage/u-<hash>.json` in the state
  dir (one file per account; mode 0600, replaced atomically; numbers, times and error tokens only, never a token
  or an email). A process uses a stored reading younger than its interval instead of calling the API; only a
  stale account is fetched, under that account's non-blocking `locks/u-<hash>.lock`, and a process that finds
  the lock taken shows the stored reading and looks again next time. The next allowed fetch time is stored too,
  so no process fetches an account that is backing off. A corrupt or missing file counts as empty.
- When a fetch fails (호출 제한, 요청 실패, 연결 실패, HTTP 5xx, an expired token) the account keeps showing its last
  successful live reading — bars, percent, reset time, with that reading's age and the failure in 갱신
  (`5분 전 · 호출 제한`) — unless Claude Code's cache holds newer numbers. A usage cell shows `-` only for an
  account that never had a live reading and has no cache. In the JSON such an account stays `source` `live`
  and carries the failure in `error`.
- `status --watch` redraws every `--interval` seconds (default 1). It re-discovers sessions and re-reads the
  daemon snapshot, records and `selected-host` only every `session_poll_s` (default 10 s); the redraws in
  between re-render the last view with the current time, so the 갱신 age (`N초 전`, i.e. N seconds ago, since
  that account's usage was fetched) and the `1시간 12분 뒤` reset times still tick every second.
- An account answered with HTTP 429 waits for the server's `Retry-After` when the answer carries one (seconds or
  an HTTP-date, RFC 9110 §10.2.3): next fetch after `max(usage_poll_s, min(900 s, Retry-After))`. Without the
  header it is fetched again only after twice its last interval, doubling up to 300 s.
  It is shown with its cached numbers and `<age> · 캐시 · 호출 제한` in 갱신 (rate_limited), or `-  (호출 제한·캐시 없음)` when Claude
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
most one account's network refresh per daemon tick, after that tick's switch decisions; not for the source or a
target of a switch in flight when phase 2 starts (phase 2 is this refresh step; its skip set is read once, so a
worker that retargets to an account whose refresh section is already running is protected only by Claude Code's
own refresh lock, see risk 14); never in dry-run (`--dry-run` or `"dry_run": true`, shown as "드라이런"); not while
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

**Reasons** (the `reason` of a record; for a cached or unavailable account the 갱신 cell shows the Korean text
first, and an unavailable account's usage cell shows it before the usage error; `daemon_off` itself appears only
in the JSON — with the daemon off an expired account reads `갱신 필요`):

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
| `live_session` | 세션이 갱신 | a Claude session runs on the account (Claude Code refreshes it) |
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
| `server_error` | 갱신 실패 | a 5xx, or a 403 without an OAuth error (an edge block); backed off |
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
| `accounts` | one entry per account (a `live` entry with an `error` is the last good reading after a failed fetch): `account_id, label, group, plan, token_expires_at` (when the account's access token expires, as last read; never the token), `source, error, fetched_at, five_hour, seven_day, fable` (each window `{utilization, resets_at}` or null), `pointer`, `sessions` (`[{pid, status, model}]`) |
| `pointer_account_id` | the account `selected-host` names, or null |
| `sessions` | managed live sessions: `pid, session_id, status, account_id, model, decision, would_switch, in_flight, in_flight_pid, stuck, unswitchable, hard_hold_until, excluded, record_state, last_result, hint, post_verify` |
| `unmanaged_sessions` | `[{pid, session_id, status, model}]` |
| `attention` | `[{pid, session_id, updated_at, record_state, last_result, hint}]`: touched, not-ok switches of gone processes from the last 24 h |

Times are ISO-8601 UTC strings. `relaunch_command` appears only inside `last_result`. `label` is the short account name shown in
the table (email local part, else display name, else folder id prefix), `group` its organization name without
the prefix all organization names share (or null, uncut), and `plan` its seat tier tag (or null); no full email
address, account UUID, token or environment value is included. The only argv or transcript value included is a session's
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
  `state.json` (daemon snapshot), `daemon.lock` (single instance, holds the daemon pid), `usage/` (the
  shared usage readings, one file per account), `locks/` and `sessions/`. The state dir is fixed: `--config PATH` changes only which config file is read, so every daemon
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
13. `locks/`, `sessions/` and `usage/` under the state dir are never pruned by the program.
14. A Claude Code session that starts on an account during a refresh section and needs a refresh itself sees
    `lock_changed` while the rotator touches the lock dirs and waits about 2 × (5-10 s) + 7.5 s before giving up
    with `lock_busy`; in the worst case (every step hitting its timeout, about 2 minutes) its first request can
    fail.
15. While a write-failed token set is held, a Claude Code session refreshing that account posts the dead refresh
    token; the server's refresh-token reuse detection may then revoke the whole token family, and the account
    needs a re-login in Orca. The daemon itself never sends a session there: an account holding such a set is
    left out of switch targets (including a worker's retarget) and selected-host pointer targets, logged once as
    `event=target_excluded … reason=token_held` and shown in the session's decision reason as
    `excluded <ids>: token_held`; if the held list cannot be read, no target is chosen (`held_unknown`). A
    session the user starts on that account by hand is still exposed.
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
20. Session records are reloaded from disk at the start of each tick's session step, outside the daemon lock. A
    switch worker that ends in that window with an untouched final record (a hard-mode hold or an excluded
    target) can have it replaced in memory by the older disk copy, so that tick may dispatch the session once
    more without the hold or the exclusion. Touched sessions are unaffected (the in-memory touched set and
    `run_switch`'s on-disk re-check still block them).
21. The token endpoint sits behind Cloudflare, which answers 403 (error 1010) to urllib's default
    `Python-urllib` agent; the refresh POST therefore sends `User-Agent: claude-account-rotator/0.1`. If the edge
    blocks that agent too, refreshes end as `server_error` with `status=403 error=-` in `rotator.log` and are
    retried after a back-off rather than stopped.

## License

MIT — see [LICENSE](LICENSE).
