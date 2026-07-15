"""Batch decensor worker (optional).

Processes every scene carrying the trigger tag, then exits (or polls when
POLL_INTERVAL > 0). This is the tag-driven batch mode; the default container
mode is the on-demand HTTP server (see server.py), which is what the Stash UI
button uses.

Run this mode with RUN_MODE=worker.
"""

import os
import sys
import time
import logging

import core

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)


def main():
    cfg = core.config_from_env()
    try:
        core.validate(cfg)
    except ValueError:
        raise SystemExit(2)

    try:
        stash = core.stash_from_env()
    except ValueError as exc:
        logging.error(str(exc))
        raise SystemExit(2)

    interval = core._int(os.environ.get("POLL_INTERVAL", "0"))
    while True:
        try:
            core.run(stash, cfg, mode="tagged")
        except Exception as exc:  # noqa: BLE001 - keep the daemon alive across errors
            logging.error(f"Run failed: {exc}")
        if interval <= 0:
            break
        logging.info(f"Sleeping {interval}s until next poll…")
        time.sleep(interval)


if __name__ == "__main__":
    main()
