"""Entry point for `python -m rename_tv`."""
import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
