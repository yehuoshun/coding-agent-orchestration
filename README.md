# coding-agent-orchestration

编码 Agent 编排 SOP —— 把一个编码任务拆成 **Planner → Coder → Tester → Reviewer** 四条流水线，
每个角色只带一小段上下文干活，硬闸门兜底。

## 为什么要有这个

单个 Agent 硬扛一切，上下文会越滚越大：实测有人跑到 **16.8 万 token / 次、一天 1.7 万次请求**，
又贵又不可控，还分不清是"真干活"还是"空转刷量"。

反过来做：**分工 + 短上下文 + 硬闸门**。同样的活，成本能低一个数量级，失败还能定位。

## 核心三条

1. **一个任务 = 一条流水线，不是一个会话。** 会话是数据，流水线是流程。
2. **交接包不传聊天记录**，只传结构化 JSON（目标 / 文件清单 / 验收标准）。
3. **不能破的红线交给机制**（脚本、权限、超时），不靠提示词自觉。

## 四个角色

| 角色 | 干什么 | 输入 | 产出 |
|---|---|---|---|
| **Planner** | 读需求 + 目录树，拆子任务 | 需求、仓库结构 | `handoff.json` |
| **Coder** | 按计划改代码 | `handoff.json` + 相关文件 | diff + 变更说明 |
| **Tester** | 跑测试/构建/静态检查 | diff + 验收命令 | 通过/失败 + 错误原文 |
| **Reviewer** | 看 diff 判质量（换模型） | diff + 规范 | 通过/打回 + 理由 |

## 快速开始

```bash
# 1. 复制模板
cp templates/handoff.schema.json ../your-task/handoff.json

# 2. 让 Planner 填 handoff.json（只读目录树，不读全仓）

# 3. 按 references/ 逐个角色执行
#    references/planner.md → coder.md → tester.md → reviewer.md

# 4. 登记台账
cat templates/ledger.md >> ../your-task/ledger.md
```

## 调度器（把流程变成能自动跑的系统）

`scripts/dispatch.py` 读任务清单，自动建 worktree、派给 Agent 后端、跑闸门、熔断、出台账：

```bash
cp templates/dispatch.manifest.example.json tasks.json   # 改成你的仓库和任务
python3 scripts/dispatch.py --manifest tasks.json --out out/
# 产出：out/ledger.md（台账） + out/results.json（明细） + out/wt/（失败任务的现场）
```

能力：
- **双通道并发**：`channels.fast` / `channels.slow` 各自 worker 数
- **重试 + 熔断**：`max_retry` 次后 `failed`，证据写 `failed-<id>.json`
- **任务隔离**：每任务独立 git worktree（G7），成功后可选 `--clean` 回收
- **闸门全程**：每轮真跑 `verify`（G1）+ `scripts/gate.py`（G3/G4/G6/G7）
- **可插拔 Agent**：`mock`（自测/CI，零成本）或 `cmd`（codex / claude / 任意脚本，JSON 进 stdin、usage 出 stdout）

自测：`python3 tests/test_dispatch.py`（16 例）= 单任务过 / 回环重试 / 熔断 / 8任务并行零污染 / 台账。

## 闸门检查器（可执行）

`references/gates.md` 里的 G3/G4/G6/G7 已经写成代码，别再靠人眼比对：

```bash
python3 scripts/gate.py --handoff tasks/s1/handoff.json --repo . --base main --usage tasks/s1/usage.json
# 退出码 0=全过 / 1=有闸门失败；输出逐条 [G3] PASS/FAIL

python3 tests/test_gate.py   # 自测 12 例（每个闸门都有能变红的用例）
```

| 闸门 | 谁来管 |
|---|---|
| G1 测试绿 | CI / verify 命令 |
| G2 评审过 | Reviewer（换模型） |
| **G3 改动范围** | `scripts/gate.py` |
| **G4 不删/跳测试** | `scripts/gate.py` |
| G5 重试 ≤3 | 调度层计数器 |
| **G6 token 预算** | `scripts/gate.py --usage/--tokens` |
| **G7 独立 worktree** | `scripts/gate.py` |

## 目录

- `SKILL.md` —— 编排总纲（给 Agent 读的作业说明书）
- `references/` —— 四个角色各自的执行细则 + 闸门清单
- `templates/` —— 交接包 schema、台账模板
- `scripts/gate.py` —— 闸门检查器（G3/G4/G6/G7）
- `scripts/dispatch.py` —— 调度器（任务清单 → worktree → Agent → 闸门 → 台账）
- `tests/test_gate.py` —— 闸门自测
- `tests/test_dispatch.py` —— 调度器自测
- `examples/` —— 失败路径、并行调度演练
- `docs/state-machine.md` —— 状态机与失败处理

## 红线

- 测试不绿不算完成
- Reviewer 不过不允许合并
- 同一任务重试 > 3 次自动停，标 `failed` 留证据
- 每个子任务独立 git worktree，绝不共享工作目录
