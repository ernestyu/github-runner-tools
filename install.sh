#!/usr/bin/env bash
set -Eeuo pipefail

# Release bootstrap template. scripts/build-release.sh replaces these exact
# placeholders with immutable release metadata before publishing this file.
VERSION="@GRT_VERSION@"
PAYLOAD_SHA256="@GRT_SHA256@"
PAYLOAD_URL="@GRT_ARCHIVE_URL@"

die() { echo "INSTALL_BOOTSTRAP_FAILED: $*" >&2; exit 1; }

[[ "${EUID}" -ne 0 ]] || die "run as the intended non-root Runner owner; do not use sudo bash"
[[ "$VERSION" != @GRT_VERSION@ && "$PAYLOAD_SHA256" != @GRT_SHA256@ && "$PAYLOAD_URL" != @GRT_ARCHIVE_URL@ ]] ||
  die "this bootstrap template is not a published release asset"
[[ "$PAYLOAD_SHA256" =~ ^[0-9a-f]{64}$ ]] || die "invalid embedded payload digest"
command -v curl >/dev/null 2>&1 || die "curl is required"
command -v sha256sum >/dev/null 2>&1 || die "sha256sum is required"
command -v tar >/dev/null 2>&1 || die "tar is required"

tmp="$(mktemp -d)"
cleanup() { rm -rf -- "$tmp"; }
trap cleanup EXIT HUP INT TERM

archive="$tmp/payload.tar.gz"
curl --fail --silent --show-error --location --proto '=https' --tlsv1.2   "$PAYLOAD_URL" -o "$archive"

actual="$(sha256sum "$archive" | awk '{print $1}')"
[[ "$actual" == "$PAYLOAD_SHA256" ]] || die "release payload SHA-256 mismatch"

mkdir "$tmp/payload"
tar -xzf "$archive" -C "$tmp/payload"
entry="$tmp/payload/scripts/fresh-install.sh"
[[ -f "$entry" && ! -L "$entry" ]] || die "verified payload missing fresh installer"

GRT_RELEASE_VERSION="$VERSION" GRT_PAYLOAD_SHA256="$PAYLOAD_SHA256"   bash "$entry"
