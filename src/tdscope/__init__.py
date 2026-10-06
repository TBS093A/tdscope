"""tdscope - analyze Java (HotSpot) thread dumps."""

from .filters import ThreadFilter
from .loader import load
from .model import Deadlock, HttpRequest, LockEvent, ThreadDump, ThreadInfo
from .parser import parse_lines

__version__ = "0.1.0"

__all__ = [
    "Deadlock",
    "HttpRequest",
    "LockEvent",
    "ThreadDump",
    "ThreadFilter",
    "ThreadInfo",
    "__version__",
    "load",
    "parse_lines",
]
