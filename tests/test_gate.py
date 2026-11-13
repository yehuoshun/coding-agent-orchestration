#!/usr/bin/env python3
"""gate.py 的自测：每个闸门都要有"能变红"的用例（否则闸门等于没写）。"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
GATE = os.path.join(os.path.dirname(HERE), "scripts", "gate.py")
PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}  {detail}")


def mkhandoff(d, files, budget=10000):
    p = os.path.join(d, "handoff.json")
    json.dump({
        "task_id": "t", "goal": "g", "repo": "r", "base_ref": "main",
        "subtasks": [{"id": "s1", "title": "t", "files": files,
                      "verify": "true", "token_budget": budget}],
    }, open(p, "w"))
    return p


def run(handoff, repo=None, diff=None, tokens=None):
    cmd = [sys.executable, GATE, "--handoff", handoff]
    if repo:
        cmd += ["--repo", repo]
    if diff:
        cmd += ["--diff-file", diff]
    if tokens is not None:
        cmd += ["--tokens", str(tokens)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    return r.returncode, r.stdout + r.stderr


def diff_for(*paths, body=""):
    """构造一个最小 unified diff，路径即改动文件。"""
    return "".join(f"diff --git a/{p} b/{p}\n--- a/{p}\n+++ b/{p}\n{body}" for p in paths)


def main():
    tmp = tempfile.mkdtemp(prefix="gate-test-")
    try:
        # ── G3 ────────────────────────────────
        print("[1] G3 改动范围")
        d = tempfile.mkdtemp(dir=tmp)
        h = mkhandoff(d, ["a.py"])
        f = os.path.join(d, "in.diff")
        open(f, "w").write(diff_for("a.py"))
        _, out = run(h, diff=f)
        check("G3 越界判定 PASS", "[G3] PASS" in out, out)

        open(f, "w").write(diff_for("a.py", "b.py"))
        rc, out = run(h, diff=f)
        check("G3 多改文件 → FAIL", "[G3] FAIL" in out and rc == 1, out)

        # ── G4 ────────────────────────────────
        print("[2] G4 测试完整性")
        open(f, "w").write(diff_for("a.py", body="-def test_login():\n+def test_login2():\n"))
        _, out = run(h, diff=f)
        check("G4 删除 def test_ → FAIL", "[G4] FAIL" in out, out)

        open(f, "w").write(diff_for("a.py", body="+@pytest.mark.skip(reason='x')\n"))
        _, out = run(h, diff=f)
        check("G4 新增 skip → FAIL", "[G4] FAIL" in out, out)

        open(f, "w").write(diff_for("a.py", body="+def test_new():\n+    assert 1\n"))
        _, out = run(h, diff=f)
        check("G4 正常加测试 → PASS", "[G4] PASS" in out, out)

        # ── G6 ────────────────────────────────
        print("[3] G6 token 预算")
        open(f, "w").write(diff_for("a.py"))
        _, out = run(h, diff=f, tokens=99999)
        check("G6 超预算 → FAIL", "[G6] FAIL" in out, out)
        _, out = run(h, diff=f, tokens=5000)
        check("G6 未超 → PASS", "[G6] PASS" in out, out)
        _, out = run(h, diff=f)
        check("G6 不传用量 → SKIP（提示）", "[G6] SKIP" in out, out)

        # ── G7 ────────────────────────────────
        print("[4] G7 独立 worktree")
        plain = tempfile.mkdtemp(dir=tmp)          # 没有 .git → FAIL
        _, out = run(h, repo=plain, diff=f)
        check("G7 普通目录 → FAIL", "[G7] FAIL" in out, out)

        fake = tempfile.mkdtemp(dir=tmp)           # .git 是文件 → PASS
        open(os.path.join(fake, ".git"), "w").write("gitdir: /tmp/x\n")
        _, out = run(h, repo=fake, diff=f)
        check("G7 worktree 形态 → PASS", "[G7] PASS" in out, out)

        # ── 端到端：真 worktree + 真 git diff ──
        print("[5] 端到端（真 worktree + git diff）")
        base = os.path.join(tmp, "base")
        os.makedirs(base)
        g = lambda *a, cwd=base: subprocess.run(["git", *a], cwd=cwd,
                                                capture_output=True, text=True)
        g("init", "-q", "-b", "main")
        g("config", "user.email", "t@t.t")
        g("config", "user.name", "t")
        open(os.path.join(base, "a.py"), "w").write("x = 1\n")
        g("add", "-A")
        g("commit", "-q", "-m", "init")
        wt = os.path.join(tmp, "wt")
        g("worktree", "add", "-q", wt, "-b", "task")

        open(os.path.join(wt, "a.py"), "w").write("x = 2\n")
        h2 = mkhandoff(tmp, ["a.py"])
        rc, out = run(h2, repo=wt, tokens=1000)
        check("端到端 全闸门通过 rc=0", rc == 0 and "ALL GATES PASS" in out, out)

        open(os.path.join(wt, "z.py"), "w").write("y = 1\n")
        subprocess.run(["git", "add", "-A"], cwd=wt, capture_output=True)
        rc, out = run(h2, repo=wt, tokens=1000)
        check("端到端 越界 → rc=1 且点名 G3", rc == 1 and "GATE FAILED: G3" in out, out)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n===== 结果: {PASS} 通过 / {FAIL} 失败 =====")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
