{{/*
=============================================================================
NAMING
=============================================================================
*/}}
{{/*
Identity name — NEVER the chart's own name (Chart.Name/.Chart.Name is
"super-universal-chart" for every single app that uses this chart, so
leaking it into a resource name or label is always a bug, never desired).
Defaults to Release.Name so the chart stays generic across any app/release
name; set nameOverride when you want something other than the release name
itself (e.g. the release name has extra GitOps plumbing baked into it).
*/}}
{{- define "universal-chart.name" -}}
{{- default .Release.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.

Resource names must come out as just the microservice's own name — never
Chart.Name, and never Release.Name UNLESS that's genuinely the name you
want (which is the common case: releases are usually one release per app).
GitOps release names sometimes carry extra namespace/app plumbing (e.g.
"analytics-svc-analytics-svc-billing"); set nameOverride/fullnameOverride
in that case. There is deliberately no fallback that appends Chart.Name —
this chart is reused by many differently-named apps, so its own name must
never leak into any of them.
*/}}
{{- define "universal-chart.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- default .Release.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}

{{/*
CronJob resource name — CronJob's own metadata.name must be <= 52 chars,
since Kubernetes appends an 11-char timestamp suffix to form the Job name
(itself <= 63 chars), which then gets a further 5-char suffix for the Pod
name. Used for both the primary fullname and the per-cronjob-key suffix
(.Values.cronjobs is map-based to support more than one CronJob per chart).
Usage: {{ include "universal-chart.cronJobName" (dict "root" $ "name" $name) }}
*/}}
{{/*
CronJob/Job names are prefixed with the release's fullname so that several of
them in one release cannot collide. But when the key IS the fullname — the
common case for a release holding a single CronJob named after itself —
prefixing produces a doubled name like "nightly-report-nightly-report": not
the name the source manifest used, so anything referencing it by name looks
for the wrong object, and it spends the length budget twice. That budget is
52 rather than 63 for CronJob because Kubernetes appends a timestamp when the
CronJob spawns a Job, so doubling makes silent truncation (and truncation
collisions) much likelier.

Skipping the prefix only on an exact match keeps the collision-avoidance
intact: two differently-named CronJobs in one release still get distinct
prefixed names. Same idea as Helm's own fullname helper avoiding
"release-release".
*/}}
{{- define "universal-chart.subresourceName" -}}
{{- $full := include "universal-chart.fullname" .root -}}
{{- if eq .name $full -}}
{{- $full -}}
{{- else -}}
{{- printf "%s-%s" $full .name -}}
{{- end -}}
{{- end }}

{{- define "universal-chart.cronJobName" -}}
{{- include "universal-chart.subresourceName" . | trunc 52 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels

Built as a real merged map (not text concatenation) so commonLabels can
never produce a duplicate YAML key. The chart's own identity keys — the
same ones selectorLabels emits, plus chart/version/managed-by — always win
over commonLabels; commonLabels can only ADD labels, never override
identity ones. That keeps metadata.labels permanently consistent with
selectorLabels (used for selector.matchLabels and pod template labels
everywhere), so a stray `commonLabels.app.kubernetes.io/name` can't drift
metadata away from the (deliberately static) selector.
*/}}
{{- define "universal-chart.labels" -}}
{{- $core := dict "app.kubernetes.io/name" (include "universal-chart.name" .) "app.kubernetes.io/instance" .Release.Name "helm.sh/chart" (printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-") "app.kubernetes.io/managed-by" .Release.Service }}
{{- if .Chart.AppVersion }}
{{- $core = merge $core (dict "app.kubernetes.io/version" (.Chart.AppVersion | toString)) }}
{{- end }}
{{- merge $core (.Values.commonLabels | default dict) | toYaml }}
{{- end }}

{{/*
Selector labels — deliberately static and immune to commonLabels/overrides.
Used everywhere something needs to MATCH pods (selector.matchLabels, pod
template metadata.labels, Service/PDB/ServiceMonitor selectors) so those
stay identical forever, including across upgrades where selector fields are
immutable.
*/}}
{{- define "universal-chart.selectorLabels" -}}
app.kubernetes.io/name: {{ include "universal-chart.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Primary Service name — the object rendered by templates/service.yaml.

Defaults to the release fullname, but a source manifest often names its Service
something else ('<app>-service' is the common convention). That name is not
decorative: Routes, Ingresses, other releases' config and in-cluster DNS all
address a Service BY NAME, so silently renaming it to the release breaks every
one of those references. .Values.service.name carries the real name.

Everything that references the primary Service must resolve it THROUGH this
helper, never through fullname directly — otherwise renaming the Service leaves
the reference dangling. Current callers: service.yaml (the object itself),
route.yaml + routes.yaml (spec.to.name) and ingress.yaml (backend.service.name).
*/}}
{{- define "universal-chart.primaryServiceName" -}}
{{- .Values.service.name | default (include "universal-chart.fullname" .) }}
{{- end }}

{{/*
ServiceAccount name
*/}}
{{- define "universal-chart.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "universal-chart.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Image string helper — a digest-pinned image (either via .Values.image.digest
or a repository that already carries an "@sha256:..." digest) must never get
a ":tag" suffix appended; that would either produce an invalid
"repo@sha256:...:tag" reference or silently swap the pinned digest for
Chart.AppVersion.
*/}}
{{- define "universal-chart.image" -}}
{{- if .Values.image.digest }}
{{- printf "%s@%s" .Values.image.repository .Values.image.digest }}
{{- else if contains "@" .Values.image.repository }}
{{- .Values.image.repository }}
{{- else }}
{{- printf "%s:%s" .Values.image.repository (.Values.image.tag | default .Chart.AppVersion) }}
{{- end }}
{{- end }}

{{/*
=============================================================================
MAP-TO-LIST — convert map-based values to Kubernetes arrays
=============================================================================
Helm deep-merges maps but replaces arrays. We store values as maps (keyed by
name) so GitOps overrides can patch individual entries. These helpers convert
maps back to the array format Kubernetes expects.

The key is normally the entry's `name`. It cannot always be: a container may
mount ONE volume several times with different subPaths (the standard way to
project individual ConfigMap keys onto individual files), and those mounts all
share a name. Such entries are keyed '<name>:<subPath>' and carry an explicit
`name` field, so the real name is `$spec.name | default $key` — never the bare
key, which would emit a mount referencing a volume that does not exist.

Usage:  {{- include "universal-chart.mapToList" .Values.ports | nindent 4 }}
*/}}
{{- define "universal-chart.mapToList" -}}
{{- range $name, $spec := . }}
- name: {{ $spec.name | default $name }}
  {{- toYaml (omit $spec "name") | nindent 2 }}
{{- end }}
{{- end }}

{{/*
First key from a map — used when we need a default port name, etc.
Usage:  {{ include "universal-chart.firstKey" .Values.service.ports }}
*/}}
{{- define "universal-chart.firstKey" -}}
{{- range $k, $_ := . }}{{ $k }}{{ break }}{{- end }}
{{- end }}

{{/*
=============================================================================
CHECKSUM — auto-restart pods when ConfigMaps or Secrets change
=============================================================================
Usage: add to pod annotations:
  annotations:
    {{- include "universal-chart.checksumAnnotations" . | nindent 8 }}
*/}}
{{- define "universal-chart.checksumAnnotations" -}}
{{- if .Values.checksums.enabled }}
{{- if .Values.configMaps }}
checksum/configmaps: {{ .Values.configMaps | toYaml | sha256sum }}
{{- end }}
{{- if .Values.secrets }}
checksum/secrets: {{ .Values.secrets | toYaml | sha256sum }}
{{- end }}
{{- end }}
{{- end }}

{{/*
=============================================================================
VALIDATION — fail fast on bad config
=============================================================================
*/}}

{{/*
Validates that every volumeMount — on the main container, every init
container, and every sidecar — references a volume that actually exists.
Without this, Kubernetes only catches the mistake at `kubectl apply` time
with a bare `Not found: "<name>"` error; this fails at template-render time
instead, naming the exact container and volume involved.

Deliberately does NOT auto-inject a fallback volume (e.g. emptyDir): that
would mask a forgotten PVC/ConfigMap/Secret and risk silent data loss. If a
container genuinely needs scratch space, declare it explicitly —
`volumes: { <name>: { emptyDir: {} } }` — in base/.

Init containers and sidecars are raw, pass-through Kubernetes container
specs (unlike the main container's map-based volumeMounts), so their
volumeMounts is a LIST of {name, mountPath}, not a map — handled separately
below.

Usage: {{ include "universal-chart.validateVolumeMounts" (dict
  "label" "the workload"
  "volumeMounts" .Values.volumeMounts
  "volumes" .Values.volumes
  "volumeClaimTemplates" .Values.volumeClaimTemplates
  "initContainers" .Values.initContainers
  "sidecars" .Values.sidecars) }}
*/}}
{{- define "universal-chart.validateVolumeMounts" -}}
{{- $validVolumes := merge (dict) (.volumeClaimTemplates | default dict) (.volumes | default dict) }}
{{- $label := .label | default "the workload" }}

{{- range $mountKey, $mountSpec := (.volumeMounts | default dict) }}
{{- /* Composite key for a volume mounted more than once — the real volume
       name is on the entry itself. See universal-chart.mapToList above. */}}
{{- $mountName := ($mountSpec.name | default $mountKey) }}
{{- if not (hasKey $validVolumes $mountName) }}
{{- fail (printf "Error: volumeMount %q is defined on %s's main container, but volume %q is missing. Declare it explicitly (e.g. volumes.%s.emptyDir: {} if it's meant to be transient) — this chart never injects a fallback volume for you." $mountName $label $mountName $mountName) }}
{{- end }}
{{- end }}

{{- range $kind, $containers := (dict "init container" (.initContainers | default dict) "sidecar" (.sidecars | default dict)) }}
{{- range $cName, $cSpec := $containers }}
{{- range $vm := ($cSpec.volumeMounts | default list) }}
{{- if not (hasKey $validVolumes $vm.name) }}
{{- fail (printf "Error: volumeMount %q on %s's %s %q is defined, but volume %q is missing. Declare it explicitly (e.g. volumes.%s.emptyDir: {} if it's meant to be transient) — this chart never injects a fallback volume for you." $vm.name $label $kind $cName $vm.name $vm.name) }}
{{- end }}
{{- end }}
{{- end }}
{{- end }}
{{- end }}

{{- define "universal-chart.validate" -}}
{{- $validTypes := list "deployment" "statefulset" "daemonset" "none" }}
{{- if not (has .Values.workload.type $validTypes) }}
{{- fail (printf "workload.type must be one of %v, got: %s" $validTypes .Values.workload.type) }}
{{- end }}
{{- if and .Values.ingress.enabled .Values.route.enabled }}
{{- fail "ingress.enabled and route.enabled cannot both be true — pick one" }}
{{- end }}
{{- if and (eq .Values.workload.type "daemonset") .Values.hpa.enabled }}
{{- fail "hpa is not supported for daemonset workloads" }}
{{- end }}
{{- if and (eq .Values.workload.type "none") .Values.hpa.enabled }}
{{- fail "hpa is not supported when workload.type is none (no pods to scale)" }}
{{- end }}
{{- if and (eq .Values.workload.type "none") .Values.vpa.enabled }}
{{- fail "vpa is not supported when workload.type is none (no pods to resize)" }}
{{- end }}
{{- if and (eq .Values.workload.type "none") .Values.pdb.enabled }}
{{- fail "pdb is not supported when workload.type is none (no pods to protect)" }}
{{- end }}
{{- $root := . }}
{{- include "universal-chart.validateVolumeMounts" (dict
      "label" "the workload"
      "volumeMounts" .Values.volumeMounts
      "volumes" .Values.volumes
      "volumeClaimTemplates" .Values.volumeClaimTemplates
      "initContainers" .Values.initContainers
      "sidecars" .Values.sidecars) }}
{{- /* Ingress/Route backend ports fall back to the primary Service's ports
when not given explicitly; an empty service.ports there resolves to an
empty string and silently drops traffic instead of erroring. */}}
{{- if .Values.ingress.enabled }}
{{- range $host := .Values.ingress.hosts }}
{{- range $path := $host.paths }}
{{- if and (not $path.portName) (not $root.Values.service.ports) }}
{{- fail (printf "ingress.hosts[%s].paths has no portName and service.ports is empty — the Ingress backend port would be blank" $host.host) }}
{{- end }}
{{- end }}
{{- end }}
{{- end }}
{{- if and .Values.route.enabled (not .Values.route.targetPort) (not .Values.service.ports) }}
{{- fail "route.enabled is true but route.targetPort is unset and service.ports is empty — the Route target port would be blank" }}
{{- end }}
{{- range $rname, $rspec := .Values.routes }}
{{- if and $rspec.enabled (not $rspec.targetPort) (not $root.Values.service.ports) }}
{{- fail (printf "routes.%s is enabled but targetPort is unset and service.ports is empty — the Route target port would be blank" $rname) }}
{{- end }}
{{- end }}
{{- end }}
