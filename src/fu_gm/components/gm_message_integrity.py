from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Optional, Sequence

from fu_gm.components.world_setting_catalog import WorldSettingCatalog
from fu_gm.safety_parser import clean_safety_item, extract_safety_declarations


@dataclass(frozen=True)
class GMSafetyDeclarationRequirement:
    """One concrete line or veil, not merely the declaration kind."""

    kind: str
    content: str


@dataclass(frozen=True)
class GMHeroSkillOptionRequirement:
    """One explicit, player-authored option attached to a learned skill."""

    skill_name: str
    choice: str


@dataclass(frozen=True)
class GMProposalConfirmationRequirement:
    """One pending proposal that an explicit table approval must resolve."""

    subject: str
    proposal_ids: tuple[str, ...] = ()
    replacement_required: bool = False
    ambiguous: bool = False
    clause: str = ""


@dataclass(frozen=True)
class GMMessageIntegrityPlan:
    """High-confidence obligations extracted from one player message.

    This is intentionally not a general natural-language task planner.  It
    only records a small set of writes whose omission is more dangerous than
    asking the model to retry: Session Zero world contributions, explicit
    safety boundaries, hero fields, and ritual skill options. Natural-language
    intent such as whether a player is confirming a sheet belongs to the model,
    not this structural validator.
    """

    source_event_id: str = ""
    strict_source_event: bool = False
    prior_source_event_ids: tuple[str, ...] = ()
    speaker: str = ""
    world_categories: tuple[str, ...] = ()
    safety_declarations: tuple[GMSafetyDeclarationRequirement, ...] = ()
    hero_attributes_explicit: bool = False
    hero_fields: tuple[str, ...] = ()
    hero_skill_options: tuple[GMHeroSkillOptionRequirement, ...] = ()
    proposal: bool = False
    proposal_subjects: tuple[str, ...] = ()
    proposal_confirmations: tuple[GMProposalConfirmationRequirement, ...] = ()
    skipped: bool = False
    proposed_world_categories: tuple[str, ...] = ()
    skipped_world_categories: tuple[str, ...] = ()
    deferred_world_categories: tuple[str, ...] = ()
    skipped_session_zero_topics: tuple[str, ...] = ()
    deferred_session_zero_topics: tuple[str, ...] = ()

    @property
    def safety_kinds(self) -> tuple[str, ...]:
        """Compatibility view for routing; terminal checks use full pairs."""

        return tuple(item.kind for item in self.safety_declarations)

    @property
    def empty(self) -> bool:
        return not (
            self.world_categories
            or self.skipped_world_categories
            or self.deferred_world_categories
            or self.skipped_session_zero_topics
            or self.deferred_session_zero_topics
            or self.safety_declarations
            or self.hero_fields
            or self.hero_skill_options
            or self.proposal_subjects
            or self.proposal_confirmation_subjects
        )

    @property
    def proposal_persistence_required(self) -> bool:
        return self.proposal and bool(self.proposal_subjects)

    @property
    def proposal_confirmation_required(self) -> bool:
        return bool(self.proposal_confirmations)

    @property
    def proposal_confirmation_subjects(self) -> tuple[str, ...]:
        return tuple(item.subject for item in self.proposal_confirmations)


@dataclass(frozen=True)
class GMMessageIntegrityIssue:
    """A retryable, non-public failure returned to the core tool loop."""

    error_code: str
    message: str
    correction_hint: str
    missing: tuple[str, ...] = ()
    details: Mapping[str, object] = field(default_factory=dict)
    required_repair_tools: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "error_code": self.error_code,
            "message": self.message,
            "correction_hint": self.correction_hint,
            "missing": list(self.missing),
            "details": dict(self.details),
            "required_repair_tools": list(self.required_repair_tools),
            "retryable": True,
        }

    def protocol_error(self) -> dict[str, object]:
        return {"protocol_error": self.to_dict()}


class GMMessageIntegrityValidator:
    """Extract and verify a narrow set of message-level write obligations.

    ``plan`` runs once for the source message. ``validate_decision`` runs
    before executing a model decision, so an option cannot corrupt base hero
    attributes. ``validate_terminal`` runs before final/silent/ask_user and
    compares the plan with all receipts accumulated by the current outer
    transaction.
    """

    SESSION_ZERO_GATES = frozenset({"pre_session", "session_zero"})
    WORLD_TOOL_NAMES = frozenset(
        {
            "create_world_setting",
            "update_world_setting",
            "delete_world_setting",
            "rename_world_setting",
        }
    )
    WORLD_CONTRIBUTION_OPERATIONS = frozenset({"create", "update", "rename"})
    WORLD_PLAYER_AUTHORITIES = frozenset(
        {"player_confirmed", "table_consensus", "retcon"}
    )
    WORLD_CATEGORY_COVERAGE = {
        # Session Zero accepts a country or another political community. A
        # village commune, tribe, league, or order may therefore be stored as
        # a faction rather than being forced into the kingdom taxonomy.
        "kingdoms": frozenset({"kingdoms", "factions"}),
        "historical_events": frozenset({"historical_events"}),
        "mysteries": frozenset({"mysteries"}),
        "world_threats": frozenset({"world_threats"}),
        "playstyle_themes": frozenset(
            {"playstyle_themes", "consensus_notes"}
        ),
        "tone_preferences": frozenset(
            {"tone_preferences", "consensus_notes"}
        ),
        "magic_tech_role": frozenset({"magic_tech_role"}),
        "world_shape": frozenset({"world_shape"}),
    }
    WORLD_CATEGORY_LABELS = {
        "kingdoms": "国家或政治共同体",
        "historical_events": "重大历史事件",
        "mysteries": "世界奥秘",
        "world_threats": "世界威胁",
        "playstyle_themes": "玩法偏好",
        "tone_preferences": "叙事基调与开局节奏",
        "magic_tech_role": "魔法与科技的关系",
        "world_shape": "世界或大陆形态",
    }
    WORLD_CATEGORY_WRITE_HINTS = {
        "kingdoms": (
            "国家或具名政治共同体：调用 create_world_setting，category 选 "
            "kingdoms 或 factions，name 必须填写玩家原话中的共同体专名，"
            "value 写完整描述；地点记录不能代替这一项"
        ),
        "historical_events": (
            "重大历史事件：调用 create_world_setting，category=historical_events，"
            "省略 name，把完整事件写入 value；地点描述中顺带提及不算独立登记"
        ),
        "mysteries": (
            "世界奥秘：调用 create_world_setting，category=mysteries，省略 name，"
            "把完整疑问写入 value"
        ),
        "world_threats": (
            "世界威胁：调用 create_world_setting，category=world_threats，省略 name，"
            "把威胁主体、意图或后果写入 value"
        ),
    }
    WORLD_SKIP_TOPIC_CODES = {
        "kingdoms": "kingdom",
        "historical_events": "historical_event",
        "mysteries": "mystery",
        "world_threats": "threat",
    }
    SAFETY_KIND_LABELS = {"line": "界限", "veil": "帷幕"}
    PROPOSAL_FORMAL_WRITE_TOOLS = WORLD_TOOL_NAMES | frozenset(
        {"confirm_session_zero_proposal", "commit_session_zero_update"}
    )
    PROPOSAL_CONFIRMATION_CATEGORY_COVERAGE = {
        "world_map": frozenset(
            {
                "continent_name",
                "world_shape",
                "map_locations",
                "major_locations",
            }
        ),
        "group_concept": frozenset({"group_concept"}),
        "kingdoms": frozenset({"kingdoms"}),
        "factions": frozenset({"factions"}),
        "historical_events": frozenset({"historical_events"}),
        "mysteries": frozenset({"mysteries"}),
        "world_threats": frozenset({"world_threats"}),
        "playstyle_themes": frozenset(
            {"playstyle_themes", "consensus_notes"}
        ),
    }

    @classmethod
    def proposal_subject_coverage(cls, subject: object) -> frozenset[str]:
        """Return the world categories represented by one proposal subject.

        A few legacy subjects intentionally group several setting categories
        (for example ``world_map``). Message semantics may also use any exact
        world CRUD category. Supporting both forms prevents less common
        categories such as ``custom_world_settings`` from falling out of the
        proposal lifecycle.
        """

        clean_subject = cls._clean(subject)
        grouped = cls.PROPOSAL_CONFIRMATION_CATEGORY_COVERAGE.get(clean_subject)
        if grouped is not None:
            return grouped
        if clean_subject in WorldSettingCatalog.CATEGORIES:
            return frozenset({clean_subject})
        return frozenset()

    _HIGH_CONFIDENCE_SAFETY_RE = re.compile(
        r"(?:安全边界|界限|帷幕|面纱|雷点|有雷|"
        r"创伤触发|触发内容|淡出处理|放到幕后|放在幕后)|"
        r"(?:游戏|故事|剧情)(?:中|里).{0,24}(?:不要|别|禁止).{0,12}"
        r"(?:出现|包含|涉及|描写|描述)|"
        r"(?:不要|别|禁止).{0,12}(?:在)?(?:游戏|故事|剧情)(?:中|里)"
    )

    _PROPOSAL_RE = re.compile(
        r"(?:我(?:先)?(?:提议|建议|提个)|还没定|先丢.{0,12}想法).{0,40}|"
        r"(?:大家|你们).{0,18}(?:觉得|怎么看|什么看法|是否同意|"
        r"同不同意|赞不赞成)|"
        r"(?:这样|这么|这个).{0,6}(?:行不行|可以吗|合适吗)|要不要"
    )
    _COMMIT_RE = re.compile(
        r"(?:就这样定|就按这个|确认(?:采用|通过)|正式(?:记下|记录|定下)|"
        r"请.{0,8}(?:记下|记录|记进)|确定(?:为|采用))"
    )
    _PROPOSAL_SUBJECT_PATTERNS = (
        (
            "world_map",
            re.compile(
                r"(?:地图|大陆|地形|山脉|内海|海岸|群岛|地区|地点|城市|村落)"
            ),
        ),
        (
            "group_concept",
            re.compile(
                r"(?:小队|队伍|团队|团体|共同身份|共同目标|小队方向|守护者)"
            ),
        ),
        (
            "kingdoms",
            re.compile(r"(?:国家|王国|帝国|公国|城邦|政治共同体)"),
        ),
        (
            "factions",
            re.compile(r"(?:组织|团体|教团|商会|军团|情报组织|非政体势力)"),
        ),
        (
            "historical_events",
            re.compile(r"(?:重大历史|历史事件|历史)"),
        ),
        (
            "mysteries",
            re.compile(r"(?:奥秘|谜团|未解之谜)"),
        ),
        (
            "world_threats",
            re.compile(r"(?:世界性?威胁|威胁|危机|灾祸|反派)"),
        ),
        (
            "playstyle_themes",
            re.compile(r"(?:玩法|游玩|游戏).{0,4}(?:偏好|风格|主题)"),
        ),
    )
    _PROPOSAL_CONFIRM_RE = re.compile(
        r"(?:我|我们)(?:明确)?(?:赞成|同意|支持|确认采用|确认通过)|"
        r"(?:大家|你们)(?:已经|都|一致|明确)?(?:赞成|同意|支持|确认通过)|"
        r"(?:就|便)?按.{0,18}(?:来|定|执行)|就这样定"
    )
    _PROPOSAL_REVISION_RE = re.compile(
        r"(?:改成|改为|换成|替换为|细化|调整为|重做|重新|"
        r"[:：].{0,100}(?:叫|放|设|不用|不要|而是))"
    )
    _EXPLICIT_KINGDOM_PROPOSAL_RE = re.compile(
        r"(?:提议|建议|想法).{0,24}(?:新增|建立|设定|加入)?"
        r"(?:国家|王国|帝国|公国|城邦|势力)|"
        r"(?:国家|王国|帝国|公国|城邦|势力).{0,12}"
        r"(?:叫|名为|设为|设定为|要不要|怎么样|行不行)"
    )
    _SKIP_RE = re.compile(
        r"(?:我|这项|这个|当前)?(?:先|暂时|暂且)?"
        r"(?:跳过|不贡献|不回答|先不填|没有想法|想不到)"
    )
    _NEGATED_SKIP_RE = re.compile(r"(?:不要|不能|别|不可).{0,3}跳过")
    _WORLD_COMMIT_RE = re.compile(
        r"(?:我的(?:国家|王国|地区|地点|城市|村落|村社|森林|群岛)|"
        r"我(?:来|要|想)?(?:贡献|补充|补|设定)|"
        r"(?:国家|王国|地区|地点|历史事件|重大历史|奥秘|威胁|玩法偏好)\s*[:：]|"
        r"正式(?:记下|记录|记进)|请.{0,8}(?:记下|记录|记进)|"
        r"我希望.{0,24}(?:第一章|本团|这场|游戏))"
    )
    _KINGDOM_RE = re.compile(
        r"(?:我的(?:国家|王国|地区|地点|城市|村落|村社|森林|群岛)|"
        r"(?:贡献|补充|补|设定).{0,28}"
        r"(?:国家|王国|帝国|公国|城邦|地区|地点|城市|村落|村社|森林|群岛)|"
        r"(?:国家|王国|帝国|公国|城邦|地区|地点)\s*[:：])"
    )
    _EXPLICIT_HISTORY_RE = re.compile(r"(?:重大历史(?:事件)?|历史事件|历史上)")
    _PAST_MARKER_RE = re.compile(
        r"(?:[一二三四五六七八九十百千\d]+年(?:前|后)|"
        r"(?:之夜|战争|灾难|灾变|事变|政变|病倒|失踪|灭亡|陨落|坠落)(?:后|时|当夜)?|"
        r"第一次)"
    )
    _PAST_EVENT_RE = re.compile(
        r"(?:病倒|抵押|拒绝|爆发|毁灭|摧毁|消失|失踪|坠落|陨落|覆灭|"
        r"灭亡|死亡|建立|签订|背叛|夺取|封印|苏醒|战争|灾难|灾变|革命|政变)"
    )
    _MYSTERY_RE = re.compile(r"(?:奥秘|谜团|未解之谜|为什么|为何)")
    _EXPLICIT_THREAT_RE = re.compile(r"(?:世界性?威胁|威胁|危机|灾祸|反派种子)")
    _CONDITIONAL_THREAT_RE = re.compile(
        r"(?:若|如果|一旦).{1,60}(?:就会|将会|便会|会).{0,40}"
        r"(?:夺|拿走|毁|杀|吞|侵|污染|奴役|献祭|爆发|沦为|失去|崩溃)"
    )
    _HOSTILE_PLAN_RE = re.compile(
        r"(?:想|试图|企图|计划|准备).{0,12}(?:把|让).{1,40}"
        r"(?:变成|化为|摧毁|占领|侵占|污染|奴役|献祭|夺走)"
    )
    _PLAYSTYLE_RE = re.compile(
        r"(?:玩法偏好|游戏偏好|游玩偏好|"
        r"我希望.{0,80}(?:不靠战斗|非战斗|证据|承诺|情感|调查|谈判))"
    )
    _TONE_PREFERENCE_RE = re.compile(
        r"(?:基调|氛围|叙事风格|整体风格)\s*[:：]|"
        r"我希望.{0,90}(?:希望感|史诗|轻松|明亮|黑暗|压抑|"
        r"从.{0,18}(?:小事|边境)|真相.{0,18}(?:中期|后期))"
    )
    _MAGIC_TECH_RE = re.compile(
        r"(?:魔法|御魂术|元素仪式).{0,80}(?:科技|机器|车辆|工坊|晶炉)|"
        r"(?:科技|机器|车辆|工坊|晶炉).{0,80}(?:魔法|御魂术|元素仪式)"
    )
    _WORLD_SHAPE_RE = re.compile(
        r"(?:世界|大陆)(?:的)?(?:形状|形态|轮廓)\s*[:：]|"
        r"(?:它|这个世界|这片大陆|大陆).{0,8}(?:就是|是|采用|按).{0,24}"
        r"(?:类地球|环形|球形|平面|碎裂|异形)|"
        r"(?:普通的?)?类地球(?:世界|大陆)|"
        r"(?:不用|不要|不是|并非|非).{0,8}异形世界"
    )
    _SKILL_OPTION_RE = re.compile(
        r"(?:^|[，。；;：:\s])(?:我为|我的|关于)?"
        r"(?P<skill>[\u4e00-\u9fffA-Za-z0-9·]{1,16}?(?:系仪式|仪式|咒法))"
        r"(?:的)?(?:施法)?属性(?:组合)?(?:我)?(?:选择|选|定为|用|是)\s*"
        r"[【\[]?(?P<first>洞察|力量)\s*[+＋与和]\s*(?P<second>意志)[】\]]?"
    )
    _HERO_ATTRIBUTES_RE = re.compile(
        r"(?:基础)?属性(?:骰|分配)?\s*[:：]|"
        r"(?:敏捷|洞察|力量|意志)\s*(?:d|D)?(?:6|8|10|12).{0,60}"
        r"(?:敏捷|洞察|力量|意志)\s*(?:d|D)?(?:6|8|10|12)|"
        r"(?:敏捷|洞察|力量|意志)(?:属性)?\s*"
        r"(?:改成|改为|设为|调整为|定为|是)\s*(?:d|D)?(?:6|8|10|12)"
    )
    _HERO_FIELD_PATTERNS = (
        (
            "hero_name",
            re.compile(r"(?:角色名|角色名字)\s*(?::|：|是|叫|为)?\s*[\u4e00-\u9fffA-Za-z]"),
        ),
        ("identity", re.compile(r"身份\s*[:：]")),
        ("theme", re.compile(r"主题\s*[:：]")),
        ("origin", re.compile(r"(?:故乡|出身|来历)\s*[:：]")),
        ("classes", re.compile(r"(?:职业分配|职业等级|等级分配)\s*[:：]")),
        ("attributes", _HERO_ATTRIBUTES_RE),
        (
            "skills",
            re.compile(
                r"(?:第[一二三四五六七八九十\d]+项技能|技能(?:选择)?)"
                r".{0,20}(?:选|选择|定为)"
            ),
        ),
        (
            "spells",
            re.compile(r"(?:法术选择|法术\s*[:：]|魔法选择\s*[:：]?)"),
        ),
        (
            "bound_arcana",
            re.compile(
                r"(?:与|和).{0,18}(?:奥灵|魔典|奥术实体).{0,12}"
                r"(?:缔结|签订|建立).{0,6}(?:起始)?契约|"
                r"(?:起始)?契约\s*[:：]"
            ),
        ),
        (
            "skill_options",
            re.compile(r"(?:便携装置|仪式的施法属性).{0,16}(?:选择|选|定为)"),
        ),
        ("equipment", re.compile(r"(?:初始|起始)?装备\s*(?:[:：]|是|选择)")),
        ("bonds", re.compile(r"羁绊\s*[:：]")),
        ("notes", re.compile(r"(?:背景钩子|人物钩子|角色背景)\s*[:：]")),
    )

    @classmethod
    def plan(
        cls,
        message: str,
        *,
        gate_status: str = "",
        source_event_id: str = "",
        strict_source_event: bool = False,
        prior_source_event_ids: Sequence[str] = (),
        speaker: str = "",
        state_summary: Mapping[str, object] | None = None,
    ) -> GMMessageIntegrityPlan:
        text = cls._clean(message)
        clean_event_id = cls._clean(source_event_id)
        clean_speaker = cls._clean(speaker)
        if not text:
            return GMMessageIntegrityPlan(
                source_event_id=clean_event_id,
                strict_source_event=bool(strict_source_event),
                prior_source_event_ids=tuple(
                    cls._clean(item) for item in prior_source_event_ids
                ),
                speaker=clean_speaker,
            )

        skipped = bool(cls._SKIP_RE.search(text)) and not bool(
            cls._NEGATED_SKIP_RE.search(text)
        )
        session_zero = cls._clean(gate_status) in cls.SESSION_ZERO_GATES
        proposal_subjects, confirmation_clauses = cls._proposal_clause_modes(
            text,
            session_zero=session_zero,
        )
        proposal = bool(proposal_subjects)

        # The shared parser intentionally accepts colloquial declarations.
        # A fail-closed completeness gate needs a narrower input, otherwise
        # ordinary comments such as “这个设定让人不舒服，我喜欢” can be cut at
        # “不舒服” and promoted to a permanent safety line.  Only explicit
        # labels or explicit story-content restrictions become hard transaction
        # obligations; ambiguous natural language remains with semantic routing.
        safety_text = cls._safety_obligation_text(text)
        safety_declarations = tuple(
            GMSafetyDeclarationRequirement(kind, content)
            for kind, content in (
                extract_safety_declarations(safety_text) if safety_text else []
            )
        )

        world_categories: list[str] = []
        world_commit = bool(cls._WORLD_COMMIT_RE.search(text))
        proposed_world_categories = cls._deferred_world_categories(
            text,
            mode="proposal",
        )
        skipped_world_categories = cls._deferred_world_categories(
            text,
            mode="skip",
        )
        deferred_world_categories = (
            proposed_world_categories | skipped_world_categories
        )
        if session_zero and world_commit:
            if cls._KINGDOM_RE.search(text):
                world_categories.append("kingdoms")
            if cls._EXPLICIT_HISTORY_RE.search(text) or (
                cls._PAST_MARKER_RE.search(text)
                and cls._PAST_EVENT_RE.search(text)
            ):
                world_categories.append("historical_events")
            if cls._MYSTERY_RE.search(text):
                world_categories.append("mysteries")
            if (
                cls._EXPLICIT_THREAT_RE.search(text)
                or cls._CONDITIONAL_THREAT_RE.search(text)
                or cls._HOSTILE_PLAN_RE.search(text)
            ):
                world_categories.append("world_threats")
            if cls._PLAYSTYLE_RE.search(text):
                world_categories.append("playstyle_themes")
            world_categories = [
                category
                for category in world_categories
                if category not in deferred_world_categories
            ]
        if session_zero and not proposal:
            if cls._TONE_PREFERENCE_RE.search(text):
                world_categories.append("tone_preferences")
            if cls._MAGIC_TECH_RE.search(text):
                world_categories.append("magic_tech_role")
            world_categories.extend(cls.explicit_player_world_categories(text))
        world_categories = list(dict.fromkeys(world_categories))

        pending = cls._pending_proposals(state_summary)
        confirmation_subjects = {
            subject for subject, _clause in confirmation_clauses
        }
        single_subject_message_revision = bool(
            len(confirmation_subjects) == 1
            and cls._PROPOSAL_REVISION_RE.search(text)
        )
        proposal_confirmations: list[GMProposalConfirmationRequirement] = []
        for subject, clause in confirmation_clauses:
            candidates = tuple(
                item
                for item in pending
                if subject in cls._proposal_subjects_for_pending(item)
            )
            if (
                not candidates
                and cls._pending_proposals_state_is_authoritative(state_summary)
                and not (
                    strict_source_event
                    and any(
                        cls._clean(item) for item in prior_source_event_ids
                    )
                )
            ):
                # The current snapshot explicitly says there is nothing left
                # to confirm.  Do not turn a later conversational agreement
                # into a fresh write obligation.  A strict batched message is
                # the exception: an earlier event in the same debounce may
                # have created the proposal after this snapshot was captured.
                continue
            selected, ambiguous = cls._select_pending_proposals(
                candidates,
                clause=clause,
            )
            requirement = GMProposalConfirmationRequirement(
                subject=subject,
                proposal_ids=tuple(
                    cls._clean(item.get("id")) for item in selected
                ),
                replacement_required=bool(
                    cls._PROPOSAL_REVISION_RE.search(clause)
                    or single_subject_message_revision
                ),
                ambiguous=ambiguous,
                clause=clause,
            )
            if requirement not in proposal_confirmations:
                proposal_confirmations.append(requirement)

        skill_options: list[GMHeroSkillOptionRequirement] = []
        hero_fields = tuple(
            field_name
            for field_name, pattern in cls._HERO_FIELD_PATTERNS
            if session_zero and pattern.search(text)
        )
        if (
            session_zero
            and "skills" not in hero_fields
            and cls._implicit_hero_skill_selection(text, state_summary)
        ):
            hero_fields = (*hero_fields, "skills")
        if session_zero:
            for match in cls._SKILL_OPTION_RE.finditer(text):
                skill_name = cls._clean(match.group("skill"))
                choice = f"{match.group('first')}+{match.group('second')}"
                requirement = GMHeroSkillOptionRequirement(skill_name, choice)
                if requirement not in skill_options:
                    skill_options.append(requirement)
        if skill_options:
            # The exact option checker compares skill name and normalized
            # choice.  Do not let the generic changed-field check mask its more
            # actionable error code.
            hero_fields = tuple(
                field_name
                for field_name in hero_fields
                if field_name != "skill_options"
            )

        return GMMessageIntegrityPlan(
            source_event_id=clean_event_id,
            strict_source_event=bool(strict_source_event),
            prior_source_event_ids=tuple(
                cls._clean(item) for item in prior_source_event_ids
            ),
            speaker=clean_speaker,
            world_categories=tuple(world_categories),
            safety_declarations=safety_declarations,
            hero_attributes_explicit=bool(
                session_zero and cls._HERO_ATTRIBUTES_RE.search(text)
            ),
            hero_fields=hero_fields,
            hero_skill_options=tuple(skill_options),
            proposal=proposal,
            proposal_subjects=tuple(proposal_subjects),
            proposal_confirmations=tuple(proposal_confirmations),
            skipped=skipped,
            proposed_world_categories=tuple(sorted(proposed_world_categories)),
            skipped_world_categories=tuple(sorted(skipped_world_categories)),
        )

    @classmethod
    def explicit_player_world_categories(cls, message: str) -> tuple[str, ...]:
        """Return exact high-confidence public world fields in a declaration.

        This intentionally stays narrow.  It is also used by the Session Zero
        proposal confirmer when a player approves an existing proposal while
        adding a separate, personally-authored fact outside that proposal's
        consensus scope.
        """

        text = cls._clean(message)
        categories: list[str] = []
        if text and cls._WORLD_SHAPE_RE.search(text):
            categories.append("world_shape")
        return tuple(categories)

    @classmethod
    def validate_decision(
        cls,
        plan: GMMessageIntegrityPlan,
        decision: Mapping[str, object],
        receipts: Iterable[object] = (),
    ) -> Optional[GMMessageIntegrityIssue]:
        """Reject a ritual option written into base character attributes."""

        all_calls = cls._decision_calls(decision)
        calls = [
            call
            for call in all_calls
            if cls._call_matches_plan(plan, call)
        ]
        confirmation_requirements = cls._effective_proposal_confirmations(
            plan,
            receipts,
        )
        if plan.prior_source_event_ids and confirmation_requirements:
            prior_proposal_subjects: set[str] = set()
            for call in all_calls:
                if (
                    cls._clean(call.get("tool_name"))
                    != "propose_session_zero_update"
                ):
                    continue
                arguments = cls._call_arguments(call)
                if cls._clean(arguments.get("source_event_id")) not in set(
                    plan.prior_source_event_ids
                ):
                    continue
                prior_proposal_subjects.update(
                    cls._proposal_subjects_for_pending(
                        {
                            "summary": arguments.get("summary"),
                            "proposed_updates": arguments.get("updates"),
                            "world_operations": arguments.get(
                                "world_operations"
                            ),
                        }
                    )
                )
            blocked_subjects = sorted(
                prior_proposal_subjects
                & {item.subject for item in confirmation_requirements}
            )
            current_writes = [
                call
                for call in calls
                if cls._clean(call.get("tool_name"))
                == "confirm_session_zero_proposal"
                or bool(cls._formal_write_subjects(call))
            ]
            if blocked_subjects and current_writes:
                return GMMessageIntegrityIssue(
                    error_code=(
                        "SESSION_ZERO_PROPOSAL_CONFIRMATION_PENDING_CREATION"
                    ),
                    message=(
                        "同一桌面轮次中，更早玩家的待定提案尚未先生成权威ID。"
                    ),
                    correction_hint=(
                        "本轮先只执行更早事件的propose_session_zero_update；"
                        "取得成功回执中的proposal.id后，再用后续玩家事件确认该ID。"
                    ),
                    missing=tuple(blocked_subjects),
                    required_repair_tools=("propose_session_zero_update",),
                    details={
                        "prior_source_event_ids": list(
                            plan.prior_source_event_ids
                        ),
                        "source_event_id": plan.source_event_id,
                    },
                )
        if plan.proposal_persistence_required:
            absorbed_confirmations: list[dict[str, object]] = []
            for call in calls:
                if (
                    cls._clean(call.get("tool_name"))
                    != "confirm_session_zero_proposal"
                ):
                    continue
                arguments = cls._call_arguments(call)
                if not arguments.get("replacement_world_operations"):
                    continue
                proposal_id = cls._clean(arguments.get("proposal_id"))
                replacement_subjects = cls._confirmation_replacement_subjects(
                    call
                )
                collided = sorted(
                    set(plan.proposal_subjects) & replacement_subjects
                )
                if collided:
                    absorbed_confirmations.append(
                        {
                            "proposal_id": proposal_id,
                            "subjects": collided,
                        }
                    )
            if absorbed_confirmations:
                return GMMessageIntegrityIssue(
                    error_code="SESSION_ZERO_CONFIRMATION_ABSORBS_NEW_PROPOSAL",
                    message=(
                        "本句既确认旧提案又另提新方案，不能把新方案塞进旧提案的修订包。"
                    ),
                    correction_hint=(
                        "原样调用confirm_session_zero_proposal确认旧提案；再单独调用"
                        "propose_session_zero_update保存仍待讨论的新方案。两项都必须"
                        "留在当前消息事务内完成。"
                    ),
                    missing=(
                        "confirm_session_zero_proposal",
                        "propose_session_zero_update",
                    ),
                    required_repair_tools=(
                        "confirm_session_zero_proposal",
                        "propose_session_zero_update",
                    ),
                    details={
                        "new_proposal_subjects": sorted(
                            plan.proposal_subjects
                        ),
                        "absorbed_confirmations": absorbed_confirmations,
                        "source_event_id": plan.source_event_id,
                    },
                )
            formal_writes = sorted(
                cls._clean(call.get("tool_name"))
                for call in calls
                if cls._formal_write_subjects(call)
                & set(plan.proposal_subjects)
            )
            if formal_writes:
                return GMMessageIntegrityIssue(
                    error_code="SESSION_ZERO_PROPOSAL_MISCOMMITTED",
                    message=(
                        "玩家明确表示这仍是待讨论提案，不能提前写成已确认事实。"
                    ),
                    correction_hint=(
                        "不要执行当前正式写入；改用propose_session_zero_update"
                        "保存待定方案，等待其他玩家明确确认。"
                    ),
                    missing=("propose_session_zero_update",),
                    required_repair_tools=("propose_session_zero_update",),
                    details={
                        "proposal_subjects": list(plan.proposal_subjects),
                        "rejected_tools": formal_writes,
                        "source_event_id": plan.source_event_id,
                    },
                )
            confirmation_ids = {
                proposal_id
                for requirement in confirmation_requirements
                for proposal_id in requirement.proposal_ids
            }
            unrelated_confirms = sorted(
                {
                    cls._clean(cls._call_arguments(call).get("proposal_id"))
                    for call in calls
                    if cls._clean(call.get("tool_name"))
                    == "confirm_session_zero_proposal"
                    and cls._clean(cls._call_arguments(call).get("proposal_id"))
                    not in confirmation_ids
                }
            )
            if unrelated_confirms:
                return GMMessageIntegrityIssue(
                    error_code="SESSION_ZERO_PROPOSAL_MISCOMMITTED",
                    message=(
                        "玩家本句是在提出新方案，不能借此确认另一条旧提案。"
                    ),
                    correction_hint=(
                        "本轮只调用propose_session_zero_update保存新方案；"
                        "旧提案必须等待一条明确赞成它的玩家消息。"
                    ),
                    missing=("propose_session_zero_update",),
                    required_repair_tools=("propose_session_zero_update",),
                    details={
                        "submitted_proposal_ids": unrelated_confirms,
                        "source_event_id": plan.source_event_id,
                    },
                )

        ambiguous_requirement = next(
            (
                requirement
                for requirement in confirmation_requirements
                if requirement.ambiguous
            ),
            None,
        )
        if (
            ambiguous_requirement is not None
            and cls._clean(decision.get("decision")).lower() != "ask_user"
        ):
            return cls._ambiguous_proposal_issue(
                ambiguous_requirement,
                source_event_id=plan.source_event_id,
            )

        all_expected_confirmation_ids = {
            proposal_id
            for requirement in confirmation_requirements
            for proposal_id in requirement.proposal_ids
        }
        submitted_confirmation_ids = {
            cls._clean(cls._call_arguments(call).get("proposal_id"))
            for call in calls
            if cls._clean(call.get("tool_name"))
            == "confirm_session_zero_proposal"
            and cls._clean(cls._call_arguments(call).get("proposal_id"))
        }
        unknown_confirmation_ids = sorted(
            submitted_confirmation_ids - all_expected_confirmation_ids
        )
        if all_expected_confirmation_ids and unknown_confirmation_ids:
            return GMMessageIntegrityIssue(
                error_code="SESSION_ZERO_PROPOSAL_CONFIRMATION_MISMATCH",
                message="本轮确认工具指向了玩家没有赞成的待定提案。",
                correction_hint=(
                    "从当前state_summary.pending_proposals选择与每个确认范围相符的"
                    "proposal_id；不要跨地图、小队或其他主题关闭提案。"
                ),
                missing=tuple(sorted(all_expected_confirmation_ids)),
                required_repair_tools=("confirm_session_zero_proposal",),
                details={
                    "expected_proposal_ids": sorted(all_expected_confirmation_ids),
                    "submitted_proposal_ids": unknown_confirmation_ids,
                    "source_event_id": plan.source_event_id,
                },
            )

        for requirement in confirmation_requirements:
            # If no matching pending proposal exists, the statement is still
            # a valid table-consensus contribution and may use ordinary CRUD.
            # Once an id is visible, however, lifecycle closure is mandatory:
            # direct CRUD would leave the pending object behind.
            if not requirement.proposal_ids:
                continue
            matching_confirms = [
                call
                for call in calls
                if cls._clean(call.get("tool_name"))
                == "confirm_session_zero_proposal"
            ]
            direct_writes = sorted(
                cls._clean(call.get("tool_name"))
                for call in calls
                if cls._formal_write_subjects(call) == {requirement.subject}
                or requirement.subject in cls._formal_write_subjects(call)
            )
            if direct_writes:
                return GMMessageIntegrityIssue(
                    error_code="SESSION_ZERO_PROPOSAL_CONFIRMATION_MISCOMMITTED",
                    message=(
                        "玩家正在确认一条现存待定提案，不能绕过提案生命周期直接写正式字段。"
                    ),
                    correction_hint=(
                        "先调用confirm_session_zero_proposal关闭对应ID；若玩家在赞成时"
                        "修改了内容，把完整新方案放进replacement_world_operations。"
                    ),
                    missing=("confirm_session_zero_proposal",),
                    required_repair_tools=("confirm_session_zero_proposal",),
                    details={
                        "proposal_subject": requirement.subject,
                        "proposal_ids": list(requirement.proposal_ids),
                        "rejected_tools": direct_writes,
                        "source_event_id": plan.source_event_id,
                    },
                )
            if requirement.replacement_required:
                incomplete = [
                    call
                    for call in matching_confirms
                    if cls._clean(cls._call_arguments(call).get("proposal_id"))
                    in requirement.proposal_ids
                    and not cls._call_arguments(call).get(
                        "replacement_world_operations"
                    )
                ]
                if incomplete:
                    return GMMessageIntegrityIssue(
                        error_code="SESSION_ZERO_PROPOSAL_REVISION_INCOMPLETE",
                        message="玩家赞成提案时同时给出了修订，但确认调用没有携带修订操作包。",
                        correction_hint=(
                            "把玩家确认后的完整版本逐项写入"
                            "replacement_world_operations；旧提案内容不会自动与新名字合并。"
                        ),
                        missing=("replacement_world_operations",),
                        required_repair_tools=("confirm_session_zero_proposal",),
                        details={
                            "proposal_subject": requirement.subject,
                            "proposal_ids": list(requirement.proposal_ids),
                            "source_event_id": plan.source_event_id,
                        },
                    )

        if not plan.hero_skill_options:
            return None
        relevant_updates: list[Mapping[str, object]] = []
        for call in calls:
            if cls._clean(call.get("tool_name")) != "update_hero_draft":
                continue
            arguments = call.get("arguments")
            if not isinstance(arguments, Mapping):
                continue
            if not cls._source_event_matches(
                plan.source_event_id,
                cls._clean(arguments.get("source_event_id")),
                strict=plan.strict_source_event,
            ):
                continue
            relevant_updates.append(arguments)
        if not relevant_updates:
            return None

        missing: list[str] = []
        patch_fields: set[str] = set()
        attributes_submitted = False
        for requirement in plan.hero_skill_options:
            covered = False
            for arguments in relevant_updates:
                patch = arguments.get("patch")
                if not isinstance(patch, Mapping):
                    continue
                patch_fields.update(str(key) for key in patch)
                attributes_submitted = attributes_submitted or bool(
                    isinstance(patch.get("attributes"), Mapping)
                    and patch.get("attributes")
                )
                options = patch.get("skill_options")
                if not isinstance(options, Mapping):
                    continue
                raw_choices = options.get(requirement.skill_name)
                choices = (
                    list(raw_choices)
                    if isinstance(raw_choices, Sequence)
                    and not isinstance(raw_choices, (str, bytes))
                    else []
                )
                if any(
                    cls._normalize_choice(choice)
                    == cls._normalize_choice(requirement.choice)
                    for choice in choices
                ):
                    covered = True
                    break
            if not covered:
                missing.append(
                    f"skill_options.{requirement.skill_name}={requirement.choice}"
                )

        unsafe_attributes = bool(
            attributes_submitted and not plan.hero_attributes_explicit
        )
        if missing or unsafe_attributes:
            expected = [
                {
                    "skill_name": item.skill_name,
                    "choice": item.choice,
                }
                for item in plan.hero_skill_options
            ]
            return GMMessageIntegrityIssue(
                error_code="SESSION_ZERO_HERO_OPTION_MISMAPPED",
                message=(
                    "玩家选择的是技能自身的附加选项，不能把它改写成角色基础属性。"
                ),
                correction_hint=(
                    "不要执行当前update_hero_draft；仅把本句选择写入"
                    "patch.skill_options，键为完整技能名，值为选择字符串数组。"
                ),
                missing=tuple(missing),
                details={
                    "expected_skill_options": expected,
                    "submitted_patch_fields": sorted(patch_fields),
                    "base_attributes_submitted": attributes_submitted,
                    "base_attributes_explicit_in_message": (
                        plan.hero_attributes_explicit
                    ),
                    "source_event_id": plan.source_event_id,
                },
            )
        return None

    @classmethod
    def validate_terminal(
        cls,
        plan: GMMessageIntegrityPlan,
        receipts: Iterable[object],
        *,
        semantic_message_kind: str = "",
    ) -> Optional[GMMessageIntegrityIssue]:
        """Verify planned obligations against transaction-local receipts.

        World-write completeness is only fail-closed when the semantic agent
        classified the message as a contribution/mixed turn, or when no
        semantic classification is available (the legacy/test path).  A
        ``gm_request`` such as "我的王国贡献是什么" is a read request; nouns in
        that question must not manufacture a write obligation behind the
        model's back.
        """

        receipt_items = list(receipts)
        evidence = [
            item
            for item in (
                cls._receipt_evidence(receipt) for receipt in receipt_items
            )
            if item is not None
            and cls._source_event_matches(
                plan.source_event_id,
                str(item.get("source_event_id") or ""),
                strict=plan.strict_source_event,
            )
        ]
        successful = [
            item
            for item in evidence
            if bool(item.get("ok"))
            and bool(item.get("state_changed"))
            and item.get("rolled_back") is not True
        ]
        confirmation_requirements = cls._effective_proposal_confirmations(
            plan,
            receipt_items,
        )

        if plan.proposal_persistence_required:
            covered_subjects: set[str] = set()
            semantically_complete = False
            for item in successful:
                if (
                    cls._clean(item.get("tool_name"))
                    != "propose_session_zero_update"
                    or item.get("proposal_persisted") is not True
                ):
                    continue
                semantically_complete = bool(
                    semantically_complete
                    or item.get("semantic_source_complete") is True
                )
                covered_subjects.update(
                    str(subject)
                    for subject in list(item.get("proposal_subjects") or [])
                )
            missing_subjects = (
                ()
                if semantically_complete
                else tuple(
                    subject
                    for subject in plan.proposal_subjects
                    if subject not in covered_subjects
                )
            )
            if missing_subjects:
                return GMMessageIntegrityIssue(
                    error_code="SESSION_ZERO_PROPOSAL_INCOMPLETE",
                    message=(
                        "玩家明确提出了尚待确认的第零章方案，但本轮没有成功保存待定提案。"
                    ),
                    correction_hint=(
                        "留在当前事务内调用propose_session_zero_update保存方案；"
                        "不得直接写入正式世界状态，也不得在成功回执前结束本轮。"
                    ),
                    missing=missing_subjects,
                    required_repair_tools=("propose_session_zero_update",),
                    details={
                        "proposal_subjects": list(plan.proposal_subjects),
                        "covered_proposal_subjects": sorted(covered_subjects),
                        "source_event_id": plan.source_event_id,
                    },
                )

        if confirmation_requirements:
            ambiguous_requirement = next(
                (
                    requirement
                    for requirement in confirmation_requirements
                    if requirement.ambiguous
                ),
                None,
            )
            if ambiguous_requirement is not None:
                return cls._ambiguous_proposal_issue(
                    ambiguous_requirement,
                    source_event_id=plan.source_event_id,
                )
            confirms = [
                item
                for item in successful
                if cls._clean(item.get("tool_name"))
                == "confirm_session_zero_proposal"
            ]
            covered_categories = cls._covered_world_categories(successful)
            missing_confirmation_subjects: list[str] = []
            for requirement in confirmation_requirements:
                if requirement.proposal_ids:
                    matched = any(
                        cls._clean(item.get("proposal_id"))
                        in requirement.proposal_ids
                        and (
                            not item.get("proposal_subjects")
                            or requirement.subject
                            in set(item.get("proposal_subjects") or [])
                            or bool(
                                set(item.get("proposal_scope_categories") or [])
                                & cls.proposal_subject_coverage(
                                    requirement.subject
                                )
                            )
                        )
                        and (
                            not requirement.replacement_required
                            or item.get("proposal_replacement_used") is True
                        )
                        and item.get("proposal_cleared") is True
                        for item in confirms
                    )
                else:
                    # There was no persisted object to close.  In that narrow
                    # case a public, player-authorized formal write is the
                    # authoritative expression of the already-reached table
                    # consensus.
                    scoped_confirm = any(
                        requirement.subject
                        in set(item.get("proposal_subjects") or [])
                        for item in confirms
                    )
                    unscoped_single_confirm = bool(
                        len(plan.proposal_confirmations) == 1
                        and len(confirms) == 1
                        and not confirms[0].get("proposal_subjects")
                    )
                    matched = bool(
                        scoped_confirm
                        or unscoped_single_confirm
                        or (
                        covered_categories
                        & cls.proposal_subject_coverage(requirement.subject)
                        )
                    )
                if not matched:
                    missing_confirmation_subjects.append(requirement.subject)
            if missing_confirmation_subjects:
                return GMMessageIntegrityIssue(
                    error_code="SESSION_ZERO_PROPOSAL_CONFIRMATION_INCOMPLETE",
                    message=(
                        "玩家已经明确赞成待定的第零章方案，但本轮没有成功确认提案或写入对应正式字段。"
                    ),
                    correction_hint=(
                        "从当前state_summary.pending_proposals选择对应ID并调用"
                        "confirm_session_zero_proposal；若没有可匹配提案，再把玩家已确认的"
                        "内容写入对应正式字段。成功回执前不得结束本轮。"
                    ),
                    missing=tuple(missing_confirmation_subjects),
                    required_repair_tools=(
                        "confirm_session_zero_proposal",
                        "create_world_setting",
                        "update_world_setting",
                    ),
                    details={
                        "required_confirmation_subjects": list(
                            plan.proposal_confirmation_subjects
                        ),
                        "covered_categories": sorted(covered_categories),
                        "confirmed_proposal_ids": sorted(
                            cls._clean(item.get("proposal_id"))
                            for item in confirms
                            if cls._clean(item.get("proposal_id"))
                        ),
                        "source_event_id": plan.source_event_id,
                    },
                )

        covered_world = cls._covered_world_categories(successful)
        message_kind = cls._clean(semantic_message_kind).lower()
        enforce_world_contributions = (
            not message_kind
            or message_kind in {"state_contribution", "mixed"}
            or bool(covered_world)
        )
        missing_world = (
            [
                category
                for category in plan.world_categories
                if not (covered_world & cls.WORLD_CATEGORY_COVERAGE[category])
            ]
            if enforce_world_contributions
            else []
        )
        if missing_world:
            labels = [cls.WORLD_CATEGORY_LABELS[item] for item in missing_world]
            write_hints = [
                cls.WORLD_CATEGORY_WRITE_HINTS[item]
                for item in missing_world
                if item in cls.WORLD_CATEGORY_WRITE_HINTS
            ]
            return GMMessageIntegrityIssue(
                error_code="SESSION_ZERO_CONTRIBUTION_INCOMPLETE",
                message=(
                    "本轮明确贡献的第零章内容尚未全部产生成功回执："
                    + "、".join(labels)
                    + "。"
                ),
                correction_hint=(
                    "留在当前消息事务内，只补尚未成功的类别，不要重建已有地点或重复"
                    "成功调用。"
                    + ("；".join(write_hints) + "。" if write_hints else "")
                    + "全部成功前不得final、silent或ask_user，也不能拖到开章时再补。"
                ),
                missing=tuple(missing_world),
                details={
                    "required_categories": list(plan.world_categories),
                    "covered_categories": sorted(covered_world),
                    "category_write_hints": write_hints,
                    "source_event_id": plan.source_event_id,
                },
            )

        committed_evidence = [
            item
            for item in evidence
            if bool(item.get("ok")) and item.get("rolled_back") is not True
        ]
        covered_skips = {
            cls._clean(item.get("topic"))
            for item in committed_evidence
            if cls._clean(item.get("tool_name"))
            == "mark_session_zero_topic_complete"
        }
        missing_world_skips = tuple(
            category
            for category in plan.skipped_world_categories
            if (
                topic_code := cls.WORLD_SKIP_TOPIC_CODES.get(category)
            )
            and topic_code not in covered_skips
        )
        missing_session_zero_skips = tuple(
            topic
            for topic in plan.skipped_session_zero_topics
            if topic not in covered_skips
        )
        missing_skips = tuple(
            [*missing_world_skips, *missing_session_zero_skips]
        )
        missing_skip_topics = tuple(
            [
                *(
                    cls.WORLD_SKIP_TOPIC_CODES[item]
                    for item in missing_world_skips
                ),
                *missing_session_zero_skips,
            ]
        )
        if missing_skips:
            return GMMessageIntegrityIssue(
                error_code="SESSION_ZERO_TOPIC_SKIP_INCOMPLETE",
                message=(
                    "玩家明确跳过的第零章贡献项尚未产生未回滚的完成回执。"
                ),
                correction_hint=(
                    "留在当前消息事务内，为每个缺项调用"
                    "mark_session_zero_topic_complete；topic依次使用"
                    + "、".join(
                        item for item in missing_skip_topics
                    )
                    + "。同批其他工具失败会回滚此前标记，修正后必须重新提交。"
                ),
                missing=missing_skips,
                required_repair_tools=("mark_session_zero_topic_complete",),
                details={
                    "required_skip_topics": [
                        item for item in missing_skip_topics
                    ],
                    "covered_skip_topics": sorted(covered_skips),
                    "source_event_id": plan.source_event_id,
                },
            )

        covered_defer_event_ids = {
            cls._clean(item.get("source_event_id"))
            for item in committed_evidence
            if cls._clean(item.get("tool_name"))
            == "pause_session_zero_nudges"
        }
        if (
            (
                plan.deferred_world_categories
                or plan.deferred_session_zero_topics
            )
            and plan.source_event_id not in covered_defer_event_ids
        ):
            return GMMessageIntegrityIssue(
                error_code="SESSION_ZERO_TOPIC_DEFER_INCOMPLETE",
                message="玩家明确暂缓的第零章问题尚未产生暂停追问回执。",
                correction_hint=(
                    "留在当前消息事务内调用pause_session_zero_nudges；"
                    "若回执包含same_turn_handoff，本轮应立即应声并把问题交给指定玩家。"
                ),
                missing=tuple(
                    dict.fromkeys(
                        [
                            *plan.deferred_world_categories,
                            *plan.deferred_session_zero_topics,
                        ]
                    )
                ),
                required_repair_tools=("pause_session_zero_nudges",),
                details={
                    "required_defer_categories": list(
                        plan.deferred_world_categories
                    )
                    + list(plan.deferred_session_zero_topics),
                    "source_event_id": plan.source_event_id,
                },
            )

        covered_safety = [
            (
                cls._clean(item.get("kind")),
                cls._normalize_safety_content(item.get("content")),
            )
            for item in successful
            if cls._clean(item.get("tool_name")) == "record_safety_boundary"
        ]
        remaining_safety = list(covered_safety)
        missing_safety: list[GMSafetyDeclarationRequirement] = []
        for declaration in plan.safety_declarations:
            expected = (
                declaration.kind,
                cls._normalize_safety_content(declaration.content),
            )
            if expected in remaining_safety:
                remaining_safety.remove(expected)
            else:
                missing_safety.append(declaration)
        if missing_safety:
            labels = [
                f"{cls.SAFETY_KIND_LABELS[item.kind]}【{item.content}】"
                for item in missing_safety
            ]
            return GMMessageIntegrityIssue(
                error_code="SAFETY_BOUNDARY_INCOMPLETE",
                message="玩家明确声明的安全边界尚未全部记录：" + "、".join(labels) + "。",
                correction_hint=(
                    "在当前事务内分别调用record_safety_boundary；每条界限使用kind=line，"
                    "每条帷幕使用kind=veil，全部成功后再结束本轮。"
                ),
                missing=tuple(
                    f"{item.kind}:{item.content}" for item in missing_safety
                ),
                details={
                    "required_declarations": [
                        {"kind": item.kind, "content": item.content}
                        for item in plan.safety_declarations
                    ],
                    "covered_declarations": [
                        {"kind": kind, "content": content}
                        for kind, content in covered_safety
                    ],
                    "source_event_id": plan.source_event_id,
                },
            )

        if plan.hero_fields:
            covered_fields: set[str] = set()
            for item in evidence:
                if (
                    cls._clean(item.get("tool_name")) != "update_hero_draft"
                    or not cls._hero_update_receipt_matches_speaker(plan, item)
                ):
                    continue
                if bool(item.get("ok")) and item.get("rolled_back") is not True:
                    if bool(item.get("state_changed")):
                        covered_fields.update(
                            cls._clean(field_name)
                            for field_name in list(
                                item.get("changed_fields") or []
                            )
                        )
                    # An idempotent success is authoritative evidence too: the
                    # hero tool has already checked ownership and applied the
                    # complete patch to a copy of the current draft before it
                    # reports that these exact fields need no change.
                    covered_fields.update(
                        cls._clean(field_name)
                        for field_name in list(
                            item.get("already_satisfied_fields") or []
                        )
                    )
                elif cls._clean(item.get("error_code")) == "HERO_PATCH_NO_EFFECT":
                    covered_fields.update(
                        cls._clean(field_name)
                        for field_name in list(
                            item.get("already_satisfied_fields") or []
                        )
                    )
            missing_fields = tuple(
                field_name
                for field_name in plan.hero_fields
                if field_name not in covered_fields
            )
            if missing_fields:
                return GMMessageIntegrityIssue(
                    error_code="SESSION_ZERO_HERO_FIELDS_INCOMPLETE",
                    message=(
                        "玩家本句明确提供的角色草稿字段尚未全部获得成功回执。"
                    ),
                    correction_hint=(
                        "在当前事务内调用update_hero_draft，只把本句明确给出的缺少字段"
                        "放进patch；不要替玩家补写未提供的值。"
                    ),
                    missing=missing_fields,
                    details={
                        "required_fields": list(plan.hero_fields),
                        "covered_fields": sorted(covered_fields),
                        "expected_player": plan.speaker,
                        "source_event_id": plan.source_event_id,
                    },
                )

        if plan.hero_skill_options:
            successful_updates = [
                item
                for item in successful
                if cls._clean(item.get("tool_name")) == "update_hero_draft"
                and cls._hero_update_receipt_matches_speaker(plan, item)
            ]
            missing_options: list[GMHeroSkillOptionRequirement] = []
            for requirement in plan.hero_skill_options:
                applied = False
                for item in successful_updates:
                    options = item.get("applied_skill_options")
                    if not isinstance(options, Mapping):
                        continue
                    raw_choices = options.get(requirement.skill_name)
                    choices = (
                        list(raw_choices)
                        if isinstance(raw_choices, Sequence)
                        and not isinstance(raw_choices, (str, bytes))
                        else []
                    )
                    if any(
                        cls._normalize_choice(choice)
                        == cls._normalize_choice(requirement.choice)
                        for choice in choices
                    ):
                        applied = True
                        break
                if not applied:
                    missing_options.append(requirement)
            if missing_options:
                missing = tuple(
                    f"skill_options.{item.skill_name}={item.choice}"
                    for item in missing_options
                )
                return GMMessageIntegrityIssue(
                    error_code="SESSION_ZERO_HERO_OPTION_INCOMPLETE",
                    message="玩家明确选择的技能附加选项尚未被成功回执逐项确认。",
                    correction_hint=(
                        "在当前事务内调用update_hero_draft，把选择写入"
                        "patch.skill_options；不要修改基础attributes。"
                    ),
                    missing=missing,
                    details={
                        "required_skill_options": [
                            {
                                "skill_name": item.skill_name,
                                "choice": item.choice,
                            }
                            for item in plan.hero_skill_options
                        ],
                        "source_event_id": plan.source_event_id,
                    },
                )

        return None

    @classmethod
    def _decision_calls(
        cls,
        decision: Mapping[str, object],
    ) -> list[Mapping[str, object]]:
        action = cls._clean(decision.get("decision")).lower()
        if action == "call_tool":
            return [decision]
        if action != "call_tools":
            return []
        raw_calls = decision.get("calls")
        if not isinstance(raw_calls, Sequence) or isinstance(
            raw_calls, (str, bytes)
        ):
            return []
        return [item for item in raw_calls if isinstance(item, Mapping)]

    @classmethod
    def _receipt_evidence(
        cls,
        raw_receipt: object,
    ) -> Optional[dict[str, object]]:
        wrapper_event_id = ""
        receipt = raw_receipt
        if isinstance(raw_receipt, Mapping) and "receipt" in raw_receipt:
            wrapper_event_id = cls._clean(raw_receipt.get("source_event_id"))
            receipt = raw_receipt.get("receipt")

        if isinstance(receipt, Mapping):
            tool_name = cls._clean(receipt.get("tool_name"))
            ok = bool(receipt.get("ok"))
            state_changed = bool(receipt.get("state_changed"))
            error_code = cls._clean(receipt.get("error_code"))
            retryable = bool(receipt.get("retryable"))
            result = receipt.get("result")
            narratives = receipt.get("narrative_events")
        else:
            tool_name = cls._clean(getattr(receipt, "tool_name", ""))
            ok = bool(getattr(receipt, "ok", False))
            state_changed = bool(getattr(receipt, "state_changed", False))
            error_code = cls._clean(getattr(receipt, "error_code", ""))
            retryable = bool(getattr(receipt, "retryable", False))
            result = getattr(receipt, "result", {})
            narratives = getattr(receipt, "narrative_events", ())
        if not tool_name:
            return None
        result = result if isinstance(result, Mapping) else {}
        category = cls._clean(result.get("category"))
        record = result.get("record")
        if not category and isinstance(record, Mapping):
            category = cls._clean(record.get("category"))

        source_event_id = wrapper_event_id
        source_event = result.get("source_event")
        if not source_event_id and isinstance(source_event, Mapping):
            source_event_id = cls._clean(source_event.get("event_id"))
        if not source_event_id:
            source_event_id = cls._clean(result.get("source_event_id"))
        if not source_event_id and isinstance(narratives, Sequence):
            for narrative in narratives:
                if isinstance(narrative, Mapping):
                    candidate = cls._clean(narrative.get("source_event_id"))
                else:
                    candidate = cls._clean(
                        getattr(narrative, "source_event_id", "")
                    )
                if candidate:
                    source_event_id = candidate
                    break

        proposal = result.get("proposal")
        proposal_subjects = cls._proposal_subjects_for_pending(
            proposal if isinstance(proposal, Mapping) else {}
        )
        proposal_scope_categories: list[str] = []
        result_scope_categories = result.get("proposal_scope_categories")
        if isinstance(result_scope_categories, Sequence) and not isinstance(
            result_scope_categories,
            (str, bytes),
        ):
            proposal_scope_categories.extend(
                cls._clean(item)
                for item in result_scope_categories
                if cls._clean(item)
            )
        if isinstance(proposal, Mapping):
            proposed_updates = proposal.get("proposed_updates")
            if isinstance(proposed_updates, Mapping):
                proposal_scope_categories.extend(
                    cls._clean(key) for key in proposed_updates
                )
            proposal_operations = proposal.get("world_operations")
            if isinstance(proposal_operations, Sequence) and not isinstance(
                proposal_operations,
                (str, bytes),
            ):
                proposal_scope_categories.extend(
                    cls._clean(operation.get("category"))
                    for operation in proposal_operations
                    if isinstance(operation, Mapping)
                    and cls._clean(operation.get("category"))
                )
        scope_subjects = result.get("proposal_scope_subjects")
        if isinstance(scope_subjects, Sequence) and not isinstance(
            scope_subjects,
            (str, bytes),
        ):
            proposal_subjects = tuple(
                dict.fromkeys(
                    [*proposal_subjects, *(cls._clean(item) for item in scope_subjects)]
                )
            )
        if proposal_scope_categories:
            proposal_subjects = tuple(
                dict.fromkeys(
                    [
                        *proposal_subjects,
                        *cls._proposal_subjects_for_pending(
                            {"scope_categories": proposal_scope_categories}
                        ),
                    ]
                )
            )
        recorded_categories = result.get("recorded_categories")
        applied_fields = result.get("applied_fields")
        return {
            "tool_name": tool_name,
            "ok": ok,
            "state_changed": state_changed,
            "error_code": error_code,
            "retryable": retryable,
            "rolled_back": result.get("rolled_back") is True,
            "operation": cls._clean(result.get("operation")),
            "category": category,
            "recorded_categories": (
                [cls._clean(item) for item in recorded_categories]
                if isinstance(recorded_categories, Sequence)
                and not isinstance(recorded_categories, (str, bytes))
                else []
            ),
            "applied_fields": (
                [cls._clean(item) for item in applied_fields]
                if isinstance(applied_fields, Sequence)
                and not isinstance(applied_fields, (str, bytes))
                else []
            ),
            "visibility": cls._clean(result.get("visibility")),
            "authority": cls._clean(result.get("authority")),
            "topic": cls._clean(result.get("topic")),
            "kind": cls._clean(result.get("kind")),
            "content": cls._clean(result.get("content")),
            "applied_skill_options": result.get("applied_skill_options"),
            "changed_fields": (
                [cls._clean(item) for item in list(result.get("changed_fields") or [])]
                if isinstance(result.get("changed_fields"), Sequence)
                and not isinstance(result.get("changed_fields"), (str, bytes))
                else []
            ),
            "already_satisfied_fields": (
                [
                    cls._clean(item)
                    for item in list(result.get("already_satisfied_fields") or [])
                ]
                if isinstance(result.get("already_satisfied_fields"), Sequence)
                and not isinstance(
                    result.get("already_satisfied_fields"),
                    (str, bytes),
                )
                else []
            ),
            "proposal_persisted": isinstance(result.get("proposal"), Mapping),
            "semantic_source_complete": (
                result.get("semantic_source_complete") is True
            ),
            "proposal_id": cls._clean(
                result.get("proposal_id")
                or (
                    proposal.get("id")
                    if isinstance(proposal, Mapping)
                    else ""
                )
            ),
            "proposal_speaker": cls._clean(
                proposal.get("speaker")
                if isinstance(proposal, Mapping)
                else ""
            ),
            "proposal_summary": cls._clean(
                proposal.get("summary")
                if isinstance(proposal, Mapping)
                else result.get("summary")
            ),
            "proposal_scope_categories": list(
                dict.fromkeys(proposal_scope_categories)
            ),
            "proposal_subjects": list(proposal_subjects),
            "proposal_cleared": result.get("proposal_cleared") is True,
            "proposal_replacement_used": (
                result.get("proposal_replacement_used") is True
            ),
            "player_name": cls._clean(result.get("player_name")),
            "hero_name": cls._clean(result.get("hero_name")),
            "record_key": cls._clean(result.get("record_key")),
            "ready": result.get("ready"),
            "source_event_id": source_event_id,
        }

    @classmethod
    def _covered_world_categories(
        cls,
        successful: Iterable[Mapping[str, object]],
    ) -> set[str]:
        covered: set[str] = set()
        for item in successful:
            tool_name = cls._clean(item.get("tool_name"))
            if tool_name in cls.WORLD_TOOL_NAMES:
                if (
                    cls._clean(item.get("operation"))
                    not in cls.WORLD_CONTRIBUTION_OPERATIONS
                    or cls._clean(item.get("visibility")) != "public"
                    or cls._clean(item.get("authority"))
                    not in cls.WORLD_PLAYER_AUTHORITIES
                ):
                    continue
                category = cls._clean(item.get("category"))
                if category:
                    covered.add(category)
                continue
            if tool_name == "commit_session_zero_update":
                covered.update(
                    cls._clean(category)
                    for category in list(item.get("applied_fields") or [])
                    if cls._clean(category)
                )
        return covered

    @classmethod
    def _hero_update_receipt_matches_speaker(
        cls,
        plan: GMMessageIntegrityPlan,
        item: Mapping[str, object],
    ) -> bool:
        """Bind a structured hero update receipt to the current speaker.

        This compares authoritative identities only. It does not infer intent
        from player prose and therefore remains part of the permission boundary.
        """

        expected = cls._clean(plan.speaker)
        if not expected:
            return True
        actual = cls._clean(item.get("player_name"))
        return bool(actual and actual == expected)

    @classmethod
    def _call_matches_plan(
        cls,
        plan: GMMessageIntegrityPlan,
        call: Mapping[str, object],
    ) -> bool:
        required = cls._clean(plan.source_event_id)
        if not required:
            return True
        actual = cls._clean(cls._call_arguments(call).get("source_event_id"))
        return bool(actual == required) if plan.strict_source_event else bool(
            not actual or actual == required
        )

    @staticmethod
    def _call_arguments(call: Mapping[str, object]) -> Mapping[str, object]:
        arguments = call.get("arguments")
        return arguments if isinstance(arguments, Mapping) else {}

    @classmethod
    def _formal_write_subjects(
        cls,
        call: Mapping[str, object],
    ) -> set[str]:
        tool_name = cls._clean(call.get("tool_name"))
        arguments = cls._call_arguments(call)
        categories: set[str] = set()
        if tool_name in cls.WORLD_TOOL_NAMES:
            category = cls._clean(arguments.get("category"))
            if category:
                categories.add(category)
        elif tool_name == "commit_session_zero_update":
            updates = arguments.get("updates")
            if isinstance(updates, Mapping):
                categories.update(cls._clean(key) for key in updates)
        subjects: set[str] = set()
        for subject, covered in cls.PROPOSAL_CONFIRMATION_CATEGORY_COVERAGE.items():
            if categories & set(covered):
                subjects.add(subject)
        subjects.update(
            category
            for category in categories
            if category in WorldSettingCatalog.CATEGORIES
        )
        return subjects

    @classmethod
    def _confirmation_replacement_subjects(
        cls,
        call: Mapping[str, object],
    ) -> set[str]:
        """Return semantic subjects embedded in a confirmation replacement."""

        if cls._clean(call.get("tool_name")) != "confirm_session_zero_proposal":
            return set()
        raw_operations = cls._call_arguments(call).get(
            "replacement_world_operations"
        )
        if not isinstance(raw_operations, Sequence) or isinstance(
            raw_operations,
            (str, bytes),
        ):
            return set()
        categories = {
            cls._clean(operation.get("category"))
            for operation in raw_operations
            if isinstance(operation, Mapping)
            and cls._clean(operation.get("category"))
        }
        subjects = {
            subject
            for subject, covered in cls.PROPOSAL_CONFIRMATION_CATEGORY_COVERAGE.items()
            if categories & set(covered)
        }
        subjects.update(
            category
            for category in categories
            if category in WorldSettingCatalog.CATEGORIES
        )
        return subjects

    @classmethod
    def _proposal_clause_modes(
        cls,
        text: str,
        *,
        session_zero: bool,
    ) -> tuple[tuple[str, ...], tuple[tuple[str, str], ...]]:
        if not session_zero:
            return (), ()
        proposals: list[str] = []
        confirmations: list[tuple[str, str]] = []
        for clause in cls._clauses(text):
            subjects = [
                subject
                for subject, pattern in cls._PROPOSAL_SUBJECT_PATTERNS
                if pattern.search(clause)
            ]
            # A group proposal often names its destination (for example,
            # “护送去钟鸣公国”).  The proper noun is evidence for the route,
            # not a second proposal to create or alter that country.
            if (
                "group_concept" in subjects
                and "kingdoms" in subjects
                and not cls._EXPLICIT_KINGDOM_PROPOSAL_RE.search(clause)
            ):
                subjects.remove("kingdoms")
            if not subjects:
                continue
            # Completed agreement wins over words such as “大家/同意” that
            # also occur in a question.  A question mark or the explicit
            # question forms remain a proposal unless the clause says the
            # agreement has already happened.
            confirmed = bool(cls._PROPOSAL_CONFIRM_RE.search(clause))
            proposed = bool(cls._PROPOSAL_RE.search(clause)) and not confirmed
            if confirmed:
                for subject in subjects:
                    item = (subject, clause)
                    if item not in confirmations:
                        confirmations.append(item)
            elif proposed:
                for subject in subjects:
                    if subject not in proposals:
                        proposals.append(subject)
        return tuple(proposals), tuple(confirmations)

    @classmethod
    def _implicit_hero_skill_selection(
        cls,
        text: str,
        state_summary: Mapping[str, object] | None,
    ) -> bool:
        """Recognize terse follow-up choices only for an existing hero name.

        Players commonly say “伊莉雅再选防御精通” after the first labelled
        skill choice.  Treating every “再选” in Session 0 as a hero edit would
        also catch map and world votes, so the terse form is enabled only when
        the sentence names a hero already present in the authoritative model
        projection.
        """

        if not re.search(r"(?:再|又)(?:选|选择)\S+", text):
            return False
        hero_names: set[str] = set()
        stack: list[object] = [state_summary] if state_summary is not None else []
        seen: set[int] = set()
        while stack:
            value = stack.pop()
            if isinstance(value, Mapping):
                marker = id(value)
                if marker in seen:
                    continue
                seen.add(marker)
                hero_name = cls._clean(value.get("hero_name"))
                if hero_name:
                    hero_names.add(hero_name)
                stack.extend(value.values())
            elif isinstance(value, Sequence) and not isinstance(
                value,
                (str, bytes),
            ):
                stack.extend(value)
        return any(
            re.search(rf"(?:^|[。！？；;，,\s]){re.escape(name)}(?:再|又)(?:选|选择)", text)
            for name in hero_names
        )

    @classmethod
    def _safety_obligation_text(cls, text: str) -> str:
        selected: list[str] = []
        for clause in cls._clauses(text):
            if not cls._HIGH_CONFIDENCE_SAFETY_RE.search(clause):
                continue
            if (
                re.search(r"(?:战术|打法|操作|路线|方案)", clause)
                and re.search(r"(?:不舒服|不适)", clause)
                and not re.search(
                    r"(?:界限|帷幕|面纱|雷点|游戏|故事|剧情)",
                    clause,
                )
            ):
                # “这个战术让我不舒服” usually means change the current
                # approach, not persist a campaign-wide content boundary.
                continue
            # Preserve “我的界限/帷幕” label boundaries for the shared parser.
            selected.append(
                re.sub(
                    r"我的(?=(?:界限|帷幕|面纱)\s*[:：])",
                    "",
                    clause,
                )
            )
        return "；".join(selected)

    @staticmethod
    def _clauses(text: str) -> tuple[str, ...]:
        return tuple(
            clause.strip()
            for clause in re.split(r"[。！？；;\n]", str(text or ""))
            if clause.strip()
        )

    @classmethod
    def _pending_proposals(
        cls,
        state_summary: Mapping[str, object] | None,
    ) -> tuple[dict[str, object], ...]:
        if not isinstance(state_summary, Mapping):
            return ()
        # Only read the authoritative current-state paths.  Recursively
        # scanning the whole prompt can rediscover stale proposal copies in a
        # delta/audit section, and sorting random ids destroys “刚才”的 order.
        containers: list[Mapping[str, object]] = [state_summary]
        session_zero = state_summary.get("session_zero")
        if isinstance(session_zero, Mapping):
            containers.append(session_zero)
        found: list[dict[str, object]] = []
        seen_ids: set[str] = set()
        for container in containers:
            raw = container.get("pending_proposals")
            if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
                continue
            for item in raw:
                if not isinstance(item, Mapping):
                    continue
                proposal_id = cls._clean(item.get("id"))
                if proposal_id and proposal_id not in seen_ids:
                    seen_ids.add(proposal_id)
                    found.append(dict(item))
        return tuple(found)

    @staticmethod
    def _pending_proposals_state_is_authoritative(
        state_summary: Mapping[str, object] | None,
    ) -> bool:
        """Whether the snapshot explicitly included the pending-proposal list."""

        if not isinstance(state_summary, Mapping):
            return False
        if "pending_proposals" in state_summary:
            return True
        session_zero = state_summary.get("session_zero")
        return isinstance(session_zero, Mapping) and "pending_proposals" in session_zero

    @classmethod
    def _select_pending_proposals(
        cls,
        candidates: Sequence[Mapping[str, object]],
        *,
        clause: str,
    ) -> tuple[tuple[Mapping[str, object], ...], bool]:
        """Narrow obvious references and leave semantic choice to the model.

        Multiple surviving candidates are not a regex-level error.  The core
        model receives their public summaries plus recent conversation, and a
        semantic preflight checks its eventual confirmation tool call.  This
        structural validator only limits the set of IDs that may be touched.
        """

        selected = [item for item in candidates if cls._clean(item.get("id"))]
        if len(selected) <= 1:
            return tuple(selected), False
        explicit = [
            item
            for item in selected
            if cls._clean(item.get("id")) in clause
        ]
        if explicit:
            selected = explicit
        else:
            mentioned_speakers = [
                item
                for item in selected
                if cls._clean(item.get("speaker"))
                and cls._clean(item.get("speaker")) in clause
            ]
            if mentioned_speakers:
                selected = mentioned_speakers
            else:
                anchored = [
                    item
                    for item in selected
                    if any(
                        anchor in clause
                        for anchor in cls._proposal_public_anchors(item)
                    )
                ]
                if anchored:
                    selected = anchored
        if len(selected) > 1 and "刚才" in clause:
            # The authoritative list preserves creation order; after explicit
            # speaker/entity filtering, “刚才” means the newest survivor.
            selected = [selected[-1]]
        return tuple(selected), False

    @classmethod
    def _effective_proposal_confirmations(
        cls,
        plan: GMMessageIntegrityPlan,
        receipts: Iterable[object],
    ) -> tuple[GMProposalConfirmationRequirement, ...]:
        """Bind same-turn proposals that did not exist in the initial state."""

        dynamic_candidates: list[dict[str, object]] = []
        for raw in receipts:
            item = cls._receipt_evidence(raw)
            if item is None:
                continue
            if (
                cls._clean(item.get("tool_name"))
                != "propose_session_zero_update"
                or not bool(item.get("ok"))
                or not bool(item.get("state_changed"))
                or item.get("rolled_back") is True
            ):
                continue
            proposal_id = cls._clean(item.get("proposal_id"))
            if not proposal_id:
                continue
            dynamic_candidates.append(
                {
                    "id": proposal_id,
                    "speaker": cls._clean(item.get("proposal_speaker")),
                    "summary": cls._clean(item.get("proposal_summary")),
                    "scope_subjects": list(item.get("proposal_subjects") or []),
                    "scope_categories": list(
                        item.get("proposal_scope_categories") or []
                    ),
                    "source_event_id": cls._clean(item.get("source_event_id")),
                }
            )
        resolved: list[GMProposalConfirmationRequirement] = []
        for requirement in plan.proposal_confirmations:
            if requirement.proposal_ids or requirement.ambiguous:
                resolved.append(requirement)
                continue
            candidates = [
                item
                for item in dynamic_candidates
                if requirement.subject
                in cls._proposal_subjects_for_pending(item)
                and (
                    not plan.source_event_id
                    or cls._clean(item.get("source_event_id"))
                    != cls._clean(plan.source_event_id)
                )
                and (
                    not plan.strict_source_event
                    or cls._clean(item.get("source_event_id"))
                    in set(plan.prior_source_event_ids)
                )
            ]
            selected, ambiguous = cls._select_pending_proposals(
                candidates,
                clause=requirement.clause,
            )
            resolved.append(
                GMProposalConfirmationRequirement(
                    subject=requirement.subject,
                    proposal_ids=tuple(
                        cls._clean(item.get("id")) for item in selected
                    ),
                    replacement_required=requirement.replacement_required,
                    ambiguous=ambiguous,
                    clause=requirement.clause,
                )
            )
        return tuple(resolved)

    @staticmethod
    def _ambiguous_proposal_issue(
        requirement: GMProposalConfirmationRequirement,
        *,
        source_event_id: str,
    ) -> GMMessageIntegrityIssue:
        return GMMessageIntegrityIssue(
            error_code="SESSION_ZERO_PROPOSAL_CONFIRMATION_AMBIGUOUS",
            message="当前有多条同类待定提案，玩家这句话不足以唯一确定要确认哪一条。",
            correction_hint=(
                "不要执行任何写操作；只用提案人和自然语言摘要区分候选，"
                "请玩家说明接受哪一版。内部proposal_id只能用于工具参数，"
                "不得出现在公开回复中。"
            ),
            missing=requirement.proposal_ids,
            details={
                "proposal_subject": requirement.subject,
                "candidate_proposal_ids": list(requirement.proposal_ids),
                "source_event_id": source_event_id,
                "clarification_allowed": True,
            },
        )

    @classmethod
    def _proposal_public_anchors(
        cls,
        proposal: Mapping[str, object],
    ) -> tuple[str, ...]:
        anchors: list[str] = []
        updates = proposal.get("proposed_updates")
        if isinstance(updates, Mapping):
            stack: list[object] = list(updates.values())
            while stack:
                value = stack.pop()
                if isinstance(value, Mapping):
                    stack.extend(value.values())
                elif isinstance(value, Sequence) and not isinstance(
                    value,
                    (str, bytes),
                ):
                    stack.extend(value)
                else:
                    text = cls._clean(value)
                    if len(text) >= 2:
                        anchors.append(text)
        operations = proposal.get("world_operations")
        if isinstance(operations, Sequence) and not isinstance(
            operations,
            (str, bytes),
        ):
            for operation in operations:
                if not isinstance(operation, Mapping):
                    continue
                for key in ("name", "new_name", "value"):
                    text = cls._clean(operation.get(key))
                    if len(text) >= 2:
                        anchors.append(text)
        return tuple(dict.fromkeys(anchors))

    @classmethod
    def _proposal_subjects_for_pending(
        cls,
        proposal: Mapping[str, object],
    ) -> tuple[str, ...]:
        if not isinstance(proposal, Mapping):
            return ()
        summary = cls._clean(proposal.get("summary"))
        categories: set[str] = set()
        structured_categories = proposal.get("scope_categories")
        if isinstance(structured_categories, Sequence) and not isinstance(
            structured_categories,
            (str, bytes),
        ):
            categories.update(cls._clean(item) for item in structured_categories)
        updates = proposal.get("proposed_updates")
        if isinstance(updates, Mapping):
            categories.update(cls._clean(key) for key in updates)
        operations = proposal.get("world_operations")
        if isinstance(operations, Sequence) and not isinstance(
            operations,
            (str, bytes),
        ):
            for operation in operations:
                if isinstance(operation, Mapping):
                    category = cls._clean(operation.get("category"))
                    if category:
                        categories.add(category)
        subjects: list[str] = []
        structured_subjects = proposal.get("scope_subjects")
        if isinstance(structured_subjects, Sequence) and not isinstance(
            structured_subjects,
            (str, bytes),
        ):
            subjects.extend(
                cls._clean(item)
                for item in structured_subjects
                if cls.proposal_subject_coverage(item)
            )
        structured_scope_available = bool(subjects or categories)
        for subject, covered in cls.PROPOSAL_CONFIRMATION_CATEGORY_COVERAGE.items():
            pattern = dict(cls._PROPOSAL_SUBJECT_PATTERNS)[subject]
            if (
                subject not in subjects
                and (
                    categories & set(covered)
                    or (
                        not structured_scope_available
                        and pattern.search(summary)
                    )
                )
            ):
                subjects.append(subject)
        subjects.extend(
            category
            for category in sorted(categories)
            if (
                category in WorldSettingCatalog.CATEGORIES
                and category not in subjects
            )
        )
        return tuple(subjects)

    @classmethod
    def _deferred_world_categories(
        cls,
        text: str,
        *,
        mode: str,
    ) -> set[str]:
        categories: set[str] = set()
        for clause in re.split(r"[。！？；;\n]", text):
            clean_clause = cls._clean(clause)
            if not clean_clause:
                continue
            if mode == "proposal":
                deferred = bool(cls._PROPOSAL_RE.search(clean_clause)) and not bool(
                    cls._COMMIT_RE.search(clean_clause)
                )
            else:
                deferred = bool(cls._SKIP_RE.search(clean_clause)) and not bool(
                    cls._NEGATED_SKIP_RE.search(clean_clause)
                )
            if not deferred:
                continue
            if re.search(r"(?:国家|王国|帝国|公国|城邦|地区|地点|城市|村落|村社|森林|群岛)", clean_clause):
                categories.add("kingdoms")
            if re.search(r"(?:重大历史|历史事件|历史)", clean_clause):
                categories.add("historical_events")
            if re.search(r"(?:奥秘|谜团|未解之谜)", clean_clause):
                categories.add("mysteries")
            if re.search(r"(?:世界性?威胁|威胁|危机|灾祸|反派)", clean_clause):
                categories.add("world_threats")
            if re.search(r"(?:玩法|游玩|游戏).{0,4}(?:偏好|风格|主题)", clean_clause):
                categories.add("playstyle_themes")
        return categories

    @staticmethod
    def _source_event_matches(
        required: str,
        actual: str,
        *,
        strict: bool = False,
    ) -> bool:
        clean_required = str(required or "").strip()
        clean_actual = str(actual or "").strip()
        if strict and clean_required:
            return clean_actual == clean_required
        if not clean_required or not clean_actual:
            return True
        return clean_required == clean_actual

    @staticmethod
    def _normalize_choice(value: object) -> str:
        text = str(value or "").strip()
        text = re.sub(r"[\s【】\[\]]+", "", text)
        return re.sub(r"[＋与和]", "+", text)

    @staticmethod
    def _normalize_safety_content(value: object) -> str:
        text = clean_safety_item(str(value or ""))
        # The shared parser intentionally preserves some scene-setting words
        # from natural declarations (for example, ``在游戏里出现蜘蛛``), while
        # the tool is allowed to persist the canonical topic ``蜘蛛``.  Compare
        # the safety topic rather than requiring those harmless wrapper words
        # to be copied verbatim.  Do not use arbitrary substring matching here:
        # only a narrow, meaning-free context prefix is removed.
        text = re.sub(
            r"^(?:在)?(?:本次|这次)?(?:游戏|故事|剧情)(?:中|里)"
            r"(?:出现|有|包含|涉及|提到|描写|描述)?\s*",
            "",
            text,
        )
        return re.sub(r"\s+", "", clean_safety_item(text))

    @staticmethod
    def _clean(value: object) -> str:
        return str(value or "").strip()
