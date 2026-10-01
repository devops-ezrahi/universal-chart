#!/usr/bin/env bash
# argo-orphan-tree.sh — remove an old ArgoCD app tree WITHOUT deleting anything
# it deployed.
#
# Given the root Application of an old app-of-apps (any depth, Applications and
# ApplicationSets mixed), this walks the whole tree and removes every
# Application / ApplicationSet in it from ArgoCD, while every Deployment,
# StatefulSet, Service, Route, ... they deployed stays running in the cluster,
# untouched, so the new ms-applicationSet apps can adopt them.
#
# Why not just `argocd app delete` / `oc delete applicationset`? Both cascade by
# default: the resources finalizer makes ArgoCD delete every managed resource,
# and an ApplicationSet deletion waits for each generated app to do that —
# which is both destructive and slow. Here:
#
#   1. discover   the full tree first, changing nothing; refuse to touch
#                 anything created by the new ms-applicationSet chart
#   2. freeze     auto-sync off on every Application, parents first, so no
#                 parent can re-create a child we are about to delete
#   3. detach     each ApplicationSet with --cascade=orphan: it disappears in
#                 seconds and its generated Applications become plain apps
#                 (frozen right after, before the controller could touch them)
#   4. delete     every Application, deepest first: finalizers removed, THEN
#                 deleted — with no resources finalizer, ArgoCD deletes nothing
#                 in the cluster. Runs in parallel batches.
#   5. verify     the apps are gone and sampled workloads still exist with the
#                 same metadata.uid (i.e. were never recreated)
#
# Dry run by default: prints every command. Pass --apply to execute.
#
# Usage:
#   argo-orphan-tree.sh <root-application> [-n <argocd-namespace>] [--apply] [-P <parallel>]
#
# Requires: jq, and oc or kubectl logged in with rights on the ArgoCD namespace
# (override the binary with KUBECTL=/path/to/kubectl).

set -euo pipefail

NS="argocd"
APPLY=0
PARALLEL=10
ROOT=""
NEW_CHART_LABEL="managed-by=ms-applicationSet"

usage() { sed -n '2,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//'; exit "${1:-0}"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    -n|--namespace) NS="$2"; shift 2 ;;
    --apply)        APPLY=1; shift ;;
    -P|--parallel)  PARALLEL="$2"; shift 2 ;;
    -h|--help)      usage 0 ;;
    -*)             echo "unknown option: $1" >&2; usage 2 ;;
    *)              [[ -z "$ROOT" ]] || { echo "only one root app" >&2; exit 2; }; ROOT="$1"; shift ;;
  esac
done
[[ -n "$ROOT" ]] || { echo "error: root application name is required" >&2; usage 2; }

command -v jq >/dev/null || { echo "error: jq is required" >&2; exit 2; }
K="${KUBECTL:-$(command -v oc || command -v kubectl || true)}"
[[ -n "$K" ]] || { echo "error: neither oc nor kubectl found (set KUBECTL)" >&2; exit 2; }

LOG="argo-orphan-tree-${ROOT}-$(date +%Y%m%d-%H%M%S).log"
exec > >(tee -a "$LOG") 2>&1

k() { "$K" -n "$NS" "$@"; }
run() {
  if [[ $APPLY -eq 1 ]]; then "$@"; else echo "[dry-run] $(printf '%q ' "$@")"; fi
}

echo "== root: Application/$ROOT  namespace: $NS  mode: $([[ $APPLY -eq 1 ]] && echo APPLY || echo DRY-RUN)"
echo "== log:  $LOG"

# --------------------------------------------------------------------------
# 1. Discover (read-only). One list call per kind, then pure jq lookups —
#    fast even with hundreds of microservices.
# --------------------------------------------------------------------------
ALL_APPS="$(k get applications.argoproj.io -o json)"
ALL_SETS="$(k get applicationsets.argoproj.io -o json 2>/dev/null || echo '{"items":[]}')"

app_json() { jq -c --arg n "$1" '.items[] | select(.metadata.name == $n)' <<<"$ALL_APPS"; }
set_json() { jq -c --arg n "$1" '.items[] | select(.metadata.name == $n)' <<<"$ALL_SETS"; }

[[ -n "$(app_json "$ROOT")" ]] || { echo "error: Application/$ROOT not found in $NS" >&2; exit 1; }

declare -a ORDER=()          # "Kind/name" in BFS order (parents before children)
declare -A DEPTH=() GENERATED=()
queue=("Application/$ROOT"); DEPTH["Application/$ROOT"]=0
while [[ ${#queue[@]} -gt 0 ]]; do
  item="${queue[0]}"; queue=("${queue[@]:1}")
  kind="${item%%/*}"; name="${item#*/}"
  [[ " ${ORDER[*]} " == *" $item "* ]] && continue
  if [[ "$kind" == "Application" ]]; then json="$(app_json "$name")"; else json="$(set_json "$name")"; fi
  if [[ -z "$json" ]]; then echo "WARN: $item is listed by its parent but does not exist — skipped"; continue; fi

  # Never touch the new tree.
  if jq -e '.metadata.labels["managed-by"] == "ms-applicationSet"' <<<"$json" >/dev/null; then
    echo "ABORT: $item carries label $NEW_CHART_LABEL — it belongs to the NEW ApplicationSets." >&2
    echo "       The old root must not (indirectly) manage the new apps. Nothing was changed." >&2
    exit 1
  fi
  ORDER+=("$item")
  d=$(( DEPTH[$item] + 1 ))

  if [[ "$kind" == "Application" ]]; then
    # Children an app-of-apps manages: the Application/ApplicationSet objects
    # in its own resource tree.
    while IFS=$'\t' read -r ck cn cns; do
      [[ -z "$ck" ]] && continue
      if [[ -n "$cns" && "$cns" != "$NS" ]]; then
        echo "WARN: $item manages $ck/$cn in namespace $cns (not $NS) — not handled, do it separately"; continue
      fi
      DEPTH["$ck/$cn"]=$d; queue+=("$ck/$cn")
    done < <(jq -r '.status.resources[]? | select(.group == "argoproj.io" and (.kind == "Application" or .kind == "ApplicationSet")) | [.kind, .name, (.namespace // "")] | @tsv' <<<"$json")
  else
    # Children an ApplicationSet generated: Applications it owns.
    while read -r cn; do
      [[ -z "$cn" ]] && continue
      DEPTH["Application/$cn"]=$d; GENERATED["Application/$cn"]=1; queue+=("Application/$cn")
    done < <(jq -r --arg s "$name" '.items[] | select(any(.metadata.ownerReferences[]?; .kind == "ApplicationSet" and .name == $s)) | .metadata.name' <<<"$ALL_APPS")
  fi
done

echo
echo "== tree (${#ORDER[@]} objects):"
for item in "${ORDER[@]}"; do
  printf '%*s%s%s\n' $(( DEPTH[$item] * 2 )) '' "$item" "${GENERATED[$item]:+  (generated)}"
done

# Sample up to 10 workloads from the leaves, to prove afterwards they were not
# recreated.
declare -A UID_BEFORE=()
for item in "${ORDER[@]}"; do
  [[ "${item%%/*}" == "Application" ]] || continue
  while IFS=$'\t' read -r wk wn wns; do
    [[ ${#UID_BEFORE[@]} -ge 10 ]] && break
    uid="$("$K" -n "$wns" get "$wk" "$wn" -o jsonpath='{.metadata.uid}' 2>/dev/null || true)"
    [[ -n "$uid" ]] && UID_BEFORE["$wns/$wk/$wn"]="$uid"
  done < <(jq -r '.status.resources[]? | select(.kind == "Deployment" or .kind == "StatefulSet" or .kind == "DaemonSet") | [.kind, .name, .namespace] | @tsv' <<<"$(app_json "${item#*/}")")
  [[ ${#UID_BEFORE[@]} -ge 10 ]] && break
done
echo "== sampled ${#UID_BEFORE[@]} workloads for the post-check"

if [[ $APPLY -eq 1 ]]; then
  echo
  read -r -p "Remove these ${#ORDER[@]} objects from ArgoCD (cluster resources are kept)? Type the root name to continue: " ans
  [[ "$ans" == "$ROOT" ]] || { echo "aborted"; exit 1; }
fi

freeze() {  # $1 = app name
  local json; json="$(app_json "$1")"
  if jq -e '.spec.syncPolicy.automated != null' <<<"$json" >/dev/null; then
    run "$K" -n "$NS" patch application "$1" --type json -p '[{"op":"remove","path":"/spec/syncPolicy/automated"}]'
  fi
}

# --------------------------------------------------------------------------
# 2. Freeze the Applications no ApplicationSet controls, top-down.
#    (Generated ones would be reverted by their ApplicationSet — step 3.)
# --------------------------------------------------------------------------
echo; echo "== 2. disable auto-sync (top-down)"
for item in "${ORDER[@]}"; do
  [[ "${item%%/*}" == "Application" && -z "${GENERATED[$item]:-}" ]] && freeze "${item#*/}"
done

# --------------------------------------------------------------------------
# 3. Detach ApplicationSets: orphan delete keeps their Applications.
# --------------------------------------------------------------------------
echo; echo "== 3. delete ApplicationSets with --cascade=orphan"
SETS=()
for item in "${ORDER[@]}"; do [[ "${item%%/*}" == "ApplicationSet" ]] && SETS+=("${item#*/}"); done
for s in "${SETS[@]}"; do
  run "$K" -n "$NS" patch applicationset "$s" --type merge -p '{"metadata":{"finalizers":null}}'
  run "$K" -n "$NS" delete applicationset "$s" --cascade=orphan --wait=false
done
if [[ $APPLY -eq 1 && ${#SETS[@]} -gt 0 ]]; then
  for _ in $(seq 60); do
    left=0; for s in "${SETS[@]}"; do k get applicationset "$s" >/dev/null 2>&1 && left=$((left+1)); done
    [[ $left -eq 0 ]] && break; sleep 2
  done
  [[ $left -eq 0 ]] || { echo "ERROR: $left ApplicationSet(s) still present after 2 min — stopping before deleting apps" >&2; exit 1; }
fi
echo "   then freeze the apps they generated:"
for item in "${ORDER[@]}"; do
  [[ -n "${GENERATED[$item]:-}" ]] && freeze "${item#*/}"
done

# --------------------------------------------------------------------------
# 4. Delete Applications deepest-first, finalizers removed first, in parallel.
# --------------------------------------------------------------------------
echo; echo "== 4. delete Applications non-cascading (deepest first, $PARALLEL in parallel)"
export K NS APPLY
delete_one() {
  if [[ $APPLY -eq 1 ]]; then
    "$K" -n "$NS" patch application "$1" --type merge -p '{"metadata":{"finalizers":null}}' >/dev/null &&
    "$K" -n "$NS" delete application "$1" --wait=false
  else
    echo "[dry-run] $K -n $NS patch application $1 --type merge -p '{\"metadata\":{\"finalizers\":null}}'"
    echo "[dry-run] $K -n $NS delete application $1 --wait=false"
  fi
}
export -f delete_one
max=0; for item in "${ORDER[@]}"; do (( DEPTH[$item] > max )) && max=${DEPTH[$item]}; done
apps_at_level() {
  local item
  for item in "${ORDER[@]}"; do
    if [[ "${item%%/*}" == "Application" && ${DEPTH[$item]} -eq $1 ]]; then echo "${item#*/}"; fi
  done
}
for (( lvl = max; lvl >= 0; lvl-- )); do
  # shellcheck disable=SC2016  # $1 expands in the child bash, on purpose
  apps_at_level "$lvl" | xargs -r -P "$PARALLEL" -I{} bash -c 'delete_one "$1"' _ {}
done

# --------------------------------------------------------------------------
# 5. Verify.
# --------------------------------------------------------------------------
[[ $APPLY -eq 1 ]] || { echo; echo "== dry run complete — re-run with --apply to execute"; exit 0; }
echo; echo "== 5. verify"
sleep 3
remaining=0
for item in "${ORDER[@]}"; do
  if [[ "${item%%/*}" == "Application" ]]; then k get application "${item#*/}" >/dev/null 2>&1 && { echo "STILL PRESENT: $item"; remaining=$((remaining+1)); }; fi
done
bad=0
for key in "${!UID_BEFORE[@]}"; do
  IFS=/ read -r wns wk wn <<<"$key"
  now="$("$K" -n "$wns" get "$wk" "$wn" -o jsonpath='{.metadata.uid}' 2>/dev/null || true)"
  if [[ "$now" == "${UID_BEFORE[$key]}" ]]; then echo "ok   $key (uid unchanged)"
  else echo "FAIL $key (uid before ${UID_BEFORE[$key]}, now '${now:-<missing>}')"; bad=$((bad+1)); fi
done
echo "== done: $remaining app(s) still present, $bad sampled workload(s) changed"
[[ $remaining -eq 0 && $bad -eq 0 ]]
