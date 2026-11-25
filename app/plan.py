"""Read-only version switch (换版) planning.

A downstream data warehouse keeps a small cache of decode rules and replaces
them one at a time while it keeps importing weigh bills.  Given the old and
the new configuration version plus the cache capacity, this module produces an
ordered check-list of

* ``install`` — add one rule that exists only in the new version,
* ``remove``  — drop one rule that exists only in the old version,
* ``replace`` — do both in a single atomic step.

A *rule* is identified by ``(marker, prefix, scale range, weight range)``:
templates that agree on all four are the same cache entry and are counted
once.  Only rules unique to one version need a step; rules shared by both
versions stay in the cache for the whole migration.

Guarantees checked for every intermediate cache state:

1. the cache never holds more than ``capacity`` rules;
2. every code decodable by *both* versions stays decodable (the intersection
   of the two decodable code sets is always covered by installed rules);
3. every decodable code has a single ``(scale_no, net_weight_g)`` result,
   i.e. no two installed rules with the same marker conflict.

All three properties are decided analytically over the entire 9-digit code
space.  Prefix cylinders form a laminar family, so decodable-code sets reduce
to small prefix unions / intersections; ambiguity is decided by the exact
pairwise conflict analysis in :mod:`app.conflicts`.  Nothing enumerates codes.

The module never writes configuration; it only receives template snapshots.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from .conflicts import analyze_pair, prefixes_compatible
from .domain import MAX_TEMPLATES, TemplateSpec

MAX_UNIQUE_RULES = 8
MIN_CAPACITY = 1
MAX_CAPACITY = MAX_TEMPLATES


class PlanError(Exception):
    """A version switch plan cannot be produced."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        reason: str,
        status: int = 409,
        details: Optional[dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.reason = reason
        self.status = status
        payload = dict(details or {})
        payload.setdefault("reason", reason)
        self.details = payload


# ---------------------------------------------------------------------------
# rules
# ---------------------------------------------------------------------------

RuleKey = tuple[str, str, int, int, int, int]


def rule_key(t: TemplateSpec) -> RuleKey:
    """Identity of a cache rule: marker + prefix + both extraction ranges."""
    marker = "" if t.marker is None else t.marker
    return (
        marker,
        t.prefix,
        t.scale.start,
        t.scale.length,
        t.weight.start,
        t.weight.length,
    )


def rule_id(key: RuleKey) -> str:
    marker, prefix, ss, sl, ws, wl = key
    return f"{marker}:{prefix or '-'}|{ss}+{sl}|{ws}+{wl}"


@dataclass
class Rule:
    key: RuleKey
    template: TemplateSpec
    names: list[str] = field(default_factory=list)
    old_index: Optional[int] = None
    new_index: Optional[int] = None

    @property
    def marker(self) -> Optional[str]:
        return self.template.marker

    @property
    def prefix(self) -> str:
        return self.template.prefix

    @property
    def label(self) -> str:
        return self.names[0] if self.names else rule_id(self.key)


def rule_to_dict(rule: Rule, index: Optional[int] = None) -> dict[str, Any]:
    t = rule.template
    marker = t.marker
    return {
        "rule_id": rule_id(rule.key),
        "key": rule_id(rule.key),
        "identity": list(rule.key),
        "index": index,
        "name": rule.names[0] if rule.names else "",
        "names": list(rule.names),
        "factory": rule.names[0] if rule.names else "",
        "marker": marker,
        "mark": marker,
        "tag": marker,
        "digit": int(marker) if marker is not None else None,
        "marker_digit": int(marker) if marker is not None else None,
        "prefix": t.prefix,
        "body_prefix": t.prefix,
        "scale": t.scale.to_dict(),
        "scale_range": [t.scale.start, t.scale.end],
        "scale_span": [t.scale.start, t.scale.length],
        "scale_start": t.scale.start,
        "scale_length": t.scale.length,
        "scale_end": t.scale.end,
        "weight": t.weight.to_dict(),
        "net_weight": t.weight.to_dict(),
        "weight_range": [t.weight.start, t.weight.end],
        "weight_span": [t.weight.start, t.weight.length],
        "weight_start": t.weight.start,
        "weight_length": t.weight.length,
        "weight_end": t.weight.end,
        "net_weight_start": t.weight.start,
        "net_weight_length": t.weight.length,
        "old_index": rule.old_index,
        "new_index": rule.new_index,
    }


def _collect_unique(
    templates: Iterable[TemplateSpec], side: str
) -> tuple[list[Rule], dict[RuleKey, Rule]]:
    rules: list[Rule] = []
    by_key: dict[RuleKey, Rule] = {}
    for i, t in enumerate(templates):
        key = rule_key(t)
        existing = by_key.get(key)
        if existing is None:
            rule = Rule(
                key=key,
                template=t,
                names=[t.name] if t.name else [],
                old_index=i if side == "old" else None,
                new_index=i if side == "new" else None,
            )
            by_key[key] = rule
            rules.append(rule)
        elif t.name and t.name not in existing.names:
            existing.names.append(t.name)
    return rules, by_key


# ---------------------------------------------------------------------------
# exact, code-space-free set predicates
# ---------------------------------------------------------------------------


def decodable_intersection(
    old_rules: list[Rule], new_rules: list[Rule]
) -> dict[str, set[str]]:
    """Prefix representation of codes decodable by BOTH configurations.

    For every marker, return a minimal set of prefixes whose cylinders are
    exactly the intersection of the two decodable sets.  Prefix cylinders are
    laminar (nested or disjoint), so unions/intersections collapse to a set of
    minimal prefixes.
    """
    per_marker: dict[str, dict[str, set[str]]] = {}
    for r in old_rules:
        if r.marker is None:
            continue
        per_marker.setdefault(r.marker, {"old": set(), "new": set()})["old"].add(
            r.prefix
        )
    for r in new_rules:
        if r.marker is None:
            continue
        per_marker.setdefault(r.marker, {"old": set(), "new": set()})["new"].add(
            r.prefix
        )

    result: dict[str, set[str]] = {}
    for marker, sides in per_marker.items():
        old_prefixes, new_prefixes = sides["old"], sides["new"]
        if not old_prefixes or not new_prefixes:
            continue
        candidates: set[str] = set()
        for p in old_prefixes:
            for q in new_prefixes:
                if prefixes_compatible(p, q):
                    candidates.add(p if len(p) >= len(q) else q)
        if not candidates:
            continue
        # Cylinder(longer) is a subset of cylinder(shorter): keep the shortest
        # prefixes, i.e. drop any prefix that extends another one.
        minimal = {
            p
            for p in candidates
            if not any(other != p and p.startswith(other) for other in candidates)
        }
        if minimal:
            result[marker] = minimal
    return result


def intersection_to_dict(intersection: dict[str, set[str]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for marker in sorted(intersection):
        for prefix in sorted(intersection[marker]):
            out.append(
                {
                    "marker": marker,
                    "prefix": prefix,
                    "code_prefix": marker + prefix,
                }
            )
    return out


def version_conflicts(rules: list[Rule]) -> list[dict[str, Any]]:
    """Same-marker ambiguous rule pairs inside one version."""
    out: list[dict[str, Any]] = []
    for i in range(len(rules)):
        for j in range(i + 1, len(rules)):
            a, b = rules[i], rules[j]
            if a.marker != b.marker or a.marker is None:
                continue
            analysis = analyze_pair(a.template, b.template)
            if not analysis.conflict:
                continue
            out.append(
                {
                    "marker": a.marker,
                    "a": rule_to_dict(a),
                    "b": rule_to_dict(b),
                    "templates": [a.label, b.label],
                    "reason": analysis.reason,
                    "detail": analysis.to_dict(),
                }
            )
    return out


# ---------------------------------------------------------------------------
# planning
# ---------------------------------------------------------------------------


def _validate_structure(
    old_rules: list[Rule],
    new_rules: list[Rule],
    old_keys: list[RuleKey],
    new_keys: list[RuleKey],
    old_only: list[RuleKey],
    new_only: list[RuleKey],
    capacity: int,
    old_version: Optional[int],
    new_version: Optional[int],
) -> None:
    # Error precedence: rule limit -> decode conflicts -> capacity.
    unique_total = len(old_only) + len(new_only)
    if unique_total > MAX_UNIQUE_RULES:
        raise PlanError(
            "RULE_LIMIT_EXCEEDED",
            f"两版各自独有的规则合计 {unique_total} 条"
            f"（旧版独有 {len(old_only)} + 新版独有 {len(new_only)}），"
            f"超过上限 {MAX_UNIQUE_RULES} 条",
            reason="unique_rule_limit_exceeded",
            details={
                "unique_rule_total": unique_total,
                "max_unique_rules": MAX_UNIQUE_RULES,
                "old_only_count": len(old_only),
                "new_only_count": len(new_only),
                "old_version": old_version,
                "new_version": new_version,
            },
        )

    old_conflicts = version_conflicts(old_rules)
    new_conflicts = version_conflicts(new_rules)
    if old_conflicts or new_conflicts:
        versions: dict[str, Any] = {}
        parts: list[str] = []
        if old_version is not None:
            versions["old"] = {
                "version": old_version,
                "conflict_count": len(old_conflicts),
                "conflicts": old_conflicts,
            }
        if new_version is not None:
            versions["new"] = {
                "version": new_version,
                "conflict_count": len(new_conflicts),
                "conflicts": new_conflicts,
            }
        if old_conflicts:
            parts.append(f"旧配置版号 {old_version} 有 {len(old_conflicts)} 处解码冲突")
        if new_conflicts:
            parts.append(f"新配置版号 {new_version} 有 {len(new_conflicts)} 处解码冲突")
        raise PlanError(
            "DECODE_CONFLICT",
            "；".join(parts)
            + "（同一标记下存在相同正文解出不同秤号或净重的模板，任何换装顺序都无法保证唯一解码）",
            reason="decode_conflict",
            details={
                "versions": versions,
                "old_version": old_version,
                "new_version": new_version,
                "old_conflict_count": len(old_conflicts),
                "new_conflict_count": len(new_conflicts),
                "conflict_count": len(old_conflicts) + len(new_conflicts),
            },
        )

    base = {
        "capacity": capacity,
        "old_version": old_version,
        "new_version": new_version,
        "old_rule_count": len(old_keys),
        "new_rule_count": len(new_keys),
        "shared_rule_count": len(set(old_keys) & set(new_keys)),
        "old_only_count": len(old_only),
        "new_only_count": len(new_only),
        "unique_rule_total": unique_total,
    }
    if len(old_keys) > capacity:
        raise PlanError(
            "NO_FEASIBLE_ORDER",
            f"旧版有 {len(old_keys)} 条规则，初始缓存已超过容量 {capacity} 条",
            reason="capacity_too_small",
            details={"detail": "initial_cache_exceeds_capacity", **base},
        )
    if len(new_keys) > capacity:
        raise PlanError(
            "NO_FEASIBLE_ORDER",
            f"新版有 {len(new_keys)} 条规则，目标缓存超过容量 {capacity} 条",
            reason="capacity_too_small",
            details={"detail": "final_cache_exceeds_capacity", **base},
        )


def plan_migration(
    old_templates: list[TemplateSpec],
    new_templates: list[TemplateSpec],
    capacity: int,
    *,
    old_version: Optional[int] = None,
    new_version: Optional[int] = None,
) -> dict[str, Any]:
    """Compute an ordered switch plan or raise :class:`PlanError`."""
    if not isinstance(capacity, int) or isinstance(capacity, bool):
        raise PlanError(
            "INVALID_CAPACITY",
            "缓存容量必须是 1～12 的整数",
            reason="invalid_capacity",
            status=422,
            details={"capacity": capacity},
        )
    if not (MIN_CAPACITY <= capacity <= MAX_CAPACITY):
        raise PlanError(
            "INVALID_CAPACITY",
            f"缓存容量 {capacity} 超出范围 {MIN_CAPACITY}～{MAX_CAPACITY} 条",
            reason="invalid_capacity",
            status=422,
            details={
                "capacity": capacity,
                "min_capacity": MIN_CAPACITY,
                "max_capacity": MAX_CAPACITY,
            },
        )

    old_rules, old_by = _collect_unique(old_templates, "old")
    new_rules, new_by = _collect_unique(new_templates, "new")

    registry: dict[RuleKey, Rule] = dict(old_by)
    for key, rule in new_by.items():
        if key in old_by:
            shared = old_by[key]
            shared.new_index = rule.new_index
            for name in rule.names:
                if name not in shared.names:
                    shared.names.append(name)
        else:
            registry[key] = rule

    old_keys = [r.key for r in old_rules]
    new_keys = [r.key for r in new_rules]
    old_set, new_set = set(old_keys), set(new_keys)
    shared_keys = [k for k in old_keys if k in new_set]
    old_only = [k for k in old_keys if k not in new_set]
    new_only = [k for k in new_keys if k not in old_set]

    _validate_structure(
        old_rules,
        new_rules,
        old_keys,
        new_keys,
        old_only,
        new_only,
        capacity,
        old_version,
        new_version,
    )

    # ---- bit assignment over all rules --------------------------------
    all_keys = list(dict.fromkeys(old_keys + new_keys))
    bit_of = {key: i for i, key in enumerate(all_keys)}
    shared_mask = 0
    for key in shared_keys:
        shared_mask |= 1 << bit_of[key]
    old_bits = [1 << bit_of[k] for k in old_only]
    new_bits = [1 << bit_of[k] for k in new_only]

    # Conflict edges (same marker, exact analysis) between any two rules.
    conflict_edges: list[tuple[int, int]] = []
    for i in range(len(all_keys)):
        a = registry[all_keys[i]].template
        if a.marker is None:
            continue
        for j in range(i + 1, len(all_keys)):
            b = registry[all_keys[j]].template
            if a.marker != b.marker:
                continue
            if analyze_pair(a, b).conflict:
                conflict_edges.append((1 << i, 1 << j))

    # Coverage masks: for each shared-decodable cylinder, the set of rules
    # (same marker, prefix is a prefix of it) able to decode it.
    intersection = decodable_intersection(old_rules, new_rules)
    cover_masks: list[int] = []
    for marker, prefixes in intersection.items():
        for prefix in prefixes:
            mask = 0
            for i, key in enumerate(all_keys):
                if key[0] == marker and prefix.startswith(key[1]):
                    mask |= 1 << i
            if mask == 0:  # pragma: no cover - defensive
                raise PlanError(
                    "NO_FEASIBLE_ORDER",
                    "共同可解码码无法被任何规则覆盖",
                    reason="no_feasible_order",
                    details={"marker": marker, "prefix": prefix},
                )
            cover_masks.append(mask)

    p, q = len(old_only), len(new_only)

    def installed_mask(rmask: int, amask: int) -> int:
        mask = shared_mask
        for i in range(p):
            if rmask & (1 << i):
                mask |= old_bits[i]
        for j in range(q):
            if amask & (1 << j):
                mask |= new_bits[j]
        return mask

    def check(mask: int) -> dict[str, bool]:
        capacity_ok = mask.bit_count() <= capacity
        unique_ok = all(not (mask & a and mask & b) for a, b in conflict_edges)
        shared_ok = all(mask & cm for cm in cover_masks)
        return {
            "capacity_ok": capacity_ok,
            "fits": capacity_ok,
            "shared_decodable_ok": shared_ok,
            "unique_decode_ok": unique_ok,
            "valid": capacity_ok and unique_ok and shared_ok,
        }

    valid_cache: dict[tuple[int, int], bool] = {}

    def state_valid(rmask: int, amask: int) -> bool:
        cached = valid_cache.get((rmask, amask))
        if cached is None:
            cached = check(installed_mask(rmask, amask))["valid"]
            valid_cache[(rmask, amask)] = cached
        return cached

    start = ((1 << p) - 1, 0)
    goal = (0, (1 << q) - 1)

    if not state_valid(*start) or not state_valid(*goal):  # pragma: no cover
        raise PlanError(
            "NO_FEASIBLE_ORDER",
            f"容量 {capacity} 条无法容纳迁移过程中的缓存，不存在可行换装顺序",
            reason="no_feasible_order",
            details={
                "detail": "endpoint_state_invalid",
                "capacity": capacity,
                "old_version": old_version,
                "new_version": new_version,
            },
        )

    # Breadth-first search over (removed old-only, installed new-only) masks.
    # Every move clears/installs one bit, so the state graph is acyclic and
    # BFS yields a plan with the fewest steps.  Replacements are expanded
    # first for a deterministic tie-break among equally short plans.
    Move = tuple[str, int, int]
    parent: dict[tuple[int, int], Optional[tuple[tuple[int, int], Move]]] = {
        start: None
    }
    queue: deque[tuple[int, int]] = deque([start])
    found = (start == goal)
    while queue and not found:
        rmask, amask = queue.popleft()
        successors: list[tuple[tuple[int, int], Move]] = []
        for i in range(p):
            if not (rmask & (1 << i)):
                continue
            for j in range(q):
                if amask & (1 << j):
                    continue
                successors.append(
                    ((rmask & ~(1 << i), amask | (1 << j)), ("replace", i, j))
                )
        for j in range(q):
            if not (amask & (1 << j)):
                successors.append(((rmask, amask | (1 << j)), ("install", -1, j)))
        for i in range(p):
            if rmask & (1 << i):
                successors.append(((rmask & ~(1 << i), amask), ("remove", i, -1)))

        for nxt, move in successors:
            if nxt in parent:
                continue
            if not state_valid(*nxt):
                continue
            parent[nxt] = ((rmask, amask), move)
            if nxt == goal:
                found = True
                break
            queue.append(nxt)

    if not found:
        raise PlanError(
            "NO_FEASIBLE_ORDER",
            "不存在可行换装顺序：无法同时满足每一步装得下、两版共同可解码码始终可解、"
            "且任意可解码只有一组秤号和净重",
            reason="no_feasible_order",
            details={
                "detail": "no_ordering_satisfies_capacity_coverage_and_uniqueness",
                "capacity": capacity,
                "old_version": old_version,
                "new_version": new_version,
                "old_only_count": len(old_only),
                "new_only_count": len(new_only),
                "shared_decodable": intersection_to_dict(intersection),
            },
        )

    # ---- reconstruct ---------------------------------------------------
    states: list[tuple[int, int]] = [goal]
    moves: list[Move] = []
    cur = goal
    while parent[cur] is not None:
        prev, move = parent[cur]  # type: ignore[misc]
        moves.append(move)
        states.append(prev)
        cur = prev
    moves.reverse()
    states.reverse()

    def key_list(rmask: int, amask: int) -> list[RuleKey]:
        keys = list(shared_keys)
        keys.extend(k for i, k in enumerate(old_only) if rmask & (1 << i))
        keys.extend(k for j, k in enumerate(new_only) if amask & (1 << j))
        return keys

    steps: list[dict[str, Any]] = []
    for idx, (move, before, after) in enumerate(zip(moves, states, states[1:])):
        kind, i, j = move
        after_mask = installed_mask(*after)
        checks = check(after_mask)
        install_rule = (
            rule_to_dict(registry[new_only[j]])
            if kind in ("install", "replace")
            else None
        )
        remove_rule = (
            rule_to_dict(registry[old_only[i]])
            if kind in ("remove", "replace")
            else None
        )
        name_in = (install_rule or {}).get("name") or (install_rule or {}).get("rule_id")
        name_out = (remove_rule or {}).get("name") or (remove_rule or {}).get("rule_id")
        if kind == "install":
            description = f"装入新版独有规则 {name_in}"
        elif kind == "remove":
            description = f"卸下旧版独有规则 {name_out}"
        else:
            description = f"以新版独有规则 {name_in} 替换旧版独有规则 {name_out}"
        after_keys = key_list(*after)
        before_keys = key_list(*before)
        primary = install_rule or remove_rule or {}
        steps.append(
            {
                "step": idx + 1,
                "index": idx,
                "order": idx + 1,
                "action": kind,
                "op": kind,
                "type": kind,
                "operation": kind,
                "install": install_rule,
                "add": install_rule,
                "new_rule": install_rule,
                "incoming": install_rule,
                "load": install_rule,
                "remove": remove_rule,
                "uninstall": remove_rule,
                "old_rule": remove_rule,
                "outgoing": remove_rule,
                "unload": remove_rule,
                "rule": primary,
                "template": primary,
                "marker": primary.get("marker"),
                "size_before": len(before_keys),
                "count_before": len(before_keys),
                "size_after": len(after_keys),
                "count_after": len(after_keys),
                "result_size": len(after_keys),
                "capacity": capacity,
                "fits": checks["fits"],
                "within_capacity": checks["fits"],
                "capacity_ok": checks["fits"],
                "checks": checks,
                "cache_after": [
                    rule_to_dict(registry[key], position)
                    for position, key in enumerate(after_keys)
                ],
                "cache_after_keys": [rule_id(key) for key in after_keys],
                "description": description,
                "说明": description,
            }
        )

    initial_keys = key_list(*start)
    final_keys = key_list(*goal)

    plan: dict[str, Any] = {
        "read_only": True,
        "config_modified": False,
        "feasible": True,
        "old_version": old_version,
        "new_version": new_version,
        "capacity": capacity,
        "min_capacity": MIN_CAPACITY,
        "max_capacity": MAX_CAPACITY,
        "old_rule_count": len(old_keys),
        "new_rule_count": len(new_keys),
        "old_template_count": len(old_templates),
        "new_template_count": len(new_templates),
        "shared_rule_count": len(shared_keys),
        "old_only_count": len(old_only),
        "new_only_count": len(new_only),
        "unique_rule_total": len(old_only) + len(new_only),
        "max_unique_rules": MAX_UNIQUE_RULES,
        "initial_size": len(initial_keys),
        "final_size": len(final_keys),
        "step_count": len(steps),
        "min_steps": len(steps),
        "steps": steps,
        "shared_rules": [
            rule_to_dict(registry[key], index)
            for index, key in enumerate(shared_keys)
        ],
        "old_only_rules": [
            rule_to_dict(registry[key], index)
            for index, key in enumerate(old_only)
        ],
        "new_only_rules": [
            rule_to_dict(registry[key], index)
            for index, key in enumerate(new_only)
        ],
        "initial_cache": [
            rule_to_dict(registry[key], index)
            for index, key in enumerate(initial_keys)
        ],
        "final_cache": [
            rule_to_dict(registry[key], index)
            for index, key in enumerate(final_keys)
        ],
        "initial_cache_keys": [rule_id(key) for key in initial_keys],
        "final_cache_keys": [rule_id(key) for key in final_keys],
        "shared_decodable": intersection_to_dict(intersection),
        "guarantees": {
            "capacity_respected_every_step": True,
            "shared_decodable_codes_always_decodable": True,
            "unique_decode_every_step": True,
            "checked_over_entire_code_space": True,
            "brute_force_used": False,
        },
    }
    # Convenient aliases for the ordered check-list.
    plan["plan"] = steps
    plan["schedule"] = steps
    plan["operations"] = steps
    plan["checklist"] = steps
    plan["换装清单"] = steps
    return plan
