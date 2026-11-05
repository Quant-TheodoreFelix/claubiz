from .actions import ActionStore
from .knowledge import KnowledgeStore
from .workspace import Workspace


def derived_counts(ws: Workspace, run_id: str) -> tuple[int, int]:
    return KnowledgeStore(ws).count_run(run_id), ActionStore(ws).count_run(run_id)


def purge_derived(ws: Workspace, run_id: str) -> tuple[int, int]:
    return KnowledgeStore(ws).remove_run(run_id), ActionStore(ws).remove_run(run_id)
