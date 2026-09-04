# Review Findings Remediation Design

## Goal

Remove the confirmed live-trading safety and recorded-data integrity defects
without changing spread thresholds, sizing, inventory policy, or venue support.

## Execution safety

An ambiguous order outcome places the engine in recovery mode immediately.
Recovery mode blocks `_scan()` from creating another opportunity while market
data, recorder, status, and reconciliation tasks remain alive.  An accepted
Lighter order is resolved only from the terminal order stream using its
`client_order_index`; every exception after that index is allocated retains the
reference, and Engine never downgrades it to an unversioned REST position
snapshot.  A venue that cannot provide an order reference remains paused until
a position snapshot is provably newer than submission; the current Hyperliquid
adapter cannot provide that proof and therefore requires manual recovery for a
post-submit unknown.  Shutdown still waits for referenced ambiguous orders to
be resolved and retries transient terminal lookups.  Once a terminal result or
sufficiently fresh complete snapshot makes the original order outcome known,
the shutdown-only unknown flag is cleared before deterministic repair is attempted;
a known unhedgeable residual or pre-submit repair failure therefore cannot cause
an infinite shutdown loop or an unnecessary unknown-outcome retry delay.  Repair
only runs from a complete venue snapshot.  A hedge attempt runs as its own
supervised execution task so shutdown waits for the order operation without
waiting for the long-lived reconcile loop itself.  Shutdown reconciliation
backoff is interruptible: a successful background recovery signals the drain
immediately instead of leaving cleanup asleep for the full retry interval.

Both legs receive the same settlement timestamp immediately after the paired
adapter calls settle, before normalized result validation.  Even a malformed
adapter return therefore observes the settlement grace period before recovery
can read positions.

Once paired submission starts, caller cancellation is remembered but does not
cancel the two bounded adapter operations.  The engine first collects both
results, registers any order references, applies known fills, and writes the
audit record; it then re-raises the original cancellation so lifecycle
supervision still sees it.

Only an explicit pre-submit/signing failure or exchange rejection is a
`send-failed` result.  An exception crossing the venue boundary, an accepted
Hyperliquid response with an unknown status, or an unrecognised Lighter order
status is `unknown`.  Unexpected strategy/execution programming errors enter
recovery and propagate to the existing task supervisor instead of being
logged-and-ignored.

`OrderResult` rejects non-finite fill quantities and prices before they can
poison local position, cash, or risk comparisons.  Lighter close failures flow
to the engine's existing cleanup error collector.

Position reconciliation applies the same finite-number boundary before a venue
read can count toward a complete snapshot.  An unexpected execution-task
cancellation is an unknown order outcome, while cancellation of long-lived
tasks is ignored only after engine shutdown begins.  Only two normalized
`filled` leg results can count as a successful arbitrage; matched partial fills
whose IOC remainders were cancelled remain execution failures.

Unexpected execution or adapter-contract failures disable further automatic
repair orders.  The engine still performs the strict position read needed to
resolve the last unknown outcome, then exits shutdown with the original error
and the known residual still paused for manual handling.  This prevents a
broken adapter from being called repeatedly during cleanup.

An audit-write failure grants exactly one supervised reduce-only submission.
The grant is consumed before submission, regardless of whether the repair is
filled, rejected, cancelled, or unresolved.  Terminal confirmation may continue
after an unresolved result, but it cannot create another automatic repair while
the audit sink remains unavailable.

If a shielded hedge child is itself cancelled, that cancellation is converted
to a normal supervised execution failure unless the awaiting caller also has a
real cancellation request.  Shutdown drain can therefore perform the required
second strict position read for the hedge's new unknown outcome instead of
mistaking child cancellation for cancellation of the drain itself.

The Lighter venue registers ownership of its SDK client immediately after
construction so startup validation failures can close it.  The shared HTTP
session is closed inside the same cancellation-resistant engine cleanup and
first-error policy as venue resources.

## Data integrity

Configuration rejects collisions among outputs that can be active in the
selected run mode.  Live mode checks trades, minute recording when enabled,
and the log file only when the dashboard actually enables file logging;
record-only mode checks minute, signal, and the active log file.  The inactive
signal or log file remains allowed to match another active path.  A collision
includes equal files, hard links, and either path being an ancestor of the
other, preventing a file output from blocking creation of another output's
parent directory.

Startup validation checks the exact output target rather than only an existing
ancestor.  Every Windows path component rejects illegal characters, trailing
spaces or dots, and reserved device names; a missing parent is created and
tested before live trading starts.  Market identity values written to CSV,
including `symbol` and `entropy.dex`, reject Unicode control characters.

CSV appenders verify both schema and the final physical record.  If the file
has a torn or structurally invalid tail, the complete file is preserved under
the next `.old.N` name and a new file is started.  This avoids destructive
truncation while preventing the first post-restart row from being joined to a
partial record.  Header inspection reads one physical line in binary mode so
invalid UTF-8 later in the file cannot bypass safe rotation.  The same check
also covers the trades audit CSV.  Filesystem access errors are never classified
as corrupt CSV content: they propagate without moving the path, while only
decoded content/schema failures qualify for archive rotation.

The analyzer combines rows sharing `(symbol, entropy_dex, hedge_venue,
minute_ts)`: sample counts are summed, means are sample-weighted, extrema span
all fragments, and the last fragment supplies the minute close.  Minimum sample
filtering happens after combination.  Exact per-venue fee flags must be supplied
as a pair.  Market identity is validated from every parseable row before time
window and minimum-sample filtering, so filters cannot hide a mixed file.

## Verification

Each defect receives a regression test that fails on the current code before
the implementation is changed.  Targeted tests run after each fix group, then
the complete suite runs with warnings treated as errors, followed by syntax,
whitespace, and worktree checks.

Dashboard shutdown keeps Engine failures primary while logging any simultaneous
Dashboard finalizer failure with traceback.  The concrete Dashboard coroutine
does not suppress cancellation, and its cleanup contains no unbounded await.

## Final review follow-up

Residual hedge submissions must obey the same unknown-outcome boundary as the
paired legs.  If an adapter exception crosses that boundary, the engine cannot
correlate the request with a terminal order and therefore disables automatic
repair, stops, and leaves the position for manual recovery.  It must not turn a
strict position read into permission to submit another repair order.

Lighter client order indexes use the API-key index as an account-wide namespace
and a monotonic millisecond counter inside that namespace.  Separate processes
on one account must use separate API keys, which is already required for safe
nonce ownership.  Unknown Lighter orders remain retained until Engine consumes
their terminal result, and a websocket cache miss is checked against the
authenticated inactive-orders REST endpoint before recovery waits again.

A positive normalized fill always carries a finite positive average price.
Malformed Hyperliquid filled responses are unknown, never successful fills with
synthetic prices.  After a local Lighter trade, an unversioned REST position that
differs from local state is not authoritative: trading pauses and retries until
the REST value agrees, rather than adopting the mismatch and hedging it.

Venue and HTTP resource closing is bounded independently from order draining.
Order operations still receive unlimited safe drain time, while a resource
close that exceeds its deadline is logged, abandoned for the outer controlled
event-loop shutdown, and cannot prevent the remaining resources from closing.

Analyzer market identity is validated before sample and metric filtering, so an
invalid row from a second identified market cannot be used to conceal a mixed
file.  Operator documentation states that an unreferenced Hyperliquid unknown
stops for manual recovery rather than promising automatic reconciliation.
