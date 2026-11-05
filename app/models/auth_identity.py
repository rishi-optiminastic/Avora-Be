"""The Better Auth identity table - read and written here, but owned elsewhere.

Better Auth, running in the Next.js app, creates and migrates `auth_user`. It
lives in the same database as everything else, which matters: a work-email
change has to move two rows or it locks the person out. `employees.work_email`
is what Avora authorises on; `auth_user.email` is what they type to sign in.
Change one without the other and they either cannot log in, or log in to an
account the roster no longer recognises.

Deliberately NOT a `Base` model, and on its own `MetaData`, so Alembic's
autogenerate never proposes creating, altering or dropping a table another
system owns. Only the columns this backend actually touches are declared; the
rest of Better Auth's schema is none of our business.

`updatedAt` is quoted camel case and `timestamp without time zone` in the live
schema, so it is spelled exactly that way here and written as a naive UTC value.
"""

from __future__ import annotations

import sqlalchemy as sa

#: Separate from `Base.metadata` on purpose - see the module docstring.
auth_metadata = sa.MetaData()

auth_user = sa.Table(
    "auth_user",
    auth_metadata,
    sa.Column("id", sa.Text(), primary_key=True),
    sa.Column("email", sa.Text(), nullable=False),
    sa.Column("updatedAt", sa.DateTime(timezone=False), nullable=False),
)
