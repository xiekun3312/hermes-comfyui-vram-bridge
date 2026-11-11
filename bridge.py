"""Local HTTP submission service and serial GPU handoff worker."""
import argparse
from copy import deepcopy
import ctypes
from ctypes import wintypes
from datetime import datetime
import errno
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from html import escape
from importlib.util import find_spec
import json
import logging
import os
from pathlib import Path
import re
import socket
import sys
import threading
import time
from urllib.parse import unquote, urlsplit
from uuid import UUID, uuid4

from machine import HttpError, Machine, hidden, listening, process_info, request, same_path
from preflight import BASE, check_prompt, checkpoint_names
import wake


ROOT = Path(__file__).resolve().parent
DESKTOP_TASK = "ComfyUI-VRAM-Bridge-Desktop"
MAX_BODY = 32 * 1024 * 1024
BUSY = {"draining", "restoring", "waking", "recovery"}


def now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def save_json(path, data):
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def uuid_text(value):
    if not isinstance(value, str):
        raise ValueError("批次 ID 必须是 UUID")
    return str(UUID(value))


def normalize(body):
    if isinstance(body, dict) and "prompts" in body:
        items = body["prompts"]
        context = body.get("context", "")
        batch_id = uuid_text(body.get("request_id", str(uuid4())))
    else:
        items = body if isinstance(body, list) else [body]
        context = ""
        batch_id = str(uuid4())
    if not isinstance(context, str):
        raise ValueError("context 必须是字符串")
    if not isinstance(items, list) or not items:
        raise ValueError("prompts 必须是非空数组")
    specs = []
    for index, item in enumerate(items, 1):
        wrapper = item if isinstance(item, dict) and "prompt" in item else {"prompt": item}
        graph = wrapper["prompt"]
        if not isinstance(graph, dict) or not graph or not all(
            isinstance(node, dict) and isinstance(node.get("class_type"), str)
            and node["class_type"] and isinstance(node.get("inputs"), dict)
            for node in graph.values()
        ):
            raise ValueError(f"任务 {index} 需要 ComfyUI API 格式的节点映射")
        name = wrapper.get("name", f"任务 {index}")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"任务 {index} 的 name 必须是非空字符串")
        payload = {"prompt": graph}
        if "extra_data" in wrapper:
            if not isinstance(wrapper["extra_data"], dict):
                raise ValueError(f"任务 {index} 的 extra_data 必须是对象")
            payload["extra_data"] = wrapper["extra_data"]
        if "partial_execution_targets" in wrapper:
            targets = wrapper["partial_execution_targets"]
            if not isinstance(targets, list) or not all(isinstance(node, str) and node in graph for node in targets):
                raise ValueError(f"任务 {index} 的 partial_execution_targets 必须是图中的节点 ID")
            payload["partial_execution_targets"] = targets
        specs.append({"name": name, "payload": payload})
    return batch_id, context, specs


def rejection_text(body):
    if not isinstance(body, dict):
        return str(body)[:600]
    details = [
        f'{node}: {error.get("details") or error.get("message", "")}'
        for node, item in body.get("node_errors", {}).items()
        for error in item.get("errors", [])
    ]
    error = body.get("error", {})
    message = error.get("message", "ComfyUI 拒绝入队") if isinstance(error, dict) else str(error)
    return (message + (" · " + " · ".join(details) if details else ""))[:800]


class Bridge:
    def __init__(self, config, data_dir, machine=None):
        self.config = config
        self.data = data_dir
        self.machine = machine or Machine(config)
        self.lock = threading.RLock()
        self.changed = threading.Event()
        self.batches = self.data / "batches"
        self.batches.mkdir(parents=True, exist_ok=True)
        self.journal = self.data / "handoff.json"
        self.wake_file = self.data / "latest-wake.json"
        self.catalog_file = self.data / "object-info.json"
        self.catalog = read_json(self.catalog_file) if self.catalog_file.is_file() else {}
        self.active = read_json(self.journal) if self.journal.is_file() else None
        self.latest_wake = read_json(self.wake_file) if self.wake_file.is_file() else None
        self.jobs = {path.stem: read_json(path) for path in self.batches.glob("*.json")}
        self.phase = "recovery" if self.active else "idle"
        self.detail = ""
        self.wake_requested = False

    def set_phase(self, phase, detail=""):
        with self.lock:
            if (phase, detail) != (self.phase, self.detail):
                logging.info("%s %s", phase, detail)
                self.phase, self.detail = phase, detail

    def update_active(self, **values):
        with self.lock:
            record = {**(self.active or {}), **values}
            save_json(self.journal, record)
            self.active = record

    def update_job(self, job, **values):
        with self.lock:
            job.update(values, updated_at=now())
            save_json(self.batches / f"{job['id']}.json", job)

    def update_wake(self, **values):
        with self.lock:
            record = {**(self.latest_wake or {}), **values}
            save_json(self.wake_file, record)
            self.latest_wake = record

    def submit(self, body):
        batch_id, context, specs = normalize(body)
        digest = hashlib.sha256(json.dumps([context, specs], sort_keys=True).encode()).hexdigest()
        with self.lock:
            existing = self.jobs.get(batch_id)
            if existing:
                if existing["digest"] != digest:
                    raise HttpError(409, {"error": "同一个 request_id 已用于其他任务"})
                return self.public_job(existing), False
            if self.phase in BUSY or self.wake_requested:
                raise HttpError(409, {"error": "调度器正在释放显存或恢复 Hermes，请稍后提交"})
            issues = self.check_specs(specs)
            if len(issues) == len(specs):
                logging.info("预检拒绝批次 %s：%s", batch_id, issues)
                raise HttpError(422, {"error": "工作流预检未通过，未切换显存", "tasks": issues})
            job = {
                "id": batch_id, "digest": digest, "context": context,
                "created_at": now(), "queued_at": time.time(), "state": "queued",
                "tasks": [
                    {**spec, "prompt_id": str(uuid4()), "state": "queued"}
                    for spec in specs
                ],
            }
            for issue in issues:
                job["tasks"][issue["index"] - 1].update(
                    state="rejected", error=issue["node_errors"], detail="工作流预检未通过",
                )
            self.update_job(job)
            self.jobs[batch_id] = job
            self.changed.set()
            return self.public_job(job), True

    def check_specs(self, specs):
        checkpoints = checkpoint_names(self.config["comfy_root"])
        errors = []
        for index, spec in enumerate(specs, 1):
            issues = check_prompt(spec["payload"], self.catalog, checkpoints)
            if issues:
                errors.append({"index": index, "name": spec["name"], "node_errors": issues})
        return errors

    def refresh_catalog(self):
        if self.catalog and time.time() - self.catalog_file.stat().st_mtime < 3600:
            return
        try:
            catalog = self.comfy("/object_info", timeout=20)
            if not isinstance(catalog, dict) or "CheckpointLoaderSimple" not in catalog:
                raise RuntimeError("ComfyUI 返回了不完整的节点目录")
            save_json(self.catalog_file, catalog)
            with self.lock:
                self.catalog = catalog
            logging.info("已缓存 %s 个 ComfyUI 节点定义", len(catalog))
        except (HttpError, OSError, ValueError, RuntimeError) as exc:
            logging.warning("暂未更新 ComfyUI 节点目录：%s", exc)

    @staticmethod
    def public_job(job):
        public = {k: deepcopy(v) for k, v in job.items() if k not in {"digest", "tasks", "queued_at"}}
        public["tasks"] = [
            {k: deepcopy(v) for k, v in task.items() if k != "payload"} for task in job["tasks"]
        ]
        return public

    def status(self):
        with self.lock:
            jobs = sorted(self.jobs.values(), key=lambda job: job["queued_at"], reverse=True)
            return {
                "phase": self.phase, "detail": self.detail,
                "llm_port": self.config["llm_port"], "comfy_port": self.config["comfy_port"],
                "batches": [
                    {"id": job["id"], "state": job["state"], "created_at": job["created_at"],
                     "detail": job.get("detail", ""),
                     "finished": sum(t["state"] not in {"queued", "submitting", "running"} for t in job["tasks"]),
                     "total": len(job["tasks"])}
                    for job in jobs[:25]
                ],
                "latest_wake": deepcopy(self.latest_wake),
            }

    def next_job(self):
        return min(
            (job for job in self.jobs.values() if job["state"] == "queued"),
            key=lambda job: job["queued_at"], default=None,
        )

    def comfy(self, route, payload=None, timeout=5):
        return request(self.config["comfy_port"], route, payload, timeout)

    def while_alive(self, process, operation):
        warned = False
        while process is None or process.poll() is None:
            try:
                return operation()
            except (HttpError, OSError, ValueError, KeyError) as exc:
                if process is None:
                    # Crash recovery has no Popen handle. A vanished listener is terminal.
                    if not listening(self.config["comfy_port"]):
                        raise RuntimeError("ComfyUI 端口已关闭") from exc
                if not warned:
                    logging.warning("等待 ComfyUI API 恢复：%s", exc)
                    warned = True
                time.sleep(self.config["poll_seconds"])
        raise RuntimeError(f"ComfyUI 已退出，退出码 {process.returncode}")

    def queue(self, process):
        def load():
            data = self.comfy("/queue")
            return data["queue_running"] + data["queue_pending"]

        return self.while_alive(process, load)

    def idle_queue(self, process):
        empty = 0
        while empty < 2:
            empty = empty + 1 if not self.queue(process) else 0
            if empty < 2:
                time.sleep(self.config["poll_seconds"])

    def task_state(self, job, task, state, **values):
        with self.lock:
            task.update(values, state=state, updated_at=now())
            self.update_job(job)

    def run_prompt(self, job, task, process):
        prompt_id = task["prompt_id"]
        self.task_state(job, task, "submitting")
        logging.info("提交 %s / %s", job["id"], task["name"])
        uncertain = ""
        try:
            response = self.comfy(
                "/prompt", {**task["payload"], "prompt_id": prompt_id, "client_id": job["id"]}, 90,
            )
            if response.get("prompt_id") != prompt_id:
                raise RuntimeError("ComfyUI 返回的 prompt_id 不匹配")
            self.task_state(job, task, "running", node_errors=response.get("node_errors", {}))
        except HttpError as exc:
            if 400 <= exc.status < 500:
                detail = rejection_text(exc.body)
                logging.warning("ComfyUI 拒绝入队 %s / %s / HTTP %s / %s",
                                job["id"], task["name"], exc.status, detail)
                self.task_state(job, task, "rejected", error=exc.body, detail=detail)
                return
            uncertain = str(exc)
        except (OSError, ValueError, RuntimeError) as exc:
            uncertain = str(exc)

        missing = 0
        while True:
            history = self.while_alive(process, lambda: self.comfy(f"/history/{prompt_id}"))
            if prompt_id in history:
                entry = history[prompt_id]
                status = entry.get("status", {})
                success = status.get("status_str") == "success" and status.get("completed") is True
                errors = [
                    item for item in status.get("messages", [])
                    if isinstance(item, list) and item and item[0] in {"execution_error", "execution_interrupted"}
                ]
                self.task_state(
                    job, task, "succeeded" if success else "failed",
                    outputs=entry.get("outputs", {}), comfy_status=status.get("status_str"),
                    errors=errors,
                )
                return

            queued = self.queue(process)
            if any(isinstance(item, list) and len(item) > 1 and item[1] == prompt_id for item in queued):
                missing = 0
                if task["state"] == "submitting":
                    self.task_state(job, task, "running")
            else:
                missing += 1
                # A task may finish between the history and queue requests.
                if missing >= 3:
                    self.task_state(
                        job, task, "failed",
                        error=uncertain or "任务不在 ComfyUI 队列或历史中",
                    )
                    return
            time.sleep(self.config["poll_seconds"])

    def run_job(self, job, process):
        with self.lock:
            self.update_active(batch_ids=[*self.active["batch_ids"], job["id"]])
            self.update_job(job, state="running")
        for task in job["tasks"]:
            if task["state"] == "queued":
                self.run_prompt(job, task, process)
        successes = sum(task["state"] == "succeeded" for task in job["tasks"])
        rejected = sum(task["state"] == "rejected" for task in job["tasks"])
        if rejected == len(job["tasks"]):
            state = "rejected"
            detail = "全部工作流均未入队。查看任务中的节点错误"
        elif successes == len(job["tasks"]):
            state, detail = "succeeded", ""
        elif successes:
            state, detail = "partial", f"{successes}/{len(job['tasks'])} 个任务成功"
        else:
            state, detail = "failed", "无成功任务。查看任务中的报错"
        self.update_job(job, state=state, detail=detail, finished_at=now())

    def fail_jobs(self, error, include_pending):
        with self.lock:
            for job in self.jobs.values():
                if job["state"] == "running" or (include_pending and job["state"] == "queued"):
                    if self.active and job["id"] not in self.active["batch_ids"]:
                        self.update_active(batch_ids=[*self.active["batch_ids"], job["id"]])
                    for task in job["tasks"]:
                        if task["state"] in {"queued", "submitting", "running"}:
                            task.update(state="interrupted", error=error)
                    self.update_job(job, state="failed", error=error, finished_at=now())

    def do_wake(self):
        self.set_phase("waking", "在 Hermes 新会话中发送“醒来”")
        if not self.machine.llm_ready():
            self.update_wake(state="pending", auto_send=False, detail="LLM 健康检查暂未通过")
            return
        try:
            def mark_sending(after_id, since):
                self.update_wake(state="sending", auto_send=False, after_id=after_id, since=since, detail="")
            try:
                session_id = wake.send(mark_sending)
            except Exception as exc:
                if (self.latest_wake["state"] != "pending"
                        or getattr(exc, "hresult", None) != wake.EVENT_E_ALL_SUBSCRIBERS_FAILED):
                    raise
                logging.warning("Hermes 界面自动化事件失败，准备重试发送", exc_info=True)
                time.sleep(1)
                session_id = wake.send(mark_sending)
            self.update_wake(state="delivered", session_id=session_id, detail="已确认新会话首条消息")
        except Exception as exc:
            state = "unconfirmed" if self.latest_wake["state"] == "sending" else "pending"
            self.update_wake(state=state, auto_send=False, detail=str(exc))
            logging.warning("Hermes 唤醒 %s：%s", state, exc, exc_info=True)

    def finish_restore(self, process=None):
        self.set_phase("restoring", "关闭 ComfyUI，等待显存释放并恢复 LLM")
        record = self.active
        self.machine.stop_comfy(process, record.get("comfy_pid"), record.get("comfy_info"))
        self.update_active(comfy_pid=None, comfy_info=None)
        self.machine.start_llm()
        self.update_wake(
            batch_ids=record["batch_ids"], state="pending", session_id=None,
            after_id=None, since=None, llm_ready=True, ready_at=now(), detail="", auto_send=True,
        )
        with self.lock:
            self.journal.unlink()
            self.active = None
        self.do_wake()

    def cycle(self):
        process = None
        comfy_ready = False
        try:
            self.set_phase("arming", "等待提交回执返回 Hermes")
            time.sleep(4)
            self.machine.check_files()
            self.machine.gpu()
            if listening(self.config["comfy_port"]):
                raise RuntimeError(f"ComfyUI 端口 {self.config['comfy_port']} 已经在运行")
            owner = self.machine.llm_owner()
            self.update_active(batch_ids=[], comfy_pid=None, comfy_info=None)
            self.set_phase("starting", "关闭 LLM 并等待显存")
            self.machine.stop_llm(owner)
            self.machine.wait_for_vram()
            self.set_phase("starting", "在可见 Python 窗口启动 ComfyUI")
            process = self.machine.start_comfy()
            info = process_info(process.pid)
            self.update_active(comfy_pid=process.pid, comfy_info=info)
            self.machine.wait_comfy(process)
            comfy_ready = True
            self.refresh_catalog()
            while True:
                with self.lock:
                    job = self.next_job()
                    if job:
                        self.set_phase("working", job["id"])
                if job:
                    self.run_job(job, process)
                    continue
                self.set_phase("working", "等候 ComfyUI 队列清空")
                self.idle_queue(process)
                with self.lock:
                    if self.next_job() is None:
                        finished = [self.jobs[job_id] for job_id in self.active["batch_ids"]]
                        if finished and all(job["state"] == "rejected" for job in finished):
                            self.set_phase("draining", "ComfyUI 拒绝了全部工作流，回切 LLM 以便修正")
                        else:
                            self.set_phase("draining", "ComfyUI 队列已清空")
                        break
        except Exception as exc:
            logging.exception("批次运行失败")
            with self.lock:
                self.set_phase("draining", str(exc))
                self.fail_jobs(str(exc), include_pending=True)
            if comfy_ready and process.poll() is None:
                self.idle_queue(process)
        if self.active:
            self.finish_restore(process)
        else:
            self.set_phase("idle", self.detail)

    def recover(self):
        record = self.active
        self.set_phase("recovery", "恢复上一次中断的显存交接")
        self.fail_jobs("上次调度器运行中断，未确认的任务需要检查后重新提交", include_pending=False)
        pid, saved = record.get("comfy_pid"), record.get("comfy_info")
        if pid and saved and listening(self.config["comfy_port"]) == {pid}:
            info = process_info(pid)
            if info and info["created"] == saved["created"] and same_path(info["path"], self.machine.comfy_python):
                try:
                    self.idle_queue(None)
                except RuntimeError:
                    if listening(self.config["comfy_port"]):
                        raise
        self.finish_restore()

    def request_wake(self):
        with self.lock:
            if self.phase != "idle" or not self.latest_wake or self.latest_wake["state"] != "pending":
                raise HttpError(409, {"error": "当前没有可重试的唤醒"})
            self.wake_requested = True
            self.changed.set()

    def worker(self):
        while True:
            self.changed.clear()
            try:
                if self.active:
                    self.recover()
                elif self.latest_wake and self.latest_wake["state"] == "sending":
                    session_id = wake.confirmation(self.latest_wake["after_id"], self.latest_wake["since"])
                    self.update_wake(
                        state="delivered" if session_id else "unconfirmed", session_id=session_id,
                        detail="已检查上次发送记录",
                    )
                elif self.wake_requested or (self.latest_wake and self.latest_wake.get("auto_send")):
                    self.do_wake()
                    with self.lock:
                        self.wake_requested = False
                else:
                    if self.latest_wake and self.latest_wake["state"] == "unconfirmed":
                        session_id = wake.confirmation(self.latest_wake["after_id"], self.latest_wake["since"])
                        if session_id:
                            self.update_wake(state="delivered", session_id=session_id, detail="已确认新会话首条消息")
                    with self.lock:
                        pending = self.next_job() is not None
                    if pending:
                        self.cycle()
                self.set_phase("idle", self.detail if self.phase == "idle" else "")
            except Exception as exc:
                logging.exception("调度器将重试恢复")
                self.set_phase("recovery" if self.active else "idle", str(exc))
                time.sleep(20)
                continue
            with self.lock:
                if self.next_job() is not None or self.wake_requested:
                    continue
            unconfirmed = self.latest_wake and self.latest_wake["state"] == "unconfirmed"
            self.changed.wait(timeout=10 if unconfirmed else None)


def dashboard(status):
    phases = {
        "idle": "空闲", "arming": "已接收", "starting": "切换到 ComfyUI", "working": "ComfyUI 运行中",
        "draining": "队列清空", "restoring": "正在恢复 LLM", "waking": "正在唤醒 Hermes", "recovery": "恢复中",
    }
    states = {
        "queued": "待运行", "running": "运行中", "succeeded": "成功",
        "partial": "部分成功", "failed": "失败", "rejected": "未入队",
    }
    rows = "".join(
        f'<tr><td><a href="/v1/batches/{job["id"]}">{job["id"]}</a></td>'
        f'<td title="{escape(job["detail"])}">{states[job["state"]]}</td>'
        f'<td>{job["finished"]} / {job["total"]}</td>'
        f'<td>{escape(job["created_at"])}</td></tr>'
        for job in status["batches"]
    )
    latest = status["latest_wake"]
    wake_text = "还没有交接记录"
    if latest:
        wake_status = {"pending": "待发送", "sending": "发送中", "delivered": "已确认", "unconfirmed": "待核实"}
        wake_text = f'{wake_status[latest["state"]]} · {escape(latest["detail"])}'
    return f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta http-equiv="refresh" content="5">
<title>ComfyUI 显存调度器</title>
<style>
body{{font:16px/1.6 "Segoe UI","Microsoft YaHei",sans-serif;background:#10151c;color:#ecf1f6;max-width:860px;margin:8vh auto;padding:0 24px}}
h1{{font-size:30px;margin-bottom:6px}} h2{{font-size:18px;margin:0 0 12px}}
section{{background:#19232e;border:1px solid #2a3949;border-radius:12px;padding:20px;margin:20px 0;overflow:auto}}
strong,a{{color:#8ce0cb}} p{{margin:6px 0;color:#c2cbd5}} table{{width:100%;border-collapse:collapse;white-space:nowrap}}
td,th{{text-align:left;padding:8px;border-bottom:1px solid #324150}} .small{{font-size:14px}}
</style>
<h1>ComfyUI 显存调度器</h1>
<p class="small">LLM {status["llm_port"]} · ComfyUI {status["comfy_port"]}</p>
<section><h2><strong>{phases[status["phase"]]}</strong></h2><p>{escape(status["detail"])}</p>
<p>Hermes 唤醒 · {wake_text}</p></section>
<section><h2>任务批次</h2>
<table><thead><tr><th>批次</th><th>状态</th><th>已结束</th><th>提交时间</th></tr></thead>
<tbody>{rows or '<tr><td colspan="4">等待 Hermes 提交 API 工作流</td></tr>'}</tbody></table></section></html>"""


def handler_for(bridge):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def reply(self, status, data, html=False):
            body = (data if html else json.dumps(data, ensure_ascii=False, indent=2)).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", ("text/html" if html else "application/json") + "; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def dispatch(self):
            try:
                self.connection.settimeout(15)
                host = self.headers.get("Host")
                port = bridge.config["control_port"]
                if host not in {f"127.0.0.1:{port}", f"localhost:{port}"}:
                    raise HttpError(403, {"error": "请通过本机地址访问"})
                origin = self.headers.get("Origin")
                if origin and origin != f"http://{host}":
                    raise HttpError(403, {"error": "不接受跨站请求"})
                route = urlsplit(self.path).path
                if self.command == "GET":
                    if route == "/":
                        self.reply(200, dashboard(bridge.status()), html=True)
                        return
                    if route == "/health":
                        self.reply(200, {
                            "status": "ok", "phase": bridge.status()["phase"],
                            "service": DESKTOP_TASK,
                        })
                        return
                    if route == "/v1/status":
                        self.reply(200, bridge.status())
                        return
                    if route == "/v1/wake/latest":
                        self.reply(200, bridge.status()["latest_wake"])
                        return
                    if route.startswith("/v1/nodes/"):
                        name = unquote(route.removeprefix("/v1/nodes/"))
                        with bridge.lock:
                            node = bridge.catalog.get(name, BASE.get(name))
                        if node is not None:
                            self.reply(200, node)
                            return
                    match = re.fullmatch(r"/v1/batches/([a-f0-9-]{36})", route)
                    if match:
                        with bridge.lock:
                            job = bridge.jobs.get(match[1])
                            if job:
                                self.reply(200, bridge.public_job(job))
                                return
                elif self.command == "POST" and route in {"/v1/batches", "/v1/validate", "/v1/wake/retry"}:
                    if self.headers.get_content_type() != "application/json":
                        raise HttpError(415, {"error": "请提交 application/json"})
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= MAX_BODY:
                        raise HttpError(413, {"error": "请求体必须在 32 MiB 以内"})
                    payload = json.loads(self.rfile.read(length).decode("utf-8-sig"))
                    if route == "/v1/wake/retry":
                        bridge.request_wake()
                        self.reply(202, {"state": "queued"})
                    elif route == "/v1/validate":
                        _, _, specs = normalize(payload)
                        errors = bridge.check_specs(specs)
                        self.reply(200, {"valid": not errors, "tasks": errors})
                    else:
                        job, created = bridge.submit(payload)
                        self.reply(202 if created else 200, {
                            "id": job["id"], "state": job["state"],
                            "rejected": [
                                task for task in job["tasks"] if task["state"] == "rejected"
                            ],
                        })
                    return
                raise HttpError(404, {"error": "地址或批次不存在"})
            except HttpError as exc:
                self.reply(exc.status, exc.body)
            except (ValueError, UnicodeError) as exc:
                self.reply(400, {"error": str(exc)})
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                pass
            except Exception:
                logging.exception("请求处理失败")
                self.reply(500, {"error": "调度器内部错误，请查看日志"})

        do_GET = dispatch
        do_POST = dispatch

    return Handler


class ExclusiveHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = False

    def server_bind(self):
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def configure_logging(data_dir):
    data_dir.mkdir(parents=True, exist_ok=True)
    handlers = [logging.FileHandler(data_dir / "bridge.log", encoding="utf-8")]
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
    )


def desktop_session():
    session = wintypes.DWORD()
    if not ctypes.windll.kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(session)):
        raise ctypes.WinError()
    return session.value


def serve(config, data_dir):
    import msvcrt

    session = desktop_session()
    if not session:
        raise RuntimeError("调度器需要在登录的 Windows 桌面会话中运行")
    if find_spec("uiautomation") is None:
        raise RuntimeError("调度器使用的 Python 缺少 uiautomation，请重新注册桌面启动入口")
    with (data_dir / "controller.lock").open("a+b") as lock:
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                raise
            raise RuntimeError("此数据目录已有一个调度器在运行") from exc
        bridge = Bridge(config, data_dir)
        port = config["control_port"]
        with ExclusiveHTTPServer(("127.0.0.1", port), handler_for(bridge)) as server:
            logging.info("调度器已启动 http://127.0.0.1:%s/（桌面会话 %s）", port, session)
            threading.Thread(target=bridge.worker, daemon=True, name="gpu-worker").start()
            server.serve_forever()


def start_desktop(port):
    result = hidden("schtasks.exe", "/Run", "/TN", DESKTOP_TASK)
    if result.returncode:
        raise RuntimeError("无法启动桌面调度器，请登录 Windows 并运行 注册桌面启动入口.ps1")
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            health = request(port, "/health", timeout=1)
            if isinstance(health, dict) and health.get("service") == DESKTOP_TASK:
                print(f"调度器已就绪 http://127.0.0.1:{port}/")
                return
        except (OSError, ValueError, HttpError):
            pass
        time.sleep(0.2)
    raise RuntimeError("桌面调度器未就绪，请确认 Windows 已登录并查看 data/bridge.log")


def main():
    parser = argparse.ArgumentParser(description="Hermes / ComfyUI 显存调度器")
    parser.add_argument("--config", type=Path, default=ROOT / "config.json")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("check", "status", "latest-wake", "retry-wake"):
        commands.add_parser(command)
    serve_command = commands.add_parser("serve")
    serve_command.add_argument("--desktop-task", action="store_true", help=argparse.SUPPRESS)
    submit = commands.add_parser("submit")
    submit.add_argument("file", type=Path)
    submit.add_argument("--request-id", type=uuid_text)
    submit.add_argument("--context")
    validate = commands.add_parser("validate")
    validate.add_argument("file", type=Path)
    result = commands.add_parser("result")
    result.add_argument("id", type=uuid_text)
    args = parser.parse_args()
    local_serve = args.command == "serve" and (
        args.desktop_task or args.config.resolve() != ROOT / "config.json"
        or args.data_dir.resolve() != ROOT / "data"
    )
    if local_serve:
        configure_logging(args.data_dir)
    config = read_json(args.config)
    if len({config["control_port"], config["llm_port"], config["comfy_port"]}) != 3:
        raise ValueError("调度器、LLM 和 ComfyUI 必须使用三个不同的端口")
    port = config["control_port"]

    if args.command == "serve":
        if local_serve:
            serve(config, args.data_dir)
        else:
            start_desktop(port)
        return
    if args.command == "check":
        machine = Machine(config)
        machine.check_files()
        owner = machine.llm_owner()
        import uiautomation
        value = {
            "files_ok": True, "gpu": machine.gpu(), "llm_pid": owner[0] if owner else None,
            "llm_ready": machine.llm_ready(), "comfy_listeners": sorted(listening(machine.comfy_port)),
            "hermes_db": str(wake.hermes_db()), "hermes_db_readable": wake.cursor() >= 0,
        }
    elif args.command == "submit":
        value = read_json(args.file)
        if args.request_id or args.context is not None:
            if not isinstance(value, dict) or "prompts" not in value:
                value = {"prompts": value if isinstance(value, list) else [value]}
            if args.request_id:
                value["request_id"] = args.request_id
            if args.context is not None:
                value["context"] = args.context
        value = request(port, "/v1/batches", value, timeout=20)
    elif args.command == "validate":
        value = request(port, "/v1/validate", read_json(args.file), timeout=20)
    elif args.command == "result":
        value = request(port, f"/v1/batches/{args.id}")
    elif args.command == "latest-wake":
        value = request(port, "/v1/wake/latest")
    elif args.command == "retry-wake":
        value = request(port, "/v1/wake/retry", {})
    else:
        value = request(port, "/v1/status")
    print(json.dumps(value, ensure_ascii=False, indent=2))
    if args.command == "validate" and not value["valid"]:
        sys.exit(2)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except (OSError, RuntimeError, ValueError) as exc:
        if sys.stderr is None:
            logging.exception("调度器启动失败")
        else:
            print(f"调度器错误：{exc}", file=sys.stderr)
        sys.exit(1)
