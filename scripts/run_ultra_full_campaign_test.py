from __future__ import annotations

import json
import os
import sys
import time
import traceback
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from fu_gm.http_server import FUGMHttpService  # noqa: E402
from run_session0_ch1_long_test import _validated_hero_drafts  # noqa: E402


class UltraCampaignHarness:
    def __init__(self) -> None:
        self.stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.run_root = PROJECT_ROOT / ".runtime" / "large_tests" / f"ultra_full_campaign_{self.stamp}"
        self.campaign_root = self.run_root / "campaigns"
        self.map_root = self.run_root / "maps"
        self.progress_path = self.run_root / "progress.jsonl"
        self.conversation_path = self.run_root / "ultra_conversation.txt"
        self.report_json_path = self.run_root / "ultra_report.json"
        self.report_txt_path = self.run_root / "ultra_report.txt"
        self.run_root.mkdir(parents=True, exist_ok=True)
        self.map_root.mkdir(parents=True, exist_ok=True)
        os.environ["FU_GM_PROJECT_DIR"] = str(PROJECT_ROOT)
        os.environ["FU_GM_NORTANTIS_OUTPUT_DIR"] = str(self.map_root)
        os.environ.setdefault("FU_GM_NORTANTIS_TIMEOUT_SECONDS", "180")
        self.campaign_id = f"超长测试_绯雨大陆_{self.stamp}"
        self.session_id = "ultra-session0-to-chapter1"
        self.channel_id = "codex-ultra-real-api-test"
        self.service = FUGMHttpService(data_root=self.campaign_root, use_llm=True)
        self.calls: list[dict[str, Any]] = []
        self.common = {
            "campaign_id": self.campaign_id,
            "session_id": self.session_id,
            "channel_id": self.channel_id,
        }
        self.conversation_path.write_text(
            "\n".join(
                [
                    "FU-GM 超长真实跑团测试完整对话",
                    f"campaign_id: {self.campaign_id}",
                    f"session_id: {self.session_id}",
                    f"started_at: {datetime.now().isoformat(timespec='seconds')}",
                    "",
                ]
            ),
            encoding="utf-8",
        )

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
            "speaker": str(payload.get("speaker") or ""),
            "message": str(payload.get("message") or ""),
            "reply": str(body.get("reply") or body.get("message") or ""),
            "body": body,
        }
        self.calls.append(record)
        self._append_progress(record)
        self._append_conversation(record)
        print(f"[{record['index']:02d}] {label}: {status} / {elapsed_ms}ms / ok={record['ok']}", flush=True)
        return body

    def run(self) -> int:
        try:
            self._run_main_flow()
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

    def _run_main_flow(self) -> None:
        self.invoke("新建战役", "POST", "/v1/campaigns/new", {"campaign_id": self.campaign_id})
        self.invoke("会话门控进入第零章", "POST", "/v1/session/gate", {**self.common, "status": "session_zero"})
        self.invoke(
            "第零章开场",
            "POST",
            "/v1/session-zero/start",
            {**self.common, "participants": ["阿凛", "白河", "南星"]},
        )

        for index, (speaker, message) in enumerate(self._session_zero_turns(), start=1):
            self.invoke(
                f"第零章共创 {index} {speaker}",
                "POST",
                "/v1/session-zero/message",
                {**self.common, "speaker": speaker, "message": message},
            )

        self._stabilize_world_and_characters()
        self.completed_before_adventure = self._runtime().app.world_state.world_profile.completed
        self.gate_body = self.invoke(
            "玩家确认开始冒险并生成地图",
            "POST",
            "/v1/session/gate",
            {**self.common, "status": "adventure", "reason": "第零章共创已有足够材料，玩家要求进入第一章"},
        )

        for index, (speaker, message) in enumerate(self._adventure_turns(), start=1):
            self.invoke(
                f"第一章长跑回合 {index:02d} {speaker}",
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
        audit_route = "/v1/audit/dashboard?" + urlencode(
            {
                "campaign_id": self.campaign_id,
                "session_id": self.session_id,
                "channel_id": self.channel_id,
                "include_private": "true",
                "limit": "260",
            }
        )
        self.audit = self.invoke("读取审计仪表盘", "GET", audit_route)

    def _stabilize_world_and_characters(self) -> None:
        runtime = self._runtime()
        app = runtime.app
        world = app.world_state.world_profile
        world.campaign_title = "绯雨大陆：碎月回声"
        world.continent_name = "绯雨大陆"
        world.world_style = "科技奇幻为主，融合史诗奇幻与海洋自然奇幻"
        world.map_card = "类地球大陆地图：西侧鸦羽山脉、中央镜线内海、东南潮鸢群岛、南岸雾潮海岸"
        world.magic_tech_role = "灵魂晶炉支撑工业与交通，古老精灵术维持自然和灵魂循环。"
        world.starting_region = "白花碑驿站"
        world.group_concept = "护送碎月遗物、调查灰晶病与记忆收购的临时守护者"
        world.core_themes = ["希望", "记忆的代价", "人与系统的和解或决裂"]
        world.major_locations.update(
            {
                "白花碑驿站": "雾潮南岸的边境驿站，第一章从这里开始。",
                "钟鸣公国": "位于镜线内海北岸，以安魂钟与灵魂工艺闻名。",
                "潮鸢群岛": "东南海域的飞翼船群岛，每年归潮祭后都会失去一座无人记得的岛。",
                "鸦羽山脉": "大陆西侧的古代遗迹带，碎月坠落后出现异常回声。",
                "奥涅里亚": "象牙城墙、河流与城堡围绕的富饶王国，可作为史诗线盟友或政治压力来源。",
                "七号采掘器": "辉钢财团控制的移动采掘城市，正在接近雾潮海岸。",
                "灵魂中枢": "失落网络与灵魂之河交界的节点，可能解释灰晶病与记忆收购。",
            }
        )
        world.kingdoms.update(
            {
                "钟鸣公国": "正午钟声可安抚灵魂，钟匠与晶炉工匠掌握政治话语权。",
                "赤羽遗民": "散居雾潮南岸，守护白花碑与失落王都的记忆。",
                "潮鸢群岛": "信奉迁徙海风神，以飞翼船维系各岛。",
                "奥涅里亚": "富饶边境王国，贵族希望收编碎月遗物以维持旧秩序。",
            }
        )
        world.factions.update(
            {
                "辉钢财团": "用灵魂晶炉、医疗债务与安保队控制记忆收购。",
                "白花守望会": "赤羽遗民的守碑组织，把失去的名字刻在风铃内侧。",
                "苍白司教团": "宣称灰晶病是灵魂升格的祝福，真实目的不明。",
            }
        )
        world.historical_events = [
            "三十年前碎月坠落，赤羽旧王都在一夜间消失。",
            "碎月之夜钟鸣公国的大钟慢了一拍，所有人都听见未来的哭声。",
            "潮鸢群岛每年归潮祭后都会少一座岛，却无人记得消失的是哪座。",
        ]
        world.mysteries = [
            "碎月遗迹为何回应英雄羁绊而不是贵族血统？",
            "灰晶病患者失去的记忆最终流向了哪里？",
            "灵魂中枢是否仍在主动筛选可以进入的人？",
        ]
        world.world_threats = [
            "辉钢财团要用病人的记忆制造魔导兵器。",
            "七号采掘器正在接近雾潮海岸，可能碾碎白花碑驿站。",
            "苍白司教团正在把灰晶病包装成神圣升格。",
        ]
        world.hero_drafts = _validated_hero_drafts()

        app.world_state.upsert_map_location(
            "镜线内海", description="大陆中央的狭长内海。", feature_type="inland_sea", position_hint="center", draw_icon=False
        )
        app.world_state.upsert_map_location(
            "钟鸣公国", description=world.major_locations["钟鸣公国"], feature_type="country",
            relative_to="镜线内海", relative_position="north", faction="钟鸣公国", draw_icon=False
        )
        app.world_state.upsert_map_location(
            "潮鸢群岛", description=world.major_locations["潮鸢群岛"], feature_type="archipelago",
            position_hint="southeast", faction="潮鸢群岛", draw_icon=False
        )
        app.world_state.upsert_map_location(
            "鸦羽山脉", description=world.major_locations["鸦羽山脉"], feature_type="mountain_range",
            position_hint="west", draw_icon=False
        )
        app.world_state.upsert_map_location(
            "雾潮海岸", description="绯雨大陆南岸的终年雾海。", feature_type="coast", position_hint="south", draw_icon=False
        )
        app.world_state.upsert_map_location(
            "白花碑驿站", description=world.major_locations["白花碑驿站"], feature_type="settlement",
            relative_to="雾潮海岸", relative_position="north", faction="白花守望会", draw_icon=True
        )
        app.world_state.upsert_map_location(
            "奥涅里亚", description=world.major_locations["奥涅里亚"], feature_type="settlement",
            relative_to="镜线内海", relative_position="east", faction="奥涅里亚", draw_icon=True
        )
        app.world_state.upsert_map_location(
            "七号采掘器", description=world.major_locations["七号采掘器"], feature_type="landmark",
            relative_to="雾潮海岸", relative_position="west", faction="辉钢财团", draw_icon=True
        )
        app.world_state.upsert_map_location(
            "灵魂中枢", description=world.major_locations["灵魂中枢"], feature_type="landmark",
            relative_to="鸦羽山脉", relative_position="east", draw_icon=True
        )
        app.world_state.upsert_map_route(
            origin="白花碑驿站",
            destination="钟鸣公国",
            distance_days=2,
            route_type="land",
            terrain="旧钟路",
        )
        app.world_state.upsert_map_route(
            origin="白花碑驿站",
            destination="七号采掘器",
            distance_days=1,
            route_type="land",
            terrain="荒原巡逻线",
        )
        app.world_state.upsert_map_route(
            origin="钟鸣公国",
            destination="奥涅里亚",
            distance_days=3,
            route_type="water",
            terrain="内海商路",
        )
        app.world_state.upsert_map_route(
            origin="鸦羽山脉",
            destination="灵魂中枢",
            distance_days=1,
            route_type="land",
            terrain="碎月回声径",
        )
        app.world_map_manager.sync_from_world_state()

        self.validation_results = {}
        for draft_key in world.hero_drafts:
            validation = app.validate_hero_draft(draft_key)
            self.validation_results[draft_key] = {
                "ready": validation.ready,
                "missing_fields": list(validation.missing_fields),
                "errors": list(validation.errors),
                "warnings": list(validation.warnings),
            }
        self.created = app.create_confirmed_player_characters_from_drafts()
        self.first_act_candidates = app.session_zero_manager.generate_first_act_candidates()
        self.selected_first_act = (
            app.session_zero_manager.confirm_first_act(self.first_act_candidates[0].candidate_id)
            if self.first_act_candidates
            else None
        )
        world.completed = False
        runtime.log_manager.append_message(
            self.campaign_id,
            self.session_id,
            speaker="测试系统",
            content=(
                "超长测试已固定世界表、地图地点和三张合法角色卡，并刻意保留 Session 0 completed=False；"
                "接下来验证冒险门控、地图生成、预备地点图标、实时上下文和收团整理。"
            ),
            role="system",
            channel_id=self.channel_id,
            metadata={"validation": self.validation_results, "created": sorted(self.created)},
        )
        self.service._autosave_campaign(runtime, self.campaign_id)

    def _session_zero_turns(self) -> list[tuple[str, str]]:
        return [
            (
                "阿凛",
                "我想要一片类地球大陆地图，不要奇怪的环形世界。大陆叫绯雨大陆，西侧是鸦羽山脉，中央有镜线内海，"
                "东南是潮鸢群岛，南岸常年被雾潮覆盖。第一眼应该是水汽、白碑和远处冒烟的晶炉塔。",
            ),
            (
                "白河",
                "魔法和科技并存。灵魂晶炉能驱动车辆、工坊和医疗设备，但它会消耗人的记忆残响；古老精灵术被认为有自己的意志，"
                "它不喜欢晶炉，也不完全听从法师。",
            ),
            (
                "南星",
                "三十年前碎月坠落，赤羽旧王都一夜消失。赤羽遗民守护白花碑；钟鸣公国的大钟能安抚灵魂；"
                "潮鸢群岛每年归潮祭后都会少一座岛，但没人记得消失的是哪座。",
            ),
            (
                "阿凛",
                "我们的小队是护送碎月遗物的临时守护者，开局在白花碑驿站。我们护送一位灰晶病旅人去钟鸣公国，"
                "同时要躲开辉钢财团的记忆收购队。",
            ),
            (
                "白河",
                "主要反派先从辉钢财团开始。他们不是一眼看上去的恶棍，而是提供医疗、道路和就业的救世主形象；"
                "真正代价是病人被买走记忆，用来制造魔导兵器。",
            ),
            (
                "南星",
                "苍白司教团宣称灰晶病是灵魂升格的祝福，想把病人带去灵魂中枢。我们不知道他们是在救人还是在喂养某种东西。",
            ),
            (
                "阿凛",
                "我的角色伊莉雅是赤羽遗民的盾卫，主题是使命。她相信碎月遗物不该属于贵族或财团，只该回应愿意承担代价的人。",
            ),
            (
                "白河",
                "我的角色洛岚是钟鸣公国的流亡钟匠，主题是愧疚。他参与过灰晶晶炉早期设计，现在想确认自己是否间接害了这些病人。",
            ),
            (
                "南星",
                "我的角色赛璃是潮鸢群岛的御魂使航手，主题是希望。她相信消失的岛屿并没有毁灭，只是被引到灵魂之河某处。",
            ),
            (
                "阿凛",
                "安全方面：不要详细描写酷刑和性暴力；儿童遇险、身体病变和亲密内容淡出。我们希望故事有悲剧感，但不要绝望到底。",
            ),
            (
                "白河",
                "我希望第零章结束后，GM 可以在第一章过程中继续追问世界细节，比如某个驿站由谁管理、某条路为什么危险，"
                "然后把玩家回答写入世界观。",
            ),
            (
                "南星",
                "第一幕目标我投白花碑驿站：我们护送旅人出发前，财团收购队和苍白司教团同时抵达，逼我们选择先救人、守遗物还是追线索。",
            ),
        ]

    def _adventure_turns(self) -> list[tuple[str, str]]:
        return [
            ("阿凛", "伊莉雅把碎月遗物固定在盾后，先观察白花碑驿站外有没有辉钢财团的车辙、暗记和跟踪者。"),
            ("白河", "洛岚检查驿站的旧钟和附近灵魂晶炉，想知道钟声迟响是不是和灰晶病人的记忆流失有关。"),
            ("南星", "赛璃安抚那名灰晶病旅人，不描写病变细节；她问旅人最后记得是谁提出收购记忆。"),
            (
                "阿凛",
                "我补充一个世界细节：白花碑驿站由赤羽遗民的白花守望会管理，他们会把每个失去的名字刻在风铃内侧。"
                "伊莉雅请求守望会提供避开财团关卡的旧路。",
            ),
            ("白河", "洛岚想说服守望会长相信我们不是来夺走遗物，而是要把旅人安全送到钟鸣公国。需要检定的话请说明失败代价。"),
            ("南星", "赛璃拿出潮鸢群岛的海风铃，试着让旅人的零散记忆按声音浮现，寻找财团收购队的下一站。"),
            ("阿凛", "财团收购队如果逼近，伊莉雅站到门前举盾，公开要求对方说明收购病人记忆的合法依据。"),
            ("白河", "洛岚趁双方交涉时观察财团车辆，判断它们是否连接到七号采掘器，或者只是普通巡逻队。"),
            ("南星", "赛璃愿意消耗精神力做一个小型御魂仪式，确认旅人的记忆有没有被导向灵魂中枢。"),
            ("阿凛", "如果财团武装人员伸手抢旅人或遗物，我直接用盾撞开他，但目标是保护和缴械，不杀人。"),
            ("白河", "洛岚启动便携装置制造烟雾与钟声回响，帮大家从驿站后门撤到旧钟路。"),
            ("南星", "赛璃扶着旅人撤离，同时对守望会说：请把今天还记得的名字都写下来，等我们回来核对。"),
            ("阿凛", "我们沿旧钟路徒步前往钟鸣公国。伊莉雅负责殿后，留意是否有飞行侦察器或财团追兵。"),
            ("白河", "旅行第一日，洛岚尝试修复路边废弃的钟塔节点，让它短暂干扰财团通讯。"),
            ("南星", "旅行夜晚，赛璃请每个人说一句自己最不愿失去的记忆，她把这些话作为临时护符。"),
            ("阿凛", "第二天清晨，如果出现岔路，我希望询问一位守望会向导：哪条路更快，哪条路更安全？"),
            ("白河", "洛岚想把向导说的路线画进地图，并确认一个旅行日只是徒步一天能走的距离，不去问固定公里数。"),
            ("南星", "赛璃在风里听见潮鸢群岛的旋律，怀疑下一条线索和消失岛屿有关；她请 GM 给一个可追踪但不强迫的预兆。"),
            ("阿凛", "抵达弃钟塔遗迹后，伊莉雅先确认入口有没有适合设防的位置，然后护送旅人进入安全角落。"),
            ("白河", "洛岚调查弃钟塔的机械钟芯，想知道它是否记录了碎月之夜慢一拍的原因。"),
            ("南星", "赛璃用御魂术倾听钟塔里的残响，寻找一段愿意被听见而不是被抽取的记忆。"),
            ("阿凛", "如果地下层出现财团自动机，伊莉雅发起攻击，目标是吸引火力并保护洛岚和赛璃。"),
            ("白河", "洛岚寻找自动机的弱点：它是靠灵魂晶炉、普通蒸汽，还是远程指令行动？"),
            ("南星", "赛璃施放治愈术支援受伤同伴，并尝试让自动机内残留的记忆短暂安静下来。"),
            ("阿凛", "伊莉雅用挺身守护挡下一次最危险的攻击，然后喊洛岚切断它和外部通讯。"),
            ("白河", "洛岚把钟芯和便携装置接在一起，建立一个命刻式目标：三步内让自动机失去远程控制。"),
            ("南星", "赛璃对旅人说：你不需要交出所有记忆才能活下去。她想把这句话变成第一章的情感核心。"),
            ("阿凛", "战斗结束后，伊莉雅不处决敌人，选择留下自动机核心作为证据，并请守望会保存见证。"),
            ("白河", "洛岚打开钟塔里的宝箱或旧储物柜，看看有没有能帮助下一段旅程的稀有材料、金币或线索。"),
            ("南星", "赛璃建议在弃钟塔休息一晚，每个人更新一段羁绊或说出旅程后对彼此的新看法。"),
            ("阿凛", "收束第一章：伊莉雅决定继续护送旅人去钟鸣公国，但也要把七号采掘器和灵魂中枢作为后续主线目标。"),
            ("白河", "洛岚承认自己过去参与晶炉设计的愧疚，并决定回到钟鸣公国面对旧同僚。"),
            ("南星", "赛璃把旅人的一句残留记忆写进航海日志：‘钟声不是慢了，是在等一个还没回来的人。’我们今天到这里。"),
        ]

    def _runtime(self):
        return self.service._runtime(self.campaign_id)

    def _append_progress(self, record: dict[str, Any]) -> None:
        progress = {
            "index": record["index"],
            "label": record["label"],
            "status": record["status"],
            "ok": record["ok"],
            "elapsed_ms": record["elapsed_ms"],
            "reply_chars": len(record["reply"]),
            "time": datetime.now().isoformat(timespec="seconds"),
        }
        with self.progress_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(progress, ensure_ascii=False) + "\n")

    def _append_conversation(self, record: dict[str, Any]) -> None:
        with self.conversation_path.open("a", encoding="utf-8") as handle:
            handle.write(
                f"\n--- {record['index']:02d}. {record['label']} | "
                f"{record['elapsed_ms']}ms | status={record['status']} ok={record['ok']} ---\n"
            )
            if record["message"]:
                handle.write(f"{record['speaker']}: {record['message']}\n")
            if record["reply"]:
                handle.write(f"时悠: {record['reply']}\n")
            elif record["body"].get("error"):
                handle.write(f"error: {record['body']['error']}\n")
            else:
                handle.write(json.dumps(self._compact_body(record["body"]), ensure_ascii=False, indent=2) + "\n")

    def _build_report(self, *, exception: Exception | None) -> dict[str, Any]:
        runtime = self._runtime()
        app = runtime.app
        audit = getattr(self, "audit", {}) or {}
        gate_body = getattr(self, "gate_body", {}) or {}
        map_status = dict(gate_body.get("world_map") or {})
        map_events = [
            event for event in app.world_state.memory_events if event.kind in {"world_map_visual", "world_map_visual_error"}
        ]
        latest_map_event = next((event for event in reversed(map_events) if event.kind == "world_map_visual"), None)
        map_payload = dict(latest_map_event.payload) if latest_map_event else {}
        brief_path = str(map_payload.get("brief_path") or "")
        brief = self._load_json_file(brief_path)
        labels = {str(item.get("text", "")): item for item in brief.get("labels", []) if isinstance(item, dict)}
        prepared_icon_checks = {
            "奥涅里亚": labels.get("奥涅里亚", {}).get("iconName") == "oneria",
            "七号采掘器": labels.get("七号采掘器", {}).get("iconName") == "excavator_seven",
            "灵魂中枢": labels.get("灵魂中枢", {}).get("iconName") == "soul_nexus",
        }
        errors = [
            {
                "label": call["label"],
                "status": call["status"],
                "error": str(call["body"].get("error") or "请求返回 ok=false"),
            }
            for call in self.calls
            if call["status"] >= 400 or not call["ok"]
        ]
        degraded_turns = [
            {"label": call["label"], "reply": call["reply"]}
            for call in self.calls
            if "模型暂时没有接上" in call["reply"] or "本地兜底" in call["reply"]
        ]
        if exception is not None:
            errors.append({"label": "script_exception", "status": 500, "error": repr(exception)})
        transcript_txt = Path(audit.get("logs", {}).get("transcript_txt_path") or "")
        llm_payload = audit.get("llm", {}) if isinstance(audit, dict) else {}
        total_llm_calls = sum(int(payload.get("total_calls", 0)) for payload in llm_payload.values() if isinstance(payload, dict))
        world = app.world_state.world_profile
        session_zero_text = "\n".join(call["reply"] for call in self.calls if call["route"] == "/v1/session-zero/message")
        report = {
            "run_root": str(self.run_root),
            "campaign_id": self.campaign_id,
            "session_id": self.session_id,
            "channel_id": self.channel_id,
            "started_stamp": self.stamp,
            "exception": repr(exception) if exception else "",
            "errors": errors,
            "degraded_turns": degraded_turns,
            "calls": self.calls,
            "created_characters": sorted(character.name for character in app.character_manager.all() if "pc" in character.traits),
            "completed_before_adventure": getattr(self, "completed_before_adventure", None),
            "map": {
                "gate_status": map_status,
                "output_path": str(map_payload.get("output_path") or map_status.get("output_path") or ""),
                "output_exists": bool(
                    str(map_payload.get("output_path") or map_status.get("output_path") or "")
                    and Path(str(map_payload.get("output_path") or map_status.get("output_path"))).exists()
                ),
                "brief_path": brief_path,
                "brief_exists": bool(brief_path and Path(brief_path).exists()),
                "prepared_icon_checks": prepared_icon_checks,
                "label_icons": {
                    name: {
                        "iconName": labels.get(name, {}).get("iconName"),
                        "iconPlaceKind": labels.get(name, {}).get("iconPlaceKind"),
                    }
                    for name in ("奥涅里亚", "七号采掘器", "灵魂中枢")
                },
                "events": [asdict(event) for event in map_events],
            },
            "assertions": {
                "real_llm_was_used": total_llm_calls > 0,
                "session_zero_incomplete_before_adventure": getattr(self, "completed_before_adventure", None) is False,
                "map_generated_before_first_turn": map_status.get("status") in {"generated", "ready"},
                "map_file_exists": bool(
                    str(map_payload.get("output_path") or map_status.get("output_path") or "")
                    and Path(str(map_payload.get("output_path") or map_status.get("output_path"))).exists()
                ),
                "prepared_icons_one_to_one_rendered": all(prepared_icon_checks.values()),
                "travel_day_not_player_configured": not bool(world.travel_day_length),
                "no_world_shape_question": "世界形状" not in session_zero_text,
                "no_travel_day_length_question": "旅行日长度" not in session_zero_text,
                "transcript_txt_exists": transcript_txt.exists(),
                "no_unrecovered_model_turns": not degraded_turns,
            },
            "latency": {
                "slowest_calls": sorted(self.calls, key=lambda item: item["elapsed_ms"], reverse=True)[:15],
                "http": audit.get("runtime", {}).get("http", {}) if isinstance(audit, dict) else {},
                "pipeline": audit.get("runtime", {}).get("pipeline", {}) if isinstance(audit, dict) else {},
                "llm": llm_payload,
            },
            "artifacts": {
                "progress_jsonl": str(self.progress_path),
                "conversation_txt": str(self.conversation_path),
                "transcript_txt": str(transcript_txt),
                "snapshot": str(audit.get("runtime", {}).get("last_saved_path") or "") if isinstance(audit, dict) else "",
            },
        }
        return report

    def _write_report(self, report: dict[str, Any]) -> None:
        self.report_json_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=lambda value: getattr(value, "value", str(value))),
            encoding="utf-8",
        )
        self.report_txt_path.write_text(self._format_report(report), encoding="utf-8")

    def _format_report(self, report: dict[str, Any]) -> str:
        lines = [
            "FU-GM 超长真实跑团测试报告",
            f"campaign_id: {report['campaign_id']}",
            f"session_id: {report['session_id']}",
            f"run_root: {report['run_root']}",
            "",
            "=== 关键结论 ===",
            f"错误数量: {len(report['errors'])}",
            f"重试后仍降级轮次: {len(report['degraded_turns'])}",
            f"真实 LLM 调用: {report['assertions']['real_llm_was_used']}",
            f"正式 PC: {', '.join(report['created_characters'])}",
            f"冒险前 Session 0 completed: {report['completed_before_adventure']}",
            f"地图状态: {json.dumps(report['map']['gate_status'], ensure_ascii=False)}",
            f"地图文件: {report['map']['output_path']}",
            f"地图文件存在: {report['map']['output_exists']}",
            f"地图 brief: {report['map']['brief_path']}",
            f"完整 API 对话: {report['artifacts']['conversation_txt']}",
            f"正式 transcript: {report['artifacts']['transcript_txt']}",
            "",
            "=== 验收断言 ===",
        ]
        for key, value in report["assertions"].items():
            lines.append(f"{key}: {value}")
        lines.append("")
        lines.append("=== 预备地点图标检查 ===")
        for name, ok in report["map"]["prepared_icon_checks"].items():
            lines.append(f"{name}: {ok} | {json.dumps(report['map']['label_icons'].get(name, {}), ensure_ascii=False)}")
        if report["errors"]:
            lines.extend(["", "=== 错误 ==="])
            for error_item in report["errors"]:
                lines.append(json.dumps(error_item, ensure_ascii=False))
        if report["degraded_turns"]:
            lines.extend(["", "=== 降级轮次 ==="])
            for degraded in report["degraded_turns"]:
                lines.append(json.dumps(degraded, ensure_ascii=False))
        lines.extend(["", "=== 最慢 API 调用 ==="])
        for call in report["latency"]["slowest_calls"]:
            lines.append(f"{call['elapsed_ms']}ms | {call['label']} | {call['method']} {call['route']} | ok={call['ok']}")
        lines.extend(["", "=== LLM 延迟 ==="])
        for component, payload in report["latency"]["llm"].items():
            if not isinstance(payload, dict):
                continue
            lines.append(
                f"{component}: calls={payload.get('total_calls', 0)}, "
                f"avg_recent={payload.get('average_recent_elapsed_ms', 0)}ms"
            )
            for call in payload.get("slowest_recent", [])[:5]:
                lines.append(
                    f"  {call.get('elapsed_ms', 0)}ms | model={call.get('model', '')} | "
                    f"ok={call.get('ok', False)} | error={call.get('error', '')}"
                )
        lines.extend(["", "=== 游戏回合流水线 ==="])
        for span in report["latency"]["pipeline"].get("recent_turns", []):
            lines.append(json.dumps(span, ensure_ascii=False))
        lines.extend(["", "=== 完整 API 对话 ==="])
        lines.append(Path(report["artifacts"]["conversation_txt"]).read_text(encoding="utf-8"))
        return "\n".join(lines)

    def _compact_body(self, body: dict[str, Any]) -> dict[str, Any]:
        keys = ("ok", "campaign_id", "session_id", "status", "world_map", "summary", "runtime", "astrbot_bridge")
        compact = {key: body[key] for key in keys if key in body}
        if not compact:
            compact = {"keys": sorted(body.keys())[:30]}
        return compact

    def _load_json_file(self, path: str) -> dict[str, Any]:
        if not path:
            return {}
        try:
            return json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}


def main() -> int:
    return UltraCampaignHarness().run()


if __name__ == "__main__":
    raise SystemExit(main())
