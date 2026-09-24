"""Cooperative cancellation shared by a single CLI execution."""
import threading

CANCELLED = threading.Event()
