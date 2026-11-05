#!/usr/bin/env bash
# sync-to-external.sh — publish a release of this repo to a repo in another
# GitHub organization (e.g. one behind Microsoft Entra ID SSO / EMU) as ONE
# snapshot commit. Internal history is never pushed — only the file tree at
# the chosen release, plus a matching tag.
#
# Run it by hand from a PC that is logged in to the target org through the
# GitHub CLI (`gh auth login --web` → Microsoft sign-in). No secrets are
# stored anywhere: the push borrows gh's token for this one command only.
#
# Usage:  scripts/sync-to-external.sh --target OWNER/REPO [options]
#         scripts/sync-to-external.sh --help
#
# Works on Linux, macOS and Windows (Git Bash).

set -euo pipefail

# ---------------------------------------------------------------- defaults --
# Anything below can also be set in scripts/sync-to-external.conf (sourced if
# present, KEY=value lines) or as environment variables.
SRC_DIR="$(git rev-parse --show-toplevel)"
CONF_FILE="${CONF_FILE:-$SRC_DIR/scripts/sync-to-external.conf}"
# shellcheck disable=SC1090
[ -f "$CONF_FILE" ] && . "$CONF_FILE"

TARGET_REPO="${TARGET_REPO:-}"          # OWNER/REPO in the other org
TARGET_BRANCH="${TARGET_BRANCH:-main}"
GH_HOST="${GH_HOST:-github.com}"
GH_USER="${GH_USER:-}"                  # gh account to push as (EMU: the *_shortcode one)
REF="${REF:-}"                          # empty = latest stable v* tag on origin/main
IGNORE_FILE="${IGNORE_FILE:-$SRC_DIR/.sync-external-ignore}"
AUTH="${AUTH:-gh}"                      # gh | git (use your own SSH key / git credentials)
TARGET_URL="${TARGET_URL:-}"            # override the push URL (AUTH=git only)
MODE="${MODE:-push}"                    # push | pr
DRY_RUN="${DRY_RUN:-0}"
ASSUME_YES="${ASSUME_YES:-0}"
FORCE="${FORCE:-0}"

usage() {
  cat <<'EOF'
Publish a release of this repo to another org's repo as a single commit.

  scripts/sync-to-external.sh --target OWNER/REPO [options]

Options
  -t, --target OWNER/REPO  Target repository (required; or TARGET_REPO=)
  -r, --ref REF            Tag/branch/commit to publish
                           (default: latest stable v* tag on origin/main)
  -b, --branch NAME        Target branch (default: main)
  -u, --user LOGIN         gh account to use, when you are logged in with more
                           than one (EMU: your managed *_shortcode account)
      --pr                 Push to a sync/<ref> branch and open a pull request
                           instead of pushing to the branch directly (use when
                           the target branch is protected)
      --ssh                Push with your own git/SSH credentials instead of gh
  -n, --dry-run            Build the commit and show it, but push nothing
  -y, --yes                Do not ask for confirmation
  -f, --force              Publish even if the target already has this tag
  -h, --help               Show this help

One-time setup on the PC
  gh auth login --hostname github.com --web    # sign in via Microsoft
  # SAML-SSO org: if access is refused, authorize "GitHub CLI" for the org at
  #   https://github.com/settings/applications  (or follow the link gh prints)
  # EMU: log in with the managed account too (gh auth login again) and pass
  #   --user <your_shortcode login>

Paths listed in .sync-external-ignore are not published.
EOF
}

die()  { printf '\n\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }
info() { printf '\033[36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33mwarning:\033[0m %s\n' "$*" >&2; }

while [ $# -gt 0 ]; do
  case "$1" in
    -t|--target)  TARGET_REPO="${2:?}"; shift 2 ;;
    -r|--ref)     REF="${2:?}"; shift 2 ;;
    -b|--branch)  TARGET_BRANCH="${2:?}"; shift 2 ;;
    -u|--user)    GH_USER="${2:?}"; shift 2 ;;
    --pr)         MODE=pr; shift ;;
    --ssh)        AUTH=git; shift ;;
    -n|--dry-run) DRY_RUN=1; shift ;;
    -y|--yes)     ASSUME_YES=1; shift ;;
    -f|--force)   FORCE=1; shift ;;
    -h|--help)    usage; exit 0 ;;
    *) usage >&2; die "unknown argument: $1" ;;
  esac
done

[ -n "$TARGET_REPO" ] || { usage >&2; die "--target OWNER/REPO is required"; }
[[ "$TARGET_REPO" == */* ]] || die "--target must look like OWNER/REPO, got '$TARGET_REPO'"
for tool in git tar; do command -v "$tool" >/dev/null || die "'$tool' is not installed"; done
if [ "$AUTH" = gh ] || [ "$MODE" = pr ]; then
  command -v gh >/dev/null || die "the GitHub CLI 'gh' is not installed (https://cli.github.com)"
fi

# ----------------------------------------------------------------- source --
info "Fetching the latest main and tags from this repo's origin"
git -C "$SRC_DIR" fetch --quiet --tags origin main

if [ -z "$REF" ]; then
  # Stable releases only: semantic-release's prereleases are vX.Y.Z-dev.N.
  REF="$(git -C "$SRC_DIR" describe --tags --abbrev=0 --match 'v[0-9]*' --exclude '*-*' origin/main 2>/dev/null)" \
    || die "no stable v* tag found on origin/main; pass --ref"
fi
SRC_SHA="$(git -C "$SRC_DIR" rev-parse --verify --quiet "$REF^{commit}")" \
  || die "'$REF' is not a tag, branch or commit in this repo"
if ! git -C "$SRC_DIR" branch -r --contains "$SRC_SHA" | grep -q .; then
  die "'$REF' ($SRC_SHA) is not on any branch of origin — push it first, only published code may be synced"
fi
# Only a tag gets re-created on the target; a branch/commit is published untagged.
TAG=""
git -C "$SRC_DIR" show-ref --verify --quiet "refs/tags/$REF" && TAG="$REF"
info "Source: $REF (${SRC_SHA:0:12})"

# ------------------------------------------------------------------- auth --
export GIT_TERMINAL_PROMPT=0
GIT_AUTH=()
if [ "$AUTH" = gh ]; then
  gh auth status --hostname "$GH_HOST" >/dev/null 2>&1 \
    || die "gh is not logged in to $GH_HOST — run: gh auth login --hostname $GH_HOST --web"
  if [ -n "$GH_USER" ]; then
    SYNC_TOKEN="$(gh auth token --hostname "$GH_HOST" --user "$GH_USER" 2>/dev/null)" \
      || die "gh has no login for '$GH_USER' on $GH_HOST — run: gh auth login --hostname $GH_HOST --web"
  else
    SYNC_TOKEN="$(gh auth token --hostname "$GH_HOST")"
  fi
  export SYNC_TOKEN
  # The token is handed to git per command through a credential helper that
  # reads it from the environment: it is never written to disk or .git/config,
  # and the empty helper first switches off any other helper (keychain, GCM)
  # so git cannot pick a different account.
  GIT_AUTH=(-c credential.helper=
            -c 'credential.helper=!f() { test "$1" = get || exit 0; echo username=x-access-token; echo "password=$SYNC_TOKEN"; }; f')
  TARGET_URL="https://$GH_HOST/$TARGET_REPO.git"

  # Check access before doing any work, with a useful message on failure.
  if ! perm_json="$(GH_TOKEN="$SYNC_TOKEN" gh api --hostname "$GH_HOST" "repos/$TARGET_REPO" 2>&1)"; then
    case "$perm_json" in
      *SAML*|*SSO*|*"single sign-on"*)
        die "the org enforces Microsoft (SAML) single sign-on and gh's token is not authorized for it.
       Open https://$GH_HOST/settings/applications → GitHub CLI → grant access to the org
       (or run 'gh auth refresh --hostname $GH_HOST' and follow the SSO link), then retry.
       gh said: $perm_json" ;;
      *"Not Found"*)
        die "$TARGET_REPO not found with this login. Does it exist, and are you logged in with the
       right account? (EMU: pass --user <your_shortcode login>; see 'gh auth status')" ;;
      *) die "cannot read $TARGET_REPO: $perm_json" ;;
    esac
  fi
  case "$perm_json" in
    *'"push":true'*) ;;
    *) die "your account can read $TARGET_REPO but cannot push to it — ask an org owner for write access" ;;
  esac
  LOGIN="$(GH_TOKEN="$SYNC_TOKEN" gh api --hostname "$GH_HOST" user --jq .login)"
  USER_ID="$(GH_TOKEN="$SYNC_TOKEN" gh api --hostname "$GH_HOST" user --jq .id)"
  info "Target: $TARGET_REPO ($TARGET_BRANCH) as $LOGIN"
else
  TARGET_URL="${TARGET_URL:-git@$GH_HOST:$TARGET_REPO.git}"
  LOGIN=""
  info "Target: $TARGET_URL ($TARGET_BRANCH) with your own git credentials"
fi

tgit() { git "${GIT_AUTH[@]}" "$@"; }

if [ -n "$TAG" ] && [ "$FORCE" != 1 ] && \
   tgit ls-remote --exit-code --tags "$TARGET_URL" "refs/tags/$TAG" >/dev/null 2>&1; then
  info "$TARGET_REPO already has $TAG — nothing to do (use --force to publish again)"
  exit 0
fi

# ----------------------------------------------------------------- build --
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
DEST="$WORK/target"

if tgit ls-remote --exit-code --heads "$TARGET_URL" "$TARGET_BRANCH" >/dev/null 2>&1; then
  info "Cloning $TARGET_BRANCH of the target (shallow)"
  tgit clone --quiet --depth 1 --branch "$TARGET_BRANCH" "$TARGET_URL" "$DEST"
else
  warn "$TARGET_BRANCH does not exist on the target yet — it will be created"
  git init --quiet "$DEST"
  git -C "$DEST" checkout --quiet -b "$TARGET_BRANCH"
  git -C "$DEST" remote add origin "$TARGET_URL"
fi

# Replace the whole tree with the source at REF, so deletions sync too.
find "$DEST" -mindepth 1 -maxdepth 1 ! -name .git -exec rm -rf {} +
git -C "$SRC_DIR" archive --format=tar "$SRC_SHA" | tar -x -C "$DEST"

# Drop internal-only paths. Read from the *published* ref, not the working
# tree, so the result does not depend on local uncommitted edits.
ignore_list="$(git -C "$SRC_DIR" show "$SRC_SHA:${IGNORE_FILE#"$SRC_DIR"/}" 2>/dev/null || true)"
[ -n "$ignore_list" ] || { [ -f "$IGNORE_FILE" ] && ignore_list="$(cat "$IGNORE_FILE")"; } || true
while IFS= read -r pattern || [ -n "$pattern" ]; do
  pattern="${pattern%%#*}"; pattern="$(printf '%s' "$pattern" | sed 's/[[:space:]]*$//')"
  [ -n "$pattern" ] || continue
  case "$pattern" in /*|*..*) die "bad pattern in ignore file: '$pattern'" ;; esac
  # shellcheck disable=SC2086 # intentional glob expansion inside DEST
  (cd "$DEST" && rm -rf -- $pattern)
done <<< "$ignore_list"

cd "$DEST"
if [ -n "$LOGIN" ]; then
  # Attribute the commit to the account that pushes it, with its private
  # noreply address — no internal e-mail leaks into the other org.
  git config user.name "$LOGIN"
  git config user.email "$USER_ID+$LOGIN@users.noreply.github.com"
fi
git add -A
if git rev-parse --verify --quiet HEAD >/dev/null && git diff --cached --quiet; then
  info "Target $TARGET_BRANCH already matches $REF — nothing to commit"
  exit 0
fi

git commit --quiet -m "Sync ${REF} from upstream" \
  -m "Snapshot of the upstream repository at ${REF} (${SRC_SHA})."
[ -n "$TAG" ] && [ "$MODE" = push ] && git tag -f "$TAG" >/dev/null

echo
info "Commit to publish:"
git --no-pager show --stat --format='  %s%n  %b' HEAD | sed -n '1,60p'
echo

if [ "$DRY_RUN" = 1 ]; then
  info "Dry run — nothing pushed. Working copy kept at: $DEST"
  trap - EXIT
  exit 0
fi

if [ "$ASSUME_YES" != 1 ]; then
  if [ "$MODE" = pr ]; then what="open a pull request into $TARGET_REPO:$TARGET_BRANCH"
  else what="push to $TARGET_REPO:$TARGET_BRANCH${TAG:+ and tag $TAG}"; fi
  read -r -p "Proceed to $what? [y/N] " answer
  [[ "$answer" =~ ^[Yy]$ ]] || die "aborted, nothing pushed"
fi

if [ "$MODE" = pr ]; then
  PR_BRANCH="sync/${REF//\//-}"
  tgit push --quiet --force origin "HEAD:refs/heads/$PR_BRANCH"
  pr_url="$(GH_TOKEN="${SYNC_TOKEN:-$(gh auth token --hostname "$GH_HOST")}" \
    gh pr create --repo "$TARGET_REPO" --base "$TARGET_BRANCH" --head "$PR_BRANCH" \
      --title "Sync ${REF} from upstream" \
      --body "Snapshot of the upstream repository at \`${REF}\` (\`${SRC_SHA}\`).${TAG:+

After merging, tag the merge commit \`${TAG}\`.}")"
  info "Pull request opened: $pr_url"
else
  # Not --force: if someone committed to the target meanwhile, git refuses
  # and nothing is overwritten.
  tgit push --quiet origin "HEAD:refs/heads/$TARGET_BRANCH"
  [ -n "$TAG" ] && tgit push --quiet --force origin "refs/tags/$TAG"
  info "Published $REF to https://$GH_HOST/$TARGET_REPO/tree/$TARGET_BRANCH"
fi
