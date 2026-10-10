"""Path parameters that more than one router uses."""

from typing import Annotated

from fastapi import Path

# Ids are database serials, so they start at 1. A smaller number never exists: 422, not 404.
ItemId = Annotated[int, Path(ge=1)]
GoalId = Annotated[int, Path(ge=1)]
JobId = Annotated[int, Path(ge=1)]
