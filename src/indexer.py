"""In-memory inverted index."""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any, Dict, Iterable, List, Tuple

from src.stemmer import tokenize


def _field(record: Any, name: str, default: Any = "") -> Any:
    """Read a field from either a dict or an asyncpg.Record."""
    try:
        value = record[name]
    except (KeyError, IndexError, TypeError):
        return default
    return default if value is None else value


class InvertedIndex:
    """Term -> postings ``[(doc_id, term_frequency), ...]`` plus metadata.

    ``doc_lengths`` stores the *number of indexed tokens* (after stopword
    removal and stemming).  That is the correct length for BM25's length
    normalisation, and it is more stable than a raw whitespace word count.
    """

    def __init__(self) -> None:
        self.index: Dict[str, List[Tuple[int, int]]] = defaultdict(list)
        self.doc_lengths: Dict[int, int] = {}
        self.doc_metadata: Dict[int, dict] = {}
        self.total_docs: int = 0
        self.avg_doc_length: float = 0.0
        self.vocabulary_size: int = 0

    def build_from_records(self, records: Iterable[Any]) -> None:
        self.index.clear()
        self.doc_lengths.clear()
        self.doc_metadata.clear()
        self.total_docs = 0
        self.avg_doc_length = 0.0
        self.vocabulary_size = 0

        total_length = 0
        for record in records:
            doc_id = int(_field(record, "id", 0))
            if doc_id <= 0:
                continue

            title = str(_field(record, "title", "") or "")
            content = str(_field(record, "content_clean", "") or "")
            url = str(_field(record, "url", "") or "")
            domain = str(_field(record, "domain", "") or "")

            tokens = tokenize(f"{title} {content}")
            effective_length = len(tokens)
            counts = Counter(tokens)

            self.doc_metadata[doc_id] = {
                "title": title,
                "url": url,
                "domain": domain,
                "length": effective_length,
                "snippet": " ".join(content.split())[:240],
            }
            self.doc_lengths[doc_id] = effective_length
            total_length += effective_length
            self.total_docs += 1

            for term, count in counts.items():
                self.index[term].append((doc_id, count))

        self.avg_doc_length = (
            (total_length / self.total_docs) if self.total_docs > 0 else 0.0
        )
        self.vocabulary_size = len(self.index)
