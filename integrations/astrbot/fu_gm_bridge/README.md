# FU-GM AstrBot Bridge

这是一个很薄的 AstrBot 插件：AstrBot 负责群消息，FU-GM 负责跑团规则、记忆和叙事。

## 启动 FU-GM 服务

在 FU-GM 项目根目录运行：

```bash
PYTHONPATH=src python3 -m fu_gm.http_server --host 127.0.0.1 --port 8766
```

Windows PowerShell：

```powershell
.\scripts\run_fu_gm_http.ps1
```

如果只想离线测试：

```bash
PYTHONPATH=src python3 -m fu_gm.http_server --offline
```

Windows PowerShell：

```powershell
.\scripts\run_fu_gm_http.ps1 -Offline
```

## Windows / AstrBot Launcher 安装

本项目默认把运行时数据放在项目内的 `.runtime` 目录。AstrBot Launcher 的数据目录是：

```text
<项目根目录>\.runtime\.astrbot_launcher
```

插件目录是：

```text
<项目根目录>\.runtime\.astrbot_launcher\instances\<实例ID>\core\data\plugins
```

在 FU-GM 项目根目录运行：

```powershell
.\scripts\install_fu_gm_astrbot_launcher.ps1
```

脚本会把本插件复制到 `data\plugins\fu_gm_bridge`，把 FU-GM 服务运行时代码复制到 `<项目根目录>\.runtime\.fu-gm`，并注册计划任务 `FU-GM HTTP Server` 来替代 macOS 的 LaunchAgent。多个 AstrBot 实例时可加 `-InstanceId <实例目录名>`；只复制文件、不注册自启任务时可加 `-NoSchedule`。

如果默认端口 `8766` 已被其他程序占用，可以指定端口；脚本会同步写入 AstrBot 插件配置：

```powershell
.\scripts\install_fu_gm_astrbot_launcher.ps1 -Port 9876
```

安装后需要在 AstrBot Launcher 中重启实例，或进入 WebUI 的插件管理页面重载 `FU-GM Bridge`。插件页应能看到目录 `fu_gm_bridge`，配置文件位于：

```text
<项目根目录>\.runtime\.astrbot_launcher\instances\<实例ID>\core\data\config\fu_gm_bridge_config.json
```

## 插件命令

- `/fugm <行动>`：走跑团规则流程。
- `/fugm_chat <内容>`：普通水群聊天，会召回公开故事记忆。
- `/fugm_s0 <内容>`：Session 0 世界创建/角色创建讨论。
- `/fugm_safety <内容>`：设置界限与帷幕。私聊 GM 使用时默认匿名写入当前团。
- `/fugm_end [标题]`：结束并整理本场，生成 transcript 和故事摘要。
- `/fugm_campaign [团名]`：查看或切换当前群绑定的 FU-GM 团。
- `/fugm_campaigns`：列出 FU-GM 服务已知团。
- `/fugm_save [存档槽]`：保存当前团；不填则保存为最新快照。
- `/fugm_load [团名] [存档槽]`：读档；不填则读取当前群绑定团的最新快照。
- `/fugm_delete_save [存档槽]`：删除当前团的最新快照或指定命名存档槽；不填则只删最新快照。
- `/fugm_delete_campaign 确认删除`：删除当前群绑定的整个战役目录，包括日志、故事记忆和所有存档。
- `/fugm_away [原因]`：标记自己临时离席，并自动保存当前团。
- `/fugm_back`：标记自己回到本场，并自动保存当前团。
- `/fugm_status`：查看当前团、场景、行动者和离席状态。
- `/fugm_health`：检查 FU-GM HTTP 服务。

也可以用配置里的前缀：

- `时悠 还记得上次宝箱王吗？` 或 `悠老师，当前跑团状态是什么样？` 会走普通水群。
- `跑团 我调查宝箱` 会走跑团规则流程。

## 会话门控与自然群聊仲裁

默认情况下，插件会监听群里的普通消息，但不会让 FU-GM 平时抢所有话。每条消息会先交给 FU-GM HTTP 服务的 `/v1/message/route` 做低延迟仲裁；FU-GM 只有在当前群被明确开团后才会接管。

明确开团信号示例：

- `开始跑团`
- `今晚开团`
- `继续上次冒险`
- `开始第零章`
- `进入第零章`

明确暂停/退出信号示例：

- `先暂停一下`
- `暂停跑团`
- `今天到这`
- `收团`
- `结束跑团`

未开团时：

- `今天晚饭吃什么`：放行给 AstrBot 本体或其他插件。
- `普通闲聊`：如果没有开团，也会先放行给 AstrBot 本体，避免 FU-GM 永久抢占群聊。
- `开始跑团`：激活 FU-GM，会话进入跑团接管状态。

开团后：

- `我攻击宝箱王`、`我调查墙上的符文`：判定为跑团行动，交给 FU-GM 结算并阻止 AstrBot 本体重复回复。
- `时悠，还记得宝箱王吗？` 或 `悠老师，当前跑团状态是什么样？`：判定为直接和 GM 聊天，交给 FU-GM 的普通水群人格。
- `我们要不要先调查宝箱？`：判定为玩家间桌面讨论，默认静默并阻止 AstrBot 本体插话。
- `今天晚饭吃什么`：跑团接管期间也会由 FU-GM 的水群人格处理，避免同桌聊天时忘记刚发生的冒险。

这意味着玩家不必每次输入命令才能和 GM 对话；命令仍保留为调试、强制路由和不确定时的备用入口。

## 开团后的连续发言合并

开团后，插件默认不会把每条自然群聊消息都立刻丢给 LLM。它会先做一个很短的缓冲：

- 第一条自然消息到达后等待 `buffer_debounce_seconds` 秒，默认 3 秒。
- 如果期间又有新消息，重新等待 3 秒。
- 一个批次最多等待 `buffer_max_wait_seconds` 秒，默认 12 秒。
- 一个批次最多合并 `buffer_max_messages` 条消息，默认 5 条。
- 合并后的输入会保留每条消息的发言人，例如 `阿凛：我先观察门`、`白河：等等，先别碰宝箱`。

这样玩家可以自然地连续补充意图，GM 会把它理解成同一轮桌面输入，而不是逐句抢答。

以下消息不会进入缓冲，会立即处理：

- 命令消息，例如 `/fugm_save`、`/fugm_load`。
- 私聊界限与帷幕声明。
- 明确开团、暂停、收团、存档、读档、离席、回归等控制信号。

存档和读档也可以用自然说法，不必强制使用命令：

- `时悠，调出存档列表`
- `保存一下`
- `新建存档 Boss 战前`
- `读取存档 Boss 战前`
- `读档`：如果没有给出存档槽，FU-GM 会先列出可用存档，避免误读。

相关配置：

- `enable_natural_routing`：是否启用自然消息仲裁。
- `natural_route_group_messages`：是否让群聊普通消息进入仲裁器。
- `natural_route_private_messages`：是否让私聊普通消息进入仲裁器。
- `block_silent_table_talk`：当判定为玩家间跑团讨论时，是否阻止 AstrBot 本体插话。
- `enable_message_buffer`：是否在开团后启用自然群聊消息合并。
- `buffer_debounce_seconds`：连续发言的静默等待时间，默认 3 秒。
- `buffer_max_wait_seconds`：单批次最长等待时间，默认 12 秒。
- `buffer_max_messages`：单批次最多合并消息数，默认 5 条。

## 私聊匿名界限与帷幕

玩家可以在跑团过程中私聊 GM 设置安全边界，例如：

```text
我不希望出现蜘蛛
儿童遇险请带过
/fugm_safety 不要详细描写不健康关系
```

插件会把私聊安全声明发送到 FU-GM 的 `/v1/safety/declare` 接口，并默认设置为匿名。FU-GM 会立即把内容写入当前 `campaign_id` 的界限与帷幕并自动保存，但群聊不会看到是谁提出的，也不会要求玩家解释原因。

为了让私聊能归属到正确跑团，插件会在玩家参与群内 FU-GM 指令时记录“玩家最近参与的战役”。如果玩家只私聊、从未在群里参与过，则会落到配置里的默认 `campaign_id`。也可以先在群里用 `/fugm_campaign <团名>` 绑定当前群。

## 设计原则

插件不会直接修改 FU-GM 状态，只通过 HTTP 调用服务。这样以后要接网页、Discord、CLI 或其他机器人框架时，FU-GM 核心不用重写。

每个群可以绑定不同的 `campaign_id`，绑定关系默认保存在 AstrBot 的
`data/plugin_data/fu_gm_bridge/channel_campaigns.json`。FU-GM 服务本身会按
`campaign_id` 区分存档目录，因此多个群同时跑不同团时不会串档。

玩家到战役的私聊映射默认保存在 AstrBot 的
`data/plugin_data/fu_gm_bridge/user_campaigns.json`，只用于把私聊安全声明投递到正确战役。
