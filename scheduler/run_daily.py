"""Dev wrapper: `python scheduler/run_daily.py` ≡ `python -m taxmatch --daily`."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from taxmatch.daily import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
