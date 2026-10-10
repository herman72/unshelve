"""Column helpers."""

from enum import StrEnum

from sqlalchemy import CheckConstraint, Dialect, Text
from sqlalchemy.types import TypeDecorator


class StrEnumType[E: StrEnum](TypeDecorator[E]):
    """Stores a ``StrEnum`` as text and returns the enum member.

    The set of values is enforced by a CHECK constraint (``enum_check``), not by a Postgres
    enum type: adding a value is then an ordinary constraint change.
    """

    impl = Text
    cache_ok = True

    def __init__(self, enum: type[E]) -> None:
        super().__init__()
        self.enum = enum

    def process_bind_param(self, value: E | str | None, dialect: Dialect) -> str | None:
        return None if value is None else self.enum(value).value

    def process_result_value(self, value: str | None, dialect: Dialect) -> E | None:
        return None if value is None else self.enum(value)


def enum_check(column: str, enum: type[StrEnum]) -> CheckConstraint:
    """A CHECK that limits ``column`` to the members of ``enum``. NULL passes."""
    values = ", ".join(f"'{member.value}'" for member in enum)
    return CheckConstraint(f"{column} IN ({values})", name=f"{column}_valid")
