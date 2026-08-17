{{/*
=============================================================================
SHARED POD SPEC — used by Deployment, StatefulSet, DaemonSet
=============================================================================
Call: {{- include "universal-chart.podSpec" . | nindent 6 }}
*/}}
{{- define "universal-chart.podSpec" -}}
serviceAccountName: {{ include "universal-chart.serviceAccountName" . }}
automountServiceAccountToken: {{ .Values.serviceAccount.automountServiceAccountToken }}

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
{{- if .Values.shareProcessNamespace }}
shareProcessNamespace: true
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
  {{- range $name, $spec := . }}
  - name: {{ $name }}
    {{- toYaml $spec | nindent 4 }}
  {{- end }}
{{- end }}

{{- /* ── Main container ───────────────────────────── */}}
containers:
  {{- include "universal-chart.mainContainer" . | nindent 2 }}
  {{- /* ── Sidecar containers (map-based) ──────────── */}}
  {{- range $name, $spec := .Values.sidecars }}
  - name: {{ $name }}
    {{- toYaml $spec | nindent 4 }}
  {{- end }}

{{- /* ── Volumes (map-based) ─────────────────────── */}}
{{- with .Values.volumes }}
volumes:
  {{- include "universal-chart.mapToList" . | nindent 2 }}
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

  {{- with .Values.securityContext }}
  securityContext:
    {{- toYaml . | nindent 4 }}
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
    {{- include "universal-chart.mapToList" . | nindent 4 }}
  {{- end }}

  {{- with .Values.env }}
  env:
    {{- include "universal-chart.mapToList" . | nindent 4 }}
  {{- end }}

  {{- with .Values.envFrom }}
  envFrom:
    {{- range $name, $spec := . }}
    - {{ $spec.type | default "configMapRef" }}:
        name: {{ $name }}
      {{- if hasKey $spec "optional" }}
      optional: {{ $spec.optional }}
      {{- end }}
    {{- end }}
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
    {{- include "universal-chart.mapToList" . | nindent 4 }}
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
serviceAccountName: {{ .job.serviceAccountName | default (include "universal-chart.serviceAccountName" .) }}
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
  {{- range $name, $spec := . }}
  - name: {{ $name }}
    {{- toYaml $spec | nindent 4 }}
  {{- end }}
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
    {{- with .job.securityContext }}
    securityContext:
      {{- toYaml . | nindent 6 }}
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
    {{- with .job.env }}
    env:
      {{- include "universal-chart.mapToList" . | nindent 6 }}
    {{- end }}
    {{- with .job.envFrom }}
    envFrom:
      {{- range $name, $spec := . }}
      - {{ $spec.type | default "configMapRef" }}:
          name: {{ $name }}
      {{- end }}
    {{- end }}
    {{- with .job.resources }}
    resources:
      {{- toYaml . | nindent 6 }}
    {{- end }}
    {{- with .job.volumeMounts }}
    volumeMounts:
      {{- include "universal-chart.mapToList" . | nindent 6 }}
    {{- end }}
  {{- range $name, $spec := .job.sidecars }}
  - name: {{ $name }}
    {{- toYaml $spec | nindent 4 }}
  {{- end }}
{{- with .job.volumes }}
volumes:
  {{- include "universal-chart.mapToList" . | nindent 2 }}
{{- end }}
{{- with .Values.nodeSelector }}
nodeSelector:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .Values.tolerations }}
tolerations:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- end }}
