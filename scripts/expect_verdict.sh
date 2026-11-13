#!/usr/bin/env bash
# Runs a netdiag command and passes only when it ends with a verdict:
# exit 0 healthy, 1 problem found, 2 warnings only, 3 incomplete.
# Any other exit code (usage error, internal error, Ctrl-C) fails, and so does
# a Python traceback on stderr, because an unhandled exception also exits 1.
err=$(mktemp)
"$@" 2>"$err"
rc=$?
cat "$err" >&2
echo "exit code: $rc" >&2
if grep -q '^Traceback (most recent call last):' "$err"; then
  rm -f "$err"
  echo "netdiag crashed: a Python traceback is on stderr" >&2
  exit 1
fi
rm -f "$err"
case $rc in
  0 | 1 | 2 | 3) exit 0 ;;
  *)
    echo "exit code $rc is not a verdict" >&2
    exit 1
    ;;
esac
