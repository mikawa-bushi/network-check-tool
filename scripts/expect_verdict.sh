#!/usr/bin/env bash
# Run a netdiag command for a CI smoke test. netdiag exits 0, 1 or 2 to report
# the network it checked, so only a higher code fails the step.
"$@"
rc=$?
echo "exit code: $rc" >&2
test "$rc" -le 2
