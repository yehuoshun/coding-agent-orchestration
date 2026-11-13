# 失败路径演练（回环 + 熔断）

验证编排机制里两条最关键的路径，不依赖任何 LLM，纯脚本可跑：

- **recover**：Coder 第 1 轮写错 → verify 红 → 打回 → 第 2 轮改对 → `done`
- **circuit**：Coder 一直写不对 → 3 轮全红 → 触发熔断 → `failed` + 证据落盘 `failed-evidence.json`

每轮都真执行 `verify` 命令（G1）+ `scripts/gate.py`（G3/G4/G6/G7）。

```bash
python3 run.py
# 期望输出末尾：✅ 回环与熔断行为符合预期，退出码 0
```

## 顺带踩到的真坑（已写进 references/tester.md）

Python `.pyc` 失效判断用 **(源文件 mtime 秒， 字节数)**。
重试循环里新旧两版若**字节数相同**且写盘在**同一秒**，Python 会复用旧字节码 →
verify 永远红（或假绿）。所以 verify 必须跑在无缓存环境：清 `__pycache__` + `python3 -B`。
