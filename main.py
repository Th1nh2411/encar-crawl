#!/usr/bin/env python3
"""
main.py -- The one entry point. See encar/cli.py for the commands.

    python main.py crawl                    # every model, every page, sold detection
    python main.py crawl --model bmw_3series_g20
    python main.py export --view cars
    python main.py check-plate 110모5877
    python main.py import-stock
    python main.py dashboard
"""

import sys

from encar.cli import main

if __name__ == "__main__":
    sys.exit(main())
