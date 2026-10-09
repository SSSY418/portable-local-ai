#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
便携 AI 工作台 —— 后端服务

只用 Python 标准库（http.server + urllib），不装任何第三方包。
监听 127.0.0.1:8000。前端只跟它说话，浏览器不直接碰 koboldcpp（避开跨域）。

接口：
  /                  静态页面（app\web\）
  /api/chat          反向代理到 koboldcpp 的 /v1/chat/completions，边收边发（SSE 流式）
  /api/models        列出 models\ 里的模型 + 当前加载的是哪个
  /api/status        执行 app\skills\status.py，返回体检报告
  /api/model/switch  执行 app\skills\modelctl.py switch <文件名>
  /api/health        工作台自身 + koboldcpp 的存活状态

所有路径都相对本文件位置推算，绝不写死盘符。
"""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# ------------------------------------------------------------------ 基本配置

APP_DIR = Path(__file__).resolve().parent      # <根>\app
ROOT = APP_DIR.parent                          # <根>
WEB_DIR = APP_DIR / "web"
SKILLS_DIR = APP_DIR / "skills"
MODELS_DIR = ROOT / "models"
WORK_DIR = ROOT / "work"
NOVELS_DIR = ROOT / "novels"
NOVEL_TEMPLATE_DIR = NOVELS_DIR / "模板"
PYEXE = ROOT / "py" / "python.exe"

WORKBENCH_HOST = "127.0.0.1"
WORKBENCH_PORT = 8000
KOBOLD_HOST = "127.0.0.1"
KOBOLD_PORT = 5001
KOBOLD_BASE = "http://%s:%d" % (KOBOLD_HOST, KOBOLD_PORT)

CHAT_TIMEOUT = 1800        # 流式生成最多等 30 分钟
PROBE_TIMEOUT = 3          # 探测 koboldcpp
SKILL_TIMEOUT = 120        # status.py 最多跑 2 分钟
ROUTER_TIMEOUT = 180       # router.py 要让模型翻译，慢一点
SWITCH_TIMEOUT = 600       # 换模型的技能调用
MODEL_STATE_FILE = ROOT / "work" / "model_state.json"   # switcher.py 写的换模型状态

NO_WINDOW = 0x08000000 if os.name == "nt" else 0   # 别弹黑窗

# 关窗口回调要一直留着引用，否则会被 GC 掉导致回调失效
_CTRL_HANDLER = None

MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".ico": "image/x-icon",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
}


def log(msg):
    try:
        print("[workbench] %s" % msg, flush=True)
    except UnicodeEncodeError:
        # 控制台代码页不是 UTF-8 时兜底，别让一句日志把服务搞挂
        print("[workbench] %s" % msg.encode("ascii", "replace").decode("ascii"), flush=True)


def use_utf8_output():
    """
    让自己的 print 走 UTF-8。便携版 Python 默认按系统 ANSI 代码页编码，
    而「启动.bat」已经把控制台切到 65001(UTF-8)，两边不对齐就会满屏乱码。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


# ------------------------------------------------------------------ 问 koboldcpp

def _http_get(path, timeout=PROBE_TIMEOUT):
    req = urllib.request.Request(KOBOLD_BASE + path, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def kobold_alive():
    """koboldcpp 起来了吗？依次试几个最轻的探活接口。"""
    for path in ("/api/extra/version", "/api/v1/model", "/"):
        try:
            _http_get(path)
            return True
        except Exception:
            continue
    return False


def kobold_info():
    """问当前加载的模型名、上下文长度和速度。任何失败都返回空字典，绝不抛异常。"""
    info = {}
    try:
        raw = _http_get("/api/v1/model", timeout=5)
        data = json.loads(raw.decode("utf-8", "replace"))
        if isinstance(data, dict):
            info["model_param"] = data.get("result") or data.get("model")
    except Exception:
        pass
    try:
        raw = _http_get("/api/extra/version", timeout=5)
        data = json.loads(raw.decode("utf-8", "replace"))
        if isinstance(data, dict):
            info["version"] = data.get("version")
            info["backend"] = data.get("backend")
    except Exception:
        pass
    try:
        raw = _http_get("/api/extra/true_max_context_length", timeout=5)
        data = json.loads(raw.decode("utf-8", "replace"))
        if isinstance(data, dict) and data.get("value"):
            info["n_ctx"] = data["value"]
    except Exception:
        pass
    try:
        raw = _http_get("/api/extra/perf", timeout=5)
        data = json.loads(raw.decode("utf-8", "replace"))
        if isinstance(data, dict) and data.get("last_eval_speed"):
            info["last_eval_speed"] = round(float(data["last_eval_speed"]), 2)
    except Exception:
        pass
    return info


def loaded_model_filename():
    r"""把 koboldcpp 报的模型名，对应回 models\ 里的文件名。

    koboldcpp 报的名字不太统一：有时是完整文件名（xxx.gguf），
    有时带前缀且不带扩展名（koboldcpp/xxx）。
    所以先按文件名精确比，再按去掉扩展名的名字比。
    """
    filled = kobold_info().get("model_param")
    if not filled:
        return None
    base = str(filled).replace("\\", "/").split("/")[-1].strip().lower()
    if base.endswith(".gguf"):
        base = base[:-5]
    try:
        for p in sorted(MODELS_DIR.iterdir()):
            if not p.is_file() or p.suffix.lower() != ".gguf":
                continue
            if p.name.lower() == base or p.stem.lower() == base:
                return p.name
    except Exception:
        pass
    return None


def list_models():
    r"""列出 models\ 里的 gguf 文件。"""
    out = []
    try:
        for p in sorted(MODELS_DIR.iterdir(), key=lambda x: x.name.lower()):
            if p.is_file() and p.suffix.lower() == ".gguf":
                st = p.stat()
                out.append({
                    "name": p.name,
                    "path": p.name,
                    "size_gb": round(st.st_size / (1024.0 ** 3), 2),
                    "vision": False,
                    "note": "",
                    "mtime": st.st_mtime,
                })
    except Exception as exc:
        log("list models failed: %s" % exc)
    return out


# ------------------------------------------------------------------ 读状态 / 技能清单

def read_model_state():
    """读换模型的状态文件（switcher.py 写的）。坏了就当空，绝不让接口挂掉。"""
    try:
        if MODEL_STATE_FILE.is_file():
            data = json.loads(MODEL_STATE_FILE.read_text(encoding="utf-8", errors="replace"))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def read_skills_json():
    """读技能清单。"""
    try:
        p = SKILLS_DIR / "skills.json"
        if p.is_file():
            data = json.loads(p.read_text(encoding="utf-8", errors="replace"))
            if isinstance(data, dict):
                return data
    except Exception as exc:
        log("read skills.json failed: %s" % exc)
    return {}


# ------------------------------------------------------------------ 小说目录

# 允许出现在小说目录里的扩展名。只读这些，别的一律不碰。
NOVEL_EXTS = {".md", ".txt"}
# 「模板」只是个样板，不算作品；列作品时要跳过它
NOVEL_RESERVED = {"模板"}


def _safe_child(base, *parts):
    """
    在 base 下面拼一个路径，并确认它真的还在 base 里面。
    防目录穿越：../ 之类的一律拒绝。
    """
    target = (base / Path(*parts)).resolve()
    try:
        target.relative_to(base.resolve())
    except ValueError:
        return None
    return target


def list_novels():
    """列出 novels\\ 下的作品（跳过「模板」），以及每个作品里的文件。"""
    out = []
    try:
        if not NOVELS_DIR.is_dir():
            return out
        for p in sorted(NOVELS_DIR.iterdir(), key=lambda x: x.name):
            if not p.is_dir() or p.name in NOVEL_RESERVED or p.name.startswith("."):
                continue
            files = []
            try:
                for f in sorted(p.rglob("*"), key=lambda x: str(x)):
                    if f.is_file() and f.suffix.lower() in NOVEL_EXTS:
                        try:
                            rel = f.relative_to(p).as_posix()
                        except ValueError:
                            continue
                        files.append({
                            "path": rel,
                            "name": f.name,
                            "size": f.stat().st_size,
                        })
            except Exception:
                pass
            out.append({"name": p.name, "files": files})
    except Exception as exc:
        log("list novels failed: %s" % exc)
    return out


def list_templates():
    """列出模板目录里的文件。"""
    out = []
    try:
        if NOVEL_TEMPLATE_DIR.is_dir():
            for f in sorted(NOVEL_TEMPLATE_DIR.iterdir(), key=lambda x: x.name):
                if f.is_file() and f.suffix.lower() in NOVEL_EXTS:
                    out.append({"path": f.name, "name": f.name, "size": f.stat().st_size})
    except Exception:
        pass
    return out


# ------------------------------------------------------------------ 调技能脚本

def run_skill(script_name, args=None, timeout=SKILL_TIMEOUT):
    r"""跑 app\skills\<脚本>，返回 (退出码, stdout, stderr)。工作目录固定在 work\。"""
    script = SKILLS_DIR / script_name
    if not script.is_file():
        return 127, "", "找不到技能脚本：%s" % script
    exe = str(PYEXE) if PYEXE.is_file() else sys.executable
    cmd = [exe, str(script)] + [str(a) for a in (args or [])]
    try:
        WORK_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    # 必须强制子进程用 UTF-8 输出：便携版 Python 在没有这个变量时，
    # 会按系统 ANSI 代码页（中文 Windows 是 GBK）编码 stdout，
    # 我们按 UTF-8 解码就会变成一堆问号。
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(WORK_DIR),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            creationflags=NO_WINDOW,
        )
        return (proc.returncode,
                proc.stdout.decode("utf-8", "replace"),
                proc.stderr.decode("utf-8", "replace"))
    except subprocess.TimeoutExpired:
        return 124, "", "技能执行超时（超过 %d 秒）" % timeout
    except Exception as exc:
        return 1, "", "技能执行失败：%s" % exc


# ------------------------------------------------------------------ HTTP 服务

class Handler(BaseHTTPRequestHandler):
    server_version = "PortableAIWorkbench/0.1"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass                        # 想调试就把这行改成 print(fmt % args)

    # ---------------- 小工具 ----------------
    def _send(self, code, body=b"", ctype="text/plain; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _json(self, obj, code=200):
        text = json.dumps(obj, ensure_ascii=False, indent=2)
        self._send(code, text, "application/json; charset=utf-8")

    def _read_json(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        raw = self.rfile.read(n) if n > 0 else b""
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8", "replace"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    # ---------------- 路由 ----------------
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/health":
            self.api_health()
        elif path == "/api/models":
            self.api_models()
        elif path == "/api/status":
            self.api_status()
        elif path == "/api/skills":
            self.api_skills()
        elif path == "/api/novels":
            self.api_novels()
        elif path.startswith("/api/"):
            self._json({"ok": False, "error": "没有这个接口：%s" % path}, 404)
        else:
            self.serve_static(path)

    def do_HEAD(self):
        self.do_GET()

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/chat":
            self.api_chat()
        elif path == "/api/model/switch":
            self.api_model_switch()
        elif path == "/api/router":
            self.api_router()
        elif path == "/api/skill/run":
            self.api_skill_run()
        elif path == "/api/novel/read":
            self.api_novel_read()
        elif path == "/api/novel/save":
            self.api_novel_save()
        elif path == "/api/novel/create":
            self.api_novel_create()
        else:
            self._json({"ok": False, "error": "没有这个接口：%s" % path}, 404)

    # ---------------- 静态页面 ----------------
    def serve_static(self, path):
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        target = (WEB_DIR / rel).resolve()
        try:
            target.relative_to(WEB_DIR.resolve())     # 防目录穿越
        except ValueError:
            self._send(403, "拒绝访问")
            return
        if target.is_dir():
            target = target / "index.html"
        if not target.is_file():
            self._send(404, "找不到页面：%s" % rel)
            return
        ctype = MIME.get(target.suffix.lower(), "application/octet-stream")
        try:
            self._send(200, target.read_bytes(), ctype)
        except Exception as exc:
            self._send(500, "读文件失败：%s" % exc)

    # ---------------- /api/health ----------------
    def api_health(self):
        info = kobold_info()
        alive = bool(info) or kobold_alive()
        self._json({
            "ok": True,
            "workbench": True,
            "kobold": alive,
            "kobold_url": KOBOLD_BASE,
            "port": WORKBENCH_PORT,
            "root": str(ROOT),
            "info": info,
        })

    # ---------------- /api/models ----------------
    def api_models(self):
        models = list_models()
        alive = kobold_alive()
        current = None
        if alive:
            info = kobold_info()
            name = loaded_model_filename()
            current = {
                "name": name or (info.get("model_param") or "已加载模型"),
                "path": name,
                "vision": False,
                "config": info.get("backend") or ("CUDA" if name else ""),
                "n_ctx": info.get("n_ctx"),
            }
        # 换模型的状态由 skills\switcher.py 写在 work\model_state.json 里，
        # 页面每 2 秒问一次这个接口来看进度。
        state = read_model_state()
        loading = bool(state.get("loading"))
        # 换模型进行中时，current 报的是"新的那个"，但服务其实还没起来；
        # 所以额外用 loaded_path 告诉页面"真正在跑的是哪个"。
        loaded_path = state.get("loaded_path") or ((current or {}).get("path"))
        if loading and state.get("loading_path") and loaded_path:
            # 正在换：把列表里的高亮指向"要换成的那个"
            loaded_path = state.get("loading_path")
        # 给每个模型标一下是不是当前在用的（模型管家页直接用）
        cur_name = None
        if not loading:
            cur_name = ((current or {}).get("path") or state.get("loaded_path"))
        for m in models:
            m["current"] = bool(cur_name and m["name"].lower() == str(cur_name).lower())
        self._json({
            "ok": True,
            "models": models,
            "current": current,
            "loaded_path": loaded_path,
            "kobold": alive,
            "loading": loading,
            "loading_path": state.get("loading_path"),
            "loading_name": state.get("loading_name"),
            "loading_started": state.get("loading_started"),
            "error": state.get("error"),
            "n_threads": os.cpu_count() or 0,
            "default_system": "",      # 默认不带系统提示词
            "default_temperature": 0.7,
            "backends": {
                "cuda": (ROOT / "bin" / "koboldcpp.exe").is_file(),
                "vulkan": (ROOT / "bin" / "koboldcpp_nocuda.exe").is_file(),
            },
            "last_backend": state.get("last_backend"),
        })

    # ---------------- /api/skills ----------------
    def api_skills(self):
        data = read_skills_json()
        if not data:
            self._json({"ok": False, "error": "读不到 skills.json"}, 500)
            return
        self._json({"ok": True, "skills": data.get("skills") or []})

    # ---------------- /api/novels ----------------
    def api_novels(self):
        self._json({
            "ok": True,
            "novels": list_novels(),
            "templates": list_templates(),
            "root": str(NOVELS_DIR),
        })

    # ---------------- /api/novel/read ----------------
    def api_novel_read(self):
        """读一个小说文件。只允许读 novels\\ 里面的 .md / .txt。"""
        data = self._read_json()
        novel = (data.get("novel") or "").strip()
        rel = (data.get("path") or "").strip().replace("\\", "/")
        # 模板单独走一个分支，这样它不用被当成"作品"
        if novel == "模板" or novel == "__template__":
            if not rel or Path(rel).name != rel:
                self._json({"ok": False, "error": "模板只允许读 模板\\ 下的单个文件"}, 400)
                return
            target = _safe_child(NOVEL_TEMPLATE_DIR, rel)
        else:
            if not novel or Path(novel).name != novel:
                self._json({"ok": False, "error": "作品名不合法"}, 400)
                return
            if not rel or ".." in rel:
                self._json({"ok": False, "error": "路径不合法"}, 400)
                return
            target = _safe_child(NOVELS_DIR, novel, rel)
        if target is None:
            self._json({"ok": False, "error": "路径越界，拒绝访问"}, 403)
            return
        if target.suffix.lower() not in NOVEL_EXTS:
            self._json({"ok": False, "error": "只支持读写 .md / .txt"}, 400)
            return
        if not target.is_file():
            self._json({"ok": False, "error": "文件不存在"}, 404)
            return
        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            self._json({"ok": False, "error": "读文件失败：%s" % exc}, 500)
            return
        self._json({"ok": True, "text": text, "size": target.stat().st_size})

    # ---------------- /api/novel/save ----------------
    def api_novel_save(self):
        """
        保存小说文件。只允许写 novels\\<作品>\\ 下面的 .md / .txt。
        不许新建作品（建作品要走 /api/novel/create），也不许写到「模板」里 ——
        模板被改坏了，以后新建作品就都是坏的。
        """
        data = self._read_json()
        novel = (data.get("novel") or "").strip()
        rel = (data.get("path") or "").strip().replace("\\", "/")
        text = data.get("text")
        if not isinstance(text, str):
            self._json({"ok": False, "error": "没有要保存的内容"}, 400)
            return
        if not novel or Path(novel).name != novel or novel in NOVEL_RESERVED:
            self._json({"ok": False, "error": "作品名不合法（或不能改模板）"}, 400)
            return
        if not rel or ".." in rel:
            self._json({"ok": False, "error": "路径不合法"}, 400)
            return
        if not (NOVELS_DIR / novel).is_dir():
            self._json({"ok": False, "error": "这个作品不存在：%s" % novel}, 404)
            return
        target = _safe_child(NOVELS_DIR, novel, rel)
        if target is None:
            self._json({"ok": False, "error": "路径越界，拒绝写入"}, 403)
            return
        if target.suffix.lower() not in NOVEL_EXTS:
            self._json({"ok": False, "error": "只支持读写 .md / .txt"}, 400)
            return
        if len(text) > 2_000_000:
            self._json({"ok": False, "error": "内容太长了（超过 200 万字）"}, 400)
            return
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        except Exception as exc:
            self._json({"ok": False, "error": "保存失败：%s" % exc}, 500)
            return
        self._json({"ok": True, "size": target.stat().st_size,
                    "message": "已保存 %s" % rel})

    # ---------------- /api/novel/create ----------------
    def api_novel_create(self):
        """新建一本小说：按模板建目录和文件。已存在的不覆盖。"""
        data = self._read_json()
        name = (data.get("name") or "").strip()
        if not name:
            self._json({"ok": False, "error": "没给作品名"}, 400)
            return
        # 名字里不许有路径分隔符和 Windows 保留字符
        if any(c in name for c in '\\/:*?"<>|') or name in (".", "..") or name in NOVEL_RESERVED:
            self._json({"ok": False, "error": "作品名里有不能用的字符"}, 400)
            return
        if len(name) > 60:
            self._json({"ok": False, "error": "作品名太长了"}, 400)
            return
        base = _safe_child(NOVELS_DIR, name)
        if base is None:
            self._json({"ok": False, "error": "路径不合法"}, 403)
            return
        if base.exists():
            self._json({"ok": False, "error": "「%s」已经存在了" % name}, 409)
            return
        created = []
        try:
            base.mkdir(parents=True, exist_ok=True)
            # 每个单元一套：大纲.md + 带 8 节的正文
            for unit in range(1, 4):
                unit_dir = base / ("单元%02d" % unit)
                unit_dir.mkdir(parents=True, exist_ok=True)
                outline_src = NOVEL_TEMPLATE_DIR / "单元大纲.md"
                if outline_src.is_file():
                    (unit_dir / "大纲.md").write_text(
                        outline_src.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
                else:
                    (unit_dir / "大纲.md").write_text("# 单元大纲\n", encoding="utf-8")
                created.append("单元%02d/大纲.md" % unit)
                for sec in range(1, 9):
                    (unit_dir / ("第%02d节.md" % sec)).write_text(
                        "# 第 %d 节\n\n（在这里写正文，或者把对话里生成的内容贴进来）\n" % sec,
                        encoding="utf-8")
                    created.append("单元%02d/第%02d节.md" % (unit, sec))
            setting_src = NOVEL_TEMPLATE_DIR / "设定卡.md"
            if setting_src.is_file():
                (base / "设定卡.md").write_text(
                    setting_src.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
            else:
                (base / "设定卡.md").write_text("# 设定卡\n", encoding="utf-8")
            created.append("设定卡.md")
        except Exception as exc:
            self._json({"ok": False, "error": "创建失败：%s" % exc}, 500)
            return
        self._json({"ok": True, "name": name, "created": created,
                    "message": "已新建「%s」，共 %d 个文件。" % (name, len(created))})

    # ---------------- /api/novel/newname 已并入 create ----------------

    # ---------------- /api/router ----------------
    def api_router(self):
        """把一句自然语言翻译成技能调用计划。只翻译，不执行。"""
        data = self._read_json()
        text = (data.get("text") or data.get("question") or "").strip()
        if not text:
            self._json({"ok": False, "error": "没收到要说的话"}, 400)
            return
        code, out, err = run_skill("router.py", [text], timeout=ROUTER_TIMEOUT)
        report = None
        if (out or "").strip():
            try:
                report = json.loads(out.strip())
            except Exception:
                report = None
        if report is None:
            self._json({
                "ok": False,
                "error": "翻译失败（退出码 %s）" % code,
                "stderr": (err or "").strip()[-800:],
                "raw": (out or "").strip()[-800:],
            }, 502)
            return
        self._json(report)

    # ---------------- /api/skill/run ----------------
    def api_skill_run(self):
        """HTTP 入口：真正执行技能，返回完整结果。"""
        payload, code = self._build_skill_result(self._read_json())
        self._json(payload, code)

    def _build_skill_result(self, req):
        """
        执行技能，返回 (要回给前端的 JSON, HTTP 状态码)。
        只允许跑 skills.json 里登记过的技能和动作，参数再校验一遍。
        router.py（模型）给的建议在这儿被最终把关，绝不让模型直接使唤东西。
        """
        req = req if isinstance(req, dict) else {}
        sid = (req.get("skill") or "").strip()
        action = (req.get("action") or "").strip()
        args = req.get("args") or []
        if not isinstance(args, list):
            args = [args]
        args = [str(a) for a in args]

        catalog = read_skills_json()
        if not catalog:
            return {"ok": False, "error": "读不到 skills.json"}, 500

        skill = None
        for s in (catalog.get("skills") or []):
            if (s.get("id") or "").lower() == sid.lower():
                skill = s
                break
        if skill is None:
            return {"ok": False, "error": "没有这个技能：%s" % sid}, 400

        actions = skill.get("actions") or {}
        if action not in actions:
            if len(actions) == 1:
                action = next(iter(actions))
            else:
                return {"ok": False, "error": "技能 %s 没有动作 %s" % (sid, action)}, 400
        spec = actions[action]

        # 参数校验：只允许登记过的参数个数，模型文件参数必须是 models\ 里真实存在的文件
        need = spec.get("args") or []
        file_arg = spec.get("file_arg")
        if file_arg:
            name = args[0].strip() if args else ""
            if Path(name).name != name:
                return {"ok": False, "error": "文件名不合法（不许带路径）"}, 400
            if not (MODELS_DIR / name).is_file():
                return {"ok": False, "error": "models\\ 里没有这个文件：%s" % name}, 400
            args = [name]
        elif need:
            args = args[:len(need)]
            if len(args) < len(need):
                return {"ok": False, "error": "动作 %s 需要 %d 个参数" % (action, len(need))}, 400
        else:
            args = []

        script = skill.get("script") or ""
        if not script or Path(script).name != script:
            return {"ok": False, "error": "技能 %s 的 script 字段不合法" % sid}, 500

        cli_args = [action] + args
        timeout = SWITCH_TIMEOUT if sid == "modelctl" and action == "switch" else SKILL_TIMEOUT
        code, out, err = run_skill(script, cli_args, timeout=timeout)

        report = None
        if (out or "").strip():
            try:
                report = json.loads(out.strip())
            except Exception:
                report = None

        summary = self._skill_summary(sid, action, report, code)
        ok = code == 0 and (report is None or report.get("ok", True))
        return {
            "ok": ok,
            "skill": sid,
            "action": action,
            "args": args,
            "summary": summary,
            "message": (report or {}).get("message") if isinstance(report, dict) else None,
            "changed": (report or {}).get("changed") if isinstance(report, dict) else None,
            "loading": (report or {}).get("loading") if isinstance(report, dict) else None,
            "result": report,
            "stderr": (err or "").strip()[-800:],
            "raw": None if report is not None else (out or "").strip()[-4000:],
            "code": code,
        }, (200 if ok else 200)

    @staticmethod
    def _skill_summary(sid, action, report, code):
        """给页面一句人话，别让用户去读 JSON。"""
        if isinstance(report, dict):
            if report.get("conclusion"):
                return report["conclusion"]
            if report.get("message"):
                return report["message"]
            if report.get("error"):
                return report["error"]
            if sid == "modelctl" and action == "list":
                n = report.get("count")
                loaded = report.get("loaded") or "（没有在跑）"
                return "一共 %s 个模型，当前用的是：%s" % (n, loaded)
            if sid == "modelctl" and action == "current":
                if report.get("loaded") or report.get("name"):
                    return "当前用的是：%s" % (report.get("loaded") or report.get("name"))
                return "当前没有模型在跑。"
        if code != 0:
            return "执行失败（退出码 %s）" % code
        return "执行完成。"

    # ---------------- /api/status ----------------
    def api_status(self):
        code, out, err = run_skill("status.py", timeout=SKILL_TIMEOUT)
        out = (out or "").strip()
        report = None
        if out:
            try:
                report = json.loads(out)
            except Exception:
                report = None
        if report is None and code != 0:
            self._json({
                "ok": False,
                "error": "体检脚本执行失败（退出码 %s）" % code,
                "stderr": (err or "").strip()[-2000:],
                "raw": out[-4000:],
            }, 500)
            return
        if report is None:
            last = out.splitlines()[-1] if out else ""
            self._json({"ok": True, "text": out, "conclusion": last})
            return
        report.setdefault("ok", True)
        self._json(report)

    # ---------------- /api/model/switch ----------------
    def api_model_switch(self):
        """
        换模型。它是 /api/skill/run 的一层薄包装：校验和执行逻辑都在那儿，
        这里只是让前端可以用更直白的 {file: "xxx.gguf"} 调用。
        """
        data = self._read_json()
        name = (data.get("file") or data.get("name") or data.get("path") or "").strip()
        if not name:
            self._json({"ok": False, "error": "没给模型文件名"}, 400)
            return
        merged = dict(data)
        merged.update({"skill": "modelctl", "action": "switch", "args": [name]})
        payload, code = self._build_skill_result(merged)
        payload.setdefault("file", name)
        self._json(payload, code)

    # ---------------- /api/chat（流式代理） ----------------
    def api_chat(self):
        data = self._read_json()

        messages = data.get("messages")
        if not isinstance(messages, list) or not messages:
            q = (data.get("question") or data.get("prompt") or "").strip()
            messages = [{"role": "user", "content": q}] if q else []
        if not messages:
            self._json({"ok": False, "error": "没有收到对话内容"}, 400)
            return

        system = (data.get("system") or "").strip()

        # 有些新模型默认会先"自言自语"想一大堆再回答，写小说时白等又白烧 token。
        # 实测：把 /no_think 放进系统提示词就能关掉思考（chat_template_kwargs 在
        # 这个版本的 koboldcpp 上无效）。默认关掉思考，前端可以显式打开。
        thinking = bool(data.get("thinking"))
        if not thinking and "/no_think" not in system:
            system = ("/no_think " + system).strip()
        if system and not (messages and messages[0].get("role") == "system"):
            messages = [{"role": "system", "content": system}] + messages

        try:
            temperature = float(data.get("temperature") if data.get("temperature") is not None else 0.7)
            max_tokens = int(data.get("max_tokens") or 1024)
        except (TypeError, ValueError):
            temperature, max_tokens = 0.7, 1024

        payload = {
            "messages": messages,
            "stream": True,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if data.get("top_p") is not None:
            try:
                payload["top_p"] = float(data["top_p"])
            except (TypeError, ValueError):
                pass
        if data.get("stop"):
            payload["stop"] = data["stop"]

        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            KOBOLD_BASE + "/v1/chat/completions",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json", "Accept": "text/event-stream"},
        )

        try:
            resp = urllib.request.urlopen(req, timeout=CHAT_TIMEOUT)
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:800]
            except Exception:
                pass
            self._json({"ok": False, "error": "模型返回错误 %s：%s" % (exc.code, detail or exc.reason)}, 502)
            return
        except Exception as exc:
            self._json({
                "ok": False,
                "error": "连不上模型服务（%s）。请确认 koboldcpp 已启动：%s" % (exc, KOBOLD_BASE),
            }, 502)
            return

        # 开始 SSE 回包：收到一小段就立刻转给浏览器（打字机效果）
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        def emit(obj):
            self.wfile.write(("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode("utf-8"))
            self.wfile.flush()

        started = time.time()
        pieces = 0
        chars = 0
        finished = False
        usage = None
        try:
            while True:
                raw = resp.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", "replace").strip()
                if not line or not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                if data_str == "[DONE]":
                    finished = True
                    break
                try:
                    ev = json.loads(data_str)
                except Exception:
                    continue
                # koboldcpp 会在流里带上真实 token 用量，比数小块准得多
                if isinstance(ev.get("usage"), dict):
                    usage = ev["usage"]
                choices = ev.get("choices") or []
                if not choices:
                    continue
                piece = (choices[0].get("delta") or {}).get("content")
                if piece:
                    pieces += 1
                    chars += len(piece)
                    emit({"type": "delta", "text": piece})
        except (BrokenPipeError, ConnectionResetError):
            log("浏览器中断了这次生成")
        except Exception as exc:
            try:
                emit({"type": "error", "text": "读取模型输出出错：%s" % exc})
            except Exception:
                pass
        finally:
            try:
                resp.close()
            except Exception:
                pass

        seconds = round(time.time() - started, 2)
        gen_tokens = None
        prompt_tokens = None
        if usage:
            gen_tokens = usage.get("completion_tokens")
            prompt_tokens = usage.get("prompt_tokens")
        # 优先用模型自己报的 token 数算速度，拿不到才退回数小块
        basis = gen_tokens if gen_tokens else pieces
        tps = round(basis / seconds, 2) if (seconds > 0 and basis) else 0
        # 一个容易踩的坑：开着思考模式、最长回复又给得小，模型会把额度全花在
        # "自言自语"上，一个字的正文都不吐（实测 200 token 全被思考吃掉）。
        # 这时要明确告诉用户该怎么办，别让他对着空泡泡发呆。
        empty_done = pieces == 0 and chars == 0
        note = None
        if empty_done:
            if thinking:
                note = ("模型把「最长回复」的额度全用在思考上了，还没开始写正文就停了。"
                        "要么把「设置」里的最长回复调大（比如 2048），要么关掉思考模式。")
            else:
                note = "模型这次没有输出任何内容，可以再发一次试试。"

        try:
            emit({
                "type": "done",
                "tokens": basis,
                "chunks": pieces,
                "prompt_tokens": prompt_tokens,
                "chars": chars,
                "seconds": seconds,
                "tps": tps,
                "finished": finished,
                "empty": empty_done,
                "note": note,
            })
        except Exception:
            pass


# ------------------------------------------------------------------ 收尾清理

def kill_model_server():
    """
    关窗口时顺手把 koboldcpp 也收掉，别留孤儿进程。
    只结束命令行里带 --port 5001 的那一个，绝不误伤别的程序。

    注意：只有「启动.bat」拉起来的工作台才负责收尾（它会设 PORTABLE_AI_MANAGED=1）。
    单独手动跑 workbench.py 时不动模型服务，免得把用户正在用的对话给断了。
    """
    if os.name != "nt":
        return
    if os.environ.get("PORTABLE_AI_MANAGED") != "1":
        log("standalone mode: leaving the model server alone")
        return
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='koboldcpp.exe' OR Name='koboldcpp_nocuda.exe'\" | "
             "Where-Object { $_.CommandLine -like '*--port*5001*' } | "
             "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; $_.ProcessId }"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=20, creationflags=NO_WINDOW)
        killed = proc.stdout.decode("utf-8", "replace").split()
        if killed:
            log("stopped koboldcpp (pid %s)" % ", ".join(killed))
    except Exception as exc:
        log("cleanup warning: %s" % exc)


def install_close_handler():
    """
    用户直接点窗口右上角的 × 时，Ctrl+C 是收不到的。
    Windows 这时会给控制台发 CTRL_CLOSE_EVENT —— 用 SetConsoleCtrlHandler 接住它，
    趁机把 koboldcpp 收掉，别留孤儿进程。
    """
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        HANDLER = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)

        def _handler(event):
            # 2 = CTRL_CLOSE_EVENT（关窗口）, 0 = CTRL_C, 1 = CTRL_BREAK
            if event in (0, 1, 2):
                log("收到关闭信号，正在收拾进程…")
                kill_model_server()
                if event == 2:
                    # 关窗口时系统只给几秒钟，必须主动退，不能等 serve_forever 收尾
                    os._exit(0)
                return False      # Ctrl+C 交回给 Python 的 KeyboardInterrupt 流程
            return False

        # 必须把回调对象留住，否则会被垃圾回收，回调就失效了
        global _CTRL_HANDLER
        _CTRL_HANDLER = HANDLER(_handler)
        if ctypes.windll.kernel32.SetConsoleCtrlHandler(_CTRL_HANDLER, True):
            return True
    except Exception as exc:
        log("close handler not installed: %s" % exc)
    return False


# 说明：这里原本还有一个"盯着父进程、它没了我就不干了"的看门狗，
# 实测它在后台/输出被重定向的场景下会误判（父进程句柄一失效就以为窗口关了），
# 把正在用的模型服务杀掉，所以直接删掉了。
# 现在收尾只靠 SetConsoleCtrlHandler 收到的 CTRL_CLOSE_EVENT 和 Ctrl+C。


# ------------------------------------------------------------------ 启动

class QuietServer(ThreadingHTTPServer):
    """
    浏览器刷新/关闭页面时会直接掐断连接，socketserver 默认会打一整段
    红色报错（ConnectionResetError），对用户来说纯属吓人，这里静音掉。
    """
    daemon_threads = True

    def handle_error(self, request, client_address):
        import sys as _sys
        exc = _sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            return
        ThreadingHTTPServer.handle_error(self, request, client_address)


def main():
    use_utf8_output()
    log("=" * 58)
    log("便携 AI 工作台 backend")
    log("project root : %s" % ROOT)
    log("model server : %s" % KOBOLD_BASE)
    log("workbench    : http://%s:%d" % (WORKBENCH_HOST, WORKBENCH_PORT))
    log("=" * 58)
    if not WEB_DIR.is_dir():
        log("warning: web dir not found: %s" % WEB_DIR)
    if not (SKILLS_DIR / "status.py").is_file():
        log("warning: skills/status.py not found")
    try:
        httpd = QuietServer((WORKBENCH_HOST, WORKBENCH_PORT), Handler)
    except OSError as exc:
        log("start failed: port %d busy? (%s)" % (WORKBENCH_PORT, exc))
        return 1
    httpd.daemon_threads = True

    # 关窗口时把 koboldcpp 一起收掉：Ctrl+C 走 KeyboardInterrupt，
    # 点右上角 × 走 SetConsoleCtrlHandler 接到的 CTRL_CLOSE_EVENT。
    #
    # 这里特意不加"盯着父进程"的看门狗：实测它会在正常运行时误判，
    # 反而把好好的模型服务杀掉。整条链路上只有 cmd.exe 和 python 两个进程，
    # Windows 关窗口时会给两个都发通知，够用了。
    if os.environ.get("PORTABLE_AI_MANAGED") == "1":
        install_close_handler()

    log("ready. open http://%s:%d" % (WORKBENCH_HOST, WORKBENCH_PORT))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log("bye")
    finally:
        try:
            httpd.server_close()
        except Exception:
            pass
        kill_model_server()
    return 0


if __name__ == "__main__":
    sys.exit(main())