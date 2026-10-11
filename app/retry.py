import random


def backoff(attempt: int, base: float, cap: float) -> float:
    """Exponential backoff with full jitter.

    Plain exponential backoff makes every client that failed together retry together,
    so a recovering provider gets a thundering herd at each step. Drawing the delay
    uniformly from [0, ceiling] spreads them out.
    """
    return random.uniform(0, min(cap, base * 2**attempt))
