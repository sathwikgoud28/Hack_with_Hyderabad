"""Load the 18 historical incidents into memory (Hindsight or the local store).

Usage:
    python -m scripts.seed_memory          # add seed incidents
    python -m scripts.seed_memory --reset  # wipe the bank / local store first
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.main import memory, seed_memory  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true", help="delete existing memories first")
    args = parser.parse_args()

    print(f"Memory backend: {memory.name}")
    start = time.time()
    try:
        n = seed_memory(reset=args.reset)
        print(f"Retained {n} past incidents in {time.time() - start:.1f}s")
    finally:
        getattr(memory, "close", lambda: None)()


if __name__ == "__main__":
    main()
