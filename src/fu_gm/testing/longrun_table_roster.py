from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

from fu_gm.testing.luna_player_agent import (
    DEFAULT_LONGRUN_PERSONAS,
    PlayerPersona,
)


@dataclass(frozen=True)
class LongRunTableSeat:
    """One stable player/hero identity used throughout a long-run table."""

    seat_id: str
    persona: PlayerPersona

    @property
    def player_name(self) -> str:
        return self.persona.player_name

    @property
    def hero_name(self) -> str:
        return self.persona.hero_name


@dataclass(frozen=True)
class LongRunTableRoster:
    """Immutable source of truth for every participant-facing test component."""

    gm_name: str
    seats: tuple[LongRunTableSeat, ...]
    schema_version: int = 1

    def __post_init__(self) -> None:
        if not str(self.gm_name or "").strip():
            raise ValueError("长测桌面必须有主持人名字。")
        if not self.seats:
            raise ValueError("长测桌面至少需要一名玩家。")
        self._require_unique("seat_id", (seat.seat_id for seat in self.seats))
        self._require_unique(
            "player_name", (seat.player_name for seat in self.seats)
        )
        self._require_unique("hero_name", (seat.hero_name for seat in self.seats))
        if self.gm_name in self.player_names:
            raise ValueError("主持人不能同时占用玩家座位。")

    @staticmethod
    def _require_unique(label: str, values: Iterable[str]) -> None:
        cleaned = tuple(str(value or "").strip() for value in values)
        if any(not value for value in cleaned):
            raise ValueError(f"长测桌面的 {label} 不能为空。")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError(f"长测桌面的 {label} 不能重复。")

    @classmethod
    def from_catalog(
        cls,
        player_names: Iterable[str],
        *,
        catalog: Mapping[str, PlayerPersona] = DEFAULT_LONGRUN_PERSONAS,
        gm_name: str = "时悠",
    ) -> "LongRunTableRoster":
        names = tuple(str(name or "").strip() for name in player_names)
        missing = [name for name in names if name not in catalog]
        if missing:
            raise ValueError("长测人格目录缺少玩家：" + "、".join(missing))
        return cls(
            gm_name=str(gm_name or "").strip(),
            seats=tuple(
                LongRunTableSeat(
                    seat_id=f"pl-{index:02d}",
                    persona=catalog[name],
                )
                for index, name in enumerate(names, start=1)
            ),
        )

    @property
    def player_names(self) -> tuple[str, ...]:
        return tuple(seat.player_name for seat in self.seats)

    @property
    def hero_names(self) -> tuple[str, ...]:
        return tuple(seat.hero_name for seat in self.seats)

    @property
    def personas(self) -> dict[str, PlayerPersona]:
        return {seat.player_name: seat.persona for seat in self.seats}

    @property
    def player_to_hero(self) -> dict[str, str]:
        return {seat.player_name: seat.hero_name for seat in self.seats}

    @property
    def hero_to_player(self) -> dict[str, str]:
        return {seat.hero_name: seat.player_name for seat in self.seats}

    def identity_at(self, index: int) -> tuple[str, str]:
        seat = self.seats[int(index) % len(self.seats)]
        return seat.player_name, seat.hero_name

    def checkpoint_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "gm_name": self.gm_name,
            "seats": [
                {
                    "seat_id": seat.seat_id,
                    "player_name": seat.player_name,
                    "hero_name": seat.hero_name,
                }
                for seat in self.seats
            ],
        }

    def assert_checkpoint_payload(self, payload: object) -> None:
        if not isinstance(payload, Mapping):
            raise ValueError("长测检查点缺少桌面名册，不能安全续跑。")
        observed = {
            "schema_version": int(payload.get("schema_version") or 0),
            "gm_name": str(payload.get("gm_name") or "").strip(),
            "seats": [
                {
                    "seat_id": str(item.get("seat_id") or "").strip(),
                    "player_name": str(item.get("player_name") or "").strip(),
                    "hero_name": str(item.get("hero_name") or "").strip(),
                }
                for item in payload.get("seats") or []
                if isinstance(item, Mapping)
            ],
        }
        if observed != self.checkpoint_payload():
            raise ValueError(
                "长测检查点的桌面名册与当前配置不同，不能把旧玩家状态静默套到新桌。"
            )

    def assert_exact_players(
        self,
        actual: Iterable[str],
        *,
        source: str,
    ) -> None:
        observed = tuple(
            str(name or "").strip()
            for name in actual
            if str(name or "").strip()
        )
        expected = self.player_names
        if observed == expected:
            return
        missing = [name for name in expected if name not in observed]
        unexpected = [name for name in observed if name not in expected]
        duplicates = [
            name
            for index, name in enumerate(observed)
            if name in observed[:index]
        ]
        order_mismatch = (
            not missing
            and not unexpected
            and not duplicates
            and observed != expected
        )
        details: list[str] = []
        if missing:
            details.append("缺少=" + "、".join(missing))
        if unexpected:
            details.append("多出=" + "、".join(unexpected))
        if duplicates:
            details.append("重复=" + "、".join(dict.fromkeys(duplicates)))
        if order_mismatch:
            details.append("顺序不一致")
        raise RuntimeError(
            f"{source}的参与者与桌面名册不一致（{'；'.join(details)}）；"
            f"期望={list(expected)!r}，实际={list(observed)!r}。"
        )


THREE_PLAYER_LONGRUN_ROSTER = LongRunTableRoster.from_catalog(
    ("阿凛", "南星", "白河")
)
