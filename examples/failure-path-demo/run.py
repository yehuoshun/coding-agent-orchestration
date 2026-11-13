#!/usr/bin/env python3
"""失败路径演练：验证「测试红→回 Coder→再验」的回环，以及重试超限的熔断。

两个场景：
  A. recover  —— 第 1 次改错、第 2 次改对 → 应 done
  B. circuit  —— 一直改不对 → 3 次后熔断 → failed + 证据落盘

每轮都真跑：verify 命令（G1）+ scripts/gate.py（G3/G4/G6/G7）。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
GATE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "scripts", "gate.py")
MAX_RETRY = 3

BUGGY = "def add(a, b):\n    return a - b\n"
WRONG = "def add(a, b):\n    return a * b\n"
RIGHT = "def add(a, b):\n    return a + b\n"
TEST = "from calc import add\nassert add(2, 3) == 5, f'got {add(2,3)}'\nprint('calc ok')\n"


def sh(*args, cwd=None):
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"   # 禁止写字节码缓存（见下方 trap 注释）
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, env=env)


def purge_pycache(root):
    """清掉所有 __pycache__。

    踩坑记录：Python 的 .pyc 失效判断用的是 (源文件 mtime 秒, 源文件字节数)。
    重试循环里如果新版本与旧版本【字节数相同】且【写盘落在同一秒】，
    Python 会认为源文件没变 → 复用旧字节码 → verify 永远红（或假绿）。
    所以验证命令必须跑在无缓存环境：清 __pycache__ + python -B。
    """
    for dirpath, dirnames, _ in os.walk(root):
        if "__pycache__" in dirnames:
            shutil.rmtree(os.path.join(dirpath, "__pycache__"), ignore_errors=True)
            dirnames.remove("__pycache__")


def g1_verify(wt):
    purge_pycache(wt)
    r = sh(sys.executable, "-B", "test_calc.py", cwd=wt)
    return r.returncode == 0, (r.stdout + r.stderr).strip()


def setup(root, name):
    """建 base 仓库 + 任务 worktree，返回 (base, wt, handoff_path)。"""
    base = os.path.join(root, name, "base")
    os.makedirs(base)
    g = lambda *a: sh("git", *a, cwd=base)
    g("init", "-q", "-b", "main")
    g("config", "user.email", "t@t.t")
    g("config", "user.name", "t")
    open(os.path.join(base, "calc.py"), "w").write(BUGGY)
    open(os.path.join(base, "test_calc.py"), "w").write(TEST)
    g("add", "-A")
    g("commit", "-q", "-m", "init: buggy add")
    wt = os.path.join(root, name, "wt")
    sh("git", "-C", base, "worktree", "add", "-q", wt, "-b", "task")

    hp = os.path.join(root, name, "handoff.json")
    json.dump({
        "task_id": f"{name}-fix-add",
        "goal": "修好 add()：2+3 应为 5",
        "repo": "demo/calc",
        "base_ref": "main",
        "worktree": wt,
        "constraints": ["不得改 test_calc.py"],
        "subtasks": [{"id": "s1", "title": "修正 add 实现", "files": ["calc.py"],
                      "verify": "python3 test_calc.py", "token_budget": 100000}],
    }, open(hp, "w"), ensure_ascii=False)
    return base, wt, hp


def g1_verify(wt):
    r = sh(sys.executable, "test_calc.py", cwd=wt)
    return r.returncode == 0, (r.stdout + r.stderr).strip()


def gates(wt, hp, base, tokens):
    purge_pycache(wt)
    r = sh(sys.executable, "-B", GATE, "--handoff", hp, "--repo", wt,
           "--base", "main", "--tokens", str(tokens))
    return r.returncode == 0, (r.stdout + r.stderr).strip()


def run_task(root, name, coder_plan):
    print(f"\n{'='*54}\n▶ 场景 {name}：{coder_plan.__doc__.strip()}\n{'='*54}")
    base, wt, hp = setup(root, name)
    state = "pending"
    history = []
    for attempt in range(1, MAX_RETRY + 1):
        code = coder_plan(attempt)
        open(os.path.join(wt, "calc.py"), "w").write(code)
        state = "testing"
        ok1, out1 = g1_verify(wt)
        ok2, out2 = gates(wt, hp, base, tokens=40000)
        print(f"  轮次 {attempt} | state={state} | verify={'绿' if ok1 else '红'} | gates={'全过' if ok2 else 'FAIL'}")
        if not ok1:
            print(f"    ↳ 错误原文: {out1.splitlines()[0] if out1 else '(no output)'}")
        if ok1 and ok2:
            state = "done"
            history.append({"attempt": attempt, "verify": "pass"})
            print(f"  ✅ 第 {attempt} 轮通过 → state=done")
            break
        history.append({"attempt": attempt, "verify": "fail",
                        "verify_out": out1, "gates_out": out2})
        print(f"    ↻ 打回 Coder（重试 {attempt}/{MAX_RETRY}）")
    else:
        state = "failed"
        ev = os.path.join(root, name, "failed-evidence.json")
        json.dump({"task": f"{name}-fix-add", "last_state": "testing",
                   "retries": MAX_RETRY, "history": history,
                   "why": f"重试超限（{MAX_RETRY}），触发熔断"},
                  open(ev, "w"), ensure_ascii=False, indent=2)
        print(f"  🛑 重试超限 → 熔断 → state=failed")
        print(f"     证据已落盘: {ev}")
    return state, len(history)


def plan_recover(attempt):
    """第 1 次写错、第 2 次改对。"""
    return WRONG if attempt < 2 else RIGHT


def plan_circuit(attempt):
    """一直改不对（模拟方向性错误，怎么试都错）。"""
    return WRONG


def main():
    root = tempfile.mkdtemp(prefix="fail-demo-")
    print(f"工作目录: {root}")
    try:
        s1, n1 = run_task(root, "recover", plan_recover)
        s2, n2 = run_task(root, "circuit", plan_circuit)
        print(f"\n{'='*54}\n结果汇总")
        print(f"  recover: state={s1}（用了 {n1} 轮）  ← 期望 done")
        print(f"  circuit: state={s2}（用了 {n2} 轮）  ← 期望 failed")
        ok = (s1 == "done" and n1 == 2) and (s2 == "failed" and n2 == MAX_RETRY)
        print(f"  {'✅ 回环与熔断行为符合预期' if ok else '❌ 行为不符预期'}")
        shutil.rmtree(root, ignore_errors=True)
        sys.exit(0 if ok else 1)
    except Exception:
        print(f"（保留现场供排查: {root}）")
        raise


if __name__ == "__main__":
    main()
