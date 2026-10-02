"""Placing similar jobs together in the job list: the same role in one country, and the
same ad in several countries (ingest/similar.py finds them)."""
from __future__ import annotations


def _sets(jobs: list[dict], fields: tuple[str, ...]) -> dict[int, int]:
    """{job id: id of its set} for jobs linked (directly or via others) through the given
    lists of similar jobs, counting only jobs in this list."""
    parent = {job["id"]: job["id"] for job in jobs}

    def root(pk: int) -> int:
        while parent[pk] != pk:
            parent[pk] = parent[parent[pk]]
            pk = parent[pk]
        return pk

    for job in jobs:
        for field in fields:
            for other in job[field]:
                if other["id"] in parent:
                    parent[root(other["id"])] = root(job["id"])
    return {pk: root(pk) for pk in parent}


def group_duplicates(jobs: list[dict]) -> list[dict]:
    """Place similar jobs next to each other, at the place of the best-ranked one,
    keeping the sort order otherwise: similar jobs, and the same ad in other
    countries. Each set of similar jobs stays together and its members get the
    same "group" number (the first member's id), else None. Jobs joined by the same ad in
    another country get the same "area" number (the first job's id), else None, so the
    page can mark the two relations differently."""
    near = _sets(jobs, ("duplicates", "elsewhere"))
    duplicate_set = _sets(jobs, ("duplicates",))
    members: dict[int, list[dict]] = {}
    for job in jobs:
        members.setdefault(duplicate_set[job["id"]], []).append(job)
    # A set of similar jobs always lies within one set of related jobs (any country).
    by_area: dict[int, list[dict]] = {}
    for job in jobs:
        by_area.setdefault(near[job["id"]], []).append(job)
    ordered, placed = [], set()
    for area_jobs in by_area.values():
        abroad = len({duplicate_set[job["id"]] for job in area_jobs}) > 1
        for job in area_jobs:
            job["area"] = area_jobs[0]["id"] if abroad else None
        for job in area_jobs:
            key = duplicate_set[job["id"]]
            if key in placed:
                continue
            placed.add(key)
            group = members[key]
            for member in group:
                member["group"] = group[0]["id"] if len(group) > 1 else None
            ordered.extend(group)
    return ordered
