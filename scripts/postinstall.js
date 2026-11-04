// Runs after `npm install -g claude-account-rotator`: fails the install on a machine that cannot run
// the rotator (rotator doctor), and on macOS refreshes an already installed background agent so it
// runs the new version (Windows has no background agent yet).
'use strict';

const { spawnSync } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');

const ROTATOR = path.join(__dirname, '..', 'bin', 'rotator');
// Same label as rotator/launchd.py LABEL.
const PLIST = path.join(os.homedir(), 'Library', 'LaunchAgents',
  'io.github.choiyounggi.claude-account-rotator.plist');

function fail(message) {
  console.error('claude-account-rotator: ' + message);
  process.exit(1);
}

// [command, argsBeforeTheScript], mirroring bin/rotator.cjs's own Python lookup.
function findWindowsPython() {
  for (const candidate of [['py', ['-3']], ['python', []]]) {
    const probe = spawnSync(candidate[0], [...candidate[1], '--version'], { stdio: 'ignore' });
    if (!probe.error && probe.status === 0) {
      return candidate;
    }
  }
  return null;
}

if (process.platform === 'darwin') {
  const PYTHON = '/usr/bin/python3';
  // Until the Command Line Tools are installed, /usr/bin/python3 is a stub that opens an install dialog.
  if (spawnSync('/usr/bin/xcode-select', ['-p'], { stdio: 'ignore' }).status !== 0) {
    fail('/usr/bin/python3 needs the Xcode Command Line Tools: run `xcode-select --install`, then install again');
  }
  if (spawnSync(PYTHON, [ROTATOR, 'doctor'], { stdio: 'inherit' }).status !== 0) {
    fail('this Mac cannot run the rotator yet: fix the [fail] lines above, then install again');
  }
  // Only a global install replaces the agent: npx and a local `npm install` (also inside a checkout) leave it alone.
  if (process.env.npm_config_global === 'true' && fs.existsSync(PLIST)) {
    if (process.getuid() === 0) {
      console.warn('claude-account-rotator: installed as root; run `rotator install` as yourself to update the agent');
    } else if (spawnSync(PYTHON, [ROTATOR, 'install'], { stdio: 'inherit' }).status !== 0) {
      console.warn('claude-account-rotator: could not update the background agent; run `rotator install`');
    }
  } else {
    console.log('next: `rotator status --watch` for the dashboard, `rotator install` to switch automatically');
  }
} else if (process.platform === 'win32') {
  const found = findWindowsPython();
  if (found === null) {
    fail('no Python 3 found on PATH (install Python 3.9+ from python.org or the Microsoft Store), then install again');
  }
  const [cmd, baseArgs] = found;
  if (spawnSync(cmd, [...baseArgs, ROTATOR, 'doctor'], { stdio: 'inherit' }).status !== 0) {
    fail('this machine cannot run the rotator yet: fix the [fail] lines above, then install again');
  }
  console.log('next: `rotator status --watch` for the dashboard (automatic switching is not yet available on Windows)');
} else {
  fail('runs only on macOS or Windows');
}
