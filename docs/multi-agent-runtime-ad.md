# Architecture Decision: Multi-Agent Runtime for Research Book Studio

> **Status**: Proposed  
> **Date**: 2026-04-23  
> **Context**: Research Book Studio skill produces evidence-backed ebooks via a 5-phase workflow (Research → Outline → Gap Research → Manuscript → Publication). The manuscript phase requires writing 8,000–35,000 words across 10+ chapters with cross-chapter dependencies. Single-agent context windows and sequential writing are bottlenecks. We need a multi-agent runtime that can parallelize chapter writing while respecting dependency order, managing shared context, and keeping memory within token limits.

---

## Table of Contents

1. [Problem Statement](#1-problem-statement)
2. [Candidate Analysis: Butterfly Agent](#2-candidate-analysis-butterfly-agent)
3. [Candidate Analysis: EVA](#3-candidate-analysis-eva)
4. [Head-to-Head Comparison](#4-head-to-head-comparison)
5. [Why Neither is Sufficient Alone](#5-why-neither-is-sufficient-alone)
6. [Proposed Solution: Agenda](#6-proposed-solution-agenda)
7. [Architecture](#7-architecture)
8. [Implementation Roadmap](#8-implementation-roadmap)
9. [Decision](#9-decision)
10. [References](#10-references)

---

## 1. Problem Statement

### Current Bottleneck

The Research Book Studio manuscript phase has two hard constraints:

| Constraint | Current Impact |
|---|---|
| **Context Window** | A single agent cannot hold the full outline + all evidence cards + dependent chapter drafts in one prompt. Writing Chapter 9 (Head-to-Head) requires reading Chapters 3 and 6 in full. |
| **Sequential Time** | Writing 12 chapters serially takes 20+ minutes of LLM time. With parallelization, the critical path is roughly `max(depth of DAG) × single-chapter time`, potentially 3–4× faster. |

### Requirements for a Multi-Agent Runtime

| Requirement | Priority | Description |
|---|---|---|
| **DAG-native scheduling** | P0 | Chapters have dependencies (e.g., Ch 9 requires Ch 3 and Ch 6). The runtime must understand DAG edges, not just run tasks in parallel. |
| **Session Isolation** | P0 | Each chapter writer must have its own context space so that evidence cards and drafts do not leak between unrelated chapters. |
| **Cross-Node Context Passing** | P0 | Downstream nodes must receive upstream outputs (drafts, summaries) without manual `cp` scripts. |
| **Memory Compression** | P1 | Long-running agents will hit token limits. The runtime must manage memory overflow gracefully, ideally letting the AI itself decide what to keep. |
| **Hot Reload** | P1 | While writing, we will discover that prompts or outlines need adjustment. Changes should propagate to not-yet-started nodes without restarting the entire pipeline. |
| **Lightweight Deployment** | P1 | The runtime should not require a complex installation. "Paste and go" is the ideal. |
| **Security Review** | P2 | Agents execute shell commands and write files. A safety layer is needed, especially when running with `-a` (allow all). |
| **Web UI / Observability** | P3 | Nice to have, but CLI-first is acceptable for v1. |

---

## 2. Candidate Analysis: Butterfly Agent

**Repository**: https://github.com/dannyxiaocn/butterfly-agent  
**Maturity**: Very early (7 stars, 0 issues, created March 2026)  
**Language**: Python (~500KB across 30+ files)  
**Philosophy**: *"Filesystem as agent's backend"*

### 2.1 Architecture

Butterfly is a layered agent runtime with five conceptual layers:

```
agenthub/ (static agent templates)
  → session_engine (materializes agents into sessions)
    → Session (wraps Agent with file-backed persistence)
      → Agent (core loop: prompt → LLM → tool calls → repeat)
        → Provider (llm_engine)
        → Tools (tool_engine)
        → Skills (skill_engine)
  → runtime (watcher, IPC, bridge)
    → UI (cli, web)
```

### 2.2 Key Design Decisions

| Decision | Rationale |
|---|---|
| **Dual Directory Pattern** | `sessions/<id>/` (agent-visible workspace) vs `_sessions/<id>/` (system-only state). Agents never see system internals. |
| **Hot Reload** | Capabilities reload from disk before every agent activation. Edit files → agent picks up changes next run. |
| **Self-Contained Agents** | Each agent in `agenthub/` is fully self-contained — all prompts, tools, and skills are physically present. New agents are created with `--init-from <source>` (one-time copy) or `--blank`. |
| **Meta Sessions** | Each agent seeds a meta session once; the meta session is the authoritative, evolving config. Child sessions are seeded from meta. Version staleness notices inform users when meta has advanced. |
| **File-Based IPC** | JSONL append-only logs with byte-offset polling. No sockets, no message queues, no databases. |

### 2.3 Strengths

**1. Meta Session = DAG Shared Context Done Right**

The meta session is Butterfly's most powerful and unique feature. In a DAG scenario:

- Chapter 3 writes its draft into the meta session.
- Chapter 9 (a child session) is seeded from meta, automatically inheriting the latest Chapter 3 draft.
- If Chapter 3 is revised, the meta session updates. Any downstream child session that has not yet started gets the corrected version automatically.

This is not manual file copying. It is a **living inheritance graph**.

**2. Hot Reload = Iterative Writing Without Restart**

When writing a book, you constantly discover that prompts need tuning. Butterfly's hot reload means:

- Edit `agenthub/book_writer/prompts/system.md`.
- The next time any session activates, it loads the new prompt.
- No process restart, no DAG re-run, no state loss.

For a multi-day book project, this is not a convenience — it is a requirement.

**3. Hook Mechanism = Node-Level Validation**

`core/hook.py` allows inserting logic at every stage of the agent loop:

```python
@hook.before_tool_call
def check_outline_alignment(ctx):
    # Reject tool calls that would write off-outline content
    ...

@hook.after_completion
def continuity_check(ctx):
    # Ensure Chapter 9 does not contradict Chapter 3
    ...
```

This enables **cross-chapter validation inside the agent**, not just as a post-processing step.

**4. File IPC = Concurrent-Safe Observability**

JSONL append-only logs are:
- **Concurrent-safe**: Multiple processes can append without locks.
- **Real-time**: A coordinator can poll byte offsets to see live progress.
- **Auditable**: The entire execution history is in plain text.

If you need a dashboard showing "which of 12 writers is currently running," Butterfly's IPC is the right foundation.

**5. Multi-Model Provider Support**

Butterfly ships with dedicated providers for Codex (48KB), Anthropic (29KB), OpenAI (26KB), and Kimi (9KB). This means different DAG nodes can use different models — e.g., Claude for deep technical chapters, GPT-4o for summarization — without environment variable gymnastics.

### 2.4 Weaknesses

**1. Massive Codebase for the Problem at Hand**

- `session.py`: 130KB single file
- `ipc.py`: 47KB
- `ui/web/frontend/`: 300KB+ of TypeScript/CSS

For a "run 12 writers in a DAG" problem, Butterfly brings a full web frontend, a git coordinator, and a model catalog. Most of this is **dead weight** for our use case.

**2. No Native DAG Concept**

Butterfly understands sessions, meta sessions, and file IPC. It does **not** understand "Chapter 3 must finish before Chapter 9 starts." You must write an orchestrator on top of Butterfly's runtime API.

**3. No Memory Compression**

When a Butterfly session hits its token limit, there is no automatic mitigation. The session will either fail or truncate. EVA's "《紧急危机》" self-compression mechanism is entirely absent.

**4. Very Early Maturity**

7 stars, 0 issues, no test directory visible in the file tree. Using Butterfly as a dependency means accepting the risk of undiscovered bugs in 130KB of session logic.

---

## 3. Candidate Analysis: EVA

**Repository**: https://github.com/usepr/eva  
**Maturity**: Early but viral (214 stars, growing fast, created April 2026)  
**Language**: Python (single file, 27KB)  
**Philosophy**: *"如果一个智能体的执行层小到只是一个脚本，那它具有病毒传播一样的潜力。"*

### 3.1 Architecture

EVA is aggressively minimal. The entire system is one file with these modules:

| Module | Lines | Purpose |
|---|---|---|
| LLM Config | ~20 | Environment variables for API endpoint |
| Environment Probe | ~30 | Auto-detect OS, installed tools, directory listing |
| System Prompt | ~50 | Role definition + robot three laws + evolution instructions |
| Tool Schemas | ~20 | `run_cli` and `leave_memory_hints` function definitions |
| Tool Executors | ~40 | Shell execution with security review |
| Session Manager | ~60 | Directory-scoped JSON sessions + file locking |
| Agent Loop | ~80 | Stream LLM output, execute tools, loop until done |
| Main | ~40 | CLI argument parsing + entry point |

### 3.2 Key Design Decisions

| Decision | Rationale |
|---|---|
| **Directory-Level Session Isolation** | `sessions/{dir_hash}.json`. `cd` to a different directory = new session. No explicit session manager needed. |
| **AI-Driven Memory Compression** | When tokens reach 75% of limit, system injects 《紧急危机》prompt. The AI itself decides what to archive, what to keep, and what hints to leave for future retrieval. |
| **Security Self-Review** | Before executing any shell command, EVA asks the LLM to classify it as "放行" (read-only) or "禁止" (write/execute). |
| **Hints.md as Dynamic Knowledge Base** | `.eva/hints.md` is loaded into the system prompt. The AI updates it during memory compression to create navigable pointers to archived knowledge. |
| **Single File Deployment** | Copy `eva.py`, set three environment variables, run. No `pip install`, no dependencies beyond `requests`. |

### 3.3 Strengths

**1. Unmatched Simplicity**

27KB in one file. Paste into any server, any container, any environment. This is not just engineering minimalism — it is a **deployment superpower**. For a DAG runtime that needs to spawn dozens of agents across different directories, the absence of installation friction matters enormously.

**2. AI-Driven Memory Compression is Elegant**

Most agent frameworks handle memory overflow with crude truncation or fixed summarization. EVA's approach is different:

```markdown
《紧急危机》！！！记忆容量即将达到上限，你需要紧急完成三件事情：
1、保存记忆：将对话内容整理到文件里保存下来
2、保存技能和知识：提炼对未来有用的内容，写入知识文件
3、留下关键线索以便你未来在有需要的时候可以找回并翻看这些记忆文件
```

The AI decides what matters. The AI decides how to organize it. The AI leaves breadcrumbs for its future self. This is **genuinely novel** and more robust than any hand-coded compression heuristic.

**3. Security Review by the LLM Itself**

Instead of a regex whitelist, EVA asks the model:

```markdown
作为安全专家，对命令进行安全审查。若命令仅为只读操作（cat, ls, grep），
输出"放行"；若涉及写入、执行、修改、网络连接，输出"禁止"。
```

This catches semantic threats that regex cannot (e.g., `curl | bash` disguised as a read operation).

**4. Hints.md as Self-Evolving Index**

The `.eva/hints.md` file acts as a **dynamic table of contents** for the agent's own knowledge base. As the agent learns, it updates hints. As it encounters new tasks, it consults hints. This creates a **self-improving retrieval system** without vector databases or embedding models.

### 3.4 Weaknesses

**1. No DAG Concept Whatsoever**

EVA understands one directory = one session. It has no concept of "this session must wait for two other sessions to finish." Orchestration must be entirely external — a shell script, a Makefile, or a Python wrapper.

**2. Session Isolation is Too Isolated**

Because each directory is a completely independent session, sharing context between nodes requires manual file copying. There is no meta session, no inheritance, no hot reload. If Chapter 3's prompt needs to change, every downstream node must be manually updated.

**3. JSON Session Files are Not Concurrent-Safe**

EVA's session is a single JSON file written in full on every save:

```python
with open(session_file, "w") as f:
    json.dump(messages, f)
```

If two EVA instances run in the same directory (or if the orchestrator tries to read while EVA is writing), **race conditions and data loss are possible**. Butterfly's JSONL append-only approach is fundamentally safer for concurrent DAG execution.

**4. No Hook Mechanism**

There is no way to inject validation logic between the LLM's tool call and its execution. If you want to enforce "Chapter 9 must not contradict Chapter 3," you must do it as a post-processing step, not as an inline guard.

**5. Single Provider Interface**

EVA hardcodes OpenAI's `chat/completions` format. Switching to Anthropic requires changing environment variables and hoping the API is compatible. Butterfly's dedicated provider abstractions handle model-specific quirks (thinking blocks, tool formats, rate limits) properly.

---

## 4. Head-to-Head Comparison

| Dimension | Butterfly | EVA | Winner |
|---|---|---|---|
| **Code Size** | ~500KB, 30+ files | 27KB, 1 file | EVA |
| **Installation** | `pip install -e .` | Paste file, set 3 env vars | EVA |
| **DAG Support** | None (needs external orchestrator) | None (needs external orchestrator) | Tie |
| **Session Isolation** | Directory + dual-dir + meta session | Directory + JSON file | Butterfly |
| **Context Passing** | Meta session inheritance | Manual file copy | Butterfly |
| **Hot Reload** | Yes (native) | No | Butterfly |
| **Memory Compression** | No | AI-driven 《紧急危机》 | EVA |
| **Hook / Validation** | Yes (`before_tool_call`, `after_completion`) | No | Butterfly |
| **Security Review** | No | LLM-based 放行/禁止 | EVA |
| **File IPC** | JSONL append-only (concurrent-safe) | JSON snapshot (race-prone) | Butterfly |
| **Multi-Model** | Native (Codex, Anthropic, OpenAI, Kimi) | Single OpenAI interface | Butterfly |
| **Web UI** | Yes (Vite + TS frontend) | No | Butterfly (if needed) |
| **Observability** | Byte-offset polling of JSONL | Read JSON file on disk | Butterfly |
| **Maturity Risk** | High (7 stars, 130KB untested session.py) | Medium (214 stars, simpler surface) | EVA |
| **Self-Contained** | No (needs full project install) | Yes (single file) | EVA |

---

## 5. Why Neither is Sufficient Alone

### Butterfly's Fatal Flaw for This Use Case

Butterfly is a **general-purpose agent OS**. It has:
- A web frontend you do not need
- A git coordinator you do not need
- 130KB of session logic when you need ~30KB
- No concept of DAG dependency

Using Butterfly for "12 chapter writers with dependency edges" is like using Kubernetes to run a shell script. It solves the problem, but the overhead dominates the work.

### EVA's Fatal Flaw for This Use Case

EVA is a **personal automation script**. It has:
- No meta session for shared context
- No safe IPC for concurrent nodes
- No hook for cross-chapter validation
- No hot reload for iterative prompt tuning

Using EVA for a multi-day book project with 12 interdependent chapters means writing a fragile external orchestrator that re-implements everything Butterfly already has.

### The Gap

What we need is a **third thing**: a runtime that is as lightweight as EVA but understands DAGs, sessions, and shared context natively — without the 400KB of Butterfly infrastructure we will never use.

---

## 6. Proposed Solution: Agenda

> **Working Name**: `Agenda` — *"An agent runtime that natively speaks DAG."*

### 6.1 Design Principles

| Principle | Source | Rationale |
|---|---|---|
| **File System is the Only State Store** | Butterfly | No databases, no sockets, no message queues. Git-friendly, debuggable, inspectable. |
| **Directory = Session = DAG Node** | EVA | `cd` into a directory, run the agent. No session manager API to learn. |
| **DAG is a First-Class Citizen** | Neither | Dependencies, context passing, and execution order are native concepts, not afterthoughts. |
| **AI Manages Its Own Memory** | EVA | When context overflows, the AI decides what to archive — not a fixed truncation rule. |
| **Hot Reload by Checksum** | Butterfly | Changing a shared prompt propagates to all not-yet-started nodes automatically. |
| **Single-File Deployment** | EVA | The core runtime should fit in one file (~300–500 lines) that can be pasted anywhere. |
| **Security Review by LLM** | EVA | Before executing shell commands, ask the model itself to classify the risk. |

### 6.2 Core Concepts

#### DAG Definition (`dag.yaml`)

The DAG is not a Python script. It is a declarative YAML file that the runtime natively understands:

```yaml
dag:
  name: "hermes_vs_openclaw"
  max_parallel: 4
  timeout_per_node: 600

nodes:
  ch01_intro:
    agent: book_writer
    prompt: "写第一章：Agent 爆发背景"
    inputs:
      - "meta/outline.md#ch01"
      - "meta/evidence/E-001.md"
    output: "output/draft.md"

  ch03_hermes:
    agent: book_writer
    prompt: "写第三章：Hermes 深度解析"
    deps: [ch01_intro]
    inputs:
      - "meta/outline.md#ch03"
      - "meta/evidence/E-002.md"
      - "meta/evidence/E-003.md"
    dep_inputs:
      - from: "ch01_intro/output/draft.md"
        to: "input/deps/ch01_intro/draft.md"
    output: "output/draft.md"

  ch09_compare:
    agent: book_writer
    prompt: "写第九章：架构对比。必须阅读 input/deps/ 下的前置章节"
    deps: [ch03_hermes, ch06_openclaw]
    dep_inputs:
      - from: "ch03_hermes/output/draft.md"
        to: "input/deps/ch03_hermes/draft.md"
      - from: "ch06_openclaw/output/draft.md"
        to: "input/deps/ch06_openclaw/draft.md"
    output: "output/draft.md"
```

#### Session Layout (Dual Directory)

Each node is a directory with Butterfly-inspired isolation:

```
nodes/ch03_hermes/
├── .context/              # Agent-visible workspace (read/write)
│   ├── outline.md         # Injected by orchestrator
│   ├── evidence/          # Injected evidence cards
│   └── deps/              # Read-only upstream outputs
│       └── ch01_intro/
│           └── draft.md
├── .system/               # System state (agent invisible)
│   ├── session.jsonl      # Append-only conversation log
│   ├── memory/            # AI-compressed archives
│   │   └── 20260423_001.md
│   ├── skills/            # Learned skills
│   └── hints.md           # Dynamic retrieval index
├── output/                # Node products (orchestrator watches)
│   └── draft.md           # Existence = completion signal
└── dag.state              # Node-specific state (checksums, timestamps)
```

#### Meta Session

The DAG has a single meta session at the workspace root:

```
workspace/
├── meta/                  # Shared config (outlines, evidence index)
├── _meta/                 # System state (orchestrator runtime)
│   ├── dag.state.json     # Global execution state
│   └── event.log          # Orchestrator audit trail
└── nodes/                 # Per-node directories
```

- **Seeding**: When a node starts, the orchestrator copies relevant files from `meta/` into `nodes/{id}/.context/`.
- **Inheritance**: Nodes launched after a meta update receive the new version automatically (checksum-based hot reload).
- **No live link**: Like Butterfly, the copy is one-time at session creation. The meta session is authoritative, but nodes do not auto-update after they have started (to avoid mid-writing disruption).

#### Memory Compression (Ported from EVA)

When a node's token count exceeds 75% of capacity:

1. Orchestrator pauses the node.
2. Injects the 《紧急危机》prompt into the conversation.
3. The AI decides what to archive to `.system/memory/`.
4. The AI updates `.system/hints.md` with retrieval pointers.
5. The AI truncates the conversation, keeping only system prompt + hints + last turn.
6. Node resumes.

This is **not** the orchestrator compressing memory. It is the **agent compressing itself**, guided by its own understanding of what matters.

#### Hook System (Simplified Butterfly)

```python
# hooks.py
class Hooks:
    def before_tool_call(self, func): ...
    def after_completion(self, func): ...

# Example: enforce outline alignment
@hooks.before_tool_call
def verify_outline(ctx):
    if ctx.tool == "write_output":
        if not aligns_with_outline(ctx.args["content"], ctx.node.outline):
            raise OutlineViolation("偏离大纲")

# Example: trigger downstream nodes
@hooks.after_completion
def dag_propagation(ctx):
    if ctx.node.output.exists():
        mark_complete(ctx.node.id)
        schedule_ready_children(ctx.node.id)
```

#### Security Review (Ported from EVA)

Before every `run_cli` tool call:

```python
def security_review(command: str) -> bool:
    result = llm_chat(
        prompt=f"审查命令: {command}\n只输出'放行'或'禁止'",
        temperature=0.0
    )
    return "放行" in result
```

Combined with a `--allow-all` flag for trusted environments.

---

## 7. Architecture

### 7.1 Component Diagram

```
┌─────────────────────────────────────────────────────────────┐
│                        agenda.py                             │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────┐  │
│  │   dag.py    │  │  session.py │  │     memory.py       │  │
│  │  DAG parser │  │ Dual-dir FS │  │ AI-driven compress  │  │
│  │  Scheduler  │  │  Hot reload │  │   Hints indexing    │  │
│  └──────┬──────┘  └──────┬──────┘  └─────────────────────┘  │
│         │                │                                   │
│  ┌──────▼────────────────▼─────────────────────┐             │
│  │           orchestrator.py                    │             │
│  │  Topological sort + Asyncio scheduler       │             │
│  │  File watcher (output/draft.md existence)   │             │
│  └──────────────────┬──────────────────────────┘             │
│                     │                                         │
│  ┌──────────────────▼──────────────────────┐                 │
│  │           hooks.py + security.py         │                 │
│  │  Validation hooks + LLM safety review   │                 │
│  └──────────────────────────────────────────┘                 │
└─────────────────────────────────────────────────────────────┘

                         │
                         ▼
              ┌────────────────────┐
              │   workspace/{dag}  │
              │   ├── dag.yaml     │
              │   ├── meta/        │
              │   └── nodes/       │
              └────────────────────┘
```

### 7.2 Execution Flow

```
1. User: agenda run
2. Orchestrator reads dag.yaml
3. Topological sort → execution queue
4. For each node whose deps are all COMPLETED:
   a. Prepare node dir: copy meta inputs + dep outputs
   b. Launch async task: python3 agenda.py --node {id} --once
   c. Task runs EVA-style loop: LLM → tools → loop → write output/draft.md
   d. Memory compression triggered automatically if needed
5. File watcher detects output/draft.md creation
6. Mark node COMPLETED in dag.state.json
7. Check if any downstream nodes are now ready
8. Repeat until all nodes COMPLETED or FAILED
9. Collect all output/draft.md files into final manuscript
```

### 7.3 State Machine (Per Node)

```
PENDING ──[deps satisfied]──► READY ──[orchestrator starts]──► RUNNING
                                  │                              │
                                  │                              │
                                  │                              ▼
                                  │                         COMPLETED
                                  │                         (output exists)
                                  │                              │
                                  │                              ▼
                                  └────────────────────────► FAILED
                                                               (timeout / error)
```

### 7.4 File System as Event Bus

Instead of a message queue, we use the file system as a lock-free event bus:

| Event | Signal | Consumer |
|---|---|---|
| Node started | `.system/session.jsonl` created | Orchestrator (timeout watchdog) |
| Node completed | `output/draft.md` created | Orchestrator (DAG propagation) |
| Node failed | `.system/error.log` created | Orchestrator (mark FAILED) |
| Meta updated | `meta/` mtime changed | Not-yet-started nodes (hot reload) |
| Memory compressed | `.system/memory/*.md` created | Same node (future retrieval) |

This is **Butterfly's IPC philosophy** adapted for DAG scheduling: no sockets, no polling loops, just `inotify` or periodic `stat` calls.

---

## 8. Implementation Roadmap

### Milestone 1: Core Runtime (Week 1)

**Goal**: Run a linear DAG (A → B → C) end-to-end.

**Deliverables**:
- [ ] `dag.py`: YAML parser + topological sort + execution queue
- [ ] `session.py`: Dual-directory session with file-based IPC
- [ ] `orchestrator.py`: Asyncio scheduler with `max_parallel` control
- [ ] `agenda.py`: CLI entry point (`init`, `run`, `status`)
- [ ] Single-provider LLM client (OpenAI-compatible)

**Validation**:
```bash
agenda init --template research-book-studio
agenda run
# Expect: 3 nodes execute sequentially, outputs in nodes/*/output/
```

### Milestone 2: Memory & Security (Week 2)

**Goal**: Handle long-running nodes without context overflow.

**Deliverables**:
- [ ] `memory.py`: Port EVA's 《紧急危机》compression mechanism
- [ ] `security.py`: LLM-based command review (放行/禁止)
- [ ] `hooks.py`: Hook registry + `before_tool_call` / `after_completion`
- [ ] Token estimation and auto-compact trigger

**Validation**:
- A node with 100+ turn conversation compresses itself without data loss.
- A `rm -rf /` command is rejected by security review.

### Milestone 3: Meta Session & Hot Reload (Week 3)

**Goal**: Shared context propagates without manual copying.

**Deliverables**:
- [ ] `meta.py`: Meta session seeding + checksum-based change detection
- [ ] Hot reload: nodes started after meta update get new version
- [ ] DAG context injection: `dep_inputs` mapping in `dag.yaml`
- [ ] Example: 12-chapter book DAG with realistic dependencies

**Validation**:
- Modify `meta/outline.md` mid-run.
- Downstream not-yet-started nodes pick up the new outline automatically.
- Already-running nodes are not disrupted.

### Milestone 4: Research Book Studio Integration (Week 4)

**Goal**: Replace manual manuscript writing with Agenda-orchestrated multi-agent pipeline.

**Deliverables**:
- [ ] `agenthub/book_writer/` agent template
- [ ] Integration with Research Book Studio's evidence cards
- [ ] Automatic `manuscript.md` assembly from node outputs
- [ ] `Makefile` target: `make write-book` → `agenda run`

**Validation**:
```bash
cd ~/research_book_studio/hermes-agent-vs-openclaw-agent
agenda run
# Expect: book.pdf generated from 12 parallel/sequential chapter writers
```

---

## 9. Decision

### Selected Approach

**Build `Agenda` as a new lightweight runtime**, incorporating:
- Butterfly's **dual-directory sessions**, **meta session inheritance**, and **hook mechanism**
- EVA's **AI-driven memory compression**, **LLM security review**, and **single-file deployment philosophy**
- **Native DAG support** that neither upstream project provides

### Why Not Fork Butterfly?

Butterfly's web frontend, git coordinator, and 130KB session.py are dead weight for our use case. Forking would require deleting more code than we write.

### Why Not Fork EVA?

EVA's single-file philosophy is brilliant for personal automation, but it lacks the architectural primitives (meta session, concurrent-safe IPC, hooks) needed for coordinated multi-agent work. Forking would require re-implementing Butterfly's session engine inside a 27KB file — impractical.

### Why Build New?

The gap between "personal agent script" (EVA) and "general agent OS" (Butterfly) is wide enough to justify a focused third option: a **DAG-native agent kernel** that weighs ~100KB and solves exactly one problem — running interdependent AI agents with shared context and memory management.

---

## 10. References

1. **Butterfly Agent**: https://github.com/dannyxiaocn/butterfly-agent
   - `docs/butterfly/design.md` — Runtime architecture
   - `docs/agent/design.md` — Agent template system
   - `docs/ui/design.md` — CLI and Web frontends

2. **EVA**: https://github.com/usepr/eva
   - `eva.py` — Single-file agent implementation
   - README — AI-driven memory compression and security review

3. **Research Book Studio**: This repository
   - `SKILL.md` — 5-phase book production workflow
   - `references/workspace.md` — Workspace layout and resume protocol
   - `references/research-node.md` — First-round and gap research rules
   - `references/manuscript-standards.md` — Chapter writing rules

4. **Prior Art**:
   - [CrewAI](https://github.com/joaomdmoura/crewAI) — Role-based multi-agent framework (Python, heavier)
   - [AutoGen](https://github.com/microsoft/autogen) — Conversational multi-agent (Microsoft, heavy)
   - [Prefect](https://github.com/PrefectHQ/prefect) / [Dagster](https://github.com/dagster-io/dagster) — DAG orchestrators (no AI agent integration)

---

*Document written during the design phase of Research Book Studio v0.2. All analysis based on source code inspection of Butterfly Agent (commit ~main, April 2026) and EVA (commit ~main, April 2026).*
