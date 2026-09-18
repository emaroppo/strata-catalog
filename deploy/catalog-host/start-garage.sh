#!/usr/bin/env bash
# Run a single-node Garage on this host, for a catalog host that has none.
#
#   ./deploy/catalog-host/start-garage.sh
#
# Fresh secrets, one node, the bucket config.example.toml names, and a
# read-write key for the machine that ingests. A host that already runs
# Garage skips this and points config.toml at it. After this, run
# bootstrap-env.sh, which finds the container by name and makes the blob
# server's read-only key.
#
#   GARAGE_DIR        where its config and data live      (default ~/garage)
#   GARAGE_IMAGE      the image                           (default dxflrs/garage:v2.4.1)
#   GARAGE_CAPACITY   what this node offers               (default 20G)
#   STRATA_S3_BUCKET  the first bucket                    (default mydata)
#   STRATA_WRITE_KEY  the ingesting machine's key         (default strata-write)
#
# A key's secret prints once, at creation, so it is written to
# $GARAGE_DIR/<key>.key (0600) rather than to the terminal. Copy that file to
# the machine that ingests; its two lines are the environment it needs.
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=garage.sh
source "$here/garage.sh"
require_docker

dir=${GARAGE_DIR:-$HOME/garage}
image=${GARAGE_IMAGE:-dxflrs/garage:v2.4.1}
capacity=${GARAGE_CAPACITY:-20G}
bucket=${STRATA_S3_BUCKET:-mydata}
write_key=${STRATA_WRITE_KEY:-strata-write}
name=garage

if docker ps -a --format '{{.Names}}' | grep -qx "$name"; then
    echo "A container named $name already exists. This host has a Garage; run" >&2
    echo "bootstrap-env.sh against it, or remove the container to start over." >&2
    exit 1
fi

mkdir -p "$dir/meta" "$dir/data"
if [ ! -e "$dir/garage.toml" ]; then
    rpc_secret=$(openssl rand -hex 32)
    admin_token=$(openssl rand -hex 32)
    (
        umask 077
        printf '%s\n' \
            'metadata_dir = "/var/lib/garage/meta"' \
            'data_dir = "/var/lib/garage/data"' \
            'db_engine = "sqlite"' \
            '' \
            '# One node, so one copy.' \
            'replication_factor = 1' \
            '' \
            'rpc_bind_addr = "[::]:3901"' \
            'rpc_public_addr = "127.0.0.1:3901"' \
            "rpc_secret = \"$rpc_secret\"" \
            '' \
            '[s3_api]' \
            's3_region = "garage"' \
            'api_bind_addr = "[::]:3900"' \
            'root_domain = ".s3.garage.localhost"' \
            '' \
            '[admin]' \
            'api_bind_addr = "[::]:3903"' \
            "admin_token = \"$admin_token\"" \
            > "$dir/garage.toml"
    )
    echo "Wrote $dir/garage.toml with fresh secrets."
fi

docker run -d --name "$name" --restart unless-stopped \
    -p 3900:3900 -p 3901:3901 -p 3903:3903 \
    -v "$dir/garage.toml:/etc/garage.toml:ro" \
    -v "$dir/meta:/var/lib/garage/meta" \
    -v "$dir/data:/var/lib/garage/data" \
    "$image" > /dev/null
echo "Started $name from $image."

g="docker exec -i -e RUST_LOG=$RUST_LOG $name /garage"
for _ in $(seq 30); do
    $g status > /dev/null 2>&1 && break
    sleep 1
done
if ! $g status > /dev/null 2>&1; then
    echo "Garage did not come up. docker logs $name says why." >&2
    exit 1
fi

node=$($g node id -q | cut -d@ -f1)
$g layout assign -z dc1 -c "$capacity" "$node" > /dev/null
$g layout apply --version 1 > /dev/null
echo "Layout: one node, $capacity."

$g bucket create "$bucket" > /dev/null
echo "Bucket: $bucket."

created=$($g key create "$write_key" 2>&1)
key_id=$(printf '%s' "$created" | grep -oE 'GK[0-9a-f]{24}' | head -1 || true)
secret=$(printf '%s' "$created" | grep -oE '\b[0-9a-f]{64}\b' | head -1 || true)
if [ -z "$key_id" ] || [ -z "$secret" ]; then
    echo "Could not read the key out of 'garage key create':" >&2
    echo "$created" >&2
    exit 1
fi
(umask 077; printf 'STRATA_S3_ACCESS_KEY=%s\nSTRATA_S3_SECRET_KEY=%s\n' "$key_id" "$secret" > "$dir/$write_key.key")
$g bucket allow --read --write "$bucket" --key "$write_key" > /dev/null
echo "Key: $write_key ($key_id), read and write on $bucket; its secret is in $dir/$write_key.key."

echo
echo "Next: $here/bootstrap-env.sh"
