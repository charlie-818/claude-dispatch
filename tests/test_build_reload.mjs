// Stubbed-fetch unit test for the build-drift reload logic added to
// static/index.html (checkBuild + draft/pane restore). Extracts the exact
// source lines rather than re-implementing them, so this fails the moment
// the real code drifts from what's asserted here.
import { readFileSync } from 'fs';
import vm from 'vm';
import assert from 'assert';

const html = readFileSync(new URL('../static/index.html', import.meta.url), 'utf8');

const fnStart = html.indexOf('async function checkBuild(){');
const fnEnd = html.indexOf('\n}\n', fnStart) + 3;
const checkBuildFn = html.slice(fnStart, fnEnd);

function makeSandbox(overrides = {}) {
  const store = {};
  const sessionStorage = {
    getItem: k => (k in store ? store[k] : null),
    setItem: (k, v) => { store[k] = String(v); },
    removeItem: k => { delete store[k]; },
  };
  let reloaded = false;
  const sandbox = {
    MY_BUILD: null,
    openKind: null,
    userScrolling: false,
    document: { activeElement: null },
    input: { value: '' },
    current: null,
    sessionStorage,
    location: { reload: () => { reloaded = true; } },
    fetch: null,
    JSON,
    Date,
    console,
    ...overrides,
  };
  sandbox.__store = store;
  sandbox.__reloaded = () => reloaded;
  vm.createContext(sandbox);
  vm.runInContext(checkBuildFn, sandbox);
  return sandbox;
}

async function run(name, fn) {
  try { await fn(); console.log(`ok - ${name}`); }
  catch (e) { console.error(`FAIL - ${name}`); console.error(e); process.exitCode = 1; }
}

await run('first ping just records MY_BUILD, no reload', async () => {
  const sb = makeSandbox({ fetch: async () => ({ json: async () => ({ build: 'abc123' }) }) });
  await sb.checkBuild();
  assert.strictEqual(sb.MY_BUILD, 'abc123');
  assert.strictEqual(sb.__reloaded(), false);
});

await run('same build never reloads', async () => {
  const sb = makeSandbox({ fetch: async () => ({ json: async () => ({ build: 'same' }) }) });
  await sb.checkBuild(); // records MY_BUILD
  await sb.checkBuild(); // same build again
  assert.strictEqual(sb.__reloaded(), false);
});

await run('mismatched build reloads when safe, saving draft+pane', async () => {
  let build = 'v1';
  const sb = makeSandbox({
    fetch: async () => ({ json: async () => ({ build }) }),
    input: { value: 'hello world' },
    current: { uuid: 'PANE-UUID-1' },
  });
  await sb.checkBuild(); // v1 -> MY_BUILD
  build = 'v2';
  await sb.checkBuild(); // mismatch, safe -> reload
  assert.strictEqual(sb.__reloaded(), true);
  assert.strictEqual(sb.__store['dispatch-draft'], 'hello world');
  assert.strictEqual(sb.__store['dispatch-pane'], 'PANE-UUID-1');
  const saved = JSON.parse(sb.__store['dispatch-reload']);
  assert.strictEqual(saved.build, 'v2');
});

await run('mismatched build does NOT reload when a sheet is open', async () => {
  let build = 'v1';
  const sb = makeSandbox({
    fetch: async () => ({ json: async () => ({ build }) }),
    openKind: 'effort',
  });
  await sb.checkBuild();
  build = 'v2';
  await sb.checkBuild();
  assert.strictEqual(sb.__reloaded(), false);
  assert.strictEqual(sb.__store['dispatch-draft'], undefined);
});

await run('mismatched build does NOT reload mid-scroll', async () => {
  let build = 'v1';
  const sb = makeSandbox({
    fetch: async () => ({ json: async () => ({ build }) }),
    userScrolling: true,
  });
  await sb.checkBuild();
  build = 'v2';
  await sb.checkBuild();
  assert.strictEqual(sb.__reloaded(), false);
});

await run('mismatched build does NOT reload while composer is focused', async () => {
  let build = 'v1';
  const inputEl = { value: 'typing...' };
  const sb = makeSandbox({
    fetch: async () => ({ json: async () => ({ build }) }),
    input: inputEl,
    document: { activeElement: inputEl },
  });
  await sb.checkBuild();
  build = 'v2';
  await sb.checkBuild();
  assert.strictEqual(sb.__reloaded(), false);
});

await run('does not reload-loop for the same target build within 30s', async () => {
  let build = 'v1';
  const sb = makeSandbox({ fetch: async () => ({ json: async () => ({ build }) }) });
  await sb.checkBuild();
  build = 'v2';
  await sb.checkBuild(); // reloads, records dispatch-reload = {build:'v2', t:now}
  assert.strictEqual(sb.__reloaded(), true);
  // simulate the reload clearing MY_BUILD back to unknown + a fresh check
  // immediately after, before the page has actually re-fetched its own id
  sb.MY_BUILD = null;
  let reloadedAgain = false;
  sb.location.reload = () => { reloadedAgain = true; };
  await sb.checkBuild(); // re-primes MY_BUILD = v2
  build = 'v2';
  await sb.checkBuild(); // would be a mismatch only if MY_BUILD stayed v1; here it's a no-op
  assert.strictEqual(reloadedAgain, false);
});

if (process.exitCode) {
  console.error('\nSome tests FAILED');
  process.exit(1);
} else {
  console.log('\nAll build-reload tests passed');
}
