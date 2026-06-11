#!/bin/bash
# Publish network-check-tool to PyPI.
# Prerequisites: `poetry config pypi-token.pypi <token>` (and test-pypi for a dry run).
set -euo pipefail

# Validate pyproject metadata before doing anything.
poetry check

# Regenerate the lock so the version bump and the optional pyyaml dep are captured.
poetry lock
poetry install --all-extras

# Build sdist + wheel into dist/.
poetry build

# Optional dry run against TestPyPI first:
#   poetry publish -r test-pypi
# Then the real thing:
poetry publish
