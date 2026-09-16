# Live Lock Kernel Hardening Design

## Scope

Close the remaining same-namespace live-account lock bypass and filesystem
safety findings without changing account identity, strategy behavior, order
flow, configuration, or pending execution state.

## Alternatives considered

1. A root-created shared lock directory can make file locks safe across users,
   but adds a privileged deployment prerequisite and still needs careful link
   and inode validation.
2. Direct files under `/tmp` avoid a user-owned subdirectory, but file owners
   can unlink their own entries while another user holds the old inode.
3. Kernel namespaces avoid filesystem ownership and replacement entirely.
   This is selected because the production targets are Linux VPS and Windows.

## Selected architecture

Default live locks use one deterministic, account-digest-derived kernel object
per signing account, acquired in sorted order:

- Linux binds an abstract `AF_UNIX` socket name. Abstract names have no
  filesystem entry, so symlinks, FIFOs, directory modes, truncation, and
  cross-user file ownership are absent. A duplicate bind in the same network
  namespace fails closed.
- Windows creates an initially owned `Global\\` named mutex. Any existing
  object is treated as contention without waiting, so a pre-created semaphore,
  unlocked mutex, or recursively opened mutex cannot allow a second engine.
- Unsupported default platforms fail closed with an actionable error.
- Explicit `directory=` keeps the existing file-lock backend only for isolated
  tests; it is not used by engine production startup.

Partial acquisition failure releases every earlier account object in reverse
order. Normal cleanup releases all objects, while process termination lets the
operating system close their handles automatically. Object names contain only
SHA-256 account digests.

Linux abstract names are scoped to one network namespace. Processes in
different containers or other isolated network namespaces must use different
signing accounts; host-wide container coordination is outside this project's
direct-VPS deployment scope.

## Errors and compatibility

Contention raises `LiveProcessLockError` with the existing "already running"
message. Kernel creation or release failures remain visible and block live
startup. Record-only behavior is unchanged and takes no lock.

The operator documentation calls this an operating-system lock object and
describes Linux abstract sockets and Windows global mutexes. No migration of
strategy or recovery files is required.

The prior file-lock version and this kernel-lock version do not share a Linux
lock namespace. Operators must stop and verify termination of every old live
engine before upgrading. Mixed-version rolling upgrades are explicitly
unsupported; record-only processes do not acquire either lock.

## Tests

- A Windows regression pre-creates count-two semaphores for the predictable
  names and proves acquisition fails closed.
- A default-backend test proves two instances for the same account conflict.
- Namespace tests prove defaults do not depend on process temp roots and do
  not expose filesystem paths.
- Existing explicit-directory file-lock and engine lifecycle tests remain.
- Full pytest, compileall, and diff checks must pass.
