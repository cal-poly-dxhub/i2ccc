"""
Re-exports the ingest pipeline's processing_config so the collector filters
and names files exactly as the pipeline expects.

The module lives at src/ingest/shared/processing_config.py; the collector
image copies it to the same relative path, so this works locally and in
the container.
"""

import os
import sys

_SHARED_DIR = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "src", "ingest", "shared")
)
if _SHARED_DIR not in sys.path:
    sys.path.insert(0, _SHARED_DIR)

from processing_config import *  # noqa: E402,F401,F403
