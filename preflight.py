"""Offline checks for the installed ComfyUI's standard node inputs."""
from pathlib import Path


# These rules also cover a fresh installation before /object_info is cached.
BASE = {
    "CheckpointLoaderSimple": {
        "input": {"required": {"ckpt_name": ["COMBO"]}},
        "output": ["MODEL", "CLIP", "VAE"],
    },
    "CLIPTextEncode": {
        "input": {"required": {"clip": ["CLIP"], "text": ["STRING"]}},
        "output": ["CONDITIONING"],
    },
    "KSampler": {
        "input": {"required": {
            "model": ["MODEL"], "seed": ["INT", {"min": 0, "max": 2**64 - 1}],
            "steps": ["INT", {"min": 1}], "cfg": ["FLOAT", {"min": 0}],
            "sampler_name": ["COMBO"], "scheduler": ["COMBO"],
            "positive": ["CONDITIONING"], "negative": ["CONDITIONING"],
            "latent_image": ["LATENT"], "denoise": ["FLOAT", {"min": 0, "max": 1}],
        }},
        "output": ["LATENT"],
    },
    "EmptyLatentImage": {"output": ["LATENT"]},
    "VAEDecode": {"input": {"required": {"samples": ["LATENT"], "vae": ["VAE"]}}, "output": ["IMAGE"]},
    "SaveImage": {"output_node": True, "input": {"required": {"images": ["IMAGE"], "filename_prefix": ["STRING"]}}},
    "PreviewImage": {"output_node": True, "input": {"required": {"images": ["IMAGE"]}}},
}
EXTENSIONS = {".ckpt", ".pt", ".pt2", ".bin", ".pth", ".safetensors", ".pkl", ".sft"}


def checkpoint_names(comfy_root):
    root = Path(comfy_root) / "ComfyUI"
    folder = root / "models/checkpoints"
    if (root / "extra_model_paths.yaml").exists() or not folder.is_dir():
        return None
    return sorted(
        str(path.relative_to(folder)) for path in folder.rglob("*")
        if path.is_file() and path.suffix.lower() in EXTENSIONS
    )


def connected_nodes(graph, targets):
    seen = set()
    remaining = list(targets)
    while remaining:
        node_id = remaining.pop()
        if node_id in seen or node_id not in graph:
            continue
        seen.add(node_id)
        for value in graph[node_id]["inputs"].values():
            if isinstance(value, list) and len(value) == 2 and isinstance(value[0], str):
                remaining.append(value[0])
    return seen


def check_prompt(payload, catalog, checkpoints):
    graph = payload["prompt"]
    schemas = {node_id: catalog.get(node["class_type"], BASE.get(node["class_type"], {}))
               for node_id, node in graph.items()}
    targets = payload.get("partial_execution_targets") or [
        node_id for node_id, schema in schemas.items() if schema.get("output_node")
    ]
    connected = connected_nodes(graph, targets)
    issues = []
    for node_id, node in graph.items():
        if node_id not in connected:
            continue
        schema = schemas[node_id]
        # Custom and dynamic nodes can implement their own input validation.
        if node["class_type"] not in BASE:
            continue
        inputs = node["inputs"]
        required = schema.get("input", {}).get("required", {})
        for name, spec in required.items():
            error = None
            if name not in inputs:
                error = "缺少必填输入"
            else:
                value = inputs[name]
                expected = spec[0]
                limits = spec[1] if len(spec) > 1 else {}
                if isinstance(value, list):
                    if len(value) != 2 or not isinstance(value[0], str) or value[0] not in graph:
                        error = "连线引用的源节点不存在，或连线格式不正确"
                    elif type(value[1]) is not int or value[1] < 0:
                        error = "连线输出编号必须是从 0 开始的整数"
                    else:
                        outputs = schemas[value[0]].get("output")
                        if outputs is not None and value[1] >= len(outputs):
                            error = f"节点 {value[0]} 没有第 {value[1]} 个输出"
                        elif outputs is not None and isinstance(expected, str):
                            actual = outputs[value[1]]
                            if isinstance(actual, str) and "*" not in (actual, expected):
                                if not set(actual.split(",")) & set(expected.split(",")):
                                    error = f"需要 {expected}，节点 {value[0]} 的输出 {value[1]} 是 {actual}"
                elif node["class_type"] == "CheckpointLoaderSimple" and name == "ckpt_name":
                    if checkpoints is not None and value not in checkpoints:
                        error = f"本机没有 checkpoint {value!r}。可用文件：{', '.join(checkpoints) or '无'}"
                elif expected in ("INT", "FLOAT") and isinstance(value, (int, float)):
                    if "min" in limits and value < limits["min"]:
                        error = f"{value} 小于最小值 {limits['min']}"
                    elif "max" in limits and value > limits["max"]:
                        error = f"{value} 大于最大值 {limits['max']}"
                elif isinstance(expected, list) and value not in expected:
                    error = f"{value!r} 不在可用选项中"
            if error:
                issues.append({"node": node_id, "class_type": node["class_type"], "input": name, "error": error})
    return issues
