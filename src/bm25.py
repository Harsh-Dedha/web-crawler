"""Vectorised BM25 ranking over the in-memory inverted index."""

from __future__ import annotations

import math
from typing import Any, Dict, List

import numpy as np

from src.indexer import InvertedIndex
from src.stemmer import tokenize


class BM25Ranker:
    """BM25 (Best Matching 25) ranker.

    Score(D, Q) = sum(
        IDF(q) * (tf * (k1 + 1)) /
        (tf + k1 * (1 - b + b * (|D| / avgdl)))
    )
    """

    def __init__(
        self, index: InvertedIndex, k1: float = 1.5, b: float = 0.75
    ) -> None:
        if k1 <= 0:
            raise ValueError("k1 must be positive")
        if not 0.0 <= b <= 1.0:
            raise ValueError("b must be between 0 and 1")
        self.idx = index
        self.k1 = k1
        self.b = b

    def _compute_idf(self, df: int) -> float:
        # The +1 inside the log keeps IDF non-negative, matching Lucene.
        n = self.idx.total_docs
        return math.log(1.0 + (n - df + 0.5) / (df + 0.5))

    def search(self, query_text: str, top_k: int = 10) -> List[Dict[str, Any]]:
        # Repeated query terms are scored once; BM25 is a bag-of-words model.
        query_tokens = list(dict.fromkeys(tokenize(query_text)))
        if not query_tokens or self.idx.total_docs == 0:
            return []

        top_k = max(1, min(int(top_k), 100))
        avgdl = max(self.idx.avg_doc_length, 1e-9)
        doc_scores: Dict[int, float] = {}

        for token in query_tokens:
            postings = self.idx.index.get(token)
            if not postings:
                continue

            df = len(postings)
            idf = self._compute_idf(df)

            doc_ids = np.fromiter(
                (posting[0] for posting in postings), dtype=np.int32, count=df
            )
            term_freqs = np.fromiter(
                (posting[1] for posting in postings), dtype=np.float32, count=df
            )
            lengths = np.fromiter(
                (self.idx.doc_lengths[int(doc_id)] for doc_id in doc_ids),
                dtype=np.float32,
                count=df,
            )

            denominator = term_freqs + self.k1 * (
                1.0 - self.b + self.b * (lengths / avgdl)
            )
            # Guard against pathological zero denominators.
            denominator = np.maximum(denominator, 1e-9)
            scores = idf * ((term_freqs * (self.k1 + 1.0)) / denominator)

            for doc_id, score in zip(doc_ids, scores):
                key = int(doc_id)
                doc_scores[key] = doc_scores.get(key, 0.0) + float(score)

        if not doc_scores:
            return []

        ranked = sorted(
            doc_scores.items(), key=lambda item: (-item[1], item[0])
        )[:top_k]

        results: List[Dict[str, Any]] = []
        for doc_id, score in ranked:
            metadata = self.idx.doc_metadata.get(doc_id, {})
            results.append(
                {
                    "doc_id": doc_id,
                    "url": metadata.get("url", ""),
                    "domain": metadata.get("domain", ""),
                    "title": metadata.get("title", ""),
                    "snippet": metadata.get("snippet", ""),
                    "score": round(float(score), 4),
                }
            )
        return results
