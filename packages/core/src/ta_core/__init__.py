"""ta_core — pure domain core. ZERO I/O: imports only stdlib + pydantic.

Nothing in this package may open a socket, read a file, or touch a database.
That rule is what makes the whole system testable without services running.
"""
__version__ = "0.1.0"
