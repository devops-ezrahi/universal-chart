#!/usr/bin/env bash
#
# setup-showcase-vault.sh — Vault kubernetes-auth wiring for the showcase's
# identity-auth/session-store release.
#
# session-store deliberately declares its OWN per-namespace SecretStore
# (namespaces-showcase/identity-auth/session-store.yaml) rather than going
# through the real cluster's shared ClusterSecretStore + the single
# 'external-secrets' Vault role every one of the six real services uses
# (manifests/external-secrets.yaml, scripts/02-deploy-all.sh) — it's
# demonstrating that ESO pattern instead. That means it needs its own Vault
# policy, its own Kubernetes-auth role, and its own KV secret, none of which
# scripts/02-deploy-all.sh creates (and this script deliberately does not
# touch that script or the 'external-secrets' role/policy it owns).
#
# Idempotent, same guard style as 02-deploy-all.sh: "enable/write something
# already there" is harmless to re-run.
#
set -euo pipefail

if [ -z "${KUBECONFIG:-}" ]; then
  echo "ERROR: set KUBECONFIG to the real cluster kubeconfig first" >&2
  exit 1
fi

if ! kubectl -n vault get secret vault-init >/dev/null 2>&1; then
  echo "ERROR: Secret vault/vault-init not found — Vault must already be initialised" >&2
  exit 1
fi

VAULT_ROOT_TOKEN="$(kubectl -n vault get secret vault-init \
  -o jsonpath='{.data.root-token}' | base64 -d)"

vault_exec() {
  kubectl -n vault exec -i vault-0 -- env \
    VAULT_ADDR=http://127.0.0.1:8200 \
    VAULT_TOKEN="${VAULT_ROOT_TOKEN}" "$@"
}

echo "Writing Vault policy 'session-store' ..."
cat <<'SESSION_STORE_POLICY' | vault_exec vault policy write session-store - >/dev/null
path "secret/data/identity-auth/session-store" {
  capabilities = ["read"]
}
SESSION_STORE_POLICY

echo "Writing Kubernetes-auth role 'session-store' ..."
vault_exec vault write auth/kubernetes/role/session-store \
  bound_service_account_names=session-store \
  bound_service_account_namespaces=identity-auth \
  token_policies=session-store \
  ttl=1h >/dev/null

if vault_exec vault kv get -format=json secret/identity-auth/session-store >/dev/null 2>&1; then
  echo "secret/identity-auth/session-store already exists — leaving it as-is (adopt, don't regenerate)"
else
  echo "Seeding secret/identity-auth/session-store ..."
  SIGNING_KEY="$(openssl rand -hex 24)"
  vault_exec vault kv put secret/identity-auth/session-store \
    signingKey="${SIGNING_KEY}" >/dev/null
fi

echo "Done. identity-auth-session-store should go Synced/Healthy on its next ArgoCD reconcile."
