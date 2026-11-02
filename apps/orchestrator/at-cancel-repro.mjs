// Offline lifecycle probe: real helper; exact pinned active-job implementation.
// Replace only its two imports: the singleton is local to this probe and the
// unused admission resolver throws. No gateway, CLI, DB, or network is called.
import assert from 'node:assert/strict';
import {createHmac} from 'node:crypto';
import {readFile, writeFile, mkdtemp, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {pathToFileURL} from 'node:url';

// Run from any directory with OPENCLAW_PACKAGE_ROOT pointing at 2026.9.4.
const packageRoot = process.env.OPENCLAW_PACKAGE_ROOT || '/tmp/mission-briefs/openclaw-contract/package';
assert.equal(JSON.parse(await readFile(join(packageRoot, 'package.json'), 'utf8')).version, '2026.9.4');
const root = join(packageRoot, 'dist') + '/';
const src = await readFile(root + 'active-jobs-tYHAQWaa.mjs', 'utf8');
const body = src.split('\n').filter(line => !line.startsWith('import ')).join('\n');
const seams = `const singleton = new Map();
const resolveGlobalSingleton = (key, init) => {if (!singleton.has(key)) singleton.set(key, init()); return singleton.get(key);};
const resolveAdmittedRunActiveAssertion = () => { throw new Error('unused admission seam'); };\n`;
const active = await import('data:text/javascript;base64,' + Buffer.from(seams + body).toString('base64'));
const {i: registerActiveCronTaskRun} = await import(pathToFileURL(root + 'active-run-cancellation-BIgTNPfC.mjs'));
const dir = await mkdtemp(join(tmpdir(), 'prep-at-cancel-proof-'));
try {
  const key = 'offline-proof-key';
  process.env.NBHD_INTERNAL_API_KEY = key;
  process.env.NBHD_CRONS_FILE = join(dir, 'nbhd-crons.json');
  const {reconcileOnce} = await import(new URL('../../runtime/openclaw/nbhd-cron-sync.mjs', import.meta.url));
  const now = Date.now();
  const job = {
    id: 'offline-active-prep', name: 'Step Prep Now offline-proof', declarationKey: 'nbhd:123',
    enabled: true, sessionTarget: 'isolated',
    schedule: {kind: 'at', at: new Date(now - 1000).toISOString()},
    state: {runningAtMs: now - 900},
    payload: {kind: 'agentTurn', message: 'Offline prep', toolsAllow: ['nbhd_step_prep_next']},
    delivery: {mode: 'none'},
  };
  const signed = JSON.stringify([job]);
  await writeFile(process.env.NBHD_CRONS_FILE, JSON.stringify({signed, sig: createHmac('sha256', key).update(signed).digest('hex')}));
  const marker = active.p(job.id);
  const controller = new AbortController();
  const unregister = registerActiveCronTaskRun({runId: 'offline-prep-run', controller, activeJobMarker: marker});
  const calls = [];
  const result = await reconcileOnce({run: async args => {
    calls.push(args.slice(0, 3));
    if (args[1] === 'list') return JSON.stringify({jobs: [job]});
    assert.deepEqual(args, ['cron', 'rm', job.id]);
    // This is the exact active-job hook service.remove calls after persistence.
    active.h(job.id);
    return '{}';
  }});
  assert.deepEqual(result, {applied: 0, removed: 0, skipped: 1, ok: true});
  assert.deepEqual(calls, [['cron', 'list', '--json']]);
  assert.equal(controller.signal.aborted, false);
  console.log(JSON.stringify({result, calls, activeRunAborted: controller.signal.aborted, reason: controller.signal.reason}, null, 2));
  unregister();
  active.i(job.id, marker);
} finally {
  await rm(dir, {recursive: true, force: true});
}
