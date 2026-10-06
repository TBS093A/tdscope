"""tdscope - analyze Java (HotSpot) thread dumps."""

from .filters import ThreadFilter
from .loader import load
from .model import Deadlock, HttpRequest, LockEvent, ThreadDump, ThreadInfo
from .parser import parse_lines

try:  # written by the build backend from the git tag (hatch-vcs)
    from ._version import __version__
except ImportError:  # running from a source tree that was never built or installed
    from importlib.metadata import PackageNotFoundError, version

    try:
        __version__ = version("tdscope")
    except PackageNotFoundError:
        __version__ = "0+unknown"

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
