# Post-Audit Remediation Design

## Scope

Resolve the three findings confirmed after commit 23f8636 without changing
strategy decisions, order construction, pending schema fields, or user-facing
configuration.

## Stable signer lock namespace

The default live-account lock namespace must be stable for every process in
one supported OS namespace and must not depend on TMPDIR, TEMP, the current
user profile, or the repository checkout.

- Linux uses abstract Unix-domain sockets with account-derived names.
- Windows uses `Global\\entropy-arb-live-<account-digest>` named mutexes.
- Tests may continue injecting directory= to exercise isolated file locks.
- If a kernel lock object cannot be created or acquired, live startup fails
  closed; it must not fall back to a per-user directory.

Both backends retain account-derived, non-secret lock identifiers and acquire
overlapping account sets in sorted order. Cross-host and cross-Linux-network-
namespace exclusion remain out of scope and require different signer accounts.

The follow-up design in
`2026-09-15-live-lock-kernel-hardening-design.md` supersedes the original
filesystem/semaphore implementation details after the post-implementation
security review.

## Derived audit overflow normalization

OPEN and ADD cross-field checks convert every operand through the existing
finite-number validators before arithmetic. Calculations that overflow to a
non-finite float are treated as inconsistent audit evidence and raise
PendingExecutionStateError. No broader exception handler is added.

## Upgrade documentation

The English and Chinese READMEs describe pending schema v4 accurately:

- v4 is the current writable/readable active schema;
- an empty v3 journal remains readable;
- an active v3 journal remains unchanged and blocks startup for manual
  verification;
- v2, unknown, and malformed journals remain fail-closed.

## Validation

Tests first reproduce the temp-root lock bypass and the three overflowing
derived calculations. Focused tests, the full suite with warnings as errors,
compileall, and git diff --check must pass. The worktree must remain clean
after the final commit.
