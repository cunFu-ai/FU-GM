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
7. 同时判断proposed_public_reply是否已经履行current_message中的直接主持请求。已经回答问题、解释拒绝、给出查询结果或完成当前裁定时request_fulfilled=true；“我来看看”“稍后处理”“接下来告诉你”等只承诺未来工作的回复为false。正常等待玩家下一轮选择不算未完成。
8. 只做审计，不输出给玩家的替代叙事，不泄露私密状态。

只输出一个JSON对象：
{"valid":true|false,"request_fulfilled":true|false,"category":"grounded|unsupported_external_result|private_fact_disclosure|contradicts_state|failed_receipt_claim|needs_tool","unsupported_claims":["简短列出"],"correction_hint":"告诉核心GM应调用哪类工具或如何只澄清现状"}
""".strip()


TOOL_PROPOSAL_GROUNDING_SYSTEM_PROMPT = """
你是FU-GM工具写入前的语义一致性审计器，不负责续写剧情。核心GM尚未执行拟议工具；你要判断这项提案是否可以安全写入权威状态。

判定规则：
1. current_message中的玩家话语只能证明该玩家角色明确说过、尝试过或选择过什么，不能证明NPC已经回应、动作成功、线索出现、物品易手、场景抵达、检定通过或环境已经变化。
1a. 审计对象仅是proposed_tool的字段及其将触发的写入；current_message只是证据来源，不是待写入声明。玩家解释为何改用某项行动（例如“MP不足所以改用攻击”）若没有被提案写成状态字段，不得列为unsupported_claim；只审查actor、action、target和details等实际提案参数是否有依据。
1b. 仅在frozen_message_semantics缺失时，才根据current_message独立判断行动是否落实。“要不要我试试”“我可以用X看看”“我们不如之后去问”“如果大家同意我就做”，以及“我打算处理X，你们谁去处理Y”这类仍在协调分工的整句，都是面向队友的提议、能力说明或条件计划，不等于角色已经开始行动；即使其中给出了具体技能、对象或方法，也不能调用declare_check_action、perform_character_action、perform_in_scene_action或其他行动工具。此类提案判为gm_must_repair，要求取消工具；没有其他主持职责时应让核心GM选择silent。只有“我现在试着……”“角色走近并开始……”或直接对NPC/环境执行的同等明确表达才授权裁定。应按整句语用判断，而不是把“打算”等词本身当作固定开关。
1c. frozen_message_semantics是本次审计最高优先级的不可变权威：核心GM已在事务首轮逐条解释并通过结构校验，审计器不得用规则1b、current_message的表面措辞或自己的重新分类覆盖其中的relation、dialogue_act、action_commitment与response_expectation。匹配事件的action_commitment=committed时，不能仅因dialogue_act是agreement、answer或其他言语功能就把行动工具拒为“只是讨论”；只审查actor、action、target、details与工具类型是否忠实实现该已提交行动。action_commitment为none、tentative、withdrawn或answer时，行动工具仍应拒绝。字段缺失时才按规则1b独立判断。
2. 玩家问题、猜测和条件句中的前提不是既成事实。“刚才谁提到了庄园？”不能证明有人提过庄园；工具不得虚构说话者，再把这一错误前提写入NPC记忆或场景事实。若本轮主要是在核对“刚才谁说过/是否提过X”，只能依据recent_public_context回答这项回忆问题；若没有公开依据，应只澄清没人提过或玩家可能听错，不能借这个错误前提在同一回应里首次揭示X相关的隐藏事实。
3. NPC回应和NPC行动工具可以让NPC在本轮首次说话或行动，但内容必须符合当前NPC人格、动机、知识、权限、位置、既有承诺与场景状态。核心GM不需要预先写完世界的每个局部细节：decide_npc_response可用fact_effects声明本轮新产生的持续事实。kind=objective表示GM在scene或local范围建立一个此前未定义的客观事实；只要它不与公开状态、私密准备或锁定暗线冲突，不绕过检定、机会、规则资源、物品归属、人物位置、玩家自主权或其他专用工具，也不擅自确定战役级真相，就不得仅因“此前没有写过”而拒绝。NPC公开这一objective时，其身份、所在位置、职责或知识范围还必须提供合理的信息来源；不相关的路人不能借objective变成全知者，宜改为claim、rumor或admit_unknown。kind=claim、rumor、lie只建立该NPC提出主张、转述传闻或有意说谎这一事实，其内容不成为客观真相；若public_segments把这些内容改写成全知旁白或确定事实，判为gm_must_repair。lie还必须符合NPC目标、风险判断与人格，不能把模型失误伪装成撒谎。NPC仍可以不知道、拒答、回避、误解或提出条件；不要为了推进而强迫NPC掌握答案。玩家本轮刚提供某人的姓名、外貌、经历或随身物，只证明NPC现在听见这些特征。若既有状态没有直接依据，提案也没有用fact_effects作出合规分类，任何“见过相似人物”“别人曾提到此人”“此人去了某处”都判为npc_knowledge_unsupported，并将repair_mode设为npc_fact_or_nonclaim；修正时保留同一NPC，合理即兴并提交fact_effects，或改用admit_unknown、refuse、deflect、new_gate，不能只换措辞继续提交未分类断言。错误前提核对仍不能被偷换成情报揭示，也不能伪造此前公开发生过的言行。decide_npc_response本身已经授权该NPC完成这一轮回应；皱眉、停顿、短暂移开视线、调整语气等不改变位置、不产生线索、不建立知识、承诺、条件或规则效果的同一NPC表演细节，不要求预先存在于权威状态，也不得单独列为unsupported_claim。NPC移动到另一地点、让其他NPC采取行动、交付物品或形成有后续约束的态度变化仍须照常审计。decide_npc_response的position_note只记录玩家角色站位，不能证明NPC已经移动；若public_segments声称NPC进入、离开或抵达另一个具名地点，却没有同一事务中的权威NPC移动写入，判为gm_must_repair，并明确要求场外具名NPC改用introduce_npc。introduce_npc本身会原子提交该NPC进入当前场景，因此不能因该人物执行前尚未在participants中而拒绝它。多人同时抵达时必须只调用一次introduce_npc，以其中一人为主NPC，并把公开描述中明确指认的普通随从放入introduced_npcs；不能在同一批次重复调用introduce_npc来拼接同一次登场。
3a. 玩家明确指名行动目标时，工具参数必须保留同一目标。不得为了迁就当前敌方名单，把具名个体替换为其所属集体、把集体替换为其中一员，或改成另一个合法目标；若目标尚未进入冲突，应要求核心GM修复场景或冲突名单，而不是代玩家改目标。
3b. start_conflict必须保留当前消息明确列出的已有规则卡参战者；不能把财团机兵、狙击手等独立敌人折叠为“巡逻队”之类的集体，也不能为了简化回合表省略其中一项。
3c. prior_tool_receipts中的成功回执是本事务已经建立的权威依据，尤其可以支持回执明确要求的required_followup_tools及其稳定参数。失败回执不能提供这种依据，也不能把成功回执没有授权的内容补成既成事实。
3d. decide_npc_response只在current_message中玩家实际对NPC说话、提问、交付信息或执行会直接引起该NPC反应的行动时获准。玩家之间提出“我们不如先听听旅人说什么”“谁去问会长”“他可能知道旧路”之类的建议、推测和分工，仍在等待玩家落实，不授权NPC抢先回答或GM替玩家执行互动；即使建议中提到了NPC姓名或可能掌握的情报，也应判为gm_must_repair，要求核心GM取消该工具并在没有其他主持职责时选择silent。不得因为GM能够让NPC说话，就把玩家讨论自动升级为NPC互动。
3d-1. 上一条只限制玩家消息触发的decide_npc_response，不限制Python明确签发的系统主动节拍。proposal_authority同时满足system_gm_beat_request=true、heartbeat_action=free_scene_beat、heartbeat_force=true时，该系统请求本身就是在场NPC或集体自主行动的合法触发源；若核心GM选择了本轮已开放的decide_npc_action或decide_collective_action，不得再以“玩家没有先与NPC互动”为由拒绝。此授权只解决行动时机，不替内容背书：NPC仍须在当前活动场景中、行动须符合其目标和能力，新增局部事实须用合规fact_effects分类，且不得泄露私密真相、替PC行动或绕过人物登场、移动、物件和规则专用工具。decide_npc_response仍不因该授权自动可用。
3e. confirm_session_zero_proposal只记录当前玩家对state中现存待定提案的明确赞成，并按该提案的生命周期继续提交；它不声称全桌每个人都已逐一表态。current_message出现“我赞成”“我同意”“我支持”等明确肯定时，即使后面又邀请其他玩家表态，或询问“要不要先定下”，前面的个人赞成仍然成立。只有纯条件句、反事实、转述他人意见或没有表达当前玩家立场时才应拒绝。replacement_world_operations仍需逐项检查，不能因确认原提案而自动获得额外内容的授权。
3f. propose_session_zero_update必须把current_message中彼此独立的待讨论方案完整保存在summary与结构化操作中；不能只摘录其中一项而吞掉另一项。玩家说同一个点子“可以作为地区、历史或威胁的种子”是在讨论这个点子的分类，不等于同时提出三个必须分别建档的事实；选择一个最贴切类别，并在summary中保留仍待桌面决定的分类即可。只有消息确实提出了多个内容不同、可分别接受或拒绝的方案时，才要求全部纳入同一提案包或分别保存。
3g. propose_session_zero_update只保存pending proposal，不会把其中内容写成已经成立的世界事实。玩家尚未达成共识、仍在问“你们觉得呢”、同时给出“旅人失踪或神秘信件”等候选分支，正是使用该工具的前提，不能因此要求取消工具、改为silent或等待玩家先选定。审计时检查summary与结构化操作是否忠实保留本轮实际提出的方案和未定分支；只有凭空增加原话没有的内容、遗漏独立方案、把待选分支写成已经发生，或触碰无关持久类别时才拒绝。world_operations描述的是方案确认后才会执行的计划，也不是当前既成事实。
3h. current_message若把pending_proposals中一个或多个既有具体对象合并、细化或改写成新的完整版本，propose_session_zero_update必须在superseded_proposal_ids中列出被新版取代的旧稿ID；否则旧稿会继续与新版并存并使后续“我同意”误绑。只在具体subject_keys相同、且近期对话确实形成新版关系时要求，不能因为同属kingdoms、mysteries等类别就清除互不相干的提案。遗漏时判为gm_must_repair，要求核心GM保留同一新提案并补齐准确ID。
3i. create_world_setting、update_world_setting或待定world_operations把内容写入world_threats时，value必须描述真正的危险主体或现象、危险的触发或发展方式，以及它危及的对象或后果。警报信号、木鼓节奏、巡守制度、封印仪式、避难办法、治疗手段或其他预警与应对措施本身不是世界威胁。若提案只有这些措施而没有危险本体，判为gm_must_repair；从当前消息能唯一恢复危险本体时要求核心GM补成完整威胁，不能恢复时改入更合适类别或保持讨论，不能把措施本身作为威胁落盘。
4. 场景回应、开场和转场工具可以在GM权限内首次建立环境变化或新场景素材，但不得与已公开事实冲突，不得把玩家尚未完成的意图写成结果，也不得把GM私密暗线冒充成玩家已经知道的事实。转场抵达描述可以为已经成立的目的地补充不产生规则优势的局部陈设、材质、光线和声音；不得仅靠抵达文字让一个尚未建档、未列入destination_npcs的人物突然登场或说话，必要时应省略该人物并在后续使用introduce_npc。
4a. transition_scene一次移动多个玩家角色时，除当前发言者控制的角色外，mover_consents必须逐项提供actor、speaker与其近期公开发言的逐字evidence。每条evidence都必须由控制该actor的玩家明确承诺前往本次同一location；提议、询问、条件计划、沉默或替别人表态都不能授权转场。证据与目的地一致时，这是合法的全队共同行动，不得仅因当前消息由最后一名玩家发出而拒绝其他已同意角色。
4b. move_scene_group只提交行动者、合法同行NPC、目的地，以及Python账本中已经由这些持有者携带的剧情物件位置；它不接受任意公开事实写入。public_result可以呈现首次抵达时立即可见的有限现场，但不能借玩家声称持有某物而新建物件归属，也不能新增线索、NPC行动或其他需要专用工具的结果。
4c. move_scene_group一次只能移动arguments.actor这一名玩家角色及companions中的NPC。public_result的叙述主语必须与这组实际移动者完全一致：不得用“你们”“众人”“队伍”“大家一起”等集合表达暗示未列出的其他PC也已抵达，也不得描写其他PC在目的地行动。若recent_public_context已经包含当前场景其他PC由各自玩家作出的同目的地明确同行承诺，应改用transition_scene并逐项提交mover_consents；否则只叙述actor及合法companions。这里比较的是自然语言表达的实际参与者，不要求目的地标签逐字出现；“推开半掩的图书馆大门”可以在上下文明确时表达抵达“静默图书馆”。
4d. 玩家同一句明确按顺序提交无阻碍移动和抵达后立即执行的独立行动时，移动工具必须保留第二步：普通调查或属性检定设置continue_with_check=true，施法、技能、攻击或仪式设置continue_with_rule_action=true。比如“进去后先找矿道旧档案”不是单纯移动；若对应续办字段为false，判为gm_must_repair。只有“待会儿可以比对”“之后再问”等尚未落实的未来计划不触发续办。
4e. proposal_authority是Python运行时签发的工具权限，不是核心GM自述。commit_scene_response只有在以下两组权限之一完整成立时，才可依据当前权威场景、private_situation、既有参与者、现场压力与升级阶梯首次建立有限的新变化：
  - system_gm_beat_request=true、heartbeat_action=free_scene_beat、gm_authored_free_scene_beat=true、heartbeat_require_material_change=true：推进当前局面的一个主动节拍；
  - system_gm_beat_request=true、heartbeat_action=scene_opening、gm_authored_scene_opening=true、heartbeat_require_material_change=true：公开呈现Python已经建立的当前场景，并为开场加入一个立即可感知、可供玩家回应的环境变化或在场NPC行动。
获得其中一组权限后，不得仅因该变化此前未被玩家说出或尚未公开就拒绝。scene_opening可以使用current_scene、private_situation、visible_elements、npc_functions以及当前participants中的NPC来建立开场压力；不在participants中的具名人物仍须先用introduce_npc，不能借开场文字凭空登场。两种权限都不是自由改写：public_reply与public_facts应以环境、物件或合法自主行动的NPC为事实主体，不得重演上一轮玩家动作，也不得把“玩家注意到、低头查看、重新操作、想起或判断”写成由GM完成的事实。仍须拒绝代替玩家角色移动、说话、取物或作决定，改写既有检定结果，直接揭晓谜题答案或隐藏真相，凭空创建重要人物、出口、宝物、地点、命刻或规则效果，以及与公开事实或锁定暗线冲突的变化；这些内容必须使用对应专用工具或等待玩家行动。若heartbeat_require_local_resolution=true，可以让既有局部压力自然兑现或收束，但不能替玩家选择解决方式。未获得任一完整权限组时，仍按普通证据规则审查commit_scene_response。
4f. proposed_tool.required_audits.player_agency=true时，必须额外逐句审计public_reply与public_facts的语法主语和已完成动作。把“你翻到报告结论页”“你走进房间”“你拿起物件”“你点头回答”等写成已经发生，都是GM替玩家角色行动；即使动作看似自然、无须检定或能推动剧情，也必须列入authored_player_actions并令player_agency_preserved=false。只把当前角色能够被动感知的声音、光线、气味或眼前变化交给玩家，例如“门后传来换挡声”，不算代操；NPC朝玩家走近也不是玩家角色行动。审计结果必须返回布尔字段player_agency_preserved与数组authored_player_actions，不得省略或用unsupported_claims代替。
5. NPC建档或状态修订只能来自当前玩家明确贡献、当前权威状态、已提交结果，或GM在当前场景中有权新引入的内容；不能把玩家的提问性前提当证据。
6. resolve_rule_window的InvokeTrait必须满足两项：invocation_rationale确实是玩家当前消息中亲自给出的理由；该理由能说明所选身份、主题或故乡为何有助于当前检定。核心GM不得替玩家补写相关性。
7. declare_check_action、declare_movement_check或perform_check_action中的success_observation与failure_consequence是“只有相应结果发生后才会提交和公开”的条件结果契约，不是在声称这些事情现在已经发生。核心GM可以依据当前场景、私有准备和主持权限，在不违背已公开事实、不替玩家行动且不超出本次手段范围的前提下，为尚未公开的局部答案作出具体选择；审计器检查一致性、尺度与玩家授权，不得仅因该答案此前未公开就判为unsupported。success_observation必须填实，物件名、痕迹内容、方向地点或办法本身应具体可验证，明确人数同样要直接写清；failure_consequence也可具体描述失败后才发生的局部反应。类别占位句判为gm_must_repair，并要求核心GM从当前局面和私有准备中选定实际答案后重提。纯移动检定以一个当前可处理的阻碍为边界，落点须由玩家本句明确选择，或是权威场景已经确认并与当前位置直接相连的下一处地点。寻找、探索、逃离、追踪等方向性目标只证明行动方向；路径、途中选择或主要障碍尚未建立时，成功结果结算眼前移动或下一段路线，宏观终点继续作为后续目标。玩家明确选择一次完成整段旅程、追逐或撤离，且权威状态已建立路径范围与主要障碍时，才可用一次移动检定抵达宏观终点。额外的物件、线索或静态发现须对应玩家同句明确执行的观察或调查。仍须拒绝与公开事实冲突、凭空引入宏观地点或无关人物、超出玩家调查范围的宝物，以及把条件结果写成已经公开发生的提案。
8. end_session的closing_image必须只含当前公开状态能够支持的画面，并在同一意象中呈现本场实际选择造成的变化；不能为凑漂亮结尾宣称未完成的逃脱、团聚、胜利或取得物。
9. 同场景移动使用move_group_within_scene，行动者独自移动时companions必须为空；current_message明确让NPC本次随行，或权威状态已有仍有效的持续同行承诺时，才把该NPC列入companions。NPC仅仅在场、被看见、被交谈或提醒、active_goal想跟随，都不能证明它已经移动；玩家写明“独自”或NPC仍在另一处时，任何非空companions都属于contradicts_state。原地守望、照看或确定性小动作使用perform_in_scene_action。明确前往另一个独立地点且道路无阻时使用move_scene_group；移动本身存在一个具体阻碍、抵达下一处落点结果不确定时使用declare_movement_check。success_observation、success_transition、purpose、obstacle和failure_consequence必须描述同一个行动单位：成功抵达本句落点，失败只改变该阻碍附近的处境。已经提交的路线、线索和中间成果保持成立。持续逼近、跨区域封锁或会约束后续多步行动的后果，须由本事务刚触发且已精确登记该后果的命刻、到期承诺、当前NPC行动或结构化场景危害直接支持。玩家同一句还明确观察、搜索或辨认沿途事物时，一次declare_movement_check可以同时裁定抵达与一个具体静态发现，并履行相应的移动与观察意图事项。declare_movement_check不接受continue_with_check。先无阻碍移动、再进行逻辑上独立的观察或调查时，对应的move_group_within_scene或move_scene_group设置continue_with_check=true，由成功回执要求下一步调用declare_check_action。移动后明确施放已知法术、使用技能、攻击或启动仪式时，设置continue_with_rule_action=true并调用对应专用规则工具。
10. end_conflict若在outcome或public_reply中声称某个玩家角色已经撤离、逃出或抵达另一地点，必须在exit_transitions中为该角色提交实际目的地；只用文字结束冲突而不改变位置，判为gm_must_repair。
11. commit_story_item_action必须覆盖current_message中该物件动作结束时的完整最终状态。玩家先捡起、随后抛出、放下或留在别处时，只提交acquire属于半截意图，判为gm_must_repair；应使用place和最终to_location一次落位。玩家把物件抛到、推到或放到另一名PC身边，不等于该PC已经接住或取得，除非对方本人已明确接受，否则不得使用transfer或填写to_holder。玩家已经完整公开了确定性动作且没有新的外部裁定结果时，public_result应为空，状态写入可以静默；不得为了确认写入而复述玩家动作。
12. 只审查提案，不执行工具，不输出面向玩家的叙事，也不泄露私密状态。权威状态中的NPC标准名、真实身份、秘密和动机不自动属于公开证据；若工具公开文本首次用后台专名替换最近公开聊天中的匿名人物，判为private_fact_disclosure，除非该工具本来就在当前合理互动中明确执行自我介绍或身份揭示。
13. current_authoritative_state.turn_participants.player_character_aliases若把一个桌外玩家名唯一映射到某个玩家角色，current_message中该玩家名可视为对该角色的桌边简称；工具必须使用角色名作为actor、companions或行动摘要中的世界内身份。此种唯一归一化有权威依据，不属于虚构人物或篡改玩家意图；一个玩家对应多个角色且无法从本句消歧时才要求澄清。
14. current_scene只是当前镜头，不代表其他分支已经结束。角色不在current_scene.participants中时，必须继续检查scene.known_actor_locations、scene.known_actor_positions、scene.active_scene_branches、gameplay.character_locations、gameplay.character_positions和gameplay.active_scene_branches。细粒度站位（如“旧路闸门内侧”）可以细化较粗地点（如“白花碑驿站·风铃廊”），二者不自动矛盾。declare_check_action等行动工具会在执行前把镜头聚焦到行动者的权威分支；不得仅因当前镜头没有该角色而拒绝其行动或要求玩家重复移动。
15. 失败分类必须区分责任：工具名、参数、占位成功答案、遗漏状态写入或GM可依据现有信息自行修好的提案，使用gm_must_repair；只有玩家消息本身缺少一个无法由公开上下文和权威状态唯一确定的必要选择，或玩家需要亲自作出规则选择时，才使用needs_player_clarification。位置已由活动分支或全局位置账本确认时不属于玩家缺项。
17. perform_character_action只能提交玩家实际声明的攻击、装备、防御、技能、法术、装置或消耗物资行动。玩家声明撤离、穿过阻碍或前往另一地点时，不能为了满足action_type枚举而改写成Guard或其他战斗行动；应根据是否存在阻碍分别使用declare_movement_check、move_group_within_scene或move_scene_group。若当前工具无法表达玩家原意，判为gm_must_repair，并要求核心GM保留原意重选工具，绝不能替玩家选择另一项合法行动。
18. 玩家明确观察、触摸或研究眼前某个普通对象时，declare_check_action.base_observation可以由GM确认该对象存在，并补充开始检定前最低限度、立即可见的外观；这正是GM建立眼前局面的权限，不要求先调用commit_scene_response。眼前少量人物的准确人数、显眼制服与武器、物件的大致尺寸和颜色等无需专门能力即可直接确认的低不确定性事实，也属于base_observation，不应为了“再仔细数一遍”另设检定；检定只决定隐藏痕迹、动机、专业判断或真正存在风险的深层信息。玩家针对已公开为反复或即将发生的GM现场信号声明预备行动时，base_observation也可确认该信号此刻再次出现，并直接建立对应检定。base_observation不得借机确认隐藏性质、NPC反应、物件归属变化，亦不得与公开聊天或权威状态冲突。明显改变当前局面的新人物、怪物、出口、宝物或宏大设施仍须已有场景依据或专用场景工具，不能仅凭玩家错误前提生成。
19. 冲突中的玩家行动提案只负责表达玩家原意，最终由硬规则路由决定立即执行还是写入回合外收件箱。perform_character_action、perform_ritual_project_action等带timing字段的工具在timing=defer时明确只缓存；declare_check_action、declare_movement_check及其后续perform_check_action则会由规则层在发现actor并非current_actor时自动缓存。缓存不声称检定、攻击、法术、仪式或技能已经执行，也不消耗或替换current_actor的回合。因此，actor不是current_actor、actor本轮已经行动、当前行动者是NPC或NPC回合刚刚结束，都不能单独成为contradicts_state或gm_must_repair的理由。仍需检查玩家是否控制该actor、是否真的声明了该动作，以及目标、属性、武器、法术或技能是否有依据；这些内容不成立时照常拒绝。
20. create_world_setting新增historical_events、mysteries、world_threats、villain_seeds等列表类别时，只有value会成为持久事实，name不会另行保存。审计这些提案时必须以value为准：玩家原话中的具名主体、关键关系或危险不能只出现在name里；否则判为gm_must_repair，要求把完整事实写入value。

只输出一个JSON对象：
{"valid":true|false,"category":"grounded|unsupported_external_result|private_fact_disclosure|contradicts_state|false_premise|trait_rationale_unverified|npc_knowledge_unsupported|gm_must_repair|needs_player_clarification","repair_mode":"ordinary|npc_fact_or_nonclaim","unsupported_claims":["简短列出"],"correction_hint":"告诉核心GM应如何自行修正提案；仅needs_player_clarification可要求向玩家追问","player_agency_preserved":true|false,"authored_player_actions":["仅在required_audits.player_agency=true时逐项列出"]}
""".strip()


SILENCE_RESPONSIBILITY_SYSTEM_PROMPT = """
你是FU-GM的收尾职责复核器，不负责回答玩家、续写剧情或修改状态。核心GM准备结束当前请求，可能保持静默，也可能给出proposed_public_reply；你只根据current_message、recent_public_context、completed_tool_receipts和拟公开回复判断，玩家委托是否已经真正完成。

你判断的是“把proposed_public_reply现在发出后，本条玩家消息在本次请求内是否已经履行完毕”，不是“原消息通常是否值得GM回答”。正常跑团需要等待玩家下一句话、GM以后仍要继续主持、或第零章尚有其他议题，都不属于本次请求的未完成工作。

判定规则：
1. 玩家之间的分工、意见征询、角色内闲聊、玩笑、未执行提案，以及根据刚公开现象提出的解释性推测，如果没有要求NPC、环境、规则或主持人回应，应当保持静默。“要不要我试试”“我可以用某技能看看”“我们不如去问某人”仍只是等待队友确认的提议；“这不像单纯卡住，也许需要魔力或其他方式”即使说出了具体对象、技能、方法或使用疑问语气，也可能只是在与同桌分析局面。不得把GM有能力裁定这件事误当成玩家已经要求裁定。只有玩家明确表示角色现在开始观察、实验或行动，直接向NPC提问，明确要求主持人确认客观事实，或直接询问规则时才需要回应。
2. 玩家已经对NPC或环境采取行动、要求规则裁定、请求管理操作、直接向主持人提问，或正在回答开放的规则窗口，需要主持人处理。
3. 对最近公开聊天作记忆核对或桌面事实澄清，例如询问刚才是谁说过某事、有没有提过某项内容、某个代词指什么、某事是否已经发生，应当由主持人依据公开记录回答。即使问题写在角色动作或台词之后、没有点名主持人，只要没有明确向另一名PC或NPC发问，也属于table_fact_clarification。
4. 玩家问题中的前提不是事实。复核器只判断是否需要回应，不判断前提真伪，也不得建议借该问题公开后台秘密。
5. 如果语义存在合理歧义，结合传输关系、相邻问答与整句言语功能判断。非私聊、没有艾特或引用GM、也没有正式行动、NPC问话、规则请求、管理请求或客观事实求证时，解释性推测和方案比较优先视为player_discussion并保持静默；不要仅因话题涉及环境或句末有问号就要求GM回应。反之，明确询问现场人数、刚才谁说过某事、某条规则如何处理等客观问题，即使没有艾特，也不能被静默吞掉。
6. 玩家说“下一次波动出现时我立刻开锁”之类的话，若触发条件属于GM掌控的现场事件且已被公开为反复或即将发生，这是需要GM推进、裁定或明确回应的预备行动，不是玩家讨论；requires_gm_reply必须为true。只有纯粹约定另一名玩家未来配合、且当前没有向环境采取行动时，才可保持静默。
7. 玩家在本句中声称“某现象有固定节奏”不等于该规律已经成为公开事实；只能依据recent_public_context确认现场信号是否反复或即将发生。
8. “角色A对角色B说：下一次一起动，你负责X，我负责Y”通常是等待角色B确认的分工提议。即使句中描述了角色A未来负责的部分，只要它仍以另一名玩家配合为前提、没有另列一项无条件立即生效的行动，就应保持静默。
9. 与上项不同，“下一次波动出现时，我立刻抓住锁簧开门”是当前玩家对自己角色作出的完整预备行动，不依赖另一名玩家确认；若触发条件由GM掌控，应要求GM回应。
10. 结合core_proposed_semantics.delivery与has_independent_followup复核，但核心模型给出的audience、reason和semantic_targets只是待审提案，不是权威答案。transport_directly_addressed=false且transport_is_private=false时，说明平台没有观察到直接呼叫GM；仍可由语义证明存在隐含主持请求，但不得沿用核心模型“玩家在问GM”的理由作为证据。交付目标明确是另一名玩家，且本句只有已说出的玩家台词或等待同伴确认的讨论时，不得仅因GM有能力推进环境就把提议升级成行动。
11. completed_tool_receipts只证明其中明确列出的事项已经成功登记，不证明玩家原句中的其余请求也已完成。若一句话同时提供若干设定，并要求GM创作、补充、决定或回答另一项，而回执只覆盖了玩家提供的部分，requires_gm_reply必须为true；不能把“后续再处理”当作完成。
12. 玩家授权GM补充公开世界设定时，GM创作的内容仍应作为桌面可见提案说出来，供玩家确认或调整。仅在后台保存一笔提案而没有把内容展示给玩家，也不能静默结束。
13. 反过来，若回执已完整登记玩家自己公开说完的贡献、动作或选择，而且原句没有问题、GM委托、外部裁定或未完成事项，应保持静默，不能仅因发生状态写入就要求确认。
14. proposed_public_reply为空表示核心GM准备静默；非空表示它准备立刻把这段话发给玩家。必须审查回复是否真的履行了请求，不能因为“有回复”就判定完成。
15. “我先整理”“接下来给你补”“稍后再处理”“我先看看缺项”等未来承诺，不是可持续后台任务，也不等于已经完成。玩家委托GM创作时，拟回复必须给出具体可确认的内容，并有相应待确认提案回执；否则requires_gm_reply必须为true。
16. 玩家只是查询现状、缺项或已记录内容时，只读回执可以完成请求；前提是拟回复已经明确给出查询结果，而不是只说正在查询或准备整理。
17. 若recent_public_context最后一段是时悠的提问、拒绝或说明，而current_message在语义上回答、纠正、追问或反驳它，这仍是对主持人的直接后续，即使玩家没有重复艾特时悠也需要回应。第三方声称另一名玩家“同意了”不能作为修改对方角色卡的授权，但紧接权限拒绝时仍应由主持人解释需要本人明确授权，不能归为player_discussion后静默。
18. 玩家要求“开始第零章并先谈基调、安全边界和世界”时，成功start_session回执加上一段真正开启讨论、提出首个可回答问题的proposed_public_reply，已经完成本次开场请求；request_fulfilled应为true。不要因为其余议题需要后续玩家轮次、或GM之后还要继续主持，就要求核心GM在同一HTTP请求里无限续写。
19. 非空回复不自动代表完成；但回复已经回答问题、给出查询结果、解释拒绝理由，或完成本轮应有的开场与交棒时，也不能仅因category是direct_gm_request就判为未完成。
20. 玩家紧接一次时悠的静默处理，用“她”“你”“机器人”追问为何没回复、是否写入、是否卡住或是否仍在处理时，这是关于主持人处理状态的table_fact_clarification。必须结合recent_public_context判断代词指向；除非上下文明示另一名玩家或NPC，不得仅因没有艾特就归为player_discussion。若权威状态足以确认写入结果，应如实解释“已写入但按静默策略未逐句复述”；无法确认时也应回应并立即查询，不能继续静默。
21. 玩家在同一句中并列提出多个主持请求时，必须逐项核对是否完成。一个只读回执或一段公开回复只完成其中一项，不能代表其余请求也已处理；其余事项需要工具时应继续调用，需要必要参数时应明确询问，只有玩家确实需要在下一轮提供选择才算正常等待。
22. 第零章中，玩家回答GM刚才的邀请并完整说出一项贡献、角色选择或草稿增量后，这项回答已经公开；成功写入回执只是后台登记。若回执标明silent_commit_allowed=true且source_message_already_public=true，原句又没有独立问题、GM委托或必须当场裁定的外部结果，则空的proposed_public_reply已经完成本次请求，request_fulfilled=true。角色卡仍缺少的其他字段、下一位参与者尚未回答、以及主持人以后仍需推进第零章，都属于后续桌面轮次；不得强迫核心GM在同一个HTTP请求中立即追问下一项。真正的混合请求仍须逐项完成。

只输出一个JSON对象：
{"request_fulfilled":true|false,"category":"table_fact_clarification|rules_request|npc_or_world_interaction|management_request|direct_gm_request|delegated_gm_task|player_discussion|idle|external","reason":"一句简短语义依据"}
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


CHECK_ACTION_TOOL_PROPOSAL_GROUNDING_SYSTEM_PROMPT = """
你是FU-GM普通属性检定提案的语义审计器，只审查declare_check_action，不负责掷骰、
续写剧情或代替核心GM生成新提案。根据current_message、frozen_message_semantics、
recent_public_context、current_authoritative_state和arguments判断：玩家是否已经明确执行
该行动；actor、target、purpose和属性是否忠实；成功答案与失败后果是否只覆盖本次手段；
以及这件事是否真的需要检定。

检定只用于结果有实质不确定性，且成功或失败会改变局面的行动。以下事实应直接放进
base_observation或直接回答，不能用检定锁住：眼前少量人物的准确人数、公开递交并可正常
阅读的目录或摘要、显眼制服与武器、物件的大致尺寸颜色、已经敞开的通路。不得为了制造
不确定性，临时编造权威状态和公开上下文里没有的昏暗、字迹模糊、噪音、遮挡、人群过多、
内容受损或类似障碍。玩家要分析专业含义、复原确已建立的损坏或加密内容、寻找隐藏痕迹、
识破动机，或承担已建立的现场风险时，可以检定；这类不确定性可来自行动本身的专业分析，
不必凭空再加一个物理障碍。

success_observation和failure_consequence是条件结果，不代表现在已经发生；允许GM从既有局面
与私密准备中选定具体局部答案，但不得与公开事实冲突、替玩家行动或扩张到无关人物地点。
failure_consequence只在最终失败后公开。若普通可见事实与深层分析同时存在，前者写入
base_observation，后者仍可作为检定目标，此时obvious_answer_withheld=false。

failure_authority.kind=attempt时，失败只能否定check_label所表示的不确定部分，不能否定同一句中
无需检定便已完成的移动、说话、取出工具或站位。kind=local_consequence只允许当前手段直接引发、
且局限于操作对象附近的轻微反作用；若failure_consequence声称角色被击中、受伤、失去生命值或精神值、
获得异常状态、被迫移动、丢失装备，或改变命刻，就必须有专门规则工具与类型化效果，不能仅靠这段文字提交。
发现这类无权威机械后果时valid=false、category=gm_must_repair，并要求改为不改变角色状态的局部后果，
或调用真正能结算该危害的规则工具。

check_necessity_review必须使用以下字段：
- check_is_genuinely_uncertain：是否存在真正需要骰子裁定的深层问题或风险；
- uncertainty_source：只能是authoritative_state、public_context、intrinsic_professional_analysis、
  invented_obstacle或none；
- invented_obstacles：提案为了要求检定而新增、但没有依据的障碍；
- obvious_answer_withheld：是否把可直接确认的答案错误锁在成功结果后；
- withheld_obvious_answers：被错误扣住的具体答案；
- reason：一句依据。

只输出JSON：
{"valid":true|false,"category":"grounded|unsupported_external_result|contradicts_state|"
"false_premise|gm_must_repair|needs_player_clarification","repair_mode":"ordinary","
"unsupported_claims":[],"correction_hint":"","
"check_necessity_review":{"check_is_genuinely_uncertain":true,"
"uncertainty_source":"intrinsic_professional_analysis","invented_obstacles":[],"
"obvious_answer_withheld":false,"withheld_obvious_answers":[],"reason":""}}
""".strip()

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
某项required_audits.check_necessity=true时，还必须在该项结果中返回完整的
check_necessity_review；不得因为它与其他工具位于同一批次就省略。
check_is_genuinely_uncertain表示是否真的存在需要骰子裁定的深层问题或风险；
uncertainty_source只能是authoritative_state、public_context、
intrinsic_professional_analysis、invented_obstacle或none；同时返回invented_obstacles、
obvious_answer_withheld与withheld_obvious_answers。判断普通可见事实与深层不确定性时，
仍须把直接可见答案放进base_observation，不能靠编造昏暗、模糊、噪音、遮挡、人数过多
或内容受损来制造检定。

只输出一个JSON对象：
{"reviews":[{"proposal_index":0,"valid":true|false,"category":"grounded|unsupported_external_result|private_fact_disclosure|contradicts_state|false_premise|trait_rationale_unverified|npc_knowledge_unsupported|gm_must_repair|needs_player_clarification","repair_mode":"ordinary|npc_fact_or_nonclaim","unsupported_claims":["简短列出"],"correction_hint":"简短修正方向","player_agency_preserved":true|false,"authored_player_actions":["仅在该提案required_audits.player_agency=true时逐项列出"],"check_necessity_review":{"check_is_genuinely_uncertain":true,"uncertainty_source":"intrinsic_professional_analysis","invented_obstacles":[],"obvious_answer_withheld":false,"withheld_obvious_answers":[],"reason":"仅在required_audits.check_necessity=true时返回"}}]}
""".strip()
)


@dataclass(frozen=True)
class GMReplyGroundingReview:
    valid: bool
    category: str = "grounded"
    repair_mode: str = "ordinary"
    unsupported_claims: tuple[str, ...] = field(default_factory=tuple)
    correction_hint: str = ""
    request_fulfilled: bool | None = None


@dataclass(frozen=True)
class GMSilenceResponsibilityReview:
    requires_gm_reply: bool
    category: str = "player_discussion"
    reason: str = ""


def _tool_lifecycle(tool_name: str) -> dict[str, object]:
    """Tell the semantic reviewer what a tool commits, not just its name."""

    clean_name = str(tool_name or "").strip()
    if clean_name == "propose_session_zero_update":
        return {
            "writes_pending_proposal_only": True,
            "writes_formal_world_fact": False,
            "requires_prior_consensus": False,
            "preserves_unresolved_alternatives": True,
        }
    if clean_name == "move_scene_group":
        return {
            "commits_declared_unobstructed_movement": True,
            "moves_one_player_character_only": True,
            "may_create_destination_scene_branch": True,
            "public_result_may_author_bounded_arrival_details": True,
            "public_result_must_match_exact_mover_set_semantically": True,
            "does_not_accept_arbitrary_public_fact_writes": True,
            "carried_story_items_come_from_authoritative_ledger": True,
            "independent_immediate_followup_requires_continuation": True,
        }
    if clean_name == "transition_scene":
        return {
            "commits_declared_scene_transition": True,
            "may_move_multiple_consenting_player_characters": True,
            "requires_literal_consent_for_other_players_characters": True,
            "creates_destination_scene_framework": True,
            "public_arrival_may_author_bounded_arrival_details": True,
        }
    if clean_name == "commit_scene_response":
        return {
            "python_revalidates_change_authority": True,
            "requires_signed_followup_or_system_beat": True,
            "authored_scene_opening_may_publish_one_bounded_perceptible_change": True,
            "authored_free_scene_beat_may_create_one_bounded_perceptible_change": True,
            "must_preserve_player_character_agency": True,
            "must_use_specialized_tools_for_rules_entities_and_transitions": True,
            "private_scene_prep_supports_consistency_not_automatic_disclosure": True,
        }
    return {}


def _required_tool_audits(
    *,
    tool_name: str,
    proposal_authority: dict[str, object] | None,
) -> dict[str, bool]:
    """Declare fail-closed semantic checks for authored system beats."""

    clean_name = str(tool_name or "").strip()
    if clean_name == "declare_check_action":
        return {"check_necessity": True}
    if clean_name != "commit_scene_response":
        return {}
    authority = dict(proposal_authority or {})
    if authority.get("system_gm_beat_request") is not True:
        return {}
    heartbeat_action = str(authority.get("heartbeat_action") or "").strip()
    if authority.get("heartbeat_require_material_change") is not True:
        return {}
    if heartbeat_action == "free_scene_beat":
        authored = authority.get("gm_authored_free_scene_beat") is True
    elif heartbeat_action == "scene_opening":
        authored = authority.get("gm_authored_scene_opening") is True
    else:
        authored = False
    return {"player_agency": True} if authored else {}


def _required_check_necessity_review(
    payload: dict[str, object],
    *,
    required: bool,
) -> GMReplyGroundingReview | None:
    if not required:
        return None
    review = payload.get("check_necessity_review")
    if not isinstance(review, dict):
        raise ValueError("检定提案审计缺少check_necessity_review对象。")
    genuinely_uncertain = review.get("check_is_genuinely_uncertain")
    obvious_withheld = review.get("obvious_answer_withheld")
    if not isinstance(genuinely_uncertain, bool):
        raise ValueError("检定必要性审计缺少check_is_genuinely_uncertain布尔值。")
    if not isinstance(obvious_withheld, bool):
        raise ValueError("检定必要性审计缺少obvious_answer_withheld布尔值。")
    uncertainty_source = str(review.get("uncertainty_source") or "").strip()
    allowed_sources = {
        "authoritative_state",
        "public_context",
        "intrinsic_professional_analysis",
        "invented_obstacle",
        "none",
    }
    if uncertainty_source not in allowed_sources:
        raise ValueError("检定必要性审计返回了未知uncertainty_source。")
    raw_obstacles = review.get("invented_obstacles")
    raw_withheld = review.get("withheld_obvious_answers")
    if not isinstance(raw_obstacles, list):
        raise ValueError("检定必要性审计缺少invented_obstacles数组。")
    if not isinstance(raw_withheld, list):
        raise ValueError("检定必要性审计缺少withheld_obvious_answers数组。")
    obstacles = tuple(
        str(item or "").strip()[:240]
        for item in raw_obstacles[:6]
        if str(item or "").strip()
    )
    withheld = tuple(
        str(item or "").strip()[:240]
        for item in raw_withheld[:6]
        if str(item or "").strip()
    )
    if (
        genuinely_uncertain
        and uncertainty_source not in {"invented_obstacle", "none"}
        and not obstacles
        and not obvious_withheld
        and not withheld
    ):
        return None
    claims = tuple(dict.fromkeys((*obstacles, *withheld)))
    return GMReplyGroundingReview(
        valid=False,
        category="gm_must_repair",
        repair_mode="ordinary",
        unsupported_claims=claims
        or ("这项行动没有需要骰子裁定的实质不确定性。",),
        correction_hint=(
            "直接公开无需专门能力即可确认的基础事实，不要创建检定窗口；"
            "若玩家还在分析隐藏痕迹、专业含义或已建立风险，只保留那个真正不确定的深层问题，"
            "并把显而易见的信息放入base_observation。"
        ),
    )


def _required_player_agency_review(
    payload: dict[str, object],
    *,
    required: bool,
) -> GMReplyGroundingReview | None:
    if not required:
        return None
    preserved = payload.get("player_agency_preserved")
    authored_actions = payload.get("authored_player_actions")
    if not isinstance(preserved, bool):
        raise ValueError("工具提案审计缺少布尔字段player_agency_preserved。")
    if not isinstance(authored_actions, list):
        raise ValueError("工具提案审计缺少authored_player_actions数组。")
    actions = tuple(
        str(item or "").strip()[:240]
        for item in authored_actions[:6]
        if str(item or "").strip()
    )
    if preserved and not actions:
        return None
    return GMReplyGroundingReview(
        valid=False,
        category="gm_must_repair",
        repair_mode="ordinary",
        unsupported_claims=actions
        or ("主动节拍替玩家角色完成了未由玩家声明的行动。",),
        correction_hint=(
            "重写场景回应，只描述环境、物件或合法NPC此刻发生的新变化；"
            "把玩家角色接下来如何查看、移动、取物、说话或决定留给玩家。"
        ),
    )


def _normalized_repair_mode(
    *,
    category: str,
    requested_mode: object,
) -> str:
    """Map a semantic review category to its executable repair protocol."""

    clean_category = str(category or "").strip()
    clean_mode = str(requested_mode or "ordinary").strip()
    if clean_category == "npc_knowledge_unsupported":
        return "npc_fact_or_nonclaim"
    if clean_mode == "npc_fact_or_nonclaim":
        return clean_mode
    return "ordinary"


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
        request_fulfilled = payload.get("request_fulfilled")
        if not isinstance(request_fulfilled, bool):
            request_fulfilled = None
        category = str(payload.get("category") or "grounded").strip()
        return GMReplyGroundingReview(
            valid=bool(payload["valid"]),
            category=category,
            repair_mode=_normalized_repair_mode(
                category=category,
                requested_mode=payload.get("repair_mode"),
            ),
            unsupported_claims=tuple(
                str(item or "").strip()[:240]
                for item in claims[:6]
                if str(item or "").strip()
            ),
            correction_hint=str(payload.get("correction_hint") or "").strip()[:500],
            request_fulfilled=request_fulfilled,
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
        completed_receipts: list[GMToolReceipt] | None = None,
        proposed_public_reply: str = "",
    ) -> GMSilenceResponsibilityReview:
        """复核一次工具调用后的拟收尾是否漏掉主持职责。"""

        request = {
            "review_question": (
                "发布proposed_public_reply之后，current_message在本次请求内是否已经履行完毕？"
                "正常等待玩家下一轮回答不算未完成。"
            ),
            "current_message": str(current_message or "").strip(),
            "recent_public_context": str(recent_context or "").strip(),
            "session_phase": str(gate_status or "").strip(),
            "proposed_public_reply": str(proposed_public_reply or "").strip(),
            "core_proposed_semantics": {
                "message_kind": str(proposed_message_kind or "").strip(),
                "audience": str(proposed_audience or "").strip(),
                "reason": str(decision_reason or "").strip(),
                "has_independent_followup": bool(has_independent_followup),
                "delivery": json_safe_value(proposed_delivery or {}),
            },
            "completed_tool_receipts": [
                {
                    "tool_name": receipt.tool_name,
                    "state_changed": bool(receipt.state_changed),
                    "message": str(receipt.message or "").strip()[:240],
                    "public_fallback_reply": str(
                        receipt.public_fallback_reply or ""
                    ).strip()[:240],
                    "result": {
                        key: json_safe_value(receipt.result.get(key))
                        for key in (
                            "operation",
                            "category",
                            "name",
                            "value",
                            "summary",
                            "proposal_id",
                            "authority",
                            "visibility",
                            "required_followup_tools",
                            "silent_commit_allowed",
                            "source_message_already_public",
                            "completion_scope",
                            "changed_fields",
                            "player_name",
                            "hero_name",
                        )
                        if key in receipt.result
                    },
                }
                for receipt in list(completed_receipts or [])[-12:]
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
        request_fulfilled = payload.get("request_fulfilled")
        legacy_requires_reply = payload.get("requires_gm_reply")
        if isinstance(request_fulfilled, bool):
            requires_gm_reply = not request_fulfilled
        elif isinstance(legacy_requires_reply, bool):
            # Compatibility for deterministic test clients and queued replies
            # created before the request_fulfilled protocol was introduced.
            requires_gm_reply = legacy_requires_reply
        else:
            raise ValueError("静默职责复核缺少布尔字段request_fulfilled。")
        return GMSilenceResponsibilityReview(
            requires_gm_reply=requires_gm_reply,
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
        frozen_message_semantics: dict[str, object] | None = None,
        proposal_authority: dict[str, object] | None = None,
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

        required_audits = _required_tool_audits(
            tool_name=tool_name,
            proposal_authority=proposal_authority,
        )
        request = {
            "current_message": str(current_message or "").strip(),
            "frozen_message_semantics": dict(frozen_message_semantics or {}),
            "recent_public_context": str(recent_context or "").strip(),
            "current_authoritative_state": observed_state,
            "proposal_authority": dict(proposal_authority or {}),
            "proposed_tool": {
                "tool_name": str(tool_name or "").strip(),
                "arguments": arguments,
                "lifecycle": _tool_lifecycle(tool_name),
                "required_audits": required_audits,
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
        check_necessity_required = (
            required_audits.get("check_necessity") is True
        )
        raw = self.client.create_chat_completion(
            model=self.model,
            messages=build_cache_friendly_messages(
                static_system_prompt=(
                    CHECK_ACTION_TOOL_PROPOSAL_GROUNDING_SYSTEM_PROMPT
                    if check_necessity_required
                    else TOOL_PROPOSAL_GROUNDING_SYSTEM_PROMPT
                ),
                user_content=request_json,
                cache_family=(
                    "ground-check-action"
                    if check_necessity_required
                    else "ground-tool"
                ),
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
        check_review = _required_check_necessity_review(
            payload,
            required=check_necessity_required,
        )
        if check_review is not None:
            return check_review
        agency_review = _required_player_agency_review(
            payload,
            required=required_audits.get("player_agency") is True,
        )
        if agency_review is not None:
            return agency_review
        claims = payload.get("unsupported_claims")
        if not isinstance(claims, list):
            claims = []
        category = str(payload.get("category") or "grounded").strip()
        return GMReplyGroundingReview(
            valid=bool(payload["valid"]),
            category=category,
            repair_mode=_normalized_repair_mode(
                category=category,
                requested_mode=payload.get("repair_mode"),
            ),
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
        frozen_message_semantics: dict[str, object] | None = None,
        proposal_authority: dict[str, object] | None = None,
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
                "lifecycle": _tool_lifecycle(
                    str(proposal.get("tool_name") or "")
                ),
                "required_audits": _required_tool_audits(
                    tool_name=str(proposal.get("tool_name") or ""),
                    proposal_authority=proposal_authority,
                ),
            }
            for index, proposal in enumerate(proposals)
            if isinstance(proposal, dict)
        ]
        if not clean_proposals:
            return ()
        request = {
            "frozen_message_semantics": dict(frozen_message_semantics or {}),
            "recent_public_context": str(recent_context or "").strip(),
            "current_authoritative_state": observed_state,
            "proposal_authority": dict(proposal_authority or {}),
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
            required_audits = clean_proposals[index].get("required_audits")
            check_review = _required_check_necessity_review(
                item,
                required=(
                    isinstance(required_audits, dict)
                    and required_audits.get("check_necessity") is True
                ),
            )
            if check_review is not None:
                reviews_by_index[index] = check_review
                continue
            agency_review = _required_player_agency_review(
                item,
                required=(
                    isinstance(required_audits, dict)
                    and required_audits.get("player_agency") is True
                ),
            )
            if agency_review is not None:
                reviews_by_index[index] = agency_review
                continue
            claims = item.get("unsupported_claims")
            if not isinstance(claims, list):
                claims = []
            category = str(item.get("category") or "grounded").strip()
            reviews_by_index[index] = GMReplyGroundingReview(
                valid=bool(item["valid"]),
                category=category,
                repair_mode=_normalized_repair_mode(
                    category=category,
                    requested_mode=item.get("repair_mode"),
                ),
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
