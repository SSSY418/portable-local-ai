#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
自然语言翻译器（第二期）。

把用户随口说的一句话，交给本地模型翻译成「调哪个技能、带什么参数」的 JSON。

    python router.py "帮我看看显卡用上了没"
    python router.py --list

输出 JSON：
    {"ok": true, "skill": "status", "action": "run", "args": [], "reason": "..."}
    {"ok": true, "skill": null, "reason": "这句话跟现有技能无关"}

重要：这个脚本**只负责翻译，不执行任何东西**。
真正执行在工作台的 /api/skill/run 里，而且执行前会再校验一遍参数。
这样模型万一胡说八道（挑了个不存在的技能、给了奇怪的参数），
最坏结果只是"翻译错"，不会真的动到东西。

只用标准库。
"""

import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILLS_JSON = HERE / "skills.json"

KOBOLD_BASE = "http://127.0.0.1:5001"
MODEL_TIMEOUT = 120


# ------------------------------------------------------------------ 清单

def load_skills():
    """读技能清单；坏了就返回空，让上层报错，别崩。"""
    try:
        data = json.loads(SKILLS_JSON.read_text(encoding="utf-8", errors="replace"))
        skills = data.get("skills")
        return skills if isinstance(skills, list) else []
    except Exception:
        return []


def skill_catalog_text(skills):
    """把技能清单压成一段给模型看的文字。"""
    lines = []
    for s in skills:
        sid = s.get("id") or ""
        title = s.get("title") or ""
        desc = (s.get("description") or "").strip()
        lines.append("- 技能 %s（%s）：%s" % (sid, title, desc))
        for aname, a in (s.get("actions") or {}).items():
            need = a.get("args") or []
            lines.append("    动作 %s：%s%s" % (
                aname, a.get("description") or "",
                ("　参数：" + "、".join(need)) if need else "　（不需要参数）"))
    return "\n".join(lines)


def build_prompt(skills, user_text, model_files):
    catalog = skill_catalog_text(skills)
    files_hint = ""
    if model_files:
        files_hint = ("\n当前 models 目录里可用的模型文件名（换成模型时 file 只能从这里选）：\n"
                      + "\n".join("  " + f for f in model_files) + "\n")

    system = (
        "你是一个命令翻译器。你的唯一工作是把用户的话翻译成 JSON，"
        "用来选择下面这些技能里的某一个。\n\n"
        "可用技能：\n" + catalog + "\n" + files_hint + "\n"
        "规则：\n"
        "1. 只输出一个 JSON 对象，不要输出任何解释、不要用 markdown 代码块。\n"
        "2. JSON 的字段固定为：intent（用户想干什么，一句话）、skill（技能 id）、"
        "action（动作名）、args（参数数组）、reason（为什么这么选）。\n"
        "3. skill 只能是上面列出的技能 id；如果用户的话跟这些技能都没关系，"
        "skill 和 action 都填 null，并在 reason 里说明。\n"
        "4. switch 动作的 args 要放模型文件名，且只能从上面列出的文件名里挑。\n"
        "5. 不许编造技能或动作名。\n\n"
        "示例输出：{\"intent\":\"想看电脑状态\",\"skill\":\"status\",\"action\":\"run\","
        "\"args\":[],\"reason\":\"用户要求体检\"}"
    )
    return system, ("用户说：" + user_text)


# ------------------------------------------------------------------ 问模型

def ask_model(system, user):
    payload = {
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.2,
        "max_tokens": 400,
        "stream": False,
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        KOBOLD_BASE + "/v1/chat/completions", data=body, method="POST",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=MODEL_TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8", "replace"))
    return (data.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""


def extract_json(text):
    """
    从模型输出里把 JSON 抠出来。
    模型很爱在前后加 ```json 或者一句废话，所以先找最外层的大括号。
    """
    if not text:
        return None
    t = text.strip()
    t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    try:
        return json.loads(t)
    except Exception:
        pass
    start = t.find("{")
    end = t.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(t[start:end + 1])
        except Exception:
            return None
    return None


# ------------------------------------------------------------------ 参数校验

def resolve_model_file(raw, model_files):
    """
    把模型报的文件名对齐到真实存在的文件。
    模型经常把 .gguf 漏掉或者只写一半，这里宽容一点，但最终必须是真实文件。
    """
    if not raw:
        return None, "没给出模型文件名"
    want = str(raw).strip().replace("\\", "/").split("/")[-1].lower()
    if not want:
        return None, "模型文件名是空的"
    # 1) 完全不差
    for f in model_files:
        if f.lower() == want:
            return f, None
    # 2) 少写了 .gguf
    for f in model_files:
        if f.lower() == want + ".gguf":
            return f, None
    # 3) 带了 .gguf 但实际没有
    for f in model_files:
        stem = f[:-5].lower() if f.lower().endswith(".gguf") else f.lower()
        if stem == want or stem == (want[:-5] if want.endswith(".gguf") else want):
            return f, None
    # 4) 唯一子串匹配（只在只有一个候选时才敢认）
    hits = [f for f in model_files if want in f.lower() or f.lower().startswith(want)]
    if len(hits) == 1:
        return hits[0], None
    if not model_files:
        return None, "models 目录里没有任何模型文件"
    return None, "models 目录里找不到「%s」（现有：%s）" % (raw, "、".join(model_files))


def validate(plan, skills, model_files):
    """
    把模型给的 JSON 校验成靠谱的计划。
    返回 (校验后的计划, 错误说明)。任何不确定的地方都判失败。
    """
    if not isinstance(plan, dict):
        return None, "模型没有输出合法的 JSON"

    raw_skill = plan.get("skill")
    if raw_skill is None or str(raw_skill).strip().lower() in ("", "null", "none"):
        return {"skill": None, "action": None, "args": [],
                "reason": plan.get("reason") or "这句话跟现有技能没关系",
                "intent": plan.get("intent")}, None

    sid = str(raw_skill).strip()
    skill = None
    for s in skills:
        if (s.get("id") or "").lower() == sid.lower():
            skill = s
            break
    if skill is None:
        return None, "模型挑了一个不存在的技能：%s" % sid

    action = str(plan.get("action") or "").strip()
    actions = skill.get("actions") or {}
    if action not in actions:
        if len(actions) == 1:
            action = next(iter(actions))          # 只有一个动作就宽容一点
        else:
            return None, "技能 %s 没有「%s」这个动作（可用：%s）" % (
                sid, action, "、".join(actions.keys()))

    spec = actions[action]
    raw_args = plan.get("args")
    if raw_args is None:
        raw_args = []
    if not isinstance(raw_args, list):
        raw_args = [raw_args]
    args = [str(a) for a in raw_args]

    need = spec.get("args") or []
    file_arg = spec.get("file_arg")

    if need:
        # 需要参数：把模型给的参数整理成位置参数
        if file_arg:
            # 找出模型想换成哪个模型
            cand = None
            for a in args:
                if a.strip():
                    cand = a.strip()
                    break
            if cand is None:
                cand = plan.get("file") or plan.get("model") or ""
            real, err = resolve_model_file(cand, model_files)
            if err:
                return None, err
            args = [real]
        else:
            if not args:
                return None, "技能 %s 的动作 %s 需要参数（%s），但模型没给" % (
                    sid, action, "、".join(need))
            args = args[:len(need)]
    else:
        args = []          # 不需要参数的技能，一律忽略模型给的多余参数

    return {
        "skill": skill.get("id"),
        "action": action,
        "args": args,
        "readonly": bool(spec.get("readonly")),
        "reason": plan.get("reason") or "",
        "intent": plan.get("intent") or "",
        "script": skill.get("script"),
    }, None


# ------------------------------------------------------------------ 主流程

def route(user_text, model_files=None):
    skills = load_skills()
    if not skills:
        return {"ok": False, "error": "读不到 skills.json，或者里面没有技能。"}

    if model_files is None:
        try:
            model_files = sorted(p.name for p in (HERE.parent.parent / "models").iterdir()
                                 if p.is_file() and p.suffix.lower() == ".gguf")
        except Exception:
            model_files = []

    system, user = build_prompt(skills, user_text, model_files)
    try:
        raw = ask_model(system, user)
    except urllib.error.HTTPError as exc:
        return {"ok": False, "error": "模型返回错误 %s：%s" % (exc.code, exc.reason)}
    except Exception as exc:
        return {"ok": False,
                "error": "连不上本地模型服务（%s）。请确认 koboldcpp 正在运行。" % exc}

    plan = extract_json(raw)
    good, err = validate(plan, skills, model_files)
    if err:
        return {"ok": False, "error": err, "raw_model_output": raw[:600]}
    good["ok"] = True
    good["raw_model_output"] = raw[:600]
    return good


def main(argv):
    args = [a for a in argv[1:]]
    if not args or args[0] in ("-h", "--help"):
        print(json.dumps({"ok": False,
                          "error": "用法：router.py \"用户说的话\"  或  router.py --list"},
                         ensure_ascii=False, indent=2))
        return 2
    if args[0] == "--list":
        skills = load_skills()
        print(json.dumps({"ok": True, "skills": [
            {"id": s.get("id"), "title": s.get("title"),
             "actions": list((s.get("actions") or {}).keys())} for s in skills]},
            ensure_ascii=False, indent=2))
        return 0

    text = " ".join(args).strip()
    if not text:
        print(json.dumps({"ok": False, "error": "没收到要说的话"}, ensure_ascii=False))
        return 2
    result = route(text)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except Exception as exc:
        sys.stdout.write(json.dumps({"ok": False, "error": "router 出错：%s" % exc},
                                    ensure_ascii=False))
        sys.exit(1)