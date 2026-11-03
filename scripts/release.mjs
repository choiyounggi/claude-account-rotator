#!/usr/bin/env node
// Release planning for the GitHub Actions release job. The workflow owns git, tags and uploads;
// this file owns the decisions, so they can be unit tested (scripts/release.check.mjs).
//
//   node scripts/release.mjs plan                   → JSON {release, version, previous, bump}, notes in release-notes.md
//   node scripts/release.mjs set-version <version>  → writes package.json, the lockfile's root version and
//                                                     README.md's `<name>@x.y.z` install pins
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const PACKAGE_JSON = path.join(ROOT, 'package.json');
const LOCKFILES = ['npm-shrinkwrap.json', 'package-lock.json'].map((name) => path.join(ROOT, name));
const NOTES = path.join(ROOT, 'release-notes.md');
const README = path.join(ROOT, 'README.md');

// Commits of these types change nothing a user installs, so on their own they do not cut a release.
// Anything else — including messages that follow no convention — does, as a patch.
const NON_RELEASING = new Set(['docs', 'chore', 'ci', 'test', 'style']);
// Git's own merge subjects: the merged commits are in the range and classified on their own.
const MERGE_COMMIT = /^Merge (pull request|branch|remote-tracking branch|tag|commit) /;
const CONVENTIONAL = /^(\w+)(?:\([^)]*\))?(!)?:\s*(.*)$/;
const RANK = { none: 0, patch: 1, minor: 2, major: 3 };

export function parseVersion(version) {
  const match = /^v?(\d+)\.(\d+)\.(\d+)$/.exec(String(version).trim());
  if (!match) throw new Error(`not a release version: ${version}`);
  return match.slice(1).map(Number);
}

export function compareVersions(a, b) {
  const [x, y] = [parseVersion(a), parseVersion(b)];
  for (let i = 0; i < 3; i += 1) if (x[i] !== y[i]) return x[i] - y[i];
  return 0;
}

export function classifyCommit({ subject, body = '' }) {
  if (MERGE_COMMIT.test(subject)) return { type: 'merge', bump: 'none', title: subject };
  const match = CONVENTIONAL.exec(subject);
  const type = match ? match[1].toLowerCase() : null;
  if (match?.[2] || /^BREAKING[ -]CHANGE:/m.test(body)) return { type, bump: 'major', title: match ? match[3] : subject };
  if (type === 'feat') return { type, bump: 'minor', title: match[3] };
  if (type && NON_RELEASING.has(type)) return { type, bump: 'none', title: match[3] };
  return { type, bump: 'patch', title: match ? match[3] : subject };
}

export function bumpVersion(version, bump) {
  const [major, minor, patch] = parseVersion(version);
  if (bump === 'major') return `${major + 1}.0.0`;
  if (bump === 'minor') return `${major}.${minor + 1}.0`;
  if (bump === 'patch') return `${major}.${minor}.${patch + 1}`;
  throw new Error(`unknown bump: ${bump}`);
}

/**
 * The next release from the last tag, the commits since it and package.json's version. With no tag
 * yet, package.json's version is the first release. A version raised by hand in package.json wins
 * over a smaller computed one.
 */
export function planRelease({ previousTag, packageVersion, commits }) {
  const classified = commits.map((c) => ({ ...c, ...classifyCommit(c) }));
  if (previousTag === null) return { release: true, version: packageVersion, previous: null, bump: 'initial', commits: classified };

  const bump = classified.reduce((best, c) => (RANK[c.bump] > RANK[best] ? c.bump : best), 'none');
  const previous = previousTag.replace(/^v/, '');
  if (bump === 'none') return { release: false, version: previous, previous, bump, commits: classified };

  const computed = bumpVersion(previous, bump);
  const version = compareVersions(packageVersion, computed) > 0 ? packageVersion : computed;
  return { release: true, version, previous, bump, commits: classified };
}

// The bot's own release commits; never listed in notes.
const RELEASE_COMMIT = /^chore\(release\): (v\d+\.\d+\.\d+|update Homebrew formula for v\S+)$/;
// Every commit since the last tag lands in one section, in `git log` order; only the bot's release
// commits and git merge commits (whose merged commits are listed themselves) are left out.
const NOTE_SECTIONS = [
  ['Breaking changes', (c) => c.bump === 'major'],
  ['Features', (c) => c.bump === 'minor'],
  ['Fixes', (c) => c.bump === 'patch' && c.type === 'fix'],
  ['Other changes', (c) => c.bump === 'patch' && c.type !== 'fix'],
  ['Documentation', (c) => c.bump === 'none' && c.type === 'docs'],
  ['Maintenance', (c) => c.bump === 'none' && c.type !== 'docs'],
];

export function renderNotes({ version, previous, commits }, repoUrl) {
  const listed = commits.filter((c) => c.type !== 'merge' && !RELEASE_COMMIT.test(c.subject));
  const lines = [];
  for (const [heading, belongs] of NOTE_SECTIONS) {
    const items = listed.filter(belongs);
    if (items.length === 0) continue;
    lines.push(`## ${heading}`, '', ...items.map((c) => `- ${c.title} (${c.hash.slice(0, 7)})`), '');
  }
  if (lines.length === 0) lines.push('Initial release.', '');
  const range = previous ? `v${previous}...v${version}` : `v${version}`;
  lines.push(`**Full changelog**: ${repoUrl}/${previous ? 'compare' : 'commits'}/${range}`, '');
  return lines.join('\n');
}

/**
 * package.json text with its top-level "version" set, everything else byte-for-byte: a JSON
 * round-trip would reformat a hand-formatted file. The top-level key is the first one at two
 * spaces of indent.
 */
export function withPackageVersion(text, version) {
  parseVersion(version);
  const field = /^( {2}"version":\s*")[^"]*(")/m;
  if (!field.test(text)) throw new Error('package.json has no top-level "version"');
  return text.replace(field, `$1${version}$2`);
}

/** A lockfile (npm-shrinkwrap.json / package-lock.json) with its root package at `version`; dependencies untouched. */
export function withLockVersion(lock, version) {
  parseVersion(version);
  const next = { ...lock, version };
  if (lock.packages?.['']) next.packages = { ...lock.packages, '': { ...lock.packages[''], version } };
  return next;
}

const escapeRegExp = (text) => text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

/** `text` with every `<name>@x.y.z` pin (an install command in a README) moved to `version`; `<name>@<…>` placeholders stay. */
export function withPinnedVersion(text, name, version) {
  parseVersion(version);
  const pin = new RegExp(`(?<![\\w@/.-])${escapeRegExp(name)}@\\d+\\.\\d+\\.\\d+(?![\\w.-]*\\d)`, 'g');
  return text.replace(pin, `${name}@${version}`);
}

function git(...args) {
  return execFileSync('git', args, { cwd: ROOT, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] }).trim();
}

function lastTag() {
  try {
    return git('describe', '--tags', '--abbrev=0', '--match', 'v[0-9]*');
  } catch {
    return null;
  }
}

function commitsSince(tag) {
  const raw = git('log', '--format=%H%x1f%s%x1f%b%x1e', ...(tag ? [`${tag}..HEAD`] : ['HEAD']));
  return raw
    .split('\x1e')
    .map((entry) => entry.trim())
    .filter(Boolean)
    .map((entry) => {
      const [hash, subject, body] = entry.split('\x1f');
      return { hash, subject, body: body ?? '' };
    });
}

function readJson(file) {
  return JSON.parse(fs.readFileSync(file, 'utf8'));
}

function writeJson(file, value) {
  fs.writeFileSync(file, `${JSON.stringify(value, null, 2)}\n`);
}

/** https://github.com/owner/repo from package.json's repository field. */
function repoUrl(pkg) {
  const raw = typeof pkg.repository === 'string' ? pkg.repository : pkg.repository?.url;
  if (!raw) throw new Error('package.json has no repository url');
  return raw.replace(/^git\+/, '').replace(/^git@github\.com:/, 'https://github.com/').replace(/\.git$/, '');
}

function main([command, ...args]) {
  if (command === 'plan') {
    const pkg = readJson(PACKAGE_JSON);
    const previousTag = lastTag();
    const plan = planRelease({ previousTag, packageVersion: pkg.version, commits: commitsSince(previousTag) });
    if (plan.release) fs.writeFileSync(NOTES, renderNotes(plan, repoUrl(pkg)));
    process.stdout.write(`${JSON.stringify({ release: plan.release, version: plan.version, previous: plan.previous, bump: plan.bump })}\n`);
    return;
  }
  if (command === 'set-version') {
    parseVersion(args[0]);
    const pkg = readJson(PACKAGE_JSON);
    fs.writeFileSync(PACKAGE_JSON, withPackageVersion(fs.readFileSync(PACKAGE_JSON, 'utf8'), args[0]));
    for (const lock of LOCKFILES) if (fs.existsSync(lock)) writeJson(lock, withLockVersion(readJson(lock), args[0]));
    if (fs.existsSync(README)) fs.writeFileSync(README, withPinnedVersion(fs.readFileSync(README, 'utf8'), pkg.name, args[0]));
    return;
  }
  throw new Error('usage: release.mjs plan | set-version <version>');
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  try {
    main(process.argv.slice(2));
  } catch (error) {
    process.stderr.write(`error: ${error.message}\n`);
    process.exit(1);
  }
}
