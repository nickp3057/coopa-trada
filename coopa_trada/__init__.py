"""Coopa Trada — Schwab single-leg options automation skeleton.

Dry run is the default. Live trading requires two independent opt-ins
(``--live`` on the CLI *and* ``LIVE_TRADING=1`` in the environment).

The signal format is deliberately unknown: ingestion is a stub behind
``models.SignalSource``. The exit logic in ``exits`` is pure and is proven
against synthetic price series before any live quote reaches it.
"""

__version__ = "0.1.0"
