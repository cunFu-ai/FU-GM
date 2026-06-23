from __future__ import annotations

from collections import Counter
from copy import deepcopy

from fu_gm.gm_guidance import build_gm_guidance
from fu_gm.models import FirstActCandidate, FirstActVoteResult, ProloguePrompt, WorldCreationProfile


PROLOGUE_PROMPTS: dict[str, list[ProloguePrompt]] = {
    "命运的相会": [
        ProloguePrompt(
            group_key="命运的相会",
            option=1,
            title="商队遇袭",
            premise="你们是萍水相逢的旅伴，此时正好搭乘同一辆交通工具或跟随同一支商队。突然，你们遭到了袭击。",
            questions=["你们为什么要跟这群人一起旅行？", "是谁或什么袭击了你们？", "袭击者的目的是什么？"],
            tags=["旅行", "突袭", "临时同盟"],
        ),
        ProloguePrompt(
            group_key="命运的相会",
            option=2,
            title="强者召见",
            premise="一名掌握大权或拥有强大力量的人物决定召见你们。",
            questions=["是谁想要召见你们？", "为什么你们会被选中？", "这次召唤是否违背了你们的意愿？"],
            tags=["召唤", "权力者", "使命"],
        ),
        ProloguePrompt(
            group_key="命运的相会",
            option=3,
            title="战场异变",
            premise="惨烈的战斗之后，某种未知的凶恶事物出现在战场之上，交战双方的英雄们必须联手对抗它。",
            questions=["你们所属的阵营是否相互敌对？", "你们是否曾遭遇过这个存在？", "你们是否会合作抗敌？"],
            tags=["战场", "共同敌人", "危机"],
        ),
        ProloguePrompt(
            group_key="命运的相会",
            option=4,
            title="明日处刑",
            premise="你们被关押在一座监狱或地牢里，明天就是处刑的日子。",
            questions=["你们为什么会被关起来？", "你们是无辜的还是有罪的？", "你们能独自逃离吗，还是需要他人的帮助？"],
            tags=["监狱", "逃亡", "倒计时"],
        ),
        ProloguePrompt(
            group_key="命运的相会",
            option=5,
            title="宝物与圈套",
            premise="你们中有部分人打算偷走某个人或某件珍贵的东西，其他人则被雇来保护这件宝物。此时，诡异的事发生了。",
            questions=["是谁雇了你们？", "那件宝物是什么人或什么东西？", "这是否是事先设计的圈套？"],
            tags=["宝物", "偷窃", "双阵营"],
        ),
        ProloguePrompt(
            group_key="命运的相会",
            option=6,
            title="奇异现象调查",
            premise="来自各个国家和组织的成员纷纷对某种奇异现象展开调查，你们也在这些人当中。",
            questions=["你们为什么会来到这里？", "是其他人派你们来的吗？", "你们对这种现象有怎样的理论或信念？"],
            tags=["调查", "异象", "多势力"],
        ),
    ],
    "守护者": [
        ProloguePrompt(
            group_key="守护者",
            option=1,
            title="庆典中的黑暗",
            premise="就在你们启程前夕的庆典上，一股黑暗力量突然现出真身，天选之人的生命受到威胁。",
            questions=["这股黑暗力量是什么？", "为什么天选之人会无人保护？", "是不是有谁背叛了你们？"],
            tags=["庆典", "黑暗力量", "背叛"],
        ),
        ProloguePrompt(
            group_key="守护者",
            option=2,
            title="朝圣路上的袭击",
            premise="你们正进行漫长的朝圣之旅，在前往第一个目的地的路上遭到袭击，显然有人想终结旅程。",
            questions=["你们的目的地是哪里，为什么要去那里？", "是什么人或什么东西攻击了你们？"],
            tags=["朝圣", "护送", "袭击"],
        ),
        ProloguePrompt(
            group_key="守护者",
            option=3,
            title="山脉下的古隧道",
            premise="你们站在千百年历史的隧道网络入口，这条黑暗而危险的道路将引领你们穿过山脉。",
            questions=["山脉之后有什么东西？", "你们为何必须走这条路？", "你们对挖出这些隧道的人是否有所了解？"],
            tags=["地下城", "古代隧道", "旅途"],
        ),
        ProloguePrompt(
            group_key="守护者",
            option=4,
            title="富人府邸的传言",
            premise="你们正在一位富人的府上作客，此人是天选之人的多年好友，但你们听到了关于东道主的可疑传言。",
            questions=["谁值得你们信任？", "东道主是什么身份，传言如何描述此人？", "是谁提供了这个信息？"],
            tags=["社交", "传言", "信任"],
        ),
        ProloguePrompt(
            group_key="守护者",
            option=5,
            title="错失的任务",
            premise="就在你们即将完成任务时，某个强大存在妨碍了你们，机会就此错过，你们必须为保护天选之人而战。",
            questions=["那个强大的存在是什么，它为何在那里？", "你们是否遭到背叛？", "谁能帮你们另辟蹊径？"],
            tags=["失败代价", "强敌", "保护"],
        ),
        ProloguePrompt(
            group_key="守护者",
            option=6,
            title="需要天选之人的村镇",
            premise="某个村庄或城镇的人们需要帮助，天选之人或许是能解救他们的唯一人选。",
            questions=["这些人受到怎样的威胁？", "为什么只有天选之人能帮助他们？", "你们是否应该专注原本使命？"],
            tags=["村镇", "救援", "使命冲突"],
        ),
    ],
    "抗争的英雄": [
        ProloguePrompt("抗争的英雄", 1, "燃烧的最后希望", "你们是某个村子或城镇的最后希望，但敌人太强大，大火和浓烟直冲天际。", ["是什么袭击了城镇？", "是否有你们认识的人住在这里？", "你们要如何拯救无辜者？"], ["城镇", "救援", "压迫"]),
        ProloguePrompt("抗争的英雄", 2, "简单任务变成包围", "这本该是一场简单任务，但眼下你们被敌人重重包围。", ["这里是什么地方？", "你们来这里执行什么任务？", "是否有谁背叛了你们？"], ["任务", "包围", "背叛"]),
        ProloguePrompt("抗争的英雄", 3, "臭名官员的演讲", "你们正在参加某位臭名昭著官员的演讲，周围聚集了很多人，人群中混杂着武装士兵。", ["这个官员是谁？", "他演讲的内容是什么？", "他是潜在盟友，还是更糟的威胁？"], ["演讲", "群众", "官员"]),
        ProloguePrompt("抗争的英雄", 4, "惨败之后", "你们刚刚经历一场惨败，现在身心俱疲、希望渺茫。", ["当时发生了什么？", "为何敌人如此强大？", "你们现在有什么计划？"], ["失败", "低谷", "复起"]),
        ProloguePrompt("抗争的英雄", 5, "影响力人物的会面", "你们设法取得与某个有影响力人物会面的机会。", ["那个人是谁？", "他们能如何帮助你们的事业？", "你们从谁那里得知此人的名字？"], ["会面", "盟友", "交涉"]),
        ProloguePrompt("抗争的英雄", 6, "明日防线", "你们正在帮助某个小村子组织防御，敌人明天就要打过来了。", ["为什么敌人会攻击这里？", "他们想得到什么？", "村民是否有能力与他们抗衡？"], ["防御", "倒计时", "村庄"]),
    ],
    "革命者": [
        ProloguePrompt("革命者", 1, "秘密会面", "你们即将与某个潜在盟友进行秘密会面，交谈时间所剩无几，还必须避免被发现。", ["与你们会面的人是谁？", "他能如何帮助你们的事业？", "你们从谁那里得知此人的名字？"], ["潜入", "盟友", "秘密"]),
        ProloguePrompt("革命者", 2, "官员公开演讲", "某位高级官员正在数名士兵护卫下发表公开演讲，你们混迹于人群当中。", ["你们来此的目的是什么？", "你们是否曾与这名官员打过交道？"], ["演讲", "群众", "政治"]),
        ProloguePrompt("革命者", 3, "秘密基地暴露", "当权者发现了你们的秘密基地。现在你们必须选择留下战斗，或立刻逃走。", ["他们是如何找到这里的？", "是否有人背叛了你们？"], ["基地", "追捕", "抉择"]),
        ProloguePrompt("革命者", 4, "偷来的黑暗秘密", "你们刚偷到一件珍贵物品，原以为它能帮助反抗事业，却发现它会带来恶劣后果。", ["你们偷了什么？", "它隐藏着怎样的黑暗秘密？", "谁会不惜一切手段夺回它？"], ["宝物", "黑暗秘密", "追逐"]),
        ProloguePrompt("革命者", 5, "敌方秘密设施", "你们掌握了某处敌方秘密设施的位置，这是个绝佳机会。", ["那是个什么样的设施？", "为何如此重要？", "是什么人或东西在守卫这里？"], ["设施", "突袭", "情报"]),
        ProloguePrompt("革命者", 6, "目标行动出错", "你们打算消灭某个重要目标，却在行动时遇到严重意外，变化令你们身陷险境。", ["你们的任务是什么？", "是谁提供的情报？", "这会是个陷阱吗？"], ["刺杀", "陷阱", "危机"]),
    ],
    "探寻者": [
        ProloguePrompt("探寻者", 1, "前往圣地", "你们正在前往某处圣地或魔法之地，希望在那里找到答案。", ["那是什么地方？", "它会给你们带来怎样的帮助？", "你们是否曾到过那里？"], ["旅途", "圣地", "答案"]),
        ProloguePrompt("探寻者", 2, "古森林边缘", "你们来到古老森林边缘，危险生物栖息其中，但留给你们的时间已经不多了。", ["森林深处藏着什么宝藏？", "传言中是什么守卫宝藏？", "为什么你们急需找到宝藏？"], ["森林", "宝藏", "守卫"]),
        ProloguePrompt("探寻者", 3, "被腐化的避难所", "你们原以为能在此找到安全避难所和下个目的地的线索，却发现这里早已被腐化。", ["这是什么地方？", "它被什么黑暗力量腐蚀？", "你们是否踏入了陷阱？"], ["腐化", "逃生", "线索"]),
        ProloguePrompt("探寻者", 4, "被看守的宝物", "征途将从寻找某件珍贵物品或原料开始，然而强大存在正看守着它。", ["你们在寻找什么？", "它被存放在哪里？", "是什么在守护它，为何受到看管？"], ["宝物", "守护者", "地下城"]),
        ProloguePrompt("探寻者", 5, "酒馆里的坏消息", "深夜里，你们正在温暖酒馆讨论下一步计划，突然有人匆匆走来报告了可怕消息。", ["你们原本的计划是什么？", "什么变故令你们无法继续前进？", "是敌人终于开始行动了吗？"], ["酒馆", "坏消息", "转折"]),
        ProloguePrompt("探寻者", 6, "睿智者的代价", "你们设法取得与某位睿智重要人物会面的机会。", ["那个人是谁？", "他们能对任务起到怎样帮助？", "他们会要求什么作为建议的代价？"], ["导师", "代价", "交涉"]),
    ],
}


class PrologueManager:
    """根据小队原型生成第一幕候选，并记录玩家投票结果。"""

    def prompt_for_group(self, group_concept: str) -> str:
        text = group_concept.strip()
        if any(token in text for token in ("革命", "起义", "推翻", "反抗财阀", "反抗帝国")):
            return "革命者"
        if any(token in text for token in ("反抗", "抗争", "解放", "抵抗")):
            return "抗争的英雄"
        if any(token in text for token in ("守护", "护送", "神器", "封印", "天选")):
            return "守护者"
        if any(token in text for token in ("探寻", "探索", "宝藏", "地下城", "遗失", "旅行", "冒险")):
            return "探寻者"
        return "命运的相会"

    def generate_candidates(
        self,
        world: WorldCreationProfile,
        *,
        count: int = 3,
        options: list[int] | None = None,
    ) -> list[FirstActCandidate]:
        group_key = self.prompt_for_group(world.group_concept)
        prompts = PROLOGUE_PROMPTS[group_key]
        selected_prompts = self._select_prompts(prompts, count=count, options=options)
        candidates: list[FirstActCandidate] = []
        for index, prompt in enumerate(selected_prompts, start=1):
            candidate = FirstActCandidate(
                candidate_id=f"first_act_{index}",
                title=prompt.title,
                group_key=prompt.group_key,
                option=prompt.option,
                premise=self._personalize_premise(prompt.premise, world),
                questions=list(prompt.questions),
                suggested_bonds=self.suggest_starting_bonds(world, prompt),
                notes=self._candidate_notes(world, prompt),
            )
            candidates.append(candidate)
        return candidates

    def record_vote(self, world: WorldCreationProfile, voter: str, candidate_id: str) -> FirstActVoteResult:
        clean_voter = voter.strip()
        clean_candidate = self.resolve_candidate_id(world, candidate_id)
        if clean_voter and clean_candidate:
            world.first_act_votes[clean_voter] = clean_candidate
            self._sync_candidate_votes(world)
        return self.vote_result(world)

    def confirm_winner(self, world: WorldCreationProfile, candidate_id: str = "") -> FirstActVoteResult:
        resolved = self.resolve_candidate_id(world, candidate_id)
        result = self.vote_result(world)
        if not resolved and result.winner is not None:
            resolved = result.winner.candidate_id
        winner = self._candidate_by_id(world, resolved)
        if winner is None:
            return result
        world.selected_first_act_id = winner.candidate_id
        world.selected_first_act_summary = self._summary_for_candidate(winner)
        world.starting_bond_suggestions = list(winner.suggested_bonds)
        return self.vote_result(world)

    def vote_result(self, world: WorldCreationProfile) -> FirstActVoteResult:
        self._sync_candidate_votes(world)
        counts = dict(Counter(world.first_act_votes.values()))
        winner = self._winner(world, counts)
        summary = "尚未选择第一幕候选。"
        if winner is not None:
            summary = self._summary_for_candidate(winner)
        return FirstActVoteResult(
            winner=deepcopy(winner) if winner is not None else None,
            candidates=deepcopy(world.first_act_candidates),
            vote_counts=counts,
            summary=summary,
        )

    def resolve_candidate_id(self, world: WorldCreationProfile, value: str) -> str:
        text = str(value).strip()
        if not text:
            return ""
        if text in {candidate.candidate_id for candidate in world.first_act_candidates}:
            return text
        if text.isdigit():
            index = int(text) - 1
            if 0 <= index < len(world.first_act_candidates):
                return world.first_act_candidates[index].candidate_id
        for candidate in world.first_act_candidates:
            if text in candidate.title or candidate.title in text:
                return candidate.candidate_id
        return ""

    def format_candidates(self, candidates: list[FirstActCandidate]) -> str:
        lines: list[str] = []
        for index, candidate in enumerate(candidates, start=1):
            questions = "；".join(candidate.questions[:3])
            lines.append(f"{index}. 【{candidate.title}】{candidate.premise} 关键问题：{questions}")
        return "\n".join(lines)

    def suggest_starting_bonds(self, world: WorldCreationProfile, prompt: ProloguePrompt) -> list[str]:
        suggestions: list[str] = []
        if world.villain_seeds:
            suggestions.append(f"对首个反派种子建立【仇恨】或【不信任】羁绊：{world.villain_seeds[0]}")
        if world.factions:
            faction = next(iter(world.factions))
            emotion = "忠诚" if prompt.group_key in {"守护者", "革命者"} else "钦佩或不信任"
            suggestions.append(f"对阵营【{faction}】建立一段【{emotion}】羁绊。")
        if prompt.group_key == "命运的相会":
            suggestions.append("对另一名同行英雄建立【信赖】或【猜忌】羁绊，体现临时同盟的张力。")
        if prompt.group_key == "守护者":
            suggestions.append("对守护对象或天选之人建立【忠诚】羁绊。")
        return self._dedupe(suggestions)

    def _select_prompts(
        self,
        prompts: list[ProloguePrompt],
        *,
        count: int,
        options: list[int] | None,
    ) -> list[ProloguePrompt]:
        if options:
            selected = [prompt for option in options for prompt in prompts if prompt.option == option]
            if selected:
                return selected[:count]
        return prompts[: max(1, count)]

    def _personalize_premise(self, premise: str, world: WorldCreationProfile) -> str:
        anchors: list[str] = []
        if world.starting_region:
            anchors.append(f"起点可放在【{world.starting_region}】")
        if world.major_locations:
            anchors.append(f"镜头可扫过【{next(iter(world.major_locations))}】")
        if world.factions:
            anchors.append(f"牵涉阵营【{next(iter(world.factions))}】")
        if not anchors:
            return premise
        return f"{premise}（{'; '.join(anchors)}。）"

    def _candidate_notes(self, world: WorldCreationProfile, prompt: ProloguePrompt) -> list[str]:
        notes = [f"适配小队原型：{prompt.group_key}", f"标签：{'、'.join(prompt.tags)}"]
        guidance = build_gm_guidance(world)
        if guidance.location_seeds:
            seed = guidance.location_seeds[0]
            notes.append(f"预备地点灵感：{seed.name}（{seed.archetype}）")
        if guidance.questions:
            notes.append(f"GM追问角度：{guidance.questions[0]}")
        if world.mysteries:
            notes.append(f"可顺手埋入谜团：{world.mysteries[0]}")
        if world.villain_mirrors:
            notes.append(f"反派映照：{world.villain_mirrors[0]}")
        return notes

    def _sync_candidate_votes(self, world: WorldCreationProfile) -> None:
        voters_by_candidate: dict[str, list[str]] = {}
        for voter, candidate_id in world.first_act_votes.items():
            voters_by_candidate.setdefault(candidate_id, []).append(voter)
        for candidate in world.first_act_candidates:
            candidate.votes = voters_by_candidate.get(candidate.candidate_id, [])

    def _winner(self, world: WorldCreationProfile, counts: dict[str, int]) -> FirstActCandidate | None:
        if world.selected_first_act_id:
            selected = self._candidate_by_id(world, world.selected_first_act_id)
            if selected is not None:
                return selected
        if not world.first_act_candidates:
            return None
        if not counts:
            return world.first_act_candidates[0]
        winner_id = max(counts, key=lambda candidate_id: (counts[candidate_id], -self._candidate_index(world, candidate_id)))
        return self._candidate_by_id(world, winner_id)

    def _candidate_by_id(self, world: WorldCreationProfile, candidate_id: str) -> FirstActCandidate | None:
        for candidate in world.first_act_candidates:
            if candidate.candidate_id == candidate_id:
                return candidate
        return None

    def _candidate_index(self, world: WorldCreationProfile, candidate_id: str) -> int:
        for index, candidate in enumerate(world.first_act_candidates):
            if candidate.candidate_id == candidate_id:
                return index
        return 999

    def _summary_for_candidate(self, candidate: FirstActCandidate) -> str:
        return f"第一幕选择【{candidate.title}】：{candidate.premise}"

    def _dedupe(self, values: list[str]) -> list[str]:
        result: list[str] = []
        for value in values:
            if value and value not in result:
                result.append(value)
        return result
