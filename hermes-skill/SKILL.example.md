---
name: comfyui-vram-bridge
description: "提交本机 ComfyUI API 工作流，交接显存，并在 Hermes 新会话收到醒来后核对结果。"
version: "1.1.1"
platforms: [windows]
---

## 提交任务

用户要求使用本机 ComfyUI 时，先准备输入和 API 格式的工作流。用户提供了 API 文件时，基于该文件准备批次，保留指定的模型、节点连线和参数。根据本次请求修改提示词和输出设置。

1. 同一批任务一起提交。可以提交单个工作流、工作流数组，或 `{"prompts":[{"name":"任务名","prompt":{...}}],"context":"目标和醒来后的验收事项","request_id":"本批 UUID"}`。同一次提交的网络重试使用同一个 UUID。
2. 先运行 `validate`。预检通过后运行 `submit`。收到批次 ID 后结束当前回合，让调度器接管显存。收到 HTTP 422 时按节点错误修改工作流，收到 HTTP 409 时等待调度器空闲。

```powershell
python -X utf8 "<PROJECT_DIR>\bridge.py" validate "批次 JSON 的绝对路径"
python -X utf8 "<PROJECT_DIR>\bridge.py" submit "批次 JSON 的绝对路径"
```

## 收到“醒来”

运行 `python -X utf8 "<PROJECT_DIR>\bridge.py" latest-wake` 获取最近一次交接的批次 ID，再用同一脚本的 `result 批次ID` 读取结果。按批次 context 和工作流的实际输出位置验收。任务状态 `rejected` 表示 ComfyUI 没有接收入队，汇报具体节点错误。
