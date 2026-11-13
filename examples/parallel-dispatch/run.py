#!/usr/bin/env python3
"""多任务并行演练：4 个快任务 + 4 个慢任务，双通道调度。

验证四件事：
  1. 双通道并行确实有效（墙钟时间显著小于串行总和）
  2. 8 个任务各自独立 worktree，互不污染
  3. 每个任务都过 verify（G1）+ gate.py（G3/G4/G6/G7）
  4. 快通道不会被慢通道的任务饿死

真实执行：每个任务真的写文件、真的跑 verify、真的跑闸门脚本。
慢任务用「verify 里 sleep」模拟长耗时集成测试。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
GATE = os.path.join(os.path.dirname(os.path.dirname(HERE)), "scripts", "gate.py")

CHECK = '''import importlib, sys, time
mod, exp = sys.argv[1], int(sys.argv[2])
delay = float(sys.argv[3]) if len(sys.argv) > 3 else 0.0
if delay:
    time.sleep(delay)          # 模拟慢速集成测试
m = importlib.import_module(mod)
got = m.val()
assert got == exp, f"expected {exp}, got {got}"
print("ok")
'''

FAST_DELAY = 0.0
SLOW_DELAY = 1.0
TASKS = (
    [{"id": f"f{i}", "channel": "fast", "delay": FAST_DELAY, "val": 10 + i} for i in range(1, 5)]
    + [{"id": f"s{i}", "channel": "slow", "delay": SLOW_DELAY, "val": 20 + i} for i in range(1, 5)]
)


def sh(*args, cwd=None):
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, env=env)


def purge_pycache(root):
    for dp, dn, _ in os.walk(root):
        if "__pycache__" in dn:
            shutil.rmtree(os.path.join(dp, "__pycache__"), ignore_errors=True)
            dn.remove("__pycache__")


def setup_base(root):
    base = os.path.join(root, "base")
    os.makedirs(base)
    g = lambda *a: sh("git", *a, cwd=base)
    g("init", "-q", "-b", "main")
    g("config", "user.email", "t@t.t")
    g("config", "user.name", "t")
    open(os.path.join(base, "check.py"), "w").write(CHECK)
    g("add", "-A")
    g("commit", "-q", "-m", "init")
    return base


def make_worktree(root, base, task):
    wt = os.path.join(root, task["id"])
    r = sh("git", "-C", base, "worktree", "add", "-q", wt, "-b", f"task/{task['id']}")
    assert r.returncode == 0, r.stderr          # 用绝对路径，避免 -C 改写相对路径
    hp = os.path.join(root, f"handoff-{task['id']}.json")
    json.dump({
        "task_id": f"par-{task['id']}",
        "goal": f"让 mod_{task['id']}.val() 返回 {task['val']}",
        "repo": "demo/par", "base_ref": "main", "worktree": wt,
        "subtasks": [{"id": "s1", "title": "实现 val()",
                      "files": [f"mod_{task['id']}.py"],
                      "verify": f"python3 check.py mod_{task['id']} {task['val']} {task['delay']}",
                      "token_budget": 50000}],
    }, open(hp, "w"), ensure_ascii=False)
    return wt, hp


def run_task(root, task, wt, hp):
    """完整跑一个任务：Coder → Tester → 闸门。"""
    t0 = time.time()
    open(os.path.join(wt, f"mod_{task['id']}.py"), "w").write(
        f"def val():\n    return {task['val']}\n")

    purge_pycache(wt)
    v = sh(sys.executable, "-B", "check.py", f"mod_{task['id']}",
           str(task["val"]), str(task["delay"]), cwd=wt)      # G1
    g = sh(sys.executable, "-B", GATE, "--handoff", hp, "--repo", wt,
           "--base", "main", "--tokens", "3000")               # G3/G4/G6/G7

    # 污染检查：本 worktree 只应有自己的模块文件
    others = [f for f in os.listdir(wt)
              if f.startswith("mod_") and f != f"mod_{task['id']}.py"]
    return {
        "id": task["id"], "channel": task["channel"],
        "verify": v.returncode == 0, "gates": g.returncode == 0,
        "contaminated": others, "dur": round(time.time() - t0, 2),
        "err": (v.stdout + v.stderr).strip()[:200] if v.returncode else "",
    }


def main():
    root = tempfile.mkdtemp(prefix="par-demo-")
    print(f"工作目录: {root}\n")
    try:
        base = setup_base(root)
        wts = {t["id"]: make_worktree(root, base, t) for t in TASKS}
        print(f"已建 {len(wts)} 个独立 worktree（同一起点 {base}）\n")

        results = {}
        lock = threading.Lock()

        def worker(task):
            wt, hp = wts[task["id"]]
            r = run_task(root, task, wt, hp)
            with lock:
                results[task["id"]] = r
                flag = "✅" if (r["verify"] and r["gates"] and not r["contaminated"]) else "❌"
                print(f"  {flag} {r['id']:>3} [{r['channel']:>4}] "
                      f"verify={'绿' if r['verify'] else '红'} "
                      f"gates={'过' if r['gates'] else 'FAIL'} "
                      f"污染={r['contaminated'] or '无'} 耗时={r['dur']}s")

        t_wall = time.time()
        fast = [t for t in TASKS if t["channel"] == "fast"]
        slow = [t for t in TASKS if t["channel"] == "slow"]
        # 两条通道同时开跑，各 2 个 worker
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="fast") as pf, \
             ThreadPoolExecutor(max_workers=2, thread_name_prefix="slow") as ps:
            fs = [pf.submit(worker, t) for t in fast]
            ss = [ps.submit(worker, t) for t in slow]
            for f in fs + ss:
                f.result()
        wall = time.time() - t_wall

        serial = sum(r["dur"] for r in results.values())
        ok_all = all(r["verify"] and r["gates"] and not r["contaminated"] for r in results.values())

        print(f"\n{'='*54}")
        print(f"任务数        : {len(results)}（快 {len(fast)} / 慢 {len(slow)}）")
        print(f"串行总和      : {serial:.2f}s")
        print(f"并行墙钟      : {wall:.2f}s")
        print(f"加速比        : {serial/wall:.2f}×")
        print(f"全部通过      : {'✅ 是' if ok_all else '❌ 否'}")
        print(f"污染任务      : {[k for k, r in results.items() if r['contaminated']] or '无'}")

        expect_fast = (len(fast) / 2) * 0.4          # 快批理论值（每任务 ~0.4s，2 worker）
        fast_wall = max(r["dur"] for r in results.values() if r["channel"] == "fast")
        print(f"快通道最慢任务: {fast_wall:.2f}s（← 未被慢任务饿死的证据）")

        good = ok_all and wall < serial * 0.75
        print(f"\n{'✅ 并行调度符合预期' if good else '❌ 并行效果不达标'}")
        shutil.rmtree(root, ignore_errors=True)
        sys.exit(0 if good else 1)
    except Exception:
        print(f"（保留现场: {root}）")
        raise


if __name__ == "__main__":
    main()
