from __future__ import annotations

import re


class SpeechIntentBoundary:
    """Reject scene prose that violates an explicit delivery constraint."""

    @classmethod
    def violation(
        cls,
        text: str,
        speech_intent: dict[str, object] | None,
    ) -> str:
        intent = dict(speech_intent or {})
        avoid = [
            str(item or "").strip()
            for item in (intent.get("avoid") or [])
            if str(item or "").strip()
        ]
        if any("列出" in item and ("选项" in item or "动作菜单" in item) for item in avoid):
            if cls._lists_player_options(text):
                return (
                    "上一候选把局势改写成了给玩家的选项菜单。请只演出NPC、对立方或环境已经采取的行动及其"
                    "可见后果，把如何回应留给玩家；不要使用‘若……若……’、‘要么……要么……’或编号选项。"
                )
        return ""

    @staticmethod
    def _lists_player_options(text: str) -> bool:
        source = " ".join(str(text or "").split())
        if not source:
            return False
        if len(re.findall(r"(?:如果|若)", source)) >= 2:
            return True
        if re.search(r"要么.{1,60}要么|或者.{1,60}或者", source):
            return True
        if re.search(r"(?:你们|英雄们|众人).{0,10}可以.{1,60}(?:也可以|或者|也能)", source):
            return True
        if re.search(r"(?:^|[。；;])\s*(?:1[.、]|①|一是).{1,80}(?:2[.、]|②|二是)", source):
            return True
        return False
