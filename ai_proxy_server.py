import json
import os
import re
import subprocess
import tempfile
import urllib.error
import urllib.request
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
        {
            "id": "google",
            "name": "Google Gemini",
            "url": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
            "apiKey": os.environ.get("GOOGLE_API_KEY", "PASTE_GOOGLE_API_KEY"),
            "models": [
                {"id": "gemini-2.5-flash", "label": "Gemini 2.5 Flash"},
                {"id": "gemini-2.5-flash-lite", "label": "Gemini 2.5 Flash-Lite"},
                {"id": "gemini-3.1-flash-lite-preview", "label": "Gemini 3.1 Flash-Lite Preview"},
                {"id": "gemini-3-flash-preview", "label": "Gemini 3 Flash Preview"},
                {"id": "gemini-flash-lite-latest", "label": "Gemini Flash-Lite Latest"},
                {"id": "gemini-flash-latest", "label": "Gemini Flash Latest"},
                {"id": "gemini-2.0-flash", "label": "Gemini 2.0 Flash"},
                {"id": "gemini-2.5-pro", "label": "Gemini 2.5 Pro"},
                {"id": "gemini-3-pro-preview", "label": "Gemini 3 Pro Preview"},
                {"id": "gemini-3.1-pro-preview", "label": "Gemini 3.1 Pro Preview"},
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
        'JSON 格式：{"templates":[{"name":"组件名称","description":"一句话说明","baseType":"custom-interactive|choice-question|fill-blank|info-card|function-plot|geometry-2d|geometry-3d","defaultConfig":{...}}]}。',
        "最多返回 2 个模板；优先使用标准组件能力。只有标准组件无法表达用户需求时，才使用 custom-interactive。",
        "如果用户要求平面几何、三角形、圆、角、尺规图、几何证明配图，必须使用 geometry-2d，不要使用 custom-interactive。",
        "如果用户要求立体几何、空间几何、三维视角、锥体、棱柱、棱锥、平面截面，必须使用 geometry-3d，不要使用 custom-interactive；前端会用固定 Three.js 渲染器解释结构化配置。",
        "如果用户要求函数图像、坐标系中的函数曲线，使用 function-plot。",
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
        "geometry-2d defaultConfig 字段：title, summary, viewBox, objects, annotations, questions。",
        'geometry-2d viewBox 形如 {"xMin":-6,"xMax":6,"yMin":-4,"yMax":4}。',
        "geometry-2d objects 支持：point {type,id,label,x,y}；segment {type,from,to}；line {type,from,to}；circle {type,center,radius}；polygon {type,points,label}；angle {type,a,o,b,label,radius}。",
        "geometry-3d defaultConfig 字段：title, summary, camera, objects, annotations, questions。",
        'geometry-3d camera 形如 {"yaw":-45,"pitch":14,"distance":11}；需要观察双圆锥时使用较低 pitch，避免俯视导致下半圆锥被遮挡。',
        "geometry-3d objects 支持：point {type,id,label,x,y,z}；segment {type,from,to}；plane {type,points,label} 或 plane {type,center,width,height,rotation,label}；polyhedron {type,vertices,edges,faces,label}；cone {type,apex,baseCenter,radius,label}；double-cone {type,center,radius,height,label}；ellipse/curve {type,center,radiusX,radiusY,rotation,color,label}。",
        'geometry-3d 坐标推荐使用对象格式，例如 {"x":0,"y":0,"z":0}；rotation 推荐 {"x":62,"y":0,"z":0}。',
        '圆锥曲线示例 objects：{"type":"double-cone","center":{"x":0,"y":0,"z":0},"radius":1.8,"height":3.2,"label":"双圆锥"}，{"type":"plane","center":{"x":0,"y":0,"z":0.35},"width":4.2,"height":2.8,"rotation":{"x":62,"y":0,"z":0},"opacity":0.32,"label":"截面平面"}，{"type":"ellipse","center":{"x":0,"y":0,"z":0.35},"radiusX":1.25,"radiusY":0.48,"rotation":{"x":62,"y":0,"z":0},"color":14417920,"label":"截面曲线"}。',
        "圆锥曲线、双圆锥、截面平面等立体几何定义组件必须优先使用 double-cone + plane + ellipse/curve，不要用大量 point/segment 近似圆锥表面。",
        "geometry-2d 和 geometry-3d 只返回结构化几何对象，不要写 htmlLines、cssLines、jsLines，不要写脚本。",
        "几何对象里的 from/to/points/center/a/o/b 可以引用 point 的 id。所有引用的点必须在 objects 中先定义。",
        "questions 是可选数组，每项可写 prompt 字段，用于给学生提问。",
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


def post_with_urllib(upstream, body, content_type, authorization):
    headers = {"Content-Type": content_type}
    if authorization:
        headers["Authorization"] = authorization

    request_timeout = REQUEST_TIMEOUT if REQUEST_TIMEOUT > 0 else 180
    req = urllib.request.Request(upstream, data=body, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=request_timeout) as resp:
            response_body = resp.read()
            response_content_type = resp.headers.get(
                "content-type", "application/json; charset=utf-8"
            )
            return resp.status, response_content_type, response_body
    except urllib.error.HTTPError as exc:
        response_body = exc.read()
        response_content_type = exc.headers.get(
            "content-type", "application/json; charset=utf-8"
        )
        return exc.code, response_content_type, response_body


def post_upstream(upstream, body, content_type, authorization):
    if "generativelanguage.googleapis.com" in upstream:
        return post_with_urllib(upstream, body, content_type, authorization)

    try:
        return post_with_curl(upstream, body, content_type, authorization)
    except Exception as curl_error:
        try:
            return post_with_urllib(upstream, body, content_type, authorization)
        except Exception as urllib_error:
            raise RuntimeError(
                f"curl 转发失败：{curl_error}；urllib 转发失败：{urllib_error}"
            ) from urllib_error


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


def stream_with_urllib(upstream, body, content_type, authorization):
    headers = {"Content-Type": content_type}
    if authorization:
        headers["Authorization"] = authorization

    request_timeout = REQUEST_TIMEOUT if REQUEST_TIMEOUT > 0 else 180
    req = urllib.request.Request(upstream, data=body, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=request_timeout) as resp:
            while True:
                chunk = resp.readline()
                if not chunk:
                    break
                yield chunk
    except urllib.error.HTTPError as exc:
        error_text = exc.read().decode("utf-8", errors="replace")
        payload = json.dumps(
            {"error": f"接口返回 {exc.code}：{error_text[:500]}"},
            ensure_ascii=False,
        )
        yield f"event: error\ndata: {payload}\n\n".encode("utf-8")
    except Exception as exc:
        payload = json.dumps({"error": str(exc)}, ensure_ascii=False)
        yield f"event: error\ndata: {payload}\n\n".encode("utf-8")


def stream_upstream(upstream, body, content_type, authorization):
    if "generativelanguage.googleapis.com" in upstream:
        return stream_with_urllib(upstream, body, content_type, authorization)
    return stream_with_curl(upstream, body, content_type, authorization)


def proxy_with_curl(upstream, body, content_type, authorization, component_request=None):
    try:
        status, response_content_type, response_body = post_upstream(
            upstream, body, content_type, authorization
        )
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502

    if component_request and status == 200:
        normalized_body, invalid_content, parse_error = normalize_component_response(
            response_body
        )
        response_content_type = "application/json; charset=utf-8"

        if not normalized_body:
            return component_generation_error_response(
                422,
                (
                    "模型返回了无法解析的组件 JSON。"
                    f"解析错误：{parse_error}。"
                    f"返回片段：{str(invalid_content or '')[:500]}"
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
        stream_upstream(upstream, body, content_type, authorization),
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
