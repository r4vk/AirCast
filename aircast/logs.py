"""Logging setup: console always, plus an optional size-rotated log file."""

from __future__ import annotations

import logging
import logging.handlers

from aircast.config import Config

FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def configure_logging(config: Config, verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else getattr(logging, config.log_level.upper(), logging.INFO)
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    path = config.log_path()
    file_error: OSError | None = None
    if path is not None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            handlers.append(logging.handlers.RotatingFileHandler(
                path,
                maxBytes=max(1, config.log_max_size) * 1024 * 1024,
                backupCount=max(0, config.log_backups),
                encoding="utf-8",
            ))
        except OSError as exc:
            file_error = exc
    logging.basicConfig(level=level, format=FORMAT, handlers=handlers, force=True)
    if not verbose:
        # pyatv and zeroconf are chatty at INFO.
        logging.getLogger("pyatv").setLevel(max(level, logging.WARNING))
        logging.getLogger("zeroconf").setLevel(max(level, logging.WARNING))
    if file_error is not None:
        logging.getLogger(__name__).warning("Cannot open log file %s: %s", path, file_error)
