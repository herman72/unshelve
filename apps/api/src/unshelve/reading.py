"""How long a text takes to read.

The pipeline counts it for each item. An extractor that reads only the first part of a long
file counts it for the whole file (``extract.pdf``).
"""

import math

WORDS_PER_MINUTE = 220


def reading_minutes(text: str) -> int:
    """How long the text takes to read, in whole minutes. At least 1."""
    return max(1, math.ceil(len(text.split()) / WORDS_PER_MINUTE))
