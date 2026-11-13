---
name: coding-agent-orchestration
description: 编排编码 Agent 的四段流水线（Planner/Coder/Tester/Reviewer），用短上下文与硬闸门跑无人值守的编码任务。当用户要求「跑编码 agent」「挂机改代码」「多 agent 编排」「自动修复/补测试/重构」时使用。
---

# 编码 Agent 编排 SOP

## 一、总则

1. **一个任务 = 一条流水线**。不要用一个长会话硬扛。
2. **每个角色只拿它需要的上下文**。Planner 读结构，Coder 读相关文件，Tester 只读命令输出。
3. **硬约束写进机制**：能写成 `if 失败 then 停` 的，绝不写成「请注意不要…」。
4. **每条产出必须可验收**：跑得起来、测试绿、diff 可读。

## 二、状态机

```
pending → planning → coding → testing → reviewing → done
                ↑        │         │          │
                └────────┴─────────┴──────────┘ (打回，最多 3 轮)
                                          ↓
                                       failed
```

- 同一任务 **重试上限 3 轮**，超了直接 `failed`，把失败证据落盘。
- `failed` 任务不自动重跑，等人工介入。

## 三、角色分工

### Planner
- 输入：需求原文 + `tree -L 2` + 关键文件摘要
- 禁止：读全仓、改代码
- 产出：`handoff.json`（见 `templates/handoff.schema.json`）
- 要求：子任务必须**可独立验收**，每个带验收命令

### Coder
- 输入：`handoff.json` 的**单个子任务** + 该子任务声明的文件
- 禁止：越界改未声明的文件、动 CI 配置、删测试
- 产出：diff + 一句话变更说明
- 每完成一个子任务就提交一次（原子提交）

### Tester
- 输入：diff + `handoff.json` 里的 `verify` 命令
- 只执行、不修改代码
- 失败时把**错误原文**（不是总结）回给 Coder

### Reviewer
- 输入：diff + 项目规范
- **必须换一个模型**（避免自己夸自己）
- 输出：`pass` / `reject` + 具体理由（指行号）

## 四、上下文策略（省钱的关键）

| 角色 | 上下文里应该有什么 | 不应该有什么 |
|---|---|---|
| Planner | 目录树、README、入口文件、依赖清单 | 全仓源码 |
| Coder | 目标文件 + 直接依赖 + 相关类型定义 | 无关模块、历史对话 |
| Tester | 命令 + 输出 | 源码（除非要定位） |
| Reviewer | diff + 规范 | 整个仓库 |

**固定前缀**（规范、目录约定、常用命令）放进 system prompt 或项目 `AGENTS.md`，
保持不变 → 吃满缓存命中（DeepSeek 缓存价约为未命中的 1/50）。

## 五、闸门（硬约束，见 references/gates.md）

- G1 测试/构建必须绿
- G2 Reviewer 必须 pass
- G3 改动范围不得超出 handoff 声明
- G4 不得删除或跳过测试
- G5 单任务重试 ≤ 3
- G6 单任务 token 预算上限
- G7 工作目录必须是独立 worktree

## 六、台账

每个任务一行，格式见 `templates/ledger.md`。没有台账的任务视为未完成。

## 七、落地顺序

1. 串行跑通一个任务（Planner→Coder→Tester）
2. 加测试失败回环（≤3 轮）
3. 加 Reviewer（换模型）
4. 接任务队列做多任务并行
5. 加监控（token 构成、失败率、熔断）
