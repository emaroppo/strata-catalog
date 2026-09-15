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

resolve_garage() {
    if [ -n "${GARAGE:-}" ]; then
        printf '%s' "$GARAGE"
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
            printf 'docker exec -i %s /garage' "$container"
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
            printf '%s' "${defined//-it/-i}"
            return
        fi
    done
    printf 'garage'
}
