#!/usr/bin/env python3
"""
duplicate_showcase_envs.py
============================
Adds environment-suffixed sibling namespaces to namespaces-showcase/ — e.g.
shop-web-tst, shop-web-stg, shop-web-prd alongside the existing shop-web —
each running the SAME microservice names as their base namespace, so
convert_to_universal_chart.py's base/ sharing (same microservice name ->
one shared base/<name>.yaml, reused across every namespace running it) has
something realistic to actually exercise: real orgs run the same app's
microservices under a -tst/-stg/-prd-suffixed namespace per environment far
more often than they run genuinely different apps that happen to share a
name.

This is a TRANSFORM, not a raw copy — several existing files hardcode their
OWN namespace, which a blind copy would leave silently pointing at the
ORIGINAL namespace instead of the new sibling's:
  - in-cluster DNS (openldap.identity-auth.svc.cluster.local, etc.)
  - background-jobs' cache-warmer/healthcheck-sweep, which reach INTO
    shop-web by name — rewired to the matching-environment shop-web sibling,
    not always the original, so a -stg copy talks to shop-web-stg
  - Ingress objects carry real public *.duckdns.org hostnames, which would
    collide across namespaces if duplicated verbatim — dropped entirely for
    the new siblings (matches the ClusterIP-only policy every other new
    showcase microservice this session already follows)
  - ClusterRoleBinding is namespace-aware (its subjects' namespace, and
    cluster-scoped names collide globally) — renamed uniquely per sibling
    and its subjects' namespace fixed; the ClusterRole it points at is left
    alone since identical rules across namespaces is exactly the case
    cluster-scoped dedup exists for (one shared ClusterRole, multiple
    per-namespace ClusterRoleBindings — the standard RBAC pattern for
    granting one role to many namespaces)

identity-auth/session-store.yaml is deliberately skipped: it's a live demo of
a per-namespace SecretStore/Vault-role pattern that still has unfinished
Vault wiring for even its ONE existing copy (see the plan file) — tripling
that unfinished state across new namespaces would just be more of the same
gap, not a new demonstration.

Usage:
    python duplicate_showcase_envs.py --root namespaces-showcase
"""
from __future__ import annotations

import argparse
import re
import shutil
from pathlib import Path

import yaml

BASE_NAMESPACES = ["identity-auth", "shop-web", "platform-data", "platform-ops", "background-jobs"]

# Suffixes vary per app, chosen so any cross-namespace reference (only
# background-jobs -> shop-web today) always has a matching sibling to
# resolve to: background-jobs' {stg,prd} subset of shop-web's {tst,stg,prd}.
SUFFIXES = {
    "identity-auth": ["tst", "prd"],
    "shop-web": ["tst", "stg", "prd"],
    "platform-data": ["dev", "prd"],
    "platform-ops": ["tst", "prd"],
    "background-jobs": ["stg", "prd"],
}

SKIP_FILES = {
    "identity-auth": {"session-store.yaml"},
}

# Matches any of the 5 base namespace names used as a Service's in-cluster
# DNS namespace segment, e.g. "shop-web.svc.cluster.local" or
# "identity-auth.svc.cluster.local" — including inside a longer FQDN like
# "openldap.identity-auth.svc.cluster.local".
NS_DNS_RE = re.compile(r"\b(" + "|".join(re.escape(n) for n in BASE_NAMESPACES) + r")\.svc\.cluster\.local\b")


def transform_doc(doc: dict, dst_ns: str, suffix: str) -> dict | None:
    if doc.get("kind") == "Ingress":
        return None  # real public hostnames would collide across namespaces
    if doc.get("kind") == "ClusterRoleBinding":
        doc["metadata"]["name"] = f'{doc["metadata"]["name"]}-{dst_ns}'
        for s in doc.get("subjects", []) or []:
            if "namespace" in s:
                s["namespace"] = dst_ns
        # roleRef intentionally untouched: the ClusterRole itself keeps its
        # original name and is shared (byte-identical rules dedup globally),
        # only the per-namespace binding needs to be unique.
    return doc


def process_file(src_path: Path, dst_dir: Path, src_ns: str, dst_ns: str, suffix: str) -> None:
    docs = [d for d in yaml.safe_load_all(src_path.read_text(encoding="utf-8")) if d]
    docs = [d for d in (transform_doc(d, dst_ns, suffix) for d in docs) if d is not None]
    if not docs:
        return
    text = yaml.safe_dump_all(docs, sort_keys=False, default_flow_style=False, width=100)
    text = NS_DNS_RE.sub(lambda m: f"{m.group(1)}-{suffix}.svc.cluster.local", text)
    # File name (== microservice identity to the converter, ms_file.stem)
    # carries the env suffix too — e.g. cache-warmer.yaml -> cache-warmer-
    # stg.yaml inside background-jobs-stg. convert_to_universal_chart.py's
    # env_normalized_ms_name() recognizes this pattern (suffix matches the
    # containing namespace's own suffix) and strips it back off internally,
    # so this still shares one base/cache-warmer.yaml with every other
    # environment's copy despite the suffixed filename on disk.
    dst_name = f"{src_path.stem}-{suffix}{src_path.suffix}"
    (dst_dir / dst_name).write_text(text, encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="namespaces-showcase")
    args = ap.parse_args()
    root = Path(args.root)

    total = 0
    for src_ns in BASE_NAMESPACES:
        src_dir = root / src_ns
        skip = SKIP_FILES.get(src_ns, set())
        for suffix in SUFFIXES[src_ns]:
            dst_ns = f"{src_ns}-{suffix}"
            dst_dir = root / dst_ns
            if dst_dir.exists():
                shutil.rmtree(dst_dir)  # clear a stale prior run's (unsuffixed) files
            dst_dir.mkdir(parents=True, exist_ok=True)
            count = 0
            for f in sorted(src_dir.glob("*.yaml")):
                if f.name in skip:
                    continue
                process_file(f, dst_dir, src_ns, dst_ns, suffix)
                count += 1
            print(f"  {dst_ns}: {count} files (from {src_ns})")
            total += count

    print(f"\nGenerated {total} files across {sum(len(v) for v in SUFFIXES.values())} new sibling namespaces.")


if __name__ == "__main__":
    main()
