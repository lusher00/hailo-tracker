#!/bin/bash
# Off-device test harness. Fakes the camera and the NPU, runs everything else
# for real, and checks the HTTP surface. Useful on a laptop before you deploy.
#
#   ./tests/run_tests.sh
set -e
cd "$(dirname "${BASH_SOURCE[0]}")/.."
python3 tests/test_follow.py
python3 tests/test_pipeline.py "$@"
