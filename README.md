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

## 目录

- `SKILL.md` —— 编排总纲（给 Agent 读的作业说明书）
- `references/` —— 四个角色各自的执行细则 + 闸门清单
- `templates/` —— 交接包 schema、台账模板
- `docs/state-machine.md` —— 状态机与失败处理

## 红线

- 测试不绿不算完成
- Reviewer 不过不允许合并
- 同一任务重试 > 3 次自动停，标 `failed` 留证据
- 每个子任务独立 git worktree，绝不共享工作目录
