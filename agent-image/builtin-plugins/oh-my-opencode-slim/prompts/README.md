# oh-my-opencode-slim Subagent 默认 Prompt 快照（v2.2.15）

本目录是 **oh-my-opencode-slim v2.2.15** 内置 8 个 subagent 默认 prompt 的**原封提取快照**
+ 工具/权限数据，用途：展示默认行为基线、供后续优化 diff。

> 提取原则：逐字节还原运行时默认值。所有 `.md` 与 `agents.manifest.json` 中的内容
> 均直接来自 npm 包编译产物 `dist/index.js`，未做任何措辞改写。

## 文件清单

| 文件 | 说明 |
|---|---|
| `explorer.md` … `councillor.md`（8 个） | 各 subagent 的**运行时等价**默认 prompt（原封提取） |
| `agents.manifest.json` | 每 agent 的元数据：description、temperature、组装说明 |
| `tools-permissions.md` | 每 agent 的工具/权限数据（权限模板原文 + 生效层级） |
| `_reference/` | 提取过程证据：共享规则常量、权限/prompt 接线证据、orchestrator 动态模板 |

## 为什么 .md 不是"纯内置常量"？—— 等价性说明

运行时默认 prompt 的组装（`resolvePrompt` + 代码追加）：

```
默认 = resolvePrompt(inline ?? file ?? fallback)
     其中 fallback = 内置常量 + "\n\n" + TASK_REJECTION_INSTRUCTION
     council 额外: 内置常量 + COUNCIL_SYNTHESIS_REINFORCEMENT（直接相加）+ 拒绝指令
```

关键点：**代码级追加（拒绝指令 / council 强化段）只作用于 fallback 路径**。
一旦存在 file prompt（`.md`），整体替换、不再追加。因此本目录每个 `.md` 都显式
内联了这些追加文本，使"播种文件生效后的行为"与"无文件的默认行为"逐字节等价：

- 非 council：`内置常量（展开共享规则） + "\n\n" + 拒绝指令`
- council：`内置常量 + 强化段（自带 "\n\n---\n\n" 开头） + "\n\n" + 拒绝指令`

`TASK_REJECTION_INSTRUCTION` 原文：
`If a task is outside your role, do not attempt partial work. Return a brief reason to the orchestrator.`

## 运行时 prompt 文件的查找位置（loadAgentPrompt）

插件按以下优先级搜索 `{agent}.md`（命中即用，fallback 不再拼接）：

1. `{项目目录}/.opencode/oh-my-opencode-slim/{preset}/`
2. `{项目目录}/.opencode/oh-my-opencode-slim/`
3. `{XDG_CONFIG_HOME}/opencode/oh-my-opencode-slim/{preset}/`
4. `{XDG_CONFIG_HOME}/opencode/oh-my-opencode-slim/` ← **entrypoint 播种目标**

同目录若存在 `{agent}_append.md` 则追加到最终 prompt 之后（inline/file/fallback 三种路径都会追加）。

优先级总结（每 agent）：`agents.<name>.prompt`（inline JSON） > `{agent}.md`（file） > 内置 fallback。

## 修改指引（后续优化）

- **调 prompt**：直接编辑容器内 `${XDG_CONFIG_HOME}/opencode/oh-my-opencode-slim/{agent}.md`
  （首启由镜像 `prompts/` 播种，见 `agent-image/entrypoint.sh`）。优化后可与本目录快照 diff。
- **调工具/权限**：`oh-my-opencode-slim.json` 中 `agents.<name>.permission`（整体替换），
  模板数据见 `tools-permissions.md`。`agents.<name>.temperature` / `.model` / `.skills` 同理。
- **只追加不替换**：写 `{agent}_append.md`。
- **orchestrator**：prompt 为运行时动态构建（见 `_reference/orchestrator.template.txt`），
  支持 `orchestrator.md` 替换但会**冻结**动态部分（Agents 列表等不再刷新），故默认不播种。
