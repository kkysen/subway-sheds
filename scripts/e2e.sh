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
# It stops at the first commit that fails, as the ones after it mostly fail with it,
# unless `--keep-going`, which checks every commit anyway.
#
# Each commit is checked out in one worktree kept in `.cache/e2e/worktree`,
# so that each step's own cache (`scenarios`', `walks`', `.venv/`, `node_modules/`)
# carries over from one commit to the next, and only what a commit changes runs again.
# It shares this checkout's downloads in `.cache/`, so it downloads nothing this checkout has,
# and builds the `route` crate in the worktree, so apart from this checkout's own build.
# The checks are this checkout's, not the commit's, so that older commits are checked the same.
# A commit that passes is recorded in `.cache/e2e/passed/`, by its tree, the stations,
# and this script and its checks, so the same tree isn't checked again, as when a push is retried
# or commits are reordered; its page in the browser is recorded the same way.
# Its tree is only what some step reads, not docs or tests, so a commit changing only those
# passes as soon as one before it with the rest the same did.
# The scenarios' feeds each commit makes are kept in `.cache/e2e/scenarios/`
# by what they're made from, for later commits made from the same.
# What it says goes into `.cache/e2e/e2e.log` too, as it says it.
#
# `$E2E_STATIONS` are the stations routed, space-separated:
# by default Rockaway Park-Beach 116 St, QueensLink's Metropolitan Av-Parkside, and Times Sq.

set -euo pipefail

root="$(git rev-parse --show-toplevel)"
scripts="$root/scripts"
dir="$root/.cache/e2e"
worktree="$dir/worktree"
stations="${E2E_STATIONS:-H15 V01 127}"
passed="$dir/passed"
scenarios="$dir/scenarios"
# What no step reads, so that a commit changing only these passes as the last one did:
# docs, the embeds pasted on other sites, the tests, CI,
# and the checks, which are this checkout's anyway.
unused=$'\t(README\\.md|LICENSE|embed/.*|tests/.*|\\.github/.*|\\.gitignore|\\.pre-commit-config\\.yaml|scripts/e2e.*)$'

# What a pass of commit `$1` is recorded by, for `$2` (`pipeline` or `browser`):
# the files in its tree the pipeline and page are made from, the stations,
# and this checkout's script and checks, as they're what's run.
pass_key() {
    {
        echo "$2"
        git ls-tree -r "$1" | rg -v "$unused"
        echo "$stations"
        cat "$scripts/e2e.sh" "$scripts/e2e_check.py" "$scripts/e2e_browser.py"
    } | sha256sum | cut -d' ' -f1
}

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

# The scenarios' feeds, given back from `$scenarios` when an earlier commit made them
# from the same code and data, as making them takes minutes,
# and kept there when made anew, by the hash of what they're made from,
# from commits that name it (`scenarios_digest` in `cli.py`).
scenarios() {
    local digest=""
    if rg -q "def scenarios_digest\(" src/subway_sheds/cli.py; then
        digest="$(mise exec -- uv run -q python -c \
            'from subway_sheds.cli import scenarios_digest; print(scenarios_digest())')"
    fi
    if [[ -n "$digest" && -e "$scenarios/$digest/inputs.sha256" ]]; then
        rm -rf out/gtfs
        mkdir -p out
        cp -a "$scenarios/$digest" out/gtfs
    fi
    step "scenarios" mise exec -- uv run -q subway-sheds scenarios >/dev/null
    if [[ -n "$digest" && ! -e "$scenarios/$digest" ]]; then
        mkdir -p "$scenarios"
        cp -a out/gtfs "$scenarios/$digest.tmp"
        mv "$scenarios/$digest.tmp" "$scenarios/$digest"
    fi
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
        scenarios
    fi
    if has_command walks; then
        step "walks" mise exec -- uv run -q subway-sheds walks
    fi
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

# Everything said, also into `.cache/e2e/e2e.log`, anew each run,
# to follow with `tail -f` while pre-commit holds a hook's output until it's done.
mkdir -p "$dir"
exec > >(tee "$dir/e2e.log") 2>&1

browser=false
keep_going=false
while [[ "${1-}" == --* ]]; do
    case "$1" in
        --browser) browser=true ;;
        --keep-going) keep_going=true ;;
        *)
            echo "unknown option: $1" >&2
            exit 2
            ;;
    esac
    shift
done

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

mkdir -p "$passed"
failed=()
unchecked=()
for commit in "${commits[@]}"; do
    if ((${#failed[@]})) && ! "$keep_going"; then
        unchecked+=("$(git log -1 --format='%h %s' "$commit")")
        continue
    fi
    subject="$(git log -1 --format='%h %s' "$commit")"
    key="$(pass_key "$commit" pipeline)"
    if [[ -e "$passed/$key" ]]; then
        echo "== $subject: passed before" >&2
        continue
    fi
    echo "== $subject" >&2
    git -C "$worktree" checkout -q --detach "$commit"
    link_downloads
    if "$BASH" "${BASH_SOURCE[0]}" --checked-out; then
        touch "$passed/$key"
        echo "== $subject: passed" >&2
    else
        failed+=("$subject")
        echo "== $subject: failed" >&2
    fi
done

if ((${#failed[@]})); then
    printf 'failed: %s\n' "${failed[@]}" >&2
    if ((${#unchecked[@]})); then
        printf 'not checked: %s\n' "${unchecked[@]}" >&2
    fi
    exit 1
fi

# The last commit's page, which the worktree still has built,
# in this checkout's environment, as `check` runs.
# A last commit that passed before isn't built, so it's built for the browser now.
if "$browser"; then
    last="${commits[-1]}"
    key="$(pass_key "$last" browser)"
    if [[ -e "$passed/$key" ]]; then
        echo "browser: passed before" >&2
        exit
    fi
    if [[ "$(git -C "$worktree" rev-parse HEAD)" != "$last" ]]; then
        git -C "$worktree" checkout -q --detach "$last"
        link_downloads
        "$BASH" "${BASH_SOURCE[0]}" --checked-out
    fi
    cd "$worktree"
    step "browser" mise -C "$root" exec -- env -C "$PWD" uv run -q --project "$root" python "$scripts/e2e_browser.py" "${stations##* }"
    touch "$passed/$key"
fi
