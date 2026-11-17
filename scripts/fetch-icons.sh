#!/usr/bin/env bash
# Downloads QueensLink's favicons and Apple touch icon from queenslink.org
# into `.cache/queenslink-icons/`, unless they're already there,
# and copies them to `web/public/`, where the page and its embeds refer to them.
# `web/package.json`'s `icons` script runs this before `dev` and `build`.
#
# queenslink.org's `<link rel="icon">` and `apple-touch-icon` point at WordPress's own resized copies
# of its logo, so these are those files as they are, not resized here.

set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cache="$root/.cache/queenslink-icons"
public="$root/web/public"
base="https://thequeenslink.org/wp-content/uploads/2023/07/cropped-QueensLink_logo_transp"

mkdir -p "$cache"

# Our name for each icon, then the size in WordPress's URL for it.
icons=(
    queenslink-favicon-32 32x32
    queenslink-favicon-192 192x192
    queenslink-apple-touch-icon 180x180
)

for ((i = 0; i < ${#icons[@]}; i += 2)); do
    name="${icons[i]}"
    size="${icons[i + 1]}"
    if [[ ! -e "$cache/$name.png" ]]; then
        echo "downloading $base-$size.png" >&2
        curl --fail --location --silent --show-error --output "$cache/$name.png.partial" "$base-$size.png"
        mv "$cache/$name.png.partial" "$cache/$name.png"
    fi
    cp "$cache/$name.png" "$public/$name.png"
done
