# 当前工作区改动审计（2026-06-26）

本文件记录当前工作区相对 `HEAD` 的未提交改动分组，方便后续审查、拆提交和回滚定位。它不是功能说明书，而是“我们现在手上有哪些变化”的索引。

## 总览

- 已修改文件：43 个。
- 未跟踪新文件：3 个。
- 当前完整测试：`508 passed`。
- 注意：`integrations/nortantis/gradlew` 显示修改但无文本差异，主要是执行权限变化。

## 未跟踪新文件

- `docs/core_rulebook_page_by_page_audit_2026-06-23.md`
  - 规则书逐页审计资料。
  - 风险：低，文档类。
- `src/fu_gm/components/skill_trigger_manager.py`
  - 技能自动触发/被动修正管理器。
  - 风险：中到高，规则结算核心扩展。
- `src/fu_gm/optional_rules.py`
  - 可选规则状态与仪表盘展示辅助。
  - 风险：中，影响 Session 0 共识与审计展示。

## 功能分组

### 1. 冲突回合与回合外意图收件箱

涉及文件：

- `src/fu_gm/components/conflict_manager.py`
- `src/fu_gm/scene_orchestrator.py`
- `src/fu_gm/expressor.py`
- `tests/test_adventure_flow.py`
- `tests/test_long_run_regressions.py`

主要内容：

- 回合外玩家行动不再被强行改写为当前行动者的动作。
- 插队行动会进入“回合外意图收件箱”。
- 轮到对应角色时，GM 会提示缓存行动，玩家可确认执行或改行动。
- 玩家可见输出使用中文属性和“难度等级”。

优先审查点：

- 群昵称与角色名映射是否足够稳定。
- 缓存行动是否会在玩家换行动时被合理覆盖。
- NPC 回合自动推进是否仍会造成死锁或过快跳过戏剧机会。

### 2. 规则结算与技能触发

涉及文件：

- `src/fu_gm/interceptor.py`
- `src/fu_gm/components/rules_engine.py`
- `src/fu_gm/components/skill_trigger_manager.py`
- `src/fu_gm/components/ritual_manager.py`
- `src/fu_gm/components/progression_manager.py`
- `src/fu_gm/components/rest_manager.py`
- `tests/test_rules_engine.py`
- `tests/test_progression_manager.py`

主要内容：

- 明确拒绝无效难度等级，避免把 `0` 静默改成默认值。
- 技能触发、资源、休息、进度与仪式相关规则有扩展。
- 错误恢复从 debug 风格改为玩家可读的澄清提示。

优先审查点：

- 技能触发是否过度自动化，是否侵犯 GM/LLM 的叙事裁量。
- 无效难度等级拦截是否在 AstrBot/QQ 场景中足够柔和。
- 仪式和项目是否仍保持“LLM 创意、Python 结算”的边界。

### 3. Session 0、共识与角色创建

涉及文件：

- `src/fu_gm/components/session_zero_manager.py`
- `src/fu_gm/session_zero_facilitator.py`
- `src/fu_gm/pre_session_consensus.py`
- `src/fu_gm/session_zero_cli.py`
- `src/fu_gm/components/world_state.py`
- `src/fu_gm/models.py`
- `tests/test_session_zero.py`

主要内容：

- 游戏开始前共识、世界创建、角色创建流程更接近规则书。
- 增加可选规则、界限与帷幕、世界卡信息和角色草稿的记录/校验。
- 世界创建不应被硬性三选一或预设回复锁死。

优先审查点：

- GM 是否仍会过度回复玩家闲聊。
- 角色草稿是否只在必要时展示，不原样泄露。
- 世界创建完成与地图生成的触发时机是否自然。

### 4. HTTP 服务、仪表盘与 AstrBot 协作边界

涉及文件：

- `src/fu_gm/http_server.py`
- `src/fu_gm/message_arbiter.py`
- `src/fu_gm/config.py`
- `tests/test_http_server.py`
- `tests/test_llm_integration.py`

主要内容：

- 仪表盘新增更多审计信息，包括规则覆盖、记忆、日志、运行状态。
- 消息路由支持更语义化的 FU-GM/AstrBot 分流。
- 规则错误以友好提示返回，不外泄内部参数。

优先审查点：

- 仪表盘是否实时显示当前存档与当前会话，而不是旧存档。
- 语义路由是否会增加延迟。
- 群聊普通水群、跑团发言、私聊安全声明是否能正确分流。

### 5. 规则资料、提示词与中文化

涉及文件：

- `src/fu_gm/prompts.py`
- `src/fu_gm/skill_library.py`
- `src/fu_gm/core_bestiary.py`
- `src/fu_gm/equipment_catalog.py`
- `src/fu_gm/components/adventure_event_manager.py`
- `src/fu_gm/components/gadget_manager.py`
- `tests/test_skill_library.py`

主要内容：

- 技能、怪物、装备和提示词继续向规则书原文靠拢。
- 玩家可见文本尽量使用“敏捷/洞察/力量/意志”和“难度等级”。
- 内部仍保留 `DEX/INS/MIG/WLP` 作为稳定数据键。

优先审查点：

- 技能库是否还有旧译名或错误效果。
- 装备外观与数值模板是否保持“可换皮、不锁死”。
- 提示词中是否还有诱导 LLM 过度结算或硬编码流程的语句。

### 6. 地图与长跑测试脚本

涉及文件：

- `src/fu_gm/components/map_renderer.py`
- `scripts/run_ultra_from_scratch_campaign_test.py`
- `tests/test_image_generation.py`

主要内容：

- 地图生成时机、Nortantis 输出、长跑测试覆盖有所调整。
- 长跑脚本用于从 Session 0 到第一章、冲突和日志审计的真实服务边界验证。

优先审查点：

- 地图是否只在世界创建完成或进入第一章前生成。
- 地图生成中的内部状态是否不会发给玩家。
- 长跑测试是否能抓到“测试通过但体验卡死”的假阳性。

## 建议拆分提交顺序

1. 文档和审计资料。
2. Session 0 与共识流程。
3. 规则资料与技能库中文化。
4. 规则结算和技能触发管理器。
5. 冲突回合、回合外意图收件箱和错误恢复。
6. HTTP 仪表盘、消息路由和 AstrBot 协作。
7. 长跑测试脚本与测试补强。

## 当前复测基线

已执行：

```bash
.venv/bin/python -m pytest -q
```

结果：

```text
508 passed
```

下一步应执行一轮长跑回归，并人工检查输出是否存在：

- 回合死锁或 NPC 卡死。
- 机会偏好粘滞复读。
- 插队行动被强行代操。
- `DL`、`DEX/INS/MIG/WLP` 等玩家可见缩写泄露。
- 地图生成提示破坏沉浸。
- 仪表盘没有同步当前存档/当前会话。
