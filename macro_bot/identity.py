from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class LegacyIdentity:
    person: str
    telegram_user_id: int
    username: str
    display_name: str


LEGACY_IDENTITIES = (
    LegacyIdentity(
        person="vaan",
        telegram_user_id=349553317,
        username="Vaanasaurus",
        display_name="Vaan",
    ),
    LegacyIdentity(
        person="pooja",
        telegram_user_id=559404539,
        username="poojyyy20",
        display_name="Pooja",
    ),
)

LEGACY_IDENTITY_BY_PERSON = {item.person: item for item in LEGACY_IDENTITIES}
LEGACY_IDENTITY_BY_TELEGRAM_ID = {item.telegram_user_id: item for item in LEGACY_IDENTITIES}
LEGACY_PERSON_BY_USERNAME = {item.username.lower(): item.person for item in LEGACY_IDENTITIES}


def legacy_identity_for_person(person: str) -> Optional[LegacyIdentity]:
    return LEGACY_IDENTITY_BY_PERSON.get((person or "").strip().lower())


def legacy_identity_for_user(
    telegram_user_id: int,
    username: Optional[str] = None,
) -> Optional[LegacyIdentity]:
    identity = LEGACY_IDENTITY_BY_TELEGRAM_ID.get(int(telegram_user_id))
    if identity is not None:
        return identity

    person = LEGACY_PERSON_BY_USERNAME.get((username or "").strip().lower())
    if not person:
        return None
    return LEGACY_IDENTITY_BY_PERSON[person]


def legacy_person_for_user(telegram_user_id: int, username: Optional[str] = None) -> Optional[str]:
    identity = legacy_identity_for_user(telegram_user_id, username=username)
    return identity.person if identity is not None else None


def map_legacy_people_to_user_ids(people: list[str]) -> list[int]:
    user_ids: list[int] = []
    seen = set()
    for item in people:
        identity = legacy_identity_for_person(item)
        if identity is None or identity.telegram_user_id in seen:
            continue
        user_ids.append(identity.telegram_user_id)
        seen.add(identity.telegram_user_id)
    return user_ids
