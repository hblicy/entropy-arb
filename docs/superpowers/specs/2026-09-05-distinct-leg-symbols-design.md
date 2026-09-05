# Distinct Leg Symbols Design

## Goal

Allow one arbitrage pair to use different exchange-native market symbols, such
as Entropy `ANTH` and Robinhood Lighter `ANTHROPIC`, while preserving every
existing command that uses the same symbol on both venues.

## Chosen interface

`--symbol` remains required and is the Entropy symbol as well as the canonical
strategy symbol. A new optional `--hedge-symbol` supplies the hedge venue's
native symbol. When it is omitted, it defaults to `--symbol`.

Existing command:

```bash
python main.py --record-only --symbol SNDK --hedge lighter-rh --no-dashboard
```

New asymmetric-symbol command:

```bash
python main.py --record-only --symbol ANTH --hedge lighter-rh \
  --hedge-symbol ANTHROPIC --no-dashboard
```

The alternative of hard-coding an `ANTH -> ANTHROPIC` alias is rejected
because each venue owns its market names and future listings would require
code releases. Replacing `--symbol` with two newly required flags is also
rejected because it would break every existing launch command unnecessarily.

## Configuration and venue construction

`load_config` accepts `hedge_symbol: Optional[str] = None`, strips and validates
the effective hedge symbol with the same identity rules used by `--symbol`, and
defaults it to the canonical symbol. `Config.symbol` stays canonical for
backward compatibility. `Config.entropy.symbol` receives `--symbol`, while
`Config.hedge.symbol` receives the effective hedge symbol. Venue adapters need
no special-case alias logic because they already load the symbol from their
own `VenueConf`.

An explicitly blank `--hedge-symbol` is invalid. Omitting the flag is the only
way to request the default. This distinguishes a safe backward-compatible
default from an accidental empty shell variable.

## Recorder identity and compatibility

New minute and signal CSV schemas identify both native markets with these four
fields:

- `entropy_symbol`
- `entropy_dex`
- `hedge_symbol`
- `hedge_venue`

The old ambiguous `symbol` identity field is replaced in newly written files.
The existing recorder header check will rotate a non-empty old-schema output
to the next `.old` path before writing the new header, so rows with different
schemas are never appended together.

`tools/analyze.py` continues to read historical CSVs containing the legacy
identity tuple `symbol, entropy_dex, hedge_venue`. It also reads the new
four-field identity. A file with a partial identity schema, empty identity
values, or more than one complete market identity is rejected. Deduplication
keys include both native symbols so two mapped pairs cannot be merged.

Signal event rows receive the same four identity fields. Trading behavior,
threshold calculations, prices, quantities, and signal lifecycle semantics do
not change.

## Error handling

Both CLI symbols are normalized once during configuration loading. Invalid
characters or an explicitly empty hedge symbol produce a `ConfigError` before
network connections or output files are opened. Existing adapter errors remain
responsible for reporting an exchange market that does not exist.

## Tests and documentation

Tests cover:

1. omission of `--hedge-symbol` preserving the same-symbol behavior;
2. `ANTH`/`ANTHROPIC` reaching the correct `VenueConf` objects;
3. rejection of an explicitly blank or invalid hedge symbol;
4. both recorder schemas writing the complete new identity;
5. analyzer acceptance of new and legacy CSVs and rejection of mixed or
   partial identities;
6. engine wiring of both symbols into minute and signal recorders;
7. command examples and CSV field documentation in `README.md`.

Targeted tests run first, followed by the complete test suite. No order sizing,
fee, threshold, credential, venue-adapter, or execution-policy behavior is in
scope.
