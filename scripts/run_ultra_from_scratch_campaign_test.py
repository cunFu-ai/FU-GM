from __future__ import annotations

import json
import os
import sys
import time
import traceback
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from statistics import mean
from typing import Any
from urllib.parse import urlencode


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from fu_gm.components.conflict_manager import EnemyRank  # noqa: E402
from fu_gm.models import Affinity, Character, Clock  # noqa: E402
from fu_gm.http_server import FUGMHttpService  # noqa: E402


class FromScratchUltraHarness:
    """Runs a long real-service smoke test through the public HTTP boundary.

    The test intentionally does not inject prebuilt PC sheets. Player characters
    are provided as Session 0 table speech, confirmed through the same route a
    user would use, and then gated into Chapter 1.
    """

    def __init__(self) -> None:
        self.stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.run_root = PROJECT_ROOT / ".runtime" / "large_tests" / f"ultra_from_scratch_{self.stamp}"
        self.campaign_root = self.run_root / "campaigns"
        self.map_root = self.run_root / "maps"
        self.progress_path = self.run_root / "progress.jsonl"
        self.conversation_path = self.run_root / "full_api_conversation.txt"
        self.report_json_path = self.run_root / "ultra_from_scratch_report.json"
        self.report_txt_path = self.run_root / "ultra_from_scratch_report.txt"
        self.run_root.mkdir(parents=True, exist_ok=True)
        self.map_root.mkdir(parents=True, exist_ok=True)

        os.environ["FU_GM_PROJECT_DIR"] = str(PROJECT_ROOT)
        os.environ["FU_GM_NORTANTIS_OUTPUT_DIR"] = str(self.map_root)
        os.environ.setdefault("FU_GM_NORTANTIS_TIMEOUT_SECONDS", "240")

        self.campaign_id = f"超长从零测试_白钟大陆_{self.stamp}"
        self.session_id = "session0-to-chapter1-from-scratch"
        self.channel_id = "codex-ultra-from-scratch"
        self.participants = ["阿凛", "南星", "白河", "时雨", "澄砚"]
        self.pc_names = ["伊莉雅", "赛璃", "洛岚", "艾薇娅", "苍祈"]
        self.common = {
            "campaign_id": self.campaign_id,
            "session_id": self.session_id,
            "channel_id": self.channel_id,
        }
        self.service = FUGMHttpService(data_root=self.campaign_root, use_llm=True)
        self.calls: list[dict[str, Any]] = []
        self.notes: list[str] = []
        self.errors: list[str] = []
        self.expected_rules_blocked_labels = {
            "第一章冲突与规则 16 白河",
        }

        self.conversation_path.write_text(
            "\n".join(
                [
                    "FU-GM 从零开始超长测试完整 API 对话",
                    f"campaign_id: {self.campaign_id}",
                    f"session_id: {self.session_id}",
                    f"started_at: {datetime.now().isoformat(timespec='seconds')}",
                    "",
                ]
            ),
            encoding="utf-8",
        )

    def run(self) -> int:
        try:
            self._main_flow()
            report = self._build_report(exception=None)
            self._write_report(report)
            return 1 if report["errors"] else 0
        except Exception as exc:
            report = self._build_report(exception=exc)
            self._write_report(report)
            traceback.print_exc()
            return 1
        finally:
            print(f"RUN_ROOT={self.run_root}", flush=True)
            print(f"REPORT_JSON={self.report_json_path}", flush=True)
            print(f"REPORT_TXT={self.report_txt_path}", flush=True)
            print(f"CONVERSATION_TXT={self.conversation_path}", flush=True)

    def invoke(self, label: str, method: str, route: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        started = time.perf_counter()
        status, raw_body = self.service.handle(method, route, payload)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        body = raw_body if isinstance(raw_body, dict) else {"ok": status < 400, "raw": str(raw_body)}
        record = {
            "index": len(self.calls) + 1,
            "label": label,
            "method": method,
            "route": route,
            "status": status,
            "elapsed_ms": elapsed_ms,
            "ok": bool(body.get("ok", status < 400)),
            "blocked": bool(body.get("blocked")),
            "rules_blocked": bool(body.get("rules_blocked")),
            "speaker": str(payload.get("speaker") or ""),
            "message": str(payload.get("message") or ""),
            "reply": str(body.get("reply") or body.get("message") or ""),
            "body": body,
        }
        self.calls.append(record)
        self.progress_path.parent.mkdir(parents=True, exist_ok=True)
        with self.progress_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        self._append_conversation(record)
        if status >= 400 or not record["ok"]:
            error_text = str(body.get("error") or body.get("message") or record["reply"] or "unknown error")
            self.errors.append(f"{label} failed: status={status}, error={error_text}")
        if record["rules_blocked"]:
            blocked_text = record["reply"][:220]
            if label in self.expected_rules_blocked_labels:
                self.notes.append(f"{label} 触发预期规则拦截：{blocked_text}")
            else:
                self.errors.append(f"{label} rules_blocked: {blocked_text}")
        leaked_tokens = [
            "内部恢复重试",
            "npc_action_type",
            "is not a valid",
            "模型暂时没有接上",
            "Traceback",
            "KeyError",
        ]
        for token in leaked_tokens:
            if token in record["reply"]:
                self.errors.append(f"{label} leaked internal token {token!r}")
                break
        print(
            f"[{record['index']:02d}] {label}: status={status} "
            f"elapsed={elapsed_ms}ms ok={record['ok']} blocked={record['blocked']}",
            flush=True,
        )
        return body

    def _main_flow(self) -> None:
        self.invoke("新建战役", "POST", "/v1/campaigns/new", {"campaign_id": self.campaign_id})
        self.invoke("会话门控进入第零章", "POST", "/v1/session/gate", {**self.common, "status": "session_zero"})
        self.invoke(
            "第零章开场",
            "POST",
            "/v1/session-zero/start",
            {**self.common, "participants": self.participants},
        )

        for index, (speaker, message) in enumerate(self._session_zero_world_turns(), start=1):
            self.invoke(
                f"第零章世界共创 {index:02d} {speaker}",
                "POST",
                "/v1/session-zero/message",
                {**self.common, "speaker": speaker, "message": message},
            )

        for index, (speaker, message) in enumerate(self._session_zero_completion_turns(), start=1):
            self.invoke(
                f"第零章流程补齐 {index:02d} {speaker}",
                "POST",
                "/v1/session-zero/message",
                {**self.common, "speaker": speaker, "message": message},
            )

        for index, (speaker, message) in enumerate(self._session_zero_character_turns(), start=1):
            self.invoke(
                f"第零章角色创建 {index:02d} {speaker}",
                "POST",
                "/v1/session-zero/message",
                {**self.common, "speaker": speaker, "message": message},
            )

        self._verify_no_direct_pc_injection()
        self._wait_for_async_map_if_any()

        self.pre_gate_snapshot = self._snapshot(include_private=True)
        self.pre_gate_hero_status = self._runtime().app.session_zero_manager.hero_creation_status()
        self.pre_gate_world_ready = self._runtime().app.session_zero_manager.world_creation_ready()
        if not self.pre_gate_hero_status.get("ready"):
            self.notes.append(f"冒险门控前角色仍未 ready：{self.pre_gate_hero_status}")

        self.gate_body = self.invoke(
            "尝试进入第一章",
            "POST",
            "/v1/session/gate",
            {**self.common, "status": "adventure", "reason": "Session 0 已完成世界、小队、角色与第一幕共识，进入第一章。"},
        )
        if self.gate_body.get("blocked"):
            self._recover_missing_character_fields()
            self.gate_body = self.invoke(
                "补齐后重新进入第一章",
                "POST",
                "/v1/session/gate",
                {**self.common, "status": "adventure", "reason": "角色已补齐并确认，重新进入第一章。"},
            )
        if self.gate_body.get("blocked"):
            self.errors.append("冒险门控仍被阻挡。")
            return

        self._start_chapter_scene()
        for index, (speaker, message) in enumerate(self._chapter_one_turns_before_combat(), start=1):
            self.invoke(
                f"第一章连贯场景 {index:02d} {speaker}",
                "POST",
                "/v1/game/turn",
                {**self.common, "speaker": speaker, "message": message},
            )

        self._prepare_conflict_state()
        for index, (speaker, message) in enumerate(self._chapter_one_combat_turns(), start=1):
            self.invoke(
                f"第一章冲突与规则 {index:02d} {speaker}",
                "POST",
                "/v1/game/turn",
                {**self.common, "speaker": speaker, "message": message},
            )

        self.invoke(
            "第一章收团",
            "POST",
            "/v1/session/end",
            {**self.common, "title": "第一章：白花碑驿站的迟响"},
        )
        self.audit = self.invoke("读取审计仪表盘", "GET", self._audit_route(limit=320))
        self._write_public_transcript_copy()

    def _session_zero_world_turns(self) -> list[tuple[str, str]]:
        return [
            (
                "阿凛",
                "我想共创的大陆叫白钟大陆，形态就是普通类地球大陆，不讨论环形、巨龟背或其他异形世界。"
                "西侧是鸦羽山脉，中央有镜线内海，南岸是白花碑驿站和雾潮海岸，东南散布潮鸢群岛。"
                "魔法与科技并存：灵魂晶炉驱动车辆、工坊和财团机器，古老的御魂术和元素仪式负责安抚灵魂之河。"
                "我贡献一个国家：钟鸣公国在镜线内海北岸，正午大钟能安抚灵魂，但也让贵族能控制谁的哀悼被听见。"
                "重大历史事件是碎月坠落当夜白钟大陆所有钟慢了一拍；世界奥秘是姐姐的名字为何刻在白花风铃内侧却无人记得她死亡。"
                "世界威胁是辉钢财团正在把灰晶病患者的记忆作为可买卖燃料。"
                "界限：不详细描写性暴力、酷刑、现实仇恨煽动。帷幕：儿童遇险、身体病变、亲密内容淡出处理。"
                "我希望故事有史诗奇幻的希望感，中期能揭开颠覆力量平衡的真相；但主线从边境驿站的选择开始。",
            ),
            (
                "南星",
                "我贡献一个地区和历史事件：潮鸢群岛信奉迁徙的海风神，飞翼船会追着季风移动；"
                "三十年前碎月坠落，赤羽旧王都一夜消失，幸存者沿雾潮海岸建立白花碑驿站。"
                "我想要的谜团是：每年归潮祭后都会少一座岛，可所有人的公开记忆都会自动改写。"
                "世界威胁是苍白司教团把灰晶病包装成灵魂升格。第一幕我提议从白花碑驿站开始，"
                "说服白花守望会提供旧路，同时避开辉钢财团巡逻队。",
            ),
            (
                "白河",
                "我贡献一个威胁和阵营：辉钢财团控制第七采掘城，它正在向雾潮海岸移动，收购灰晶病患者的记忆作为魔导燃料。"
                "苍白司教团宣称灰晶病是灵魂升格的祝福，暗中帮财团筛选病人。"
                "重大历史事件是记忆炉第一次启动时吞掉了一整条矿道工人的姓名；"
                "世界奥秘是第七采掘城的紧急停机协议为何只回应赤羽遗民的歌。"
                "小队原型是临时守护者：护送一名失忆旅人和碎月遗物，从白花碑驿站前往钟鸣公国求证真相。"
                "反派种子：第七采掘城的监察官艾蕾娜曾是赤羽遗民，认为只有把记忆集中管理，世界才不会再遗忘灾难。",
            ),
            (
                "时雨",
                "我贡献一个社会冲突：奥涅里亚灯塔舰队维持海上贸易，但王室和港口行会互不信任。"
                "摄政王想把失踪群岛调查权交给辉钢财团，因为财团承诺能让记忆不再被归潮祭改写。"
                "重大历史事件是老国王病倒后，摄政王把王室海图抵押给辉钢财团；"
                "世界奥秘是灯塔为什么能照见已经消失的岛。"
                "世界威胁是港口行会和王室决裂会让财团取得失踪群岛调查权。"
                "我希望第一章里有一场不是战斗的冲突，要靠证据、承诺和情感说服别人。",
            ),
            (
                "澄砚",
                "我贡献一个神秘地点：沉默森林位于白钟大陆东南内陆，森林里的奥灵拒绝回应人类，"
                "但会在碎月之夜把未说出口的名字写到树皮上。世界奥秘是：这些名字里有些人仍然活着。"
                "王国或国家是沉默森林周边的树誓村社，村社不承认王权，只与奥灵立约；"
                "重大历史事件是碎月之夜后森林第一次拒绝所有人类祈祷。"
                "世界威胁是苍白司教团想把沉默森林变成灰晶病圣地。"
                "我也投白花碑驿站开幕：如果队伍只抢线索不保护普通人，奥灵以后就不会回应我们。",
            ),
        ]

    def _session_zero_completion_turns(self) -> list[tuple[str, str]]:
        return []

    def _session_zero_character_turns(self) -> list[tuple[str, str]]:
        return [
            (
                "阿凛",
                "我的玩家名是阿凛，角色名伊莉雅。身份：赤羽遗民的盾誓骑士；主题：责任；故乡：白花碑驿站。"
                "职业分配：守护者3级、元素使2级。属性骰：敏捷d8、洞察d8、力量d10、意志d6。"
                "职业技能：保镖1、防御精通1、挺身守护1、元素魔法1、元素系仪式1。"
                "法术选择：元素幕障。初始装备：钢匕首、青铜盾、旅行装束。羁绊：赛璃：信赖；洛岚：钦佩。"
                "背景钩子：她的姐姐名字在白花风铃内侧，却无人记得她是否真的死去。伊莉雅确认角色并正式建卡。",
            ),
            (
                "南星",
                "我的玩家名是南星，角色名赛璃。身份：钟鸣公国的御魂医师；主题：希望；故乡：钟鸣公国。"
                "职业分配：御魂使3级、旅人2级。属性骰：敏捷d6、洞察d10、力量d8、意志d8。"
                "职业技能：灵魂魔法2、御魂系仪式1、见多识广1、充足补给1。"
                "法术选择：治愈术、屏障。初始装备：法杖、旅行装束。羁绊：伊莉雅：信赖；洛岚：喜爱。"
                "背景钩子：她曾听见钟声里有自己的未来遗言。赛璃确认角色并正式建卡。",
            ),
            (
                "白河",
                "我的玩家名是白河，角色名洛岚。身份：辉钢财团出逃的魔导工匠；主题：赎罪；故乡：第七采掘城。"
                "职业分配：造物使3级、武器大师2级。属性骰：敏捷d8、洞察d10、力量d8、意志d6。"
                "职业技能：便携装置1、秘密配方1、先见之明1、碎骨1、破防打击1。"
                "初始装备：铁锤、旅行装束。羁绊：伊莉雅：钦佩；赛璃：信赖。"
                "背景钩子：他参与设计过第七采掘城的记忆炉，知道它有一个不能公开的紧急停机协议。洛岚确认角色并正式建卡。",
            ),
            (
                "时雨",
                "我的玩家名是时雨，角色名艾薇娅。身份：奥涅里亚的灯塔外交官；主题：妥协；故乡：奥涅里亚王都。"
                "职业分配：游说家2级、熵术士2级、旅人1级。属性骰：敏捷d8、洞察d8、力量d6、意志d10。"
                "职业技能：谴责1、鼓舞1、熵系魔法1、熵系仪式1、见多识广1。"
                "法术选择：加速术。初始装备：法杖、旅行装束。羁绊：伊莉雅：信赖；苍祈：猜忌。"
                "背景钩子：她知道摄政王为什么愿意相信辉钢财团。她希望用谈判阻止战争。艾薇娅确认角色并正式建卡。",
            ),
            (
                "澄砚",
                "我的玩家名是澄砚，角色名苍祈。身份：沉默森林的失约奥灵使；主题：亏欠；故乡：树誓村社。"
                "职业分配：奥灵使2级、拟兽使2级、暗刃骑士1级。属性骰：敏捷d6、洞察d10、力量d8、意志d8。"
                "职业技能：契约与召唤1、奥灵系仪式1、野性之语1、拟兽系仪式1、暗影击1。"
                "初始装备：魔典、旅行装束。羁绊：洛岚：猜忌；赛璃：喜爱。"
                "背景钩子：他曾向沉默森林的奥灵许诺会带回一个被世界遗忘的名字。苍祈确认角色并正式建卡。",
            ),
            (
                "阿凛",
                "我们确认第一幕：白花碑驿站的迟响。目标是说服白花守望会给出旧路，保护失忆旅人，发现财团收购记忆的第一条证据。",
            ),
        ]

    def _recover_missing_character_fields(self) -> None:
        status = self.gate_body.get("hero_creation") or self._runtime().app.session_zero_manager.hero_creation_status()
        missing = status.get("missing_by_player", {}) if isinstance(status, dict) else {}
        if not missing:
            self.errors.append("门控 blocked 但未提供缺项。")
            return
        recovery_profiles = {
            "伊莉雅": (
                "阿凛",
                "角色名伊莉雅，身份赤羽遗民的盾誓骑士，主题责任，故乡白花碑驿站，"
                "职业守护者3/元素使2，属性敏捷d8洞察d8力量d10意志d6，技能保镖1、防御精通1、挺身守护1、元素魔法1、元素系仪式1，"
                "法术元素幕障，装备钢匕首、青铜盾、旅行装束，并确认角色正式建卡。",
            ),
            "赛璃": (
                "南星",
                "角色名赛璃，身份钟鸣公国的御魂医师，主题希望，故乡钟鸣公国，"
                "职业御魂使3/旅人2，属性敏捷d6洞察d10力量d8意志d8，技能灵魂魔法2、御魂系仪式1、见多识广1、充足补给1，"
                "法术治愈术、屏障，装备法杖、旅行装束，并确认角色正式建卡。",
            ),
            "洛岚": (
                "白河",
                "角色名洛岚，身份辉钢财团出逃的魔导工匠，主题赎罪，故乡第七采掘城，"
                "职业造物使3/武器大师2，属性敏捷d8洞察d10力量d8意志d6，技能便携装置1、秘密配方1、先见之明1、碎骨1、破防打击1，"
                "装备铁锤、旅行装束，并确认角色正式建卡。",
            ),
            "艾薇娅": (
                "时雨",
                "角色名艾薇娅，身份奥涅里亚的灯塔外交官，主题妥协，故乡奥涅里亚王都，"
                "职业游说家2/熵术士2/旅人1，属性敏捷d8洞察d8力量d6意志d10，技能谴责1、鼓舞1、熵系魔法1、熵系仪式1、见多识广1，"
                "法术加速术，装备法杖、旅行装束，并确认角色正式建卡。",
            ),
            "苍祈": (
                "澄砚",
                "角色名苍祈，身份沉默森林的失约奥灵使，主题亏欠，故乡树誓村社，"
                "职业奥灵使2/拟兽使2/暗刃骑士1，属性敏捷d6洞察d10力量d8意志d8，技能契约与召唤1、奥灵系仪式1、野性之语1、拟兽系仪式1、暗影击1，"
                "装备魔典、旅行装束，并确认角色正式建卡。",
            ),
        }
        for label, fields in missing.items():
            text_fields = "、".join(str(field) for field in fields) if isinstance(fields, list) else str(fields)
            profile = next(
                (
                    value
                    for hero_name, value in recovery_profiles.items()
                    if hero_name in str(label) or value[0] in str(label)
                ),
                ("阿凛", "请按当前玩家的角色草稿补齐姓名、身份、主题、故乡、职业、属性、技能、法术、装备，并确认角色正式建卡。"),
            )
            speaker, profile_text = profile
            message = f"补齐角色【{label}】缺项：{text_fields}。{profile_text}"
            self.invoke(
                f"门控补齐角色 {label}",
                "POST",
                "/v1/session-zero/message",
                {**self.common, "speaker": speaker, "message": message},
            )

    def _chapter_one_turns_before_combat(self) -> list[tuple[str, str]]:
        return [
            (
                "阿凛",
                "伊莉雅把碎月遗物固定在盾后，走进白花碑驿站的风铃廊。她先向守望会会长说明来意："
                "我们不是来夺走名字，而是想护送失忆旅人去钟鸣公国确认记忆被导向哪里。",
            ),
            (
                "南星",
                "赛璃不使用未掌握法术，只做普通调查：她观察旅人的呼吸、灰晶光泽和听到钟声时的反应，"
                "想判断记忆是否被导向灵魂中枢。若需要检定，我用洞察+意志，并请公开所有骰子、修正值和目标值。",
            ),
            (
                "白河",
                "洛岚检查驿站旧钟与财团车辙，想找出第七采掘城巡逻队多久会抵达。"
                "如果时间紧迫，请建立公开威胁命刻【财团巡逻队逼近】6格，赌注是谈判拖太久会被包围。",
            ),
            (
                "时雨",
                "艾薇娅请求把接下来的会谈作为社交冲突处理：目标是说服白花守望会给旧路，但不让他们公开背锅。"
                "她提出证据和退路，推进目标命刻【争取守望会信任】，如果要先攻，请由艾薇娅带头做先攻团队检定。",
            ),
            (
                "澄砚",
                "苍祈调查风铃廊里有没有沉默森林奥灵留下的痕迹。"
                "他只想知道这些风铃刻名是否包括仍然活着的人，不强行发动法术；如果需要，就是洞察+意志开放检定。",
            ),
            (
                "阿凛",
                "我临场补充一个世界细节：白花碑驿站由白花守望会管理，他们会把每个失去的名字刻在风铃内侧。"
                "伊莉雅请求守望会提供避开财团关卡的旧路；如果这会改变世界事实，伊莉雅愿意消耗1点物语点。",
            ),
            (
                "南星",
                "赛璃计划一个御魂仪式【风铃回声】：学科御魂，效力轻微，范围小范围，"
                "效果是让风铃暂时回响昨夜经过驿站的脚步和名字，不直接伤害任何人。",
            ),
            (
                "南星",
                "赛璃为仪式【风铃回声】供能，使用洞察+意志推进仪式命刻。她把旅人的名字写在白花纸上，挂到风铃下。",
            ),
            (
                "白河",
                "洛岚协助推进仪式命刻【仪式：风铃回声】，用洞察+敏捷调整旧钟的共鸣，让风铃只回放公开经过的痕迹。",
            ),
            (
                "南星",
                "赛璃尝试完成仪式【风铃回声】。如果仪式命刻还没完成，请明确告诉我还差多少格，不要把它当作角色失败。",
            ),
            (
                "时雨",
                "如果守望会答应给旧路，艾薇娅组织一次短途旅行：队伍从白花碑驿站沿旧路前往潮汐下的钟塔遗迹，"
                "她负责路线和补给。旅行日按地图路线结算，不要询问世界形状或旅行日长度。",
            ),
            (
                "白河",
                "洛岚在旧路尽头检查潮汐下的钟塔遗迹入口，把它当作地下城场景而不是幕间跳过。"
                "请建立危险命刻【潮水没顶】6格；如果我检查机关失败，就推进这个威胁命刻。",
            ),
            (
                "澄砚",
                "苍祈用野性之语尝试和遗迹边缘的潮生藤交流，问它最近有没有见过财团机兵经过。"
                "如果这个技能只是 GM 判断，请清楚告诉我们它被识别了，但不要假装已经硬改数值。",
            ),
        ]

    def _chapter_one_combat_turns(self) -> list[tuple[str, str]]:
        return [
            (
                "阿凛",
                "伊莉雅挡在失忆旅人前面，攻击财团机兵，使用钢匕首近战攻击。请公开命中检定的属性、每颗骰子、修正值、物防和伤害。",
            ),
            (
                "时雨",
                "艾薇娅不攻击，她用游说家的方式妨碍监察官艾蕾娜：指出她所谓保护记忆其实是在剥夺选择。"
                "若成功，请对艾蕾娜施加动摇；如果这是冲突行动，请公开 DL10 和骰子。",
            ),
            (
                "财团机兵",
                "财团机兵用电棘枪攻击伊莉雅，试图把她从旅人身边逼开。",
            ),
            (
                "白河",
                "洛岚推进目标命刻【旧路闸门开启】，用洞察+敏捷拆开驿站旧闸门的财团封锁，成功后队伍能护送旅人离开。",
            ),
            (
                "澄砚",
                "苍祈协助洛岚推进【旧路闸门开启】，他用奥灵留下的树皮名纹稳定门轴。"
                "如果这是冲突团队合作，请把苍祈的本轮回合消耗掉，并把支援加值公开。",
            ),
            (
                "南星",
                "赛璃执行防御行动并掩护失忆旅人，同时提醒守望会的人带孩子离开，儿童遇险淡出处理。",
            ),
            (
                "白河",
                "洛岚执行装备动作，把主手换成铁锤，副手空出来方便调整机关；不要更换防具。",
            ),
            (
                "阿凛",
                "伊莉雅不继续硬拼，她推进目标命刻【旧路闸门开启】，用力量+意志顶住闸门，把洛岚打开的缝隙撑住。",
            ),
            (
                "阿凛",
                "如果刚才伊莉雅的推进检定差一点，我消耗1点物语点援用主题【责任】重掷低的那枚骰子；"
                "如果已经成功，就把这句话当作不触发的规则窗口说明。",
            ),
            (
                "财团机兵",
                "财团机兵推进威胁命刻【财团巡逻队逼近】，它向远处发出红色信号，让第二队巡逻准备包围驿站。",
            ),
            (
                "白河",
                "洛岚使用铁锤攻击财团机兵的腿部联轴，想让它迟缓；如果这更适合妨碍行动，请按妨碍结算。",
            ),
            (
                "澄砚",
                "如果洛岚刚才掷出大成功，苍祈建议把机会用于【进展】，额外推进【旧路闸门开启】；"
                "如果没有大成功，就请 GM 只记住我们想优先选择进展机会。",
            ),
            (
                "南星",
                "赛璃对伊莉雅施放治愈术，确认这是她掌握的御魂使法术，并公开施法消耗、检定或恢复量。",
            ),
            (
                "时雨",
                "艾薇娅提醒监察官艾蕾娜：如果她愿意撤退，我们会把记忆炉证据交给奥涅里亚公开审理。"
                "这不是投降，只是给反派一个符合动机的退场机会；如果她消耗终结点逃离，请记录终结点变化。",
            ),
            (
                "阿凛",
                "如果旧路闸门已经打开，伊莉雅要求结束冲突场景：我们带着旅人和碎月遗物撤入旧路，留下风铃继续回响。",
            ),
            (
                "白河",
                "冲突结束后，洛岚出售一面备用青铜盾，把钱用作旧式信号塔工程的材料费。"
                "如果他没有备用盾，就明确告诉我不能出售，不要凭空给钱。",
            ),
            (
                "白河",
                "洛岚启动工程【修复白花守望会旧式信号塔】，目标是让守望会能提前发现财团巡逻。"
                "赛璃和伊莉雅今天帮工，若需要费用和进度，请按工程规则结算。",
            ),
            (
                "澄砚",
                "下一段我这个玩家会缺席半小时，苍祈选择淡出场景去和沉默森林奥灵交涉，之后再回来。"
                "请按缺席玩家流程记录，不要让他替队伍做关键选择。",
            ),
        ]

    def _verify_no_direct_pc_injection(self) -> None:
        pcs = [character.name for character in self._runtime().app.character_manager.all() if "pc" in character.traits]
        self.notes.append(f"Session 0 发言后正式 PC：{pcs}")

    def _wait_for_async_map_if_any(self) -> None:
        status = self._runtime().app.world_map_generation_status()
        if status.get("status") != "generating":
            return
        started = time.perf_counter()
        while status.get("status") == "generating" and time.perf_counter() - started < 260:
            time.sleep(2)
            status = self._runtime().app.world_map_generation_status()
        self.notes.append(f"异步地图生成等待结果：{status}")

    def _start_chapter_scene(self) -> None:
        app = self._runtime().app
        if app.scene_manager.current_scene is None or app.scene_manager.current_scene.name.startswith("Session 0"):
            app.start_scene(
                "第一章：白花碑驿站的迟响",
                location="白花碑驿站",
                participants=self.pc_names,
                objective="说服白花守望会、保护失忆旅人，并避开财团巡逻队。",
                summary="从第零章共创的白花碑驿站切入第一章。",
            )

    def _prepare_conflict_state(self) -> None:
        app = self._runtime().app
        if not app.character_manager.exists("财团机兵"):
            app.character_manager.add(
                Character(
                    name="财团机兵",
                    level=5,
                    identity="辉钢财团安保构装体",
                    theme="控制",
                    origin="第七采掘城",
                    attributes={"DEX": 8, "INS": 8, "MIG": 10, "WLP": 6},
                    max_hp=60,
                    hp=60,
                    max_mp=35,
                    mp=35,
                    crisis_threshold=30,
                    weapon_damage=10,
                    weapon_type="physical",
                    defenses={"physical": 10, "magic": 8},
                    affinities={"thunder": Affinity.WEAK, "earth": Affinity.RESIST},
                    traits=["enemy", "construct", "辉钢财团"],
                    weapon_accuracy_attributes=["MIG", "MIG"],
                    weapon_accuracy_modifier=1,
                    weapon_range="melee",
                )
            )
        if not app.character_manager.exists("监察官艾蕾娜"):
            app.character_manager.add(
                Character(
                    name="监察官艾蕾娜",
                    level=10,
                    identity="第七采掘城监察官",
                    theme="秩序",
                    origin="赤羽旧王都",
                    attributes={"DEX": 8, "INS": 10, "MIG": 6, "WLP": 10},
                    max_hp=90,
                    hp=90,
                    max_mp=70,
                    mp=70,
                    crisis_threshold=45,
                    weapon_damage=8,
                    weapon_type="dark",
                    defenses={"physical": 10, "magic": 10},
                    affinities={"light": Affinity.WEAK, "dark": Affinity.RESIST},
                    traits=["enemy", "villain", "humanoid", "辉钢财团", "赤羽遗民"],
                    weapon_accuracy_attributes=["INS", "WLP"],
                    weapon_accuracy_modifier=1,
                    weapon_range="ranged",
                    initiative=10,
                )
            )
        if not app.character_manager.exists("财团狙击手"):
            app.character_manager.add(
                Character(
                    name="财团狙击手",
                    level=5,
                    identity="辉钢财团远程护卫",
                    theme="服从",
                    origin="第七采掘城",
                    attributes={"DEX": 10, "INS": 8, "MIG": 6, "WLP": 6},
                    max_hp=45,
                    hp=45,
                    max_mp=30,
                    mp=30,
                    crisis_threshold=22,
                    weapon_damage=8,
                    weapon_type="physical",
                    defenses={"physical": 10, "magic": 8},
                    affinities={},
                    traits=["enemy", "humanoid", "辉钢财团"],
                    weapon_accuracy_attributes=["DEX", "INS"],
                    weapon_accuracy_modifier=0,
                    weapon_range="ranged",
                    initiative=9,
                )
            )
        for pc in self.pc_names:
            if not app.character_manager.exists(pc):
                self.errors.append(f"准备冲突时缺少正式 PC：{pc}")
        app.conflict_manager.register_enemy("财团机兵", EnemyRank.SOLDIER)
        app.conflict_manager.register_enemy("财团狙击手", EnemyRank.SOLDIER)
        app.conflict_manager.register_enemy("监察官艾蕾娜", EnemyRank.VILLAIN, ultima_points=3, action_count=2)
        if not app.clock_manager.exists("财团巡逻队逼近"):
            app.clock_manager.add(
                Clock(name="财团巡逻队逼近", max_segments=6, current=0, clock_type="threat", stakes="巡逻队包围白花碑驿站。")
            )
        if not app.clock_manager.exists("旧路闸门开启"):
            app.clock_manager.add(
                Clock(name="旧路闸门开启", max_segments=6, current=0, clock_type="objective", stakes="旧路开启后队伍可撤离冲突。")
            )
        path = app.save_campaign_memory(self.campaign_id)
        self._runtime().last_saved_path = str(path)
        self.notes.append("测试脚本建立敌方对象，以覆盖硬规则战斗流程；PC 仍由 Session 0 发言创建。")
        if not app.conflict_manager.state.active:
            self.invoke(
                "第一章冲突启动",
                "POST",
                "/v1/game/turn",
                {
                    **self.common,
                    "speaker": "阿凛",
                    "message": (
                        "监察官艾蕾娜带着财团机兵和财团狙击手拦住旧路，我们进入冲突场景【白花碑驿站伏击】。"
                        "玩家方是伊莉雅、赛璃、洛岚、艾薇娅、苍祈；敌方是监察官艾蕾娜、财团机兵、财团狙击手。"
                        "由伊莉雅担任先攻团队检定队长，赛璃、洛岚、艾薇娅、苍祈支援。请先结算 DEX+INS 先攻团队检定。"
                    ),
                },
            )
        if not app.conflict_manager.state.active:
            participants = [
                name
                for name in [self.pc_names[0], "监察官艾蕾娜", self.pc_names[1], "财团机兵", self.pc_names[2], "财团狙击手", self.pc_names[3], self.pc_names[4]]
                if app.character_manager.exists(name)
            ]
            app.conflict_manager.start_scene("白花碑驿站伏击", participants)
            self.notes.append("自然语言启动冲突未成功，已使用手动冲突场景兜底。")

    def _snapshot(self, *, include_private: bool = False) -> dict[str, Any]:
        route = f"/v1/campaigns/{self.campaign_id}/snapshot?" + urlencode({"include_private": str(include_private).lower()})
        return self.invoke("读取 Session 0 快照", "GET", route)

    def _audit_route(self, *, limit: int = 200) -> str:
        return "/v1/audit/dashboard?" + urlencode(
            {
                "campaign_id": self.campaign_id,
                "session_id": self.session_id,
                "channel_id": self.channel_id,
                "include_private": "true",
                "limit": str(limit),
            }
        )

    def _runtime(self):
        return self.service._runtime(self.campaign_id)

    def _append_conversation(self, record: dict[str, Any]) -> None:
        lines = [
            f"--- {record['index']:02d}. {record['label']} | {record['elapsed_ms']}ms | "
            f"status={record['status']} ok={record['ok']} ---"
        ]
        if record["speaker"] or record["message"]:
            lines.append(f"{record['speaker']}: {record['message']}")
        if record["reply"]:
            lines.append(f"时悠: {record['reply']}")
        if record["blocked"]:
            lines.append(
                "状态: "
                + json.dumps(
                    {"blocked": record["blocked"]},
                    ensure_ascii=False,
                )
            )
        lines.append("")
        with self.conversation_path.open("a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")

    def _write_public_transcript_copy(self) -> None:
        audit = getattr(self, "audit", {})
        path_text = audit.get("logs", {}).get("transcript_txt_path") if isinstance(audit, dict) else ""
        if not path_text:
            return
        transcript = Path(path_text)
        if not transcript.exists():
            return
        copy_path = self.run_root / "session_transcript_copy.txt"
        copy_path.write_text(transcript.read_text(encoding="utf-8"), encoding="utf-8")
        self.notes.append(f"已复制正式 transcript：{copy_path}")

    def _build_report(self, *, exception: Exception | None) -> dict[str, Any]:
        if exception is not None:
            self.errors.append(f"{exception.__class__.__name__}: {exception}")
        audit = getattr(self, "audit", {})
        if not isinstance(audit, dict):
            audit = {}
        runtime = self._runtime()
        world = runtime.app.world_state.world_profile
        chars = [character.name for character in runtime.app.character_manager.all()]
        pcs = [character.name for character in runtime.app.character_manager.all() if "pc" in character.traits]
        elapsed_values = [int(call["elapsed_ms"]) for call in self.calls]
        slowest = sorted(self.calls, key=lambda item: int(item.get("elapsed_ms", 0)), reverse=True)[:10]
        transcript_path = Path(audit.get("logs", {}).get("transcript_txt_path") or "")
        dashboard_phase = audit.get("phase", {}) if isinstance(audit.get("phase"), dict) else {}
        map_status = (
            getattr(self, "gate_body", {}).get("world_map")
            or runtime.app.world_map_generation_status()
            or audit.get("world_map")
        )
        checks = {
            "used_real_llm_service": self.service.use_llm is True,
            "session_zero_world_ready_before_gate": bool(getattr(self, "pre_gate_world_ready", False)),
            "session_zero_hero_ready_before_gate": bool(getattr(self, "pre_gate_hero_status", {}).get("ready", False)),
            "gate_not_blocked": not bool(getattr(self, "gate_body", {}).get("blocked")),
            "official_transcript_txt_exists": transcript_path.exists(),
            "map_generation_attempted": bool(map_status),
            "map_generated_or_ready": isinstance(map_status, dict) and map_status.get("status") in {"generated", "ready"},
            "phase_not_session_zero_after_end": dashboard_phase.get("current_scene") != "Session 0 世界创建",
            "has_clock_coverage": any("命刻" in call["message"] or "命刻" in call["reply"] for call in self.calls),
            "has_ritual_coverage": any("仪式" in call["message"] or "仪式" in call["reply"] for call in self.calls),
            "has_combat_coverage": any("攻击" in call["message"] or "战斗" in call["reply"] or "冲突" in call["reply"] for call in self.calls),
            "has_roll_detail_output": any("骰" in call["reply"] and ("目标" in call["reply"] or "物防" in call["reply"]) for call in self.calls),
            "no_world_shape_question": not any("世界形状" in call["reply"] for call in self.calls),
            "no_internal_world_style_public": not any("世界风格" in call["reply"] or "地图形式" in call["reply"] for call in self.calls),
        }
        if not checks["phase_not_session_zero_after_end"]:
            self.errors.append("收团后审计面板仍显示 Session 0 世界创建。")
        if not checks["session_zero_hero_ready_before_gate"]:
            self.errors.append(f"冒险门控前角色未 ready：{getattr(self, 'pre_gate_hero_status', {})}")
        if getattr(self, "gate_body", {}).get("blocked"):
            self.errors.append("进入第一章时仍被角色创建门控阻挡。")
        if elapsed_values:
            avg_ms = int(mean(elapsed_values))
        else:
            avg_ms = 0
        return {
            "ok": not self.errors,
            "campaign_id": self.campaign_id,
            "session_id": self.session_id,
            "channel_id": self.channel_id,
            "started_at": self.stamp,
            "errors": self.errors,
            "notes": self.notes,
            "checks": checks,
            "latency": {
                "count": len(elapsed_values),
                "total_ms": sum(elapsed_values),
                "avg_ms": avg_ms,
                "max_ms": max(elapsed_values) if elapsed_values else 0,
                "slowest": [
                    {
                        "index": call["index"],
                        "label": call["label"],
                        "route": call["route"],
                        "elapsed_ms": call["elapsed_ms"],
                        "status": call["status"],
                        "ok": call["ok"],
                    }
                    for call in slowest
                ],
            },
            "world": {
                "campaign_title": world.campaign_title,
                "continent_name": world.continent_name,
                "completed": world.completed,
                "starting_region": world.starting_region,
                "kingdoms": dict(world.kingdoms),
                "factions": dict(world.factions),
                "major_locations": dict(world.major_locations),
                "hero_drafts": {
                    key: {
                        "player_name": draft.player_name,
                        "hero_name": draft.hero_name,
                        "confirmed": draft.confirmed,
                        "classes": dict(draft.classes),
                    }
                    for key, draft in world.hero_drafts.items()
                },
            },
            "characters": {"all": chars, "pcs": pcs},
            "gate": getattr(self, "gate_body", {}),
            "map_status": map_status,
            "dashboard_phase": dashboard_phase,
            "dashboard_runtime": audit.get("runtime", {}),
            "astrbot_bridge": audit.get("astrbot_bridge", {}),
            "telemetry": audit.get("telemetry", {}),
            "artifacts": {
                "run_root": str(self.run_root),
                "conversation_txt": str(self.conversation_path),
                "progress_jsonl": str(self.progress_path),
                "report_json": str(self.report_json_path),
                "report_txt": str(self.report_txt_path),
                "official_transcript_txt": str(transcript_path) if transcript_path else "",
                "map_output_dir": str(self.map_root),
            },
            "calls": self.calls,
        }

    def _write_report(self, report: dict[str, Any]) -> None:
        self.report_json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        self.report_txt_path.write_text(self._format_report(report), encoding="utf-8")

    def _format_report(self, report: dict[str, Any]) -> str:
        lines = [
            "FU-GM 从零开始超长测试报告",
            f"campaign_id: {report['campaign_id']}",
            f"session_id: {report['session_id']}",
            f"ok: {report['ok']}",
            "",
            "=== 检查项 ===",
        ]
        for key, value in report["checks"].items():
            lines.append(f"- {key}: {value}")
        lines.extend(
            [
                "",
                "=== 错误 ===",
                *([f"- {item}" for item in report["errors"]] or ["- 无"]),
                "",
                "=== 备注 ===",
                *([f"- {item}" for item in report["notes"]] or ["- 无"]),
                "",
                "=== 延迟统计 ===",
                f"调用数: {report['latency']['count']}",
                f"总耗时: {report['latency']['total_ms']}ms",
                f"平均耗时: {report['latency']['avg_ms']}ms",
                f"最大耗时: {report['latency']['max_ms']}ms",
                "最慢调用:",
            ]
        )
        for item in report["latency"]["slowest"]:
            lines.append(
                f"- #{item['index']} {item['label']} {item['route']} "
                f"{item['elapsed_ms']}ms status={item['status']} ok={item['ok']}"
            )
        lines.extend(
            [
                "",
                "=== 地图状态 ===",
                json.dumps(report["map_status"], ensure_ascii=False, indent=2, default=str),
                "",
                "=== 收团后阶段 ===",
                json.dumps(report["dashboard_phase"], ensure_ascii=False, indent=2, default=str),
                "",
                "=== 产物 ===",
            ]
        )
        for key, value in report["artifacts"].items():
            lines.append(f"- {key}: {value}")
        lines.extend(["", "=== 完整 API 对话 ==="])
        lines.append(self.conversation_path.read_text(encoding="utf-8") if self.conversation_path.exists() else "")
        return "\n".join(lines)


def main() -> int:
    return FromScratchUltraHarness().run()


if __name__ == "__main__":
    raise SystemExit(main())
