from __future__ import annotations

from typing import Any

RULES = {
    "proportional": "Proportional — the same percentage of each request",
    "urgency": "Urgency-first — earliest required start date, then submission time",
    "equal_share": "Equal-share (max-min) — small requests are filled, then the rest is shared",
}


def _largest_remainder(raw: dict[str, float], limit: int, requests: list[dict[str, Any]]) -> dict[str, int]:
    """Floor allocations and distribute only feasible leftover units deterministically."""
    result = {item["id"]: min(item["requested_m3"], int(raw.get(item["id"], 0))) for item in requests}
    remaining = max(0, limit - sum(result.values()))
    by_id = {item["id"]: item for item in requests}
    ranked = sorted(requests, key=lambda item: (-(raw.get(item["id"], 0) % 1), item["created_at"], item["id"]))
    while remaining:
        changed = False
        for item in ranked:
            key = item["id"]
            if result[key] < item["requested_m3"]:
                result[key] += 1
                remaining -= 1
                changed = True
                if not remaining:
                    break
        if not changed:
            break
    return result


def allocate(requests: list[dict[str, Any]], pool_m3: int, rule: str) -> dict[str, int]:
    if rule not in RULES:
        raise ValueError("Choose proportional, urgency, or equal_share allocation support.")
    ordered = sorted(requests, key=lambda item: (item["created_at"], item["id"]))
    pool = max(0, int(pool_m3))
    demand = sum(max(0, int(item["requested_m3"])) for item in ordered)
    if not ordered or pool == 0:
        return {item["id"]: 0 for item in ordered}
    if demand <= pool:
        return {item["id"]: int(item["requested_m3"]) for item in ordered}

    if rule == "proportional":
        raw = {item["id"]: item["requested_m3"] * pool / demand for item in ordered}
    elif rule == "urgency":
        raw, remaining = {}, pool
        for item in sorted(ordered, key=lambda x: (x["start_date"], x["created_at"], x["id"])):
            amount = min(int(item["requested_m3"]), remaining)
            raw[item["id"]] = float(amount)
            remaining -= amount
    else:
        # Water-filling: meet requests below the current equal share, redistribute
        # their unused portion, and repeat until demand or pool is exhausted.
        raw = {item["id"]: 0.0 for item in ordered}
        active = list(ordered)
        remaining = float(pool)
        while active and remaining > 0:
            share = remaining / len(active)
            below = [item for item in active if item["requested_m3"] - raw[item["id"]] <= share]
            if not below:
                for item in active:
                    raw[item["id"]] += share
                break
            for item in below:
                amount = item["requested_m3"] - raw[item["id"]]
                raw[item["id"]] += amount
                remaining -= amount
                active.remove(item)
    allocations = _largest_remainder(raw, pool, ordered)
    assert sum(allocations.values()) <= pool
    assert all(allocations[item["id"]] <= item["requested_m3"] for item in ordered)
    return allocations


def explain(rule: str, pool_m3: int, demand_m3: int, allocated_m3: int) -> str:
    label = RULES[rule].split(" — ")[0]
    return (f"{label} applied to {demand_m3:,} m³ requested against a declared "
            f"{pool_m3:,} m³ pool. The suggested total is {allocated_m3:,} m³. "
            "This is a policy choice for the manager to review, not an automated decision.")
