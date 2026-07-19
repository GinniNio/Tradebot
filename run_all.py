"""
run_all.py — Launch dashboard + bot together in one command.

Usage:
  python run_all.py

Opens:
  http://localhost:3001  ← dashboard
  Bot runs in background (paper trading by default)
"""

import asyncio
import os
import sys
import logging
import traceback
from datetime import datetime
from pathlib import Path

# When launched via pythonw.exe (no console), sys.stdout/stderr are None.
# Previously these were redirected to os.devnull, which silently swallowed
# every crash and made it impossible to debug why the bot died. Now we
# redirect to crash.log so any unhandled exception or stray print() lands
# somewhere we can read after the fact. Use line buffering so writes are
# flushed promptly even on abrupt termination.
_PROJECT_DIR = Path(__file__).parent
_CRASH_LOG_PATH = _PROJECT_DIR / "crash.log"

if sys.stdout is None or sys.stderr is None:
    _crash_fh = open(_CRASH_LOG_PATH, "a", encoding="utf-8", buffering=1)
    _crash_fh.write(f"\n--- pythonw session started {datetime.now().isoformat()} ---\n")
    if sys.stdout is None:
        sys.stdout = _crash_fh
    if sys.stderr is None:
        sys.stderr = _crash_fh

# Force UTF-8 on Windows console so non-ASCII log chars don't crash the stream
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

sys.path.insert(0, str(_PROJECT_DIR))


async def main():
    fmt = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
    # Route all logging to the rotating file only. Do NOT call basicConfig
    # without explicit handlers — the default StreamHandler would write every
    # log line to sys.stderr, which under pythonw.exe points to crash.log,
    # causing crash.log to balloon to 500k+ lines of normal operation noise
    # and making it useless as a crash/exception capture.
    # crash.log is intentionally kept sparse: startup markers + unhandled
    # exceptions only (written directly by the except BaseException block).
    from logging.handlers import RotatingFileHandler
    fh = RotatingFileHandler("tradebot.log", maxBytes=10_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(logging.Formatter(fmt))
    logging.basicConfig(level=logging.INFO, format=fmt, handlers=[fh])

    from database import init_db
    init_db()

    from main import Tradebot
    import uvicorn
    from server import app

    bot = Tradebot()
    config = uvicorn.Config(app, host="0.0.0.0", port=3001, log_level="warning")
    server = uvicorn.Server(config)

    print()
    print("  ╔══════════════════════════════════════╗")
    print("  ║         TRADEBOT STARTING            ║")
    print("  ║  Dashboard → http://localhost:3001   ║")
    print("  ║  Mode      → PAPER TRADING           ║")
    print("  ║  Ctrl+C    → graceful shutdown       ║")
    print("  ╚══════════════════════════════════════╝")
    print()

    server_task = asyncio.create_task(server.serve(), name="uvicorn")
    bot_task    = asyncio.create_task(bot.run(),      name="tradebot")

    try:
        # Wait until either task finishes (typically Ctrl+C triggers cancellation)
        done, pending = await asyncio.wait(
            {server_task, bot_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in done:
            exc = t.exception()
            if exc:
                logging.error("%s exited with: %s", t.get_name(), exc)
    finally:
        # Tell both to stop
        bot.shutdown()
        server.should_exit = True
        for t in (server_task, bot_task):
            if not t.done():
                t.cancel()
        # Wait for them to actually stop, swallowing CancelledError
        await asyncio.gather(server_task, bot_task, return_exceptions=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutdown complete.")
    except SystemExit:
        # uvicorn or asyncio may raise this on clean shutdown — don't treat
        # it as a crash.
        raise
    except BaseException as e:
        # Any other unhandled exception is a crash. Log it loudly to BOTH
        # the rotating tradebot.log (if logging is up) AND crash.log
        # (which exists even before logging.basicConfig has run).
        crash_msg = (
            f"\n=== UNHANDLED CRASH {datetime.now().isoformat()} ===\n"
            f"{type(e).__name__}: {e}\n"
            f"{traceback.format_exc()}\n"
            f"=== end crash ===\n"
        )
        try:
            logging.getLogger().critical("UNHANDLED CRASH: %s\n%s", e, traceback.format_exc())
        except Exception:
            pass
        try:
            with open(_CRASH_LOG_PATH, "a", encoding="utf-8") as _f:
                _f.write(crash_msg)
        except Exception:
            pass
        # Also write to stderr — useful when run from a console for testing.
        try:
            sys.stderr.write(crash_msg)
            sys.stderr.flush()
        except Exception:
            pass
        raise
