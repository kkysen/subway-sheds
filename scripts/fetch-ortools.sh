#!/usr/bin/env bash
# Downloads OR-Tools' prebuilt C++ library, with CP-SAT, into `$ORTOOLS_PREFIX`,
# which the `route` crate links against through the `cp_sat` crate.
# Before `uv sync`, since that builds the `route` crate.
# `mise.toml` sets `$ORTOOLS_VERSION` and `$ORTOOLS_PREFIX`, and runs this after `mise install`.
#
# AlmaLinux 8's build, as its old glibc runs on any newer Linux,
# and it's the one built for both aarch64 and x86-64.

set -euo pipefail

: "${ORTOOLS_VERSION:?set by mise.toml}"
: "${ORTOOLS_PREFIX:?set by mise.toml}"

if [[ -e "$ORTOOLS_PREFIX/include/ortools/sat/cp_model.h" ]]; then
    exit 0
fi

arch="$(uname -m)"
case "$arch" in
    aarch64 | x86_64) ;;
    *)
        echo "no OR-Tools build for $arch" >&2
        exit 1
        ;;
esac

minor="${ORTOOLS_VERSION%.*}"
name="or-tools_${arch}_AlmaLinux-8.10_cpp_v$ORTOOLS_VERSION"
url="https://github.com/google/or-tools/releases/download/v$minor/$name.tar.gz"

parent="$(dirname "$ORTOOLS_PREFIX")"
mkdir -p "$parent"
partial="$(mktemp -d "$parent/.partial.XXXXXX")"
trap 'rm -rf "$partial"' EXIT

echo "downloading $url" >&2
curl --fail --location --silent --show-error "$url" | tar -xz -C "$partial"
mv "$partial/$name" "$ORTOOLS_PREFIX"
