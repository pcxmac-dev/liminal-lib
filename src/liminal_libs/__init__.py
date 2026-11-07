"""The Liminal library: the stores, the plugin contract, the collector and the scheme every Liminal
program builds on (the chart app, the integrator, the plugins). The user, 2026-11-01: "all lib
calls will point to liminal-libs"."""

import os

__version__ = "1.11.0"


def limit_threads() -> None:
    """numpy's OpenBLAS commits a buffer for every CPU thread when it loads: 757 MB of private memory
    in every process (and every command or build it starts) on a 24-thread machine, almost none of it
    used. The matrix work is small (Savitzky-Golay coefficients, cached): one thread (18 MB). Set
    before numpy loads; a value already in the environment is kept."""
    for name in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS"):
        os.environ.setdefault(name, "1")


limit_threads()
