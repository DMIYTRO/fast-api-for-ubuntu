"""Allow-listed PitStop profiles. Client input selects an id, never a path."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PureWindowsPath
from types import MappingProxyType
from typing import Iterable, Mapping


@dataclass(frozen=True, slots=True)
class PitStopProfile:
    id: str
    label: str
    windows_path: PureWindowsPath

    def __post_init__(self) -> None:
        if not self.id or not self.windows_path.is_absolute():
            raise ValueError("профиль PitStop должен иметь id и абсолютный Windows-путь")
        if self.windows_path.suffix.lower() != ".ppp":
            raise ValueError("режим проверки принимает только PitStop-профили .ppp")


class PitStopProfileCatalog:
    def __init__(self, profiles: Iterable[PitStopProfile]) -> None:
        profile_list = tuple(profiles)
        indexed = {profile.id: profile for profile in profile_list}
        if not indexed:
            raise ValueError("каталог профилей PitStop не может быть пустым")
        if len(indexed) != len(profile_list):
            raise ValueError("id профилей PitStop должны быть уникальными")
        self._profiles: Mapping[str, PitStopProfile] = MappingProxyType(indexed)

    def get(self, profile_id: str) -> PitStopProfile:
        try:
            return self._profiles[profile_id]
        except KeyError as exc:
            raise KeyError(f"профиль PitStop не разрешён: {profile_id}") from exc

    @property
    def profiles(self) -> tuple[PitStopProfile, ...]:
        return tuple(self._profiles.values())
