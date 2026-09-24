#!/usr/bin/env python3
"""
Fetch Kubernetes/OpenShift resources from an OpenShift console URL,
clean them of runtime metadata, and output raw YAML to namespaces/<namespace>/ directory.

Supports two URL formats:
  1. Single resource URL:
       python correct.py https://console.apps.oscp-dev.mamdas.iaf/k8s/ns/goldenbell-next/deployments/ballistic-api-sim-1
  2. Topology (app) URL - outputs all resources in the app to namespaces/<namespace>/:
       python correct.py -o namespaces "https://console.apps.oscp-rak.mamdas.iaf/topology/ns/bluetorch-tst?view=graph&selectId=group%3Alapid-tst-blue"
  3. Full namespace - fetches ALL apps AND ALL namespace-level resources
       (workloads incl. Jobs, referenced CMs/Secrets/PVCs/SAs, ALL services and
       routes, HPAs, PDBs, Ingresses, NetworkAttachmentDefinitions; unclaimed
       ones land in shared.yaml) plus a final fetch-failure report.
       NetworkPolicies and ServiceMonitors are not fetched — the converter
       deliberately does not convert them:
       python correct.py --kubeconfig my-kubeconfig.yaml -n bluetorch-tst -o namespaces
  4. Topologies file - multiple topology URLs from one namespace:
       python correct.py --topologies urls.txt -o namespaces

  --kubeconfig can be combined with any mode; without it oc uses its default
  config / KUBECONFIG env var.

  ExternalSecrets bring their store along: the SecretStore (or
  ClusterSecretStore) named by spec.secretStoreRef, plus what that store needs
  in this namespace — the Vault CA ConfigMap/Secret (spec.provider.vault.
  caProvider) and the ServiceAccount of auth.kubernetes.serviceAccountRef.
  Each is written once: into shared.yaml when more than one microservice file
  needs it, else into the one file that does (place_secret_store_dependencies).

Output Structure:
  <output>/<namespace>/
    <microservice_name>.yaml  # Raw multi-document YAML: Deployment + Service + Route + ConfigMap etc.
    shared.yaml               # Resources shared by multiple workloads
"""
import argparse
import re
import sys
import subprocess
import yaml
import unicodedata
from pathlib import Path
from urllib.parse import unquote

# Kubernetes label used to identify apps in topology view
TOPOLOGY_LABEL = "app.kubernetes.io/part-of"

# Primary workload resource types to start discovery from
PRIMARY_RESOURCE_TYPES = [
    "deployments",
    "cronjobs",
    "jobs",
    "statefulsets",
    "daemonsets",
]

# Resources that a COMPLETE namespace dump must always contain, even when no
# workload references them (RBAC, network add-ons, autoscaling, policies...).
# These were silently missing from earlier dumps (Roles/RoleBindings,
# NetworkAttachmentDefinitions, per-ordinal LoadBalancer services, ...).
NAMESPACE_SCAN_SPEC = [
    # roles/rolebindings excluded: OpenShift RBAC admin/user bindings, not app resources
    ("horizontalpodautoscalers", None),
    ("poddisruptionbudgets", None),
    ("ingresses", None),
    ("network-attachment-defs", "k8s.cni.cncf.io"),
]

# All resource types that can be referenced by workloads
REFERENCED_RESOURCE_TYPES = [
    "configmaps",
    "secrets",
    "persistentvolumeclaims",
    "serviceaccounts",
    "services",
    "routes",
    "externalsecrets",
    "secretstores",
    "clustersecretstores",
]

RESOURCE_PLURAL_TO_KIND = {
    "deployments": "Deployment",
    "services": "Service",
    "configmaps": "ConfigMap",
    "serviceaccounts": "ServiceAccount",
    "routes": "Route",
    "cronjobs": "CronJob",
    "persistentvolumeclaims": "PersistentVolumeClaim",
    "ingresses": "Ingress",
    "horizontalpodautoscalers": "HorizontalPodAutoscaler",
    "secrets": "Secret",
    "statefulsets": "StatefulSet",
    "daemonsets": "DaemonSet",
    "jobs": "Job",
    "roles": "Role",
    "rolebindings": "RoleBinding",
    "poddisruptionbudgets": "PodDisruptionBudget",
    "network-attachment-defs": "NetworkAttachmentDefinition",
    "externalsecrets": "ExternalSecret",
    "secretstores": "SecretStore",
    "clustersecretstores": "ClusterSecretStore",
}

# CRD for External Secrets
EXTERNAL_SECRET_GROUP = "external-secrets.io"
EXTERNAL_SECRET_VERSION = "v1beta1"
EXTERNAL_SECRET_KIND = "ExternalSecret"
EXTERNAL_SECRET_PLURAL = "externalsecrets"

# CRD for Secret Stores
SECRETSTORE_GROUP = "external-secrets.io"
SECRETSTORE_VERSION = "v1beta1"
SECRETSTORE_KIND = "SecretStore"
SECRETSTORE_PLURAL = "secretstores"
CLUSTERSECRETSTORE_PLURAL = "clustersecretstores"

METADATA_FIELDS_TO_REMOVE = [
    "creationTimestamp",
    "resourceVersion",
    "uid",
    "generation",
    "finalizers",
    "selfLink",
    "managedFields",
]

# Runtime stamps, not configuration: the last `kubectl apply` payload (a stale
# full copy of the object) and the source ArgoCD Application's ownership id.
ANNOTATIONS_TO_REMOVE = {
    "kubectl.kubernetes.io/last-applied-configuration",
    "argocd.argoproj.io/tracking-id",
}

TOP_LEVEL_FIELDS_TO_REMOVE = [
    "status",
]

# When set (via --kubeconfig), every oc invocation uses this kubeconfig file.
# Leave None to let oc use its default config / KUBECONFIG env var.
KUBECONFIG_PATH = None

# Every resource that failed to be fetched, so a run ends with an explicit
# report instead of silently dropping them from the dump.
FETCH_FAILURES = []


def parse_yaml_stream(output):
    """Parse a multi-document YAML string into a flat list of resource dicts,
    unwrapping *List wrappers."""
    docs = [d for d in yaml.safe_load_all(output or "") if d is not None]
    resources = []
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        if doc.get("kind", "").endswith("List"):
            for item in doc.get("items", []) or []:
                if isinstance(item, dict):
                    resources.append(item)
        else:
            resources.append(doc)
    return resources


def run_oc(args):
    """Run an oc command with the configured kubeconfig (if any)."""
    cmd = ["oc"]
    if KUBECONFIG_PATH:
        cmd += ["--kubeconfig", KUBECONFIG_PATH]
    cmd += args
    return subprocess.run(cmd, capture_output=True, text=True, check=True,
                          encoding="utf-8", errors="replace")


def fetch_all_in_namespace(namespace, resource_type, api_group=None):
    """Fetch ALL resources of a kind in a namespace. Returns a list of dicts;
    an unknown/absent kind (typical for CRDs) yields a warning and []."""
    if api_group:
        args = ["get", f"{resource_type}.{api_group}", "-n", namespace, "-o", "yaml"]
    else:
        args = ["get", resource_type, "-n", namespace, "-o", "yaml"]
    try:
        result = run_oc(args)
    except subprocess.CalledProcessError as e:
        stderr_text = (e.stderr or "").strip()
        print(f"  Skipping {resource_type} in '{namespace}': {stderr_text}", file=sys.stderr)
        return []
    return parse_yaml_stream(result.stdout)

def parse_url(url):
    """
    Parse an OpenShift console URL and extract namespace, resource type, and name.
    """
    pattern = r"/k8s/ns/([^/]+)/([^/]+)/([^/]+)$"
    match = re.search(pattern, url)
    if not match:
        print(f"Error: Could not parse URL: {url}", file=sys.stderr)
        print("Expected format: https://<host>/k8s/ns/<namespace>/<resource-type>/<resource-name>", file=sys.stderr)
        sys.exit(1)
    namespace = match.group(1)
    resource_type = match.group(2)
    resource_name = match.group(3)

    # Normalize resource type (plural to singular for oc get)
    if resource_type.endswith("s") and not resource_type.endswith("ss"):
        resource_singular = resource_type[:-1]
    else:
        resource_singular = resource_type
    return namespace, resource_singular, resource_name

def parse_topology_url(url):
    """
    Parse an OpenShift topology/app URL and extract namespace and app name.
    """
    pattern = r"/topology/ns/([^/?]+)"
    match = re.search(pattern, url)
    if not match:
        print(f"Error: Could not parse topology URL: {url}", file=sys.stderr)
        print("Expected format: https://<host>/topology/ns/<namespace>?", file=sys.stderr)
        sys.exit(1)
    namespace = match.group(1)

    select_id_match = re.search(r"[?&]selectId=([^&]+)", url)
    if select_id_match:
        select_id = unquote(select_id_match.group(1))
        app_name = re.sub(r"^group:", "", select_id)
        if app_name:
            return namespace, app_name

    path_match = re.search(r"/topology/ns/([^/?]+)/([^/?]+)", url)
    if path_match:
        return path_match.group(1), path_match.group(2)

    return namespace, namespace


def fetch_resource(namespace, resource_type, resource_name, api_group=None):
    """Fetch a resource using oc get and return the YAML string."""
    if api_group:
        args = ["get", f"{resource_type}.{api_group}", resource_name, "-n", namespace, "-o", "yaml"]
    else:
        args = ["get", resource_type, resource_name, "-n", namespace, "-o", "yaml"]

    try:
        result = run_oc(args)
        return result.stdout
    except subprocess.CalledProcessError as e:
        print(f"Warning: Error running oc {' '.join(args)}: {e.stderr.strip()}", file=sys.stderr)
        FETCH_FAILURES.append(f"{RESOURCE_PLURAL_TO_KIND.get(resource_type, resource_type)} '{resource_name}' (namespace {namespace}): {e.stderr.strip()}")
        return None

def fetch_cluster_resource(resource_type, resource_name, api_group=None):
    """Fetch a cluster-scoped resource (like ClusterSecretStore)."""
    if api_group:
        args = ["get", f"{resource_type}.{api_group}", resource_name, "-o", "yaml"]
    else:
        args = ["get", resource_type, resource_name, "-o", "yaml"]

    try:
        result = run_oc(args)
        return result.stdout
    except subprocess.CalledProcessError as e:
        print(f"Warning: Error running oc {' '.join(args)}: {e.stderr.strip()}", file=sys.stderr)
        FETCH_FAILURES.append(f"{RESOURCE_PLURAL_TO_KIND.get(resource_type, resource_type)} '{resource_name}' (cluster-scoped): {e.stderr.strip()}")
        return None


def extract_pod_spec_from_resource(resource):
    """Extract the pod spec from a workload resource."""
    if not isinstance(resource, dict):
        return None
    kind = resource.get("kind", "")
    if kind in ["Deployment", "StatefulSet", "DaemonSet"]:
        spec = resource.get("spec", {})
        template = spec.get("template", {})
        return template.get("spec")
    elif kind == "CronJob":
        spec = resource.get("spec", {})
        job_template = spec.get("jobTemplate", {})
        job_spec = job_template.get("spec", {})
        template = job_spec.get("template", {})
        return template.get("spec")
    elif kind == "Job":
        template = resource.get("spec", {}).get("template", {})
        return template.get("spec")
    return None


def extract_resource_refs_from_pod_spec(pod_spec):
    """
    Recursively extract all referenced resource names from a pod spec.
    """
    if not isinstance(pod_spec, dict):
        return {}
    refs = {
        "configmaps": set(),
        "secrets": set(),
        "persistentvolumeclaims": set(),
        "serviceaccounts": set(),
    }

    sa = pod_spec.get("serviceAccountName") or pod_spec.get("serviceAccount")
    if sa:
        refs["serviceaccounts"].add(sa)

    for container_key in ("containers", "initContainers"):
        containers = pod_spec.get(container_key, [])
        if not isinstance(containers, list):
            continue
        for container in containers:
            if not isinstance(container, dict):
                continue
            for envVar in container.get("env", []) or []:
                if not isinstance(envVar, dict):
                    continue
                value_from = envVar.get("valueFrom")
                if not isinstance(value_from, dict):
                    continue
                cm_ref = value_from.get("configMapKeyRef")
                if isinstance(cm_ref, dict) and cm_ref.get("name"):
                    refs["configmaps"].add(cm_ref["name"])
                secret_ref = value_from.get("secretKeyRef")
                if isinstance(secret_ref, dict) and secret_ref.get("name"):
                    refs["secrets"].add(secret_ref["name"])
            for env_from in container.get("envFrom", []) or []:
                if not isinstance(env_from, dict):
                    continue
                cm_ref = env_from.get("configMapRef")
                if isinstance(cm_ref, dict) and cm_ref.get("name"):
                    refs["configmaps"].add(cm_ref["name"])
                secret_ref = env_from.get("secretRef")
                if isinstance(secret_ref, dict) and secret_ref.get("name"):
                    refs["secrets"].add(secret_ref["name"])

    volumes = pod_spec.get("volumes", [])
    if not isinstance(volumes, list):
        volumes = []
    for volume in volumes:
        if not isinstance(volume, dict):
            continue
        cm = volume.get("configMap")
        if isinstance(cm, dict) and cm.get("name"):
            refs["configmaps"].add(cm["name"])
        secret = volume.get("secret")
        if isinstance(secret, dict):
            if secret.get("secretName"):
                refs["secrets"].add(secret["secretName"])
            elif secret.get("name"):
                refs["secrets"].add(secret["name"])
        pvc = volume.get("persistentVolumeClaim")
        if isinstance(pvc, dict) and pvc.get("claimName"):
            refs["persistentvolumeclaims"].add(pvc["claimName"])
        projected = volume.get("projected", {})
        if isinstance(projected, dict):
            sources = projected.get("sources", [])
            if not isinstance(sources, list):
                sources = []
            for proj in sources:
                if not isinstance(proj, dict):
                    continue
                cm_ref = proj.get("configMap")
                if isinstance(cm_ref, dict) and cm_ref.get("name"):
                    refs["configmaps"].add(cm_ref["name"])
                secret_ref = proj.get("secret")
                if isinstance(secret_ref, dict):
                    s_name = secret_ref.get("secretName") or secret_ref.get("name")
                    if s_name:
                        refs["secrets"].add(s_name)
    return refs


def get_app_id(workload):
    """
    Get the app identifier for a workload using priority:
    1. metadata.labels app.kubernetes.io/part-of
    2. metadata.labels app
    3. spec.selector.matchLabels app  (or template.metadata.labels app)
    4. metadata name
    """
    metadata = workload.get("metadata", {})
    if not isinstance(metadata, dict):
        return None
    labels = metadata.get("labels", {})
    if not isinstance(labels, dict):
        labels = {}

    if labels.get(TOPOLOGY_LABEL):
        return labels[TOPOLOGY_LABEL]
    if labels.get("app"):
        return labels["app"]

    kind = workload.get("kind", "")

    # 3a. Check spec.selector.matchLabels.app (Deployments, StatefulSets, DaemonSets)
    if kind in ("Deployment", "StatefulSet", "DaemonSet"):
        spec = workload.get("spec", {})
        if isinstance(spec, dict):
            selector = spec.get("selector", {})
            if isinstance(selector, dict):
                match_labels = selector.get("matchLabels", {})
                if isinstance(match_labels, dict) and match_labels.get("app"):
                    return match_labels["app"]

    # 3b. Check spec.jobTemplate.spec.template.metadata.labels.app (CronJobs)
    if kind == "CronJob":
        spec = workload.get("spec", {})
        if isinstance(spec, dict):
            job_template = spec.get("jobTemplate", {})
            if isinstance(job_template, dict):
                job_spec = job_template.get("spec", {})
                if isinstance(job_spec, dict):
                    template = job_spec.get("template", {})
                    if isinstance(template, dict):
                        tmpl_labels = template.get("metadata", {})
                        if isinstance(tmpl_labels, dict) and tmpl_labels.get("labels", {}):
                            tmpl_label_map = tmpl_labels["labels"]
                            if isinstance(tmpl_label_map, dict) and tmpl_label_map.get("app"):
                                return tmpl_label_map["app"]

    # 3c. Fallback: check pod template labels directly for any kind
    if kind in ("Deployment", "StatefulSet", "DaemonSet", "Job"):
        spec = workload.get("spec", {})
        if isinstance(spec, dict):
            template = spec.get("template", {})
            if isinstance(template, dict):
                tmpl_meta = template.get("metadata", {})
                if isinstance(tmpl_meta, dict):
                    tmpl_labels = tmpl_meta.get("labels", {})
                    if isinstance(tmpl_labels, dict) and tmpl_labels.get("app"):
                        return tmpl_labels["app"]

    name = metadata.get("name")
    if name:
        return name
    return None


def fetch_workloads(namespace, app_name, all_workloads=None):
    """
    Fetch primary workload resources for the app.
    If all_workloads is provided, filters from that list. Otherwise fetches all workloads.
    """
    if all_workloads is None:
        all_workloads = fetch_all_workloads_in_namespace(namespace)
    return [wl for wl in all_workloads if get_app_id(wl) == app_name]


def fetch_all_workloads_in_namespace(namespace):
    """
    Fetch ALL primary workload resources in a namespace without any label filter.
    Returns a list of workload dicts.
    """
    results = []
    for resource_type in PRIMARY_RESOURCE_TYPES:
        print(f"  Fetching all {resource_type} in namespace '{namespace}'...", file=sys.stderr)
        result_items = fetch_all_in_namespace(namespace, resource_type)
        if resource_type == "jobs":
            # Skip Jobs that are children of a CronJob - the CronJob template
            # already captures their podSpec; child Jobs are runtime instances
            # and must not become their own microservice file.
            result_items = [j for j in result_items
                            if not any(r.get("kind") == "CronJob"
                                       for r in ((j.get("metadata") or {}).get("ownerReferences") or []))]
        results.extend(result_items)
    return results


def fetch_namespace_scan(namespace):
    """Fetch the namespace-level resource kinds that a complete dump must
    always contain even when no workload references them (RBAC, HPAs, PDBs,
    ingresses, NADs)."""
    results = []
    for resource_type, api_group in NAMESPACE_SCAN_SPEC:
        print(f"  Scanning namespace for {resource_type}...", file=sys.stderr)
        results.extend(fetch_all_in_namespace(namespace, resource_type, api_group=api_group))
    return results


def fetch_referenced_resources(namespace, gathered_refs, secrets_to_skip=None, cluster_gathered_refs=None):
    """
    Fetch all referenced resources by type and name.
    Args:
        secrets_to_skip: Set of secret names to skip fetching (managed by ExternalSecrets)
        cluster_gathered_refs: Dict of cluster-scoped resources to fetch
    """
    if secrets_to_skip is None:
        secrets_to_skip = set()
    if cluster_gathered_refs is None:
        cluster_gathered_refs = {}

    results = []

    # 1. Fetch Namespaced Resources
    for resource_type in REFERENCED_RESOURCE_TYPES:
        names = gathered_refs.get(resource_type, set())
        if not names:
            continue

        if resource_type == "secrets":
            names = names - secrets_to_skip
            if not names:
                continue

        kind = RESOURCE_PLURAL_TO_KIND.get(resource_type, resource_type)
        api_group = None
        if resource_type in ["externalsecrets", "secretstores", "clustersecretstores"]:
            api_group = EXTERNAL_SECRET_GROUP

        for name in sorted(names):
            print(f"    Fetching {kind} {name}...", file=sys.stderr)
            raw_yaml = fetch_resource(namespace, resource_type, name, api_group=api_group)
            if not raw_yaml:
                continue
            output = raw_yaml.strip()
            if not output:
                continue
            docs = list(yaml.safe_load_all(output))
            for doc in docs:
                if isinstance(doc, dict) and doc is not None:
                    if doc.get("kind", "").endswith("List"):
                        for item in doc.get("items", []):
                            if isinstance(item, dict):
                                results.append(item)
                    else:
                        results.append(doc)

    # 2. Fetch Cluster Scoped Resources (e.g. ClusterSecretStore)
    if cluster_gathered_refs:
        for resource_type, names in cluster_gathered_refs.items():
            if not names:
                continue
            kind = RESOURCE_PLURAL_TO_KIND.get(resource_type, resource_type)
            api_group = None
            if resource_type == "clustersecretstores":
                api_group = EXTERNAL_SECRET_GROUP

            for name in sorted(names):
                print(f"    Fetching Cluster-scoped {kind} {name}...", file=sys.stderr)
                raw_yaml = fetch_cluster_resource(resource_type, name, api_group=api_group)
                if not raw_yaml:
                    continue
                output = raw_yaml.strip()
                if not output:
                    continue
                docs = list(yaml.safe_load_all(output))
                for doc in docs:
                    if isinstance(doc, dict) and doc is not None:
                        if doc.get("kind", "").endswith("List"):
                            for item in doc.get("items", []):
                                if isinstance(item, dict):
                                    results.append(item)
                        else:
                            results.append(doc)
    return results


def discover_routes_for_services(namespace, services, all_routes=None):
    """
    Discover routes that point to the given services.
    If all_routes is provided (pre-fetched), filter from it instead of querying the cluster.
    """
    if not services and not all_routes:
        return []
    if all_routes is None:
        print(f"  Fetching routes in namespace {namespace} to match services...", file=sys.stderr)
        all_routes = fetch_all_in_namespace(namespace, "routes")
    service_names = set(services)
    matching_routes = []
    for route in all_routes:
        if not isinstance(route, dict):
            continue
        spec = route.get("spec", {})
        if not isinstance(spec, dict):
            continue
        to = spec.get("to", {})
        if isinstance(to, dict) and to.get("name") in service_names:
            matching_routes.append(route)
    return matching_routes


def discover_services_for_workloads(namespace, workloads, all_services=None):
    """
    Discover services that match workloads via:
    1. Selector match: service.spec.selector matches workload's spec.selector.matchLabels
    2. Name prefix match: service name starts with any workload name
    3. App label overlap: service metadata labels match workload metadata labels
    If all_services is provided (pre-fetched), filter from it instead of querying the cluster.
    """
    selectors = []
    for workload in workloads:
        spec = workload.get("spec", {})
        if not isinstance(spec, dict):
            continue
        selector = spec.get("selector", {})
        if isinstance(selector, dict):
            match_labels = selector.get("matchLabels", {})
            if isinstance(match_labels, dict) and match_labels:
                selectors.append(match_labels)
    if not selectors:
        return []
    if all_services is None:
        print(f"  Fetching services in namespace {namespace} to match workload selectors...", file=sys.stderr)
        all_services = fetch_all_in_namespace(namespace, "services")
    matching = []
    for service in all_services:
        svc_selector = service.get("spec", {}).get("selector", {})
        if not isinstance(svc_selector, dict):
            continue
        for sel in selectors:
            if all(svc_selector.get(k) == v for k, v in sel.items()):
                matching.append(service)
                break
    return matching


def discover_services_for_app(namespace, app_name, all_workloads=None, all_services=None):
    """
    Discover services for a given app (used in --namespace mode).
    Matches services by:
    1. Selector match with workload matchLabels
    2. Service name starts with or contains the app name
    If all_services is provided (pre-fetched), filter from it instead of querying the cluster.
    """
    workloads = fetch_workloads(namespace, app_name, all_workloads=all_workloads)
    if not workloads:
        return []

    selectors = []
    workload_names = set()
    workload_labels = set()
    for wl in workloads:
        metadata = wl.get("metadata", {})
        if isinstance(metadata, dict):
            wl_name = metadata.get("name", "")
            if wl_name:
                workload_names.add(wl_name)
            labels = metadata.get("labels", {})
            if isinstance(labels, dict):
                for k, v in labels.items():
                    workload_labels.add((k, v))

        spec = wl.get("spec", {})
        if isinstance(spec, dict):
            selector = spec.get("selector", {})
            if isinstance(selector, dict):
                match_labels = selector.get("matchLabels", {})
                if isinstance(match_labels, dict) and match_labels:
                    selectors.append(match_labels)

    if not selectors and not workload_names and not workload_labels:
        return []

    if all_services is None:
        print(f"  Fetching services in namespace {namespace} for app '{app_name}'...", file=sys.stderr)
        all_services = fetch_all_in_namespace(namespace, "services")

    matching = []
    for service in all_services:
        svc_name = service.get("metadata", {}).get("name", "")
        svc_selector = service.get("spec", {}).get("selector", {})
        svc_labels = service.get("metadata", {}).get("labels", {})
        if not isinstance(svc_selector, dict):
            svc_selector = {}
        if not isinstance(svc_labels, dict):
            svc_labels = {}

        matched = False

        # 1. Selector match
        for sel in selectors:
            if all(svc_selector.get(k) == v for k, v in sel.items()):
                matched = True
                break

        # 2. Name contains app name
        if not matched and app_name and svc_name:
            if app_name in svc_name:
                matched = True

        # 3. Name starts with workload name
        if not matched:
            for wl_name in workload_names:
                if svc_name and (svc_name == wl_name or svc_name.startswith(wl_name + "-") or svc_name.startswith(wl_name + ".")):
                    matched = True
                    break

        # 4. Metadata label overlap (excluding internal/system labels)
        if not matched:
            for k, v in svc_labels.items():
                if (k, v) in workload_labels and not k.startswith("app.kubernetes.io/managed-by"):
                    matched = True
                    break

        if matched:
            matching.append(service)
    return matching


def discover_all_workloads_in_namespace(namespace):
    """Fetch ALL primary workload types in a namespace."""
    return fetch_all_workloads_in_namespace(namespace)


def clean_kind(kind_raw):
    """A store kind as typed, minus the invisible characters a copy-paste can
    leave in it ('Se<ZWNJ>cretStore' reads as SecretStore and matches nothing)."""
    if not kind_raw:
        return ""
    # NFKD, then drop zero-width / bidi-control characters and combining marks
    # (Python's re has no \p{M}, hence unicodedata.combining).
    kind = unicodedata.normalize("NFKD", str(kind_raw))
    kind = re.sub("[\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff]", "", kind)
    kind = "".join(c for c in kind if not unicodedata.combining(c)).strip()
    if kind != kind_raw:
        print(f"    Warning: Cleaned kind '{kind_raw}' (repr: {repr(kind_raw)}) to '{kind}'", file=sys.stderr)
    return kind


def store_ref_of(es):
    """(kind, name) of the store an ExternalSecret reads from, or None.

    spec.secretStoreRef is the ESO field; the old lookup read `storeRef`, which
    no ExternalSecret has, so no store was ever found. storeRef is still
    accepted in case a dump used it. kind defaults to SecretStore, as in ESO."""
    spec = es.get("spec") if isinstance(es, dict) else None
    if not isinstance(spec, dict):
        return None
    ref = spec.get("secretStoreRef") or spec.get("storeRef")
    if not isinstance(ref, dict) or not ref.get("name"):
        return None
    return (clean_kind(ref.get("kind")) or "SecretStore", ref["name"])


def store_dependencies(store, namespace):
    """(kind, name) of what a SecretStore needs in this namespace to work: the
    Vault CA named by spec.provider.vault.caProvider (a ConfigMap or Secret) and
    the ServiceAccount Vault's kubernetes auth logs in as
    (auth.kubernetes.serviceAccountRef). One that names ANOTHER namespace —
    usual for a ClusterSecretStore — is not this namespace's to own: reported,
    not pulled."""
    vault = ((store.get("spec") or {}).get("provider") or {}).get("vault") or {}
    ca = vault.get("caProvider") or {}
    sa = ((vault.get("auth") or {}).get("kubernetes") or {}).get("serviceAccountRef") or {}
    refs = []
    if ca.get("type") in ("ConfigMap", "Secret") and ca.get("name"):
        refs.append((ca["type"], ca["name"], ca.get("namespace")))
    if sa.get("name"):
        refs.append(("ServiceAccount", sa["name"], sa.get("namespace")))
    store_id = f"{store.get('kind')}/{(store.get('metadata') or {}).get('name')}"
    deps = []
    for kind, name, ns in refs:
        if ns and ns != namespace:
            print(f"    Note: {store_id} needs {kind} '{name}' from namespace '{ns}' - "
                  f"not pulled into '{namespace}'", file=sys.stderr)
        else:
            deps.append((kind, name))
    return deps


SHARED_KEY = "__shared__"
KIND_TO_PLURAL = {kind: plural for plural, kind in RESOURCE_PLURAL_TO_KIND.items()}


def place_secret_store_dependencies(namespace, microservices):
    """Pull the store every ExternalSecret in `microservices` reads from, plus
    that store's Vault CA and ServiceAccount, and file each object exactly ONCE:

      * in shared.yaml when it is actually shared — more than one file needs it;
      * otherwise in the one microservice file that needs it.

    A file "needs" a store when it holds an ExternalSecret reading from it, and
    a CA/ServiceAccount when it needs a store that uses them. A file that
    ALREADY holds the object (the ServiceAccount a workload runs as, a CA
    ConfigMap a pod mounts, anything already in shared.yaml) counts too, so an
    existing placement is kept or moved to shared.yaml, never duplicated.

    `microservices` is {file key: {"workload": ..., "resources": [(name, kind,
    yaml)]}}, SHARED_KEY being shared.yaml; mutated in place, SHARED_KEY
    created when needed. Objects go in raw — the writers clean them like
    everything else."""
    files = {key: ms["resources"] for key, ms in microservices.items()}
    needed_by = {}
    for key, entries in files.items():
        for _, kind, raw in entries:
            if kind == "ExternalSecret":
                ref = store_ref_of(yaml.safe_load(raw))
                if ref:
                    needed_by.setdefault(ref, set()).add(key)

    fetched = {}
    for store_key in sorted(needed_by):  # the stores; their deps are added below
        kind, name = store_key
        if kind == "ClusterSecretStore":
            raw = fetch_cluster_resource(CLUSTERSECRETSTORE_PLURAL, name, api_group=EXTERNAL_SECRET_GROUP)
        elif kind == "SecretStore":
            raw = fetch_resource(namespace, SECRETSTORE_PLURAL, name, api_group=EXTERNAL_SECRET_GROUP)
        else:
            print(f"    Warning: store kind '{kind}' ({name}) is not SecretStore or "
                  f"ClusterSecretStore - not pulled", file=sys.stderr)
            continue
        store = next(iter(parse_yaml_stream(raw)), None) if raw else None
        if store is None:
            continue
        fetched[store_key] = store
        for dep in store_dependencies(store, namespace):
            needed_by.setdefault(dep, set()).update(needed_by[store_key])

    for key in sorted(needed_by):
        kind, name = key
        holders = {f for f, entries in files.items()
                   if any(k == kind and n == name for n, k, _ in entries)}
        users = needed_by[key] | holders
        target = SHARED_KEY if len(users) > 1 else next(iter(users))
        if holders == {target}:
            continue
        doc = fetched.get(key)
        if doc is None and holders:
            doc = yaml.safe_load(next(r for n, k, r in files[next(iter(holders))]
                                      if k == kind and n == name))
        if doc is None and kind in KIND_TO_PLURAL:
            raw = fetch_resource(namespace, KIND_TO_PLURAL[kind], name)
            doc = next(iter(parse_yaml_stream(raw)), None) if raw else None
        if doc is None:
            continue
        for f in holders:
            files[f][:] = [e for e in files[f] if not (e[1] == kind and e[0] == name)]
        if target not in files:
            microservices[target] = {"workload": None, "resources": []}
            files[target] = microservices[target]["resources"]
        files[target].append((name, kind, yaml.dump(doc, default_flow_style=False, sort_keys=False)))
        where = "shared.yaml" if target == SHARED_KEY else f"{target}.yaml"
        print(f"    {kind} '{name}' -> {where} (needed by {len(users)} file(s))", file=sys.stderr)


def discover_externalsecrets_and_stores(namespace):
    """
    Discover ExternalSecrets and the stores they reference. The stores are
    NOT fetched here — see place_secret_store_dependencies.
    Returns:
        es_target_map: dict mapping target_secret_name -> ExternalSecret dict.
        store_refs: dict mapping 'secretstores' -> set(names) and 'clustersecretstores' -> set(names).
        es_to_store_map: dict mapping ExternalSecret name -> store name.
        sa_refs_from_stores: always empty; kept for the return shape.
    """
    print(f"  Fetching ExternalSecrets in namespace {namespace}...", file=sys.stderr)
    args = ["get", f"{EXTERNAL_SECRET_PLURAL}.{EXTERNAL_SECRET_GROUP}", "-n", namespace, "-o", "yaml"]
    es_target_map = {}
    store_refs = {"secretstores": set(), "clustersecretstores": set()}
    es_to_store_map = {}
    sa_refs_from_stores = set() # ServiceAccounts used by stores

    try:
        result = run_oc(args)
        output = result.stdout.strip()
        if not output:
            return es_target_map, store_refs, es_to_store_map, sa_refs_from_stores

        docs = list(yaml.safe_load_all(output))
        docs = [d for d in docs if d is not None]

        for doc in docs:
            items = []
            if isinstance(doc, dict) and doc.get("kind", "").endswith("List"):
                items = doc.get("items", [])
            elif isinstance(doc, dict):
                items = [doc]

            for es in items:
                if not isinstance(es, dict):
                    continue
                es_name = es.get("metadata", {}).get("name", "unknown")
                spec = es.get("spec", {})
                if not isinstance(spec, dict):
                    continue

                # 1. Extract Store References. The store itself (and its Vault
                # CA / ServiceAccount) is pulled later by
                # place_secret_store_dependencies, once it is known which
                # microservice files end up holding an ExternalSecret that reads it.
                ref = store_ref_of(es)
                if ref:
                    kind, name = ref
                    es_to_store_map[es_name] = name
                    if kind == "SecretStore":
                        store_refs["secretstores"].add(name)
                        print(f"    Found ExternalSecret '{es_name}' referencing SecretStore '{name}'", file=sys.stderr)
                    elif kind == "ClusterSecretStore":
                        store_refs["clustersecretstores"].add(name)
                        print(f"    Found ExternalSecret '{es_name}' referencing ClusterSecretStore '{name}'", file=sys.stderr)
                    else:
                        print(f"    Warning: ExternalSecret '{es_name}' has kind '{kind}' which is not SecretStore or ClusterSecretStore", file=sys.stderr)
                # 2. Extract Target Secret Name
                target = spec.get("target", {})
                if not isinstance(target, dict):
                    continue
                target_name = target.get("name")
                template = target.get("template", {})
                if not isinstance(template, dict):
                    template = {}
                if not target_name and isinstance(template, dict):
                    target_name = template.get("name")
                if target_name:
                    es_target_map[target_name] = es
                else:
                    print(f"    Warning: ExternalSecret '{es_name}' has no target name", file=sys.stderr)

        return es_target_map, store_refs, es_to_store_map, sa_refs_from_stores

    except subprocess.CalledProcessError as e:
        print(f"    Warning: Could not fetch ExternalSecrets: {e.stderr.strip()}", file=sys.stderr)
        return es_target_map, store_refs, es_to_store_map, sa_refs_from_stores


def fetch_all_app_resources(namespace, app_name, all_workloads=None, all_services=None, all_routes=None):
    """
    Fetch all resources for an app with STRICT categorization.
    If all_workloads is provided, filters workloads from that list instead of querying the cluster.
    all_services / all_routes: pre-fetched namespace-wide lists to filter from
    (avoids re-querying the cluster per app).
    """
    print(f"Discovering resources for app '{app_name}' in namespace '{namespace}'...", file=sys.stderr)

    # Step 1: Fetch primary workloads
    workloads = fetch_workloads(namespace, app_name, all_workloads=all_workloads)
    if not workloads:
        print(f"No workloads found for app '{app_name}'", file=sys.stderr)
        return {}

    # Step 2: Extract referenced resources per workload
    workload_refs = {}
    all_refs = {rt: {} for rt in REFERENCED_RESOURCE_TYPES}

    for workload in workloads:
        workload_name = workload.get("metadata", {}).get("name", "unknown")
        pod_spec = extract_pod_spec_from_resource(workload)
        per_workload_refs = {rt: set() for rt in REFERENCED_RESOURCE_TYPES}
        if pod_spec:
            refs = extract_resource_refs_from_pod_spec(pod_spec)
            for rt, names in refs.items():
                if rt in per_workload_refs:
                    per_workload_refs[rt].update(names)
        workload_refs[workload_name] = per_workload_refs

        for rt, names in per_workload_refs.items():
            if rt not in all_refs:
                all_refs[rt] = {}
            for name in names:
                if name not in all_refs[rt]:
                    all_refs[rt][name] = set()
                all_refs[rt][name].add(workload_name)

    # Step 3: Discover ExternalSecrets. Their stores, and what the stores need
    # (Vault CA, ServiceAccount), are pulled by place_secret_store_dependencies
    # once every file is assembled — only then is it known how many
    # microservices share a store.
    es_target_map, store_refs, es_to_store_map, sa_refs_from_stores = discover_externalsecrets_and_stores(namespace)

    gathered_refs = {rt: set() for rt in REFERENCED_RESOURCE_TYPES}
    cluster_gathered_refs = {}

    for rt in REFERENCED_RESOURCE_TYPES:
        for wl_refs in workload_refs.values():
            gathered_refs[rt].update(wl_refs.get(rt, set()))

    secrets_to_skip = set(es_target_map.keys())
    if secrets_to_skip:
        print(f"  Skipping secrets managed by ExternalSecrets: {secrets_to_skip}", file=sys.stderr)

    # Identify shared vs unique resources
    shared_refs = {rt: set() for rt in REFERENCED_RESOURCE_TYPES}
    unique_refs = {rt: {} for rt in REFERENCED_RESOURCE_TYPES}

    for rt in REFERENCED_RESOURCE_TYPES:
        for name, referencing_workloads in all_refs[rt].items():
            if rt == "secrets" and name in secrets_to_skip:
                continue
            if len(referencing_workloads) > 1:
                shared_refs[rt].add(name)
            else:
                workload_name = next(iter(referencing_workloads))
                unique_refs[rt][name] = workload_name

    # --- STRICT CATEGORIZATION LOGIC ---
    # (SecretStores and the ServiceAccounts they use are no longer forced into
    # shared.yaml here: place_secret_store_dependencies files them in shared.yaml
    # only when more than one microservice needs them.)

    # Handle ExternalSecrets
    # ExternalSecrets are usually Unique to the workload, unless multiple workloads reference the SAME ES name.
    # We already added them to all_refs via workload_refs if they were referenced by env/volumes?
    # Actually, ExternalSecrets are usually NOT referenced directly in pod spec (they create Secrets which are referenced).
    # So, we need to explicitly add the ExternalSecret itself to the resources of the workload that uses its Target Secret.

    # We will handle distribution of ES later, after we fetch them.

    if shared_refs:
        shared_count = sum(len(v) for v in shared_refs.values())
        print(f"  Found {shared_count} shared resources referenced by multiple workloads", file=sys.stderr)

    # Fetch resources
    referenced_resources = fetch_referenced_resources(namespace, gathered_refs, secrets_to_skip=secrets_to_skip, cluster_gathered_refs=cluster_gathered_refs)

    # Also fetch the ExternalSecrets themselves if they were discovered
    for target_name, es_resource in es_target_map.items():
        # Only add if the secret it targets was actually referenced by a workload
        if "secrets" in all_refs and target_name in all_refs["secrets"]:
            referenced_resources.append(es_resource)

    # Index fetched resources by (Kind, Name)
    resource_index = {}
    for resource in referenced_resources:
        kind = resource.get("kind", "")
        name = resource.get("metadata", {}).get("name", "")
        resource_index[(kind, name)] = resource

    # Step 4: Discover services
    if all_workloads is not None:
        services = discover_services_for_app(namespace, app_name, all_workloads=all_workloads, all_services=all_services)
    else:
        services = discover_services_for_workloads(namespace, workloads, all_services=all_services)
    service_to_workloads = {}
    for service in services:
        svc_selector = service.get("spec", {}).get("selector", {})
        if not isinstance(svc_selector, dict):
            continue
        svc_name = service.get("metadata", {}).get("name", "")
        matching_workloads = []
        for workload in workloads:
            spec = workload.get("spec", {})
            if not isinstance(spec, dict):
                continue
                continue
            selector = spec.get("selector", {})
            if isinstance(selector, dict):
                match_labels = selector.get("matchLabels", {})
                if isinstance(match_labels, dict) and match_labels:
                    if all(svc_selector.get(k) == v for k, v in match_labels.items()):
                        wl_name = workload.get("metadata", {}).get("name", "")
                        matching_workloads.append(wl_name)
        service_to_workloads[svc_name] = matching_workloads

    # Step 5: Discover routes
    service_names = [s["metadata"]["name"] for s in services if isinstance(s, dict) and s.get("metadata")]
    routes = discover_routes_for_services(namespace, service_names if service_names else [], all_routes=all_routes)
    route_to_service = {}
    for route in routes:
        spec = route.get("spec", {})
        if isinstance(spec, dict):
            to = spec.get("to", {})
            if isinstance(to, dict):
                route_to_service[route.get("metadata", {}).get("name", "")] = to.get("name", "")

    # Build microservice grouping
    microservices = {}
    for workload in workloads:
        workload_name = workload.get("metadata", {}).get("name", "unknown")
        kind = workload.get("kind", "Unknown")
        yaml_str = yaml.dump(workload, default_flow_style=False, sort_keys=False)
        microservices[workload_name] = {
            "workload": (workload_name, kind, yaml_str),
            "resources": [],
        }

    # Distribute shared resources
    shared_resources = []
    for rt in REFERENCED_RESOURCE_TYPES:
        for name in shared_refs[rt]:
            kind = RESOURCE_PLURAL_TO_KIND.get(rt, rt)
            resource = resource_index.get((kind, name))
            if resource:
                yaml_str = yaml.dump(resource, default_flow_style=False, sort_keys=False)
                shared_resources.append((name, kind, yaml_str))

    # Distribute unique resources
    for rt in REFERENCED_RESOURCE_TYPES:
        for name, workload_name in unique_refs[rt].items():
            kind = RESOURCE_PLURAL_TO_KIND.get(rt, rt)
            resource = resource_index.get((kind, name))
            if resource and workload_name in microservices:
                yaml_str = yaml.dump(resource, default_flow_style=False, sort_keys=False)
                microservices[workload_name]["resources"].append((name, kind, yaml_str))

    # Distribute services and routes
    for service in services:
        svc_name = service.get("metadata", {}).get("name", "")
        kind = service.get("kind", "Service")
        yaml_str = yaml.dump(service, default_flow_style=False, sort_keys=False)
        matching = service_to_workloads.get(svc_name, [])
        if len(matching) > 1:
            shared_resources.append((svc_name, kind, yaml_str))
        elif len(matching) == 1:
            wl_name = matching[0]
            if wl_name in microservices:
                microservices[wl_name]["resources"].append((svc_name, kind, yaml_str))

    for route in routes:
        route_name = route.get("metadata", {}).get("name", "")
        kind = route.get("kind", "Route")
        yaml_str = yaml.dump(route, default_flow_style=False, sort_keys=False)
        svc_name = route_to_service.get(route_name, "")
        matching = service_to_workloads.get(svc_name, [])
        if len(matching) > 1:
            shared_resources.append((route_name, kind, yaml_str))
        elif len(matching) == 1:
            wl_name = matching[0]
            if wl_name in microservices:
                microservices[wl_name]["resources"].append((route_name, kind, yaml_str))

    # Distribute ExternalSecrets (their stores: place_secret_store_dependencies).

    for target_name, es_resource in es_target_map.items():
        if "secrets" in all_refs and target_name in all_refs["secrets"]:
            # Determine which workload(s) use this target secret
            referencing_workloads = all_refs["secrets"].get(target_name, set())

            # If multiple workloads use the same target secret (and thus the same ES?),
            # technically they might share the ES, but usually each workload has its own ES definition.
            # Here, we associate the ES with the primary workload that requested the secret.

            if len(referencing_workloads) > 1:
                # Shared ES
                es_yaml = yaml.dump(es_resource, default_flow_style=False, sort_keys=False)
                shared_resources.append((target_name + "-ext", "ExternalSecret", es_yaml))
            else:
                # Unique ES
                wl_name = next(iter(referencing_workloads))
                if wl_name in microservices:
                    es_yaml = yaml.dump(es_resource, default_flow_style=False, sort_keys=False)
                    microservices[wl_name]["resources"].append((target_name + "-ext", "ExternalSecret", es_yaml))

    if shared_resources:
        microservices["__shared__"] = {
            "workload": None,
            "resources": shared_resources,
        }
    return microservices


# ---------------------------------------------------------------------------
# Simplified cleaning - only strip runtime metadata, preserve everything else
# ---------------------------------------------------------------------------

def clean_metadata(metadata):
    """Remove only runtime-generated metadata fields. Keep all labels, annotations, ownerReferences."""
    if not isinstance(metadata, dict):
        return metadata
    cleaned = {}
    for k, v in metadata.items():
        if k in METADATA_FIELDS_TO_REMOVE:
            continue
        if k == "namespace":
            continue
        if k == "annotations" and isinstance(v, dict):
            v = {a: x for a, x in v.items() if a not in ANNOTATIONS_TO_REMOVE}
            if not v:
                continue
        cleaned[k] = v
    return cleaned

def clean_resource(resource):
    """Minimal cleaning: remove runtime metadata only. Preserve ALL resource content."""
    if not isinstance(resource, dict):
        return resource

    cleaned = {}
    for k, v in resource.items():
        if k in TOP_LEVEL_FIELDS_TO_REMOVE:
            continue
        if k == "metadata":
            cleaned_metadata = clean_metadata(v)
            if cleaned_metadata:
                cleaned[k] = cleaned_metadata
            continue
        cleaned[k] = v
    return cleaned

def sanitize_filename(name):
    return re.sub(r'[^a-zA-Z0-9._\-]', '-', name)

def is_topology_url(url):
    return "/topology/ns/" in url

# ---------------------------------------------------------------------------
# Output writing - raw YAML to namespaces/<namespace>/
# ---------------------------------------------------------------------------

def write_resources_to_namespace(all_resources, output_path, shared_sink=None):
    """Write microservice and shared YAML files to the namespace output directory.

    If shared_sink (a list) is given, shared resources are APPENDED to it instead of
    being written to shared.yaml, so a full-namespace run (many apps) ends with a
    single combined shared.yaml.
    """
    output_path.mkdir(parents=True, exist_ok=True)

    for microservice_name, microservice in sorted(all_resources.items()):
        is_shared = (microservice_name == "__shared__")
        if is_shared and shared_sink is not None:
            shared_sink.extend(microservice.get("resources", []))
            continue
        resources_to_write = []

        workload = microservice.get("workload")
        if workload:
            wl_name, wl_kind, raw_yaml = workload
            data = yaml.safe_load(raw_yaml)
            if data:
                cleaned = clean_resource(data)
                resources_to_write.append(cleaned)

        for name, kind, raw_yaml in microservice.get("resources", []):
            data = yaml.safe_load(raw_yaml)
            if data:
                cleaned = clean_resource(data)
                resources_to_write.append(cleaned)

        if is_shared:
            yaml_filename = "shared.yaml"
        else:
            yaml_filename = f"{sanitize_filename(microservice_name)}.yaml"

        yaml_file_path = output_path / yaml_filename
        if resources_to_write:
            content = yaml.dump_all(
                resources_to_write,
                default_flow_style=False,
                sort_keys=False,
                allow_unicode=True,
            )
            yaml_file_path.write_text(content, encoding="utf-8")
            print(f"  Wrote {yaml_file_path}", file=sys.stderr)


def write_shared_yaml(shared_entries, output_path):
    """Write one combined shared.yaml from (name, kind, yaml_str) entries,
    deduplicated by (kind, name)."""
    if not shared_entries:
        return
    seen = set()
    docs = []
    for name, kind, raw_yaml in shared_entries:
        key = (kind, name)
        if key in seen:
            continue
        seen.add(key)
        data = yaml.safe_load(raw_yaml)
        if data:
            docs.append(clean_resource(data))
    if not docs:
        return
    path = output_path / "shared.yaml"
    path.write_text(
        yaml.dump_all(docs, default_flow_style=False, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    print(f"  Wrote {path}", file=sys.stderr)

# ---------------------------------------------------------------------------
# Single resource handler
# ---------------------------------------------------------------------------

def handle_single_resource(url):
    namespace, resource_type, resource_name = parse_url(url)
    print(f"Fetching {resource_type} {resource_name} from namespace {namespace}...", file=sys.stderr)
    raw_yaml = fetch_resource(namespace, resource_type, resource_name)
    if not raw_yaml:
        return
    data = yaml.safe_load(raw_yaml)
    if data:
        cleaned = clean_resource(data)
        print(yaml.dump(cleaned, default_flow_style=False, sort_keys=False, allow_unicode=True))

# ---------------------------------------------------------------------------
# Topology URL handler
# ---------------------------------------------------------------------------

def handle_topology_url(url, output_dir):
    namespace, app_name = parse_topology_url(url)
    print(f"Fetching all resources for app '{app_name}' in namespace '{namespace}'...", file=sys.stderr)
    all_resources = fetch_all_app_resources(namespace, app_name)
    if not all_resources:
        print(f"No resources found for app '{app_name}' in namespace '{namespace}'", file=sys.stderr)
        sys.exit(1)
    if not output_dir:
        print("Error: --output is required for topology URLs", file=sys.stderr)
        sys.exit(1)
    base_path = Path(output_dir)
    ns_path = base_path / namespace
    place_secret_store_dependencies(namespace, all_resources)
    write_resources_to_namespace(all_resources, ns_path)
    print(f"\nOutput written to {ns_path}/", file=sys.stderr)

# ---------------------------------------------------------------------------
# Topologies file handler
# ---------------------------------------------------------------------------

def handle_topologies_file(topologies_file, args):
    if not args.output:
        print("Error: --output is required when using --topologies", file=sys.stderr)
        sys.exit(1)
    file_path = Path(topologies_file)
    if not file_path.exists():
        print(f"Error: Topologies file not found: {topologies_file}", file=sys.stderr)
        sys.exit(1)
    urls = [line.strip() for line in file_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not urls:
        print("Error: No URLs found in topologies file", file=sys.stderr)
        sys.exit(1)
    first_namespace, _ = parse_topology_url(urls[0])
    base_path = Path(args.output)
    ns_path = base_path / first_namespace

    for url in urls:
        namespace, app_name = parse_topology_url(url)
        if namespace != first_namespace:
            print(f"Error: URL {url} belongs to namespace '{namespace}', but expected '{first_namespace}'. All URLs must be from the same namespace.", file=sys.stderr)
            sys.exit(1)
        print(f"Fetching all resources for app '{app_name}' in namespace '{namespace}'...", file=sys.stderr)
        all_resources = fetch_all_app_resources(namespace, app_name)
        if not all_resources:
            print(f"Warning: No resources found for app '{app_name}' in namespace '{namespace}', skipping.", file=sys.stderr)
            continue
        place_secret_store_dependencies(namespace, all_resources)
        write_resources_to_namespace(all_resources, ns_path)

    print(f"\nOutput written to {ns_path}/", file=sys.stderr)

# ---------------------------------------------------------------------------
# Full namespace handler
# ---------------------------------------------------------------------------

def discover_apps_in_namespace(namespace):
    """
    Discover all unique app names in a namespace by looking at workloads
    and using get_app_id() which checks app.kubernetes.io/part-of, app,
    and falls back to the workload name.
    """
    all_workloads = fetch_all_workloads_in_namespace(namespace)
    app_names = set()
    for wl in all_workloads:
        app_id = get_app_id(wl)
        if app_id:
            app_names.add(app_id)
    return sorted(app_names)

def print_run_summary(namespace, all_workloads, shared_sink):
    """Print what a run captured, plus every resource that FAILED to fetch -
    so silently missing ConfigMaps/RBAC/NADs can no longer slip into the dump."""
    counts = {}
    for wl in all_workloads:
        kind = wl.get("kind", "Unknown")
        counts[kind] = counts.get(kind, 0) + 1
    shared_counts = {}
    for _, kind, _ in shared_sink:
        shared_counts[kind] = shared_counts.get(kind, 0) + 1
    print(f"\n=== Fetch summary for namespace '{namespace}' ===", file=sys.stderr)
    print("  workloads: " + (", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "none"), file=sys.stderr)
    print("  shared.yaml entries: " + (", ".join(f"{k}={v}" for k, v in sorted(shared_counts.items())) or "none"), file=sys.stderr)
    if FETCH_FAILURES:
        print(f"\n!!! {len(FETCH_FAILURES)} resource(s) FAILED to fetch and are MISSING from the dump:", file=sys.stderr)
        for failure in FETCH_FAILURES:
            print(f"  - {failure}", file=sys.stderr)
    else:
        print("  no fetch failures.", file=sys.stderr)


def handle_full_namespace(namespace, args):
    if not args.output:
        print("Error: --output is required when using --namespace", file=sys.stderr)
        sys.exit(1)
    print(f"Fetching all workloads in namespace '{namespace}'...", file=sys.stderr)
    all_workloads = fetch_all_workloads_in_namespace(namespace)
    if not all_workloads:
        print(f"No workloads found in namespace '{namespace}'", file=sys.stderr)
        sys.exit(1)

    app_ids = discover_apps_in_namespace(namespace)
    print(f"Found {len(app_ids)} microservice(s) from {len(all_workloads)} workload(s): {app_ids}", file=sys.stderr)

    base_path = Path(args.output)
    ns_path = base_path / namespace

    # Namespace-wide resources, fetched ONCE (shared across all apps).
    print("\nFetching namespace-wide resources (services, routes, RBAC, NADs, ...)...", file=sys.stderr)
    all_services = fetch_all_in_namespace(namespace, "services")
    all_routes = fetch_all_in_namespace(namespace, "routes")
    extras = fetch_namespace_scan(namespace)
    print(f"  found services={len(all_services)}, routes={len(all_routes)}, scan extras={len(extras)}", file=sys.stderr)

    shared_sink = []
    # Every app's microservice files, written only after the loop: whether a
    # SecretStore is shared depends on ALL of them (place_secret_store_dependencies).
    combined = {}
    matched_service_names = set()
    matched_route_names = set()

    # Attach HPAs to the microservice file of their scale target.
    hpas = [r for r in extras if r.get("kind") == "HorizontalPodAutoscaler"]
    other_extras = [r for r in extras if r.get("kind") != "HorizontalPodAutoscaler"]
    hpa_by_target = {}
    for hpa in hpas:
        target = ((hpa.get("spec") or {}).get("scaleTargetRef") or {}).get("name")
        if target:
            hpa_by_target.setdefault(target, []).append(hpa)
    placed_hpa = set()

    for app_id in app_ids:
        print(f"\nFetching all resources for '{app_id}' in namespace '{namespace}'...", file=sys.stderr)
        all_resources = fetch_all_app_resources(namespace, app_id, all_workloads=all_workloads,
                                                all_services=all_services, all_routes=all_routes)
        if not all_resources:
            print(f"Warning: No resources found for '{app_id}', skipping.", file=sys.stderr)
            continue
        for wl_name in list(all_resources):
            for hpa in hpa_by_target.get(wl_name, []):
                hpa_name = hpa.get("metadata", {}).get("name", "unknown")
                all_resources[wl_name]["resources"].append(
                    (hpa_name, "HorizontalPodAutoscaler", yaml.dump(hpa, default_flow_style=False, sort_keys=False)))
                placed_hpa.add(hpa_name)
        for entry in all_resources.values():
            for name, kind, _ in entry.get("resources", []):
                if kind == "Service":
                    matched_service_names.add(name)
                elif kind == "Route":
                    matched_route_names.add(name)
        shared_sink.extend(all_resources.pop(SHARED_KEY, {}).get("resources", []))
        combined.update(all_resources)

    # Everything else in the namespace goes to the combined shared.yaml:
    # RBAC, PDBs, ingresses, NADs, unmatched
    # HPAs, and all services/routes no workload claimed (e.g. the per-ordinal
    # LoadBalancer announce services that redis init scripts look up by host).
    # HPAs attached to a workload file above are kept out of shared.yaml;
    # the rest (target not a fetched workload) go to shared.yaml.
    for hpa in hpas:
        hpa_name = (hpa.get("metadata") or {}).get("name", "unknown")
        if hpa_name in placed_hpa:
            continue
        shared_sink.append((hpa_name, "HorizontalPodAutoscaler",
                            yaml.dump(hpa, default_flow_style=False, sort_keys=False)))
    for res in other_extras:
        name = res.get("metadata", {}).get("name", "unknown")
        kind = res.get("kind", "Unknown")
        shared_sink.append((name, kind, yaml.dump(res, default_flow_style=False, sort_keys=False)))
    for svc in all_services:
        name = svc.get("metadata", {}).get("name", "unknown")
        if name in matched_service_names:
            continue
        shared_sink.append((name, "Service", yaml.dump(svc, default_flow_style=False, sort_keys=False)))
        print(f"  [extra] Service '{name}' not claimed by any workload -> shared.yaml", file=sys.stderr)
    for route in all_routes:
        name = route.get("metadata", {}).get("name", "unknown")
        if name in matched_route_names:
            continue
        shared_sink.append((name, "Route", yaml.dump(route, default_flow_style=False, sort_keys=False)))
        print(f"  [extra] Route '{name}' not claimed by any service -> shared.yaml", file=sys.stderr)
    combined[SHARED_KEY] = {"workload": None, "resources": shared_sink}
    place_secret_store_dependencies(namespace, combined)
    del combined[SHARED_KEY]
    write_resources_to_namespace(combined, ns_path)
    write_shared_yaml(shared_sink, ns_path)

    print_run_summary(namespace, all_workloads, shared_sink)
    print(f"\nOutput written to {ns_path}/", file=sys.stderr)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Fetch and clean Kubernetes/OpenShift YAML resources to namespaces/ directory."
    )
    parser.add_argument("url", nargs="?", help="OpenShift console URL (single resource or topology/app view). Always wrap in quotes in bash/shell so query params (&, ?) reach this script intact.")
    parser.add_argument("--output", "-o", help="Output base folder. Resources go to <output>/<namespace>/.")
    parser.add_argument("--topologies", help="Path to a file containing one topology URL per line. All URLs must belong to the same namespace.")
    parser.add_argument("--namespace", "-n", dest="namespace", help="Fetch ALL apps from an OpenShift namespace/project. Discovers apps via the app.kubernetes.io/part-of label, then the app label (see get_app_id).")
    parser.add_argument("--kubeconfig", help="Kubeconfig file to pass to every oc call (use this when the default oc config cannot reach the cluster).")
    args = parser.parse_args()

    if args.kubeconfig:
        global KUBECONFIG_PATH
        KUBECONFIG_PATH = args.kubeconfig

    if args.topologies:
        handle_topologies_file(args.topologies, args)
    elif args.namespace:
        handle_full_namespace(args.namespace, args)
    elif args.url:
        url = args.url
        if "/topology/ns/" in url and "?" not in url:
            import warnings
            warnings.warn(
                "URL contains no query parameters. You may need to wrap the URL in quotes "
                "to prevent your shell from interpreting '?' and '&' as wildcards.\n"
                "Example: python correct.py -o namespaces 'https://...?view=graph&selectId=...'",
                stacklevel=1,
            )
        if is_topology_url(url):
            handle_topology_url(url, args.output)
        else:
            handle_single_resource(url)
    else:
        parser.print_help()
        sys.exit(1)

if __name__ == "__main__":
    main()
