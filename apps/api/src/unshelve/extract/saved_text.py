"""The text that you saved: it reads a note, and it is the fallback when a page cannot be read.

It opens nothing. For a saved message with a link, its text is the text of the message,
and the item is "partial": the page of the link was not read.
"""

from typing import ClassVar

from unshelve.contracts.dto import Extracted, SourceRef
from unshelve.contracts.enums import ItemKind
from unshelve.contracts.protocols import ExtractContext

# The weakest claim: it takes a source only when no other extractor does.
LAST_RESORT = 1


class SavedTextExtractor:
    name: ClassVar[str] = "saved_text"
    version: ClassVar[int] = 1

    def match(self, ref: SourceRef) -> int:
        return LAST_RESORT if ref.text.strip() or ref.url else 0

    async def extract(self, ref: SourceRef, ctx: ExtractContext) -> Extracted:
        # A note is a note. For a link whose page was not read, the kind is not known.
        kind = ItemKind.NOTE if ref.url is None else ItemKind.UNKNOWN
        return Extracted(kind=kind, text=ref.text)
