#!/usr/bin/env python3
"""dispatch.py 自测：成功 / 回环重试 / 熔断 / 并行 / 台账 五件事都要能验。"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
DISPATCH = os.path.join(os.path.dirname(HERE), "scripts", "dispatch.py")
PASS = FAIL = 0

CHECK = '''import importlib, sys, time
mod, exp = sys.argv[1], int(sys.argv[2])
delay = float(sys.argv[3]) if len(sys.argv) > 3 else 0.0
if delay:
    time.sleep(delay)
m = importlib.import_module(mod)
assert m.val() == exp, f"expected {exp}, got {m.val()}"
print("ok")
'''


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}  {detail}")


def make_repo(root):
    repo = os.path.join(root, "repo")
    os.makedirs(repo)
    g = lambda *a: subprocess.run(["git", *a], cwd=repo, capture_output=True, text=True)
    g("init", "-q", "-b", "main")
    g("config", "user.email", "t@t.t")
    g("config", "user.name", "t")
    open(os.path.join(repo, "check.py"), "w").write(CHECK)
    g("add", "-A")
    g("commit", "-q", "-m", "init")
    return repo


def task(tid, val, channel="fast", fail_times=0, delay=0.0):
    return {
        "id": tid, "channel": channel, "goal": f"val() 返回 {val}",
        "files": [f"mod_{tid}.py"],
        "verify": f"python3 check.py mod_{tid} {val} {delay}",
        "token_budget": 60000, "verify_timeout": 60,
        "mock": {"fail_times": fail_times,
                 "writes": [{"path": f"mod_{tid}.py", "content": f"def val():\n    return {val}\n"}]},
    }


def run(repo, tasks, root, name, extra=None):
    m = {"repo": repo, "base": "main", "max_retry": 3,
         "agent": {"backend": "mock"}, "channels": {"fast": 2, "slow": 2}, "tasks": tasks}
    m.update(extra or {})
    mp = os.path.join(root, f"manifest-{name}.json")
    json.dump(m, open(mp, "w"), ensure_ascii=False)
    out = os.path.join(root, f"out-{name}")
    r = subprocess.run([sys.executable, DISPATCH, "--manifest", mp, "--out", out],
                       capture_output=True, text=True, timeout=300)
    res = json.load(open(os.path.join(out, "results.json"), encoding="utf-8")) \
        if os.path.exists(os.path.join(out, "results.json")) else {}
    return r, res, out


def main():
    root = tempfile.mkdtemp(prefix="dispatch-test-")
    try:
        repo = make_repo(root)

        print("[1] 单任务一次过")
        _, res, _ = run(repo, [task("a1", 11)], root, "single")
        r = res.get("a1", {})
        check("status=done", r.get("status") == "done", str(r.get("status")))
        check("attempts=1", r.get("attempts") == 1, str(r.get("attempts")))
        check("verify 绿", r.get("verify") == [True], str(r.get("verify")))
        check("闸门过", r.get("gates") == [True], str(r.get("gates")))

        print("[2] 回环：第 1 轮失败、第 2 轮成功")
        _, res, _ = run(repo, [task("b1", 22, fail_times=1)], root, "retry")
        r = res.get("b1", {})
        check("status=done", r.get("status") == "done", str(r.get("status")))
        check("attempts=2", r.get("attempts") == 2, str(r.get("attempts")))
        check("verify 先红后绿", r.get("verify") == [False, True], str(r.get("verify")))

        print("[3] 熔断：一直失败 → 3 轮后 failed + 证据")
        r3, res, out = run(repo, [task("c1", 33, fail_times=99)], root, "circuit")
        r = res.get("c1", {})
        check("status=failed", r.get("status") == "failed", str(r.get("status")))
        check("attempts=3", r.get("attempts") == 3, str(r.get("attempts")))
        check("退出码=1", r3.returncode == 1, str(r3.returncode))
        check("证据落盘", os.path.exists(os.path.join(out, "wt", "failed-c1.json")))

        print("[4] 并行：4 快 4 慢，全部完成且零污染")
        tasks = ([task(f"f{i}", 40 + i) for i in range(1, 5)]
                 + [task(f"s{i}", 50 + i, channel="slow", delay=0.6) for i in range(1, 5)])
        _, res, out = run(repo, tasks, root, "parallel")
        check("8 个全 done", all(v["status"] == "done" for v in res.values()),
              str({k: v["status"] for k, v in res.items()}))
        wt_root = os.path.join(out, "wt")
        clean = True
        for tid in res:
            d = os.path.join(wt_root, tid)
            if not os.path.isdir(d):
                clean = False
                print(f"      worktree 不存在: {tid}")
                continue
            others = [f for f in os.listdir(d) if f.startswith("mod_") and f != f"mod_{tid}.py"]
            if others:
                clean = False
                print(f"      污染 {tid}: {others}")
        check("零污染（每个 worktree 只有自己的文件）", clean)

        print("[5] 台账")
        led = open(os.path.join(out, "ledger.md"), encoding="utf-8").read()
        check("ledger.md 有表头", "| 任务 | 通道 |" in led, led[:200])
        check("8 行任务记录", led.count("\n| f") + led.count("\n| s") >= 8, led)
        check("results.json 可解析", len(res) == 8, str(len(res)))
    finally:
        shutil.rmtree(root, ignore_errors=True)

    print(f"\n===== 结果: {PASS} 通过 / {FAIL} 失败 =====")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
