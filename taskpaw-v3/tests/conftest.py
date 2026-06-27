"""Put taskpaw-v3/ on sys.path so `core` / `agent` / `hub` import in tests."""

import sys
from pathlib import Path

V3_ROOT = Path(__file__).resolve().parents[1]
if str(V3_ROOT) not in sys.path:
    sys.path.insert(0, str(V3_ROOT))
