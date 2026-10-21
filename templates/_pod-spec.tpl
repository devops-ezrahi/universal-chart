{{/*
Init containers, in the order they must run.

The values are a map (so GitOps layers can patch one entry), and Helm ranges a
map in SORTED key order — so an init container that has to run before an
alphabetically-earlier one got reordered and the pod crashed. `order` (the
converter's initContainerOrder) lists names in the raw manifest's order; keys
it does not name follow in sorted order, so a hand-added entry is never lost.
Usage: {{- include "universal-chart.initContainers" (dict "containers" .Values.initContainers "order" .Values.initContainerOrder) | nindent 2 }}
*/}}
{{- define "universal-chart.initContainers" -}}
{{- $map := .containers | default dict }}
{{- $order := .order | default list }}
{{- range $name := $order }}
{{- if hasKey $map $name }}
- name: {{ $name }}
  {{- toYaml (index $map $name) | nindent 2 }}
{{- end }}
{{- end }}
{{- range $name, $spec := $map }}
{{- if not (has $name $order) }}
- name: {{ $name }}
  {{- toYaml $spec | nindent 2 }}
{{- end }}
{{- end }}
{{- end }}

{{/*
envFrom refs in the order they apply. Later sources win a key both define, so
order is meaning — and Helm ranges a map in sorted key order. `order` (the
converter's envFromOrder) names keys in the raw order; the rest follow sorted.
*/}}
{{- define "universal-chart.envFrom" -}}
{{- $refs := .refs | default dict }}
{{- $order := .order | default list }}
{{- $keys := list }}
{{- range $k := $order }}{{- if hasKey $refs $k }}{{- $keys = append $keys $k }}{{- end }}{{- end }}
{{- range $k, $_ := $refs }}{{- if not (has $k $order) }}{{- $keys = append $keys $k }}{{- end }}{{- end }}
{{- range $name := $keys }}
{{- $spec := index $refs $name | default dict }}
{{- /* `name` when the key cannot be the object's name: a ConfigMap and a
       Secret of ONE name (Harbor's harbor-core) need two keys. */}}
- {{ $spec.type | default "configMapRef" }}:
    name: {{ $spec.name | default $name }}
    {{- if hasKey $spec "optional" }}
    optional: {{ $spec.optional }}
    {{- end }}
  {{- with $spec.prefix }}
  prefix: {{ . | quote }}
  {{- end }}
{{- end }}
{{- end }}

{{/*
=============================================================================
SHARED POD SPEC — used by Deployment, StatefulSet, DaemonSet
=============================================================================
Call: {{- include "universal-chart.podSpec" . | nindent 6 }}
*/}}
{{- define "universal-chart.podSpec" -}}
{{- /* Rendered only when the release names or creates a ServiceAccount. A pod
       that never set one runs as `default` anyway, but rendering
       `serviceAccountName: default` onto a template that never had it is still
       a pod-template change: migrating such a workload onto this chart rolled
       every pod for no reason. */}}
{{- if or .Values.serviceAccount.create .Values.serviceAccount.name }}
serviceAccountName: {{ include "universal-chart.serviceAccountName" . }}
{{- end }}
{{- /* The POD's token mount, which is not the ServiceAccount's. Hardened charts
       (Bitnami, Argo CD, Traefik) set the SA to false and the pod to true, so the
       one serviceAccount value cannot say both — and a controller that inherited
       the SA's `false` lost its API token. A top-level automountServiceAccountToken
       wins when present; without it the pod follows the SA's, and with neither set
       it is omitted, as on a manifest that never set it (false must still render). */}}
{{- if hasKey .Values "automountServiceAccountToken" }}
automountServiceAccountToken: {{ .Values.automountServiceAccountToken }}
{{- else if kindIs "bool" .Values.serviceAccount.automountServiceAccountToken }}
automountServiceAccountToken: {{ .Values.serviceAccount.automountServiceAccountToken }}
{{- end }}

{{- /* ── Image pull secrets ─────────────────────────── */}}
{{- with .Values.imagePullSecrets }}
imagePullSecrets:
  {{- toYaml . | nindent 2 }}
{{- end }}

{{- /* ── Pod security context ──────────────────────── */}}
{{- with .Values.podSecurityContext }}
securityContext:
  {{- toYaml . | nindent 2 }}
{{- end }}

{{- /* ── Termination & DNS ────────────────────────── */}}
terminationGracePeriodSeconds: {{ .Values.terminationGracePeriodSeconds }}
{{- if .Values.hostNetwork }}
hostNetwork: true
{{- end }}
{{- if .Values.hostIPC }}
hostIPC: true
{{- end }}
{{- if .Values.hostPID }}
hostPID: true
{{- end }}
{{- /* Rendered only when TRUE, unlike enableServiceLinks below, and the
       asymmetry is deliberate. This field's Kubernetes default is false, so an
       explicit `false` changes nothing; and unlike enableServiceLinks it has a
       declared default in values.yaml, so `hasKey` is always true and would
       stamp `shareProcessNamespace: false` onto every pod the chart renders —
       inventing a field on workloads whose source never mentioned it. The
       `false` diff is suppressed in the checker instead
       (_is_benign_default_loss), which is the one place the distinction can be
       made without changing the chart's public values API. */}}
{{- if .Values.shareProcessNamespace }}
shareProcessNamespace: true
{{- end }}
{{- /* hasKey, not `if` — and the difference is load-bearing here in a way it is
       not for the three booleans above. Their Kubernetes default is FALSE, so
       rendering them only when true loses nothing. enableServiceLinks defaults
       to TRUE, so an explicit `false` is a deliberate opt-out and `if` would
       drop exactly the value that means something: the pods would come back
       with the {SVCNAME}_SERVICE_HOST/_PORT variables the source manifest
       switched off, and an injected name can silently override an application's
       own env var. */}}
{{- if hasKey .Values "enableServiceLinks" }}
enableServiceLinks: {{ .Values.enableServiceLinks }}
{{- end }}
{{- with .Values.dnsPolicy }}
dnsPolicy: {{ . }}
{{- end }}
{{- with .Values.dnsConfig }}
dnsConfig:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .Values.priorityClassName }}
priorityClassName: {{ . }}
{{- end }}
{{- with .Values.runtimeClassName }}
runtimeClassName: {{ . }}
{{- end }}
{{- with .Values.schedulerName }}
schedulerName: {{ . }}
{{- end }}
{{- with .Values.restartPolicy }}
restartPolicy: {{ . }}
{{- end }}
{{- with .Values.readinessGates }}
readinessGates:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .Values.hostAliases }}
hostAliases:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .Values.overhead }}
overhead:
  {{- toYaml . | nindent 2 }}
{{- end }}

{{- /* ── Init containers (map-based) ────────────────── */}}
{{- with .Values.initContainers }}
initContainers:
  {{- include "universal-chart.initContainers" (dict "containers" . "order" $.Values.initContainerOrder) | nindent 2 }}
{{- end }}

{{- /* ── Main container ───────────────────────────── */}}
containers:
  {{- include "universal-chart.mainContainer" . | nindent 2 }}
  {{- /* ── Sidecar containers (map-based) ──────────── */}}
  {{- $sidecars := .Values.sidecars | default dict }}
  {{- range $name := (include "universal-chart.orderedKeys" (dict "map" $sidecars "order" $.Values.sidecarOrder) | fromJsonArray) }}
  - name: {{ $name }}
    {{- toYaml (index $sidecars $name) | nindent 4 }}
  {{- end }}

{{- /* ── Volumes (map-based) ─────────────────────── */}}
{{- with .Values.volumes }}
volumes:
  {{- include "universal-chart.orderedMapToList" (dict "map" . "order" $.Values.volumeOrder) | nindent 2 }}
{{- end }}

{{- /* ── Scheduling ───────────────────────────────── */}}
{{- with .Values.nodeSelector }}
nodeSelector:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .Values.tolerations }}
tolerations:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .Values.affinity }}
affinity:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .Values.topologySpreadConstraints }}
topologySpreadConstraints:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- end }}


{{/*
=============================================================================
MAIN CONTAINER — all possible container spec fields
=============================================================================
*/}}
{{- define "universal-chart.mainContainer" -}}
{{- /* The raw manifest's own container name when the converter captured one.
       Falls back to the release name so a hand-written values file that never
       sets it keeps the previous behaviour. Named, not release-derived,
       because `kubectl logs -c`, log-pipeline container selectors and the
       `container` label on kubelet metrics all address it by name. */}}
- name: {{ .Values.containerName | default (include "universal-chart.name" .) }}
  image: {{ include "universal-chart.image" . }}
  imagePullPolicy: {{ .Values.image.pullPolicy }}

  {{- /* kindIs, not `with`: an explicit EMPTY securityContext ({}) is kept.
         The API server stores `securityContext: {}` on a container as given, so
         dropping it is a pod-template change that restarts the pods. Unset
         (the null default) still renders nothing. */}}
  {{- if kindIs "map" .Values.securityContext }}
  {{- if .Values.securityContext }}
  securityContext:
    {{- toYaml .Values.securityContext | nindent 4 }}
  {{- else }}
  securityContext: {}
  {{- end }}
  {{- end }}

  {{- with .Values.command }}
  command:
    {{- toYaml . | nindent 4 }}
  {{- end }}

  {{- with .Values.args }}
  args:
    {{- toYaml . | nindent 4 }}
  {{- end }}

  {{- with .Values.workingDir }}
  workingDir: {{ . }}
  {{- end }}

  {{- with .Values.ports }}
  ports:
    {{- include "universal-chart.orderedMapToList" (dict "map" . "order" $.Values.portOrder) | nindent 4 }}
  {{- end }}

  {{- with .Values.env }}
  env:
    {{- include "universal-chart.listOrMap" (dict "items" . "stringValues" true) | nindent 4 }}
  {{- end }}

  {{- with .Values.envFrom }}
  {{- $envFrom := . }}
  envFrom:
    {{- include "universal-chart.envFrom" (dict "refs" . "order" $.Values.envFromOrder) | trim | nindent 4 }}
  {{- end }}

  {{- with .Values.resources }}
  resources:
    {{- toYaml . | nindent 4 }}
  {{- end }}

  {{- with .Values.livenessProbe }}
  livenessProbe:
    {{- toYaml . | nindent 4 }}
  {{- end }}

  {{- with .Values.readinessProbe }}
  readinessProbe:
    {{- toYaml . | nindent 4 }}
  {{- end }}

  {{- with .Values.startupProbe }}
  startupProbe:
    {{- toYaml . | nindent 4 }}
  {{- end }}

  {{- with .Values.containerRestartRules }}
  containerRestartRules:
    {{- toYaml . | nindent 4 }}
  {{- end }}

  {{- /* The CONTAINER's own restartPolicy, not the pod's (.Values.restartPolicy,
         rendered by podSpec above). Distinct field, distinct key: `Always` on a
         container restarts it in place instead of failing the pod, and on an
         initContainer it is what makes that container a native sidecar. */}}
  {{- with .Values.containerRestartPolicy }}
  restartPolicy: {{ . }}
  {{- end }}

  {{- with .Values.lifecycle }}
  lifecycle:
    {{- toYaml . | nindent 4 }}
  {{- end }}

  {{- with .Values.volumeMounts }}
  volumeMounts:
    {{- include "universal-chart.listOrMap" (dict "items" .) | nindent 4 }}
  {{- end }}

  {{- with .Values.volumeDevices }}
  volumeDevices:
    {{- toYaml . | nindent 4 }}
  {{- end }}

  {{- if .Values.stdin }}
  stdin: true
  {{- end }}
  {{- if .Values.stdinOnce }}
  stdinOnce: true
  {{- end }}
  {{- if .Values.tty }}
  tty: true
  {{- end }}

  {{- with .Values.terminationMessagePath }}
  terminationMessagePath: {{ . }}
  {{- end }}
  {{- with .Values.terminationMessagePolicy }}
  terminationMessagePolicy: {{ . }}
  {{- end }}
{{- end }}


{{/*
=============================================================================
SHARED JOB POD SPEC — for CronJob / Job
=============================================================================
Call: {{- include "universal-chart.jobPodSpec" (dict "Values" $.Values "Chart" $.Chart "Release" $.Release "job" $job) }}
*/}}
{{- define "universal-chart.jobPodSpec" -}}
restartPolicy: {{ .job.restartPolicy | default "OnFailure" }}
{{- /* A Job/CronJob may run as a ServiceAccount that is not the release's own
       — a shared backup or maintenance identity. Prefer the per-job value and
       fall back to the release-level helper, so a job that names no SA keeps
       the previous behaviour. */}}
{{- if or .job.serviceAccountName .Values.serviceAccount.create .Values.serviceAccount.name }}
serviceAccountName: {{ .job.serviceAccountName | default (include "universal-chart.serviceAccountName" .) }}
{{- end }}
{{- /* Pod-level fields a Job's own manifest commonly sets — admission-webhook
       and CRD-upgrade hooks pin themselves to Linux nodes, opt out of service
       links, or run without a token. Each renders only when the job declares
       it (hasKey where false/0 is the meaningful value), so a job that names
       none renders exactly as before. */}}
{{- if hasKey .job "automountServiceAccountToken" }}
automountServiceAccountToken: {{ .job.automountServiceAccountToken }}
{{- end }}
{{- if hasKey .job "enableServiceLinks" }}
enableServiceLinks: {{ .job.enableServiceLinks }}
{{- end }}
{{- if hasKey .job "terminationGracePeriodSeconds" }}
terminationGracePeriodSeconds: {{ .job.terminationGracePeriodSeconds }}
{{- end }}
{{- with .job.priorityClassName }}
priorityClassName: {{ . }}
{{- end }}
{{- with .job.dnsPolicy }}
dnsPolicy: {{ . }}
{{- end }}
{{- with .job.hostAliases }}
hostAliases:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .Values.imagePullSecrets }}
imagePullSecrets:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- /* The job's OWN pod-level securityContext when it declares one. Falling back
       to the release-level value silently re-owns the job's files: a job asking
       for runAsUser/fsGroup 1000 would run as whatever the workload declared. */}}
{{- with (.job.podSecurityContext | default .Values.podSecurityContext) }}
securityContext:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .job.initContainers }}
initContainers:
  {{- include "universal-chart.initContainers" (dict "containers" . "order" $.job.initContainerOrder) | nindent 2 }}
{{- end }}
containers:
  {{- /* .job.name is the jobs/cronjobs MAP KEY, not the container's name.
         Prefer the captured container name, same as mainContainer above. */}}
  - name: {{ .job.containerName | default .job.name }}
    {{- /* A digest is the strongest pin and wins over a tag, mirroring
           universal-chart.image for the main container. Without this branch a
           digest-pinned Job silently falls back to Chart.AppVersion. */}}
    image: {{ if .job.image }}{{ if .job.image.digest }}{{ printf "%s@%s" .job.image.repository .job.image.digest }}{{ else if contains "@" .job.image.repository }}{{ .job.image.repository }}{{ else }}{{ printf "%s:%s" .job.image.repository (.job.image.tag | default .Chart.AppVersion) }}{{ end }}{{ else }}{{ include "universal-chart.image" . }}{{ end }}
    {{- /* The job's OWN pull policy when it declares one. Falling back to the
           release-level value silently downgrades a CronJob that asked for
           `Always`, so it never re-pulls a mutable tag. */}}
    imagePullPolicy: {{ (.job.image).pullPolicy | default .Values.image.pullPolicy }}
    {{- /* Container-level hardening. Not rendered here at all until folder 12,
           so a job's allowPrivilegeEscalation / readOnlyRootFilesystem /
           capabilities were dropped outright. */}}
    {{- if kindIs "map" .job.securityContext }}
    {{- if .job.securityContext }}
    securityContext:
      {{- toYaml .job.securityContext | nindent 6 }}
    {{- else }}
    securityContext: {}
    {{- end }}
    {{- end }}
    {{- /* Container-level, not the pod-level `restartPolicy` at the top of this
           helper — see the matching comment in mainContainer. */}}
    {{- with .job.containerRestartPolicy }}
    restartPolicy: {{ . }}
    {{- end }}
    {{- with .job.command }}
    command:
      {{- toYaml . | nindent 6 }}
    {{- end }}
    {{- with .job.args }}
    args:
      {{- toYaml . | nindent 6 }}
    {{- end }}
    {{- with .job.workingDir }}
    workingDir: {{ . }}
    {{- end }}
    {{- with .job.ports }}
    ports:
      {{- toYaml . | nindent 6 }}
    {{- end }}
    {{- range $probe := list "livenessProbe" "readinessProbe" "startupProbe" "lifecycle" }}
    {{- with index $.job $probe }}
    {{ $probe }}:
      {{- toYaml . | nindent 6 }}
    {{- end }}
    {{- end }}
    {{- with .job.env }}
    env:
      {{- include "universal-chart.listOrMap" (dict "items" . "stringValues" true) | nindent 6 }}
    {{- end }}
    {{- with .job.envFrom }}
    {{- $envFrom := . }}
    envFrom:
      {{- include "universal-chart.envFrom" (dict "refs" . "order" $.job.envFromOrder) | trim | nindent 6 }}
    {{- end }}
    {{- with .job.resources }}
    resources:
      {{- toYaml . | nindent 6 }}
    {{- end }}
    {{- with .job.volumeMounts }}
    volumeMounts:
      {{- include "universal-chart.listOrMap" (dict "items" .) | nindent 6 }}
    {{- end }}
  {{- $sidecars := .job.sidecars | default dict }}
  {{- range $name := (include "universal-chart.orderedKeys" (dict "map" $sidecars "order" $.job.sidecarOrder) | fromJsonArray) }}
  - name: {{ $name }}
    {{- toYaml (index $sidecars $name) | nindent 4 }}
  {{- end }}
{{- with .job.volumes }}
volumes:
  {{- include "universal-chart.orderedMapToList" (dict "map" . "order" $.job.volumeOrder) | nindent 2 }}
{{- end }}
{{- /* The job's own scheduling wins; without one it keeps the release's. */}}
{{- with (.job.nodeSelector | default .Values.nodeSelector) }}
nodeSelector:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with (.job.tolerations | default .Values.tolerations) }}
tolerations:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .job.affinity }}
affinity:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- end }}
