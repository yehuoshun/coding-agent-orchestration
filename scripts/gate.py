#!/usr/bin/env python3
"""编码 Agent 编排闸门检查器（把 references/gates.md 里"纸上"的闸门写成代码）。

实现的闸门：
  G3  改动范围不得超出 handoff 声明
  G4  不得删除或跳过测试
  G6  单任务 token 预算（需 --usage 或 --tokens）
  G7  必须跑在独立 git worktree 里

用法：
  # 在任务 worktree 里、基于 main 比对
  python3 scripts/gate.py --handoff tasks/s1/handoff.json --repo . --base main --usage tasks/s1/usage.json

  # 或直接喂 diff 文件（不依赖 git）
  python3 scripts/gate.py --handoff handoff.json --diff-file review.diff --tokens 42000

退出码：0 = 全过；1 = 有闸门失败；2 = 用法/输入错误。
"""
import argparse
import json
import os
import re
import subprocess
import sys

DEF_TST = re.compile(r"^-\s*def\s+test_")
SKIP = re.compile(r"^\+.*(pytest\.mark\.skip|unittest\.skip|@skip|# *skip)", re.I)


def sh(args, cwd=None):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True)


def load_handoff(path):
    with open(path, encoding="utf-8") as f:
        h = json.load(f)
    allowed = set()
    budget = 0
    for st in h.get("subtasks", []):
        allowed.update(st.get("files", []))
        budget += int(st.get("token_budget") or 0)
    if h.get("token_budget"):
        budget = int(h["token_budget"])
    return h, allowed, budget


def get_diff(args):
    """返回 (changed_files:set, diff_text:str)。"""
    if args.diff_file:
        diff = open(args.diff_file, encoding="utf-8", errors="ignore").read()
        files = set(re.findall(r"^diff --git a/(\S+) b/", diff, re.M))
        return files, diff
    r = sh(["git", "-C", args.repo, "diff", "--name-only", args.base])
    if r.returncode != 0:
        print(f"[gate] git diff 失败: {r.stderr.strip()}", file=sys.stderr)
        sys.exit(2)
    files = {x for x in r.stdout.splitlines() if x.strip()}
    d = sh(["git", "-C", args.repo, "diff", args.base])
    return files, d.stdout


def get_tokens(args):
    if args.tokens is not None:
        return int(args.tokens)
    if args.usage:
        u = json.load(open(args.usage, encoding="utf-8"))
        if "total" in u:
            return int(u["total"])
        return sum(int(u.get(k) or 0) for k in ("input", "output", "cache_read", "cache_write"))
    return None


def is_worktree(repo):
    return os.path.isfile(os.path.join(repo, ".git"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--handoff", required=True)
    ap.add_argument("--repo", default=".")
    ap.add_argument("--base", default="HEAD")
    ap.add_argument("--diff-file")
    ap.add_argument("--usage")
    ap.add_argument("--tokens", type=int)
    args = ap.parse_args()

    if not os.path.exists(args.handoff):
        print(f"[gate] handoff 不存在: {args.handoff}", file=sys.stderr)
        sys.exit(2)

    h, allowed, budget = load_handoff(args.handoff)
    changed, diff = get_diff(args)

    fails = []

    # ── G3 改动范围 ──────────────────────────────
    extra = sorted(changed - allowed)
    if extra:
        fails.append("G3")
        print(f"[G3] FAIL 超出声明范围: {extra}")
    else:
        print(f"[G3] PASS 改动未越界（{len(changed)} 文件 ⊆ 声明 {len(allowed)}）")

    # ── G4 测试完整性 ────────────────────────────
    removed_tests = [l for l in diff.splitlines() if DEF_TST.match(l)]
    skipped = [l for l in diff.splitlines() if SKIP.match(l)]
    if removed_tests or skipped:
        fails.append("G4")
        print(f"[G4] FAIL 删除测试 {len(removed_tests)} 处 / 新增跳过 {len(skipped)} 处")
        for l in (removed_tests + skipped)[:5]:
            print(f"       {l[:100]}")
    else:
        print("[G4] PASS 没有删除或跳过测试")

    # ── G6 token 预算 ────────────────────────────
    used = get_tokens(args)
    if used is None:
        print(f"[G6] SKIP 未提供用量（预算 {budget}）——建议总是传 --usage 或 --tokens")
    elif budget and used > budget:
        fails.append("G6")
        print(f"[G6] FAIL 用量 {used} > 预算 {budget}")
    else:
        print(f"[G6] PASS 用量 {used} ≤ 预算 {budget or '∞'}")

    # ── G7 独立 worktree ─────────────────────────
    if is_worktree(args.repo):
        print("[G7] PASS 跑在独立 worktree")
    else:
        fails.append("G7")
        print(f"[G7] FAIL {args.repo} 不是独立 worktree（.git 应为文件）")

    print()
    if fails:
        print(f"GATE FAILED: {','.join(fails)}")
        sys.exit(1)
    print("ALL GATES PASS")
    sys.exit(0)


if __name__ == "__main__":
    main()
