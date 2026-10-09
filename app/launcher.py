#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
给技能和启动器共用的“模型服务”操作。

为什么单独抽一个模块：换模型（modelctl.py）和启动（_tools/start.ps1）
要做的是同一件事 —— 找 koboldcpp、拼参数、等服务就绪、把它停掉。
如果各写一份，两边参数迟早会对不上（比如这边改了 gpulayers，那边还在用旧的）。
工作台本身不 import 这个模块，只有技能脚本用；工作台只读状态文件。

只用标准库。
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

IS_WIN = os.name == "nt"
NO_WINDOW = 0x08000000 if IS_WIN else 0

ROOT = Path(__file__).resolve().parent.parent      # <根>（app 的上一级）
BIN_DIR = ROOT / "bin"
MODELS_DIR = ROOT / "models"
WORK_DIR = ROOT / "work"
PYEXE = ROOT / "py" / "python.exe"

STATE_FILE = WORK_DIR / "model_state.json"

KOBOLD_HOST = "127.0.0.1"
KOBOLD_PORT = 5001
KOBOLD_BASE = "http://%s:%d" % (KOBOLD_HOST, KOBOLD_PORT)

CUDA_EXE = "koboldcpp.exe"
VULKAN_EXE = "koboldcpp_nocuda.exe"

# 默认给显卡的层数。这个值要按机器显存调：
# 在一台 4GB 显存的机器上，22 层约占 2.5GB，留了安全余量。
# 别一上来就给全部层 —— 显存榨干会直接蓝屏（实测遇到过 0x0000010E）。
DEFAULT_GPULAYERS = "22"
DEFAULT_CTX = 8192

# 单个后端的等待总上限（秒）。移动盘冷读一个 2GB 级模型实测要 105~366 秒，
# 给太短会把正在正常加载的进程掐掉，然后去试并不更快的另一个后端。
HARD_TIMEOUT_SECONDS = int(os.environ.get("PORTABLE_AI_MAXWAIT") or 300)


# ------------------------------------------------------------------ 基本工具

def port_open(host=KOBOLD_HOST, port=KOBOLD_PORT, timeout=1.0):
    """端口上有没有人在监听。"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def http_get(path, timeout=4):
    """问 koboldcpp 一个接口，返回解析后的 JSON；失败返回 None。"""
    import urllib.request
    try:
        with urllib.request.urlopen(KOBOLD_BASE + path, timeout=timeout) as resp:
            raw = resp.read()
        data = json.loads(raw.decode("utf-8", "replace"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def server_alive():
    """koboldcpp 是否已经就绪（能应答）？"""
    for path in ("/api/extra/version", "/api/v1/model"):
        if http_get(path, timeout=3):
            return True
    return port_open()


def loaded_model_name():
    """问 koboldcpp 当前加载的模型名（可能带 koboldcpp/ 前缀，也可能不带扩展名）。"""
    data = http_get("/api/v1/model")
    if data:
        name = data.get("result") or data.get("model")
        if name:
            return str(name).replace("\\", "/").split("/")[-1]
    return None


def match_model_file(reported):
    r"""
    把 koboldcpp 报的名字对回 models\ 里的真实文件名。
    koboldcpp 报名字不太统一：有时是完整文件名（xxx.gguf），
    有时带前缀且不带扩展名（koboldcpp/xxx），所以两种都比一遍。
    """
    if not reported:
        return None
    base = str(reported).strip().lower()
    if base.endswith(".gguf"):
        base = base[:-5]
    try:
        for p in sorted(MODELS_DIR.iterdir()):
            if p.is_file() and p.suffix.lower() == ".gguf":
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
                    "size_gb": round(st.st_size / (1024.0 ** 3), 2),
                    "size_bytes": st.st_size,
                })
    except Exception:
        pass
    return out


# ------------------------------------------------------------------ 状态文件

def read_state():
    """读换模型的状态文件；坏了就当没有。"""
    try:
        if STATE_FILE.is_file():
            data = json.loads(STATE_FILE.read_text(encoding="utf-8", errors="replace"))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def write_state(**kw):
    """写状态文件。原子替换在 FAT32 上不可靠，所以直接写临时文件再改名。"""
    state = read_state()
    state.update(kw)
    state["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        WORK_DIR.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            os.replace(str(tmp), str(STATE_FILE))
        except Exception:
            STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
            try:
                tmp.unlink()
            except Exception:
                pass
    except Exception:
        pass
    return state


def clear_switch_state():
    """切模型结束后，把 loading/error 这些一次性字段清掉。"""
    return write_state(loading=False, loading_path=None, loading_name=None,
                       loading_started=None, error=None, error_at=None)


# ------------------------------------------------------------------ 启动 / 停止

def backend_order():
    """
    可用的后端，按优先顺序：默认 CUDA，可用 PORTABLE_AI_BACKEND=vulkan 换。
    返回 [(可执行文件名, 是否加 --usevulkan), ...]
    """
    want_vulkan = (os.environ.get("PORTABLE_AI_BACKEND", "").strip().lower() == "vulkan")
    order = [(VULKAN_EXE, True), (CUDA_EXE, False)] if want_vulkan else [(CUDA_EXE, False), (VULKAN_EXE, True)]
    out = []
    for name, vulkan in order:
        if (BIN_DIR / name).is_file():
            out.append((name, vulkan))
    return out


def build_command(exe_name, vulkan_flag, model_file, ctx=None, gpu_layers=None):
    """拼出启动 koboldcpp 的命令行。"""
    ctx = ctx or os.environ.get("PORTABLE_AI_CTX") or DEFAULT_CTX
    gpu_layers = gpu_layers if gpu_layers is not None else (
        os.environ.get("PORTABLE_AI_GPULAYERS") or DEFAULT_GPULAYERS)
    cmd = [
        str(BIN_DIR / exe_name),
        "--model", str(MODELS_DIR / model_file),
        "--port", str(KOBOLD_PORT),
        "--host", KOBOLD_HOST,
        "--contextsize", str(ctx),
        "--gpulayers", str(gpu_layers),
    ]
    if vulkan_flag:
        cmd.append("--usevulkan")
    return cmd


def extract_dir():
    r"""
    koboldcpp 是 PyInstaller 打包的单文件程序：每次启动都会把自带的
    CUDA/Vulkan 运行库（约 600 MB）解包到临时目录（_MEIxxxx），
    正常退出时它自己会删掉。

    为什么放 C 盘（系统盘）而不是项目盘：
    解包是纯粹的写磁盘操作，实测放 C 盘固态约 59 秒；项目盘是 USB，
    写只有 40 MB/s，放那儿更慢，而且会和"读模型"抢同一块盘的 I/O。

    那为什么之前放项目盘：
    因为强制结束进程（任务管理器杀、蓝屏、断电）时它来不及自清理，
    残留会堆在系统 Temp —— 实测堆过 23 个 _MEI、共 14.8 GB，把 C 盘压到 11.5 GB。
    现在每两次启动都会 clean_stale_extract() 主动清一遍，所以放 C 盘是安全的：
    残留最多存在到下次启动，不会无限堆积。
    """
    base = os.environ.get("TEMP") or os.environ.get("TMP") or str(ROOT)
    d = Path(base) / "portable-ai-extract"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        # 系统 Temp 不可用时退回项目盘，宁可慢也别起不来
        d = ROOT / "_extract"
        try:
            d.mkdir(parents=True, exist_ok=True)
        except Exception:
            return None
    clean_stale_extract()
    return d


def _extract_roots():
    """可能存放解包残留的地方：C 盘的临时目录 + 项目盘（老版本用过）。"""
    roots = []
    for key in ("TEMP", "TMP"):
        v = os.environ.get(key)
        if v:
            roots.append(Path(v) / "portable-ai-extract")
    roots.append(ROOT / "_extract")
    out, seen = [], set()
    for r in roots:
        s = str(r).lower()
        if s not in seen:
            seen.add(s)
            out.append(r)
    return out


def clean_stale_extract():
    r"""
    清掉上次留下的解包残留（只在自己那两个目录里动手：C 盘 Temp 下的
    portable-ai-extract\ 和项目盘的 _extract\）。

    正在运行的那个目录删不掉（文件被占用），忽略错误即可 —— 正好保证
    不会误删当前服务的解包文件。
    """
    n = 0
    for ex in _extract_roots():
        if not ex.is_dir():
            continue
        for d in ex.iterdir():
            try:
                if d.is_dir():
                    shutil.rmtree(str(d), ignore_errors=True)
                    n += 1
                else:
                    d.unlink()
            except Exception:
                pass
    return n


def launch(exe_name, vulkan_flag, model_file, ctx=None, gpu_layers=None):
    """
    把 koboldcpp 拉起来（不占住当前进程的输出），返回子进程对象。
    注意 koboldcpp 自己还会派生一个真正干活的子进程，所以停的时候要整棵树一起停。
    """
    cmd = build_command(exe_name, vulkan_flag, model_file, ctx, gpu_layers)
    log_path = WORK_DIR / "model_server.log"
    try:
        WORK_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    try:
        fh = open(str(log_path), "ab")
    except Exception:
        fh = subprocess.DEVNULL
    # 关键：把子进程的临时目录指到项目盘，别让它往系统盘解包
    env = dict(os.environ)
    ex = extract_dir()
    if ex is not None:
        env["TEMP"] = str(ex)
        env["TMP"] = str(ex)
    try:
        return subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            env=env,
            stdout=fh,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            creationflags=NO_WINDOW,
        )
    finally:
        if fh not in (subprocess.DEVNULL, None):
            try:
                fh.close()
            except Exception:
                pass


def wait_ready(timeout=150, interval=1.0, proc=None, hard_timeout=None):
    """
    等服务就绪。

    这个函数故意做得很笨：进程还活着就一直等，直到到达总上限。

    为什么不搞"卡死检测"：都试过了 ——
      · 用 CPU 判断没用：实测加载时 CPU 从第 30 秒到第 105 秒一直停在 15.4 秒不动
        （解包 + 读盘 + 等 GPU 基本不烧 CPU），会把正常加载误判成卡死；
      · 用"内存 + 显存指纹"判断也不稳，同样误判过，提前把进程掐掉。
    这个盘读 2.3GB 模型实测 105~366 秒（看磁盘忙不忙），所以诚实地给足时间更靠谱。
    进程自己退出（崩了/参数不对）就立刻放弃，不用干等。
    """
    hard_timeout = hard_timeout or HARD_TIMEOUT_SECONDS
    deadline = time.time() + hard_timeout
    while True:
        if server_alive():
            return True
        if proc is not None:
            try:
                if proc.poll() is not None:
                    return False          # 进程自己退了，别再等
            except Exception:
                pass
        if time.time() >= deadline:
            return False
        time.sleep(interval)


def stop_server():
    """
    停掉本项目的 koboldcpp（只认命令行里带本项目路径的，绝不误伤别人的）。

    用 PowerShell 查 Win32_Process 再按 PID 杀，这样父进程和它派生的子进程
    都能一起收掉，也不会把别人的 koboldcpp 干掉。
    """
    if not IS_WIN:
        return []
    root = str(ROOT).replace("'", "''")
    script = (
        "Get-CimInstance Win32_Process -Filter \"Name='koboldcpp.exe' OR Name='koboldcpp_nocuda.exe'\" | "
        "Where-Object { $_.CommandLine -and $_.CommandLine -like '*%s*' } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; $_.ProcessId }"
        % root
    )
    killed = []
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30,
            creationflags=NO_WINDOW)
        killed = [x for x in proc.stdout.decode("utf-8", "replace").split() if x.strip().isdigit()]
    except Exception:
        pass
    # 等端口真的放开，不然新进程会绑不上
    for _ in range(40):
        if not port_open():
            break
        time.sleep(0.5)
    return killed


def current_loaded():
    """
    当前到底加载着哪个模型文件。
    先问 koboldcpp，再退回读状态文件里记的。
    """
    if server_alive():
        name = match_model_file(loaded_model_name())
        if name:
            return name
    st = read_state()
    if st.get("loaded_path"):
        return st.get("loaded_path")
    return None


def launch_preferred(model_file, ctx=None, gpu_layers=None, wait_seconds=150):
    """
    按优先顺序挑后端启动，起来的第一个就留下。
    返回 (成功与否, 用的是哪个后端, 说明文字)。
    """
    order = backend_order()
    if not order:
        return False, None, "bin\\ 里没有 koboldcpp.exe 或 koboldcpp_nocuda.exe"
    for exe_name, vulkan in order:
        try:
            proc = launch(exe_name, vulkan, model_file, ctx, gpu_layers)
        except Exception as exc:
            return False, exe_name, "启动失败：%s" % exc
        # 把进程传进去：只要它还在加载就多等一会儿，别把马上要好的服务掐掉
        if wait_ready(wait_seconds, proc=proc):
            return True, exe_name, "已就绪"
        # 这次没起来：先清干净再换下一个，否则两个会一起抢显存
        try:
            proc.terminate()
        except Exception:
            pass
        try:
            proc.kill()
        except Exception:
            pass
        stop_server()
    return False, None, "两个后端都没能在 %d 秒内就绪" % wait_seconds


if __name__ == "__main__":
    # 方便手工排查：python launcher.py status
    print(json.dumps({
        "root": str(ROOT),
        "server_alive": server_alive(),
        "loaded": current_loaded(),
        "models": list_models(),
        "backends": [b[0] for b in backend_order()],
        "state": read_state(),
    }, ensure_ascii=False, indent=2))