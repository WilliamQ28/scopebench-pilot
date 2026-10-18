"""Agent construction and the package-level agent-harness agent.

A single, globally importable PydanticAI agent assembled from the harness
toolsets. Import it directly::

    from agent_harness import agent, default_deps, DeferredToolRequests

    result = agent.run_sync("list the files here", deps=default_deps())
    # result.output is a str, or a DeferredToolRequests when a tool needs the
    # caller (approval / ask_user) — see the README for the resume loop.

Use :func:`create_agent` with explicit :class:`~agent_harness.config.HarnessSettings`
when an application owns configuration. The package-level :data:`agent` is a
convenience instance built from ``HARNESS_*`` environment variables at import time.
"""

from pydantic_ai import AbstractToolset, Agent, DeferredToolRequests

from agent_harness.config import HarnessSettings
from agent_harness.deps import HarnessDeps
from agent_harness.toolsets import TOOLSETS


def create_agent(
    settings: HarnessSettings | None = None,
    *,
    toolsets: list[AbstractToolset[HarnessDeps]] | None = None,
) -> Agent[HarnessDeps, str | DeferredToolRequests]:
    """Assemble an independent harness agent from explicit settings.

    When ``settings`` is omitted, configuration is read from the current
    ``HARNESS_*`` environment. Supplying a settings object avoids import-time or
    process-global configuration and is preferred for applications and tests.

    ``toolsets`` overrides the attached toolsets (default: all of :data:`TOOLSETS`); pass a subset
    from :func:`agent_harness.select_toolsets` to restrict the agent's capabilities for an
    experiment arm.
    """
    resolved = HarnessSettings.from_env() if settings is None else settings
    return Agent(
        resolved.model,
        deps_type=HarnessDeps,
        output_type=[str, DeferredToolRequests],
        system_prompt=resolved.system_prompt,
        retries=resolved.retries,
        toolsets=TOOLSETS if toolsets is None else toolsets,
        defer_model_check=True,
    )


agent = create_agent()
