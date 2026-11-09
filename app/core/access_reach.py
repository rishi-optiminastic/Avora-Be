"""Who can reach an asset - pure, no I/O.

A grant on an account reaches everything that sits under it: whoever holds
devops@ can sign in to the Vercel, GitHub and Adobe logins hanging off it,
whether or not anyone wrote a grant for each tool. The catalog and the People
view both ask this module, so they always agree on who has what.

Rules:

* Direct grants win. Someone granted Vercel directly is listed as direct, even
  if they also hold devops@.
* Access flows down the parent chain, at most `MAX_DEPTH` accounts up, and a
  loop in the chain (A under B under A) is cut rather than followed forever.
* When the same person reaches an asset through two accounts, the nearest
  account is the one named.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from app.models.access import AccessGrant

MAX_DEPTH = 3


@dataclass(frozen=True)
class Reach:
    """One person's way into one asset."""

    asset_id: uuid.UUID
    grant: AccessGrant
    # None for a direct grant; else the account the access comes through.
    via_asset_id: uuid.UUID | None

    @property
    def employee_id(self) -> uuid.UUID:
        return self.grant.employee_id

    @property
    def direct(self) -> bool:
        return self.via_asset_id is None


def ancestors(
    asset_id: uuid.UUID, parent_of: Mapping[uuid.UUID, uuid.UUID | None]
) -> list[uuid.UUID]:
    """The accounts above an asset, nearest first, loop-safe and depth-capped."""
    chain: list[uuid.UUID] = []
    seen = {asset_id}
    current = parent_of.get(asset_id)
    while current is not None and current not in seen and len(chain) < MAX_DEPTH:
        chain.append(current)
        seen.add(current)
        current = parent_of.get(current)
    return chain


def reach_for_asset(
    asset_id: uuid.UUID,
    parent_of: Mapping[uuid.UUID, uuid.UUID | None],
    grants_by_asset: Mapping[uuid.UUID, Sequence[AccessGrant]],
) -> list[Reach]:
    """Everyone who can reach this asset: direct holders, then those who come in
    through an account above it."""
    out = [Reach(asset_id, g, None) for g in grants_by_asset.get(asset_id, ())]
    present = {r.employee_id for r in out}
    for account in ancestors(asset_id, parent_of):
        for grant in grants_by_asset.get(account, ()):
            if grant.employee_id in present:
                continue
            present.add(grant.employee_id)
            out.append(Reach(asset_id, grant, account))
    return out


def reach_by_person(
    asset_ids: Sequence[uuid.UUID],
    parent_of: Mapping[uuid.UUID, uuid.UUID | None],
    grants_by_asset: Mapping[uuid.UUID, Sequence[AccessGrant]],
) -> dict[uuid.UUID, list[Reach]]:
    """For every person, each asset they can reach and how."""
    out: dict[uuid.UUID, list[Reach]] = {}
    for asset_id in asset_ids:
        for reach in reach_for_asset(asset_id, parent_of, grants_by_asset):
            out.setdefault(reach.employee_id, []).append(reach)
    return out
