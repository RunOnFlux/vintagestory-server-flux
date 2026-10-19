#!/bin/bash
# Tests against a BUILT image: what the unit tests cannot see, because they run the scripts with
# the machine's own Python and tools, not the image's.
#
#   ./tests/test-image.sh [image]        default vintagestory-server-flux:local
#
# No network and no game: tests/image/in-container.sh serves a stand-in release from local files
# and puts a stand-in server in place of `dotnet`, and runs the real supervisor around them. CI
# runs this BEFORE it publishes.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

image="${1:-vintagestory-server-flux:local}"
docker run --rm --entrypoint bash -v "${PWD}:/repo:ro" "${image}" /repo/tests/image/in-container.sh
