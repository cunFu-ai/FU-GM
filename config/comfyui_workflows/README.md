# ComfyUI 角色立绘工作流

角色工房通过可替换的 API-format JSON 连接 ComfyUI。仓库中的
`anima-api.json` 已从当前 Windows ComfyUI 里名为 `anima` 的工作流整理而来，
并移除了 D 站画廊、示例提示词、预览对比和首段额外存图节点。网页提供的角色
提示词是工作流唯一的内容提示来源。

`krea-lora-api.json` 来自名为 `krea-lora` 的工作流。它保留了 Krea 2 Turbo、
Qwen3-VL 文本编码器和当前启用的 `z3zz4-k2-4_c1-st5000.safetensors` LoRA，
并移除了地址说明、预览、显存清理以及处于 bypass 状态且本机缺失的 LoRA 节点。

这份 Anima 工作流依赖：

- UNet：`fnMomentAnimaTurbo_v20.safetensors`
- CLIP：`qwen_3_06b_base.safetensors`
- VAE：`qwen_image_vae.safetensors`
- 自定义节点：`Lora Loader (LoraManager)` 与 `JoinStringMulti`

Krea 2 + LoRA 工作流依赖：

- UNet：`krea2_turbo_fp8_scaled.safetensors`
- CLIP：`qwen3vl_4b_bf16.safetensors`
- VAE：`qwen_image_vae.safetensors`
- LoRA：`z3zz4-k2-4_c1-st5000.safetensors`

迁移到另一套 ComfyUI 时，需要同步对应模型与自定义节点，或者用那台机器可运行的
工作流重新导出 API-format JSON，并保持下列占位符。普通 Krea 2 仍可单独配置工作流。

在工作流节点的输入值中放入以下占位符，角色工房会在每次任务提交前替换它们：

| 占位符 | 内容 |
| --- | --- |
| `{{POSITIVE_PROMPT}}` | 网页中可编辑的正向提示词 |
| `{{NEGATIVE_PROMPT}}` | 网页中可编辑的负向提示词 |
| `{{SEED}}` | 随机种子 |
| `{{WIDTH}}` | 输出宽度，默认 768 |
| `{{HEIGHT}}` | 输出高度，默认 1152 |
| `{{FILENAME_PREFIX}}` | 经过清理的角色卡 ID 或角色名 |

示例：将正向 CLIP Text Encode 节点的 `text` 改成
`{{POSITIVE_PROMPT}}`，将 KSampler 的 `seed` 改成 `{{SEED}}`。占位符可以处于
任意节点和任意深度，但导出的文件必须是 API-format 对象，而不是浏览器工作流。

源码开发环境也可以在 `.env` 中填写：

```env
FU_GM_COMFYUI_ENABLED=1
FU_GM_COMFYUI_BASE_URL=http://127.0.0.1:8188
FU_GM_COMFYUI_ANIMA_WORKFLOW=config/comfyui_workflows/anima-api.json
FU_GM_COMFYUI_KREA2_WORKFLOW=config/comfyui_workflows/krea2-api.json
FU_GM_COMFYUI_KREA_LORA_WORKFLOW=config/comfyui_workflows/krea-lora-api.json
FU_GM_COMFYUI_KREA_LORA_WIDTH=1280
FU_GM_COMFYUI_KREA_LORA_HEIGHT=1832
```

默认只允许连接回环地址。确实需要访问局域网或远端 ComfyUI 时才将
`FU_GM_COMFYUI_ALLOW_REMOTE` 设为 `1`，并自行配置访问控制与防火墙。
