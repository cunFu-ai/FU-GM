# FU-GM 系统提示词优化报告

日期：2026-07-05

## 结论摘要

当前提示词最大的问题不是“单句写得长”，而是架构上把一整套《最终物语》核心规则全文式摘要塞进了多个不同职责的 system prompt。尤其是 Action Brain、Session 0、NPC Act、Expressor 都共用 `FABULA_ULTIMA_CORE_SYSTEM_PROMPT`，导致每次调用都背着 1.5 万字符以上的静态规则底座。

这会带来四类问题：

1. 成本和延迟偏高：即使有 prompt cache，首次创建和缓存击穿时都很重。
2. 行为过约束：表达器本来只该润色，却读到了大量硬规则和主持流程，容易输出“流程说明”“后台评价”“可互动焦点清单”。
3. 指令冲突：同一 prompt 里同时存在规则讲解、主持哲学、JSON 契约、输出风格、错误兜底，模型更容易抓错重点。
4. 维护困难：很多已经由 Python 硬编码保障的规则仍重复写在 prompt 中，后续修 bug 容易出现“代码修了，prompt 还在诱导旧行为”。

建议采用“短静态契约 + 动态 system-reminder + 按需规则片段”的结构，而不是继续把规则书塞进所有 system prompt。

## 当前提示词体积

统计对象：`src/fu_gm/prompts.py`

| Prompt | 字符数 | 行数 | 当前用途 | 主要问题 |
| --- | ---: | ---: | --- | --- |
| `FABULA_ULTIMA_CORE_SYSTEM_PROMPT` | 15,609 | 774 | 所有核心 prompt 的共用规则底座 | 过宽，包含大量并非每个角色都需要的规则和主持建议 |
| `ACTION_BRAIN_SYSTEM_PROMPT` | 25,554 | 843 | 选择结构化 action | 职责复杂，但确实最需要规则；仍可拆分为契约 + 按需规则片段 |
| `SESSION_ZERO_SYSTEM_PROMPT` | 25,852 | 932 | 第零章 JSON 抽取和引导 | 不需要携带完整战斗、伤害、地下城、项目等规则 |
| `NPC_ACT_SYSTEM_PROMPT` | 17,568 | 815 | NPC 战术行动 JSON | 不需要携带完整第零章、角色创建、旅行、经济规则 |
| `EXPRESSOR_SYSTEM_PROMPT` | 16,591 | 790 | 规则面板后的叙事补充 | 最不该携带完整规则；它只该遵守“不得改数值、不得泄露、少量表达” |

另有较短、职责更清楚的 prompt：

| Prompt | 文件 | 评价 |
| --- | --- | --- |
| `MESSAGE_ROUTER_SYSTEM_PROMPT` | `src/fu_gm/message_arbiter.py` | 方向正确，适合保持短小，只需继续避免和本地护栏冲突 |
| `IMPORT_SYSTEM_PROMPT` | `src/fu_gm/campaign_importer.py` | 后台结构化任务，长度合理 |
| `SessionLogManager._system_prompt()` | `src/fu_gm/components/session_log_manager.py` | 后台摘要任务，长度合理 |
| `CasualChatResponder._system_prompt()` | `src/fu_gm/casual_chat.py` | 需要另行检查体积，但职责比主 prompt 清楚 |

## 已有优势

项目已经具备很好的缓存工程基础：

1. `src/fu_gm/prompt_cache.py` 已有 `SYSTEM_PROMPT_DYNAMIC_BOUNDARY`。
2. 动态内容通过 `<system-reminder>` 包进 user message，而不是直接改 system prompt。
3. `build_cache_friendly_messages()` 已经能维持静态 system prompt 前缀稳定。

也就是说，优化不需要推倒重来。主要工作是把静态 prompt 瘦下来，并让动态规则片段进入 reminder 或 user payload。

## 推荐目标结构

### 1. 全局静态底座：`CORE_GM_CONTRACT`

建议控制在 800-1,500 中文字。

只保留所有调用都必须遵守的内容：

- 你是《最终物语》AI GM 时悠。
- 遵守安全边界、界限与帷幕。
- 不泄露 GM 私密暗线、后台字段、测试标签、规则实现细节。
- 玩家有主角能动性，GM 负责场景、NPC、压力和裁定。
- 硬规则由 Python 落地，LLM 不自行改 HP/MP/金币/命刻/状态等数值。
- 动态世界状态以本轮 payload/reminder 为准。

不放完整战斗、旅行、角色创建、项目、仪式、经验、装备表。

### 2. Action Brain：`ACTION_ROUTER_CONTRACT`

建议控制在 5,000-8,000 字符，再按需注入规则片段。

保留：

- action_type 枚举和 JSON 输出契约。
- action 选择边界：Attack/Spell/Objective/Narrate/AdvanceClock/PlanRitual 等。
- 玩家输入不能直接变成事实的原则。
- 冲突中非当前行动者只缓存，不代操。
- 第零章/冒险/冲突/地下城/仪式/项目的高层分流规则。

移出：

- 大段完整规则解释。
- 细粒度规则数值表。
- 已由 Python validator 严格处理的字段细节。

按需注入：

- 当前处于冲突时注入“冲突规则片段”。
- 玩家提到仪式时注入“仪式片段”。
- 地下城状态存在时注入“地下城片段”。
- 第零章阶段时不应调用 Action Brain 或只注入冒险接管片段。

### 3. Session 0：`SESSION_ZERO_CONTRACT`

建议控制在 6,000-10,000 字符。

保留：

- 开始前共识：基调、主题、安全、队伍关系。
- 创建世界：形状、地图、魔法科技、国家、历史、奥秘、威胁。
- 创建队伍与角色：身份、主题、故乡、2-3 职业、5 级、属性、装备、外观。
- 写入规则：玩家确认后写入；不确定时询问；闲聊可静默。
- 不机械推进：只有停滞时才轻提醒。

移出：

- 战斗完整规则。
- 地下城/项目/旅行/经验/英雄技能细节。
- 大量“怎样才是好 GM”的散文式描述。

### 4. Expressor：`EXPRESSOR_CONTRACT`

建议控制在 1,500-3,000 字符。

表达器是最应该瘦身的模块。它不该知道完整规则，只需要知道：

- 系统给出的规则面板是权威，必须保留。
- 只允许补 0-2 句叙事或桌边短评。
- 不写骰子、数值、规则解释、后台标签。
- 不复述玩家动作。
- 成功写“世界如何回应成功”，失败写“阻力/代价/错失”。
- 不泄露私密暗线，不输出“可互动焦点”“规则层处理”“共同创作固定”等后台话术。

当前 `EXPRESSOR_SYSTEM_PROMPT` 带完整核心规则，是“人机味外露”的主要诱因之一。

### 5. NPC Act：`NPC_ACT_CONTRACT`

建议控制在 3,000-5,000 字符。

保留：

- 只输出 `NPCAct` JSON。
- 使用 NPC 人设、目标、禁忌、战况做选择。
- 不编造不在场角色。
- 反派可通过动作、压力命刻、蓄力、妨碍表达意图。
- NPC 可以推进威胁命刻，不能替玩家推进目标/仪式命刻。

移出：

- 第零章规则。
- 角色创建与装备经济细节。
- 旅行、地下城完整规则，除非当前 NPC 行动确实相关。

## 建议的文件拆分

建议新建或重构：

- `src/fu_gm/prompt_contracts.py`
- `src/fu_gm/prompt_snippets.py`
- `src/fu_gm/prompt_budget.py`

候选结构：

```python
CORE_GM_CONTRACT = "..."
ACTION_ROUTER_CONTRACT = CORE_GM_CONTRACT + "..."
SESSION_ZERO_CONTRACT = CORE_GM_CONTRACT + "..."
EXPRESSOR_CONTRACT = CORE_GM_CONTRACT + "..."
NPC_ACT_CONTRACT = CORE_GM_CONTRACT + "..."

RULE_SNIPPETS = {
    "conflict": "...",
    "clock": "...",
    "ritual": "...",
    "project": "...",
    "dungeon": "...",
    "travel": "...",
    "character_creation": "...",
}
```

Action Brain 和 Session 0 根据当前面板选择 snippet，作为 `build_cache_friendly_messages(... reminders=...)` 的动态内容注入，而不是拼进静态 system prompt。

## 哪些内容不应继续放 system prompt

1. 当前场景、地图、角色草稿、最近聊天记录、命刻状态。
2. 章节包、长期节奏、反派暗线、场景框架。
3. 技能表、装备表、法术表、NPC 图鉴全文。
4. “刚修过的 bug 的具体补丁描述”，例如不要复述玩家、不要输出下一位行动者等。这类应尽量由测试和 sanitizer 兜住，只在对应 contract 中保留一句原则。
5. 供应商、API、调试状态、异常恢复细节。

## 缓存布局建议

推荐消息顺序：

1. 静态 system：短 `*_CONTRACT` + `SYSTEM_PROMPT_DYNAMIC_BOUNDARY`。
2. user 前部 reminders：规则片段、当前状态、角色表、场景框架、记忆召回。
3. user 主体：本轮玩家消息和游戏面板。

这样缓存层更稳定：

- 跨会话可复用：短核心契约。
- 会话内较稳定：角色/世界/场景框架 reminder。
- 每轮变化：玩家输入、当前行动者、近期消息。

## 实施顺序

### 阶段 0：加预算审计，不改行为

新增一个轻量测试或脚本，输出 prompt 字符数，并设置软预算：

- Expressor < 4,000 字符
- NPC Act < 7,000 字符
- Action Brain < 12,000 字符
- Session 0 < 12,000 字符

先只生成报告，不立刻 fail，避免影响当前开发。

### 阶段 1：先瘦 Expressor

风险最低，收益最高。

它只负责表达，且已有规则面板和 sanitizer。把完整规则底座移除后，重点回归：

- 不改规则面板。
- 不复述玩家。
- 不输出后台话术。
- 成败表达要贴合规则面板。

预期可从 16.6k 字符降到 2-3k 字符。

### 阶段 2：瘦 NPC Act

保留战术与人设契约，删除完整核心规则。

预期可从 17.6k 字符降到 4-6k 字符。

### 阶段 3：拆 Action Brain

这是风险最高的一步，建议逐项拆：

1. 保留 action schema 与分流规则。
2. 把命刻、冲突、仪式、项目、地下城拆成 snippets。
3. 根据当前 `GamePanel` 和玩家输入选择 1-3 个片段注入。
4. 长测确认没有出现错误 action、仪式/命刻/DL 退化、回合代操。

预期从 25.5k 字符降到 8-12k 静态，加 1-4k 动态片段。

### 阶段 4：拆 Session 0

把完整规则底座换成第零章专用契约。

重点保留：

- 自由共创，不强迫三选一。
- 玩家确认后写入。
- 草稿查询/修改 skill。
- 闲聊静默。
- 进入第一章必须明确确认。

预期从 25.8k 字符降到 8-12k 字符。

## 必须保留的测试护栏

每阶段至少跑：

- `tests/test_prompts.py`
- `tests/test_expressor.py`
- `tests/test_action_brain_boundary.py`
- `tests/test_message_arbiter.py`
- `tests/test_session_zero.py`
- `tests/test_http_server.py`
- `tests/test_long_run_regressions.py`

长测要重点看：

1. 是否还输出后台标签、流程标签、玩家不可见说明。
2. 是否复述玩家上一句话。
3. 是否在玩家未确认时写入共识。
4. 是否未经明确确认进入第一章。
5. 冲突中是否代操、劫持当前行动者、强行协助。
6. 命刻是否持续存在，但不会刷屏。
7. NPC 是否能明确回应玩家，而不是只说“他等待答复”。

## 预期收益

| 模块 | 当前字符数 | 目标字符数 | 预估下降 |
| --- | ---: | ---: | ---: |
| Expressor | 16.6k | 2-3k | 80% 左右 |
| NPC Act | 17.6k | 4-6k | 65% 左右 |
| Action Brain | 25.5k | 8-12k 静态 | 50-65% |
| Session 0 | 25.8k | 8-12k | 50-65% |

行为收益比 token 节省更重要：

- 表达器更少“人机味”。
- Action Brain 更不容易把后台规则念给玩家。
- Session 0 更少机械提醒和流程复读。
- 更容易定位 bug 是 prompt 问题还是代码状态机问题。

## 不建议的做法

1. 不建议只让 LLM “帮忙压缩现有 prompt”。这会把结构问题压成更难维护的一团文本。
2. 不建议把完整规则表放进全局 system prompt。规则表应作为检索/片段/工具返回内容。
3. 不建议用输出过滤代替 prompt 治理。过滤可以兜底，但源头仍应减少会诱发外露的话术。
4. 不建议一次性改完四个核心 prompt。最好先 Expressor，再 NPC Act，再 Action Brain，最后 Session 0。

## 下一步建议

优先执行“阶段 0 + 阶段 1”：

1. 添加 prompt 预算审计测试/脚本。
2. 新建 `EXPRESSOR_CONTRACT`，让表达器不再继承完整核心规则。
3. 跑表达器与长测片段，确认不再输出“可互动焦点”“规则层处理”“共同创作固定”等后台句式。

如果阶段 1 稳定，再继续拆 NPC Act 和 Action Brain。

## 本轮已执行

本轮没有加入硬性预算测试，而是先完成了运行时静态 prompt 的职责拆分：

1. 保留 `FABULA_ULTIMA_CORE_SYSTEM_PROMPT` 作为完整规则参考与兼容常量。
2. 新增短核心契约 `CORE_GM_CONTRACT`，作为 Action Brain、Expressor、NPC Act、Session 0 的共同静态前缀。
3. 新增按岗位使用的规则摘要：
   - `FABULA_ULTIMA_RULES_BRIEF`
   - `ACTION_BRAIN_RULES_BRIEF`
   - `EXPRESSOR_RULES_BRIEF`
   - `NPC_ACT_RULES_BRIEF`
   - `SESSION_ZERO_RULES_BRIEF`
4. 运行时 prompt 不再默认继承完整规则书底座：
   - `ACTION_BRAIN_SYSTEM_PROMPT` 约 10,995 字符。
   - `EXPRESSOR_SYSTEM_PROMPT` 约 1,521 字符。
   - `NPC_ACT_SYSTEM_PROMPT` 约 2,981 字符。
   - `SESSION_ZERO_SYSTEM_PROMPT` 约 11,302 字符。
5. 更新 `tests/test_prompts.py`，明确保护新结构：运行时 prompt 应以短核心契约开头，而不是完整规则书底座。

已验证：

```bash
.venv/bin/python -m pytest tests/test_prompts.py tests/test_expressor.py tests/test_action_brain_boundary.py tests/test_message_arbiter.py tests/test_session_zero.py tests/test_http_server.py -q
```

结果：214 passed。
