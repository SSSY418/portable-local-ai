#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
体检技能 —— 只用标准库，输出一段 JSON 给工作台。

采集内容：
  CPU 占用 / 内存占用与总量
  显卡型号、驱动日期、当前哪块显卡在干活（有没有误用核显）
  磁盘剩余空间、固态还是机械
  最占资源的进程 top 5
  本地模型服务状态
  最后一句人话结论（纯规则判断，不经过模型）

原则：任何一条命令失败就跳过这一项，整个脚本绝不允许崩掉。
"""

import ctypes
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

IS_WIN = os.name == "nt"
NO_WINDOW = 0x08000000 if IS_WIN else 0
ROOT = Path(__file__).resolve().parent.parent.parent     # <根>  （skills 的上两级）
WORK_DIR = ROOT / "work"                                 # 技能唯一允许动文件的地方
PS_EXE = shutil.which("powershell") or shutil.which("pwsh")

KOBOLD_URLS = ("http://127.0.0.1:5001/api/extra/version",)
COMMAND_TIMEOUT = 25


# ------------------------------------------------------------------ 取数小工具

def run(cmd, timeout=COMMAND_TIMEOUT):
    """跑一条命令，返回 (退出码, 文本)。失败一律返回 (非0, 空串)，不抛异常。"""
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            creationflags=NO_WINDOW,
        )
        return proc.returncode, proc.stdout.decode("utf-8", "replace").strip()
    except Exception:
        return 1, ""


def ps_json(script, timeout=COMMAND_TIMEOUT):
    """跑一段 PowerShell 并把结果当 JSON 读回来。失败返回空列表。"""
    if not PS_EXE:
        return []
    wrapped = ("$ProgressPreference='SilentlyContinue';"
               "$OutputEncoding=[System.Text.Encoding]::UTF8;"
               "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
               "ConvertTo-Json -Depth 4 -Compress -InputObject (@(" + script + "))")
    code, out = run([PS_EXE, "-NoProfile", "-NonInteractive", "-Command", wrapped], timeout)
    if code != 0 or not out:
        return []
    out = out.strip()
    first = min([i for i in (out.find("["), out.find("{")) if i >= 0] or [-1])
    if first > 0:
        out = out[first:]
    try:
        data = json.loads(out)
    except Exception:
        return []
    if isinstance(data, dict):
        data = [data]
    return data if isinstance(data, list) else []


def first(d, *keys, default=None):
    """在字典里按多个可能的键名取值。"""
    if not isinstance(d, dict):
        return default
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return default


def num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def gb(nbytes):
    return round(num(nbytes) / (1024.0 ** 3), 2)


def gb_from_mb(mb):
    return round(num(mb) / 1024.0, 2)


def gb_from_kb(kb):
    """Win32_OperatingSystem 的内存字段单位是 KB，不是 MB。"""
    return round(num(kb) / (1024.0 ** 2), 2)


def fmt_driver_date(value):
    """
    WMI 给的驱动日期是 /Date(1780761600000)/ 这种鬼格式，
    转成 2026-06-09 这样的人话；转不了就原样显示。
    """
    if not value:
        return ""
    text = str(value).strip()
    m = re.search(r"/Date\((\d+)", text)
    if m:
        try:
            secs = int(m.group(1)) / 1000.0
            if secs > 1e11:            # 有的系统直接给毫秒
                secs /= 1000.0
            return time.strftime("%Y-%m-%d", time.localtime(secs))
        except Exception:
            return text[:10]
    return text[:10]


# ------------------------------------------------------------------ 各项检查

def check_cpu():
    try:
        rows = ps_json("Get-CimInstance Win32_Processor | "
                       "Select-Object Name,NumberOfCores,NumberOfLogicalProcessors,LoadPercentage,MaxClockSpeed")
    except Exception:
        rows = []
    if not rows:
        return {"ok": False, "note": "没能读到 CPU 信息"}
    r = rows[0]
    return {
        "ok": True,
        "name": str(first(r, "Name", default="未知 CPU")).strip(),
        "cores": first(r, "NumberOfCores", default=0),
        "threads": first(r, "NumberOfLogicalProcessors", default=0),
        "load_percent": round(num(first(r, "LoadPercentage", default=0)), 1),
        "max_mhz": first(r, "MaxClockSpeed", default=0),
    }


def check_memory():
    try:
        rows = ps_json("Get-CimInstance Win32_OperatingSystem | "
                       "Select-Object TotalVisibleMemorySize,FreePhysicalMemory,"
                       "TotalVirtualMemorySize,FreeVirtualMemory")
    except Exception:
        rows = []
    if not rows:
        return {"ok": False, "note": "没能读到内存信息"}
    r = rows[0]
    total_kb = num(first(r, "TotalVisibleMemorySize", default=0))
    free_kb = num(first(r, "FreePhysicalMemory", default=0))
    used_kb = max(0.0, total_kb - free_kb)
    pct = round(used_kb / total_kb * 100, 1) if total_kb else 0.0

    swap = []
    try:
        swap = ps_json("Get-CimInstance Win32_PageFileUsage | Select-Object Name,AllocatedBaseSize,CurrentUsage")
    except Exception:
        pass
    page = None
    if swap:
        s = swap[0]
        page = {
            "name": str(first(s, "Name", default="")),
            "allocated_gb": gb_from_mb(first(s, "AllocatedBaseSize", default=0)),
            "used_gb": gb_from_mb(first(s, "CurrentUsage", default=0)),
        }
    return {
        "ok": True,
        "total_gb": gb_from_kb(total_kb),
        "used_gb": gb_from_kb(used_kb),
        "free_gb": gb_from_kb(free_kb),
        "used_percent": pct,
        "pagefile": page,
    }


def nvidia_smi(*args):
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    code, out = run([exe] + list(args), timeout=15)
    return out if code == 0 and out else None


def check_gpu():
    result = {"ok": True, "cards": [], "nvidia": None, "primary": None, "notes": []}

    # 1) Windows 眼里的显卡（含驱动日期）
    try:
        rows = ps_json("Get-CimInstance Win32_VideoController | "
                       "Select-Object Name,DriverVersion,DriverDate,AdapterRAM,"
                       "CurrentHorizontalResolution,CurrentVerticalResolution,Status")
    except Exception:
        rows = []
    onboard = False
    for r in rows:
        name = str(first(r, "Name", default="未知显卡")).strip()
        low = name.lower()
        if any(k in low for k in ("intel", "uhd graphics", "hd graphics", "iris", "radeon graphics", "vega")):
            onboard = True
        res = first(r, "CurrentHorizontalResolution")
        vres = first(r, "CurrentVerticalResolution")
        result["cards"].append({
            "name": name,
            "driver_version": str(first(r, "DriverVersion", default="")).strip(),
            "driver_date": fmt_driver_date(first(r, "DriverDate", default="")),
            "vram_gb": gb(first(r, "AdapterRAM", default=0)),
            "resolution": ("%sx%s" % (res, vres)) if res else "",
            "status": str(first(r, "Status", default="")).strip(),
        })

    # 2) NVIDIA 独显的实际状态
    q = nvidia_smi("--query-gpu=name,driver_version,memory.total,memory.used,utilization.gpu,temperature.gpu",
                   "--format=csv,noheader,nounits")
    if q:
        line = q.strip().splitlines()[0]
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 6:
            total = num(parts[2])
            used = num(parts[3])
            result["nvidia"] = {
                "name": parts[0],
                "driver_version": parts[1],
                "vram_total_gb": round(total / 1024.0, 2),
                "vram_used_gb": round(used / 1024.0, 2),
                "vram_used_percent": round(used / total * 100, 1) if total else 0.0,
                "utilization_percent": num(parts[4]),
                "temperature_c": num(parts[5]),
            }
        procs = nvidia_smi("--query-compute-apps=pid,process_name,used_memory", "--format=csv,noheader")
        if procs:
            result["nvidia_processes"] = [p.strip() for p in procs.strip().splitlines() if p.strip()]

    # 3) 判断到底哪块卡在干活
    nv = result["nvidia"]
    if onboard and nv:
        if nv["utilization_percent"] <= 1 and not result.get("nvidia_processes"):
            result["primary"] = "核显（Intel 集成显卡）在显示，独显空闲"
            result["notes"].append("当前画面是核显在输出，独显没在干活。")
        else:
            result["primary"] = "独显（%s）在干活" % nv["name"]
    elif nv:
        result["primary"] = "独显（%s）在干活" % nv["name"]
    elif onboard:
        result["primary"] = "只有核显，没有检测到独立的 NVIDIA 显卡"
        result["notes"].append("只有核显的话，跑模型会明显偏慢。")
    else:
        result["primary"] = "没能判断出主显卡"
    return result


def volume_info(path, bench=None):
    """目标盘的总容量/剩余空间，以及固态还是机械。"""
    info = {"path": str(path), "ok": False}
    try:
        drive = os.path.splitdrive(str(path))[0]
        if not drive:
            drive = str(path)[:2]
        free_bytes = ctypes.c_ulonglong(0)
        total_bytes = ctypes.c_ulonglong(0)
        avail_bytes = ctypes.c_ulonglong(0)
        ctypes.windll.kernel32.GetDiskFreeSpaceExW(
            ctypes.c_wchar_p(drive + "\\"),
            ctypes.byref(avail_bytes), ctypes.byref(total_bytes), ctypes.byref(free_bytes))
        info.update({
            "ok": True,
            "drive": drive,
            "total_gb": gb(total_bytes.value),
            "free_gb": gb(free_bytes.value),
            "used_gb": gb(total_bytes.value - free_bytes.value),
            "free_percent": round(free_bytes.value / total_bytes.value * 100, 1) if total_bytes.value else 0.0,
        })
    except Exception as exc:
        info["note"] = "读磁盘容量失败：%s" % exc

    # 固态还是机械
    try:
        dl = (info.get("drive") or "").rstrip("\\")
        if dl:
            rows = ps_json("Get-Partition -DriveLetter '%s' -ErrorAction SilentlyContinue | "
                           "Get-Disk -ErrorAction SilentlyContinue | "
                           "Select-Object FriendlyName,MediaType,BusType,Size" % dl.replace("'", ""))
            if rows:
                r = rows[0]
                media = str(first(r, "MediaType", default="")).strip()
                bus = str(first(r, "BusType", default="")).strip()
                model = str(first(r, "FriendlyName", default="")).strip()
                info["media_type"] = media
                info["bus_type"] = bus
                info["disk_model"] = model
                kind = None
                if media == "4":
                    kind = "固态硬盘（SSD）"
                elif media == "3":
                    kind = "机械硬盘（HDD）"
                else:
                    # Windows 没说是啥，先看型号名里有没有线索
                    low = model.lower()
                    if any(k in low for k in ("ssd", "nvme", "solid")):
                        kind = "固态硬盘（SSD）"
                    elif any(k in low for k in ("hdd", "hard disk", "机械")):
                        kind = "机械硬盘（HDD）"
                if kind:
                    info["kind"] = kind
                    info["kind_source"] = "系统报告的硬盘类型"
                else:
                    # 靠实测读写速度判断（结果缓存 7 天，不用每次都测）
                    bench = bench or {}
                    if bench.get("kind"):
                        info["kind"] = bench["kind"]
                        info["kind_source"] = "实测读写速度（%s）" % bench.get("measured_at", "")
                    else:
                        info["kind"] = None
                        info["kind_note"] = "读不到硬盘类型，也没测出速度"
    except Exception:
        pass
    return info


def probe_disk_speed(force=False):
    """
    实测项目盘的速度，用来判断固态还是机械。
    Windows 对 USB 移动盘常常不报告类型，只能自己测。

    只在 work\\ 里写一个临时文件，测完立刻删掉；结果缓存 7 天。
    磁盘太满（可用不足 1200 MB）就跳过，绝不给用户添麻烦。
    """
    cache = WORK_DIR / "disk_speed.json"
    try:
        if not force and cache.is_file() and (time.time() - cache.stat().st_mtime) < 7 * 86400:
            data = json.loads(cache.read_text(encoding="utf-8", errors="replace"))
            if isinstance(data, dict) and data.get("kind"):
                data["from_cache"] = True
                return data
    except Exception:
        pass

    out = {"measured": False}
    try:
        free_mb = shutil.disk_usage(str(WORK_DIR if WORK_DIR.exists() else ROOT)).free / (1024.0 * 1024.0)
    except Exception:
        free_mb = 9999
    size_mb = int(os.environ.get("STATUS_BENCH_MB", "100"))
    if free_mb < 1200:
        out["note"] = "磁盘可用空间不足，跳过速度测试"
        return out

    try:
        WORK_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    tmp = WORK_DIR / "disk_speed_test.bin"
    mb = 1024 * 1024
    chunk = os.urandom(mb)
    try:
        # 顺序写：连续写 1 MB，测大文件写入速度
        t0 = time.perf_counter()
        with open(tmp, "wb", buffering=0) as fh:
            for _ in range(size_mb):
                fh.write(chunk)
            fh.flush()
            os.fsync(fh.fileno())
        write_secs = time.perf_counter() - t0
        seq_write = round(size_mb / write_secs, 1) if write_secs > 0 else 0.0

        # 随机写：跳着写 4 KB，机械硬盘每次找位置都要等，延迟会明显高
        lats = []
        with open(tmp, "r+b", buffering=0) as fh:
            for i in range(24):
                fh.seek(((i * 7919) % size_mb) * mb)
                t1 = time.perf_counter()
                fh.write(chunk[:4096])
                fh.flush()
                os.fsync(fh.fileno())
                lats.append((time.perf_counter() - t1) * 1000.0)
        lats.sort()
        lat_med = round(lats[len(lats) // 2], 2)
        lat_p90 = round(lats[int(len(lats) * 0.9)], 2)

        # 随机写延迟是判断盘类型最可靠的一条：机械硬盘的磁头要寻道，
        # 延迟通常是固态的几十倍，缓存也压不下来。
        if lat_med < 1.0 and seq_write >= 120:
            kind = "固态硬盘（SSD，实测）"
        elif lat_med > 60.0:
            kind = "机械硬盘（HDD，实测）"
        elif seq_write < 60:
            kind = "U 盘 / 存储卡级别的闪存（实测，比固态慢不少）"
        else:
            kind = "闪存盘（实测，速度中等）"

        out.update({
            "measured": True,
            "seq_write_mbps": seq_write,
            "rand_write_latency_ms": lat_med,
            "rand_write_p90_ms": lat_p90,
            "kind": kind,
            "measured_at": time.strftime("%Y-%m-%d %H:%M"),
            "test_size_mb": size_mb,
        })
        try:
            cache.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass
    except Exception as exc:
        out["note"] = "速度测试失败：%s" % exc
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass
    return out


def check_disk():
    out = {"ok": True, "project_volume": None, "volumes": [], "notes": []}
    try:
        # 先实测盘的速度（固态/机械），只在 work\ 里写临时文件，7 天缓存一次
        out["speed_test"] = probe_disk_speed()
    except Exception as exc:
        out["speed_test"] = {"measured": False, "note": str(exc)}
    try:
        out["project_volume"] = volume_info(ROOT, out.get("speed_test"))
    except Exception as exc:
        out["notes"].append("项目所在盘读不到：%s" % exc)
    try:
        rows = ps_json("Get-CimInstance Win32_LogicalDisk -Filter \"DriveType=3\" | "
                       "Select-Object DeviceID,Size,FreeSpace,FileSystem,VolumeName")
        for r in rows:
            total = num(first(r, "Size", default=0))
            free = num(first(r, "FreeSpace", default=0))
            out["volumes"].append({
                "drive": str(first(r, "DeviceID", default="")),
                "label": str(first(r, "VolumeName", default="") or ""),
                "fs": str(first(r, "FileSystem", default="")),
                "total_gb": gb(total),
                "free_gb": gb(free),
                "free_percent": round(free / total * 100, 1) if total else 0.0,
            })
    except Exception:
        pass
    return out


def check_processes(top=5):
    """
    最占资源的进程。
    样本太少不准，所以取两次（间隔 1 秒）算真实 CPU 占用率。
    只读信息，不结束任何进程。
    """
    snap1 = ps_json("Get-Process | Select-Object Id,ProcessName,WorkingSet64,CPU", timeout=30)
    time.sleep(1.0)
    snap2 = ps_json("Get-Process | Select-Object Id,ProcessName,WorkingSet64,CPU", timeout=30)
    if not snap2:
        return {"ok": False, "note": "没能读到进程列表", "items": []}

    def index(snap):
        d = {}
        for p in snap:
            try:
                d[int(first(p, "Id", default=-1))] = p
            except Exception:
                continue
        return d

    i1, i2 = index(snap1), index(snap2)
    n_cpu = os.cpu_count() or 1
    rows = []
    for pid, p2 in i2.items():
        try:
            mem = num(first(p2, "WorkingSet64", default=0))
            cpu2 = num(first(p2, "CPU", default=0))
            p1 = i1.get(pid)
            cpu1 = num(first(p1, "CPU", default=0)) if p1 else 0.0
            delta = max(0.0, cpu2 - cpu1)
            pct = round(delta / n_cpu * 100, 1)      # 相对整机的占用率
            rows.append({
                "pid": pid,
                "name": str(first(p2, "ProcessName", default="?")),
                "mem_mb": round(mem / (1024.0 * 1024.0), 1),
                "cpu_percent": pct,
                "cpu_seconds": round(cpu2, 1),
            })
        except Exception:
            continue

    rows.sort(key=lambda x: (x["mem_mb"], x["cpu_seconds"]), reverse=True)
    items = rows[:top]

    # 顺手看看有没有「明明是同一个程序却开了一堆」的情况。
    # Windows 自带的后台进程（svchost / conhost 之类）本来就一大堆，
    # 报出来只会干扰判断，所以这些一律不算。
    SYSTEM_PROCESSES = {
        "svchost", "conhost", "csrss", "wininit", "winlogon", "services", "lsass",
        "smss", "dwm", "explorer", "taskhostw", "sihost", "ctfmon", "fontdrvhost",
        "runtimebroker", "searchindexer", "searchapp", "shellexperiencehost",
        "startmenuexperiencehost", "textinputhost", "systemsettings", "backgroundtaskhost",
        "wmiprvse", "dllhost", "spoolsv", "audiodg", "securityhealthservice",
        "securityhealthsystray", "msmpeng", "nissrv", "registry", "memory compression",
        "msedgewebview2", "widgets", "phoneexperiencehost", "crossdeviceservice",
        "lockapp", "useroobebroker", "applicationframehost", "crashpad_handler",
    }
    counts = {}
    for r in rows:
        key = r["name"].lower()
        if key in SYSTEM_PROCESSES:
            continue
        counts[r["name"]] = counts.get(r["name"], 0) + 1
    dupes = sorted([(k, v) for k, v in counts.items() if v >= 4], key=lambda x: -x[1])[:3]

    return {
        "ok": True,
        "items": items,
        "duplicate_groups": [{"name": k, "count": v} for k, v in dupes],
        "total_processes": len(rows),
    }


def check_model_service():
    out = {"ok": True, "running": False, "url": "http://127.0.0.1:5001", "detail": None}
    try:
        import urllib.request
        for url in KOBOLD_URLS:
            try:
                with urllib.request.urlopen(url, timeout=3) as resp:
                    raw = resp.read().decode("utf-8", "replace")
                out["running"] = True
                try:
                    data = json.loads(raw)
                    if isinstance(data, dict):
                        out["detail"] = {
                            "version": data.get("version"),
                            "backend": data.get("backend"),
                        }
                except Exception:
                    pass
                break
            except Exception as exc:
                out["error"] = str(exc)
    except Exception as exc:
        out["error"] = str(exc)
    models = []
    try:
        for p in sorted((ROOT / "models").iterdir()):
            if p.is_file() and p.suffix.lower() == ".gguf":
                models.append({"name": p.name, "size_gb": round(p.stat().st_size / (1024.0 ** 3), 2)})
    except Exception:
        pass
    out["models"] = models
    return out


# ------------------------------------------------------------------ 规则结论

def make_conclusion(cpu, mem, gpu, disk, proc, svc):
    """纯规则判断，挑最要紧的一条讲人话。"""
    lines = []

    if not svc.get("running"):
        lines.append("模型服务现在没在跑，对话页会连不上；从「启动.bat」重新启动就行。")

    pv = (disk or {}).get("project_volume") or {}
    if pv.get("ok") and pv.get("free_gb") is not None:
        if pv["free_gb"] < 6:
            lines.append("项目盘只剩 %.1f GB，空间偏紧，建议先清点东西再下模型。" % pv["free_gb"])
    bench = (disk or {}).get("speed_test") or {}
    if bench.get("measured") and bench.get("seq_write_mbps") is not None:
        if bench["seq_write_mbps"] < 100:
            lines.append("项目盘实测写入只有 %.0f MB/s，加载模型会慢一些，"
                         "换固态硬盘（SSD）体验会明显更好。" % bench["seq_write_mbps"])
        if bench.get("rand_write_latency_ms") and bench["rand_write_latency_ms"] > 20:
            lines.append("这个盘随机读写延迟偏高（%.0f 毫秒），适合存模型，不太适合频繁读写小文件。"
                         % bench["rand_write_latency_ms"])
    for v in (disk or {}).get("volumes") or []:
        if v.get("drive", "").rstrip("\\").upper() != (pv.get("drive") or "").rstrip("\\").upper():
            if v.get("free_percent") is not None and v["free_percent"] < 10:
                lines.append("%s 盘只剩 %.1f%% 空间，也顺手清一下吧。" % (v["drive"], v["free_percent"]))

    if mem.get("ok"):
        if mem["used_percent"] >= 90:
            lines.append("内存已经用掉 %.0f%%，只剩 %.1f GB，跑 4B 模型很可能不够；"
                         "先关掉些占内存的程序再聊。" % (mem["used_percent"], mem["free_gb"]))
        elif mem["used_percent"] >= 75:
            lines.append("内存用掉 %.0f%%，还算能跑，但别再开太多程序。" % mem["used_percent"])
        # 虚拟内存只作为明细展示，不写进结论：改它要动系统设置，而且改不改都能用，
        # 每次都提只会让结论变吵。

    nv = (gpu or {}).get("nvidia")
    if nv:
        if nv.get("vram_used_percent", 0) >= 90:
            lines.append("显卡显存已占用 %.0f%%，加载模型可能失败；"
                         "可以把 --gpulayers 调小一点再试。" % nv["vram_used_percent"])
        if nv.get("temperature_c", 0) >= 85:
            lines.append("显卡温度 %.0f℃，偏热，注意散热。" % nv["temperature_c"])
    primary = (gpu or {}).get("primary") or ""
    if "核显" in primary and nv:
        lines.append("画面是核显在输出、独显闲着，这属于正常；跑模型时独显会被用起来。")
    if nv is None and (gpu or {}).get("cards"):
        lines.append("没检测到 NVIDIA 独显，只能靠 CPU 算，速度会慢很多。")

    if cpu.get("ok") and cpu.get("load_percent", 0) >= 85:
        lines.append("CPU 占用 %.0f%%，现在有点忙。" % cpu["load_percent"])

    for d in (proc or {}).get("duplicate_groups") or []:
        lines.append("%s 一下子开了 %d 个，不是你特意开的话可以关掉一些。" % (d["name"], d["count"]))

    if not lines:
        lines.append("硬件状态正常，可以放心用。")
    return " ".join(lines)


# ------------------------------------------------------------------ 主流程

def main():
    report = {"ok": True, "generated_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    cpu = mem = gpu = disk = proc = svc = {}
    for key, fn in (("cpu", check_cpu), ("memory", check_memory), ("gpu", check_gpu),
                    ("disk", check_disk), ("processes", check_processes),
                    ("model_service", check_model_service)):
        try:
            report[key] = fn()
        except Exception as exc:
            report[key] = {"ok": False, "note": "%s 检查失败：%s" % (key, exc)}
        report.setdefault("skipped", [])
    try:
        report["conclusion"] = make_conclusion(
            report.get("cpu") or {}, report.get("memory") or {}, report.get("gpu") or {},
            report.get("disk") or {}, report.get("processes") or {}, report.get("model_service") or {})
    except Exception as exc:
        report["conclusion"] = "结论生成失败：%s" % exc

    text = json.dumps(report, ensure_ascii=False, indent=2)
    try:
        sys.stdout.write(text)
        sys.stdout.flush()
    except Exception:
        sys.stdout.buffer.write(text.encode("utf-8", "replace"))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:                       # 最后一道保险：绝不崩
        sys.stdout.write(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        sys.exit(0)