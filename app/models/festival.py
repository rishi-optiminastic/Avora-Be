"""Festival — an admin-curated greeting the org broadcasts on a given day.

HR/Admin add festivals (name + date + message); on that date the celebration
scheduler emails every active employee. Dates are one-off (a specific calendar
date) — re-add for next year. Sits next to birthday / work-anniversary greetings,
which are derived automatically from each employee's date_of_birth / hire_date.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import Boolean, Date, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Festival(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "festivals"

    name: Mapped[str] = mapped_column(String(120))
    on_date: Mapped[date] = mapped_column(Date, index=True)
    message: Mapped[str] = mapped_column(String(2000))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
