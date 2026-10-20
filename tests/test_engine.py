from __future__ import annotations

import math
import unittest

from src.bloom_filter import RedisBloomFilter
from src.bm25 import BM25Ranker
from src.crawler import DistributedCrawler, normalize_url
from src.indexer import InvertedIndex
from src.stemmer import tokenize


class BloomMathTests(unittest.TestCase):
    def test_expected_dimensions(self) -> None:
        bloom = RedisBloomFilter(
            redis_client=None,  # only hash maths is exercised
            expected_elements=1_000_000,
            false_positive_rate=0.01,
        )
        self.assertEqual(bloom.m, 9_585_058)
        self.assertEqual(bloom.k, 7)

    def test_hashes_are_deterministic_and_in_range(self) -> None:
        bloom = RedisBloomFilter(None, expected_elements=10_000)
        first = bloom._get_hashes("https://example.com/page")
        second = bloom._get_hashes("https://example.com/page")
        self.assertEqual(first, second)
        self.assertEqual(len(first), bloom.k)
        self.assertTrue(all(0 <= offset < bloom.m for offset in first))
        self.assertNotEqual(first, bloom._get_hashes("https://example.com/other"))


class StemmerTests(unittest.TestCase):
    def test_tokenize_stems_and_removes_stopwords(self) -> None:
        self.assertEqual(
            tokenize("The distributed computing networks are running"),
            ["distribut", "comput", "network", "run"],
        )


class BM25Tests(unittest.TestCase):
    def test_relevant_document_ranks_first(self) -> None:
        records = [
            {
                "id": 1,
                "url": "https://example.com/distributed",
                "domain": "example.com",
                "title": "Distributed computing",
                "content_clean": (
                    "Distributed computing networks coordinate many machines. "
                    "Distributed systems provide fault tolerance."
                ),
                "doc_length": 12,
            },
            {
                "id": 2,
                "url": "https://example.com/cooking",
                "domain": "example.com",
                "title": "Cooking recipes",
                "content_clean": "Bread, pasta, and soup recipes for dinner.",
                "doc_length": 8,
            },
        ]
        index = InvertedIndex()
        index.build_from_records(records)
        ranker = BM25Ranker(index)
        results = ranker.search("distributed computing networks")
        self.assertTrue(results)
        self.assertEqual(results[0]["doc_id"], 1)
        self.assertGreater(results[0]["score"], 0)
        self.assertEqual(results[0]["url"], "https://example.com/distributed")

    def test_empty_index_returns_empty(self) -> None:
        index = InvertedIndex()
        index.build_from_records([])
        self.assertEqual(BM25Ranker(index).search("anything"), [])


class UrlTests(unittest.TestCase):
    def test_normalize_removes_fragment_and_default_port(self) -> None:
        self.assertEqual(
            normalize_url("HTTPS://Example.com:443/a?b=1#frag"),
            "https://example.com/a?b=1",
        )

    def test_extract_links_filters_non_http(self) -> None:
        crawler = DistributedCrawler(redis_client=None)
        html = """
        <html><body>
          <a href="/one">one</a>
          <a href="https://other.example/two#x">two</a>
          <a href="mailto:test@example.com">mail</a>
          <a href="javascript:void(0)">js</a>
        </body></html>
        """
        links = crawler.extract_links("https://example.com/base", html)
        self.assertEqual(
            links,
            {"https://example.com/one", "https://other.example/two"},
        )


if __name__ == "__main__":
    unittest.main()
