from collections import defaultdict, deque

from app.common.exceptions import CircularDependencyError
from core.schemas import ResolvedAgentTask


def resolve_stages(tasks: list[ResolvedAgentTask]) -> list[list[ResolvedAgentTask]]:
    """
    Kahn's algorithm: returns tasks grouped into parallel execution stages.
    Tasks within a stage have no dependencies on each other — safe to gather().
    Raises CircularDependencyError if a cycle exists.
    """
    task_map = {t.agent_id: t for t in tasks}
    in_degree: dict[str, int] = {t.agent_id: 0 for t in tasks}
    dependents: dict[str, list[str]] = defaultdict(list)

    for task in tasks:
        for dep in task.depends_on:
            if dep not in task_map:
                continue
            in_degree[task.agent_id] += 1
            dependents[dep].append(task.agent_id)

    queue = deque(agent_id for agent_id, deg in in_degree.items() if deg == 0)
    stages: list[list[ResolvedAgentTask]] = []
    visited = 0

    while queue:
        stage_ids = list(queue)
        queue.clear()
        stages.append([task_map[aid] for aid in stage_ids])
        visited += len(stage_ids)

        next_level: list[str] = []
        for aid in stage_ids:
            for dependent_id in dependents[aid]:
                in_degree[dependent_id] -= 1
                if in_degree[dependent_id] == 0:
                    next_level.append(dependent_id)
        queue.extend(next_level)

    if visited != len(tasks):
        raise CircularDependencyError()

    return stages
