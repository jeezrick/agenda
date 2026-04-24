# Agenda 核心设计文档

> 版本: v0.0.6  
> 代码量: 2436 行  
> 设计哲学: **文件即状态，目录即 Session，DAG 即编排，Guardian 即边界。**

---

## 目录

1. [概述](#概述)
2. [文件即状态](#文件即状态)
3. [目录即 Session](#目录即-session)
4. [DAG 即编排](#dag-即编排)
5. [Guardian 即边界](#guardian-即边界)
6. [四者如何协同工作](#四者如何协同工作)

---

## 概述

Agenda 是一个给 **Meta Agent 调度子 Agent** 的运行时。它不是给人类用的交互工具，而是一个基础设施——Meta Agent 写一个 DAG，Agenda 自动调度执行，每个节点是一个独立 Agent，可以创建子 Agent，可以读写文件，可以中断后恢复。

关键设计约束：
- **无数据库**: 所有状态在文件系统
- **无 socket/网络服务**: 进程间通信靠文件
- **无 UI**: 输出是 JSON，输入是 YAML/JSON
- **单 asyncio 事件循环**: 不依赖多进程/多线程

这四个设计哲学不是独立存在的，而是互相支撑的一个整体。

---

## 文件即状态

### 设计思路

传统应用用数据库保存状态，Agenda 用文件。原因：
1. **可观察**: 人类或 Agent 随时 `cat` 文件就知道状态
2. **可恢复**: 进程崩溃后重启，读文件即可重建状态
3. **无依赖**: 不需要安装 PostgreSQL/MongoDB
4. **版本友好**: 文件可以用 git 管理

核心原则：**append-only JSONL**。所有运行时日志都是追加写，不覆盖已有数据。这样即使进程在中途崩溃，已写入的数据不会丢失。

### 状态文件清单

| 文件 | 位置 | 格式 | 用途 |
|------|------|------|------|
| `turns.jsonl` | `.system/turns.jsonl` | JSONL | 对话历史（turn 级持久化） |
| `events.jsonl` | `.system/events.jsonl` | JSONL | IPC 事件队列 |
| `state.json` | `.system/state.json` | JSON | 节点运行状态 |
| `session.jsonl` | `.system/session.jsonl` | JSONL | 运行时日志 |
| `scheduler_state.json` | `.system/scheduler_state.json` | JSON | 调度器运行状态 |
| `draft.md` | `output/draft.md` | Markdown | 节点完成标记 |
| `error.log` | `.system/error.log` | Text | 节点失败标记 |

### 算法: Turn 级持久化

**为什么 turn 级？**

在 Butterfly 中，session 的持久化是逐条消息写入 `context.jsonl`。Agenda 更进一步，每轮 LLM 运行（一个 iteration）打包成一个 turn 追加到 `turns.jsonl`。

Turn 格式：
```json
{"type": "turn", "messages": [...], "iteration": 5, "ts": "2026-04-23T23:00:00"}
```

**写入时机**（AgentLoop.run 中）：
1. 每轮迭代结束时：`save_turn()` 追加完整 turn
2. 取消/中断时：`save_partial_turn()` 追加已 committed 的部分 turn，标记 `interrupted: true`
3. 记忆压缩后：`save_turn()` 追加压缩后的 turn，标记 `compact: true`

**恢复算法**（`replay_history()`）：
```
输入: turns.jsonl
输出: flat messages 列表

1. 逐行读取 turns.jsonl
2. 每行的 messages 按顺序追加到结果列表
3. 返回结果列表

复杂度: O(n)，n = 总消息数
```

### 实现

`Session.save_turn()`:
```python
def save_turn(self, turn: dict) -> None:
    with open(self._turns_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(turn, ensure_ascii=False) + "\n")
```

`Session.save_partial_turn()`（Butterfly v2.0.34 式修复）:
```python
def save_partial_turn(self, messages, iteration, interrupted=True):
    turn = {
        "type": "turn",
        "messages": list(messages),
        "iteration": iteration,
        "interrupted": interrupted,
        "ts": datetime.now().isoformat(),
    }
    self.save_turn(turn)
```

`Session.replay_history()`:
```python
def replay_history(self) -> list[dict]:
    messages = []
    for turn in self.load_turns():
        for msg in turn.get("messages", []):
            messages.append(msg)
    return messages
```

### 实际流程

**场景: Agent 运行到第 5 轮时被 Ctrl+C 中断**

1. 第 1-4 轮正常结束，`turns.jsonl` 已有 4 条 turn
2. 第 5 轮 LLM 返回了 tool_calls，但工具还没执行完
3. Ctrl+C 触发 `asyncio.CancelledError`
4. AgentLoop 调用 `_seal_orphan_tool_calls()`：为未完成的 tool_calls 补全 synthetic tool_result
5. AgentLoop 调用 `save_partial_turn()`：把第 5 轮已 committed 的消息追加到 `turns.jsonl`，标记 `interrupted: true`
6. 进程退出

**恢复**:
7. 重新运行 `agenda dag run`
8. Scheduler 加载 `scheduler_state.json`，知道哪些节点已完成
9. 该节点未在 completed 中，进入 `ready_nodes()`
10. `prepare_node()` 调用 `replay_history()`，从 `turns.jsonl` 恢复 5 轮消息
11. AgentLoop 从第 5 轮中断处继续（system prompt 更新，其余从 turns 恢复）

---

## 目录即 Session

### 设计思路

每个 Agent 的运行环境是一个目录。目录结构即运行时环境：
- `.context/` — Agent 可见的输入/工作区
- `.system/` — 系统私有（Agent 不可见）
- `output/` — Agent 产物
- `children/` — 子 Agent 的 Session

这是从 Butterfly 学来的**双目录隔离**。核心洞察：
1. Agent 和系统共享一个物理目录，但通过路径约定划分可见性
2. 子 Agent 不是线程/进程，而是嵌套目录
3. 目录层级即嵌套深度（`node_dir/children/child_name/`）

### 目录结构

```
nodes/{node_id}/                    ← 一个 Session = 一个目录
├── .context/                       ← Agent 可读/写
│   ├── hints.md                    ← 系统注入的提示
│   ├── input.md                    ← 外部输入
│   └── deps/                       ← 上游产物（通过 dep_inputs 复制）
├── .system/                        ← 系统私有（Agent 不可见）
│   ├── turns.jsonl                 ← 对话历史
│   ├── events.jsonl                ← IPC 事件队列
│   ├── state.json                  ← 运行状态
│   └── session.jsonl               ← 运行时日志
├── output/                         ← Agent 产物
│   └── draft.md                    ← 完成标记
└── children/                       ← 子 Agent Session
    └── {child_name}/
        ├── .context/
        ├── .system/
        ├── output/
        └── children/
```

### 实现

`Session.__init__()`:
```python
def __init__(self, node_dir: Path) -> None:
    self.node_dir = Path(node_dir).resolve()
    self.context_dir = self.node_dir / ".context"
    self.system_dir = self.node_dir / ".system"
    self.output_dir = self.node_dir / "output"
    self.children_dir = self.node_dir / "children"
    # 自动创建
    for d in (self.context_dir, self.system_dir, self.output_dir, self.children_dir):
        d.mkdir(parents=True, exist_ok=True)
```

**关键设计: node_dir 在构造时 resolve()**

```python
self.node_dir = Path(node_dir).resolve()
```

这保证了：
1. 即使当前工作目录变化，node_dir 永远是绝对路径
2. 相对路径在构造时就被固定，不依赖后续 cwd
3. Guardian 的 root 也使用同一个 resolve() 后的路径

### 嵌套深度计算

子 Agent 的 Session 是父 Session 的 `children/` 子目录：
```python
def _current_depth(self) -> int:
    depth = 0
    node_dir = self.parent_session.node_dir
    while node_dir.name == "children" or (node_dir.parent and node_dir.parent.name == "children"):
        depth += 1
        node_dir = node_dir.parent.parent if node_dir.parent else node_dir
    return depth
```

最大深度限制为 2（`MAX_SUB_AGENT_DEPTH = 2`），防止无限 fork。

### 实际流程

**场景: 父 Agent 创建子 Agent**

1. 父 Agent 调用 `spawn_child(task="分析数据", name="analyzer")`
2. `SubAgentManager` 计算当前深度，检查是否超过 `MAX_SUB_AGENT_DEPTH`
3. `parent_session.child_session("analyzer")` 返回 `Session(node_dir/children/analyzer)`
4. 子目录自动创建：`children/analyzer/.context/`、`children/analyzer/.system/` 等
5. 子 Agent 的 `AgentLoop` 在 `children/analyzer/` 内运行，完全隔离
6. 子 Agent 完成后向自己的 `events.jsonl` 写入 `{"type": "completed"}`
7. 父 Agent 轮询 `children/analyzer/.system/events.jsonl` 获取结果

---

## DAG 即编排

### 设计思路

Meta Agent 描述任务依赖关系，Agenda 自动执行。不需要 Meta Agent 写控制流代码，只需要声明式地写：
- 有哪些任务（节点）
- 每个任务的 prompt 和模型
- 任务之间的依赖关系
- 产物如何在任务间传递

这是**声明式编排**，不是命令式编排。

### 核心算法: 拓扑排序 + 并行调度

#### 环检测（DFS）

```
输入: DAG 的节点和依赖关系
输出: 环中的节点列表，或 None

算法:
1. 每个节点标记为 WHITE（未访问）
2. 对每个 WHITE 节点执行 DFS:
   a. 标记为 GRAY（访问中）
   b. 对每个依赖节点:
      - 如果是 GRAY → 发现环，返回当前路径中从该节点开始的子路径
      - 如果是 WHITE → 递归 DFS
   c. 标记为 BLACK（已完成）
3. 无环返回 None

复杂度: O(V + E)，V = 节点数，E = 依赖边数
```

#### 拓扑排序（Kahn 算法）

```
输入: DAG 的节点和依赖关系
输出: 拓扑排序后的节点列表

算法:
1. 计算每个节点的入度（in_degree）
2. 将所有入度为 0 的节点加入队列
3. 当队列非空:
   a. 取出节点 n，加入结果
   b. 对 n 的每个下游节点 m，入度减 1
   c. 如果 m 的入度变为 0，加入队列
4. 返回结果

复杂度: O(V + E)
```

#### 并行调度

```
输入: 拓扑排序后的节点，max_parallel
状态: completed, running, failed

算法（主循环）:
1. 加载 scheduler_state.json 恢复状态
2. 扫描文件系统，标记已完成/失败的节点
3. while completed + failed < total:
   a. 清理已完成的 running 任务
   b. 死锁检测: 如果没有 ready 节点且没有 running 任务但还有 pending 节点 → 死锁
   c. 就绪节点 = 依赖全部在 completed 中，且不在 running/failed/completed 中
   d. 启动 min(就绪节点数, max_parallel - running数) 个任务
   e. 等待任意任务完成或 1 秒超时
4. 返回所有节点状态

复杂度: 每个节点最多运行 (1 + retries) 次
```

#### 产物传递

产物通过 `dep_inputs` 声明式传递：
```yaml
dep_inputs:
  - from: "upstream_node/output/draft.md"
    to: "input/deps/upstream/draft.md"
```

实现（`prepare_node()`）：
```python
def prepare_node(self, node_id: str) -> Session:
    session = Session(node_dir)
    # 1. 复制 meta inputs
    for src_pattern in config.get("inputs", []):
        self._copy_input(src_pattern, session.context_dir)
    # 2. 复制依赖产物
    for mapping in config.get("dep_inputs", []):
        src = self.dag_dir / mapping["from"]
        dst = session.context_dir / mapping["to"].lstrip("/")
        if src.exists():
            shutil.copy(src, dst)
    # 3. 恢复历史
    loaded = session.replay_history()
    # 4. 写 hints
    session.write_system("hints.md", hints)
    return session
```

### 恢复机制

**Scheduler 恢复**（`_load_scheduler_state()` + running 节点重置）：

```python
# 1. 从 scheduler_state.json 恢复
completed, failed, running, retries = load_state()

# 2. 文件系统扫描验证
for n in node_ids:
    if node_is_done(n):      # output/draft.md 存在
        completed.add(n)
    elif node_is_failed(n):  # .system/error.log 存在
        handle_retry(n)

# 3. 崩溃后 running 状态无效，重新分类
for n in list(running):
    if node_is_done(n):
        completed.add(n)
        running.discard(n)
    elif node_is_failed(n):
        handle_retry(n)
        running.discard(n)
    else:
        # 被中断了，重置为 pending
        running.discard(n)
```

### 实际流程

**场景: 3 个节点的 DAG**
```yaml
nodes:
  collect:   { prompt: "收集数据" }
  analyze:   { prompt: "分析数据", deps: [collect] }
  report:    { prompt: "写报告", deps: [collect, analyze] }
```

执行流程：
1. Scheduler 加载 DAG，拓扑排序: `[collect, analyze, report]`
2. `collect` 入度为 0，启动（running=1）
3. `analyze` 依赖 `collect` 未完成，等待
4. `report` 依赖未完成，等待
5. `collect` 完成（`output/draft.md` 出现）→ completed={collect}
6. `analyze` 依赖满足，启动
7. `report` 依赖还有 `analyze` 未完成，等待
8. `analyze` 完成 → completed={collect, analyze}
9. `report` 依赖满足，启动
10. `report` 完成 → 全部完成

产物传递：
- `analyze` 启动前，`prepare_node()` 把 `collect/output/draft.md` 复制到 `analyze/.context/input/deps/collect/draft.md`
- `report` 启动前，复制 `collect` 和 `analyze` 的产物

---

## Guardian 即边界

### 设计思路

Agent 运行不受信任的代码（LLM 生成的 tool calls）。必须有一个**硬边界**防止 Agent 逃出允许的文件范围。

关键约束：
1. **不能依赖 LLM 审查**（SecurityReviewer）—— LLM 无法可靠判断路径是否安全
2. **不能依赖 prompt 约束**—— Agent 可能"忽略"prompt 中的规则
3. **必须用操作系统级别的路径解析**—— `resolve()` 跟随 symlink，`relative_to()` 检查归属

这是从 Butterfly 的 `Guardian` 类学来的设计，但简化了：
- 没有 `check_read()`/`check_write()` 的区分（合并为 `check()`）
- 没有 explorer/executor 模式切换
- 只保留核心：`resolve()` → `relative_to(root)`

### 算法

#### resolve + relative_to 防逃逸

```
输入: 路径 path, 边界根目录 root
输出: resolve 后的绝对路径，或 PermissionError

算法:
1. root = Path(root).resolve()    # 构造时 resolve，防止 cwd 变化影响
2. p = Path(path)
3. 如果 p 不是绝对路径:
      p = root / p                 # 相对路径 join 到 root
4. target = p.resolve()           # 跟随 symlink，消除 .. 和 .
5. 尝试: target.relative_to(root) # 检查是否在 root 内
      成功 → 返回 target
      失败 → 抛 PermissionError

复杂度: O(路径深度)
```

**为什么能防 symlink 逃逸？**

假设攻击者创建 symlink：
```bash
ln -s /etc node_dir/.context/link
```

调用 `guardian.resolve(".context/link/passwd")`：
1. `p = root / ".context/link/passwd"`
2. `p.resolve()` 跟随 symlink → `/etc/passwd`
3. `/etc/passwd`.relative_to(`/workspace/node_dir`) → ValueError
4. 抛 PermissionError

#### 双层防护

Session 使用双层防护：

| 层级 | 实现 | 职责 |
|------|------|------|
| **Guardian（安全层）** | `check_read()` / `check_write()` | 任何路径不能逃出 `node_dir` |
| **`_resolve_safe`（语义层）** | `relative_to(context_dir)` 或 `relative_to(output_dir)` | Agent 只能读 `.context/` / `output/`，只能写 `output/` |

```python
def read_context(self, rel_path: str) -> str:
    target = self.guardian.check_read(rel_path)   # 层1: 不能出 node_dir
    safe = self._resolve_safe(rel_path)            # 层2: 语义限制
    if not safe:
        return "[错误] 路径不允许"
    # ... 读取文件

def write_output(self, rel_path: str, content: str) -> str:
    target = self.guardian.check_write(rel_path)   # 层1: 不能出 node_dir
    try:
        target.relative_to(self.output_dir)        # 层2: 只能写 output/
    except ValueError:
        return "[错误] 只能写入 output/"
    # ... 写入文件
```

### 实现

`Guardian` 类（47 行）：
```python
class Guardian:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root).resolve()

    def resolve(self, path: Path | str) -> Path:
        p = Path(path)
        if not p.is_absolute():
            p = self.root / p
        return p.resolve()

    def is_allowed(self, path: Path | str) -> bool:
        target = self.resolve(path)
        try:
            target.relative_to(self.root)
            return True
        except ValueError:
            return False

    def check(self, path: Path | str, *, operation: str = "access") -> Path:
        target = self.resolve(path)
        try:
            target.relative_to(self.root)
            return target
        except ValueError:
            raise PermissionError(f"[Guardian] {operation} to {target} denied")
```

### 实际流程

**场景: Agent 尝试路径遍历**

1. Agent 调用 `write_file(path="../escape.md", content="...")`
2. `session.write_output("../escape.md", "...")`
3. `guardian.check_write("../escape.md")`:
   - `p = root / "../escape.md"` → `root/../escape.md`
   - `p.resolve()` → `/workspace/escape.md`（逃出 root）
   - `relative_to(root)` → ValueError
   - 抛 PermissionError
4. Session catch PermissionError，返回字符串错误：`"[Guardian] write to /workspace/escape.md denied"`
5. Agent 收到错误，继续执行

**场景: Agent 尝试读取 .system/**

1. Agent 调用 `read_file(path=".system/state.json")`
2. `guardian.check_read(".system/state.json")`:
   - 路径在 root 内 → 通过
3. `_resolve_safe(".system/state.json")`:
   - 不在 `.context/` 或 `output/` 内 → 返回 None
4. 返回语义错误：`"[错误] 路径不允许"`

---

## 四者如何协同工作

这四个设计哲学不是独立的，而是互相依赖的：

```
目录即 Session
    ├── 提供隔离边界（每个 Agent 一个目录）
    ├── 文件即状态（目录内的 turns.jsonl/events.jsonl）
    │       └── 中断后从文件恢复
    ├── Guardian 即边界（目录 = Guardian 的 root）
    │       └── 防逃逸 + 语义限制
    └── DAG 即编排（目录间的产物传递 + 调度）
            └── dep_inputs 复制上游 output 到下游 .context
```

**端到端示例**: Meta Agent 写一个研究任务的 DAG

```
Meta Agent 生成 JSON
    ↓
agenda dag create --from-json - -o dag.yaml
    ↓
agenda dag validate dag.yaml          ← Guardian 检查产物路径
    ↓
agenda dag run dag.yaml
    ↓
Scheduler 创建 collect_sources/ Session 目录
    ↓
AgentLoop 在 collect_sources/ 内运行
    - Guardian 限制只能在 collect_sources/ 内
    - 每轮 save_turn() 到 .system/turns.jsonl
    - 完成时写 output/draft.md
    ↓
Scheduler 检测到 collect_sources 完成
    ↓
Scheduler 创建 analyze_trends/ Session 目录
    prepare_node():
        - dep_inputs: 复制 collect_sources/output/draft.md → analyze_trends/.context/sources.md
        - replay_history(): 空（新节点）
    ↓
AgentLoop 在 analyze_trends/ 内运行...
    ↓
（循环直到全部完成或失败）
    ↓
Ctrl+C 中断
    ↓
重新运行 agenda dag run dag.yaml
    - scheduler_state.json: 知道哪些节点已完成
    - turns.jsonl: 未完成节点恢复对话历史
    - 从断点继续
```

---

## 参考

- Butterfly Agent: https://github.com/dannyxiaocn/butterfly-agent
- EVA: https://github.com/usepr/eva
