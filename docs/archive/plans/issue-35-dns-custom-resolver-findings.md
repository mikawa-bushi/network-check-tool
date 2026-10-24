# Findings: issue-35-dns-custom-resolver

## Round 1

### Code Reviewer
- Blocking: 0
- Should Fix: 3
- Informational: 7
- Key Issues:
  - **Should Fix**: the slow-WARN (>1000ms) summary omitted the `via <resolver>` suffix (it was
    computed after the early return), so a slow custom-resolver query didn't say which server was
    slow. Moved `suffix` above the slow-WARN branch and appended it there too.
  - **Should Fix**: `_canned_run` was dead code in the test file (the custom-resolver test used an
    inline `spy_run`). Removed.
  - **Should Fix**: no test covered the custom-resolver FAIL path. Added
    `test_dns_custom_resolver_fail_path` (nslookup returns only the server line / NXDOMAIN →
    `FAIL` naming the resolver).
  - Informational (verified): the `#`-exclusion heuristic is sound for bind-utils nslookup on
    Linux/macOS (server line has `#port`, answers don't; IPv6 answers have no `#`); no shell
    injection (`_resolver` is an argv element, no `shell=True`); timeout/NXDOMAIN both degrade to
    `None` → FAIL correctly; loader→`_resolver` plumbing is correct.
- Judgment: Strong review — no correctness bugs; the slow-WARN suffix and the missing FAIL test
  were real gaps. All addressed.

### Doc Parrot
- Divergences Found: 0
- Details:
  - `_parse_nslookup_addresses` — **OK**: prose ("keep values without `#`, drop the server's
    `#port` line, sorted unique") matches; verified the server line is excluded and the result is
    sorted/unique.
  - `_resolve_via_server` — **OK**: prose ("resolve against a specific server via nslookup; answer
    addresses or `None` if none") matches; verified both the answer and the `None` cases.
- Judgment: Nothing to flag.

### Actions taken (Round 1)
- `dns.py`: include the resolver in the slow-WARN summary.
- `tests`: removed dead `_canned_run`; added the custom-resolver FAIL-path test.

### Carry-forward (informational, not changed)
- Record type isn't configurable from the scenario (nslookup default A/AAAA) — out of scope.
- Very old BSD `nslookup` output isn't handled; CI targets ubuntu-latest, so acceptable.
