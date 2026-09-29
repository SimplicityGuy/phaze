"""Standalone real-process harness for the SIGHUP reload-trigger test (phaze-mvq8z.5).

Run as its OWN OS process (never imported) -- ``test_runtime_config_sighup_process.py`` launches
it as a subprocess, exactly the way ``pytest`` uses ``phaze.analysis_child`` as a real-subprocess
target (``tests/analyze/core/test_analysis_child.py``). It builds a ``RuntimeConfigStore`` and
installs the SAME production ``install_sighup_handler`` the api lifespan, control worker startup,
and agent worker startup all call -- so a signal sent to THIS process exercises the real trigger
wiring, not a stand-in. JSON logging makes the reload line grep-able from the parent's captured
stdout.

Protocol (stdout, one line each, unbuffered):
    HARNESS_READY installed=<bool>   -- printed once, after the handler is installed
    (then whatever configure_logging's JSON renderer emits for each SIGHUP-triggered reload)

Exits on SIGTERM (a second, independent handler -- this harness's own idea of "please stop",
distinct from the SIGHUP trigger under test).
"""

from __future__ import annotations

import asyncio
import signal

from phaze.config import ControlSettings
from phaze.logging_config import configure_logging
from phaze.runtime_config import RuntimeConfigStore
from phaze.runtime_config_triggers import install_sighup_handler


async def _main() -> None:
    configure_logging(json_logs=True, level="INFO")
    store = RuntimeConfigStore(ControlSettings(), runtime_toml=None, env={}, physical_cores=lambda: 64)
    loop = asyncio.get_running_loop()
    installed = install_sighup_handler(loop, store)
    print(f"HARNESS_READY installed={installed}", flush=True)

    stop = asyncio.Event()
    loop.add_signal_handler(signal.SIGTERM, stop.set)
    await stop.wait()


if __name__ == "__main__":
    asyncio.run(_main())
