"""Lightweight tokenizer and suffix-stripping stemmer.

This deliberately avoids NLTK/spaCy so that indexing stays dependency-light
and fast enough to run inside the API process.
"""

from __future__ import annotations

import re
from typing import List

STOPWORDS = {
    "a", "about", "above", "after", "again", "against", "all", "am", "an",
    "and", "any", "are", "as", "at", "be", "because", "been", "before",
    "being", "below", "between", "both", "but", "by", "could", "did", "do",
    "does", "doing", "down", "during", "each", "few", "for", "from",
    "further", "had", "has", "have", "having", "he", "her", "here", "hers",
    "herself", "him", "himself", "his", "how", "i", "if", "in", "into", "is",
    "it", "its", "itself", "just", "me", "more", "most", "my", "myself",
    "no", "nor", "not", "now", "of", "off", "on", "once", "only", "or",
    "other", "our", "ours", "ourselves", "out", "over", "own", "same", "she",
    "should", "so", "some", "such", "than", "that", "the", "their", "theirs",
    "them", "themselves", "then", "there", "these", "they", "this", "those",
    "through", "to", "too", "under", "until", "up", "very", "was", "we",
    "were", "what", "when", "where", "which", "while", "who", "whom", "why",
    "with", "would", "you", "your", "yours", "yourself", "yourselves",
}

TOKEN_PATTERN = re.compile(r"\b[a-zA-Z0-9]{2,}\b")


class SimplePorterStemmer:
    """Fast step-down suffix-stripping rules for algorithmic stemming."""

    @staticmethod
    def stem(word: str) -> str:
        w = word.lower()
        if len(w) <= 3:
            return w

        # Step 1a
        if w.endswith("sses"):
            w = w[:-2]
        elif w.endswith("ies"):
            w = w[:-2]
        elif not w.endswith("ss") and w.endswith("s"):
            w = w[:-1]

        # Step 1b
        if w.endswith("eed"):
            if len(w) > 4:
                w = w[:-1]
        elif w.endswith("ed") or w.endswith("ing"):
            stem_candidate = w[:-2] if w.endswith("ed") else w[:-3]
            if any(char in "aeiou" for char in stem_candidate):
                w = stem_candidate
                # Porter's double-consonant cleanup: running -> runn -> run.
                if (
                    len(w) >= 2
                    and w[-1] == w[-2]
                    and w[-1] not in "lsz"
                ):
                    w = w[:-1]
                if w.endswith("at") or w.endswith("bl") or w.endswith("iz"):
                    w += "e"

        # A small set of common structural suffixes.
        for suffix in ("tional", "ization", "ation", "fully", "ness", "ment"):
            if w.endswith(suffix) and len(w) > len(suffix) + 2:
                w = w[: -len(suffix)]
                break

        return w


_STEMMER = SimplePorterStemmer()


def tokenize(text: str) -> List[str]:
    """Lowercase, split, remove stopwords, and stem a text blob."""
    raw_tokens = TOKEN_PATTERN.findall(text.lower())
    return [
        _STEMMER.stem(token)
        for token in raw_tokens
        if token not in STOPWORDS and len(token) >= 2
    ]
