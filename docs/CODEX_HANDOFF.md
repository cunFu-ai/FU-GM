# FU-GM Mac Runtime Handoff

本文档只记录从 Windows 压缩包迁移到 Mac 开发时的运行环境变化。项目功能进度、路线规划和待办事项以 `ROADMAP.md` 为准。

## 给 Mac Codex 的第一条消息

```text
这是从 Windows zip 迁移到 Mac 的 fu-gm 项目。请先阅读 docs/CODEX_HANDOFF.md、README.md、ROADMAP.md。重点检查 Mac 运行环境、路径、环境变量、服务启动方式、AstrBot 接入和地图生成依赖，不要假设 Windows 上的 .venv、.env、data、logs、runtime 路径仍然可用。
```

## 迁移原则

- `.zip` 只迁移源码、测试、配置模板、脚本和文档。
- `.venv` 不迁移，Mac 上重新创建。
- `.env` 不放进 zip，Mac 上重新创建或通过安全渠道复制。
- `data/` 和 `logs/` 默认不迁移；如果要迁移真实战役数据，单独备份并明确指定数据目录。
- 不要让 Windows 和 Mac 同时写同一份战役数据目录。

## 推荐 zip 内容

应包含：

```text
src/
tests/
assets/
config/
integrations/
scripts/
launch_agents/
docs/
README.md
ROADMAP.md
pyproject.toml
.gitignore
```

应排除：

```text
.venv/
.runtime/
.env
data/
logs/
tmp/
__pycache__/
.pytest_cache/
.git/
AstrBot.app/
AstrBot Launcher/
.DS_Store
```

## Mac 初始环境

进入项目根目录后执行：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
pip install -e .
pip install pytest
```

说明：

- 项目本体是 Python 包，Mac 上不要复用 Windows `.venv`。
- `pytest` 需要显式安装，因为部分测试文件会直接 `import pytest`。
- 如果 Mac 有多个 Python，优先使用 `python3 --version` 确认版本；项目要求 Python 3.9+。

## 环境变量

Mac 上建议重新创建 `.env`，不要从 zip 中携带真实密钥。

建议使用 Mac 用户目录下的运行时目录：

```bash
FU_GM_DATA_ROOT="$HOME/.fu-gm/data/campaigns"
FU_GM_LOG_DIR="$HOME/.fu-gm/logs"
```

模型/API 变量按你的实际配置填写：

```bash
FU_GM_MODEL=
FU_GM_API_BASE=
FU_GM_API_KEY=
```

如果只跑离线测试，可以不配置 API Key。

## 直接启动 HTTP 服务

先用命令行启动，确认服务可用，再考虑后台服务：

```bash
source .venv/bin/activate
mkdir -p "$HOME/.fu-gm/data/campaigns" "$HOME/.fu-gm/logs"
PYTHONPATH=src python -m fu_gm.http_server --host 127.0.0.1 --port 8765 --data-root "$HOME/.fu-gm/data/campaigns"
```

健康检查：

```bash
curl http://127.0.0.1:8765/health
```

离线模式：

```bash
PYTHONPATH=src python -m fu_gm.http_server --offline --host 127.0.0.1 --port 8765 --data-root "$HOME/.fu-gm/data/campaigns"
```

## Windows 与 Mac 的关键差异

### 路径

Windows 当前项目路径类似：

```text
F:\New project
```

Mac 解压后路径可能类似：

```text
/Users/<name>/Documents/fu-gm
```

代码和脚本里不能依赖 Windows 盘符、反斜杠或固定用户名。Mac 接手后应重点检查：

```text
scripts/run_fu_gm_http.sh
scripts/install_fu_gm_launch_agent.sh
launch_agents/com.fugm.http.plist
integrations/astrbot_plugin_fu_gm/
```

### Shell

Windows 使用 PowerShell：

```powershell
.\.venv\Scripts\Activate.ps1
```

Mac 使用 zsh/bash：

```bash
source .venv/bin/activate
```

不要在 Mac 上运行 `.ps1`；不要在 Windows 上假设 `.sh` 可直接运行。

### 脚本权限

Mac 解压 zip 后，`.sh` 可能没有执行权限：

```bash
chmod +x scripts/*.sh
```

### 换行

Windows 可能是 CRLF，Mac 更适合 LF。一般 Python 不受影响，但 shell 脚本如果出现 `bad interpreter`，先检查换行：

```bash
file scripts/run_fu_gm_http.sh
```

必要时转换为 LF。

## Mac 后台服务

先不要直接启用 LaunchAgent。当前 LaunchAgent 相关文件可能仍有固定路径，需要确认后再使用：

```text
scripts/run_fu_gm_http.sh
scripts/install_fu_gm_launch_agent.sh
launch_agents/com.fugm.http.plist
```

接手后应把这些脚本改成：

- 从脚本位置推导项目根目录。
- 默认使用 `$HOME/.fu-gm/...`。
- 允许用环境变量覆盖端口、数据目录、日志目录。
- 由安装脚本生成 plist，避免 plist 内写死用户名。

LaunchAgent 启用前，必须先用前面的直接启动命令确认 HTTP 服务能正常运行。

## AstrBot 接入

Mac 上 AstrBot 的位置和 Windows 不同，不能沿用 Windows 路径。

接手检查项：

- AstrBot 插件目录是否已经包含 `integrations/astrbot_plugin_fu_gm`。
- 插件配置的 FU-GM HTTP 地址是否指向当前 Mac 服务端口。
- FU-GM 实际监听端口是否与插件配置一致，常见为 `8765` 或 `8766`。
- AstrBot 和 FU-GM 是否运行在同一台机器；如果不是，`127.0.0.1` 不可用。

先用 `curl /health` 确认 FU-GM 服务，再排查 AstrBot。

## 地图生成依赖

地图生成依赖可能涉及 Nortantis、Java、字体和输出目录。Mac 接手后如果地图测试失败，优先检查运行环境：

```bash
java -version
```

还需要检查：

- Nortantis jar 或运行入口是否存在。
- 项目配置中的地图输出目录是否可写。
- 字体路径是否适配 Mac。
- 路径中是否仍存在 Windows 反斜杠或盘符。

地图相关失败不应先归因于规则逻辑。

## 测试命令

基础测试：

```bash
PYTHONPATH=src python -m unittest discover -s tests
```

完整测试：

```bash
PYTHONPATH=src pytest
```

如果只想先跳过地图环境问题，可以临时排除地图测试，但需要在最终记录里说明：

```bash
PYTHONPATH=src pytest -k "not map_renderer"
```

## 日志和数据目录

推荐 Mac 使用：

```text
$HOME/.fu-gm/data/campaigns
$HOME/.fu-gm/logs
```

开发项目目录中不建议长期保存真实运行数据。这样 zip 迁移、git diff 和测试都更干净。

## 交接完成标准

Mac Codex 接手后，先完成以下检查，再继续开发功能：

1. 能创建 `.venv` 并 `pip install -e .`。
2. 能跑基础测试。
3. 能启动 HTTP 服务。
4. `/health` 能返回正常状态。
5. AstrBot 插件配置的端口与 FU-GM 服务端口一致。
6. 地图生成依赖的失败原因已明确：环境问题、配置问题或代码问题。
7. `ROADMAP.md` 已阅读，功能规划不写入本文档。
