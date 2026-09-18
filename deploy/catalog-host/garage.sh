# How to invoke Garage, worked out rather than assumed. Sourced by the
# scripts beside it; not run on its own.
#
# A shell alias is the usual way people reach a containerised Garage, and an
# alias is exactly what a script cannot see: non-interactive shells do not
# read them. So this looks for the binary, then for a running container,
# then for the alias definition in the usual rc files — and a caller reports
# which one it used, because "not reachable" when the thing is plainly
# running is a confusing way to end up with placeholders.
#
#   GARAGE="docker exec -i garage /garage" ./deploy/catalog-host/<script>.sh

# Garage 2 logs every CLI connection at INFO on stderr, and a script's own
# lines drown in it. Warnings and errors still show.
export RUST_LOG=${RUST_LOG:-warn}

# The scripts beside this one run docker without sudo: its group, not root.
# Checked first, so the failure says so rather than surfacing as a permission
# error from the middle of a compose command.
require_docker() {
    if ! command -v docker >/dev/null 2>&1; then
        echo "Docker is not installed. The catalog host runs Postgres and the blob" >&2
        echo "server under Docker Engine with its compose plugin." >&2
        exit 1
    fi
    if ! docker info >/dev/null 2>&1; then
        echo "Docker does not answer for $(id -un) without sudo. These scripts run it" >&2
        echo "as you, so add yourself to its group and log in again:" >&2
        echo "  sudo usermod -aG docker $(id -un)" >&2
        exit 1
    fi
}

# A docker exec form of the CLI gets the log level passed in, since the
# container does not see this shell's environment.
_quiet() {
    case "$1" in
        "docker exec "*) printf 'docker exec -e RUST_LOG=%s %s' "$RUST_LOG" "${1#docker exec }" ;;
        *) printf '%s' "$1" ;;
    esac
}

resolve_garage() {
    if [ -n "${GARAGE:-}" ]; then
        _quiet "$GARAGE"
        return
    fi
    if command -v garage >/dev/null 2>&1; then
        printf 'garage'
        return
    fi
    if command -v docker >/dev/null 2>&1; then
        local container
        container=$(docker ps --format '{{.Names}} {{.Image}}' 2>/dev/null |
            grep -i garage | head -1 | cut -d' ' -f1 || true)
        if [ -n "$container" ]; then
            _quiet "docker exec -i $container /garage"
            return
        fi
    fi
    local defined
    for rc in "$HOME/.bashrc" "$HOME/.bash_aliases" "$HOME/.zshrc" "$HOME/.profile"; do
        [ -r "$rc" ] || continue
        defined=$(sed -nE "s/^[[:space:]]*alias[[:space:]]+garage=['\"]?(.*[^'\"])['\"]?[[:space:]]*$/\1/p" \
            "$rc" | tail -1)
        if [ -n "$defined" ]; then
            # -t allocates a pseudo-TTY, which injects carriage returns into
            # output these scripts match hex against. Harmless interactively,
            # fatal here, and the failure would look like a parse problem.
            _quiet "${defined//-it/-i}"
            return
        fi
    done
    printf 'garage'
}
