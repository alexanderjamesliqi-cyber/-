import json
import os
import re
import subprocess
import tempfile
import uuid
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_file


PORT = int(os.environ.get("PORT", "8787"))
HOST = os.environ.get("HOST", "127.0.0.1")
SCRIPT_DIR = Path(__file__).resolve().parent
MODEL_CONFIG_PATH = Path(os.environ.get("MODEL_CONFIG_PATH", SCRIPT_DIR / "model.config"))
EDITOR_HTML_PATH = SCRIPT_DIR / "teaching_interactive_editor.html"
MAX_BODY_BYTES = int(os.environ.get("MAX_BODY_BYTES", "2000000"))
REQUEST_TIMEOUT = int(os.environ.get("REQUEST_TIMEOUT", "0"))
COMPONENT_RETRY_ATTEMPTS = int(os.environ.get("COMPONENT_RETRY_ATTEMPTS", "2"))

app = Flask(__name__)


DEFAULT_MODEL_CONFIG = {
    "providers": [
        {
            "id": "scnet",
            "name": "SCNet",
            "url": "https://api.scnet.cn/api/llm/v1/chat/completions",
            "apiKey": os.environ.get("SCNET_API_KEY", "PASTE_SCNET_API_KEY"),
            "models": [
                {"id": "MiniMax-M2.5", "label": "MiniMax-M2.5"},
                {"id": "DeepSeek-V4-Flash", "label": "DeepSeek-V4-Flash"},
            ],
        },
        {
            "id": "nvidia",
            "name": "NVIDIA",
            "url": "https://integrate.api.nvidia.com/v1/chat/completions",
            "apiKey": os.environ.get("NVIDIA_API_KEY", "PASTE_NVIDIA_API_KEY"),
            "models": [
                {"id": "deepseek-ai/deepseek-v4-flash", "label": "deepseek-ai/deepseek-v4-flash"},
                {"id": "nvidia/llama-3.3-nemotron-super-49b-v1.5", "label": "nvidia/llama-3.3-nemotron-super-49b-v1.5"},
                {"id": "qwen/qwen3-coder-480b-a35b-instruct", "label": "qwen/qwen3-coder-480b-a35b-instruct"},
                {"id": "openai/gpt-oss-120b", "label": "openai/gpt-oss-120b"},
                {"id": "meta/llama-3.1-405b-instruct", "label": "meta/llama-3.1-405b-instruct"},
                {"id": "meta/llama-3.3-70b-instruct", "label": "meta/llama-3.3-70b-instruct"},
            ],
        },
    ]
}


COMPONENT_SYSTEM_PROMPT = "\n".join(
    [
        "你是教学交互组件模板生成器，目标是把用户的自然语言需求变成左侧组件栏里可拖拽复用的互动组件。",
        "只能返回严格 JSON，不要返回 Markdown，不要解释。",
        "JSON 必须是单个对象，所有 key 和字符串必须使用英文双引号。",
        "不要在字符串中直接换行；不要使用中文引号；不要漏逗号；不要写注释。",
        "代码不要放进带换行的大字符串。HTML/CSS/JS 必须优先用字符串数组字段 htmlLines、cssLines、jsLines，每一行一个 JSON 字符串。",
        "数组里的代码行如果需要引号，HTML 属性和 JS 字符串优先使用英文单引号，避免破坏 JSON 双引号。",
        "你的任务不是生成正文里的实例，而是生成可放入左侧组件栏的可复用组件模板；用户之后会拖入正文。",
        "优先理解用户要的交互流程、输入、按钮、反馈、随机性、状态展示和教学目标。",
        'JSON 格式：{"templates":[{"name":"组件名称","description":"一句话说明","baseType":"custom-interactive|choice-question|fill-blank|info-card|function-plot","defaultConfig":{...}}]}。',
        "最多返回 2 个模板；除非用户明确要选择题、填空题、知识卡片或函数绘图，否则优先使用 custom-interactive。",
        "custom-interactive defaultConfig 字段：title, summary, htmlLines, cssLines, jsLines；不要再使用 html/css/js 大段字符串。",
        "custom-interactive 的 htmlLines 只写组件内部结构，严禁写 script/style/html/body 标签；所有样式必须放在 cssLines；jsLines 是函数体，会以 root 参数执行。",
        "custom-interactive 的 jsLines 必须是直接执行的函数体语句，不要包裹 IIFE，不要写 window.onload/addEventListener('load')，不要定义 init(root) 这类需要外部调用的函数。",
        "运行时已经提供 root 和 helpers 两个变量；root 是当前组件内部根节点 HTMLElement。严禁声明 const root/let root/var root，严禁定义名为 root 的函数或参数。",
        "custom-interactive 的 js 必须使用 root.querySelector/root.querySelectorAll 查找元素，不要使用 document.querySelector/document.getElementById；可以使用 document.createElement；不要请求网络；不要使用 alert；所有交互应在组件内即时反馈。",
        '如果组件需要 2D/3D 绘图、坐标系、视角、旋转、透视或动画，html 必须包含真实 <canvas> 标签；js 必须用 const { canvas, context: ctx } = helpers.context2d("canvas") 获取画布和 2D 上下文，不要对 div 或 root 调用 getContext。',
        "canvas 绘图组件应设置 canvas 的 width/height 属性和 CSS 尺寸，使用 requestAnimationFrame 时应提供暂停/重置或避免无限高负载；不允许依赖 Three.js 等外部网络库。",
        "custom-interactive 必须是紧凑的正文内教学组件，不是整页网页；不要使用 body/html 样式、fixed/absolute 页面定位、超大留白或超过 420px 的固定高度。",
        "custom-interactive 必须能直接运行，有完整默认状态，有清晰按钮、输入、结果区域、状态反馈和必要的重置/清空按钮。",
        "核心交互必须使用 button、input、select、textarea 等语义控件；不要只让装饰性的 div 承担点击操作；按钮文字要清晰。",
        "如果组件涉及随机、范围、数量、题目数量、时间、阈值或分类，应提供可编辑输入控件，并做输入校验和错误提示。",
        "如果组件包含历史记录或列表，最多显示最近 10 条，并提供清空按钮，避免无限增长。",
        "组件初始状态不得预先显示随机结果；必须等待用户操作后才显示结果。",
        "CSS 必须让组件在 320px 到 700px 宽度内不横向滚动，控件文字不重叠；所有容器 max-width:100%，避免固定宽度超过父容器；按钮和输入应有清晰 hover/focus 状态。",
        "组件拖入正文后应和前后段落贴合，外层留白控制在 12-18px，不能生成会把正文撑开的大画布或整屏容器。",
        "class 名尽量使用组件相关前缀，避免影响页面其他内容。",
        "比如抽小球组件应包含最大编号输入、明确的抽取按钮、当前结果、最近历史、清空历史和非法输入提示；历史最多保留 10 条。",
        "baseType 决定这个自定义组件复用哪一种组件能力。",
        "choice-question defaultConfig 字段：question, options(数组), answer, correctFeedback, wrongFeedback。",
        "fill-blank defaultConfig 字段：question, answer, placeholder, correctFeedback, wrongFeedback。",
        "info-card defaultConfig 字段：title, content。",
        "function-plot defaultConfig 字段：title, expression, xMin, xMax, yMin, yMax。",
        "模板内容必须适合当前教学页面，拖入正文后应能作为一个完整互动组件使用。",
    ]
)


def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = (
        "Content-Type, X-Provider"
    )
    return response


def normalize_provider_id(value):
    normalized = re.sub(r"[^a-z0-9_-]+", "-", str(value or "").strip().lower())
    return normalized.strip("-") or f"provider-{uuid.uuid4().hex[:8]}"


def normalize_model_config(config):
    providers = []
    for provider in config.get("providers", []):
        if not isinstance(provider, dict):
            continue

        provider_id = normalize_provider_id(provider.get("id") or provider.get("name"))
        models = []
        seen_models = set()
        for model in provider.get("models", []):
            if isinstance(model, str):
                model = {"id": model, "label": model}
            if not isinstance(model, dict):
                continue
            model_id = str(model.get("id") or model.get("value") or "").strip()
            if not model_id or model_id in seen_models:
                continue
            seen_models.add(model_id)
            models.append(
                {
                    "id": model_id,
                    "label": str(model.get("label") or model_id).strip() or model_id,
                }
            )

        providers.append(
            {
                "id": provider_id,
                "name": str(provider.get("name") or provider_id).strip() or provider_id,
                "url": str(provider.get("url") or provider.get("upstream") or "").strip(),
                "apiKey": str(provider.get("apiKey") or provider.get("api_key") or "").strip(),
                "models": models,
            }
        )

    return {"providers": providers}


def load_model_config():
    if not MODEL_CONFIG_PATH.exists():
        save_model_config(DEFAULT_MODEL_CONFIG)

    try:
        with MODEL_CONFIG_PATH.open("r", encoding="utf-8") as file:
            return normalize_model_config(json.load(file))
    except (OSError, json.JSONDecodeError):
        return normalize_model_config(DEFAULT_MODEL_CONFIG)


def save_model_config(config):
    normalized = normalize_model_config(config)
    MODEL_CONFIG_PATH.write_text(
        json.dumps(normalized, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return normalized


def public_model_config(config=None):
    config = config or load_model_config()
    return {
        "providers": [
            {
                "id": provider["id"],
                "name": provider["name"],
                "url": provider["url"],
                "apiKeyConfigured": not is_placeholder_key(provider.get("apiKey")),
                "models": provider["models"],
            }
            for provider in config["providers"]
        ]
    }


def build_component_user_prompt(component_request):
    prompt = str(
        component_request.get("prompt")
        or "请根据当前教学内容设计一个可复用的交互组件模板，加入左侧组件栏供用户拖拽使用。"
    )
    chat_context = str(component_request.get("chatContext") or "无")
    editor_text = str(component_request.get("editorText") or "")

    return "\n".join(
        [
            prompt,
            "",
            "最近对话：",
            chat_context[:4000],
            "",
            "当前教学内容：",
            editor_text[:4000],
        ]
    )


def prepare_model_payload(body):
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return body

    component_request = payload.pop("componentRequest", None)
    if not isinstance(component_request, dict):
        return body

    payload["messages"] = [
        {"role": "system", "content": COMPONENT_SYSTEM_PROMPT},
        {"role": "user", "content": build_component_user_prompt(component_request)},
    ]
    payload["temperature"] = min(float(payload.get("temperature") or 0.2), 0.2)
    payload.setdefault("max_tokens", 8192)
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def build_component_retry_prompt(component_request, invalid_content, error_message, attempt):
    return "\n".join(
        [
            build_component_user_prompt(component_request),
            "",
            f"上一次生成结果无法解析，这是第 {attempt} 次重新生成。",
            f"解析错误：{error_message}",
            "上一次返回片段：",
            str(invalid_content or "")[:2500],
            "",
            "请重新生成完整组件模板。必须只返回严格 JSON 对象，不要解释，不要 Markdown。",
            "必须使用 htmlLines、cssLines、jsLines 数组，每个数组元素都是一行 JSON 字符串。",
            "不要在 JSON 字符串中写未转义换行，不要漏逗号，不要在末尾多逗号。",
        ]
    )


def prepare_retry_model_payload(body, component_request, invalid_content, error_message, attempt):
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return body

    payload["messages"] = [
        {"role": "system", "content": COMPONENT_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": build_component_retry_prompt(
                component_request,
                invalid_content,
                error_message,
                attempt,
            ),
        },
    ]
    payload["temperature"] = 0
    payload["stream"] = False
    payload.setdefault("max_tokens", 8192)
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def read_component_request(body):
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None

    component_request = payload.get("componentRequest")
    return component_request if isinstance(component_request, dict) else None


def extract_json_like_text(text):
    text = str(text or "").replace("```json", "").replace("```", "").strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return text
    return text[start : end + 1]


def repair_json_like_text(text):
    repaired = str(text or "")
    replacements = {
        "“": '"',
        "”": '"',
        "‘": "'",
        "’": "'",
        "，": ",",
        "：": ":",
    }
    for source, target in replacements.items():
        repaired = repaired.replace(source, target)
    repaired = re.sub(r",\s*([}\]])", r"\1", repaired)
    repaired = re.sub(
        r"([{,]\s*)([A-Za-z_][A-Za-z0-9_-]*)(\s*:)", r'\1"\2"\3', repaired
    )
    repaired = re.sub(
        r'("(?:[^"\\]|\\.)*"|\d+(?:\.\d+)?|true|false|null|[}\]])\s*\n\s*(")',
        r"\1,\n\2",
        repaired,
    )
    repaired = re.sub(r"([}\]])\s*\n\s*([{\[])", r"\1,\n\2", repaired)
    return repaired


def parse_component_content(content):
    text = extract_json_like_text(content)
    last_error = None
    for candidate in (text, repair_json_like_text(text)):
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError as exc:
            last_error = exc
            continue
        templates = parsed.get("templates") if isinstance(parsed, dict) else None
        if isinstance(templates, list) and templates:
            return parsed, None
        last_error = ValueError("JSON 中缺少非空 templates 数组")
    if last_error:
        return None, str(last_error)
    return None, "模型没有返回可解析 JSON"


def component_content_is_valid(content):
    parsed, _ = parse_component_content(content)
    return parsed is not None


def normalize_component_content(content, component_request):
    parsed, _ = parse_component_content(content)
    if parsed:
        return json.dumps(parsed, ensure_ascii=False)
    return None


def vector_projection_fallback_template():
    return {
        "name": "平面向量点积与投影",
        "description": "用画布演示点积、夹角和向量投影的几何意义。",
        "baseType": "custom-interactive",
        "defaultConfig": {
            "title": "平面向量点积与投影",
            "summary": "调整向量坐标，观察点积、夹角和 a 在 b 方向上的投影。",
            "htmlLines": [
                "<div class='vector-dot-demo'>",
                "  <canvas class='vector-dot-canvas' width='560' height='340' aria-label='平面向量点积与投影画布'></canvas>",
                "  <div class='vector-dot-controls'>",
                "    <label>向量 a：<input data-role='ax' type='number' value='3' step='0.5'> <input data-role='ay' type='number' value='2' step='0.5'></label>",
                "    <label>向量 b：<input data-role='bx' type='number' value='4' step='0.5'> <input data-role='by' type='number' value='1' step='0.5'></label>",
                "    <button data-role='draw'>更新图像</button>",
                "    <button data-role='swap'>交换 a / b</button>",
                "    <button data-role='reset'>重置</button>",
                "  </div>",
                "  <div class='vector-dot-result' data-role='result'></div>",
                "</div>",
            ],
            "cssLines": [
                ".vector-dot-demo { max-width: 100%; padding: 14px; border: 1px solid #dbe3ef; border-radius: 12px; background: #f8fafc; }",
                ".vector-dot-canvas { display: block; width: 100%; max-width: 640px; height: auto; border: 1px solid #cbd5e1; border-radius: 10px; background: #ffffff; }",
                ".vector-dot-controls { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; margin-top: 12px; }",
                ".vector-dot-controls label { display: inline-flex; gap: 6px; align-items: center; font-size: 14px; color: #334155; }",
                ".vector-dot-controls input { width: 64px; padding: 6px 8px; border: 1px solid #cbd5e1; border-radius: 8px; }",
                ".vector-dot-controls button { border: none; border-radius: 8px; padding: 7px 12px; background: #2563eb; color: #fff; cursor: pointer; }",
                ".vector-dot-controls button:nth-of-type(2) { background: #64748b; }",
                ".vector-dot-result { margin-top: 10px; line-height: 1.6; color: #0f172a; font-size: 14px; }",
            ],
            "jsLines": [
                "const { canvas, context: ctx } = helpers.context2d('canvas');",
                "const result = root.querySelector('[data-role=\"result\"]');",
                "const input = role => root.querySelector(`[data-role=\"${role}\"]`);",
                "const scale = 42;",
                "function readVector(prefix) { return { x: Number(input(prefix + 'x').value) || 0, y: Number(input(prefix + 'y').value) || 0 }; }",
                "function setVector(prefix, v) { input(prefix + 'x').value = v.x; input(prefix + 'y').value = v.y; }",
                "function toCanvas(v) { return { x: canvas.width / 2 + v.x * scale, y: canvas.height / 2 - v.y * scale }; }",
                "function drawArrow(v, color, label) { const origin = toCanvas({ x: 0, y: 0 }); const end = toCanvas(v); const angle = Math.atan2(end.y - origin.y, end.x - origin.x); ctx.beginPath(); ctx.moveTo(origin.x, origin.y); ctx.lineTo(end.x, end.y); ctx.lineTo(end.x - 12 * Math.cos(angle - Math.PI / 6), end.y - 12 * Math.sin(angle - Math.PI / 6)); ctx.moveTo(end.x, end.y); ctx.lineTo(end.x - 12 * Math.cos(angle + Math.PI / 6), end.y - 12 * Math.sin(angle + Math.PI / 6)); ctx.strokeStyle = color; ctx.lineWidth = 3; ctx.stroke(); ctx.fillStyle = color; ctx.font = '14px sans-serif'; ctx.fillText(label, end.x + 6, end.y - 6); }",
                "function drawGrid() { ctx.clearRect(0, 0, canvas.width, canvas.height); ctx.lineWidth = 1; ctx.strokeStyle = '#e2e8f0'; for (let x = canvas.width / 2 % scale; x < canvas.width; x += scale) { ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, canvas.height); ctx.stroke(); } for (let y = canvas.height / 2 % scale; y < canvas.height; y += scale) { ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(canvas.width, y); ctx.stroke(); } ctx.strokeStyle = '#94a3b8'; ctx.beginPath(); ctx.moveTo(0, canvas.height / 2); ctx.lineTo(canvas.width, canvas.height / 2); ctx.moveTo(canvas.width / 2, 0); ctx.lineTo(canvas.width / 2, canvas.height); ctx.stroke(); }",
                "function drawProjection(a, b) { const bLen2 = b.x * b.x + b.y * b.y; if (!bLen2) return null; const factor = (a.x * b.x + a.y * b.y) / bLen2; const p = { x: factor * b.x, y: factor * b.y }; const cp = toCanvas(p); const ca = toCanvas(a); ctx.setLineDash([6, 5]); ctx.strokeStyle = '#f97316'; ctx.lineWidth = 2; ctx.beginPath(); ctx.moveTo(ca.x, ca.y); ctx.lineTo(cp.x, cp.y); ctx.stroke(); ctx.setLineDash([]); drawArrow(p, '#f97316', 'proj_b(a)'); return p; }",
                "function render() { const a = readVector('a'); const b = readVector('b'); const dot = a.x * b.x + a.y * b.y; const lenA = Math.hypot(a.x, a.y); const lenB = Math.hypot(b.x, b.y); const cos = lenA && lenB ? dot / (lenA * lenB) : 0; const angle = lenA && lenB ? Math.acos(Math.max(-1, Math.min(1, cos))) * 180 / Math.PI : 0; drawGrid(); drawArrow(a, '#2563eb', 'a'); drawArrow(b, '#16a34a', 'b'); const p = drawProjection(a, b); result.innerHTML = `a · b = ${dot.toFixed(2)}；夹角约 ${angle.toFixed(1)}°；${p ? `a 在 b 方向上的投影向量约 (${p.x.toFixed(2)}, ${p.y.toFixed(2)})` : '向量 b 为零，无法计算投影。'}`; }",
                "input('draw').addEventListener('click', render);",
                "input('swap').addEventListener('click', () => { const a = readVector('a'); const b = readVector('b'); setVector('a', b); setVector('b', a); render(); });",
                "input('reset').addEventListener('click', () => { setVector('a', { x: 3, y: 2 }); setVector('b', { x: 4, y: 1 }); render(); });",
                "render();",
            ],
        },
    }


def conic_fallback_template():
    return {
        "name": "圆锥曲线几何定义演示",
        "description": "展示平面截圆锥形成椭圆、抛物线、双曲线，支持拖拽旋转视角。",
        "baseType": "custom-interactive",
        "defaultConfig": {
            "title": "圆锥曲线几何定义",
            "summary": "拖动鼠标旋转视角，调整截面角度和高度，观察圆锥曲线类型变化。",
            "htmlLines": [
                "<div class='conic-demo'>",
                "  <canvas class='conic-canvas' width='620' height='380' aria-label='圆锥曲线三维示意画布'></canvas>",
                "  <div class='conic-controls'>",
                "    <label>截面角度 <input data-role='angle' type='range' min='10' max='80' value='35'> <span data-role='angleText'>35°</span></label>",
                "    <label>截面高度 <input data-role='height' type='range' min='20' max='78' value='48'> <span data-role='heightText'>48%</span></label>",
                "    <button data-role='reset'>重置视角</button>",
                "  </div>",
                "  <div class='conic-result' data-role='result'></div>",
                "</div>",
            ],
            "cssLines": [
                ".conic-demo { max-width: 100%; padding: 14px; border: 1px solid #dbe3ef; border-radius: 12px; background: #f8fafc; }",
                ".conic-canvas { display: block; width: 100%; max-width: 660px; height: auto; min-height: 260px; border: 1px solid #cbd5e1; border-radius: 10px; background: #ffffff; cursor: grab; touch-action: none; }",
                ".conic-canvas:active { cursor: grabbing; }",
                ".conic-controls { display: grid; gap: 10px; margin-top: 12px; }",
                ".conic-controls label { display: grid; grid-template-columns: 72px 1fr 48px; gap: 8px; align-items: center; color: #334155; font-size: 14px; }",
                ".conic-controls button { justify-self: start; border: none; border-radius: 8px; padding: 7px 12px; background: #2563eb; color: #fff; cursor: pointer; }",
                ".conic-result { margin-top: 10px; padding: 10px; border-radius: 8px; background: #eef6ff; color: #0f172a; line-height: 1.6; font-size: 14px; }",
            ],
            "jsLines": [
                "const { canvas, context: ctx } = helpers.context2d('canvas');",
                "const angleInput = root.querySelector('[data-role=\"angle\"]');",
                "const heightInput = root.querySelector('[data-role=\"height\"]');",
                "const angleText = root.querySelector('[data-role=\"angleText\"]');",
                "const heightText = root.querySelector('[data-role=\"heightText\"]');",
                "const result = root.querySelector('[data-role=\"result\"]');",
                "const reset = root.querySelector('[data-role=\"reset\"]');",
                "let yaw = -0.55;",
                "let pitch = 0.72;",
                "let dragging = false;",
                "let last = { x: 0, y: 0 };",
                "function project(p) { const cy = Math.cos(yaw), sy = Math.sin(yaw); const cp = Math.cos(pitch), sp = Math.sin(pitch); const x1 = p.x * cy - p.z * sy; const z1 = p.x * sy + p.z * cy; const y1 = p.y * cp - z1 * sp; const z2 = p.y * sp + z1 * cp; const scale = 180 / (260 + z2); return { x: canvas.width / 2 + x1 * scale, y: canvas.height * 0.58 - y1 * scale, z: z2 }; }",
                "function path(points) { ctx.beginPath(); points.forEach((p, i) => { const q = project(p); if (i) ctx.lineTo(q.x, q.y); else ctx.moveTo(q.x, q.y); }); }",
                "function drawArrow(a, b, color, label) { const p = project(a), q = project(b); const ang = Math.atan2(q.y - p.y, q.x - p.x); ctx.strokeStyle = color; ctx.fillStyle = color; ctx.lineWidth = 2; ctx.beginPath(); ctx.moveTo(p.x, p.y); ctx.lineTo(q.x, q.y); ctx.stroke(); ctx.beginPath(); ctx.moveTo(q.x, q.y); ctx.lineTo(q.x - 9 * Math.cos(ang - Math.PI / 6), q.y - 9 * Math.sin(ang - Math.PI / 6)); ctx.lineTo(q.x - 9 * Math.cos(ang + Math.PI / 6), q.y - 9 * Math.sin(ang + Math.PI / 6)); ctx.closePath(); ctx.fill(); ctx.font = '13px sans-serif'; ctx.fillText(label, q.x + 6, q.y - 6); }",
                "function classify(angle) { if (angle < 42) return ['椭圆', '截面同时切过圆锥两侧母线，形成封闭曲线。']; if (angle < 56) return ['抛物线', '截面近似平行于一条母线，形成抛物线。']; return ['双曲线', '截面更陡，穿过双锥的两支，形成双曲线。']; }",
                "function render() { const angle = Number(angleInput.value); const height = Number(heightInput.value); angleText.textContent = angle + '°'; heightText.textContent = height + '%'; const type = classify(angle); ctx.clearRect(0, 0, canvas.width, canvas.height); ctx.fillStyle = '#fff'; ctx.fillRect(0, 0, canvas.width, canvas.height); ctx.strokeStyle = '#edf2f7'; ctx.lineWidth = 1; for (let x = 24; x < canvas.width; x += 28) { ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, canvas.height); ctx.stroke(); } for (let y = 24; y < canvas.height; y += 28) { ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(canvas.width, y); ctx.stroke(); } drawArrow({x:0,y:0,z:0},{x:1.25,y:0,z:0},'#2563eb','x'); drawArrow({x:0,y:0,z:0},{x:0,y:0,z:1.25},'#16a34a','y'); drawArrow({x:0,y:0,z:0},{x:0,y:1.65,z:0},'#dc2626','z'); const levels = [-1, 0, 1]; ctx.strokeStyle = '#60a5fa'; ctx.fillStyle = 'rgba(96,165,250,0.16)'; ctx.lineWidth = 2; for (const sign of [-1, 1]) { for (let i = 0; i < 15; i++) { const y = sign * i / 14 * 1.45; const r = Math.abs(y) * 0.58; const pts = []; for (let t = 0; t <= Math.PI * 2 + 0.01; t += Math.PI / 36) pts.push({x: Math.cos(t) * r, y, z: Math.sin(t) * r}); path(pts); ctx.stroke(); } for (let t = 0; t < Math.PI * 2; t += Math.PI / 4) { const pts = []; for (let i = 0; i <= 14; i++) { const y = sign * i / 14 * 1.45; const r = Math.abs(y) * 0.58; pts.push({x: Math.cos(t) * r, y, z: Math.sin(t) * r}); } path(pts); ctx.stroke(); } } const h = (height - 50) / 38; const tilt = (angle - 45) * Math.PI / 180; const plane = []; for (const p of [[-0.9,-0.55],[0.9,-0.55],[0.9,0.55],[-0.9,0.55]]) plane.push({x:p[0], y:h + p[1] * Math.sin(tilt), z:p[1] * Math.cos(tilt)}); ctx.fillStyle = 'rgba(245,158,11,0.25)'; ctx.strokeStyle = '#f59e0b'; path(plane); ctx.closePath(); ctx.fill(); ctx.stroke(); ctx.strokeStyle = '#7c3aed'; ctx.lineWidth = 3; const curve = []; const rx = angle < 42 ? 0.38 : angle < 56 ? 0.46 : 0.55; const rz = angle < 42 ? 0.18 : angle < 56 ? 0.28 : 0.12; for (let t = 0; t <= Math.PI * 2 + 0.01; t += Math.PI / 60) curve.push({x: Math.cos(t) * rx, y: h + Math.sin(t) * rz * Math.sin(tilt), z: Math.sin(t) * rz * Math.cos(tilt)}); path(curve); ctx.stroke(); result.innerHTML = `<strong>${type[0]}</strong>：${type[1]}<br>拖动图像可旋转视角，滑块可改变截面。`; }",
                "canvas.addEventListener('pointerdown', event => { dragging = true; last = { x: event.clientX, y: event.clientY }; canvas.setPointerCapture(event.pointerId); });",
                "canvas.addEventListener('pointermove', event => { if (!dragging) return; yaw += (event.clientX - last.x) * 0.008; pitch += (event.clientY - last.y) * 0.006; pitch = Math.max(0.15, Math.min(1.35, pitch)); last = { x: event.clientX, y: event.clientY }; render(); });",
                "canvas.addEventListener('pointerup', () => { dragging = false; });",
                "canvas.addEventListener('pointercancel', () => { dragging = false; });",
                "angleInput.addEventListener('input', render);",
                "heightInput.addEventListener('input', render);",
                "reset.addEventListener('click', () => { yaw = -0.55; pitch = 0.72; angleInput.value = 35; heightInput.value = 48; render(); });",
                "render();",
            ],
        },
    }


def generic_fallback_template(component_request):
    prompt = str(component_request.get("prompt") or "自定义互动组件")
    return {
        "name": "互动组件草稿",
        "description": "模型输出异常时生成的可编辑组件草稿。",
        "baseType": "custom-interactive",
        "defaultConfig": {
            "title": prompt[:32],
            "summary": "这是一个可编辑的组件草稿，请在右侧继续调整 HTML、CSS 和 JavaScript。",
            "htmlLines": [
                "<div class='draft-widget'>",
                "  <p data-role='text'>组件需求已记录，可以继续编辑完善。</p>",
                "  <button data-role='toggle'>查看需求</button>",
                "  <div data-role='detail' hidden></div>",
                "</div>",
            ],
            "cssLines": [
                ".draft-widget { padding: 14px; border: 1px solid #dbe3ef; border-radius: 12px; background: #f8fafc; }",
                ".draft-widget button { border: none; border-radius: 8px; padding: 7px 12px; background: #2563eb; color: #fff; cursor: pointer; }",
                ".draft-widget [data-role='detail'] { margin-top: 10px; color: #334155; line-height: 1.6; }",
            ],
            "jsLines": [
                f"const requirement = {json.dumps(prompt, ensure_ascii=False)};",
                "const detail = root.querySelector('[data-role=\"detail\"]');",
                "detail.textContent = requirement;",
                "root.querySelector('[data-role=\"toggle\"]').addEventListener('click', () => { detail.hidden = !detail.hidden; });",
            ],
        },
    }


def build_fallback_component_content(component_request):
    prompt = str(component_request.get("prompt") or "")
    if any(word in prompt for word in ("圆锥", "圆锥曲线", "椭圆", "抛物线", "双曲线")):
        template = conic_fallback_template()
    elif "向量" in prompt and ("点积" in prompt or "投影" in prompt):
        template = vector_projection_fallback_template()
    else:
        template = generic_fallback_template(component_request)
    return json.dumps({"templates": [template]}, ensure_ascii=False)


def extract_component_response_content(response_body):
    try:
        payload = json.loads(response_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, None, f"接口返回的响应不是 JSON：{exc}"

    choices = payload.setdefault("choices", [{"message": {}}])
    if not choices:
        choices.append({"message": {}})
    message = choices[0].setdefault("message", {})
    content = message.get("content") or choices[0].get("text") or ""
    return payload, message, content


def normalize_component_response(response_body):
    payload, message, content = extract_component_response_content(response_body)
    if payload is None:
        return None, content, message

    parsed, error = parse_component_content(content)
    if not parsed:
        return None, content, error

    message["content"] = json.dumps(parsed, ensure_ascii=False)
    return json.dumps(payload, ensure_ascii=False).encode("utf-8"), None, None


def component_generation_error_response(status, message):
    return jsonify({"error": message}), status


def quote_curl_config(value):
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def parse_curl_headers(header_text):
    blocks = [
        block
        for block in header_text.replace("\r\n", "\n").split("\n\n")
        if block.strip()
    ]
    if not blocks:
        return 502, "application/json; charset=utf-8"

    lines = blocks[-1].splitlines()
    status = 502
    content_type = "application/json; charset=utf-8"

    if lines and lines[0].startswith("HTTP/"):
        parts = lines[0].split()
        if len(parts) > 1 and parts[1].isdigit():
            status = int(parts[1])

    for line in lines[1:]:
        name, sep, value = line.partition(":")
        if sep and name.lower() == "content-type":
            content_type = value.strip() or content_type
            break

    return status, content_type


def post_with_curl(upstream, body, content_type, authorization):
    request_file = tempfile.NamedTemporaryFile(delete=False)
    response_file = tempfile.NamedTemporaryFile(delete=False)
    header_file = tempfile.NamedTemporaryFile(delete=False)

    try:
        request_file.write(body)
        request_file.close()
        response_file.close()
        header_file.close()

        config_lines = [
            f'url = "{quote_curl_config(upstream)}"',
            "request = POST",
            f'header = "Content-Type: {quote_curl_config(content_type)}"',
            f'data-binary = "@{quote_curl_config(request_file.name)}"',
        ]
        if authorization:
            config_lines.append(
                f'header = "Authorization: {quote_curl_config(authorization)}"'
            )

        command = [
            "curl",
            "-sS",
            "-D",
            header_file.name,
            "-o",
            response_file.name,
            "--config",
            "-",
        ]
        if REQUEST_TIMEOUT > 0:
            command[2:2] = ["--max-time", str(REQUEST_TIMEOUT)]

        proc = subprocess.run(
            command,
            input="\n".join(config_lines) + "\n",
            text=True,
            capture_output=True,
            timeout=REQUEST_TIMEOUT + 5 if REQUEST_TIMEOUT > 0 else None,
        )

        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or f"curl exited {proc.returncode}")

        with open(header_file.name, "r", encoding="utf-8", errors="replace") as file:
            status, response_content_type = parse_curl_headers(file.read())
        with open(response_file.name, "rb") as file:
            response_body = file.read()

        return status, response_content_type, response_body
    finally:
        for path in (request_file.name, response_file.name, header_file.name):
            try:
                os.unlink(path)
            except OSError:
                pass


def stream_with_curl(upstream, body, content_type, authorization):
    request_file = tempfile.NamedTemporaryFile(delete=False)
    proc = None

    try:
        request_file.write(body)
        request_file.close()

        config_lines = [
            f'url = "{quote_curl_config(upstream)}"',
            "request = POST",
            f'header = "Content-Type: {quote_curl_config(content_type)}"',
            f'data-binary = "@{quote_curl_config(request_file.name)}"',
        ]
        if authorization:
            config_lines.append(
                f'header = "Authorization: {quote_curl_config(authorization)}"'
            )

        command = [
            "curl",
            "-sS",
            "-N",
            "--config",
            "-",
        ]
        if REQUEST_TIMEOUT > 0:
            command[3:3] = ["--max-time", str(REQUEST_TIMEOUT)]

        proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        proc.stdin.write(("\n".join(config_lines) + "\n").encode("utf-8"))
        proc.stdin.close()

        for line in iter(proc.stdout.readline, b""):
            if line:
                yield line

        proc.wait(timeout=5)
        if proc.returncode:
            error = proc.stderr.read().decode("utf-8", errors="replace").strip()
            payload = json.dumps({"error": error or f"curl exited {proc.returncode}"})
            yield f"event: error\ndata: {payload}\n\n".encode("utf-8")
    finally:
        if proc and proc.poll() is None:
            proc.kill()
        try:
            os.unlink(request_file.name)
        except OSError:
            pass


def proxy_with_curl(upstream, body, content_type, authorization, component_request=None):
    try:
        status, response_content_type, response_body = post_with_curl(
            upstream, body, content_type, authorization
        )
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502

    if component_request and status == 200:
        normalized_body, invalid_content, parse_error = normalize_component_response(
            response_body
        )
        response_content_type = "application/json; charset=utf-8"

        for attempt in range(1, COMPONENT_RETRY_ATTEMPTS + 1):
            if normalized_body:
                break

            retry_body = prepare_retry_model_payload(
                body,
                component_request,
                invalid_content,
                parse_error,
                attempt,
            )
            try:
                status, response_content_type, response_body = post_with_curl(
                    upstream,
                    retry_body,
                    content_type,
                    authorization,
                )
            except Exception as exc:
                return jsonify({"error": f"重新生成失败：{exc}"}), 502

            if status != 200:
                return Response(
                    response_body,
                    status=status,
                    content_type=response_content_type,
                )

            normalized_body, invalid_content, parse_error = normalize_component_response(
                response_body
            )
            response_content_type = "application/json; charset=utf-8"

        if not normalized_body:
            return component_generation_error_response(
                422,
                (
                    "模型连续返回了无法解析的组件 JSON。"
                    f"最后一次解析错误：{parse_error}"
                ),
            )

        response_body = normalized_body

    return Response(
        response_body,
        status=status,
        content_type=response_content_type,
    )


def proxy_stream_with_curl(upstream, body, content_type, authorization):
    return Response(
        stream_with_curl(upstream, body, content_type, authorization),
        content_type="text/event-stream; charset=utf-8",
        direct_passthrough=True,
    )


def get_provider_config(provider):
    normalized = normalize_provider_id(provider or "scnet")
    for item in load_model_config()["providers"]:
        if item["id"] == normalized:
            return normalized, {
                "name": item["name"],
                "upstream": item["url"],
                "api_key": item["apiKey"],
                "models": item["models"],
            }
    return normalized, None


def is_placeholder_key(api_key):
    return not api_key or api_key.startswith("PASTE_")


@app.after_request
def after_request(response):
    return add_cors_headers(response)


@app.route("/", methods=["GET"])
@app.route("/editor", methods=["GET"])
def editor_page():
    return send_file(EDITOR_HTML_PATH)


@app.route("/models", methods=["GET", "POST", "OPTIONS"])
def models_config():
    if request.method == "OPTIONS":
        return Response(status=204)

    if request.method == "GET":
        return jsonify(public_model_config())

    payload = request.get_json(silent=True) or {}
    provider_id = normalize_provider_id(
        payload.get("providerId") or payload.get("providerName")
    )
    provider_name = str(payload.get("providerName") or provider_id).strip()
    url = str(payload.get("url") or "").strip()
    api_key = str(payload.get("apiKey") or "").strip()
    model_id = str(payload.get("modelId") or "").strip()
    model_label = str(payload.get("modelLabel") or model_id).strip() or model_id

    if not provider_name or not url or not api_key or not model_id:
        return jsonify({"error": "提供商名称、接口 URL、API Key 和模型名称都不能为空。"}), 400

    config = load_model_config()
    provider = next(
        (item for item in config["providers"] if item["id"] == provider_id),
        None,
    )
    if not provider:
        provider = {
            "id": provider_id,
            "name": provider_name,
            "url": url,
            "apiKey": api_key,
            "models": [],
        }
        config["providers"].append(provider)
    else:
        provider["name"] = provider_name
        provider["url"] = url
        provider["apiKey"] = api_key

    existing_model = next(
        (model for model in provider["models"] if model["id"] == model_id),
        None,
    )
    if existing_model:
        existing_model["label"] = model_label
    else:
        provider["models"].append({"id": model_id, "label": model_label})

    return jsonify(public_model_config(save_model_config(config)))


@app.route("/models/<provider_id>/<path:model_id>", methods=["DELETE", "OPTIONS"])
def delete_model_config(provider_id, model_id):
    if request.method == "OPTIONS":
        return Response(status=204)

    provider_key = normalize_provider_id(provider_id)
    config = load_model_config()
    provider = next(
        (item for item in config["providers"] if item["id"] == provider_key),
        None,
    )
    if not provider:
        return jsonify({"error": f"Unknown provider: {provider_key}"}), 404

    before = len(provider["models"])
    provider["models"] = [
        model for model in provider["models"] if model["id"] != model_id
    ]
    if len(provider["models"]) == before:
        return jsonify({"error": f"Unknown model: {model_id}"}), 404

    return jsonify(public_model_config(save_model_config(config)))


@app.route("/health", methods=["GET"])
def health():
    config = load_model_config()
    return jsonify(
        {
            "ok": True,
            "providers": {
                provider["id"]: {
                    "name": provider["name"],
                    "upstream": provider["url"],
                    "api_key_configured": not is_placeholder_key(provider["apiKey"]),
                    "model_count": len(provider["models"]),
                }
                for provider in config["providers"]
            },
        }
    )


@app.route("/ai", methods=["POST", "OPTIONS"])
def ai_proxy():
    if request.method == "OPTIONS":
        return Response(status=204)

    raw_body = request.get_data(cache=False)
    component_request = read_component_request(raw_body)
    body = prepare_model_payload(raw_body)
    if len(body) > MAX_BODY_BYTES:
        return jsonify({"error": "Request body too large"}), 413

    provider_key, provider = get_provider_config(request.headers.get("X-Provider"))
    if not provider:
        return jsonify({"error": f"Unknown provider: {provider_key}"}), 400

    if is_placeholder_key(provider["api_key"]):
        return jsonify(
            {
                "error": (
                    f"{provider['name']} API key is not configured. "
                    "Please edit ai_proxy_server.py and replace the placeholder."
                )
            }
        ), 401

    upstream = provider["upstream"]
    authorization = f"Bearer {provider['api_key']}"
    content_type = request.headers.get("Content-Type", "application/json")
    return proxy_with_curl(
        upstream, body, content_type, authorization, component_request
    )


@app.route("/ai/stream", methods=["POST", "OPTIONS"])
def ai_stream_proxy():
    if request.method == "OPTIONS":
        return Response(status=204)

    body = prepare_model_payload(request.get_data(cache=False))
    if len(body) > MAX_BODY_BYTES:
        return jsonify({"error": "Request body too large"}), 413

    provider_key, provider = get_provider_config(request.headers.get("X-Provider"))
    if not provider:
        return jsonify({"error": f"Unknown provider: {provider_key}"}), 400

    if is_placeholder_key(provider["api_key"]):
        return jsonify(
            {
                "error": (
                    f"{provider['name']} API key is not configured. "
                    "Please edit ai_proxy_server.py and replace the placeholder."
                )
            }
        ), 401

    upstream = provider["upstream"]
    authorization = f"Bearer {provider['api_key']}"
    content_type = request.headers.get("Content-Type", "application/json")
    return proxy_stream_with_curl(upstream, body, content_type, authorization)


if __name__ == "__main__":
    print(f"AI proxy listening on http://{HOST}:{PORT}/ai")
    app.run(host=HOST, port=PORT, debug=False)
