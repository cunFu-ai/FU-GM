from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from fu_gm.gm_tool_contracts import GMToolReceipt, json_safe_value
from fu_gm.llm_utils import extract_json_object
from fu_gm.prompt_cache import build_cache_friendly_messages


REPLY_GROUNDING_SYSTEM_PROMPT = """
你是FU-GM公开回复的事实一致性审计器，不负责续写剧情。判断拟发布回复是否只使用了本轮玩家原话、最近公开聊天、当前权威状态和成功工具回执能够支持的事实。

判定规则：
1. 玩家可以声明自己角色的意图、言语和动作，但不能单方面决定NPC反应、敌人落败、物品到手、线索出现、场景抵达、环境变化、检定结果或其他外部结果。
2. 工具回执是本轮新增外部事实的唯一依据。失败回执不能支持成功叙述；当前状态只能支持已经存在的事实。
3. 新的NPC台词、NPC行动、环境反应、战斗结果、位置迁移、获得或失去物品、公开线索和命刻变化，都需要对应成功回执。仅仅纠正玩家不成立的前提、解释规则、回答后台状态查询或询问必需参数，不算新增外部事实。
4. 不要求逐字复述回执，但不能扩大、倒置或补完回执没有提交的结果。玩家说“示意递出牌子”不等于“牌子已经被接走”；“寻找藏身处”不等于“已经抵达藏身处”。
5. 玩家问题中的前提也必须有公开依据。“刚才谁提到了庄园？”不能证明有人提过庄园；若最近公开聊天和权威状态均无依据，确认该前提、为它虚构说话者或在后文沿用它，都属于unsupported_external_result。此类回忆核对只能澄清公开对话中没人提过或玩家可能听错，不能借玩家误提的词在同一回复中首次揭示相关私密事实。
6. 权威状态中的NPC标准名、真实身份、秘密和动机可以证明后台一致性，却不能证明玩家已经知道。拟回复首次用专名替换recent_public_context中的匿名说话者或描述，例如把“隔壁牢房的人”改称为后台档案名“赫德”，属于private_fact_disclosure；只有最近公开聊天、公开工具回执或明确公开事实已经介绍该名称时才可使用。
7. 只做审计，不输出给玩家的替代叙事，不泄露私密状态。

只输出一个JSON对象：
{"valid":true|false,"category":"grounded|unsupported_external_result|private_fact_disclosure|contradicts_state|failed_receipt_claim|needs_tool","unsupported_claims":["简短列出"],"correction_hint":"告诉核心GM应调用哪类工具或如何只澄清现状"}
""".strip()


TOOL_PROPOSAL_GROUNDING_SYSTEM_PROMPT = """
你是FU-GM工具写入前的语义一致性审计器，不负责续写剧情。核心GM尚未执行拟议工具；你要判断这项提案是否可以安全写入权威状态。

判定规则：
1. current_message中的玩家话语只能证明该玩家角色明确说过、尝试过或选择过什么，不能证明NPC已经回应、动作成功、线索出现、物品易手、场景抵达、检定通过或环境已经变化。
1a. 审计对象仅是proposed_tool的字段及其将触发的写入；current_message只是证据来源，不是待写入声明。玩家解释为何改用某项行动（例如“MP不足所以改用攻击”）若没有被提案写成状态字段，不得列为unsupported_claim；只审查actor、action、target和details等实际提案参数是否有依据。
2. 玩家问题、猜测和条件句中的前提不是既成事实。“刚才谁提到了庄园？”不能证明有人提过庄园；工具不得虚构说话者，再把这一错误前提写入NPC记忆或场景事实。若本轮主要是在核对“刚才谁说过/是否提过X”，只能依据recent_public_context回答这项回忆问题；若没有公开依据，应只澄清没人提过或玩家可能听错，不能借这个错误前提在同一回应里首次揭示X相关的隐藏事实。
3. NPC回应和NPC行动工具可以让NPC在本轮首次说话或行动，但内容必须符合当前NPC人格、动机、知识、权限、位置、既有承诺与场景状态。它可以在玩家真正询问NPC所知内容时揭示NPC原本知道且有理由公开的事实，却不能把一次错误前提核对偷换成新的情报揭示，也不能伪造此前公开发生过的言行。decide_npc_response的position_note只记录玩家角色站位，不能证明NPC已经移动；若public_segments声称NPC进入、离开或抵达另一个具名地点，却没有同一事务中的权威NPC移动写入，判为gm_must_repair，并明确要求场外具名NPC改用introduce_npc。introduce_npc本身会原子提交该NPC进入当前场景，因此不能因该人物执行前尚未在participants中而拒绝它。多人同时抵达时必须只调用一次introduce_npc，以其中一人为主NPC，并把公开描述中明确指认的普通随从放入introduced_npcs；不能在同一批次重复调用introduce_npc来拼接同一次登场。
3a. 玩家明确指名行动目标时，工具参数必须保留同一目标。不得为了迁就当前敌方名单，把具名个体替换为其所属集体、把集体替换为其中一员，或改成另一个合法目标；若目标尚未进入冲突，应要求核心GM修复场景或冲突名单，而不是代玩家改目标。
3b. start_conflict必须保留当前消息明确列出的已有规则卡参战者；不能把财团机兵、狙击手等独立敌人折叠为“巡逻队”之类的集体，也不能为了简化回合表省略其中一项。
3c. prior_tool_receipts中的成功回执是本事务已经建立的权威依据，尤其可以支持回执明确要求的required_followup_tools及其稳定参数。失败回执不能提供这种依据，也不能把成功回执没有授权的内容补成既成事实。
4. 场景回应、开场和转场工具可以在GM权限内首次建立环境变化或新场景素材，但不得与已公开事实冲突，不得把玩家尚未完成的意图写成结果，也不得把GM私密暗线冒充成玩家已经知道的事实。
5. NPC建档或状态修订只能来自当前玩家明确贡献、当前权威状态、已提交结果，或GM在当前场景中有权新引入的内容；不能把玩家的提问性前提当证据。
6. resolve_rule_window的InvokeTrait必须满足两项：invocation_rationale确实是玩家当前消息中亲自给出的理由；该理由能说明所选身份、主题或故乡为何有助于当前检定。核心GM不得替玩家补写相关性。
7. declare_check_action、declare_movement_check或perform_check_action中的success_observation必须已经填实。物件名、痕迹内容、方向地点或办法本身应具体可验证；类别占位句判为gm_must_repair，并要求核心GM从当前局面和私有准备中选定实际答案后重提。纯移动检定以一个当前可处理的阻碍为边界，落点须由玩家本句明确选择，或是权威场景已经确认并与当前位置直接相连的下一处地点。寻找、探索、逃离、追踪等方向性目标只证明行动方向；路径、途中选择或主要障碍尚未建立时，成功结果结算眼前移动或下一段路线，宏观终点继续作为后续目标。玩家明确选择一次完成整段旅程、追逐或撤离，且权威状态已建立路径范围与主要障碍时，才可用一次移动检定抵达宏观终点。额外的物件、线索或静态发现须对应玩家同句明确执行的观察或调查。
8. end_session的closing_image必须只含当前公开状态能够支持的画面，并在同一意象中呈现本场实际选择造成的变化；不能为凑漂亮结尾宣称未完成的逃脱、团聚、胜利或取得物。
9. 同场景移动使用move_group_within_scene，行动者独自移动时companions必须为空；current_message明确让NPC本次随行，或权威状态已有仍有效的持续同行承诺时，才把该NPC列入companions。NPC仅仅在场、被看见、被交谈或提醒、active_goal想跟随，都不能证明它已经移动；玩家写明“独自”或NPC仍在另一处时，任何非空companions都属于contradicts_state。原地守望、照看或确定性小动作使用perform_in_scene_action。明确前往另一个独立地点且道路无阻时使用move_scene_group；移动本身存在一个具体阻碍、抵达下一处落点结果不确定时使用declare_movement_check。success_observation、success_transition、purpose、obstacle和failure_consequence必须描述同一个行动单位：成功抵达本句落点，失败只改变该阻碍附近的处境。已经提交的路线、线索和中间成果保持成立。持续逼近、跨区域封锁或会约束后续多步行动的后果，须由本事务刚触发且已精确登记该后果的命刻、到期承诺、当前NPC行动或结构化场景危害直接支持。玩家同一句还明确观察、搜索或辨认沿途事物时，一次declare_movement_check可以同时裁定抵达与一个具体静态发现，并履行相应的移动与观察意图事项。declare_movement_check不接受continue_with_check。先无阻碍移动、再进行逻辑上独立的观察或调查时，对应的move_group_within_scene或move_scene_group设置continue_with_check=true，由成功回执要求下一步调用declare_check_action。移动后明确施放已知法术、使用技能、攻击或启动仪式时，设置continue_with_rule_action=true并调用对应专用规则工具。
10. end_conflict若在outcome或public_reply中声称某个玩家角色已经撤离、逃出或抵达另一地点，必须在exit_transitions中为该角色提交实际目的地；只用文字结束冲突而不改变位置，判为gm_must_repair。
11. commit_story_item_action必须覆盖current_message中该物件动作结束时的完整最终状态。玩家先捡起、随后抛出、放下或留在别处时，只提交acquire属于半截意图，判为gm_must_repair；应使用place和最终to_location一次落位。玩家把物件抛到、推到或放到另一名PC身边，不等于该PC已经接住或取得，除非对方本人已明确接受，否则不得使用transfer或填写to_holder。玩家已经完整公开了确定性动作且没有新的外部裁定结果时，public_result应为空，状态写入可以静默；不得为了确认写入而复述玩家动作。
12. 只审查提案，不执行工具，不输出面向玩家的叙事，也不泄露私密状态。权威状态中的NPC标准名、真实身份、秘密和动机不自动属于公开证据；若工具公开文本首次用后台专名替换最近公开聊天中的匿名人物，判为private_fact_disclosure，除非该工具本来就在当前合理互动中明确执行自我介绍或身份揭示。
13. current_authoritative_state.turn_participants.player_character_aliases若把一个桌外玩家名唯一映射到某个玩家角色，current_message中该玩家名可视为对该角色的桌边简称；工具必须使用角色名作为actor、companions或行动摘要中的世界内身份。此种唯一归一化有权威依据，不属于虚构人物或篡改玩家意图；一个玩家对应多个角色且无法从本句消歧时才要求澄清。
14. current_scene只是当前镜头，不代表其他分支已经结束。角色不在current_scene.participants中时，必须继续检查scene.known_actor_locations、scene.known_actor_positions、scene.active_scene_branches、gameplay.character_locations、gameplay.character_positions和gameplay.active_scene_branches。细粒度站位（如“旧路闸门内侧”）可以细化较粗地点（如“白花碑驿站·风铃廊”），二者不自动矛盾。declare_check_action等行动工具会在执行前把镜头聚焦到行动者的权威分支；不得仅因当前镜头没有该角色而拒绝其行动或要求玩家重复移动。
15. 失败分类必须区分责任：工具名、参数、占位成功答案、遗漏状态写入或GM可依据现有信息自行修好的提案，使用gm_must_repair；只有玩家消息本身缺少一个无法由公开上下文和权威状态唯一确定的必要选择，或玩家需要亲自作出规则选择时，才使用needs_player_clarification。位置已由活动分支或全局位置账本确认时不属于玩家缺项。
17. perform_character_action只能提交玩家实际声明的攻击、装备、防御、技能、法术、装置或消耗物资行动。玩家声明撤离、穿过阻碍或前往另一地点时，不能为了满足action_type枚举而改写成Guard或其他战斗行动；应根据是否存在阻碍分别使用declare_movement_check、move_group_within_scene或move_scene_group。若当前工具无法表达玩家原意，判为gm_must_repair，并要求核心GM保留原意重选工具，绝不能替玩家选择另一项合法行动。
18. 玩家明确观察、触摸或研究眼前某个普通对象时，declare_check_action.base_observation可以由GM确认该对象存在，并补充开始检定前最低限度、立即可见的外观；这正是GM建立眼前局面的权限，不要求先调用commit_scene_response。玩家针对已公开为反复或即将发生的GM现场信号声明预备行动时，base_observation也可确认该信号此刻再次出现，并直接建立对应检定。base_observation不得借机确认玩家问题中的答案、隐藏性质、NPC反应、物件归属变化，亦不得与公开聊天或权威状态冲突。明显改变当前局面的新人物、怪物、出口、宝物或宏大设施仍须已有场景依据或专用场景工具，不能仅凭玩家错误前提生成。
19. 冲突中的玩家行动提案只负责表达玩家原意，最终由硬规则路由决定立即执行还是写入回合外收件箱。perform_character_action、perform_ritual_project_action等带timing字段的工具在timing=defer时明确只缓存；declare_check_action、declare_movement_check及其后续perform_check_action则会由规则层在发现actor并非current_actor时自动缓存。缓存不声称检定、攻击、法术、仪式或技能已经执行，也不消耗或替换current_actor的回合。因此，actor不是current_actor、actor本轮已经行动、当前行动者是NPC或NPC回合刚刚结束，都不能单独成为contradicts_state或gm_must_repair的理由。仍需检查玩家是否控制该actor、是否真的声明了该动作，以及目标、属性、武器、法术或技能是否有依据；这些内容不成立时照常拒绝。

只输出一个JSON对象：
{"valid":true|false,"category":"grounded|unsupported_external_result|private_fact_disclosure|contradicts_state|false_premise|trait_rationale_unverified|gm_must_repair|needs_player_clarification","unsupported_claims":["简短列出"],"correction_hint":"告诉核心GM应如何自行修正提案；仅needs_player_clarification可要求向玩家追问"}
""".strip()


SILENCE_RESPONSIBILITY_SYSTEM_PROMPT = """
你是FU-GM的静默职责复核器，不负责回答玩家、续写剧情或修改状态。核心GM准备保持静默；你只根据current_message和recent_public_context判断，这句话是否包含必须由游戏主持人回应的事项。

判定规则：
1. 玩家之间的分工、意见征询、角色内闲聊、玩笑和未执行提案，如果没有要求NPC、环境、规则或主持人回应，应当保持静默。
2. 玩家已经对NPC或环境采取行动、要求规则裁定、请求管理操作、直接向主持人提问，或正在回答开放的规则窗口，需要主持人处理。
3. 对最近公开聊天作记忆核对或桌面事实澄清，例如询问刚才是谁说过某事、有没有提过某项内容、某个代词指什么、某事是否已经发生，应当由主持人依据公开记录回答。即使问题写在角色动作或台词之后、没有点名主持人，只要没有明确向另一名PC或NPC发问，也属于table_fact_clarification。
4. 玩家问题中的前提不是事实。复核器只判断是否需要回应，不判断前提真伪，也不得建议借该问题公开后台秘密。
5. 如果语义存在合理歧义，优先保护真实玩家问题不被无声吞掉；但不能把明确面向其他玩家的对话改成主持请求。
6. 玩家说“下一次波动出现时我立刻开锁”之类的话，若触发条件属于GM掌控的现场事件且已被公开为反复或即将发生，这是需要GM推进、裁定或明确回应的预备行动，不是玩家讨论；requires_gm_reply必须为true。只有纯粹约定另一名玩家未来配合、且当前没有向环境采取行动时，才可保持静默。
7. 玩家在本句中声称“某现象有固定节奏”不等于该规律已经成为公开事实；只能依据recent_public_context确认现场信号是否反复或即将发生。
8. “角色A对角色B说：下一次一起动，你负责X，我负责Y”通常是等待角色B确认的分工提议。即使句中描述了角色A未来负责的部分，只要它仍以另一名玩家配合为前提、没有另列一项无条件立即生效的行动，就应保持静默。
9. 与上项不同，“下一次波动出现时，我立刻抓住锁簧开门”是当前玩家对自己角色作出的完整预备行动，不依赖另一名玩家确认；若触发条件由GM掌控，应要求GM回应。
10. 结合core_proposed_semantics.delivery与has_independent_followup复核。交付目标明确是另一名玩家，且本句只有已说出的玩家台词或等待同伴确认的讨论时，不得仅因GM有能力推进环境就把提议升级成行动。

只输出一个JSON对象：
{"requires_gm_reply":true|false,"category":"table_fact_clarification|rules_request|npc_or_world_interaction|management_request|direct_gm_request|player_discussion|idle|external","reason":"一句简短语义依据"}
""".strip()


GROUNDING_EVIDENCE_PROTOCOL = """
## 逐断言证据流程

审计前先把待审内容拆成最小事实断言，每条至少标出主语、动作或状态、对象、地点与时态，再按以下优先级寻找直接依据：成功工具回执高于当前权威状态，当前权威状态高于最近公开聊天，最近公开聊天高于玩家本轮对自己角色意图与动作的声明。低优先级内容不能覆盖高优先级事实；玩家的猜测、目的、条件句、比喻和问题前提都不能补足缺失证据。没有证据只表示不能确认，不要擅自把它判成相反事实。这个优先级只判断事实真伪，不授予公开权限：后台身份即使真实，也必须另有公开证据才能对玩家说出。

逐条检查是否发生了“尝试变成功、意图变结果、递出变接收、寻找变抵达、看到变取得、准备变完成、NPC沉默变同意、失败回执变成功叙述、私密准备变公开线索”。只要其中一条外部结果缺少直接支持，整体valid必须为false，并在unsupported_claims中只列真正越界的最小断言。若所有外部变化都有成功回执或既有权威状态支持，措辞差异、合理的感官修饰和不改变事实的简短衔接不应误判。

普通光线、天气、材质、气味、远近背景声和不会提供线索或规则优势的装饰性陈设，可以作为既有地点的感官润色，无需额外工具。例如“潮雾贴着廊柱”“木梁带着盐霜”可以保留。与此不同，新增可利用物件或出口、NPC已经拒绝或答应、某人刚听见自己的名字、追兵已经逼近、隐藏线索出现、命刻发生变化，都会改变角色可采取的判断或行动，必须由场景、NPC、检定、命刻或其他成功回执支持。混合文本中只有后一类断言越界时，只列出并删除这些断言，不要要求把其余合规的感官描写一起改成概括性说明或行动菜单。

correction_hint只指出需要补哪类权威工具、删去哪项无依据结论，或应向玩家追问哪个必要参数；不得代写新剧情、补造NPC反应或泄露尚未公开的私密内容。
""".strip()

REPLY_GROUNDING_SYSTEM_PROMPT = (
    REPLY_GROUNDING_SYSTEM_PROMPT
    + "\n\n"
    + GROUNDING_EVIDENCE_PROTOCOL
)
TOOL_PROPOSAL_GROUNDING_SYSTEM_PROMPT = (
    TOOL_PROPOSAL_GROUNDING_SYSTEM_PROMPT
    + "\n\n"
    + GROUNDING_EVIDENCE_PROTOCOL
)

TOOL_PROPOSAL_BATCH_GROUNDING_SYSTEM_PROMPT = (
    TOOL_PROPOSAL_GROUNDING_SYSTEM_PROMPT
    + "\n\n"
    + """
本次输入包含proposed_tools数组。逐项独立审计，但允许结合数组中的事务顺序
判断前后工具是否共同完成一件事。不得因为后一项能够修复前一项，就把前一项
本身的越权写入判为合法。proposal_index必须原样返回且每项恰好出现一次。
同一批次重复调用introduce_npc不构成合法的群体登场；应要求核心GM改为一次
introduce_npc，并把共同出现的普通随从放进该调用的introduced_npcs。后一项
introduce_npc不能为前一项public_reply中提前声称已到场的人物补写依据。

只输出一个JSON对象：
{"reviews":[{"proposal_index":0,"valid":true|false,"category":"grounded|unsupported_external_result|private_fact_disclosure|contradicts_state|false_premise|trait_rationale_unverified|gm_must_repair|needs_player_clarification","unsupported_claims":["简短列出"],"correction_hint":"简短修正方向"}]}
""".strip()
)


@dataclass(frozen=True)
class GMReplyGroundingReview:
    valid: bool
    category: str = "grounded"
    unsupported_claims: tuple[str, ...] = field(default_factory=tuple)
    correction_hint: str = ""


@dataclass(frozen=True)
class GMSilenceResponsibilityReview:
    requires_gm_reply: bool
    category: str = "player_discussion"
    reason: str = ""


def _locally_ground_exact_same_target_dual_wield_attack(
    *,
    current_message: str,
    observed_state: dict[str, object],
    tool_name: str,
    arguments: object,
) -> GMReplyGroundingReview | None:
    """Accept one fully literal dual-wield proposal without semantic drift.

    The semantic reviewer occasionally treats a player's explanatory clause
    (for example, why they stopped casting) as though it were a field being
    written by ``perform_character_action``.  This narrow path checks only an
    Attack whose complete dual-wield structure is already literal in the
    player's message and whose equipped weapons/target are authoritative.  The
    normal Python action handler still validates weapon type, handedness and
    every other rules/transaction invariant.
    """

    if str(tool_name or "").strip() != "perform_character_action":
        return None
    if not isinstance(arguments, dict):
        return None
    allowed_arguments = {
        "action_type",
        "actor",
        "target",
        "timing",
        "details",
        "source_event_id",
    }
    if set(arguments) - allowed_arguments:
        return None
    if str(arguments.get("action_type") or "").strip() != "Attack":
        return None
    if str(arguments.get("timing") or "").strip() != "immediate":
        return None

    details = arguments.get("details")
    if not isinstance(details, dict) or set(details) != {"dual_wield", "targets"}:
        return None
    if details.get("dual_wield") is not True:
        return None
    raw_targets = details.get("targets")
    if not isinstance(raw_targets, list) or len(raw_targets) != 2:
        return None
    targets = [str(item or "").strip() for item in raw_targets]
    # This fast path exists for the same-target ambiguity seen in the live
    # Boss probe.  Distinct-target phrasing remains under semantic review.
    if not targets[0] or targets[0] != targets[1]:
        return None

    actor = str(arguments.get("actor") or "").strip()
    target = str(arguments.get("target") or "").strip()
    if not actor or target != targets[0]:
        return None

    gameplay = observed_state.get("gameplay")
    if not isinstance(gameplay, dict):
        return None
    controlled = {
        str(item or "").strip()
        for item in list(gameplay.get("controlled_characters") or [])
        if str(item or "").strip()
    }
    if actor not in controlled:
        return None
    character_rows = [
        row
        for row in list(gameplay.get("characters") or [])
        if isinstance(row, dict) and str(row.get("name") or "").strip() == actor
    ]
    if len(character_rows) != 1:
        return None
    character = character_rows[0]
    if character.get("can_act") is not True:
        return None
    equipped = character.get("equipped")
    if not isinstance(equipped, dict):
        return None
    main_hand = str(equipped.get("main_hand") or "").strip()
    off_hand = str(equipped.get("off_hand") or "").strip()
    if not main_hand or not off_hand or main_hand == off_hand:
        return None
    runtime = observed_state.get("runtime")
    runtime_conflict = runtime.get("conflict") if isinstance(runtime, dict) else None
    resolution = (
        runtime_conflict.get("resolution_status")
        if isinstance(runtime_conflict, dict)
        else None
    )
    if (
        not isinstance(runtime_conflict, dict)
        or runtime_conflict.get("active") is not True
        or not isinstance(resolution, dict)
    ):
        return None
    active_hostiles = {
        str(item or "").strip()
        for item in list(resolution.get("active_hostiles") or [])
        if str(item or "").strip()
    }
    if target not in active_hostiles:
        return None

    message = "".join(str(current_message or "").split()).translate(
        str.maketrans("", "", "【】[]")
    )
    if not message or actor not in message:
        return None
    if any(
        marker in message
        for marker in (
            "不使用双持",
            "不要双持",
            "不得双持",
            "不是双持",
            "不能双持",
            "不使用双武器",
            "不要双武器",
            "不得双武器",
            "不是双武器",
            "不能双武器",
            "不进行双武器",
        )
    ):
        return None
    if not any(marker in message for marker in ("双武器", "双持")):
        return None
    if main_hand not in message or off_hand not in message:
        return None
    if "两次" not in message or "命中检定" not in message:
        return None
    if not any(
        marker in message
        for marker in (f"都攻击{target}", f"均攻击{target}")
    ):
        return None

    return GMReplyGroundingReview(
        valid=True,
        category="local_authoritative_same_target_dual_wield",
    )


def _locally_ground_known_spell_intent(
    *,
    current_message: str,
    observed_state: dict[str, object],
    tool_name: str,
    arguments: object,
) -> GMReplyGroundingReview | None:
    """Prove only a literal known-spell declaration without settling it.

    This path deliberately does not decide MP cost, turn order, legal target
    type, target count, spell parameters, hit, damage or effects.  It merely
    proves that the controlled actor, learned spell, optional element and
    proposed targets are all literal in the player's current message.  The
    normal ``perform_character_action`` Python handler remains authoritative
    for every rules and transaction invariant.
    """

    if str(tool_name or "").strip() != "perform_character_action":
        return None
    if not isinstance(arguments, dict):
        return None
    allowed_arguments = {
        "action_type",
        "actor",
        "target",
        "timing",
        "details",
        "source_event_id",
    }
    if set(arguments) - allowed_arguments:
        return None
    if str(arguments.get("action_type") or "").strip() != "Spell":
        return None
    if str(arguments.get("timing") or "").strip() not in {
        "immediate",
        "defer",
    }:
        return None

    details = arguments.get("details")
    if not isinstance(details, dict):
        return None
    allowed_details = {
        "spell_name",
        "targets",
        "element",
        "chosen_damage_type",
    }
    if set(details) - allowed_details:
        return None
    if details.get("element") not in (None, "") and details.get(
        "chosen_damage_type"
    ) not in (None, ""):
        return None
    spell_name = str(details.get("spell_name") or "").strip()
    if not spell_name:
        return None

    actor = str(arguments.get("actor") or "").strip()
    gameplay = observed_state.get("gameplay")
    if not actor or not isinstance(gameplay, dict):
        return None
    controlled = {
        str(item or "").strip()
        for item in list(gameplay.get("controlled_characters") or [])
        if str(item or "").strip()
    }
    if actor not in controlled:
        return None
    character_rows = [
        row
        for row in list(gameplay.get("characters") or [])
        if isinstance(row, dict) and str(row.get("name") or "").strip() == actor
    ]
    if len(character_rows) != 1:
        return None
    known_spells = {
        str(item or "").strip()
        for item in list(character_rows[0].get("spells") or [])
        if str(item or "").strip()
    }
    if spell_name not in known_spells:
        return None

    raw_targets = details.get("targets")
    if raw_targets is None:
        targets = [str(arguments.get("target") or "").strip()]
    elif isinstance(raw_targets, list) and 1 <= len(raw_targets) <= 3:
        targets = [str(item or "").strip() for item in raw_targets]
    else:
        return None
    if not all(targets) or len(set(targets)) != len(targets):
        return None
    target = str(arguments.get("target") or "").strip()
    if not target or target != targets[0]:
        return None

    message = "".join(str(current_message or "").split()).translate(
        str.maketrans("", "", "【】[]「」『』")
    )
    if not message or actor not in message:
        return None
    positive_spell_markers = tuple(
        f"{verb}{spell_name}" for verb in ("施放", "施展", "使用")
    )
    if not any(marker in message for marker in positive_spell_markers):
        return None
    negative_spell_markers = tuple(
        f"{prefix}{marker}"
        for marker in positive_spell_markers
        for prefix in ("不", "不要", "不得", "不能", "取消", "放弃", "停止")
    ) + (
        f"{spell_name}不施放",
        f"{spell_name}不使用",
    )
    if any(marker in message for marker in negative_spell_markers):
        return None

    for proposed_target in targets:
        if any(
            marker in message
            for marker in (
                f"不攻击{proposed_target}",
                f"不要攻击{proposed_target}",
                f"不保护{proposed_target}",
                f"不要保护{proposed_target}",
                f"不以{proposed_target}为目标",
                f"不要以{proposed_target}为目标",
            )
        ):
            return None

    joins = {
        separator.join(targets)
        for separator in ("和", "与", "、", "及")
    }
    target_phrases = {
        f"{verb}{joined}"
        for verb in ("攻击", "保护", "治疗")
        for joined in joins
    } | {
        f"以{joined}为目标"
        for joined in joins
    } | {
        f"目标{copula}{joined}"
        for copula in ("是", "为")
        for joined in joins
    }
    if not any(phrase in message for phrase in target_phrases):
        return None

    selected_element = details.get("chosen_damage_type")
    if selected_element in (None, ""):
        selected_element = details.get("element")
    if selected_element not in (None, ""):
        raw_element = str(selected_element).strip().lower()
        element_labels = {
            "wind": "风",
            "lightning": "雷",
            "ice": "冰",
            "fire": "火",
            "earth": "土",
            "dark": "暗",
            "light": "光",
            "poison": "毒",
            "physical": "物理",
        }
        element_evidence = {
            raw_element,
            element_labels.get(raw_element, ""),
        }
        if not any(value and value in message for value in element_evidence):
            return None

    return GMReplyGroundingReview(
        valid=True,
        category="local_authoritative_known_spell_intent",
    )


def _locally_ground_authoritative_natural_end_conflict(
    *,
    observed_state: dict[str, object],
    tool_name: str,
    arguments: object,
) -> GMReplyGroundingReview | None:
    """Accept only the minimal closure already signed by conflict state.

    This proves neither a narrated ending nor any movement.  It accepts only
    the exact natural outcome exposed by Python after the hostile side has no
    active combatants and every closure-relevant decision has settled.  The
    normal ``end_conflict`` handler still owns blocking-window validation,
    scene restoration, creative rendering, persistence and the commit.
    """

    if str(tool_name or "").strip() != "end_conflict":
        return None
    if not isinstance(arguments, dict) or set(arguments) != {
        "outcome",
        "continue_scene",
    }:
        return None
    outcome = str(arguments.get("outcome") or "").strip()
    # Ending the entire parent scene is a separate semantic decision.  The
    # local proof is intentionally limited to returning to the same scene.
    if (
        outcome != "hostile_side_removed"
        or arguments.get("continue_scene") is not True
    ):
        return None

    runtime = observed_state.get("runtime")
    conflict = runtime.get("conflict") if isinstance(runtime, dict) else None
    resolution = (
        conflict.get("resolution_status")
        if isinstance(conflict, dict)
        else None
    )
    if (
        not isinstance(conflict, dict)
        or conflict.get("active") is not True
        or not isinstance(resolution, dict)
        or resolution.get("ready_for_natural_end") is not True
        or str(resolution.get("natural_outcome") or "").strip() != outcome
    ):
        return None
    # Missing fields are not equivalent to authoritative empty collections.
    if resolution.get("active_hostiles") != []:
        return None
    active_player_side = resolution.get("active_player_side")
    if (
        not isinstance(active_player_side, list)
        or not any(str(item or "").strip() for item in active_player_side)
    ):
        return None
    if resolution.get("pending_exit_transitions") != []:
        return None
    if resolution.get("pending_zero_hp_characters") != []:
        return None

    decision_rows: list[dict[str, object]] = []
    decision_projection_seen = False
    gameplay = observed_state.get("gameplay")
    if isinstance(gameplay, dict) and "pending_decisions" in gameplay:
        raw_gameplay_decisions = gameplay.get("pending_decisions")
        if not isinstance(raw_gameplay_decisions, list):
            return None
        if not all(isinstance(item, dict) for item in raw_gameplay_decisions):
            return None
        decision_rows.extend(raw_gameplay_decisions)
        decision_projection_seen = True
    processes = observed_state.get("processes")
    process_decisions = (
        processes.get("decisions") if isinstance(processes, dict) else None
    )
    if isinstance(process_decisions, dict) and "pending" in process_decisions:
        raw_process_decisions = process_decisions.get("pending")
        if not isinstance(raw_process_decisions, list):
            return None
        if not all(isinstance(item, dict) for item in raw_process_decisions):
            return None
        decision_rows.extend(raw_process_decisions)
        decision_projection_seen = True
    if not decision_projection_seen:
        return None

    closure_relevant_kinds = {
        "acceleration_benefit",
        "check_roll_confirmation",
        "critical_opportunity",
        "held_action",
        "immediate_attack",
        "initiative_support",
        "npc_fate",
        "opportunity_parameter",
        "reactive_check",
        "skill_judgement",
        "skill_parameter",
        "spell_parameter",
        "zero_hp",
    }
    for row in decision_rows:
        if any(str(key).startswith("_") for key in row):
            return None
        status = str(row.get("status") or "pending").strip().lower()
        if status not in {"", "pending"}:
            continue
        if not isinstance(row.get("blocking"), bool):
            return None
        kind = str(row.get("kind") or "").strip()
        # Trait invocation is explicitly non-blocking: the underlying check
        # has already committed and the runtime end tool does not wait on it.
        if kind == "trait_invocation" and row.get("blocking") is False:
            continue
        if (
            row.get("blocking") is True
            or str(row.get("scope_kind") or "").strip() == "conflict"
            or kind in closure_relevant_kinds
        ):
            return None

    return GMReplyGroundingReview(
        valid=True,
        category="local_authoritative_natural_end_conflict",
    )


class GMReplyGroundingVerifier:
    """Semantically reject public prose that outruns authoritative receipts."""

    def __init__(
        self,
        client: Any,
        *,
        model: str,
        max_output_tokens: int = 900,
    ) -> None:
        self.client = client
        self.model = str(model or "").strip()
        self.max_output_tokens = max(256, int(max_output_tokens))

    def verify(
        self,
        *,
        current_message: str,
        recent_context: str,
        observed_state: dict[str, object],
        receipts: list[GMToolReceipt],
        proposed_reply: str,
        message_kind: str,
        decision_reason: str,
        deadline: float,
    ) -> GMReplyGroundingReview:
        request = {
            "current_message": str(current_message or "").strip(),
            "recent_public_context": str(recent_context or "").strip(),
            "current_authoritative_state": observed_state,
            "successful_and_failed_receipts": [
                receipt.to_dict() for receipt in receipts
            ],
            "proposed_public_reply": str(proposed_reply or "").strip(),
            "core_message_kind": str(message_kind or "").strip(),
            "core_decision_reason": str(decision_reason or "").strip(),
        }
        response_format = (
            {"type": "json_object"}
            if bool(
                getattr(
                    getattr(self.client, "config", None),
                    "response_format_enabled",
                    True,
                )
            )
            else None
        )
        request_json = json.dumps(
            json_safe_value(request),
            ensure_ascii=False,
        )
        raw = self.client.create_chat_completion(
            model=self.model,
            messages=build_cache_friendly_messages(
                static_system_prompt=REPLY_GROUNDING_SYSTEM_PROMPT,
                user_content=request_json,
                cache_family="ground-reply",
                user_cache_breakpoint_offsets=(
                    request_json.find('"proposed_public_reply"'),
                ),
            ),
            temperature=0.0,
            response_format=response_format,
            max_tokens=self.max_output_tokens,
            deadline=deadline,
            operation="gm_reply_grounding_verification",
            thinking_enabled=False,
            max_recovery_retries=1,
            retry_without_response_format_on_empty=True,
        )
        payload = extract_json_object(raw)
        if not isinstance(payload.get("valid"), bool):
            raise ValueError("回复事实审计缺少布尔字段valid。")
        claims = payload.get("unsupported_claims")
        if not isinstance(claims, list):
            claims = []
        return GMReplyGroundingReview(
            valid=bool(payload["valid"]),
            category=str(payload.get("category") or "grounded").strip(),
            unsupported_claims=tuple(
                str(item or "").strip()[:240]
                for item in claims[:6]
                if str(item or "").strip()
            ),
            correction_hint=str(payload.get("correction_hint") or "").strip()[:500],
        )

    def verify_silence_responsibility(
        self,
        *,
        current_message: str,
        recent_context: str,
        gate_status: str,
        proposed_message_kind: str,
        proposed_audience: str,
        decision_reason: str,
        deadline: float,
        proposed_delivery: dict[str, object] | None = None,
        has_independent_followup: bool = False,
    ) -> GMSilenceResponsibilityReview:
        """复核一次拟静默决策是否漏掉了主持职责。"""

        request = {
            "current_message": str(current_message or "").strip(),
            "recent_public_context": str(recent_context or "").strip(),
            "session_phase": str(gate_status or "").strip(),
            "core_proposed_semantics": {
                "message_kind": str(proposed_message_kind or "").strip(),
                "audience": str(proposed_audience or "").strip(),
                "reason": str(decision_reason or "").strip(),
                "has_independent_followup": bool(has_independent_followup),
                "delivery": json_safe_value(proposed_delivery or {}),
            },
        }
        response_format = (
            {"type": "json_object"}
            if bool(
                getattr(
                    getattr(self.client, "config", None),
                    "response_format_enabled",
                    True,
                )
            )
            else None
        )
        request_json = json.dumps(json_safe_value(request), ensure_ascii=False)
        raw = self.client.create_chat_completion(
            model=self.model,
            messages=build_cache_friendly_messages(
                static_system_prompt=SILENCE_RESPONSIBILITY_SYSTEM_PROMPT,
                user_content=request_json,
                cache_family="route-silence",
                user_cache_breakpoint_offsets=(
                    request_json.find('"current_message"'),
                ),
            ),
            temperature=0.0,
            response_format=response_format,
            max_tokens=min(self.max_output_tokens, 480),
            deadline=deadline,
            operation="gm_silence_responsibility_verification",
            thinking_enabled=False,
            max_recovery_retries=1,
            retry_without_response_format_on_empty=True,
        )
        payload = extract_json_object(raw)
        if not isinstance(payload.get("requires_gm_reply"), bool):
            raise ValueError("静默职责复核缺少布尔字段requires_gm_reply。")
        return GMSilenceResponsibilityReview(
            requires_gm_reply=bool(payload["requires_gm_reply"]),
            category=str(payload.get("category") or "player_discussion").strip()[:80],
            reason=str(payload.get("reason") or "").strip()[:500],
        )

    def verify_tool_proposal(
        self,
        *,
        current_message: str,
        recent_context: str,
        observed_state: dict[str, object],
        tool_name: str,
        arguments: object,
        deadline: float,
        batch_context: list[dict[str, object]] | None = None,
        receipts: list[GMToolReceipt] | None = None,
    ) -> GMReplyGroundingReview:
        """Review a semantic write before the registry can mutate state."""

        local_review = _locally_ground_exact_same_target_dual_wield_attack(
            current_message=current_message,
            observed_state=observed_state,
            tool_name=tool_name,
            arguments=arguments,
        )
        if local_review is None:
            local_review = _locally_ground_known_spell_intent(
                current_message=current_message,
                observed_state=observed_state,
                tool_name=tool_name,
                arguments=arguments,
            )
        if local_review is None:
            local_review = _locally_ground_authoritative_natural_end_conflict(
                observed_state=observed_state,
                tool_name=tool_name,
                arguments=arguments,
            )
        if local_review is not None:
            return local_review

        request = {
            "current_message": str(current_message or "").strip(),
            "recent_public_context": str(recent_context or "").strip(),
            "current_authoritative_state": observed_state,
            "proposed_tool": {
                "tool_name": str(tool_name or "").strip(),
                "arguments": arguments,
            },
            "same_batch_proposals": list(batch_context or []),
            "prior_tool_receipts": [
                receipt.to_dict()
                for receipt in list(receipts or [])
                if receipt.ok
            ],
        }
        response_format = (
            {"type": "json_object"}
            if bool(
                getattr(
                    getattr(self.client, "config", None),
                    "response_format_enabled",
                    True,
                )
            )
            else None
        )
        request_json = json.dumps(
            json_safe_value(request),
            ensure_ascii=False,
        )
        raw = self.client.create_chat_completion(
            model=self.model,
            messages=build_cache_friendly_messages(
                static_system_prompt=TOOL_PROPOSAL_GROUNDING_SYSTEM_PROMPT,
                user_content=request_json,
                cache_family="ground-tool",
                user_cache_breakpoint_offsets=(
                    request_json.find('"proposed_tool"'),
                ),
            ),
            temperature=0.0,
            response_format=response_format,
            max_tokens=self.max_output_tokens,
            deadline=deadline,
            operation="gm_tool_proposal_grounding_verification",
            thinking_enabled=False,
            max_recovery_retries=1,
            retry_without_response_format_on_empty=True,
        )
        payload = extract_json_object(raw)
        if not isinstance(payload.get("valid"), bool):
            raise ValueError("工具提案审计缺少布尔字段valid。")
        claims = payload.get("unsupported_claims")
        if not isinstance(claims, list):
            claims = []
        return GMReplyGroundingReview(
            valid=bool(payload["valid"]),
            category=str(payload.get("category") or "grounded").strip(),
            unsupported_claims=tuple(
                str(item or "").strip()[:240]
                for item in claims[:6]
                if str(item or "").strip()
            ),
            correction_hint=str(payload.get("correction_hint") or "").strip()[:500],
        )

    def verify_tool_proposals(
        self,
        *,
        recent_context: str,
        observed_state: dict[str, object],
        proposals: list[dict[str, object]],
        deadline: float,
        batch_context: list[dict[str, object]] | None = None,
        receipts: list[GMToolReceipt] | None = None,
    ) -> tuple[GMReplyGroundingReview, ...]:
        """在一次模型往返中审计同一事务的多个自由文本提案。"""

        clean_proposals = [
            {
                "proposal_index": index,
                "evidence_message": str(
                    proposal.get("current_message") or ""
                ).strip(),
                "tool_name": str(proposal.get("tool_name") or "").strip(),
                "arguments": proposal.get("arguments"),
            }
            for index, proposal in enumerate(proposals)
            if isinstance(proposal, dict)
        ]
        if not clean_proposals:
            return ()
        request = {
            "recent_public_context": str(recent_context or "").strip(),
            "current_authoritative_state": observed_state,
            "proposed_tools": clean_proposals,
            "same_batch_proposals": list(batch_context or []),
            "prior_tool_receipts": [
                receipt.to_dict()
                for receipt in list(receipts or [])
                if receipt.ok
            ],
        }
        response_format = (
            {"type": "json_object"}
            if bool(
                getattr(
                    getattr(self.client, "config", None),
                    "response_format_enabled",
                    True,
                )
            )
            else None
        )
        request_json = json.dumps(
            json_safe_value(request),
            ensure_ascii=False,
        )
        raw = self.client.create_chat_completion(
            model=self.model,
            messages=build_cache_friendly_messages(
                static_system_prompt=TOOL_PROPOSAL_BATCH_GROUNDING_SYSTEM_PROMPT,
                user_content=request_json,
                cache_family="ground-tool-batch",
                user_cache_breakpoint_offsets=(
                    request_json.find('"proposed_tools"'),
                ),
            ),
            temperature=0.0,
            response_format=response_format,
            max_tokens=max(
                self.max_output_tokens,
                min(3600, len(clean_proposals) * 320),
            ),
            deadline=deadline,
            operation="gm_tool_proposals_grounding_verification",
            thinking_enabled=False,
            max_recovery_retries=1,
            retry_without_response_format_on_empty=True,
        )
        payload = extract_json_object(raw)
        raw_reviews = payload.get("reviews")
        if not isinstance(raw_reviews, list):
            raise ValueError("批量工具提案审计缺少reviews数组。")
        reviews_by_index: dict[int, GMReplyGroundingReview] = {}
        for item in raw_reviews:
            if not isinstance(item, dict):
                raise ValueError("批量工具提案审计包含非对象结果。")
            index = item.get("proposal_index")
            if not isinstance(index, int) or isinstance(index, bool):
                raise ValueError("批量工具提案审计缺少整数proposal_index。")
            if index in reviews_by_index or not 0 <= index < len(clean_proposals):
                raise ValueError("批量工具提案审计的proposal_index重复或越界。")
            if not isinstance(item.get("valid"), bool):
                raise ValueError("批量工具提案审计缺少布尔字段valid。")
            claims = item.get("unsupported_claims")
            if not isinstance(claims, list):
                claims = []
            reviews_by_index[index] = GMReplyGroundingReview(
                valid=bool(item["valid"]),
                category=str(item.get("category") or "grounded").strip(),
                unsupported_claims=tuple(
                    str(claim or "").strip()[:240]
                    for claim in claims[:6]
                    if str(claim or "").strip()
                ),
                correction_hint=str(
                    item.get("correction_hint") or ""
                ).strip()[:500],
            )
        expected = set(range(len(clean_proposals)))
        if set(reviews_by_index) != expected:
            raise ValueError("批量工具提案审计没有逐项返回完整结果。")
        return tuple(reviews_by_index[index] for index in sorted(expected))
