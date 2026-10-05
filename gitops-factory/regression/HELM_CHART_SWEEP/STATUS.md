# STATUS — HELM_CHART_SWEEP

**Result: FIXED.** 57 public Helm charts (31 Bitnami, 26 upstream: ingress-nginx,
cert-manager, Argo CD/Rollouts/Workflows, Traefik, Vault, Harbor, KEDA, Gatekeeper,
Velero, …) were rendered with `helm template`, split by `namespace_importer` the way
the devops-portal's Convert does it, converted with `--verify-fidelity`, and scored.

Before: 3 charts failed outright (alertmanager, harbor, kafka), 202 fidelity ERRORs.
After: 57/57 convert and render with no conflict, no missing object and no render
failure; 14 ERRORs remain, all `checksum/*` pod annotations on releases whose config
lives in `shared` (the chart can only hash a release's own configMaps/secrets).

| Defect | Where | Fix |
|---|---|---|
| Services kept in `shared` (StatefulSet headless, Kafka's client Service) selected no pods: the chart relabels pods per release | converter | `rewire_service_selectors`: pods a raw selector matched get `svc.universal-chart.io/<svc>: "true"`, the Service selects that; primary `service.selector` added to the chart |
| Pod `automountServiceAccountToken: true` under an SA set to `false` rendered `false` (Argo CD, Traefik, kube-state-metrics, …) | chart + converter | top-level `automountServiceAccountToken` for the pod, written only where it differs from the SA's |
| Objects the selective pull skips (argocd-cm, argocd-secret, Roles, cluster-scoped kinds) were lost from a paste | importer | `file_unclaimed`, called by the portal's paste adapter only |
| Two kinds of one name (fluentd DaemonSet + StatefulSet) — the second replaced the first | importer | `workload_keys` |
| Harbor's Ingress: per-path backends, numbered ports | chart + converter | `paths[].serviceName`, `paths[].portNumber` |
| `shared` (no pods) rendered a stray ServiceAccount, colliding with Kafka's | converter | `serviceAccount.create: false` for a podless release |
| Job/CronJob nodeSelector, affinity, tolerations, automount, enableServiceLinks, terminationGracePeriodSeconds, priorityClassName, probes, ports dropped; `ttlSecondsAfterFinished: 0` dropped by the chart's `with` | chart + converter | `carry_job_pod_fields`; `hasKey` in job/cronjob templates |
| envFrom: a ConfigMap and Secret of one name collapsed; order and `prefix` lost | chart + converter | `name`, `prefix`, `envFromOrder` |
| ClusterRole/Binding labels (aggregate-to-*) and annotations dropped; `aggregationRule` unsupported | chart + converter | carried |
| RoleBinding subject in ANOTHER namespace re-pointed at this one | converter | kept unless it is the binding's own |
| Service `trafficDistribution` dropped | chart + converter | carried |
| A trailing TAB after a scalar (alertmanager) crashed PyYAML | importer + converter | retry without trailing whitespace |
| Checker: ~150 null/API-default false positives; blind to init-container order and to a Service selecting nothing | checker | `KUBERNETES_DEFAULTS`, order check, selector check |

Not changed, deliberately: VCT identity labels stay excluded (the StatefulSet
controller stamps its selector labels onto every PVC), now reported as benign.
