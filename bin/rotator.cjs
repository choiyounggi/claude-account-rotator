#!/usr/bin/env node
// npm's Windows shim cannot run bin/rotator directly (a Python script with a `#!/usr/bin/python3`
// shebang): package.json's "bin" points here instead. This finds a Python interpreter for the current
// OS and runs bin/rotator with it, passing argv through and exiting with its exit code. bin/rotator
// itself is unchanged and still used directly by launchd on macOS.
'use strict';

const { spawn, spawnSync } = require('child_process');
const os = require('os');
const path = require('path');

// ROTATOR_LAUNCHER_ENTRY lets the tests run a stand-in script; it is not a user setting.
const ROTATOR = process.env.ROTATOR_LAUNCHER_ENTRY || path.join(__dirname, 'rotator');

// [command, argsBeforeTheScript]
function findPython() {
  if (process.platform !== 'win32') {
    return ['/usr/bin/python3', []];
  }
  // `py -3` (the Python Launcher) picks the right installed interpreter; `python` is the fallback for
  // an interpreter installed without the launcher (e.g. some python.org or Microsoft Store installs).
  for (const candidate of [['py', ['-3']], ['python', []]]) {
    const probe = spawnSync(candidate[0], [...candidate[1], '--version'], { stdio: 'ignore' });
    if (!probe.error && probe.status === 0) {
      return candidate;
    }
  }
  return null;
}

const found = findPython();
if (found === null) {
  console.error('claude-account-rotator: no Python 3 found on PATH (install Python 3.9+ from python.org '
    + 'or the Microsoft Store)');
  process.exit(1);
}
const [cmd, baseArgs] = found;
const child = spawn(cmd, [...baseArgs, ROTATOR, ...process.argv.slice(2)], { stdio: 'inherit' });

// The launcher only waits, so the rotator behaves as if it had been started directly:
// - Ctrl-C reaches the child from the terminal (same foreground group); the launcher ignores it, so the
//   child's cleanup (cursor, alternate screen) finishes before the shell prompt comes back, and its exit
//   code is the one the shell sees.
// - SIGTERM/SIGHUP sent to the launcher are passed on, so the child never outlives it. When the whole group
//   was signalled (GNU timeout, a closing terminal) the child gets the signal twice; the dashboard acts on
//   the first only.
// - SIGINT sent to the launcher alone (`kill -INT <pid>`) is not passed on: doing so would deliver every
//   Ctrl-C twice. Stop it with SIGTERM instead.
const ignore = () => {};
process.on('SIGINT', ignore);
const forwarded = process.platform === 'win32' ? [] : ['SIGTERM', 'SIGHUP'];
const handlers = forwarded.map((sig) => [sig, () => { child.kill(sig); }]);
for (const [sig, handler] of handlers) process.on(sig, handler);

child.on('error', (err) => {
  console.error('claude-account-rotator: could not run `' + cmd + '`: ' + err.message);
  process.exit(1);
});
child.on('exit', (code, signal) => {
  if (signal) {
    // End the same way the child did, so a caller sees the signal and not a made-up exit code. Node answers
    // SIGUSR1 by opening its debugger instead of ending, so that one (and any signal that fails to end the
    // launcher) leaves with the shell's 128 + signal number.
    process.removeListener('SIGINT', ignore);
    for (const [sig, handler] of handlers) process.removeListener(sig, handler);
    process.exitCode = 128 + (os.constants.signals[signal] || 0);
    if (signal !== 'SIGUSR1') process.kill(process.pid, signal);
    return;
  }
  process.exit(code === null ? 1 : code);
});
