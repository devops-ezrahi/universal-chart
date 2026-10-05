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
{{- .name -}}
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
{{- /* chartLabels: false = render the object's own labels verbatim (plus
       commonLabels), no chart identity labels. The converter sets it: a live
       object carries its own name/instance/version/helm.sh/chart, and
       re-deriving them from this release rewrites every one of them. */}}
{{- if not .root.Values.chartLabels }}
{{- toYaml (merge (dict) (.own | default dict) (.root.Values.commonLabels | default dict)) }}
{{- else }}
{{- $core := fromYaml (include "universal-chart.labels" .root) }}
{{- toYaml (merge $core (.own | default dict)) }}
{{- end }}
{{- end }}

{{- define "universal-chart.labels" -}}
{{- $core := dict "app.kubernetes.io/name" (include "universal-chart.name" .) "app.kubernetes.io/instance" .Release.Name "helm.sh/chart" (printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-") "app.kubernetes.io/managed-by" .Release.Service }}
{{- if .Chart.AppVersion }}
{{- $core = merge $core (dict "app.kubernetes.io/version" (.Chart.AppVersion | toString)) }}
{{- end }}
{{- /* An explicit selectorLabels override is what the workload's pods and
       selector are actually identified by (an existing release's labels,
       carried over when migrating it onto this chart), so it outranks the
       chart's derived name/instance on metadata too. */}}
{{- $core = merge (deepCopy (.Values.selectorLabels | default dict)) $core }}
{{- merge $core (.Values.commonLabels | default dict) | toYaml }}
{{- end }}

{{/*
Selector labels — deliberately static and immune to commonLabels/overrides.
Static unless .Values.selectorLabels is set: a workload migrated from another
chart or raw manifests must keep its EXISTING selector, because
spec.selector is immutable — a different selector cannot be applied at all,
and the only way through is delete-and-recreate, which kills every pod at once.
The converter copies the raw spec.selector.matchLabels into selectorLabels.
Used everywhere something needs to MATCH pods (selector.matchLabels, pod
template metadata.labels, Service/PDB/ServiceMonitor selectors) so those
stay identical forever, including across upgrades where selector fields are
immutable.
*/}}
{{- define "universal-chart.selectorLabels" -}}
{{- /* .Values.selectorLabels pins the selector to a live object's own
       matchLabels — selector is immutable on a StatefulSet/Deployment, so
       re-deriving it from a differently named release cannot even sync. */}}
{{- if .Values.selectorLabels }}
{{- toYaml .Values.selectorLabels }}
{{- else }}
app.kubernetes.io/name: {{ include "universal-chart.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}
{{- end }}

{{/*
Pod TEMPLATE labels: the selector labels plus .Values.podLabels, as ONE merged
map (selector keys win) so a podLabels entry repeating a selector key can never
render a duplicate YAML key or drift the pods away from their selector.
*/}}
{{- define "universal-chart.podTemplateLabels" -}}
{{- $sel := fromYaml (include "universal-chart.selectorLabels" .) }}
{{- toYaml (merge $sel (.Values.podLabels | default dict)) }}
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
{{- (.Values.service | default dict).name | default (include "universal-chart.fullname" .) }}
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
{{- include "universal-chart.orderedMapToList" (dict "map" . "order" list) }}
{{- end }}

{{/*
Map keys in the order a list must render them: the keys `order` names first, in
that order, then every key it does not name in sorted order (so a hand-added
entry is never lost). Returned as JSON; read it back with fromJsonArray.

Helm ranges a map in SORTED key order, and for a pod template that is not
cosmetic. env, ports, volumeMounts, volumes, envFrom and containers are LISTS in
Kubernetes: reordering one changes the pod template, so the Deployment rolls a
new ReplicaSet (or the StatefulSet a new revision) and every pod restarts on the
first sync after migrating — even though nothing about the workload changed.
env order is also semantic: `$(VAR)` only expands variables defined EARLIER in
the list. The converter writes the raw manifest's order into the *Order values.
Usage: {{- $keys := include "universal-chart.orderedKeys" (dict "map" $m "order" $o) | fromJsonArray }}
*/}}
{{- define "universal-chart.orderedKeys" -}}
{{- $map := .map | default dict }}
{{- $keys := list }}
{{- range $k := (.order | default list) }}
{{- if and (hasKey $map $k) (not (has $k $keys)) }}
{{- $keys = append $keys $k }}
{{- end }}
{{- end }}
{{- range $k, $_ := $map }}
{{- if not (has $k $keys) }}
{{- $keys = append $keys $k }}
{{- end }}
{{- end }}
{{- toJson $keys }}
{{- end }}

{{/*
mapToList with an explicit order (see universal-chart.orderedKeys).
Usage: {{- include "universal-chart.orderedMapToList" (dict "map" .Values.env "order" .Values.envOrder) | nindent 4 }}
*/}}
{{- define "universal-chart.orderedMapToList" -}}
{{- $map := .map | default dict }}
{{- $stringValues := .stringValues }}
{{- range $name := (include "universal-chart.orderedKeys" . | fromJsonArray) }}
{{- $spec := index $map $name }}
{{- /* stringValues (env): EnvVar.value is a string, and a values file read by
       Helm turns a bare `y`, `08` or `true` into a bool or a number that the
       API server then refuses — the whole Deployment fails to apply. */}}
{{- if and $stringValues (kindIs "map" $spec) (hasKey $spec "value") (not (kindIs "invalid" $spec.value)) (not (kindIs "string" $spec.value)) }}
{{- $spec = merge (dict "value" (toString $spec.value)) $spec }}
{{- end }}
{{- /* `unnamed: true` means the map key is bookkeeping, not a real name: a
       container port the raw manifest left unnamed still needs a key here, but
       emitting `name: port-8080` would give the object a name it never had, and
       a Service targetPort or probe that resolves ports BY name would then
       match something that did not exist before. */}}
{{- if $spec.unnamed }}
- {{- toYaml (omit $spec "unnamed" "name") | nindent 2 }}
{{- else }}
- name: {{ $spec.name | default $name }}
  {{- /* An entry whose body is empty renders `- name: X` followed by a bare
         `{}`, which is malformed for an env var (no value, no valueFrom) and
         for a port. An empty body almost always means the source had
         `value: ""` and the empty scalar became an empty map on the way in, so
         `value: ""` is the faithful reading — an env var explicitly set to the
         empty string is NOT the same as an absent one, and dropping the entry
         would change behaviour rather than preserve it. */}}
  {{- $body := omit $spec "name" }}
  {{- if $body }}
  {{- toYaml $body | nindent 2 }}
  {{- else }}
  value: ""
  {{- end }}
{{- end }}
{{- end }}
{{- end }}

{{/*
=============================================================================
TPL — every value can carry {{ }} expressions
=============================================================================
Values files usually come from git (ArgoCD ApplicationSet valueFiles), so any
string in them — a map value, a list item, or a map KEY such as a jobs/cronjobs
name — may contain {{ }} evaluated against the release root.

renderValues walks .Values ONCE, before any template reads it, and replaces
every string holding "{{" with its tpl result, in place. Every template file
calls it on its first line; the marker makes the calls after the first free.

Exactly once, never per field as well: a value that must emit a literal "{{"
is written escaped ({{ "{{" }}), and a second pass would render the escaped
form and break it. For the same reason extraDeploy is skipped — extra-deploy.yaml
tpl's its entries itself.

A rendered value is always a string ("3", "true"), exactly as if it had been
quoted in the values file.
*/}}
{{- define "universal-chart.renderValues" -}}
{{- if not (hasKey .Values "__universalChartRendered") }}
{{- $_ := set .Values "__universalChartRendered" true }}
{{- include "universal-chart.tplWalk" (dict "node" (omit .Values "extraDeploy" "__universalChartRendered") "into" .Values "ctx" .) }}
{{- end }}
{{- end }}

{{/*
Renders .node's strings (values and keys) and writes each rendered entry into
.into, which is .node itself except at the top level, where .node is a copy
minus the keys that must not be walked. Maps nested anywhere are mutated in
place; lists are rebuilt, since a list element cannot be assigned in place.
ponytail: a list nested directly inside a list has only its maps rendered, not
its bare strings — no Kubernetes field has that shape.
*/}}
{{- define "universal-chart.tplWalk" -}}
{{- $ctx := .ctx }}
{{- $into := .into }}
{{- range $k, $v := .node }}
{{- $nv := $v }}
{{- if kindIs "string" $v }}
{{- if contains "{{" $v }}{{ $nv = tpl $v $ctx }}{{ end }}
{{- else if kindIs "map" $v }}
{{- include "universal-chart.tplWalk" (dict "node" $v "into" $v "ctx" $ctx) }}
{{- else if kindIs "slice" $v }}
{{- $nv = list }}
{{- range $item := $v }}
{{- if and (kindIs "string" $item) (contains "{{" $item) }}
{{- $nv = append $nv (tpl $item $ctx) }}
{{- else }}
{{- if kindIs "map" $item }}{{ include "universal-chart.tplWalk" (dict "node" $item "into" $item "ctx" $ctx) }}{{ end }}
{{- $nv = append $nv $item }}
{{- end }}
{{- end }}
{{- end }}
{{- $nk := $k }}
{{- if contains "{{" $k }}
{{- $nk = tpl $k $ctx }}
{{- $_ := unset $into $k }}
{{- if hasKey $into $nk }}{{ fail (printf "values key %q renders to %q, which already exists" $k $nk) }}{{ end }}
{{- end }}
{{- $_ := set $into $nk $nv }}
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
{{- if (and .Values.checksums .Values.checksums.enabled) }}
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
{{- if and (and .Values.ingress .Values.ingress.enabled) (and .Values.route .Values.route.enabled) }}
{{- fail "ingress.enabled and route.enabled cannot both be true — pick one" }}
{{- end }}
{{- if and (eq .Values.workload.type "daemonset") (and .Values.hpa .Values.hpa.enabled) }}
{{- fail "hpa is not supported for daemonset workloads" }}
{{- end }}
{{- if and (eq .Values.workload.type "none") (and .Values.hpa .Values.hpa.enabled) }}
{{- fail "hpa is not supported when workload.type is none (no pods to scale)" }}
{{- end }}
{{- if and (eq .Values.workload.type "none") (and .Values.vpa .Values.vpa.enabled) }}
{{- fail "vpa is not supported when workload.type is none (no pods to resize)" }}
{{- end }}
{{- if and (eq .Values.workload.type "none") (and .Values.pdb .Values.pdb.enabled) }}
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
{{- if (and .Values.ingress .Values.ingress.enabled) }}
{{- range $host := .Values.ingress.hosts }}
{{- range $path := $host.paths }}
{{- if and (not $path.portName) (not $path.portNumber) (not ($root.Values.service | default dict).ports) }}
{{- fail (printf "ingress.hosts[%s].paths has no portName and service.ports is empty — the Ingress backend port would be blank" $host.host) }}
{{- end }}
{{- end }}
{{- end }}
{{- end }}
{{- if and (and .Values.route .Values.route.enabled) (not .Values.route.targetPort) (not .Values.service.ports) }}
{{- fail "route.enabled is true but route.targetPort is unset and service.ports is empty — the Route target port would be blank" }}
{{- end }}
{{- range $rname, $rspec := .Values.routes }}
{{- if and $rspec.enabled (not $rspec.targetPort) (not ($root.Values.service | default dict).ports) }}
{{- fail (printf "routes.%s is enabled but targetPort is unset and service.ports is empty — the Route target port would be blank" $rname) }}
{{- end }}
{{- end }}
{{- end }}
