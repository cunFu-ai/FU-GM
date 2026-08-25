# FU-GM 模型职责分配

## 分工原则

FU-GM 不用写作模型直接修改规则状态。每条消息仍按以下链路处理：

1. 核心 GM 理解玩家意图，选择工具并提交结构化语义。
2. Python 校验权限、参与者、骰子、资源、命刻格数、回合与事实边界。
3. 核心 DeepSeek 在结构化循环内直接形成普通最终回复；需要长篇场景创作时，
   专用 DeepSeek 作者只根据已授权数据生成私密局面或玩家可见文字。
4. Python 再次检查必需字段、公开事实、人物名单、玩家自主权与后台泄露，通过后才原子写入。

第一章邀请成功提交后，`optimized` 流程会在后台、脱离玩家关键路径生成私密场次契约。
后台任务只可写入带精确输入指纹的私密缓存，不能提前切换会话、建立场景、注册 NPC 或
改变角色资源。玩家明确同意时，核心 GM 只需选择一次 `start_adventure`；Python 校验邀请、
同意证据和缓存指纹后，在同一可回滚事务中完成 `start_session` 与 `start_scene`。缓存失效、
质量降级或准备未完成时不会冒充命中，而是回到受截止时间约束的前台准备。

完整的时悠人格文档直接进入核心 GM 的初始决策和工具后决策。普通回复不再经过外层
`LLMExpressor.render_agent_message()` 二次改写；显式 `FU_GM_PUBLIC_EXPRESSION_MODE=expressor`
只作为回滚兼容。`SceneCreativeWriter` 的文字有时会作为锁定回复直接送达玩家，因此它仍保留
短小的场景写作与事实边界契约，但不接收完整人格。

## 当前职责表

| 任务 | 默认模型/组件 | 理由 |
| --- | --- | --- |
| 消息意图、是否回复、工具选择、失败后重试、普通最终回复 | 核心 GM（DeepSeek V4 Flash Vision Exp，Thinking off） | 一次结构化循环内完成决策与公开成品，省去外层改写 |
| 规则、骰子、伤害、资源、命刻、回合和状态写入 | Python | 权威真值不交给生成模型 |
| 场次契约的具体化、暗线、线索路径和场景机会 | DeepSeek 创作作者 | 属于长程写作和戏剧结构 |
| 场次契约是否真的可玩、条件是否可达 | DeepSeek 只读审查（Thinking off） | 是语义验证，不是创作；失败时使用本地确定性兜底 |
| 场景私密局面、开场、转场、冲突开场 | DeepSeek 创作作者 | 需要连贯的氛围、意象与行动交接 |
| 普通环境回应、NPC 首次登场、NPC 战斗起手 | DeepSeek 创作作者 | 都是“已决定事实的玩家可见表现” |
| 命刻建立、变化、逼近填满和结案的氛围文字 | DeepSeek 创作作者 | Python 先决定格数、原因和后果，DeepSeek 只表现压力 |
| 场景、冲突和场次收束 | DeepSeek 创作作者 | 需要回收意象且不改写结果 |
| NPC 立场、知识边界、条件和承诺 | 核心 GM（DeepSeek V4 Flash Vision Exp） | 属于事实与权限决策，仍受 Python 状态约束 |
| NPC 台词声线 | DeepSeek `NPCVoiceRenderer` | 只改口吻，不改结构化内容 |
| NPC 战斗卡和合法行动设计 | DeepSeek + Python | 模型提出结构化候选，Python 编译并校验规则合法性 |
| 场次总结、存档摘要 | DeepSeek 总结器（Thinking off） | 要求保真和可检索，不做文学化改写 |
| 地图坐标、位置关系、角色卡、读档与状态查询 | 核心 GM + Python 工具 | 属于结构化资料与事务，不走创作作者 |

## 运行配置

主线把所有语言职责锁定到 DeepSeek 官方接口，并显式关闭 Thinking：

```env
FU_GM_API_BASE_URL=https://api.deepseek.com
FU_GM_ACTION_MODEL=deepseek-v4-flash-vision-exp
FU_GM_CORE_GM_MODEL=deepseek-v4-flash-vision-exp
FU_GM_TOOL_AGENT_MODEL=deepseek-v4-flash-vision-exp
FU_GM_TOOL_PROTOCOL_REPAIR_MODEL=deepseek-v4-flash-vision-exp
FU_GM_REPLY_GROUNDING_MODEL=deepseek-v4-flash-vision-exp
FU_GM_CREATIVE_API_BASE_URL=https://api.deepseek.com
FU_GM_CREATIVE_MODEL=deepseek-v4-flash-vision-exp
FU_GM_NPC_DESIGN_MODEL=deepseek-v4-flash-vision-exp
FU_GM_NPC_VOICE_MODEL=deepseek-v4-flash-vision-exp
FU_GM_THINKING_ENABLED=false
FU_GM_CORE_GM_THINKING=off
FU_GM_CREATIVE_THINKING=off
FU_GM_NPC_VOICE_THINKING=off
FU_GM_PUBLIC_EXPRESSION_MODE=core
FU_GM_EXPRESSOR_RULE_RESULT_PROSE_ENABLED=0
FU_GM_ADVENTURE_OPENING_FLOW_MODE=optimized
FU_GM_ADVENTURE_OPENING_PREFETCH_TIMEOUT_SECONDS=65
FU_GM_CAPABILITY_ROUTING_MODE=baseline
FU_GM_STATE_CONTEXT_MODE=summary_delta
FU_GM_BACKUP_API_BASE_URLS=
```

核心结构化输出、场次准备、场景作者和语义审查均使用有界共享截止时间；HTTP 2xx 空正文时，
最多进行一次取消 `response_format` 的恢复请求，随后由上层失败关闭或使用明确的本地兜底。
仪表盘的“模型与路由”会分别显示核心 GM、场次/暗线/开场作者、NPC 台词、NPC 规则卡与总结器的实际模型和端点。
