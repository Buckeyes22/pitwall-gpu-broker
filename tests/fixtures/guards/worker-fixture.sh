#!/bin/bash
# Fixture: worker entrypoint that uses the supported HF CLI
# This file is a TEST FIXTURE only - it should never be scanned by pre-commit
# as it's in tests/fixtures/, not in worker-owned paths.

set -euo pipefail

echo "Downloading model..."
hf download meta-llama/Llama-2-7b --cache-dir /tmp/model-cache
echo "Done"
