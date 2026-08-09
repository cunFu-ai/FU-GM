from __future__ import annotations

import re
from difflib import SequenceMatcher


class SceneMomentPolicy:
    """Validate player-facing scene prose without owning the story."""

    _COMMITTED_STATE_PATTERNS = {
        "sealed": (
            r"(?:封印|符文).{0,10}(?:重新)?亮(?:起|了)?",
            r"封死|封锁|锁死|上锁|封住|堵住|不可通行|无法通行",
        ),
        "opened": (
            r"打开|开启|解锁|解封|敞开|可以通行|恢复通行",
        ),
        "broken": (
            r"碎裂|破碎|折断|坍塌|崩塌|被毁|摧毁|失效",
        ),
        "alerted": (
            r"察觉|发觉|警觉|发现了|暴露|惊动|拉响警报",
        ),
        "arrived": (
            r"抵达|赶到|闯入|冲入|现身|出现在",
        ),
        "departed": (
            r"离开|撤离|逃走|退去|消失在",
        ),
        "extinguished": (
            r"熄灭|暗下|失去光芒",
        ),
    }
    _STATE_WORDS = re.compile(
        r"重新亮起|亮起|亮了|封死|封锁|锁死|上锁|封住|堵住|不可通行|无法通行|"
        r"打开|开启|解锁|解封|敞开|可以通行|恢复通行|碎裂|破碎|折断|坍塌|崩塌|"
        r"被毁|摧毁|失效|察觉|发觉|警觉|发现了|暴露|惊动|拉响警报|抵达|赶到|"
        r"闯入|冲入|现身|出现在|离开|撤离|逃走|退去|消失在|熄灭|暗下|失去光芒"
    )

    @classmethod
    def sanitize(
        cls,
        reply: str,
        packet: dict[str, object],
        *,
        allow_empty: bool = False,
    ) -> str:
        text = str(reply or "").replace("旅旅人", "旅人")
        text = re.sub(r"(?m)^(.{0,30}?)的镜头打开[：:]\s*", r"\1：", text)
        text = re.sub(r"(?m)^\s*镜头打开[：:]\s*", "", text)
        text = re.sub(
            r"(?m)^\s*地点、人物和压力同时浮出，桌面安静一拍，等英雄把第一句话或第一步行动落进去。\s*",
            "",
            text,
        )
        text = text.replace("可互动焦点", "眼前最紧要的事").strip()
        if cls.looks_like_backstage_formula(text):
            text = ""
        if text:
            return text
        return "" if allow_empty else cls.fallback(packet)

    @staticmethod
    def looks_like_backstage_formula(text: str) -> bool:
        clean = str(text or "")
        if any(
            marker in clean
            for marker in (
                "让玩家",
                "必须出现",
                "场景包",
                "GM后台",
                "可揭示内容",
                "可互动焦点",
                "本场核心问题",
                "故事大纲",
            )
        ):
            return True
        if re.search(r"建立.{0,18}(?:压力|冲突|命刻|证据链)", clean):
            return True
        if "局势已经压到眼前" in clean and "不再只是远处的传闻" in clean:
            return True
        backstage_terms = (
            "社交冲突",
            "派系态度",
            "第一条公开证据",
            "地下城探索",
            "证据链",
            "危险命刻",
            "互动焦点",
            "本场核心问题",
        )
        return "不再只是远处的传闻" in clean and any(term in clean for term in backstage_terms)

    @classmethod
    def is_player_facing_fact(cls, value: object) -> bool:
        text = cls._clean_fact(value)
        return bool(text and not cls.looks_like_backstage_formula(text))

    @classmethod
    def fallback(cls, packet: dict[str, object]) -> str:
        location = str(packet.get("location") or packet.get("scene_name") or "").strip()
        premise = cls._clean_fact(packet.get("premise"))
        pressure = cls._clean_fact(packet.get("current_pressure"))
        visible = [
            cls._clean_fact(item)
            for item in (packet.get("visible_elements") or [])
            if cls.is_player_facing_fact(item)
            and not str(item).startswith(("地点：", "在场英雄："))
        ]
        public = [
            cls._clean_fact(item)
            for item in (packet.get("public_facts") or [])
            if cls.is_player_facing_fact(item)
        ]
        if not cls.is_player_facing_fact(premise):
            premise = ""
        if not cls.is_player_facing_fact(pressure):
            pressure = ""
        sentences: list[str] = []
        for fact in [*visible[:2], *public[:1], pressure, premise]:
            if not fact or fact in sentences or any(fact in prior or prior in fact for prior in sentences):
                continue
            sentences.append(fact)
            if len(sentences) >= 3:
                break
        if sentences:
            return "".join(cls._sentence(item) for item in sentences)
        return cls._sentence(f"众人的注意力重新落回{location}") if location else ""

    @classmethod
    def recap(cls, packet: dict[str, object]) -> str:
        """Summarize only the already-public live state after a reconnect."""

        location = str(packet.get("location") or packet.get("scene_name") or "").strip()
        # A frame may inherit public continuity from a nearby prior scene. A
        # reconnect recap is the live camera, not a campaign summary: take at
        # most the newest fact and newest beat from their separate channels so
        # two old beats cannot crowd out the current location.
        public_facts = list(packet.get("public_facts") or [])
        recent_beats = list(packet.get("recent_beats") or [])
        consequences = list(packet.get("committed_consequences") or [])
        candidates = [
            *(public_facts[-1:] if public_facts else consequences[-1:]),
            *recent_beats[-1:],
        ]
        facts: list[str] = []
        for value in candidates:
            fact = cls._clean_fact(value)
            if not cls.is_player_facing_fact(fact):
                continue
            if fact.startswith(("可见势力痕迹：", "相关地点：", "在场英雄：", "地点：")):
                continue
            if fact in facts or any(fact in prior or prior in fact for prior in facts):
                continue
            facts.append(fact)
            if len(facts) >= 2:
                break
        sentences = [f"众人仍在{location}" if location else "", *facts]
        return "".join(cls._sentence(item) for item in sentences if item)

    @classmethod
    def beat_needs_fallback(
        cls,
        reply: str,
        instruction: str,
        packet: dict[str, object] | None = None,
    ) -> bool:
        text = str(reply or "").strip()
        if not text:
            return True
        waiting_only = any(
            marker in text
            for marker in ("像是在等", "仍在等待", "尚未决定", "没有立刻表态", "等你们开口", "等待你们")
        )
        concrete_response = bool(
            re.search(r"[：:][‘'\"“「『][^’'\"”」』\n]{2,}", text)
            or cls.has_committed_change(text)
        )
        if waiting_only and not concrete_response:
            return True
        if "明确回应" in str(instruction or "") and not concrete_response:
            return True
        return bool(packet and cls.only_restates_packet(text, packet))

    @staticmethod
    def has_committed_change(text: str) -> bool:
        """Best-effort local guard; semantic extraction remains authoritative."""

        clean = " ".join(str(text or "").split())
        if not clean:
            return False
        if re.search(r"(?:即将|就要|准备|试图|正(?:在)?(?:逼近|靠近|撞|冲|撬|施法)|最后(?:一次)?警告)", clean):
            # A sentence may contain both a warning and a later completed
            # consequence; completed markers below may still prove a commit.
            warning_only = True
        else:
            warning_only = False
        completed = bool(
            re.search(
                r"(?:已经|终于|当场|随即|立刻|猛地|应声)(?:.{0,14})"
                r"(?:答应|拒绝|决定|交出|放下|打开|关上|封死|撞破|砸开|冲入|闯入|倒下|熄灭|碎裂|坍塌|夺走|带走)",
                clean,
            )
            or re.search(r"(?:门|闸|窗|墙|桥|灯|法阵|风铃).{0,8}(?:开了|关了|破了|碎了|塌了|熄灭了|被封死)", clean)
            # A newly exposed inscription, mark, or object is an already
            # observable change, not merely atmosphere. Keep this narrow so
            # "the wind chime rings" and other mood descriptions remain
            # non-committal.
            or re.search(
                r"(?:风铃|铃舌|钟舌|门缝|壁龛|地面|纸页|旧路图|账册|封蜡|石碑|树皮|机关|箱子)"
                r".{0,12}(?:露出|显出|浮出|显现|翻出|掉出|被掀开|被揭开|出现)"
                r".{0,28}(?:刻字|字迹|名字|暗记|图案|线索|裂纹|钥匙|纸条|标记)",
                clean,
            )
            or any(marker in clean for marker in ("明确答应", "明确拒绝", "提出条件", "下令封锁", "命人封住"))
        )
        return completed or (not warning_only and any(marker in clean for marker in ("答应了", "拒绝了", "放行了", "冲进来")))

    @classmethod
    def has_irreversible_consequence(cls, text: str) -> bool:
        """Local availability fallback for an explicit situation commit."""

        clean = " ".join(str(text or "").split())
        if not cls.has_committed_change(clean):
            return False
        return bool(
            re.search(
                r"撞破|砸开|闯入|冲进|封死|折断|碎裂|坍塌|燃起|熄灭|"
                r"夺走|带走|倒下|离场|逃走|押走|沉没|决堤|被俘|已经包围",
                clean,
            )
        )

    @staticmethod
    def only_restates_packet(reply: str, packet: dict[str, object]) -> bool:
        def normalize(value: object) -> str:
            text = re.sub(r"^(?:现场人物|当前压力|前提|地点|已公开事实)[：:]", "", str(value or "").strip())
            return re.sub(r"[\s，,。；;：:‘’'\"“”「」『』【】]", "", text)

        seed_values: list[str] = []
        committed_seed_values: list[str] = []
        for key in (
            "premise",
            "mission_anchor",
            "current_pressure",
            "visible_elements",
            "public_facts",
            "committed_consequences",
            "revealed_clues",
            "recent_beats",
        ):
            value = packet.get(key)
            if isinstance(value, list):
                normalized_items = [normalize(item) for item in value if normalize(item)]
                seed_values.extend(normalized_items)
                if key in {"public_facts", "committed_consequences", "recent_beats"}:
                    committed_seed_values.extend(normalized_items)
            else:
                normalized = normalize(value)
                if normalized:
                    seed_values.append(normalized)
        reply_sentences = [normalize(item) for item in re.split(r"[。！？!?\n]+", reply) if normalize(item)]
        if not reply_sentences or not seed_values:
            return False
        return all(
            any(
                sentence in seed
                or seed in sentence
                or SequenceMatcher(None, sentence, seed).ratio() >= 0.86
                for seed in seed_values
            )
            or any(
                SceneMomentPolicy._same_committed_state(sentence, seed)
                for seed in committed_seed_values
            )
            for sentence in reply_sentences
        )

    @staticmethod
    def restates_recent_public_text(
        reply: str,
        recent_texts: list[object],
    ) -> bool:
        """Catch a table nudge that paraphrases a recently delivered line.

        Table nudges are allowed to joke about a die or say that the GM is
        waiting, but they have no fiction-writing authority.  This comparison
        therefore uses only recent GM text and requires substantial character
        overlap; a short shared noun such as ``牢门`` is not enough.
        """

        def normalize(value: object) -> str:
            return re.sub(
                r"[^0-9A-Za-z\u4e00-\u9fff]+|(?:已经|正在|仍然|仍|又|再|的|了|着|被|正)",
                "",
                str(value or "").casefold(),
            )

        def bigrams(value: str) -> set[str]:
            return {
                value[index : index + 2]
                for index in range(max(0, len(value) - 1))
            }

        reply_parts = [
            normalize(item)
            for item in re.split(r"[。！？!?；;\n]+", str(reply or ""))
            if normalize(item)
        ]
        recent_parts = [
            normalize(item)
            for text in recent_texts
            for item in re.split(r"[。！？!?；;\n]+", str(text or ""))
            if normalize(item)
        ]
        for candidate in reply_parts:
            if len(candidate) < 6:
                continue
            candidate_bigrams = bigrams(candidate)
            for prior in recent_parts:
                if len(prior) < 6:
                    continue
                if candidate in prior or prior in candidate:
                    return True
                if SequenceMatcher(None, candidate, prior).ratio() >= 0.72:
                    return True
                shared = candidate_bigrams.intersection(bigrams(prior))
                if len(shared) >= 6 and (
                    len(shared)
                    / max(1, min(len(candidate_bigrams), len(bigrams(prior))))
                    >= 0.40
                ):
                    return True
        return False

    @classmethod
    def _same_committed_state(cls, left: str, right: str) -> bool:
        """Catch a reworded state that was already delivered at the table."""

        left_states = cls._committed_state_markers(left)
        right_states = cls._committed_state_markers(right)
        if not left_states or not left_states.intersection(right_states):
            return False
        left_scope = cls._state_scope_bigrams(left)
        right_scope = cls._state_scope_bigrams(right)
        if not left_scope or not right_scope:
            return False
        shared = left_scope.intersection(right_scope)
        return len(shared) >= 2 and (
            len(shared) / max(1, min(len(left_scope), len(right_scope))) >= 0.08
        )

    @classmethod
    def _committed_state_markers(cls, value: object) -> set[str]:
        text = str(value or "")
        return {
            label
            for label, patterns in cls._COMMITTED_STATE_PATTERNS.items()
            if any(re.search(pattern, text) for pattern in patterns)
        }

    @classmethod
    def _state_scope_bigrams(cls, value: object) -> set[str]:
        text = cls._STATE_WORDS.sub("", str(value or ""))
        text = re.sub(
            r"[^0-9A-Za-z\u4e00-\u9fff]+|(?:已经|正在|仍然|仍|又|再|的|了|着|被|正)",
            "",
            text,
        )
        return {text[index : index + 2] for index in range(max(0, len(text) - 1))}

    @staticmethod
    def ensure_complete_present_character_list(reply: str, packet: dict[str, object]) -> str:
        present = [
            str(item).split("：", 1)[1].strip()
            for item in packet.get("visible_elements", [])
            if str(item).startswith("在场英雄：") and "：" in str(item)
        ]
        present = list(dict.fromkeys(name for name in present if name))
        text = str(reply or "")
        mentioned = [name for name in present if name in text]
        if len(present) < 2 or not mentioned or len(mentioned) == len(present) or "都在场" not in text:
            return text
        marker_index = text.find("都在场")
        prefix = text[:marker_index]
        sentence_start = max(prefix.rfind(mark) for mark in ("。", "！", "？", "\n")) + 1
        clause = prefix[sentence_start:]
        name_offsets = [clause.find(name) for name in mentioned if name in clause]
        if not name_offsets:
            return text
        list_start = sentence_start + min(name_offsets)
        return f"{text[:list_start]}{'、'.join(present)}{text[marker_index:]}"

    @staticmethod
    def _clean_fact(value: object) -> str:
        text = " ".join(str(value or "").split()).strip()
        return re.sub(r"^(?:现场|现场人物|当前压力|前提|已公开事实)[：:]\s*", "", text)

    @staticmethod
    def _sentence(text: str) -> str:
        clean = str(text or "").strip()
        return clean if not clean or clean[-1] in "。！？!?" else clean + "。"
