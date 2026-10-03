# Argo CD cutover e2e

`argo_cutover_e2e.py` proves, on a real Argo CD, that moving a namespace from
an old Argo CD app tree onto this chart deletes nothing and restarts nothing:

1. **Old world.** An app-of-apps → ApplicationSet → one Application per
   microservice, from raw manifests shaped like the live objects that prompted
   this:
   - a selector the release name does not produce;
   - env vars interleaved with `secretKeyRef`s;
   - no ServiceAccount and `securityContext: {}`;
   - a Route without `insecureEdgeTerminationPolicy`;
   - a StatefulSet with a `volumeClaimTemplate`.

   Then one `kubectl rollout restart`.
2. **Convert.** The live objects are dumped and converted with
   `convert_to_universal_chart.py` at `--chart-ref`.
3. **Teardown.** `scripts/argo-orphan-tree.sh` removes every old Application and
   ApplicationSet. Every object must keep its UID.
4. **Adopt.** `ms-applicationSet` at `--chart-ref` is applied with auto-sync
   off.
   - `argocd app diff` may show only Argo's tracking label.
   - After the sync, pod UIDs, ReplicaSets and StatefulSet revisions must be
     unchanged, and the apps must be Synced/Healthy.

`--expect restart` is the negative control. Run against a chart from before the
fix (`--chart-ref origin/main`), it must see the diff or the failed sync, so it
proves the test can tell a broken chart from a fixed one.

```bash
python3 githttp.py /tmp/argo-cutover-e2e/git 8765 0.0.0.0 &      # the git server Argo reads from
python3 argo_cutover_e2e.py --ns cutover --chart-ref dev --expect no-restart \
  --git-root /tmp/argo-cutover-e2e/git --git-url http://<node-ip>:8765
python3 argo_cutover_e2e.py --ns cutover-neg --chart-ref origin/main --expect restart \
  --git-root /tmp/argo-cutover-e2e/git --git-url http://<node-ip>:8765
```

## Requirements

- A cluster: kubectl on `KUBECONFIG`.
- Argo CD 2.6 or later in `argocd`, because `ms-applicationSet` uses multi-source
  apps, with `application.instanceLabelKey: argocd.argoproj.io/instance` and
  `server.insecure: "true"`.
- The OpenShift Route CRD.
- On the host: the `argocd` CLI, `helm`, `git` and PyYAML.

The script runs on a cluster node: it reaches the `argocd-server` ClusterIP
directly. It also plays the OpenShift router, marking Routes admitted, since a
plain cluster has none.

## Where it was run (2026-10)

The environment was a Claude Code cloud VM (cgroup v1, Firecracker), with
k3s v1.31 using Docker as its runtime (`k3s server --docker`) and Argo CD
v2.13.0. Three workarounds were needed there and will not be needed on a
normal machine (kind works too):

- The VM refuses a negative `oom_score_adj`, and the kubelet gives every pod
  sandbox -998. Docker's default runtime was a wrapper that clamps it to 0
  before calling runc.
- quay.io was unreachable, so the `quay.io/argoproj/argocd:v2.13.0` image was
  built locally from the release's multi-call `argocd` binary plus git, gpg and
  helm, with `ARGOCD_GPG_ENABLED=false` on the repo-server.
- Images were preloaded into Docker (`imagePullPolicy: IfNotPresent`).

Results:

| run | diff beyond the tracking label | pods / RS / STS revision | sync |
|---|---|---|---|
| `dev` (8f1ca27) | Route only: `argocd.argoproj.io/sync-options: Force=true,Replace=true` | unchanged | Synced/Healthy; the Route was deleted and recreated |
| `origin/main` (eae24a5), the negative control | 59 + 30 lines | unchanged, because the sync was refused | **Failed**: `spec.selector` immutable, StatefulSet spec forbidden |

On `dev`, the teardown removed the whole old tree with 7/7 objects keeping
their UIDs.
