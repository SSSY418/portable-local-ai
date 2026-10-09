#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
后台换模型。

为什么单独一个脚本、还要脱离父进程跑：
换模型要「停掉旧的 → 起新的 → 等就绪」，这一步要 1~2 分钟。
如果直接卡在 HTTP 请求里等，页面就一直转圈、用户也不知道进度。
所以 modelctl.py switch 只负责"把活儿派出去"就立刻返回，
真正干活的是这个脚本，进度通过 work\model_state.json 让页面轮询。

用法（正常由 modelctl.py 调起，一般不用手敲）：
    python switcher.py <模型文件名>
"""

import os
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))   # 让 import launcher 能找到

import launcher  # noqa: E402


def main(argv):
    if len(argv) < 2:
        launcher.write_state(loading=False, error="switcher 没收到模型文件名")
        return 2
    want = argv[1].strip()

    target = launcher.MODELS_DIR / want
    # 只认 models\ 里真实存在的文件，别的路径一律拒绝 —— 技能不许碰别的地方
    if Path(want).name != want or not target.is_file():
        launcher.write_state(loading=False, loading_path=None, loading_name=None,
                             error="models\\ 里没有这个文件：%s" % want)
        return 2

    launcher.write_state(
        loading=True, loading_path=want, loading_name=want,
        loading_started=time.strftime("%Y-%m-%d %H:%M:%S"),
        error=None, error_at=None, pid=os.getpid(),
    )

    try:
        # 1) 先问一下旧服务用的什么参数，尽量沿用（上下文这些）
        ctx = None
        gpu_layers = None
        st = launcher.read_state()
        if st.get("last_ctx"):
            ctx = st.get("last_ctx")
        if st.get("last_gpulayers") is not None:
            gpu_layers = st.get("last_gpulayers")

        # 2) 停掉旧的
        killed = launcher.stop_server()
        if killed:
            launcher.write_state(note="已停止旧服务 pid=%s" % ",".join(killed))

        # 3) 起新的（按优先顺序挑后端，起不来会自动清掉再换下一个）
        ok, exe_name, why = launcher.launch_preferred(want, ctx=ctx, gpu_layers=gpu_layers,
                                                      wait_seconds=150)

        if ok:
            launcher.write_state(
                loading=False, loading_path=None, loading_name=None, loading_started=None,
                loaded_path=want, last_backend=exe_name,
                last_ctx=ctx or launcher.DEFAULT_CTX,
                last_gpulayers=gpu_layers if gpu_layers is not None else launcher.DEFAULT_GPULAYERS,
                error=None, error_at=None)
            return 0

        # 4) 失败：收干净，把错误写清楚给页面看
        launcher.stop_server()
        launcher.write_state(loading=False, loading_path=None, loading_name=None,
                             loading_started=None,
                             error="换到「%s」失败：%s" % (want, why),
                             error_at=time.strftime("%Y-%m-%d %H:%M:%S"))
        return 1

    except Exception as exc:
        try:
            launcher.stop_server()
        except Exception:
            pass
        launcher.write_state(
            loading=False, loading_path=None, loading_name=None, loading_started=None,
            error="换模型过程中出错：%s" % exc,
            error_at=time.strftime("%Y-%m-%d %H:%M:%S"),
            traceback=traceback.format_exc()[-1500:])
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))