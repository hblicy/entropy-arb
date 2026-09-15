# Live Lock Kernel Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace bypassable default live-account locks with fail-closed Linux and Windows kernel objects and correct operator documentation.

**Architecture:** Linux holds abstract Unix-domain sockets and Windows holds initially owned global named mutexes. Explicit `directory=` retains the existing file-lock backend for isolated tests, while production defaults never touch a shared filesystem path.

**Tech Stack:** Python 3, Linux `AF_UNIX`, Windows Kernel32 mutex APIs through `ctypes`, pytest, Markdown.

---

### Task 1: Reproduce default-lock bypasses

**Files:**
- Modify: `tests/test_live_lock.py`

- [x] **Step 1: Add the failing namespace test**

Update the temp-root test so a production default has no file paths. Assert
Windows names start with `Global\\entropy-arb-live-`; assert Linux names are
NUL-prefixed abstract socket names.

- [x] **Step 2: Add the failing Windows pre-creation test**

On Windows, derive unique default names from `tmp_path`, pre-create each name
as a semaphore with initial and maximum count two, and assert
`LiveProcessLock.acquire()` raises `LiveProcessLockError`. Always close the
seed handles.

- [x] **Step 3: Add a default contention test**

Create two default locks with unique identical identities. Acquire the first,
assert the second raises the existing `already running` error, then release
both in `finally`.

- [x] **Step 4: Verify RED**

Run:

~~~bash
python -m pytest -q -p no:cacheprovider tests/test_live_lock.py
~~~

Expected: the namespace and pre-created-semaphore tests fail against the
current semaphore/file implementation.

### Task 2: Implement kernel-backed production locks

**Files:**
- Modify: `entropy_arb/live_lock.py`
- Test: `tests/test_live_lock.py`
- Test: `tests/test_engine.py`

- [x] **Step 1: Select a backend only from platform and explicit directory**

Compute sorted account digests once. Use file targets only when `directory` is
explicit. With no directory, populate Windows mutex names or Linux abstract
socket names; reject other platforms with `LiveProcessLockError`.

- [x] **Step 2: Implement Linux abstract socket acquisition**

For every abstract name create `socket.socket(AF_UNIX, SOCK_STREAM)` and call
`bind(name)`. Convert `EADDRINUSE` to the existing contention error, close the
current socket on every error, and roll back prior sockets in reverse order.

- [x] **Step 3: Implement Windows initially owned mutex acquisition**

Bind `CreateMutexW`, `ReleaseMutex`, and `CloseHandle`. Clear last error before
`CreateMutexW(None, True, name)`. A null handle is a creation failure; a valid
handle with `ERROR_ALREADY_EXISTS` is closed and treated as contention. Store
only newly created, owned handles.

- [x] **Step 4: Implement release for both kernel backends**

Close Linux sockets in reverse order. Release and close every Windows mutex,
preserving the first cleanup error after attempting all handles. Keep the
existing explicit-directory file release unchanged.

- [x] **Step 5: Verify GREEN**

Run:

~~~bash
python -m pytest -q -p no:cacheprovider tests/test_live_lock.py tests/test_engine.py
~~~

Expected: all selected tests pass, including live startup contention before
tasks or orders.

### Task 3: Correct documentation and prior design notes

**Files:**
- Modify: `README.md`
- Modify: `README.zh-CN.md`
- Modify: `docs/superpowers/specs/2026-09-15-post-audit-remediation-design.md`
- Modify: `docs/superpowers/plans/2026-09-15-post-audit-remediation.md`

- [x] **Step 1: Replace lock-file-only operator wording**

State that Linux uses abstract Unix sockets, Windows uses global named
mutexes, the object is released by the OS at process exit, and operators must
not bypass it. Limit Linux guarantees to one network namespace and require all
old live engines to stop before upgrading from a file-lock version.

- [x] **Step 2: Supersede the previous semaphore/file design**

Update the earlier same-day design and plan to point to the kernel-hardening
follow-up and remove claims that production uses Windows semaphores or POSIX
`/tmp` files.

- [x] **Step 3: Verify stale wording is gone**

Run:

~~~bash
rg -n "named semaphores|/tmp/entropy-arb-live-locks|绕过锁文件|its file" README.md README.zh-CN.md docs/superpowers/specs/2026-09-15-post-audit-remediation-design.md docs/superpowers/plans/2026-09-15-post-audit-remediation.md
~~~

Expected: no stale production-lock claims remain.

### Task 4: Complete verification and commit

**Files:**
- Verify all files listed above and this plan.

- [x] **Step 1: Run full verification**

~~~bash
python -m pytest -q -p no:cacheprovider -W error
python -m compileall -q entropy_arb main.py tools tests
git diff --check
git status --short
~~~

Expected: 0 failures, compile and diff checks exit zero, and status lists only
the intended source, tests, docs, and plan changes.

- [x] **Step 2: Commit the verified implementation**

~~~bash
git add entropy_arb/live_lock.py tests/test_live_lock.py README.md README.zh-CN.md docs/superpowers/specs/2026-09-15-post-audit-remediation-design.md docs/superpowers/plans/2026-09-15-post-audit-remediation.md docs/superpowers/plans/2026-09-15-live-lock-kernel-hardening.md
git commit -m "修复：加固跨平台实盘账户锁"
~~~
