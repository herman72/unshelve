"""Which extractor reads a source: the one with the highest score.

A new extractor joins by one line in ``EXTRACTORS``. The text that you saved is always the
last resort, also for a source that no extractor claims (a photo without a caption).
"""

from collections.abc import Sequence

from unshelve.contracts.dto import SourceRef
from unshelve.contracts.protocols import Extractor
from unshelve.extract.article import ArticleExtractor
from unshelve.extract.arxiv import ArxivExtractor
from unshelve.extract.github import GitHubExtractor
from unshelve.extract.saved_text import SavedTextExtractor
from unshelve.extract.x_post import XPostExtractor
from unshelve.extract.youtube import YouTubeExtractor

FALLBACK = SavedTextExtractor()
# The scores: an extractor for one site 50, the article extractor 10, the saved text 1.
EXTRACTORS: tuple[Extractor, ...] = (
    YouTubeExtractor(),
    XPostExtractor(),
    GitHubExtractor(),
    ArxivExtractor(),
    ArticleExtractor(),
    FALLBACK,
)


def choose(ref: SourceRef, extractors: Sequence[Extractor] = EXTRACTORS) -> Extractor:
    """The extractor with the highest score for ``ref``. On a tie, the first one in the list."""
    best = FALLBACK
    best_score = 0
    for extractor in extractors:
        score = extractor.match(ref)
        if score > best_score:
            best, best_score = extractor, score
    return best
