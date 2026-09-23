#!/usr/bin/env bash
# T42 setup: a local registry serving a good and a crashing tag, plus the `canary` fixture stack.
#
# Run as casaroot inside the throwaway guest, before tests/homelab/t42-canary.py:
#
#     bash tests/homelab/t42-canary.sh setup      # registry + images + stack, service on :good
#     bash tests/homelab/t42-canary.sh point bad  # move :latest to the crashing image
#     bash tests/homelab/t42-canary.sh point good2
#     bash tests/homelab/t42-canary.sh teardown
set -euo pipefail

REGISTRY=localhost:5000
STACK_DIR="$HOME/stacks/canary"
BUILD="$(mktemp -d)"
trap 'rm -rf "$BUILD"' EXIT

start_registry() {
    docker inspect pe-registry >/dev/null 2>&1 && return
    docker run -d --name pe-registry --restart unless-stopped -p 5000:5000 registry:2 >/dev/null
    for _ in $(seq 1 30); do
        curl -sf "http://$REGISTRY/v2/" >/dev/null && return
        sleep 1
    done
    echo "registry did not come up" >&2; exit 1
}

build_images() {
    # good: stays up and answers its healthcheck. bad: exits immediately, so the canary watch fails.
    cat > "$BUILD/Dockerfile.good" <<'DOCKER'
FROM busybox:1.36
RUN echo ok > /ok
HEALTHCHECK --interval=3s --timeout=2s --retries=2 CMD test -f /ok
CMD ["sh", "-c", "while true; do sleep 5; done"]
DOCKER
    cat > "$BUILD/Dockerfile.good2" <<'DOCKER'
FROM busybox:1.36
RUN echo ok > /ok && echo second > /generation
HEALTHCHECK --interval=3s --timeout=2s --retries=2 CMD test -f /ok
CMD ["sh", "-c", "while true; do sleep 5; done"]
DOCKER
    cat > "$BUILD/Dockerfile.bad" <<'DOCKER'
FROM busybox:1.36
HEALTHCHECK --interval=3s --timeout=2s --retries=2 CMD test -f /ok
CMD ["sh", "-c", "echo 'canary image is broken'; exit 1"]
DOCKER
    for tag in good good2 bad; do
        docker build -q -f "$BUILD/Dockerfile.$tag" -t "$REGISTRY/canary:$tag" "$BUILD" >/dev/null
        docker push -q "$REGISTRY/canary:$tag" >/dev/null
    done
}

point() {  # move :latest to one of the built tags, in the registry and locally
    local tag="$1"
    docker pull -q "$REGISTRY/canary:$tag" >/dev/null
    docker tag "$REGISTRY/canary:$tag" "$REGISTRY/canary:latest"
    docker push -q "$REGISTRY/canary:latest" >/dev/null
    echo "canary:latest now serves $tag ($(docker image inspect --format '{{.Id}}' "$REGISTRY/canary:$tag"))"
}

case "${1:-setup}" in
setup)
    start_registry
    build_images
    mkdir -p "$STACK_DIR"
    cat > "$STACK_DIR/docker-compose.yml" <<COMPOSE
# Fixture: the canary update target. :latest is moved between a good and a crashing image by
# tests/homelab/t42-canary.sh so the update.canary step has something real to pull.
services:
  app:
    image: $REGISTRY/canary:latest
    container_name: fixture-canary
    restart: unless-stopped
COMPOSE
    point good
    docker compose -f "$STACK_DIR/docker-compose.yml" up -d
    docker inspect --format '{{.Name}} {{.State.Status}} {{.Image}}' fixture-canary
    ;;
point)
    point "${2:?good|good2|bad}"
    ;;
teardown)
    docker compose -f "$STACK_DIR/docker-compose.yml" down 2>/dev/null || true
    rm -rf "$STACK_DIR"
    docker rm -f pe-registry >/dev/null 2>&1 || true
    docker image rm -f "$REGISTRY/canary:latest" "$REGISTRY/canary:good" \
        "$REGISTRY/canary:good2" "$REGISTRY/canary:bad" >/dev/null 2>&1 || true
    echo "torn down"
    ;;
*)
    echo "usage: $0 {setup|point good|good2|bad|teardown}" >&2; exit 2
    ;;
esac
