# entropy-arb

**[中文文档 / Chinese documentation → README.zh-CN.md](README.zh-CN.md)**

Open-source two-venue perp arbitrage bot. One leg is always **Entropy**
(the `io` builder dex on Hyperliquid); the other leg — the hedge — is one of:

| `--hedge` | venue | quote | taker fee | protocol |
|---|---|---|---|---|
| `lighter` | Lighter mainnet | USDC | 0 bps | zkLighter ws (diff books, async settle) |
| `lighter-rh` | Lighter Robinhood chain | **USDG** | 0 bps | zkLighter ws |
| `tradexyz` | Hyperliquid trade.xyz dex | USDC | ~1 bps | HL l2Book, sync IOC settle |

> **Referral links** — signing up through these supports this project:
> - Entropy — Tier 4 referral, 100% rebates: <https://entropy.io/?r=yourquantguy>
> - Lighter Robinhood chain: <https://robinhoodchain.lighter.xyz/?referral=QUANT>
> - trade.xyz (Hyperliquid): <https://app.hyperliquid.xyz/join/QUANTGUY>

When the same symbol trades rich on one venue and cheap on the other, the bot
simultaneously sells the rich book and buys the cheap book with taker orders,
carrying a delta-neutral position until the premium reverts and the opposite
crossing unwinds it. Every price it acts on is the **actual order book of the
exchange that will fill the order** — Hyperliquid books come from the official
websocket (`wss://api.hyperliquid.xyz/ws`), Lighter books from Lighter's
official websocket.

While it runs — even with no credentials and no strategy — it records both
books to **1-minute CSV bars**, and the bundled analyzer turns that data into
the three numbers that define the whole strategy.

## The signal

The band is three numbers in `config.yaml`, derived by you from recorded
data:

```
premium_bps = (Entropy price / hedge price − 1) × 10 000

                          ┌──────────────  SELL entropy + BUY hedge
midline + upper  ───────────────────────────────────────────────────
                                       ▲
midline          ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ┼ ─ ─   the premium's usual level
                                       ▼
midline − lower  ───────────────────────────────────────────────────
                          └──────────────  BUY entropy + SELL hedge
```

- `midline_bps` — where the premium normally sits. Cross-venue premiums are
  rarely centered at zero (different oracles, different quote assets, listing
  premia), so a zero-centered band would fire one direction only, cap out and
  never unwind. Measure where the premium actually sits and type it in.
- `upper_bps` / `lower_bps` — the entry bands on each side of the midline.

Both hurdles are applied to **executable** prices (entropy bid vs hedge ask,
and vice versa) and are **net of both venues' taker fees** — the engine adds
fees on top before a slice qualifies. A full round trip therefore nets
**≥ upper + lower bps after fees by construction**.

One consequence worth understanding: with `midline_bps: 5`, the buy-entropy
hurdle is `lower − midline`, which can be **negative**. That is intentional —
if entropy is persistently 5 bps rich, buying it at a 0 bps premium is 5 bps
cheap versus its own equilibrium, and that trade is the profitable unwind of
an earlier sell at `midline + upper`. It also means a **wrong midline loses
money**: if you type `midline_bps: 5` while the true premium sits at 0, the
bot happily buys entropy at fair value all day. Measure first, then trade —
that is what the recorder and analyzer are for.

## Quick start

```bash
git clone https://github.com/your-quantguy/entropy-arb.git && cd entropy-arb
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # data collection needs only this

cp config.example.yaml config.yaml       # the strategy (thresholds, sizing, risk)
cp .env.example .env                     # credentials — required to trade
```

The markets are **not** in the config file — you state them explicitly on
every start: `--symbol` selects the Entropy market and `--hedge` selects one
of `lighter`, `lighter-rh`, or `tradexyz`. If the hedge venue uses a different
name for the same asset, add `--hedge-symbol`; it defaults to `--symbol` when
omitted.

There is **no paper mode** — the bot either collects data (`--record-only`)
or trades live. Validate with recorded data and tiny position caps, not with
simulated fills.

**1. Collect data first** (no credentials needed):

```bash
python3 main.py --record-only --symbol SNDK --hedge lighter-rh
```

For example, Entropy `ANTH` and Robinhood Lighter `ANTHROPIC` are selected
with:

```bash
python3 main.py --record-only --symbol ANTH --hedge lighter-rh \
  --hedge-symbol ANTHROPIC --no-dashboard
```

Let it run for at least a few hours (a day is better — premiums have
intraday regimes). It writes minute aggregates to `logs/minutes.csv` and,
in `--record-only` only, signal lifecycles to `logs/signals.csv`. A signal
row is written immediately on `start`, once per second as `sample`, and on
disappearance, stale books, or shutdown as `end`. These rows are observation
only: they do not gate entries or change live strategy behavior. The signal
path can be changed with `recorder.signal_csv`. Both files include each leg's
native symbol, the Entropy DEX, and the hedge venue on every row.

Reference prices and funding are collected on the existing market-data
WebSockets, initialized by REST, and refreshed by REST while a reference
stream is stale. Hyperliquid and Lighter funding are normalized to
`bps/hour`. Reference failures and residual alerts are observational only:
they are logged and recorded but do not block an entry or change its threshold.
Signal rows append both legs' reference price/funding/age fields plus the
directional signed executable premium, signed residual, residual edge, and
net funding. Missing reference values stay blank without dropping the signal.

Use a separate `recorder.csv` for each symbol/venue combination. The analyzer
accepts legacy files with the old `symbol` identity or with no market columns,
but rejects a file that contains more than one identified market instead of
producing unsafe combined thresholds. A legacy schema or incomplete/invalid
final CSV row causes the file to be preserved at the next free `.old`,
`.old.1`, ... archive before a clean file is written.
In `--record-only`, both recorder output files are opened at startup; an output
creation or write failure stops the process with an error. If a minute row has
already been handed to the CSV writer when `flush()` reports an ambiguous I/O
failure, that minute aggregate is not blindly written again; this prevents
duplicates but cannot guarantee delivery after a failed flush.
With `recorder.signal_rotate_daily: true`, `signals.csv` rolls at the first
row of a new UTC day. A September 10 file becomes
`signals-20260910.csv.gz`; conflicts use `.gz.1`, `.gz.2`, and so on. The gzip
is fully verified before the raw archive is removed. If compression fails,
the dated raw CSV is retained and recording continues in a new `signals.csv`.

**2. Analyze and set your thresholds:**

```bash
python3 tools/analyze.py --entropy-fee-bps 0.9 --hedge-fee-bps 0.0
python3 tools/analyze.py --csv logs/minutes-20260910.csv.gz \
  --entropy-fee-bps 0.9 --hedge-fee-bps 0.0
```

It analyzes `logs/minutes.csv` and prints the premium distribution, how often
each candidate band would have fired, and a ready-to-paste `thresholds:` block
for `config.yaml`. Restart fragments carrying the same market and minute are
combined before sample filtering, so a minute is counted once. It does not
analyze `logs/signals.csv`, and it will not mix multiple identified markets
from one minute file.
When the new reference columns contain valid samples, the analyzer also prints
the minute-close distributions of reference basis, signed residual, and
funding difference. Plain `.csv` and `.csv.gz` inputs use the same logic.

**3. Go live** — fill in `.env`, install the signing SDKs, and start with
the smallest position caps that clear the venue minimums:

```bash
pip install -r requirements-live.txt
python3 main.py --symbol SNDK --hedge lighter-rh
```

The direct runtime and signing dependencies are pinned, including Lighter at
a specific Git commit. Upgrade them deliberately and repeat the full test and
record-only checks before deploying the new environment.

Running without `--record-only` sends real orders immediately once both
feeds are fresh and the band is crossed.

**Dashboard.** On a terminal the bot shows a live Rich dashboard: both
books with age/spread, positions and caps, equity and session PnL, the
executable premium of each direction against its full hurdle (fees and
inventory surcharge included, ● = armed), recorder progress, the last
executions, and a tail of the log (the full log goes to `logging.file`,
default `logs/engine.log`). It works in `--record-only` too. Add `--cn` to
display the dashboard in Chinese. Use `--no-dashboard` for plain console
logs (nohup/systemd — off-terminal runs fall back automatically), or set
`logging.dashboard: false`.

## Data collection & analysis

The recorder runs automatically in every mode (`recorder.enabled: true`).
Once per second it samples both live books; once per minute it writes a row:

| column | meaning |
|---|---|
| `minute_ts`, `time_utc` | minute start (epoch seconds, ISO UTC) |
| `entropy_symbol`, `entropy_dex`, `hedge_symbol`, `hedge_venue` | native market identity for both legs; one file should contain one pair |
| `entropy_bid/ask`, `hedge_bid/ask` | last fresh top-of-book of the minute |
| `premium_open/high/low/close/mean/std_bps` | mid-to-mid premium of Entropy over the hedge |
| `sell_edge_mean/max_bps` | executable premium for SELL entropy (entropy bid / hedge ask − 1) |
| `buy_edge_mean/max_bps` | executable premium for BUY entropy (hedge bid / entropy ask − 1) |
| `*_oracle_px`, `*_index_px`, `*_mark_px` | latest available normalized reference prices; unavailable venue fields remain blank |
| `*_funding_current/last_bps_per_hour`, `*_funding_last_ts_ms` | normalized current/last funding and its exchange timestamp |
| `*_reference_age_ms`, `reference_update_skew_ms` | monotonic age of each reference and receive-time skew between legs |
| `reference_basis_close_bps`, `funding_diff_close_bps_per_hour` | Entropy oracle / hedge index basis; current Entropy funding minus hedge funding |
| `residual_open/high/low/close/mean/std_bps` | mid-price premium minus reference basis, using only samples with both required references |
| `samples` | how many of the ~60 seconds both books were fresh |

Recorded edges are pre-fee. Pass each venue's taker fee separately with
`--entropy-fee-bps` and `--hedge-fee-bps`; the analyzer then applies the same
buy/sell ratio formula as live execution before counting firings. For example,
use `0.9` and `0.0` for Entropy + Lighter, or `0.9` and `1.0` for Entropy +
`tradexyz`. The two exact fee flags must be supplied together. The legacy
`--fees-bps` combined value remains accepted as an approximation for existing
scripts.
Fees can vary by account or venue; verify them before deployment. `--hours 24`
restricts to recent data; premiums drift, so re-run it regularly and update
`config.yaml`.

## Configuration

Strategy lives in `config.yaml` (validated — unknown keys, non-finite values,
and unsafe amount/rate/timeout boundaries are startup errors), credentials in `.env`, and the markets on the command line
(`--symbol`, optional `--hedge-symbol`, and `--hedge`). Full commented reference:
[config.example.yaml](config.example.yaml). The essentials:

| key | meaning | default |
|---|---|---|
| `thresholds.midline_bps` | premium center (measure it!) | — |
| `thresholds.upper_bps` / `lower_bps` | entry bands (> 0) | — |
| `entropy.dex` | Entropy's dex name on Hyperliquid | `io` |
| `*.taker_fee_bps` | per-venue taker fee | Entropy 0.9; Lighter 0.0; tradexyz hedge 1.0 |
| `*.max_position_usd` | per-venue position cap | 1000 |
| `*.max_orders_per_min` | per-venue send budget (sliding 60 s) | 120; lighter hedges 30 |
| `sizing.take_fraction` | fraction of crossable depth taken | 0.5 |
| `sizing.max_order_notional_usd` | hard cap on each leg's actual planned notional for one slice | 500 |
| `inventory.scale_bps` / `floor_frac` | inventory ladder (extra bps past `floor_frac` of the cap) | 10 / 0.5 |
| `execution.premium_persist_sec` | edge must persist before firing | 0.3 |
| `execution.*` | slippage bounds, timeouts, reconcile cadence… | see file |
| `recorder.*` | minute data; record-only signal lifecycle path | on, `logs/minutes.csv`; `logs/signals.csv` |
| `recorder.signal_rotate_daily` | rotate and verified-gzip signal rows by UTC day | true |
| `reference.rest_recovery_sec` / `stale_sec` | REST recovery cadence / reference stale threshold | 15 / 60 |
| `reference.residual_alert_bps` / `residual_persist_sec` | stateful observational residual alert threshold / persistence | 20 / 30 |
| `logging.dashboard` / `logging.file` | Rich dashboard on a tty; log file while it runs | on, `logs/engine.log` |

## Credentials (`.env`, live only)

- **Entropy / tradexyz (Hyperliquid)** — create an API ("agent") wallet at
  <https://app.hyperliquid.xyz/API>. `HL_PRIVATE_KEY` is the **agent** key,
  `HL_ACCOUNT_ADDRESS` your main account address. With `--hedge tradexyz`
  both legs share this account by default (one nonce sequence is handled
  internally); set `HL_PRIVATE_KEY_XYZ` / `HL_ACCOUNT_ADDRESS_XYZ`
  to split them. Fund the dex-specific clearinghouses you trade.
- **Lighter** — `LIGHTER_ACCOUNT_INDEX`, `LIGHTER_API_KEY_INDEX`,
  `LIGHTER_API_PRIVATE_KEY`, registered on the **same deployment** as your
  `--hedge` flag (mainnet and the Robinhood chain are separate accounts and
  keys — see [lighter-python](https://github.com/elliottech/lighter-python)).
  Every simultaneously running process on one account must use its own API
  key index/private key pair; sharing one key also shares its nonce stream and
  is unsupported.

## How execution works

- Both legs are **taker** orders sent concurrently: Lighter market orders
  with average-price protection settling on the authenticated account
  websocket; Hyperliquid HIP-3 IOC limits settle synchronously and omit
  `cloid` because HIP-3 currently rejects it. A timeout/5xx stays explicitly
  unresolved; because there is no order reference, the engine stops and
  requires manual position verification/recovery instead of automatically
  submitting a repair. The order is never blindly resent. Lighter submission
  and settlement each get a separate
  `settle_timeout_sec` window; a submission timeout is also treated as
  unresolved because the order may already have reached the venue.
- A **persistence gate** (`premium_persist_sec`) arms each direction and only
  fires if the edge survives — one-tick phantoms are filtered.
- **Inventory ladder**: past `floor_frac` of a venue's cap, adding to the
  position requires linearly more edge, up to `scale_bps` extra at the cap.
- **Net-delta hedge**: if legs fill unevenly, the imbalance is immediately
  reduced (reduce-only, price-protected), and positions are reconciled
  against the chain every `reconcile_sec`.
- **Failure containment**: a rate-limited venue pauses briefly; an
  unreachable venue (e.g. exchange maintenance) pauses trading and is probed
  every `venue_probe_sec` until it recovers; `max_consecutive_errors`
  execution pathologies halt the engine entirely. An ambiguous order result
  freezes new entries until strict position reconciliation and any required
  reduce-only hedge leave the known net position within tolerance.
- **Safe shutdown**: after a stop signal, no new opportunity is started and
  the process keeps waiting for every already-submitted two-leg execution to
  settle before closing exchange connections. A long wait is logged as
  critical rather than cancelling the in-flight order tasks. Initialization
  failures still close every venue and task already created. Any supervised
  background task that fails or exits unexpectedly stops the engine and makes
  the process exit nonzero after cleanup.
- **Live-only**: there is no simulated-fill mode. `--record-only` is the
  risk-free way to run it; anything else trades real money.

## Layout

```
main.py                  entry point (--record-only, or live by default)
entropy_arb/config.py    YAML + .env contract, validation
entropy_arb/book.py      order books + fee-aware crossing/sizing math
entropy_arb/models.py    normalized order-result domain values
entropy_arb/feeds.py     official HL ws + zkLighter ws book feeds
entropy_arb/venue_hl.py  Hyperliquid dex adapter (Entropy, tradexyz)
entropy_arb/venue_lighter.py  zkLighter adapter (mainnet, Robinhood chain)
entropy_arb/venues/base.py  common venue adapter protocol
entropy_arb/venues/registry.py  explicit adapter factory registry
entropy_arb/engine.py    the two-venue strategy loop
entropy_arb/dashboard.py Rich terminal dashboard
entropy_arb/recorder.py  1-minute bars + record-only signal lifecycles
tools/analyze.py         minutes.csv -> suggested thresholds
tests/                   python3 -m pytest tests/
```

The current CLI still runs exactly two legs. Venue construction now goes
through a common protocol and an explicit registry; this is the compatibility
foundation for the staged multi-hedge design in
`docs/superpowers/specs/2026-08-27-multi-hedge-arbitrage-design.md`.

## Known risks

- **A wrong midline is a losing strategy.** The premium center drifts;
  re-measure regularly and keep `config.yaml` current.
- **USDG basis** (`lighter-rh`): the hedge quotes in USDG. Part of any
  persistent premium is the stablecoin itself; your midline absorbs the
  level, but a USDG *move* is real PnL.
- **Funding**: two venues have independent funding rates. They are normalized,
  recorded, and alerted on, but carry still does not gate entries or alter
  thresholds. Position caps bound it — keep them modest.
- **Thin books**: Entropy depth can be tiny; `take_fraction` and notional
  caps keep clips small, but slippage on the hedge leg after a partial fill
  is real.
- **Market hours**: for equity perps (e.g. SNDK), off-hours oracle regimes
  differ per venue; consider wider bands or not trading them.
- **One-leg risk**: a leg can fail after the other filled. The bot normally
  hedges and reconciles automatically. An unreferenced Hyperliquid timeout/5xx
  deliberately stops for manual recovery, so active monitoring is required.

Use at your own risk. This is trading software operating with real money;
nothing here is investment advice. Before any live run, repeat
`--record-only`, verify the reference columns and `logs/engine.log`, and start
with tiny position caps. These checks do not make live trading risk-free.

## License

[MIT](LICENSE)
