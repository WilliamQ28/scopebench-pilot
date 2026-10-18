"""The agent-harness toolsets.

The tools are grouped into five PydanticAI ``FunctionToolset``s, each with its own
``id`` and focused instructions. Destructive tools (shell/Python execution and file
writes) are tagged ``metadata={"destructive": True}`` and their toolsets are wrapped
with ``.approval_required(...)`` so that, when ``deps.require_approval`` is set, those
calls are surfaced to the caller for approval before running.
"""

from pydantic_ai import AbstractToolset, FunctionToolset, RunContext, Tool, ToolDefinition

from agent_harness.deps import HarnessDeps
from agent_harness.tools import (
    apply_patch,
    ask_user,
    bash,
    clear_memory,
    confirm,
    delete_lines,
    edit_file,
    fetch,
    glob,
    grep,
    insert_lines,
    list_memory_keys,
    ls,
    multiedit,
    python,
    read,
    report,
    retrieve_memory,
    save_memory,
    think,
    todo,
    web_extract,
    write,
)

_DESTRUCTIVE = {"destructive": True}


def _needs_approval(
    ctx: RunContext[HarnessDeps],
    tool_def: ToolDefinition,
    _args: dict[str, object],
) -> bool:
    """Require approval for tools tagged destructive when the caller opts in."""
    metadata = tool_def.metadata or {}
    return ctx.deps.require_approval and metadata.get("destructive") is True


_EXECUTION_INSTRUCTIONS = (
    "Use `bash` for shell commands and `python` to run Python; both execute locally "
    "in the session working directory."
)
_FILESYSTEM_INSTRUCTIONS = (
    "Read and navigate with `read`, `ls`, `glob`, and `grep`; modify files with "
    "`write`, `edit_file`, `multiedit`, `insert_lines`, `delete_lines`, and "
    "`apply_patch`. Read a file before editing or overwriting it, and read it "
    "again if it changes on disk. Prefer `edit_file` over `write` for existing "
    "files."
)
_WEB_INSTRUCTIONS = "Use `fetch` for one URL and `web_extract` for several; both are read-only."
_WORKFLOW_INSTRUCTIONS = (
    "Use `think` to reason, `todo` to track tasks, and `report` to persist findings. "
    "Use `ask_user` to ask a question and `confirm` before risky actions."
)
_MEMORY_INSTRUCTIONS = (
    "Persist small values across steps with `save_memory` / `retrieve_memory` / "
    "`list_memory_keys` / `clear_memory`."
)

# Base toolsets (kept module-level so tests can introspect their `.tools`).
# Destructive tools carry `metadata={"destructive": True}`; `confirm` always
# requires approval. Both are declared inline via `Tool(...)`.
execution: FunctionToolset[HarnessDeps] = FunctionToolset(
    tools=[
        Tool(bash, metadata=_DESTRUCTIVE),
        Tool(python, metadata=_DESTRUCTIVE),
    ],
    id="execution",
    instructions=_EXECUTION_INSTRUCTIONS,
)

filesystem: FunctionToolset[HarnessDeps] = FunctionToolset(
    tools=[
        read,
        ls,
        glob,
        grep,
        Tool(write, metadata=_DESTRUCTIVE),
        Tool(edit_file, metadata=_DESTRUCTIVE),
        Tool(multiedit, metadata=_DESTRUCTIVE),
        Tool(insert_lines, metadata=_DESTRUCTIVE),
        Tool(delete_lines, metadata=_DESTRUCTIVE),
        Tool(apply_patch, metadata=_DESTRUCTIVE),
    ],
    id="filesystem",
    instructions=_FILESYSTEM_INSTRUCTIONS,
    # The read gate refuses edits of unread/stale files via ModelRetry; give the file
    # tools more consecutive-failure headroom than the agent-wide default of 2 so a
    # refuse -> retry -> refuse -> read -> succeed sequence can never kill a run.
    max_retries=4,
)

web: FunctionToolset[HarnessDeps] = FunctionToolset(
    tools=[fetch, web_extract],
    id="web",
    instructions=_WEB_INSTRUCTIONS,
)

workflow: FunctionToolset[HarnessDeps] = FunctionToolset(
    tools=[
        think,
        todo,
        report,
        ask_user,
        Tool(confirm, requires_approval=True),
    ],
    id="workflow",
    instructions=_WORKFLOW_INSTRUCTIONS,
)

memory: FunctionToolset[HarnessDeps] = FunctionToolset(
    tools=[save_memory, retrieve_memory, list_memory_keys, clear_memory],
    id="memory",
    instructions=_MEMORY_INSTRUCTIONS,
)

# The selectable toolset groups, keyed by id in the order they attach to the agent. This is the
# single source of truth for the reproducible tool-set-selection axis (the harness-capability
# experimental variable): a run may restrict the agent to a subset of these groups.
_BASE_BY_ID: dict[str, FunctionToolset[HarnessDeps]] = {
    "execution": execution,
    "filesystem": filesystem,
    "web": web,
    "workflow": workflow,
    "memory": memory,
}
TOOLSET_IDS: tuple[str, ...] = tuple(_BASE_BY_ID)

# The ungated toolsets, in attach order (kept for tests/introspection).
_BASE_TOOLSETS = tuple(_BASE_BY_ID.values())

# Destructive toolsets are gated with approval_required; the rest attach directly. Built once per id
# so selection reuses the exact same instances the full default uses.
_GATED_BY_ID: dict[str, AbstractToolset[HarnessDeps]] = {
    "execution": execution.approval_required(_needs_approval),
    "filesystem": filesystem.approval_required(_needs_approval),
    "web": web,
    "workflow": workflow,
    "memory": memory,
}

# Default: every group, in canonical order.
TOOLSETS: list[AbstractToolset[HarnessDeps]] = list(_GATED_BY_ID.values())

# Every tool name the agent exposes (single source of truth for tests/introspection).
ALL_TOOL_NAMES = frozenset(name for toolset in _BASE_TOOLSETS for name in toolset.tools)


def select_toolsets(ids: list[str] | None) -> list[AbstractToolset[HarnessDeps]]:
    """Return the gated toolsets for ``ids`` (canonical order), or all groups when ``ids`` is None.

    Raises ValueError on an unknown id so a typo in the ``--tools`` selection fails loudly rather
    than silently running with an unexpected tool set.
    """
    if ids is None:
        return list(_GATED_BY_ID.values())
    unknown = [i for i in ids if i not in _GATED_BY_ID]
    if unknown:
        known = ", ".join(TOOLSET_IDS)
        raise ValueError(f"unknown toolset id(s): {', '.join(unknown)}; known ids: {known}")
    chosen = set(ids)
    return [_GATED_BY_ID[i] for i in TOOLSET_IDS if i in chosen]


def tool_names_for(ids: list[str] | None) -> list[str]:
    """Effective tool names exposed by the selected groups, in canonical order (for recording)."""
    chosen = set(TOOLSET_IDS if ids is None else ids)
    return [name for i in TOOLSET_IDS if i in chosen for name in _BASE_BY_ID[i].tools]


def all_tool_definitions() -> list[ToolDefinition]:
    """Every tool's definition, from the ungated toolsets in attach order (deterministic)."""
    return [tool.tool_def for toolset in _BASE_TOOLSETS for tool in toolset.tools.values()]
