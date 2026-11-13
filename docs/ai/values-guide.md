# Writing values for the universal chart

Current, verified rules for chart v4. When this file and `COOKBOOK.md` disagree,
this file and the templates win. Every snippet here matches
`docs/ai/examples/*.yaml`, which render cleanly.

## 1. Naming

| Value | Effect |
|---|---|
| `fullnameOverride` | Name of the workload, primary Service, SA, HPA, PDB, Ingress, Route. **Always set it** in GitOps values. |
| `nameOverride` | Same, but loses to `fullnameOverride`. |
| neither | The Helm release name. |
| `containerName` | Main container name (default: release name). Set it to keep `kubectl logs -c` and log selectors working. |
| map keys of `configMaps`, `secrets`, `pvc`, `cronjobs`, `jobs`, `services`, `routes`, `rbac.*`, `externalSecrets`, ... | The object's real name, verbatim. |
| `service.name`, `routes.<n>.name` | Render a Service or Route under a different real name. |

Cluster-scoped objects (`rbac.clusterRoles`, `storageClasses`,
`persistentVolumes`, `scc`, `clusterSecretStores`) are global. Two releases
defining the same name fight over one object, so define each one once.

## 2. Shapes: map or list

Maps (key = name): `ports`, `env`, `envFrom`, `volumes`, `initContainers`,
`sidecars`, `service.ports`, `services`, `routes`, `configMaps`, `secrets`,
`pvc`, `persistentVolumes`, `storageClasses`, `volumeClaimTemplates`,
`cronjobs`, `jobs`, `networkPolicies`, `scc`, `secretStores`,
`clusterSecretStores`, `externalSecrets`, `rbac.roles`, `rbac.roleBindings`,
`rbac.clusterRoles`, `rbac.clusterRoleBindings`.

Lists (no `name` to key on, or order is the point): `volumeMounts` (rendered in
written order; a later file replaces the whole list), `command`, `args`,
`tolerations`, `imagePullSecrets`, `topologySpreadConstraints`,
`hostAliases`, `extraDeploy`, `ingress.hosts`, `ingress.tls`, rule lists inside
roles, `subjects`.

```yaml
# WRONG (old format, fails the schema: "got array, want object")
ports:
  - name: http
    containerPort: 8080
# RIGHT
ports:
  http:
    containerPort: 8080
```

`ports.<n>.unnamed: true` renders a port with no `name` (the key is bookkeeping only).

## 3. Environment variables

```yaml
env:
  LOG_LEVEL:
    value: info
  DB_PASSWORD:
    valueFrom:
      secretKeyRef: {name: orders-db, key: password}
  POD_NAME:
    valueFrom:
      fieldRef: {fieldPath: metadata.name}
envOrder: [BASE_URL]        # only when a later var uses $(BASE_URL)
envFrom:
  orders-api-config: {type: configMapRef}
  orders-secrets:    {type: secretRef, optional: true}
envFromOrder: []            # when two sources share a key (later wins)
```

- Env vars render sorted by name, with `envOrder` names first.
- A later values file overrides one var, or removes it with `LOG_LEVEL: null`.
- Switching a var from `value` to `valueFrom` in a later file needs
  `value: null` alongside, or the render fails and names the var.
- The shorthand `NAME: "x"` / `NAME: {secretKeyRef: ...}` still renders, but
  don't write it. The converter never does.
- A Kubernetes-style list still renders as written, but it can't be partially
  overridden. Avoid it.
- Sidecars, init containers and `cronjobs.<n>.jobTemplate` take the same `env` map.
- A ConfigMap from the namespace's `configMaps/` folder (its own Application,
  see `gitops-and-converter.md`) is used like any existing ConfigMap: by name,
  `envFrom: {env1: {type: configMapRef}}` or a `volumes` entry with
  `configMap: {name: namespace-conf}`. Don't also define it in the release's
  `configMaps`, because two Applications would own one object. `checksums` doesn't
  see it, so a change there doesn't roll the pods.

## 4. Workloads

`workload.type`: `deployment` (default) | `statefulset` | `daemonset` | `none`.

- `none`: only config, RBAC, secrets and jobs. `hpa`, `vpa` and `pdb` fail validation with it.
- `hpa.enabled: true` drops `spec.replicas`. It isn't valid for `daemonset`/`none`.
- StatefulSet: set `serviceName` to a **headless** Service (`service.clusterIP: None`),
  use `volumeClaimTemplates` for per-pod storage, and set
  `persistentVolumeClaimRetentionPolicy` explicitly for data you can't lose.
- Image: `image.repository` + `image.tag` (default `Chart.appVersion`), or
  `image.digest` (wins over the tag).
- `securityContext: null` (default) renders nothing. `{}` renders an empty one.
- `checksums.enabled: true` adds checksum annotations so pods roll when the
  chart's ConfigMaps or Secrets change. It is off by default.

## 5. Volumes and mounts

```yaml
volumes:
  app-props:
    configMap: {name: orders-api-config}
  scratch:
    emptyDir: {}
  data:
    persistentVolumeClaim: {claimName: orders-data}
volumeMounts:
  - name: app-props
    mountPath: /etc/orders
    readOnly: true
pvc:
  orders-data:
    accessModes: [ReadWriteOnce]
    size: 10Gi
```

The render fails if a mount names a volume that isn't defined (`volumes`,
`volumeClaimTemplates`, ...). This is checked per workload and per job.

## 6. Networking

- `service.enabled: true` + `service.ports.<name>: {port, targetPort}`.
  No default port exists. Declare ports explicitly.
- Extra Services: `services.<name>: {enabled: true, ports: {...}}`.
- `ingress.hosts[].paths[]` takes `portName` or `portNumber`, and optionally
  `serviceName` (default: the primary Service).
- OpenShift: `route.enabled/host/targetPort/tls`, plus extra `routes.<name>`.
  `insecureEdgeTerminationPolicy` renders only when set.
- `networkPolicies.<name>`: the spec as written.

## 7. Jobs and CronJobs

```yaml
cronjobs:
  orders-nightly-cleanup:
    schedule: "0 2 * * *"
    concurrencyPolicy: Forbid     # default
    jobTemplate:
      command: ["/bin/cleanup.sh"]
      env: {DRY_RUN: {value: "false"}}
      # image, serviceAccountName, podSecurityContext, securityContext,
      # volumes, volumeMounts, initContainers, backoffLimit, parallelism ...
jobs:
  orders-db-migrate:
    command: ["/bin/migrate.sh"]
    annotations:
      argocd.argoproj.io/hook: PreSync
```

A job with no `image` uses the release `image`. `0` and `false` are honoured
(`backoffLimit: 0`, `successfulJobsHistoryLimit: 0`).

## 8. Secrets

- `secrets.<name>: {type, stringData | data}`: plain Secrets. Avoid
  committing real secret values to git.
- External Secrets Operator: `externalSecrets.<name>` (creates a Secret of that
  name; `target.name` overrides it), with `secretStoreRef.kind` `SecretStore` or
  `ClusterSecretStore`. `secretStores` / `clusterSecretStores` render the stores.
  The chart doesn't install the operator.

## 9. Labels, annotations, links

- `commonLabels` and `commonAnnotations` go on every object. Each object's own
  `labels`/`annotations` merge in.
- `chartLabels: false` (default): no `app.kubernetes.io/*` identity labels.
  Selectors still come from `selectorLabels`, which you override **only** to
  match an existing live workload, since selectors are immutable.
- `podLabels` / `podAnnotations` sit on the pod template, so changing them restarts pods.
- `links.*` is filled in by ms-applicationSet. Leave it out of values files.

## 10. Templating inside values

```yaml
# base/orders-api.yaml (shared across namespaces)
ingress:
  hosts:
    - host: orders.{{ .Values.environment }}.example.com
# dev/defaults.yaml
environment: dev
```

Map keys are templated too (`certs-{{ .Values.color }}`). A key that renders
onto an existing key fails the render. Escape a literal `{{` as `{{ "{{" }}`.

## 11. Escape hatch

`extraDeploy`: a list of raw manifests (each `tpl`'d) for kinds the chart
doesn't model. Use it last. Prefer a real values key when one exists.

## 12. Pitfalls checklist

- [ ] Lists written where maps are required (old COOKBOOK style)
- [ ] `service.enabled` / `serviceAccount.create` forgotten (both default off)
- [ ] `fullnameOverride` missing, so names follow the Argo CD Application name
- [ ] HPA on with `replicaCount` expected to matter
- [ ] StatefulSet `serviceName` pointing at a non-headless Service
- [ ] Unescaped `{{` in ConfigMap data
- [ ] A cronjob key written as a short suffix, expecting a release prefix
- [ ] A changed selector or pod-template field on a live workload (immutable, or a restart)
- [ ] A key invented that exists nowhere in `values.yaml`/schema/templates

Always finish with `helm template ... -f <files>` and read the output.
