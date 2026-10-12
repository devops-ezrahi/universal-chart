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

test('extra YAML with its own image block gets no second image key', () => {
  const files = fleet({
    tag: '1.0.0',
    ns: [{ name: 'dev', extra: 'image:\n  tag: 3.0.0' }, { name: 'qa', extra: 'image:\n  pullPolicy: Always' }],
    rows: [],
  });
  for (const f of ['values-dev.yaml', 'values-qa.yaml'])
    expect(files[f].match(/^image:/gm)).toHaveLength(1);
  expect(files['values-dev.yaml']).toMatch(/tag: 3\.0\.0/);
  const warn = window.checks().find(c => /qa.*without a <code>tag<\/code>/.test(c.t));
  expect(warn && warn.l).toBe('warn');
  expect(window.checks().some(c => /dev.*without a <code>tag/.test(c.t))).toBe(false);
});

test('a key set by both a matrix row and the extra YAML blocks', () => {
  fleet({
    tag: '1.0.0',
    ns: [{ name: 'dev', extra: 'resources:\n  limits:\n    cpu: 1' }],
    rows: [{ path: 'resources.requests.cpu', vals: { dev: '100m' } }],
  });
  expect(window.checks().some(c => c.l === 'bad' && /values-dev\.yaml<\/code> would have <code>resources<\/code> twice/.test(c.t))).toBe(true);
});

test('a numeric-looking tag stays a string', () => {
  const files = fleet({ tag: '8', ns: [{ name: 'a' }, { name: 'b' }],
                        rows: [{ path: 'image.tag', vals: { a: '7' } }, { path: 'replicaCount', vals: { b: '2' } }] });
  expect(files['values-a.yaml']).toMatch(/tag: ["']7["']/);
  expect(files['values-b.yaml']).toMatch(/tag: ["']8["']/);
  expect(files['values-b.yaml']).toMatch(/replicaCount: 2$/m);
});
