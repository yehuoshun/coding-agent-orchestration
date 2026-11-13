#!/usr/bin/env python3
"""dispatch.py —— 编码 Agent 编排调度器。

把 SKILL.md 里的流程变成能自动跑的东西：
  读任务清单 → 建独立 worktree → 派给 Agent 后端 → verify(G1) + gate.py(G3/G4/G6/G7)
  → 失败打回重试 → 超限熔断 → 出 ledger.md / results.json

Agent 后端可插拔：
  mock  —— 内置，按任务里的 mock 脚本改文件（CI / 自测用，零依赖零成本）
  cmd   —— 跑外部 Agent CLI（codex / claude / 任意脚本），JSON 走 stdin，usage 走 stdout

用法：
  python3 scripts/dispatch.py --manifest tasks.json --out out/
  python3 scripts/dispatch.py --manifest tasks.json --out out/ --clean

退出码：0 = 全部 done；1 = 有任务 failed；2 = 输入/环境错误。
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
GATE = os.path.join(HERE, "gate.py")
PY = sys.executable

# 实测坑：多个任务并发 `git worktree add` 会撞 .git 锁（index/worktrees 目录），
# 表现为 fatal: could not lock ... / Unable to create ... ，任务直接 error。
# 结论：worktree 创建必须串行化（便宜操作，不拖慢流水线）。
_WT_LOCK = threading.Lock()


def add_worktree(repo, wt, branch, base, retries=3):
    last = None
    with _WT_LOCK:
        for i in range(retries):
            r = sh(["git", "-C", repo, "worktree", "add", "-q", "-B", branch, wt, base])
            if r.returncode == 0:
                return None
            last = (r.stderr or r.stdout or "").strip()
            time.sleep(0.3 * (i + 1))
        return last


def sh(args, cwd=None, timeout=None, stdin_data=None):
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        return subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                              timeout=timeout, env=env, input=stdin_data)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(args, 124, "", f"timeout after {timeout}s")


def purge_pycache(root):
    for dp, dn, _ in os.walk(root):
        if "__pycache__" in dn:
            shutil.rmtree(os.path.join(dp, "__pycache__"), ignore_errors=True)
            dn.remove("__pycache__")


# ────────────────────────── Agent 后端 ──────────────────────────
def agent_mock(task, wt, attempt, last_error):
    """内置 mock：按 fail_times 故意失败，之后写入正确内容。"""
    m = task.get("mock", {})
    if attempt <= int(m.get("fail_times", 0)):
        return {"tokens": {"input": 900, "output": 150},
                "error": f"mock 第 {attempt} 次故意写错"}
    for w in m.get("writes", []):
        p = os.path.join(wt, w["path"])
        os.makedirs(os.path.dirname(p) or wt, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(w["content"])
    return {"tokens": {"input": 1200, "output": 300}}


def agent_cmd(task, wt, attempt, last_error):
    """外部 Agent：stdin 收 JSON 交接包，在 worktree 里改文件，stdout 回报 usage。"""
    payload = json.dumps({
        "task_id": task["id"], "goal": task.get("goal", ""), "prompt": task.get("prompt", ""),
        "worktree": wt, "attempt": attempt, "allowed_files": task["files"],
        "verify": task["verify"], "last_error": last_error,
    }, ensure_ascii=False)
    r = sh(["bash", "-lc", task["agent_cmd"]], cwd=wt, stdin_data=payload,
           timeout=task.get("agent_timeout", 900))
    out = {}
    for line in reversed((r.stdout or "").strip().splitlines()):
        try:
            out = json.loads(line)
            break
        except Exception:
            continue
    out.setdefault("tokens", {})
    if r.returncode != 0 and not out.get("error"):
        out["error"] = (r.stderr or f"agent exited {r.returncode}")[:500]
    return out


BACKENDS = {"mock": agent_mock, "cmd": agent_cmd}


# ────────────────────────── 单任务流水线 ──────────────────────────
def run_task(task, ctx, results, lock):
    tid = task["id"]
    repo, base, wt_root = ctx["repo"], ctx["base"], ctx["wt_root"]
    wt = os.path.join(wt_root, tid)
    max_retry = int(ctx.get("max_retry", 3))
    backend = BACKENDS[ctx.get("agent", {}).get("backend", "mock")]
    t0 = time.time()
    rec = {"id": tid, "channel": task.get("channel", "fast"), "attempts": 0,
           "verify": [], "gates": [], "tokens": 0, "status": "pending", "worktree": wt}

    r = add_worktree(repo, wt, f"dispatch/{tid}", base)
    if r is not None:
        rec.update(status="error", error=r[:400])
        with lock:
            results[tid] = rec
        return rec

    hp = os.path.join(wt_root, f"handoff-{tid}.json")
    with open(hp, "w", encoding="utf-8") as f:
        json.dump({"task_id": tid, "goal": task.get("goal", ""), "repo": repo,
                   "base_ref": base, "worktree": wt,
                   "subtasks": [{"id": "s1", "title": task.get("goal", tid),
                                 "files": task["files"], "verify": task["verify"],
                                 "token_budget": task.get("token_budget", 60000)}]},
                  f, ensure_ascii=False)

    last_error = None
    for attempt in range(1, max_retry + 1):
        rec["attempts"] = attempt
        out = backend(task, wt, attempt, last_error)
        rec["tokens"] += sum(int(v or 0) for v in out.get("tokens", {}).values())

        purge_pycache(wt)
        sh(["git", "-C", wt, "add", "-A"])                      # 让新文件进 diff，G3 才看得见
        v = sh(["bash", "-lc", task["verify"]], cwd=wt, timeout=task.get("verify_timeout", 600))
        g = sh([PY, "-B", GATE, "--handoff", hp, "--repo", wt,
                "--base", base, "--tokens", str(rec["tokens"])], timeout=120)

        ok_v, ok_g = v.returncode == 0, g.returncode == 0
        rec["verify"].append(ok_v)
        rec["gates"].append(ok_g)
        if ok_v and ok_g:
            rec["status"] = "done"
            break
        last_error = ((v.stdout or "") + (v.stderr or "")).strip()[:800] or (g.stdout or "")[:800]
        if not ok_v:
            rec["last_verify_error"] = last_error
        else:
            rec["last_gate_error"] = (g.stdout or "")[:800]
        if out.get("error"):
            rec["agent_error"] = out["error"]

    if rec["status"] != "done":
        rec["status"] = "failed"
        ev = os.path.join(wt_root, f"failed-{tid}.json")
        with open(ev, "w", encoding="utf-8") as f:
            json.dump({"task": tid, "retries": rec["attempts"], "record": rec},
                      f, ensure_ascii=False, indent=2)
        rec["evidence"] = ev

    rec["duration"] = round(time.time() - t0, 2)
    if ctx.get("clean") and rec["status"] == "done":
        sh(["git", "-C", repo, "worktree", "remove", "--force", wt])
    with lock:
        results[tid] = rec
    return rec


# ────────────────────────── 调度 ──────────────────────────
def write_ledger(out_dir, results, manifest):
    os.makedirs(out_dir, exist_ok=True)
    order = [t["id"] for t in manifest["tasks"]]
    rows = ["| 任务 | 通道 | 轮次 | verify | 闸门 | token | 耗时 | 状态 |",
            "|---|---|---|---|---|---|---|---|"]
    for tid in order:
        r = results[tid]
        v = "".join("绿" if x else "红" for x in r["verify"]) or "-"
        g = "".join("过" if x else "×" for x in r["gates"]) or "-"
        icon = "✅" if r["status"] == "done" else "❌"
        rows.append(f"| {tid} | {r['channel']} | {r['attempts']} | {v} | {g} | "
                    f"{r['tokens']} | {r.get('duration', 0)}s | {icon} {r['status']} |")
    done = sum(1 for r in results.values() if r["status"] == "done")
    head = [f"# 调度台账 {time.strftime('%Y-%m-%d %H:%M:%S')}", "",
            f"- 任务总数 **{len(results)}** ｜ 完成 **{done}** ｜ 失败 **{len(results)-done}**",
            f"- 总 token **{sum(r['tokens'] for r in results.values())}**",
            f"- 仓库 `{manifest['repo']}` @ `{manifest['base']}`", ""]
    with open(os.path.join(out_dir, "ledger.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(head + rows) + "\n")
    with open(os.path.join(out_dir, "results.json"), "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--out", default="dispatch-out")
    ap.add_argument("--clean", action="store_true", help="成功的任务跑完删掉 worktree")
    ap.add_argument("--only", help="只跑指定任务 id（逗号分隔）")
    args = ap.parse_args()

    manifest = json.load(open(args.manifest, encoding="utf-8"))
    repo = os.path.abspath(manifest["repo"])
    if not os.path.isdir(os.path.join(repo, ".git")) and not os.path.isfile(os.path.join(repo, ".git")):
        print(f"[dispatch] 不是 git 仓库: {repo}", file=sys.stderr)
        sys.exit(2)

    wt_root = os.path.abspath(manifest.get("worktree_root") or os.path.join(args.out, "wt"))
    os.makedirs(wt_root, exist_ok=True)
    tasks = manifest["tasks"]
    if args.only:
        want = {x.strip() for x in args.only.split(",")}
        tasks = [t for t in tasks if t["id"] in want]

    ctx = {"repo": repo, "base": manifest.get("base", "main"), "wt_root": wt_root,
           "max_retry": manifest.get("max_retry", 3), "agent": manifest.get("agent", {}),
           "clean": args.clean}
    chan = manifest.get("channels", {})
    results, lock = {}, threading.Lock()
    print(f"[dispatch] {len(tasks)} 个任务 ｜ 通道并发 fast={chan.get('fast',2)} slow={chan.get('slow',2)}")

    groups = {}
    for t in tasks:
        groups.setdefault(t.get("channel", "fast"), []).append(t)

    def run_group(name, group):
        with ThreadPoolExecutor(max_workers=int(chan.get(name, 2)),
                                thread_name_prefix=name) as pool:
            for f in [pool.submit(run_task, t, ctx, results, lock) for t in group]:
                f.result()

    threads = [threading.Thread(target=run_group, args=(n, g)) for n, g in groups.items()]
    t0 = time.time()
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    wall = round(time.time() - t0, 2)

    write_ledger(args.out, results, {**manifest, "tasks": tasks})
    done = sum(1 for r in results.values() if r["status"] == "done")
    for tid, r in results.items():
        print(f"  {'✅' if r['status']=='done' else '❌'} {tid:>4} [{r['channel']:>4}] "
              f"轮次={r['attempts']} token={r['tokens']} {r.get('duration',0)}s {r['status']}")
    print(f"\n[dispatch] 完成 {done}/{len(results)} ｜ 墙钟 {wall}s ｜ 台账 {args.out}/ledger.md")
    sys.exit(0 if done == len(results) else 1)


if __name__ == "__main__":
    main()
