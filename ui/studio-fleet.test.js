const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

// Fleet mode: one shared values.yaml, one values-<ns>.yaml per namespace.
// A namespace with nothing set is not deployed, and image.tag is never in
// the shared base.

let window;

function fleet({ tag, ns, rows }) {
  const html = fs.readFileSync(path.join(__dirname, 'studio.html'), 'utf-8');
  window = new JSDOM(html, { runScripts: 'dangerously', pretendToBeVisual: true, url: 'http://localhost' }).window;
  window.eval(`
    state.mode = 'fleet';
    state.f.workload = { on: true, v: { type: 'deployment' } };
    state.f.image = { on: true, v: { repository: 'registry.example.com/app'${tag ? `, tag: '${tag}'` : ''} } };
    state.fleet = ${JSON.stringify({ ns, rows, release: 'app' })};
  `);
  const files = window.fleetFiles();
  return Object.fromEntries(files.map(f => [f.name, f.text]));
}

test('image.tag stays out of the base and goes to each deployed namespace', () => {
  const files = fleet({
    tag: '1.0.0',
    ns: [{ name: 'dev' }, { name: 'prod' }],
    rows: [{ path: 'image.tag', vals: { dev: '2.0.0', prod: '' } },
           { path: 'replicaCount', vals: { prod: '3' } }],
  });
  expect(files['values.yaml']).toContain('registry.example.com/app');
  expect(files['values.yaml']).not.toMatch(/^\s*tag:/m);
  expect(files['values-dev.yaml']).toMatch(/tag: "?2\.0\.0"?/);
  // prod pins no tag of its own: the base's tag moves into its file.
  expect(files['values-prod.yaml']).toMatch(/tag: "?1\.0\.0"?/);
});

test('a namespace with nothing set gets no file and no release', () => {
  const files = fleet({
    tag: '1.0.0',
    ns: [{ name: 'dev' }, { name: 'staging' }, { name: 'prod', extra: 'ingress:\n  enabled: true' }],
    rows: [{ path: 'image.tag', vals: { dev: '2.0.0' } }],
  });
  expect(Object.keys(files).sort()).toEqual(['deploy.sh', 'values-dev.yaml', 'values-prod.yaml', 'values.yaml']);
  expect(files['deploy.sh']).toContain('--namespace dev ');
  expect(files['deploy.sh']).toContain('--namespace prod ');
  expect(files['deploy.sh']).not.toContain('--namespace staging');
  expect(files['deploy.sh']).toContain('# Not deployed — nothing set for them: staging');
  const warn = window.checks().find(c => /Not deployed to/.test(c.t));
  expect(warn && warn.l).toBe('warn');
});

test('nothing set anywhere deploys nothing and blocks', () => {
  const files = fleet({ tag: '1.0.0', ns: [{ name: 'dev' }], rows: [] });
  expect(Object.keys(files).sort()).toEqual(['deploy.sh', 'values.yaml']);
  expect(window.checks().some(c => c.l === 'bad' && /No namespace has anything set/.test(c.t))).toBe(true);
});
