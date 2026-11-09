#!/usr/bin/env bash
highest_verdict_exit=2
"$@"
rc=$?
echo "exit code: $rc" >&2
test "$rc" -le "$highest_verdict_exit"
