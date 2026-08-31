# 各 Subagent 工具与权限数据（原封提取自 oh-my-opencode-slim v2.2.15）

> 来源：`node_modules/oh-my-opencode-slim/dist/index.js`（v2.2.15 编译产物）
> 提取日期：2026-08-30。所有数据逐字对应 bundle，未做任何改写。

## 一、权限的三个生效层级

1. **factory 内置 permission**：仅 `council`（synthesis-only）与 `councillor`（read-only）在
   `createXxxAgent()` 中显式携带；其余 6 个 agent 的 factory config **没有** permission 字段。
2. **applyDefaultPermissions 统一补齐**（所有 subagent 都会经过）：
   - `question`: 原值为 `deny` 则保持 `deny`，否则补 `allow`
   - `task_cancel` / `task_message` / `task_revive` / `task_status` / `task_result`
     （`TASK_CONTROL_TOOL_NAMES`）: 非 orchestrator 一律补 `deny`
   - `wait_for_user`: 仅 orchestrator `allow`，其余 `deny`
   - `skill`: 无显式 skills 配置时非 orchestrator 为 `{"*": "deny"}`
3. **override 整体替换**：`agents.<name>.permission`（oh-my-opencode-slim.json）存在时
   **整体替换** config.permission（`applyOverrides`），然后再走第 2 步补齐。

## 二、两个内置权限模板原文

### createReadOnlyAgentPermission()（用于 councillor）

```js
{
  "*": "deny",
  bash: "deny", edit: "deny", write: "deny",
  apply_patch: "deny", ast_grep_replace: "deny",
  task: "deny", question: "deny",
  read: "allow", glob: "allow", grep: "allow",
  lsp: "allow", list: "allow", codesearch: "allow", ast_grep_search: "allow"
}
```

### createSynthesisOnlyPermission()（用于 council）

```js
{
  "*": "deny",
  bash: "deny", edit: "deny", write: "deny",
  apply_patch: "deny", ast_grep_replace: "deny",
  task: "deny", question: "deny",
  read: "deny", glob: "deny", grep: "deny",
  lsp: "deny", list: "deny", codesearch: "deny", ast_grep_search: "deny"
}
```

## 三、各 agent 最终有效权限

| agent | factory permission | applyDefaultPermissions 补齐后（最终生效） |
|---|---|---|
| explorer | 无 | `question=allow`；`task_*、wait_for_user=deny`；`skill={"*":"deny"}`；bash/edit/write/read 等其余工具**无显式规则**（跟随全局/项目 permission 默认） |
| librarian | 无 | 同 explorer |
| oracle | 无 | 同 explorer |
| designer | 无 | 同 explorer |
| fixer | 无 | 同 explorer |
| observer | 无 | 同 explorer |
| council | createSynthesisOnlyPermission() | 全部工具 deny（含 `*`），`question=deny`，`task_*=deny`，`wait_for_user=deny`，`skill={"*":"deny"}` |
| councillor | createReadOnlyAgentPermission() | deny: `*、bash、edit、write、apply_patch、ast_grep_replace、task、question`；allow: `read、glob、grep、lsp、list、codesearch、ast_grep_search`；补齐：`task_*、wait_for_user=deny`，`skill={"*":"deny"}` |

> 注意：6 个常规 agent（explorer/librarian/oracle/designer/fixer/observer）prompt 中声明的
> "工具边界"（如 explorer "search and report, don't modify"）只是**行为约定**，不是硬权限；
> 硬隔离只有 council（全 deny）与 councillor（只读白名单）。

## 四、模型温度与元数据

见同目录 `agents.manifest.json`（description / temperature 逐 agent 记录）。
温度速览：explorer/librarian/oracle/observer/council = 0.1；fixer/councillor = 0.2；designer = 0.7。

## 五、orchestrator 权限补齐差异

orchestrator 经 `applyDefaultPermissions` 后：`task_* = allow`、`wait_for_user = allow`、`skill={"*":"allow"}`，
与全部 subagent 相反。orchestrator prompt 为运行时动态构建（Role/Agents/Workflow/Communication），
其模板快照见 `_reference/orchestrator.template.txt`（仅供参考，未播种）。
