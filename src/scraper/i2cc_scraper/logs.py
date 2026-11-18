"""Logging setup shared by the command-line tools (i2cc-scrape, i2cc-download): one place to
change the format later, e.g. to JSON lines for CloudWatch."""
import logging
import sys


def setup_logging(level: int = logging.INFO) -> None:
    """Log to stderr as "LEVEL: message" (stdout carries the JSON results)."""
    logging.basicConfig(level=level, format='%(levelname)s: %(message)s', stream=sys.stderr)
