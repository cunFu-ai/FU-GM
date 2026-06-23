from __future__ import annotations

from dataclasses import dataclass, field

from fu_gm.models import WorldCreationProfile
from fu_gm.safety_parser import extract_safety_declarations


@dataclass
class PreSessionConsensusResponse:
    message: str
    accepted_facts: list[str] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)
    ready_to_start_session_zero: bool = False


class PreSessionConsensusFacilitator:
    """开团前共识引导。

    这一层故意不替玩家决定世界设定，只负责让桌面先对齐：
    基调、主题、队伍分歧处理、描述尺度、界限与帷幕。
    """

    def __init__(self, *, gm_name: str = "时悠") -> None:
        self.gm_name = gm_name

    def opening(self) -> PreSessionConsensusResponse:
        questions = [
            "大家希望这次故事更偏严肃正剧、王道英雄幻想，还是介于两者之间？",
            "战斗和场景描述想更像夸张动漫演出，还是更像英雄传说？",
            "有没有想探索的主题，以及不想碰触或希望淡出的内容？可以群里说，也可以私聊我匿名记录。",
        ]
        message = (
            f"我是{self.gm_name}，先不急着开第零章。开团前我们先把桌面的共识对齐一下，"
            "这样后面的世界创建会更自由，也不会把谁不舒服的内容踩进剧情里。\n"
            "不用按问卷答，随便聊画面、氛围、雷点、想看的桥段都可以。我会把大家说出的共识记下来。"
        )
        return PreSessionConsensusResponse(message=message, questions=questions)

    def handle(self, profile: WorldCreationProfile, speaker: str, message: str) -> PreSessionConsensusResponse:
        text = str(message or "").strip()
        accepted: list[str] = []
        if not text:
            return PreSessionConsensusResponse(message="我在，等大家慢慢补想法就好。", questions=self._next_questions(profile))

        self._extract_safety(profile, speaker, text, accepted)
        self._extract_tone(profile, speaker, text, accepted)
        self._extract_playstyle_themes(profile, speaker, text, accepted)
        self._extract_party_dynamic(profile, speaker, text, accepted)
        self._extract_description_style(profile, speaker, text, accepted)
        self._extract_content_guidelines(profile, speaker, text, accepted)

        if self._wants_session_zero(text):
            if self._has_minimum_consensus(profile):
                profile.pre_session_ready = True
                return PreSessionConsensusResponse(
                    message="共识我记好了。那我们正式开启第零章，开始一起创造世界吧。",
                    accepted_facts=accepted,
                    ready_to_start_session_zero=True,
                )
            questions = self._next_questions(profile)
            return PreSessionConsensusResponse(
                message="可以开，但我想先补齐一两条桌面共识，免得后面世界创建跑偏：" + "；".join(questions),
                accepted_facts=accepted,
                questions=questions,
            )

        questions = self._next_questions(profile)
        ready = self._has_minimum_consensus(profile)
        if ready:
            profile.pre_session_ready = True
            message_out = (
                "目前的开团共识已经够稳了。"
                "如果大家都点头，就直接说“开启第零章”或“开始世界创建”，我会把当前阶段切到第零章。"
            )
        elif accepted:
            message_out = "记下来了：" + "；".join(accepted[:4])
        else:
            message_out = "这更像桌边闲聊，我先不硬塞进设定。"

        return PreSessionConsensusResponse(
            message=message_out,
            accepted_facts=accepted,
            questions=questions,
            ready_to_start_session_zero=False,
        )

    def is_substantive(self, message: str) -> bool:
        text = str(message or "").strip()
        if not text:
            return False
        if self._wants_session_zero(text):
            return True
        if len(text) <= 4 and text in {"哈哈", "哈哈哈", "笑死", "好耶", "草", "？", "??", "。"}:
            return False
        probes = (
            "严肃",
            "轻松",
            "王道",
            "正剧",
            "黑暗",
            "希望",
            "主题",
            "风格",
            "基调",
            "动漫",
            "传说",
            "英雄",
            "队伍",
            "挚友",
            "分歧",
            "冲突",
            "不要",
            "不希望",
            "不想",
            "淡出",
            "帷幕",
            "界限",
            "血腥",
            "暴力",
            "恋爱",
            "亲密",
            "帝国",
            "迫害",
            "精神控制",
            "都可以",
            "没雷",
            "没有雷",
        )
        return any(token in text for token in probes)

    def _extract_safety(self, profile: WorldCreationProfile, speaker: str, text: str, accepted: list[str]) -> None:
        for kind, item in extract_safety_declarations(text):
            target = profile.safety_lines if kind == "line" else profile.safety_veils
            if item not in target:
                target.append(item)
                accepted.append(f"{speaker} 声明{'界限' if kind == 'line' else '帷幕'}：{item}")
        if any(token in text for token in ("没有雷", "没雷", "都可以", "暂无雷点", "没有特别不接受")):
            note = f"{speaker} 暂未声明额外界限与帷幕，但保留之后随时补充的权利。"
            self._append_unique(profile.consensus_notes, note)
            accepted.append(note)

    def _extract_tone(self, profile: WorldCreationProfile, speaker: str, text: str, accepted: list[str]) -> None:
        tone = ""
        if any(token in text for token in ("严肃", "正剧", "沉重", "复杂", "政治", "悲剧")):
            tone = "偏严肃正剧，允许复杂情绪和较沉重处境。"
        if any(token in text for token in ("王道", "轻松", "善恶分明", "热血", "希望", "友情", "冒险")):
            tone = "偏王道英雄幻想，强调希望、友情和冒险感。"
        if any(token in text for token in ("地下城", "宝箱", "奇遇", "寻宝", "迷宫")):
            tone = "保留地下城、宝箱、奇遇和探索奖励的经典冒险味。"
        if tone:
            fact = f"{speaker} 倾向：{tone}"
            self._append_unique(profile.tone_preferences, fact)
            accepted.append(fact)

    def _extract_playstyle_themes(self, profile: WorldCreationProfile, speaker: str, text: str, accepted: list[str]) -> None:
        theme_hits = []
        for token in ("希望", "友情", "成长", "救赎", "复仇", "自由", "反抗", "发现", "牺牲", "羁绊", "谜团"):
            if token in text:
                theme_hits.append(token)
        if "主题" in text or theme_hits:
            fact = f"{speaker} 想探索的主题：" + ("、".join(dict.fromkeys(theme_hits)) if theme_hits else text)
            self._append_unique(profile.playstyle_themes, fact)
            accepted.append(fact)

    def _extract_party_dynamic(self, profile: WorldCreationProfile, speaker: str, text: str, accepted: list[str]) -> None:
        dynamic = ""
        if any(token in text for token in ("挚友", "老朋友", "一开始就认识", "已经认识")):
            dynamic = "英雄们可以在开局前已有联系或信任基础。"
        if any(token in text for token in ("陌生人", "萍水相逢", "刚认识")):
            dynamic = "英雄们可以从陌生人或临时同路人开始。"
        if any(token in text for token in ("分歧", "内部冲突", "争执", "理念不合")):
            dynamic = "允许角色间出现理念分歧，但需要以玩家共识和合作叙事解决。"
        if any(token in text for token in ("不要内斗", "不想内斗", "不背刺", "别背刺")):
            dynamic = "不鼓励队内背刺或破坏合作，分歧应以桌面共识为边界。"
        if dynamic:
            profile.party_dynamic = dynamic
            accepted.append(f"{speaker} 的队伍关系共识：{dynamic}")

    def _extract_description_style(self, profile: WorldCreationProfile, speaker: str, text: str, accepted: list[str]) -> None:
        style = ""
        wants_anime = any(token in text for token in ("动漫", "夸张", "JRPG", "演出", "必杀", "帅一点"))
        wants_epic = any(token in text for token in ("传说", "英雄主义", "史诗", "神话"))
        wants_restrained = any(token in text for token in ("克制", "不要太夸张", "朴素"))
        if wants_anime and wants_epic and not wants_restrained:
            style = "偏夸张动漫/JRPG式演出，同时保留英雄传说般的史诗感。"
        elif wants_anime:
            style = "偏夸张动漫/JRPG式演出，允许帅气分镜和大招感。"
        elif wants_epic:
            style = "偏英雄传说/史诗式演绎，强调庄严感和命运感。"
        if wants_restrained:
            style = "描述更克制，保留英雄主义但不过度浮夸。"
        if style:
            profile.description_style = style
            accepted.append(f"{speaker} 的描述风格偏好：{style}")

    def _extract_content_guidelines(self, profile: WorldCreationProfile, speaker: str, text: str, accepted: list[str]) -> None:
        if any(token in text for token in ("不要血腥", "血腥淡化", "暴力淡化", "少血腥", "不细写伤口")):
            profile.violence_guideline = "暴力与死亡采用日式RPG式淡化表现，不细写血腥伤口。"
            accepted.append(f"{speaker} 的暴力描写尺度：{profile.violence_guideline}")
        elif any(token in text for token in ("可以血腥", "血腥可以", "战斗残酷")):
            profile.violence_guideline = "可以表现战斗残酷感，但仍避免越过界限与帷幕。"
            accepted.append(f"{speaker} 的暴力描写尺度：{profile.violence_guideline}")

        evil_hits = []
        for token in ("精神控制", "迫害", "帝国主义", "弱势者", "奴役", "歧视", "宗教压迫"):
            if token in text:
                evil_hits.append(token)
        if evil_hits:
            fact = f"{speaker} 提到邪恶/压迫相关尺度需要留意：" + "、".join(dict.fromkeys(evil_hits))
            self._append_unique(profile.evil_guidelines, fact)
            accepted.append(fact)

        if any(token in text for token in ("不要恋爱", "不想恋爱", "恋爱淡出", "亲密淡出", "拉灯")):
            profile.romance_guideline = "浪漫与亲密关系可淡出或不作为主轴，亲密内容不正面描写。"
            accepted.append(f"{speaker} 的浪漫/亲密尺度：{profile.romance_guideline}")
        elif any(token in text for token in ("恋爱可以", "暧昧可以", "感情线可以")):
            profile.romance_guideline = "允许浪漫或暧昧剧情，但仍遵守所有玩家的界限与帷幕。"
            accepted.append(f"{speaker} 的浪漫/亲密尺度：{profile.romance_guideline}")

    def _next_questions(self, profile: WorldCreationProfile) -> list[str]:
        questions: list[str] = []
        if not profile.tone_preferences:
            questions.append("这次故事想偏严肃正剧、王道冒险，还是混合？")
        if not profile.description_style:
            questions.append("描述风格想偏动漫夸张，还是偏英雄传说？")
        if not profile.party_dynamic:
            questions.append("英雄们开局是熟人、陌生人，还是允许带一点理念分歧？")
        if not (profile.safety_lines or profile.safety_veils or profile.consensus_notes or profile.violence_guideline or profile.romance_guideline):
            questions.append("有没有界限与帷幕？也可以私聊我匿名记录。")
        return questions[:3]

    def _has_minimum_consensus(self, profile: WorldCreationProfile) -> bool:
        has_safety = bool(
            profile.safety_lines
            or profile.safety_veils
            or profile.consensus_notes
            or profile.violence_guideline
            or profile.romance_guideline
        )
        return bool(profile.tone_preferences and profile.description_style and profile.party_dynamic and has_safety)

    def _wants_session_zero(self, text: str) -> bool:
        return any(
            token in text
            for token in (
                "开启第零章",
                "开始第零章",
                "进入第零章",
                "开始世界创建",
                "开启世界创建",
                "进入世界创建",
            )
        )

    def _append_unique(self, target: list[str], item: str) -> None:
        if item and item not in target:
            target.append(item)
