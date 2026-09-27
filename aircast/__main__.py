"""Entry point: `aircast` / `python -m aircast`."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal

from aircast import __version__
from aircast.config import load_config


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="aircast",
        description="Expose AirPlay speakers as Chromecast and DLNA/UPnP renderers.",
    )
    parser.add_argument("-c", "--config", help="YAML config file (default: $AIRCAST_CONFIG "
                        "or <state_dir>/config.yaml if present)")
    parser.add_argument("--scan", action="store_true",
                        help="list AirPlay receivers on the network and exit")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    parser.add_argument("--version", action="version", version=f"aircast {__version__}")
    args = parser.parse_args()

    config = load_config(args.config)
    level = logging.DEBUG if args.verbose else getattr(logging, config.log_level.upper(), 20)
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    if not args.verbose:
        # pyatv and zeroconf are chatty at INFO.
        logging.getLogger("pyatv").setLevel(max(level, logging.WARNING))
        logging.getLogger("zeroconf").setLevel(max(level, logging.WARNING))

    if args.scan:
        asyncio.run(_scan(config.scan_timeout))
        return
    asyncio.run(_run(config))


async def _scan(timeout: int) -> None:
    from aircast.airplay.discovery import scan

    targets = await scan(timeout=timeout)
    if not targets:
        print("No AirPlay receivers found.")
    for target in targets:
        print(f"{target.name:<32} {target.address:<16} {target.model:<20} id={target.identifier}")


async def _run(config) -> None:
    from aircast.bridge import Bridge

    bridge = Bridge(config)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    await bridge.start()
    try:
        await stop.wait()
    finally:
        logging.getLogger(__name__).info("Shutting down")
        await bridge.stop()


if __name__ == "__main__":
    main()
