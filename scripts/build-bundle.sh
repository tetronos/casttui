#!/bin/sh
# Build the pure-Python bundle that the casttui .deb installs in /usr/lib/casttui.
# casttui plus the exact runtime dependencies from uv.lock, installed with pip in a
# python:3.11 container (the lowest supported Python, so its version-specific backports
# are included). zeroconf, charset-normalizer and wcwidth are built from source without
# their optional C speedups; protobuf's compiled module is removed (the launcher selects its
# pure-Python implementation). The result runs on any python3 >= 3.11 on any architecture.
# Usage: scripts/build-bundle.sh OUTDIR    (needs uv and docker)
set -eu
out="${1:?usage: scripts/build-bundle.sh OUTDIR}"
root="$(cd "$(dirname "$0")/.." && pwd)"
[ ! -e "$out" ] || { echo "$out exists" >&2; exit 1; }
mkdir -p "$out"; out="$(cd "$out" && pwd)"
(cd "$root" && uv export --frozen --no-dev --no-hashes --no-emit-project -o "$out/requirements.txt" >/dev/null)
docker run --rm -v "$root:/src:ro" -v "$out:/out" -e SKIP_CYTHON=1 -e CHARSET_NORMALIZER_USE_MYPYC=0 \
	python:3.11-slim sh -c '
	set -e
	pip -q install --root-user-action=ignore --disable-pip-version-check --no-compile --no-deps \
		--no-binary zeroconf,charset-normalizer,wcwidth --target /out/bundle -r /out/requirements.txt
	cp -r /src /tmp/src && rm -rf /tmp/src/.venv
	pip -q install --root-user-action=ignore --disable-pip-version-check --no-compile --no-deps --target /out/bundle /tmp/src
	rm -rf /out/bundle/bin
	find /out/bundle -name "_message.abi3.so" -path "*google/_upb/*" -delete
	so=$(find /out/bundle \( -name "*.so" -o -name "*.so.[0-9]*" \) | head -5)
	[ -z "$so" ] || { echo "compiled modules left in the bundle:"; echo "$so"; exit 1; }
	chown -R '"$(id -u):$(id -g)"' /out'
echo "bundle: $out/bundle ($(du -sh "$out/bundle" | cut -f1))"
