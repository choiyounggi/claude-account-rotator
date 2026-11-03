// Tests for scripts/release.mjs. Run with `node --test scripts/release.check.mjs` (kept out of the
// vitest suite: the release script is plain JavaScript for the CI job, not part of the package).
import test from 'node:test';
import assert from 'node:assert/strict';
import {
  bumpVersion, classifyCommit, compareVersions, planRelease, renderNotes, withLockVersion, withPackageVersion, withPinnedVersion,
} from './release.mjs';

const commit = (subject, body = '', hash = 'abcdef1234567890') => ({ hash, subject, body });

test('classifyCommit maps conventional types to bumps', () => {
  assert.equal(classifyCommit(commit('feat: 근태 도구 추가')).bump, 'minor');
  assert.equal(classifyCommit(commit('fix(rotator): retry soft')).bump, 'patch');
  assert.equal(classifyCommit(commit('docs: README 보강')).bump, 'none');
  assert.equal(classifyCommit(commit('ci: cache npm')).bump, 'none');
});

test('classifyCommit treats ! and BREAKING CHANGE as major, and an unconventional message as a patch', () => {
  assert.equal(classifyCommit(commit('feat!: drop node 22')).bump, 'major');
  assert.equal(classifyCommit(commit('refactor: config', 'BREAKING CHANGE: new path')).bump, 'major');
  assert.deepEqual(classifyCommit(commit('Update stuff')), { type: null, bump: 'patch', title: 'Update stuff' });
});

test('classifyCommit releases nothing for a merge commit, whatever it merges', () => {
  assert.equal(classifyCommit(commit('Merge pull request #12 from someone/feature')).bump, 'none');
  assert.equal(classifyCommit(commit("Merge branch 'main' into topic")).bump, 'none');
  assert.equal(classifyCommit(commit("Merge remote-tracking branch 'origin/main'")).bump, 'none');
  assert.equal(classifyCommit(commit('Merged the parser: faster')).bump, 'patch');
});

test('versions bump, compare numerically and refuse malformed input', () => {
  assert.equal(bumpVersion('0.2.0', 'patch'), '0.2.1');
  assert.equal(bumpVersion('0.2.3', 'minor'), '0.3.0');
  assert.equal(bumpVersion('0.2.3', 'major'), '1.0.0');
  assert.ok(compareVersions('0.10.0', '0.9.9') > 0);
  assert.throws(() => bumpVersion('0.2', 'patch'), /not a release version/);
  assert.throws(() => bumpVersion('0.2.0', 'huge'), /unknown bump/);
});

test('planRelease takes the highest bump since the last tag', () => {
  const plan = planRelease({ previousTag: 'v0.2.0', packageVersion: '0.2.0',
    commits: [commit('fix: one'), commit('feat: two'), commit('docs: three')] });
  assert.deepEqual([plan.release, plan.version, plan.bump], [true, '0.3.0', 'minor']);
});

test('planRelease skips non-releasing commits, merge commits and an empty range', () => {
  const quiet = planRelease({ previousTag: 'v0.2.0', packageVersion: '0.2.0',
    commits: [commit('ci: add release job'), commit('chore(release): v0.2.0'), commit('Merge pull request #3 from x/ci')] });
  assert.deepEqual([quiet.release, quiet.version], [false, '0.2.0']);
  assert.equal(planRelease({ previousTag: 'v0.2.0', packageVersion: '0.2.0', commits: [] }).release, false);
});

test('planRelease honors a version raised by hand, and releases package.json without a tag', () => {
  assert.equal(planRelease({ previousTag: 'v0.2.0', packageVersion: '1.0.0', commits: [commit('fix: x')] }).version, '1.0.0');
  assert.equal(planRelease({ previousTag: null, packageVersion: '0.2.0', commits: [] }).version, '0.2.0');
});

test('renderNotes groups changes and links the compare view', () => {
  const plan = planRelease({ previousTag: 'v0.2.0', packageVersion: '0.2.0', commits: [commit('feat: a'), commit('fix: b')] });
  const notes = renderNotes(plan, 'https://github.com/example/repo');
  assert.match(notes, /## Features\n\n- a \(abcdef1\)/);
  assert.match(notes, /## Fixes\n\n- b \(abcdef1\)/);
  assert.match(notes, /https:\/\/github\.com\/example\/repo\/compare\/v0\.2\.0\.\.\.v0\.3\.0/);
  assert.match(renderNotes({ version: '0.2.0', previous: null, commits: [] }, 'https://x'), /Initial release\./);
});

test('withLockVersion sets the root version in a lockfile and leaves dependencies alone', () => {
  const lock = { name: 'pkg', version: '0.2.0', lockfileVersion: 3,
    packages: { '': { name: 'pkg', version: '0.2.0' }, 'node_modules/dep': { version: '0.2.0' } } };
  const next = withLockVersion(lock, '0.3.0');
  assert.equal(next.version, '0.3.0');
  assert.equal(next.packages[''].version, '0.3.0');
  assert.equal(next.packages['node_modules/dep'].version, '0.2.0');
  assert.equal(lock.version, '0.2.0', 'the input is not changed');
  // A v1 lockfile has no packages map.
  assert.deepEqual(withLockVersion({ version: '0.2.0', lockfileVersion: 1 }, '0.3.0'), { version: '0.3.0', lockfileVersion: 1 });
  assert.throws(() => withLockVersion(lock, 'next'), /not a release version/);
});

test('withPinnedVersion moves name@x.y.z pins to the new version and leaves placeholders and other packages', () => {
  const readme = [
    'claude mcp add jade -s user -- npx -y jade-mcp@0.2.0',
    'npx -y jade-mcp@<버전> and jade-mcp@<새 버전>',
    'not-jade-mcp@0.2.0 stays, so does jade-mcp-extra@0.2.0',
    'pinned twice: jade-mcp@0.1.9 jade-mcp@0.2.0.',
  ].join('\n');
  const next = withPinnedVersion(readme, 'jade-mcp', '0.3.0');
  assert.match(next, /npx -y jade-mcp@0\.3\.0\n/);
  assert.match(next, /jade-mcp@<버전> and jade-mcp@<새 버전>/);
  assert.match(next, /not-jade-mcp@0\.2\.0 stays, so does jade-mcp-extra@0\.2\.0/);
  assert.match(next, /pinned twice: jade-mcp@0\.3\.0 jade-mcp@0\.3\.0\./);
  assert.equal(withPinnedVersion('no pins here', 'jade-mcp', '0.3.0'), 'no pins here');
  assert.throws(() => withPinnedVersion(readme, 'jade-mcp', 'latest'), /not a release version/);
});

test('withPackageVersion changes only the top-level version and keeps the file formatting', () => {
  const text = '{\n  "name": "pkg",\n  "version": "0.1.0",\n  "bin": { "pkg": "bin/pkg" },\n  "files": ["a", "b"],\n'
    + '  "dependencies": { "dep": { "version": "0.1.0" } }\n}\n';
  const next = withPackageVersion(text, '0.1.1');
  assert.equal(next, text.replace('"version": "0.1.0",', '"version": "0.1.1",'));
  assert.throws(() => withPackageVersion('{ "name": "pkg" }', '0.1.1'), /no top-level "version"/);
  assert.throws(() => withPackageVersion(text, 'v1'), /not a release version/);
});

test('renderNotes lists every commit since the last tag by kind, leaving out release and merge commits', () => {
  const commits = [
    commit('feat!: drop node 22', '', 'a00000000'),
    commit('feat: 근태 도구 추가', '', 'b00000000'),
    commit('fix(cli): keep the exit code', '', 'c00000000'),
    commit('refactor: split the client', '', 'd00000000'),
    commit('Update stuff', '', 'e00000000'),
    commit('docs: README 보강', '', 'f00000000'),
    commit('ci: cache npm', '', '100000000'),
    commit('chore(release): v0.2.0', '', '200000000'),
    commit('Merge pull request #3 from someone/topic', '', '300000000'),
    commit('test: more cases', '', '400000000'),
  ];
  const notes = renderNotes(planRelease({ previousTag: 'v0.2.0', packageVersion: '0.2.0', commits }), 'https://github.com/example/repo');
  assert.deepEqual([...notes.matchAll(/^## (.+)$/gm)].map((m) => m[1]),
    ['Breaking changes', 'Features', 'Fixes', 'Other changes', 'Documentation', 'Maintenance']);
  assert.match(notes, /## Breaking changes\n\n- drop node 22 \(a000000\)\n/);
  assert.match(notes, /## Other changes\n\n- split the client \(d000000\)\n- Update stuff \(e000000\)\n/);
  assert.match(notes, /## Documentation\n\n- README 보강 \(f000000\)\n/);
  assert.match(notes, /## Maintenance\n\n- cache npm \(1000000\)\n- more cases \(4000000\)\n/);
  assert.doesNotMatch(notes, /\(2000000\)|\(3000000\)|Merge pull request/);
});

test('renderNotes leaves out only the bot release commits, not a human chore(release) commit', () => {
  const notes = renderNotes(planRelease({ previousTag: 'v0.2.0', packageVersion: '0.2.0', commits: [
    commit('chore(release): v0.2.1', '', '500000000'),
    commit('chore(release): update Homebrew formula for v0.2.1', '', '600000000'),
    commit('chore(release): switch to trusted publishing (#16)', '', '700000000'),
    commit('fix: x', '', '800000000'),
  ] }), 'https://x');
  assert.match(notes, /## Maintenance\n\n- switch to trusted publishing \(#16\) \(7000000\)\n/);
  assert.doesNotMatch(notes, /\(5000000\)|\(6000000\)/);
});
