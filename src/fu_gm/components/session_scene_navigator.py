from __future__ import annotations

import re
from collections.abc import Iterable

from fu_gm.models import SessionDramaticContract, SessionSceneOpportunity


class SessionSceneNavigator:
    """Select a prepared situation for the current scene without fixing a plot.

    The dramatic contract contains movable scene opportunities.  This navigator
    gives them a lifecycle role so a long table session does not accidentally
    replay one conversation under four different scene names.  Player-facing
    facts still decide which middle route is chosen; only the strong start,
    climax and aftermath have a preferred functional position.
    """

    _ROLES_BY_ACT: dict[int, tuple[str, ...]] = {
        1: ("strong_start",),
        2: ("social_or_investigation", "alternate_approach"),
        3: ("climax_candidate",),
        4: ("aftermath",),
    }
    _SOCIAL_MARKERS = (
        "问",
        "说服",
        "交涉",
        "谈判",
        "承诺",
        "条件",
        "答复",
        "态度",
        "信任",
    )
    _ALTERNATE_MARKERS = (
        "调查",
        "检查",
        "观察",
        "痕迹",
        "记录",
        "仪式",
        "法术",
        "工程",
        "命刻",
        "绕路",
        "潜入",
    )

    def select(
        self,
        contract: SessionDramaticContract,
        *,
        act_number: int = 0,
        used_keys: Iterable[str] = (),
        scene_text: str = "",
        recent_context: str = "",
        location_anchor: str = "",
    ) -> SessionSceneOpportunity | None:
        opportunities = list(contract.potential_scenes or [])
        if not opportunities:
            return None
        used = {str(item or "").strip() for item in used_keys if str(item or "").strip()}
        act = act_number or self.infer_act(scene_text)
        desired_roles = self._ROLES_BY_ACT.get(act, ())
        unused = [item for item in opportunities if item.scene_key not in used]
        pool = unused or opportunities

        if desired_roles:
            role_pool = [item for item in pool if item.scene_role in desired_roles]
            if role_pool:
                pool = role_pool
            else:
                # A missing middle scene must not consume the prepared climax,
                # just as a missing climax must not rewind to the strong start.
                # The caller can create a neutral scene for this act instead.
                return None

        anchor = str(location_anchor or "").strip()
        if anchor:
            anchored_pool = [
                item
                for item in pool
                if self.location_matches_anchor(item.location, anchor)
            ]
            if not anchored_pool:
                # The player-established landing point is authoritative. A
                # prepared situation that only works elsewhere remains unused;
                # callers can open a neutral scene at the chosen destination.
                return None
            pool = anchored_pool

        haystack = f"{scene_text}\n{recent_context}".lower()
        scored: list[tuple[int, int, SessionSceneOpportunity]] = []
        for index, opportunity in enumerate(pool):
            score = self._match_score(opportunity, haystack)
            if opportunity.scene_role in desired_roles:
                score += 20 - desired_roles.index(opportunity.scene_role)
            if act == 2:
                if opportunity.scene_role == "social_or_investigation":
                    score += sum(marker in recent_context for marker in self._SOCIAL_MARKERS)
                elif opportunity.scene_role == "alternate_approach":
                    score += sum(marker in recent_context for marker in self._ALTERNATE_MARKERS)
            scored.append((score, -index, opportunity))
        return max(scored, key=lambda item: (item[0], item[1]))[2]

    @classmethod
    def location_matches_anchor(cls, candidate: str, anchor: str) -> bool:
        """Return whether a prepared location can honor a resolved destination.

        Sharing only the same building or region is insufficient: choosing the
        east platform must not silently reopen a desk inside the waiting hall.
        A location-less opportunity is movable and may adopt the anchor.
        """

        candidate_parts = cls._location_parts(candidate)
        anchor_parts = cls._location_parts(anchor)
        if not candidate_parts:
            return True
        if not anchor_parts:
            return False
        if candidate_parts == anchor_parts:
            return True
        candidate_leaf = candidate_parts[-1]
        anchor_leaf = anchor_parts[-1]
        if len(candidate_leaf) >= 2 and len(anchor_leaf) >= 2 and (
            candidate_leaf in anchor_leaf or anchor_leaf in candidate_leaf
        ):
            if len(candidate_parts) == 1 or len(anchor_parts) == 1:
                return True
            return candidate_parts[0] == anchor_parts[0]
        return False

    @staticmethod
    def infer_act(scene_text: str) -> int:
        text = str(scene_text or "")
        match = re.search(r"(?:场景|第)\s*([1-4一二三四])\s*(?:幕|场景)?", text)
        if match:
            return {"一": 1, "二": 2, "三": 3, "四": 4}.get(
                match.group(1), int(match.group(1)) if match.group(1).isdigit() else 0
            )
        if any(marker in text for marker in ("强开场", "开幕")):
            return 1
        if any(marker in text for marker in ("探索", "调查", "发展")):
            return 2
        if any(marker in text for marker in ("高潮", "决战", "反转")):
            return 3
        if any(marker in text for marker in ("余波", "收束", "尾声")):
            return 4
        return 0

    @classmethod
    def _match_score(cls, opportunity: SessionSceneOpportunity, haystack: str) -> int:
        score = 0
        for value in (
            opportunity.title,
            opportunity.location,
            *opportunity.npc_names,
            *opportunity.entry_points,
        ):
            for token in cls._tokens(value):
                if token.lower() in haystack:
                    score += 1
        return score

    @staticmethod
    def _tokens(value: str) -> list[str]:
        text = str(value or "").strip()
        if not text:
            return []
        chunks = [
            item
            for item in re.split(r"[\s，,。；;：:、｜|（）()·/]+", text)
            if len(item) >= 2
        ]
        # Proper names and concise location labels are usually more useful than
        # splitting Chinese text into arbitrary character n-grams.
        return chunks[:8]

    @staticmethod
    def _location_parts(value: str) -> tuple[str, ...]:
        return tuple(
            re.sub(r"[\s，,。；;：:]+", "", item)
            for item in re.split(r"[·•／/＞>]", str(value or ""))
            if re.sub(r"[\s，,。；;：:]+", "", item)
        )
