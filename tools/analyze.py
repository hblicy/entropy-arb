#!/usr/bin/env python3
"""Analyze recorded minute data and suggest config.yaml thresholds.

Reads the CSV written by the built-in recorder (logs/minutes.csv by default)
and prints:

  * the premium distribution (midline candidates),
  * how often each candidate upper/lower band would have fired,
  * a ready-to-paste `thresholds:` snippet.

分析机器人自动采集的分钟级盘口数据，输出溢价分布、各档阈值的触发频率，
以及可直接粘贴进 config.yaml 的 thresholds 建议值。

Usage:
    python3 tools/analyze.py --entropy-fee-bps 0.9 --hedge-fee-bps 0.0
    python3 tools/analyze.py --csv path.csv --hours 24 --min-samples 10
"""
from __future__ import annotations

import argparse
import csv
import gzip
import math
import sys
import time
from collections import Counter

CANDIDATES = [1.0, 1.5, 2.0, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0, 15.0, 20.0]
NEW_IDENTITY = (
    "entropy_symbol", "entropy_dex", "hedge_symbol", "hedge_venue")
LEGACY_IDENTITY = ("symbol", "entropy_dex", "hedge_venue")
STRATEGY_REQUIRED_FIELDS = {
    "ts_ms", "mode", "event", "intent", "reason", "campaign_id",
    *NEW_IDENTITY, "direction", "hold_seconds", "realized_pnl_usd",
}


def open_csv_text(path: str):
    if path.lower().endswith(".gz"):
        return gzip.open(path, "rt", newline="", encoding="utf-8")
    return open(path, newline="", encoding="utf-8")


def pctl(sorted_vals: list, q: float) -> float:
    """Linear-interpolated percentile of a pre-sorted list, q in [0, 100]."""
    if not sorted_vals:
        return float("nan")
    k = (len(sorted_vals) - 1) * q / 100.0
    lo = math.floor(k)
    hi = math.ceil(k)
    if lo == hi:
        return sorted_vals[int(k)]
    return sorted_vals[lo] * (hi - k) + sorted_vals[hi] * (k - lo)


def describe(values: list[float]) -> tuple[float, float, float, float, float]:
    ordered = sorted(values)
    mean = sum(ordered) / len(ordered)
    std = math.sqrt(
        sum((value - mean) ** 2 for value in ordered) / len(ordered))
    return (mean, std, pctl(ordered, 50), pctl(ordered, 5),
            pctl(ordered, 95))


def _optional_finite_float(row: dict, field: str):
    raw = row.get(field)
    if raw is None or not raw.strip():
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def net_edge_bps(gross_edge_bps: float, *, buy_fee_bps: float,
                 sell_fee_bps: float) -> float:
    """Convert a gross sell/buy price ratio into executable net edge."""
    gross_ratio = 1.0 + gross_edge_bps / 1e4
    net_ratio = (gross_ratio * (1.0 - sell_fee_bps / 1e4)
                 / (1.0 + buy_fee_bps / 1e4))
    return (net_ratio - 1.0) * 1e4


def fee_adjusted_rooms(rows: list, *, midline: float,
                       entropy_fee_bps: float,
                       hedge_fee_bps: float) -> tuple[list, list]:
    sell_room = sorted((
        net_edge_bps(
            row["sell_max"], buy_fee_bps=hedge_fee_bps,
            sell_fee_bps=entropy_fee_bps) - midline
        for row in rows
    ), reverse=True)
    buy_room = sorted((
        net_edge_bps(
            row["buy_max"], buy_fee_bps=entropy_fee_bps,
            sell_fee_bps=hedge_fee_bps) + midline
        for row in rows
    ), reverse=True)
    return sell_room, buy_room


def _validate_market_set(markets: set) -> tuple[str, str, str, str]:
    if len(markets) > 1:
        raise ValueError(
            "multiple markets found in one minute CSV; use a separate file "
            "for each symbol and hedge venue")
    return next(iter(markets), ("", "", "", ""))


def _row_identity(row: dict) -> tuple[str, str, str, str]:
    if "entropy_symbol" in row or "hedge_symbol" in row:
        return tuple(row.get(field, "") for field in NEW_IDENTITY)
    symbol = row.get("symbol", "")
    return (symbol, row.get("entropy_dex", ""), symbol,
            row.get("hedge_venue", ""))


def validate_single_market(rows: list) -> tuple[str, str, str, str]:
    return _validate_market_set({_row_identity(row) for row in rows})


def load_rows(path: str, hours: float, min_samples: int) -> list:
    cutoff = time.time() - hours * 3600 if hours > 0 else 0.0
    merged = {}
    markets = set()
    with open_csv_text(path) as fh:
        reader = csv.DictReader(fh)
        fieldnames = set(reader.fieldnames or [])
        has_new_symbol = bool(
            fieldnames.intersection({"entropy_symbol", "hedge_symbol"}))
        has_legacy_symbol = "symbol" in fieldnames
        if has_new_symbol and has_legacy_symbol:
            raise ValueError(
                "market identity columns must be all present or all absent")
        if has_new_symbol:
            if not set(NEW_IDENTITY).issubset(fieldnames):
                raise ValueError(
                    "market identity columns must be all present or all absent")
            identity_fields = NEW_IDENTITY
        elif has_legacy_symbol:
            if not set(LEGACY_IDENTITY).issubset(fieldnames):
                raise ValueError(
                    "market identity columns must be all present or all absent")
            identity_fields = LEGACY_IDENTITY
        else:
            shared_fields = {"entropy_dex", "hedge_venue"}
            if fieldnames.intersection(shared_fields):
                raise ValueError(
                    "market identity columns must be all present or all absent")
            identity_fields = ()
        for r in reader:
            if identity_fields == NEW_IDENTITY:
                identity = tuple(
                    (r.get(field) or "").strip()
                    for field in NEW_IDENTITY)
            elif identity_fields == LEGACY_IDENTITY:
                legacy = tuple(
                    (r.get(field) or "").strip()
                    for field in LEGACY_IDENTITY)
                identity = (legacy[0], legacy[1], legacy[0], legacy[2])
            else:
                identity = ("", "", "", "")
            if identity_fields and not all(identity):
                raise ValueError(
                    "market identity values must not be empty")
            markets.add(identity)
            _validate_market_set(markets)
            try:
                ts = float(r["minute_ts"])
                samples = int(r["samples"])
                row = {
                    "ts": ts,
                    "entropy_symbol": identity[0],
                    "entropy_dex": identity[1],
                    "hedge_symbol": identity[2],
                    "hedge_venue": identity[3],
                    "prem": float(r["premium_close_bps"]),
                    "prem_mean": float(r["premium_mean_bps"]),
                    "sell_max": float(r["sell_edge_max_bps"]),
                    "buy_max": float(r["buy_edge_max_bps"]),
                    "samples": samples,
                    "reference_basis": _optional_finite_float(
                        r, "reference_basis_close_bps"),
                    "residual": _optional_finite_float(
                        r, "residual_close_bps"),
                    "funding_diff": _optional_finite_float(
                        r, "funding_diff_close_bps_per_hour"),
                }
            except (KeyError, ValueError):
                continue
            metrics = (
                row["ts"], row["prem"], row["prem_mean"],
                row["sell_max"], row["buy_max"],
            )
            if samples <= 0 or not all(math.isfinite(v) for v in metrics):
                continue
            if ts < cutoff:
                continue
            key = (row["entropy_symbol"], row["entropy_dex"],
                   row["hedge_symbol"], row["hedge_venue"], ts)
            previous = merged.get(key)
            if previous is None:
                merged[key] = row
                continue
            total_samples = previous["samples"] + samples
            if total_samples > 0:
                previous["prem_mean"] = (
                    previous["prem_mean"] * previous["samples"]
                    + row["prem_mean"] * samples) / total_samples
            previous["samples"] = total_samples
            previous["prem"] = row["prem"]
            previous["reference_basis"] = row["reference_basis"]
            previous["residual"] = row["residual"]
            previous["funding_diff"] = row["funding_diff"]
            previous["sell_max"] = max(
                previous["sell_max"], row["sell_max"])
            previous["buy_max"] = max(
                previous["buy_max"], row["buy_max"])
    return sorted(
        (row for row in merged.values()
         if row["samples"] >= min_samples),
        key=lambda row: row["ts"])


def load_strategy_events(path: str) -> list[dict]:
    """Load one pair's low-frequency strategy journal."""
    rows = []
    markets = set()
    with open_csv_text(path) as handle:
        reader = csv.DictReader(handle)
        fields = set(reader.fieldnames or [])
        missing = STRATEGY_REQUIRED_FIELDS - fields
        if missing:
            raise ValueError(
                "strategy event CSV missing required columns: "
                + ", ".join(sorted(missing)))
        for source in reader:
            identity = tuple(
                (source.get(field) or "").strip()
                for field in NEW_IDENTITY)
            if not all(identity):
                raise ValueError(
                    "strategy event market identity values must not be empty")
            markets.add(identity)
            _validate_market_set(markets)
            try:
                ts_ms = float(source["ts_ms"])
            except (TypeError, ValueError):
                raise ValueError(
                    "strategy event timestamp must be finite") from None
            if not math.isfinite(ts_ms) or ts_ms < 0:
                raise ValueError("strategy event timestamp must be finite")
            parsed = dict(source)
            parsed["ts_ms"] = ts_ms
            for field in (
                    "hold_seconds", "realized_pnl_usd",
                    "projected_net_bps", "projected_net_usd",
                    "estimated_campaign_pnl_usd"):
                parsed[field] = _optional_finite_float(source, field)
            rows.append(parsed)
    return sorted(rows, key=lambda row: row["ts_ms"])


def summarize_strategy_events(rows: list[dict]) -> dict:
    opened = set()
    completed = set()
    holds = []
    pnl_values = []
    directions = Counter()
    reasons = Counter()
    forced_closes = 0
    model_snapshots = 0
    ready_snapshots = 0
    unstable_snapshots = 0

    for row in rows:
        intent = row.get("intent", "")
        event = row.get("event", "")
        campaign_id = row.get("campaign_id", "")
        if campaign_id and event != "campaign_closed":
            opened.add(campaign_id)
        if intent == "SKIP" and row.get("reason"):
            reasons[row["reason"]] += 1
        if intent == "OPEN" and campaign_id:
            if row.get("direction"):
                directions[row["direction"]] += 1
        if event == "model_snapshot":
            model_snapshots += 1
            status = row.get("model_status", "")
            ready_snapshots += status == "READY"
            unstable_snapshots += status == "REGIME_UNSTABLE"
        if event != "campaign_closed" or not campaign_id:
            continue
        completed.add(campaign_id)
        forced_closes += intent == "FORCED_CLOSE"
        if row.get("hold_seconds") is not None:
            holds.append(row["hold_seconds"])
        if row.get("realized_pnl_usd") is not None:
            pnl_values.append(row["realized_pnl_usd"])

    return {
        "events": len(rows),
        "completed_campaigns": len(completed),
        "still_open": len(opened - completed),
        "within_1h": sum(value <= 3600 for value in holds),
        "within_6h": sum(value <= 21600 for value in holds),
        "forced_closes": forced_closes,
        "realized_pnl_usd": sum(pnl_values),
        "realized_pnl_samples": len(pnl_values),
        "directions": directions,
        "reasons": reasons,
        "model_snapshots": model_snapshots,
        "model_ready_snapshots": ready_snapshots,
        "model_unstable_snapshots": unstable_snapshots,
    }


def print_strategy_summary(path: str, rows: list[dict]) -> None:
    summary = summarize_strategy_events(rows)
    print(f"\n=== {path}: dynamic strategy events ===")
    print(f"  completed campaigns: {summary['completed_campaigns']}")
    print(f"  within 1h: {summary['within_1h']}")
    print(f"  within 6h: {summary['within_6h']}")
    print(f"  still open: {summary['still_open']}")
    print(f"  forced closes: {summary['forced_closes']}")
    if summary["realized_pnl_samples"]:
        print("  realized/shadow PnL: "
              f"{summary['realized_pnl_usd']:+.4f} USD")
    if summary["model_snapshots"]:
        total = summary["model_snapshots"]
        ready = summary["model_ready_snapshots"]
        unstable = summary["model_unstable_snapshots"]
        print(f"  model ready: {ready}/{total} ({ready / total:.1%})")
        print(f"  regime unstable: {unstable}/{total} "
              f"({unstable / total:.1%})")
    if summary["directions"]:
        values = ", ".join(
            f"{key}={value}"
            for key, value in sorted(summary["directions"].items()))
        print(f"  opened directions: {values}")
    if summary["reasons"]:
        values = ", ".join(
            f"{key}={value}"
            for key, value in summary["reasons"].most_common())
        print(f"  skip reasons: {values}")


def main() -> None:
    p = argparse.ArgumentParser(description="suggest thresholds from recorded "
                                            "minute data")
    p.add_argument("--csv", default="logs/minutes.csv")
    p.add_argument("--hours", type=float, default=0.0,
                   help="only use the last N hours (0 = all data)")
    p.add_argument("--min-samples", type=int, default=10,
                   help="skip minutes with fewer fresh samples than this")
    p.add_argument("--entropy-fee-bps", type=float,
                   help="Entropy taker fee in bps; used with "
                        "--hedge-fee-bps for exact execution math")
    p.add_argument("--hedge-fee-bps", type=float,
                   help="hedge-venue taker fee in bps; used with "
                        "--entropy-fee-bps for exact execution math")
    p.add_argument("--fees-bps", type=float,
                   help="legacy approximate sum of both taker fees; cannot be "
                        "combined with the exact per-venue options")
    p.add_argument("--strategy-csv",
                   help="optional strategy-events.csv to summarize")
    args = p.parse_args()

    if ((args.entropy_fee_bps is None)
            != (args.hedge_fee_bps is None)):
        p.error("--entropy-fee-bps and --hedge-fee-bps must be supplied "
                "together")
    exact_fees = (args.entropy_fee_bps is not None)
    if args.fees_bps is not None and exact_fees:
        p.error("--fees-bps cannot be combined with per-venue fee options")
    supplied_fees = [value for value in (
        args.fees_bps, args.entropy_fee_bps, args.hedge_fee_bps)
        if value is not None]
    if any(not math.isfinite(value) or value < 0 for value in supplied_fees):
        p.error("fee values must be finite and >= 0")

    try:
        rows = load_rows(args.csv, args.hours, args.min_samples)
    except FileNotFoundError:
        print(f"{args.csv} not found — run the bot (even --record-only) to "
              f"collect data first / 未找到数据文件，请先运行机器人采集数据",
              file=sys.stderr)
        sys.exit(1)
    except ValueError as exc:
        print(f"{exc} / 一个分钟文件中包含多个市场，请按品种和对冲交易所"
              f"分别采集", file=sys.stderr)
        sys.exit(2)
    if len(rows) < 30:
        print(f"only {len(rows)} usable minute(s) in {args.csv} — collect at "
              f"least a few hours before trusting the numbers / 数据太少，"
              f"建议至少采集数小时", file=sys.stderr)
        if not rows:
            sys.exit(1)
    span_h = (rows[-1]["ts"] - rows[0]["ts"]) / 3600.0 + 1 / 60.0
    prem = sorted(r["prem"] for r in rows)
    mean = sum(prem) / len(prem)
    var = sum((x - mean) ** 2 for x in prem) / len(prem)
    median = pctl(prem, 50)

    print(f"\n=== {args.csv}: {len(rows)} minutes over {span_h:.1f}h ===\n")
    print("premium of Entropy over hedge, minute close (bps) / "
          "Entropy 相对对冲腿的溢价:")
    print(f"  mean {mean:+.2f}   std {math.sqrt(var):.2f}   "
          f"median {median:+.2f}")
    print(f"  p5 {pctl(prem, 5):+.2f}   p25 {pctl(prem, 25):+.2f}   "
          f"p75 {pctl(prem, 75):+.2f}   p95 {pctl(prem, 95):+.2f}")

    for field, label in (
        ("reference_basis", "reference basis, minute close (bps)"),
        ("residual", "signed residual, minute close (bps)"),
        ("funding_diff", "funding difference, minute close (bps/hour)"),
    ):
        values = [row[field] for row in rows if row[field] is not None]
        if not values:
            continue
        stat_mean, stat_std, stat_median, stat_p5, stat_p95 = describe(values)
        print(f"\n{label}:")
        print(f"  mean {stat_mean:+.2f}   std {stat_std:.2f}   "
              f"median {stat_median:+.2f}")
        print(f"  p5 {stat_p5:+.2f}   p95 {stat_p95:+.2f}   "
              f"samples {len(values)}")

    midline = round(median, 1) or 0.0   # normalize -0.0
    # room beyond the midline that was actually executable each minute, net
    # of taker fees (config thresholds are net-of-fee: the engine adds fees
    # on top, and recorded edges are pre-fee)
    if exact_fees:
        entropy_fee = args.entropy_fee_bps or 0.0
        hedge_fee = args.hedge_fee_bps or 0.0
        sell_room, buy_room = fee_adjusted_rooms(
            rows, midline=midline, entropy_fee_bps=entropy_fee,
            hedge_fee_bps=hedge_fee)
        fee_summary_en = (
            f"exact taker fees entropy={entropy_fee:.3f} bps and "
            f"hedge={hedge_fee:.3f} bps")
        fee_summary_zh = (
            f"精确吃单费 Entropy={entropy_fee:.3f} bps、"
            f"对冲腿={hedge_fee:.3f} bps")
    else:
        fees = args.fees_bps or 0.0
        sell_room = sorted(
            (row["sell_max"] - midline - fees for row in rows),
            reverse=True)
        buy_room = sorted(
            (row["buy_max"] + midline - fees for row in rows),
            reverse=True)
        fee_summary_en = f"legacy approximate combined fees={fees:.3f} bps"
        fee_summary_zh = f"兼容近似两腿合计手续费={fees:.3f} bps"

    print(f"\nwith midline_bps = {midline:+.1f} (median) and "
          f"{fee_summary_en}, minutes each band would have fired / "
          f"{fee_summary_zh}，"
          f"各档净阈值触发的分钟数:")
    print(f"  {'band bps':>9} | {'SELL entropy':>17} | {'BUY entropy':>17}")
    print(f"  {'':>9} | {'minutes':>8} {'per day':>8} | "
          f"{'minutes':>8} {'per day':>8}")
    per_day = 24.0 / span_h if span_h > 0 else 0.0
    for t in CANDIDATES:
        s_hits = sum(1 for x in sell_room if x >= t)
        b_hits = sum(1 for x in buy_room if x >= t)
        print(f"  {t:>9.1f} | {s_hits:>8} {s_hits * per_day:>8.1f} | "
              f"{b_hits:>8} {b_hits * per_day:>8.1f}")

    # default suggestion: the band that fired in ~10% of minutes (p90 of the
    # fee-adjusted executable room), floored at 1 bps — tune from the table
    sug_upper = max(round(pctl(sorted(sell_room), 90) * 2) / 2, 1.0)
    sug_lower = max(round(pctl(sorted(buy_room), 90) * 2) / 2, 1.0)
    print(f"""
suggested starting point (fires ~10% of minutes, already net of the
configured {fee_summary_en}; after paying fees on entry and exit, a full
round trip nets >= upper+lower bps) /
建议起点（约 10% 的分钟触发；已使用{fee_summary_zh}，开仓和平仓分别扣费后，
一次完整往返净赚 >= upper+lower bps）:

thresholds:
  midline_bps: {midline}
  upper_bps: {sug_upper}
  lower_bps: {sug_lower}

Re-run with --hours to focus on recent regimes; premiums drift, so refresh
these numbers regularly. / 溢价中枢会漂移，请定期重新分析并更新配置。
""")
    if args.strategy_csv:
        try:
            strategy_rows = load_strategy_events(args.strategy_csv)
        except (FileNotFoundError, ValueError) as exc:
            print(f"cannot analyze strategy events: {exc}", file=sys.stderr)
            sys.exit(2)
        print_strategy_summary(args.strategy_csv, strategy_rows)


if __name__ == "__main__":
    main()
