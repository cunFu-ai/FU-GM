from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any, Callable

from fu_gm.http_server import FUGMHttpService
from fu_gm.testing.conversation_quality import ConversationQualityAuditor
from fu_gm.testing.kariba_fixture import (
    KARIBA_HEROES,
    KARIBA_INVITATION,
    KaribaReplayMessage,
    seed_kariba_ready_campaign,
)
from fu_gm.testing.model_benchmark import ModelProviderSpec


@dataclass(frozen=True)
class KaribaSessionBeat:
    beat_id: str
    speaker: str = ""
    text: str = ""
    expectation: str = "reply"
    kind: str = "player"
    addressed: bool = False
    reply_to_gm: bool = False
    quoted_text: str = ""


@dataclass
class KaribaSessionTurn:
    index: int
    beat_id: str
    kind: str
    speaker: str
    message: str
    expectation: str
    status: int
    elapsed_ms: int
    target: str
    route: str
    send_reply: bool
    reply: str
    agent_error: str = ""
    model_call_count: int = 0
    successful_model_call_count: int = 0
    failed_model_call_count: int = 0
    model_call_records: list[dict[str, object]] = field(default_factory=list)
    agent_trace: list[dict[str, object]] = field(default_factory=list)
    receipts: list[dict[str, object]] = field(default_factory=list)
    state_after: dict[str, object] = field(default_factory=dict)


class KaribaFirstSessionDirector:
    """Human-authored, public-information-only player agenda for one session."""

    def __init__(self) -> None:
        self.cursor = 0
        self.combat_player_actions = 0
        self.adaptive_attempts: dict[str, int] = {}
        self.abandoned_equipment: dict[str, set[str]] = {}
        self.conflict_turn_attempts: dict[str, int] = {}
        self.conflict_turn_numbers_by_actor: dict[str, int] = {}
        self.escape_attempts_by_actor: dict[str, int] = {}
        self.stalled_reason = ""
        self.beats = self._beats()

    def _unresolved_equipment(
        self,
        app: Any,
    ) -> dict[str, set[str]]:
        blocked: dict[str, set[str]] = {}
        for hero_name in KARIBA_HEROES:
            if not app.character_manager.exists(hero_name):
                continue
            unavailable = set(
                app.character_manager.get(hero_name).unavailable_equipment
            )
            unresolved = unavailable - self.abandoned_equipment.get(
                hero_name,
                set(),
            )
            if unresolved:
                blocked[hero_name] = unresolved
        return blocked

    def _adapt_blocked_equipment(
        self,
        app: Any,
        blocked: dict[str, set[str]],
        *,
        phase: str,
    ) -> KaribaSessionBeat:
        """Let simulated players negotiate, then consciously accept a loss."""

        hero_name = next(iter(blocked))
        items = sorted(blocked[hero_name])
        named = "、".join(items)
        speaker = "测试玩家甲" if hero_name == "诺艾尔" else "loading"
        key = f"equipment-blocked:{phase}:{hero_name}"
        attempts = self.adaptive_attempts.get(key, 0) + 1
        self.adaptive_attempts[key] = attempts
        current_scene = app.scene_manager.current_scene
        known_pcs = {
            character.name
            for character in app.character_manager.all()
            if "pc" in character.traits
        }
        present_npcs = [
            name
            for name in list(
                getattr(current_scene, "participants", []) or []
            )
            if name not in known_pcs
        ]
        if attempts == 1 and present_npcs:
            counterpart = present_npcs[0]
            text = (
                f"{hero_name}停下继续拉扯证物柜，直接问当前在场的【{counterpart}】："
                f"这些【{named}】是我的。要满足什么条件，你才肯让我拿走？"
            )
        elif attempts <= 2 and present_npcs:
            text = (
                f"{hero_name}把自己的牢号、封条和物品归属摆给对方看，"
                f"要求对方现在归还【{named}】，不再重复撬同一处机关。"
            )
        else:
            self.abandoned_equipment.setdefault(hero_name, set()).update(items)
            text = (
                f"{hero_name}听着越来越近的动静，决定不再把同伴困在证物柜前："
                f"【{named}】先留下，等活着出去再想办法拿回来。"
            )
        return KaribaSessionBeat(
            beat_id=f"equipment-blocked-{phase}-{hero_name}-{attempts}",
            speaker=speaker,
            text=text,
        )

    @staticmethod
    def _safe_aftermath_location(location: str) -> bool:
        clean = str(location or "").strip()
        if not clean:
            return False
        safe_markers = (
            "监狱外",
            "服务出口外",
            "出口外",
            "村外",
            "祭祀堂",
            "排水旧道出口",
            "避难",
            "落脚",
            "安全屋",
            "藏身",
        )
        if any(marker in clean for marker in safe_markers):
            return True
        unsafe_markers = (
            "监狱",
            "牢房",
            "值班室",
            "转运",
            "检修通道",
            "地牢",
        )
        return not any(marker in clean for marker in unsafe_markers)

    @staticmethod
    def _public_escape_destination(turns: list[KaribaSessionTurn]) -> str:
        public_text = "\n".join(
            str(turn.reply or "")
            for turn in turns
            if str(turn.reply or "").strip()
        )
        if "祭祀堂" in public_text and "排水" in public_text:
            return "卡里巴村西祭祀堂地下排水旧道出口"
        if "排水旧道" in public_text:
            return "卡里巴村监狱外·村西排水旧道出口"
        if "后门" in public_text:
            return "卡里巴村监狱外·后门外侧"
        if "村外" in public_text:
            return "卡里巴村外的临时藏身处"
        return ""

    @staticmethod
    def _successful_check_from_beat(
        turns: list[KaribaSessionTurn],
        beat_prefix: str,
    ) -> bool:
        """Use authoritative check receipts instead of wording heuristics."""

        source_messages = {
            str(turn.message or "").strip()
            for turn in turns
            if turn.beat_id.startswith(beat_prefix)
            and str(turn.message or "").strip()
        }
        if not source_messages:
            return False
        for turn in turns:
            for receipt in turn.receipts:
                if receipt.get("ok") is not True:
                    continue
                result = receipt.get("result")
                result = dict(result) if isinstance(result, dict) else {}
                check_receipt = result.get("check_receipt")
                check_receipt = (
                    dict(check_receipt)
                    if isinstance(check_receipt, dict)
                    else {}
                )
                source_event = result.get("source_event")
                source_event = (
                    dict(source_event)
                    if isinstance(source_event, dict)
                    else {}
                )
                if (
                    check_receipt.get("success") is True
                    and str(source_event.get("text") or "").strip()
                    in source_messages
                ):
                    return True
        return False

    @staticmethod
    def _beats() -> list[KaribaSessionBeat]:
        return [
            KaribaSessionBeat(
                "consent",
                "测试玩家甲",
                "嗯，进入第一章吧。",
                reply_to_gm=True,
                quoted_text=KARIBA_INVITATION,
            ),
            KaribaSessionBeat(
                "cell-observe",
                "测试玩家甲",
                "诺艾尔没急着跨出牢门，先看清门锁、走廊和值守动静，也摸了摸自己身上还剩什么。",
            ),
            KaribaSessionBeat(
                "magic-observe",
                "loading",
                "艾丽妮贴近牢门观察符文，想弄清它的波动是否和自己身上的魔力有关。",
            ),
            KaribaSessionBeat(
                "pc-question-one",
                "测试玩家甲",
                "诺艾尔隔着栏杆小声问艾丽妮：你看出什么了吗？",
                expectation="silent",
            ),
            KaribaSessionBeat(
                "share-and-search",
                "loading",
                "艾丽妮把自己看见的现象原样告诉诺艾尔，再翻找牢房里真正能派上用场的东西。",
            ),
            KaribaSessionBeat(
                "pc-plan-one",
                "测试玩家甲",
                "诺艾尔说：符文的松动有固定节奏。下一次一起动，你盯魔力，我来处理锁。",
                expectation="silent",
            ),
            KaribaSessionBeat(
                "idle-after-plan",
                kind="idle",
                expectation="gm_beat",
            ),
            KaribaSessionBeat(
                "work-lock",
                "测试玩家甲",
                "下一次波动出现时，诺艾尔立刻抓住已经松动的锁簧，尝试把自己的牢门彻底打开。",
            ),
            KaribaSessionBeat(
                "help-lock",
                "loading",
                "艾丽妮照着刚才观察到的节奏压住符文回流，配合诺艾尔把两扇牢门都稳定下来。",
            ),
            KaribaSessionBeat(
                "ask-neighbor",
                "测试玩家甲",
                "诺艾尔朝相邻牢房压低声音：还有人醒着吗？你们知道这次封印为什么会突然失控吗？",
                expectation="silent",
            ),
            KaribaSessionBeat(
                "false-premise-manor",
                "测试玩家甲",
                "诺艾尔皱了皱眉：等等，刚才是谁提到了庄园？我没听清。",
            ),
            KaribaSessionBeat(
                "pc-dilemma",
                "loading",
                "艾丽妮对诺艾尔说：我们可以只顾自己，也可以先看看这里还有多少人撑得住。你怎么想？",
                expectation="silent",
            ),
            KaribaSessionBeat(
                "offer-neighbor",
                "测试玩家甲",
                "诺艾尔对牢里的人说：把你知道的安全路线告诉我们；门一旦能开，我们不会把你独自留给看守。",
            ),
            KaribaSessionBeat(
                "leave-cell-row",
                "loading",
                "艾丽妮确认诺艾尔和邻牢的人都跟得上，随后贴着墙走出这排牢房，先去看脚步声来向。",
            ),
            KaribaSessionBeat(
                "listen-corridor",
                "测试玩家甲",
                "诺艾尔停在转角，不探头硬闯，先听守卫人数、距离和钥匙声来自哪边。",
            ),
            KaribaSessionBeat(
                "move-to-duty-room",
                "loading",
                "艾丽妮把听到的方位告诉诺艾尔，随后贴着遮蔽物向值班室入口移动，想先找回自己被收走的随身物品。",
            ),
            KaribaSessionBeat(
                "search-property",
                "测试玩家甲",
                "诺艾尔检查值班室里贴着牢号的柜子和交接记录，先找两人的装备，也留意囚犯是否被秘密转往别处。",
            ),
            KaribaSessionBeat(
                "pc-evidence-choice",
                "loading",
                "艾丽妮低声说：装备拿回就走，还是把转运记录也带上？后者会更慢。",
                expectation="silent",
            ),
            KaribaSessionBeat(
                "take-gear-and-proof",
                "测试玩家甲",
                "诺艾尔取回自己的钢匕首和细剑，把写有艾丽妮牢号的物品包放到她伸手可及的位置；随后只拿最能证明转运去向的一份记录，不翻空整间值班室。",
            ),
            KaribaSessionBeat(
                "elinie-recovers-gear",
                "loading",
                "艾丽妮核对牢号和封条后取回自己的法杖、魔典与贤者之袍，确认没有拿走别人的东西。",
            ),
            KaribaSessionBeat(
                "free-prisoners",
                "loading",
                "艾丽妮拿到钥匙后先去开仍有人回应的牢门，让能行动的人互相搀扶，不替任何人决定是否跟着越狱。",
            ),
            KaribaSessionBeat(
                "idle-before-opposition",
                kind="idle",
                expectation="gm_beat",
            ),
            KaribaSessionBeat(
                "talk-to-guard",
                "测试玩家甲",
                "诺艾尔停在西侧通路，保持非攻击姿态，先确认眼前是否真的有人拦路。",
            ),
            KaribaSessionBeat(
                "guard-refusal-action",
                "loading",
                "艾丽妮盯住来人的手和退路，借走廊里的遮挡压缩其靠近囚犯的空间。",
            ),
            KaribaSessionBeat(
                "force-passage",
                "测试玩家甲",
                "诺艾尔把最后一句话说清：我们现在要走。她压低攻击姿态，从守卫封锁的一侧开始强行通过，仍以让对方失去战斗能力为限。",
            ),
            KaribaSessionBeat(
                "idle-opposition-response",
                kind="idle",
                expectation="gm_beat",
            ),
            KaribaSessionBeat(
                "inspect-transfer-cart",
                "loading",
                "艾丽妮沿眼前已经打开的逃生方向检查牢狱如何转移囚犯，留意任何与灵魂抽取有关的可见装置或痕迹，不徒手破坏未知封印。",
            ),
            KaribaSessionBeat(
                "pc-final-dilemma",
                "测试玩家甲",
                "诺艾尔问艾丽妮：眼前这些痕迹还指向活人吗？我们是带着已经拿到的证据先走，还是先处理这里正在发生的事？",
                expectation="silent",
            ),
            KaribaSessionBeat(
                "choose-rescue",
                "loading",
                "艾丽妮回答：先救人。她继续追查能停止抽取、又不会把灵魂残留直接震散的封印节点。",
            ),
            KaribaSessionBeat(
                "ritual-stabilize",
                "loading",
                "艾丽妮尝试用元素系仪式稳定失控封印，让囚犯残留不再被拖向那个尚未查明的容器；她会照规则推进，不把结果说在前面。",
            ),
            KaribaSessionBeat(
                "help-ritual",
                "测试玩家甲",
                "诺艾尔守住入口并按艾丽妮的指示移动导流物，协助她完成眼前的仪式。",
            ),
            KaribaSessionBeat(
                "idle-before-escape",
                kind="idle",
                expectation="gm_beat",
            ),
            KaribaSessionBeat(
                "escape-back-route",
                "测试玩家甲",
                "诺艾尔招呼愿意同行的人开始撤离，沿当前已经找到、能避开监狱正门的方向走。",
            ),
            KaribaSessionBeat(
                "watch-pursuit",
                "loading",
                "艾丽妮沿刚才已经走通的路线跟在撤离队伍最后，从检修通道返回服务出口外的雨巷，"
                "同时留意有没有追兵火光或残留再次被牵引；她不把是否安全说成既定结果。",
            ),
            KaribaSessionBeat(
                "reach-shelter",
                "测试玩家甲",
                "诺艾尔一边留意身后的动静，一边寻找能避雨、也不会把普通村民卷进追捕的临时落脚点。",
            ),
            KaribaSessionBeat(
                "pc-aftermath",
                "loading",
                "艾丽妮坐下后对诺艾尔说：本来只是偷了点吃的，结果现在连别人的灵魂也背上了。那份记录接下来交给谁？",
                expectation="silent",
            ),
            KaribaSessionBeat(
                "aftermath-decision",
                "测试玩家甲",
                "诺艾尔把转运记录收好：先不交给监狱看守。天亮后找一个与这批转运无关、又看得懂这些编号的人。",
            ),
            KaribaSessionBeat(
                "end-session",
                "测试玩家甲",
                "今晚先到这里，麻烦收团、结算并保存这一场。",
                addressed=True,
            ),
        ]

    def next_scripted(
        self,
        runtime: Any | None = None,
        *,
        turns: list[KaribaSessionTurn] | None = None,
        conflict_seen: bool = False,
    ) -> KaribaSessionBeat | None:
        if self.cursor >= len(self.beats):
            return None
        beat = self.beats[self.cursor]
        if runtime is not None:
            adapted = self._adapt_to_authoritative_state(
                beat,
                runtime,
                turns=turns or [],
                conflict_seen=conflict_seen,
            )
            if adapted is None:
                if self.stalled_reason:
                    return None
                self.cursor += 1
                return self.next_scripted(
                    runtime,
                    turns=turns,
                    conflict_seen=conflict_seen,
                )
            if adapted.beat_id != beat.beat_id:
                return adapted
            beat = adapted
        self.cursor += 1
        return beat

    def _adapt_to_authoritative_state(
        self,
        beat: KaribaSessionBeat,
        runtime: Any,
        *,
        turns: list[KaribaSessionTurn],
        conflict_seen: bool,
    ) -> KaribaSessionBeat | None:
        app = runtime.app
        scene = app.scene_manager.current_scene
        location = str(getattr(scene, "location", "") or "")
        frame = app.scene_frame_manager.current_frame
        opportunity_role = str(
            getattr(frame, "session_opportunity_role", "") or ""
        ).strip()
        public_text = "\n".join(
            str(turn.reply or "") for turn in turns if str(turn.reply or "").strip()
        )

        if beat.beat_id == "search-property" and not any(
            marker in app.scene_manager.location_of("诺艾尔")
            for marker in ("值班", "证物", "登记", "档案")
        ):
            noel = app.character_manager.get("诺艾尔")
            noel_unavailable = set(noel.unavailable_equipment)
            if noel_unavailable <= self.abandoned_equipment.get("诺艾尔", set()):
                return None
            property_known = any(
                marker in public_text
                for marker in (
                    "证物柜就在",
                    "就是值班室",
                    "值班室入口",
                    "被收缴的装备放在值班室",
                    "被收缴的装备都放在值班室",
                    "证物·收",
                )
            )
            if property_known:
                attempts = self.adaptive_attempts.get("enter-property-room", 0) + 1
                self.adaptive_attempts["enter-property-room"] = attempts
                if attempts == 1:
                    return KaribaSessionBeat(
                        beat_id="enter-property-room-1",
                        speaker="测试玩家甲",
                        text=(
                            "诺艾尔沿刚刚查明的路线实际前往证物柜所在的值班室；"
                            "她只移动自己，遇到眼前阻碍就当场处理。"
                        ),
                    )
                latest_reply = next(
                    (
                        str(turn.reply or "").strip()
                        for turn in reversed(turns)
                        if str(turn.reply or "").strip()
                    ),
                    "",
                )
                if attempts == 2:
                    obstacle = (
                        "活铁藤与符文供能"
                        if "活铁藤" in latest_reply
                        else "眼前封路机关与守卫视线"
                    )
                    return KaribaSessionBeat(
                        beat_id="enter-property-room-2-alternate",
                        speaker="loading",
                        text=(
                            f"艾丽妮不让诺艾尔继续硬挤同一条通路，转而观察【{obstacle}】，"
                            "寻找能让阻碍松开或露出另一条入口的办法。"
                        ),
                    )
                if attempts == 3:
                    return KaribaSessionBeat(
                        beat_id="enter-property-room-3-negotiate",
                        speaker="测试玩家甲",
                        text=(
                            "诺艾尔停在守卫能看清双手的位置，说明地下封印已经失控，"
                            "自己只取回被收缴的物品；她直接问眼前拦路者，要怎样才肯放行。"
                        ),
                    )
                if attempts == 4:
                    held_weapon = str(
                        getattr(noel, "equipped_main_hand", "") or ""
                    ).strip()
                    stance = (
                        f"压低【{held_weapon}】"
                        if held_weapon
                        and held_weapon != "徒手攻击"
                        and held_weapon not in noel_unavailable
                        else "压低重心，保持空手"
                    )
                    return KaribaSessionBeat(
                        beat_id="enter-property-room-4-force",
                        speaker="测试玩家甲",
                        text=(
                            f"谈不拢，诺艾尔{stance}强行突破值班室通路；"
                            "她只求穿过去，不做致命攻击。"
                        ),
                    )
                self.abandoned_equipment.setdefault("诺艾尔", set()).update(
                    noel_unavailable
                )
                return KaribaSessionBeat(
                    beat_id="enter-property-room-5-leave-gear",
                    speaker="测试玩家甲",
                    text=(
                        "值班室已经被彻底封死，诺艾尔不再让同伴为装备困在这里；"
                        "钢匕首和细剑先留下，她转身处理眼前更紧迫的越狱。"
                    ),
                )

            attempts = self.adaptive_attempts.get("locate-property-room", 0) + 1
            self.adaptive_attempts["locate-property-room"] = attempts
            if attempts <= 3:
                return KaribaSessionBeat(
                    beat_id=f"locate-property-room-{attempts}",
                    speaker=(
                        "测试玩家甲" if attempts in {1, 3} else "loading"
                    ),
                    text={
                        1: "诺艾尔沿着钥匙声与守卫来路寻找值班室入口；走到新的门口就先停下观察，不预设门后是什么。",
                        2: "艾丽妮查看当前走廊真实可见的门牌、符文线路与交接痕迹，寻找证物柜所在方向。",
                        3: "诺艾尔向当前真正听得见她的人问：被收缴的装备放在哪？她留在原地等一个明确回应。",
                    }[attempts],
                )
            if attempts == 4:
                return KaribaSessionBeat(
                    beat_id="locate-property-room-4-seize-keys",
                    speaker="测试玩家甲",
                    text=(
                        "诺艾尔不再盯着门牌兜圈。她伏在转角，等带钥匙的守卫靠近，"
                        "扑上去压住持钥匙的手并夺下钥匙；只求制伏，不做致命攻击。"
                    ),
                )
            if attempts == 5:
                self.abandoned_equipment.setdefault("诺艾尔", set()).update(
                    noel_unavailable
                )
                return KaribaSessionBeat(
                    beat_id="locate-property-room-5-leave-gear",
                    speaker="测试玩家甲",
                    text=(
                        "通往值班室的线索已经断了，守卫也彻底压到眼前。"
                        "诺艾尔不再让同伴为两件武器耗在这里，转身先处理越狱。"
                    ),
                )
            return None

        if beat.beat_id == "pc-evidence-choice" and not any(
            marker in public_text for marker in ("记录", "运单", "凭据", "名册", "转运")
        ):
            return KaribaSessionBeat(
                beat_id=beat.beat_id,
                speaker=beat.speaker,
                text="艾丽妮低声问诺艾尔：眼前已经看见的东西里，哪些值得带走，哪些碰了反而会惊动守卫？",
                expectation="silent",
            )

        if beat.beat_id == "take-gear-and-proof":
            hero = app.character_manager.get("诺艾尔")
            unavailable = set(hero.unavailable_equipment) - (
                self.abandoned_equipment.get("诺艾尔", set())
            )
            if unavailable:
                named = "、".join(sorted(unavailable))
                return KaribaSessionBeat(
                    beat_id=beat.beat_id,
                    speaker=beat.speaker,
                    text=f"诺艾尔核对牢号与封条，尝试从对应证物柜取回仍被收缴的【{named}】；她只处理自己的物品。",
                )
            return KaribaSessionBeat(
                beat_id=beat.beat_id,
                speaker=beat.speaker,
                text="诺艾尔确认自己已经能够取用的装备都在，随后查看眼前最能说明囚犯去向的一份凭据，不翻空整间房。",
            )

        if beat.beat_id == "elinie-recovers-gear":
            noel = app.character_manager.get("诺艾尔")
            noel_unavailable = set(noel.unavailable_equipment) - (
                self.abandoned_equipment.get("诺艾尔", set())
            )
            if noel_unavailable:
                return self._adapt_blocked_equipment(
                    app,
                    {"诺艾尔": noel_unavailable},
                    phase="noel-before-elinie",
                )
            if not any(
                marker in app.scene_manager.location_of("艾丽妮")
                for marker in ("值班", "证物", "登记", "档案")
            ):
                attempts = self.adaptive_attempts.get("elinie-enters-property-room", 0) + 1
                self.adaptive_attempts["elinie-enters-property-room"] = attempts
                if attempts <= 2:
                    return KaribaSessionBeat(
                        beat_id=f"elinie-enters-property-room-{attempts}",
                        speaker="loading",
                        text=(
                            "艾丽妮根据眼前已经公开的通路和自己观察到的机关规律，"
                            "尝试实际进入证物柜所在的值班室；只移动自己，"
                            "遇到仍在眼前的阻碍就当场处理。"
                        ),
                    )
                self.stalled_reason = "诺艾尔已经抵达值班室，但艾丽妮多次行动后仍未能自行会合。"
                return None
            hero = app.character_manager.get("艾丽妮")
            unavailable = set(hero.unavailable_equipment)
            if not unavailable:
                return None
            named = "、".join(sorted(unavailable))
            return KaribaSessionBeat(
                beat_id=beat.beat_id,
                speaker=beat.speaker,
                text=f"艾丽妮核对自己的牢号和封条，尝试取回仍被收缴的【{named}】，不拿走别人的东西。",
            )

        if beat.beat_id == "free-prisoners":
            blocked = self._unresolved_equipment(app)
            if blocked:
                return self._adapt_blocked_equipment(
                    app,
                    blocked,
                    phase="before-free-prisoners",
                )
            return KaribaSessionBeat(
                beat_id=beat.beat_id,
                speaker=beat.speaker,
                text="艾丽妮检查仍有人回应的牢门和眼前可用的锁控，尝试为能行动的囚犯打开一条路，不替任何人决定是否跟着越狱。",
            )

        if beat.beat_id in {"talk-to-guard", "guard-refusal-action"}:
            participants = [
                str(item or "").strip()
                for item in list(getattr(scene, "participants", []) or [])
            ]
            guard_present = any(
                any(marker in participant for marker in ("守卫", "狱卒", "看守"))
                for participant in participants
            )
            if not guard_present:
                if beat.beat_id == "guard-refusal-action":
                    return None
                if any(
                    marker in public_text
                    for marker in ("排水格栅", "狭窄水道", "雨水巷")
                ):
                    return KaribaSessionBeat(
                        beat_id=beat.beat_id,
                        speaker="loading",
                        text=(
                            "艾丽妮伏在刚露出的排水格栅旁，先确认狭窄水道是否真的能让"
                            "能行动的囚犯通过，以及怎样过去才不会立刻触发追捕；"
                            "她不替其他人决定是否同行。"
                        ),
                    )
                return KaribaSessionBeat(
                    beat_id=beat.beat_id,
                    speaker="测试玩家甲",
                    text=(
                        "诺艾尔停在西侧通路，摊开空着的双手观察后方，"
                        "想确认追兵是否真的跟来，不把远处声响当成已经到场的人。"
                    ),
                )

            if beat.beat_id == "talk-to-guard":
                noel = app.character_manager.get("诺艾尔")
                held_weapon = str(noel.equipped_main_hand or "").strip()
                held_weapon_available = bool(
                    held_weapon
                    and held_weapon != "徒手攻击"
                    and held_weapon not in set(noel.unavailable_equipment)
                )
                stance = (
                    f"把【{held_weapon}】压低"
                    if held_weapon_available
                    else "摊开空着的双手"
                )
                return KaribaSessionBeat(
                    beat_id=beat.beat_id,
                    speaker=beat.speaker,
                    text=(
                        f"诺艾尔朝眼前的守卫停下，{stance}而不是先动手："
                        "让开，我们只带人和自己的东西走，不想在这里杀谁。"
                    ),
                )

        if beat.beat_id == "force-passage" and conflict_seen:
            return None

        if beat.beat_id == "idle-opposition-response" and conflict_seen:
            return None

        if beat.beat_id == "help-ritual" and not app.ritual_manager.active_rituals:
            return None

        lower_markers = ("下层", "机房", "转运", "封印", "抽取", "地牢深处", "检修通道")

        def hero_in_lower_area(hero_name: str) -> bool:
            return any(
                marker in app.scene_manager.location_of(hero_name)
                for marker in lower_markers
            )

        lower_area_reached = bool(
            opportunity_role in {"alternate_approach", "climax_candidate"}
            or any(marker in location for marker in lower_markers)
        )
        if beat.beat_id == "pc-final-dilemma" and not all(
            hero_in_lower_area(hero_name) for hero_name in KARIBA_HEROES
        ):
            lower_route_known = (
                lower_area_reached
                or self._successful_check_from_beat(
                    turns,
                    "locate-lower-prison-",
                )
                or any(
                marker in public_text
                for marker in (
                    "通往下层",
                    "转运设施入口",
                    "转运设施一侧",
                    "检修通道",
                    "下层入口",
                    "地下转运",
                    "封印机房入口",
                )
                )
            )
            if lower_route_known:
                hero_name = next(
                    name for name in KARIBA_HEROES if not hero_in_lower_area(name)
                )
                speaker = "测试玩家甲" if hero_name == "诺艾尔" else "loading"
                key = f"enter-lower-prison:{hero_name}"
                attempts = self.adaptive_attempts.get(key, 0) + 1
                self.adaptive_attempts[key] = attempts
                if attempts <= 3:
                    return KaribaSessionBeat(
                        beat_id=f"enter-lower-prison-{hero_name}-{attempts}",
                        speaker=speaker,
                        text=(
                            f"{hero_name}沿刚才已经查明的路线，实际前往转运设施一侧的检修通道；"
                            "只移动自己，遇到仍在眼前的门禁或阻碍就当场处理。"
                        ),
                    )
                self.stalled_reason = f"转运路线已经公开，但【{hero_name}】多次行动后仍未实际抵达下层区域。"
                return None

            attempts = self.adaptive_attempts.get("locate-lower-prison", 0) + 1
            self.adaptive_attempts["locate-lower-prison"] = attempts
            if attempts <= 3:
                return KaribaSessionBeat(
                    beat_id=f"locate-lower-prison-{attempts}",
                    speaker="loading" if attempts % 2 else "测试玩家甲",
                    text=(
                        "艾丽妮沿当前已经公开的转运痕迹寻找它实际通往哪里；遇到门、楼梯或岔路就停下来确认，不声称已经抵达。"
                        if attempts % 2
                        else "诺艾尔核对眼前出口、转运记录与门牌，寻找能实际通往下层转运设施的入口；找到入口后先停下说明位置。"
                    ),
                )
            self.stalled_reason = "玩家连续调查后仍未能定位通往下层转运设施的路线。"
            return None

        hero_locations = {
            name: app.scene_manager.location_of(name)
            for name in KARIBA_HEROES
        }
        safe_hero_locations = [
            hero_location
            for hero_location in hero_locations.values()
            if self._safe_aftermath_location(hero_location)
        ]
        public_escape_destination = self._public_escape_destination(turns)
        aftermath_destination = (
            safe_hero_locations[0]
            if safe_hero_locations
            else public_escape_destination
        )
        heroes_at_aftermath = bool(
            aftermath_destination
            and all(
                self._safe_aftermath_location(hero_location)
                and app.scene_manager.locations_overlap(
                    hero_location,
                    aftermath_destination,
                )
                for hero_location in hero_locations.values()
            )
        )
        aftermath_reached = bool(
            heroes_at_aftermath
            or (
                opportunity_role == "aftermath"
                and all(
                    self._safe_aftermath_location(hero_location)
                    for hero_location in hero_locations.values()
                )
            )
        )
        if beat.beat_id == "pc-aftermath" and not aftermath_reached:
            if aftermath_destination:
                hero_name = next(
                    name
                    for name, hero_location in hero_locations.items()
                    if not (
                        self._safe_aftermath_location(hero_location)
                        and app.scene_manager.locations_overlap(
                            hero_location,
                            aftermath_destination,
                        )
                    )
                )
                key = f"reach-aftermath:{hero_name}"
                attempts = self.adaptive_attempts.get(key, 0) + 1
                self.adaptive_attempts[key] = attempts
                if attempts <= 3:
                    speaker = (
                        "测试玩家甲"
                        if hero_name == "诺艾尔"
                        else "loading"
                    )
                    return KaribaSessionBeat(
                        beat_id=f"reach-aftermath-{hero_name}-{attempts}",
                        speaker=speaker,
                        text=(
                            f"{hero_name}沿已经公开的撤离路线实际前往【{aftermath_destination}】，"
                            "抵达后停下来与同伴会合；这次只移动自己的角色。"
                        ),
                    )
                self.stalled_reason = (
                    f"【{hero_name}】多次撤离后仍未抵达已经公开的安全地点。"
                )
                return None
            attempts = self.adaptive_attempts.get("reach-aftermath", 0) + 1
            self.adaptive_attempts["reach-aftermath"] = attempts
            if attempts <= 2:
                return KaribaSessionBeat(
                    beat_id=f"reach-aftermath-{attempts}",
                    speaker="loading" if attempts == 1 else "测试玩家甲",
                    text=(
                        "艾丽妮沿撤离方向继续找能真正离开监狱控制范围的出口，边走边确认同行者是否跟得上。"
                        if attempts == 1
                        else "诺艾尔带队离开眼前这片危险区域，寻找一处可以停下来清点人员与线索的临时落脚点。"
                    ),
                )
            self.stalled_reason = "玩家已经尝试撤离，但仍未进入可结算事后反应的新场景。"
            return None

        if beat.beat_id == "end-session":
            unavailable = {
                name: sorted(
                    set(app.character_manager.get(name).unavailable_equipment)
                    - self.abandoned_equipment.get(name, set())
                )
                for name in KARIBA_HEROES
                if app.character_manager.exists(name)
                and (
                    set(app.character_manager.get(name).unavailable_equipment)
                    - self.abandoned_equipment.get(name, set())
                )
            }
            if unavailable:
                key = "end-recover-equipment"
                attempts = self.adaptive_attempts.get(key, 0) + 1
                self.adaptive_attempts[key] = attempts
                if attempts <= 2:
                    hero_name = next(iter(unavailable))
                    speaker = (
                        "测试玩家甲"
                        if hero_name == "诺艾尔"
                        else "loading"
                    )
                    return KaribaSessionBeat(
                        beat_id=f"{key}-{attempts}",
                        speaker=speaker,
                        text=(
                            f"{hero_name}先处理仍未取回的【{'、'.join(unavailable[hero_name])}】，"
                            "确认真正拿到手后再离开。"
                        ),
                    )
                self.stalled_reason = "收团前仍有角色的既有装备处于不可用状态。"
                return None

            expected_loadouts = {
                "诺艾尔": ("细剑", "钢匕首"),
                "艾丽妮": ("法杖", "魔典"),
            }
            for hero_name, choices in expected_loadouts.items():
                hero = app.character_manager.get(hero_name)
                if hero.unavailable_equipment:
                    # A consciously abandoned loadout is a valid story loss,
                    # not a reason for the test player to teleport gear back.
                    continue
                if str(hero.equipped_main_hand or "") not in {"", "徒手攻击"}:
                    continue
                key = f"end-equip:{hero_name}"
                attempts = self.adaptive_attempts.get(key, 0) + 1
                self.adaptive_attempts[key] = attempts
                if attempts <= 2:
                    speaker = (
                        "测试玩家甲"
                        if hero_name == "诺艾尔"
                        else "loading"
                    )
                    return KaribaSessionBeat(
                        beat_id=f"end-equip-{hero_name}-{attempts}",
                        speaker=speaker,
                        text=(
                            f"{hero_name}把已经取回的【{choices[0]}】装备到主手，"
                            f"把【{choices[1]}】收在随时可取用的位置。"
                        ),
                    )
                self.stalled_reason = f"【{hero_name}】已经取回装备，但始终没有形成可用装配。"
                return None

            locations = {
                name: app.scene_manager.location_of(name)
                for name in KARIBA_HEROES
            }
            safe_locations = [
                value
                for value in locations.values()
                if self._safe_aftermath_location(value)
            ]
            destination = (
                safe_locations[0]
                if safe_locations
                else self._public_escape_destination(turns)
            )
            heroes_together = bool(
                destination
                and all(
                    self._safe_aftermath_location(location)
                    and app.scene_manager.locations_overlap(
                        location,
                        destination,
                    )
                    for location in locations.values()
                )
            )
            if not heroes_together:
                if not destination:
                    key = "end-find-exit"
                    attempts = self.adaptive_attempts.get(key, 0) + 1
                    self.adaptive_attempts[key] = attempts
                    if attempts <= 2:
                        speaker = (
                            "测试玩家甲" if attempts == 1 else "loading"
                        )
                        hero_name = "诺艾尔" if attempts == 1 else "艾丽妮"
                        return KaribaSessionBeat(
                            beat_id=f"{key}-{attempts}",
                            speaker=speaker,
                            text=(
                                f"{hero_name}先不宣称已经逃脱，沿眼前公开的撤离线索确认一个"
                                "能真正离开监狱控制范围的具体出口。"
                            ),
                        )
                    self.stalled_reason = "收团前仍没有公开、可实际抵达的监狱出口。"
                    return None
                hero_name = next(
                    name
                    for name, hero_location in locations.items()
                    if not (
                        self._safe_aftermath_location(hero_location)
                        and app.scene_manager.locations_overlap(
                            hero_location,
                            destination,
                        )
                    )
                )
                key = f"end-escape:{hero_name}"
                attempts = self.adaptive_attempts.get(key, 0) + 1
                self.adaptive_attempts[key] = attempts
                if attempts <= 3:
                    speaker = (
                        "测试玩家甲"
                        if hero_name == "诺艾尔"
                        else "loading"
                    )
                    return KaribaSessionBeat(
                        beat_id=f"end-escape-{hero_name}-{attempts}",
                        speaker=speaker,
                        text=(
                            f"{hero_name}沿已经公开并走通的撤离路线实际前往【{destination}】，"
                            "抵达后停下来清点人员；这次只移动自己的角色。"
                        ),
                    )
                self.stalled_reason = f"【{hero_name}】多次撤离后仍未抵达公开的安全地点。"
                return None

            progress = app.story_arc_manager.state.current_session_progress
            if (
                not bool(progress.closure_ready)
                and not self.adaptive_attempts.get("closure-heartbeat")
            ):
                self.adaptive_attempts["closure-heartbeat"] = 1
                return KaribaSessionBeat(
                    beat_id="idle-session-closure",
                    kind="idle",
                    expectation="gm_beat",
                )
        return beat

    def conflict_action(self, runtime: Any) -> KaribaSessionBeat | None:
        state = runtime.app.conflict_manager.state
        actor = str(state.current_actor() or "").strip()
        if actor not in KARIBA_HEROES:
            return None
        speaker = "测试玩家甲" if actor == "诺艾尔" else "loading"
        living_enemies = [
            name
            for name in state.enemy_side
            if runtime.app.character_manager.exists(name)
            and runtime.app.character_manager.get(name).hp > 0
            and name not in state.escaped_combatants
            and name not in state.surrendered_combatants
            and name not in state.defeated_combatants
        ]
        self.combat_player_actions += 1
        turn_key = f"{int(state.turn_serial or 0)}:{actor}"
        turn_attempt = self.conflict_turn_attempts.get(turn_key, 0) + 1
        self.conflict_turn_attempts[turn_key] = turn_attempt
        if turn_attempt == 1:
            self.conflict_turn_numbers_by_actor[actor] = (
                self.conflict_turn_numbers_by_actor.get(actor, 0) + 1
            )
        actor_turn_number = self.conflict_turn_numbers_by_actor.get(actor, 1)
        if not living_enemies:
            text = f"{actor}确认对方已经没有继续战斗的人，停手并准备结束这场冲突。"
        elif state.round_number >= 4:
            escape_attempt = self.escape_attempts_by_actor.get(actor, 0) + 1
            self.escape_attempts_by_actor[actor] = escape_attempt
            phase = (escape_attempt - 1) % 4
            if phase in {0, 2}:
                unavailable = set(
                    getattr(
                        runtime.app.character_manager.get(actor),
                        "unavailable_equipment",
                        {},
                    )
                )
                if unavailable:
                    self.abandoned_equipment.setdefault(actor, set()).update(
                        unavailable
                    )
                text = (
                    f"{actor}{'抓住刚才制造的空当，' if phase == 2 else '不再恋战，'}"
                    "沿已经确认的西侧通路突围，尝试寻找撤离监狱的出口；"
                    "这次只处理自己的移动，不替同伴决定。"
                )
            elif actor == "诺艾尔" and phase == 1:
                hero = runtime.app.character_manager.get(actor)
                weapon = str(hero.equipped_main_hand or "徒手攻击")
                text = (
                    f"刚才的撤离没有形成出口，诺艾尔改用【{weapon}】攻击"
                    f"【{living_enemies[0]}】，先逼出一个能走的空当，不做致命处决。"
                )
            elif actor == "艾丽妮" and phase == 1:
                text = (
                    f"刚才的撤离没有形成出口，艾丽妮改为妨碍【{living_enemies[0]}】，"
                    "用【洞察+意志】把对方从撤离方向引开。"
                )
            else:
                text = f"{actor}执行防御行动，先护住自己并等待下一次撤离机会。"
        elif actor == "诺艾尔":
            hero = runtime.app.character_manager.get(actor)
            weapon = str(hero.equipped_main_hand or "徒手攻击")
            if turn_attempt > 1:
                text = "诺艾尔改为执行防御行动，不重复刚才没有结算的攻击。"
            elif actor_turn_number % 2 == 1:
                text = (
                    f"诺艾尔用当前装备的【{weapon}】攻击【{living_enemies[0]}】，"
                    "目标是让对方失去战斗能力，不做致命处决。"
                )
            else:
                text = "诺艾尔执行防御行动，守住通路并观察看守下一次换位。"
        else:
            if turn_attempt > 1:
                text = "艾丽妮改为执行防御行动，不重复刚才没有结算的妨碍。"
            elif actor_turn_number == 1:
                text = (
                    f"艾丽妮执行妨碍行动，观察【{living_enemies[0]}】的重心和视线，"
                    "用【洞察+意志】迫使他远离囚犯。"
                )
            elif actor_turn_number % 2 == 0:
                text = "艾丽妮执行防御行动，先稳住阵脚并留意通往出口的空当。"
            else:
                text = (
                    f"艾丽妮再次妨碍【{living_enemies[0]}】，这次观察钥匙与风灯的配合，"
                    "用【洞察+意志】诱使对方露出通路。"
                )
        return KaribaSessionBeat(
            beat_id=f"conflict-{self.combat_player_actions:02d}",
            speaker=speaker,
            text=text,
        )


class KaribaFirstSessionRunner:
    def __init__(
        self,
        service: FUGMHttpService,
        *,
        provider: ModelProviderSpec,
        output_root: str | Path,
        campaign_id: str,
        session_id: str = "kariba-first-session",
        channel_id: str = "kariba-first-session-group",
        max_turns: int = 90,
        provider_retry_limit: int = 1,
        provider_retry_delay_seconds: float = 0.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.service = service
        self.provider = provider
        self.output_root = Path(output_root)
        self.output_root.mkdir(parents=True, exist_ok=True)
        self.campaign_id = campaign_id
        self.session_id = session_id
        self.channel_id = channel_id
        self.max_turns = max(20, int(max_turns))
        self.provider_retry_limit = max(0, int(provider_retry_limit))
        self.provider_retry_delay_seconds = max(
            0.0,
            float(provider_retry_delay_seconds),
        )
        self._provider_retry_sleep = sleep
        self.director = KaribaFirstSessionDirector()
        self.turns: list[KaribaSessionTurn] = []
        self.window_attempts: dict[str, int] = {}
        self.transaction_retry_attempts: dict[str, int] = {}
        self.provider_retry_attempts: dict[str, int] = {}
        self.provider_retry_events: list[dict[str, object]] = []
        self.sent_beats: dict[int, KaribaSessionBeat] = {}
        self.conflict_seen = False
        self.answered_gm_request_turns: set[int] = set()
        self.outcome_branch = ""
        self.defeat_scene_attempts = 0
        self.defeat_cursor = 0
        self.split_scene_attempts = 0
        self.split_cursor = 0
        self.split_captured_heroes: list[str] = []
        self.split_escaped_heroes: list[str] = []
        self.partial_scene_attempts = 0
        self.partial_cursor = 0
        self.partial_captured_heroes: list[str] = []
        self.partial_free_heroes: list[str] = []
        self.terminal_conflict_attempts = 0
        self.fabula_rerolls_by_actor: dict[str, int] = {}
        self.fabula_reroll_sources: set[tuple[str, str]] = set()
        self.failure_timeout_attempts: dict[str, int] = {}
        self.runtime: Any | None = None

    def _detect_outcome_branch(self, runtime: Any) -> str:
        """Classify the resolved conflict without confusing history with HP state."""

        if self.outcome_branch:
            return self.outcome_branch
        conflict = runtime.app.conflict_manager.state
        fallen_now = {
            name for name in KARIBA_HEROES if name in conflict.fallen_pcs
        }
        defeated_here = {
            name
            for name in KARIBA_HEROES
            if name in fallen_now
            or bool(conflict.pc_defeat_consequences.get(name))
        }
        escaped_here = {
            name for name in KARIBA_HEROES if name in conflict.escaped_combatants
        }
        if defeated_here == set(KARIBA_HEROES) and not escaped_here:
            self.outcome_branch = "party_defeat"
        elif defeated_here and escaped_here and defeated_here | escaped_here == set(
            KARIBA_HEROES
        ):
            self.outcome_branch = "split_capture"
            self.split_captured_heroes = sorted(defeated_here)
            self.split_escaped_heroes = sorted(escaped_here)
        elif defeated_here:
            free_here = set(KARIBA_HEROES) - defeated_here - escaped_here
            if free_here:
                self.outcome_branch = "partial_capture"
                self.partial_captured_heroes = sorted(defeated_here)
                self.partial_free_heroes = sorted(free_here)
        return self.outcome_branch

    def run(self) -> dict[str, object]:
        runtime = seed_kariba_ready_campaign(
            self.service,
            campaign_id=self.campaign_id,
            session_id=self.session_id,
            channel_id=self.channel_id,
        )
        self.runtime = runtime
        stalled_conflict_actor = ""
        stalled_conflict_count = 0
        stalled_player_turn = ""
        stalled_player_count = 0
        while len(self.turns) < self.max_turns:
            gate = self.service.session_gates.get(
                self.campaign_id,
                self.channel_id,
                self.session_id,
            )
            if (
                self.turns
                and not runtime.app.session_ledger.active
                and gate.status not in {"session_zero", "adventure"}
            ):
                break
            if self.director.stalled_reason:
                break

            followup = self._pending_window_followup(runtime)
            if followup is not None:
                self._send_message(followup)
                continue
            if self._settle_silent_failure_grace(runtime):
                continue

            retry = self._provider_unavailable_message_retry()
            if retry is not None:
                self._send_message(retry)
                continue

            retry = self._rolled_back_message_retry()
            if retry is not None:
                self._send_message(retry)
                continue
            if self.director.stalled_reason:
                break

            clarification = self._gm_clarification_followup(runtime)
            if clarification is not None:
                self._send_message(clarification)
                continue

            conflict = runtime.app.conflict_manager.state
            if conflict.active:
                self.conflict_seen = True
                actor = str(conflict.current_actor() or "").strip()
                if actor in KARIBA_HEROES:
                    beat = self.director.conflict_action(runtime)
                    if beat is None:
                        break
                    before_serial = int(conflict.turn_serial or 0)
                    self._send_message(beat)
                    after_actor = str(conflict.current_actor() or "").strip()
                    after_serial = int(conflict.turn_serial or 0)
                    signature = f"{actor}:{before_serial}"
                    if actor == after_actor and before_serial == after_serial:
                        if stalled_player_turn == signature:
                            stalled_player_count += 1
                        else:
                            stalled_player_turn = signature
                            stalled_player_count = 1
                    else:
                        stalled_player_turn = ""
                        stalled_player_count = 0
                    if stalled_player_count >= 3:
                        self.director.stalled_reason = (
                            f"【{actor}】在同一冲突行动位更换三种行动后，"
                            "规则状态仍未推进。"
                        )
                        break
                    continue
                if actor:
                    before_serial = int(conflict.turn_serial or 0)
                    self._send_heartbeat(
                        beat_id=f"npc-turn-{len(self.turns) + 1:02d}",
                        instruction=(
                            f"当前是【{actor}】的NPC回合。依据其战术快照执行一个合法行动，"
                            "结算后把镜头交给下一位行动者；不代替玩家角色行动。"
                        ),
                    )
                    after_actor = str(conflict.current_actor() or "").strip()
                    after_serial = int(conflict.turn_serial or 0)
                    if actor == after_actor and before_serial == after_serial:
                        if stalled_conflict_actor == actor:
                            stalled_conflict_count += 1
                        else:
                            stalled_conflict_actor = actor
                            stalled_conflict_count = 1
                    else:
                        stalled_conflict_actor = ""
                        stalled_conflict_count = 0
                    if stalled_conflict_count >= 2:
                        break
                    continue
                self.terminal_conflict_attempts += 1
                if self.terminal_conflict_attempts > 2:
                    self.director.stalled_reason = (
                        "冲突仍标记为进行中，但连续两次没有可行动者且未被正式收束。"
                    )
                    break
                resolution = runtime.app.conflict_manager.resolution_status()
                self._send_heartbeat(
                    beat_id=(
                        "terminal-conflict-resolution-"
                        f"{self.terminal_conflict_attempts}"
                    ),
                    instruction=(
                        "当前冲突没有可行动者。先读取conflict_resolution_status；"
                        "一方已经无人可行动时，依据已经成立的胜负、撤离或投降结果调用"
                        "end_conflict正式收束，并忠实描述结果；不要进入普通场景议程。"
                        if bool(resolution.get("ready_for_natural_end"))
                        else
                        "当前冲突仍标记为进行中，却没有当前行动者。读取完整冲突状态，"
                        "恢复合法行动表或按已经成立的客观结局调用end_conflict；"
                        "不要代替玩家角色行动，也不要进入普通场景议程。"
                    ),
                )
                continue

            self._detect_outcome_branch(runtime)
            if self.outcome_branch == "party_defeat":
                if any(
                    hero_name in conflict.fallen_pcs
                    for hero_name in KARIBA_HEROES
                ):
                    self.defeat_scene_attempts += 1
                    if self.defeat_scene_attempts > 2:
                        self.director.stalled_reason = (
                            "玩家方败北后，GM没有建立包含两名英雄的后果场景。"
                        )
                        break
                    self._send_heartbeat(
                        beat_id=(
                            "party-defeat-consequence-"
                            f"{self.defeat_scene_attempts}"
                        ),
                        instruction=(
                            "玩家方已经在越狱冲突中全部放弃抵抗，冲突也已经结束。"
                            "现在开启或转入一个明确发生在稍后时刻的后果场景，"
                            "让诺艾尔与艾丽妮都在场，并忠实兑现被俘或分离后果；"
                            "按场景生命周期让昏迷角色恢复到危机值。"
                            "给她们一个可以立即回应的具体处境，不把败北改写成成功越狱。"
                        ),
                    )
                    continue
                defeat_beat = self._next_defeat_aftermath_beat()
                if defeat_beat is None:
                    break
                self._send_message(defeat_beat)
                continue

            if self.outcome_branch == "split_capture":
                still_fallen = [
                    name
                    for name in self.split_captured_heroes
                    if name in conflict.fallen_pcs
                ]
                if still_fallen:
                    self.split_scene_attempts += 1
                    if self.split_scene_attempts > 2:
                        self.director.stalled_reason = (
                            "分头撤离后，GM没有为被俘英雄建立下一场后果场景。"
                        )
                        break
                    captured = "、".join(still_fallen)
                    self._send_heartbeat(
                        beat_id=(
                            "split-capture-consequence-"
                            f"{self.split_scene_attempts}"
                        ),
                        instruction=(
                            f"越狱冲突已经以分头结局结束：【{captured}】放弃抵抗，"
                            "另一名英雄已经脱离。现在为被俘英雄开启一个发生在稍后时刻的"
                            "独立后果场景，只移动被俘者并保留逃脱者原有分支；"
                            "按场景生命周期让被俘者恢复到危机值。忠实兑现被俘后果，"
                            "给出一个可以立即回应的具体处境，不把败北改写成逃脱。"
                        ),
                    )
                    continue
                split_beat = self._next_split_aftermath_beat()
                if split_beat is None:
                    break
                self._send_message(split_beat)
                continue

            if self.outcome_branch == "partial_capture":
                still_fallen = [
                    name
                    for name in self.partial_captured_heroes
                    if name in conflict.fallen_pcs
                ]
                if still_fallen:
                    self.partial_scene_attempts += 1
                    if self.partial_scene_attempts > 2:
                        self.director.stalled_reason = (
                            "部分败北后，GM没有为被俘英雄建立独立后果场景。"
                        )
                        break
                    captured = "、".join(still_fallen)
                    free = "、".join(self.partial_free_heroes)
                    self._send_heartbeat(
                        beat_id=(
                            "partial-capture-consequence-"
                            f"{self.partial_scene_attempts}"
                        ),
                        instruction=(
                            f"越狱冲突已经结束：【{captured}】放弃抵抗，而【{free}】"
                            "没有被判定败北或撤离。现在为被俘英雄开启一个发生在稍后时刻的"
                            "独立后果场景，只移动被俘者，保留另一名英雄当前真实位置和自由；"
                            "按场景生命周期让被俘者恢复到危机值。忠实兑现被俘后果，"
                            "不要把另一名英雄写成已经逃脱、被捕或跟随移动。"
                        ),
                    )
                    continue
                partial_beat = self._next_partial_aftermath_beat()
                if partial_beat is None:
                    break
                self._send_message(partial_beat)
                continue

            beat = self.director.next_scripted(
                runtime,
                turns=self.turns,
                conflict_seen=self.conflict_seen,
            )
            if beat is None:
                break
            if beat.kind == "idle":
                instruction = self._idle_instruction(beat.beat_id)
                self._send_heartbeat(
                    beat_id=beat.beat_id,
                    instruction=instruction,
                )
            else:
                self._send_message(beat)

        result = self._build_result(runtime)
        self._write_artifacts(result)
        return result

    def _next_defeat_aftermath_beat(self) -> KaribaSessionBeat | None:
        beats = (
            KaribaSessionBeat(
                "defeat-awakening-action",
                "测试玩家甲",
                "诺艾尔醒来后没有立刻挣扎，先确认自己和艾丽妮在哪里、身上还能取用什么，以及门外有谁。",
            ),
            KaribaSessionBeat(
                "defeat-companion-check",
                "loading",
                "艾丽妮确认诺艾尔也醒着后，小声问她：刚才守卫把我们带回来时，你有没有听见他们谈论换岗或下一次转运？",
                expectation="silent",
            ),
            KaribaSessionBeat(
                "defeat-next-intent",
                "测试玩家甲",
                "诺艾尔把能确认的情况告诉艾丽妮：这次没逃出去，但监狱确实藏着不愿让囚犯知道的安排。先活着记住这些，下一次不再硬闯。",
            ),
            KaribaSessionBeat(
                "defeat-end-session",
                "测试玩家甲",
                "今晚先到这里，麻烦收团、结算并保存这一场。",
                addressed=True,
            ),
        )
        if self.defeat_cursor >= len(beats):
            return None
        beat = beats[self.defeat_cursor]
        self.defeat_cursor += 1
        return beat

    def _next_split_aftermath_beat(self) -> KaribaSessionBeat | None:
        captured = (self.split_captured_heroes or ["艾丽妮"])[0]
        escaped = (self.split_escaped_heroes or ["诺艾尔"])[0]
        captured_speaker = self._speaker_for_hero_name(captured)
        escaped_speaker = self._speaker_for_hero_name(escaped)
        beats = (
            KaribaSessionBeat(
                "split-captured-awakening",
                captured_speaker,
                f"{captured}恢复意识后先不挣扎，确认自己被带到了哪里、身上还剩什么，以及门外有谁。",
            ),
            KaribaSessionBeat(
                "split-escaped-reaction",
                escaped_speaker,
                f"{escaped}已经脱离监狱，没有立刻折返送死；她先确认带出的证据和追兵动向，再记下营救同伴所需的入口。",
            ),
            KaribaSessionBeat(
                "split-captured-intent",
                captured_speaker,
                f"{captured}把眼前能确认的看守、出口和转运动静记在心里，决定先活着弄清自己会被送去哪里。",
                expectation="silent",
            ),
            KaribaSessionBeat(
                "split-end-session",
                escaped_speaker,
                "今晚先到这里，麻烦按这次分头结局收团、结算并保存这一场。",
                addressed=True,
            ),
        )
        if self.split_cursor >= len(beats):
            return None
        beat = beats[self.split_cursor]
        self.split_cursor += 1
        return beat

    def _next_partial_aftermath_beat(self) -> KaribaSessionBeat | None:
        captured = (self.partial_captured_heroes or ["诺艾尔"])[0]
        free = (self.partial_free_heroes or ["艾丽妮"])[0]
        captured_speaker = self._speaker_for_hero_name(captured)
        free_speaker = self._speaker_for_hero_name(free)
        beats = (
            KaribaSessionBeat(
                "partial-captured-awakening",
                captured_speaker,
                f"{captured}恢复意识后先不挣扎，确认自己被带到了哪里、身上还剩什么，以及门外有谁。",
            ),
            KaribaSessionBeat(
                "partial-free-reaction",
                free_speaker,
                f"{free}确认{captured}已经被带走后，留在自己实际所在的位置，先观察可用出口、追兵和能追查同伴去向的痕迹。",
            ),
            KaribaSessionBeat(
                "partial-captured-intent",
                captured_speaker,
                f"{captured}把眼前能确认的看守、出口和转运动静记在心里，决定先活着弄清自己会被送去哪里。",
            ),
            KaribaSessionBeat(
                "partial-end-session",
                free_speaker,
                "今晚先到这里，麻烦按一人被俘、另一人仍自由的结局收团、结算并保存这一场。",
                addressed=True,
            ),
        )
        if self.partial_cursor >= len(beats):
            return None
        beat = beats[self.partial_cursor]
        self.partial_cursor += 1
        return beat

    @staticmethod
    def _speaker_for_hero_name(hero_name: str) -> str:
        return "测试玩家甲" if hero_name == "诺艾尔" else "loading"

    @staticmethod
    def _idle_instruction(beat_id: str) -> str:
        if beat_id == "idle-session-closure":
            return (
                "两名英雄已经离开监狱并在安全地点停下。用一个具体的事后场景兑现今晚行动造成的直接后果，"
                "让一项已公开线索、获救者反应或追捕代价成为下次会记得的钩子；"
                "不要替玩家决定下一步，也不要虚构他们已经完成尚未完成的目标。"
            )
        if beat_id in {"idle-before-opposition", "idle-opposition-response"}:
            return (
                "玩家已经公开触碰监狱现有的守卫或防御阻力。让当前真正存在的对立方现在依据目标作出一个具体回应；"
                "可以提出最后条件、关闭退路或发动非致命阻拦。若双方行动已不可调和，使用正式冲突工具开战，"
                "不要只增加脚步声或另一轮预警，也不要替玩家角色行动。"
            )
        return (
            "桌面在一个自然决定点停顿。只在现有NPC、环境或对立方确实应当行动时推进一个新变化；"
            "若玩家正在等彼此回应或局面无需GM介入，就保持静默。"
        )

    def _send_message(self, beat: KaribaSessionBeat) -> None:
        index = len(self.turns) + 1
        self.sent_beats[index] = beat
        retry_root = self._retry_root_id(beat.beat_id)
        payload: dict[str, object] = {
            "campaign_id": self.campaign_id,
            "session_id": self.session_id,
            "channel_id": self.channel_id,
            "speaker": beat.speaker,
            "speaker_id": (
                "player-noel"
                if beat.speaker == "测试玩家甲"
                else "player-elinie"
            ),
            "message": beat.text,
            "message_id": f"kariba-session-{index}",
            "logical_source_event_id": (
                f"kariba:{self.campaign_id}:{self.session_id}:{retry_root}"
            ),
            "is_at_bot": beat.addressed,
            "is_reply_to_bot": beat.reply_to_gm,
        }
        if retry_root != beat.beat_id:
            payload["retry_reason"] = (
                "provider_unavailable"
                if "-provider-retry-" in beat.beat_id
                else "transaction_rolled_back"
            )
            try:
                payload["retry_attempt"] = int(beat.beat_id.rsplit("-", 1)[-1])
            except ValueError:
                payload["retry_attempt"] = 1
        if beat.quoted_text:
            payload["quoted_message"] = {
                "message_id": "kariba-invitation",
                "sender_id": "gm-shiyou",
                "text": beat.quoted_text,
                "source": "astrbot",
            }
        self._invoke(
            beat_id=beat.beat_id,
            kind=beat.kind,
            speaker=beat.speaker,
            message=beat.text,
            expectation=beat.expectation,
            method="POST",
            route="/v1/message/route",
            payload=payload,
        )

    def _rolled_back_message_retry(self) -> KaribaSessionBeat | None:
        """Repeat a player message only when its whole transaction rolled back."""

        if not self.turns:
            return None
        latest = self.turns[-1]
        if latest.route != "gm_agent_message_transaction_rolled_back":
            return None
        original = self.sent_beats.get(latest.index)
        if original is None:
            self.director.stalled_reason = "GM事务回滚后无法还原需要重发的玩家消息。"
            return None
        root_id = original.beat_id.split("-retry-", 1)[0]
        attempts = self.transaction_retry_attempts.get(root_id, 0) + 1
        self.transaction_retry_attempts[root_id] = attempts
        if attempts > 2:
            self.director.stalled_reason = f"玩家重发【{root_id}】两次后，GM事务仍持续回滚。"
            return None
        return KaribaSessionBeat(
            beat_id=f"{root_id}-retry-{attempts}",
            speaker=original.speaker,
            text=original.text,
            expectation=original.expectation,
            kind=original.kind,
            addressed=original.addressed,
            reply_to_gm=original.reply_to_gm,
            quoted_text=original.quoted_text,
        )

    def _provider_unavailable_message_retry(self) -> KaribaSessionBeat | None:
        """Retry one uncommitted player turn after bounded client recovery fails."""

        if not self.turns:
            return None
        latest = self.turns[-1]
        if not str(latest.route or "").startswith("gm_agent_unavailable"):
            return None
        original = self.sent_beats.get(latest.index)
        if original is None or original.expectation == "silent":
            return None
        if any(
            receipt.get("ok") is True and receipt.get("state_changed") is True
            for receipt in latest.receipts
        ):
            return None
        root_id = original.beat_id.split("-provider-retry-", 1)[0]
        attempts = self.provider_retry_attempts.get(root_id, 0) + 1
        self.provider_retry_attempts[root_id] = attempts
        retry_limit = max(0, int(getattr(self, "provider_retry_limit", 1)))
        if attempts > retry_limit:
            self.director.stalled_reason = (
                f"玩家消息【{root_id}】在客户端有界恢复后仍连续"
                f"{retry_limit + 1}次无法取得GM响应。"
            )
            return None
        delay_seconds = max(
            0.0,
            float(getattr(self, "provider_retry_delay_seconds", 0.0)),
        )
        event = {
            "beat_id": root_id,
            "attempt": attempts,
            "delay_seconds": delay_seconds,
            "source_turn": latest.index,
            "agent_error": latest.agent_error,
        }
        events = getattr(self, "provider_retry_events", None)
        if events is None:
            events = []
            self.provider_retry_events = events
        events.append(event)
        if delay_seconds > 0:
            print(
                f"[kariba] provider unavailable; wait {delay_seconds:g}s "
                f"before retrying {root_id} ({attempts}/{retry_limit})",
                flush=True,
            )
            sleeper = getattr(self, "_provider_retry_sleep", time.sleep)
            sleeper(delay_seconds)
        return KaribaSessionBeat(
            beat_id=f"{root_id}-provider-retry-{attempts}",
            speaker=original.speaker,
            text=original.text,
            expectation=original.expectation,
            kind=original.kind,
            addressed=original.addressed,
            reply_to_gm=original.reply_to_gm,
            quoted_text=original.quoted_text,
        )

    def _gm_clarification_followup(
        self,
        runtime: Any,
    ) -> KaribaSessionBeat | None:
        """Answer an explicit GM/NPC question before advancing the agenda."""

        if not self.turns:
            return None
        latest = self.turns[-1]
        reply = str(latest.reply or "").strip()
        if (
            not reply
            or latest.index in self.answered_gm_request_turns
            or "要投吗" in reply
            or "援用" in reply
        ):
            return None

        speaker = ""
        text = ""
        if (
            "请明确" in reply
            and "具体地点" in reply
            and any(token in reply for token in ("同行", "跟随", "哪些人"))
        ):
            destination = self.director._public_escape_destination(self.turns)
            if destination:
                speaker = "测试玩家甲"
                companion_names = [
                    persona.name
                    for persona in runtime.app.world_state.npc_personas.values()
                    if str(getattr(persona, "status", "active") or "active") == "active"
                    and runtime.app.scene_manager.actors_share_movement_origin(
                        "诺艾尔",
                        persona.name,
                    )
                    and persona.name not in KARIBA_HEROES
                ]
                companions = (
                    "；同行的是" + "、".join(companion_names)
                    if companion_names
                    else "；诺艾尔这一步先只移动自己"
                )
                text = (
                    f"诺艾尔明确说：先去【{destination}】{companions}。"
                    "她现在就沿这条路线撤离。"
                )
        elif "到底在找什么" in reply:
            speaker = "测试玩家甲"
            text = (
                "诺艾尔没有绕开哈根的问题：我们来拿回属于自己的装备，也要带走一份能证明囚犯被送去哪里的记录。"
                "你把钥匙交出来，我们不伤你。"
            )
        elif any(
            phrase in reply
            for phrase in (
                "具体打算怎么接",
                "打算怎么接",
                "准备怎么接",
                "开口谈，还是",
                "开口谈还是",
                "为什么从牢里出来",
                "为什么出来",
                "你怎么回应",
                "你怎么说",
            )
        ):
            speaker = latest.speaker if latest.speaker in {
                "测试玩家甲",
                "loading",
            } else "测试玩家甲"
            hero_name = "艾丽妮" if speaker == "loading" else "诺艾尔"
            text = (
                f"{hero_name}直接回答：封印先失控，牢门才松开。"
                "我们只拿回自己的东西，也不想伤人。你要什么条件，直说。"
            )

        if not text:
            return None
        self.answered_gm_request_turns.add(latest.index)
        return KaribaSessionBeat(
            beat_id=f"answer-gm-request-{latest.index}",
            speaker=speaker,
            text=text,
            reply_to_gm=True,
            quoted_text=reply,
        )

    def _send_heartbeat(self, *, beat_id: str, instruction: str) -> dict[str, object]:
        return self._invoke(
            beat_id=beat_id,
            kind="heartbeat",
            speaker="时悠",
            message="<桌面自然停顿>",
            expectation="gm_beat",
            method="POST",
            route="/v1/session/heartbeat",
            payload={
                "campaign_id": self.campaign_id,
                "session_id": self.session_id,
                "channel_id": self.channel_id,
                "auto_respond": True,
                "force": True,
                "cooldown_seconds": 0,
                "adventure_idle_seconds": 0,
                "pc_turn_idle_seconds": 0,
                "npc_turn_grace_seconds": 0,
                "instruction": instruction,
            },
        )

    def _invoke(
        self,
        *,
        beat_id: str,
        kind: str,
        speaker: str,
        message: str,
        expectation: str,
        method: str,
        route: str,
        payload: dict[str, object],
    ) -> dict[str, object]:
        clients_before = self._llm_client_registry()
        call_start_totals = {
            role: int(client.total_calls)
            for role, client in clients_before.items()
        }
        started = time.perf_counter()
        status, raw = self.service.handle(method, route, payload)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        body = raw if isinstance(raw, dict) else {"reply": str(raw)}
        model_call_count = 0
        recent_calls: list[dict[str, object]] = []
        for role, client in self._llm_client_registry().items():
            call_delta = max(
                0,
                int(client.total_calls) - int(call_start_totals.get(role, 0)),
            )
            model_call_count += call_delta
            if call_delta <= 0:
                continue
            # The production client intentionally caps recent_calls at 50
            # entries. Slice from the tail by the monotonic call-count delta so
            # a long run keeps observing new calls after that cap is reached.
            for call in client.recent_calls[
                -min(call_delta, len(client.recent_calls)) :
            ]:
                recent_calls.append({"client_role": role, **dict(call)})
        successful_calls = sum(
            1
            for call in recent_calls
            if bool(call.get("ok")) and not bool(call.get("response_empty"))
        )
        receipts = [
            dict(receipt)
            for receipt in list(body.get("tool_receipts") or [])
            if isinstance(receipt, dict)
        ]
        self.turns.append(
            KaribaSessionTurn(
                index=len(self.turns) + 1,
                beat_id=beat_id,
                kind=kind,
                speaker=speaker,
                message=message,
                expectation=expectation,
                status=status,
                elapsed_ms=elapsed_ms,
                target=str(body.get("target") or ""),
                route=str(body.get("route") or ""),
                send_reply=bool(body.get("send_reply")),
                reply=str(body.get("reply") or ""),
                agent_error=str(body.get("agent_error") or ""),
                model_call_count=model_call_count,
                successful_model_call_count=successful_calls,
                failed_model_call_count=model_call_count - successful_calls,
                model_call_records=recent_calls,
                agent_trace=[
                    dict(item)
                    for item in list(body.get("agent_trace") or [])
                    if isinstance(item, dict)
                ],
                receipts=receipts,
                state_after=self._state_snapshot(),
            )
        )
        self._write_checkpoint()
        latest = self.turns[-1]
        eligible_cache_calls = sum(
            1
            for call in latest.model_call_records
            if bool(dict(call.get("prompt_cache") or {}).get("eligible"))
        )
        reported_cache_hits = sum(
            1
            for call in latest.model_call_records
            if int(dict(call.get("usage") or {}).get("cached_tokens") or 0) > 0
        )
        print(
            (
                f"[kariba] {latest.index:02d}/{self.max_turns} "
                f"{latest.beat_id} {latest.elapsed_ms}ms "
                f"route={latest.route or '<none>'} "
                f"reply={'yes' if latest.reply else 'silent'} "
                f"cache={reported_cache_hits}/{eligible_cache_calls}"
            ),
            flush=True,
        )
        return body

    def _llm_client_registry(self) -> dict[str, Any]:
        """Return every unique LLM client participating in this test run."""

        candidates: list[tuple[str, Any]] = [
            ("core_gm", self.service.gm_agent_runtime.llm_client),
        ]
        runtime = self.runtime
        if runtime is not None:
            candidates.extend(
                [
                    ("scene_orchestrator", getattr(runtime.app, "llm_client", None)),
                    ("expressor", getattr(runtime.app, "expressor", None)),
                    ("summarizer", getattr(runtime.log_manager, "summarizer", None)),
                ]
            )

        grouped: dict[int, tuple[list[str], Any]] = {}
        for role, component in candidates:
            client = (
                component
                if hasattr(component, "telemetry_payload")
                else getattr(component, "client", None)
            )
            if client is None or not hasattr(client, "telemetry_payload"):
                continue
            key = id(client)
            if key not in grouped:
                grouped[key] = ([role], client)
            elif role not in grouped[key][0]:
                grouped[key][0].append(role)
        return {
            "+".join(roles): client
            for roles, client in grouped.values()
        }

    def _llm_telemetry_payload(self) -> dict[str, object]:
        clients = self._llm_client_registry()
        payloads = {
            role: client.telemetry_payload()
            for role, client in clients.items()
        }
        prompt_tokens = sum(
            max(0, int(client.prompt_token_total))
            for client in clients.values()
        )
        cached_tokens = sum(
            max(0, int(client.cached_token_total))
            for client in clients.values()
        )
        cache_write_tokens = sum(
            max(0, int(client.cache_write_token_total))
            for client in clients.values()
        )
        eligible_prompt_tokens = sum(
            max(0, int(client.cache_eligible_prompt_token_total))
            for client in clients.values()
        )
        eligible_cached_tokens = sum(
            max(0, int(client.cache_eligible_cached_token_total))
            for client in clients.values()
        )
        reported_prompt_tokens = sum(
            max(0, int(client.cache_usage_reported_prompt_token_total))
            for client in clients.values()
        )
        hit_latencies = sorted(
            latency
            for client in clients.values()
            for latency in client.cache_hit_latency_history_ms
        )
        miss_latencies = sorted(
            latency
            for client in clients.values()
            for latency in client.cache_miss_latency_history_ms
        )
        configured_modes = sorted(
            {
                str(client.config.prompt_cache_mode or "auto")
                for client in clients.values()
            }
        )
        capabilities = [
            {"client_role": role, **dict(capability)}
            for role, item in payloads.items()
            for capability in list(
                dict(item.get("prompt_cache") or {}).get("capabilities") or []
            )
        ]
        family_breakdown = [
            {"client_role": role, **dict(row)}
            for role, item in payloads.items()
            for row in list(
                dict(item.get("prompt_cache") or {}).get("by_family") or []
            )
        ]
        operation_breakdown = [
            {"client_role": role, **dict(row)}
            for role, item in payloads.items()
            for row in list(
                dict(item.get("prompt_cache") or {}).get("by_operation") or []
            )
        ]
        return {
            "total_calls": sum(
                max(0, int(client.total_calls)) for client in clients.values()
            ),
            "failed_calls": sum(
                max(0, int(client.failed_call_count))
                for client in clients.values()
            ),
            "prompt_cache": {
                "enabled": any(
                    bool(client.config.prompt_cache_enabled)
                    for client in clients.values()
                ),
                "configured_mode": ",".join(configured_modes),
                "eligible_calls": sum(
                    max(0, int(client.cache_eligible_calls))
                    for client in clients.values()
                ),
                "usage_reported_calls": sum(
                    max(0, int(client.cache_usage_reported_calls))
                    for client in clients.values()
                ),
                "hit_calls": sum(
                    max(0, int(client.cache_hit_calls))
                    for client in clients.values()
                ),
                "known_miss_calls": sum(
                    max(0, int(client.cache_known_miss_calls))
                    for client in clients.values()
                ),
                "prompt_tokens": prompt_tokens,
                "eligible_prompt_tokens": eligible_prompt_tokens,
                "reported_prompt_tokens": reported_prompt_tokens,
                "cached_tokens": cached_tokens,
                "cache_write_tokens": cache_write_tokens,
                "read_ratio": (
                    round(cached_tokens / prompt_tokens, 4)
                    if prompt_tokens
                    else 0.0
                ),
                "eligible_read_ratio": (
                    round(eligible_cached_tokens / eligible_prompt_tokens, 4)
                    if eligible_prompt_tokens
                    else 0.0
                ),
                "reported_read_ratio": (
                    round(cached_tokens / reported_prompt_tokens, 4)
                    if reported_prompt_tokens
                    else 0.0
                ),
                "write_ratio": (
                    round(cache_write_tokens / prompt_tokens, 4)
                    if prompt_tokens
                    else 0.0
                ),
                "hit_latency": {
                    "sample_count": len(hit_latencies),
                    "p50_ms": self._percentile(hit_latencies, 0.50),
                    "p95_ms": self._percentile(hit_latencies, 0.95),
                },
                "miss_latency": {
                    "sample_count": len(miss_latencies),
                    "p50_ms": self._percentile(miss_latencies, 0.50),
                    "p95_ms": self._percentile(miss_latencies, 0.95),
                },
                "capabilities": capabilities,
                "by_family": family_breakdown,
                "by_operation": operation_breakdown,
            },
            "clients": payloads,
        }

    def _pending_window_followup(
        self,
        runtime: Any,
    ) -> KaribaSessionBeat | None:
        manager = runtime.app.interceptor.decision_window_manager
        for window in manager.awaiting_player_response():
            if bool(window.payload.get("silent_failure_grace")):
                latest_public = str(self.turns[-1].reply or "") if self.turns else ""
                if "失败" not in latest_public:
                    continue
            attempts = int(self.window_attempts.get(window.window_id, 0))
            if attempts >= 2:
                continue
            speaker = self._speaker_for_hero(runtime, window.owner)
            if not speaker:
                continue
            text = self._strategic_window_reply_text(runtime, window)
            if not text:
                continue
            self.window_attempts[window.window_id] = attempts + 1
            return KaribaSessionBeat(
                beat_id=f"window-{window.kind}-{window.window_id[:8]}-{attempts + 1}",
                speaker=speaker,
                text=text,
                expectation="rule_window",
                reply_to_gm=True,
            )
        return None

    def _strategic_window_reply_text(self, runtime: Any, window: Any) -> str:
        """Spend a limited Fabula-point budget on consequential failed checks."""

        if window.kind not in {"trait_invocation", "bond_invocation"}:
            return self._window_reply_text(window)
        if window.payload.get("roll_success") is not False:
            return ""

        actor = str(window.owner or "").strip()
        if not actor or not runtime.app.character_manager.exists(actor):
            return ""
        character = runtime.app.character_manager.get(actor)
        if int(character.fabula_points or 0) <= 0:
            return ""
        if self.fabula_rerolls_by_actor.get(actor, 0) >= 2:
            return ""

        source_beat_id = next(
            (
                turn.beat_id
                for turn in reversed(self.turns)
                if turn.kind == "player"
                and not str(turn.beat_id or "").startswith("window-")
            ),
            "",
        )
        source_key = (actor, source_beat_id)
        used_sources = getattr(self, "fabula_reroll_sources", None)
        if used_sources is None:
            used_sources = set()
            self.fabula_reroll_sources = used_sources
        if source_key in used_sources:
            return ""
        high_stakes = (
            "work-lock",
            "help-lock",
            "leave-cell-row",
            "move-to-duty-room",
            "enter-property-room",
            "force-passage",
            "ritual",
            "escape",
            "conflict-",
            "lower-area",
            "reach-shelter",
        )
        if not any(marker in source_beat_id for marker in high_stakes):
            return ""

        if window.kind == "trait_invocation":
            legal_traits = {
                str(option.get("trait") or "").strip()
                for option in list(window.options or [])
                if str(option.get("trait") or "").strip()
            }
            preferred_traits = (
                character.identity,
                character.origin,
                character.theme,
            )
            for value in preferred_traits:
                choice = str(value or "").strip()
                if not choice or choice not in legal_traits:
                    continue
                rationale = self._trait_rationale(
                    actor=actor,
                    choice=choice,
                    character=character,
                    window=window,
                )
                if not rationale:
                    continue
                self.fabula_rerolls_by_actor[actor] = (
                    self.fabula_rerolls_by_actor.get(actor, 0) + 1
                )
                used_sources.add(source_key)
                return (
                    f"我花 1 点物语点，援用【{choice}】重掷两枚骰："
                    f"{rationale}。"
                )
        if window.kind == "bond_invocation":
            choice = next(
                (
                    str(option.get("target") or "").strip()
                    for option in list(window.options or [])
                    if str(option.get("target") or "").strip()
                ),
                "",
            )
            if choice:
                self.fabula_rerolls_by_actor[actor] = (
                    self.fabula_rerolls_by_actor.get(actor, 0) + 1
                )
                used_sources.add(source_key)
                return f"我花 1 点物语点，援用与【{choice}】的羁绊重掷。"
        return ""

    @staticmethod
    def _trait_rationale(
        *,
        actor: str,
        choice: str,
        character: Any,
        window: Any,
    ) -> str:
        source_action = window.payload.get("source_action")
        parameters = (
            source_action.get("parameters")
            if isinstance(source_action, dict)
            and isinstance(source_action.get("parameters"), dict)
            else {}
        )
        purpose = str(
            parameters.get("check_label")
            or parameters.get("target")
            or "眼前这次行动"
        ).strip()
        if choice == str(character.identity or "").strip():
            if actor == "诺艾尔":
                if not any(
                    token in purpose
                    for token in ("机关", "路线", "守卫", "锁", "通路", "出口", "封印", "证物", "撤离", "潜行")
                ):
                    return ""
                return f"作为秘宝猎人，我寻找机关、路线和守卫破绽的本事能帮助我处理【{purpose}】"
            if actor == "艾丽妮":
                if not any(
                    token in purpose
                    for token in ("魔法", "符文", "封印", "仪式", "法术", "奥术")
                ):
                    return ""
                return f"即使被放逐，我仍受过魔法训练，这些知识能帮助我处理【{purpose}】"
        if choice == str(character.origin or "").strip():
            if not any(
                token in purpose
                for token in ("地形", "道路", "环境", "故乡", "王国", "旅行")
            ):
                return ""
            return f"我来自【{choice}】，在那里学到的环境知识能帮助我处理【{purpose}】"
        if choice == str(character.theme or "").strip():
            if actor == "诺艾尔":
                if not any(
                    token in purpose
                    for token in ("突破", "逃", "出口", "守卫", "阻拦", "脱身")
                ):
                    return ""
                return f"我的野心不允许我在【{purpose}】前退下，我要亲手再试一次"
            if actor == "艾丽妮":
                if not any(
                    token in purpose
                    for token in ("同伴", "诺艾尔", "营救", "同行", "脱身", "撤离")
                ):
                    return ""
                return f"我不愿再让同行者被单独留下，【{purpose}】正关系到我追求的归属"
        return ""

    @staticmethod
    def _window_reply_text(window: Any) -> str:
        if window.kind == "check_roll_confirmation":
            return "投。"
        if window.kind in {"trait_invocation", "bond_invocation"}:
            return ""
        if window.kind == "critical_opportunity":
            return f"我把这次大成功的机会用于【优势】，目标是【{window.owner}】。"
        if window.kind == "opportunity_parameter":
            return f"这次机会的目标是【{window.owner}】。"
        if window.kind == "npc_fate":
            return "留他一命，解除武装并绑好，不做处决。"
        if window.kind == "zero_hp":
            return "我选择放弃抵抗，接受这次失败带来的后果。"
        if window.kind == "fumble_opportunity":
            return "请把这次大失败机会用于制造一个与当前行动直接相关的麻烦。"
        options = list(window.options or [])
        if options:
            option = options[0]
            label = str(
                option.get("label")
                or option.get("choice")
                or option.get("trait")
                or option.get("name")
                or ""
            ).strip()
            if label:
                return f"我选择【{label}】。"
        return ""

    def _settle_silent_failure_grace(self, runtime: Any) -> bool:
        """Let an unclaimed failed check mature through the real delivery path."""

        manager = runtime.app.interceptor.decision_window_manager
        window = next(
            (
                candidate
                for candidate in manager.pending()
                if bool(candidate.payload.get("silent_failure_grace"))
                and str(candidate.payload.get("failure_grace_token") or "").strip()
            ),
            None,
        )
        if window is None:
            return False
        attempts = self.failure_timeout_attempts.get(window.window_id, 0) + 1
        self.failure_timeout_attempts[window.window_id] = attempts
        if attempts > 1:
            self.director.stalled_reason = (
                f"失败检定窗口【{window.window_id}】到期后仍未释放；"
                "停止重复发送同一规则心跳。"
            )
            return False
        due_at = str(window.payload.get("failure_grace_due_at") or "").strip()
        try:
            due = datetime.fromisoformat(due_at.replace("Z", "+00:00"))
            if due.tzinfo is None:
                due = due.replace(tzinfo=timezone.utc)
            remaining = max(
                0.0,
                (due - datetime.now(timezone.utc)).total_seconds(),
            )
        except ValueError:
            remaining = float(
                max(0, int(window.payload.get("failure_grace_seconds") or 15))
            )
        if remaining:
            time.sleep(remaining + 0.05)
        token = str(window.payload.get("failure_grace_token") or "").strip()
        body = self._invoke(
            beat_id=f"failed-check-timeout-{window.window_id[:8]}",
            kind="heartbeat",
            speaker="时悠",
            message="<失败检定静默等待结束>",
            expectation="failed_check_timeout",
            method="POST",
            route="/v1/session/heartbeat",
            payload={
                "campaign_id": self.campaign_id,
                "session_id": self.session_id,
                "channel_id": self.channel_id,
                "auto_respond": True,
                "force": True,
                "defer_delivery_log": True,
                "rule_followup_kind": "failed_check_grace",
                "rule_followup_window_id": window.window_id,
                "rule_followup_token": token,
            },
        )
        delivery_id = str(body.get("delivery_id") or "").strip()
        if not delivery_id:
            still_pending = manager.find_pending(window_id=window.window_id)
            if still_pending is not None:
                self.director.stalled_reason = (
                    "失败检定到期后没有生成可确认的送达事务，且待决窗口仍未释放："
                    + str(body.get("reason") or "未知原因")
                )
            return True
        status, confirmation = self.service.handle(
            "POST",
            "/v1/session/heartbeat/delivered",
            {
                "campaign_id": self.campaign_id,
                "session_id": self.session_id,
                "channel_id": self.channel_id,
                "delivery_id": delivery_id,
            },
        )
        if status != 200 or not bool(confirmation.get("ok")):
            self.director.stalled_reason = (
                "失败检定叙述已经生成，但送达确认失败："
                + str(confirmation.get("error") or status)
            )
            return True
        if self.turns:
            self.turns[-1].state_after = self._state_snapshot()
            self._write_checkpoint()
        return True

    @staticmethod
    def _speaker_for_hero(runtime: Any, hero_name: str) -> str:
        for key, draft in runtime.app.world_state.world_profile.hero_drafts.items():
            if str(draft.hero_name or "").strip() == str(hero_name or "").strip():
                return str(draft.player_name or key or "").strip()
        return ""

    def _state_snapshot(self) -> dict[str, object]:
        runtime = self.service._runtime(self.campaign_id)
        app = runtime.app
        scene = app.scene_manager.current_scene
        conflict = app.conflict_manager.state
        gate = self.service.session_gates.get(
            self.campaign_id,
            self.channel_id,
            self.session_id,
        )
        return {
            "gate_status": gate.status,
            "ledger_active": bool(app.session_ledger.active),
            "ledger_settled": bool(app.session_ledger.settled),
            "scene": {
                "id": str(scene.scene_id if scene is not None else ""),
                "name": str(scene.name if scene is not None else ""),
                "type": str(scene.scene_type.value if scene is not None else ""),
                "location": str(scene.location if scene is not None else ""),
                "participants": list(scene.participants if scene is not None else []),
            },
            "character_locations": {
                name: app.scene_manager.location_of(name)
                for name in KARIBA_HEROES
                if app.character_manager.exists(name)
            },
            "scene_history_count": len(app.scene_manager.history),
            "conflict": {
                "active": bool(conflict.active),
                "round": int(conflict.round_number or 0),
                "current_actor": str(conflict.current_actor() or ""),
                "turn_serial": int(conflict.turn_serial or 0),
                "fallen_pcs": dict(conflict.fallen_pcs),
                "resolution_status": (
                    runtime.app.conflict_manager.resolution_status()
                ),
            },
            "pending_windows": [
                {
                    "id": window.window_id,
                    "kind": window.kind,
                    "owner": window.owner,
                    "blocking": bool(window.blocking),
                }
                for window in app.interceptor.decision_window_manager.pending()
            ],
            "clocks": [
                {
                    "name": clock.name,
                    "current": clock.current,
                    "max": clock.max_segments,
                    "scope": clock.scope,
                    "status": clock.status,
                }
                for clock in app.clock_manager.all()
            ],
            "heroes": {
                name: {
                    "hp": app.character_manager.get(name).hp,
                    "mp": app.character_manager.get(name).mp,
                    "level": app.character_manager.get(name).level,
                    "xp": app.character_manager.get(name).experience_points,
                    "fabula_points": app.character_manager.get(name).fabula_points,
                    "unavailable_equipment": sorted(
                        app.character_manager.get(name).unavailable_equipment
                    ),
                    "main_hand": app.character_manager.get(name).equipped_main_hand,
                    "off_hand": app.character_manager.get(name).equipped_off_hand,
                }
                for name in KARIBA_HEROES
                if app.character_manager.exists(name)
            },
        }

    def _build_result(self, runtime: Any) -> dict[str, object]:
        app = runtime.app
        gate = self.service.session_gates.get(
            self.campaign_id,
            self.channel_id,
            self.session_id,
        )
        receipts = [
            receipt for turn in self.turns for receipt in turn.receipts
        ]
        failed_receipts = [
            receipt for receipt in receipts if receipt.get("ok") is False
        ]
        recovered_receipts, unrecovered_receipts = self._classify_failed_receipts()
        recovered_agent_errors, unrecovered_agent_errors = (
            self._classify_agent_errors()
        )
        expected_silence = [
            turn for turn in self.turns if turn.expectation == "silent"
        ]
        silent_state_writes = [
            turn
            for turn in expected_silence
            if any(
                receipt.get("ok") is True
                and receipt.get("state_changed") is True
                and not self._receipt_allows_silent_commit(receipt)
                for receipt in turn.receipts
            )
        ]
        unexpected_silence = [
            turn
            for turn in self.turns
            if turn.expectation not in {"silent", "gm_beat"}
            and not bool(turn.send_reply and str(turn.reply or "").strip())
        ]
        public_rows = [
            {
                "label": turn.beat_id,
                "message": turn.message,
                "reply": turn.reply,
                "elapsed_ms": turn.elapsed_ms,
                "expected_send_reply": turn.expectation not in {"silent", "gm_beat"},
                "body": {
                    "tool_receipts": turn.receipts,
                    "route": turn.route,
                    "agent_error": turn.agent_error,
                },
            }
            for turn in self.turns
        ]
        quality = ConversationQualityAuditor().audit(public_rows).as_dict()
        scene_records = [
            *app.scene_manager.history,
            *app.scene_manager.active_scenes(),
        ]
        scene_names = list(
            dict.fromkeys(scene.name for scene in scene_records if scene.name)
        )
        pending = app.interceptor.decision_window_manager.pending()
        latencies = sorted(turn.elapsed_ms for turn in self.turns)
        llm_telemetry = self._llm_telemetry_payload()
        final_locations = {
            name: app.scene_manager.location_of(name)
            for name in KARIBA_HEROES
        }
        heroes_created = all(
            app.character_manager.exists(name)
            for name in KARIBA_HEROES
        )
        first_location = final_locations.get(KARIBA_HEROES[0], "")
        heroes_reached_aftermath = bool(
            first_location
            and all(
                self.director._safe_aftermath_location(location)
                and app.scene_manager.locations_overlap(
                    first_location,
                    location,
                )
                for location in final_locations.values()
            )
        )
        loadouts_restored = heroes_created and all(
            not app.character_manager.get(name).unavailable_equipment
            and str(app.character_manager.get(name).equipped_main_hand or "")
            not in {"", "徒手攻击"}
            for name in KARIBA_HEROES
        )
        defeated_with_consequence = {
            name
            for name in KARIBA_HEROES
            if bool(app.conflict_manager.state.pc_defeat_consequences.get(name))
        }
        equipment_outcome_resolved = heroes_created and all(
            set(app.character_manager.get(name).unavailable_equipment)
            <= self.director.abandoned_equipment.get(name, set())
            or name in defeated_with_consequence
            for name in KARIBA_HEROES
        )
        end_receipt = next(
            (
                receipt
                for turn in reversed(self.turns)
                for receipt in turn.receipts
                if receipt.get("tool_name") == "end_session"
                and receipt.get("ok") is True
            ),
            None,
        )
        end_result = (
            dict(end_receipt.get("result") or {})
            if isinstance(end_receipt, dict)
            else {}
        )
        progress = app.story_arc_manager.state.current_session_progress
        stalled_reason = str(self.director.stalled_reason or "").strip()
        if not stalled_reason and not heroes_created:
            stalled_reason = "启动事务未能建立两名测试角色，长测无法进入第一章。"
        if self.outcome_branch == "party_defeat":
            all_outcome_beats_reached = self.defeat_cursor >= 4
        elif self.outcome_branch == "split_capture":
            all_outcome_beats_reached = self.split_cursor >= 4
        elif self.outcome_branch == "partial_capture":
            all_outcome_beats_reached = self.partial_cursor >= 4
        else:
            all_outcome_beats_reached = self.director.cursor >= len(
                self.director.beats
            )
        branched_aftermath_resolved = bool(
            self.outcome_branch in {"split_capture", "partial_capture"}
            and all(
                name not in app.conflict_manager.state.fallen_pcs
                and bool(final_locations.get(name))
                for name in KARIBA_HEROES
            )
        )
        false_premise_replies = [
            turn.reply
            for turn in self.turns
            if turn.beat_id == "false-premise-manor"
        ]
        consecutive_npc_turns = any(
            previous.beat_id.startswith("npc-turn-")
            and current.beat_id.startswith("npc-turn-")
            for previous, current in zip(self.turns, self.turns[1:])
        )
        assertions = {
            "entered_adventure": any(
                turn.state_after.get("gate_status") == "adventure"
                for turn in self.turns
            ),
            "session_ended_and_settled": bool(app.session_ledger.settled)
            and not bool(app.session_ledger.active),
            "at_least_three_scene_records": len(scene_names) >= 3,
            "sufficient_substantial_scene_evidence": bool(
                len(progress.substantial_scene_ids) >= 3
                or (
                    len(progress.substantial_scene_ids) >= 2
                    and bool(progress.closure_ready)
                )
            ),
            "conflict_occurred": self.conflict_seen,
            "at_least_twenty_eight_table_turns": len(self.turns) >= 28,
            "no_unresolved_decision_windows": not pending,
            "no_state_writes_on_player_dialogue": not silent_state_writes,
            "no_unexpected_silence_on_required_reply": not unexpected_silence,
            "no_unrecovered_agent_errors": not unrecovered_agent_errors,
            "no_unrecovered_tool_failures": not unrecovered_receipts,
            "no_false_premise_hidden_fact_leak": not any(
                "卡里巴庄园" in reply or "庄园的运输单" in reply
                for reply in false_premise_replies
            ),
            "no_vague_success_placeholders": int(
                quality.get("vague_placeholder_gm_outputs") or 0
            ) == 0,
            "no_consecutive_npc_turns_while_pc_can_act": not consecutive_npc_turns,
            "all_scheduled_beats_reached": all_outcome_beats_reached,
            "no_stage_stall": not bool(stalled_reason),
            "both_heroes_reached_same_aftermath": (
                heroes_reached_aftermath
                or branched_aftermath_resolved
                or (
                    self.outcome_branch == "party_defeat"
                    and all(
                        name not in app.conflict_manager.state.fallen_pcs
                        for name in KARIBA_HEROES
                    )
                    and bool(first_location)
                    and all(
                        app.scene_manager.locations_overlap(
                            first_location,
                            location,
                        )
                        for location in final_locations.values()
                    )
                )
            ),
            "equipment_outcome_resolved": equipment_outcome_resolved,
            "session_closure_ready": bool(progress.closure_ready),
            "end_receipt_has_authoritative_snapshot": bool(
                end_result.get("final_state_snapshot")
            ),
        }
        return {
            "provider": self.provider.name,
            "model": self.provider.model,
            "endpoint_host": self.provider.endpoint_host,
            "campaign_id": self.campaign_id,
            "session_id": self.session_id,
            "gate_status": gate.status,
            "turn_count": len(self.turns),
            "scene_names": scene_names,
            "substantial_scene_ids": list(progress.substantial_scene_ids),
            "dense_two_scene_closure": bool(
                len(progress.substantial_scene_ids) == 2
                and progress.closure_ready
            ),
            "conflict_seen": self.conflict_seen,
            "outcome_branch": self.outcome_branch or "escape_attempt",
            "stalled_reason": stalled_reason,
            "equipment_outcome": {
                "opening_loadouts_restored": loadouts_restored,
                "deliberately_left_behind": {
                    name: sorted(items)
                    for name, items in self.director.abandoned_equipment.items()
                    if items
                },
            },
            "pending_windows": [
                {
                    "id": window.window_id,
                    "kind": window.kind,
                    "owner": window.owner,
                }
                for window in pending
            ],
            "failed_tool_receipts": failed_receipts,
            "recovered_tool_receipts": recovered_receipts,
            "unrecovered_tool_receipts": unrecovered_receipts,
            "recovered_agent_errors": recovered_agent_errors,
            "unrecovered_agent_errors": unrecovered_agent_errors,
            "silent_state_write_beats": [
                turn.beat_id for turn in silent_state_writes
            ],
            "assertions": assertions,
            "passed": all(assertions.values()),
            "latency": {
                "p50_ms": int(median(latencies)) if latencies else 0,
                "p95_ms": self._percentile(latencies, 0.95),
                "max_ms": max(latencies, default=0),
            },
            "model_calls": {
                "total": sum(turn.model_call_count for turn in self.turns),
                "successful": sum(
                    turn.successful_model_call_count for turn in self.turns
                ),
                "failed": sum(
                    turn.failed_model_call_count for turn in self.turns
                ),
            },
            "provider_recovery": {
                "retry_limit": self.provider_retry_limit,
                "retry_delay_seconds": self.provider_retry_delay_seconds,
                "events": list(self.provider_retry_events),
            },
            "llm_telemetry": llm_telemetry,
            "conversation_quality": quality,
            "final_state": self._state_snapshot(),
            "turns": [asdict(turn) for turn in self.turns],
        }

    def _classify_failed_receipts(
        self,
    ) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        """Separate corrected tool retries from failures left unresolved.

        Raw failures remain in the report because they are useful model-
        efficiency evidence.  A correction loop is operationally successful
        when the same tool, or the explicitly requested next tool, succeeds
        later in that same player transaction.
        """

        recovered: list[dict[str, object]] = []
        unrecovered: list[dict[str, object]] = []
        for turn in self.turns:
            for index, receipt in enumerate(turn.receipts):
                if receipt.get("ok") is not False:
                    continue
                later_receipts = turn.receipts[index + 1 :]
                later_successes = {
                    str(item.get("tool_name") or "").strip()
                    for item in later_receipts
                    if item.get("ok") is True
                }
                result = receipt.get("result")
                result = dict(result) if isinstance(result, dict) else {}
                required: set[str] = set()
                next_tool = str(result.get("required_next_tool") or "").strip()
                if next_tool:
                    required.add(next_tool)
                for key in ("required_next_tools", "required_followup_tools"):
                    value = result.get(key)
                    if isinstance(value, list):
                        required.update(
                            str(item or "").strip()
                            for item in value
                            if str(item or "").strip()
                        )
                tool_name = str(receipt.get("tool_name") or "").strip()
                annotated = {
                    "turn_index": turn.index,
                    "beat_id": turn.beat_id,
                    **dict(receipt),
                }
                source_event = result.get("source_event")
                source_event_id = str(
                    source_event.get("event_id")
                    if isinstance(source_event, dict)
                    else ""
                ).strip()
                corrected_by_state_change = any(
                    self._receipt_resolves_same_source_event(
                        candidate,
                        source_event_id=source_event_id,
                    )
                    for candidate in later_receipts
                ) if receipt.get("retryable") is True else False
                if (
                    tool_name in later_successes
                    or bool(required & later_successes)
                    or corrected_by_state_change
                ):
                    recovered.append(annotated)
                elif self._failure_recovered_by_transaction_retry(turn):
                    annotated["recovered_by_turn"] = self._recovery_turn_index(turn)
                    recovered.append(annotated)
                elif self._turn_safely_absorbed_tool_rejection(turn, receipt):
                    annotated["recovered_by_terminal_route"] = turn.route
                    recovered.append(annotated)
                else:
                    unrecovered.append(annotated)
        return recovered, unrecovered

    def _classify_agent_errors(
        self,
    ) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        recovered: list[dict[str, object]] = []
        unrecovered: list[dict[str, object]] = []
        for turn in self.turns:
            error = str(turn.agent_error or "").strip()
            if not error:
                continue
            annotated: dict[str, object] = {
                "turn_index": turn.index,
                "beat_id": turn.beat_id,
                "route": turn.route,
                "error": error,
            }
            recovery_index = self._recovery_turn_index(turn)
            if recovery_index:
                annotated["recovered_by_turn"] = recovery_index
                recovered.append(annotated)
            elif any(
                receipt.get("ok") is True
                and receipt.get("state_changed") is True
                for receipt in turn.receipts
            ):
                annotated["recovered_by_turn"] = turn.index
                recovered.append(annotated)
            else:
                unrecovered.append(annotated)
        return recovered, unrecovered

    def _failure_recovered_by_transaction_retry(
        self,
        turn: KaribaSessionTurn,
    ) -> bool:
        if turn.route != "gm_agent_message_transaction_rolled_back":
            return False
        return bool(self._recovery_turn_index(turn, require_state_change=True))

    @staticmethod
    def _turn_safely_absorbed_tool_rejection(
        turn: KaribaSessionTurn,
        receipt: dict[str, object],
    ) -> bool:
        """Treat a fail-closed correction as handled, while retaining it."""

        if receipt.get("retryable") is not True or turn.agent_error:
            return False
        if turn.route in {
            "gm_agent_message_transaction_rolled_back",
            "gm_agent_fail_closed",
        } or str(turn.route or "").startswith("gm_agent_unavailable"):
            return False
        return bool(
            turn.route == "gm_agent_silent"
            or (turn.send_reply and str(turn.reply or "").strip())
        )

    def _recovery_turn_index(
        self,
        turn: KaribaSessionTurn,
        *,
        require_state_change: bool = False,
    ) -> int:
        root_id = self._retry_root_id(turn.beat_id)
        for candidate in self.turns:
            if candidate.index <= turn.index:
                continue
            if self._retry_root_id(candidate.beat_id) != root_id:
                continue
            if candidate.agent_error or candidate.status < 200 or candidate.status >= 300:
                continue
            if str(candidate.route or "").startswith("gm_agent_unavailable"):
                continue
            if candidate.route == "gm_agent_message_transaction_rolled_back":
                continue
            if require_state_change and not any(
                receipt.get("ok") is True
                and receipt.get("state_changed") is True
                for receipt in candidate.receipts
            ):
                continue
            return candidate.index
        return 0

    @staticmethod
    def _retry_root_id(beat_id: str) -> str:
        clean = str(beat_id or "")
        clean = clean.split("-provider-retry-", 1)[0]
        return clean.split("-retry-", 1)[0]

    @staticmethod
    def _receipt_allows_silent_commit(receipt: dict[str, object]) -> bool:
        result = receipt.get("result")
        return bool(
            isinstance(result, dict)
            and result.get("silent_commit_allowed") is True
        )

    @staticmethod
    def _receipt_resolves_same_source_event(
        receipt: dict[str, object],
        *,
        source_event_id: str,
    ) -> bool:
        """Accept a corrected specialized write within the same player turn."""

        if receipt.get("ok") is not True or receipt.get("state_changed") is not True:
            return False
        result = receipt.get("result")
        result = dict(result) if isinstance(result, dict) else {}
        candidate_source = result.get("source_event")
        candidate_event_id = str(
            candidate_source.get("event_id")
            if isinstance(candidate_source, dict)
            else ""
        ).strip()
        if source_event_id and candidate_event_id:
            return source_event_id == candidate_event_id
        # Receipts in one KaribaSessionTurn all originate from the same routed
        # table message.  A successful write is therefore a valid correction
        # for an earlier retryable tool-selection failure even when an older
        # fixture omitted source provenance.
        return True

    def _write_artifacts(self, result: dict[str, object]) -> None:
        (self.output_root / "report.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (self.output_root / "conversation.txt").write_text(
            self._render_conversation(),
            encoding="utf-8",
        )

    def _write_checkpoint(self) -> None:
        """Persist partial evidence so a provider/network interruption is auditable."""

        payload = {
            "provider": self.provider.name,
            "model": self.provider.model,
            "campaign_id": self.campaign_id,
            "session_id": self.session_id,
            "turn_count": len(self.turns),
            "director_cursor": self.director.cursor,
            "stalled_reason": self.director.stalled_reason,
            "conflict_seen": self.conflict_seen,
            "provider_recovery": {
                "retry_limit": self.provider_retry_limit,
                "retry_delay_seconds": self.provider_retry_delay_seconds,
                "attempts": dict(self.provider_retry_attempts),
                "events": list(self.provider_retry_events),
            },
            "turns": [asdict(turn) for turn in self.turns],
        }
        (self.output_root / "checkpoint.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (self.output_root / "conversation.partial.txt").write_text(
            self._render_conversation(),
            encoding="utf-8",
        )

    def _render_conversation(self) -> str:
        lines = [
            f"FU-GM 卡里巴村完整第一场：{self.provider.name}",
            f"model: {self.provider.model}",
            f"endpoint: {self.provider.endpoint_host}",
            "",
        ]
        for turn in self.turns:
            lines.extend(
                [
                    (
                        f"--- {turn.index:02d}. {turn.beat_id} | {turn.kind} | "
                        f"{turn.elapsed_ms}ms | expected={turn.expectation} ---"
                    ),
                    f"{turn.speaker}: {turn.message}",
                    f"时悠: {turn.reply}" if turn.reply else "时悠: <静默>",
                ]
            )
            if turn.agent_error:
                lines.append(f"agent_error: {turn.agent_error}")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    @staticmethod
    def _percentile(values: list[int], ratio: float) -> int:
        if not values:
            return 0
        index = max(
            0,
            min(len(values) - 1, int(round((len(values) - 1) * ratio))),
        )
        return int(values[index])


__all__ = [
    "KaribaFirstSessionDirector",
    "KaribaFirstSessionRunner",
    "KaribaSessionBeat",
    "KaribaSessionTurn",
]
