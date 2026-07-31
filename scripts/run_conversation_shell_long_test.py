from __future__ import annotations

import json
import os
import re
import shutil
import sys
import time
from dataclasses import replace
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from fu_gm.config import LLMConfig  # noqa: E402
from fu_gm.http_server import FUGMHttpService  # noqa: E402
from fu_gm.llm_client import OpenAICompatibleClient  # noqa: E402
from fu_gm.llm_utils import extract_json_object  # noqa: E402
from fu_gm.prompt_cache import build_cache_friendly_messages  # noqa: E402


PLAYER_PROFILES = {
    "阿凛": "偏果断，喜欢先保护普通人；会征求队友意见，但不会替别人做决定。角色是伊莉雅。",
    "白河": "谨慎、技术派，常从现场物件和因果关系入手；角色是洛岚。",
    "南星": "关心 NPC 感受，偶尔用一句轻松的话缓和气氛；角色是赛璃。",
}


class ConversationShellLongHarness:
    def __init__(self) -> None:
        self.stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.run_root = PROJECT_ROOT / ".runtime" / "large_tests" / f"conversation_shell_{self.stamp}"
        self.data_root = self.run_root / "campaigns"
        self.run_root.mkdir(parents=True, exist_ok=True)
        self.data_root.mkdir(parents=True, exist_ok=True)
        self.conversation_path = self.run_root / "完整对话记录.txt"
        self.report_json_path = self.run_root / "conversation_shell_report.json"
        self.report_txt_path = self.run_root / "conversation_shell_report.txt"
        self.progress_path = self.run_root / "progress.jsonl"
        self.campaign_id = f"会话壳真人感长测_{self.stamp}"
        self.session_id = "qq-group-session"
        self.channel_id = "qq-group-200000001-sim"
        self.bot_id = "900000001"
        self.service = FUGMHttpService(data_root=self.data_root, use_llm=True)
        self.calls: list[dict[str, Any]] = []
        self.errors: list[str] = []
        self.findings: list[str] = []
        self.player_generation: list[dict[str, Any]] = []
        self.message_counter = 0
        self.last_route_payload: dict[str, Any] = {}
        self.heartbeat: dict[str, Any] = {}
        self.map_artifact_path = ""
        self.adventure_started = False
        self.duplicate_ok = False
        self.player_client, self.player_model = self._player_client()
        self.conversation_path.write_text(
            "\n".join(
                [
                    "FU-GM 会话壳多人长测完整对话",
                    f"campaign_id: {self.campaign_id}",
                    f"started_at: {datetime.now().isoformat()}",
                    "说明：玩家只看到本文件中此前公开的消息，不读取 GM 暗线或测试答案。",
                    "",
                ]
            ),
            encoding="utf-8",
        )

    def _player_client(self) -> tuple[OpenAICompatibleClient | None, str]:
        config = LLMConfig.from_env()
        if not config.api_key:
            return None, ""
        player_config = replace(
            config,
            timeout_seconds=float(os.environ.get("FU_GM_LONG_TEST_PLAYER_TIMEOUT_SECONDS", "45")),
            reactive_recovery_enabled=False,
            reactive_recovery_max_retries=0,
        )
        model = os.environ.get("FU_GM_REPLAY_PLAYER_MODEL", "").strip() or config.action_model
        return OpenAICompatibleClient(player_config), model

    def run(self) -> int:
        try:
            self._new_campaign()
            self._pre_session_flow()
            self._session_zero_world_flow()
            self._character_flow()
            gate = self._enter_adventure_with_recovery()
            if not gate.get("blocked") and (gate.get("gate") or {}).get("status") == "adventure":
                self.adventure_started = True
                self._adventure_flow()
            else:
                self.errors.append("完成恢复后仍无法进入第一章。")
            report = self._build_report()
            self._write_report(report)
            return 0 if report["ok"] else 1
        except Exception as exc:
            self.errors.append(f"长测未捕获异常：{type(exc).__name__}: {exc}")
            report = self._build_report(exception=exc)
            self._write_report(report)
            return 1
        finally:
            print(f"RUN_ROOT={self.run_root}", flush=True)
            print(f"CONVERSATION_TXT={self.conversation_path}", flush=True)
            print(f"REPORT_JSON={self.report_json_path}", flush=True)
            print(f"REPORT_TXT={self.report_txt_path}", flush=True)

    def _new_campaign(self) -> None:
        status, body = self.service.handle("POST", "/v1/campaigns/new", {"campaign_id": self.campaign_id})
        if status != 200 or not isinstance(body, dict) or not body.get("ok"):
            raise RuntimeError(f"新建战役失败：{body}")

    def _pre_session_flow(self) -> None:
        self.send_dynamic(
            "未开团的群友闲聊",
            "阿凛",
            "刚到群里，和另外两名玩家轻松打招呼，不要叫 GM，也不要谈具体规则。",
            "我刚泡好茶，今晚大家慢慢来，别一上来就把世界炸了。",
            expected_target="astrbot",
            expected_reply=False,
            must_avoid_gm_name=True,
        )
        self.send(
            "明确准备开团",
            "阿凛",
            "时悠，我们三个人准备开一场《最终物语》，先聊清楚基调和安全边界。",
            expected_target="fu_gm",
            expected_reply=True,
        )
        self.send_dynamic(
            "玩家讨论基调",
            "阿凛",
            "向另外两名玩家提议王道但不轻飘的冒险基调，句尾问他们意见，不要请 GM 裁决。",
            "我想走有希望的王道冒险，但失败也得留下代价。你们俩觉得会不会太正经？",
            expected_target=("silent", "fu_gm"),
            expected_reply=False,
            must_avoid_gm_name=True,
        )
        self.send_dynamic(
            "玩家回应基调",
            "白河",
            "直接回应阿凛，赞成并补充队伍可以争论但不互相拆台，不要重复她整句话。",
            "我赞成。队内可以吵，但别靠藏信息和背刺制造冲突，最后得能坐下来谈。",
            expected_target=("silent", "fu_gm"),
            expected_reply=False,
            must_avoid_gm_name=True,
        )
        self.send(
            "安全边界声明",
            "南星",
            "我补一条界限：不出现性暴力和针对儿童的残酷虐待；身体病变与亲密场景放在帷幕后淡出。",
            expected_target="fu_gm",
            expected_reply=True,
        )
        self.send(
            "艾特GM总结共识",
            "白河",
            "我们大致说完了。能用两三句帮我们确认目前的基调、队内分歧处理和安全边界吗？",
            expected_target="fu_gm",
            expected_reply=True,
            is_at_bot=True,
        )
        self.send(
            "明确进入第零章",
            "南星",
            "时悠，我们都同意了，现在开始第零章吧。",
            expected_target="fu_gm",
            expected_reply=True,
        )

    def _session_zero_world_flow(self) -> None:
        self.send_dynamic(
            "世界名提案",
            "阿凛",
            "只向队友提出世界叫白钟大陆这个暂定名，明确还没定，问他们意见。",
            "我先扔个暂定名：白钟大陆。只是觉得听起来像会出事，你们不喜欢就换。",
            expected_target=("silent", "fu_gm"),
            expected_reply=False,
            must_avoid_gm_name=True,
        )
        self.send(
            "队友确认世界名",
            "白河",
            "白钟大陆我赞成，就按这个名字写吧。",
            expected_target="fu_gm",
            expected_reply=None,
        )
        self.send(
            "地图与世界形状",
            "阿凛",
            "地图是一片类地球大陆：西边鸦羽山脉，中央镜线内海与外海相通，东南是潮鸢群岛，南岸有雾潮海岸。",
            expected_target="fu_gm",
            expected_reply=None,
        )
        self.send(
            "魔法与科技定位",
            "白河",
            "魔法与科技并存。灵魂晶炉驱动车辆和工坊，古老御魂术与元素仪式负责安抚灵魂之河。",
            expected_target="fu_gm",
            expected_reply=None,
        )
        contributions = [
            ("国家贡献 阿凛", "阿凛", "我在国家这一项贡献钟鸣公国：它在镜线内海北岸，正午大钟能安抚亡者，也让钟匠议会控制公共哀悼。"),
            ("国家贡献 白河", "白河", "我的国家贡献是第七采掘城。辉钢财团控制这里的矿道、工坊和记忆炉，城市靠出售记忆燃料维持繁荣。"),
            ("国家贡献 南星", "南星", "我贡献潮鸢群岛，各岛追着季风迁徙，居民用飞翼船往来，也信奉一位从不在同一座岛停留的海风神。"),
            ("历史贡献 阿凛", "阿凛", "我的重大历史事件是三十年前碎月坠落，整片大陆的钟都慢了一拍，赤羽旧王都也在那一夜消失。"),
            ("历史贡献 白河", "白河", "我贡献的历史事件是记忆炉首次启动时吞掉了一整条矿道工人的姓名，后来官方记录把他们改成了无人驾驶事故。"),
            ("历史贡献 南星", "南星", "我的历史事件是第一次归潮祭后少了一座岛，但所有海图和人的记忆都自行补上了空缺。"),
            ("奥秘贡献 阿凛", "阿凛", "我想探索的世界奥秘是：为什么姐姐的名字刻在白花风铃内侧，却没人记得她是否真的死过？"),
            ("奥秘贡献 白河", "白河", "我的奥秘是第七采掘城的紧急停机协议为什么只回应赤羽遗民的歌，而不是工程师的权限。"),
            ("奥秘贡献 南星", "南星", "我想知道每年消失的岛究竟去了哪里，以及岛上的灵魂为什么没有回到灵魂之河。"),
            ("威胁贡献 阿凛", "阿凛", "我的世界威胁是辉钢财团正收购灰晶病患者的记忆，把它们加工成不会反抗的魔导兵器。"),
            ("威胁贡献 白河", "白河", "我贡献的威胁是监察官艾蕾娜推动记忆集中管理，她相信只有国家保管全部记忆，历史才不会再次被改写。"),
            ("威胁贡献 南星", "南星", "我的威胁是苍白司教团把灰晶病说成灵魂升格，诱导病人主动交出名字与记忆。"),
        ]
        for label, speaker, message in contributions:
            self.send(label, speaker, message, expected_target="fu_gm", expected_reply=None)
        self.send(
            "点名GM补充地点",
            "阿凛",
            "时悠，我再补一个起始地点：白花碑驿站在雾潮海岸北侧，由赤羽遗民的白花守望会管理。",
            expected_target="fu_gm",
            expected_reply=True,
        )
        self.send_dynamic(
            "小队原型提案",
            "白河",
            "向队友提议三人是临时组成的护送队，负责把失忆旅人与碎月遗物送到钟鸣公国；明确先问意见。",
            "小队要不要先定成临时护送队？把失忆旅人和碎月遗物送去钟鸣公国，路上再决定要不要一起追真相。",
            expected_target=("silent", "fu_gm"),
            expected_reply=False,
            must_avoid_gm_name=True,
        )
        self.send(
            "队友确认小队原型",
            "南星",
            "这个同行理由我同意，就按临时护送队定下来。至少我们不会刚见面就假装是生死之交。",
            expected_target="fu_gm",
            expected_reply=None,
        )
        self.send_dynamic(
            "第零章桌边玩笑",
            "南星",
            "对刚才不必假装生死之交这句话开一个很短的玩笑，只和队友说，不新增世界事实。",
            "很好，那我可以先把你们写进‘暂时不至于互相丢下’那一栏。",
            expected_target=("silent", "fu_gm"),
            # 时悠可以偶尔接一句桌边短评，也可以让玩家间玩笑自然落下；
            # 这里审计回复长度和内容，不把两种真人节奏硬编码成唯一答案。
            expected_reply=None,
            must_avoid_gm_name=True,
        )
        self.send(
            "确认第一幕起点",
            "阿凛",
            "我们也确认第一幕从白花碑驿站开始：临时护送队接下失忆旅人与碎月遗物，准备前往钟鸣公国。",
            expected_target="fu_gm",
            expected_reply=None,
        )

    def _character_flow(self) -> None:
        cores = [
            (
                "角色核心 阿凛",
                "阿凛",
                "时悠，我的玩家名是阿凛，角色叫伊莉雅。身份是赤羽遗民的盾誓骑士；主题是责任；故乡是白花碑驿站。"
                "职业分配守护者3级、元素使2级；属性骰敏捷d8、洞察d8、力量d10、意志d6。",
            ),
            (
                "角色核心 白河",
                "白河",
                "我的角色叫洛岚，身份是辉钢财团出逃的魔导工匠；主题是赎罪；故乡是第七采掘城。"
                "职业分配造物使3级、武器大师2级；属性骰敏捷d8、洞察d10、力量d8、意志d6。",
            ),
            (
                "角色核心 南星",
                "南星",
                "我的角色叫赛璃，身份是钟鸣公国的御魂医师；主题是希望；故乡是钟鸣公国。"
                "职业分配御魂使3级、旅人2级；属性骰敏捷d6、洞察d10、力量d8、意志d8。",
            ),
        ]
        for label, speaker, message in cores:
            self.send(label, speaker, message, expected_target="fu_gm", expected_reply=None)

        skill_rounds = [
            [("阿凛", "伊莉雅第一项技能选保镖。"), ("白河", "洛岚第一项技能选便携装置。"), ("南星", "赛璃第一项技能选灵魂魔法。")],
            [("阿凛", "伊莉雅第二项技能选防御精通。"), ("白河", "洛岚第二项技能选秘密配方。"), ("南星", "赛璃第二项技能再选一次灵魂魔法。")],
            [("阿凛", "伊莉雅第三项技能选挺身守护。"), ("白河", "洛岚第三项技能选先见之明。"), ("南星", "赛璃第三项技能选御魂系仪式。")],
            [("阿凛", "伊莉雅第四项技能选元素魔法。"), ("白河", "洛岚第四项技能选碎骨。"), ("南星", "赛璃第四项技能选见多识广。")],
            [("阿凛", "伊莉雅第五项技能选元素系仪式。"), ("白河", "洛岚第五项技能选破防打击。"), ("南星", "赛璃第五项技能选充足补给。")],
        ]
        for round_number, choices in enumerate(skill_rounds, start=1):
            for speaker, message in choices:
                self.send(
                    f"逐项选技能 R{round_number} {speaker}",
                    speaker,
                    message,
                    expected_target="fu_gm",
                    expected_reply=None,
                )
            if round_number == 2:
                self.send_dynamic(
                    "建卡中玩家商量",
                    "白河",
                    "问队友目前队伍是不是缺远程手段，不要求马上改卡，也不要叫 GM。",
                    "我们现在是不是有点缺稳定的远程手段？先记着，装备轮再看要不要补。",
                    expected_target=("silent", "fu_gm"),
                    expected_reply=False,
                    must_avoid_gm_name=True,
                )

        finals = [
            (
                "角色完成 阿凛",
                "阿凛",
                "伊莉雅的元素魔法选择元素幕障。初始装备钢匕首、青铜盾、旅行装束。"
                "她对赛璃有信赖羁绊。责任会让她优先守住无辜者的名字与退路。伊莉雅确认角色并正式建卡。",
            ),
            (
                "角色完成 白河",
                "白河",
                "洛岚的便携装置选择魔导装置。初始装备铁锤、旅行装束。"
                "他对伊莉雅有钦佩羁绊。赎罪会让他在财团机器伤人时站出来补救。洛岚确认角色并正式建卡。",
            ),
            (
                "角色完成 南星",
                "南星",
                "赛璃的灵魂魔法选择治愈术与屏障。初始装备法杖、旅行装束。"
                "她对伊莉雅有信赖羁绊。希望会让她在最糟的时候仍先救人。赛璃确认角色并正式建卡。",
            ),
        ]
        for label, speaker, message in finals:
            self.send(label, speaker, message, expected_target="fu_gm", expected_reply=None)

    def _enter_adventure_with_recovery(self) -> dict[str, Any]:
        gate = self.send(
            "确认进入第一章",
            "阿凛",
            "时悠，大家的世界和角色都定好了，我们一致同意进入第一章。请由你先描述开场。",
            expected_target="fu_gm",
            expected_reply=True,
        )
        for attempt in range(2):
            if not gate.get("blocked"):
                return gate
            self._repair_from_blockers(gate, attempt + 1)
            gate = self.send(
                f"修补后再次进入第一章 {attempt + 1}",
                "阿凛",
                "时悠，刚才缺的项目已经补完，我们再次确认进入第一章。请先描述现场。",
                expected_target="fu_gm",
                expected_reply=True,
            )
        return gate

    def _repair_from_blockers(self, gate: dict[str, Any], attempt: int) -> None:
        missing_heroes = (gate.get("hero_creation") or {}).get("missing_by_player") or {}
        cards = {
            "伊莉雅": (
                "阿凛",
                "补充核对伊莉雅：盾誓骑士，主题责任，故乡白花碑驿站；守护者3、元素使2；"
                "敏捷d8、洞察d8、力量d10、意志d6；技能保镖、防御精通、挺身守护、元素魔法、元素系仪式；"
                "法术元素幕障；装备钢匕首、青铜盾、旅行装束。确认角色并正式建卡。",
            ),
            "洛岚": (
                "白河",
                "补充核对洛岚：出逃魔导工匠，主题赎罪，故乡第七采掘城；造物使3、武器大师2；"
                "敏捷d8、洞察d10、力量d8、意志d6；技能便携装置、秘密配方、先见之明、碎骨、破防打击；"
                "便携装置选择魔导装置；装备铁锤、旅行装束。确认角色并正式建卡。",
            ),
            "赛璃": (
                "南星",
                "补充核对赛璃：御魂医师，主题希望，故乡钟鸣公国；御魂使3、旅人2；"
                "敏捷d6、洞察d10、力量d8、意志d8；技能灵魂魔法2、御魂系仪式、见多识广、充足补给；"
                "法术治愈术、屏障；装备法杖、旅行装束。确认角色并正式建卡。",
            ),
        }
        for label in missing_heroes:
            hero = next((name for name in cards if name in str(label)), "")
            if hero:
                speaker, message = cards[hero]
                self.send(
                    f"角色阻塞修补 {attempt} {hero}",
                    speaker,
                    message,
                    expected_target="fu_gm",
                    expected_reply=True,
                )
        if gate.get("session_zero"):
            repairs = [
                ("阿凛", "世界创建核对：我的国家、历史、奥秘与威胁贡献都保留；地图是白钟大陆，安全边界沿用开团前共识。"),
                ("白河", "世界创建核对：我已贡献第七采掘城、记忆炉事故、停机协议奥秘与艾蕾娜的记忆集中威胁。"),
                ("南星", "世界创建核对：我已贡献潮鸢群岛、归潮祭事件、失踪岛奥秘与苍白司教团威胁；小队是临时护送队。"),
            ]
            for speaker, message in repairs:
                self.send(
                    f"世界阻塞修补 {attempt} {speaker}",
                    speaker,
                    message,
                    expected_target="fu_gm",
                    expected_reply=True,
                )

    def _adventure_flow(self) -> None:
        self.send(
            "开场后玩家商量 1",
            "白河",
            "先别急着往前冲。你们觉得应该先护住旅人，还是先看清门外那阵动静？",
            expected_target="silent",
            expected_reply=False,
        )
        self.send(
            "开场后玩家商量 2",
            "南星",
            "我觉得先护住旅人更稳，门口风险交给洛岚和伊莉雅盯；赛璃先准备补位，别让人和遗物分开。",
            expected_target="silent",
            expected_reply=False,
        )
        first_action = self.send_dynamic(
            "第一章实际行动 1",
            "阿凛",
            "以伊莉雅身份观察院门与守门人的动作，确认他是否准备突然拦人、关门或靠近旅人；"
            "消息里明确写出院门、守门人和观察目的，只声明做法，不声明成功。",
            "伊莉雅没有越过院门，先盯住守门人按着门闩的手，确认他是在防备门外，还是准备突然关门拦人或靠近旅人。",
            expected_target="fu_gm",
            expected_reply=True,
        )
        original_payload = dict(self.last_route_payload)
        duplicate = self._invoke_route("平台重复投递同一行动", original_payload, expected_target="fu_gm", expected_reply=True)
        self.duplicate_ok = bool(duplicate.get("deduplicated")) and (
            (duplicate.get("reply_envelopes") or [{}])[0].get("target_message_id")
            == (first_action.get("reply_envelopes") or [{}])[0].get("target_message_id")
        )
        if not self.duplicate_ok:
            self.errors.append("相同 QQ message_id 的重复投递没有复用原回复信封。")
        self._answer_post_check_prompt("第一次检定选择", "阿凛", "伊莉雅", first_action)
        second_action = self.send_dynamic(
            "第一章实际行动 2",
            "白河",
            "让洛岚基于 GM 最新回复，和一个已经公开出现的 NPC 或现场物件互动；不要凭空创造钟匠、旅人或车辙。",
            "洛岚蹲到刚才最可疑的机械痕迹旁，只检查磨损方向和残留温度，想判断它是刚停下还是已经离开很久。",
            expected_target="fu_gm",
            expected_reply=True,
        )
        self._answer_post_check_prompt("第二次检定选择", "白河", "洛岚", second_action)
        self.send(
            "第一章实际行动 3",
            "南星",
            "赛璃走到失名旅人能看见的侧面，不碰他怀里的遗物，只轻声问：你现在还撑得住吗？如果不方便说，点一下头就好。",
            expected_target="fu_gm",
            expected_reply=True,
        )
        self.send(
            "向守门人提出具体请求",
            "白河",
            "洛岚把那道中断的拖痕先记住，再回头问守门人能不能借用桌上碎月遗物外面的灰布，把旅人和遗物先隔开。",
            expected_target="fu_gm",
            expected_reply=True,
        )
        self.send(
            "无文字别名但实际艾特",
            "白河",
            "以后检定时把属性和难度等级写清楚就好，失败后果等结果出来再说。",
            expected_target="fu_gm",
            expected_reply=True,
            is_at_bot=True,
        )
        recent_gm = self._latest_gm_reply()
        self.send(
            "引用GM消息追问",
            "阿凛",
            "这件事和我们刚才看到的现场压力是同一条线吗？如果还不能确定，直接说不能确定就好。",
            expected_target="fu_gm",
            expected_reply=True,
            is_reply_to_bot=True,
            quoted_text=recent_gm[-500:],
        )
        self.heartbeat = self._invoke(
            "玩家停顿后的GM主动节拍",
            "POST",
            "/v1/session/heartbeat",
            {
                "campaign_id": self.campaign_id,
                "session_id": self.session_id,
                "channel_id": self.channel_id,
                "speaker": "系统心跳",
                "force": True,
                "auto_respond": True,
                "adventure_idle_seconds": 1,
                "pc_turn_idle_seconds": 1,
                "npc_turn_grace_seconds": 1,
                "instruction": "玩家短暂停顿；让已经在场的 NPC 明确回应，或让已公开压力自然变化一拍，不替玩家行动。",
            },
        )
        if self.heartbeat.get("send_reply") and self.heartbeat.get("reply"):
            self._append_proactive("时悠主动节拍", str(self.heartbeat.get("reply")))
        self.send_dynamic(
            "主动节拍后的玩家回应",
            "南星",
            "直接回应 GM 主动节拍中最新发生的事，明确说出刚才开口的 NPC 称呼；选择一个具体反应，不要换场或跳到预定剧情。",
            "赛璃转向刚刚开口的守门人，确认他愿意让我们做什么，而不是替他决定。",
            expected_target="fu_gm",
            expected_reply=True,
        )
        self.send_dynamic(
            "第一章桌边玩笑",
            "阿凛",
            "在不打断局势的前提下，对队友说一句很短的玩笑，不叫 GM、不声明行动结果。",
            "至少这次不是门一开就有巨龙，先算半个好消息。",
            expected_target=("silent", "fu_gm"),
            expected_reply=None,
            must_avoid_gm_name=True,
        )
        self.send(
            "自然称呼GM的场内短问",
            "南星",
            "时悠，我只确认一下，现在仍然是同一个场景，对吧？",
            expected_target="fu_gm",
            expected_reply=True,
        )
        self.send(
            "本次长测收团",
            "白河",
            "时悠，今天先到这里，收团吧。",
            expected_target="fu_gm",
            expected_reply=True,
        )

    def _answer_post_check_prompt(
        self,
        label: str,
        speaker: str,
        hero_name: str,
        result: dict[str, Any],
    ) -> None:
        reply = str(result.get("reply") or "")
        if "你想把它用在" in reply or "选择机会" in reply:
            self.send(
                label,
                speaker,
                f"{hero_name}把这次机会用于优势。",
                expected_target="fu_gm",
                expected_reply=True,
            )
            return
        if "这个结果还没定稿" in reply or "这次差一点" in reply:
            self.send(
                label,
                speaker,
                f"{hero_name}不消耗物语点，接受这次结果。",
                expected_target="fu_gm",
                expected_reply=None,
            )

    def send_dynamic(
        self,
        label: str,
        speaker: str,
        directive: str,
        fallback: str,
        *,
        expected_target: str | tuple[str, ...],
        expected_reply: bool | None,
        must_avoid_gm_name: bool = False,
    ) -> dict[str, Any]:
        message = self._generate_player_line(
            speaker,
            directive,
            fallback,
            must_avoid_gm_name=must_avoid_gm_name,
        )
        return self.send(
            label,
            speaker,
            message,
            expected_target=expected_target,
            expected_reply=expected_reply,
        )

    def _generate_player_line(
        self,
        speaker: str,
        directive: str,
        fallback: str,
        *,
        must_avoid_gm_name: bool,
    ) -> str:
        if self.player_client is None or not self.player_model:
            self.player_generation.append({"speaker": speaker, "fallback": True, "reason": "no_player_model"})
            return fallback
        context = self._public_context(limit=10)
        prompt = (
            f"玩家：{speaker}\n玩家习惯：{PLAYER_PROFILES[speaker]}\n"
            f"当前公开聊天：\n{context or '暂无'}\n\n"
            f"这次想表达的意图：{directive}\n"
            "只输出这一名玩家此刻会发到群里的一条自然中文消息。不得写 GM 的回应、结果、骰点、后台状态或测试说明；"
            "不要使用‘如果……就……’替代真实决定，不要新增尚未公开的人物、线索或地点。"
        )
        if must_avoid_gm_name:
            prompt += "这句话只对其他玩家说，不得出现时悠、GM、主持人、悠老师，也不要请求规则裁决。"
        started = time.perf_counter()
        try:
            raw = self.player_client.create_chat_completion(
                model=self.player_model,
                messages=build_cache_friendly_messages(
                    static_system_prompt=(
                        "你只扮演一名真人桌游玩家。你只能知道公开聊天和自己的玩家习惯。"
                        "不要替主持人叙事，不追求覆盖测试点，不输出角色名标签或引号外说明。"
                    ),
                    user_content=prompt,
                ),
                temperature=0.85,
            )
            message = self._clean_player_line(raw, speaker)
            invalid = (
                not message
                or len(message) > 360
                or any(token in message for token in ("测试", "系统提示", "action_type", "JSON"))
                or (must_avoid_gm_name and any(token in message for token in ("时悠", "GM", "主持人", "悠老师")))
                or (
                    any(token in directive for token in ("不声明行动", "只开一个很短的玩笑", "只和队友说"))
                    and any(
                        token in message
                        for token in (
                            "我先",
                            "我去",
                            "我来",
                            "我这边先",
                            "我的角色先",
                            "我们先",
                            "大家先",
                            "那就先",
                            "先把人",
                            "先把门",
                            "现在就走",
                        )
                    )
                )
            )
            if invalid:
                message = fallback
            self.player_generation.append(
                {
                    "speaker": speaker,
                    "fallback": invalid,
                    "elapsed_ms": int((time.perf_counter() - started) * 1000),
                    "message": message,
                }
            )
            return message
        except Exception as exc:
            self.player_generation.append(
                {
                    "speaker": speaker,
                    "fallback": True,
                    "elapsed_ms": int((time.perf_counter() - started) * 1000),
                    "reason": f"{type(exc).__name__}: {exc}",
                }
            )
            return fallback

    @staticmethod
    def _clean_player_line(raw: str, speaker: str) -> str:
        text = str(raw or "").replace("旅旅人", "旅人").strip().strip("`").strip()
        text = re.sub(rf"^(?:{re.escape(speaker)}|玩家)\s*[：:]\s*", "", text)
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        message = " ".join(lines[:2]).strip()
        matching_wrappers = (("“", "”"), ('"', '"'), ("「", "」"), ("『", "』"))
        for opening, closing in matching_wrappers:
            if message.startswith(opening) and message.endswith(closing):
                message = message[len(opening) : -len(closing)].strip()
                break
        return message

    def send(
        self,
        label: str,
        speaker: str,
        message: str,
        *,
        expected_target: str | tuple[str, ...],
        expected_reply: bool | None,
        is_at_bot: bool = False,
        is_reply_to_bot: bool = False,
        quoted_text: str = "",
    ) -> dict[str, Any]:
        self.message_counter += 1
        message_id = f"qq-sim-{self.message_counter:04d}"
        payload: dict[str, Any] = {
            "campaign_id": self.campaign_id,
            "session_id": self.session_id,
            "channel_id": self.channel_id,
            "speaker": speaker,
            "speaker_id": f"player-{speaker}",
            "message": message,
            "message_id": message_id,
            "is_at_bot": is_at_bot,
            "is_reply_to_bot": is_reply_to_bot,
            "astrbot_context": {
                "sender_id": f"player-{speaker}",
                "sender_name": speaker,
                "group_id": self.channel_id,
                "self_id": self.bot_id,
                "is_private": False,
                "is_at_bot": is_at_bot,
                "is_reply_to_bot": is_reply_to_bot,
                "segment_types": ["text", *( ["at"] if is_at_bot else [] ), *( ["reply"] if is_reply_to_bot else [] )],
            },
        }
        if is_reply_to_bot:
            payload["quoted_message"] = {
                "message_id": f"gm-sim-{self.message_counter - 1:04d}",
                "sender_id": self.bot_id,
                "text": quoted_text,
                "source": "qq",
            }
        self.last_route_payload = dict(payload)
        return self._invoke_route(label, payload, expected_target=expected_target, expected_reply=expected_reply)

    def _invoke_route(
        self,
        label: str,
        payload: dict[str, Any],
        *,
        expected_target: str | tuple[str, ...],
        expected_reply: bool | None,
    ) -> dict[str, Any]:
        body = self._invoke(label, "POST", "/v1/message/route", payload)
        target = str(body.get("target") or "")
        send_reply = bool(body.get("send_reply"))
        expected_targets = (expected_target,) if isinstance(expected_target, str) else expected_target
        if target not in expected_targets:
            self.errors.append(f"{label}：target={target!r}，预期 {expected_target!r}。")
        if expected_reply is not None and send_reply != expected_reply:
            self.errors.append(f"{label}：send_reply={send_reply}，预期 {expected_reply}。")
        if send_reply:
            envelopes = body.get("reply_envelopes") or []
            if not envelopes:
                self.errors.append(f"{label}：可见回复缺少 ReplyEnvelope。")
            elif str(envelopes[0].get("target_message_id") or "") != str(payload.get("message_id") or ""):
                self.errors.append(f"{label}：回复信封没有指向原 QQ 消息。")
        return body

    def _invoke(self, label: str, method: str, route: str, payload: dict[str, Any]) -> dict[str, Any]:
        started = time.perf_counter()
        status, raw = self.service.handle(method, route, payload)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        body = raw if isinstance(raw, dict) else {"ok": status < 400, "raw": str(raw)}
        self._capture_world_map(body)
        record = {
            "index": len(self.calls) + 1,
            "label": label,
            "method": method,
            "route": route,
            "status": status,
            "elapsed_ms": elapsed_ms,
            "ok": bool(body.get("ok", status < 400)),
            "speaker": str(payload.get("speaker") or ""),
            "message": str(payload.get("message") or ""),
            "message_id": str(payload.get("message_id") or ""),
            "target": str(body.get("target") or ""),
            "resolved_route": str(body.get("route") or ""),
            "send_reply": bool(body.get("send_reply")),
            "reply": str(body.get("reply") or body.get("message") or ""),
            "blocked": bool(body.get("blocked")),
            "deduplicated": bool(body.get("deduplicated")),
            "body": body,
        }
        self.calls.append(record)
        with self.progress_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        if route == "/v1/message/route":
            self._append_conversation(record)
        if status >= 400 or not record["ok"]:
            self.errors.append(f"{label}：HTTP {status}，{body.get('error') or body.get('message') or 'unknown error'}")
        print(
            f"[{record['index']:02d}] {label} | {record['speaker'] or '-'} | "
            f"target={record['target'] or '-'} route={record['resolved_route'] or route} "
            f"reply={record['send_reply']} {elapsed_ms}ms",
            flush=True,
        )
        return body

    def _capture_world_map(self, body: dict[str, Any]) -> None:
        world_map = body.get("world_map")
        if not isinstance(world_map, dict) or world_map.get("status") != "generated":
            return
        source = Path(str(world_map.get("output_path") or ""))
        if not source.is_file():
            return
        destination = self.run_root / "生成地图.png"
        if source.resolve() != destination.resolve():
            shutil.copy2(source, destination)
        self.map_artifact_path = str(destination)

    def _append_conversation(self, record: dict[str, Any]) -> None:
        lines = [
            f"--- {record['index']:02d}. {record['label']} | {record['elapsed_ms']}ms | "
            f"target={record['target'] or '-'} route={record['resolved_route'] or '-'} ---",
            f"{record['speaker']}: {record['message']}",
        ]
        if record["reply"]:
            if record["deduplicated"]:
                lines.append("时悠: （平台重复投递；复用上一条回复，未再次结算。）")
            else:
                lines.append(f"时悠: {record['reply']}")
        else:
            lines.append("时悠: （静默）")
        lines.append("")
        with self.conversation_path.open("a", encoding="utf-8") as handle:
            handle.write("\n".join(lines))

    def _append_proactive(self, label: str, reply: str) -> None:
        with self.conversation_path.open("a", encoding="utf-8") as handle:
            handle.write(f"--- {label} ---\n时悠: {reply}\n\n")

    def _public_context(self, *, limit: int) -> str:
        lines: list[str] = []
        for call in self.calls[-limit:]:
            if call.get("route") != "/v1/message/route":
                continue
            lines.append(f"{call['speaker']}: {call['message']}")
            if call.get("reply"):
                lines.append(f"时悠: {call['reply']}")
        return "\n".join(lines)[-6000:]

    def _latest_gm_reply(self) -> str:
        return next((str(call.get("reply") or "") for call in reversed(self.calls) if call.get("reply")), "")

    def _build_report(self, *, exception: Exception | None = None) -> dict[str, Any]:
        runtime = self.service._runtime(self.campaign_id)
        audit_status, audit_raw = self.service.handle(
            "GET",
            f"/v1/audit/dashboard?campaign_id={self.campaign_id}&session_id={self.session_id}&channel_id={self.channel_id}",
        )
        audit = audit_raw if audit_status == 200 and isinstance(audit_raw, dict) else {}
        route_calls = [call for call in self.calls if call["route"] == "/v1/message/route"]
        replies = [call["reply"] for call in route_calls if call["reply"] and not call["deduplicated"]]
        target_counts = {
            target: sum(1 for call in route_calls if call["target"] == target)
            for target in ("fu_gm", "silent", "astrbot")
        }
        mechanical_tokens = (
            "这笔我记下了",
            "下一步可以",
            "接下来可以",
            "互动焦点",
            "当前目标",
            "已记录并立即应用",
            "FU-GM",
            "action_type",
            "规则结算拦截",
            "没有返回文本",
            "只愿意答应一件具体事",
            "这些细节不像孤立巧合",
            "把目光引向同一处隐情",
        )
        mechanical_hits = [
            {"index": call["index"], "token": token, "reply": call["reply"][:300]}
            for call in route_calls
            for token in mechanical_tokens
            if token in call["reply"]
        ]
        repeated_pairs: list[dict[str, Any]] = []
        for left, right in zip(replies, replies[1:]):
            normalized_left = re.sub(r"\s+", "", left)
            normalized_right = re.sub(r"\s+", "", right)
            similarity = SequenceMatcher(None, normalized_left, normalized_right).ratio()
            if similarity >= 0.84 and min(len(normalized_left), len(normalized_right)) >= 12:
                repeated_pairs.append({"similarity": round(similarity, 3), "left": left[:240], "right": right[:240]})
        hero_drafts = runtime.app.session_zero_manager.state.world.hero_drafts
        hero_validation = {}
        for key, draft in hero_drafts.items():
            formal_pc_exists = bool(
                draft.hero_name
                and runtime.app.character_manager.exists(draft.hero_name)
                and "pc" in runtime.app.character_manager.get(draft.hero_name).traits
            )
            if formal_pc_exists:
                hero_validation[key] = {
                    "hero_name": draft.hero_name,
                    "ready": True,
                    "missing": [],
                    "errors": [],
                    "confirmed": draft.confirmed,
                    "formal_pc_exists": True,
                }
            else:
                validation = runtime.app.validate_hero_draft(key)
                hero_validation[key] = {
                    "hero_name": draft.hero_name,
                    "ready": validation.ready,
                    "missing": list(validation.missing_fields),
                    "errors": list(validation.errors),
                    "confirmed": draft.confirmed,
                    "formal_pc_exists": False,
                }
        transcript = self.conversation_path.read_text(encoding="utf-8")
        llm_audit = self._llm_audit(transcript)
        if mechanical_hits:
            self.findings.append(f"玩家可见回复仍出现 {len(mechanical_hits)} 处机械模板词。")
            if any(hit["token"] == "只愿意答应一件具体事" for hit in mechanical_hits):
                self.errors.append("NPC 回应退化成泛化模板，没有根据当前人物和问题作答。")
        if repeated_pairs:
            self.findings.append(f"相邻 GM 回复中发现 {len(repeated_pairs)} 组高相似表达。")
        for call in route_calls:
            player_message = str(call.get("message") or "")
            gm_reply = str(call.get("reply") or "")
            if (
                any(token in player_message for token in ("门缝外", "栅栏边"))
                and any(token in player_message for token in ("马上", "立刻"))
                and " 对 退路 的检定" in gm_reply
            ):
                self.errors.append("即时门外观察被保护目的‘退路’劫持成了错误调查目标。")
            if any(token in player_message for token in ("盯门口", "门口那阵动静", "监视痕迹")):
                if " 对 不是" in gm_reply or " 对 是不是" in gm_reply:
                    self.errors.append("门口观察的条件句被截成了检定对象。")
                if "苍白司教团" in gm_reply and "苍白司教团" not in player_message:
                    self.errors.append("门口局部观察错误泄露了不相关的全局势力线索。")
            if (
                "守门人" in player_message
                and any(token in player_message for token in ("木牌", "规矩", "手势"))
                and any(token in player_message for token in ("盯", "看", "观察", "检查", "确认"))
            ):
                if " 对 旅人 的检定" in gm_reply:
                    self.errors.append("观察守门人与门边规矩时，调查目标被保护对象‘旅人’劫持。")
                if "苍白司教团" in gm_reply and "苍白司教团" not in player_message:
                    self.errors.append("观察守门人与门边规矩时泄露了无关的全局威胁。")
            if (
                "守门人" in player_message
                and any(token in player_message for token in ("院门", "门口", "门边", "门闩", "关门"))
                and any(token in player_message for token in ("盯", "看", "观察", "检查", "确认"))
            ):
                if " 对 旅人 的检定" in gm_reply:
                    self.errors.append("观察院门与守门人动作时，调查目标被保护对象‘旅人’劫持。")
                if "苍白司教团" in gm_reply and "苍白司教团" not in player_message:
                    self.errors.append("观察院门与守门人动作时泄露了无关的全局威胁。")
            if "灰布" in player_message and any(token in player_message for token in ("固定", "压稳", "压住", "掀起", "隔开")):
                if "接下来就看" in gm_reply or "怎么把这层隔开" in gm_reply:
                    self.errors.append("玩家已经处理灰布，GM 却把同一动作重新抛回给玩家。")
            if "拖痕" in player_message and "侧路" in player_message and " 对 门口与柜台周边 的检定" in gm_reply:
                self.errors.append("侧路拖痕调查仍被泛化成门口与柜台周边。")
            if "【迫近的威胁】0/" in gm_reply or "[迫近的威胁] 0/" in gm_reply:
                self.errors.append("调查凭空公开了零进度的泛化威胁命刻。")
            if "旅人" in player_message and any(token in player_message for token in ("撑得住", "点个头")):
                if "只愿意答应一件具体事" in gm_reply or not any(
                    token in gm_reply for token in ("点头", "摇头", "我还能", "还撑得住", "能走", "呼吸")
                ):
                    self.errors.append("旅人状态询问没有得到旅人的具体可见反应或答复。")
            if "守门人" in player_message and any(token in player_message for token in ("做到哪一步", "才肯放行")):
                if "检定：" in gm_reply or " 对 " in gm_reply or not any(
                    token in gm_reply for token in ("旧路可以借", "条件", "放行")
                ):
                    self.errors.append("向守门人询问放行条件时没有得到可行动的 NPC 答复。")
            if "问守门人" in player_message and "灰布" in player_message:
                if "没有立刻答复" in gm_reply or not any(token in gm_reply for token in ("可以", "不行", "不能")):
                    self.errors.append("向守门人询问借用灰布时没有得到明确、贴合请求的 NPC 答复。")
            if "接受这次结果" in player_message and gm_reply.strip() != "好，刚才的结果保留。":
                self.errors.append("玩家接受失败结果后，GM 没有只确认结果，反而继续改写了场景事实。")
            if "同一条线" in player_message and "目前还不能确定" not in gm_reply:
                self.errors.append("玩家询问两件事是否同源时，GM 没有回答当前问题。")
            if "大成功" in gm_reply and "机会" in gm_reply and not any(
                token in gm_reply for token in ("你想把它用在", "选择机会", "机会用于")
            ):
                self.errors.append("大成功产生机会后没有向玩家开放机会效果选择。")
            if "最不安的人一句" in gm_reply:
                self.errors.append("模糊 NPC 指代被错误拼接成了人物名称。")
            if "照看旅人" in player_message and "只愿意答应一件具体事" in gm_reply:
                self.errors.append("照看旅人的行动被误判成要求 NPC 作出承诺。")
            if "收团" in player_message and "跑团记录" in gm_reply:
                self.errors.append("收团回复把后台故事摘要原样倾倒给了玩家。")
        heartbeat_reply = str(self.heartbeat.get("reply") or "")
        if "仍在等待" in heartbeat_reply and "尚未决定" in heartbeat_reply:
            self.errors.append("GM 主动节拍只复述了未决状态，没有让 NPC、环境或压力真正变化。")
        chapter_start = next((call for call in route_calls if call.get("label") == "确认进入第一章"), None)
        if chapter_start and int(chapter_start.get("elapsed_ms") or 0) > 45_000:
            self.errors.append("第一章开场被地图生成阻塞超过 45 秒。")
        if chapter_start:
            opening = str(chapter_start.get("reply") or "")
            for hero_name in ("伊莉雅", "洛岚", "赛璃"):
                if re.search(
                    rf"{re.escape(hero_name)}(?:刚|先|已经|正)?(?:把|拿|放|摊开|展开|走向|开口|检查|观察|选择)",
                    opening,
                ):
                    self.errors.append(f"第一章开场替玩家角色【{hero_name}】执行了尚未声明的行动。")
        post_check_prompts = [
            call
            for call in route_calls
            if "你想把它用在" in str(call.get("reply") or "")
            or "这个结果还没定稿" in str(call.get("reply") or "")
            or "这次差一点" in str(call.get("reply") or "")
        ]
        answered_choice_labels = {str(call.get("label") or "") for call in route_calls if "检定选择" in str(call.get("label") or "")}
        if post_check_prompts and not answered_choice_labels:
            self.errors.append("模拟玩家忽略了 GM 明确提出的检定后决策窗口。")
        if not self.adventure_started:
            self.errors.append("长测没有成功进入第一章。")
        if self.adventure_started and not self.map_artifact_path:
            self.errors.append("第一章已开启，但长测没有收集到生成地图。")
        if self.adventure_started:
            map_events = [event for event in runtime.app.world_state.memory_events if event.kind == "world_map_visual"]
            if len(map_events) > 1:
                self.errors.append(f"同一轮第零章重复生成了 {len(map_events)} 张世界地图。")
            map_event = next(
                (
                    event
                    for event in reversed(runtime.app.world_state.memory_events)
                    if event.kind == "world_map_visual"
                ),
                None,
            )
            brief_path = Path(str((map_event.payload if map_event else {}).get("brief_path") or ""))
            if brief_path.is_file():
                brief = json.loads(brief_path.read_text(encoding="utf-8"))
                label_names = {
                    str(item.get("text") or "")
                    for item in brief.get("labels", [])
                    if isinstance(item, dict)
                }
                starting_region = str(runtime.app.world_state.world_profile.starting_region or "")
                if starting_region and starting_region not in label_names:
                    self.errors.append(f"生成地图遗漏第一幕起始地点【{starting_region}】。")
        conversation_audit = audit.get("conversation") if isinstance(audit, dict) else {}
        quoted_count = int((conversation_audit or {}).get("quoted_reply_count") or 0)
        visible_route_replies = sum(1 for call in route_calls if call["send_reply"] and not call["deduplicated"])
        if quoted_count < visible_route_replies:
            self.errors.append(
                f"精确引用账本数量不足：quoted={quoted_count}, visible_replies={visible_route_replies}。"
            )
        report = {
            "ok": not self.errors and exception is None,
            "generated_at": datetime.now().isoformat(),
            "campaign_id": self.campaign_id,
            "total_api_calls": len(self.calls),
            "routed_player_messages": len(route_calls),
            "target_counts": target_counts,
            "visible_reply_count": visible_route_replies,
            "quoted_reply_count": quoted_count,
            "duplicate_delivery_ok": self.duplicate_ok,
            "adventure_started": self.adventure_started,
            "latency": self._latency_summary(route_calls),
            "heartbeat": {
                "action": self.heartbeat.get("action"),
                "send_reply": self.heartbeat.get("send_reply"),
                "reply": str(self.heartbeat.get("reply") or "")[:600],
            },
            "hero_validation": hero_validation,
            "world_progress": runtime.app.session_zero_manager.progress_summary(),
            "mechanical_hits": mechanical_hits,
            "repeated_reply_pairs": repeated_pairs,
            "player_generation": self.player_generation,
            "llm_audit": llm_audit,
            "errors": self.errors,
            "findings": self.findings,
            "calls": self.calls,
            "audit_summary": {
                "conversation": conversation_audit,
                "gate": audit.get("gate") if isinstance(audit, dict) else {},
                "phase": audit.get("phase") if isinstance(audit, dict) else {},
            },
            "artifacts": {
                "run_root": str(self.run_root),
                "conversation_txt": str(self.conversation_path),
                "report_json": str(self.report_json_path),
                "report_txt": str(self.report_txt_path),
                "progress_jsonl": str(self.progress_path),
                "world_map": self.map_artifact_path,
            },
        }
        if exception is not None:
            report["exception"] = f"{type(exception).__name__}: {exception}"
        return report

    def _latency_summary(self, route_calls: list[dict[str, Any]]) -> dict[str, int]:
        values = sorted(int(call.get("elapsed_ms") or 0) for call in route_calls)
        if not values:
            return {"median_ms": 0, "p95_ms": 0, "max_ms": 0}
        return {
            "median_ms": values[len(values) // 2],
            "p95_ms": values[min(len(values) - 1, int(len(values) * 0.95))],
            "max_ms": values[-1],
        }

    def _llm_audit(self, transcript: str) -> dict[str, Any]:
        if self.player_client is None or not self.player_model:
            return {"available": False, "reason": "no_model"}
        prompt = (
            "审计下面这段多人TRPG群聊。只评价玩家可见对话，不根据你知道的测试目标补脑。"
            "以‘---’开头的步骤标题只是测试报告索引，不是群聊中展示给玩家的文本，不要评价这些标题的人机感。"
            "请输出 JSON：{human_likeness:1-10, continuity:1-10, gm_timing:1-10, player_responsiveness:1-10, "
            "strengths:[最多4条], issues:[{severity:'high|medium|low', step:'编号', detail:'问题', suggestion:'修改方向'}]}。"
            "重点检查：GM是否抢玩家讨论、是否复述玩家、是否替玩家行动、是否用后台术语、玩家是否回应GM上一句、剧情是否连续。"
            "玩家明确提交设定或技能后，GM可以安静记录，不要求每条都公开确认；应评价的是是否在自然节点给出承接，而不是把静默本身判成错误。\n\n"
            + transcript[-30000:]
        )
        try:
            raw = self.player_client.create_chat_completion(
                model=self.player_model,
                messages=build_cache_friendly_messages(
                    static_system_prompt="你是严格的TRPG实际游玩记录审计员，只输出JSON对象。",
                    user_content=prompt,
                ),
                temperature=0.1,
                response_format={"type": "json_object"},
            )
            data = extract_json_object(raw)
            return data if isinstance(data, dict) else {"available": False, "raw": raw[:1000]}
        except Exception as exc:
            return {"available": False, "reason": f"{type(exc).__name__}: {exc}"}

    def _write_report(self, report: dict[str, Any]) -> None:
        self.report_json_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        lines = [
            "FU-GM 会话壳多人长测报告",
            f"ok: {report.get('ok')}",
            f"campaign: {self.campaign_id}",
            f"routed_player_messages: {report.get('routed_player_messages')}",
            f"target_counts: {json.dumps(report.get('target_counts'), ensure_ascii=False)}",
            f"visible_reply_count: {report.get('visible_reply_count')}",
            f"quoted_reply_count: {report.get('quoted_reply_count')}",
            f"duplicate_delivery_ok: {report.get('duplicate_delivery_ok')}",
            f"adventure_started: {report.get('adventure_started')}",
            f"latency: {json.dumps(report.get('latency'), ensure_ascii=False)}",
            "",
            "错误：",
            *([f"- {item}" for item in report.get("errors", [])] or ["- 无"]),
            "",
            "观察项：",
            *([f"- {item}" for item in report.get("findings", [])] or ["- 无"]),
            "",
            "LLM 对话审计：",
            json.dumps(report.get("llm_audit"), ensure_ascii=False, indent=2),
            "",
            "机械表达命中：",
            json.dumps(report.get("mechanical_hits"), ensure_ascii=False, indent=2),
            "",
            "高相似回复：",
            json.dumps(report.get("repeated_reply_pairs"), ensure_ascii=False, indent=2),
            "",
            "角色校验：",
            json.dumps(report.get("hero_validation"), ensure_ascii=False, indent=2),
            "",
            "产物：",
            *[f"- {key}: {value}" for key, value in report.get("artifacts", {}).items()],
        ]
        self.report_txt_path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    return ConversationShellLongHarness().run()


if __name__ == "__main__":
    raise SystemExit(main())
