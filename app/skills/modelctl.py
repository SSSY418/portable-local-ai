#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
模型管家技能。

    python modelctl.py list              列出 models\ 里的模型
    python modelctl.py current           报告当前加载的是哪个
    python modelctl.py switch <文件名>   换成指定模型

关于 switch：它**不会**在这儿等模型加载完。
它会先确认文件存在、没人正在换，然后把活派给同目录的 switcher.py
（脱离父进程跑），自己立刻返回。页面靠轮询 /api/models 看进度。
这样页面上能显示“正在加载 xxx”，而不是干转两分钟圈。

只用标准库。输出 JSON 到 stdout。
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent      # <根>\app
sys.path.insert(0, str(APP_DIR))

import launcher  # noqa: E402

PYEXE = launcher.PYEXE if launcher.PYEXE.is_file() else Path(sys.executable)
SWITCHER = Path(__file__).resolve().parent / "switcher.py"
NO_WINDOW = launcher.NO_WINDOW


def out(obj, code=0):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, indent=2))
    sys.stdout.flush()
    return code


def cmd_list():
    models = launcher.list_models()
    loaded = launcher.current_loaded()
    for m in models:
        m["current"] = bool(loaded and m["name"].lower() == loaded.lower())
    return out({
        "ok": True,
        "models": models,
        "loaded": loaded,
        "count": len(models),
        "models_dir": str(launcher.MODELS_DIR),
    })


def cmd_current():
    alive = launcher.server_alive()
    loaded = launcher.current_loaded()
    info = launcher.http_get("/api/extra/version") or {}
    state = launcher.read_state()
    return out({
        "ok": True,
        "running": alive,
        "loaded": loaded,
        "name": launcher.loaded_model_name(),
        "version": info.get("version"),
        "n_ctx": (launcher.http_get("/api/extra/true_max_context_length") or {}).get("value"),
        "backend": state.get("last_backend"),
        "loading": bool(state.get("loading")),
        "error": state.get("error"),
    })


def cmd_switch(args):
    if not args:
        return out({"ok": False, "error": "没给模型文件名。用法：modelctl.py switch <文件名>"}, 2)
    want = args[0].strip()

    # 只允许 models\ 里的文件名，不许带路径
    if Path(want).name != want:
        return out({"ok": False, "error": "文件名不合法（不许带路径）：%s" % want}, 2)

    target = launcher.MODELS_DIR / want
    if not target.is_file():
        return out({"ok": False, "error": "models\\ 里没有这个文件：%s" % want}, 4)

    # 已经正在换了就别重复派活，否则会两个进程抢端口
    state = launcher.read_state()
    if state.get("loading"):
        started = state.get("loading_started") or "?"
        return out({
            "ok": False,
            "error": "正在换模型（%s，从 %s 开始），等这次结束再试。"
                     % (state.get("loading_path") or "?", started),
            "loading": True,
        }, 3)

    # 想换的就是当前这个，没必要折腾
    loaded = launcher.current_loaded()
    if loaded and loaded.lower() == want.lower():
        return out({"ok": True, "changed": False, "file": want,
                    "message": "当前用的就是这个模型，不用换。", "loaded": loaded})

    if not SWITCHER.is_file():
        return out({"ok": False, "error": "找不到 switcher.py，换模型功能不完整。"}, 5)

    # 先落一个 loading 状态，页面立刻就能看到反馈
    launcher.write_state(
        loading=True, loading_path=want, loading_name=want,
        loading_started=time.strftime("%Y-%m-%d %H:%M:%S"),
        error=None, error_at=None)

    # 把活派出去，不等它
    flags = 0
    if os.name == "nt":
        flags = NO_WINDOW | 0x00000008 | 0x00000200      # 不弹窗 + 新进程组 + 新会话
    try:
        proc = subprocess.Popen(
            [str(PYEXE), str(SWITCHER), want],
            cwd=str(launcher.ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            creationflags=flags,
            close_fds=True,
        )
    except Exception as exc:
        launcher.write_state(loading=False, loading_path=None, loading_name=None,
                             error="启动换模型进程失败：%s" % exc)
        return out({"ok": False, "error": "启动换模型进程失败：%s" % exc}, 5)

    launcher.write_state(pid=proc.pid)
    return out({
        "ok": True,
        "changed": True,
        "file": want,
        "pid": proc.pid,
        "loading": True,
        "message": "已开始切换到「%s」，大约需要 1~2 分钟。页面会自动刷新进度。" % want,
    })


def main(argv):
    if len(argv) < 2:
        return out({
            "ok": False,
            "error": "用法：modelctl.py list | current | switch <文件名>",
        }, 2)
    action = argv[1].strip().lower()
    if action == "list":
        return cmd_list()
    if action == "current":
        return cmd_current()
    if action == "switch":
        return cmd_switch(argv[2:])
    return out({"ok": False, "error": "不认识的命令：%s（可用 list / current / switch）" % action}, 2)


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except Exception as exc:
        sys.stdout.write(json.dumps({"ok": False, "error": "modelctl 出错：%s" % exc},
                                    ensure_ascii=False))
        sys.exit(1)