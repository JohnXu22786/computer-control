"""Entry point: python -m computer_control [serve|check|list]."""

import sys

from computer_control.cli import main

if __name__ == "__main__":
    sys.exit(main())
