#!/usr/bin/env python3
"""entropy-arb entry point.

    # collect minute data only — no strategy, no credentials needed
    python3 main.py --record-only --symbol SNDK --hedge lighter-rh

    # venue-native symbols may differ
    python3 main.py --record-only --symbol ANTH --hedge lighter-rh \
        --hedge-symbol ANTHROPIC

    # LIVE trading: real orders, real money (needs .env credentials)
    python3 main.py --symbol SNDK --hedge lighter-rh

--symbol and --hedge are required on every start: the markets you trade are
an explicit decision, not a config default. If the hedge venue uses another
name for the same asset, pass it with --hedge-symbol. Add --cn for a
Chinese-language dashboard. There is no paper mode. Collect data with
--record-only, set your thresholds with tools/analyze.py, then go live with
small position caps.

On a terminal the bot shows a live Rich dashboard (books, signal, positions,
PnL, last executions) and writes log lines to logging.file; use
--no-dashboard for plain console logs (nohup/systemd). Strategy lives in
config.yaml, credentials in .env — see the README (English) /
README.zh-CN.md (中文).
"""
import argparse
import asyncio
import logging
import os
import signal
import sys

from entropy_arb.config import (
    HEDGE_VENUES,
    ConfigError,
    load_config,
    validate_output_paths,
)
from entropy_arb.engine import Engine

DASHBOARD_STOP_TIMEOUT_SEC = 5.0
log = logging.getLogger("main")


def setup_logging(level: str, log_file: str = None,
                  extra_handler: logging.Handler = None) -> None:
    root = logging.getLogger()
    root.setLevel(getattr(logging, level, logging.INFO))
    fmt = logging.Formatter(
        "%(asctime)s.%(msecs)03d %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S")
    if log_file:
        d = os.path.dirname(log_file)
        if d:
            os.makedirs(d, exist_ok=True)
        h = logging.FileHandler(log_file)
    else:
        h = logging.StreamHandler()
    h.setFormatter(fmt)
    root.addHandler(h)
    if extra_handler is not None:
        root.addHandler(extra_handler)
    logging.getLogger("websockets").setLevel(logging.WARNING)


async def _run_with_dashboard(eng, dash) -> None:
    stopped_engine = False

    def dashboard_done(_task) -> None:
        nonlocal stopped_engine
        if not _task.cancelled():
            _task.exception()
        if not eng.stop.is_set():
            stopped_engine = True
            eng.request_stop()

    dash_task = asyncio.create_task(dash.run(), name="dashboard")
    dash_task.add_done_callback(dashboard_done)
    engine_error = None
    dashboard_error = None

    async def finish_dashboard():
        done, _ = await asyncio.wait(
            {dash_task}, timeout=DASHBOARD_STOP_TIMEOUT_SEC)
        if not done:
            dash_task.cancel()
            done, _ = await asyncio.wait(
                {dash_task}, timeout=DASHBOARD_STOP_TIMEOUT_SEC)
        if not done:
            return RuntimeError(
                "dashboard did not stop within the cleanup deadline")
        if dash_task.cancelled():
            return asyncio.CancelledError()
        return dash_task.exception()

    try:
        await eng.run()
    except BaseException as exc:
        engine_error = exc
    finally:
        eng.request_stop()
        finish_task = asyncio.create_task(
            finish_dashboard(), name="dashboard-cleanup")
        try:
            dashboard_error = await asyncio.shield(finish_task)
        except asyncio.CancelledError as exc:
            dash_task.cancel()
            dashboard_error = await finish_task
            if engine_error is None:
                engine_error = exc

    if (engine_error is not None and dashboard_error is not None
            and not isinstance(dashboard_error, asyncio.CancelledError)):
        log.error(
            "dashboard also failed while preserving the engine error",
            exc_info=(type(dashboard_error), dashboard_error,
                      dashboard_error.__traceback__))
    if engine_error is not None:
        raise engine_error
    if dashboard_error is not None:
        if isinstance(dashboard_error, asyncio.CancelledError):
            if stopped_engine:
                raise RuntimeError(
                    "dashboard was cancelled unexpectedly") from dashboard_error
        else:
            raise dashboard_error
    if stopped_engine:
        raise RuntimeError("dashboard exited unexpectedly")


def _run_application(awaitable):
    """Run the application without letting a broken UI task block exit.

    Engine cleanup completes inside ``amain``.  Any task still alive here is
    therefore an abnormal auxiliary task (not an order operation), and gets a
    final bounded cancellation window before the loop is closed.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(awaitable)
    finally:
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        if pending:
            done, _ = loop.run_until_complete(asyncio.wait(
                pending, timeout=DASHBOARD_STOP_TIMEOUT_SEC))
            for task in done:
                if task.cancelled():
                    continue
                error = task.exception()
                if error is not None:
                    log.error(
                        "task %s failed during final loop cleanup",
                        task.get_name(),
                        exc_info=(type(error), error, error.__traceback__))
        abandoned = asyncio.all_tasks(loop)
        for task in abandoned:
            log.critical(
                "abandoning non-cooperative task %s after cleanup deadline",
                task.get_name())
            # asyncio cannot forcibly terminate a coroutine that suppresses
            # every cancellation.  The loop is about to close deliberately.
            task._log_destroy_pending = False
        loop.close()
        asyncio.set_event_loop(None)


async def amain(cfg, record_only: bool, use_dashboard: bool, force_tty: bool,
                 log_buffer, lang: str) -> None:
    eng = Engine(cfg, record_only=record_only)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, eng.request_stop)
    if not use_dashboard:
        await eng.run()
        return
    from entropy_arb.dashboard import Dashboard
    dash = Dashboard(eng, log_buffer, cfg.log_file, force_terminal=force_tty,
                     lang=lang)
    await _run_with_dashboard(eng, dash)


def main() -> None:
    p = argparse.ArgumentParser(
        description="Two-venue LIVE arbitrage: Entropy vs Lighter mainnet / "
                    "Lighter Robinhood / trade.xyz. Without --record-only, "
                    "real orders are sent.")
    p.add_argument("--symbol", required=True,
                   help="Entropy symbol, e.g. SNDK or ANTH / "
                        "Entropy 交易品种")
    p.add_argument("--hedge", required=True, choices=HEDGE_VENUES,
                   metavar="VENUE",
                   help=f"hedge venue, one of: {', '.join(HEDGE_VENUES)} / "
                        f"对冲腿，三选一")
    p.add_argument(
        "--hedge-symbol",
        help="hedge venue symbol when it differs from --symbol; defaults "
             "to --symbol / 对冲交易所品种；默认与 --symbol 相同")
    p.add_argument("--config", default="config.yaml",
                   help="strategy config (default: config.yaml)")
    p.add_argument("--env-file", default=".env",
                   help="credentials file (default: .env)")
    p.add_argument("--record-only", action="store_true",
                   help="only collect minute data, run no strategy, send no "
                        "orders (needs no credentials)")
    p.add_argument("--cn", action="store_true",
                   help="display the dashboard in Chinese / 仪表盘使用中文")
    disp = p.add_mutually_exclusive_group()
    disp.add_argument("--dashboard", action="store_true",
                      help="force the Rich dashboard even without a tty")
    disp.add_argument("--no-dashboard", action="store_true",
                      help="plain console logs instead of the dashboard")
    args = p.parse_args()

    try:
        cfg = load_config(args.config, args.env_file,
                          symbol=args.symbol, hedge_venue=args.hedge,
                          hedge_symbol=args.hedge_symbol,
                          record_only=args.record_only,
                          validate_outputs=False)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        sys.exit(2)

    use_dashboard = (cfg.dashboard or args.dashboard) and not args.no_dashboard
    force_tty = args.dashboard
    if use_dashboard and not (sys.stdout.isatty() or force_tty):
        use_dashboard = False

    log_buffer = None
    if use_dashboard:
        try:
            from entropy_arb.dashboard import BufferLogHandler
        except ImportError:
            print("`rich` is not installed — falling back to plain logs "
                  "(pip install -r requirements.txt)", file=sys.stderr)
            use_dashboard = False
    if use_dashboard:
        log_buffer = BufferLogHandler()
    try:
        validate_output_paths(
            cfg, record_only=args.record_only,
            log_file_active=use_dashboard)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        sys.exit(2)
    if use_dashboard:
        setup_logging(cfg.log_level, log_file=cfg.log_file,
                      extra_handler=log_buffer)
    else:
        setup_logging(cfg.log_level)

    try:
        _run_application(amain(
            cfg, record_only=args.record_only,
            use_dashboard=use_dashboard, force_tty=force_tty,
            log_buffer=log_buffer, lang="zh" if args.cn else "en"))
    except RuntimeError as e:
        # startup failures (missing credentials, market not found, venue
        # unreachable) — a clean message, not a traceback
        print(f"startup error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
