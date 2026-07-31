from __future__ import annotations

import re
from dataclasses import dataclass

from fu_gm.components.scene_frame_manager import SceneFrame
from fu_gm.models import Action, ActionType, SceneRecord


@dataclass(frozen=True)
class SceneAccessReview:
    blocked: bool = False
    route: str = ""
    reason: str = ""


class SceneAccessBoundary:
    """Keep declared movement inside the scene's established affordances.

    This is a rules boundary, not a message router. A route is blocked only
    when the prepared situation or a still-open NPC promise explicitly makes
    access to that same route something the heroes have yet to earn.
    """

    ROUTE_LABELS = (
        "旧路闸门",
        "旧路内门",
        "旧路外门",
        "维修小道",
        "风铃廊侧室",
        "秘密通道",
        "地下入口",
        "封锁门",
        "旧路",
        "侧路",
        "后门",
        "通道",
        "入口",
        "出口",
        "闸门",
        "门",
    )

    _CROSSING_PATTERNS = (
        r"(?:沿|顺着)(?P<route>[^。！？!?；;：:\n“”‘’\"'「」『』]{0,18}?)(?:走到|走进|进入|穿过|越过|深入|抵达)",
        r"(?:进入|走进|穿过|越过|深入|抵达)(?P<route>[^。！？!?；;：:\n“”‘’\"'「」『』]{0,18})",
        r"(?:跟上|随)[^。！？!?；;：:\n“”‘’\"'「」『』]{0,16}(?:穿过|进入|走进)"
        r"(?P<route>[^。！？!?；;：:\n“”‘’\"'「」『』]{0,12})",
    )

    def review(
        self,
        player_text: str,
        *,
        frame: SceneFrame | None,
        scene: SceneRecord | None,
        route_decision: dict[str, object] | None = None,
    ) -> SceneAccessReview:
        route = (
            self._semantic_crossed_route(route_decision)
            if route_decision is not None
            else self._crossed_route(player_text)
        )
        if not route:
            return SceneAccessReview()
        if self._requires_public_establishment(route) and not self._is_publicly_established(
            route,
            frame=frame,
            scene=scene,
        ):
            return SceneAccessReview(
                blocked=True,
                route=route,
                reason=f"{route}尚未在当前场景中公开出现，角色不能直接越过场景边界抵达那里。",
            )
        if not self._is_gated(route, frame=frame, scene=scene):
            return SceneAccessReview()
        if self._is_open(route, frame=frame, scene=scene):
            return SceneAccessReview()
        return SceneAccessReview(
            blocked=True,
            route=route,
            reason=f"{route}仍由当前场景的开放条件拦住，尚未成为可通行地点。",
        )

    def blocked_routes(
        self,
        *,
        frame: SceneFrame | None,
        scene: SceneRecord | None,
    ) -> list[str]:
        """Return concrete gated route names for player-facing legal context."""

        result: list[str] = []
        for route in self.ROUTE_LABELS:
            if route in {"门", "入口", "出口", "通道"}:
                continue
            if self._requires_public_establishment(route) and not self._is_publicly_established(
                route,
                frame=frame,
                scene=scene,
            ):
                result.append(route)
                continue
            if not self._is_gated(route, frame=frame, scene=scene):
                continue
            if self._is_open(route, frame=frame, scene=scene):
                continue
            if any(route in existing or existing in route for existing in result):
                continue
            result.append(route)
        return result

    def guard_action(
        self,
        player_text: str,
        *,
        frame: SceneFrame | None,
        scene: SceneRecord | None,
        route_decision: dict[str, object] | None = None,
    ) -> Action | None:
        review = self.review(
            player_text,
            frame=frame,
            scene=scene,
            route_decision=route_decision,
        )
        if not review.blocked:
            return None
        route = review.route or "这条路"
        return Action(
            ActionType.NARRATE,
            {
                "summary": (
                    f"{route}仍未开放；角色停在入口这一侧，没有抵达门后的地点。"
                ),
                "scene_access_blocked": True,
                "blocked_route": route,
                "non_damage": True,
                "gm_private_notes": (
                    "只呈现角色眼前可见的阻挡与停下的位置；不要替角色执行其他行动。"
                ),
                "reasoning": review.reason,
            },
        )

    @classmethod
    def _semantic_crossed_route(
        cls,
        route_decision: dict[str, object] | None,
    ) -> str:
        """Return a gated route only from the reviewed movement contract.

        Once semantic routing is present, raw prose is no longer a second
        authority. This prevents a plan, quoted direction, or metaphorical
        mention of a door from being blocked as an actual crossing.
        """

        route = dict(route_decision or {})
        if (
            route.get("target") != "fu_gm"
            or not route.get("performed_action")
            or route.get("table_proposal_only")
            or str(route.get("movement_scope") or "none") != "cross_scene"
        ):
            return ""
        if route.get("action_semantics_required") and not route.get(
            "action_semantics_reviewed"
        ):
            return ""
        destination = " ".join(
            str(route.get("movement_destination") or "").split()
        ).strip()
        return cls._route_label(destination)

    @classmethod
    def _crossed_route(cls, text: str) -> str:
        clean = str(text or "").strip()
        if not clean:
            return ""
        for pattern in cls._CROSSING_PATTERNS:
            for match in re.finditer(pattern, clean):
                span = str(match.group("route") or "")
                route = cls._route_label(span)
                if route:
                    return route
        # Common compact form: “沿旧路走到转折处”. The first expression above
        # captures only the text between 沿 and 走到, so keep a direct fallback.
        if re.search(r"(?:沿|顺着)旧路.{0,12}(?:走到|进入|穿过|抵达)", clean):
            return "旧路"
        return ""

    @classmethod
    def _route_label(cls, text: str) -> str:
        clean = str(text or "")
        for label in cls.ROUTE_LABELS:
            if label in clean:
                return label
        return ""

    @classmethod
    def _is_gated(
        cls,
        route: str,
        *,
        frame: SceneFrame | None,
        scene: SceneRecord | None,
    ) -> bool:
        aliases = cls._route_aliases(route)
        for condition in cls._conditions(frame, scene):
            if str(condition.get("status") or "open") != "open":
                continue
            promise = " ".join(
                str(condition.get(key) or "")
                for key in ("condition", "promised_result", "promise_subject")
            )
            if cls._mentions_route(promise, aliases):
                return True

        situation = cls._situation_text(frame, scene)
        if not cls._mentions_route(situation, aliases):
            return False
        gating_markers = (
            "说服",
            "争取",
            "获准",
            "准许",
            "允许",
            "开放",
            "开启",
            "打开",
            "放行",
            "取得",
            "获得",
            "解锁",
            "落锁",
            "封锁",
            "门闩",
            "闸钥",
            "条件",
            "承诺",
        )
        return any(marker in situation for marker in gating_markers)

    @classmethod
    def _is_publicly_established(
        cls,
        route: str,
        *,
        frame: SceneFrame | None,
        scene: SceneRecord | None,
    ) -> bool:
        aliases = cls._route_aliases(route)
        public_text = " ".join(
            [
                str(getattr(scene, "location", "") or ""),
                str(getattr(scene, "name", "") or ""),
                str(getattr(scene, "summary", "") or ""),
                str(getattr(scene, "objective", "") or ""),
                str(frame.location if frame else ""),
                str(frame.scene_name if frame else ""),
                *(frame.visible_elements if frame else []),
                *(frame.public_facts if frame else []),
                *(frame.established_facts if frame else []),
                *(frame.revealed_clues if frame else []),
                *(frame.committed_consequences if frame else []),
            ]
        )
        return cls._mentions_route(public_text, aliases)

    @staticmethod
    def _requires_public_establishment(route: str) -> bool:
        # Generic doors and entrances are often implicit in a room. Named
        # routes and destination-bearing passages are not: they must first be
        # shown or described by the GM/NPC before a character can cross them.
        return str(route or "").strip() not in {"门", "入口", "出口", "通道", "闸门"}

    @classmethod
    def _is_open(
        cls,
        route: str,
        *,
        frame: SceneFrame | None,
        scene: SceneRecord | None,
    ) -> bool:
        aliases = cls._route_aliases(route)
        for condition in cls._conditions(frame, scene):
            if str(condition.get("status") or "open") == "open":
                continue
            promise = " ".join(
                str(condition.get(key) or "")
                for key in ("promised_result", "promise_subject", "condition")
            )
            if cls._mentions_route(promise, aliases):
                return True

        public_text = " ".join(
            [
                *(frame.public_facts if frame else []),
                *(frame.established_facts if frame else []),
                *(frame.committed_consequences if frame else []),
                str(getattr(scene, "summary", "") or ""),
            ]
        )
        if not cls._mentions_route(public_text, aliases):
            return False
        opened_markers = (
            "已经开放",
            "已开放",
            "已经开启",
            "已开启",
            "已经打开",
            "已打开",
            "终于打开",
            "准许使用",
            "允许通行",
            "可以通行",
            "可以进入",
            "已经放行",
            "兑现承诺",
        )
        return any(marker in public_text for marker in opened_markers)

    @staticmethod
    def _conditions(frame: SceneFrame | None, scene: SceneRecord | None) -> list[dict[str, object]]:
        combined = [
            *(frame.open_conditions if frame else []),
            *(scene.open_conditions if scene else []),
        ]
        result: list[dict[str, object]] = []
        seen: set[str] = set()
        for condition in combined:
            condition_id = str(condition.get("condition_id") or "")
            fingerprint = condition_id or repr(sorted(condition.items()))
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            result.append(condition)
        return result

    @staticmethod
    def _situation_text(frame: SceneFrame | None, scene: SceneRecord | None) -> str:
        return " ".join(
            [
                str(getattr(scene, "objective", "") or ""),
                str(frame.premise if frame else ""),
                str(frame.dramatic_question if frame else ""),
                str(frame.closure_requirement if frame else ""),
                str(frame.current_pressure if frame else ""),
                *(frame.contract_situation_facts if frame else []),
                *(frame.open_questions if frame else []),
            ]
        )

    @staticmethod
    def _route_aliases(route: str) -> tuple[str, ...]:
        clean = str(route or "").strip()
        aliases = [clean]
        if "旧路" in clean:
            aliases.extend(["旧路", "旧路闸门", "旧路内门", "旧路外门"])
        if "门" in clean or "闸" in clean:
            aliases.extend(["门", "闸门", "门闩", "闸钥"])
        if clean in {"入口", "出口", "通道"}:
            aliases.extend(["入口", "出口", "通道"])
        return tuple(dict.fromkeys(item for item in aliases if item))

    @staticmethod
    def _mentions_route(text: str, aliases: tuple[str, ...]) -> bool:
        source = str(text or "")
        return any(alias and alias in source for alias in aliases)
