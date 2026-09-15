#!/usr/bin/env python3
"""
generate_showcase_extra.py
============================
Adds 10 more hand-specified, REAL-image microservices to each of
namespaces-showcase/'s 5 namespaces (identity-auth, shop-web, platform-data,
platform-ops, background-jobs) — 50 new files total, on top of the 12 that
already exist there.

Unlike generate_mock_environment.py (which fabricates random OpenShift-dump
noise to stress-test the converter's conflict resolution at scale), every
image here is a real, publicly pullable one, and every raw manifest matches
namespaces-showcase's existing hand-authored house style: plain `app: <name>`
labels/selectors (no OpenShift dump noise), real resource requests/limits,
readiness/liveness probes where the image supports one cheaply, ClusterIP-only
Services (no Ingress/Route — none of these are meant to be publicly exposed),
and ReadWriteOnce-only PVCs (the one StorageClass on this cluster, k3s's
bundled 'local-path', does not support ReadWriteMany — see shop-web/shared.yaml
for the same lesson learned the hard way on the existing fixture).

Usage:
    python generate_showcase_extra.py --output namespaces-showcase
"""
from __future__ import annotations

import argparse
from pathlib import Path

import yaml


def write_multidoc(path: Path, docs: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump_all(docs, sort_keys=False, default_flow_style=False, width=100), encoding="utf-8")


def resources(cpu_req: str, mem_req: str, cpu_lim: str, mem_lim: str) -> dict:
    return {"requests": {"cpu": cpu_req, "memory": mem_req}, "limits": {"cpu": cpu_lim, "memory": mem_lim}}


def service_account(name: str) -> dict:
    return {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": name}}


def configmap(name: str, data: dict) -> dict:
    return {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": name}, "data": data}


def secret(name: str, string_data: dict) -> dict:
    return {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": name}, "type": "Opaque", "stringData": string_data}


def service(name: str, app: str, ports: list[tuple[str, int, str]], headless: bool = False) -> dict:
    spec = {
        "type": "ClusterIP",
        "selector": {"app": app},
        "ports": [{"name": n, "port": p, "targetPort": t} for n, p, t in ports],
    }
    if headless:
        spec["clusterIP"] = None
    return {"apiVersion": "v1", "kind": "Service", "metadata": {"name": name}, "spec": spec}


def _probe(kind: str, **kw) -> dict:
    return {kind: kw.pop("check"), **kw}


def deployment(
    name: str, image: str, *, container: str | None = None, replicas: int = 1,
    command: list[str] | None = None, args: list[str] | None = None,
    env: list[dict] | None = None, env_from: list[str] | None = None,
    ports: list[tuple[str, int]] | None = None,
    volume_mounts: list[dict] | None = None, volumes: list[dict] | None = None,
    res: dict | None = None, readiness: dict | None = None, liveness: dict | None = None,
    extra_pod_spec: dict | None = None,
) -> dict:
    c: dict = {"name": container or name, "image": image}
    if command:
        c["command"] = command
    if args:
        c["args"] = args
    if env:
        c["env"] = env
    if env_from:
        c["envFrom"] = [{"configMapRef": {"name": n}} for n in env_from]
    if ports:
        c["ports"] = [{"name": n, "containerPort": p} for n, p in ports]
    c["resources"] = res or resources("50m", "64Mi", "250m", "256Mi")
    if volume_mounts:
        c["volumeMounts"] = volume_mounts
    if readiness:
        c["readinessProbe"] = readiness
    if liveness:
        c["livenessProbe"] = liveness
    pod_spec: dict = {"serviceAccountName": name, "containers": [c]}
    if volumes:
        pod_spec["volumes"] = volumes
    if extra_pod_spec:
        pod_spec.update(extra_pod_spec)
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": name, "labels": {"app": name}},
        "spec": {
            "replicas": replicas,
            "selector": {"matchLabels": {"app": name}},
            "template": {"metadata": {"labels": {"app": name}}, "spec": pod_spec},
        },
    }


def statefulset(
    name: str, image: str, *, container: str | None = None,
    command: list[str] | None = None, args: list[str] | None = None,
    env: list[dict] | None = None,
    ports: list[tuple[str, int]] | None = None,
    volume_mounts: list[dict] | None = None, volumes: list[dict] | None = None,
    pvc_name: str = "data", pvc_size: str = "1Gi",
    res: dict | None = None, readiness: dict | None = None, liveness: dict | None = None,
    init_containers: list[dict] | None = None,
) -> dict:
    c: dict = {"name": container or name, "image": image}
    if command:
        c["command"] = command
    if args:
        c["args"] = args
    if env:
        c["env"] = env
    if ports:
        c["ports"] = [{"name": n, "containerPort": p} for n, p in ports]
    c["resources"] = res or resources("100m", "128Mi", "500m", "512Mi")
    if volume_mounts:
        c["volumeMounts"] = volume_mounts
    if readiness:
        c["readinessProbe"] = readiness
    if liveness:
        c["livenessProbe"] = liveness
    pod_spec: dict = {"containers": [c]}
    if init_containers:
        pod_spec["initContainers"] = init_containers
    if volumes:
        pod_spec["volumes"] = volumes
    return {
        "apiVersion": "apps/v1",
        "kind": "StatefulSet",
        "metadata": {"name": name, "labels": {"app": name}},
        "spec": {
            "serviceName": name,
            "replicas": 1,
            "selector": {"matchLabels": {"app": name}},
            "template": {"metadata": {"labels": {"app": name}}, "spec": pod_spec},
            "volumeClaimTemplates": [{
                "metadata": {"name": pvc_name},
                "spec": {"accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": pvc_size}}},
            }],
        },
    }


def daemonset(
    name: str, image: str, *, container: str | None = None,
    args: list[str] | None = None, env_from: list[str] | None = None,
    ports: list[tuple[str, int]] | None = None,
    volume_mounts: list[dict] | None = None, volumes: list[dict] | None = None,
    res: dict | None = None, host_network: bool = False, host_pid: bool = False,
    automount_service_account_token: bool = True,
) -> dict:
    c: dict = {"name": container or name, "image": image}
    if args:
        c["args"] = args
    if env_from:
        c["envFrom"] = [{"configMapRef": {"name": n}} for n in env_from]
    if ports:
        c["ports"] = [{"name": n, "containerPort": p} for n, p in ports]
    c["resources"] = res or resources("50m", "64Mi", "200m", "128Mi")
    if volume_mounts:
        c["volumeMounts"] = volume_mounts
    pod_spec: dict = {
        "serviceAccountName": name,
        "tolerations": [{"operator": "Exists"}],
        "containers": [c],
    }
    if not automount_service_account_token:
        pod_spec["automountServiceAccountToken"] = False
    if host_network:
        pod_spec["hostNetwork"] = True
    if host_pid:
        pod_spec["hostPID"] = True
    if volumes:
        pod_spec["volumes"] = volumes
    return {
        "apiVersion": "apps/v1",
        "kind": "DaemonSet",
        "metadata": {"name": name, "labels": {"app": name}},
        "spec": {
            "selector": {"matchLabels": {"app": name}},
            "template": {"metadata": {"labels": {"app": name}}, "spec": pod_spec},
        },
    }


def cronjob(name: str, image: str, schedule: str, command: list[str], args: list[str],
            res: dict | None = None) -> dict:
    return {
        "apiVersion": "batch/v1",
        "kind": "CronJob",
        "metadata": {"name": name},
        "spec": {
            "schedule": schedule,
            "concurrencyPolicy": "Forbid",
            "successfulJobsHistoryLimit": 3,
            "failedJobsHistoryLimit": 1,
            "jobTemplate": {"spec": {
                "backoffLimit": 2,
                "template": {"spec": {
                    "restartPolicy": "Never",
                    "containers": [{
                        "name": name, "image": image, "command": command, "args": args,
                        "resources": res or resources("10m", "16Mi", "50m", "32Mi"),
                    }],
                }},
            }},
        },
    }


def job(name: str, image: str, command: list[str], args: list[str], res: dict | None = None) -> dict:
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": name},
        "spec": {
            "backoffLimit": 1,
            "template": {"spec": {
                "restartPolicy": "Never",
                "containers": [{
                    "name": name, "image": image, "command": command, "args": args,
                    "resources": res or resources("10m", "16Mi", "50m", "32Mi"),
                }],
            }},
        },
    }


def echo_args(verb: str) -> list[str]:
    return [f"echo {verb}; sleep 2; echo done"]


# ---------------------------------------------------------------------------
# identity-auth
# ---------------------------------------------------------------------------

def build_identity_auth(ns_dir: Path) -> None:
    write_multidoc(ns_dir / "hydra.yaml", [
        deployment("hydra", "oryd/hydra:v2.2.0",
                   command=["hydra", "serve", "all", "--dev"],
                   env=[{"name": "DSN", "value": "memory"},
                        {"name": "SECRETS_SYSTEM", "value": "showcase-hydra-system-secret-change-me"},
                        {"name": "URLS_SELF_ISSUER", "value": "http://hydra.identity-auth.svc.cluster.local:4444"}],
                   ports=[("public", 4444), ("admin", 4445)],
                   readiness={"httpGet": {"path": "/health/ready", "port": "admin"}, "initialDelaySeconds": 10, "periodSeconds": 10},
                   liveness={"httpGet": {"path": "/health/alive", "port": "admin"}, "initialDelaySeconds": 15, "periodSeconds": 20}),
        service_account("hydra"),
        service("hydra", "hydra", [("public", 4444, "public"), ("admin", 4445, "admin")]),
    ])

    write_multidoc(ns_dir / "opa.yaml", [
        deployment("opa", "openpolicyagent/opa:0.65.0",
                   command=["/opa", "run", "--server", "--addr=0.0.0.0:8181"],
                   ports=[("http", 8181)],
                   readiness={"httpGet": {"path": "/health", "port": "http"}, "initialDelaySeconds": 5, "periodSeconds": 10},
                   liveness={"httpGet": {"path": "/health", "port": "http"}, "initialDelaySeconds": 10, "periodSeconds": 20},
                   res=resources("50m", "64Mi", "200m", "128Mi")),
        service_account("opa"),
        service("opa", "opa", [("http", 8181, "http")]),
    ])

    write_multidoc(ns_dir / "openldap.yaml", [
        statefulset("openldap", "osixia/openldap:1.5.0",
                    env=[{"name": "LDAP_ORGANISATION", "value": "Showcase Inc"},
                         {"name": "LDAP_DOMAIN", "value": "showcase.example.com"},
                         {"name": "LDAP_ADMIN_PASSWORD", "valueFrom": {"secretKeyRef": {"name": "openldap-credentials", "key": "admin-password"}}}],
                    ports=[("ldap", 389)],
                    volume_mounts=[{"name": "data", "mountPath": "/var/lib/ldap"}],
                    readiness={"exec": {"command": ["ldapsearch", "-x", "-H", "ldap://localhost", "-b", "", "-s", "base"]}, "initialDelaySeconds": 15, "periodSeconds": 15},
                    res=resources("50m", "128Mi", "250m", "256Mi")),
        secret("openldap-credentials", {"admin-password": "showcase-ldap-change-me"}),
        service("openldap", "openldap", [("ldap", 389, "ldap")], headless=True),
    ])

    write_multidoc(ns_dir / "lldap.yaml", [
        statefulset("lldap", "lldap/lldap:stable",
                    env=[{"name": "LLDAP_LDAP_BASE_DN", "value": "dc=showcase,dc=example,dc=com"},
                         {"name": "LLDAP_LDAP_USER_PASS", "valueFrom": {"secretKeyRef": {"name": "lldap-credentials", "key": "admin-password"}}},
                         {"name": "LLDAP_JWT_SECRET", "valueFrom": {"secretKeyRef": {"name": "lldap-credentials", "key": "jwt-secret"}}}],
                    ports=[("ldap", 3890), ("http", 17170)],
                    volume_mounts=[{"name": "data", "mountPath": "/data"}],
                    readiness={"httpGet": {"path": "/health", "port": "http"}, "initialDelaySeconds": 10, "periodSeconds": 10},
                    res=resources("30m", "64Mi", "150m", "128Mi")),
        secret("lldap-credentials", {"admin-password": "showcase-lldap-change-me",
                                     "jwt-secret": "showcase-lldap-jwt-secret-change-me-32ch"}),
        service("lldap", "lldap", [("ldap", 3890, "ldap"), ("http", 17170, "http")], headless=True),
    ])

    write_multidoc(ns_dir / "keto.yaml", [
        deployment("keto", "oryd/keto:v0.12.0",
                   command=["keto", "serve", "-c", "/etc/keto/keto.yml"],
                   ports=[("read", 4466), ("write", 4467)],
                   volume_mounts=[{"name": "config", "mountPath": "/etc/keto"}],
                   volumes=[{"name": "config", "configMap": {"name": "keto-config"}}],
                   readiness={"tcpSocket": {"port": "read"}, "initialDelaySeconds": 10, "periodSeconds": 10},
                   res=resources("30m", "64Mi", "150m", "128Mi")),
        configmap("keto-config", {"keto.yml": (
            "dsn: memory\n"
            "namespaces:\n"
            "  - id: 0\n"
            "    name: files\n"
            "serve:\n"
            "  read:\n"
            "    port: 4466\n"
            "  write:\n"
            "    port: 4467\n"
        )}),
        service_account("keto"),
        service("keto", "keto", [("read", 4466, "read"), ("write", 4467, "write")]),
    ])

    write_multidoc(ns_dir / "glauth.yaml", [
        # No command/args override: the image's own ENTRYPOINT is dumb-init,
        # wrapping a CMD that already execs the glauth binary correctly (its
        # exact location isn't on $PATH, confirmed on the real cluster —
        # overriding `command` to a bare "glauth" broke that). Its default
        # CMD already points at /app/config/config.cfg, so the config is
        # mounted there instead of a custom path/flag.
        deployment("glauth", "glauth/glauth:latest",
                   ports=[("ldap", 3893)],
                   volume_mounts=[{"name": "config", "mountPath": "/app/config"}],
                   volumes=[{"name": "config", "configMap": {"name": "glauth-config"}}],
                   readiness={"tcpSocket": {"port": "ldap"}, "initialDelaySeconds": 5, "periodSeconds": 10},
                   res=resources("20m", "32Mi", "100m", "64Mi")),
        configmap("glauth-config", {"config.cfg": (
            "[ldap]\n  enabled = true\n  listen = \"0.0.0.0:3893\"\n\n"
            # v2.5.2 errors out at startup if [ldaps] is absent entirely,
            # even to just disable it — confirmed on the real cluster
            # ("no LDAPS bind address was specified").
            "[ldaps]\n  enabled = false\n\n"
            "[backend]\n  datastore = \"config\"\n  baseDN = \"dc=showcase,dc=example,dc=com\"\n\n"
            "[[users]]\n  name = \"demo\"\n  uidnumber = 5001\n  primarygroup = 5501\n"
            "  passsha256 = \"6478579e37aff45f013e14eeb30b3cc56c72ccdc310123bcdf1a6b990f1b579\"\n\n"
            "[[groups]]\n  name = \"showcase\"\n  gidnumber = 5501\n"
        )}),
        service_account("glauth"),
        service("glauth", "glauth", [("ldap", 3893, "ldap")]),
    ])

    write_multidoc(ns_dir / "cfssl.yaml", [
        deployment("cfssl", "cfssl/cfssl:latest",
                   command=["/bin/sh", "-c"],
                   args=[
                       "cfssl genkey -initca /etc/cfssl/ca-csr.json | cfssljson -bare /tmp/ca && "
                       "cfssl serve -address=0.0.0.0 -port=8888 -ca=/tmp/ca.pem -ca-key=/tmp/ca-key.pem"
                   ],
                   ports=[("http", 8888)],
                   volume_mounts=[{"name": "config", "mountPath": "/etc/cfssl"}],
                   volumes=[{"name": "config", "configMap": {"name": "cfssl-config"}}],
                   readiness={"tcpSocket": {"port": "http"}, "initialDelaySeconds": 10, "periodSeconds": 10},
                   res=resources("30m", "64Mi", "150m", "128Mi")),
        configmap("cfssl-config", {"ca-csr.json": (
            '{"CN": "showcase-demo-ca", "key": {"algo": "rsa", "size": 2048}, '
            '"names": [{"O": "Showcase Inc"}]}\n'
        )}),
        service_account("cfssl"),
        service("cfssl", "cfssl", [("http", 8888, "http")]),
    ])

    write_multidoc(ns_dir / "mock-oauth2-server.yaml", [
        deployment("mock-oauth2-server", "ghcr.io/navikt/mock-oauth2-server:2.1.10",
                   env=[{"name": "SERVER_PORT", "value": "8080"}],
                   ports=[("http", 8080)],
                   readiness={"httpGet": {"path": "/default/.well-known/openid-configuration", "port": "http"}, "initialDelaySeconds": 10, "periodSeconds": 10},
                   liveness={"httpGet": {"path": "/default/.well-known/openid-configuration", "port": "http"}, "initialDelaySeconds": 15, "periodSeconds": 20},
                   # Confirmed on the real cluster: 256Mi OOMKilled it — a
                   # JVM's own startup/classloading overhead alone exceeds
                   # that before it ever gets to serving traffic.
                   res=resources("100m", "320Mi", "250m", "512Mi")),
        service_account("mock-oauth2-server"),
        service("mock-oauth2-server", "mock-oauth2-server", [("http", 8080, "http")]),
    ])

    write_multidoc(ns_dir / "ldap-account-manager.yaml", [
        deployment("ldap-account-manager", "ldapaccountmanager/lam:latest",
                   env=[{"name": "LDAP_SERVER", "value": "ldap://openldap.identity-auth.svc.cluster.local:389"},
                        {"name": "LDAP_DOMAIN", "value": "showcase.example.com"}],
                   ports=[("http", 80)],
                   res=resources("30m", "64Mi", "150m", "128Mi")),
        service_account("ldap-account-manager"),
        service("ldap-account-manager", "ldap-account-manager", [("http", 80, "http")]),
    ])

    write_multidoc(ns_dir / "directory-replica.yaml", [
        # bitnami/openldap doesn't exist on Docker Hub under :2.6 OR :latest
        # — confirmed on the real cluster (404 on both). Bitnami's free-tier
        # catalog moved most images behind bitnamilegacy/paid tiers; switched
        # to osixia/openldap, the same image openldap.yaml already proves
        # works, just with a different LDAP_DOMAIN so this is a distinct
        # real second directory, not a duplicate of it.
        statefulset("directory-replica", "osixia/openldap:1.5.0",
                    env=[{"name": "LDAP_ORGANISATION", "value": "Showcase Replica Inc"},
                         {"name": "LDAP_DOMAIN", "value": "replica.showcase.example.com"},
                         {"name": "LDAP_ADMIN_PASSWORD", "valueFrom": {"secretKeyRef": {"name": "directory-replica-credentials", "key": "admin-password"}}}],
                    ports=[("ldap", 389)],
                    volume_mounts=[{"name": "data", "mountPath": "/var/lib/ldap"}],
                    readiness={"exec": {"command": ["ldapsearch", "-x", "-H", "ldap://localhost", "-b", "", "-s", "base"]}, "initialDelaySeconds": 15, "periodSeconds": 15},
                    res=resources("50m", "128Mi", "250m", "256Mi")),
        secret("directory-replica-credentials", {"admin-password": "showcase-replica-change-me"}),
        service("directory-replica", "directory-replica", [("ldap", 389, "ldap")], headless=True),
    ])


# ---------------------------------------------------------------------------
# shop-web
# ---------------------------------------------------------------------------

def build_shop_web(ns_dir: Path) -> None:
    write_multidoc(ns_dir / "search.yaml", [
        statefulset("search", "opensearchproject/opensearch:2.15.0",
                    env=[{"name": "discovery.type", "value": "single-node"},
                         {"name": "DISABLE_SECURITY_PLUGIN", "value": "true"},
                         {"name": "OPENSEARCH_JAVA_OPTS", "value": "-Xms256m -Xmx256m"}],
                    ports=[("http", 9200), ("transport", 9300)],
                    volume_mounts=[{"name": "data", "mountPath": "/usr/share/opensearch/data"}],
                    readiness={"httpGet": {"path": "/_cluster/health", "port": "http"}, "initialDelaySeconds": 30, "periodSeconds": 15},
                    res=resources("250m", "768Mi", "1", "1Gi"),
                    pvc_size="2Gi"),
        service("search", "search", [("http", 9200, "http"), ("transport", 9300, "transport")], headless=True),
    ])

    write_multidoc(ns_dir / "cache-edge.yaml", [
        deployment("cache-edge", "varnish:7.5",
                   args=["-F", "-f", "/etc/varnish/default.vcl", "-a", "0.0.0.0:8080", "-s", "malloc,64m"],
                   ports=[("http", 8080)],
                   volume_mounts=[{"name": "config", "mountPath": "/etc/varnish"}],
                   volumes=[{"name": "config", "configMap": {"name": "cache-edge-vcl"}}],
                   readiness={"tcpSocket": {"port": "http"}, "initialDelaySeconds": 5, "periodSeconds": 10},
                   res=resources("50m", "96Mi", "200m", "192Mi")),
        configmap("cache-edge-vcl", {"default.vcl": (
            "vcl 4.1;\n"
            "backend default {\n"
            "  .host = \"storefront.shop-web.svc.cluster.local\";\n"
            "  .port = \"80\";\n"
            "}\n"
        )}),
        service_account("cache-edge"),
        service("cache-edge", "cache-edge", [("http", 8080, "http")]),
    ])

    write_multidoc(ns_dir / "image-resizer.yaml", [
        deployment("image-resizer", "darthsim/imgproxy:v3.21",
                   env=[{"name": "IMGPROXY_BIND", "value": ":8080"}],
                   ports=[("http", 8080)],
                   readiness={"httpGet": {"path": "/health", "port": "http"}, "initialDelaySeconds": 5, "periodSeconds": 10},
                   res=resources("50m", "96Mi", "250m", "192Mi")),
        service_account("image-resizer"),
        service("image-resizer", "image-resizer", [("http", 8080, "http")]),
    ])

    write_multidoc(ns_dir / "cart-store.yaml", [
        deployment("cart-store", "redis:7-alpine",
                   command=["redis-server", "--save", "\"\"", "--maxmemory", "128mb", "--maxmemory-policy", "allkeys-lru"],
                   ports=[("redis", 6379)],
                   readiness={"exec": {"command": ["redis-cli", "ping"]}, "initialDelaySeconds": 5, "periodSeconds": 10},
                   liveness={"exec": {"command": ["redis-cli", "ping"]}, "initialDelaySeconds": 10, "periodSeconds": 20},
                   res=resources("30m", "64Mi", "150m", "192Mi")),
        service_account("cart-store"),
        service("cart-store", "cart-store", [("redis", 6379, "redis")]),
    ])

    write_multidoc(ns_dir / "reviews-store.yaml", [
        statefulset("reviews-store", "mongo:7",
                    env=[{"name": "MONGO_INITDB_ROOT_USERNAME", "value": "appuser"},
                         {"name": "MONGO_INITDB_ROOT_PASSWORD", "valueFrom": {"secretKeyRef": {"name": "reviews-store-credentials", "key": "password"}}}],
                    ports=[("mongo", 27017)],
                    volume_mounts=[{"name": "data", "mountPath": "/data/db"}],
                    readiness={"exec": {"command": ["mongosh", "--quiet", "--eval", "db.adminCommand('ping')"]}, "initialDelaySeconds": 15, "periodSeconds": 15},
                    res=resources("100m", "256Mi", "500m", "512Mi")),
        secret("reviews-store-credentials", {"password": "showcase-reviews-change-me"}),
        service("reviews-store", "reviews-store", [("mongo", 27017, "mongo")], headless=True),
    ])

    write_multidoc(ns_dir / "payments-mock.yaml", [
        deployment("payments-mock", "stripe/stripe-mock:latest",
                   ports=[("http", 12111), ("https", 12112)],
                   readiness={"tcpSocket": {"port": "http"}, "initialDelaySeconds": 5, "periodSeconds": 10},
                   res=resources("30m", "64Mi", "150m", "128Mi")),
        service_account("payments-mock"),
        service("payments-mock", "payments-mock", [("http", 12111, "http"), ("https", 12112, "https")]),
    ])

    write_multidoc(ns_dir / "message-bus.yaml", [
        statefulset("message-bus", "rabbitmq:3.13-management-alpine",
                    env=[{"name": "RABBITMQ_DEFAULT_USER", "value": "appuser"},
                         {"name": "RABBITMQ_DEFAULT_PASS", "valueFrom": {"secretKeyRef": {"name": "message-bus-credentials", "key": "password"}}}],
                    ports=[("amqp", 5672), ("management", 15672)],
                    volume_mounts=[{"name": "data", "mountPath": "/var/lib/rabbitmq"}],
                    readiness={"exec": {"command": ["rabbitmq-diagnostics", "-q", "ping"]}, "initialDelaySeconds": 20, "periodSeconds": 15},
                    res=resources("100m", "256Mi", "500m", "512Mi")),
        secret("message-bus-credentials", {"password": "showcase-rabbitmq-change-me"}),
        service("message-bus", "message-bus", [("amqp", 5672, "amqp"), ("management", 15672, "management")], headless=True),
    ])

    write_multidoc(ns_dir / "wishlist-cache.yaml", [
        deployment("wishlist-cache", "memcached:1.6-alpine",
                   args=["-m", "64"],
                   ports=[("memcached", 11211)],
                   readiness={"tcpSocket": {"port": "memcached"}, "initialDelaySeconds": 5, "periodSeconds": 10},
                   res=resources("20m", "80Mi", "100m", "128Mi")),
        service_account("wishlist-cache"),
        service("wishlist-cache", "wishlist-cache", [("memcached", 11211, "memcached")]),
    ])

    write_multidoc(ns_dir / "promo-engine.yaml", [
        deployment("promo-engine", "httpd:2.4-alpine",
                   ports=[("http", 80)],
                   volume_mounts=[{"name": "site", "mountPath": "/usr/local/apache2/htdocs"}],
                   volumes=[{"name": "site", "configMap": {"name": "promo-engine-site"}}],
                   readiness={"httpGet": {"path": "/", "port": "http"}, "initialDelaySeconds": 5, "periodSeconds": 10},
                   res=resources("30m", "64Mi", "150m", "128Mi")),
        configmap("promo-engine-site", {"index.html": "<!doctype html>\n<html><body><h1>Active Promotions</h1></body></html>\n"}),
        service_account("promo-engine"),
        service("promo-engine", "promo-engine", [("http", 80, "http")]),
    ])

    write_multidoc(ns_dir / "inventory-sync.yaml", [
        cronjob("inventory-sync", "busybox:1.36", "*/15 * * * *",
                ["/bin/sh", "-c"], echo_args("syncing inventory levels")),
    ])


# ---------------------------------------------------------------------------
# platform-data
# ---------------------------------------------------------------------------

def build_platform_data(ns_dir: Path) -> None:
    write_multidoc(ns_dir / "mysql.yaml", [
        statefulset("mysql", "mysql:8.4",
                    env=[{"name": "MYSQL_DATABASE", "value": "appdb"},
                         {"name": "MYSQL_USER", "value": "appuser"},
                         {"name": "MYSQL_PASSWORD", "valueFrom": {"secretKeyRef": {"name": "mysql-credentials", "key": "password"}}},
                         {"name": "MYSQL_ROOT_PASSWORD", "valueFrom": {"secretKeyRef": {"name": "mysql-credentials", "key": "root-password"}}}],
                    ports=[("mysql", 3306)],
                    volume_mounts=[{"name": "data", "mountPath": "/var/lib/mysql"}],
                    readiness={"exec": {"command": ["mysqladmin", "ping", "-h", "127.0.0.1"]}, "initialDelaySeconds": 20, "periodSeconds": 15},
                    res=resources("100m", "256Mi", "500m", "512Mi")),
        secret("mysql-credentials", {"password": "showcase-mysql-change-me", "root-password": "showcase-mysql-root-change-me"}),
        service("mysql", "mysql", [("mysql", 3306, "mysql")], headless=True),
    ])

    write_multidoc(ns_dir / "cockroachdb.yaml", [
        statefulset("cockroachdb", "cockroachdb/cockroach:v24.1.5",
                    command=["/cockroach/cockroach"],
                    args=["start-single-node", "--insecure", "--store=/cockroach/cockroach-data"],
                    ports=[("sql", 26257), ("http", 8080)],
                    volume_mounts=[{"name": "data", "mountPath": "/cockroach/cockroach-data"}],
                    readiness={"httpGet": {"path": "/health?ready=1", "port": "http"}, "initialDelaySeconds": 15, "periodSeconds": 15},
                    res=resources("100m", "256Mi", "500m", "512Mi")),
        service("cockroachdb", "cockroachdb", [("sql", 26257, "sql"), ("http", 8080, "http")], headless=True),
    ])

    write_multidoc(ns_dir / "cassandra.yaml", [
        statefulset("cassandra", "cassandra:5.0",
                    env=[{"name": "MAX_HEAP_SIZE", "value": "512M"},
                         {"name": "HEAP_NEWSIZE", "value": "100M"},
                         {"name": "CASSANDRA_CLUSTER_NAME", "value": "showcase"},
                         {"name": "CASSANDRA_SEEDS", "value": "cassandra-0.cassandra.platform-data.svc.cluster.local"}],
                    ports=[("cql", 9042), ("intra", 7000)],
                    volume_mounts=[{"name": "data", "mountPath": "/var/lib/cassandra"}],
                    readiness={"exec": {"command": ["cqlsh", "-e", "describe keyspaces"]}, "initialDelaySeconds": 60, "periodSeconds": 30},
                    # Confirmed on the real cluster: 1Gi limit OOMKilled it —
                    # Cassandra's off-heap (MaxDirectMemorySize) sizes itself
                    # off the HOST's total memory, not the container's cgroup
                    # limit, so on a 16GB node it reserved ~3.4G of direct
                    # memory alone regardless of MAX_HEAP_SIZE. 2Gi is still
                    # tight for that JVM but survives single-node idle load.
                    res=resources("250m", "1Gi", "1", "2Gi"),
                    pvc_size="2Gi"),
        service("cassandra", "cassandra", [("cql", 9042, "cql"), ("intra", 7000, "intra")], headless=True),
    ])

    write_multidoc(ns_dir / "nats.yaml", [
        deployment("nats", "nats:2.10-alpine",
                   # -m enables the monitoring HTTP port — without it :8222
                   # never listens, so the readiness probe (and the Service
                   # port) had nothing to connect to. Confirmed on the real
                   # cluster: server logged "ready" but pod stayed 0/1.
                   args=["-js", "-m", "8222"],
                   ports=[("client", 4222), ("monitor", 8222)],
                   readiness={"httpGet": {"path": "/healthz", "port": "monitor"}, "initialDelaySeconds": 5, "periodSeconds": 10},
                   res=resources("50m", "96Mi", "250m", "192Mi")),
        service_account("nats"),
        service("nats", "nats", [("client", 4222, "client"), ("monitor", 8222, "monitor")]),
    ])

    write_multidoc(ns_dir / "kafka.yaml", [
        statefulset("kafka", "apache/kafka:3.7.0",
                    env=[
                        {"name": "KAFKA_NODE_ID", "value": "1"},
                        {"name": "KAFKA_PROCESS_ROLES", "value": "broker,controller"},
                        {"name": "KAFKA_LISTENERS", "value": "PLAINTEXT://:9092,CONTROLLER://:9093"},
                        {"name": "KAFKA_ADVERTISED_LISTENERS", "value": "PLAINTEXT://kafka.platform-data.svc.cluster.local:9092"},
                        {"name": "KAFKA_CONTROLLER_LISTENER_NAMES", "value": "CONTROLLER"},
                        {"name": "KAFKA_CONTROLLER_QUORUM_VOTERS", "value": "1@localhost:9093"},
                        {"name": "KAFKA_INTER_BROKER_LISTENER_NAME", "value": "PLAINTEXT"},
                        {"name": "KAFKA_LISTENER_SECURITY_PROTOCOL_MAP", "value": "PLAINTEXT:PLAINTEXT,CONTROLLER:PLAINTEXT"},
                        {"name": "CLUSTER_ID", "value": "ShowcaseKRaftCluster1"},
                    ],
                    ports=[("broker", 9092), ("controller", 9093)],
                    volume_mounts=[{"name": "data", "mountPath": "/var/lib/kafka/data"}],
                    readiness={"tcpSocket": {"port": "broker"}, "initialDelaySeconds": 30, "periodSeconds": 15},
                    res=resources("250m", "768Mi", "1", "1Gi"),
                    pvc_size="2Gi"),
        service("kafka", "kafka", [("broker", 9092, "broker"), ("controller", 9093, "controller")], headless=True),
    ])

    write_multidoc(ns_dir / "minio.yaml", [
        statefulset("minio", "minio/minio:latest",
                    command=["minio", "server", "/data", "--console-address", ":9001"],
                    env=[{"name": "MINIO_ROOT_USER", "value": "appuser"},
                         {"name": "MINIO_ROOT_PASSWORD", "valueFrom": {"secretKeyRef": {"name": "minio-credentials", "key": "password"}}}],
                    ports=[("api", 9000), ("console", 9001)],
                    volume_mounts=[{"name": "data", "mountPath": "/data"}],
                    readiness={"httpGet": {"path": "/minio/health/ready", "port": "api"}, "initialDelaySeconds": 15, "periodSeconds": 15},
                    res=resources("100m", "256Mi", "500m", "512Mi"),
                    pvc_size="2Gi"),
        secret("minio-credentials", {"password": "showcase-minio-change-me"}),
        service("minio", "minio", [("api", 9000, "api"), ("console", 9001, "console")], headless=True),
    ])

    write_multidoc(ns_dir / "elasticsearch.yaml", [
        statefulset("elasticsearch", "elasticsearch:8.15.0",
                    env=[{"name": "discovery.type", "value": "single-node"},
                         {"name": "xpack.security.enabled", "value": "false"},
                         {"name": "ES_JAVA_OPTS", "value": "-Xms512m -Xmx512m"}],
                    ports=[("http", 9200), ("transport", 9300)],
                    volume_mounts=[{"name": "data", "mountPath": "/usr/share/elasticsearch/data"}],
                    readiness={"httpGet": {"path": "/_cluster/health", "port": "http"}, "initialDelaySeconds": 30, "periodSeconds": 15},
                    res=resources("250m", "768Mi", "1", "1Gi"),
                    pvc_size="2Gi"),
        service("elasticsearch", "elasticsearch", [("http", 9200, "http"), ("transport", 9300, "transport")], headless=True),
    ])

    write_multidoc(ns_dir / "influxdb.yaml", [
        statefulset("influxdb", "influxdb:2.7",
                    env=[{"name": "DOCKER_INFLUXDB_INIT_MODE", "value": "setup"},
                         {"name": "DOCKER_INFLUXDB_INIT_USERNAME", "value": "appuser"},
                         {"name": "DOCKER_INFLUXDB_INIT_PASSWORD", "valueFrom": {"secretKeyRef": {"name": "influxdb-credentials", "key": "password"}}},
                         {"name": "DOCKER_INFLUXDB_INIT_ORG", "value": "showcase"},
                         {"name": "DOCKER_INFLUXDB_INIT_BUCKET", "value": "metrics"}],
                    ports=[("http", 8086)],
                    volume_mounts=[{"name": "data", "mountPath": "/var/lib/influxdb2"}],
                    readiness={"httpGet": {"path": "/health", "port": "http"}, "initialDelaySeconds": 15, "periodSeconds": 15},
                    res=resources("100m", "256Mi", "500m", "512Mi")),
        secret("influxdb-credentials", {"password": "showcase-influx-change-me"}),
        service("influxdb", "influxdb", [("http", 8086, "http")], headless=True),
    ])

    write_multidoc(ns_dir / "etcd.yaml", [
        # bitnami/etcd:3.5 doesn't exist on Docker Hub — confirmed on the
        # real cluster (404), same Bitnami catalog issue as directory-replica.
        # Switched to the real upstream image, which needs an explicit
        # single-node cluster invocation rather than Bitnami's env-var
        # wrapper.
        statefulset("etcd", "quay.io/coreos/etcd:v3.5.15",
                    command=["etcd"],
                    args=[
                        "--name=etcd0",
                        "--data-dir=/etcd-data",
                        "--listen-client-urls=http://0.0.0.0:2379",
                        "--advertise-client-urls=http://etcd.platform-data.svc.cluster.local:2379",
                        "--listen-peer-urls=http://0.0.0.0:2380",
                        "--initial-advertise-peer-urls=http://etcd.platform-data.svc.cluster.local:2380",
                        "--initial-cluster=etcd0=http://etcd.platform-data.svc.cluster.local:2380",
                        "--initial-cluster-state=new",
                    ],
                    ports=[("client", 2379), ("peer", 2380)],
                    volume_mounts=[{"name": "data", "mountPath": "/etcd-data"}],
                    readiness={"exec": {"command": ["etcdctl", "--endpoints=http://localhost:2379", "endpoint", "health"]}, "initialDelaySeconds": 15, "periodSeconds": 15},
                    res=resources("50m", "128Mi", "250m", "256Mi")),
        service("etcd", "etcd", [("client", 2379, "client"), ("peer", 2380, "peer")], headless=True),
    ])

    write_multidoc(ns_dir / "clickhouse.yaml", [
        statefulset("clickhouse", "clickhouse/clickhouse-server:24.3",
                    ports=[("http", 8123), ("native", 9000)],
                    volume_mounts=[{"name": "data", "mountPath": "/var/lib/clickhouse"}],
                    readiness={"httpGet": {"path": "/ping", "port": "http"}, "initialDelaySeconds": 20, "periodSeconds": 15},
                    res=resources("100m", "384Mi", "500m", "768Mi"),
                    pvc_size="2Gi"),
        service("clickhouse", "clickhouse", [("http", 8123, "http"), ("native", 9000, "native")], headless=True),
    ])


# ---------------------------------------------------------------------------
# platform-ops
# ---------------------------------------------------------------------------

def build_platform_ops(ns_dir: Path) -> None:
    write_multidoc(ns_dir / "prometheus.yaml", [
        deployment("prometheus", "prom/prometheus:v2.53.0",
                   args=["--config.file=/etc/prometheus/prometheus.yml", "--storage.tsdb.path=/prometheus"],
                   ports=[("http", 9090)],
                   volume_mounts=[{"name": "config", "mountPath": "/etc/prometheus"}],
                   volumes=[{"name": "config", "configMap": {"name": "prometheus-config"}}],
                   readiness={"httpGet": {"path": "/-/ready", "port": "http"}, "initialDelaySeconds": 10, "periodSeconds": 10},
                   res=resources("100m", "256Mi", "500m", "512Mi")),
        configmap("prometheus-config", {"prometheus.yml": (
            "global:\n  scrape_interval: 30s\n"
            "scrape_configs:\n"
            "  - job_name: prometheus\n"
            "    static_configs:\n"
            "      - targets: [\"localhost:9090\"]\n"
        )}),
        service_account("prometheus"),
        service("prometheus", "prometheus", [("http", 9090, "http")]),
    ])

    write_multidoc(ns_dir / "grafana.yaml", [
        deployment("grafana", "grafana/grafana:11.1.0",
                   env=[{"name": "GF_SECURITY_ADMIN_PASSWORD", "valueFrom": {"secretKeyRef": {"name": "grafana-credentials", "key": "admin-password"}}}],
                   ports=[("http", 3000)],
                   readiness={"httpGet": {"path": "/api/health", "port": "http"}, "initialDelaySeconds": 15, "periodSeconds": 15},
                   res=resources("50m", "192Mi", "250m", "384Mi")),
        secret("grafana-credentials", {"admin-password": "showcase-grafana-change-me"}),
        service_account("grafana"),
        service("grafana", "grafana", [("http", 3000, "http")]),
    ])

    write_multidoc(ns_dir / "alertmanager.yaml", [
        deployment("alertmanager", "prom/alertmanager:v0.27.0",
                   args=["--config.file=/etc/alertmanager/alertmanager.yml"],
                   ports=[("http", 9093)],
                   volume_mounts=[{"name": "config", "mountPath": "/etc/alertmanager"}],
                   volumes=[{"name": "config", "configMap": {"name": "alertmanager-config"}}],
                   readiness={"httpGet": {"path": "/-/ready", "port": "http"}, "initialDelaySeconds": 10, "periodSeconds": 10},
                   res=resources("30m", "64Mi", "150m", "128Mi")),
        configmap("alertmanager-config", {"alertmanager.yml": (
            "route:\n  receiver: default\n"
            "receivers:\n  - name: default\n"
        )}),
        service_account("alertmanager"),
        service("alertmanager", "alertmanager", [("http", 9093, "http")]),
    ])

    write_multidoc(ns_dir / "jaeger.yaml", [
        deployment("jaeger", "jaegertracing/all-in-one:1.58",
                   ports=[("ui", 16686), ("collector", 14268)],
                   readiness={"httpGet": {"path": "/", "port": "ui"}, "initialDelaySeconds": 10, "periodSeconds": 10},
                   res=resources("50m", "192Mi", "250m", "384Mi")),
        service_account("jaeger"),
        service("jaeger", "jaeger", [("ui", 16686, "ui"), ("collector", 14268, "collector")]),
    ])

    write_multidoc(ns_dir / "loki.yaml", [
        deployment("loki", "grafana/loki:3.1.0",
                   args=["-config.file=/etc/loki/loki.yaml"],
                   ports=[("http", 3100)],
                   volume_mounts=[{"name": "config", "mountPath": "/etc/loki"}],
                   volumes=[{"name": "config", "configMap": {"name": "loki-config"}}],
                   readiness={"httpGet": {"path": "/ready", "port": "http"}, "initialDelaySeconds": 15, "periodSeconds": 15},
                   res=resources("100m", "256Mi", "500m", "512Mi")),
        configmap("loki-config", {"loki.yaml": (
            "auth_enabled: false\n"
            "server:\n  http_listen_port: 3100\n"
            "common:\n"
            "  path_prefix: /tmp/loki\n"
            "  storage:\n    filesystem:\n      chunks_directory: /tmp/loki/chunks\n"
            "      rules_directory: /tmp/loki/rules\n"
            "  replication_factor: 1\n"
            "  ring:\n    kvstore:\n      store: inmemory\n"
            "schema_config:\n"
            "  configs:\n"
            "    - from: 2024-01-01\n"
            "      store: tsdb\n"
            "      object_store: filesystem\n"
            "      schema: v13\n"
            "      index:\n        prefix: index_\n        period: 24h\n"
        )}),
        service_account("loki"),
        service("loki", "loki", [("http", 3100, "http")]),
    ])

    write_multidoc(ns_dir / "promtail.yaml", [
        daemonset("promtail", "grafana/promtail:3.1.0",
                  args=["-config.file=/etc/promtail/promtail.yaml"],
                  volume_mounts=[{"name": "config", "mountPath": "/etc/promtail"},
                                 {"name": "varlog", "mountPath": "/var/log", "readOnly": True}],
                  volumes=[{"name": "config", "configMap": {"name": "promtail-config"}},
                           {"name": "varlog", "hostPath": {"path": "/var/log"}}],
                  res=resources("30m", "64Mi", "150m", "128Mi")),
        configmap("promtail-config", {"promtail.yaml": (
            "server:\n  http_listen_port: 9080\n"
            "positions:\n  filename: /tmp/positions.yaml\n"
            "clients:\n  - url: http://loki.platform-ops.svc.cluster.local:3100/loki/api/v1/push\n"
            "scrape_configs:\n"
            "  - job_name: containers\n"
            "    static_configs:\n"
            "      - targets: [localhost]\n"
            "        labels:\n          job: containers\n          __path__: /var/log/containers/*.log\n"
        )}),
        service_account("promtail"),
    ])

    write_multidoc(ns_dir / "cadvisor.yaml", [
        # No hostPath mount at /var/run: it shadows the path the kubelet
        # itself needs to inject the projected ServiceAccount token into
        # (/var/run/secrets/kubernetes.io/serviceaccount), and a read-only
        # mount there makes that injection fail outright — confirmed on the
        # real cluster ("read-only file system" mounting the token).
        # cadvisor doesn't talk to the k8s API, so the token isn't needed
        # anyway; automountServiceAccountToken: false avoids it being
        # injected at all, on top of not shadowing /var/run.
        daemonset("cadvisor", "gcr.io/cadvisor/cadvisor:v0.49.1",
                  ports=[("http", 8080)],
                  volume_mounts=[{"name": "rootfs", "mountPath": "/rootfs", "readOnly": True},
                                 {"name": "sys", "mountPath": "/sys", "readOnly": True},
                                 {"name": "containerd", "mountPath": "/var/lib/containerd", "readOnly": True}],
                  volumes=[{"name": "rootfs", "hostPath": {"path": "/"}},
                           {"name": "sys", "hostPath": {"path": "/sys"}},
                           {"name": "containerd", "hostPath": {"path": "/var/lib/containerd"}}],
                  res=resources("50m", "128Mi", "250m", "256Mi"),
                  automount_service_account_token=False),
        service_account("cadvisor"),
        service("cadvisor", "cadvisor", [("http", 8080, "http")]),
    ])

    write_multidoc(ns_dir / "kube-state-metrics.yaml", [
        deployment("kube-state-metrics", "registry.k8s.io/kube-state-metrics/kube-state-metrics:v2.13.0",
                   ports=[("http", 8080), ("telemetry", 8081)],
                   readiness={"httpGet": {"path": "/healthz", "port": "http"}, "initialDelaySeconds": 10, "periodSeconds": 10},
                   res=resources("50m", "128Mi", "250m", "256Mi")),
        service_account("kube-state-metrics"),
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "ClusterRole",
            "metadata": {"name": "kube-state-metrics-reader"},
            "rules": [{
                "apiGroups": ["*"],
                "resources": ["*"],
                "verbs": ["list", "watch"],
            }],
        },
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "ClusterRoleBinding",
            "metadata": {"name": "kube-state-metrics-reader-binding"},
            "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": "kube-state-metrics-reader"},
            "subjects": [{"kind": "ServiceAccount", "name": "kube-state-metrics", "namespace": "platform-ops"}],
        },
        service("kube-state-metrics", "kube-state-metrics", [("http", 8080, "http"), ("telemetry", 8081, "telemetry")]),
    ])

    write_multidoc(ns_dir / "blackbox-exporter.yaml", [
        deployment("blackbox-exporter", "prom/blackbox-exporter:v0.25.0",
                   ports=[("http", 9115)],
                   readiness={"httpGet": {"path": "/", "port": "http"}, "initialDelaySeconds": 5, "periodSeconds": 10},
                   res=resources("30m", "64Mi", "150m", "128Mi")),
        service_account("blackbox-exporter"),
        service("blackbox-exporter", "blackbox-exporter", [("http", 9115, "http")]),
    ])

    write_multidoc(ns_dir / "otel-collector.yaml", [
        deployment("otel-collector", "otel/opentelemetry-collector-contrib:0.104.0",
                   args=["--config=/etc/otel/config.yaml"],
                   ports=[("otlp-grpc", 4317), ("otlp-http", 4318)],
                   volume_mounts=[{"name": "config", "mountPath": "/etc/otel"}],
                   volumes=[{"name": "config", "configMap": {"name": "otel-collector-config"}}],
                   readiness={"tcpSocket": {"port": "otlp-grpc"}, "initialDelaySeconds": 10, "periodSeconds": 10},
                   res=resources("50m", "128Mi", "250m", "256Mi")),
        configmap("otel-collector-config", {"config.yaml": (
            "receivers:\n"
            # Default OTLP receiver binds to localhost only — unreachable
            # for the kubelet's readiness probe, which connects via the pod
            # IP. Confirmed on the real cluster: logs said "ready", pod
            # stayed 0/1.
            "  otlp:\n    protocols:\n      grpc:\n        endpoint: 0.0.0.0:4317\n"
            "      http:\n        endpoint: 0.0.0.0:4318\n"
            "exporters:\n  debug: {}\n"
            "service:\n"
            "  pipelines:\n"
            "    traces:\n      receivers: [otlp]\n      exporters: [debug]\n"
            "    metrics:\n      receivers: [otlp]\n      exporters: [debug]\n"
        )}),
        service_account("otel-collector"),
        service("otel-collector", "otel-collector", [("otlp-grpc", 4317, "otlp-grpc"), ("otlp-http", 4318, "otlp-http")]),
    ])


# ---------------------------------------------------------------------------
# background-jobs
# ---------------------------------------------------------------------------

def build_background_jobs(ns_dir: Path) -> None:
    write_multidoc(ns_dir / "backup-vault.yaml", [
        cronjob("backup-vault", "restic/restic:0.16.4", "0 3 * * *",
                ["/bin/sh", "-c"], echo_args("running backup snapshot")),
    ])
    write_multidoc(ns_dir / "cert-renewer.yaml", [
        cronjob("cert-renewer", "smallstep/step-cli:latest", "0 4 * * 0",
                ["/bin/sh", "-c"], echo_args("renewing internal certs")),
    ])
    write_multidoc(ns_dir / "cache-warmer.yaml", [
        cronjob("cache-warmer", "curlimages/curl:8.9.1", "*/30 * * * *",
                ["/bin/sh", "-c"], ["curl -s -o /dev/null http://storefront.shop-web.svc.cluster.local || true; echo warmed cache; sleep 1; echo done"]),
    ])
    write_multidoc(ns_dir / "log-archiver.yaml", [
        cronjob("log-archiver", "alpine:3.20", "0 1 * * *",
                ["/bin/sh", "-c"], echo_args("archiving old logs")),
    ])
    write_multidoc(ns_dir / "db-vacuum.yaml", [
        cronjob("db-vacuum", "postgres:16-alpine", "0 5 * * *",
                ["/bin/sh", "-c"], echo_args("vacuuming database tables")),
    ])
    write_multidoc(ns_dir / "queue-purge.yaml", [
        job("queue-purge", "redis:7-alpine", ["/bin/sh", "-c"], echo_args("purging stale queue entries")),
    ])
    write_multidoc(ns_dir / "report-exporter.yaml", [
        job("report-exporter", "python:3.12-alpine", ["/bin/sh", "-c"], echo_args("exporting usage report")),
    ])
    write_multidoc(ns_dir / "image-cleanup.yaml", [
        cronjob("image-cleanup", "busybox:1.36", "30 2 * * *",
                ["/bin/sh", "-c"], echo_args("cleaning unused image cache")),
    ])
    write_multidoc(ns_dir / "metrics-rollup.yaml", [
        job("metrics-rollup", "busybox:1.36", ["/bin/sh", "-c"], echo_args("rolling up hourly metrics")),
    ])
    write_multidoc(ns_dir / "healthcheck-sweep.yaml", [
        cronjob("healthcheck-sweep", "curlimages/curl:8.9.1", "*/10 * * * *",
                ["/bin/sh", "-c"], ["curl -s -o /dev/null http://api-gateway.shop-web.svc.cluster.local || true; echo swept; sleep 1; echo done"]),
    ])


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", default="namespaces-showcase")
    args = ap.parse_args()
    root = Path(args.output)

    build_identity_auth(root / "identity-auth")
    build_shop_web(root / "shop-web")
    build_platform_data(root / "platform-data")
    build_platform_ops(root / "platform-ops")
    build_background_jobs(root / "background-jobs")

    print("Added 10 microservices to each of 5 namespaces (50 new files) under", root)


if __name__ == "__main__":
    main()
