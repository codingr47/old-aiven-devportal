"""Timestamped progress on stderr, without requests, credentials, or source text."""

import contextlib
import sys
import threading
import time
from datetime import datetime, timezone


def trace(stage, message):
    stamp = datetime.now(timezone.utc).isoformat(timespec='seconds')
    print(f'[{stamp}] [{stage}] {message}', file=sys.stderr, flush=True)


@contextlib.contextmanager
def traced(stage, message, interval=15):
    """Keep blocking local work and API waits visible; never touch browser/state off-thread."""
    started = time.monotonic()
    stopped = threading.Event()
    trace(stage, message)

    def heartbeat():
        while not stopped.wait(interval):
            trace(stage, f'Still running: {message} ({time.monotonic() - started:.0f}s elapsed)')

    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    try:
        yield
    except BaseException as exc:
        trace(stage, f'Failed/interrupted after {time.monotonic() - started:.1f}s ({type(exc).__name__})')
        raise
    else:
        trace(stage, f'Finished in {time.monotonic() - started:.1f}s: {message}')
    finally:
        stopped.set()
        thread.join()
