// End-to-end test for multi-app-manager.html: loads the real HTML/JS into a
// jsdom window, wraps a REAL directory on disk (a copy of converter output)
// behind a minimal File System Access API-compatible adapter, and drives
// the actual app code (button clicks, form fills) exactly like a browser
// would. Needs jsdom (npm install jsdom); core-logic.test.js needs nothing.
//
// Run: node ui/e2e-test.js <path-to-app-dir-copy>

const fs = require("fs");
const path = require("path");
const { JSDOM } = require("jsdom");
const assert = require("assert");

const appDirPath = process.argv[2];
if (!appDirPath) {
  console.error("Usage: node e2e-test.js <path-to-app-dir>");
  process.exit(1);
}

// --- Minimal File System Access API adapter backed by real fs ---
class NodeFileHandle {
  constructor(fullPath) {
    this.kind = "file";
    this.fullPath = fullPath;
  }
  async getFile() {
    const fullPath = this.fullPath;
    return { text: async () => fs.promises.readFile(fullPath, "utf8") };
  }
  async createWritable() {
    const fullPath = this.fullPath;
    let buffer = "";
    return {
      write: async (text) => { buffer += text; },
      close: async () => { await fs.promises.writeFile(fullPath, buffer, "utf8"); },
    };
  }
}

class NodeDirHandle {
  constructor(fullPath, name) {
    this.kind = "directory";
    this.fullPath = fullPath;
    this.name = name || path.basename(fullPath);
  }
  async *entries() {
    let names;
    try {
      names = await fs.promises.readdir(this.fullPath);
    } catch (e) {
      return;
    }
    for (const name of names) {
      const full = path.join(this.fullPath, name);
      const st = await fs.promises.stat(full);
      yield [name, st.isDirectory() ? new NodeDirHandle(full, name) : new NodeFileHandle(full)];
    }
  }
  async getFileHandle(name, opts) {
    const full = path.join(this.fullPath, name);
    if (!fs.existsSync(full)) {
      if (opts && opts.create) {
        await fs.promises.writeFile(full, "", "utf8");
      } else {
        throw new Error("NotFoundError");
      }
    }
    return new NodeFileHandle(full);
  }
  async getDirectoryHandle(name, opts) {
    const full = path.join(this.fullPath, name);
    if (!fs.existsSync(full)) {
      if (opts && opts.create) {
        await fs.promises.mkdir(full, { recursive: true });
      } else {
        throw new Error("NotFoundError");
      }
    }
    return new NodeDirHandle(full, name);
  }
}

(async () => {
  const html = fs.readFileSync(path.join(__dirname, "multi-app-manager.html"), "utf8");
  const dom = new JSDOM(html, { runScripts: "dangerously", resources: "usable" });
  const { window } = dom;

  // Let the inline <script> tags execute (jsdom runs them synchronously on
  // parse when runScripts:"dangerously" is set + resources:"usable").
  await new Promise((resolve) => {
    if (window.document.readyState === "complete") resolve();
    else window.addEventListener("load", resolve);
  });

  assert.ok(window.jsyaml, "jsyaml should be loaded on window");

  // Function DECLARATIONS at top level of a classic <script> DO become
  // window properties (like var); top-level `const state` does NOT — that's
  // why the app explicitly does `window.__state = state` for testability.
  const win = window;
  assert.strictEqual(typeof win.readAppTree, "function", "readAppTree should be a global function");
  assert.strictEqual(typeof win.deepMerge, "function", "deepMerge should be a global function");
  assert.ok(win.__state, "window.__state should be exposed for testing");

  // The app dir is a copy of converter output with variant folders, e.g.
  //   python convert_to_universal_chart.py --input regression/ERRORS_NEW_ANALYSIS/Env_Suffix_Folders/fixture \
  //     --output <dir> --env-group color=black,yellow --env-suffixes dev,prd --skip-verify
  // with dev/black/values/ms1.yaml moved into dev/black/values/group1/ first.
  const dirHandle = new NodeDirHandle(appDirPath);
  const app = await win.readAppTree(dirHandle);

  for (const key of ["dev", "dev/yellow", "dev/black", "prd/yellow"]) {
    assert.ok(app.namespaces[key], `${key} folder should be read`);
  }
  assert.ok(app.namespaces["dev/black"].microservices["ms1"], "ms1 inside values/group1/ should be read");
  assert.ok(app.namespaces["dev"].microservices["ms2"], "ms2 in the namespace folder should be read");
  assert.ok(!app.namespaces["base"], "base/ is not a folder of releases");
  console.log("readAppTree: OK —", Object.keys(app.namespaces).length, "folders,",
    Object.keys(app.base).length, "base files");

  win.__state.apps.push(app);
  win.__state.activeAppIndex = 0;
  win.__state.activeNamespace = "dev/yellow";
  win.__state.activeMicroservice = "ms1";
  win.__state.activeView = "effective";
  win.renderAll();

  // base -> dev/yellow/defaults.yaml -> dev/yellow/values/ms1.yaml
  const merged = win.jsyaml.load(win.document.querySelector("pre.yaml").textContent);
  assert.strictEqual(merged.image.repository, "registry.example.org/ms1");
  assert.strictEqual(merged.image.tag, "1.0.0");
  assert.strictEqual(merged.color, "yellow", "the folder's defaults.yaml layer is merged");
  assert.strictEqual(merged.fullnameOverride, "ms1-{{ .Values.color }}", "base/ layer is merged");
  console.log("show effective: OK — base -> defaults -> values");

  win.__state.activeView = "form";
  win.renderAll();
  const repoInput = win.document.querySelector("fieldset input[placeholder='my-registry/checkout']");
  assert.strictEqual(repoInput.value, "registry.example.org/ms1", "form should prefill image repository");
  console.log("form editor prefill for EXISTING microservice: OK");

  // Saving a folder's values file keeps it in its values/ sub-folder.
  win.__state.activeNamespace = "dev/black";
  await win.saveRawValues("ms1", "minimal", { image: { tag: "9.9.9" } });
  const grouped = path.join(appDirPath, "dev/black/values/group1/ms1.yaml");
  assert.strictEqual(win.jsyaml.load(fs.readFileSync(grouped, "utf8")).image.tag, "9.9.9");
  assert.ok(!fs.existsSync(path.join(appDirPath, "dev/black/values/ms1.yaml")), "no second copy next to the group folder");
  console.log("save values file inside a group sub-folder: OK");

  // "+ New microservice" writes base/<name>.yaml and the folder's values file.
  async function create(folder, name) {
    win.__state.activeNamespace = folder;
    win.__state.activeMicroservice = { __new: true };
    win.__state.activeView = "form";
    win.renderAll();
    win.document.querySelectorAll("fieldset")[0].querySelectorAll("input")[0].value = name;
    win.document.querySelector("fieldset input[placeholder='my-registry/checkout']").value = "nginx";
    win.document.querySelector("fieldset input[placeholder='1.0.0']").value = "1.0.0";
    win.document.querySelector("input[placeholder='checkout.apps.example.com']").value = "e2e.apps.example.com";
    const btn = Array.from(win.document.querySelectorAll("button")).find((b) => b.textContent === "Create microservice");
    assert.ok(btn, "Create microservice button should exist");
    await btn.onclick();
  }
  await create("dev/yellow", "e2e-test-service");
  const basePath = path.join(appDirPath, "base", "e2e-test-service.yaml");
  const parsedBase = win.jsyaml.load(fs.readFileSync(basePath, "utf8"));
  const parsedValues = win.jsyaml.load(fs.readFileSync(path.join(appDirPath, "dev/yellow/values/e2e-test-service.yaml"), "utf8"));
  assert.strictEqual(parsedBase.image.repository, "nginx");
  assert.strictEqual(parsedBase.route.host, "e2e.apps.example.com");
  assert.strictEqual(parsedValues.image.tag, "1.0.0");
  assert.ok(!fs.existsSync(path.join(appDirPath, "dev/yellow/releases")), "no releases/ pointer files any more");
  console.log("new microservice create: OK — base/ + dev/yellow/values/");

  // The same release in another folder reuses the shared base untouched.
  const before = fs.readFileSync(basePath, "utf8");
  await create("prd/black", "e2e-test-service");
  assert.strictEqual(fs.readFileSync(basePath, "utf8"), before, "existing base/ must be reused, not rewritten");
  assert.ok(fs.existsSync(path.join(appDirPath, "prd/black/values/e2e-test-service.yaml")));
  console.log("second folder reuses the shared base: OK");

  // A tag hand-written into the shared base/ moves out on the next save: into
  // the saving folder's values/ file and into every other folder that was
  // relying on it, so no deployment changes image.
  fs.writeFileSync(basePath, win.jsyaml.dump({ ...parsedBase, image: { repository: "nginx", tag: "7.7.7" } }));
  const prdValues = path.join(appDirPath, "prd/black/values/e2e-test-service.yaml");
  fs.writeFileSync(prdValues, win.jsyaml.dump({ replicaCount: 2 }));
  win.__state.apps[0] = await win.readAppTree(new NodeDirHandle(appDirPath));
  win.__state.activeNamespace = "dev/yellow";
  win.__state.activeMicroservice = "e2e-test-service";
  win.__state.activeView = "form";
  win.renderAll();
  const save = Array.from(win.document.querySelectorAll("button")).find((b) => b.textContent === "Save");
  assert.ok(save, "Save button should exist");
  await save.onclick();
  const baseAfter = win.jsyaml.load(fs.readFileSync(basePath, "utf8"));
  assert.strictEqual(baseAfter.image.tag, undefined, "base/ must not keep an image tag");
  assert.strictEqual(baseAfter.image.repository, "nginx");
  assert.strictEqual(win.jsyaml.load(fs.readFileSync(prdValues, "utf8")).image.tag, "7.7.7");
  assert.strictEqual(win.jsyaml.load(fs.readFileSync(prdValues, "utf8")).replicaCount, 2);
  console.log("image tag moved out of the shared base: OK");

  console.log("\nAll e2e checks passed.");
})().catch((e) => {
  console.error("E2E TEST FAILED:", e);
  process.exit(1);
});
