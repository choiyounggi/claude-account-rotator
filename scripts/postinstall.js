// Runs after `npm install -g claude-account-rotator`: fails the install on a Mac that cannot run the rotator
// (rotator doctor), and refreshes an already installed background agent so it runs the new version.
'use strict';

const { spawnSync } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');

const PYTHON = '/usr/bin/python3';
const ROTATOR = path.join(__dirname, '..', 'bin', 'rotator');
// Same label as rotator/launchd.py LABEL.
const PLIST = path.join(os.homedir(), 'Library', 'LaunchAgents',
  'io.github.choiyounggi.claude-account-rotator.plist');

function fail(message) {
  console.error('claude-account-rotator: ' + message);
  process.exit(1);
}

if (process.platform !== 'darwin') {
  fail('runs only on macOS');
}
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
