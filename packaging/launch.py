"""PyInstaller entry point for the double-click app."""
import sys

if len(sys.argv) > 1 and sys.argv[1] == "simulate":
    # The Simulator page runs each simulation in its own process (this same app).
    from coopa_trada.__main__ import main as cli

    raise SystemExit(cli(sys.argv[1:]))

from coopa_trada.desktop import main

raise SystemExit(main())
