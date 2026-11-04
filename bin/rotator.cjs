#!/usr/bin/env node
// npm's Windows shim cannot run bin/rotator directly (a Python script with a `#!/usr/bin/python3`
// shebang): package.json's "bin" points here instead. This finds a Python interpreter for the current
// OS and runs bin/rotator with it, passing argv through and exiting with its exit code. bin/rotator
// itself is unchanged and still used directly by launchd on macOS.
'use strict';

const { spawnSync } = require('child_process');
const path = require('path');

const ROTATOR = path.join(__dirname, 'rotator');

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
const result = spawnSync(cmd, [...baseArgs, ROTATOR, ...process.argv.slice(2)], { stdio: 'inherit' });
if (result.error) {
  console.error('claude-account-rotator: could not run `' + cmd + '`: ' + result.error.message);
  process.exit(1);
}
process.exit(result.status === null ? 1 : result.status);
