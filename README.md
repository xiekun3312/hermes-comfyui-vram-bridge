# Hermes / ComfyUI 显存调度器

在一台显存有限的 Windows 电脑上，让 Hermes 使用的 llama.cpp 模型和 ComfyUI 轮流使用 NVIDIA GPU。调度器独立运行，默认端口为 LLM `8021`、ComfyUI `8189`、调度器 `8781`。调度器 API 只监听回环地址。

## 安装和配置

需要 Windows、Python 3.12、`nvidia-smi`、ComfyUI 便携版、本地 `llama-server.exe` 和 Hermes Desktop。

```powershell
python -m pip install -r requirements.txt
if (-not (Test-Path -LiteralPath .\config.json)) {
    Copy-Item -LiteralPath .\config.example.json -Destination .\config.json
}
```

编辑 `config.json` 中的 `comfy_root` 和 `llm_bat`。ComfyUI 根目录下应有 `python_embeded\python.exe` 和 `ComfyUI\main.py`。LLM 启动脚本旁应有 `llama.cpp\llama-server.exe`，脚本的 `--port` 和 `--alias` 应对应 `llm_port` 和 `llm_alias`。将 Hermes 的本地模型地址指向该端口。

从旧版升级时，将现有 `config.json` 的 `comfy_port` 改为 `8189`。注册下文的桌面启动入口，然后在调度器空闲时重启以加载新配置。

在 Hermes 技能目录中安装 [技能模板](hermes-skill/SKILL.example.md) 时，将它保存为 `comfyui-vram-bridge\SKILL.md`，把 `<PROJECT_DIR>` 换成项目绝对路径。可在 Hermes 的 `config.yaml` 中把 `comfyui-vram-bridge` 加入 `skills.auto_load`，使新聊天加载它。

## 启动和查看状态

用运行 Hermes Desktop 的 Windows 账户登录，在项目目录注册一次桌面启动入口。注册时会检查当前 Python 是否安装了依赖，也可以用 `-PythonExe` 指定安装了依赖的 Python 路径。

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\注册桌面启动入口.ps1
```

双击 `启动调度器.cmd`，或运行 `python -X utf8 .\bridge.py serve`。两种方式都按需启动同一个桌面调度器，确认 HTTP 就绪后返回。Hermes 后台也可以运行同一条命令。保持 Windows 登录且桌面可用，以便打开可见的 ComfyUI 和 LLM 控制台，以及 Hermes Desktop 新会话。状态页是 [127.0.0.1:8781](http://127.0.0.1:8781/)。

```powershell
python -X utf8 .\bridge.py check
python -X utf8 .\bridge.py status
```

`check` 读取显存、进程、启动文件和 Hermes 会话库。示例配置的显存阈值为已用 3000 MiB，连续两次低于或等于阈值才开始加载。

## 交接任务

Hermes 可以按技能说明先准备输入和 ComfyUI API JSON，再向调度器提交。也可以手工提交同样的文件。

```powershell
python -X utf8 .\bridge.py validate .\batch.json
python -X utf8 .\bridge.py submit .\batch.json
python -X utf8 .\bridge.py result 批次ID
python -X utf8 .\bridge.py latest-wake
```

直接 API 为 `POST http://127.0.0.1:8781/v1/batches`，请求头为 `Content-Type: application/json`。支持单个 API 工作流、工作流数组，或以下带批次交接信息的格式。示例生成一张 64×64 预览图，提交后会触发完整的服务切换。

```json
{
  "request_id": "032b06bf-8c5e-4b37-aed1-58f8878d712b",
  "context": "生成一张预览，醒来后汇报运行结果。",
  "prompts": [
    {
      "name": "纯色预览",
      "prompt": {
        "1": {
          "class_type": "EmptyImage",
          "inputs": { "width": 64, "height": 64, "batch_size": 1, "color": 8421504 }
        },
        "2": {
          "class_type": "PreviewImage",
          "inputs": { "images": ["1", 0] }
        }
      }
    }
  ]
}
```

`validate` 在 LLM 在线时检查常见基础节点的连线、必填参数和本机 checkpoint。若整批预检失败，提交返回 HTTP `422` 并给出各节点问题，LLM 继续在线。HTTP `202` 表示已保存批次，其中 `rejected` 列出混合批次内预检失败的任务。使用同一个 `request_id` 重试同一份内容会返回原批次，执行新一轮时生成新 UUID。调度器在 ComfyUI 执行阶段继续接收批次，按接收顺序运行。显存交接收尾期间新提交会返回 `409`。ComfyUI 启动后会缓存节点目录，可以用 `GET /v1/nodes/{class_type}` 查询。

`GET /v1/batches/{id}` 返回每个任务的 `prompt_id`、状态、ComfyUI 报错和输出元数据。通常正式输出在 ComfyUI 根目录下的 `ComfyUI\output`，预览在 `ComfyUI\temp`。工作流自定义输出节点可能使用自己的目录。

## 切换与恢复

1. 验证 LLM 端口的监听者是配置里的 llama.cpp 实例。关闭该进程，确认端口释放并等待显存。
2. 在单独的可见 Python 窗口启动 ComfyUI，禁用自动打开浏览器。每个任务提交到 `/prompt`，从 `/history/{prompt_id}` 读取结果。个别任务被拒绝或报错时继续执行其余任务。
3. 确认待运行和运行中的 ComfyUI 队列都清空后关闭本次启动的 Python 进程。等待显存，启动可见的 LLM 窗口，检查 `/health`。
4. 调度器将 Hermes Desktop 切到前台，按 `Ctrl+N` 新建聊天，通过 Windows UI Automation 触发发送按钮。整个过程无需鼠标坐标。调度器核对“醒来”是否为新会话第一条用户消息，结果可在状态页或 `latest-wake` 查看。

启动前若 ComfyUI 配置端口（默认 `8189`）已被占用，批次会报错并保留正在运行的 LLM。需要停止调度器时，等状态页显示空闲，再在任务计划程序中结束 `ComfyUI-VRAM-Bridge-Desktop`。重新启动时，调度器读取上次交接记录，恢复本地模型。中断时未确认的任务会标记失败，以免重跑已经提交的任务。

若状态显示「待发送」，打开 Hermes Desktop，保持桌面解锁，执行 `python -X utf8 .\bridge.py retry-wake`。调度器遇到暂时的界面自动化事件错误时，会在调用发送按钮前重试一次。若显示「待核实」，先查看 Hermes 是否已收到消息。

批次、交接记录和日志保存在项目的 `data` 目录，服务输出另见各自的可见控制台。

## 测试

```powershell
python -X utf8 -B -m unittest discover -s tests -v
```

这些测试可在 LLM 和 ComfyUI 保持当前状态时运行。
