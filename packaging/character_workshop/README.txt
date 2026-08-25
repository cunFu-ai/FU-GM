最终物语（Fabula Ultima）角色工房 Windows 本地版

一、启动
1. 请先完整解压 ZIP 文件。
2. 双击“Fabula-Ultima-Character-Workshop.exe”。
3. 程序会启动本地服务，并用默认浏览器打开角色工房。
4. 请保留小型控制窗口；关闭它会停止本地服务。

二、数据位置
点击控制窗口中的“打开数据目录”即可查看。
角色名册保存在当前 Windows 用户的本地应用数据目录中。
尚未完成的草稿由浏览器保存在本机，请不要随意清除该站点的浏览器数据。

三、导入导出
角色卡可导出为 JSON 或纯文本 TXT。
JSON 可以在另一台角色工房继续导入；旧版 FU-GM 角色卡也可兼容读取。
TXT 仅用于阅读和分享。

四、自动立绘
1. 先在本机启动 ComfyUI。
2. 打开角色工房右上角的齿轮按钮，填写 ComfyUI 端口（默认 8188）。
3. 填写 OpenAI 兼容的 LLM 接口地址、模型名和 API Key。
4. 分别点击测试按钮，确认连接后即可在第七步生成提示词与立绘。

发行包的 workflows 文件夹包含 Anima 与 Krea 2 + LoRA 的 API-format JSON。
程序会直接读取这个文件夹；如果要使用普通 Krea 2，可将兼容的工作流命名为
krea2-api.json 放入该文件夹。不要改变工作流中的 {{POSITIVE_PROMPT}}、
{{NEGATIVE_PROMPT}}、{{SEED}}、{{WIDTH}}、{{HEIGHT}} 与
{{FILENAME_PREFIX}} 占位符。

五、安全说明
程序与 ComfyUI 都只连接本机地址 127.0.0.1，不会自动开放公网访问。
API Key 只保存在程序内存中，关闭程序后自动清除，不会写入设置文件或角色卡。
LLM 接口地址、模型名和 ComfyUI 端口会保存在当前用户的数据目录中。
本发行包不包含 API Key、FU-GM 私密上下文或作者电脑上的运行数据。
