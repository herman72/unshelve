"""The limits of the watcher. They are constants in v1, not settings (issue #154)."""

from typing import Final

# Texts.
# An error text in a message is cut to this length.
ERROR_CHARS: Final = 200
# secrets_of drops a shorter value, so it is not hidden by its value. The default password
# "unshelve" (8 characters) is one: many fix texts name the command "unshelve".
SECRET_MIN: Final = 12
# The shortest part of a cut secret that is hidden.
SECRET_PART_MIN: Final = 8
