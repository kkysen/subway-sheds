#!/usr/bin/env bash
# Runs the whole pipeline end to end, on a few stations, for each commit given (`HEAD` if none),
# as CI's `data` and `pages` jobs run it on every station,
# then checks what comes out of it (see `scripts/e2e_check.py`),
# and, with `--browser`, the page the last commit builds in a browser (see `scripts/e2e_browser.py`),
# with the last station selected, once, as it takes several seconds.
#
#     scripts/e2e.sh                          # `HEAD`
#     scripts/e2e.sh main~3 main              # each of them
#     scripts/e2e.sh main~3..main             # each commit after `main~3`, through `main`
#     scripts/e2e.sh --browser main~3..main   # and the page `main` builds
#
# Each commit is checked out in one worktree kept in `.cache/e2e/worktree`,
# so that each step's own cache (`scenarios`', `walks`', `.venv/`, `node_modules/`)
# carries over from one commit to the next, and only what a commit changes runs again.
# It shares this checkout's downloads in `.cache/`, so it downloads nothing this checkout has,
# and builds the `route` crate in the worktree, so apart from this checkout's own build.
# The checks are this checkout's, not the commit's, so that older commits are checked the same.
#
# `$E2E_STATIONS` are the stations routed, space-separated:
# by default Rockaway Park-Beach 116 St, QueensLink's Metropolitan Av-Parkside, and Times Sq.

set -euo pipefail

root="$(git rev-parse --show-toplevel)"
scripts="$root/scripts"
dir="$root/.cache/e2e"
worktree="$dir/worktree"
stations="${E2E_STATIONS:-H15 V01 127}"

# Each step, timed.
step() {
    local name="$1"
    shift
    local start="$EPOCHREALTIME"
    "$@"
    awk -v name="$name" -v start="$start" -v end="$EPOCHREALTIME" \
        'BEGIN { printf "%-10s %6.1fs\n", name, end - start }' >&2
}

# Whether the commit's CLI has the command `$1`, from its source,
# as running it for `--help` takes Python's startup each time.
has_command() {
    rg -q -e "def $1\(" -e "name=\"$1\"" src/subway_sheds/cli.py
}

# One commit, already checked out in the worktree, in a process of its own,
# so that `set -e` stops it at the first step that fails,
# which it doesn't within a condition, like the `if` that runs this.
if [[ "${1-}" == --checked-out ]]; then
    cd "$worktree"
    step "uv sync" mise exec -- uv sync -q --locked
    step "fetch" mise exec -- uv run -q subway-sheds fetch
    # Older commits made the scenarios' feeds in `fetch`, and walked the streets in `route`.
    if has_command scenarios; then
        step "scenarios" mise exec -- uv run -q subway-sheds scenarios >/dev/null
    fi
    if has_command walks; then
        step "walks" mise exec -- uv run -q subway-sheds walks
    fi
    # Only the stations routed, so that none routed before by another commit are checked.
    rm -rf out/travel_times
    # shellcheck disable=SC2086
    step "route" mise exec -- uv run -q subway-sheds route $stations >/dev/null
    step "export" mise exec -- uv run -q subway-sheds export >/dev/null
    step "bun install" mise exec -- bun install --silent --frozen-lockfile --cwd web
    step "build" mise exec -- bun run --silent --cwd web build >/dev/null
    # shellcheck disable=SC2086
    # In this checkout's environment, which has Playwright, as older commits' may not,
    # with its `mise` variables, not the worktree's: `$ORTOOLS_PREFIX` is a path in each,
    # which `uv` rebuilds the `route` crate whenever it differs, on and off.
    step "check" mise -C "$root" exec -- env -C "$PWD" uv run -q --project "$root" python "$scripts/e2e_check.py" $stations
    exit
fi

browser=false
if [[ "${1-}" == --browser ]]; then
    browser=true
    shift
fi

commits=()
for arg in "${@:-HEAD}"; do
    if [[ "$arg" == *..* ]]; then
        mapfile -t -O "${#commits[@]}" commits < <(git rev-list --reverse "$arg")
    else
        commits+=("$(git rev-parse --verify "$arg^{commit}")")
    fi
done

if [[ ! -e "$worktree/.git" ]]; then
    mkdir -p "$dir"
    git worktree add -q --detach "$worktree" "${commits[0]}"
fi

# This checkout's downloads, linked anew each run, so that ones downloaded since are too,
# but not other worktrees kept in `.cache/`, like this one.
# When Geofabrik was last asked is linked even before it's been asked, so `fetch` writes it here.
link_downloads() {
    mkdir -p "$worktree/.cache"
    local path
    local name
    for path in "$root/.cache"/* "$root/.cache/nyc-streets.checked.txt"; do
        name="$(basename "$path")"
        case "$name" in
            e2e | e2e-* | wt-*) continue ;;
        esac
        ln -sfn "$path" "$worktree/.cache/$name"
    done
}

failed=()
for commit in "${commits[@]}"; do
    subject="$(git log -1 --format='%h %s' "$commit")"
    echo "== $subject" >&2
    git -C "$worktree" checkout -q --detach "$commit"
    link_downloads
    if ! "$BASH" "${BASH_SOURCE[0]}" --checked-out; then
        failed+=("$subject")
    fi
done

if ((${#failed[@]})); then
    printf 'failed: %s\n' "${failed[@]}" >&2
    exit 1
fi

# The last commit's page, which the worktree still has built,
# in this checkout's environment, as `check` runs.
if "$browser"; then
    cd "$worktree"
    step "browser" mise -C "$root" exec -- env -C "$PWD" uv run -q --project "$root" python "$scripts/e2e_browser.py" "${stations##* }"
fi
