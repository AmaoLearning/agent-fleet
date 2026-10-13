"""Async worker configuration and cancellation ownership."""

import asyncio
import os


def configured_threads(name, default):
    value = os.environ.get(name, "")
    if not value:
        return default
    try:
        count = int(value)
    except ValueError:
        count = 0
    if count <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return count


async def await_owned(awaitable):
    future = asyncio.ensure_future(awaitable)
    cancelled = False
    while not future.done():
        try:
            await asyncio.shield(future)
        except asyncio.CancelledError:
            cancelled = True
        except Exception:  # noqa: BLE001 -- Retrieve the result after resolving cancellation ownership.
            break
    if cancelled:
        if not future.cancelled():
            future.exception()
        raise asyncio.CancelledError
    return future.result()
