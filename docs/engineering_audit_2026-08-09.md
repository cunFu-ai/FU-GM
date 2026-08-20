# FU-GM 工程审计报告（2026-08-09）

## 1. 审计结论

当前仓库具备较强的规则回归基础，Python 硬规则、LLM 创意层和独立地图渲染器的职责边界总体清楚；修改前完整基线为 `2249 passed`。本轮没有发现已提交的真实 API 密钥、TLS 校验关闭、常规文件路径穿越或 shell 命令注入。

需要优先处理的风险集中在四处：

1. 私聊安全声明的匿名性没有由系统端到端强制，且私聊 transcript 被误标为公开记录。
2. 本地 HTTP 管理接口没有认证；一旦绑定到非回环地址，会直接暴露私密审计和战役写操作。即使只绑定回环地址，也需要防范浏览器跨源请求。
3. GM dashboard 把部分未转义字段写入 `innerHTML`，存在同源 DOM XSS 链。
4. 单端点 LLM 调用忽略“单次尝试超时”，会把一次故障放大到整段事务预算，并持续占用同战役锁。

本轮只实施不改变规则语义、存档格式或外部协议的低风险修复。认证协议、存档 ID 迁移、锁与事务层合并、巨型模块拆分、依赖升级及许可证选择均保留为待确认项。

## 2. 范围、方法与基线

- 源码快照：`codex/shenji@df63058`。
- 审计开始时工作树已有 49 个修改文件及若干未跟踪开发文件；这些都视为用户现有工作并原样保留。
- 已枚举全部 Git 跟踪文件及目录；完整阅读 Python 主运行时、配置、启动脚本、README、关键测试和集成入口。对 `.runtime/`、字体、JAR、图片等生成物或二进制资产只做清单、体积、来源和抽样检查，不把运行时副本当作源码重复审阅。
- 权威规则书：`最终物语-核心规则书-FU-CoreRulebook-SCN-v.1.03-0930带书签1.pdf`，SHA-256 为 `643095710126d48b569f8b441daa7e0325b6b8362f0a614de324fdfb51f7ed5e`。重点核对了检定、大成功/大失败、特质重掷、命刻、冲突轮次、HP/MP 与危机、零 HP、伤害相性、异常、物语点、反派、物资点、旅行以及 NPC/Boss 设计章节。
- 规则核对结论：当前双骰检定、危机阈值、相性、异常骰阶下调、命刻、冲突轮次和玩家零 HP 选择等 Python 权威规则与规则书相符。NPC 零 HP 的玩家命运选择等属于项目的 AIGM 适配，未在没有确认时改动。
- 修改前验证：
  - `PYTHONPATH=src .venv/bin/python -m pytest -q -p no:cacheprovider`：`2249 passed in 20.84s`。
  - Python `compileall`、`git diff --check`、`zsh -n scripts/*.sh`、`plutil -lint launch_agents/*.plist`：通过。

## 3. 按严重度排序的发现

### 严重 / 高

#### H-01 私聊匿名安全边界未端到端强制

- 证据：`integrations/astrbot/fu_gm_bridge/main.py` 的桥接载荷携带真实 `speaker`/`speaker_id`；`gm_message_envelope.py` 保留身份元数据；`gm_session_zero_tools.py` 把 `anonymous` 交给模型可选，缺省为 `False`；`safety_manager.py` 在非匿名时把说话者写入持久记忆。
- 影响：模型漏填一个可选字段即可把私聊安全声明和真实身份永久关联；身份也可能进入日志、dashboard、摘要和后续上下文。
- 处置：本轮修复。由 `context.is_private` 强制匿名，私聊日志使用私有 role、稳定化名和去标识元数据；桥接和消息信封补齐可信私聊标志。旧日志不自动猜测或改写。

#### H-02 私聊 transcript 被标成公开记录

- 证据：`GMAgentMessageCoordinator` 已收到 `is_private`，但 `_append_audit_log()` 没有接收它；日志只按路由目标选择 `user`、`table_talk` 或 `assistant`。公开摘要、实时上下文和 dashboard 只按 role 排除 `private`/`gm_private`/`system_private`。
- 影响：私聊原文和身份可进入公开摘要、实时群聊上下文及默认 dashboard。
- 处置：与 H-01 一并修复并补端到端回归。

#### H-03 HTTP 管理面无认证，回环地址也缺少跨源请求约束

- 证据：`http_server.py` 的所有读写路由都没有 Authorization、Origin 或 Host 校验；`include_private=true`、导入、调用模型、保存和删除都可直接访问。
- 影响：绑定 `0.0.0.0` 或经反向代理暴露时成为未认证远程管理接口。默认 `127.0.0.1` 降低远程风险，但浏览器简单请求仍构成条件性 CSRF 面。
- 处置：本轮只做兼容性低风险的 `application/json` 强制、请求体上限和安全响应头；完整 bearer token、Origin/Host 策略与 AstrBot 配置变更需用户确认。README 明确禁止无鉴权公网暴露。

#### H-04 GM dashboard 存在 DOM XSS

- 证据：dashboard 的 `row()` 只转义标题，正文直接进入 `innerHTML`；战役 ID、场次、阶段、保存路径等部分字段未经过 `esc()`。
- 影响：恶意战役名或持久文本可在同源 dashboard 执行脚本，继而读取私密审计或调用写接口。
- 处置：本轮修复。区分纯文本行与已审查 HTML 行、补恶意 ID 回归，并增加 `nosniff`/防嵌入响应头。

#### H-05 单端点调用忽略每次尝试超时

- 证据：`llm_client.py` 只有在备用端点数量大于零时才应用 `endpoint_attempt_timeout_seconds`。离线 transport 复现为单端点配置 1 秒、实际转发 30 秒。
- 影响：单端点挂起可耗尽 90 秒总预算，重试和恢复没有剩余时间，同时阻塞同战役后续消息。
- 处置：本轮修复并补单端点 timeout 转发测试。

#### H-06 在线探针产物未被 Git 忽略

- 证据：未跟踪 `artifacts/` 中现有在线探针 JSON 含 `raw`、`text`、`agent_trace`、`authoritative_state` 等字段，而 `.gitignore` 没有该目录。
- 影响：误提交会泄露提示词、玩家文本、模型原始输出和战役状态。
- 处置：本轮只增加忽略规则，不删除现有文件。

#### H-07 macOS 部署链硬编码本机用户名与路径

- 证据：`run_fu_gm_http.sh`、`install_fu_gm_launch_agent.sh` 和 plist 中固定了当前用户的绝对目录；`docs/CODEX_HANDOFF.md` 已把它列为待修项。
- 影响：换用户名、换目录或对外分发会失败；安装脚本包含运行时目录替换，不能在未验证目标路径时贸然泛化。
- 处置：本轮仅安全地移除直接启动脚本中的个人默认路径，并在 README 明确 LaunchAgent 安装器仍是本机部署模板。完整 plist 生成器需单独验证后再启用。

#### H-08 发布许可证与素材来源不完整

- 证据：根项目没有 `LICENSE`；Nortantis 子树为 AGPL-3.0 且保留了上游 commit，但字体和自定义图标没有来源/再分发许可说明。
- 影响：公开发行、整包分发或公开网络服务时许可边界不清楚。
- 处置：不自动选择许可证或断言素材权利；需用户确认发布计划和素材来源。

### 中

#### M-01 HTTP 请求、上游响应和线程并发缺少资源上限

- HTTP handler 按任意 `Content-Length` 整块读取，未限制尺寸、编码或 JSON 顶层类型；`ThreadingHTTPServer` 无工作线程上限。LLM/图片响应和导入载荷也缺少完整尺寸上限。
- 本轮修复 HTTP JSON 请求体上限、格式和读取错误；线程池、LLM/图片响应限长及导入配额需结合真实载荷后确认。

#### M-02 LLM 遥测在并发空响应时会串线

- `_mark_last_call_empty()` 修改共享的 `recent_calls[-1]`，并发时可能把 A 的空响应记到 B；统计列表也没有统一锁。
- 本轮修复空响应记录的精确归属和失败计数，并补确定性交错测试；更大的遥测快照一致性后续再评估。

#### M-03 同战役锁覆盖完整 LLM、工具和地图 I/O

- 自然消息路由从去重到最终响应一直持有 campaign `RLock`；模型事务默认 90 秒，地图单次默认 180 秒并可能重试，后台线程 `join()` 也无超时。
- 影响：同团队头阻塞可能达到数分钟。
- 不直接删除锁。后续应先增加 `lock_wait_ms`/`lock_hold_ms`，再设计有界等待和地图异步完成协议。

#### M-04 写工具存在多层全量快照与重复全量保存

- 批次、消息级和工具级事务可形成约 `2N+1` 份快照；每份会读取完整 snapshot/events/memory；工具模块有大量自动保存调用，`events.jsonl` 也会全量重写。
- 影响：大团档在战役锁内出现 CPU、内存和磁盘写放大。
- 回滚正确性优先，本轮不合并事务。后续先加每层耗时/字节指标，再通过“已有外层快照”标记和无变化跳过逐步优化。

#### M-05 transcript 去重和上下文读取呈 O(n²) 增长

- 每次带 `message_id` 的追加都全量读取并解析 transcript；800 条离线写入约为 400 条的 3.65 倍，正常一轮还会重复读取。
- 后续可建立惰性 ID 索引和尾读 API；需要覆盖跨进程写入、重启和损坏日志，未在本轮仓促引入缓存。

#### M-06 依赖元数据无法复现完整测试环境

- `dependencies=[]` 且没有 test extra，但多个测试直接导入 `pytest`；README 的快速安装不足以运行全测。
- 本轮增加 `test` extra，并把 README 测试安装命令改为 `pip install -e ".[test]"`。

#### M-07 配置面分散且 dotenv 语义跨平台不一致

- 源码读取约百个 `FU_GM_*` 变量，多处直接 `int()`/`float()`；Python、PowerShell 和 zsh 对引号、变量展开及 shell 语法的处理不同；仓库缺少 `.env.example`。
- 本轮增加无密钥模板和 FAQ，明确 `.env` 只使用简单 `KEY=value`。集中解析器和兼容迁移不在本轮重写。

#### M-08 README、端口、安装位置和心跳默认值漂移

- README 错称 Session 0 CLI 可无密钥启发式回退和使用 `--offline`；实际 CLI 会失败关闭。
- 通用 HTTP 默认 8765，Windows AstrBot 安装器默认 8766；这可以保留，但必须说明安装器会同步插件配置。
- README 错称 Windows 默认写到 `%USERPROFILE%`，实际是项目 `.runtime`；Bridge README 的轻推次数也与 schema 不一致。
- 本轮同步文档，不强制迁移现有端口。

#### M-09 源码 wheel 不是自包含发行包

- wheel 只包含 `src` 下 Python 包，不包含 `config/`、字体、Nortantis 或 AstrBot 插件；地图资源依赖项目根目录。
- README 明确当前支持“源码检出 + editable install”。是否制作自包含 wheel 需确认。

#### M-10 Nortantis/Gradle 供应链未完整锁定

- wrapper 没有 `distributionSha256Sum`，没有 dependency verification/locking；子树含多份旧版 vendored JAR。
- 未联网核验 CVE，因此不宣称存在具体漏洞。依赖升级、锁文件和 SBOM 应在独立 Java 回归中处理。

#### M-11 战役/场次路径清洗存在碰撞和 Windows 保留名风险

- 非法字符统一替换为 `_`，`foo/bar` 与 `foo_bar` 可落到同一目录；`CON`、`NUL`、尾点等在 Windows 也会失败。
- 改映射会影响既有存档。本轮不迁移；后续应对新 ID 做严格校验，并为旧 ID 提供显式迁移。

#### M-12 日志、运行数据和错误详情的暴露面偏大

- transcript、模型原始输出和运行目录默认可为 `0644/0755`；500 响应直接返回 `str(exc)`；stdout 运维日志会污染脚本的 JSON 输出。
- 本轮把运维日志移到 stderr，并在 FAQ 提醒日志含敏感内容。统一 `0600/0700`、错误关联 ID 和旧权限迁移需用户确认。

### 低 / 可维护性

- `interceptor.py`、`gm_gameplay_tools.py`、`http_server.py` 和长测脚本均已达到数千至上万行，增加审查和合并成本。本轮不为重构而重构；只在以后修改相应职责时沿现有组件边界渐进抽取。
- 主项目缺少根级 CI、ruff/mypy、pre-commit 和 `.gitattributes`。当前工作树有大量既有修改，不做整树行尾归一化；后续可先加只跑 Python 3.9 的最小测试 CI。
- 单条损坏 JSONL 会让完整 transcript 读取失败。恢复策略涉及“忽略、隔离还是中止”的审计语义，本轮只在故障排查中说明，不静默丢弃记录。
- `FU_GM_PROMPT_CACHE_TTL` 当前解析后仍固定为 `30m`；需要明确受支持的上游 TTL 白名单后再修正。
- 导出提示词脚本和部分历史文档含本机路径或平台标识；若要公开仓库，应先确认哪些是固定审计夹具，再参数化或脱敏。

## 4. 已有的良好控制

- Python 保持数值、资源、生死、相性、状态、Boss 阶段和冲突状态的权威执行；LLM 主要选择结构化工具并负责叙事。
- LLM 重试共享单调时钟截止时间，已有端点故障转移、熔断器、提示词缓存、状态压缩和可审计 telemetry。
- 事务层虽然昂贵，但现有多层回滚和失败注入测试保护了状态一致性。
- `.env*`、日志、数据、构建目录大多已忽略；当前 `.env` 文件权限为 `0600`，Git 跟踪集和历史扫描未发现真实密钥。
- 地图产物读取有解析后路径、扩展名 allowlist 和允许根目录检查；Nortantis 子进程使用参数数组而非 shell 字符串。
- Nortantis 的上游 commit、AGPL 许可证及“视觉地图不覆盖 Python 图结构规则真相”的边界已记录。

## 5. 本轮低风险修复清单

报告生成后已按顺序完成以下低风险修复：

1. 已完成：私聊匿名与私有 transcript 端到端强制；私聊输入/回复分别使用 `private`/`system_private`，当前平台身份和投递上下文不进入匿名模型请求或 transcript。
2. 已完成：HTTP JSON Content-Type、1 MiB 默认请求体上限、UTF-8/长度/顶层对象校验及安全响应头。
3. 已完成：Dashboard 对战役、场次、频道、阶段、状态、模型名、保存路径和存档路径使用纯文本转义，并增加静态恶意标识回归。
4. 已完成：单端点也应用单次尝试超时；空响应持有本次 record 引用，不再修改并发的 `recent_calls[-1]`，失败计数同步修正。
5. 已完成：HTTP 与 LLM 运维日志改写 stderr，避免污染 JSON stdout。
6. 已完成：忽略 `artifacts/`，声明 `pytest>=8,<9` test extra，增加无密钥 `.env.example`。
7. 已完成：直接运行脚本从自身位置推导源码目录、支持 Python/数据根覆盖且不再含个人路径；Nortantis 缺 JAR 提示按 Windows/POSIX 选择构建命令。
8. 已完成：更新中文 README、Bridge README、FAQ 和故障排查，纠正 Session 0 离线语义、Windows `.runtime` 路径、8765/8766 关系和心跳次数。
9. 已完成：完整回归由 `2249` 增至 `2257 passed in 25.54s`；`compileall`、`git diff --check`、全部 zsh 脚本语法、plist、TOML/JSON 解析和隔离 wheel 构建均通过。

新增的 8 项回归覆盖匿名消息信封、私聊安全持久化、私有 transcript/dashboard 隔离、HTTP Content-Type/顶层类型/体积和安全头、dashboard 纯文本渲染、单端点 timeout，以及并发空响应遥测归属。没有调用真实模型或修改规则数值。

## 6. 需要用户确认后再动的项目

1. 是否为 HTTP/AstrBot 增加 bearer token；若增加，旧插件配置和所有手工 curl 都需要同步迁移。
2. 是否允许服务绑定非回环地址；如果允许，部署层是否已有 TLS、认证、反向代理和防火墙。
3. NPC 零 HP 命运选择等 AIGM 适配是否要继续保持，还是严格收回到规则书所说的 GM 决定。
4. 是否公开发行/提供网络服务，以及根项目、字体、图标和 Nortantis 修改版的许可方案。
5. 战役 ID 新校验和旧目录迁移策略。
6. 是否把地图生成改为异步任务，以及同战役忙碌时应等待、排队还是快速返回。
7. 是否将源码项目制作成包含配置与地图资源的自包含 wheel。
