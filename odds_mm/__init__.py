"""In-play odds market maker agent.

A production-minded, paper-trading market maker for in-play (live) sports
odds. Consumes multi-bookmaker odds feeds (TxODDS TxLINE or a deterministic
simulator), computes a de-vigged consensus fair price, and quotes two-sided
markets with inventory-aware skew and hard circuit breakers.

No real-money betting. No secrets. Paper trading only.
"""

__version__ = "0.1.0"
