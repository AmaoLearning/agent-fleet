"""Explicit CSV-to-Harbor preparation; see README.md for run commands."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "Agents/utils/web_search/src"))

from web_search_adapter.main import deepsearchqa_main

if __name__ == "__main__":
    deepsearchqa_main()
