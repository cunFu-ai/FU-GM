# FU-GM 单智能体工具架构

日期：2026-07-23

## 目标

时悠直接阅读玩家本轮原话、最近公开聊天和权威游戏状态，自主选择静默、自然回答、查询资料或调用类型化工具。规则层只验证工具格式、对象权限、当前阶段、客观状态和《最终物语》规则，不再用关键词或正则重新解释玩家意图。

一项变化只有在工具成功回执提交后才成为游戏事实。失败回执不能被表达文本伪装成成功，模型也不能在没有工具回执时声称已经存档、读档、建卡、推进命刻或改变人物状态。

## 唯一在线数据流

```text
AstrBot / HTTP
  -> GMMessageEnvelope
       current_message：本轮玩家原话
       recent_public_context：最近公开聊天
       transport metadata：艾特、引用、私聊、附件等平台事实
  -> GMNaturalMessageRouter
  -> GMAgentMessageCoordinator
       构造权威状态快照
  -> LLMGMToolAgent（唯一主持语义决策者）
       silent / ask_user / final / call_tool
       可连续调用多个类型化工具
  -> GMToolProtocol + GMToolExecution
       schema、权限、阶段、客观规则与事务验证
  -> 领域工具
       成功：原子提交并返回结构化回执
       失败：不改状态，返回可修正错误
  -> 智能体根据回执继续调用、追问或结束
  -> 日志、仪表盘与 AstrBot 回复
```

所有自然语言入口最终都是上述入口的别名：

- `/v1/message/route`
- `/v1/chat`
- `/v1/game/turn`
- `/v1/game/scene-opening`
- `/v1/game/gm-beat`
- `/v1/session-zero/message`

斜杠命令属于传输协议，不进入自然语言语义判断。核心智能体未配置或提供商失败时，事务失败关闭；不会转入关键词路由器、Action Brain 或启发式 GM。

## 规则行动

```text
LLMGMToolAgent
  -> 类型化玩法工具
  -> StructuredTurnExecutor
       玩家本轮原话与最近公开上下文分开传递
  -> ActionInterceptor / 窄规则协调器
       DecisionWindow
       检定事务
       回合外意图收件箱
       完整行动轮
  -> ResolvedTurnPublisher
       提交结果
       发布玩家可见回复
       用本轮原话记录场次证据
```

`ResolvedTurnPublisher` 不从拼接聊天记录中抽取或猜测当前玩家消息。`current_message` 从消息信封一直原样传到场次追踪器；最近聊天只用于指代理解、记忆召回和表达承接。

## NPC

普通 NPC 交互和战斗 NPC 均有独立的类型化决策阶段，但它们是核心 GM 调用的下属能力，不是第二个主持入口。

- NPC 档案保存身份、当前目标、知识边界、动机、承诺、说话方式、技能和战斗能力。
- 集体 NPC 使用显式 `entity_kind=collective`，不再根据名称猜测。
- NPC 决策只能读取当前场景、公开事实、私有档案和规则层提供的合法行动清单。
- NPC 模型失败或返回非法行动时事务中止，不调用启发式选择器补做决定。
- 规则目录只生成合法行动和战术状态，不拥有在线行动决定权。
- 命刻未完成时，NPC 不得提前宣布完成后果、让远方威胁抵达或把尚未发生的封锁写成事实。

## 模块边界

- `gm_message_envelope.py`：保存原始消息与可信平台元数据，不分类意图。
- `gm_natural_message_router.py`：唯一自然语言入口、幂等与失败关闭。
- `gm_agent_message_coordinator.py`：状态快照、单次智能体事务、日志和节奏证据。
- `gm_tool_agent.py`：唯一主持语义循环，不直接修改领域状态。
- `gm_tool_contracts.py`：无 LLM/HTTP 依赖的 schema、上下文和回执。
- `gm_tool_protocol.py`：JSON 协议与语法修复；不改写语义。
- `gm_tool_execution.py`：调用账本、限次、事务和回执。
- `gm_*_tools.py`：战役、第零章、场景、命刻、NPC、玩法、运行时、旅行与资料能力。
- `structured_turn_executor.py`：执行已经由智能体明确选择的类型化动作。
- `resolved_turn_publisher.py`：提交并发布已经结算的结果，不解释意图。
- `npc_decision_planner.py` / `LLMNPCDirector`：核心 GM 调用的 NPC 下属决策器。

`scene_orchestrator.py` 和 `interceptor.py` 仍承载成熟领域规则，但没有自然语言路由权。新增语义能力必须进入核心智能体和类型化工具；新增硬规则应进入窄规则组件，而不是恢复 `_recover_*` 或关键词特判。

## 不属于第二个 GM 的辅助组件

- `Expressor` 的确定性渲染只格式化已经结算的规则数据，不能选择行动或写入状态。
- `StorySummarizer` 只压缩已经记录的日志，不能成为行动证据或修改游戏事实。
- FU-PL 和 rules-only 测试桩只用于测试，不接入真实 AstrBot 消息入口。

即使这些辅助组件有离线降级能力，核心 GM 与 NPC 决策仍始终失败关闭。

## 强制不变量

1. 本轮玩家原话是唯一当前语义证据；最近聊天不能被偷换成玩家本轮已完成的行动。
2. 生产自然语言入口只有一个核心工具智能体。
3. 规则层不能用正则或关键词重判存档、角色、命刻、NPC 对话或行动意图。
4. 只有成功工具回执可以改变状态。
5. 工具未知、字段非法、对象缺失、权限错误和规则前置条件不满足时必须在副作用前拒绝。
6. 多人缓冲只合并回复时机，不合并发言者或行动归属。
7. 私有暗线、NPC 秘密和后台提示不得进入公开回复。
8. 玩家行动、NPC 决定、规则结算与公开表达互不越权。
9. 核心模型和 NPC 模型失败时安全停止，不调用第二个语义决策器。
10. 命刻、待决窗口、临时效果和场景事实必须遵循各自生命周期。

## 已删除的旧架构

以下模块和入口已经删除，而不是保留为兼容路径：

- `action_brain.py`
- `message_arbiter.py`
- `session_zero_facilitator.py`
- `DecisionResponseRouter`
- `PendingDecisionMessageRouter`
- `GMBeatGenerationPolicy`
- `SceneOrchestrator.run_turn`
- `SceneOrchestrator.run_gm_beat`
- `SceneOrchestrator.run_scene_opening`
- `LLMActionBrain.answer_npc`

旧客户端必须迁移到统一消息入口或直接调用类型化工具，不能要求项目恢复第二套自然语言解释链。

## 当前验证

- 2026-07-24 全量自动化回归：`1411 passed`。
- 架构测试覆盖单一入口、旧模块缺失、工具事务、多人身份保留、核心/NPC 无次级语义决策器，以及玩家原话的端到端直传。
- 2026-07-24 的 20 场 rules-only 连续性测试完成 20 次开场与收团、316 次类型化工具事件、20 次经验结算和 50 次角色升级调用；没有残留场景、冲突、阻塞窗口或场景/场次命刻。
- 第 5、10、15、20 场各建立一个行动轮自动命刻，均只在五名玩家角色全部行动后从 `0/6` 推进到 `1/6`。
- AstrBot bridge 在第零章后和战役结束后分别进行了隔离 HTTP 冒烟，均保持主战役状态不变。
- 2026-07-24 的全新 20 场真实 LLM 长测在首个语义请求前被供应端以 HTTP 403 `INSUFFICIENT_BALANCE` 拒绝。该结果只能归类为外部账户阻断，不能证明或否定 Luna 的主持能力。
- 下一次真实 LLM 测试必须从干净战役开始，单独审计静默、NPC 一致性、剧情承接、玩家自主性、每场记忆点及 P50/P95 延迟；rules-only、额度失败或未完成检查点都不能计为真人语义测试通过。
