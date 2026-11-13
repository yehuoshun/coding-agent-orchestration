# 状态机与失败处理

## 状态

| 状态 | 进入条件 | 退出条件 |
|---|---|---|
| pending | 任务入队 | Planner 开始 |
| planning | Planner 运行 | 产出 handoff.json |
| coding | Coder 运行 | 子任务提交 |
| testing | Tester 运行 | verify 通过/失败 |
| reviewing | Reviewer 运行 | pass/reject |
| done | 全部门过 | — |
| failed | 重试超限 / 红线 / 预算超 | 人工介入 |

## 回环
- `testing fail` → 回 `coding`（重试 +1）
- `reviewing reject` → 回 `coding`（重试 +1）
- 重试计数 > 3 → `failed`

## 失败证据
`failed` 时落盘：任务ID、最后状态、错误原文、diff、token 用量、重试次数。

## 监控指标
- 每任务 token 构成（输入/输出/缓存命中）
- 平均重试次数
- 失败率
- 单子任务平均耗时

异常特征（间隔规整、重复 prompt、空输出）→ 判定为空转，直接熔断。
