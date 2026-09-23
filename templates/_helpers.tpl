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
{{- $name := default .Release.Name .Values.nameOverride }}
{{- tpl (printf "%v" $name) . | trunc 63 | trimSuffix "-" }}
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
{{- $name := .Values.fullnameOverride | default (.Values.nameOverride | default .Release.Name) }}
{{- tpl (printf "%v" $name) . | trunc 63 | trimSuffix "-" }}
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
{{/*
STRICT NAMING: the map key IS the object's name, rendered verbatim.

This used to return "<fullname>-<key>", pairing with a converter that stripped
the same prefix on the way in. The round trip worked for names that happened to
carry the release prefix and mangled every name that did not — and two raw names
differing only by that prefix collapsed onto one key, silently losing an object.

A cronjobs/jobs key is now the full `metadata.name` from the raw manifest, which
is unique per kind per namespace by API rule, so it needs no prefix to stay
unique and no disambiguation to stay distinct. A hand-written values file gets
exactly the name it wrote.
*/}}
{{- define "universal-chart.subresourceName" -}}
{{- tpl (printf "%v" .name) .root -}}
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
{{/*
One object's OWN metadata.labels, merged with the chart's identity labels.

Every template used to render `universal-chart.labels` alone, so a raw
manifest's own labels — team, environment, part-of, argocd.argoproj.io/* —
were dropped from every object the chart renders. That was the single
highest-count defect in the fidelity report (~3,087 labels).

Two details are load-bearing:

  * ONE merged map, not two `labels:` blocks. The few templates that did carry
    the object's labels emitted a second block under the same key, which is a
    duplicate YAML key: some parsers reject it, and Helm silently keeps the
    last, which means a values file could overwrite `app.kubernetes.io/name`.
  * The chart's identity labels WIN. `merge` leaves the destination's keys
    intact, so an object's own labels can only ADD — the same rule
    universal-chart.labels already applies to commonLabels, and what keeps
    metadata permanently consistent with selectorLabels.

Usage: {{- include "universal-chart.objectLabels" (dict "root" $ "own" $spec.labels) | nindent 4 }}
*/}}
{{- define "universal-chart.objectLabels" -}}
{{- $core := fromYaml (include "universal-chart.labels" .root) }}
{{- tpl (toYaml (merge $core (.own | default dict))) .root }}
{{- end }}

{{- define "universal-chart.labels" -}}
{{- $core := dict "app.kubernetes.io/name" (include "universal-chart.name" .) "app.kubernetes.io/instance" .Release.Name "helm.sh/chart" (printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-") "app.kubernetes.io/managed-by" .Release.Service }}
{{- if .Chart.AppVersion }}
{{- $core = merge $core (dict "app.kubernetes.io/version" (.Chart.AppVersion | toString)) }}
{{- end }}
{{- tpl (toYaml (merge $core (.Values.commonLabels | default dict))) . }}
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
{{- tpl (printf "%v" (.Values.service.name | default (include "universal-chart.fullname" .))) . }}
{{- end }}

{{/*
ServiceAccount name
*/}}
{{- define "universal-chart.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- tpl (printf "%v" (default (include "universal-chart.fullname" .) .Values.serviceAccount.name)) . }}
{{- else }}
{{- tpl (printf "%v" (default "default" .Values.serviceAccount.name)) . }}
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
{{- $repo := tpl (printf "%v" .Values.image.repository) . }}
{{- if .Values.image.digest }}
{{- printf "%s@%s" $repo .Values.image.digest }}
{{- else if contains "@" $repo }}
{{- $repo }}
{{- else }}
{{- $tag := tpl (printf "%v" (.Values.image.tag | default .Chart.AppVersion)) . }}
{{- printf "%s:%s" $repo $tag }}
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

Usage:  {{- include "universal-chart.mapToList" (list .Values.ports $) | nindent 4 }}

The second argument is the tpl context (the release root) used to evaluate
expressions embedded in map values, same as universal-chart.tplval.
*/}}
{{- define "universal-chart.mapToList" -}}
{{- $ctx := index . 1 }}
{{- range $name, $spec := index . 0 }}
{{- /* `unnamed: true` means the map key is bookkeeping, not a real name: a
       container port the raw manifest left unnamed still needs a key here, but
       emitting `name: port-8080` would give the object a name it never had, and
       a Service targetPort or probe that resolves ports BY name would then
       match something that did not exist before. */}}
{{- if $spec.unnamed }}
- {{- tpl (toYaml (omit $spec "unnamed" "name")) $ctx | nindent 2 }}
{{- else }}
- name: {{ include "universal-chart.tplval" (list ($spec.name | default $name) $ctx) }}
  {{- /* An entry whose body is empty renders `- name: X` followed by a bare
         `{}`, which is malformed for an env var (no value, no valueFrom) and
         for a port. An empty body almost always means the source had
         `value: ""` and the empty scalar became an empty map on the way in, so
         `value: ""` is the faithful reading — an env var explicitly set to the
         empty string is NOT the same as an absent one, and dropping the entry
         would change behaviour rather than preserve it. */}}
  {{- $body := omit $spec "name" }}
  {{- if $body }}
  {{- tpl (toYaml $body) $ctx | nindent 2 }}
  {{- else }}
  value: ""
  {{- end }}
{{- end }}
{{- end }}
{{- end }}

{{/*
=============================================================================
TPL — every value that is EMITTED goes through tpl
=============================================================================
Values files are often passed in from git (ArgoCD ApplicationSet valueFiles),
so a value can itself contain {{ }} expressions that must be evaluated against
the release root at render time. Every template therefore emits values only
through this helper:

  {{ include "universal-chart.tplval" (list <value> <ctx>) }}

where <ctx> is the release root ($ in templates, $ in _pod-spec.tpl, and .
inside define helpers that receive the root as their argument). The value is
rendered with %v BEFORE tpl so non-string types (numbers, bools, lists) still
work, and a plain value with no {{ }} round-trips unchanged.
*/}}
{{- define "universal-chart.tplval" -}}
{{- tpl (printf "%v" (index . 0)) (index . 1) -}}
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
checksum/configmaps: {{ tpl (toYaml .Values.configMaps) . | sha256sum }}
{{- end }}
{{- if .Values.secrets }}
checksum/secrets: {{ tpl (toYaml .Values.secrets) . | sha256sum }}
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
