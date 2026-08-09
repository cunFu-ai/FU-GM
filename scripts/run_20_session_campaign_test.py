from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import sys
import time
import traceback
import threading
import types
from collections import Counter
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from pathlib import Path
from statistics import mean
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
SCRIPTS_ROOT = PROJECT_ROOT / "scripts"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from fu_gm.models import (  # noqa: E402
    Character,
    Clock,
    DungeonExploreMode,
    SceneType,
    SessionFeedbackSignals,
    SessionDramaticContract,
    SessionSceneOpportunity,
)
from fu_gm.config import LLMConfig, uses_high_latency_model  # noqa: E402
from fu_gm.http_server import FUGMHttpService, make_server  # noqa: E402
from fu_gm.gm_tool_contracts import GMToolExecutionContext  # noqa: E402
from fu_gm.llm_client import (  # noqa: E402
    ChatMessage,
    LLMDeadlineExceeded,
    LLMEmptyResponseError,
    LLMHTTPError,
    OpenAICompatibleClient,
)
from fu_gm.testing.legal_actions import LegalActionLayer  # noqa: E402
from fu_gm.testing.player_simulator import ConstrainedPlayerSimulator  # noqa: E402
from fu_gm.testing.replay_models import ReplayScenario, ReplayStep  # noqa: E402
from fu_gm.testing.conversation_quality import ConversationQualityAuditor  # noqa: E402
from fu_gm.testing.quality_attribution import LongRunIssueAttributor  # noqa: E402
from fu_gm.testing.campaign_checkpoint import CampaignRunCheckpoint  # noqa: E402
from fu_gm.testing.session_progress_evaluator import (  # noqa: E402
    SessionProgressAssessment,
    SessionProgressEvaluator,
)
from fu_gm.components.session_closure_policy import SessionActEvidence  # noqa: E402
from fu_gm.components.session_scene_navigator import SessionSceneNavigator  # noqa: E402
from fu_gm.components.scene_transition_coordinator import SceneTransitionCoordinator  # noqa: E402
from fu_gm.components.scene_cast_coordinator import SceneCastCoordinator  # noqa: E402
from fu_gm.components.clock_narrative_boundary import ClockNarrativeBoundary  # noqa: E402
from fu_gm.components.npc_response_window_manager import (  # noqa: E402
    NPCResponseWindowManager,
)
from run_ultra_from_scratch_campaign_test import FromScratchUltraHarness  # noqa: E402


@dataclass
class CampaignSessionSpec:
    number: int
    title: str
    arc: str
    gm_opening: str
    turns: list[tuple[str, str]]
    expected_focus: list[str] = field(default_factory=list)
    boss_session: bool = False
    notes: list[str] = field(default_factory=list)


class _FakeAstrResult:
    def __init__(self, text: str) -> None:
        self.text = text
        self.stopped = False

    def stop_event(self):
        self.stopped = True
        return self


class _FakeAstrEvent:
    def __init__(
        self,
        *,
        message_str: str,
        group_id: str,
        session_id: str,
        sender_id: str,
        sender_name: str,
    ) -> None:
        self.message_str = message_str
        self._group_id = group_id
        self._session_id = session_id
        self._sender_id = sender_id
        self._sender_name = sender_name
        self.stopped = False

    def get_group_id(self) -> str:
        return self._group_id

    def get_session_id(self) -> str:
        return self._session_id

    def get_sender_id(self) -> str:
        return self._sender_id

    def get_user_id(self) -> str:
        return self._sender_id

    def get_sender_name(self) -> str:
        return self._sender_name

    def plain_result(self, text: str) -> _FakeAstrResult:
        return _FakeAstrResult(text)

    def stop_event(self):
        self.stopped = True
        return None


class TwentySessionCampaignHarness(FromScratchUltraHarness):
    """Runs a single continuous campaign-length soak test.

    The goal is not to isolate features. It intentionally keeps one campaign
    runtime alive, letting summaries, memory, clocks, villains and pacing carry
    from session to session.
    """

    LENGTH_BY_TARGET = {20: "short", 35: "standard", 50: "long"}
    SIGNATURE_META_MARKERS = (
        "选定一件",
        "首次出镜",
        "固定其感官",
        "标志画面",
        "可被触碰或改变",
    )
    GENERIC_NPC_NAMES = frozenset(
        {
            "",
            "世界威胁",
            "世界奥秘",
            "现场阻力",
            "现场人物",
            "现场关键人物",
            "对立方",
        }
    )
    ARC_BY_TARGET = {20: 5, 35: 5, 50: 6}
    GM_STINGER_SESSIONS = {5, 10, 15, 19}
    EPISODE_IDENTITIES: dict[int, dict[str, Any]] = {
        1: {
            "question": "队伍能否在不出卖失忆旅人的前提下，让守望会在巡逻队抵达前打开旧路？",
            "image": "没有风时，一只白花风铃独自迟响，铃舌内侧刻着被刮去一半的名字。",
            "opposition": "守望会会长要保护旧路秘密，财团巡逻队要带走旅人。",
            "reversal": "被刮去的名字并非旅人的，而属于伊莉雅本应记得的人。",
            "escalation": ["会长提出具体担保条件", "远处巡逻灯开始逐盏熄灭路标", "旧路与正门只能保住一边"],
            "payoff": ["旧路是否开放", "守望会对队伍的初始态度", "旅人第一次主动说出一个名字"],
        },
        2: {
            "question": "队伍愿意牺牲休息、补给还是隐蔽，换取旅人安全穿过雾潮旧路？",
            "image": "退潮后的里程碑露出一排已经不存在的村名，雾中每个人读到的顺序都不同。",
            "opposition": "雾潮与追踪标记不断缩小安全路线，旅人的状况也不允许无限赶路。",
            "reversal": "追踪标记不是落在队伍身上，而是旅人会无意识回应财团信标。",
            "escalation": ["旧路信标失去一段", "旅人在睡梦中回应远处钟声", "追兵与潮水迫使队伍舍弃一项准备"],
            "payoff": ["路线、休息和隐蔽的取舍", "一处营地成为可回访地点", "追兵掌握的信息发生改变"],
        },
        3: {
            "question": "队伍能否让旅人的证词被正式听见，而不是被合法程序与恐惧吞没？",
            "image": "正午大钟每逢一句谎言便少响一声，旁听席却假装什么也没听见。",
            "opposition": "财团代理人要把案件降格成财产纠纷，听证官害怕公国承担后果。",
            "reversal": "所谓合法收购文书上的见证签名，属于一个已被所有人忘记的官员。",
            "escalation": ["代理人质疑旅人身份", "旁听证人临时改口", "听证官必须当场决定是否封存证据"],
            "payoff": ["听证是否立案", "医师协会是否站队", "财团第一次留下公开法律败绩或胜绩"],
        },
        4: {
            "question": "英雄能否从逐渐灌满的水道带出证据与被困者，而不触发全城警报？",
            "image": "灰晶箱破裂后，水面漂着一串发光的名字，碰到管壁就被磨掉一个字。",
            "opposition": "走私机关要销毁样本，水位与巡逻系统会封闭退路。",
            "reversal": "样本不是成品，而是从钟鸣居民身上抽取的记忆废料。",
            "escalation": ["旧阀门开始倒灌", "走私者切断一条退路", "只能优先带走人、样本或完整账册中的两项"],
            "payoff": ["获救者名单", "证据链完整度", "地下水道是否成为己方秘密路线"],
        },
        5: {
            "question": "队伍能否在钟塔崩坏前保住证人和证据，并决定如何处置财团代理人？",
            "image": "裂开的正午大钟悬在城市上空，断面里不是铜，而是一层层被封存的回忆。",
            "opposition": "财团代理人要烧毁证据后借钟声逃离，并逼钟鸣卫队替他挡路。",
            "reversal": "代理人只是奉命回收一段会证明摄政王参与其中的钟声记录。",
            "escalation": ["钟塔台阶坍塌", "卫队被假命令分裂", "代理人启动会摧毁证据的钟鸣装置"],
            "payoff": ["证人和证据各自是否保住", "代理人的命运", "第一幕中哪个派系公开站到队伍一边"],
        },
        6: {
            "question": "队伍能否弄清摄政王合作的真正代价，并决定是否接受一份带条件的盟约？",
            "image": "王都港口所有钟都停在不同的时刻，唯有摄政王书房里的沙漏仍向上流。",
            "opposition": "摄政王要保住舰队与王位，不愿公开自己用海图向财团抵押了什么。",
            "reversal": "摄政王并非不知道代价，而是在用合作延缓一座岛被世界遗忘。",
            "escalation": ["港口行会公开质问", "王室卫兵封闭医舍", "摄政王提出只能秘密履行的交换条件"],
            "payoff": ["是否接受摄政王援助", "港口行会与王室的态度", "一条通往空白海域的真实航线"],
        },
        7: {
            "question": "队伍能否找到不存在于同一张记忆里的岛，并让飞翼船平安抵达？",
            "image": "每位船员手中的海图都缺着不同形状的一块，拼在一起才像一只睁开的眼。",
            "opposition": "季风、错误记忆与财团远程标记共同把船引向错误海域。",
            "reversal": "被忘记的岛仍在移动，它正主动避开携带灰晶信号的人。",
            "escalation": ["船员对航向产生分裂", "灵魂晶炉收到伪造灯塔信号", "风暴迫使队伍选择追岛或救另一艘船"],
            "payoff": ["船员是否信任队伍", "空白岛的真实方向", "被救或被放弃的海上见证人"],
        },
        8: {
            "question": "英雄能否唤醒半沉灯塔并取回岛名，而不让遗迹与守护者一同沉没？",
            "image": "灯塔光束扫过海面时，水下浮现一座倒悬的街道，窗中有人抬头回应。",
            "opposition": "遗迹守护机制要把所有来访者归档为失踪者，潮汐正吞没核心层。",
            "reversal": "灯塔不是寻找岛屿，而是在替财团筛选仍有人记得的地点。",
            "escalation": ["入口被潮水切断", "守护者要求交出一个真实名字", "核心只能恢复岛名或保存航路记录之一"],
            "payoff": ["岛名是否归还", "灯塔奥灵的态度", "摄政王抵押海图的证据"],
        },
        9: {
            "question": "苍祈与队伍能否修复树誓村社的信任，并阻止活人的名字继续出现在树皮上？",
            "image": "树皮上的名字在月光下渗出银色树脂，每念错一个字就有一片叶子变黑。",
            "opposition": "村社要保护奥灵不再受骗，森林则把所有外来承诺视作新的伤口。",
            "reversal": "名字不是死亡预告，而是森林在替被记忆炉触及的人保存最后备份。",
            "escalation": ["村社拒绝祈祷", "一名孩子的名字新出现在树上", "奥灵要求苍祈兑现旧承诺后才给答案"],
            "payoff": ["苍祈是否修复契约", "村社是否成为盟友", "被保存名字的用途"],
        },
        10: {
            "question": "英雄能否承受碎月真相，并选择下一步该公开、潜入还是先保护谁？",
            "image": "最古老的树轮一圈圈亮起，每一圈都用不同人的声音念出同一个被抹去的名字。",
            "opposition": "真相会撕裂现有联盟，财团代理人则正赶来烧毁森林备份。",
            "reversal": "记忆炉并非因碎月而生；碎月是第一次启动记忆炉时被撕下的灵魂之河残片。",
            "escalation": ["树轮只允许一次提问", "盟友对公开时机产生分歧", "代理人点燃森林边缘逼迫立即选择"],
            "payoff": ["中盘真相被谁知晓", "队伍下一幕方向", "一个角色主题被公开挑战"],
        },
        11: {
            "question": "队伍能否进入第七采掘城、保护病人并找到愿意冒险帮助他们的内部人？",
            "image": "换班钟一响，矿工们交换名牌继续工作，仿佛连自己的名字也属于财团。",
            "opposition": "财团宣传与门禁把病人塑造成自愿献出记忆的模范。",
            "reversal": "部分矿工确实主动签约，因为财团承诺替家人保存即将消失的名字。",
            "escalation": ["接待员核验伪装", "病人被提前转运", "旧同事要求洛岚先救自己的家人"],
            "payoff": ["潜入身份是否保住", "内部盟友是谁", "矿工把队伍视为救援者还是破坏者"],
        },
        12: {
            "question": "英雄能否穿过记忆炉矿道救回可救之人，并决定哪些残响必须放手？",
            "image": "无人驾驶的矿车载着过去的谈话循环驶过，每次回程都会少一个说话者。",
            "opposition": "矿道系统要把残响送入熔炉，守卫与怪物只服从早已过期的安全协议。",
            "reversal": "矿道怪物由被丢弃的记忆聚成，它攻击的是携带财团权限的人。",
            "escalation": ["运输轨道改道", "残响开始互相覆盖", "队伍必须在救援与追赶控制车之间分工"],
            "payoff": ["获救残响与名字", "怪物是否成为向导", "进入熔炉中枢的代价"],
        },
        13: {
            "question": "队伍能否取得完整停机钥匙，并决定熔炉停机时哪些被封存记忆会先承受冲击？",
            "image": "熔炉上空飘落灰色雪片，每片落在皮肤上都会短暂想起陌生人的一生。",
            "opposition": "熔炉守护者要维持生产，控制系统会把受困记忆当作稳定燃料。",
            "reversal": "完整停机协议需要洛岚承认并输入自己当年的设计权限。",
            "escalation": ["守护者启动多目标清除", "工程进度暴露洛岚身份", "停机会释放一波无法全部保护的记忆冲击"],
            "payoff": ["停机钥匙是否取得", "洛岚过去是否公开", "灰晶熔炉与矿工命运"],
        },
        14: {
            "question": "联盟能否在世界开始遗忘时选出共同保护的第一批人、地点与记忆？",
            "image": "街上的招牌从最旧的一笔开始变白，人们站在自己家门前却说不出门牌。",
            "opposition": "记忆集中协议把分散遗忘包装成有序救援，各派都想优先保住自己。",
            "reversal": "协议确实暂时救下了一部分人，但代价是把遗忘转嫁给无人代表的地区。",
            "escalation": ["各地求援同时抵达", "联盟代表争夺保护顺序", "一个熟悉地点从公开记录中消失"],
            "payoff": ["联盟优先保护谁", "被放弃者如何回应", "反派计划第一次成为全世界可见的事实"],
        },
        15: {
            "question": "英雄能否在不让议事厅沦为战场的情况下迫使艾蕾娜面对她选择牺牲的人？",
            "image": "议事厅每个人面前的水杯都回响着一段被集中协议删去的声音，艾蕾娜的杯子却沉默。",
            "opposition": "艾蕾娜要证明集中管理是唯一能阻止世界崩解的办法，并分裂联盟。",
            "reversal": "艾蕾娜也失去过最重要的名字，她把空白当成制度必须消灭的错误。",
            "escalation": ["艾蕾娜公开一项真实救援成果", "联盟内部有人倒向她", "谈判破裂时她用终结点改变现场或撤离"],
            "payoff": ["联盟是否维持", "艾蕾娜与队伍关系升级", "谁承担集中协议的短期后果"],
        },
        16: {
            "question": "队伍能否在喘息中修复关系与装备，并明确自己愿意带进终局的承诺？",
            "image": "被修好的物件偶尔用陌生声音说出一个名字，营火旁每个人都能选择是否回应。",
            "opposition": "疲惫、愧疚与联盟债务让所有准备都带着无法同时满足的请求。",
            "reversal": "一项看似技术性的停机模拟暴露出终局需要有人主动保留炉心中的痛苦记忆。",
            "escalation": ["伤员与盟友提出不同请求", "工程模拟第一次失败", "角色必须说出一项不会交给别人承担的责任"],
            "payoff": ["恢复与工程成果", "至少一段角色关系改变", "终局承诺清单"],
        },
        17: {
            "question": "三方会盟能否形成各自愿意承担代价的计划，而不是把风险全交给英雄？",
            "image": "三面旗帜在灯塔光下只投出一道影子，风向一变，影子却裂成三条路。",
            "opposition": "王室、村社与公国都愿意支援，却都想保留最后撤出的权利。",
            "reversal": "最终潜入不缺兵力，真正缺的是一个愿意在全世界面前敲响真相的大钟。",
            "escalation": ["三方争执指挥权", "财团发来最后通牒", "每一方必须公开承诺一项不可撤回的支援"],
            "payoff": ["支援清单", "会盟指挥关系", "终局失败时谁仍会留下"],
        },
        18: {
            "question": "联盟能否打开采掘城外环并撤出平民，而不把城市变成自己要反对的废墟？",
            "image": "撤离警报唱着一首赤羽旧摇篮曲，许多守卫第一次想起自己小时候听过它。",
            "opposition": "外环防线要把平民当作稳定协议的人质，部分守卫仍相信撤离会毁掉世界。",
            "reversal": "安全路线经过一座维持外环记忆的节点，破坏它会让整片街区忘记回家的路。",
            "escalation": ["海陆攻势迫使守卫封门", "撤离人群与突入路线冲突", "外环节点进入不可逆过载"],
            "payoff": ["平民伤亡与撤离规模", "外环是否保留", "守卫是否倒戈"],
        },
        19: {
            "question": "队伍能否关闭协议塔倒计时，并让艾蕾娜在最终炉心前作出无法回避的选择？",
            "image": "塔内每层都随着一次记忆改写旋转，窗外同一座城市不断换成陌生的名字。",
            "opposition": "协议塔守卫拖延关闭，艾蕾娜试图把所有选择压缩成继续或毁灭。",
            "reversal": "最后一块真相表明旅人是协议最初用来测试“可被世界忘记的人”。",
            "escalation": ["塔层错位切断队伍", "倒计时抹去一名盟友的公开记录", "艾蕾娜用最后资源保护炉心或救人"],
            "payoff": ["协议塔是否关闭", "旅人真实身份", "艾蕾娜进入终局时的立场"],
        },
        20: {
            "question": "英雄会如何归还所有名字，并决定一个保留痛苦记忆却仍能自由生活的新世界？",
            "image": "碎月残片绕着炉心旋转，每一片都映出一张被遗忘的脸，名字像星群一样重新亮起。",
            "opposition": "失控炉心与艾蕾娜最后的信念都要求有人替世界决定该记住什么。",
            "reversal": "炉心无法替所有人无痛归还记忆；英雄必须选择如何让人们共同承担真相。",
            "escalation": ["炉心分阶段失控", "艾蕾娜作出最后行动", "名字归还要求全队兑现一路作出的承诺"],
            "payoff": ["被遗忘者的名字归还方式", "艾蕾娜与旅人的结局", "五名英雄和世界的具体尾声"],
        },
    }
    UPGRADE_PLANS: dict[str, list[tuple[str, str]]] = {
        "伊莉雅": [
            ("守护者", "防御精通"),
            ("元素使", "魔法炮击"),
            ("守护者", "防御精通"),
            ("元素使", "魔法炮击"),
            ("守护者", "防御精通"),
            ("元素使", "魔法炮击"),
            ("守护者", "防御精通"),
            ("元素使", "天灾骤降"),
            ("守护者", "铁壁"),
            ("元素使", "天灾骤降"),
        ],
        "赛璃": [
            ("御魂使", "治愈之力"),
            ("旅人", "充足补给"),
            ("御魂使", "治愈之力"),
            ("旅人", "充足补给"),
            ("御魂使", "法术支援"),
            ("旅人", "充足补给"),
            ("御魂使", "生命秘法"),
            ("旅人", "酒馆攀谈"),
            ("御魂使", "灵魂魔法"),
            ("旅人", "酒馆攀谈"),
        ],
        "洛岚": [
            ("造物使", "秘密配方"),
            ("武器大师", "近战武器精通"),
            ("造物使", "秘密配方"),
            ("武器大师", "近战武器精通"),
            ("造物使", "秘密配方"),
            ("武器大师", "近战武器精通"),
            ("造物使", "秘密配方"),
            ("武器大师", "近战武器精通"),
            ("造物使", "药剂雨"),
            ("武器大师", "利刃风暴"),
        ],
        "艾薇娅": [
            ("游说家", "予以信任"),
            ("熵术士", "灵智回流"),
            ("游说家", "予以信任"),
            ("熵术士", "灵智回流"),
            ("游说家", "巧舌如簧"),
            ("熵术士", "灵智回流"),
            ("游说家", "巧舌如簧"),
            ("熵术士", "灵智回流"),
            ("游说家", "意外盟友"),
            ("熵术士", "灵智回流"),
        ],
        "苍祈": [
            ("奥灵使", "奥灵回响"),
            ("拟兽使", "摄能为食"),
            ("奥灵使", "奥灵回响"),
            ("拟兽使", "摄能为食"),
            ("奥灵使", "奥灵回响"),
            ("拟兽使", "摄能为食"),
            ("奥灵使", "奥灵回响"),
            ("拟兽使", "摄能为食"),
            ("奥灵使", "奥灵疗愈"),
            ("拟兽使", "摄能为食"),
        ],
    }

    def __init__(
        self,
        *,
        target_sessions: int = 20,
        run_astrbot_smoke: bool = True,
        semantic_llm: bool = True,
        scripted_identities: bool = False,
        setup_only: bool = False,
        resume_root: Path | str | None = None,
        fail_fast_route_mismatch: bool | None = None,
    ) -> None:
        resume_checkpoint: CampaignRunCheckpoint | None = None
        resolved_resume_root: Path | None = None
        if resume_root is not None:
            (
                resolved_resume_root,
                _resume_checkpoint_path,
                resume_checkpoint,
            ) = CampaignRunCheckpoint.load_resume_source(Path(resume_root))
            target_sessions = resume_checkpoint.target_sessions
        self.target_sessions = int(target_sessions)
        # A short pilot still represents the opening of a 20-session campaign;
        # otherwise a one-session diagnostic is incorrectly paced as a finale.
        self.campaign_profile_sessions = max(20, self.target_sessions)
        self.semantic_llm = bool(semantic_llm)
        if fail_fast_route_mismatch is None:
            fail_fast_route_mismatch = str(
                os.environ.get("FU_GM_LONG_TEST_FAIL_FAST_ROUTE_MISMATCH", "1")
            ).strip().lower() not in {"0", "false", "no", "off"}
        self.fail_fast_route_mismatch = bool(fail_fast_route_mismatch)
        self._llm_preflight_attempted = False
        self._llm_preflight_ok = False
        self._llm_preflight_error = ""
        # The normal semantic run stops at the first structural quality
        # failure.  Report construction may still make a few local API calls;
        # keep those diagnostic calls from recursively re-triggering the same
        # gate and hiding the report that explains the original failure.
        self._quality_gate_enabled = True
        self.scripted_identities = bool(scripted_identities)
        self.setup_only = bool(setup_only)
        self._setup_only_completed = False
        self.length_profile = self.LENGTH_BY_TARGET.get(self.campaign_profile_sessions, "standard")
        self.target_arcs = self.ARC_BY_TARGET.get(self.campaign_profile_sessions)
        self.run_astrbot_smoke = run_astrbot_smoke
        self.stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.run_root = resolved_resume_root or (
            PROJECT_ROOT
            / ".runtime"
            / "large_tests"
            / f"campaign_{self.target_sessions}_session_{self.stamp}"
        )
        self.checkpoint_path = self.run_root / "campaign_checkpoint.json"
        self.checkpoint_root = self.run_root / ".checkpoints"
        self.campaign_root = self.run_root / "campaigns"
        self.map_root = self.run_root / "maps"
        self.progress_path = self.run_root / "progress.jsonl"
        self.conversation_path = self.run_root / f"full_{self.target_sessions}_session_conversation.txt"
        self.conversation_export_path = self.run_root / f"完整{self.target_sessions}场对话记录.txt"
        self.report_json_path = self.run_root / f"{self.target_sessions}_session_campaign_report.json"
        self.report_txt_path = self.run_root / f"{self.target_sessions}_session_campaign_report.txt"
        self.run_root.mkdir(parents=True, exist_ok=True)
        self.map_root.mkdir(parents=True, exist_ok=True)
        if resume_checkpoint is not None:
            self.campaign_root = self._restore_checkpoint_campaign(
                resume_checkpoint,
                self.run_root,
                self.stamp,
            )
        self.campaign_root.mkdir(parents=True, exist_ok=True)

        os.environ["FU_GM_DATA_ROOT"] = str(self.campaign_root)
        os.environ["FU_GM_MAP_OUTPUT_DIR"] = str(self.map_root)
        os.environ["FU_GM_NORTANTIS_OUTPUT_DIR"] = str(self.map_root)
        os.environ["FU_GM_PROJECT_DIR"] = str(PROJECT_ROOT)
        os.environ.setdefault("FU_GM_WORLD_MAP_RENDERER", "nortantis")
        # A long run must survive brief gateway bursts without letting one bad
        # socket stall an entire campaign for hours. These limits only affect
        # this harness and remain overridable for endpoint soak tests.
        configured_action_model = os.environ.get("FU_GM_ACTION_MODEL", "gpt-5.6-luna")
        high_latency_model = uses_high_latency_model(configured_action_model)
        long_test_timeout = os.environ.get(
            "FU_GM_LONG_TEST_LLM_TIMEOUT_SECONDS",
            "120" if high_latency_model else "60",
        )
        long_test_retries = os.environ.get("FU_GM_LONG_TEST_LLM_RECOVERY_RETRIES", "2")
        os.environ["FU_GM_TIMEOUT_SECONDS"] = long_test_timeout
        os.environ["FU_GM_ACTION_TIMEOUT_SECONDS"] = long_test_timeout
        os.environ["FU_GM_EXPRESSOR_TIMEOUT_SECONDS"] = long_test_timeout
        os.environ["FU_GM_SESSION_ZERO_TIMEOUT_SECONDS"] = long_test_timeout
        os.environ.setdefault(
            "FU_GM_TOOL_AGENT_TIMEOUT_SECONDS",
            "600" if high_latency_model else "300",
        )
        os.environ.setdefault("FU_GM_TOOL_AGENT_MAX_ITERATIONS", "8")
        # Structured GM decisions occasionally need one repair for a malformed
        # batch and a second for the provider's repaired JSON.  Keep a third
        # bounded repair available so a transient formatting fault cannot
        # discard a valid player contribution; the shared transaction deadline
        # still prevents unbounded retries.
        os.environ.setdefault("FU_GM_TOOL_AGENT_PARSE_RETRIES", "3")
        os.environ["FU_GM_CORE_GM_TIMEOUT_SECONDS"] = os.environ.get(
            "FU_GM_LONG_TEST_ROUTER_TIMEOUT_SECONDS",
            str(max(120.0 if high_latency_model else 90.0, float(long_test_timeout))),
        )
        os.environ.setdefault(
            "FU_GM_CORE_GM_ENDPOINT_ATTEMPT_TIMEOUT_SECONDS",
            "45" if high_latency_model else "35",
        )
        os.environ.setdefault("FU_GM_CORE_GM_RECOVERY_MAX_RETRIES", "3")
        os.environ.setdefault(
            "FU_GM_ENDPOINT_ATTEMPT_TIMEOUT_SECONDS",
            "45" if high_latency_model else "20",
        )
        os.environ["FU_GM_REACTIVE_RECOVERY_MAX_RETRIES"] = long_test_retries
        os.environ["FU_GM_ALLOW_HEURISTIC_FALLBACK"] = "0" if self.semantic_llm else "1"
        self.min_table_turns_per_session = max(
            20,
            int(os.environ.get("FU_GM_LONG_TEST_MIN_TURNS_PER_SESSION", "28")),
        )
        self.max_table_turns_per_session = max(
            self.min_table_turns_per_session + 6,
            int(os.environ.get("FU_GM_LONG_TEST_MAX_TURNS_PER_SESSION", "42")),
        )
        self.gm_beats_per_session = max(
            1,
            int(os.environ.get("FU_GM_LONG_TEST_GM_BEATS_PER_SESSION", "3")),
        )

        self.campaign_id = (
            resume_checkpoint.campaign_id
            if resume_checkpoint is not None
            else f"{self.target_sessions}场完整战役_白钟大陆_{self.stamp}"
        )
        self.session_id = "session-zero"
        self.channel_id = f"{self.target_sessions}-session-longrun"
        self.common = {
            "campaign_id": self.campaign_id,
            "session_id": self.session_id,
            "channel_id": self.channel_id,
        }
        self.service = FUGMHttpService(data_root=self.campaign_root, use_llm=self.semantic_llm)
        self.calls: list[dict[str, Any]] = []
        self.notes: list[str] = []
        self.errors: list[str] = []
        self.tool_events: list[dict[str, Any]] = []
        self.session_reports: list[dict[str, Any]] = []
        self.rules_only_session_results: list[dict[str, Any]] = []
        self.astrbot_bridge_results: list[dict[str, Any]] = []
        self.heartbeat_results: list[dict[str, Any]] = []
        self.session_table_metrics: dict[int, dict[str, Any]] = {}
        self.session_scene_metrics: dict[int, dict[str, Any]] = {}
        self.player_simulation_metrics: list[dict[str, Any]] = []
        self.session_progress_assessments: dict[int, SessionProgressAssessment] = {}
        self.session_completion_results: dict[int, dict[str, Any]] = {}
        self._session_progress_evaluator: SessionProgressEvaluator | None = None
        self._session_progress_client: OpenAICompatibleClient | None = None
        self.session_scene_navigator = SessionSceneNavigator()
        self.conversation_quality_auditor = ConversationQualityAuditor()
        self.level_up_results: list[dict[str, Any]] = []
        self._previous_session_summary = ""
        self._auto_followup_depth = 0
        self.expected_rules_blocked_labels: set[str] = set()
        self.pc_names = ["伊莉雅", "赛璃", "洛岚", "艾薇娅", "苍祈"]
        self._upgrade_cursors = {name: 0 for name in self.pc_names}
        self.player_simulator = ConstrainedPlayerSimulator(
            use_llm=self.semantic_llm,
            # Continue broad campaign coverage with a separately reported,
            # validator-approved fallback when the player model ignores all
            # repair prompts. The run must still fail its model-quality gate.
            continue_on_invalid=True,
        )
        self.player_legal_actions = LegalActionLayer()
        self._resume_completed_session = 0
        self._in_progress_session_state: dict[str, Any] = {}
        # Prepared act opportunities are not authority over PC position.  This
        # only records that the long-test GM has publicly offered a route; the
        # next scene still needs a resolved player movement anchor.
        self._pending_scene_transition: dict[str, Any] = {}
        self._adventure_started = False
        self._resume_checkpoint_loaded = resume_checkpoint is not None
        if resume_checkpoint is not None:
            self._restore_harness_checkpoint(resume_checkpoint)
            with self.conversation_path.open("a", encoding="utf-8") as fh:
                fh.write(
                    f"\n=== 从第 {self._resume_completed_session + 1} 场断点续跑 "
                    f"{datetime.now().isoformat(timespec='seconds')} ===\n"
                )
        else:
            self.conversation_path.write_text(
                "\n".join(
                    [
                        f"FU-GM {self.target_sessions} 场完整战役长测 API 对话",
                        f"campaign_id: {self.campaign_id}",
                        f"started_at: {datetime.now().isoformat()}",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

    @staticmethod
    def _restore_checkpoint_campaign(
        checkpoint: CampaignRunCheckpoint,
        run_root: Path,
        stamp: str,
    ) -> Path:
        unique_stamp = f"{stamp}_{datetime.now().strftime('%f')}_{time.time_ns()}"
        restored_root = run_root / ".resume" / f"after_{checkpoint.completed_session:02d}_{unique_stamp}" / "campaigns"
        restored_campaign = restored_root / checkpoint.campaign_id
        checkpoint.restore_campaign_copy(run_root, restored_campaign)
        return restored_root

    def _restore_harness_checkpoint(self, checkpoint: CampaignRunCheckpoint) -> None:
        state = dict(checkpoint.state)
        self._resume_completed_session = int(checkpoint.completed_session)
        self.checkpoint_root.mkdir(parents=True, exist_ok=True)

        progress_lines = int(state.get("progress_line_count") or 0)
        if self.progress_path.exists():
            lines = self.progress_path.read_text(encoding="utf-8").splitlines(keepends=True)
            if len(lines) > progress_lines:
                abandoned = self.checkpoint_root / f"abandoned_progress_{self.stamp}.jsonl"
                abandoned.write_text("".join(lines[progress_lines:]), encoding="utf-8")
                self.progress_path.write_text("".join(lines[:progress_lines]), encoding="utf-8")
            self.calls = [
                json.loads(line)
                for line in lines[:progress_lines]
                if str(line).strip()
            ]

        conversation_size = int(state.get("conversation_size") or 0)
        if self.conversation_path.exists() and conversation_size >= 0:
            raw = self.conversation_path.read_bytes()
            if len(raw) > conversation_size:
                abandoned = self.checkpoint_root / f"abandoned_conversation_{self.stamp}.txt"
                abandoned.write_bytes(raw[conversation_size:])
                self.conversation_path.write_bytes(raw[:conversation_size])

        self.notes = [str(item) for item in state.get("notes", [])]
        self.errors = [str(item) for item in state.get("errors", [])]
        self.tool_events = list(state.get("tool_events") or [])
        self.session_reports = list(state.get("session_reports") or [])
        self.astrbot_bridge_results = list(state.get("astrbot_bridge_results") or [])
        self.heartbeat_results = list(state.get("heartbeat_results") or [])
        self.player_simulation_metrics = list(state.get("player_simulation_metrics") or [])
        self.level_up_results = list(state.get("level_up_results") or [])
        self._previous_session_summary = str(state.get("previous_session_summary") or "")
        self._adventure_started = bool(
            state.get("adventure_started", checkpoint.completed_session > 0)
        )
        self._upgrade_cursors.update(
            {str(key): int(value) for key, value in dict(state.get("upgrade_cursors") or {}).items()}
        )
        self.session_table_metrics = {
            int(key): value for key, value in dict(state.get("session_table_metrics") or {}).items()
        }
        self.session_scene_metrics = {
            int(key): value for key, value in dict(state.get("session_scene_metrics") or {}).items()
        }
        self.session_completion_results = {
            int(key): value for key, value in dict(state.get("session_completion_results") or {}).items()
        }
        self._in_progress_session_state = dict(state.get("in_progress_session") or {})
        self._pending_scene_transition = dict(
            self._in_progress_session_state.get("pending_scene_transition") or {}
        )
        self.session_progress_assessments = {
            int(key): SessionProgressAssessment(**value)
            for key, value in dict(state.get("session_progress_assessments") or {}).items()
        }

    def _checkpoint_state(self, *, campaign_backup: str) -> dict[str, Any]:
        return {
            "campaign_backup": campaign_backup,
            "progress_line_count": len(self.calls),
            "conversation_size": self.conversation_path.stat().st_size if self.conversation_path.exists() else 0,
            "notes": list(self.notes),
            "errors": list(self.errors),
            "tool_events": list(self.tool_events),
            "session_reports": list(self.session_reports),
            "astrbot_bridge_results": list(self.astrbot_bridge_results),
            "heartbeat_results": list(self.heartbeat_results),
            "session_table_metrics": dict(self.session_table_metrics),
            "session_scene_metrics": dict(self.session_scene_metrics),
            "player_simulation_metrics": list(self.player_simulation_metrics),
            "session_progress_assessments": {
                str(key): asdict(value) for key, value in self.session_progress_assessments.items()
            },
            "session_completion_results": dict(self.session_completion_results),
            "level_up_results": list(self.level_up_results),
            "previous_session_summary": self._previous_session_summary,
            "adventure_started": self._adventure_started,
            "upgrade_cursors": dict(self._upgrade_cursors),
            "in_progress_session": dict(self._in_progress_session_state),
        }

    def _write_campaign_checkpoint(
        self,
        completed_session: int,
        *,
        completed: bool = False,
        in_progress_state: dict[str, Any] | None = None,
    ) -> None:
        source_campaign = self.campaign_root / self.campaign_id
        if not source_campaign.is_dir():
            raise FileNotFoundError(f"无法为长测保存战役检查点：{source_campaign}")
        self._in_progress_session_state = dict(in_progress_state or {})
        if self._in_progress_session_state:
            active_session = int(self._in_progress_session_state.get("session_number") or completed_session + 1)
            cursor = int(
                self._in_progress_session_state.get("scripted_next_index")
                or self._in_progress_session_state.get("continuation_index")
                or 0
            )
            bundle_name = (
                f"turn_{active_session:02d}_{cursor:03d}_"
                f"{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"
            )
        else:
            bundle_name = f"session_{int(completed_session):02d}_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"
        temporary = self.checkpoint_root / f".{bundle_name}.tmp"
        final = self.checkpoint_root / bundle_name
        temporary.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source_campaign, temporary / "campaign")
        backup_relative = str((final / "campaign").relative_to(self.run_root))
        backup_digest = CampaignRunCheckpoint.directory_digest(temporary / "campaign")
        checkpoint_state = self._checkpoint_state(campaign_backup=backup_relative)
        checkpoint_state["campaign_backup_sha256"] = backup_digest
        checkpoint = CampaignRunCheckpoint(
            target_sessions=self.target_sessions,
            campaign_id=self.campaign_id,
            completed_session=int(completed_session),
            completed=bool(completed),
            state=checkpoint_state,
        )
        checkpoint.save(temporary / CampaignRunCheckpoint.FILENAME)
        temporary.rename(final)
        checkpoint.save(self.checkpoint_path)
        if self._in_progress_session_state:
            active_session = int(self._in_progress_session_state.get("session_number") or completed_session + 1)
            try:
                history_limit = int(os.getenv("FU_GM_LONG_TEST_CHECKPOINT_HISTORY", "3"))
            except ValueError:
                history_limit = 3
            history_limit = max(2, history_limit)
            bundles = sorted(
                (
                    path
                    for path in self.checkpoint_root.glob(f"turn_{active_session:02d}_*")
                    if path.is_dir()
                ),
                key=lambda path: path.stat().st_mtime_ns,
                reverse=True,
            )
            for old_bundle in bundles[history_limit:]:
                shutil.rmtree(old_bundle, ignore_errors=True)
        else:
            finished_session = int(completed_session)
            for old_bundle in self.checkpoint_root.glob(f"turn_{finished_session:02d}_*"):
                if old_bundle.is_dir():
                    shutil.rmtree(old_bundle, ignore_errors=True)

    def _service_retry_delay_seconds(
        self,
        *,
        label: str,
        method: str,
        route: str,
        payload: dict[str, Any],
        status: int,
        body: dict[str, Any],
        attempt: int,
    ) -> float | None:
        """Retry a provider outage only when the core GM committed no state."""

        if not self.semantic_llm or method.upper() != "POST" or route != "/v1/message/route":
            return None
        receipts = [
            dict(item)
            for item in list(body.get("tool_receipts") or [])
            if isinstance(item, dict)
        ]
        no_successful_commit = not any(
            bool(receipt.get("ok")) and bool(receipt.get("state_changed"))
            for receipt in receipts
        )
        provider_error = str(
            body.get("agent_error") or body.get("error") or ""
        ).strip()
        provider_failed_before_commit = bool(
            no_successful_commit
            and (
                (
                    bool(body.get("llm_unavailable"))
                    and str(body.get("llm_failure_kind") or "")
                    == "provider_unavailable"
                )
                or (
                    provider_error
                    and self._is_provider_unavailable_exception(
                        RuntimeError(provider_error)
                    )
                )
            )
        )
        if not provider_failed_before_commit:
            return None
        retry_limit = max(
            0,
            int(os.environ.get("FU_GM_LONG_TEST_PROVIDER_RETRY_LIMIT", "12")),
        )
        if attempt > retry_limit:
            return None
        base_delay = max(
            0.0,
            float(os.environ.get("FU_GM_LONG_TEST_PROVIDER_RETRY_BASE_SECONDS", "15")),
        )
        maximum_delay = max(
            base_delay,
            float(os.environ.get("FU_GM_LONG_TEST_PROVIDER_RETRY_MAX_SECONDS", "60")),
        )
        backoff = min(maximum_delay, base_delay * (1.5 ** max(0, attempt - 1)))
        # A circuit may open for longer than the ordinary retry backoff.  Its
        # advertised retry time is an earliest-safe instant, not a suggestion;
        # probing before it expires only produces an immediate local failure.
        circuit_wait = self._provider_circuit_retry_after_seconds(provider_error)
        return max(backoff, circuit_wait + 1.0 if circuit_wait > 0 else 0.0)

    def _provider_circuit_retry_after_seconds(self, error_text: str = "") -> float:
        waits: list[float] = []
        match = re.search(
            r"retry\s+after\s+([0-9]+(?:\.[0-9]+)?)s",
            str(error_text or ""),
            flags=re.IGNORECASE,
        )
        if match:
            waits.append(float(match.group(1)))
        try:
            app = self._runtime().app
            clients = [
                getattr(getattr(app, "gm_tool_agent", None), "client", None),
                getattr(getattr(app, "expressor", None), "client", None),
            ]
            for client in clients:
                payload_builder = getattr(client, "circuit_breaker_payload", None)
                if not callable(payload_builder):
                    continue
                payload = payload_builder()
                for circuit in payload.get("circuits", []):
                    if str(circuit.get("state") or "") == "open":
                        waits.append(float(circuit.get("retry_after_seconds") or 0.0))
        except (AttributeError, TypeError, ValueError):
            pass
        return max(waits, default=0.0)

    def invoke(self, label: str, method: str, route: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Stop strict semantic runs at the first unexpected HTTP failure.

        Continuing after a failed model-backed call makes later state look
        complete even though a player message was never adjudicated.
        """

        body = super().invoke(label, method, route, payload)
        record = self.calls[-1]
        if self.semantic_llm and int(record.get("status") or 0) >= 400:
            expressor = self._runtime().app.expressor
            private_diagnostics = {
                "expressor_error": str(getattr(expressor, "last_error", "") or ""),
                "scene_candidates": list(getattr(expressor, "last_scene_candidates", []) or []),
                "scene_candidate_diagnostics": list(
                    getattr(expressor, "last_scene_candidate_diagnostics", []) or []
                ),
            }
            record["private_failure_diagnostics"] = private_diagnostics
            self.notes.append(
                f"【{label}】私有失败诊断："
                + json.dumps(private_diagnostics, ensure_ascii=False)
            )
            error_text = str(body.get("error") or body.get("message") or "unknown error")
            raise RuntimeError(
                f"严格语义长测在【{label}】停止：HTTP {record.get('status')}，{error_text[:300]}"
            )
        if self.semantic_llm and bool(body.get("llm_invalid_output")):
            core_gm_diagnostics = dict(record.get("llm_diagnostics", {}).get("core_gm", {}) or {})
            npc_diagnostics = dict(record.get("llm_diagnostics", {}).get("npc_transaction", {}) or {})
            model_error = str(
                body.get("error")
                or npc_diagnostics.get("error")
                or core_gm_diagnostics.get("error")
                or "model returned invalid structured output"
            )
            record["strict_semantic_failure"] = {
                "kind": "model_invalid_output",
                "failure_kind": str(body.get("llm_failure_kind") or "invalid_output"),
                "core_gm": core_gm_diagnostics,
                "npc_transaction": npc_diagnostics,
            }
            raise RuntimeError(
                f"严格语义长测在【{label}】停止：模型结构化输出无效，{model_error[:300]}"
            )
        if self.semantic_llm and bool(body.get("llm_unavailable")):
            core_gm_diagnostics = dict(record.get("llm_diagnostics", {}).get("core_gm", {}) or {})
            expressor_diagnostics = dict(record.get("llm_diagnostics", {}).get("expressor", {}) or {})
            model_error = str(
                body.get("error")
                or core_gm_diagnostics.get("error")
                or expressor_diagnostics.get("error")
                or "model unavailable"
            )
            record["strict_semantic_failure"] = {
                "kind": "llm_unavailable",
                "core_gm": core_gm_diagnostics,
                "expressor": expressor_diagnostics,
            }
            raise RuntimeError(
                f"严格语义长测在【{label}】停止：模型链路不可用，{model_error[:300]}"
            )
        if self.semantic_llm and (
            str(body.get("route") or "").startswith("gm_agent_unavailable")
            or str(body.get("agent_error") or "").strip()
        ):
            # Production intentionally fails closed to silent when the GM
            # agent cannot finish. That is safe for players, but it is not
            # evidence that a semantic routing expectation passed. After the
            # private retry budget is exhausted, strict longruns must stop
            # rather than count the fallback silence as model correctness.
            model_error = str(
                body.get("agent_error")
                or body.get("error")
                or "GM tool agent unavailable"
            ).strip()
            record["strict_semantic_failure"] = {
                "kind": "gm_agent_unavailable",
                "route": str(body.get("route") or ""),
                "agent_error": model_error,
            }
            raise RuntimeError(
                f"严格语义长测在【{label}】停止：GM智能体链路不可用，{model_error[:300]}"
            )
        if self.semantic_llm:
            self._assert_npc_route_integrity(label, record)
            self._assert_npc_movement_response_integrity(label, record)
            self._assert_local_guide_response_integrity(label, record)
            self._assert_action_fact_integrity(label, record)
        if self._quality_gate_enabled:
            self._assert_incremental_conversation_quality(label)
        return body

    @staticmethod
    def _assert_action_fact_integrity(
        label: str,
        record: dict[str, Any],
    ) -> None:
        """Reject lossy route summaries before they can poison a campaign."""

        body = record.get("body")
        if not isinstance(body, dict):
            return
        decision = body.get("decision")
        if not isinstance(decision, dict) or not bool(decision.get("performed_action")):
            return
        source = " ".join(str(record.get("message") or "").split()).strip()
        if not source:
            return

        if bool(decision.get("action_semantics_required")) and not bool(
            decision.get("action_semantics_reviewed")
        ):
            raise RuntimeError(
                f"严格语义长测在【{label}】停止：高风险行动未完成事实账本复核。"
            )

        facts = [
            item
            for item in (decision.get("action_facts") or [])
            if isinstance(item, dict)
        ]
        for fact in facts:
            evidence = " ".join(str(fact.get("evidence") or "").split()).strip()
            if not evidence or evidence not in source:
                raise RuntimeError(
                    f"严格语义长测在【{label}】停止：行动事实没有玩家原文证据。"
                )
            if bool(fact.get("can_commit_world_fact")) and (
                str(fact.get("stage") or "") != "completed"
                or bool(fact.get("requires_check"))
                or bool(fact.get("requires_external_acceptance"))
            ):
                raise RuntimeError(
                    f"严格语义长测在【{label}】停止：待检定或待接受动作被标成既成事实。"
                )

        if str(decision.get("object_transfer_status") or "none") == "completed":
            if not any(
                str(fact.get("kind") or "") == "transfer"
                and bool(fact.get("can_commit_world_fact"))
                for fact in facts
            ):
                raise RuntimeError(
                    f"严格语义长测在【{label}】停止：物件交接没有完成证据却被记为已完成。"
                )

        summary = " ".join(str(decision.get("action_summary") or "").split()).strip()
        if summary and not facts and summary != source and summary not in source:
            raise RuntimeError(
                f"严格语义长测在【{label}】停止：路由仍使用了无法回指玩家原文的行动摘要。"
            )

    @staticmethod
    def _assert_npc_movement_response_integrity(
        label: str,
        record: dict[str, Any],
    ) -> None:
        """Reject an NPC treating its own movement choice as unknown lore."""

        body = record.get("body")
        if not isinstance(body, dict):
            return
        decision = body.get("decision")
        if not isinstance(decision, dict):
            return
        if (
            str(decision.get("movement_scope") or "") != "cross_scene"
            or not list(decision.get("movement_companions") or [])
        ):
            return
        reply = re.sub(
            r"【[^】]+】\s*\d+\s*/\s*\d+[^\n]*",
            "",
            str(record.get("reply") or body.get("reply") or ""),
        ).strip()
        normalized = re.sub(r"[\s，,。！？!?]", "", reply)
        if normalized in {"不知道", "我不知道", "我也不知道", "这件事我不知道"}:
            raise RuntimeError(
                f"严格语义长测在【{label}】停止：NPC 把自己的同行或移动选择回答成了“不知道”。"
            )

    @staticmethod
    def _assert_local_guide_response_integrity(
        label: str,
        record: dict[str, Any],
    ) -> None:
        """A local guide must answer ordinary geography within their role."""

        body = record.get("body")
        if not isinstance(body, dict):
            return
        decision = body.get("decision")
        if not isinstance(decision, dict) or not bool(decision.get("npc_reply_required")):
            return
        target = str(decision.get("npc_target") or "")
        message = str(record.get("message") or "")
        if not re.search(r"(?:巡守|守巡|向导|带路|领路)", target):
            return
        if not (
            re.search(r"(?:旧路|路线|路径|前方|附近|岔路|遮蔽处|藏身处|地标)", message)
            and re.search(r"(?:哪里|哪儿|哪处|怎么走|如何走|最近|方向)", message)
        ):
            return
        reply = re.sub(
            r"【[^】]+】\s*\d+\s*/\s*\d+[^\n]*",
            "",
            str(record.get("reply") or body.get("reply") or ""),
        ).strip()
        normalized = re.sub(r"[\s，,。！？!?]", "", reply)
        if normalized in {"不知道", "我不知道", "我也不知道", "这件事我不知道"}:
            raise RuntimeError(
                f"严格语义长测在【{label}】停止：本地带路 NPC 对职责内的普通路线问题只回答了“不知道”。"
            )

    def _assert_npc_route_integrity(
        self,
        label: str,
        record: dict[str, Any],
    ) -> None:
        """Fail when a routed NPC question is answered or remembered by someone else."""

        span = record.get("pipeline_span")
        if not isinstance(span, dict):
            return
        dialogue = span.get("npc_dialogue")
        if not isinstance(dialogue, dict):
            return
        pc_targets = [
            str(item or "").strip()
            for item in dialogue.get("player_character_targets", [])
            if str(item or "").strip()
        ]
        if pc_targets:
            raise RuntimeError(
                f"严格语义长测在【{label}】停止：GM 代替玩家角色开口："
                + "、".join(pc_targets)
            )
        actual_targets = {
            str(item or "").strip()
            for item in dialogue.get("actual_targets", [])
            if str(item or "").strip()
        }
        memory_targets = {
            str(item or "").strip()
            for item in dialogue.get("memory_targets", [])
            if str(item or "").strip()
        }
        if memory_targets and memory_targets != actual_targets:
            raise RuntimeError(
                f"严格语义长测在【{label}】停止：NPC 开口者 {sorted(actual_targets)} "
                f"与记忆写入者 {sorted(memory_targets)} 不一致。"
            )
        routed_target = str(dialogue.get("routed_target") or "").strip()
        if not routed_target or not actual_targets:
            return
        app = self._runtime().app
        routed_labels = [
            item.strip()
            for item in re.split(r"\s*(?:；|;|、|，|,|\|)\s*", routed_target)
            if item.strip()
        ]
        expected_targets = {
            app.world_state.resolve_npc_name(item)
            or item
            for item in routed_labels
        }
        if expected_targets.isdisjoint(actual_targets):
            raise RuntimeError(
                f"严格语义长测在【{label}】停止：语义路由指定 {sorted(expected_targets)}，"
                f"实际却由 {sorted(actual_targets)} 回答。"
            )

    def _assert_incremental_conversation_quality(self, label: str) -> None:
        """Stop immediately on structural failures that poison later evidence."""

        if not self.semantic_llm:
            return
        report = self.conversation_quality_auditor.audit(self.calls)
        failures: list[str] = []
        if report.backstage_instruction_leaks:
            failures.append(f"后台指令泄露 {report.backstage_instruction_leaks} 次")
        if report.embedded_prior_gm_replays:
            failures.append(f"旧GM回复嵌入新结算 {report.embedded_prior_gm_replays} 次")
        if report.fulfilled_promise_reopens:
            failures.append(f"NPC 已兑现的承诺重新索价 {report.fulfilled_promise_reopens} 次")
        if report.npc_commitment_violations:
            failures.append(f"NPC 公开承诺未兑现 {report.npc_commitment_violations} 次")
        # Repeated player approaches are a FU-PL/table-quality finding, not an
        # authoritative-state failure. Keep counting them in the final report,
        # but do not discard an otherwise valid multi-session run mid-session.
        if report.contradictory_check_responses:
            failures.append(f"成功/失败检定叙事矛盾 {report.contradictory_check_responses} 次")
        if failures:
            raise RuntimeError(
                f"严格语义长测在【{label}】触发增量质量门禁：" + "；".join(failures)
            )

    def route_table_message(
        self,
        label: str,
        speaker: str,
        message: str,
        *,
        expected_target: str,
        expected_send_reply: bool,
        directed_at_gm: bool = False,
        tolerate_route_mismatch: bool = False,
    ) -> dict[str, Any]:
        errors = getattr(self, "errors", None)
        if errors is None:
            errors = []
            self.errors = errors
        error_count_before = len(errors)
        body = super().route_table_message(
            label,
            speaker,
            message,
            expected_target=expected_target,
            expected_send_reply=expected_send_reply,
            directed_at_gm=directed_at_gm,
        )
        accepted_silent_commit = bool(
            expected_target == "fu_gm"
            and expected_send_reply
            and self._is_valid_silent_commit(body)
        )
        effective_target = "silent" if accepted_silent_commit else expected_target
        effective_send_reply = False if accepted_silent_commit else expected_send_reply
        if accepted_silent_commit:
            appended = self.errors[error_count_before:]
            mismatch_prefixes = (
                f"{label} routing target=",
                f"{label} send_reply=",
            )
            self.errors[error_count_before:] = [
                item
                for item in appended
                if not item.startswith(mismatch_prefixes)
            ]
        if self.calls:
            self.calls[-1]["expected_target"] = effective_target
            self.calls[-1]["expected_send_reply"] = bool(effective_send_reply)
            self.calls[-1]["accepted_silent_commit"] = accepted_silent_commit
        if self.semantic_llm and (
            str(body.get("target") or "") != effective_target
            or bool(body.get("send_reply")) != effective_send_reply
        ) and not tolerate_route_mismatch and getattr(
            self,
            "fail_fast_route_mismatch",
            True,
        ):
            raise RuntimeError(
                f"严格语义长测在【{label}】停止：玩家消息路由为"
                f"{body.get('target')!r}/send_reply={bool(body.get('send_reply'))}，"
                f"预期 {effective_target!r}/send_reply={effective_send_reply}。"
            )
        return body

    @staticmethod
    def _is_valid_silent_commit(body: dict[str, Any]) -> bool:
        if (
            str(body.get("target") or "") != "silent"
            or bool(body.get("send_reply"))
            or str(body.get("reply") or "").strip()
        ):
            return False
        return any(
            bool(receipt.get("ok"))
            and bool(receipt.get("state_changed"))
            and bool(dict(receipt.get("result") or {}).get("silent_commit_allowed"))
            for receipt in body.get("tool_receipts", [])
            if isinstance(receipt, dict)
        )

    def route_session_zero_contribution(
        self,
        label: str,
        speaker: str,
        message: str,
        *,
        expected_state_change: bool = True,
    ) -> dict[str, Any]:
        """Exercise the production semantic/tool path for setup mutations."""

        body = self.invoke(
            label,
            "POST",
            "/v1/message/route",
            {**self.common, "speaker": speaker, "message": message},
        )
        if self.calls and not expected_state_change:
            self.calls[-1]["expected_target"] = "silent"
            self.calls[-1]["expected_send_reply"] = False
        if not self.semantic_llm:
            return body
        target = str(body.get("target") or "")
        if target not in {"fu_gm", "silent"}:
            raise RuntimeError(
                f"严格语义长测在【{label}】停止：第零章贡献被路由到 {target!r}。"
            )
        receipts = [
            dict(item)
            for item in (body.get("tool_receipts") or [])
            if isinstance(item, dict)
        ]
        successful_writes = [
            item
            for item in receipts
            if bool(item.get("ok")) and bool(item.get("state_changed"))
        ]
        if expected_state_change and not successful_writes:
            raise RuntimeError(
                f"严格语义长测在【{label}】停止：明确的第零章贡献没有产生成功状态工具回执；"
                f"agent_error={body.get('agent_error')!r}。"
            )
        if not expected_state_change:
            if target != "silent" or bool(body.get("send_reply")):
                raise RuntimeError(
                    f"严格语义长测在【{label}】停止：未获确认的玩家提案应保持静默，"
                    f"实际为 {target!r}/send_reply={bool(body.get('send_reply'))}。"
                )
            if successful_writes:
                raise RuntimeError(
                    f"严格语义长测在【{label}】停止：未获确认的玩家提案被提前写入权威状态。"
                )
        return body

    @staticmethod
    def _records_noncombat_resolution_preference(world: Any) -> bool:
        values = (
            getattr(world, "consensus_notes", []),
            getattr(world, "core_themes", []),
            getattr(world, "playstyle_themes", []),
        )
        text = " ".join(
            " ".join(f"{key} {value}" for key, value in item.items())
            if isinstance(item, dict)
            else " ".join(str(value) for value in item)
            if isinstance(item, (list, tuple, set))
            else str(item or "")
            for item in values
        )
        return any(
            phrase in text
            for phrase in (
                "不靠战斗",
                "不以战斗",
                "不依靠战斗",
                "不依赖战斗",
                "非战斗",
                "非暴力",
            )
        )

    def _session_zero_world_turns(self) -> list[tuple[str, str]]:
        """Use incremental table talk instead of an all-knowing setup dump."""

        return [
            (
                "阿凛",
                "我希望整体有史诗奇幻的希望感，但别一上来就是拯救世界。先从边境上一件会影响普通人的小事开始，真相到中期再慢慢掀开。",
            ),
            (
                "时雨",
                "先说安全边界。界限：不详细描写性暴力、酷刑和现实仇恨煽动。帷幕：儿童遇险、身体病变和亲密内容都淡出处理。",
            ),
            (
                "白河",
                "我先丢一个还没定的地图想法：大陆叫白钟大陆，西边有山，中央有内海，南边是海岸和驿站，东南是群岛。大家觉得这个轮廓合适吗？",
            ),
            (
                "南星",
                "我赞成白河刚才的轮廓，就按白钟大陆来：西侧叫鸦羽山脉，中央是镜线内海，南岸放雾潮海岸和白花碑驿站，东南是潮鸢群岛。它就是普通的类地球大陆，不用异形世界。",
            ),
            (
                "阿凛",
                "魔法和科技可以并存。灵魂晶炉驱动车辆、工坊和财团机器，古老的御魂术与元素仪式则负责安抚灵魂之河。",
            ),
            (
                "阿凛",
                "我贡献钟鸣公国，放在镜线内海北岸。正午大钟能安抚灵魂，也让贵族控制谁的哀悼能被听见。历史事件是碎月坠落当夜，全大陆的钟都慢了一拍。奥秘是姐姐的名字为何刻在白花风铃内侧，却无人记得她死亡。威胁是辉钢财团正把灰晶病患者的记忆当成可买卖燃料。",
            ),
            (
                "南星",
                "国家这一项我先跳过。我补潮鸢群岛这个地区：飞翼船追着季风迁徙。三十年前碎月坠落，赤羽旧王都一夜消失；我想查的奥秘是每年归潮祭后都会少一座岛，所有人的公开记忆还会跟着改写。苍白司教团则把灰晶病包装成灵魂升格，这是我贡献的威胁。",
            ),
            (
                "白河",
                "国家我也先跳过。我补西北的第七采掘城，它受辉钢财团控制。记忆炉第一次启动时吞掉了一整条矿道工人的姓名；紧急停机协议为何只回应赤羽遗民的歌，是我想追的奥秘。财团正在向雾潮海岸扩张，这是眼下的威胁。监察官艾蕾娜相信集中管理记忆能阻止世界再次遗忘灾难。",
            ),
            (
                "时雨",
                "我的国家是东部海岸的奥涅里亚，灯塔舰队维持贸易，王室却和港口行会互不信任。老国王病倒后，摄政王把王室海图抵押给财团；灯塔为什么能照见已经消失的岛，是我想留下的奥秘。若港口行会与王室决裂，财团就会拿走失踪群岛调查权。",
            ),
            (
                "澄砚",
                "我贡献东南内陆的沉默森林，以及森林南侧的树誓村社。村社不认王权，只和奥灵立约。碎月之夜后，森林第一次拒绝所有人类祈祷；树皮写下的名字里为何有人仍活着，是这里的奥秘。苍白司教团想把森林变成灰晶病圣地。",
            ),
            (
                "白河",
                "小队我先提个还没定的方向：大家是在白花碑驿站临时结成的守护者，护送失忆旅人和碎月遗物去钟鸣公国。你们觉得合适吗？",
            ),
            (
                "时雨",
                "我希望第一章至少有一场冲突不靠战斗解决，要靠证据、承诺和情感去改变别人的决定。",
            ),
            (
                "澄砚",
                "我赞成白河的小队方向。我们就是在白花碑驿站临时结成的守护者，护送失忆旅人和碎月遗物前往钟鸣公国；如果只抢线索、不保护普通人，奥灵会沉默。",
            ),
        ]

    def _assert_world_contribution_complete(self, index: int) -> None:
        runtime = self._runtime()
        world = runtime.app.world_state.world_profile
        map_names = set(runtime.app.world_state.map_locations)

        def contains(values: Any, token: str) -> bool:
            if isinstance(values, dict):
                text = " ".join(f"{key} {value}" for key, value in values.items())
            elif isinstance(values, (list, tuple, set)):
                text = " ".join(str(item) for item in values)
            else:
                text = str(values or "")
            return token in text

        required: dict[int, list[tuple[str, bool]]] = {
            1: [
                ("基调", bool(world.tone_preferences or world.playstyle_themes or world.core_themes)),
            ],
            2: [
                ("界限", bool(world.safety_lines)),
                ("帷幕", bool(world.safety_veils)),
            ],
            3: [
                ("未确认地图提案没有落档", world.continent_name != "白钟大陆"),
            ],
            4: [
                ("大陆名", world.continent_name == "白钟大陆"),
                ("世界形态", bool(world.world_shape)),
                ("鸦羽山脉地图点", "鸦羽山脉" in map_names),
                ("镜线内海地图点", "镜线内海" in map_names),
                ("白花碑驿站地图点", "白花碑驿站" in map_names),
                ("潮鸢群岛地图点", "潮鸢群岛" in map_names),
            ],
            5: [("魔法与科技", bool(world.magic_tech_role))],
            6: [
                ("钟鸣公国", "钟鸣公国" in world.kingdoms),
                ("碎月历史", contains(world.historical_events, "碎月")),
                ("姐姐名字奥秘", contains(world.mysteries, "姐姐")),
                ("辉钢财团威胁", contains(world.world_threats, "辉钢财团")),
            ],
            7: [
                ("潮鸢群岛", "潮鸢群岛" in map_names),
                ("赤羽旧王都历史", contains(world.historical_events, "赤羽旧王都")),
                ("归潮祭奥秘", contains(world.mysteries, "归潮祭")),
                ("苍白司教团威胁", contains(world.world_threats, "苍白司教团")),
            ],
            8: [
                ("第七采掘城", "第七采掘城" in map_names or "第七采掘城" in world.major_locations),
                ("记忆炉历史", contains(world.historical_events, "记忆炉")),
                ("停机协议奥秘", contains(world.mysteries, "停机协议")),
                ("艾蕾娜反派种子", contains(world.villain_seeds, "艾蕾娜")),
            ],
            9: [
                ("奥涅里亚", any("奥涅里亚" in str(name) for name in world.kingdoms)),
                ("王室海图历史", contains(world.historical_events, "王室海图")),
                ("灯塔奥秘", contains(world.mysteries, "灯塔")),
                ("港口行会威胁", contains(world.world_threats, "港口行会")),
            ],
            10: [
                ("沉默森林", "沉默森林" in map_names or "沉默森林" in world.major_locations),
                ("树誓村社", any("树誓村社" in name for name in (*world.kingdoms, *world.factions))),
                ("人类祈祷历史", contains(world.historical_events, "人类") and contains(world.historical_events, "祈祷")),
                (
                    "活人名字奥秘",
                    contains(world.mysteries, "树皮")
                    and contains(world.mysteries, "活"),
                ),
                ("沉默森林威胁", contains(world.world_threats, "沉默森林")),
            ],
            11: [
                ("小队提案未提前写入", not contains(world.group_concept, "临时") and not world.pending_proposals),
            ],
            12: [("非战斗解决偏好", self._records_noncombat_resolution_preference(world))],
            13: [
                ("临时守护者小队", contains(world.group_concept, "临时") and contains(world.group_concept, "守护")),
                ("白花碑驿站起点", contains(world.group_concept, "白花碑驿站") or contains(world.starting_region, "白花碑驿站")),
            ],
        }
        missing = [label for label, ok in required.get(index, []) if not ok]
        if missing:
            raise RuntimeError(
                f"第零章世界共创第 {index} 条只完成了部分写入，缺少：{'、'.join(missing)}。"
            )

    def _assert_character_setup_complete(self) -> None:
        runtime = self._runtime()
        world = runtime.app.world_state.world_profile
        missing: list[str] = []
        for player, hero_name in zip(
            ("阿凛", "南星", "白河", "时雨", "澄砚"),
            self.pc_names,
        ):
            key, draft = self.service.gm_session_zero_tools._resolve_draft(
                world.hero_drafts,
                player,
            )
            if draft is None:
                missing.append(f"{player}无草稿")
                continue
            validation = runtime.app.validate_hero_draft(key)
            if draft.hero_name != hero_name:
                missing.append(f"{player}角色名不是{hero_name}")
            if not validation.ready:
                missing.append(f"{hero_name}不完整")
            if not draft.confirmed:
                missing.append(f"{hero_name}未确认")
        if missing:
            raise RuntimeError("第零章角色创建未完整收口：" + "、".join(missing))

    def _run_setup_contributions(
        self,
        *,
        world_completed_index: int = 0,
        character_completed_index: int = 0,
    ) -> None:
        world_turns = self._session_zero_world_turns()
        character_turns = self._session_zero_character_turns()
        discussion_only_turns = {3, 11}
        for index, (speaker, message) in enumerate(world_turns, start=1):
            if index <= world_completed_index:
                continue
            self.route_session_zero_contribution(
                f"第零章世界共创 {index:02d} {speaker}",
                speaker,
                message,
                expected_state_change=index not in discussion_only_turns,
            )
            self._assert_world_contribution_complete(index)
            self._write_campaign_checkpoint(
                0,
                in_progress_state={
                    "phase": "session_zero",
                    "world_completed_index": index,
                    "character_completed_index": 0,
                },
            )
        for index, (speaker, message) in enumerate(character_turns, start=1):
            if index <= character_completed_index:
                continue
            self.route_session_zero_contribution(
                f"第零章角色创建 {index:02d} {speaker}",
                speaker,
                message,
            )
            self._write_campaign_checkpoint(
                0,
                in_progress_state={
                    "phase": "session_zero",
                    "world_completed_index": len(world_turns),
                    "character_completed_index": index,
                },
            )
        self._assert_character_setup_complete()

    def _run_rules_only_setup(self) -> None:
        """Seed the shared rules fixture through typed domain tools.

        Rules-only mode must not ask a disabled language model or a legacy
        keyword parser to understand Session 0 prose. It still uses the same
        validated write handlers as production, so field shape, character
        creation, equipment, skills and confirmation gates remain under test.
        """

        tools = self.service.gm_session_zero_tools

        def context(speaker: str, evidence: str) -> GMToolExecutionContext:
            return GMToolExecutionContext(
                campaign_id=self.campaign_id,
                session_id=self.session_id,
                channel_id=self.channel_id,
                speaker=speaker,
                gate_status="session_zero",
                directly_addressed=True,
                metadata={
                    "current_message": evidence,
                    "recent_public_context": "离线规则机械演练的结构化第零章夹具。",
                },
            )

        def require(receipt: Any, label: str) -> None:
            if not bool(getattr(receipt, "ok", False)):
                raise RuntimeError(
                    f"离线规则夹具在【{label}】被类型化工具拒绝："
                    f"{getattr(receipt, 'error_code', '')} "
                    f"{getattr(receipt, 'message', '')}"
                )

        world_updates: list[tuple[str, dict[str, object]]] = [
            (
                "阿凛",
                {
                    "continent_name": "白钟大陆",
                    "world_shape": "普通类地球大陆",
                    "magic_tech_role": "灵魂晶炉驱动机器，御魂术与元素仪式安抚灵魂之河。",
                    "kingdoms": {
                        "钟鸣公国": "位于镜线内海北岸，正午大钟能安抚灵魂。",
                    },
                    "historical_events": ["碎月坠落当夜，白钟大陆所有钟慢了一拍。"],
                    "mysteries": ["姐姐的名字为何刻在白花风铃内侧，却无人记得她死亡。"],
                    "world_threats": ["辉钢财团正在把灰晶病患者的记忆作为可买卖燃料。"],
                    "tone_preferences": ["史诗奇幻的希望感，从边境驿站的选择开始。"],
                    "starting_region": "白花碑驿站",
                    "map_locations": [
                        {"name": "鸦羽山脉", "feature_type": "mountain_range", "position_hint": "west", "terrain": "mountains", "draw_icon": False},
                        {"name": "镜线内海", "feature_type": "inland_sea", "position_hint": "center", "terrain": "water", "draw_icon": False},
                        {"name": "雾潮海岸", "feature_type": "coast", "position_hint": "south", "terrain": "coast", "draw_icon": False},
                        {"name": "白花碑驿站", "feature_type": "settlement", "position_hint": "south", "terrain": "coast", "draw_icon": True},
                        {"name": "潮鸢群岛", "feature_type": "archipelago", "position_hint": "southeast", "terrain": "islands", "draw_icon": False},
                    ],
                },
            ),
            (
                "南星",
                {
                    "major_locations": {"潮鸢群岛": "追随季风移动飞翼船的群岛地区。"},
                    "historical_events": ["三十年前碎月坠落，赤羽旧王都一夜消失。"],
                    "mysteries": ["每年归潮祭后都会少一座岛，公开记忆会自动改写。"],
                    "world_threats": ["苍白司教团把灰晶病包装成灵魂升格。"],
                },
            ),
            (
                "白河",
                {
                    "major_locations": {"第七采掘城": "辉钢财团控制的记忆燃料采掘城。"},
                    "factions": {"辉钢财团": "控制第七采掘城并收购灰晶病患者记忆。"},
                    "historical_events": ["记忆炉第一次启动时吞掉了一整条矿道工人的姓名。"],
                    "mysteries": ["第七采掘城的紧急停机协议为何只回应赤羽遗民的歌。"],
                    "world_threats": ["辉钢财团正向雾潮海岸扩张。"],
                    "group_concept": "临时守护者：护送失忆旅人与碎月遗物前往钟鸣公国。",
                    "villain_seeds": ["监察官艾蕾娜相信集中管理记忆才能避免世界再次遗忘灾难。"],
                    "map_locations": [
                        {"name": "第七采掘城", "feature_type": "settlement", "position_hint": "northwest", "terrain": "badlands", "faction": "辉钢财团", "draw_icon": True},
                    ],
                },
            ),
            (
                "时雨",
                {
                    "kingdoms": {"奥涅里亚": "灯塔舰队维持海上贸易，王室与港口行会互不信任。"},
                    "historical_events": ["老国王病倒后，摄政王把王室海图抵押给辉钢财团。"],
                    "mysteries": ["奥涅里亚的灯塔为什么能照见已经消失的岛。"],
                    "world_threats": ["港口行会与王室决裂会让财团取得失踪群岛调查权。"],
                    "map_locations": [
                        {"name": "奥涅里亚", "feature_type": "country", "position_hint": "east", "terrain": "coast", "draw_icon": True},
                    ],
                },
            ),
            (
                "澄砚",
                {
                    "major_locations": {"沉默森林": "奥灵会在碎月之夜把未说出口的名字写到树皮上。"},
                    "kingdoms": {"树誓村社": "不承认王权、只与奥灵立约的村社共同体。"},
                    "historical_events": ["碎月之夜后，沉默森林第一次拒绝所有人类祈祷。"],
                    "mysteries": ["沉默森林树皮写下的名字里，有些人仍然活着。"],
                    "world_threats": ["苍白司教团想把沉默森林变成灰晶病圣地。"],
                    "map_locations": [
                        {"name": "沉默森林", "feature_type": "forest", "position_hint": "southeast", "terrain": "forest", "draw_icon": False},
                        {"name": "树誓村社", "feature_type": "country", "position_hint": "southeast", "relative_to": "沉默森林", "relative_position": "south", "terrain": "forest", "draw_icon": True},
                    ],
                },
            ),
            (
                "时雨",
                {
                    "playstyle_themes": ["第一章包含一场依靠证据、承诺和情感解决的非战斗冲突。"],
                },
            ),
            (
                "澄砚",
                {
                    "selected_first_act_summary": (
                        "白花碑驿站的迟响：说服白花守望会给出旧路，保护失忆旅人，"
                        "发现财团收购记忆的第一条证据。"
                    ),
                },
            ),
        ]
        for index, (speaker, updates) in enumerate(world_updates, start=1):
            evidence = f"离线规则夹具：{speaker}确认第零章贡献{index}。"
            receipt = tools.commit_update(
                context(speaker, evidence),
                {"updates": updates, "evidence": evidence},
            )
            require(receipt, f"世界贡献{index}")

        for kind, content in (
            ("line", "不详细描写性暴力、酷刑、现实仇恨煽动"),
            ("veil", "儿童遇险、身体病变、亲密内容淡出处理"),
        ):
            evidence = f"离线规则夹具安全声明：{content}。"
            receipt = tools.record_safety_boundary(
                context("阿凛", evidence),
                {"kind": kind, "content": content, "evidence": evidence},
            )
            require(receipt, f"安全边界{kind}")

        hero_patches: list[tuple[str, dict[str, object]]] = [
            ("阿凛", {"hero_name": "伊莉雅", "identity": "赤羽遗民的盾誓骑士", "theme": "责任", "origin": "白花碑驿站", "classes": {"守护者": 3, "元素使": 2}, "attributes": {"敏捷": 8, "洞察": 8, "力量": 10, "意志": 6}, "skills": {"保镖": 1, "防御精通": 1, "挺身守护": 1, "元素魔法": 1, "元素系仪式": 1}, "spells": ["元素幕障"], "equipment": ["钢匕首", "青铜盾", "旅行装束"], "bonds": ["赛璃：信赖", "洛岚：钦佩"], "notes": ["姐姐的名字刻在白花风铃内侧。"]}),
            ("南星", {"hero_name": "赛璃", "identity": "钟鸣公国的御魂医师", "theme": "希望", "origin": "钟鸣公国", "classes": {"御魂使": 3, "旅人": 2}, "attributes": {"敏捷": 6, "洞察": 10, "力量": 8, "意志": 8}, "skills": {"灵魂魔法": 2, "御魂系仪式": 1, "见多识广": 1, "充足补给": 1}, "spells": ["治愈术", "屏障"], "equipment": ["法杖", "旅行装束"], "bonds": ["伊莉雅：信赖", "洛岚：喜爱"]}),
            ("白河", {"hero_name": "洛岚", "identity": "辉钢财团出逃的魔导工匠", "theme": "赎罪", "origin": "第七采掘城", "classes": {"造物使": 3, "武器大师": 2}, "attributes": {"敏捷": 8, "洞察": 10, "力量": 8, "意志": 6}, "skills": {"便携装置": 1, "秘密配方": 1, "先见之明": 1, "碎骨": 1, "破防打击": 1}, "skill_options": {"便携装置": ["魔导装置"]}, "equipment": ["铁锤", "旅行装束"], "bonds": ["伊莉雅：钦佩", "赛璃：信赖"]}),
            ("时雨", {"hero_name": "艾薇娅", "identity": "奥涅里亚的灯塔外交官", "theme": "妥协", "origin": "奥涅里亚王都", "classes": {"游说家": 2, "熵术士": 2, "旅人": 1}, "attributes": {"敏捷": 8, "洞察": 8, "力量": 6, "意志": 10}, "skills": {"谴责": 1, "鼓舞": 1, "熵系魔法": 1, "熵系仪式": 1, "见多识广": 1}, "spells": ["加速术"], "equipment": ["法杖", "旅行装束"], "bonds": ["伊莉雅：信赖", "苍祈：猜忌"]}),
            ("澄砚", {"hero_name": "苍祈", "identity": "沉默森林的失约奥灵使", "theme": "亏欠", "origin": "树誓村社", "classes": {"奥灵使": 2, "拟兽使": 2, "暗刃骑士": 1}, "attributes": {"敏捷": 6, "洞察": 10, "力量": 8, "意志": 8}, "skills": {"契约与召唤": 1, "奥灵系仪式": 1, "野性之语": 1, "拟兽系仪式": 1, "暗影击": 1}, "skill_options": {"拟兽系仪式": ["洞察+意志"]}, "bound_arcana": ["魔典"], "equipment": ["魔典", "旅行装束"], "bonds": ["洛岚：猜忌", "赛璃：喜爱"]}),
        ]
        for index, (speaker, patch) in enumerate(hero_patches, start=1):
            evidence = f"离线规则夹具：{speaker}提交并确认角色。"
            receipt = tools.update_hero_draft(
                context(speaker, evidence),
                {"subject": speaker, "patch": patch, "evidence": evidence},
            )
            require(receipt, f"角色草稿{index}")
            receipt = tools.confirm_hero_draft(
                context(speaker, evidence),
                {"subject": speaker, "evidence": evidence},
            )
            require(receipt, f"角色确认{index}")

        self._assert_character_setup_complete()
        # Rules-only setup is a final, already-confirmed fixture. Intermediate
        # semantic states such as "proposal not yet committed" are exercised by
        # the live message flow and must not be asserted after final consensus.
        for index in (1, 2, 4, 5, 6, 7, 8, 9, 10, 12, 13):
            self._assert_world_contribution_complete(index)
        self._record_tool_event(
            "类型化第零章夹具",
            "离线规则机械演练",
            "世界、安全边界与五张角色卡均通过类型化工具写入和确认，不经过自然语言解析。",
            {"world_updates": len(world_updates), "heroes": len(hero_patches)},
        )

    def _prepare_campaign_after_setup(self) -> list[CampaignSessionSpec]:
        self._register_test_chapter_package()
        runtime = self._runtime()
        configure_kwargs = self._pacing_configure_kwargs()
        runtime.app.campaign_pacing_manager.configure(**configure_kwargs)
        self._record_tool_event(
            "战役节奏控制",
            f"{self.target_sessions}场战役初始化",
            f"将本次执行 {self.target_sessions} 场，并按 {self.campaign_profile_sessions} 场完整战役的"
            f"{self.length_profile} 档位评估，每场约四小时节奏。",
            runtime.app.campaign_pacing_manager.audit_payload(),
        )
        self._wait_for_async_map_if_any()
        campaign_specs = self._campaign_sessions()
        if not campaign_specs:
            self.errors.append("战役脚本没有生成任何场次。")
            return []
        if not self._ensure_adventure_started(campaign_specs[0]):
            return []
        self._wait_for_async_map_if_any()
        return campaign_specs

    def run(self) -> int:
        try:
            self._main_flow()
            report = (
                self._build_setup_only_report()
                if self.setup_only
                else (
                    self._build_report()
                    if self.semantic_llm
                    else self._build_rules_only_report()
                )
            )
            self._write_report(report)
            print(f"RUN_ROOT={self.run_root}")
            print(f"REPORT_JSON={self.report_json_path}")
            print(f"REPORT_TXT={self.report_txt_path}")
            print(f"CONVERSATION_TXT={self.conversation_path}")
            return 0 if report["ok"] else 1
        except Exception as exc:  # pragma: no cover - debugging harness
            self.errors.append(f"未捕获异常：{exc}")
            self.notes.append(traceback.format_exc())
            previous_quality_gate = self._quality_gate_enabled
            self._quality_gate_enabled = False
            try:
                report = (
                    self._build_setup_only_report()
                    if self.setup_only
                    else (
                        self._build_report()
                        if self.semantic_llm
                        else self._build_rules_only_report()
                    )
                )
            finally:
                self._quality_gate_enabled = previous_quality_gate
            self._write_report(report)
            print(traceback.format_exc())
            print(f"RUN_ROOT={self.run_root}")
            print(f"REPORT_JSON={self.report_json_path}")
            return 1

    def _rules_only_context(
        self,
        speaker: str,
        message: str,
        *,
        system_beat: bool = False,
    ) -> GMToolExecutionContext:
        gate = self.service.session_gates.get(
            self.campaign_id,
            self.channel_id,
            self.session_id,
        )
        return GMToolExecutionContext(
            campaign_id=self.campaign_id,
            session_id=self.session_id,
            channel_id=self.channel_id,
            speaker=speaker,
            gate_status=str(gate.status or "inactive"),
            directly_addressed=True,
            metadata={
                "current_message": message,
                "recent_public_context": "离线规则机械演练，只验证类型化状态生命周期。",
                "system_gm_beat_request": bool(system_beat),
                "heartbeat_action": "rules_only_mechanical_probe" if system_beat else "",
            },
        )

    def _execute_rules_only_tool(
        self,
        tool_name: str,
        *,
        speaker: str,
        message: str,
        arguments: dict[str, object],
        label: str,
        system_beat: bool = False,
    ) -> Any:
        started = time.perf_counter()
        receipt = self.service.gm_tool_registry.execute(
            tool_name,
            arguments,
            self._rules_only_context(speaker, message, system_beat=system_beat),
        )
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        record = {
            "tool": tool_name,
            "label": label,
            "speaker": speaker,
            "ok": bool(receipt.ok),
            "state_changed": bool(receipt.state_changed),
            "error_code": str(receipt.error_code or ""),
            "message": str(receipt.message or ""),
            "elapsed_ms": elapsed_ms,
            "result": dict(receipt.result or {}),
        }
        self._record_tool_event(
            f"类型化工具：{tool_name}",
            label,
            "离线规则机械演练通过统一工具注册表执行。",
            record,
            public=False,
        )
        if not receipt.ok:
            raise RuntimeError(
                f"{label}被工具【{tool_name}】拒绝："
                f"{receipt.error_code} {receipt.message} {receipt.correction_hint}"
            )
        return receipt

    def _run_rules_only_campaign_session(self, spec: CampaignSessionSpec) -> None:
        """Exercise one complete session without pretending to simulate humans."""

        self.session_id = f"campaign-session-{spec.number:02d}"
        self.common["session_id"] = self.session_id
        runtime = self._runtime()
        app = runtime.app
        self._refresh_session_pacing(spec)

        gate = self.service.session_gates.get(
            self.campaign_id,
            self.channel_id,
            self.session_id,
        )
        if gate.status != "adventure":
            start_message = f"离线机械演练明确开始第{spec.number:02d}场冒险。"
            self._execute_rules_only_tool(
                "start_session",
                speaker="时悠",
                message=start_message,
                arguments={
                    "phase": "adventure",
                    "reason": f"第{spec.number:02d}场·{spec.title}",
                },
                label=f"第{spec.number:02d}场开启",
                system_beat=True,
            )

        # Replace the previous session scene with a typed scene so every
        # mechanical session crosses the same public/private lifecycle.
        if app.conflict_manager.state.active:
            raise RuntimeError("离线场次开始前不应残留活动冲突。")
        if app.scene_manager.current_scene is not None:
            end_message = f"第{spec.number:02d}场类型化开场前，旧镜头已经收束。"
            self._execute_rules_only_tool(
                "end_scene",
                speaker="时悠",
                message=end_message,
                arguments={
                    "summary": "旧镜头已经收束，所有场景级临时状态应被清理。",
                    "public_reply": "镜头在这里收住。",
                },
                label=f"第{spec.number:02d}场清理旧镜头",
                system_beat=True,
            )

        travel_receipt = None
        if spec.number > 1 and spec.number % 4 == 2:
            travel_state = self.service.gm_tool_registry.execute(
                "get_travel_state",
                {},
                self._rules_only_context("阿凛", "查看当前已登记的旅行地点。"),
            )
            if not travel_state.ok:
                raise RuntimeError(
                    f"第{spec.number:02d}场无法读取旅行状态：{travel_state.message}"
                )
            known_locations = [
                str(name).strip()
                for name in travel_state.result.get("discovered_locations", [])
                if str(name).strip()
            ]
            if len(known_locations) < 2:
                raise RuntimeError("离线旅行演练需要至少两个已经发现并登记的地图地点。")
            origin = known_locations[(spec.number - 2) % len(known_locations)]
            destination = next(
                name
                for name in known_locations[(spec.number - 1) % len(known_locations) :]
                + known_locations[: (spec.number - 1) % len(known_locations)]
                if name != origin
            )
            travel_message = f"队伍明确从{origin}徒步前往{destination}。"
            travel_receipt = self._execute_rules_only_tool(
                "travel_party",
                speaker="阿凛",
                message=travel_message,
                arguments={
                    "origin": origin,
                    "destination": destination,
                    "transport": "徒步",
                    "explicit_distance": 1,
                    "route_type": "land",
                    "default_threat_level": "low",
                },
                label=f"第{spec.number:02d}场旅行结算",
            )

        contract = app.story_arc_manager.state.current_pacing_plan.dramatic_contract
        location = self._session_location(spec)
        scene_type = (
            SceneType.DUNGEON.value
            if spec.number % 4 == 0
            else SceneType.TRAVEL.value
            if spec.number % 4 == 2
            else SceneType.REST.value
            if spec.number % 10 == 5
            else SceneType.STANDARD.value
        )
        opening = f"队伍抵达{location}，眼前的局面要求他们在本场作出一个会留下后果的选择。"
        start_scene_message = f"离线机械演练建立第{spec.number:02d}场场景。"
        self._execute_rules_only_tool(
            "start_scene",
            speaker="时悠",
            message=start_scene_message,
            arguments={
                "name": f"第{spec.number:02d}场·{spec.title}",
                "scene_type": scene_type,
                "location": location,
                "participants": list(self.pc_names),
                "objective": str(contract.dramatic_question or spec.title),
                "private_situation": {
                    "premise": str(contract.opening_disruption or spec.gm_opening or spec.title),
                    "stakes": str(contract.closure_requirement or "本场选择会改变后续局面。"),
                    "current_pressure": str(contract.opposition_goal or "现场阻力正在推进自身目标。"),
                    "dramatic_question": str(contract.dramatic_question or spec.title),
                    "signature_image": str(contract.signature_image or f"{location}的一处鲜明景象"),
                    "visible_elements": [location, *spec.expected_focus[:2]],
                    "secrets": list(contract.flexible_secrets[:2]),
                    "possible_payoffs": list(contract.possible_payoffs[:3]),
                },
                "public_opening": opening,
            },
            label=f"第{spec.number:02d}场类型化开场",
            system_beat=True,
        )

        objective_clock = f"第{spec.number:02d}场阶段目标"
        clock_message = f"当前局面需要建立命刻{objective_clock}。"
        self._execute_rules_only_tool(
            "create_clock",
            speaker="时悠",
            message=clock_message,
            arguments={
                "name": objective_clock,
                "segments": 4,
                "clock_type": "objective",
                "scope": "scene",
                "stakes": "填满表示队伍完成本场阶段目标。",
                "completion_consequence": "阶段目标完成，局面获得可追踪结果。",
                "auto_advance": False,
                "auto_advance_every": 1,
                "visibility": "foreground",
                "public_reply": f"【{objective_clock}】0/4",
            },
            label=f"第{spec.number:02d}场建立目标命刻",
            system_beat=True,
        )

        auto_clock_name = ""
        if spec.number % 5 == 0:
            auto_clock_name = f"第{spec.number:02d}场环境压力"
            auto_message = f"当前局面需要建立自动命刻{auto_clock_name}。"
            self._execute_rules_only_tool(
                "create_clock",
                speaker="时悠",
                message=auto_message,
                arguments={
                    "name": auto_clock_name,
                    "segments": 6,
                    "clock_type": "threat",
                    "scope": "scene",
                    "stakes": "填满表示环境压力切断安全退路。",
                    "completion_consequence": "安全退路被切断。",
                    "auto_advance": True,
                    "auto_advance_every": 1,
                    "visibility": "foreground",
                    "public_reply": f"【{auto_clock_name}】0/6",
                },
                label=f"第{spec.number:02d}场建立行动轮命刻",
                system_beat=True,
            )

        speaker_by_actor = {
            "伊莉雅": "阿凛",
            "赛璃": "南星",
            "洛岚": "白河",
            "艾薇娅": "时雨",
            "苍祈": "澄砚",
        }
        for actor in self.pc_names:
            action_message = f"{actor}明确在当前场景完成一次守望与准备。"
            self._execute_rules_only_tool(
                "perform_in_scene_action",
                speaker=speaker_by_actor[actor],
                message=action_message,
                arguments={
                    "actor": actor,
                    "action_summary": "在当前场景完成一次守望与准备",
                    "position_note": location,
                    "join_current_focus": False,
                },
                label=f"第{spec.number:02d}场行动轮·{actor}",
            )

        auto_clock_progress = None
        if auto_clock_name:
            if not app.clock_manager.exists(auto_clock_name):
                raise RuntimeError(f"自动命刻【{auto_clock_name}】在完整行动轮后意外消失。")
            auto_clock_progress = int(app.clock_manager.get(auto_clock_name).current)
            if auto_clock_progress != 1:
                raise RuntimeError(
                    f"自动命刻【{auto_clock_name}】应在全体行动后推进一次，实际为{auto_clock_progress}/6。"
                )
            close_auto_message = f"本场已化解{auto_clock_name}，该压力不再成立。"
            self._execute_rules_only_tool(
                "close_clock",
                speaker="时悠",
                message=close_auto_message,
                arguments={
                    "name": auto_clock_name,
                    "mode": "abandoned",
                    "reason": "队伍完成整轮行动后改变了局面，环境压力失去意义。",
                    "public_reply": f"【{auto_clock_name}】1/6，压力已经散去。",
                    "public_facts": [],
                },
                label=f"第{spec.number:02d}场关闭行动轮命刻",
                system_beat=True,
            )

        for after in (2, 4):
            progress_message = f"队伍已经直接推进{objective_clock}到{after}/4。"
            self._execute_rules_only_tool(
                "change_clock",
                speaker="阿凛" if after == 2 else "南星",
                message=progress_message,
                arguments={
                    "name": objective_clock,
                    "delta": 2,
                    "cause": "direct_action_success",
                    "reason": "队伍在当前场景完成了直接对应阶段目标的行动。",
                    "public_reply": f"【{objective_clock}】{after}/4",
                    "completion_facts": [],
                },
                label=f"第{spec.number:02d}场推进目标命刻到{after}/4",
            )
        close_message = f"{objective_clock}已经完成并在现场兑现。"
        self._execute_rules_only_tool(
            "close_clock",
            speaker="时悠",
            message=close_message,
            arguments={
                "name": objective_clock,
                "mode": "resolved",
                "reason": "阶段目标已经完成并产生可追踪结果。",
                "public_reply": f"【{objective_clock}】4/4，阶段目标已经完成。",
                "public_facts": ["阶段目标已经完成。"],
            },
            label=f"第{spec.number:02d}场结案目标命刻",
            system_beat=True,
        )

        end_scene_message = f"第{spec.number:02d}场现场结果已经落地，结束当前场景。"
        self._execute_rules_only_tool(
            "end_scene",
            speaker="时悠",
            message=end_scene_message,
            arguments={
                "summary": f"第{spec.number:02d}场阶段目标完成，现场结果已记录。",
                "public_reply": "这段局面在已经发生的结果上收住。",
            },
            label=f"第{spec.number:02d}场结束场景",
            system_beat=True,
        )

        end_session_message = f"第{spec.number:02d}场已经收束，结束本场并保存。"
        ended_receipt = self._execute_rules_only_tool(
            "end_session",
            speaker="时悠",
            message=end_session_message,
            arguments={
                "title": spec.title,
                "public_reply": "本场到这里，进度已经保存。",
            },
            label=f"第{spec.number:02d}场收团",
            system_beat=True,
        )
        ended = dict(ended_receipt.result or {})
        level_ups = self._apply_between_session_level_ups(spec, ended)
        result = {
            "number": spec.number,
            "title": spec.title,
            "arc": spec.arc,
            "phase": "rules_only_mechanical",
            "boss_session": spec.boss_session,
            "foreground_clocks": [],
            "scene_type": scene_type,
            "travel_exercised": travel_receipt is not None,
            "auto_round_clock_progress": auto_clock_progress,
            "experience": dict(ended.get("experience") or {}),
            "level_ups": level_ups,
            "gate_status": str((ended.get("gate") or {}).get("status") or ""),
            "active_scene_after": app.scene_manager.current_scene is not None,
            "active_conflict_after": bool(app.conflict_manager.state.active),
            "blocking_windows_after": len(
                [
                    window
                    for window in app.interceptor.decision_window_manager.pending()
                    if window.blocking
                ]
            ),
            "session_scoped_clocks_after": [
                clock.name
                for clock in app.clock_manager.all()
                if clock.scope in {"scene", "session"}
            ],
        }
        self.rules_only_session_results.append(result)
        self.session_reports.append(result)
        self.session_completion_results[spec.number] = {
            "earned": True,
            "continued": False,
            "reasons": ["离线规则机械演练已完成类型化场次生命周期。"],
            "player_turns": len(self.pc_names),
            "act": 4,
            "pending_blocking_decisions": result["blocking_windows_after"],
        }

    def _build_rules_only_report(self) -> dict[str, Any]:
        runtime = self._runtime()
        app = runtime.app
        map_files = [
            str(path)
            for path in sorted(self.map_root.rglob("*"))
            if path.is_file()
        ]
        receipts = [
            event.get("result", {})
            for event in self.tool_events
            if str(event.get("tool") or "").startswith("类型化工具：")
        ]
        session_results = list(self.rules_only_session_results)
        checks = {
            "all_sessions_completed": len(session_results) == self.target_sessions,
            "all_typed_tool_receipts_succeeded": bool(receipts)
            and all(bool(item.get("ok")) for item in receipts),
            "all_session_gates_closed": all(
                item.get("gate_status") == "inactive" for item in session_results
            ),
            "no_scene_or_conflict_leak": app.scene_manager.current_scene is None
            and not app.conflict_manager.state.active
            and all(
                not item.get("active_scene_after")
                and not item.get("active_conflict_after")
                for item in session_results
            ),
            "no_blocking_decision_leak": not any(
                int(item.get("blocking_windows_after") or 0) for item in session_results
            ),
            "no_scene_or_session_clock_leak": not any(
                item.get("session_scoped_clocks_after") for item in session_results
            ),
            "experience_settled_each_session": all(
                bool(item.get("experience")) for item in session_results
            ),
            "complete_action_round_advances_auto_clock_once": all(
                item.get("auto_round_clock_progress") in {None, 1}
                for item in session_results
            )
            and any(item.get("auto_round_clock_progress") == 1 for item in session_results),
            "travel_lifecycle_exercised": any(
                bool(item.get("travel_exercised")) for item in session_results
            ),
            "level_progression_observed": self.target_sessions < 2
            or bool(self.level_up_results),
            "world_map_generated": bool(map_files),
            "astrbot_bridge_smoke_ok": (not self.run_astrbot_smoke)
            or (
                bool(self.astrbot_bridge_results)
                and all(
                    item.get("ok")
                    and item.get("main_campaign_unchanged")
                    and item.get("probe_gate_closed")
                    for item in self.astrbot_bridge_results
                )
            ),
            "heartbeat_probe_ok": bool(self.heartbeat_results)
            and all(item.get("ok") for item in self.heartbeat_results),
        }
        errors = list(self.errors)
        errors.extend(
            f"rules-only check failed: {name}"
            for name, ok in checks.items()
            if not ok
        )
        elapsed = [int(item.get("elapsed_ms") or 0) for item in receipts]
        mechanical_ok = all(checks.values()) and not errors
        return {
            "ok": mechanical_ok,
            "mechanical_ok": mechanical_ok,
            "campaign_id": self.campaign_id,
            "target_sessions": self.target_sessions,
            "length_profile": self.length_profile,
            "semantic_llm": False,
            "scripted_identities": self.scripted_identities,
            "semantic_quality_applicable": False,
            "completed_sessions": len(session_results),
            "checks": checks,
            "check_applicability": {
                "conversation_quality": False,
                "astrbot_bridge": self.run_astrbot_smoke,
                "heartbeat_probe": True,
            },
            "errors": errors,
            "notes": [
                *self.notes,
                "该报告只证明类型化规则与生命周期可运行，不证明GM或FU-PL具有真人对话质量。",
            ],
            "latency": {
                "count": len(elapsed),
                "avg_ms": int(mean(elapsed)) if elapsed else 0,
                "max_ms": max(elapsed) if elapsed else 0,
            },
            "session_reports": session_results,
            "tool_events": self.tool_events,
            "astrbot_bridge_results": self.astrbot_bridge_results,
            "heartbeat_results": self.heartbeat_results,
            "level_up_results": self.level_up_results,
            "final_characters": {
                name: asdict(app.character_manager.get(name))
                for name in self.pc_names
                if app.character_manager.exists(name)
            },
            "artifacts": {
                "run_root": str(self.run_root),
                "conversation": str(self.conversation_path),
                "report_json": str(self.report_json_path),
                "report_txt": str(self.report_txt_path),
                "campaign_root": str(self.campaign_root),
                "map_root": str(self.map_root),
                "map_output": "\n".join(map_files),
            },
        }

    def _build_setup_only_report(self) -> dict[str, Any]:
        runtime = self._runtime()
        world = runtime.app.world_state.world_profile
        gate = self.service.session_gates.get(
            self.campaign_id,
            self.channel_id,
            self.session_id,
        )
        agent_error_calls = self._agent_error_calls(self.calls)
        recovered_agent_error_calls = self._recovered_agent_error_calls(self.calls)
        failed_tool_receipts = self._failed_tool_receipts(self.calls)
        unrecovered_tool_failure_calls = self._unrecovered_tool_failure_calls(self.calls)
        parse_recoveries = [
            item
            for call in self.calls
            for item in ((call.get("body") or {}).get("agent_trace") or [])
            if isinstance(item, dict) and item.get("phase") == "parse_recovery"
        ]
        map_files = [
            str(path)
            for path in sorted(self.map_root.rglob("*"))
            if path.is_file()
        ]
        checks = {
            "setup_flow_completed": self._setup_only_completed,
            "adventure_gate_active": gate.status == "adventure",
            "world_creation_complete": runtime.app.session_zero_manager.world_creation_ready(),
            "all_hero_drafts_confirmed": len(world.hero_drafts) == len(self.pc_names)
            and all(draft.confirmed for draft in world.hero_drafts.values()),
            "all_player_characters_materialized": all(
                runtime.app.character_manager.exists(name) for name in self.pc_names
            ),
            "map_generated": bool(map_files),
            "no_agent_errors": not agent_error_calls,
            "no_unrecovered_tool_failures": not unrecovered_tool_failure_calls,
        }
        errors = list(self.errors)
        errors.extend(
            f"setup check failed: {name}" for name, ok in checks.items() if not ok
        )
        elapsed = [int(call.get("elapsed_ms") or 0) for call in self.calls]
        return {
            "ok": all(checks.values()) and not errors,
            "mechanical_ok": all(checks.values()),
            "campaign_id": self.campaign_id,
            "target_sessions": self.target_sessions,
            "length_profile": self.length_profile,
            "semantic_llm": self.semantic_llm,
            "scripted_identities": self.scripted_identities,
            "completed_sessions": 0,
            "checks": checks,
            "check_applicability": {},
            "errors": errors,
            "latency": {
                "sample_count": len(elapsed),
                "average_ms": int(mean(elapsed)) if elapsed else 0,
                "max_ms": max(elapsed) if elapsed else 0,
            },
            "conversation_quality": {},
            "session_table_metrics": {},
            "session_scene_metrics": {},
            "session_reports": [],
            "issue_classification": {
                "agent_error_calls": agent_error_calls,
                "recovered_agent_error_calls": recovered_agent_error_calls,
                "failed_tool_receipts": failed_tool_receipts,
                "unrecovered_tool_failure_calls": unrecovered_tool_failure_calls,
                "parse_recoveries": parse_recoveries,
            },
            "final_characters": {},
            "player_simulation_metrics": [],
            "campaign_pacing": {},
            "tool_events": self.tool_events,
            "astrbot_bridge_results": self.astrbot_bridge_results,
            "heartbeat_results": self.heartbeat_results,
            "artifacts": {
                "run_root": str(self.run_root),
                "conversation": str(self.conversation_path),
                "conversation_export": str(self.conversation_export_path),
                "report_json": str(self.report_json_path),
                "report_txt": str(self.report_txt_path),
                "campaign_root": str(self.campaign_root),
                "map_root": str(self.map_root),
                "map_output": "\n".join(map_files),
            },
        }

    @staticmethod
    def _agent_error_calls(calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "index": call.get("index"),
                "label": call.get("label"),
                "error": str((call.get("body") or {}).get("agent_error") or ""),
            }
            for call in calls
            if str((call.get("body") or {}).get("agent_error") or "").strip()
            and not TwentySessionCampaignHarness._call_has_recovered_state_change(call)
        ]

    @staticmethod
    def _recovered_agent_error_calls(
        calls: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        return [
            {
                "index": call.get("index"),
                "label": call.get("label"),
                "error": str((call.get("body") or {}).get("agent_error") or ""),
            }
            for call in calls
            if str((call.get("body") or {}).get("agent_error") or "").strip()
            and TwentySessionCampaignHarness._call_has_recovered_state_change(call)
        ]

    @staticmethod
    def _call_has_recovered_state_change(call: dict[str, Any]) -> bool:
        receipts = [
            item
            for item in ((call.get("body") or {}).get("tool_receipts") or [])
            if isinstance(item, dict)
        ]
        successful_write_indexes = [
            index
            for index, receipt in enumerate(receipts)
            if bool(receipt.get("ok")) and bool(receipt.get("state_changed"))
        ]
        if not successful_write_indexes:
            return False
        return all(
            bool(receipt.get("ok"))
            for receipt in receipts[successful_write_indexes[-1] + 1 :]
        )

    @staticmethod
    def _failed_tool_receipts(calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "call_index": call.get("index"),
                "call_label": call.get("label"),
                **dict(receipt),
            }
            for call in calls
            for receipt in ((call.get("body") or {}).get("tool_receipts") or [])
            if isinstance(receipt, dict) and not bool(receipt.get("ok"))
        ]

    @classmethod
    def _unrecovered_tool_failure_calls(
        cls,
        calls: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        failed_labels = {
            (item.get("call_index"), item.get("call_label"))
            for item in cls._failed_tool_receipts(calls)
        }
        return [
            {
                "index": call.get("index"),
                "label": call.get("label"),
                "agent_error": str((call.get("body") or {}).get("agent_error") or ""),
            }
            for call in calls
            if (call.get("index"), call.get("label")) in failed_labels
            and str((call.get("body") or {}).get("agent_error") or "").strip()
            and not cls._call_has_recovered_state_change(call)
        ]

    def _main_flow(self) -> None:
        if self._resume_checkpoint_loaded:
            self._resume_main_flow()
            return
        self._record_tool_event(
            "核心规则书第32页战役结构",
            "战役设计前",
            "按20场短篇战役执行；每场以约四小时为单位，由多个场景组成，并围绕一个地点与紧迫事件形成会影响后续场次的阶段性结果。",
            {
                "target_sessions": self.target_sessions,
                "session_duration_hours": 4,
                "minimum_scenes_per_session": 3,
                "episode_contract": ["具体地点", "紧迫事件", "多场景推进", "阶段性结果", "结果进入后续状态"],
                "scene_types_sampled": ["普通", "冲突", "插曲", "GM", "休息", "旅行", "地下城"],
            },
        )
        self._record_tool_event(
            "优秀GM方法参考",
            "战役设计前",
            "本测试按强开场、可移动线索、反派前线/凶兆、状态文档与命刻预算设计，不写死剧情路线。",
            {
                "methods": [
                    "Sly Flourish: strong start, scenes, secrets/clues, locations, NPCs, monsters, treasure",
                    "The Alexandrian: node-based scenario design and campaign status document",
                    "Dungeon World: fronts, grim portents, impending doom",
                    "Blades-style clocks: visible pressure, clear consequences, not every clock foreground at once",
                ]
            },
        )
        self.invoke("新建战役", "POST", "/v1/campaigns/new", {"campaign_id": self.campaign_id})
        if self.semantic_llm:
            self._assert_llm_preflight()
            self.route_table_message(
                "玩家请求开始第零章",
                "阿凛",
                "@时悠，大家准备好了，请开始第零章。先让我们一起谈基调、安全边界和世界。",
                expected_target="fu_gm",
                expected_send_reply=True,
                directed_at_gm=True,
            )
        else:
            self.notes.append("本次为离线规则机械演练：不调用语义模型，不作为真人主持质量结论。")
            self._execute_rules_only_tool(
                "start_session",
                speaker="阿凛",
                message="大家准备好了，请开始第零章。先让我们一起谈基调、安全边界和世界。",
                arguments={
                    "phase": "session_zero",
                    "reason": "离线规则机械演练开始第零章",
                },
                label="类型化工具开启第零章",
            )
        setup_status = self.invoke(
            "确认第零章门控",
            "POST",
            "/v1/session/status",
            self.common,
        )
        if str((setup_status.get("gate") or {}).get("status") or "") != "session_zero":
            raise RuntimeError("玩家明确请求后，第零章门控没有由start_session工具建立。")
        if self.run_astrbot_smoke:
            self._run_astrbot_bridge_smoke("第零章门控后")
        if self.semantic_llm:
            self.route_table_message(
                "第零章自由讨论静默 01",
                "阿凛",
                "我刚泡好茶，大家今天慢慢来，别急。",
                expected_target="silent",
                expected_send_reply=False,
            )
            self._run_setup_contributions()
        else:
            self._run_rules_only_setup()
        campaign_specs = self._prepare_campaign_after_setup()
        if not campaign_specs:
            return
        if self.setup_only:
            self._setup_only_completed = True
            self._write_campaign_checkpoint(0)
            return
        for spec in campaign_specs:
            if self.semantic_llm:
                self._run_campaign_session(spec)
            else:
                self._run_rules_only_campaign_session(spec)
            self._write_campaign_checkpoint(spec.number)
        if self.run_astrbot_smoke:
            self._run_astrbot_bridge_smoke("战役长测结束后")
        self._run_heartbeat_probe()
        self._write_campaign_checkpoint(self.target_sessions, completed=True)

    def _resume_main_flow(self) -> None:
        if self.semantic_llm:
            self._assert_llm_preflight()
        if str(self._in_progress_session_state.get("phase") or "") == "session_zero":
            self.service.session_gates.activate(
                self.campaign_id,
                self.channel_id,
                self.session_id,
                status="session_zero",
                reason="恢复第零章长测检查点",
            )
            self._run_setup_contributions(
                world_completed_index=int(
                    self._in_progress_session_state.get("world_completed_index") or 0
                ),
                character_completed_index=int(
                    self._in_progress_session_state.get("character_completed_index") or 0
                ),
            )
            campaign_specs = self._prepare_campaign_after_setup()
            if not campaign_specs:
                return
            if self.setup_only:
                self._setup_only_completed = True
                self._write_campaign_checkpoint(0)
                return
            for spec in campaign_specs:
                self._run_campaign_session(spec)
                self._write_campaign_checkpoint(spec.number)
            if self.run_astrbot_smoke:
                self._run_astrbot_bridge_smoke("战役长测结束后")
            self._run_heartbeat_probe()
            self._write_campaign_checkpoint(self.target_sessions, completed=True)
            return
        runtime = self._runtime()
        configure_kwargs = self._pacing_configure_kwargs()
        runtime.app.campaign_pacing_manager.configure(**configure_kwargs)
        self._record_tool_event(
            "战役长测断点续跑",
            f"第{self._resume_completed_session + 1:02d}场之前",
            "从上一场完整收团后的战役快照继续；中断时未完成的半场已隔离，不计入状态或指标。",
            {
                "completed_session": self._resume_completed_session,
                "campaign_id": self.campaign_id,
                "campaign_root": str(self.campaign_root),
            },
        )
        campaign_specs = self._campaign_sessions()
        if not campaign_specs:
            self.errors.append("战役脚本没有生成任何场次。")
            return
        in_progress_number = int(self._in_progress_session_state.get("session_number") or 0)
        resume_spec = next(
            (spec for spec in campaign_specs if spec.number == in_progress_number),
            campaign_specs[0],
        )
        if not self._ensure_adventure_started(resume_spec):
            return
        if self.setup_only:
            self._setup_only_completed = True
            self._write_campaign_checkpoint(0)
            return
        self._restore_current_scene_public_context_if_needed(resume_spec)
        for spec in campaign_specs:
            if spec.number <= self._resume_completed_session:
                continue
            resume_state = (
                dict(self._in_progress_session_state)
                if spec.number == in_progress_number
                else None
            )
            self._run_campaign_session(spec, resume_state=resume_state)
            self._write_campaign_checkpoint(spec.number)
        if self.run_astrbot_smoke:
            self._run_astrbot_bridge_smoke("战役长测结束后")
        self._run_heartbeat_probe()
        self._write_campaign_checkpoint(self.target_sessions, completed=True)

    def _restore_current_scene_public_context_if_needed(
        self,
        spec: CampaignSessionSpec,
    ) -> None:
        """Re-establish a public scene boundary after a mid-scene resume.

        A persisted campaign snapshot can already be on scene three while the
        checkpoint was written just before that scene's opening response made
        it into the long-test transcript.  Without a public boundary, FU-PL
        sees prior-scene actions as current and rejects sensible new actions as
        repeats.  The recap is deliberately a no-change narration: it restores
        shared table context without replaying or advancing the scene.
        """

        state = dict(self._in_progress_session_state or {})
        if int(state.get("session_number") or 0) != spec.number:
            return
        current_act = int(state.get("current_act") or 1)
        if current_act <= 1:
            return
        expected_label = f"第{spec.number:02d}场场景{current_act}开场"
        if any(str(call.get("label") or "") == expected_label for call in self.calls):
            return
        # A player action may itself establish the new scene boundary and be
        # checkpointed before the harness has emitted a formal "scene opening"
        # label. In that case the table already shares the live location, so a
        # reconnect recap would immediately repeat the just-published GM reply.
        # Only synthesize a recap when the persisted scene has no matching
        # public location statement in the recent transcript.
        try:
            current_scene = self._runtime().app.scene_manager.current_scene
        except Exception:
            current_scene = None
        current_location = str(getattr(current_scene, "location", "") or "").strip()
        if current_location:
            # Players usually shorten ``白花碑驿站·登记小室`` to
            # ``登记小室`` once the camera is established.  That is still a
            # valid shared scene boundary; requiring the fully qualified name
            # caused reconnects to paste an unnecessary recap over live play.
            location_aliases = {current_location}
            location_aliases.update(
                part.strip()
                for part in re.split(r"[·/／>＞→]", current_location)
                if len(part.strip()) >= 4
            )
            recent_public_text = "\n".join(
                part
                for call in self.calls[-16:]
                for part in (
                    str(call.get("message") or "").strip(),
                    str(call.get("reply") or "").strip(),
                )
                if part
            )
            if any(alias in recent_public_text for alias in location_aliases):
                return
        self.invoke(
            f"第{spec.number:02d}场场景{current_act}断点现场回顾",
            "POST",
            "/v1/game/scene-recap",
            {
                **self.common,
                "speaker": "时悠",
            },
        )

    def _pacing_configure_kwargs(self) -> dict[str, Any]:
        """Keep pilot/resume runs on the full campaign's pacing horizon."""

        kwargs: dict[str, Any] = {
            "length": self.length_profile,
            "target_sessions": self.campaign_profile_sessions,
        }
        if self.target_arcs:
            kwargs["target_arcs"] = self.target_arcs
        return kwargs

    def _ensure_adventure_started(self, first_spec: CampaignSessionSpec) -> bool:
        if self._adventure_started:
            # Campaign snapshots intentionally do not serialize a deployment's
            # channel gate. A resumed harness therefore has the correct scene
            # but a fresh _session_gates.json; reactivate it without generating
            # a second opening, public reply, or consuming a player turn.
            self.session_id = f"campaign-session-{first_spec.number:02d}"
            self.common["session_id"] = self.session_id
            app = self._runtime().app
            current = app.scene_manager.current_scene
            if current is None or current.scene_type == SceneType.SESSION_ZERO:
                # Older/cancelled calibration checkpoints could persist the
                # adventure flag before the first real scene was committed.
                # Repair that invariant before restoring the channel gate so
                # clues and scene-scoped state cannot leak into Session 0.
                self._prepare_session_runtime(first_spec)
                self._write_campaign_checkpoint(0)
            self._normalize_resumed_opening_clock()
            self.service.session_gates.activate(
                self.campaign_id,
                self.channel_id,
                self.session_id,
                status="adventure",
                reason=first_spec.title,
            )
            self._record_tool_event(
                "断点恢复第一场门控",
                f"第{first_spec.number:02d}场恢复",
                "只恢复部署级频道门控；不向玩家发送伪造的续跑台词。",
                {
                    "campaign_id": self.campaign_id,
                    "session_id": self.session_id,
                    "status": "adventure",
                },
            )
            return True
        runtime = self._runtime()
        # Install the first session's situation contract before the gate asks
        # for narration. Otherwise the generic Session 0 scene opens first and
        # the episode's signature image arrives one response too late.
        self._refresh_session_pacing(first_spec)
        self.session_id = "campaign-session-01"
        self.common["session_id"] = self.session_id
        if not any(clock.source == "第01场强开场" for clock in runtime.app.clock_manager.all()):
            frame = runtime.app.scene_frame_manager.current_frame
            opening_situation = str(
                getattr(frame, "session_opportunity_situation", "") or ""
            ).strip()
            arrival_probe = Clock(
                name="财团巡逻队逼近",
                max_segments=8,
                current=0,
                clock_type="threat",
                stakes="填满后财团巡逻队包围白花碑驿站。",
            )
            patrol_already_present = bool(
                ClockNarrativeBoundary.violation(
                    opening_situation,
                    ClockNarrativeBoundary.packet([arrival_probe]),
                )
            )
            runtime.app.clock_manager.add(
                Clock(
                    name=("财团强制搜查升级" if patrol_already_present else "财团巡逻队逼近"),
                    max_segments=8,
                    current=0,
                    clock_type="threat",
                    stakes=(
                        "填满后旅人的身份暴露，守望会失去回旋余地。"
                        if patrol_already_present
                        else "填满后财团巡逻队包围白花碑驿站。"
                    ),
                    auto_advance="每个行动轮结束后推进1格",
                    auto_advance_timing="action_round_end",
                    auto_advance_every=1,
                    scope="session",
                    source="第01场强开场",
                )
            )
        # Save the fully prepared Session 0 before the expensive model-backed
        # opening. A failed candidate validation can then retry only this gate.
        self._write_campaign_checkpoint(0)
        if not self.semantic_llm:
            self._execute_rules_only_tool(
                "start_session",
                speaker="时悠",
                message=f"离线规则机械演练明确进入第一章，并准备开始{first_spec.title}。",
                arguments={
                    "phase": "adventure",
                    "reason": first_spec.title,
                },
                label="类型化工具进入第一场",
                system_beat=True,
            )
            self._adventure_started = True
            self._write_campaign_checkpoint(0)
            return True
        gate = self.route_table_message(
            "玩家明确确认进入第一场",
            "阿凛",
            "大家都点头了，时悠，可以进入第一章了。请先描述白花碑驿站的现场。",
            expected_target="fu_gm",
            expected_send_reply=True,
        )
        if gate.get("blocked"):
            self.errors.append(f"进入第一场被阻挡：{gate.get('reply')}")
            return False
        if (gate.get("gate") or {}).get("status") != "adventure":
            self.errors.append(f"玩家明确确认后没有进入第一场：{gate.get('reply')}")
            return False
        opening_reply = str(gate.get("reply") or "").strip()
        if not self._is_substantive_first_scene_opening(opening_reply):
            self.errors.append(
                "第一场开场没有真正呈现可行动的现场，而是空白或元叙述："
                + opening_reply[:240]
            )
            return False
        self._adventure_started = True
        self._wait_for_async_map_if_any()
        self._write_campaign_checkpoint(0)
        return True

    @staticmethod
    def _is_substantive_first_scene_opening(reply: str) -> bool:
        text = " ".join(str(reply or "").split()).strip()
        if len(text) < 32 or any(
            marker in text
            for marker in (
                "现场描述已经呈现",
                "场景描述已经呈现",
                "开场已经呈现",
                "现场已经送达",
                "场景已经送达",
                "互动焦点",
                "可互动的焦点",
                "场景框架",
                "玩家可以",
                "以下是开场",
            )
        ):
            return False
        # A natural opening does not have to repeat the location's proper name
        # after Session 0 has already established it.  What matters at the
        # table is that the reply presents a place, a concrete person or changed
        # object the players can engage with, and an immediate situation they
        # can act on.  A strong investigative opening need not force an NPC into
        # the first paragraph merely to satisfy the harness.
        opening_dimensions = (
            (
                "白花碑驿站",
                "驿站",
                "门廊",
                "风铃廊",
                "闸门",
                "炉火",
                "雾潮",
                "山道",
            ),
            (
                "失名旅人",
                "失忆旅人",
                "旅人",
                "守望会",
                "会长",
                "巡守",
                "监察官",
                "公告板",
                "结算单",
                "账册",
                "路牌",
                "印记",
                "遗物",
                "痕迹",
                "车辙",
            ),
            (
                "逼近",
                "靠近",
                "脚步声",
                "巡逻",
                "追兵",
                "封锁",
                "放行",
                "旧路",
                "谁负责",
                "先说清楚",
                "等着回答",
                "等你们回答",
                "褪成空白",
                "滑出",
                "遮去",
                "你们可以",
                "你们准备",
                "你们打算",
                "先去",
                "观察",
                "寻找",
                "窥视",
                "灯光忽明忽暗",
            ),
        )
        return all(any(anchor in text for anchor in dimension) for dimension in opening_dimensions)

    def _normalize_resumed_opening_clock(self) -> None:
        """Bring old pilot checkpoints onto the current round-clock contract.

        Early interrupted runs stored the test-only opening countdown as one
        tick every two action rounds.  Reusing that snapshot after changing the
        scenario contract makes a correct one-round implementation look broken.
        Only this harness-owned source is migrated; real campaign clocks and
        deliberately slower countdowns remain untouched.
        """

        app = self._runtime().app
        for clock in app.clock_manager.all():
            if str(clock.source or "").strip() != "第01场强开场":
                continue
            clock.auto_advance = "每个行动轮结束后推进1格"
            clock.auto_advance_timing = "action_round_end"
            clock.auto_advance_every = 1
            clock.auto_advance_progress = 0

    def _assert_llm_preflight(self) -> None:
        """Verify the semantic model, waiting through a bounded provider outage."""

        self._llm_preflight_attempted = True
        self._llm_preflight_ok = False
        self._llm_preflight_error = ""
        runtime = self._runtime()
        components = [
            self.service.gm_tool_agent,
            runtime.app.expressor,
        ]
        checked_clients: set[int] = set()
        results: list[dict[str, Any]] = []
        for component in components:
            client = getattr(component, "client", None)
            model = str(getattr(component, "model", "") or "")
            if client is None or not model or id(client) in checked_clients:
                continue
            checked_clients.add(id(client))
            started = time.monotonic()
            try:
                content, provider_retries = self._preflight_completion_with_recovery(
                    client=client,
                    model=model,
                    component_name=component.__class__.__name__,
                )
            except Exception as exc:
                error = RuntimeError(
                    f"长测 LLM 前置检查失败（{component.__class__.__name__}/{model}）：{exc}"
                )
                self._llm_preflight_error = str(error)
                raise error from exc
            if not str(content or "").strip():
                self._llm_preflight_error = (
                    f"长测 LLM 前置检查返回空内容（{component.__class__.__name__}/{model}）。"
                )
                raise RuntimeError(self._llm_preflight_error)
            results.append(
                {
                    "component": component.__class__.__name__,
                    "model": model,
                    "elapsed_ms": int((time.monotonic() - started) * 1000),
                    "nonempty": True,
                    "provider_retries": provider_retries,
                }
            )
        if not results:
            self._llm_preflight_error = "长测要求真实语义模型，但当前运行时没有可调用的 LLM 客户端。"
            raise RuntimeError(self._llm_preflight_error)
        self._llm_preflight_ok = True
        self._record_tool_event(
            "LLM 长测前置检查",
            "战役设计前",
            "在创建角色和生成地图前验证核心 GM、NPC 子智能体与 Expressor 的真实模型可用，避免静默降级污染整份报告。",
            results,
        )

    def _preflight_completion_with_recovery(
        self,
        *,
        client: Any,
        model: str,
        component_name: str,
    ) -> tuple[str, list[dict[str, Any]]]:
        retry_limit = max(
            0,
            int(os.environ.get("FU_GM_LONG_TEST_PROVIDER_RETRY_LIMIT", "12")),
        )
        base_delay = max(
            0.0,
            float(os.environ.get("FU_GM_LONG_TEST_PROVIDER_RETRY_BASE_SECONDS", "15")),
        )
        maximum_delay = max(
            base_delay,
            float(os.environ.get("FU_GM_LONG_TEST_PROVIDER_RETRY_MAX_SECONDS", "60")),
        )
        recoveries: list[dict[str, Any]] = []
        attempt = 1
        while True:
            started = time.monotonic()
            try:
                content = client.create_chat_completion(
                    model=model,
                    messages=[
                        ChatMessage(role="system", content="这是 FU-GM 长测连通性检查。只回复 JSON。"),
                        ChatMessage(role="user", content='只输出 {"ok":true}。'),
                    ],
                    temperature=0,
                    response_format={"type": "json_object"},
                    operation="longrun.preflight",
                )
                return content, recoveries
            except Exception as exc:
                if attempt > retry_limit or not self._is_provider_unavailable_exception(exc):
                    raise
                delay = min(maximum_delay, base_delay * (1.5 ** max(0, attempt - 1)))
                recoveries.append(
                    {
                        "attempt": attempt,
                        "elapsed_ms": int((time.monotonic() - started) * 1000),
                        "error": str(exc)[:500],
                        "retry_delay_seconds": delay,
                    }
                )
                print(
                    f"[FU-GM LLM] {component_name}/{model} preflight unavailable; "
                    f"private retry {attempt} in {delay:.1f}s",
                    flush=True,
                )
                if delay > 0:
                    time.sleep(delay)
                attempt += 1

    @staticmethod
    def _is_provider_unavailable_exception(exc: Exception) -> bool:
        if isinstance(
            exc,
            (LLMDeadlineExceeded, LLMEmptyResponseError, TimeoutError),
        ):
            return True
        if isinstance(exc, LLMHTTPError):
            return exc.status_code == 429 or exc.status_code >= 500
        text = str(exc or "").lower()
        if re.search(r"\b(?:llm\s+)?http\s+(?:429|5\d\d)\b", text):
            return True
        return any(
            marker in text
            for marker in (
                "timed out",
                "timeout",
                "upstream request failed",
                "temporarily unavailable",
                "connection reset",
                "remote end closed connection",
                "bad gateway",
                "gateway timeout",
                "provider circuit is open",
                "网站请求超时",
                "请求超时",
                "网关超时",
            )
        )

    def _write_in_progress_session_checkpoint(
        self,
        spec: CampaignSessionSpec,
        *,
        phase: str,
        scripted_next_index: int,
        continuation_index: int,
        session_start_call_count: int,
        scene_history_start: int,
        resource_before: dict[str, Any],
        gm_beat_count: int,
        player_turn_count: int,
        routed_discussion_count: int,
        processed_player_turns: int,
        current_act: int,
        act_started_at_turn: int,
        last_assessment_turn: int,
        last_extension_gm_beat_turn: int,
        last_lane_refocus_signature: str,
        last_lane_refocus_turn: int,
        assessment: SessionProgressAssessment,
        authoritative_resolution_at_turn: int | None = None,
        pending_table_event: dict[str, Any] | None = None,
    ) -> None:
        """Persist only after one complete table event and all of its windows."""

        state = {
            "session_number": spec.number,
            "phase": str(phase),
            "scripted_next_index": int(scripted_next_index),
            "continuation_index": int(continuation_index),
            "session_start_call_count": int(session_start_call_count),
            "scene_history_start": int(scene_history_start),
            "resource_before": dict(resource_before),
            "gm_beat_count": int(gm_beat_count),
            "player_turn_count": int(player_turn_count),
            "routed_discussion_count": int(routed_discussion_count),
            "processed_player_turns": int(processed_player_turns),
            "current_act": int(current_act),
            "act_started_at_turn": int(act_started_at_turn),
            "last_assessment_turn": int(last_assessment_turn),
            "last_extension_gm_beat_turn": int(last_extension_gm_beat_turn),
            "last_lane_refocus_signature": str(last_lane_refocus_signature or ""),
            "last_lane_refocus_turn": int(last_lane_refocus_turn),
            "authoritative_resolution_at_turn": authoritative_resolution_at_turn,
            "assessment": asdict(assessment),
            "pending_scene_transition": dict(self._pending_scene_transition),
            "pending_table_event": dict(pending_table_event or {}),
        }
        self._write_campaign_checkpoint(
            spec.number - 1,
            in_progress_state=state,
        )

    def _run_campaign_session(
        self,
        spec: CampaignSessionSpec,
        *,
        resume_state: dict[str, Any] | None = None,
    ) -> None:
        self.session_id = f"campaign-session-{spec.number:02d}"
        self.common["session_id"] = self.session_id
        app = self._runtime().app
        expanded_turns = self._expanded_session_turns(spec)
        state = dict(resume_state or {})
        if state:
            if int(state.get("session_number") or 0) != spec.number:
                raise ValueError("场内检查点与待恢复场次不一致。")
            session_start_call_count = int(state.get("session_start_call_count") or 0)
            scene_history_start = int(state.get("scene_history_start") or 0)
            resource_before = dict(state.get("resource_before") or {})
            gm_beat_count = int(state.get("gm_beat_count") or 0)
            player_turn_count = int(state.get("player_turn_count") or 0)
            routed_discussion_count = int(state.get("routed_discussion_count") or 0)
            processed_player_turns = int(state.get("processed_player_turns") or 0)
            current_act = int(state.get("current_act") or 1)
            act_started_at_turn = int(state.get("act_started_at_turn") or 0)
            last_assessment_turn = int(state.get("last_assessment_turn") or 0)
            last_extension_gm_beat_turn = int(
                state.get("last_extension_gm_beat_turn") or player_turn_count
            )
            last_lane_refocus_signature = str(
                state.get("last_lane_refocus_signature") or ""
            )
            last_lane_refocus_turn = int(state.get("last_lane_refocus_turn") or -100)
            resolution_turn_value = state.get("authoritative_resolution_at_turn")
            authoritative_resolution_at_turn = (
                int(resolution_turn_value)
                if resolution_turn_value is not None
                else None
            )
            assessment = SessionProgressAssessment(**dict(state.get("assessment") or {}))
            phase = str(state.get("phase") or "scripted")
            scripted_next_index = max(1, int(state.get("scripted_next_index") or 1))
            continuation_index = int(state.get("continuation_index") or len(expanded_turns))
            self._pending_scene_transition = dict(
                state.get("pending_scene_transition") or {}
            )
            pending_table_event = dict(state.get("pending_table_event") or {})
        else:
            session_start_call_count = len(self.calls)
            scene_history_start = len(app.scene_manager.history)
            resource_before = self._party_resource_snapshot()
            if spec.number == 1:
                # The explicit player confirmation above naturally opens chapter 1.
                self._adopt_first_session_scene(spec)
            else:
                self.invoke(
                    f"第{spec.number:02d}场开启门控",
                    "POST",
                    "/v1/session/gate",
                    {
                        **self.common,
                        "status": "adventure",
                        "reason": spec.title,
                        "defer_scene_opening": True,
                    },
                )
                self._prepare_session_runtime(spec)
                self.invoke(
                    f"第{spec.number:02d}场 GM 强开场",
                    "POST",
                    "/v1/game/scene-opening",
                    {**self.common, "speaker": "时悠", "message": self._continuity_opening_prompt(spec)},
                )
            self._answer_pending_decisions(spec, 0)
            self.route_table_message(
                f"第{spec.number:02d}场玩家自由讨论静默",
                "南星",
                self._opening_table_prompt(spec, 0),
                expected_target="silent",
                expected_send_reply=False,
            )
            gm_beat_count = 0
            player_turn_count = 0
            routed_discussion_count = 1
            processed_player_turns = 0
            current_act = 1
            act_started_at_turn = 0
            last_assessment_turn = 0
            last_extension_gm_beat_turn = 0
            last_lane_refocus_signature = ""
            last_lane_refocus_turn = -100
            authoritative_resolution_at_turn = None
            assessment = SessionProgressAssessment()
            phase = "scripted"
            scripted_next_index = 1
            continuation_index = len(expanded_turns)
            self._pending_scene_transition = {}
            pending_table_event = {}

        def checkpoint(
            *,
            next_index: int,
            checkpoint_phase: str,
            pending_event: dict[str, Any] | None = None,
        ) -> None:
            self._write_in_progress_session_checkpoint(
                spec,
                phase=checkpoint_phase,
                scripted_next_index=next_index,
                continuation_index=continuation_index,
                session_start_call_count=session_start_call_count,
                scene_history_start=scene_history_start,
                resource_before=resource_before,
                gm_beat_count=gm_beat_count,
                player_turn_count=player_turn_count,
                routed_discussion_count=routed_discussion_count,
                processed_player_turns=processed_player_turns,
                current_act=current_act,
                act_started_at_turn=act_started_at_turn,
                last_assessment_turn=last_assessment_turn,
                last_extension_gm_beat_turn=last_extension_gm_beat_turn,
                last_lane_refocus_signature=last_lane_refocus_signature,
                last_lane_refocus_turn=last_lane_refocus_turn,
                authoritative_resolution_at_turn=authoritative_resolution_at_turn,
                assessment=assessment,
                pending_table_event=pending_event,
            )

        if not state:
            checkpoint(next_index=1, checkpoint_phase="scripted")

        if phase == "scripted":
            for index, (speaker, message) in enumerate(expanded_turns, start=1):
                if index < scripted_next_index:
                    continue
                if speaker == "__GM_IDLE__":
                    if self._recent_scene_opening_needs_player_space(
                        self.calls,
                        session_number=spec.number,
                    ):
                        checkpoint(next_index=index + 1, checkpoint_phase="scripted")
                        continue
                    self._answer_pending_decisions(spec, index)
                    result = self._session_gm_beat(spec, index, message)
                    if result.get("send_reply") or result.get("reply"):
                        gm_beat_count += 1
                    checkpoint(next_index=index + 1, checkpoint_phase="scripted")
                    continue
                if speaker == "__TABLE__":
                    self._answer_pending_decisions(spec, index)
                    if message == "__DYNAMIC_DISCUSSION__":
                        message = self._table_discussion_prompt(spec, index)
                    else:
                        message = self._simulate_table_discussion(
                            spec,
                            index,
                            scripted_message=message,
                        )
                    self.route_table_message(
                        f"第{spec.number:02d}场玩家自由讨论 {index:02d}",
                        "南星",
                        message,
                        expected_target="silent",
                        expected_send_reply=False,
                    )
                    routed_discussion_count += 1
                    checkpoint(next_index=index + 1, checkpoint_phase="scripted")
                    continue
                pending_for_turn = bool(
                    pending_table_event
                    and str(pending_table_event.get("phase") or "") == "scripted"
                    and int(pending_table_event.get("index") or 0) == index
                    and str(pending_table_event.get("kind") or "") == "player_action"
                )
                if pending_for_turn:
                    speaker = str(pending_table_event.get("speaker") or speaker)
                    message = str(pending_table_event.get("message") or message)
                    simulated_fallback_kind = str(
                        pending_table_event.get("fallback_kind") or ""
                    )
                else:
                    player_turn_count += 1
                    processed_player_turns += 1
                    simulated_fallback_kind = ""
                    if message == "__SIMULATE__":
                        speaker = self._preferred_open_condition_speaker(speaker)
                        refocus = self._refocus_saturated_action_lane(
                            spec,
                            index=index,
                            player_turn_count=player_turn_count,
                            last_signature=last_lane_refocus_signature,
                            last_refocus_turn=last_lane_refocus_turn,
                        )
                        if refocus is not None:
                            last_lane_refocus_signature = str(refocus["signature"])
                            last_lane_refocus_turn = player_turn_count
                            if refocus["result"].get("send_reply") or refocus["result"].get("reply"):
                                gm_beat_count += 1
                                last_extension_gm_beat_turn = player_turn_count
                        message = self._simulate_player_turn(
                            spec,
                            speaker,
                            index,
                            current_act=current_act,
                        )
                        simulated_fallback_kind = str(
                            (self.player_simulation_metrics[-1] or {}).get(
                                "fallback_kind", ""
                            )
                        )
                    pending_table_event = {
                        "phase": "scripted",
                        "kind": "player_action",
                        "index": index,
                        "speaker": speaker,
                        "message": message,
                        "fallback_kind": simulated_fallback_kind,
                    }
                    checkpoint(
                        next_index=index,
                        checkpoint_phase="scripted",
                        pending_event=pending_table_event,
                    )
                expected_target, expected_send_reply = self._player_route_expectation(
                    simulated_fallback_kind,
                    speaker=speaker,
                )
                routed = self.route_table_message(
                    f"第{spec.number:02d}场行动 {index:02d} {speaker}",
                    speaker,
                    message,
                    expected_target=expected_target,
                    expected_send_reply=expected_send_reply,
                    tolerate_route_mismatch=(
                        simulated_fallback_kind == "exhaustion_safe_pass"
                    ),
                )
                self._answer_agent_clarification(
                    spec,
                    index,
                    speaker=speaker,
                    actor=self._hero_for_speaker(speaker),
                    body=routed,
                )
                self._answer_pending_decisions(spec, index)
                pending_table_event = {}
                if processed_player_turns >= 8 and processed_player_turns - last_assessment_turn >= 4:
                    assessment = self._evaluate_session_progress(
                        spec,
                        session_start_call_count=session_start_call_count,
                        player_turn_count=player_turn_count,
                        scene_history_start=scene_history_start,
                    )
                    last_assessment_turn = processed_player_turns
                    next_act = self._advance_session_act_if_earned(
                        spec,
                        current_act,
                        assessment,
                        turns_in_act=player_turn_count - act_started_at_turn,
                    )
                    if next_act != current_act:
                        current_act = next_act
                        act_started_at_turn = player_turn_count
                checkpoint(
                    next_index=index + 1,
                    checkpoint_phase="scripted",
                    pending_event=None,
                )

            phase = "continuation"
            continuation_index = len(expanded_turns)
            last_extension_gm_beat_turn = player_turn_count
            checkpoint(next_index=len(expanded_turns) + 1, checkpoint_phase="continuation")

        # A four-hour session ends when play earns an ending, not when the
        # authored outline runs out. Keep playing semantic, public-information
        # turns until the local dramatic question changes or resolves.
        speaker_cycle = ["阿凛", "南星", "白河", "时雨", "澄砚"]
        completion_reasons: list[str] = []
        can_end = False
        absolute_turn_limit = self.max_table_turns_per_session
        closure_grace_turn_limit = absolute_turn_limit + 4
        closure_grace_active = False
        closure_commit_turn = max(
            self.min_table_turns_per_session + 4,
            self.max_table_turns_per_session - 6,
        )
        while True:
            pending_continuation_action = bool(
                pending_table_event
                and str(pending_table_event.get("phase") or "") == "continuation"
                and str(pending_table_event.get("kind") or "") == "player_action"
            )
            if pending_continuation_action:
                continuation_index = int(
                    pending_table_event.get("index") or continuation_index
                )
                speaker = str(pending_table_event.get("speaker") or "玩家")
                message = str(pending_table_event.get("message") or "")
                fallback_kind = str(
                    pending_table_event.get("fallback_kind") or ""
                )
                expected_target, expected_send_reply = self._player_route_expectation(
                    fallback_kind,
                    speaker=speaker,
                )
                routed = self.route_table_message(
                    f"第{spec.number:02d}场延伸行动 {continuation_index:02d} {speaker}",
                    speaker,
                    message,
                    expected_target=expected_target,
                    expected_send_reply=expected_send_reply,
                    tolerate_route_mismatch=(
                        fallback_kind == "exhaustion_safe_pass"
                    ),
                )
                self._answer_agent_clarification(
                    spec,
                    continuation_index,
                    speaker=speaker,
                    actor=self._hero_for_speaker(speaker),
                    body=routed,
                )
                self._answer_pending_decisions(spec, continuation_index)
                player_turn_count += 1
                processed_player_turns += 1
                pending_table_event = {}
                checkpoint(
                    next_index=len(expanded_turns) + 1,
                    checkpoint_phase="continuation",
                    pending_event=None,
                )
                if processed_player_turns % 3 == 0:
                    self._answer_pending_decisions(spec, continuation_index)
                    discussion = self._table_discussion_prompt(
                        spec,
                        continuation_index,
                    )
                    self.route_table_message(
                        f"第{spec.number:02d}场玩家自由讨论 {continuation_index:02d}",
                        "南星",
                        discussion,
                        expected_target="silent",
                        expected_send_reply=False,
                    )
                    routed_discussion_count += 1
                    checkpoint(
                        next_index=len(expanded_turns) + 1,
                        checkpoint_phase="continuation",
                    )
                continue
            assessment = self._evaluate_session_progress(
                spec,
                session_start_call_count=session_start_call_count,
                player_turn_count=player_turn_count,
                scene_history_start=scene_history_start,
            )
            transition_before = dict(
                getattr(self, "_pending_scene_transition", {}) or {}
            )
            next_act = self._advance_session_act_if_earned(
                spec,
                current_act,
                assessment,
                turns_in_act=player_turn_count - act_started_at_turn,
            )
            transition_after = dict(
                getattr(self, "_pending_scene_transition", {}) or {}
            )
            transition_offer_just_published = self._transition_offer_became_public(
                transition_before,
                transition_after,
            )
            act_just_changed = next_act != current_act
            if act_just_changed:
                current_act = next_act
                act_started_at_turn = self._act_started_at_turn_after_sync(
                    next_act=current_act,
                    player_turn_count=player_turn_count,
                    transition_before=transition_before,
                )
                # The scene opening generated by the transition is already a
                # proactive GM beat. Do not stack a heartbeat directly on it.
                last_extension_gm_beat_turn = player_turn_count
            if transition_offer_just_published:
                # Publishing a route is a GM hand-off, not an ending.  Even at
                # the strict turn cap the addressed player must receive one
                # real action slot to accept the move or choose to remain.
                closure_grace_active = True
                last_extension_gm_beat_turn = player_turn_count
                checkpoint(
                    next_index=len(expanded_turns) + 1,
                    checkpoint_phase="continuation",
                )
                continue
            feedback = self._build_session_feedback(
                spec,
                assessment,
                session_start_call_count=session_start_call_count,
                player_turn_count=player_turn_count,
                scene_history_start=scene_history_start,
                resource_before=resource_before,
            )
            can_end, completion_reasons = app.campaign_pacing_manager.assess_session_completion(feedback)
            earned_memory = assessment.memory_anchor_complete
            episode = app.story_arc_manager.state.current_session_progress
            authoritative_resolution = bool(episode.local_question_resolved)
            if authoritative_resolution and authoritative_resolution_at_turn is None:
                authoritative_resolution_at_turn = player_turn_count
            turns_after_authoritative_resolution = (
                max(0, player_turn_count - authoritative_resolution_at_turn)
                if authoritative_resolution_at_turn is not None
                else 0
            )
            route_waiting_for_players = self._public_transition_awaits_player_response(
                spec,
                current_act=current_act,
                turns_in_act=player_turn_count - act_started_at_turn,
            )
            if not assessment.scene_topology_ok:
                completion_reasons = [
                    *completion_reasons,
                    "本场尚未实际形成至少三种功能场景和可辨认的镜头变化",
                ]
            if not route_waiting_for_players and self._session_has_earned_fictional_ending(
                current_act=current_act,
                turns_in_closure=player_turn_count - act_started_at_turn,
                pacing_can_end=can_end,
                authoritative_resolution=authoritative_resolution,
                memory_anchor_complete=earned_memory,
                pending_blocking_decisions=feedback.pending_blocking_decision_count,
                turns_after_authoritative_resolution=turns_after_authoritative_resolution,
            ):
                app.campaign_pacing_manager.record_feedback(feedback)
                break
            pending_npc_response = app.scene_frame_manager.latest_pending_npc_question()
            if player_turn_count >= absolute_turn_limit and not closure_grace_active:
                # The normal turn budget is a cue for one last GM-led closure
                # window, not an immediate exception.  Otherwise a resumed run
                # that reached the cap exits before the GM can answer the last
                # player proposal or make the already-earned consequence land.
                closure_grace_active = True

            continuation_index += 1
            turns_since_gm_beat = player_turn_count - last_extension_gm_beat_turn
            grace_resolution_beat = bool(
                closure_grace_active
                and not authoritative_resolution
                and player_turn_count > last_extension_gm_beat_turn
            )
            should_gm_beat = bool(
                not route_waiting_for_players
                and (
                grace_resolution_beat
                or (
                # A proactive GM beat should leave enough table space for the
                # whole party to react. Triggering again after only two player
                # messages made one obstacle reproduce as a new signal, flag
                # or warning before the group could meaningfully address it.
                turns_since_gm_beat >= 4
                and (
                    (assessment.repeated_loop_detected and current_act < 4)
                    or player_turn_count >= closure_commit_turn
                    or (turns_since_gm_beat >= 4 and continuation_index % 5 == 0)
                )
                )
                )
            )
            gm_beat_attempted = False
            gm_beat_produced_public_reply = False
            if should_gm_beat:
                gm_beat_attempted = True
                self._answer_pending_decisions(spec, continuation_index)
                if grace_resolution_beat and pending_npc_response is not None:
                    need = (
                        "【待答复后的收束】玩家已经回应NPC刚才明确提出的问题。"
                        "只让该NPC或当前局面处理这份答复及其直接后果；不要另开线索、新敌人或新任务。"
                        "若答复足以改变本场核心问题，就兑现局部结果并把标志画面的变化落到现场。"
                    )
                elif grace_resolution_beat:
                    need = (
                        "【最终收束窗口】本场已经用完常规桌面时间。不要再提出条件、线索、敌人或任务；"
                        "只让当前对立方或现场人物兑现已经成熟的后果，直接回答仍悬而未决的提议，"
                        "并把玩家已经作出的选择变成可见结果。不要替玩家作新的选择。"
                    )
                elif current_act >= 4:
                    need = (
                        "【余波收束】只兑现本场已经发生的结果、代价与人物反应；不要开启新线索、新敌人或新任务。"
                        f"当前仍缺的收束证据是：{assessment.next_gm_need or '让标志画面因玩家选择出现可见变化。'}"
                    )
                elif player_turn_count >= closure_commit_turn:
                    if current_act >= 3:
                        need = (
                            "【高潮提交】本场已接近桌面时限。不要开启新线索或新任务；让当前冲突或核心问题"
                            "因已经公开的证据、玩家选择和对立方目标得到明确答案或不可逆地改变，"
                            "并把标志画面的变化落到现场。不要替玩家作选择。"
                        )
                    else:
                        need = (
                            "【局势提交】本场已接近桌面时限。不要开启新线索或新任务；让对立方立即完成一次明确行动，"
                            "兑现玩家先前选择的局部结果，并把局面推入必须回应的取舍或正面对决。"
                        )
                elif assessment.repeated_loop_detected:
                    need = (
                        "【局势提交】当前镜头已经重复。不要再给警告或让玩家重查同一对象；"
                        "让对立方、NPC或环境完成一个会改变现场的动作，把局面推入必须回应的取舍或正面对决。"
                    )
                else:
                    need = assessment.next_gm_need or "让当前局面产生一个可见变化，并给等待答复的NPC一句明确答复。"
                result = self._session_gm_beat(spec, continuation_index, need)
                if result.get("send_reply") or result.get("reply"):
                    gm_beat_count += 1
                    gm_beat_produced_public_reply = True
                    last_extension_gm_beat_turn = player_turn_count
            if gm_beat_produced_public_reply:
                # A GM beat may resolve the dramatic question or earn a scene
                # transition. Re-evaluate that authoritative state before
                # asking the simulator for another player action; otherwise a
                # completed threat can be accidentally continued for one more
                # turn merely because the old act pointer is stale.
                checkpoint(
                    next_index=len(expanded_turns) + 1,
                    checkpoint_phase="continuation",
                )
                continue
            resolved_table_response_limit = closure_grace_turn_limit + (
                2 if authoritative_resolution else 0
            )
            if player_turn_count >= resolved_table_response_limit:
                # At the hard cap the final GM resolution beat gets the first
                # chance to commit a success, failure, or costly outcome. Only
                # fail after that beat was attempted. Once it resolves the
                # question, reserve at most two player slots for accepting the
                # aftermath transition and reacting to the ending.
                app.campaign_pacing_manager.record_feedback(feedback)
                self.session_progress_assessments[spec.number] = assessment
                self.session_completion_results[spec.number] = {
                    "earned": False,
                    "continued": True,
                    "reasons": list(completion_reasons),
                    "player_turns": player_turn_count,
                    "act": current_act,
                    "pending_blocking_decisions": feedback.pending_blocking_decision_count,
                }
                raise RuntimeError(
                    f"第{spec.number:02d}场在 {player_turn_count} 条玩家行动后仍未赢得收束；"
                    "测试拒绝静默切到下一场："
                    + "；".join(completion_reasons or ["局面尚未赢得收束"])
                )
            default_speaker = speaker_cycle[player_turn_count % len(speaker_cycle)]
            speaker = self._preferred_npc_followup_speaker(default_speaker)
            speaker = self._preferred_open_condition_speaker(speaker)
            refocus = None
            if not gm_beat_attempted:
                refocus = self._refocus_saturated_action_lane(
                    spec,
                    index=continuation_index,
                    player_turn_count=player_turn_count,
                    last_signature=last_lane_refocus_signature,
                    last_refocus_turn=last_lane_refocus_turn,
                )
            if refocus is not None:
                last_lane_refocus_signature = str(refocus["signature"])
                last_lane_refocus_turn = player_turn_count
                if refocus["result"].get("send_reply") or refocus["result"].get("reply"):
                    gm_beat_count += 1
                    last_extension_gm_beat_turn = player_turn_count
            message = self._simulate_player_turn(
                spec,
                speaker,
                continuation_index,
                current_act=current_act,
            )
            fallback_kind = str(
                (self.player_simulation_metrics[-1] or {}).get("fallback_kind", "")
            )
            pending_table_event = {
                "phase": "continuation",
                "kind": "player_action",
                "index": continuation_index,
                "speaker": speaker,
                "message": message,
                "fallback_kind": fallback_kind,
            }
            checkpoint(
                next_index=len(expanded_turns) + 1,
                checkpoint_phase="continuation",
                pending_event=pending_table_event,
            )
            expected_target, expected_send_reply = self._player_route_expectation(
                fallback_kind,
                speaker=speaker,
            )
            routed = self.route_table_message(
                f"第{spec.number:02d}场延伸行动 {continuation_index:02d} {speaker}",
                speaker,
                message,
                expected_target=expected_target,
                expected_send_reply=expected_send_reply,
                tolerate_route_mismatch=(
                    fallback_kind == "exhaustion_safe_pass"
                ),
            )
            self._answer_agent_clarification(
                spec,
                continuation_index,
                speaker=speaker,
                actor=self._hero_for_speaker(speaker),
                body=routed,
            )
            self._answer_pending_decisions(spec, continuation_index)
            player_turn_count += 1
            processed_player_turns += 1
            pending_table_event = {}
            checkpoint(
                next_index=len(expanded_turns) + 1,
                checkpoint_phase="continuation",
                pending_event=None,
            )
            if processed_player_turns % 3 == 0:
                self._answer_pending_decisions(spec, continuation_index)
                discussion = self._table_discussion_prompt(spec, continuation_index)
                self.route_table_message(
                    f"第{spec.number:02d}场玩家自由讨论 {continuation_index:02d}",
                    "南星",
                    discussion,
                    expected_target="silent",
                    expected_send_reply=False,
                )
                routed_discussion_count += 1
                checkpoint(
                    next_index=len(expanded_turns) + 1,
                    checkpoint_phase="continuation",
                )

        self.session_progress_assessments[spec.number] = assessment
        episode = app.story_arc_manager.state.current_session_progress
        fictional_ending_earned = self._session_has_earned_fictional_ending(
            current_act=current_act,
            turns_in_closure=player_turn_count - act_started_at_turn,
            pacing_can_end=can_end,
            authoritative_resolution=bool(episode.local_question_resolved),
            memory_anchor_complete=assessment.memory_anchor_complete,
            pending_blocking_decisions=feedback.pending_blocking_decision_count,
            turns_after_authoritative_resolution=(
                max(0, player_turn_count - authoritative_resolution_at_turn)
                if authoritative_resolution_at_turn is not None
                else 0
            ),
        )
        self.session_completion_results[spec.number] = {
            "earned": bool(fictional_ending_earned),
            "continued": not bool(fictional_ending_earned),
            "reasons": list(completion_reasons),
            "player_turns": player_turn_count,
            "act": current_act,
            "pending_blocking_decisions": feedback.pending_blocking_decision_count,
        }
        if fictional_ending_earned:
            self._run_gm_stinger_if_needed(spec)
        ended = self.invoke(
            f"第{spec.number:02d}场收团",
            "POST",
            "/v1/session/end",
            {**self.common, "title": spec.title},
        )
        level_ups = self._apply_between_session_level_ups(spec, ended)
        audit = self.invoke(
            f"第{spec.number:02d}场审计",
            "GET",
            f"/v1/audit/dashboard?campaign_id={self.campaign_id}&session_id={self.session_id}&channel_id={self.channel_id}&include_private=true&limit=80",
            {},
        )
        session_calls = self.calls[session_start_call_count:]
        self.session_table_metrics[spec.number] = self._session_table_metrics(
            spec,
            session_calls,
            player_turn_count=player_turn_count,
            gm_beat_count=gm_beat_count,
            routed_discussion_count=routed_discussion_count,
        )
        scene_records = app.scene_manager.history[scene_history_start:]
        self.session_scene_metrics[spec.number] = self._session_scene_metric(spec, scene_records)
        resource_after = self._party_resource_snapshot()
        report = self._session_report(
            spec,
            ended,
            audit,
            level_ups=level_ups,
            resource_before=resource_before,
            resource_after=resource_after,
        )
        self.session_reports.append(report)
        self._previous_session_summary = str(report.get("summary") or "")

    def _progress_evaluator(self) -> SessionProgressEvaluator:
        if self._session_progress_evaluator is not None:
            return self._session_progress_evaluator
        core_gm = self.service.gm_tool_agent
        core_client = getattr(core_gm, "client", None)
        model = str(getattr(core_gm, "model", "") or LLMConfig.from_env().action_model)
        client = core_client
        if isinstance(core_client, OpenAICompatibleClient):
            # Quality auditing is deliberately non-authoritative and uses a
            # much larger transcript than one GM turn.  A timeout here must
            # degrade the audit to its deterministic fallback, not open the
            # core GM's provider circuit and silence the next table beat.
            self._session_progress_client = OpenAICompatibleClient(
                core_client.config,
                transport=core_client.transport,
                circuit_breaker_enabled=True,
                circuit_failure_threshold=1,
                circuit_cooldown_seconds=30.0,
                circuit_max_cooldown_seconds=120.0,
            )
            client = self._session_progress_client
        self._session_progress_evaluator = SessionProgressEvaluator(client=client, model=model)
        return self._session_progress_evaluator

    def _evaluate_session_progress(
        self,
        spec: CampaignSessionSpec,
        *,
        session_start_call_count: int,
        player_turn_count: int,
        scene_history_start: int,
    ) -> SessionProgressAssessment:
        app = self._runtime().app
        calls = self.calls[session_start_call_count:]
        transcript = self._public_session_transcript(calls)
        scene_records = self._substantial_session_scene_records(scene_history_start)
        scene_count = len(scene_records)
        contract = app.story_arc_manager.state.current_pacing_plan.dramatic_contract
        scene_roles = self._scene_roles_for_records(contract, scene_records)
        scene_locations = [str(record.location or "").strip() for record in scene_records]
        assessment = self._progress_evaluator().evaluate(
            transcript=transcript,
            contract=contract,
            meaningful_turns=player_turn_count,
            scene_count=scene_count,
            scene_roles=scene_roles,
            scene_locations=scene_locations,
            scene_names=[str(record.name or "").strip() for record in scene_records],
            previous_memory_anchors=self._recent_memory_anchors(spec.number),
            authoritative_progress=app.story_arc_manager.state.current_session_progress,
        )
        assessment = self._progress_evaluator().merge_cumulative(
            self.session_progress_assessments.get(spec.number),
            assessment,
        )
        functional_roles = {
            role for role in scene_roles if role and role != "unclassified"
        }
        distinct_locations = {location for location in scene_locations if location}
        camera_signatures = {
            (
                str(role or "unclassified"),
                str(location or ""),
                str(getattr(record, "name", "") or ""),
            )
            for role, location, record in zip(scene_roles, scene_locations, scene_records)
        }
        assessment.distinct_functional_scene_count = len(functional_roles)
        assessment.distinct_location_count = len(distinct_locations)
        assessment.distinct_camera_count = len(camera_signatures)
        assessment.scene_topology_ok = bool(
            assessment.scene_topology_ok
            and len(functional_roles) >= 3
            and len(camera_signatures) >= 3
        )
        self.session_progress_assessments[spec.number] = assessment
        if assessment.used_fallback and self.semantic_llm:
            marker = f"第{spec.number:02d}场语义进展审计降级"
            if not any(marker in error for error in self.errors):
                self.errors.append(f"{marker}：{assessment.model_error or 'unknown error'}")
        self._record_tool_event(
            "本场实录进展审计",
            f"第{spec.number:02d}场·{player_turn_count}行动",
            "只依据玩家实际看见的对话判断转折、收束和记忆锚点，不以预设标题或固定轮数代替实际进展。",
            asdict(assessment),
        )
        return assessment

    def _recent_memory_anchors(self, session_number: int) -> list[dict[str, str]]:
        anchors: list[dict[str, str]] = []
        for number in sorted(self.session_progress_assessments):
            if number >= session_number:
                continue
            assessment = self.session_progress_assessments[number]
            if not assessment.memory_anchor_complete:
                continue
            anchors.append(
                {
                    "session": str(number),
                    "image": assessment.memory_image,
                    "choice": assessment.memory_choice,
                    "consequence": assessment.memory_consequence,
                }
            )
        return anchors[-3:]

    def _advance_session_act_if_earned(
        self,
        spec: CampaignSessionSpec,
        current_act: int,
        assessment: SessionProgressAssessment,
        *,
        turns_in_act: int = 0,
    ) -> int:
        app = self._runtime().app
        episode = app.story_arc_manager.state.current_session_progress
        minimum_turns = {1: 5, 2: 7, 3: 6}.get(current_act, 2)
        if turns_in_act < minimum_turns and not episode.local_question_resolved:
            return current_act
        if self._latest_world_action_is_unanswered():
            return current_act
        prepared_next_act = int(current_act) + 1
        if prepared_next_act <= 4 and self._synchronize_active_scene_act(
            spec,
            prepared_next_act,
        ):
            # The production scene is authoritative. Do not make its matching
            # test act wait for the old scene's evidence to recommend a move
            # that the player has already completed.
            return prepared_next_act
        if self._retry_unannounced_scene_transition(
            spec,
            current_act=current_act,
            assessment=assessment,
            turns_in_act=turns_in_act,
        ):
            return current_act
        scene_progress_map = getattr(episode, "scene_progress", {}) or {}
        active_scene_progress = scene_progress_map.get(
            str(getattr(episode, "active_scene_id", "") or "")
        )
        authoritative_question_resolved = bool(episode.local_question_resolved)
        authoritative_cliffhanger = bool(episode.deliberate_cliffhanger)
        frame = app.scene_frame_manager.current_frame
        pending_scene_commitments = (
            app.scene_frame_manager.pending_settled_exchanges(frame)
            if frame is not None
            else []
        )
        unresolved_scene_condition = bool(
            frame
            and (
                any(
                    str(item.get("status") or "open") == "open"
                    for item in frame.open_conditions
                )
                or pending_scene_commitments
                or app.scene_frame_manager.npc_deferred_commitment_manager.pending(frame)
            )
        )
        decision = app.campaign_pacing_manager.closure_policy.recommend_act(
            current_act=current_act,
            evidence=SessionActEvidence(
                stage=assessment.stage,
                scene_change_recommended=bool(
                    assessment.scene_change_recommended
                    or (
                        current_act == 1
                        and episode.concrete_consequences
                        and episode.opposition_moves
                    )
                ),
                local_question_changed=assessment.local_question_changed,
                # The semantic evaluator can recommend where the session is
                # heading, but it cannot by itself close the dramatic question.
                # A committed rules result or independently audited GM beat must
                # first put the same fact into the authoritative episode state.
                local_question_resolved=bool(
                    assessment.local_question_resolved
                    and authoritative_question_resolved
                ),
                deliberate_cliffhanger=bool(
                    assessment.deliberate_cliffhanger
                    and authoritative_cliffhanger
                ),
                reversal_reached=assessment.reversal_reached,
                concrete_consequence=assessment.concrete_consequence,
                npc_answer_complete=assessment.npc_answer_complete,
                opposition_move_present=assessment.opposition_move_present,
                local_payoff_present=assessment.local_payoff_present,
                repeated_loop_detected=assessment.repeated_loop_detected,
                unresolved_scene_condition=unresolved_scene_condition,
                scene_evidence_available=active_scene_progress is not None,
                current_scene_player_actions=(
                    active_scene_progress.player_actions
                    if active_scene_progress is not None
                    else 0
                ),
                current_scene_material_change=bool(
                    active_scene_progress
                    and active_scene_progress.material_changes
                ),
                current_scene_local_outcome=bool(
                    active_scene_progress
                    and active_scene_progress.has_local_outcome
                ),
                current_scene_opposition_move=bool(
                    active_scene_progress
                    and active_scene_progress.opposition_moves
                ),
                current_scene_reveal=bool(
                    active_scene_progress
                    and active_scene_progress.reveals
                ),
                current_scene_reversal=bool(
                    active_scene_progress
                    and active_scene_progress.reversal_reached
                ),
                current_scene_core_resolution=bool(
                    active_scene_progress
                    and (
                        active_scene_progress.local_question_resolved
                        or (
                            assessment.deliberate_cliffhanger
                            and active_scene_progress.local_question_changed
                        )
                    )
                ),
            ),
            has_blocking_decision=bool(
                app.interceptor.decision_window_manager.pending(blocking_only=True)
            ),
        )
        if decision.advance:
            # Player-owned movement can open the prepared functional scene before
            # the pacing evaluator catches up.  In that case the new camera is
            # already authoritative; opening another scene here would replay the
            # move (and can even reselect a different prepared location after the
            # public context has changed).
            if self._synchronize_active_scene_act(spec, decision.next_act):
                return decision.next_act
            # A prepared opportunity can tell the GM what sort of situation
            # might be useful next.  It can never teleport the party there.
            # If the next act needs a different physical place, first make the
            # route public in the current scene and wait for a player-owned
            # cross-scene movement to create the anchor.
            if self._offer_player_led_scene_transition(
                spec,
                current_act=current_act,
                next_act=decision.next_act,
                assessment=assessment,
                turns_in_act=turns_in_act,
            ):
                return current_act
            transition_request = dict(
                getattr(self, "_pending_scene_transition", {}) or {}
            )
            in_place_transition = bool(transition_request.get("continue_in_place"))
            self._pending_scene_transition = {}
            self._transition_session_scene(
                spec,
                decision.next_act,
                assessment=assessment,
                in_place=in_place_transition,
                prepared_opportunity_key=str(
                    transition_request.get("prepared_opportunity_key") or ""
                ),
            )
            self._record_tool_event(
                "单场场景生命周期",
                f"第{spec.number:02d}场·第{current_act}幕",
                "只在公开实录已经产生实质变化后换镜头；固定轮数不触发转场。",
                asdict(decision),
            )
            return decision.next_act
        return current_act

    def _synchronize_active_scene_act(
        self,
        spec: CampaignSessionSpec,
        next_act: int,
    ) -> bool:
        if not self._active_scene_represents_act(spec, next_act):
            return False
        arrived = getattr(self._runtime().app.scene_manager, "current_scene", None)
        pending = dict(getattr(self, "_pending_scene_transition", {}) or {})
        if arrived is not None:
            if not str(getattr(arrived, "session_opportunity_key", "") or "").strip():
                arrived.session_opportunity_key = str(
                    pending.get("prepared_opportunity_key") or ""
                ).strip()
            if not str(getattr(arrived, "session_opportunity_role", "") or "").strip():
                arrived.session_opportunity_role = str(
                    pending.get("prepared_opportunity_role") or ""
                ).strip()
        self._pending_scene_transition = {}
        self._record_tool_event(
            "玩家转场已兑现",
            f"第{spec.number:02d}场·第{next_act}幕",
            "玩家行动已经打开下一功能场景；节奏器只同步幕次，不重复移动角色或重开场景。",
            {
                "scene_id": str(getattr(arrived, "scene_id", "") or ""),
                "scene_name": str(getattr(arrived, "name", "") or ""),
                "location": str(getattr(arrived, "location", "") or ""),
                "scene_role": str(
                    getattr(arrived, "session_opportunity_role", "") or ""
                ),
                "prepared_opportunity": str(
                    getattr(arrived, "session_opportunity_key", "") or ""
                ),
                "pending_transition": pending,
            },
        )
        return True

    @staticmethod
    def _transition_offer_became_public(
        before: dict[str, Any],
        after: dict[str, Any],
    ) -> bool:
        return bool(
            after.get("public_target_announced")
            and not before.get("public_target_announced")
        )

    def _act_started_at_turn_after_sync(
        self,
        *,
        next_act: int,
        player_turn_count: int,
        transition_before: dict[str, Any],
    ) -> int:
        """Count a resolved player move as the first aftermath response."""

        scene = getattr(self._runtime().app.scene_manager, "current_scene", None)
        player_entered_public_aftermath = bool(
            int(next_act) >= 4
            and transition_before.get("public_target_announced")
            and scene is not None
            and self._same_scene_location(
                str(getattr(scene, "location", "") or ""),
                str(transition_before.get("target_location") or ""),
            )
        )
        if player_entered_public_aftermath:
            return max(0, int(player_turn_count) - 1)
        return int(player_turn_count)

    def _retry_unannounced_scene_transition(
        self,
        spec: CampaignSessionSpec,
        *,
        current_act: int,
        assessment: SessionProgressAssessment,
        turns_in_act: int,
    ) -> bool:
        """Retry a failed route hand-off after the table has acted once.

        A provider timeout or the ordinary duplicate-beat guard can make the
        first authored route offer silent.  The destination remains private in
        that case, so FU-PL cannot legitimately choose it.  Retry at most once
        per subsequent player turn and keep the act pointer unchanged until a
        player actually moves or elects to remain.
        """

        pending = dict(getattr(self, "_pending_scene_transition", {}) or {})
        next_act = int(pending.get("next_act") or 0)
        if (
            int(pending.get("session_number") or 0) != int(spec.number)
            or int(pending.get("current_act") or 0) != int(current_act)
            or next_act <= int(current_act)
            or bool(pending.get("public_target_announced"))
            or not str(pending.get("target_location") or "").strip()
        ):
            return False
        last_attempt = max(0, int(pending.get("offered_at_turn_in_act") or 0))
        if int(turns_in_act) <= last_attempt:
            return False
        return bool(
            self._offer_player_led_scene_transition(
                spec,
                current_act=current_act,
                next_act=next_act,
                assessment=assessment,
                turns_in_act=turns_in_act,
            )
        )

    def _active_scene_represents_act(
        self,
        spec: CampaignSessionSpec,
        act_number: int,
    ) -> bool:
        """Recognize a scene already opened by a resolved player movement.

        The test's pacing act is bookkeeping, not authority over the camera.
        Production may focus/open the destination as soon as a player reaches
        it, so the harness must synchronize to that scene instead of replaying
        the prepared transition later.
        """

        app = self._runtime().app
        scene_manager = getattr(app, "scene_manager", None)
        scene = getattr(scene_manager, "current_scene", None)
        if scene is None or not bool(getattr(scene, "active", True)):
            return False
        expected_roles = {
            1: {"strong_start"},
            2: {"social_or_investigation", "alternate_approach"},
            3: {"climax_candidate"},
            4: {"aftermath"},
        }.get(int(act_number), set())
        role = str(getattr(scene, "session_opportunity_role", "") or "").strip()
        if role and role in expected_roles:
            return True

        pending = dict(getattr(self, "_pending_scene_transition", {}) or {})
        if (
            int(pending.get("session_number") or 0) != int(spec.number)
            or int(pending.get("next_act") or 0) != int(act_number)
        ):
            return False
        if (
            pending.get("public_target_announced")
            and self._same_scene_location(
                str(getattr(scene, "location", "") or ""),
                str(pending.get("target_location") or ""),
            )
        ):
            # The production scene tool does not know the test harness's
            # prepared-opportunity key. An exact move to the destination that
            # the GM already announced is enough to prove the player-owned
            # transition happened; requiring private test metadata here would
            # leave the act pointer behind the authoritative camera.
            return True
        prepared_key = str(pending.get("prepared_opportunity_key") or "").strip()
        scene_key = str(getattr(scene, "session_opportunity_key", "") or "").strip()
        return bool(prepared_key and scene_key and prepared_key == scene_key)

    @staticmethod
    def _session_has_earned_fictional_ending(
        *,
        current_act: int,
        turns_in_closure: int,
        pacing_can_end: bool,
        authoritative_resolution: bool,
        memory_anchor_complete: bool,
        pending_blocking_decisions: int,
        turns_after_authoritative_resolution: int = 0,
    ) -> bool:
        """Stop after an earned ending without padding the resolved fiction.

        Table-density and scene-topology targets remain reportable quality
        metrics. They must not force the simulator to reopen a local threat
        after an authoritative resolution has already entered the aftermath.
        One player-owned closure response is still required so a GM beat cannot
        end the session before the table has a chance to react.
        """

        if not memory_anchor_complete or int(pending_blocking_decisions or 0) != 0:
            return False
        if authoritative_resolution:
            return int(turns_after_authoritative_resolution or 0) >= 1
        return bool(
            current_act >= 4
            and turns_in_closure >= 1
            and pacing_can_end
        )

    def _public_transition_awaits_player_response(
        self,
        spec: CampaignSessionSpec,
        *,
        current_act: int,
        turns_in_act: int,
    ) -> bool:
        """Reserve table space after the GM names a route.

        Moving to the named destination resolves the hand-off immediately.
        Otherwise the existing two-action stay rule decides that the party is
        deliberately continuing in place. Until either happens, another GM
        beat would amount to answering the GM's own question.
        """

        pending = dict(getattr(self, "_pending_scene_transition", {}) or {})
        if (
            int(pending.get("session_number") or 0) != int(spec.number)
            or int(pending.get("current_act") or 0) != int(current_act)
            or not bool(pending.get("public_target_announced"))
        ):
            return False
        scene = getattr(self._runtime().app.scene_manager, "current_scene", None)
        if scene is not None and self._same_scene_location(
            str(getattr(scene, "location", "") or ""),
            str(pending.get("target_location") or ""),
        ):
            return False
        return not self._earned_in_place_scene_cut(
            pending,
            turns_in_act=turns_in_act,
        )

    @staticmethod
    def _same_scene_location(left: str, right: str) -> bool:
        """Compare camera locations without treating a shared building as equal.

        ``白花碑驿站·风铃廊`` and ``白花碑驿站·登记小室`` are nearby, but a
        player still has to choose to walk between them.  Only an exact public
        location may support an in-place camera change.
        """

        normalize = lambda value: re.sub(r"[\s，,。；;：:]+", "", str(value or ""))
        return bool(normalize(left) and normalize(left) == normalize(right))

    def _required_player_transition(
        self,
        spec: CampaignSessionSpec,
        *,
        next_act: int,
    ) -> dict[str, str] | None:
        """Return an unfulfilled move needed before opening a new location.

        This is intentionally a harness guard rather than a production plot
        rule.  Real play may stay in one scene forever; the test only needs to
        ensure that its authored opportunity list never moves PCs by fiat.
        """

        app = self._runtime().app
        scene_manager = getattr(app, "scene_manager", None)
        current_scene = getattr(scene_manager, "current_scene", None)
        if current_scene is None:
            return None
        if SceneTransitionCoordinator.anchor_for_scene(current_scene) is not None:
            return None

        existing = dict(getattr(self, "_pending_scene_transition", {}) or {})
        if (
            int(existing.get("session_number") or 0) == int(spec.number)
            and int(existing.get("next_act") or 0) == int(next_act)
            and str(existing.get("target_location") or "").strip()
        ):
            target_location = str(existing["target_location"]).strip()
            prepared_opportunity_key = str(
                existing.get("prepared_opportunity_key") or ""
            ).strip()
            prepared_opportunity_role = str(
                existing.get("prepared_opportunity_role") or ""
            ).strip()
        else:
            opportunity = self._scene_opportunity_for_act(spec, next_act)
            target_location = str(
                getattr(opportunity, "location", "") or self._scene_location_for_act(spec, next_act)
            ).strip()
            prepared_opportunity_key = str(
                getattr(opportunity, "scene_key", "") or ""
            ).strip()
            prepared_opportunity_role = str(
                getattr(opportunity, "scene_role", "") or ""
            ).strip()

        current_location = str(getattr(current_scene, "location", "") or "").strip()
        if not target_location or self._same_scene_location(current_location, target_location):
            return None
        return {
            "from_location": current_location,
            "target_location": target_location,
            "prepared_opportunity_key": prepared_opportunity_key,
            "prepared_opportunity_role": prepared_opportunity_role,
        }

    def _offer_player_led_scene_transition(
        self,
        spec: CampaignSessionSpec,
        *,
        current_act: int,
        next_act: int,
        assessment: SessionProgressAssessment,
        turns_in_act: int = 0,
    ) -> bool:
        """Offer, but never perform, the physical move to the next act."""

        required = self._required_player_transition(spec, next_act=next_act)
        if required is None:
            return False

        request = {
            "session_number": int(spec.number),
            "current_act": int(current_act),
            "next_act": int(next_act),
            **required,
        }
        previous = dict(getattr(self, "_pending_scene_transition", {}) or {})
        same_transition = bool(
            int(previous.get("session_number") or 0) == int(spec.number)
            and int(previous.get("current_act") or 0) == int(current_act)
            and int(previous.get("next_act") or 0) == int(next_act)
            and str(previous.get("target_location") or "").strip()
            == str(request.get("target_location") or "").strip()
        )
        if same_transition and bool(previous.get("public_target_announced")):
            if self._earned_in_place_scene_cut(previous, turns_in_act=turns_in_act):
                previous["continue_in_place"] = True
                self._pending_scene_transition = previous
                self._record_tool_event(
                    "玩家选择原地推进",
                    f"第{spec.number:02d}场·第{current_act}幕",
                    "GM 已公开另一处去路；英雄随后继续在当前地点完成了多次实质行动，"
                    "因此把这视为留在现场处理局面的选择，而不是重复催促转场。",
                    {
                        **previous,
                        "turns_in_act": int(turns_in_act),
                    },
                )
                return False
            # The route is already public. Repeating it would make the GM
            # sound like it is railroading the table.
            self._pending_scene_transition = previous
            return True

        result = self._session_gm_beat(
            spec,
            100 + int(next_act),
            (
                "【玩家主导转场】当前仍在【{from_location}】，尚未有人离开。"
                "不要切镜头、不要说任何角色已经抵达【{target_location}】。"
                "让当前现场的NPC、环境或已公开后果明确呈现一条现在可以选择的去路，"
                "如果这条去路通向下一地点，必须把地点全名【{target_location}】直接说给玩家；"
                "不能只说‘里面’、‘那边’、‘跟我来’，也不能把角色直接送达。"
                "并给出立即出发或继续留下各自看得见的代价；停在玩家决定是否真正移动的位置。"
                "不能替玩家出发，也不要为了转场新造一个无关任务。"
            ).format(**request),
        )
        public_reply = str(result.get("reply") or "").strip()
        target_is_public = bool(
            result.get("send_reply")
            and self._public_reply_names_transition_target(
                public_reply,
                str(request.get("target_location") or ""),
            )
        )
        request["public_target_announced"] = target_is_public
        request["public_route_evidence"] = public_reply[:500] if target_is_public else ""
        request["offered_at_turn_in_act"] = int(turns_in_act)
        self._pending_scene_transition = request
        self._record_tool_event(
            "玩家主导场景转场",
            f"第{spec.number:02d}场·第{current_act}幕",
            "准备场景只作为GM提出可见去路的参考；下一地点必须由公开、成功的玩家移动建立。",
            {
                **request,
                "assessment_need": str(assessment.next_gm_need or ""),
                "gm_reply": public_reply[:400],
            },
            public=bool(result.get("send_reply")),
        )
        return True

    @staticmethod
    def _earned_in_place_scene_cut(
        request: dict[str, Any],
        *,
        turns_in_act: int,
    ) -> bool:
        """Recognize meaningful play that chooses to stay after a public route.

        A long-running table does not need to restate "we refuse to leave".
        Once the GM has named a route and the players then spend two meaningful
        actions on the current problem, their continued play is an observable
        choice to remain.  This avoids railroading them toward a prepared room
        while still preventing one hesitant line from producing a fake scene
        break.
        """

        if not bool(request.get("public_target_announced")):
            return False
        offered_at = max(0, int(request.get("offered_at_turn_in_act") or 0))
        return int(turns_in_act) >= offered_at + 2

    @staticmethod
    def _public_reply_names_transition_target(reply: str, target_location: str) -> bool:
        """Require the GM to name a route before FU-PL may know it.

        A prepared scene is private harness material. A nearby room may be
        named naturally by its final segment, so accept that compact form, but
        inspect only the GM's public reply rather than a future act brief.
        """

        public = " ".join(str(reply or "").split())
        target = " ".join(str(target_location or "").split())
        if not public or not target:
            return False
        leaf = re.split(r"[·/＞>]+", target)[-1].strip()
        return bool(target in public or (len(leaf) >= 2 and leaf in public))

    def _latest_world_action_is_unanswered(self) -> bool:
        for call in reversed(self.calls):
            if str(call.get("route") or "") != "/v1/message/route":
                continue
            body = dict(call.get("body") or {})
            decision = dict(body.get("decision") or {})
            return bool(
                decision.get("target") == "fu_gm"
                and decision.get("world_response_required")
                and not str(call.get("reply") or "").strip()
            )
        return False

    def _build_session_feedback(
        self,
        spec: CampaignSessionSpec,
        assessment: SessionProgressAssessment,
        *,
        session_start_call_count: int,
        player_turn_count: int,
        scene_history_start: int,
        resource_before: dict[str, dict[str, Any]],
    ) -> SessionFeedbackSignals:
        app = self._runtime().app
        resource_now = self._party_resource_snapshot()
        spend_events = sum(
            1
            for name in self.pc_names
            for key in ("hp", "mp", "inventory_points", "fabula_points")
            if int(resource_now.get(name, {}).get(key, 0) or 0)
            < int(resource_before.get(name, {}).get(key, 0) or 0)
        )
        pressure_parts: list[float] = []
        for name in self.pc_names:
            before = resource_before.get(name, {})
            now = resource_now.get(name, {})
            for key, maximum_key in (
                ("hp", "max_hp"),
                ("mp", "max_mp"),
                ("inventory_points", "max_inventory_points"),
                ("fabula_points", ""),
            ):
                before_value = int(before.get(key, 0) or 0)
                now_value = int(now.get(key, 0) or 0)
                maximum = (
                    int(before.get(maximum_key, 0) or 0)
                    if maximum_key
                    else max(3, before_value)
                )
                pressure_parts.append(
                    max(0.0, float(before_value - now_value) / max(1, maximum))
                )
        calls = self.calls[session_start_call_count:]
        replies = [str(call.get("reply") or "").strip() for call in calls if str(call.get("reply") or "").strip()]
        repeated = sum(count - 1 for count in Counter(replies).values() if count > 1)
        active_threads = [
            thread
            for thread in app.story_arc_manager.state.threads
            if thread.status not in {"resolved", "abandoned"}
        ]
        previous = app.story_arc_manager.state.session_feedback_history[-1:] or []
        villain_moved = bool(
            assessment.opposition_move_present
            or app.story_arc_manager.state.current_session_progress.opposition_moves
        )
        prior_drought = previous[0].villain_drought_sessions if previous else 0
        episode = app.story_arc_manager.state.current_session_progress
        current_anchor = "|".join(
            (
                assessment.memory_image,
                assessment.memory_choice,
                assessment.memory_consequence,
            )
        )
        similarity = max(
            (
                self.conversation_quality_auditor._character_ngram_similarity(
                    "|".join((item["image"], item["choice"], item["consequence"])),
                    current_anchor,
                )
                for item in self._recent_memory_anchors(spec.number)
            ),
            default=0.0,
        )
        return SessionFeedbackSignals(
            session_number=spec.number,
            meaningful_turns=player_turn_count,
            scene_count=len(self._substantial_session_scene_records(scene_history_start)),
            resource_spend_events=spend_events,
            unresolved_thread_count=len(active_threads),
            villain_drought_sessions=0 if villain_moved else prior_drought + 1,
            reveal_uptake=1.0 if assessment.local_question_changed else (0.5 if assessment.evidence else 0.0),
            stalled_beats=repeated + int(not assessment.npc_answer_complete),
            foreground_pressure_count=len(app.campaign_pacing_manager.formatted_public_clocks()),
            choice_count=len(episode.player_choices),
            consequence_count=len(episode.concrete_consequences),
            villain_move_observed=(
                villain_moved or bool(episode.opposition_moves)
            ),
            reveal_understood=bool(
                assessment.local_question_changed or assessment.local_question_resolved
            ),
            resource_pressure_ratio=(
                sum(pressure_parts) / len(pressure_parts)
                if pressure_parts
                else 0.0
            ),
            local_question_changed=assessment.local_question_changed,
            local_question_resolved=bool(
                assessment.local_question_resolved
                and episode.local_question_resolved
            ),
            deliberate_cliffhanger=bool(
                assessment.deliberate_cliffhanger
                and episode.deliberate_cliffhanger
            ),
            reversal_reached=assessment.reversal_reached,
            memory_anchor_complete=assessment.memory_anchor_complete,
            session_identity_distinct=assessment.session_identity_distinct,
            cause_effect_linked=assessment.cause_effect_linked,
            gm_control_present=assessment.gm_control_present,
            npc_answer_complete=assessment.npc_answer_complete,
            player_agency_preserved=assessment.player_agency_preserved,
            signature_image_evolved=assessment.signature_image_evolved,
            local_payoff_present=assessment.local_payoff_present,
            previous_consequence_recalled=assessment.previous_consequence_recalled,
            memory_similarity_to_recent=similarity,
            pending_blocking_decision_count=len(
                app.interceptor.decision_window_manager.pending(blocking_only=True)
            ),
            pending_scene_commitment_count=len(
                app.scene_frame_manager.pending_settled_exchanges()
            )
            + len(
                app.scene_frame_manager.npc_deferred_commitment_manager.pending(
                    app.scene_frame_manager.current_frame
                )
            ),
            notes=[*assessment.evidence, assessment.unresolved_now][:4],
        )

    def _current_session_scene_count(self, scene_history_start: int) -> int:
        return len(self._current_session_scene_records(scene_history_start))

    def _current_session_scene_records(self, scene_history_start: int) -> list[Any]:
        app = self._runtime().app
        candidates = [
            *list(app.scene_manager.history[scene_history_start:]),
            *list(app.scene_manager.active_scenes()),
        ]
        records: list[Any] = []
        seen: set[str] = set()
        for record in candidates:
            key = str(
                getattr(record, "scene_id", "")
                or f"object:{id(record)}"
            ).strip()
            if key in seen:
                continue
            seen.add(key)
            records.append(record)
        return records

    def _substantial_session_scene_records(
        self,
        scene_history_start: int,
    ) -> list[Any]:
        """Return only cameras that produced authoritative play evidence."""

        records = self._current_session_scene_records(scene_history_start)
        progress = self._runtime().app.story_arc_manager.state.current_session_progress
        substantial_ids = set(progress.substantial_scene_ids)
        return [
            record
            for record in records
            if str(record.scene_id or record.name) in substantial_ids
        ]

    @staticmethod
    def _scene_roles_for_records(
        contract: SessionDramaticContract,
        records: list[Any],
    ) -> list[str]:
        roles: list[str] = []
        for record in records:
            roles.append(
                TwentySessionCampaignHarness._scene_role_for_record(contract, record)
            )
        return roles

    @staticmethod
    def _scene_role_for_record(
        contract: SessionDramaticContract,
        record: Any,
    ) -> str:
        explicit_role = str(
            getattr(record, "session_opportunity_role", "") or ""
        ).strip()
        if explicit_role:
            return explicit_role
        opportunity = TwentySessionCampaignHarness._match_scene_opportunity(
            contract,
            record,
        )
        return opportunity.scene_role if opportunity is not None else "unclassified"

    @staticmethod
    def _match_scene_opportunity(
        contract: SessionDramaticContract,
        record: Any,
    ) -> SessionSceneOpportunity | None:
        """Match an observed scene without treating a shared hub as identity.

        Several functional scenes may occur in one settlement. A title is
        authoritative; a location is only safe when exactly one prepared
        opportunity owns it.
        """

        explicit_key = str(
            getattr(record, "session_opportunity_key", "") or ""
        ).strip()
        if explicit_key:
            for item in contract.potential_scenes:
                if str(item.scene_key or "").strip() == explicit_key:
                    return item
        name = str(getattr(record, "name", "") or "")
        title_matches = [
            item
            for item in contract.potential_scenes
            if item.title and item.title in name
        ]
        if title_matches:
            return max(title_matches, key=lambda item: len(item.title))
        location = str(getattr(record, "location", "") or "")
        location_matches = [
            item
            for item in contract.potential_scenes
            if item.location and item.location == location
        ]
        return location_matches[0] if len(location_matches) == 1 else None

    @staticmethod
    def _public_session_transcript(calls: list[dict[str, Any]]) -> str:
        lines: list[str] = []
        for call in calls:
            message = str(call.get("message") or "").strip()
            reply = str(call.get("reply") or "").strip()
            if message and str(call.get("speaker") or "").strip():
                lines.append(f"{call.get('speaker')}：{message}")
            if reply:
                lines.append(f"时悠：{reply}")
        return "\n".join(lines)

    def _refresh_session_pacing(self, spec: CampaignSessionSpec) -> None:
        app = self._runtime().app
        plan = app.campaign_pacing_manager.refresh_plan(
            conflict_active=False,
            boss_scene=spec.boss_session,
            force_session_number=spec.number,
        )
        if self.scripted_identities:
            self._apply_session_identity(spec, plan)
        self._record_tool_event(
            "战役节奏控制",
            f"第{spec.number:02d}场",
            f"根据 {self.target_sessions} 场战役档位给本场设置节奏预算、反派压力和命刻上限。",
            asdict(plan),
        )

    def _apply_session_identity(self, spec: CampaignSessionSpec, plan: Any) -> None:
        if "（续）" in str(plan.dramatic_contract.title or ""):
            return
        identity = self.EPISODE_IDENTITIES.get(spec.number)
        if not identity:
            return
        location = self._session_location(spec)
        previous_anchor = self._recent_memory_anchors(spec.number)
        inherited_consequence = (
            str(previous_anchor[-1].get("consequence") or "").strip()
            if previous_anchor
            else ""
        )
        opening_disruption = str(identity["escalation"][0])
        if inherited_consequence:
            opening_disruption = (
                f"先让上一场后果在现场可见：{inherited_consequence}；随后，"
                f"{opening_disruption}"
            )
        contract = replace(
            plan.dramatic_contract,
            session_number=spec.number,
            title=f"第{spec.number:02d}场·{spec.title}",
            location=location,
            dramatic_question=str(identity["question"]),
            local_question_key=f"{spec.number:02d}:{spec.title}",
            opening_disruption=opening_disruption,
            signature_image=str(identity["image"]),
            spotlight_hero=self.pc_names[(spec.number - 1) % len(self.pc_names)],
            focus_thread=spec.title,
            opposition_goal=str(identity["opposition"]),
            dilemma=(
                "把至少两个都合理但代价不同的方向放到现场；"
                "玩家选择方法和路线，GM不预写唯一解。"
            ),
            reversal=str(identity["reversal"]),
            climax_type=(
                "多阶段首领、环境机制或不可回避的集体选择"
                if spec.boss_session
                else "由玩家实际方法决定的交涉、险境、仪式、追逐或冲突"
            ),
            closure_requirement=(
                f"本场结束前必须让“{identity['question']}”获得答案或发生不可逆改变；"
                "只得到下一条线索不能收团。"
            ),
            situation_facts=[
                f"本场发生在【{location}】。",
                f"对立方目标：{identity['opposition']}。",
                "上一场已经公开的选择与后果不可更改。",
            ],
            flexible_secrets=[
                f"可移动转折：{identity['reversal']}",
                "转折可附着于玩家真正调查或交涉触及的合适对象，但不得否定已公开事实。",
            ],
            escalation_ladder=list(identity["escalation"]),
            possible_payoffs=list(identity["payoff"]),
            stinger="先让本场结果落地，再以一个短画面显示长期后果；不要用新谜团淹没结局。",
            callback_seed=(
                f"本场开局前段必须用人物反应、地点变化或现实代价回收上一场后果：{inherited_consequence}"
                if inherited_consequence
                else f"下一场必须回收第{spec.number:02d}场的一项实际选择或后果。"
            ),
            inherited_consequence=inherited_consequence,
            memory_anchor=f"留下“{identity['image']}”相关画面、一个玩家选择与一个可追踪后果。",
            irreversible_change=(
                f"本场结束时，至少让以下一项成为无法假装没发生过的事实："
                f"{'；'.join(str(item) for item in identity['payoff'][:2])}。"
            ),
            ending_echo=f"结尾再次呈现“{identity['image']}”，但让它因英雄的实际选择改变状态。",
        )
        app = self._runtime().app
        contract = app.campaign_pacing_manager.contract_planner.rebuild_scene_opportunities(
            contract
        )
        plan.dramatic_contract = contract
        app.campaign_pacing_manager.adopt_dramatic_contract(contract)

    def _continuity_opening_prompt(self, spec: CampaignSessionSpec) -> str:
        contract = self._runtime().app.story_arc_manager.state.current_pacing_plan.dramatic_contract
        if "（续）" in str(contract.title or ""):
            return (
                f"这是上一场未收束局面的下一次真实开桌。承接已经发生的后果：{contract.inherited_consequence or contract.opening_disruption}\n"
                f"继续围绕这个尚未解决的问题主持：{contract.dramatic_question}\n"
                "先展示时间经过后现场具体改变了什么，再把决定权交还玩家；不要开启新的任务，也不要复述上场摘要。"
            )
        if not self._previous_session_summary:
            return spec.gm_opening
        return (
            f"上一场公开结果是：{self._previous_session_summary[-600:]}\n"
            f"{spec.gm_opening}\n"
            "请让开场可见地承接上一场的一个后果；不要复述这段摘要，也不要宣布玩家尚未做到的事。"
        )

    def _adopt_first_session_scene(self, spec: CampaignSessionSpec) -> None:
        app = self._runtime().app
        if app.scene_manager.current_scene is None:
            self._prepare_session_runtime(spec)
            return
        scene = app.scene_manager.current_scene
        opportunity = self._scene_opportunity_for_act(spec, 1, used_keys=set())
        title = opportunity.title if opportunity is not None else spec.title
        scene.name = f"第{spec.number:02d}场·场景1：{title}"
        scene.scene_type = self._scene_type_for_act(spec, 1)
        scene.location = (
            opportunity.location
            if opportunity is not None and opportunity.location
            else self._session_location(spec)
        )
        scene.participants = SceneCastCoordinator.compose(
            self.pc_names,
            opportunity=opportunity,
            established=scene.participants,
        )
        scene.objective = (
            opportunity.purpose
            if opportunity is not None and opportunity.purpose
            else "；".join(spec.expected_focus[:3]) or spec.title
        )
        scene.summary = (
            opportunity.situation
            if opportunity is not None and opportunity.situation
            else f"{spec.arc}：{spec.title}的强开场"
        )
        if opportunity is not None:
            scene.session_opportunity_key = str(opportunity.scene_key or "").strip()
            scene.session_opportunity_role = str(opportunity.scene_role or "").strip()
            scene.session_opportunity_title = str(opportunity.title or "").strip()
            scene.session_opportunity_purpose = str(opportunity.purpose or "").strip()
            scene.session_opportunity_situation = str(opportunity.situation or "").strip()

    def _transition_session_scene(
        self,
        spec: CampaignSessionSpec,
        act_number: int,
        *,
        assessment: SessionProgressAssessment | None = None,
        in_place: bool = False,
        prepared_opportunity_key: str = "",
    ) -> None:
        app = self._runtime().app
        current_scene = app.scene_manager.current_scene
        transition_anchor = SceneTransitionCoordinator.anchor_for_scene(
            current_scene
        )
        anchor_location = transition_anchor.location if transition_anchor is not None else ""
        current_location = str(
            getattr(current_scene, "location", "") or ""
        ).strip()
        if in_place:
            location = current_location or self._session_location(spec)
            opportunity = self._in_place_scene_opportunity(
                spec,
                act_number,
                location=location,
                prepared=self._session_opportunity_by_key(
                    spec,
                    prepared_opportunity_key,
                ),
            )
        else:
            opportunity = self._session_opportunity_by_key(
                spec,
                prepared_opportunity_key,
            )
            if opportunity is not None and anchor_location:
                if not self.session_scene_navigator.location_matches_anchor(
                    opportunity.location,
                    anchor_location,
                ):
                    opportunity = None
            if opportunity is None:
                opportunity = self._scene_opportunity_for_act(
                    spec,
                    act_number,
                    location_anchor=anchor_location,
                )
            if anchor_location:
                location = anchor_location
            elif opportunity is not None and opportunity.location:
                location = opportunity.location
            else:
                location = self._scene_location_for_act(spec, act_number)
        scene_participants = SceneCastCoordinator.compose(
            self.pc_names,
            opportunity=opportunity,
            established=self._scene_transition_participants(
                current_scene=current_scene,
                transition_anchor=transition_anchor,
                in_place=in_place,
            ),
        )
        scene_type = self._scene_type_for_act(spec, act_number)
        scene_title = (
            opportunity.title
            if opportunity is not None and opportunity.title
            else self._act_label(act_number)
        )
        scene_name = f"第{spec.number:02d}场·场景{act_number}：{scene_title}"
        if (
            transition_anchor is None
            and not in_place
            and not self._same_scene_location(current_location, location)
        ):
            raise RuntimeError(
                "长测拒绝替玩家跨场移动："
                f"当前在【{current_location or '未标记地点'}】，"
                f"但下一幕准备地点是【{location or '未标记地点'}】。"
                "必须先由玩家的公开移动建立转场锚点。"
            )
        objective = (
            opportunity.purpose
            if opportunity is not None and opportunity.purpose
            else "；".join(spec.expected_focus[:3]) or spec.title
        )
        self._close_active_play_scene(f"第{spec.number:02d}场第{act_number - 1}幕告一段落。")
        if scene_type == SceneType.DUNGEON:
            app.start_dungeon(
                scene_name,
                DungeonExploreMode.SCENE,
                location=location,
                danger_clocks={f"{spec.title}危险": 6},
                session_opportunity_key=opportunity.scene_key if opportunity is not None else "",
                session_opportunity_role=opportunity.scene_role if opportunity is not None else "",
                session_opportunity_title=opportunity.title if opportunity is not None else "",
                session_opportunity_purpose=opportunity.purpose if opportunity is not None else "",
                session_opportunity_situation=opportunity.situation if opportunity is not None else "",
            )
            if app.scene_manager.current_scene is not None:
                app.scene_manager.current_scene.participants = list(scene_participants)
                app.scene_manager.current_scene.objective = objective
        else:
            app.start_scene(
                scene_name,
                scene_type,
                location=location,
                participants=scene_participants,
                objective=objective,
                summary=(
                    opportunity.situation
                    if opportunity is not None and opportunity.situation
                    else f"{spec.arc}：{spec.title}第{act_number}幕"
                ),
                session_opportunity_key=opportunity.scene_key if opportunity is not None else "",
                session_opportunity_role=opportunity.scene_role if opportunity is not None else "",
                session_opportunity_title=opportunity.title if opportunity is not None else "",
                session_opportunity_purpose=opportunity.purpose if opportunity is not None else "",
                session_opportunity_situation=opportunity.situation if opportunity is not None else "",
            )
        if act_number == 3 and spec.boss_session:
            clock_name = f"{spec.title}：首领机制"
            if not app.clock_manager.exists(clock_name):
                app.clock_manager.add(
                    Clock(
                        name=clock_name,
                        max_segments=8,
                        current=1,
                        clock_type="boss",
                        stakes=f"{spec.title}的首领机制兑现。",
                        auto_advance="每轮结束推进1格",
                        pacing_weight=5,
                    )
                )
        if transition_anchor is not None:
            self._record_tool_event(
                "玩家选择转场锚点",
                f"第{spec.number:02d}场·场景{act_number}",
                "下一场地点服从已经由世界回应确认的玩家移动；不兼容的章节包候选保持未使用。",
                {
                    "location": transition_anchor.location,
                    "reason": transition_anchor.reason,
                    "participants": list(transition_anchor.participants),
                    "prepared_opportunity": opportunity.scene_key if opportunity is not None else "",
                },
            )
        elif in_place:
            self._record_tool_event(
                "原地功能场景切换",
                f"第{spec.number:02d}场·场景{act_number}",
                "英雄没有离开当前地点；场景功能随局势变化而切换，不把预设地点当作强制路线。",
                {
                    "location": location,
                    "scene_role": opportunity.scene_role if opportunity is not None else "",
                    "prepared_opportunity": opportunity.scene_key if opportunity is not None else "",
                },
            )
        self._pending_scene_transition = {}
        prompt = self._act_opening_prompt(
            spec,
            act_number,
            opportunity=opportunity,
            resolved_location=location,
            assessment=assessment,
            verified_results=self._verified_session_results(),
        )
        self.invoke(
            f"第{spec.number:02d}场场景{act_number}开场",
            "POST",
            "/v1/game/scene-opening",
            {**self.common, "speaker": "时悠", "message": prompt},
        )

    def _scene_transition_participants(
        self,
        *,
        current_scene: Any,
        transition_anchor: Any,
        in_place: bool,
    ) -> list[str]:
        """Keep only people whose presence survives the camera change.

        A functional cut at the same location does not make NPCs disappear.
        A physical transition may carry NPCs only when the resolved movement
        anchor names them.  Player characters remain part of this campaign
        simulation's shared party unless attendance says otherwise.
        """

        participants = list(self.pc_names)
        if in_place and current_scene is not None:
            participants.extend(list(getattr(current_scene, "participants", []) or []))
        elif transition_anchor is not None:
            participants.extend(list(getattr(transition_anchor, "participants", ()) or ()))
        return list(
            dict.fromkeys(
                str(name or "").strip()
                for name in participants
                if str(name or "").strip()
            )
        )

    def _close_active_play_scene(self, summary: str) -> None:
        app = self._runtime().app
        if app.conflict_manager.state.active:
            app.end_conflict_scene()
            return
        if app.dungeon_manager.state.active:
            app.end_dungeon(summary)
            return
        app.end_scene(summary)

    def _scene_type_for_act(self, spec: CampaignSessionSpec, act_number: int) -> SceneType:
        is_dungeon = spec.number in {4, 8, 12} or "dungeon" in spec.notes or "地下城" in "；".join(spec.expected_focus)
        if act_number == 1:
            if spec.number in {2, 7}:
                return SceneType.TRAVEL
            if spec.number == 16:
                return SceneType.REST
            return SceneType.STANDARD
        if act_number == 2:
            if is_dungeon:
                return SceneType.DUNGEON
            return SceneType.STANDARD
        if act_number == 3:
            if spec.boss_session or spec.number in {5, 10, 15, 18, 19, 20}:
                return SceneType.CONFLICT
            return SceneType.STANDARD
        if spec.number == 16 or spec.boss_session:
            return SceneType.INTERLUDE
        if spec.number in {2, 7}:
            return SceneType.REST
        return SceneType.STANDARD

    @staticmethod
    def _act_label(act_number: int) -> str:
        return {
            1: "强开场",
            2: "探索与升级",
            3: "反转与高潮",
            4: "余波与收束",
        }.get(act_number, f"第{act_number}幕")

    def _scene_location_for_act(self, spec: CampaignSessionSpec, act_number: int) -> str:
        opportunity = self._scene_opportunity_for_act(spec, act_number)
        if opportunity is not None and opportunity.location:
            return opportunity.location
        location = self._session_location(spec)
        if act_number == 1:
            return location
        if act_number == 2:
            if spec.number in {4, 8, 12}:
                return f"{location}深处"
            return location
        if act_number == 3 and spec.number in {4, 8, 12}:
            return f"{location}核心区"
        if act_number == 4 and spec.number in {2, 7}:
            return f"{location}背风营地"
        if act_number == 4 and spec.number in {4, 8, 12}:
            return f"{location}出口"
        return location

    def _act_opening_prompt(
        self,
        spec: CampaignSessionSpec,
        act_number: int,
        *,
        opportunity=None,
        resolved_location: str = "",
        assessment: SessionProgressAssessment | None = None,
        verified_results: list[str] | None = None,
    ) -> str:
        focus = "、".join(spec.expected_focus[:3]) or spec.title
        goal_instruction = f"本场目标清单是：{focus}；这些目标不代表已经完成。"
        actual_results = [
            " ".join(str(item or "").split()).strip()
            for item in (verified_results or [])
            if " ".join(str(item or "").split()).strip()
        ][:3]
        result_instruction = (
            "实录已经兑现的结果只有：" + "；".join(actual_results) + "。"
            if actual_results
            else "当前还没有可宣告为已经完成的本场结果。"
        )
        unresolved_instruction = ""
        if assessment is not None and str(assessment.unresolved_now or "").strip():
            unresolved_instruction = (
                f"实录审计认为仍未解决的是：{str(assessment.unresolved_now).strip()}。"
                "这只是防止提前收束的约束，不要把这句话念给玩家。"
            )
        if opportunity is None and not resolved_location:
            opportunity = self._scene_opportunity_for_act(spec, act_number)
        if resolved_location:
            next_location = resolved_location
        elif opportunity is not None and opportunity.location:
            next_location = opportunity.location
        else:
            next_location = self._scene_location_for_act(spec, act_number)
        base_location = self._session_location(spec)
        camera_instruction = f"把镜头切到【{next_location}】"
        opportunity_instruction = ""
        if opportunity is not None:
            opportunity_instruction = (
                f"本段可用局面是：{opportunity.situation}；它的作用是：{opportunity.purpose}。"
                "这不是固定剧情；若最近公开行动已改变前提，保留玩家造成的事实，调整局面后再开场。"
            )
        if act_number == 2:
            return (
                f"承接本场已经公开的行动结果，{camera_instruction}。"
                f"{goal_instruction}{result_instruction}{unresolved_instruction}"
                f"{opportunity_instruction}"
                "让紧迫事件围绕本场尚未完成的目标发生一项可见变化，并让相关NPC明确回应。"
                "把本段可调查的人、物件或异常现象明确放进现场，但场景开场本身不替玩家完成调查、仪式或交涉，"
                "也不把后台反转直接宣布为已经发现。若上一段已经公开了重要发现，只展示NPC或环境对此产生的新反应；"
                "若尚未发现，就让至少一条证据路径变得可以接触，并停在玩家能自行选择方法的位置。"
                "不要再增加一道同类核验手续，也不要为了拖延而换一个新物件重复同一问题。"
                "必须出现一个此前没有公开过的具体变化、决定或后果；"
                "不要复述玩家原话、上一段风景和NPC已有姿态，不要替玩家行动，也不要预先宣布结局。"
            )
        if act_number == 3:
            return (
                f"承接已经公开的转折，{camera_instruction}。"
                f"{goal_instruction}{result_instruction}{unresolved_instruction}"
                f"{opportunity_instruction}"
                "让尚未解决的本场问题进入不可回避的对决、险境或取舍；对立方必须按自己的目标行动，"
                "但结果取决于玩家接下来真正采用的方法。不要再开新的支线，也不要替玩家选边。"
            )
        return (
            f"根据本场至今真正发生的结果，{camera_instruction}。"
            f"{goal_instruction}{result_instruction}{unresolved_instruction}"
            f"{opportunity_instruction}"
            "只让上面列出的已兑现结果落地，并展示人物、地点或关系因此发生的余波；"
            "让玩家有机会回应结果、处理代价或决定下一步，但不要再追加新任务来拖延收束。"
            "标志性画面应因本场选择出现可见变化；保留长期暗线，不要把后台策划说给玩家。"
        )

    def _verified_session_results(self) -> list[str]:
        """Return committed outcomes, never planned focus labels."""

        episode = self._runtime().app.story_arc_manager.state.current_session_progress
        candidates = [
            *list(episode.concrete_consequences[-3:]),
            *list(episode.local_payoffs[-2:]),
        ]
        results: list[str] = []
        for item in candidates:
            clean = " ".join(str(item or "").split()).strip()
            if clean and clean not in results:
                results.append(clean[:500])
        return results[-3:]

    def _scene_opportunity_for_act(
        self,
        spec: CampaignSessionSpec,
        act_number: int,
        *,
        used_keys: set[str] | None = None,
        location_anchor: str = "",
    ):
        app = self._runtime().app
        contract = app.story_arc_manager.state.current_pacing_plan.dramatic_contract
        if used_keys is None:
            frame_manager = app.scene_frame_manager
            frames = [*frame_manager.history]
            if frame_manager.current_frame is not None:
                frames.append(frame_manager.current_frame)
            used_keys = {
                frame.session_opportunity_key
                for frame in frames
                if frame.session_title == contract.title and frame.session_opportunity_key
            }
        return self.session_scene_navigator.select(
            contract,
            act_number=act_number,
            used_keys=used_keys,
            scene_text=f"第{spec.number:02d}场·场景{act_number}：{self._act_label(act_number)}",
            # Opportunity selection needs campaign continuity.  Player simulation,
            # by contrast, only receives the current scene below.
            recent_context=self._recent_public_dialogue(limit=8, current_scene_only=False),
            location_anchor=location_anchor,
        )

    def _session_opportunity_by_key(
        self,
        spec: CampaignSessionSpec,
        scene_key: str,
    ):
        key = str(scene_key or "").strip()
        if not key:
            return None
        contract = self._runtime().app.story_arc_manager.state.current_pacing_plan.dramatic_contract
        for opportunity in contract.potential_scenes:
            if str(opportunity.scene_key or "").strip() == key:
                return opportunity
        return None

    def _in_place_scene_opportunity(
        self,
        spec: CampaignSessionSpec,
        act_number: int,
        *,
        location: str,
        prepared=None,
    ) -> SessionSceneOpportunity:
        """Reuse a prepared *function* without exposing its old location.

        Session opportunities are movable GM prep.  When the table deliberately
        stays put, only the dramatic role survives; a private alternate room,
        NPC placement, or clue route must not leak into the new scene opening.
        """

        if prepared is None:
            prepared = self._scene_opportunity_for_act(spec, act_number)
        role = (
            str(prepared.scene_role or "").strip()
            if prepared is not None
            else {
                1: "strong_start",
                2: "social_or_investigation",
                3: "climax_candidate",
                4: "aftermath",
            }.get(act_number, "situation")
        )
        key = (
            str(prepared.scene_key or "").strip()
            if prepared is not None
            else f"s{spec.number:02d}-in-place-{act_number}"
        )
        phase = {
            1: "开场",
            2: "局势转折",
            3: "正面取舍",
            4: "结果余波",
        }.get(act_number, f"第{act_number}幕")
        return SessionSceneOpportunity(
            scene_key=key,
            scene_role=role,
            title=f"{phase}（原地推进）",
            location=location,
            situation=(
                f"英雄选择继续留在{location}；已公开的压力、人物立场或异常现象因此进入新的阶段。"
            ),
            purpose=(
                "让当前地点出现一项由此前行动引出的具体变化，并把新的应对权交回给英雄。"
            ),
            optional=False,
        )

    def _recent_public_replies(self, *, limit: int = 4) -> str:
        replies: list[str] = []
        for call in reversed(self.calls):
            reply = str(call.get("reply") or "").strip()
            if not reply:
                continue
            replies.append(reply[-500:])
            if len(replies) >= max(1, limit):
                break
        return "\n".join(reversed(replies))

    def _preferred_npc_followup_speaker(self, default_speaker: str) -> str:
        """Give Luna's explicit NPC question to its addressed hero first."""

        speaker_by_hero = {
            "伊莉雅": "阿凛",
            "赛璃": "南星",
            "洛岚": "白河",
            "艾薇娅": "时雨",
            "苍祈": "澄砚",
        }
        pending = self._runtime().app.scene_frame_manager.latest_pending_npc_question()
        addressed_actor = str((pending or {}).get("addressed_actor") or "").strip()
        return speaker_by_hero.get(addressed_actor, default_speaker)

    def _preferred_open_condition_speaker(self, default_speaker: str) -> str:
        """Give a personally assigned public condition to that hero's player.

        This affects FU-PL scheduling only.  It never changes FU-GM state or
        assumes the hero accepts the condition; it simply lets the right
        simulated player respond instead of making another PC act for them.
        """

        frame = self._runtime().app.scene_frame_manager.current_frame
        conditions = list(getattr(frame, "open_conditions", []) or []) if frame else []
        return self._speaker_for_personal_condition(default_speaker, conditions)

    @staticmethod
    def _speaker_for_personal_condition(
        default_speaker: str,
        conditions: list[dict[str, object]],
    ) -> str:
        speaker_by_hero = {
            "伊莉雅": "阿凛",
            "赛璃": "南星",
            "洛岚": "白河",
            "艾薇娅": "时雨",
            "苍祈": "澄砚",
        }
        candidates: list[str] = []
        ownership = re.compile(
            r"(?:亲自|当面|以自己的名义|由自己|本人|承担|担保|承诺|宣誓|立誓|签名|说明)"
        )
        for item in reversed(conditions):
            if str(item.get("status") or "open") != "open":
                continue
            condition = " ".join(str(item.get("condition") or "").split())
            for hero, speaker in speaker_by_hero.items():
                if hero in condition and ownership.search(condition[condition.find(hero) : condition.find(hero) + 40]):
                    candidates.append(speaker)
        unique = list(dict.fromkeys(candidates))
        return unique[0] if len(unique) == 1 else default_speaker

    def _run_gm_stinger_if_needed(self, spec: CampaignSessionSpec) -> None:
        if spec.number not in self.GM_STINGER_SESSIONS or spec.number >= self.target_sessions:
            return
        app = self._runtime().app
        self._close_active_play_scene(f"第{spec.number:02d}场玩家场景结束。")
        app.start_scene(
            f"第{spec.number:02d}场·GM切镜",
            SceneType.GM,
            location="玩家角色视野之外",
            participants=[],
            objective="展示反派行动造成的可见伏笔，不泄露完整答案",
            summary=f"{spec.arc}幕尾切镜",
        )
        self.invoke(
            f"第{spec.number:02d}场GM幕尾切镜",
            "POST",
            "/v1/game/scene-opening",
            {
                **self.common,
                "speaker": "时悠",
                "message": (
                    "这是一个很短的GM场景，玩家角色不在场。展示艾蕾娜或辉钢财团如何回应本场真实结果，"
                    "只给观众一个能在后续兑现的伏笔；不要解释计划全貌，不要要求玩家立刻行动。"
                ),
            },
        )

    def _simulate_player_turn(
        self,
        spec: CampaignSessionSpec,
        speaker: str,
        index: int,
        *,
        current_act: int = 1,
    ) -> str:
        hero_by_speaker = {
            "阿凛": "伊莉雅",
            "南星": "赛璃",
            "白河": "洛岚",
            "时雨": "艾薇娅",
            "澄砚": "苍祈",
        }
        actor = hero_by_speaker.get(speaker, speaker)
        app = self._runtime().app
        pending_npc_response = app.scene_frame_manager.latest_pending_npc_question()
        pending_response_instruction = ""
        pending_response_contract: dict[str, object] = {}
        if pending_npc_response is not None:
            npc = str(pending_npc_response.get("npc") or "对方").strip()
            addressed_actor = str(
                pending_npc_response.get("addressed_actor") or ""
            ).strip()
            remaining_items = NPCResponseWindowManager.remaining_items(
                pending_npc_response
            )
            summary = str(pending_npc_response.get("summary") or "刚才的问题").strip()
            required = "、".join(
                item["prompt"] for item in remaining_items
            ) or summary
            if addressed_actor and addressed_actor == actor:
                pending_response_instruction = (
                    f"现场的【{npc}】正在明确等【{actor}】回应【{required}】。本轮直接对【{npc}】作答："
                    "可以给出角色确实知道的答案，也可以明确拒绝回答或承认不知道；"
                    "不能只和队友商量要不要说。若有多个未回答部分，本句应尽量逐项回应，"
                    "但不得编造角色不知道的事实。"
                )
                pending_response_contract = {
                    "question_id": str(
                        pending_npc_response.get("question_id") or ""
                    ).strip(),
                    "npc": npc,
                    "summary": summary,
                    "remaining_items": remaining_items,
                    "speaker_evidence": str(
                        pending_npc_response.get("speaker_evidence") or ""
                    ).strip(),
                }
            elif addressed_actor:
                pending_response_instruction = (
                    f"【{npc}】刚才明确在等【{addressed_actor}】回应【{required}】；"
                    f"不要替【{addressed_actor}】作答。让【{actor}】采取自己此刻能控制的另一项具体行动。"
                )
            else:
                pending_response_instruction = (
                    f"【{npc}】仍向整队提出了【{required}】。这是一项可回应的现场压力，不是对每名英雄的强制问卷；"
                    f"【{actor}】可以回答、拒答或承认不知道，也可以采取另一项会真实改变当前局面的具体行动。"
                )
        last_gm_reply = next(
            (str(call.get("reply") or "") for call in reversed(self.calls) if str(call.get("reply") or "").strip()),
            "",
        )
        unresolved_public_choice = self._public_choice_unresolved(last_gm_reply)
        diversity_instruction = self._player_action_diversity_instruction(spec)
        act_instruction = self._player_act_instruction(
            current_act,
            unresolved_public_choice=unresolved_public_choice,
        )
        transition_instruction = self._player_transition_instruction(
            spec,
            current_act=current_act,
        )
        step = ReplayStep(
            id=f"session-{spec.number:02d}-dynamic-{index:02d}",
            kind="player_message",
            speaker=speaker,
            actor=actor,
            payload={
                **(
                    {"npc_response_contract": pending_response_contract}
                    if pending_response_contract
                    else {}
                ),
                "dramatic_progress_context": self._player_progress_review_context(
                    spec,
                    current_act=current_act,
                ),
            },
            stage_goal=(
                f"只根据上一条GM公开内容回应【{spec.title}】当前场景。这是行动槽："
                "必须让指定角色提交一个明确行动，或直接向现场NPC提出一个需要回答的问题；"
                "可以先表达犹豫，但最后要落到角色现在实际做什么。不要只给队友建议或询问某位英雄要不要行动，"
                "必须由指定角色本人立即完成这项行动；让另一名玩家角色签字、开门、调查或替自己处理局面不算本行动，"
                "询问时使用上一条公开回复里已经出现的具体人物称呼，不要说‘旁边能回答的人’、‘对方’或‘当前目标’。"
                "不能替GM宣布结果，也不要跳到尚未出现的地点。"
                f"{pending_response_instruction}"
                f"{act_instruction}"
                f"{transition_instruction}"
                f"{diversity_instruction}"
            ),
        )
        scenario = ReplayScenario(
            name=f"第{spec.number:02d}场实时玩家模拟",
            campaign_id=self.campaign_id,
            session_id=self.session_id,
            channel_id=self.channel_id,
            participants=["阿凛", "南星", "白河", "时雨", "澄砚"],
            steps=[step],
        )
        recent_public_context = self._recent_public_dialogue(limit=10)
        legal_context = self.player_legal_actions.build(
            self.service,
            scenario,
            step,
            public_context=recent_public_context,
        )
        utterance = self.player_simulator.compose(
            step=step,
            legal_context=legal_context,
            last_gm_reply=last_gm_reply,
            recent_public_context=recent_public_context,
        )
        text = str(utterance.text or "").strip()
        for prefix in (f"{speaker}:", f"{speaker}："):
            if text.startswith(prefix):
                text = text[len(prefix) :].strip()
        if not text:
            text = self._extra_session_turn(spec, speaker, index)
        self.player_simulation_metrics.append(
            {
                "session": spec.number,
                "speaker": speaker,
                "actor": actor,
                "used_fallback": bool(utterance.used_fallback),
                "validation_errors": list(utterance.validation_errors or []),
                "fallback_kind": str(utterance.fallback_kind or ""),
                "fallback_diagnostics": list(utterance.fallback_diagnostics or []),
                "text": text,
                "model_attempts": list(utterance.model_attempts or []),
            }
        )
        return text

    def _player_route_expectation(
        self,
        fallback_kind: str,
        *,
        speaker: str = "",
    ) -> tuple[str, bool]:
        """Use the GM's real contract when FU-PL falls back to a safe pass.

        A mid-round pass is recorded silently. If this actor is the final
        outstanding PC and the shared round will visibly advance an automatic
        clock, the GM must publish that consequence instead of swallowing it.
        This keeps the strict route assertion aligned with the actual table
        state rather than treating the same sentence as universally neutral.
        """

        if str(fallback_kind or "").strip() == "exhaustion_safe_pass":
            if self._safe_pass_will_publish_clock_change(speaker):
                return "fu_gm", True
            return "silent", False
        return "fu_gm", True

    def _safe_pass_will_publish_clock_change(self, speaker: str) -> bool:
        actor = self._hero_for_speaker(speaker)
        if not actor:
            return False
        app = self.service._runtime(self.campaign_id).app
        if app.conflict_manager.state.active:
            return True
        scene_manager = app.scene_manager
        _number, required, acted, skip_names = scene_manager._action_round_state()
        waiting = [name for name in required if name not in acted]
        if waiting != [actor]:
            return False
        clocks = app.clock_manager
        skip = {str(name or "").strip() for name in skip_names}
        for clock in clocks.all():
            if (
                clock.name in skip
                or str(clock.status or "active") != "active"
                or not str(clock.auto_advance or "").strip()
                or clock.current >= clock.max_segments
                or clocks._clock_auto_advance_timing(clock) != "action_round_end"
            ):
                continue
            if int(clock.auto_advance_progress or 0) + 1 < clocks._auto_advance_every(clock):
                continue
            if clocks._auto_advance_delta(clock.auto_advance) != 0:
                return True
        return False

    def _player_progress_review_context(
        self,
        spec: CampaignSessionSpec,
        *,
        current_act: int,
    ) -> dict[str, object]:
        """Give the offline quality checker goals without briefing FU-PL.

        This context is deliberately absent from the player-generation prompt
        so hidden campaign preparation cannot leak into a simulated player's
        knowledge.
        """

        assessment = self.session_progress_assessments.get(spec.number)
        return {
            "act": int(current_act),
            "stage": str(getattr(assessment, "stage", "") or ""),
            "unresolved_now": str(
                getattr(assessment, "unresolved_now", "") or ""
            ),
            "next_gm_need": str(
                getattr(assessment, "next_gm_need", "") or ""
            ),
            "repeated_loop_detected": bool(
                getattr(assessment, "repeated_loop_detected", False)
            ),
        }

    def _player_transition_instruction(
        self,
        spec: CampaignSessionSpec,
        *,
        current_act: int,
    ) -> str:
        """Give FU-PL a visible route without turning it into a forced move."""

        request = dict(getattr(self, "_pending_scene_transition", {}) or {})
        if (
            int(request.get("session_number") or 0) != int(spec.number)
            or int(request.get("current_act") or 0) != int(current_act)
            or not str(request.get("target_location") or "").strip()
            or not bool(request.get("public_target_announced"))
        ):
            return ""
        target = str(request["target_location"]).strip()
        return (
            "GM刚刚在公开叙事中给出了离开当前地点的机会。此轮必须由该角色作出可见回应："
            f"明确带人或随队前往【{target}】；或明确拒绝、留下并承担已说出的代价；"
            "也可以选择另一处已经在公开对话中出现的明确地点。只讨论‘要不要走’不算行动，"
            "但任何实际移动都必须由该角色自己说出口。"
        )

    @staticmethod
    def _player_act_instruction(
        current_act: int,
        *,
        unresolved_public_choice: bool = False,
    ) -> str:
        if current_act <= 1:
            return "当前是开场段：先回应眼前人物、压力或可见物，取得能够作决定的立足点。"
        if current_act == 2:
            return (
                "当前局面已经进入升级与转折：利用已公开发现改变做法，不要把已经回答的问题或检查过的物件重新做一遍。"
            )
        if current_act == 3:
            return (
                "当前已进入高潮段：必须回应GM刚摆出的公开对决或取舍，作出会改变现场的具体选择或行动；"
                "除非GM刚公开了全新证据，否则不要回头检查旧线索。"
            )
        if unresolved_public_choice:
            return (
                "当前仍有公开取舍没有解决，尚不是余波：必须当场接受并履行、明确拒绝并承担后果，"
                "或立即采取能改变条件的对抗行动；不要复述信息、重复询价或把关键动作留到下一步。"
            )
        return (
            "当前是余波段：只回应已经落地的结果、照顾受影响者、处理关系或说明下一步承诺；"
            "不要重新调查旧物件，也不要自行开启新任务。"
        )

    @staticmethod
    def _public_choice_unresolved(reply: str) -> bool:
        clean = " ".join(str(reply or "").split())
        if not clean:
            return False
        return bool(
            re.search(
                r"只接受|不接受别的交换|(?:给我|交出|告诉我)[^。！？]{0,45}(?:就|换)|"
                r"不给[^。！？]{0,35}(?:就|会)|要么[^。！？]{1,45}要么|二选一",
                clean,
            )
        )

    def _player_action_diversity_instruction(self, spec: CampaignSessionSpec) -> str:
        """Keep synthetic players from turning one answer into a question loop."""

        recent_messages: list[str] = []
        prefix = f"第{spec.number:02d}场"
        for call in reversed(self.calls):
            label = str(call.get("label") or "")
            if not label.startswith(prefix):
                continue
            if "行动" not in label or "待决回应" in label:
                continue
            if ConversationQualityAuditor._is_resolved_scene_relocation(call):
                continue
            message = str(call.get("message") or "").strip()
            if message:
                recent_messages.append(message)
            if len(recent_messages) >= 5:
                break
        direct_questions = sum(
            1
            for message in recent_messages
            if ConstrainedPlayerSimulator._looks_like_direct_npc_question(message)
        )
        instructions: list[str] = []
        if direct_questions >= 2:
            instructions.append(
                "最近两次以上行动已经用NPC问答推进；本行动不得继续向NPC追问，"
                "必须落实已得答案、操作现场物件、应对环境/威胁，或作出一个有代价的决定。"
            )
        recent_action_profiles: list[tuple[str, tuple[str, ...]]] = []
        for message in recent_messages[:4]:
            family = ConstrainedPlayerSimulator._action_family(message)
            anchors = tuple(sorted(ConstrainedPlayerSimulator._action_lane_anchors(message)))
            if family and anchors:
                recent_action_profiles.append((family, anchors))
        repeated_profiles = {
            profile
            for profile in recent_action_profiles
            if recent_action_profiles.count(profile) >= 2
        }
        if repeated_profiles:
            instructions.append(
                "最近已有多名角色处理同一行动路线。本行动必须同时更换主要对象、实际手段和直接目的，"
                "或明确协助其中一人；不得再次独立躲到同一掩体、监听同一征兆、检查同一物件或守住同一入口。"
            )
        lane_pressure = self._action_lane_pressure(recent_messages)
        if lane_pressure:
            focus_text = "、".join(self._action_lane_anchor_label(item) for item in lane_pressure["anchors"])
            instructions.append(
                f"最近至少三名英雄已经围绕【{focus_text}】完成了共同动作；这条路线已经交给GM结算。"
                "本行动不得再复述同行、贴路、守同一位置、盯同一动静或确认同一分工。"
                "必须回应GM刚刚给出的新变化，或选择一个不重叠的公开人物、物件、威胁或取舍。"
            )
        # Synthetic players must know only what appeared in the public chat.
        # Scene-frame facts and conditions are GM/runtime state and can be
        # populated before expression; feeding them here turns an unspoken clue
        # into player knowledge. The public dialogue already carries every fact
        # the simulator is allowed to use.
        last_gm_reply = next(
            (str(call.get("reply") or "").strip() for call in reversed(self.calls) if str(call.get("reply") or "").strip()),
            "",
        )
        affordance = self._explicit_gm_affordance(last_gm_reply)
        if affordance:
            instructions.append(
                f"GM刚给出的可立即回应内容是：{affordance}。"
                "请明确接受、拒绝或选定其中一项并现在行动，不要退回此前准备。"
            )
        return ("本轮额外约束：" + "".join(instructions)) if instructions else ""

    def _recent_session_action_messages(
        self,
        spec: CampaignSessionSpec,
        *,
        limit: int = 6,
    ) -> list[str]:
        """Return recent committed PC actions for one session in table order."""

        messages: list[str] = []
        prefix = f"第{spec.number:02d}场"
        for call in reversed(self.calls):
            label = str(call.get("label") or "")
            if not label.startswith(prefix):
                continue
            if "行动" not in label or "待决回应" in label:
                continue
            if ConversationQualityAuditor._is_resolved_scene_relocation(call):
                continue
            message = str(call.get("message") or "").strip()
            if message:
                messages.append(message)
            if len(messages) >= max(3, limit):
                break
        return list(reversed(messages))

    @staticmethod
    def _action_lane_pressure(messages: list[str]) -> dict[str, Any] | None:
        """Find a crowded low-progress lane before it becomes table repetition.

        A party may intentionally make several moves around one clock or target.
        This only catches three or more low-progress actions that keep sharing a
        concrete public anchor such as the same route, traveler, or wind chime.
        Ritual work is excluded: multiple heroes advancing an actual ritual is
        a normal Fabula Ultima collaboration pattern, not a stale scene.
        """

        low_progress_families = {"investigate", "guard", "manipulate", "move", "care"}
        excluded_anchors = {"ritual"}
        records: list[tuple[str, set[str]]] = []
        for message in messages[-6:]:
            if ConstrainedPlayerSimulator._looks_like_uncommitted_table_talk(message):
                continue
            family = ConstrainedPlayerSimulator._action_family(message)
            anchors = ConstrainedPlayerSimulator._action_lane_anchors(message) - excluded_anchors
            # Some natural group commitments ("接受旧阶、陪着旅人走") do
            # not contain one of the simulator's explicit action verbs.  Once
            # they share concrete anchors they still consume the same table
            # spotlight, so keep an unclassified-but-grounded commitment.
            if (family and family not in low_progress_families) or not anchors:
                continue
            records.append((family, anchors))
        if len(records) < 3:
            return None
        counts = Counter(anchor for _, anchors in records for anchor in anchors)
        crowded = [anchor for anchor, count in counts.items() if count >= 3]
        if not crowded:
            return None
        matching = [
            (family, anchors)
            for family, anchors in records
            if anchors & set(crowded)
        ]
        if len(matching) < 3:
            return None
        anchors = sorted(crowded, key=lambda item: (-counts[item], item))
        return {
            "signature": "|".join(anchors),
            "anchors": anchors,
            "occurrences": len(matching),
            "families": sorted({family for family, _ in matching if family}),
        }

    @staticmethod
    def _action_lane_anchor_label(anchor: str) -> str:
        labels = {
            "approach_signal": "逼近征兆",
            "concealment": "同一掩体",
            "container": "同一容器",
            "counter": "柜台与文书",
            "door": "同一入口",
            "evidence": "同一证据",
            "oil_trace": "油迹",
            "patrol": "巡逻队",
            "road": "同一条路",
            "traveler": "失忆旅人",
            "wind_chime": "风铃",
        }
        return labels.get(anchor, anchor)

    @staticmethod
    def _recent_public_gm_beat(
        calls: list[dict[str, Any]],
        *,
        session_number: int,
        max_player_actions: int = 1,
    ) -> bool:
        """Whether a meaningful GM beat already refreshed this action space.

        A lane reset is a safety valve for a stale group plan, not permission to
        stack two GM interruptions.  Ignore table talk, then look back only one
        committed player action for a public proactive GM reply.
        """

        prefix = f"第{session_number:02d}场"
        player_actions_after = 0
        for call in reversed(calls):
            label = str(call.get("label") or "")
            if not label.startswith(prefix):
                continue
            if "行动" in label and "待决回应" not in label:
                player_actions_after += 1
                if player_actions_after > max_player_actions:
                    return False
                continue
            if "GM主动节拍" in label:
                # A heartbeat that deliberately stayed silent is not a
                # fictional beat and must not hide an earlier material GM
                # response from this look-back.
                if str(call.get("reply") or "").strip():
                    return player_actions_after <= max_player_actions
                continue
        return False

    @classmethod
    def _recent_scene_opening_needs_player_space(
        cls,
        calls: list[dict[str, Any]],
        *,
        session_number: int,
        minimum_player_actions: int = 2,
    ) -> bool:
        """Do not stack a heartbeat directly on a newly opened scene."""

        prefix = f"第{session_number:02d}场"
        player_actions_after = 0
        for call in reversed(calls):
            label = str(call.get("label") or "")
            if not label.startswith(prefix):
                continue
            if cls._is_scene_opening_call(call):
                return player_actions_after < max(1, int(minimum_player_actions))
            if "行动" in label and "待决回应" not in label:
                player_actions_after += 1
        return False

    def _refocus_saturated_action_lane(
        self,
        spec: CampaignSessionSpec,
        *,
        index: int,
        player_turn_count: int,
        last_signature: str,
        last_refocus_turn: int,
    ) -> dict[str, Any] | None:
        """Ask the GM for a concrete world response before another duplicate turn.

        This is deliberately a long-test table-management beat, rather than a
        player-facing rule. It models a human GM advancing the fiction after a
        group has collectively committed to the same route or protection plan.
        """

        if self._runtime().app.scene_frame_manager.latest_pending_npc_question() is not None:
            # The next meaningful move belongs to a player who must answer the
            # NPC.  Asking the GM for a fresh world beat here would either be
            # correctly silent or would steal that response window.
            return None

        pressure = self._action_lane_pressure(self._recent_session_action_messages(spec))
        if not pressure:
            return None
        if self._recent_public_gm_beat(
            self.calls,
            session_number=spec.number,
            max_player_actions=3,
        ):
            return None
        signature = str(pressure["signature"])
        if signature == str(last_signature or "") and player_turn_count - last_refocus_turn < 4:
            return None
        focus_text = "、".join(self._action_lane_anchor_label(item) for item in pressure["anchors"])
        directive = (
            "【共同动作兑现】多名英雄已经围绕"
            f"【{focus_text}】作出并落实了同一行动。不要再让他们重复同行、警戒、确认分工或检查同一对象。"
            "现在必须让世界作出一个具体、可见且有后果的回应：推进到下一处明确地点，或让NPC、环境或对立方完成一件动作。"
            "回应里要出现至少一个新的可互动人物、物件、威胁或取舍，让下一位英雄能做不同的事。"
            "只描述已经发生的变化，不替玩家决定接下来选什么；不得静默、不得复述刚才的队形与计划。"
            "不要用偶然翻页、掉落或机关自解替玩家完成调查，也不要一次公开多条核心谜团答案。"
        )
        result = self._session_gm_beat(spec, index, directive)
        if not str(result.get("reply") or "").strip():
            raise RuntimeError(
                f"第{spec.number:02d}场共同动作已饱和，但GM主动节拍没有提供新的公开局面。"
            )
        self._record_tool_event(
            "队伍行动通道收束",
            f"第{spec.number:02d}场",
            "检测到多名英雄围绕同一低进展行动反复投入；GM先兑现共同动作，再交给下一位英雄新的局面。",
            {**pressure, "index": index, "player_turn_count": player_turn_count},
            public=False,
        )
        return {"signature": signature, "result": result, "pressure": pressure}

    @staticmethod
    def _explicit_gm_affordance(reply: str) -> str:
        clean = " ".join(str(reply or "").split()).strip()
        if not clean:
            return ""
        for sentence in re.split(r"(?<=[。！？!?])", clean):
            if re.search(
                r"跟我来|随我来|带你们(?:去|进)|门(?:已经|现在)?(?:开了|打开)|"
                r"通道(?:已经|现在)?(?:开了|打开)|可以进去|现在进去|选一个|二选一|要么.{1,30}要么|"
                r"只接受|不接受别的交换|(?:给我|交出|告诉我).{1,40}(?:就|换)|不给.{1,30}(?:就|会)",
                sentence,
            ):
                return sentence.strip()[:220]
        return ""

    def _answer_pending_decisions(self, spec: CampaignSessionSpec, index: int) -> int:
        """Let the owning FU-PL answer player-facing choices before play moves on."""

        app = self._runtime().app
        hero_by_speaker = {
            "阿凛": "伊莉雅",
            "南星": "赛璃",
            "白河": "洛岚",
            "时雨": "艾薇娅",
            "澄砚": "苍祈",
        }
        speaker_by_hero = {hero: speaker for speaker, hero in hero_by_speaker.items()}
        answered = 0
        for attempt in range(3):
            waiting = app.interceptor.decision_window_manager.awaiting_player_response()
            summary_by_id = {
                str(item.get("window_id") or ""): item
                for item in app.interceptor.decision_window_manager.public_summary()
            }
            if not waiting:
                break
            window = summary_by_id.get(waiting[0].window_id)
            if window is None:
                self.errors.append(
                    f"第{spec.number:02d}场待决窗口 {waiting[0].window_id} 缺少公开摘要。"
                )
                break
            owner = str(window.get("owner") or "").strip()
            allowed = [str(value).strip() for value in window.get("allowed_speakers", []) if str(value).strip()]
            speaker = next((name for name in hero_by_speaker if name in allowed), "")
            if not speaker:
                speaker = speaker_by_hero.get(owner, "")
            if not speaker:
                self.errors.append(
                    f"第{spec.number:02d}场待决窗口 {window.get('window_id')} 找不到合法玩家：{allowed or [owner]}"
                )
                break
            actor = hero_by_speaker[speaker]
            legal_context_probe = self.player_legal_actions.build(
                self.service,
                ReplayScenario(
                    name=f"第{spec.number:02d}场待决窗口回应",
                    campaign_id=self.campaign_id,
                    session_id=self.session_id,
                    channel_id=self.channel_id,
                    participants=list(hero_by_speaker),
                    steps=[],
                ),
                ReplayStep(
                    id=f"session-{spec.number:02d}-decision-probe-{index:02d}-{attempt + 1}",
                    kind="player_message",
                    speaker=speaker,
                    actor=actor,
                ),
                public_context=self._recent_public_dialogue(limit=10),
            )
            exact_choice = ConstrainedPlayerSimulator._decision_window_fallback(
                window,
                legal_context_probe,
            )
            step = ReplayStep(
                id=f"session-{spec.number:02d}-decision-{index:02d}-{attempt + 1}",
                kind="player_message",
                speaker=speaker,
                actor=actor,
                message=exact_choice,
                stage_goal=(
                    "GM刚刚把一个明确选择交给当前玩家。只回答这个待决窗口，使用合法选项并补齐必要目标或参数；"
                    "不要声明新的场景行动，不要替GM描述结果。如果选择【揭示】，必须从最近公开现场中选一个已出现的生物，"
                    "并说明要得知其目标或动机。"
                ),
            )
            scenario = ReplayScenario(
                name=f"第{spec.number:02d}场待决窗口回应",
                campaign_id=self.campaign_id,
                session_id=self.session_id,
                channel_id=self.channel_id,
                participants=list(hero_by_speaker),
                steps=[step],
            )
            recent_public_context = self._recent_public_dialogue(limit=10)
            legal_context = self.player_legal_actions.build(
                self.service,
                scenario,
                step,
                public_context=recent_public_context,
            )
            last_gm_reply = next(
                (str(call.get("reply") or "") for call in reversed(self.calls) if str(call.get("reply") or "").strip()),
                "",
            )
            utterance = self.player_simulator.compose(
                step=step,
                legal_context=legal_context,
                last_gm_reply=last_gm_reply,
                recent_public_context=recent_public_context,
            )
            message = str(utterance.text or "").strip()
            for prefix in (f"{speaker}:", f"{speaker}："):
                if message.startswith(prefix):
                    message = message[len(prefix) :].strip()
            if not message:
                self.errors.append(f"第{spec.number:02d}场待决窗口未生成玩家回应。")
                break
            self.player_simulation_metrics.append(
                {
                    "session": spec.number,
                    "speaker": speaker,
                    "actor": actor,
                    "kind": "decision_window",
                    "window_id": str(window.get("window_id") or ""),
                    "used_fallback": bool(utterance.used_fallback),
                    "validation_errors": list(utterance.validation_errors or []),
                    "text": message,
                }
            )
            before_ids = {
                item.window_id
                for item in app.interceptor.decision_window_manager.awaiting_player_response()
            }
            response = self.route_table_message(
                f"第{spec.number:02d}场待决回应 {index:02d}.{attempt + 1} {speaker}",
                speaker,
                message,
                expected_target="fu_gm",
                expected_send_reply=True,
            )
            answered += 1
            after_ids = {
                item.window_id
                for item in app.interceptor.decision_window_manager.awaiting_player_response()
            }
            if before_ids == after_ids:
                decision = dict(response.get("decision") or {})
                if (
                    str(decision.get("agent_action") or "").strip() == "ask_user"
                    and str(response.get("reply") or "").strip()
                ):
                    # Missing required parameters keep the same durable
                    # decision window open. This is a clarification exchange,
                    # not a sticky-window failure; the same player answers it
                    # on the next attempt.
                    continue
                self.errors.append(
                    f"第{spec.number:02d}场待决窗口 {window.get('window_id')} 在合法玩家回应后仍未变化。"
                )
                # Do not let a broken synthetic choice contaminate all later
                # turns.  Trait/bond rerolls always have the rules-legal option
                # to accept the original result.
                kind = str(window.get("kind") or "")
                if kind in {"trait_invocation", "bond_invocation"}:
                    acceptance = (
                        "我接受这次结果，不重掷。"
                        if window.get("roll_success") is True
                        else "我接受这次失败，不重掷。"
                    )
                    self.route_table_message(
                        f"第{spec.number:02d}场待决回应修复 {index:02d}.{attempt + 1} {speaker}",
                        speaker,
                        acceptance,
                        expected_target="fu_gm",
                        expected_send_reply=True,
                    )
                    answered += 1
                    repaired_ids = {
                        item.window_id
                        for item in app.interceptor.decision_window_manager.awaiting_player_response()
                    }
                    if repaired_ids != after_ids:
                        continue
                raise RuntimeError(
                    f"第{spec.number:02d}场待决窗口 {window.get('window_id')} "
                    f"({window.get('kind')}) 未在合法回应后消费；测试停止以避免粘滞复读。"
                )
        remaining = app.interceptor.decision_window_manager.awaiting_player_response()
        if remaining:
            first = remaining[0]
            raise RuntimeError(
                f"第{spec.number:02d}场待决窗口 {first.window_id} ({first.kind}) "
                "在三轮合法回应后仍未消费；测试停止以避免粘滞复读。"
            )
        return answered

    def _answer_agent_clarification(
        self,
        spec: CampaignSessionSpec,
        index: int,
        *,
        speaker: str,
        actor: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        """Keep a semantic ``ask_user`` with the same player action.

        Agent clarifications are not durable rules windows, but they still
        belong to the action currently being adjudicated.  Advancing the
        scripted speaker cycle here would reproduce the exact table failure
        this runner is meant to catch: one player's incomplete declaration is
        abandoned and another hero starts acting over it.
        """

        current = dict(body or {})
        for attempt in range(3):
            decision = dict(current.get("decision") or {})
            if str(decision.get("agent_action") or "").strip() != "ask_user":
                return current
            last_gm_reply = str(current.get("reply") or "").strip()
            if not last_gm_reply:
                raise RuntimeError(
                    f"第{spec.number:02d}场GM返回ask_user但没有给玩家可回答的问题。"
                )
            step = ReplayStep(
                id=f"session-{spec.number:02d}-clarification-{index:02d}-{attempt + 1}",
                kind="player_message",
                speaker=speaker,
                actor=actor,
                stage_goal=(
                    "GM刚向当前玩家追问执行这项行动所必需的信息。只回答这一个问题，"
                    "补齐自己角色已知且规则允许的选择；不要开始另一项行动，不替GM宣布结果。"
                    "如果GM指出角色能力并不支持原说法，就澄清角色实际采用的普通方法，"
                    "不要临时发明新的技能功能。"
                ),
            )
            scenario = ReplayScenario(
                name=f"第{spec.number:02d}场GM追问回应",
                campaign_id=self.campaign_id,
                session_id=self.session_id,
                channel_id=self.channel_id,
                participants=["阿凛", "南星", "白河", "时雨", "澄砚"],
                steps=[step],
            )
            recent_public_context = self._recent_public_dialogue(limit=10)
            legal_context = self.player_legal_actions.build(
                self.service,
                scenario,
                step,
                public_context=recent_public_context,
            )
            utterance = self.player_simulator.compose(
                step=step,
                legal_context=legal_context,
                last_gm_reply=last_gm_reply,
                recent_public_context=recent_public_context,
            )
            message = str(utterance.text or "").strip()
            for prefix in (f"{speaker}:", f"{speaker}："):
                if message.startswith(prefix):
                    message = message[len(prefix) :].strip()
            if not message:
                raise RuntimeError(
                    f"第{spec.number:02d}场玩家【{speaker}】没有回答GM的行动追问。"
                )
            self.player_simulation_metrics.append(
                {
                    "session": spec.number,
                    "speaker": speaker,
                    "actor": actor,
                    "kind": "gm_clarification",
                    "attempt": attempt + 1,
                    "used_fallback": bool(utterance.used_fallback),
                    "validation_errors": list(utterance.validation_errors or []),
                    "text": message,
                }
            )
            current = self.route_table_message(
                f"第{spec.number:02d}场GM追问回应 {index:02d}.{attempt + 1} {speaker}",
                speaker,
                message,
                expected_target="fu_gm",
                expected_send_reply=True,
                directed_at_gm=True,
            )

        if str(dict(current.get("decision") or {}).get("agent_action") or "").strip() == "ask_user":
            raise RuntimeError(
                f"第{spec.number:02d}场GM连续三次追问同一行动仍未取得可执行参数；测试停止。"
            )
        return current

    @staticmethod
    def _hero_for_speaker(speaker: str) -> str:
        return {
            "阿凛": "伊莉雅",
            "南星": "赛璃",
            "白河": "洛岚",
            "时雨": "艾薇娅",
            "澄砚": "苍祈",
        }.get(str(speaker or "").strip(), str(speaker or "").strip())

    def _party_resource_snapshot(self) -> dict[str, dict[str, Any]]:
        app = self._runtime().app
        snapshot: dict[str, dict[str, Any]] = {}
        for name in self.pc_names:
            if not app.character_manager.exists(name):
                continue
            character = app.character_manager.get(name)
            snapshot[name] = {
                "level": character.level,
                "xp": character.experience_points,
                "hp": character.hp,
                "max_hp": character.max_hp,
                "mp": character.mp,
                "max_mp": character.max_mp,
                "inventory_points": character.inventory_points,
                "max_inventory_points": character.max_inventory_points,
                "fabula_points": character.fabula_points,
            }
        return snapshot

    def _apply_between_session_level_ups(
        self,
        spec: CampaignSessionSpec,
        ended: dict[str, Any],
    ) -> list[dict[str, Any]]:
        if spec.number % 2 != 0:
            return []
        available = {str(name) for name in ended.get("level_up_available") or []}
        results: list[dict[str, Any]] = []
        for character_name in self.pc_names:
            if character_name not in available:
                self.errors.append(f"第{spec.number:02d}场后 {character_name} 未达到预期的升级条件。")
                continue
            cursor = self._upgrade_cursors.get(character_name, 0)
            plan = self.UPGRADE_PLANS.get(character_name, [])
            if cursor >= len(plan):
                continue
            class_name, skill_name = plan[cursor]
            body = self.invoke(
                f"第{spec.number:02d}场后升级 {character_name}",
                "POST",
                "/v1/progression/level-up",
                {
                    "campaign_id": self.campaign_id,
                    "character_name": character_name,
                    "class_name": class_name,
                    "skill_name": skill_name,
                },
            )
            if body.get("ok"):
                self._upgrade_cursors[character_name] = cursor + 1
                result = dict(body.get("result") or {})
                result["session"] = spec.number
                results.append(result)
                self.level_up_results.append(result)
        if results:
            self._record_tool_event(
                "结团经验与升级",
                f"第{spec.number:02d}场后",
                "玩家按每10经验值提升一级，并分别选择职业与技能；每名角色每场至多升级一次。",
                results,
            )
        return results

    def _session_scene_metric(self, spec: CampaignSessionSpec, scene_records: list[Any]) -> dict[str, Any]:
        app = self._runtime().app
        contract = app.story_arc_manager.state.current_pacing_plan.dramatic_contract
        progress = next(
            (
                item
                for item in reversed(app.story_arc_manager.state.session_progress_history)
                if item.session_number == spec.number
            ),
            app.story_arc_manager.state.current_session_progress,
        )
        substantial_ids = set(progress.substantial_scene_ids)
        substantial_records = [
            record
            for record in scene_records
            if str(record.scene_id or record.name) in substantial_ids
        ]
        types = [record.scene_type.value for record in substantial_records]
        locations = [record.location for record in substantial_records if record.location]
        roles: list[str] = []
        opportunity_keys: list[str] = []
        camera_signatures: list[tuple[str, str, str, str]] = []
        for record in substantial_records:
            opportunity = self._match_scene_opportunity(contract, record)
            explicit_role = str(
                getattr(record, "session_opportunity_role", "") or ""
            ).strip()
            explicit_key = str(
                getattr(record, "session_opportunity_key", "") or ""
            ).strip()
            if opportunity is None:
                role = explicit_role or "unclassified"
                roles.append(role)
                if explicit_key:
                    opportunity_keys.append(explicit_key)
                camera_signatures.append(
                    (
                        explicit_key,
                        role,
                        str(record.location or ""),
                        str(record.name or ""),
                    )
                )
                continue
            roles.append(explicit_role or opportunity.scene_role)
            opportunity_keys.append(explicit_key or opportunity.scene_key)
            camera_signatures.append(
                (
                    explicit_key or opportunity.scene_key,
                    explicit_role or opportunity.scene_role,
                    str(record.location or ""),
                    str(record.name or ""),
                )
            )
        distinct_locations = list(dict.fromkeys(locations))
        distinct_roles = list(dict.fromkeys(roles))
        camera_signatures = list(dict.fromkeys(camera_signatures))
        metric = {
            "opened_scene_count": len(scene_records),
            "scene_count": len(substantial_records),
            "scene_names": [record.name for record in substantial_records],
            "discarded_empty_scene_names": [
                record.name for record in scene_records if record not in substantial_records
            ],
            "scene_types": types,
            "locations": locations,
            "distinct_locations": distinct_locations,
            "distinct_location_count": len(distinct_locations),
            "functional_scene_roles": roles,
            "distinct_functional_roles": distinct_roles,
            "distinct_functional_role_count": len(distinct_roles),
            "distinct_camera_count": len(camera_signatures),
            "scene_opportunity_keys": opportunity_keys,
            "multiple_scenes": len(substantial_records) >= 3,
            "has_location": any(record.location for record in substantial_records),
            "has_escalation_scene": len(substantial_records) >= 2,
            "has_resolution_scene": any(
                token in record.name
                for record in substantial_records
                for token in ("结果与余波", "余波与收束")
            ),
            "short_clock_leaks": [
                clock.name for clock in app.clock_manager.all() if str(clock.scope or "") in {"scene", "session"}
            ],
        }
        if not metric["multiple_scenes"]:
            self.errors.append(
                f"第{spec.number:02d}场只形成 {len(substantial_records)} 个有玩家介入且局势变化的场景"
                f"（共打开 {len(scene_records)} 个），不符合规则书第32页的多场景结构。"
            )
        if len(distinct_roles) < 3:
            self.errors.append(
                f"第{spec.number:02d}场只有 {len(distinct_roles)} 种功能场景（{','.join(distinct_roles) or '未识别'}），"
                "场景数量不能替代强开场、发展/探索、高潮与余波的实际变化。"
            )
        if len(camera_signatures) < 3:
            self.errors.append(
                f"第{spec.number:02d}场只有 {len(camera_signatures)} 个实质不同镜头；"
                "同一大型地点可以包含多个子区域，但不能只给同一段局面换标题。"
            )
        if metric["short_clock_leaks"]:
            self.errors.append(
                f"第{spec.number:02d}场结束后仍残留短期命刻：{'、'.join(metric['short_clock_leaks'])}"
            )
        return metric

    def _expanded_session_turns(self, spec: CampaignSessionSpec) -> list[tuple[str, str]]:
        """Expand a campaign outline into a table-like four-hour session sample.

        The authored specs are the campaign spine. This method adds the texture
        a real table would create around that spine: table discussion, NPC
        answers, follow-up questions, pressure beats and scene refocusing.
        """

        turns: list[tuple[str, str]] = []
        gm_beats_inserted = 0
        player_turns = 0
        for index, speaker in enumerate(self._seed_speakers(spec), start=1):
            # The authored outline is GM-only coverage planning.  Synthetic
            # players must compose from the public transcript just like real
            # players, otherwise the test quietly leaks unrevealed NPCs,
            # clues and intended solutions into their declarations.
            turns.append((speaker, "__SIMULATE__"))
            player_turns += 1
            if index % 2 == 0:
                turns.append(("__TABLE__", "__DYNAMIC_DISCUSSION__"))

        speaker_cycle = ["阿凛", "南星", "白河", "时雨", "澄砚"]
        extra_index = 0
        while player_turns < self.min_table_turns_per_session:
            speaker = speaker_cycle[extra_index % len(speaker_cycle)]
            message = "__SIMULATE__"
            turns.append((speaker, message))
            player_turns += 1
            extra_index += 1
            if extra_index % 3 == 0:
                turns.append(("__TABLE__", "__DYNAMIC_DISCUSSION__"))
            if gm_beats_inserted < self.gm_beats_per_session and player_turns % 6 == 0:
                gm_beats_inserted += 1
                turns.append(("__GM_IDLE__", self._gm_beat_reason(spec, gm_beats_inserted)))
        return turns

    @staticmethod
    def _seed_speakers(spec: CampaignSessionSpec) -> list[str]:
        """Choose opening speakers without exposing the authored outline."""

        speakers: list[str] = []
        for speaker, _private_outline in spec.turns:
            clean = str(speaker or "").strip()
            if clean and clean not in speakers:
                speakers.append(clean)
            if len(speakers) >= 3:
                break
        return speakers or ["阿凛", "南星", "白河"]

    def _opening_table_prompt(self, spec: CampaignSessionSpec, index: int) -> str:
        semantic_discussion = self._simulate_table_discussion(spec, index)
        if semantic_discussion:
            return semantic_discussion
        prompts = [
            "我先缓一下，别急着丢技能；我们先听清楚时悠刚摆出来的现场。",
            "这场先别散开太快吧，眼前的人、可见痕迹和门外的动静都要有人顾。",
            "我觉得先定一个桌面共识：谁负责说话，谁负责看危险，谁照顾旅人或旁观者。",
            "先别为了找线索把每件东西都翻一遍；我们先处理最影响眼前选择的那一件事。",
            "如果刚才那股压力继续靠近，我们要先决定撤离、交涉还是硬扛。",
        ]
        return prompts[(spec.number + index) % len(prompts)]

    def _table_discussion_prompt(self, spec: CampaignSessionSpec, index: int) -> str:
        semantic_discussion = self._simulate_table_discussion(spec, index)
        if semantic_discussion:
            return semantic_discussion
        last_reply = next(
            (str(call.get("reply") or "") for call in reversed(self.calls) if str(call.get("reply") or "").strip()),
            "",
        )
        if any(
            marker in last_reply
            for marker in ("给出答复", "把话说清", "提出条件", "旧路可以借", "同意", "拒绝", "前提是")
        ):
            return "条件已经说清了，先别让对方重复。谁来落实承诺，谁继续看着外面的动静？"
        prompts = [
            "我觉得先别跳场，对方的底线还没完全摸清，但也别只换个说法重复问。",
            "现在最怕外面的压力突然压上来，谁方便盯着，谁继续处理眼前的人？",
            "我们手里已经看到的痕迹也许能撬动一点态度，但要想清楚先给谁看。",
            "我先整理一下优先级：旅人的安全、眼前证据、撤离路线，三件事别互相打架。",
            "如果要冒险推进，我倾向先保住普通人和退路；大家有不同看法先说。",
            "对方如果还犹豫，我们是不是需要一个更具体的承诺，而不是继续讲大道理？",
        ]
        return prompts[(spec.number + index) % len(prompts)]

    def _simulate_table_discussion(
        self,
        spec: CampaignSessionSpec,
        index: int,
        *,
        scripted_message: str = "",
    ) -> str:
        """Generate table talk from the latest public GM output, not the outline."""

        step = ReplayStep(
            id=f"session-{spec.number:02d}-table-{index:02d}",
            kind="player_message",
            speaker="南星",
            actor="赛璃",
            message=str(scripted_message or "").strip(),
            stage_goal=(
                "你正在和其他玩家短暂商量。只根据上一条GM公开回复，说一句自然、简短的意见、疑问或分工建议；"
                "不要对时悠提问，不要替角色声明行动，不要说‘我先调查/我来掩护/我施放法术/我去追问’；"
                "可以说‘谁来处理/我们要不要/我倾向于’，但不要说‘那我们就继续走/咱们先进去’之类已经执行集体行动的话；"
                "把真正行动留到对应玩家的行动槽。"
                "不要引入上一条回复没有出现的人名、地点或结论，"
                "已经说清的条件不要再要求NPC重复。"
            ),
        )
        scenario = ReplayScenario(
            name=f"第{spec.number:02d}场实时桌边讨论",
            campaign_id=self.campaign_id,
            session_id=self.session_id,
            channel_id=self.channel_id,
            participants=["阿凛", "南星", "白河", "时雨", "澄砚"],
            steps=[step],
        )
        last_gm_reply = next(
            (str(call.get("reply") or "") for call in reversed(self.calls) if str(call.get("reply") or "").strip()),
            "",
        )
        if not last_gm_reply:
            return ""
        recent_public_context = self._recent_public_dialogue(limit=10)
        legal_context = self.player_legal_actions.build(
            self.service,
            scenario,
            step,
            public_context=recent_public_context,
        )
        utterance = self.player_simulator.compose(
            step=step,
            legal_context=legal_context,
            last_gm_reply=last_gm_reply,
            recent_public_context=recent_public_context,
        )
        text = str(utterance.text or "").strip()
        for prefix in ("南星:", "南星："):
            if text.startswith(prefix):
                text = text[len(prefix) :].strip()
        self.player_simulation_metrics.append(
            {
                "session": spec.number,
                "speaker": "南星",
                "actor": "赛璃",
                "kind": "table_discussion",
                "used_fallback": bool(utterance.used_fallback),
                "validation_errors": list(utterance.validation_errors or []),
                "model_attempts": list(utterance.model_attempts or []),
                "table_discussion_review": dict(
                    self.player_simulator.last_table_discussion_review
                ),
                "text": text,
            }
        )
        return text

    @staticmethod
    def _is_scene_opening_call(call: dict[str, Any]) -> bool:
        """Return whether a recorded call opens a new player-facing scene."""

        if str(call.get("route") or "") in {"/v1/game/scene-opening", "/v1/game/scene-recap"}:
            return True
        label = str(call.get("label") or "")
        return bool(re.search(r"场景\d+开场|GM 强开场|GM幕尾切镜", label))

    @classmethod
    def _is_public_dialogue_call(cls, call: dict[str, Any]) -> bool:
        """Keep internal control prompts out of the simulated players' context."""

        route = str(call.get("route") or "")
        return route in {
            "/v1/message/route",
            "/v1/game/turn",
            "/v1/session-zero/message",
            # The heartbeat request is private, but a non-empty heartbeat
            # reply is a GM beat heard by the table. _recent_public_dialogue
            # records only the reply and never exposes the control prompt.
            "/v1/session/heartbeat",
        } or cls._is_scene_opening_call(call)

    def _recent_public_dialogue(
        self,
        *,
        limit: int = 10,
        current_scene_only: bool = True,
    ) -> str:
        """Build only player-visible context, normally bounded to the live scene.

        FU-PL uses this to avoid repeating an action that has already been tried.
        An old scene must not make a newly opened gate, corridor, or NPC look like
        an invalid repeat merely because it shares words with a previous scene.
        """

        lines: list[str] = []
        for call in reversed(self.calls):
            if not self._is_public_dialogue_call(call):
                continue
            scene_opening = self._is_scene_opening_call(call)
            speaker = str(call.get("speaker") or "").strip()
            message = " ".join(str(call.get("message") or "").split())
            reply = " ".join(str(call.get("reply") or "").split())
            if reply:
                lines.append(f"时悠：{reply[-700:]}")
            # The request body for /scene-opening is a private GM brief, not a
            # line the table heard.  Its reply is public and remains useful.
            if speaker and message and not scene_opening:
                lines.append(f"{speaker}：{message[-500:]}")
            if current_scene_only and scene_opening:
                break
            if len(lines) >= max(2, limit * 2):
                break
        return "\n".join(reversed(lines[: max(2, limit * 2)]))

    def _gm_beat_reason(self, spec: CampaignSessionSpec, beat_number: int) -> str:
        focus = "、".join(spec.expected_focus[:3]) or spec.title
        contract = self._runtime().app.story_arc_manager.state.current_pacing_plan.dramatic_contract
        escalation = list(contract.escalation_ladder or [])
        scene = self._runtime().app.scene_manager.current_scene
        present = [
            str(name).strip()
            for name in (getattr(scene, "participants", []) or [])
            if str(name).strip()
        ]
        named_npcs = [
            str(getattr(item, "name", "") or "").strip()
            for item in (contract.important_npcs or [])
            if str(getattr(item, "name", "") or "").strip()
        ]
        absent_named_npcs = [name for name in named_npcs if name not in present]
        eligible_escalation = [
            candidate
            for candidate in escalation
            if not any(name in candidate for name in absent_named_npcs)
        ]
        candidates = (
            "；".join(eligible_escalation)
            if eligible_escalation
            else "从当前在场NPC、集体、环境或既有威胁中落实一次可见行动"
        )
        assessment = self.session_progress_assessments.get(spec.number)
        observed_gap = ""
        if assessment is not None and not bool(getattr(assessment, "used_fallback", False)):
            observed_gap = (
                str(assessment.next_gm_need or "").strip()
                or str(assessment.unresolved_now or "").strip()
            )
        location = str(
            getattr(scene, "location", "") or getattr(scene, "name", "") or ""
        ).strip()
        scene_boundary = (
            f"当前聚焦地点是【{location or '未命名场景'}】，当前参与者唯一名单是"
            f"【{'、'.join(present) if present else '无'}】。点名名单外人物的准备候选本拍无效。"
        )
        return (
            scene_boundary
            + f"桌面自然停顿后，判断是否需要由NPC、环境或对立方推进【{focus}】。"
            + (f"根据目前公开实录，最需要补上的一拍是：{observed_gap}。" if observed_gap else "")
            + f"本场准备的升级候选是：{candidates}。这些只是可移动准备，不是固定顺序；"
            "对照最近公开对话，从中选择一项尚未发生、且最符合当前局势的变化来演出。"
            "已经发生或已经被NPC说过的候选必须跳过；若全部发生，就让现有NPC、环境或对立方"
            f"作出另一个合乎动机的新决定。这是本场第{beat_number}次主动介入，但不要向玩家报数。"
            "只落实一个新变化，不复述条件、线索和景物，不替英雄决定。"
            "不得从玩家刚调查出的细小部件中再生出一个更小的隐藏部件或新谜题；"
            "优先让已有NPC作决定、已有威胁兑现动作、已有条件得到回应，或让已开放路线真正改变镜头。"
        )

    def _extra_session_turn(self, spec: CampaignSessionSpec, speaker: str, index: int) -> str:
        hero_by_speaker = {
            "阿凛": "伊莉雅",
            "南星": "赛璃",
            "白河": "洛岚",
            "时雨": "艾薇娅",
            "澄砚": "苍祈",
        }
        hero = hero_by_speaker.get(speaker, speaker)
        location = self._session_location(spec)
        npc = self._session_npc_or_faction(spec)
        clue = self._session_scene_clue(spec)
        pressure = self._session_pressure_phrase(spec)
        templates = [
            f"{hero}先不换场，追问{npc}：如果我们现在失败，最先受害的是谁？我想听到明确答复。",
            f"{hero}回到现场可见物，仔细调查{location}里的{clue}，想弄清它刚才发生了什么变化。",
            f"{hero}把刚才得到的线索讲给队友听，问大家是继续追证据、稳住{npc}，还是先处理{pressure}。",
            f"{hero}留意附近有没有财团代理人、巡逻火光或环境异变正在靠近，但只做警戒观察，不声明结果。",
            f"{hero}向{npc}给出一个具体承诺，要求对方据此明确表态。",
            f"{hero}转向失忆旅人或最脆弱的旁观者，先确认他们能不能承受继续拖延。",
            f"{hero}试着把{clue}和刚才的公开发言连起来，提出一个能让{npc}立刻回应的判断。",
            f"{hero}不急着掷骰，先描述自己如何站位、护住退路，避免{pressure}直接切断队伍。",
            f"{hero}请时悠重申眼前已经公开、可以立刻互动的人或物，然后基于其中一个做行动。",
        ]
        return templates[(spec.number * 5 + index) % len(templates)]

    def _session_npc_or_faction(self, spec: CampaignSessionSpec) -> str:
        text = f"{spec.title} {spec.gm_opening} {' '.join(spec.expected_focus)}"
        if "守望会" in text or "驿站" in text:
            return "白花守望会会长"
        if "钟鸣" in text or "听证" in text:
            return "听证官或钟鸣医师代表"
        if "财团" in text or "采掘" in text:
            return "辉钢财团代理人"
        if "司教" in text or "灰晶" in text:
            return "苍白司教团代表"
        if "奥涅里亚" in text or "灯塔" in text:
            return "奥涅里亚港口代表"
        if "森林" in text or "奥灵" in text:
            return "沉默森林的奥灵或村社长者"
        return "眼前最关键的见证人"

    def _session_scene_clue(self, spec: CampaignSessionSpec) -> str:
        text = f"{spec.title} {spec.gm_opening} {' '.join(spec.expected_focus)}"
        if "地下" in text or "水道" in text:
            return "灰晶箱、旧水痕和阀门编号"
        if "听证" in text or "钟鸣" in text:
            return "证词记录、风铃刻痕和旁听席反应"
        if "驿站" in text or "旧路" in text:
            return "风铃回声、旧路钥匙和车辙"
        if "旅行" in text or "海岸" in text or "旧路" in text:
            return "路标、营火灰和追兵痕迹"
        if "灯塔" in text or "海图" in text:
            return "海图墨迹、灯塔光痕和潮汐记录"
        if "森林" in text or "奥灵" in text:
            return "树皮名字、苔痕和奥灵沉默的位置"
        if "炉" in text or "采掘" in text:
            return "记忆炉接口、停机协议残痕和灰晶粉尘"
        return "现场最不合拍的痕迹"

    def _session_pressure_phrase(self, spec: CampaignSessionSpec) -> str:
        text = f"{spec.title} {spec.gm_opening} {' '.join(spec.expected_focus)}"
        if "巡逻" in text or "追兵" in text:
            return "财团巡逻逼近"
        if "潮" in text or "海" in text:
            return "潮水和海风带来的危险"
        if "地下" in text or "水道" in text:
            return "地下警报和水位上涨"
        if "Boss" in text or "决战" in text or "小高潮" in text:
            return "敌方强者的下一次行动"
        if "司教" in text or "灰晶" in text:
            return "灰晶病被当成祝福传播"
        return "当前公开压力"

    def _session_gm_beat(self, spec: CampaignSessionSpec, index: int, reason: str) -> dict[str, Any]:
        assessment = self.session_progress_assessments.get(spec.number)
        runtime_need = (
            str(getattr(assessment, "next_gm_need", "") or "").strip()
            or str(getattr(assessment, "unresolved_now", "") or "").strip()
        )
        instruction = str(reason or "").strip()
        priority_instruction = instruction.startswith(
            (
                "【共同动作兑现】",
                "【玩家主导转场】",
                "【最终收束窗口】",
                "【待答复后的收束】",
                "【余波收束】",
                "【高潮提交】",
                "【局势提交】",
            )
        )
        if runtime_need and runtime_need not in instruction and not priority_instruction:
            instruction = (
                f"后台进展评估认为仍需处理：{runtime_need}。这只是方向提示，不是秘密揭示、检定成功或既定事实授权；"
                "若玩家尚未通过行动取得答案，只推进压力或提供可互动机会，不得直接替他们发现或解释。"
                f"{instruction}"
            )
        payload = {
            **self.common,
            "auto_respond": True,
            # These are authored table beats (the GM deliberately leans in),
            # not idle-monitor probes. Real idle behavior is tested separately
            # by ``_run_heartbeat_probe`` with aged transcript timestamps.
            "force": True,
            "cooldown_seconds": 0,
            "adventure_idle_seconds": 0,
            "pc_turn_idle_seconds": 0,
            "npc_turn_grace_seconds": 0,
            "instruction": instruction,
        }
        result = self.invoke(
            f"第{spec.number:02d}场GM主动节拍 {index:02d}",
            "POST",
            "/v1/session/heartbeat",
            payload,
        )
        material_receipts = [
            receipt
            for receipt in (result.get("tool_receipts") or [])
            if isinstance(receipt, dict)
            and receipt.get("ok")
            and receipt.get("state_changed")
            and receipt.get("lock_public_reply")
            and str(receipt.get("public_fallback_reply") or "").strip()
        ]
        if len(material_receipts) > 1:
            tools = "、".join(str(item.get("tool_name") or "") for item in material_receipts)
            raise RuntimeError(
                f"第{spec.number:02d}场一次GM主动节拍连续兑现了{len(material_receipts)}个公开变化：{tools}"
            )
        if not str(result.get("reply") or "").strip():
            diagnostics = {
                "action": result.get("action"),
                "reason": result.get("reason"),
                "generation_error": result.get("generation_error"),
                "generation_error_detail": result.get("generation_error_detail"),
                "expression_candidate_count": result.get("expression_candidate_count"),
                "expression_diagnostics": result.get("expression_diagnostics"),
                "beat_directive": result.get("beat_directive"),
            }
            print(
                "[FU-GM LONGRUN] blank GM beat diagnostics: "
                + json.dumps(diagnostics, ensure_ascii=False, default=str),
                flush=True,
            )
        self._record_tool_event(
            "GM自主性/导演节拍",
            f"第{spec.number:02d}场",
            "GM依据本场局面主动让NPC、威胁或环境向前走一拍；真实idle monitor另行隔离验证。",
            {
                "action": result.get("action"),
                "send_reply": result.get("send_reply"),
                "reason": result.get("reason"),
                "reply": str(result.get("reply") or "")[:300],
            },
            public=bool(result.get("send_reply")),
        )
        return result

    def _session_table_metrics(
        self,
        spec: CampaignSessionSpec,
        session_calls: list[dict[str, Any]],
        *,
        player_turn_count: int,
        gm_beat_count: int,
        routed_discussion_count: int,
    ) -> dict[str, Any]:
        game_turns = [
            call
            for call in session_calls
            if call.get("route") == "/v1/game/turn"
            or (
                call.get("route") == "/v1/message/route"
                and str(((call.get("body") or {}).get("decision") or {}).get("mode") or "") == "game"
                and str((call.get("body") or {}).get("target") or "") == "fu_gm"
            )
        ]
        replies = "\n".join(str(call.get("reply") or "") for call in session_calls)
        npc_markers = sum(replies.count(token) for token in ("会长", "摄政王", "艾蕾娜", "代表", "代理人", "医师", "守卫"))
        hidden_markers = sum(replies.count(token) for token in ("暗线", "远处", "背后", "短镜头", "仍在", "某个"))
        metric = {
            "api_calls": len(session_calls),
            "game_turn_calls": len(game_turns),
            "player_turns_authored": player_turn_count,
            "gm_autonomy_beats": gm_beat_count,
            "routed_table_discussions": routed_discussion_count,
            "estimated_table_minutes": player_turn_count * 10 + gm_beat_count * 15 + routed_discussion_count * 5,
            "npc_or_faction_mentions": npc_markers,
            "hidden_thread_surface_markers": hidden_markers,
            "meets_four_hour_proxy": player_turn_count >= self.min_table_turns_per_session and gm_beat_count >= self.gm_beats_per_session,
        }
        if not metric["meets_four_hour_proxy"]:
            self.errors.append(
                f"第{spec.number:02d}场桌面粒度不足：player_turns={player_turn_count}, gm_beats={gm_beat_count}"
            )
        return metric

    def _all_free_discussion_samples_stayed_silent(self) -> bool:
        samples = [call for call in self.calls if "玩家自由讨论" in str(call.get("label") or "")]
        return len(samples) >= self.target_sessions and all(
            str((call.get("body") or {}).get("target") or "") == "silent"
            and not bool((call.get("body") or {}).get("send_reply"))
            and not str(call.get("reply") or "").strip()
            for call in samples
        )

    def _prepare_session_runtime(self, spec: CampaignSessionSpec) -> None:
        app = self._runtime().app
        if app.conflict_manager.state.active:
            app.conflict_manager.end_scene()
        if app.scene_manager.current_scene is not None:
            app.scene_manager.end_scene(f"切入第 {spec.number} 场：{spec.title}")
        plan = app.campaign_pacing_manager.refresh_plan(
            conflict_active=False,
            boss_scene=spec.boss_session,
            force_session_number=spec.number,
        )
        if self.scripted_identities:
            self._apply_session_identity(spec, plan)
        contract = plan.dramatic_contract
        continuing = "（续）" in str(contract.title or "")
        opportunity = self._scene_opportunity_for_act(spec, 1, used_keys=set())
        location = (
            opportunity.location
            if opportunity is not None and opportunity.location
            else contract.location or self._session_location(spec)
        )
        objective = (
            opportunity.purpose
            if opportunity is not None and opportunity.purpose
            else contract.dramatic_question or "；".join(spec.expected_focus[:3]) or spec.title
        )
        summary = (
            f"续接上场未完成局面：{contract.focus_thread}"
            if continuing
            else f"{spec.arc}：{spec.title}"
        )
        app.start_scene(
            (
                f"第{spec.number:02d}场·场景1：{opportunity.title}"
                if opportunity is not None and opportunity.title
                else contract.title or f"第{spec.number:02d}场·场景1：{spec.title}"
            ),
            self._scene_type_for_act(spec, 1),
            location=location,
            participants=SceneCastCoordinator.compose(
                self.pc_names,
                opportunity=opportunity,
            ),
            objective=objective,
            summary=(
                opportunity.situation
                if opportunity is not None and opportunity.situation
                else summary
            ),
            session_opportunity_key=(
                opportunity.scene_key if opportunity is not None else ""
            ),
            session_opportunity_role=(
                opportunity.scene_role if opportunity is not None else ""
            ),
            session_opportunity_title=(
                opportunity.title if opportunity is not None else ""
            ),
            session_opportunity_purpose=(
                opportunity.purpose if opportunity is not None else ""
            ),
            session_opportunity_situation=(
                opportunity.situation if opportunity is not None else ""
            ),
        )
        self._record_tool_event(
            f"{self.target_sessions}场战役节奏器",
            f"第{spec.number:02d}场",
            "为本场刷新战役节奏计划、Boss/反派节奏和命刻压力预算。",
            {
                "title": spec.title,
                "arc": spec.arc,
                "boss_session": spec.boss_session,
                "continuing_previous_local_story": continuing,
                "plan": plan,
                "focus": spec.expected_focus,
            },
        )

    def _session_location(self, spec: CampaignSessionSpec) -> str:
        hints = {
            1: "白花碑驿站",
            2: "雾潮海岸旧路",
            3: "钟鸣公国",
            4: "白钟地下水道",
            5: "正午大钟塔",
            6: "奥涅里亚王都",
            7: "潮鸢群岛",
            8: "灯塔遗迹",
            9: "沉默森林",
            10: "树誓村社",
            11: "第七采掘城",
            12: "记忆炉矿道",
            13: "灰晶熔炉中枢",
            14: "镜线内海北岸",
            15: "白钟议事厅",
            16: "白花碑驿站",
            17: "奥涅里亚灯塔舰队",
            18: "第七采掘城外环",
            19: "记忆集中协议塔",
            20: "碎月炉心",
        }
        if spec.number in hints:
            return hints[spec.number]
        extended = [
            "奥涅里亚外港",
            "钟鸣公国北钟楼",
            "沉默森林深处",
            "第七采掘城外环",
            "潮鸢群岛雾港",
            "镜线内海航路",
            "赤羽旧王都遗址",
            "碎月炉心外层",
        ]
        return extended[(spec.number - 21) % len(extended)]

    def _session_report(
        self,
        spec: CampaignSessionSpec,
        ended: dict[str, Any],
        audit: dict[str, Any],
        *,
        level_ups: list[dict[str, Any]],
        resource_before: dict[str, dict[str, Any]],
        resource_after: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        app = self._runtime().app
        pacing = audit.get("campaign_pacing") if isinstance(audit, dict) else {}
        story_arc = audit.get("story_arc") if isinstance(audit, dict) else {}
        summary = ended.get("summary") if isinstance(ended, dict) else {}
        experience = ended.get("experience") if isinstance(ended, dict) else {}
        table_metric = self.session_table_metrics.get(spec.number, {})
        scene_metric = self.session_scene_metrics.get(spec.number, {})
        summary_text = summary.get("short_memory") or summary.get("public_summary") or ""
        progress = self.session_progress_assessments.get(spec.number, SessionProgressAssessment())
        contract = app.story_arc_manager.state.current_pacing_plan.dramatic_contract
        contract_quality = self._contract_quality_inputs(contract)
        resource_delta: dict[str, dict[str, int]] = {}
        for name in self.pc_names:
            before = resource_before.get(name, {})
            after = resource_after.get(name, {})
            resource_delta[name] = {
                key: int(after.get(key, 0) or 0) - int(before.get(key, 0) or 0)
                for key in ("hp", "mp", "inventory_points", "fabula_points", "xp", "level")
            }
        return {
            "number": spec.number,
            "title": spec.title,
            "arc": spec.arc,
            "boss_session": spec.boss_session,
            "table_metric": table_metric,
            "scene_metric": scene_metric,
            "summary": summary_text,
            "episode_contract": {
                "specific_location": bool(self._session_location(spec)),
                "distinct_signature_image": bool(contract.signature_image),
                "concrete_signature_image": bool(contract.signature_image)
                and not any(
                    marker in contract.signature_image
                    for marker in self.SIGNATURE_META_MARKERS
                ),
                "local_dramatic_question": bool(contract.dramatic_question),
                "opposition_has_goal": bool(contract.opposition_goal),
                "playable_npc_cast": bool(contract_quality["prepared_npc_names"])
                and all(
                    name not in self.GENERIC_NPC_NAMES
                    for name in contract_quality["prepared_npc_names"]
                ),
                "scene_cast_has_no_placeholders": all(
                    name not in self.GENERIC_NPC_NAMES
                    for name in contract_quality["scene_cast_names"]
                ),
                "clue_sources_have_no_placeholders": all(
                    source not in self.GENERIC_NPC_NAMES
                    for source in contract_quality["clue_sources"]
                ),
                "payoff_prepared": bool(contract.possible_payoffs),
                "urgent_event": progress.concrete_consequence or progress.reversal_reached,
                "multiple_scenes": bool(scene_metric.get("multiple_scenes")),
                "stage_result": progress.local_question_changed
                or progress.local_question_resolved
                or (progress.deliberate_cliffhanger and progress.reversal_reached),
                "inherits_previous_result": spec.number == 1 or bool(self._previous_session_summary),
                "memory_image": bool(progress.memory_image),
                "memory_choice": bool(progress.memory_choice),
                "memory_consequence": bool(progress.memory_consequence),
                "previous_consequence_recalled": progress.previous_consequence_recalled,
            },
            "session_completion": dict(self.session_completion_results.get(spec.number, {})),
            "progress_assessment": asdict(progress),
            "memory_anchor": {
                "image": progress.memory_image,
                "choice": progress.memory_choice,
                "consequence": progress.memory_consequence,
            },
            "experience": experience or {},
            "level_ups": level_ups,
            "resources_before": resource_before,
            "resources_after": resource_after,
            "resource_delta": resource_delta,
            "phase": story_arc.get("phase") if isinstance(story_arc, dict) else "",
            "pacing_plan": pacing.get("current_plan") if isinstance(pacing, dict) else {},
            "foreground_clocks": pacing.get("foreground_clock_names") if isinstance(pacing, dict) else [],
            "background_pressure": pacing.get("background_pressure_names") if isinstance(pacing, dict) else [],
            "villain_pressure": story_arc.get("villain_pressure", [])[:4] if isinstance(story_arc, dict) else [],
            "active_clocks": [clock.name for clock in app.clock_manager.all() if clock.current < clock.max_segments],
        }

    @staticmethod
    def _contract_quality_inputs(contract: Any) -> dict[str, list[str]]:
        return {
            "prepared_npc_names": [
                str(item.name or "").strip()
                for item in list(getattr(contract, "important_npcs", []) or [])
            ],
            "scene_cast_names": [
                str(name or "").strip()
                for scene in list(getattr(contract, "potential_scenes", []) or [])
                for name in [
                    *list(getattr(scene, "npc_names", []) or []),
                    *list(getattr(scene, "required_npc_names", []) or []),
                ]
            ],
            "clue_sources": [
                str(item.source or "").strip()
                for item in list(getattr(contract, "clue_routes", []) or [])
            ],
        }

    def _run_astrbot_bridge_smoke(self, stage: str) -> None:
        main_state_before = self._astrbot_main_state_fingerprint()
        server = make_server("127.0.0.1", 0, service=self.service)
        port = int(server.server_address[1])
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result = asyncio.run(self._astrbot_bridge_smoke_async(stage, port))
        except Exception as exc:  # pragma: no cover - smoke harness diagnostics
            result = {"stage": stage, "ok": False, "error": str(exc), "traceback": traceback.format_exc()}
            self.errors.append(f"AstrBot bridge smoke failed at {stage}: {exc}")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        main_state_after = self._astrbot_main_state_fingerprint()
        main_campaign_unchanged = main_state_after == main_state_before
        result["main_campaign_unchanged"] = main_campaign_unchanged
        if not main_campaign_unchanged:
            result["main_state_before"] = main_state_before
            result["main_state_after"] = main_state_after
            result["ok"] = False
            self.errors.append(
                f"AstrBot bridge smoke contaminated the main campaign at {stage}."
            )
        probe_campaign_id = str(result.get("probe_campaign_id") or "")
        if probe_campaign_id:
            probe_gate = self.service.session_gates.deactivate(
                probe_campaign_id,
                str(result.get("probe_channel_id") or ""),
                str(result.get("probe_session_id") or ""),
                reason=f"AstrBot bridge probe completed: {stage}",
            )
            result["probe_gate_closed"] = probe_gate.status == "inactive"
        self.astrbot_bridge_results.append(result)
        self._record_tool_event("AstrBot桥接真实HTTP冒烟", stage, "通过插件真实 HTTP 请求访问 FU-GM 服务。", result)

    async def _astrbot_bridge_smoke_async(self, stage: str, port: int) -> dict[str, Any]:
        self._install_astrbot_test_stubs()
        from integrations.astrbot.fu_gm_bridge.main import FuGmBridgePlugin

        probe_index = len(self.astrbot_bridge_results) + 1
        probe_campaign_id = f"{self.campaign_id}__astrbot_probe_{probe_index}"
        probe_session_id = f"{self.session_id}-astrbot-probe-{probe_index}"
        probe_channel_id = f"{self.channel_id}-astrbot-probe-{probe_index}"
        probe_runtime = self.service._runtime(probe_campaign_id, auto_load=False)
        probe_runtime.app.session_zero_manager.start()
        self.service.session_gates.activate(
            probe_campaign_id,
            probe_channel_id,
            probe_session_id,
            status="session_zero",
            reason=f"AstrBot bridge isolated probe: {stage}",
        )
        plugin = FuGmBridgePlugin(
            None,
            {
                "server_url": f"http://127.0.0.1:{port}",
                "campaign_id": probe_campaign_id,
                "default_session_id": probe_session_id,
                "http_timeout_seconds": 30,
                "log_http_timing": False,
                "enable_message_buffer": False,
                "campaign_bindings_path": str(self.run_root / "astrbot_channel_campaigns.json"),
                "user_campaign_bindings_path": str(self.run_root / "astrbot_user_campaigns.json"),
            },
        )
        event = _FakeAstrEvent(
            message_str="/fugm_health",
            group_id=probe_channel_id,
            session_id=probe_session_id,
            sender_id="astrbot-smoke-user",
            sender_name="AstrBot测试员",
        )
        health = await plugin._get("/health")
        status_payload = plugin._payload(event, message="", mode="status")
        status = await plugin._post("/v1/session/status", status_payload)
        route_payload = plugin._payload(event, message="我先听大家讨论一下。", mode="auto")
        route = await plugin._post("/v1/message/route", route_payload)
        command_replies: list[str] = []
        async for reply in plugin.fugm_health(event):
            command_replies.append(getattr(reply, "text", str(reply)))
        ok = bool(health.get("ok")) and status.get("ok") is not False and route.get("ok") is not False
        if not ok:
            self.errors.append(f"AstrBot bridge smoke failed at {stage}: health={health}, status={status}, route={route}")
        return {
            "stage": stage,
            "ok": ok,
            "probe_campaign_id": probe_campaign_id,
            "probe_session_id": probe_session_id,
            "probe_channel_id": probe_channel_id,
            "health": health,
            "status": {
                "ok": status.get("ok", True),
                "campaign_id": status.get("campaign_id"),
                "gate_status": (status.get("gate") or {}).get("status") if isinstance(status.get("gate"), dict) else "",
            },
            "route": {
                "ok": route.get("ok", True),
                "target": route.get("target"),
                "send_reply": route.get("send_reply"),
                "stop_astrbot": route.get("stop_astrbot"),
            },
            "command_replies": command_replies,
        }

    def _astrbot_main_state_fingerprint(self) -> dict[str, Any]:
        """Capture the main-table identities that a transport probe must not mutate."""

        runtime = self._runtime()
        session_zero = runtime.app.session_zero_manager.state
        return {
            "participants": [participant.name for participant in session_zero.participants],
            "session_zero_transcript_length": len(session_zero.transcript),
            "hero_draft_keys": sorted(runtime.app.world_state.world_profile.hero_drafts),
            "present_players": list(runtime.app.world_state.present_players),
            "character_names": sorted(
                character.name for character in runtime.app.character_manager.all()
            ),
        }

    def _run_heartbeat_probe(self) -> None:
        """Exercise the idle monitor without contaminating the main campaign."""

        heartbeat_campaign = f"{self.campaign_id}_heartbeat_probe"
        channel_id = f"{self.channel_id}-heartbeat"
        runtime = self.service._runtime(heartbeat_campaign)

        def old_assistant(session_id: str, content: str) -> None:
            original_now = runtime.log_manager._now
            runtime.log_manager._now = lambda: "2020-01-01T00:00:00+00:00"
            try:
                runtime.log_manager.append_message(
                    heartbeat_campaign,
                    session_id,
                    speaker=self.service.gm_name,
                    content=content,
                    role="assistant",
                    channel_id=channel_id,
                )
            finally:
                runtime.log_manager._now = original_now

        # Session 0: the GM asked something, table is idle, heartbeat should nudge lightly.
        self.service.session_gates.activate(heartbeat_campaign, channel_id, "hb-session-zero", status="session_zero")
        old_assistant("hb-session-zero", "大家可以先说说这个世界的魔法和科技是什么关系。")
        self._heartbeat_invoke(
            "心跳探针 第零章轻推",
            "hb-session-zero",
            {
                "session_zero_idle_seconds": 1,
                "cooldown_seconds": 0,
            },
        )

        # Free scene: after a GM output, heartbeat should trigger an autonomous GM beat.
        self.service.session_gates.activate(heartbeat_campaign, channel_id, "hb-free-scene", status="adventure")
        runtime.app.start_scene(
            "心跳自由场景：风铃廊对峙",
            location="白花碑驿站",
            participants=["伊莉雅", "白花守望会会长"],
            objective="让守望会给出明确条件",
            summary="会长正等英雄说明失名旅人的去处。",
        )
        old_assistant("hb-free-scene", "会长把旧路钥匙压在掌心里，等你们给出承诺。")
        self._heartbeat_invoke(
            "心跳探针 自由场景GM主动节拍",
            "hb-free-scene",
            {
                "adventure_idle_seconds": 1,
                "cooldown_seconds": 0,
            },
        )

        # PC turn: reminder only, no action should be taken for the player.
        runtime.app.conflict_manager.end_scene()
        for name, traits in [("伊莉雅", ["pc"]), ("白花守望会会长", ["npc"])]:
            if not runtime.app.character_manager.exists(name):
                runtime.app.character_manager.add(
                    Character(
                        name=name,
                        attributes={"DEX": 8, "INS": 8, "MIG": 8, "WLP": 8},
                        max_hp=40,
                        hp=40,
                        max_mp=30,
                        mp=30,
                        traits=traits,
                    )
                )
        self.service.session_gates.activate(heartbeat_campaign, channel_id, "hb-pc-turn", status="adventure")
        runtime.app.conflict_manager.start_scene("心跳玩家回合", ["伊莉雅", "白花守望会会长"])
        old_assistant("hb-pc-turn", "镜头推进到伊莉雅。")
        self._heartbeat_invoke(
            "心跳探针 玩家回合只提醒",
            "hb-pc-turn",
            {
                "pc_turn_idle_seconds": 1,
                "cooldown_seconds": 0,
            },
        )

        # NPC turn: heartbeat may auto-resolve the blocking non-player turn.
        runtime.app.conflict_manager.end_scene()
        self.service.session_gates.activate(heartbeat_campaign, channel_id, "hb-npc-turn", status="adventure")
        runtime.app.conflict_manager.start_scene("心跳NPC回合", ["白花守望会会长", "伊莉雅"])
        old_assistant("hb-npc-turn", "会长仍在衡量你们。")
        self._heartbeat_invoke(
            "心跳探针 NPC回合自动推进",
            "hb-npc-turn",
            {
                "npc_turn_grace_seconds": 1,
                "cooldown_seconds": 0,
            },
        )

        actions = [str(item.get("action") or "") for item in self.heartbeat_results]
        ok = all(item.get("ok") for item in self.heartbeat_results) and {
            "session_zero_nudge",
            "free_scene_beat",
            "pc_turn_reminder",
            "npc_turn",
        }.issubset(set(actions))
        if not ok:
            self.errors.append(f"心跳探针未完整覆盖预期动作：{actions}")
        self._record_tool_event(
            "心跳/idle monitor 探针",
            "战役长测结束后",
            "验证第零章轻推、自由场景 GM 主动节拍、玩家回合提醒、NPC 回合自动推进。",
            {"ok": ok, "actions": actions, "results": self.heartbeat_results},
        )

    def _heartbeat_invoke(self, label: str, session_id: str, extra_payload: dict[str, Any]) -> dict[str, Any]:
        payload = {
            "campaign_id": f"{self.campaign_id}_heartbeat_probe",
            "session_id": session_id,
            "channel_id": f"{self.channel_id}-heartbeat",
            "auto_respond": True,
            **extra_payload,
        }
        result = self.invoke(label, "POST", "/v1/session/heartbeat", payload)
        self.heartbeat_results.append(
            {
                "label": label,
                "ok": bool(result.get("ok")),
                "action": result.get("action"),
                "send_reply": result.get("send_reply"),
                "reply": result.get("reply"),
                "reason": result.get("reason"),
                "current_actor": result.get("current_actor"),
            }
        )
        return result

    def _install_astrbot_test_stubs(self) -> None:
        if "astrbot.api.event" in sys.modules and "astrbot.api.star" in sys.modules:
            return

        astrbot = sys.modules.setdefault("astrbot", types.ModuleType("astrbot"))
        api = sys.modules.setdefault("astrbot.api", types.ModuleType("astrbot.api"))
        event_module = types.ModuleType("astrbot.api.event")
        star_module = types.ModuleType("astrbot.api.star")

        class _Filter:
            class EventMessageType:
                ALL = "ALL"

            def command(self, *_args: Any, **_kwargs: Any):
                return lambda func: func

            def event_message_type(self, *_args: Any, **_kwargs: Any):
                return lambda func: func

        class _Star:
            def __init__(self, context: Any = None) -> None:
                self.context = context

        class _MessageChain:
            def __init__(self) -> None:
                self.chain: list[Any] = []

            def message(self, text: str) -> "_MessageChain":
                self.chain.append(types.SimpleNamespace(text=text))
                return self

        class _StarTools:
            @classmethod
            async def send_message_by_id(
                cls,
                _type: str,
                _id: str,
                _message_chain: Any,
                platform: str = "aiocqhttp",
            ) -> None:
                del cls, platform

        def _register(*_args: Any, **_kwargs: Any):
            return lambda cls: cls

        event_module.AstrMessageEvent = object
        event_module.MessageChain = _MessageChain
        event_module.MessageEventResult = object
        event_module.filter = _Filter()
        star_module.Context = object
        star_module.Star = _Star
        star_module.StarTools = _StarTools
        star_module.register = _register

        setattr(astrbot, "api", api)
        setattr(api, "event", event_module)
        setattr(api, "star", star_module)
        sys.modules["astrbot.api.event"] = event_module
        sys.modules["astrbot.api.star"] = star_module

    def _campaign_sessions(self) -> list[CampaignSessionSpec]:
        base = [
            CampaignSessionSpec(
                1,
                "白花碑驿站的迟响",
                "第一幕：边境钟声",
                "第一场开场。请时悠先描述白花碑驿站风铃廊、失忆旅人、白花守望会，以及尚在远处逼近、还未抵达的财团巡逻压力，然后交给我们行动。",
                [
                    ("阿凛", "伊莉雅先表明来意：保护失忆旅人，不夺走白花守望会的秘密。她观察会长是否愿意谈条件。"),
                    ("南星", "赛璃调查失忆旅人的灰晶光泽和听到风铃时的反应，想确认记忆是否被外力牵引。"),
                    ("白河", "洛岚蹲下检查旧路闸门旁的车辙，想从泥痕深浅估计财团巡逻队还有多久抵达。"),
                    (
                        "时雨",
                        "艾薇娅请会长把今夜财团追索旅人的经过写进封路记录，并询问守望会是否愿意派人到钟鸣公国听证作证。",
                    ),
                    ("澄砚", "苍祈问风铃廊旁的潮生藤：昨夜谁带着失忆旅人经过这里？"),
                    ("南星", "赛璃计划御魂仪式【风铃回声】，只回放昨夜公开经过的脚步和名字，不直接伤害任何人。"),
                    ("阿凛", "伊莉雅把盾横在旅人与门口之间，示意巡守开启旧路；她准备护着旅人先撤，不让财团碰到他。"),
                ],
                ["取得旧路", "建立财团巡逻压力", "揭示风铃线索"],
            ),
            CampaignSessionSpec(
                2,
                "雾潮旧路的第一夜",
                "第一幕：边境钟声",
                "第二场开场。请描述队伍离开驿站后的旧路、雾潮海岸和追兵暂时被甩开的余波，让我们决定旅行与休息安排。",
                [
                    ("白河", "洛岚检查旧路信标，想找出一条避开财团哨站的路线。"),
                    ("南星", "大家同意在背风石坎后搭起魔法帐篷短休；由赛璃支付物资，并照看旅人。"),
                    ("阿凛", "伊莉雅巡夜，想确认有没有守望会留下的暗号或追兵火光。"),
                    ("时雨", "艾薇娅写给钟鸣公国的临时通行信，避免抵达后被当作走私者。"),
                    (
                        "澄砚",
                        "苍祈检查路边潮生藤、苔痕和土壤扰动，想判断队伍踪迹有没有被财团标记或追踪；需要的话用洞察+意志检定。",
                    ),
                ],
                ["旅行", "休息", "追兵后果"],
            ),
            CampaignSessionSpec(
                3,
                "钟鸣公国的听证",
                "第一幕：边境钟声",
                "第三场开场。请描述钟鸣公国正午大钟、听证厅和各派代表，重点放在谁愿意听失忆旅人作证。",
                [
                    ("时雨", "艾薇娅请求公开听证，先给旅人一个安全座位，再要求财团代理人出示收购记忆的合法文书。"),
                    ("南星", "赛璃用御魂知识解释灰晶病不是灵魂升格，请求钟鸣医师协会协助。"),
                    ("白河", "洛岚拿出自己知道的记忆炉设计痕迹，但不公开全部停机协议。"),
                    ("阿凛", "伊莉雅观察听证厅里谁对赤羽遗民这个词反应过度。"),
                    ("澄砚", "苍祈留意正午大钟是否回应旅人的名字。"),
                ],
                ["社交冲突", "派系态度", "第一条公开证据"],
            ),
            CampaignSessionSpec(
                4,
                "白钟地下水道",
                "第一幕：边境钟声",
                "第四场开场。请把听证后的线索引向钟鸣地下水道：那里有被偷运的灰晶样本和旧王国水道。",
                [
                    ("白河", "洛岚带队进入地下水道，检查管线和魔导阀门。"),
                    ("阿凛", "伊莉雅走在前面防御，保护赛璃和旅人免受机关伤害。"),
                    ("南星", "赛璃调查灰晶样本是否含有人的名字或回忆片段。"),
                    ("澄砚", "苍祈和地下苔藓交流，询问最近是否有财团运输箱经过。"),
                    ("时雨", "艾薇娅记录证据链，准备回到地面后交给听证官。"),
                    ("白河", "洛岚在通道岔口做标记，并提醒大家优先避开警报线，不要在地下水道里惊动整支巡逻队。"),
                ],
                ["地下城探索", "证据链", "危险命刻"],
            ),
            CampaignSessionSpec(
                5,
                "正午大钟塔的小高潮",
                "第一幕：边境钟声",
                "第五场开场。请描述正午大钟塔被财团代理人封锁，第一幕小 Boss 或强代理人正在夺取证据。",
                [
                    ("阿凛", "伊莉雅冲上钟塔台阶，优先保护证人而不是追击敌人。"),
                    ("白河", "洛岚用洞察+敏捷做普通检定，检查财团干扰器的接线并尝试关掉它。"),
                    ("南星", "赛璃施放屏障保护听证官和旅人。"),
                    ("时雨", "艾薇娅试图说服钟鸣卫队别被财团命令带偏。"),
                    ("澄砚", "苍祈召唤已契约的奥灵帮助压住钟声中的虚假记忆。"),
                    ("阿凛", "若进入 Boss 机制，可以使用两个命刻，但请明确一个是保护证据，一个是敌人逃脱。"),
                ],
                ["第一幕小Boss", "证据保护", "反派代理人"],
                boss_session=True,
            ),
            CampaignSessionSpec(
                6,
                "奥涅里亚摄政王的邀请",
                "第二幕：海图与灯塔",
                "第六场开场。请描述奥涅里亚港口、灯塔舰队与摄政王邀请，表现政治压力而不是立刻战斗。",
                [
                    ("时雨", "艾薇娅请求私下会见摄政王，想知道他为什么信任辉钢财团。"),
                    ("阿凛", "伊莉雅观察王室卫兵和港口行会之间的紧张。"),
                    ("白河", "洛岚查看港口里财团设备是否和第七采掘城同源。"),
                    ("南星", "赛璃去港口医舍询问灰晶病人在奥涅里亚的情况。"),
                    ("澄砚", "苍祈用洞察+洞察进行普通检定，观察海风、潮线与礁石盐痕，寻找消失岛屿留下的方向。"),
                ],
                ["派系政治", "奥涅里亚", "摄政王动机"],
            ),
            CampaignSessionSpec(
                7,
                "潮鸢群岛的空白海图",
                "第二幕：海图与灯塔",
                "第七场开场。请描述飞翼船追着季风离港，海图上有一座刚被所有人忘记的岛。",
                [
                    ("南星", "赛璃负责旅行掷骰和补给检查，想让队伍安全抵达空白海域。"),
                    ("白河", "洛岚维护飞翼船的灵魂晶炉，不让它被财团远程标记。"),
                    ("时雨", "艾薇娅向船员打听哪些岛在归潮祭后失踪。"),
                    ("阿凛", "伊莉雅训练船员遇到财团船时如何疏散。"),
                    ("澄砚", "苍祈询问海鸟和风神祭司，找出那座岛最后一次被看见的位置。"),
                ],
                ["旅行", "海图谜团", "群岛线索"],
            ),
            CampaignSessionSpec(
                8,
                "灯塔遗迹下的回声",
                "第二幕：海图与灯塔",
                "第八场开场。请描述一座半沉灯塔遗迹，灯光照到不存在的岛影，里面有可探索区域。",
                [
                    ("阿凛", "伊莉雅先确认遗迹结构是否会坍塌，要求大家绑好绳索。"),
                    ("南星", "赛璃调查灯塔光束照出的岛影是否是灵魂残响。"),
                    ("白河", "洛岚拆解灯塔旧核心，寻找与记忆炉共用的零件。"),
                    ("时雨", "艾薇娅记录王室海图抵押给财团的证据。"),
                    ("澄砚", "苍祈向灯塔中的奥灵承认自己曾失约，询问如何让岛名回来。"),
                ],
                ["地下城/遗迹", "灯塔证据", "岛名线索"],
            ),
            CampaignSessionSpec(
                9,
                "沉默森林拒绝祈祷",
                "第二幕：海图与灯塔",
                "第九场开场。请描述队伍回到大陆东南的沉默森林，树皮上写着活人的名字。",
                [
                    ("澄砚", "苍祈主动向树誓村社说明自己失约的事，请求一次补救机会。"),
                    ("阿凛", "伊莉雅尊重村社规矩，不以公国或财团法律压人。"),
                    ("南星", "赛璃调查树皮名字与灰晶病人的联系。"),
                    ("白河", "洛岚承认自己懂记忆炉，愿意帮村社判断财团是否动过森林边界。"),
                    ("时雨", "艾薇娅尝试让村社代表愿意参加下一次公开会谈。"),
                ],
                ["角色主题", "奥灵关系", "森林线索"],
            ),
            CampaignSessionSpec(
                10,
                "碎月真相的中盘揭示",
                "第三幕：记忆之价",
                "第十场开场。请让沉默森林给出一枚能颠覆理解的证据：碎月、赤羽旧王都和记忆炉之间有关。",
                [
                    ("澄砚", "苍祈设计一个轻微效力、小范围的奥灵系仪式，用意志+意志请求树皮名字显示第一位被记忆炉吞掉的人。"),
                    ("南星", "赛璃用御魂仪式确认这些名字是否还连着灵魂之河。"),
                    ("白河", "洛岚终于公开紧急停机协议的一部分：它回应赤羽遗民的歌。"),
                    ("阿凛", "伊莉雅追问姐姐的名字为何也在风铃和树皮上。"),
                    ("时雨", "艾薇娅整理中盘揭示后的选择：公开真相、潜入第七采掘城，或先争取奥涅里亚舰队。"),
                ],
                ["中盘揭示", "碎月真相", "下一幕方向"],
                boss_session=True,
            ),
            CampaignSessionSpec(
                11,
                "第七采掘城外环",
                "第三幕：记忆之价",
                "第十一场开场。请描述第七采掘城外环、矿工宿舍和财团宣传，让队伍决定如何潜入。",
                [
                    ("白河", "洛岚伪装成维修工进入外环，寻找旧同事和停机协议入口。"),
                    ("时雨", "艾薇娅用外交身份拖住财团接待员，争取时间。"),
                    ("阿凛", "伊莉雅寻找灰晶病患者安置区，确认是否有人被强行转移。"),
                    ("南星", "赛璃去临时医站，想知道病人是否被诱导签署记忆买卖契约。"),
                    ("澄砚", "苍祈让奥灵记住矿工的名字，避免他们被公开记忆改写。"),
                ],
                ["潜入", "第七采掘城", "病人选择"],
            ),
            CampaignSessionSpec(
                12,
                "记忆炉矿道",
                "第三幕：记忆之价",
                "第十二场开场。请描述矿道里的记忆残响、运输轨道和灰晶熔炉下层，让地下城探索接上潜入。",
                [
                    ("阿凛", "伊莉雅防御并带队通过运输轨道，不让矿工被卷入。"),
                    ("白河", "洛岚破解矿道门禁，寻找能进入熔炉中枢的线路。"),
                    ("南星", "赛璃调查残响中的名字，判断哪些人还可能被救回。"),
                    ("时雨", "艾薇娅用谴责动摇一名财团小队长，让他交出转运清单。"),
                    ("澄砚", "苍祈与矿道深处的怪物交流，试图绕开不必要战斗。"),
                ],
                ["地下城", "矿道危险", "救援线索"],
            ),
            CampaignSessionSpec(
                13,
                "灰晶熔炉中枢",
                "第三幕：记忆之价",
                "第十三场开场。请描述灰晶熔炉中枢和一个强敌守护者，目标是取得停机协议完整钥匙。",
                [
                    ("白河", "洛岚启动工程【熔炉停机协议破解】，把停机协议从熔炉子系统里拉出来。"),
                    ("阿凛", "伊莉雅攻击熔炉守护者，优先打断它的多重攻击。"),
                    ("南星", "赛璃治疗被熔炉残响灼伤的队友。"),
                    ("时雨", "艾薇娅尝试用言语让守护者认出自己保护的是活人名字，不是财团资产。"),
                    ("澄砚", "苍祈用暗影击承担代价，给洛岚争取最后一次破解机会。"),
                ],
                ["强敌", "停机协议", "高压命刻"],
                boss_session=True,
            ),
            CampaignSessionSpec(
                14,
                "危机：记忆集中协议公开启动",
                "第四幕：世界开始遗忘",
                "第十四场开场。请描述艾蕾娜公开启动记忆集中协议，世界各地开始忘记赤羽旧王都相关名字。",
                [
                    ("时雨", "艾薇娅召集奥涅里亚、钟鸣公国和树誓村社代表，说明现在不能再旁观。"),
                    ("南星", "赛璃建立危机救护点，决定哪些记忆残响优先稳定。"),
                    ("阿凛", "伊莉雅面对姐姐名字被再次抹去的风险，仍坚持先救活人。"),
                    ("白河", "洛岚判断协议启动后还有几个节点能被切断。"),
                    ("澄砚", "苍祈请求沉默森林暂时恢复祈祷回应，代价由他承担。"),
                ],
                ["危机升级", "多派系联盟", "反派计划公开"],
            ),
            CampaignSessionSpec(
                15,
                "白钟议事厅的反派登场",
                "第四幕：世界开始遗忘",
                "第十五场开场。请让监察官艾蕾娜亲自出现在白钟议事厅，用她的理由挑战队伍。",
                [
                    ("时雨", "艾薇娅要求艾蕾娜说清楚：集中管理记忆到底保护了谁，又牺牲了谁。"),
                    ("阿凛", "伊莉雅不急着拔剑，她问艾蕾娜是否记得赤羽旧王都孩子们的名字。"),
                    ("白河", "洛岚公开自己参与过记忆炉设计，愿意承担证词后果。"),
                    ("南星", "赛璃用御魂术稳定被艾蕾娜话语动摇的听众。"),
                    ("澄砚", "苍祈准备在谈判破裂时召唤奥灵保护会议现场。"),
                    ("阿凛", "伊莉雅用青铜盾封住艾蕾娜退向议事厅侧门的路线，但不替她决定是否逃脱。"),
                ],
                ["反派亲自登场", "终结点", "高代价选择"],
                boss_session=True,
            ),
            CampaignSessionSpec(
                16,
                "战后喘息与角色回声",
                "第四幕：世界开始遗忘",
                "第十六场开场。请给队伍一个喘息场景：治疗、修理、购物、升级选择和角色主题对话。",
                [
                    ("南星", "赛璃安排大家休息，检查 HP、MP、物资点和灰晶病人情况。"),
                    ("白河", "洛岚启动项目【记忆炉停机模拟器】，准备用于最终潜入。"),
                    ("阿凛", "伊莉雅和赛璃谈姐姐名字的事，确认自己不是为了复仇才走到这里。"),
                    ("时雨", "艾薇娅清点现有物资并整理联盟承诺，防止最终战后互相推责。"),
                    ("澄砚", "苍祈向奥灵履行一个小承诺，把一个被遗忘的名字刻回树皮。"),
                ],
                ["休息", "工程", "角色主题"],
            ),
            CampaignSessionSpec(
                17,
                "灯塔舰队与树誓盟约",
                "第五幕：碎月炉心",
                "第十七场开场。请描述奥涅里亚灯塔舰队、树誓村社和钟鸣公国三方会盟，终局前需要争取支援。",
                [
                    ("时雨", "艾薇娅主持会盟，确保每方知道自己能贡献什么，也知道不会被牺牲。"),
                    ("阿凛", "伊莉雅请求守望会派出旧路向导，但不强迫他们参战。"),
                    ("南星", "赛璃请求医师协会准备记忆恢复后的救护。"),
                    ("白河", "洛岚展示停机模拟器，说明最终潜入需要飞翼船和钟鸣大钟同步。"),
                    ("澄砚", "苍祈请求沉默森林奥灵在最终战中记住所有参战者名字。"),
                ],
                ["盟友", "终局准备", "支援清单"],
            ),
            CampaignSessionSpec(
                18,
                "第七采掘城外环突入",
                "第五幕：碎月炉心",
                "第十八场开场。请描述联盟从海陆两路逼近第七采掘城，队伍负责打开外环入口。",
                [
                    ("阿凛", "伊莉雅带队保护平民撤离，不把最终战变成无差别破坏。"),
                    ("白河", "洛岚用停机模拟器打开外环入口，并承担失败会触发警报的风险。"),
                    ("南星", "赛璃维持屏障，保护正在撤离的灰晶病人。"),
                    ("时雨", "艾薇娅对财团守卫喊话，让愿意撤退的人放下武器。"),
                    ("澄砚", "苍祈让奥灵标记安全路线，避免队伍迷失在被改写的地图里。"),
                ],
                ["终局突入", "撤离平民", "入口命刻"],
            ),
            CampaignSessionSpec(
                19,
                "记忆集中协议塔",
                "第五幕：碎月炉心",
                "第十九场开场。请描述协议塔内部，艾蕾娜的计划进入倒计时，但最终真相还差最后一块。",
                [
                    ("白河", "洛岚推进关闭协议塔的目标命刻，使用洞察+敏捷处理控制台。"),
                    ("阿凛", "伊莉雅防御并掩护洛岚，承受协议塔守卫的攻击。"),
                    ("南星", "赛璃用治愈术和御魂术稳定被协议塔拉扯的旅人。"),
                    ("时雨", "艾薇娅试图说服艾蕾娜：记住灾难不等于剥夺所有人的选择。"),
                    ("澄砚", "苍祈准备遣散奥灵，把机会留给最终炉心前的真名回响。"),
                ],
                ["协议塔倒计时", "艾蕾娜的最后计划", "清晰可阻止的压力"],
                boss_session=True,
            ),
            CampaignSessionSpec(
                20,
                "碎月炉心与名字归还",
                "终幕：尾声",
                "第二十场终局开场。请描述碎月炉心、艾蕾娜最后的选择、旅人真实身份和所有被遗忘名字的回响。",
                [
                    ("阿凛", "伊莉雅面对姐姐名字的真相，选择先保护所有人的记忆自由，再决定自己是否原谅。"),
                    ("白河", "洛岚输入完整停机协议，并公开自己过去参与的罪。"),
                    ("南星", "赛璃引导灵魂之河接住归还的名字，避免记忆回归变成伤害。"),
                    ("时雨", "艾薇娅给艾蕾娜最后一次投降和见证真相的机会。"),
                    ("澄砚", "苍祈召唤与自己缔结契约的魔典奥灵，请它读出并记住所有被归还的名字。"),
                    ("阿凛", "最终威胁已经被压下，请时悠给出结局、世界变化、每名英雄的尾声和二十场战役结算。"),
                ],
                ["碎月炉心决战", "被归还的名字", "英雄们的选择余波"],
                boss_session=True,
            ),
        ]
        if self.target_sessions <= len(base):
            return base[: self.target_sessions]
        sessions = base[:-1]
        for number in range(len(base), self.target_sessions):
            sessions.append(self._extended_campaign_session(number))
        sessions.append(self._final_campaign_session(self.target_sessions, base[-1]))
        return sessions

    def _extended_campaign_session(self, number: int) -> CampaignSessionSpec:
        ratio = number / max(1, self.target_sessions)
        arc = self._arc_label_for_session(number)
        location = self._session_location(CampaignSessionSpec(number, "", arc, "", []))
        boss_interval = max(4, round(self.target_sessions / 6))
        is_boss = number % boss_interval == 0 or number == self.target_sessions - 1
        is_dungeon = number % 7 == 0
        if ratio < 0.45:
            title = f"{location}的第二条线索"
            opening = (
                f"第{number}场开场。请从上一场后果切入，让队伍在{location}看到一个公开后果，"
                "并给出两个可行动方向：追踪线索或安抚受影响的人。"
            )
            focus = ["地点回访", "线索推进", "反派远景"]
        elif ratio < 0.68:
            title = f"{location}的真相裂缝"
            opening = (
                f"第{number}场开场。请让{location}出现一枚能改写理解的证据，"
                "但不要直接公布全部真相；让玩家通过调查、交涉或仪式获得它。"
            )
            focus = ["中盘揭示", "可移动线索", "角色主题"]
        elif ratio < 0.86:
            title = f"{location}的危机前线"
            opening = (
                f"第{number}场开场。请让艾蕾娜或辉钢财团的计划在{location}留下可见后果，"
                "只把一个主威胁命刻放到前台，除非玩家主动扩大风险。"
            )
            focus = ["危机前线", "联盟代价", "反派压力"]
        else:
            title = f"{location}的终局准备"
            opening = (
                f"第{number}场开场。请把{location}作为终局前的关键准备点，"
                "让每名英雄都有一个能影响最终战的选择。"
            )
            focus = ["终局准备", "盟友支援", "最终选择"]
        turns = [
            ("阿凛", f"伊莉雅先确认{location}里谁正处于危险中，优先保护活人而不是立刻追击。"),
            ("南星", f"赛璃调查{location}出现的记忆或灵魂异状，想找出它与碎月炉心的联系。"),
            ("白河", f"洛岚检查现场的魔导设备和灰晶痕迹，判断财团是否留下可被关闭的节点。"),
            ("时雨", f"艾薇娅和当地代表谈判，明确队伍不会用一个地区去交换另一个地区的安全。"),
            ("澄砚", f"苍祈询问{location}附近的奥灵或自然痕迹，请它们记住一个差点被抹去的名字。"),
        ]
        if is_dungeon:
            turns.append(
                (
                    "白河",
                    f"洛岚只处理{location}里已经看得见的通道、门禁或路线标记；"
                    "如果现场没有明确入口，就先记录现有退路和警报线，不强行声明进入隐藏区域。",
                )
            )
            focus.append("地下城")
        if is_boss:
            turns.append(("阿凛", "若强敌或反派代理人出现，伊莉雅先守住队伍目标，再决定是否正面决斗。"))
            focus.append("Boss/强敌")
        return CampaignSessionSpec(
            number,
            title,
            arc,
            opening,
            turns,
            focus,
            boss_session=is_boss,
            notes=["dungeon"] if is_dungeon else [],
        )

    def _final_campaign_session(self, number: int, template: CampaignSessionSpec) -> CampaignSessionSpec:
        return CampaignSessionSpec(
            number,
            template.title,
            "终幕：尾声",
            f"第{number}场终局开场。请描述碎月炉心、艾蕾娜最后的选择、旅人真实身份和所有被遗忘名字的回响。",
            template.turns,
            template.expected_focus,
            boss_session=True,
            notes=template.notes,
        )

    def _arc_label_for_session(self, number: int) -> str:
        ratio = number / max(1, self.target_sessions)
        if ratio >= 0.86:
            return "终幕：尾声"
        if ratio >= 0.68:
            return "第五幕：世界开始遗忘"
        if ratio >= 0.45:
            return "第三幕：记忆之价"
        if ratio >= 0.18:
            return "第二幕：海图与灯塔"
        return "第一幕：边境钟声"

    def _model_latency_metrics(self) -> dict[str, Any]:
        """Separate provider/model time from end-to-end HTTP latency."""

        runtime = self._runtime()
        candidates = {
            "Core GM": getattr(self.service, "gm_tool_agent", None),
            "Expressor": getattr(runtime.app, "expressor", None),
            "Casual Chat": getattr(runtime, "casual_chat", None),
            "Summarizer": getattr(getattr(runtime, "log_manager", None), "summarizer", None),
            "FU-PL": self.player_simulator,
            "Session Progress Evaluator": self._session_progress_evaluator,
        }
        by_client: dict[int, dict[str, Any]] = {}
        for label, component in candidates.items():
            client = getattr(component, "client", None)
            if client is None or not hasattr(client, "telemetry_payload"):
                continue
            entry = by_client.setdefault(
                id(client),
                {"client": client, "components": []},
            )
            entry["components"].append(label)

        all_values: list[int] = []
        per_client: list[dict[str, Any]] = []
        failed_calls = 0
        for entry in by_client.values():
            client = entry["client"]
            telemetry = client.telemetry_payload()
            values = [
                max(0, int(value))
                for value in getattr(client, "call_latency_history_ms", [])
            ]
            all_values.extend(values)
            failed_calls += int(telemetry.get("failed_calls") or 0)
            per_client.append(
                {
                    "components": list(entry["components"]),
                    "total_calls": int(telemetry.get("total_calls") or 0),
                    "failed_calls": int(telemetry.get("failed_calls") or 0),
                    "latency": dict(telemetry.get("latency") or {}),
                }
            )
        values = sorted(all_values)
        return {
            "count": len(values),
            "failed_calls": failed_calls,
            "p50_ms": self.conversation_quality_auditor._percentile(values, 0.50),
            "p95_ms": self.conversation_quality_auditor._percentile(values, 0.95),
            "max_ms": max(values, default=0),
            "clients": per_client,
        }

    @staticmethod
    def _world_map_artifact_ready(map_status: dict[str, Any]) -> bool:
        output_text = str(map_status.get("output_path") or "").strip()
        return (
            str(map_status.get("status") or "") in {"generated", "ready"}
            and bool(output_text)
            and Path(output_text).is_file()
        )

    def _build_report(self) -> dict[str, Any]:
        runtime = self._runtime()
        audit = self.invoke(
            "最终审计仪表盘",
            "GET",
            f"/v1/audit/dashboard?campaign_id={self.campaign_id}&session_id={self.session_id}&channel_id={self.channel_id}&include_private=true&limit=120",
            {},
        )
        elapsed_values = [int(call["elapsed_ms"]) for call in self.calls]
        model_latency = self._model_latency_metrics()
        slowest = sorted(self.calls, key=lambda item: int(item.get("elapsed_ms", 0)), reverse=True)[:15]
        story_arc = audit.get("story_arc", {}) if isinstance(audit, dict) else {}
        pacing = audit.get("campaign_pacing", {}) if isinstance(audit, dict) else {}
        map_status = runtime.app.world_map_generation_status()
        map_output_text = str(map_status.get("output_path") or "")
        map_output = Path(map_output_text) if map_output_text else None
        agent_error_calls = self._agent_error_calls(self.calls)
        failed_tool_receipts = self._failed_tool_receipts(self.calls)
        unrecovered_tool_failure_calls = self._unrecovered_tool_failure_calls(self.calls)
        tool_text = json.dumps(self.tool_events, ensure_ascii=False, default=str)
        transcript = self.conversation_path.read_text(encoding="utf-8") if self.conversation_path.exists() else ""
        table_metrics = list(self.session_table_metrics.values())
        all_sessions_meet_four_hour_proxy = bool(table_metrics) and all(
            bool(item.get("meets_four_hour_proxy")) for item in table_metrics
        )
        avg_player_turns = int(mean([int(item.get("player_turns_authored") or 0) for item in table_metrics])) if table_metrics else 0
        avg_gm_beats = int(mean([int(item.get("gm_autonomy_beats") or 0) for item in table_metrics])) if table_metrics else 0
        scene_metrics = list(self.session_scene_metrics.values())
        scene_types_seen = {
            scene_type
            for metric in scene_metrics
            for scene_type in metric.get("scene_types", [])
        }
        episode_contracts = [item.get("episode_contract") or {} for item in self.session_reports]
        experience_reports = [item.get("experience") or {} for item in self.session_reports]
        final_characters = self._party_resource_snapshot()
        resource_spend_sessions = sum(
            1
            for item in self.session_reports
            if any(
                int(delta.get(key, 0) or 0) < 0
                for delta in (item.get("resource_delta") or {}).values()
                for key in ("hp", "mp", "inventory_points", "fabula_points")
            )
        )
        resource_recovery_sessions = sum(
            1
            for item in self.session_reports
            if any(
                int(delta.get(key, 0) or 0) > 0
                for delta in (item.get("resource_delta") or {}).values()
                for key in ("hp", "mp", "inventory_points", "fabula_points")
            )
        )
        long_replies = [
            " ".join(str(call.get("reply") or "").split())
            for call in self.calls
            if len(str(call.get("reply") or "").strip()) >= 60
        ]
        repeated_long_replies = [
            {"text": text[:240], "count": count}
            for text, count in Counter(long_replies).most_common()
            if count >= 3
        ]
        llm_fallback_calls = [
            {
                "index": call.get("index"),
                "label": call.get("label"),
                "diagnostics": call.get("llm_diagnostics"),
            }
            for call in self.calls
            if any(
                bool(component.get("used_fallback"))
                for component in (call.get("llm_diagnostics") or {}).values()
                if isinstance(component, dict)
            )
        ]
        player_simulator_fallbacks = [
            item for item in self.player_simulation_metrics if bool(item.get("used_fallback"))
        ]
        player_simulator_validation_errors = [
            item for item in self.player_simulation_metrics if item.get("validation_errors")
        ]
        ordered_assessments = [
            self.session_progress_assessments[number]
            for number in sorted(self.session_progress_assessments)
        ]
        quality_report = self.conversation_quality_auditor.audit(self.calls, ordered_assessments)
        memory_anchors = [
            "|".join(
                (
                    assessment.memory_image.strip(),
                    assessment.memory_choice.strip(),
                    assessment.memory_consequence.strip(),
                )
            )
            for assessment in ordered_assessments
            if assessment.memory_anchor_complete
        ]
        duplicate_memory_anchors = [
            anchor for anchor, count in Counter(memory_anchors).items() if count > 1
        ]
        earned_session_endings = sum(
            1
            for result in self.session_completion_results.values()
            if bool(result.get("earned"))
        )
        progress_fallbacks = [
            asdict(assessment) for assessment in ordered_assessments if assessment.used_fallback
        ]
        expected_dynamic_player_turns = self.target_sessions * 3
        required_boss_sessions = max(1, min(5, self.target_sessions // 4))
        discussion_samples = [
            call
            for call in self.calls
            if "玩家自由讨论" in str(call.get("label") or "")
        ]
        successful_discussion_samples = [
            call
            for call in discussion_samples
            if int(call.get("status") or 0) < 400
        ]
        definite_discussion_overreply = any(
            str((call.get("body") or {}).get("target") or "") != "silent"
            or bool((call.get("body") or {}).get("send_reply"))
            or bool(str(call.get("reply") or "").strip())
            for call in successful_discussion_samples
        )
        check_applicability = {
            "scene_types_cover_core_mix": self.target_sessions >= 7,
            "average_level_up_every_two_sessions": self.target_sessions >= 2,
            "resource_attrition_observed": self.target_sessions >= 3,
            "resource_recovery_observed": self.target_sessions >= 3,
            "finale_phase_reached": self.target_sessions >= 20,
            "boss_sessions_covered": self.target_sessions >= 4,
            "all_sessions_earned_an_ending": bool(self.session_reports),
            "short_lived_clocks_cleaned": bool(scene_metrics),
            "no_blocking_decisions_at_session_end": bool(self.session_completion_results),
            "session_experience_uses_core_formula": bool(self.session_reports),
            "free_discussion_silent_samples": bool(definite_discussion_overreply)
            or len(successful_discussion_samples) >= self.target_sessions,
        }

        def scoped(check_name: str, result: bool) -> bool:
            return bool(result) if check_applicability.get(check_name, True) else True

        required_continuity_terms = ["白花碑驿站", "钟鸣公国"]
        if self.target_sessions >= 6:
            required_continuity_terms.append("奥涅里亚")
        if self.target_sessions >= 9:
            required_continuity_terms.append("沉默森林")
        if self.target_sessions >= 11:
            required_continuity_terms.append("第七采掘城")
        if self.target_sessions >= 20:
            required_continuity_terms.append("碎月炉心")
        checks = {
            "ran_target_sessions": len(self.session_reports) == self.target_sessions,
            "story_arc_count_reached_target": int(story_arc.get("session_count") or 0) >= self.target_sessions,
            "pacing_tool_recorded": "战役节奏控制" in tool_text
            and f"{self.target_sessions}场战役节奏器" in tool_text,
            "four_hour_session_proxy": all_sessions_meet_four_hour_proxy,
            "multiple_scenes_per_session": len(scene_metrics) == self.target_sessions
            and all(bool(item.get("multiple_scenes")) for item in scene_metrics),
            "episode_contract_per_session": len(episode_contracts) == self.target_sessions
            and all(contract and all(bool(value) for value in contract.values()) for contract in episode_contracts),
            "offline_session_evaluation_active": len(ordered_assessments) == self.target_sessions
            and not progress_fallbacks,
            "memorable_anchor_per_session": quality_report.complete_memory_anchors == self.target_sessions,
            "memory_anchors_are_distinct": not duplicate_memory_anchors
            and not quality_report.high_similarity_anchor_pairs,
            "all_sessions_earned_an_ending": scoped(
                "all_sessions_earned_an_ending",
                earned_session_endings == self.target_sessions,
            ),
            "opposition_moves_each_session": quality_report.opposition_move_session_count
            == self.target_sessions,
            "signature_image_present_at_each_opening": quality_report.opening_signature_present_count
            == self.target_sessions,
            "concrete_npc_agenda_each_session": quality_report.concrete_npc_agenda_session_count
            == self.target_sessions,
            "signature_image_evolves_each_session": quality_report.signature_image_evolved_count
            == self.target_sessions,
            "local_payoff_each_session": quality_report.local_payoff_session_count
            == self.target_sessions,
            "previous_consequence_recalled": quality_report.previous_consequence_callback_count
            == self.target_sessions,
            "npc_answers_complete": quality_report.npc_answer_failures == 0,
            "npc_personality_consistent": quality_report.npc_personality_failures == 0,
            "player_agency_preserved": quality_report.agency_violations == 0,
            "plot_continuity_preserved": quality_report.continuity_failures == 0,
            "npc_public_commitments_honored": quality_report.npc_commitment_violations == 0,
            "player_actions_have_causal_feedback": quality_report.cause_effect_failures == 0,
            "gm_control_present_per_session": quality_report.gm_control_failures == 0,
            "session_identity_distinct": quality_report.indistinct_session_count == 0,
            "gm_responses_relevant": quality_report.irrelevant_gm_response_sessions == 0,
            "gm_player_echo_rate_acceptable": quality_report.player_echo_rate <= 0.12,
            "group_silence_recall_acceptable": quality_report.silence_recall >= 0.95,
            "group_silence_precision_acceptable": quality_report.silence_precision >= 0.95,
            "directed_reply_recall_acceptable": quality_report.reply_recall >= 0.95,
            "unnecessary_reply_rate_acceptable": quality_report.unnecessary_reply_rate <= 0.05,
            "typed_state_tools_observed": quality_report.successful_state_tool_receipts > 0,
            "no_unbacked_state_change_claims": quality_report.unbacked_state_change_claims == 0,
            "no_failed_tool_success_claims": quality_report.failed_tool_success_claims == 0,
            "knowledge_action_consistent": quality_report.knowledge_action_consistency_rate == 1.0,
            "core_agent_available": self._llm_preflight_ok
            and quality_report.core_agent_unavailable_count == 0,
            "tool_recovery_rate_acceptable": (
                quality_report.tool_validation_rejections
                + quality_report.agent_output_retry_failures
            )
            <= max(
                3,
                (quality_report.successful_state_tool_receipts * 15 + 99) // 100,
            ),
            "p95_latency_reported_and_bounded": quality_report.p95_latency_ms <= 60_000,
            "model_latency_reported_and_bounded": model_latency["count"] > 0
            and model_latency["p95_ms"] <= 60_000,
            "no_contradictory_check_responses": quality_report.contradictory_check_responses == 0,
            "no_retired_clock_reappearance": quality_report.retired_clock_reappearances == 0,
            "no_vague_gm_placeholders": quality_report.vague_placeholder_gm_outputs == 0,
            "no_premature_clock_consequences": quality_report.premature_clock_consequences == 0,
            "near_duplicate_gm_reply_rate_acceptable": quality_report.near_duplicate_gm_replies
            <= max(2, self.target_sessions // 5),
            "no_blocking_decisions_at_session_end": scoped(
                "no_blocking_decisions_at_session_end",
                all(
                    int(result.get("pending_blocking_decisions") or 0) == 0
                    for result in self.session_completion_results.values()
                ),
            ),
            "scene_types_cover_core_mix": scoped(
                "scene_types_cover_core_mix",
                {"standard", "conflict", "interlude", "gm", "rest", "travel", "dungeon"}
                .issubset(scene_types_seen),
            ),
            "short_lived_clocks_cleaned": scoped(
                "short_lived_clocks_cleaned",
                all(not item.get("short_clock_leaks") for item in scene_metrics),
            ),
            "gm_autonomy_beats_per_session": avg_gm_beats >= self.gm_beats_per_session,
            "table_turn_density": avg_player_turns >= self.min_table_turns_per_session,
            "llm_player_simulator_active": bool(self.player_simulator.use_llm),
            "dynamic_player_turns_covered": len(self.player_simulation_metrics) >= expected_dynamic_player_turns,
            "no_player_simulator_fallback": not player_simulator_fallbacks,
            "player_simulator_outputs_valid": not player_simulator_validation_errors,
            "player_action_lanes_diverse": quality_report.repeated_player_action_lanes == 0,
            "session_experience_uses_core_formula": scoped(
                "session_experience_uses_core_formula",
                len(experience_reports) == self.target_sessions
                and all(
                    int(report.get("base_xp") or 0) == 5
                    and int(report.get("total_xp") or 0) >= 5
                    and len(report.get("gains") or []) == len(self.pc_names)
                    for report in experience_reports
                ),
            ),
            "average_level_up_every_two_sessions": scoped(
                "average_level_up_every_two_sessions",
                len(self.level_up_results) >= (self.target_sessions // 2) * len(self.pc_names)
                and all(int(data.get("level") or 0) >= 5 + self.target_sessions // 2 for data in final_characters.values()),
            ),
            "resource_attrition_observed": scoped(
                "resource_attrition_observed",
                resource_spend_sessions >= max(1, self.target_sessions // 5),
            ),
            "resource_recovery_observed": scoped(
                "resource_recovery_observed",
                resource_recovery_sessions >= 1,
            ),
            "no_exact_gm_reply_loop": not repeated_long_replies,
            "no_unexpected_llm_fallback": not llm_fallback_calls,
            "no_gm_tool_agent_errors": not agent_error_calls,
            "no_unrecovered_tool_failures": not unrecovered_tool_failure_calls,
            "finale_phase_reached": scoped(
                "finale_phase_reached",
                str(story_arc.get("phase") or "") in {"finale", "crisis"},
            ),
            "boss_sessions_covered": scoped(
                "boss_sessions_covered",
                sum(1 for item in self.session_reports if item.get("boss_session"))
                >= required_boss_sessions,
            ),
            "villain_pressure_exists": bool(story_arc.get("villain_pressure")),
            "no_repeated_out_of_turn_deadlock": not self._has_repeated_out_of_turn_deadlock(),
            "no_sticky_opportunity_preference": not self._has_sticky_opportunity_preference(),
            "no_backend_labels": quality_report.backstage_instruction_leaks == 0
            and not any(
                token in transcript
                for token in ("【叙事】", "【物语改写】", "GM私密暗线", "ActionType")
            ),
            "no_explanatory_player_intent_commentary": not any(
                token in transcript for token in ("这一步的重点", "这一步的目的", "这个动作的重点", "这个动作的目的", "没有急着替任何人做决定")
            ),
            "free_discussion_silent_samples": scoped(
                "free_discussion_silent_samples",
                self._all_free_discussion_samples_stayed_silent(),
            ),
            "campaign_continuity_memory": all(phrase in transcript for phrase in required_continuity_terms),
            "pacing_audit_has_budget": bool((pacing.get("current_plan") or {}).get("pressure_budget")) if isinstance(pacing, dict) else False,
            "world_map_generated": self._world_map_artifact_ready(map_status),
            "astrbot_bridge_smoke_ok": (not self.run_astrbot_smoke)
            or (bool(self.astrbot_bridge_results) and all(item.get("ok") for item in self.astrbot_bridge_results)),
            "heartbeat_probe_ok": bool(self.heartbeat_results)
            and all(item.get("ok") for item in self.heartbeat_results)
            and {
                "session_zero_nudge",
                "free_scene_beat",
                "pc_turn_reminder",
                "npc_turn",
            }.issubset({str(item.get("action") or "") for item in self.heartbeat_results}),
        }
        if not checks["ran_target_sessions"]:
            self.errors.append(f"只完成 {len(self.session_reports)} 场，没有跑满 {self.target_sessions} 场。")
        if not checks["story_arc_count_reached_target"]:
            self.errors.append(f"StoryArcManager 没有记录到 {self.target_sessions} 场总结。")
        if not checks["finale_phase_reached"]:
            self.errors.append(f"{self.target_sessions} 场结束后战役阶段未进入 crisis/finale。")
        if not checks["no_backend_labels"]:
            self.errors.append(
                "完整对话中仍有后台标签、系统术语或规划器指令泄露："
                f"{quality_report.backstage_instruction_leaks} 条。"
            )
        if not checks["no_explanatory_player_intent_commentary"]:
            self.errors.append("完整对话中仍有解释玩家动作目的/重点的说明文句式。")
        if not checks["four_hour_session_proxy"]:
            self.errors.append(
                "至少一场战役没有达到四小时代理指标："
                f"每场至少 {self.min_table_turns_per_session} 条玩家行动和 {self.gm_beats_per_session} 次 GM 主动节拍。"
            )
        if not checks["multiple_scenes_per_session"]:
            self.errors.append("至少一场没有形成规则书第32页要求的多场景阶段结构。")
        if not checks["offline_session_evaluation_active"]:
            self.errors.append(f"离线场次进展评估未覆盖全部场次或发生降级：{progress_fallbacks[:3]}")
        if not checks["memorable_anchor_per_session"]:
            self.errors.append(
                f"只有 {quality_report.complete_memory_anchors}/{self.target_sessions} 场留下完整的画面、选择与后果。"
            )
        if duplicate_memory_anchors:
            self.errors.append(f"不同场次出现重复记忆锚点：{duplicate_memory_anchors[:3]}")
        if quality_report.high_similarity_anchor_pairs:
            self.errors.append(
                "近邻场次的记忆锚点结构过于相似："
                f"{quality_report.high_similarity_anchor_pairs[:5]}"
            )
        if not checks["all_sessions_earned_an_ending"]:
            self.errors.append(
                f"只有 {earned_session_endings}/{self.target_sessions} 场在切换前赢得了局部结局或经过转折的悬念。"
            )
        if not checks["opposition_moves_each_session"]:
            self.errors.append(
                f"只有 {quality_report.opposition_move_session_count}/{self.target_sessions} 场出现对立方主动行动。"
            )
        if not checks["signature_image_present_at_each_opening"]:
            self.errors.append(
                f"只有 {quality_report.opening_signature_present_count}/{self.target_sessions} 场在开局真正展示标志画面。"
            )
        if not checks["concrete_npc_agenda_each_session"]:
            self.errors.append(
                f"只有 {quality_report.concrete_npc_agenda_session_count}/{self.target_sessions} 场让NPC公开表现具体目标或条件。"
            )
        if not checks["signature_image_evolves_each_session"]:
            self.errors.append(
                f"只有 {quality_report.signature_image_evolved_count}/{self.target_sessions} 场让标志画面随选择发生变化。"
            )
        if not checks["local_payoff_each_session"]:
            self.errors.append(
                f"只有 {quality_report.local_payoff_session_count}/{self.target_sessions} 场兑现局部结果。"
            )
        if not checks["previous_consequence_recalled"]:
            self.errors.append(
                f"只有 {quality_report.previous_consequence_callback_count}/{self.target_sessions} 场在开局回收上一场后果。"
            )
        if not checks["npc_answers_complete"]:
            self.errors.append(f"有 {quality_report.npc_answer_failures} 场在玩家等待时没有得到NPC明确答复。")
        if not checks["npc_personality_consistent"]:
            self.errors.append(f"有 {quality_report.npc_personality_failures} 场出现NPC动机或人格不一致。")
        if not checks["player_agency_preserved"]:
            self.errors.append(f"离线质量评估发现 {quality_report.agency_violations} 场存在GM代替玩家决定行动。")
        if not checks["plot_continuity_preserved"]:
            self.errors.append(
                f"连续性审计发现 {quality_report.continuity_failures} 项剧情承接异常"
                f"（既成事实倒退 {quality_report.irreversible_state_regressions}，"
                f"已兑现承诺重开 {quality_report.fulfilled_promise_reopens}，"
                f"公开承诺未兑现 {quality_report.npc_commitment_violations}）。"
            )
        if not checks["npc_public_commitments_honored"]:
            self.errors.append(
                f"发现 {quality_report.npc_commitment_violations} 次NPC明确承诺未在触发后兑现。"
            )
        if not checks["player_action_lanes_diverse"]:
            self.errors.append(
                f"FU-PL 连续重复同一对象/手段的行动 {quality_report.repeated_player_action_lanes} 次。"
            )
        if not checks["player_actions_have_causal_feedback"]:
            self.errors.append(f"有 {quality_report.cause_effect_failures} 场缺少玩家行动后的因果反馈。")
        if not checks["gm_control_present_per_session"]:
            self.errors.append(f"有 {quality_report.gm_control_failures} 场缺少GM主动主持局势的证据。")
        if not checks["session_identity_distinct"]:
            self.errors.append(f"有 {quality_report.indistinct_session_count} 场缺少独立记忆点。")
        if not checks["gm_responses_relevant"]:
            self.errors.append(
                f"有 {quality_report.irrelevant_gm_response_sessions} 场出现与玩家消息无关的GM回应。"
            )
        if not checks["gm_player_echo_rate_acceptable"]:
            self.errors.append(f"GM复述玩家率过高：{quality_report.player_echo_rate:.1%}。")
        if not checks["group_silence_recall_acceptable"]:
            self.errors.append(f"玩家自由讨论静默召回率不足：{quality_report.silence_recall:.1%}。")
        if not checks["group_silence_precision_acceptable"]:
            self.errors.append(f"GM静默精确率不足：{quality_report.silence_precision:.1%}。")
        if not checks["directed_reply_recall_acceptable"]:
            self.errors.append(f"明确需要GM回应的消息回复召回率不足：{quality_report.reply_recall:.1%}。")
        if not checks["unnecessary_reply_rate_acceptable"]:
            self.errors.append(f"GM无必要回复率过高：{quality_report.unnecessary_reply_rate:.1%}。")
        if not checks["typed_state_tools_observed"]:
            self.errors.append("真实长测没有观察到任何成功的类型化状态工具回执。")
        if not checks["no_unbacked_state_change_claims"]:
            self.errors.append(
                f"GM有 {quality_report.unbacked_state_change_claims} 次公开声称状态已改变，却没有成功写工具回执。"
            )
        if not checks["no_failed_tool_success_claims"]:
            self.errors.append(
                f"GM有 {quality_report.failed_tool_success_claims} 次在工具失败后仍公开声称成功。"
            )
        if not checks["core_agent_available"]:
            self.errors.append(
                f"核心 GM 智能体有 {quality_report.core_agent_unavailable_count} 次不可用。"
            )
        if not checks["tool_recovery_rate_acceptable"]:
            self.errors.append(
                "模型提交无效工具参数或下级智能体输出的频率过高："
                f"工具校验拒绝 {quality_report.tool_validation_rejections} 次，"
                f"下级智能体输出重试 {quality_report.agent_output_retry_failures} 次。"
            )
        if not checks["p95_latency_reported_and_bounded"]:
            self.errors.append(f"P95响应延迟超过60秒：{quality_report.p95_latency_ms}ms。")
        if not checks["model_latency_reported_and_bounded"]:
            self.errors.append(
                f"模型P95延迟未记录或超过60秒：count={model_latency['count']}，p95={model_latency['p95_ms']}ms。"
            )
        if not checks["no_contradictory_check_responses"]:
            self.errors.append(
                f"发现 {quality_report.contradictory_check_responses} 条检定结果与后续叙事相互矛盾。"
            )
        if not checks["no_retired_clock_reappearance"]:
            self.errors.append(
                f"发现 {quality_report.retired_clock_reappearances} 次已完成命刻重新以未完成进度出现。"
            )
        if not checks["no_vague_gm_placeholders"]:
            self.errors.append(
                f"GM输出了 {quality_report.vague_placeholder_gm_outputs} 条‘当前目标/那件东西’式空泛占位语。"
            )
        if not checks["no_premature_clock_consequences"]:
            self.errors.append(
                f"发现 {quality_report.premature_clock_consequences} 次命刻未满却提前叙述完成后果。"
            )
        if not checks["near_duplicate_gm_reply_rate_acceptable"]:
            self.errors.append(
                f"GM近似复读偏多：{quality_report.near_duplicate_gm_replies} 次。"
            )
        if not checks["no_blocking_decisions_at_session_end"]:
            self.errors.append("至少一场在仍有玩家待决选择时执行了收团。")
        if not checks["session_experience_uses_core_formula"]:
            self.errors.append("至少一场没有按基础5 XP、终结点与物语点均分公式结算经验。")
        if not checks["average_level_up_every_two_sessions"]:
            self.errors.append("角色成长没有达到规则书预期的平均每两场约升一级。")
        if not checks["resource_attrition_observed"]:
            self.errors.append(
                f"{self.target_sessions}场实跑中资源消耗场次过少，疑似行动只产生叙事而没有进入规则结算。"
            )
        if not checks["resource_recovery_observed"]:
            self.errors.append(f"{self.target_sessions}场实跑中没有观察到一次明确资源恢复。")
        if repeated_long_replies and self.semantic_llm:
            self.errors.append(f"GM出现三次以上完全相同的长回复：{repeated_long_replies[:3]}")
        if llm_fallback_calls and self.semantic_llm:
            self.errors.append(f"真实长测中出现 LLM 静默降级：{llm_fallback_calls[:5]}")
        if agent_error_calls and self.semantic_llm:
            self.errors.append(f"真实长测中出现未恢复的GM工具智能体错误：{agent_error_calls[:5]}")
        if unrecovered_tool_failure_calls and self.semantic_llm:
            self.errors.append(
                "真实长测中存在未恢复的工具拒绝："
                f"{unrecovered_tool_failure_calls[:5]}"
            )
        if player_simulator_fallbacks and self.semantic_llm:
            self.errors.append(f"真实长测中 FU-PL 出现静默降级：{player_simulator_fallbacks[:5]}")
        if player_simulator_validation_errors and self.semantic_llm:
            self.errors.append(f"真实长测中 FU-PL 生成了越界内容：{player_simulator_validation_errors[:5]}")
        if not checks["world_map_generated"]:
            self.errors.append(f"世界地图没有成功生成：{map_status}")
        issue_classification = LongRunIssueAttributor.classify(
            configured_model=LLMConfig.from_env().action_model,
            calls=self.calls,
            assessments=ordered_assessments,
            checks=checks,
            check_applicability=check_applicability,
            quality=quality_report,
            player_validation_errors=player_simulator_validation_errors,
            repeated_long_replies=repeated_long_replies,
        )
        mechanical_checks = LongRunIssueAttributor.mechanical_checks(
            checks,
            check_applicability=check_applicability,
        )
        mechanical_ok = all(mechanical_checks.values())
        ok = self.semantic_llm and not self.errors and all(checks.values())
        return {
            "ok": ok,
            "mechanical_ok": mechanical_ok,
            "campaign_id": self.campaign_id,
            "target_sessions": self.target_sessions,
            "length_profile": self.length_profile,
            "semantic_llm": self.semantic_llm,
            "llm_preflight": {
                "attempted": self._llm_preflight_attempted,
                "ok": self._llm_preflight_ok,
                "error": self._llm_preflight_error,
            },
            "scripted_identities": self.scripted_identities,
            "completed_sessions": len(self.session_reports),
            "checks": checks,
            "check_applicability": check_applicability,
            "errors": self.errors,
            "notes": self.notes,
            "llm_fallback_calls": llm_fallback_calls,
            "player_simulator_fallbacks": player_simulator_fallbacks,
            "player_simulator_validation_errors": player_simulator_validation_errors,
            "gm_tool_agent_errors": agent_error_calls,
            "failed_tool_receipts": failed_tool_receipts,
            "unrecovered_tool_failure_calls": unrecovered_tool_failure_calls,
            "session_progress_fallbacks": progress_fallbacks,
            "conversation_quality": quality_report.as_dict(),
            "duplicate_memory_anchors": duplicate_memory_anchors,
            "issue_classification": issue_classification,
            "latency": {
                "count": len(elapsed_values),
                "total_ms": sum(elapsed_values),
                "avg_ms": int(mean(elapsed_values)) if elapsed_values else 0,
                "max_ms": max(elapsed_values) if elapsed_values else 0,
                "slowest": [
                    {
                        "index": item["index"],
                        "label": item["label"],
                        "route": item["route"],
                        "elapsed_ms": item["elapsed_ms"],
                        "status": item["status"],
                        "ok": item["ok"],
                    }
                    for item in slowest
                ],
                "model": model_latency,
            },
            "session_reports": self.session_reports,
            "session_completion_results": self.session_completion_results,
            "session_table_metrics": self.session_table_metrics,
            "session_scene_metrics": self.session_scene_metrics,
            "player_simulation_metrics": self.player_simulation_metrics,
            "level_up_results": self.level_up_results,
            "final_characters": final_characters,
            "resource_curve": {
                "spend_sessions": resource_spend_sessions,
                "recovery_sessions": resource_recovery_sessions,
            },
            "repeated_long_replies": repeated_long_replies,
            "story_arc": story_arc,
            "campaign_pacing": pacing,
            "world_map": map_status,
            "tool_events": self.tool_events,
            "astrbot_bridge_results": self.astrbot_bridge_results,
            "heartbeat_results": self.heartbeat_results,
            "artifacts": {
                "run_root": str(self.run_root),
                "conversation": str(self.conversation_path),
                "conversation_export": str(self.conversation_export_path),
                "report_json": str(self.report_json_path),
                "report_txt": str(self.report_txt_path),
                "campaign_root": str(self.campaign_root),
                "map_root": str(self.map_root),
                "map_output": map_output_text,
            },
        }

    def _write_report(self, report: dict[str, Any]) -> None:
        if self.conversation_path.exists():
            self.conversation_export_path.write_text(self.conversation_path.read_text(encoding="utf-8"), encoding="utf-8")
        self.report_json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        self.report_txt_path.write_text(self._format_report(report), encoding="utf-8")

    def _format_report(self, report: dict[str, Any]) -> str:
        lines = [
            f"FU-GM {self.target_sessions} 场完整战役长测报告",
            f"campaign_id: {report['campaign_id']}",
            f"target_sessions: {report.get('target_sessions')}",
            f"length_profile: {report.get('length_profile')}",
            f"semantic_llm: {report.get('semantic_llm')}",
            f"scripted_identities: {report.get('scripted_identities')}",
            f"ok: {report['ok']}",
            f"mechanical_ok: {report.get('mechanical_ok')}",
            f"completed_sessions: {report['completed_sessions']}",
            "",
            "=== 检查项 ===",
        ]
        applicability = report.get("check_applicability") or {}
        for key, value in report["checks"].items():
            suffix = "（本次规模不适用）" if applicability.get(key) is False else ""
            lines.append(f"- {key}: {value}{suffix}")
        lines.extend(["", "=== 错误 ===", *([f"- {item}" for item in report["errors"]] or ["- 无"])])
        lines.extend(["", "=== 延迟统计 ===", json.dumps(report["latency"], ensure_ascii=False, indent=2)])
        lines.extend(
            [
                "",
                "=== 真人对话与戏剧质量 ===",
                json.dumps(report.get("conversation_quality", {}), ensure_ascii=False, indent=2),
            ]
        )
        lines.extend(["", "=== 每场桌面粒度 ==="])
        for session_number, metric in sorted(
            (report.get("session_table_metrics") or {}).items(),
            key=lambda pair: int(pair[0]),
        ):
            lines.append(
                f"- 第{int(session_number):02d}场: 玩家行动 {metric.get('player_turns_authored')}，"
                f"GM主动节拍 {metric.get('gm_autonomy_beats')}，"
                f"自由讨论 {metric.get('routed_table_discussions')}，"
                f"估算桌面分钟 {metric.get('estimated_table_minutes')}，"
                f"达标={metric.get('meets_four_hour_proxy')}"
            )
        lines.extend(["", "=== 每场场景结构（核心规则书第32页） ==="])
        for session_number, metric in sorted(
            (report.get("session_scene_metrics") or {}).items(),
            key=lambda pair: int(pair[0]),
        ):
            lines.append(
                f"- 第{int(session_number):02d}场: 场景 {metric.get('scene_count')}，"
                f"类型={metric.get('scene_types')}，地点={metric.get('locations')}，"
                f"短期命刻残留={metric.get('short_clock_leaks')}"
            )
        lines.extend(["", "=== 每场摘要 ==="])
        for item in report["session_reports"]:
            lines.append(
                f"- 第{item['number']:02d}场 {item['title']} [{item['arc']}] phase={item.get('phase')} "
                f"boss={item['boss_session']} foreground={item.get('foreground_clocks')}"
            )
            if item.get("table_metric"):
                lines.append(f"  桌面粒度：{json.dumps(item['table_metric'], ensure_ascii=False, default=str)}")
            if item.get("experience"):
                lines.append(f"  经验：{json.dumps(item['experience'], ensure_ascii=False, default=str)}")
            if item.get("level_ups"):
                lines.append(f"  升级：{json.dumps(item['level_ups'], ensure_ascii=False, default=str)}")
            if item.get("summary"):
                lines.append(f"  {item['summary']}")
            if item.get("memory_anchor"):
                lines.append(f"  记忆锚点：{json.dumps(item['memory_anchor'], ensure_ascii=False)}")
            if item.get("progress_assessment"):
                progress = item["progress_assessment"]
                lines.append(
                    f"  实录阶段：{progress.get('stage')}；"
                    f"局部问题改变={progress.get('local_question_changed')}；"
                    f"已解决={progress.get('local_question_resolved')}；"
                    f"转折={progress.get('reversal_reached')}"
                )
        lines.extend(
            [
                "",
                "=== 问题归因（框架 / 模型 / 供应端） ===",
                json.dumps(report.get("issue_classification", {}), ensure_ascii=False, indent=2, default=str),
            ]
        )
        lines.extend(
            [
                "",
                "=== 终局角色成长 ===",
                json.dumps(report.get("final_characters", {}), ensure_ascii=False, indent=2, default=str),
                "",
                "=== 受限FU-PL实时模拟 ===",
                json.dumps(report.get("player_simulation_metrics", []), ensure_ascii=False, indent=2, default=str),
                "",
                "=== 战役节奏器 ===",
                json.dumps(report.get("campaign_pacing", {}), ensure_ascii=False, indent=2, default=str),
                "",
                "=== 工具轨迹 ===",
                json.dumps(report.get("tool_events", []), ensure_ascii=False, indent=2, default=str),
                "",
                "=== AstrBot 桥接冒烟 ===",
                json.dumps(report.get("astrbot_bridge_results", []), ensure_ascii=False, indent=2, default=str),
                "",
                "=== 心跳/idle monitor 探针 ===",
                json.dumps(report.get("heartbeat_results", []), ensure_ascii=False, indent=2, default=str),
                "",
                "=== 产物 ===",
            ]
        )
        for key, value in report["artifacts"].items():
            lines.append(f"- {key}: {value}")
        lines.extend(["", "=== 完整 API 对话 ==="])
        lines.append(self.conversation_path.read_text(encoding="utf-8") if self.conversation_path.exists() else "")
        return "\n".join(lines)


def _parse_session_targets(args: argparse.Namespace) -> list[int]:
    if args.matrix:
        return [20, 35, 50]
    raw_targets = args.sessions or [20]
    targets: list[int] = []
    for value in raw_targets:
        target = int(value)
        if target <= 0:
            raise ValueError("sessions must be positive")
        targets.append(target)
    return targets


def _write_matrix_report(matrix_root: Path, summaries: list[dict[str, Any]]) -> None:
    matrix_root.mkdir(parents=True, exist_ok=True)
    json_path = matrix_root / "campaign_length_matrix_report.json"
    txt_path = matrix_root / "campaign_length_matrix_report.txt"
    ok = all(item.get("ok") for item in summaries)
    payload = {
        "ok": ok,
        "generated_at": datetime.now().isoformat(),
        "runs": summaries,
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    lines = [
        "FU-GM 20/35/50 场战役档位矩阵报告",
        f"ok: {ok}",
        "",
    ]
    for item in summaries:
        lines.append(
            f"- {item.get('target_sessions')} 场：ok={item.get('ok')} "
            f"completed={item.get('completed_sessions')} report={item.get('report_txt')}"
        )
        for error in item.get("errors") or []:
            lines.append(f"  error: {error}")
    lines.append("")
    lines.append(f"JSON: {json_path}")
    txt_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"MATRIX_REPORT_JSON={json_path}", flush=True)
    print(f"MATRIX_REPORT_TXT={txt_path}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="运行 FU-GM 20/35/50 场战役档位长测。")
    parser.add_argument("--sessions", nargs="*", type=int, help="要测试的目标场次数，例如 20 35 50。默认 20。")
    parser.add_argument("--matrix", action="store_true", help="顺序运行 20/35/50 三个官方推荐档位。")
    parser.add_argument("--skip-astrbot", action="store_true", help="跳过 AstrBot bridge 真实 HTTP 冒烟。")
    parser.add_argument("--min-turns-per-session", type=int, help="每场至少生成多少条玩家行动；默认 28。")
    parser.add_argument("--max-turns-per-session", type=int, help="实录仍未收束时的诊断上限；默认 42，不会被视为自动成功。")
    parser.add_argument("--gm-beats-per-session", type=int, help="每场至少插入多少次 GM 主动节拍；默认 3。")
    parser.add_argument(
        "--scripted-identities",
        action="store_true",
        help="仅用于A/B对照：使用长测脚本预写的场次身份。默认关闭，以验证生产会话规划器。",
    )
    parser.add_argument(
        "--resume-run",
        type=Path,
        help=(
            "从长测根目录、某个不可变 turn/session 断点目录，或其 "
            "campaign_checkpoint.json 继续。"
        ),
    )
    parser.add_argument(
        "--rules-only",
        action="store_true",
        help="只跑离线规则与生命周期机械演练；不会冒充真人语义长测。",
    )
    parser.add_argument(
        "--setup-only",
        action="store_true",
        help="只验证第零章完整写入、角色确认、地图生成和第一章门控，不进入20场正文。",
    )
    parser.add_argument(
        "--collect-route-findings",
        action="store_true",
        help=(
            "耐久审计模式：消息路由与预期不一致时记录为质量问题并继续；"
            "HTTP、状态完整性、玩家自主权等结构性故障仍会立即中止。"
        ),
    )
    args = parser.parse_args()

    if args.min_turns_per_session is not None:
        os.environ["FU_GM_LONG_TEST_MIN_TURNS_PER_SESSION"] = str(args.min_turns_per_session)
    if args.max_turns_per_session is not None:
        os.environ["FU_GM_LONG_TEST_MAX_TURNS_PER_SESSION"] = str(args.max_turns_per_session)
    if args.gm_beats_per_session is not None:
        os.environ["FU_GM_LONG_TEST_GM_BEATS_PER_SESSION"] = str(args.gm_beats_per_session)

    if args.resume_run and args.matrix:
        parser.error("--resume-run 不能与 --matrix 同时使用。")
    if args.resume_run:
        _, _, resume_checkpoint = CampaignRunCheckpoint.load_resume_source(args.resume_run)
        targets = [resume_checkpoint.target_sessions]
    else:
        targets = _parse_session_targets(args)
    summaries: list[dict[str, Any]] = []
    exit_code = 0
    matrix_root = PROJECT_ROOT / ".runtime" / "large_tests" / f"campaign_length_matrix_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    for target in targets:
        harness = TwentySessionCampaignHarness(
            target_sessions=target,
            run_astrbot_smoke=not args.skip_astrbot,
            semantic_llm=not args.rules_only,
            scripted_identities=args.scripted_identities,
            setup_only=args.setup_only,
            resume_root=args.resume_run,
            fail_fast_route_mismatch=False if args.collect_route_findings else None,
        )
        code = harness.run()
        exit_code = max(exit_code, code)
        try:
            report = json.loads(harness.report_json_path.read_text(encoding="utf-8"))
        except Exception:
            report = {"ok": False, "errors": [f"无法读取报告：{harness.report_json_path}"]}
            exit_code = 1
        summaries.append(
            {
                "target_sessions": target,
                "ok": bool(report.get("ok")),
                "completed_sessions": report.get("completed_sessions"),
                "checks": report.get("checks", {}),
                "errors": report.get("errors", []),
                "run_root": str(harness.run_root),
                "report_json": str(harness.report_json_path),
                "report_txt": str(harness.report_txt_path),
                "conversation": str(harness.conversation_path),
            }
        )
    if len(targets) > 1:
        _write_matrix_report(matrix_root, summaries)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
