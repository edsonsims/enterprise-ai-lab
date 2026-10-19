"""Export a validated metadata-only artifact; never copy private runtime files."""
from pathlib import Path
import sys

from enterprise_ai_lab.review.failure import read_failure, save_failure


def export(source, destination):
    try:
        value = read_failure(source)
        save_failure(destination, value)
        return read_failure(destination) == value
    except Exception:
        return False


if __name__ == "__main__":
    valid = len(sys.argv) == 3 and export(Path(sys.argv[1]), Path(sys.argv[2]))
    # Only these fixed strings may reach GitHub Actions' output channel.
    print("valid=true" if valid else "valid=false")
