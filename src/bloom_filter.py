"""A Redis-backed Bloom filter built from primitives.

The filter uses Redis bitmaps (``SETBIT``/``GETBIT``), so it works with any
stock Redis server.  Double hashing (the Kirsch-Mitzenmacher optimisation)
derives ``k`` independent positions from two base hashes:

    g_i(x) = (h1(x) + i * h2(x)) mod m
"""

from __future__ import annotations

import hashlib
import math
from typing import Iterable, List

import redis.asyncio as aioredis


class RedisBloomFilter:
    """Space-efficient probabilistic set implemented on a Redis bitmap."""

    def __init__(
        self,
        redis_client: aioredis.Redis,
        key: str = "crawler:bloom_filter",
        expected_elements: int = 1_000_000,
        false_positive_rate: float = 0.01,
    ) -> None:
        if expected_elements <= 0:
            raise ValueError("expected_elements must be positive")
        if not 0.0 < false_positive_rate < 1.0:
            raise ValueError("false_positive_rate must be between 0 and 1")

        self.redis = redis_client
        self.key = key
        self.n = expected_elements
        self.p = false_positive_rate

        # m = -(n * ln(p)) / (ln(2)^2)
        self.m = max(1, int(-1 * (self.n * math.log(self.p)) / (math.log(2) ** 2)))
        # k = (m / n) * ln(2)
        self.k = max(1, int(round((self.m / self.n) * math.log(2))))

    @property
    def memory_bytes(self) -> int:
        """Approximate number of bytes occupied by the backing bitmap."""
        return (self.m + 7) // 8

    def _get_hashes(self, item: str) -> List[int]:
        item_bytes = item.encode("utf-8")
        h1 = int.from_bytes(hashlib.sha256(item_bytes).digest(), "big")
        try:
            h2 = int.from_bytes(hashlib.md5(item_bytes).digest(), "big")
        except ValueError:  # pragma: no cover - only on FIPS-restricted builds
            h2 = int.from_bytes(
                hashlib.blake2b(item_bytes, digest_size=16).digest(), "big"
            )
        if h2 == 0:
            h2 = 0x9E3779B97F4A7C15
        return [(h1 + i * h2) % self.m for i in range(self.k)]

    async def add(self, item: str) -> None:
        offsets = self._get_hashes(item)
        async with self.redis.pipeline(transaction=False) as pipe:
            for offset in offsets:
                pipe.setbit(self.key, offset, 1)
            await pipe.execute()

    async def add_many(self, items: Iterable[str]) -> None:
        async with self.redis.pipeline(transaction=False) as pipe:
            for item in items:
                for offset in self._get_hashes(item):
                    pipe.setbit(self.key, offset, 1)
            await pipe.execute()

    async def contains(self, item: str) -> bool:
        offsets = self._get_hashes(item)
        async with self.redis.pipeline(transaction=False) as pipe:
            for offset in offsets:
                pipe.getbit(self.key, offset)
            results = await pipe.execute()
        return all(results)

    async def bit_count(self) -> int:
        """Number of set bits, useful as a cheap fill-ratio metric."""
        return int(await self.redis.bitcount(self.key))

    async def clear(self) -> None:
        await self.redis.delete(self.key)

    def estimate_fill_ratio(self, bit_count: int) -> float:
        return (bit_count / self.m) if self.m else 0.0
