"""One shared pool of worker processes for CPU-heavy work (decoding, fingerprinting and
rendering posters), so every task uses every core but one instead of a single core.

Functions run here must be importable at module level, as the workers are spawned fresh."""

from __future__ import annotations

import asyncio
import multiprocessing
import os
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from typing import Any

_pool: ProcessPoolExecutor | None = None


def workers() -> int:
    """Worker processes: every core but one (at least one)."""
    return max(1, (os.cpu_count() or 2) - 1)


def pool() -> ProcessPoolExecutor:
    global _pool
    if _pool is None:
        _pool = ProcessPoolExecutor(
            max_workers=workers(), mp_context=multiprocessing.get_context("spawn")
        )
    return _pool


async def run[T](fn: Callable[..., T], *args: Any) -> T:
    """Run ``fn(*args)`` in a worker process."""
    global _pool
    try:
        return await asyncio.get_running_loop().run_in_executor(pool(), fn, *args)
    except BrokenProcessPool:
        # A worker died (e.g. killed for memory): start a fresh pool for what follows.
        _pool = None
        raise


def shutdown() -> None:
    global _pool
    if _pool is not None:
        _pool.shutdown(wait=False, cancel_futures=True)
        _pool = None
