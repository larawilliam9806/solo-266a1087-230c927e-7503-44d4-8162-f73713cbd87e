"""Read-only version switch (换版) plan: exactness, errors and read-only-ness."""

import itertools
import random

import pytest

from app.conflicts import conflicts
from app.domain import Range, TemplateSpec
from app.plan import (
    MAX_UNIQUE_RULES,
    PlanError,
    plan_migration,
    rule_key,
)


def tpl(marker, prefix, s=(2, 2), w=(4, 4), name=""):
    return TemplateSpec(
        name=name, marker=marker, prefix=prefix, scale=Range(*s), weight=Range(*w)
    )


# ---------------------------------------------------------------------------
# rule identity: marker + prefix + both ranges define one rule
# ---------------------------------------------------------------------------


def test_duplicate_templates_count_as_one_rule():
    old = [tpl("1", "10", name="a"), tpl("1", "10", name="b")]
    new = [tpl("1", "10", name="c")]
    plan = plan_migration(old, new, 1, old_version=1, new_version=2)
    assert plan["old_rule_count"] == 1
    assert plan["new_rule_count"] == 1
    assert plan["shared_rule_count"] == 1
    assert plan["step_count"] == 0
    assert plan["shared_rules"][0]["names"] == ["a", "b", "c"]


@pytest.mark.parametrize(
    "other",
    [
        tpl("2", "10"),               # different marker
        tpl("1", "11"),               # different prefix
        tpl("1", "10", s=(2, 3)),     # different scale range
        tpl("1", "10", w=(5, 3)),     # different weight range
    ],
)
def test_rule_identity_requires_all_four_fields(other):
    old = [tpl("1", "10")]
    plan = plan_migration(old, [other], 2, old_version=1, new_version=2)
    assert plan["shared_rule_count"] == 0
    assert plan["old_only_count"] == 1
    assert plan["new_only_count"] == 1
    assert plan["step_count"] == 1
    assert plan["steps"][0]["action"] == "replace"


# ---------------------------------------------------------------------------
# step kinds and ordering
# ---------------------------------------------------------------------------


def test_install_only_when_capacity_allows():
    old = [tpl("1", "10", name="a")]
    new = [tpl("1", "10", name="a"), tpl("1", "109", name="b")]
    plan = plan_migration(old, new, 2, old_version=1, new_version=2)
    assert plan["steps"][0]["action"] == "install"
    assert plan["steps"][0]["install"]["name"] == "b"
    assert plan["steps"][0]["remove"] is None
    assert plan["steps"][0]["size_after"] == 2


def test_remove_only():
    old = [tpl("1", "10", name="a"), tpl("2", "20", name="b")]
    new = [tpl("1", "10", name="a")]
    plan = plan_migration(old, new, 2, old_version=1, new_version=2)
    assert plan["steps"][0]["action"] == "remove"
    assert plan["steps"][0]["remove"]["name"] == "b"


def test_replace_combines_install_and_remove_at_full_capacity():
    old = [tpl("1", "10", name="a"), tpl("1", "109", name="b")]
    new = [tpl("1", "10", name="a"), tpl("1", "108", name="c")]
    plan = plan_migration(old, new, 2, old_version=1, new_version=2)
    assert plan["step_count"] == 1
    step = plan["steps"][0]
    assert step["action"] == "replace"
    assert step["remove"]["name"] == "b"
    assert step["install"]["name"] == "c"
    assert step["size_after"] <= step["capacity"]


def test_plan_is_minimal_and_ordered():
    # capacity 3: must drop+add before the second install fits.
    old = [tpl("1", "10", name="keep"), tpl("1", "105", name="drop")]
    new = [
        tpl("1", "10", name="keep"),
        tpl("1", "106", name="n1"),
        tpl("1", "107", name="n2"),
    ]
    plan = plan_migration(old, new, 3, old_version=1, new_version=2)
    assert plan["step_count"] == 2
    sizes = [s["size_after"] for s in plan["steps"]]
    assert sizes == [2, 3]
    assert plan["steps"][0]["action"] == "replace"
    assert plan["steps"][1]["action"] == "install"
    assert plan["final_cache_keys"] == [
        f"{t.marker}:{t.prefix or '-'}|{t.scale.start}+{t.scale.length}|"
        f"{t.weight.start}+{t.weight.length}"
        for t in new
    ]


# ---------------------------------------------------------------------------
# the three invariants
# ---------------------------------------------------------------------------


def _decode_equal(a: TemplateSpec, b: TemplateSpec) -> bool:
    return not conflicts(a, b)


def _state_ok(installed, shared_prefixes, capacity):
    """Capacity + unique decode for every body in the union of cylinders."""
    rules = {}
    for t in installed:
        rules.setdefault(rule_key(t), t)
    installed = list(rules.values())
    if len(installed) > capacity:
        return False
    # pairwise same-marker conflicts == ambiguity somewhere
    for i in range(len(installed)):
        for j in range(i + 1, len(installed)):
            a, b = installed[i], installed[j]
            if a.marker == b.marker and not _decode_equal(a, b):
                return False
    # every shared-decodable prefix cylinder is covered
    for marker, prefix in shared_prefixes:
        if not any(
            t.marker == marker and prefix.startswith(t.prefix) for t in installed
        ):
            return False
    return True


def test_shared_codes_stay_decodable_during_split():
    # old "1" -> new {"12","13"}: the shared decodable set is "12"∪"13".
    # A single replace would leave "1" without coverage, so the planner keeps
    # the wide rule until the split rules are installed.
    old = [tpl("1", "1", name="wide")]
    new = [tpl("1", "12", name="n12"), tpl("1", "13", name="n13")]

    plan = plan_migration(old, new, 2, old_version=1, new_version=2)
    assert plan["step_count"] == 2
    assert [s["action"] for s in plan["steps"]] == ["install", "replace"]
    for step in plan["steps"]:
        assert step["size_after"] <= 2

    # A direct replace would drop the only rule covering shared bodies, so the
    # first move must only add; by the second move both split prefixes exist,
    # so removing the wide rule is safe.
    assert plan["steps"][0]["remove"] is None
    assert "1:1|2+2|4+4" in plan["steps"][0]["cache_after_keys"]
    assert plan["steps"][1]["cache_after_keys"] == [
        "1:12|2+2|4+4",
        "1:13|2+2|4+4",
    ]


def test_insufficient_target_capacity_is_capacity_too_small():
    # The new version needs two slots but capacity is 1.
    old = [tpl("1", "1", name="wide")]
    new = [tpl("1", "12", name="n12"), tpl("1", "13", name="n13")]
    with pytest.raises(PlanError) as exc:
        plan_migration(old, new, 1, old_version=1, new_version=2)
    assert exc.value.code == "NO_FEASIBLE_ORDER"
    assert exc.value.details["reason"] == "capacity_too_small"
    assert exc.value.details["detail"] == "final_cache_exceeds_capacity"


def test_no_order_when_shared_coverage_and_uniqueness_cannot_both_hold():
    # Old rule "1" (scale length 2) conflicts with both new rules "12"/"13"
    # (scale length 3).  The shared decodable codes "12"&"13" must remain
    # decodable, but keeping the old rule alongside either new rule would make
    # decoding ambiguous.  No capacity can rescue this: it is a structural
    # incompatibility, not a capacity problem.
    old = [tpl("1", "1", s=(2, 2), name="wide")]
    new = [
        tpl("1", "12", s=(2, 3), name="n12"),
        tpl("1", "13", s=(2, 3), name="n13"),
    ]
    assert conflicts(old[0], new[0]) and conflicts(old[0], new[1])
    assert not conflicts(new[0], new[1])  # new version itself is clean
    for capacity in (2, 3, 4, 12):
        with pytest.raises(PlanError) as exc:
            plan_migration(old, new, capacity, old_version=1, new_version=2)
        assert exc.value.code == "NO_FEASIBLE_ORDER"
        assert exc.value.details["reason"] == "no_feasible_order"
        assert (
            exc.value.details["detail"]
            == "no_ordering_satisfies_capacity_coverage_and_uniqueness"
        )


def test_conflict_between_versions_but_not_inside_either_is_schedulable():
    # old rule and new rule conflict (same marker, different weight range) but
    # they are never installed together if capacity forces a replacement.
    old = [tpl("1", "10", w=(4, 4), name="a")]
    new = [tpl("1", "10", w=(3, 4), name="b")]
    assert conflicts(old[0], new[0])
    plan = plan_migration(old, new, 1, old_version=1, new_version=2)
    assert plan["step_count"] == 1
    assert plan["steps"][0]["action"] == "replace"


# ---------------------------------------------------------------------------
# error reasons
# ---------------------------------------------------------------------------


def test_old_version_decode_conflict_is_reported():
    old = [
        tpl("1", "10", s=(2, 2), name="a"),
        tpl("1", "10", s=(2, 3), name="b"),
    ]
    assert conflicts(old[0], old[1])
    with pytest.raises(PlanError) as exc:
        plan_migration(old, [tpl("1", "10", name="c")], 4, old_version=3, new_version=4)
    assert exc.value.code == "DECODE_CONFLICT"
    assert exc.value.details["versions"]["old"]["conflict_count"] == 1
    assert exc.value.details["versions"]["new"]["conflict_count"] == 0


def test_new_version_decode_conflict_is_reported():
    new = [
        tpl("2", "20", s=(2, 2), name="a"),
        tpl("2", "20", s=(2, 3), name="b"),
    ]
    with pytest.raises(PlanError) as exc:
        plan_migration([], new, 4, old_version=1, new_version=2)
    assert exc.value.code == "DECODE_CONFLICT"
    assert exc.value.details["old_conflict_count"] == 0
    assert exc.value.details["new_conflict_count"] == 1


def test_unique_rule_limit():
    old = [tpl(str(i % 10), str(i), name=f"o{i}") for i in range(5)]
    new = [tpl(str(i % 10), str(10 + i), name=f"n{i}") for i in range(4)]
    with pytest.raises(PlanError) as exc:
        plan_migration(old, new, 12, old_version=1, new_version=2)
    assert exc.value.code == "RULE_LIMIT_EXCEEDED"
    assert exc.value.details["unique_rule_total"] == 9
    assert exc.value.details["max_unique_rules"] == MAX_UNIQUE_RULES


def test_unique_rule_limit_boundary_of_eight_is_allowed():
    old = [tpl(str(i % 10), str(i), name=f"o{i}") for i in range(4)]
    new = [tpl(str(i % 10), str(10 + i), name=f"n{i}") for i in range(4)]
    plan = plan_migration(old, new, 8, old_version=1, new_version=2)
    assert plan["unique_rule_total"] == 8
    assert plan["feasible"] is True


@pytest.mark.parametrize("capacity", [0, 13, -1, 100])
def test_capacity_out_of_range(capacity):
    with pytest.raises(PlanError) as exc:
        plan_migration([], [tpl("1", "10")], capacity, old_version=1, new_version=2)
    assert exc.value.code == "INVALID_CAPACITY"


def test_capacity_smaller_than_a_version_has_no_order():
    old = [tpl(str(i), str(i), name=f"o{i}") for i in range(3)]
    with pytest.raises(PlanError) as exc:
        plan_migration(old, old, 2, old_version=1, new_version=2)
    assert exc.value.code == "NO_FEASIBLE_ORDER"
    assert exc.value.details["reason"] == "capacity_too_small"


def test_no_feasible_order_when_new_rule_conflicts_with_shared_rule():
    shared = tpl("1", "10", s=(2, 2), name="shared")
    bad = tpl("1", "10", s=(2, 3), name="bad")
    assert conflicts(shared, bad)
    # `bad` is a new-only rule; it can never coexist with the shared rule.
    with pytest.raises(PlanError) as exc:
        plan_migration([shared], [shared, bad], 4, old_version=1, new_version=2)
    # ... because the *new version itself* would decode ambiguously.
    assert exc.value.code in ("DECODE_CONFLICT", "NO_FEASIBLE_ORDER")


# ---------------------------------------------------------------------------
# exact cross-check against a brute-force oracle
# ---------------------------------------------------------------------------


def _dedup(templates):
    out = {}
    for t in templates:
        out.setdefault(rule_key(t), t)
    return list(out.values())


def _rand_tpl(rng, marker):
    plen = rng.randint(5, 7)
    prefix = "".join(str(rng.randint(0, 9)) for _ in range(plen))

    def rng_range():
        start = rng.randint(0, 7)
        length = rng.randint(1, min(3, 8 - start))
        return Range(start, length)

    return TemplateSpec(prefix=prefix, scale=rng_range(), weight=rng_range(), marker=marker)


def _bodies_union(templates):
    prefixes = {t.prefix for t in templates}
    if not prefixes:
        return set()
    out = set()
    for p in prefixes:
        for combo in itertools.product("0123456789", repeat=8 - len(p)):
            out.add(p + "".join(combo))
    return out


def _oracle(old, new, capacity):
    """Return the minimum step count, or None if infeasible."""
    bodies = _bodies_union(old + new)
    keys = [(str(m), b) for m in range(10) for b in bodies]

    def decode(templates):
        out = {}
        for m, body in keys:
            cands = [t for t in templates if t.marker == m and t.matches(body)]
            if cands:
                out[(m, body)] = frozenset(t.decode_parts(body) for t in cands)
        return out

    old_map, new_map = decode(old), decode(new)
    shared_codes = set(old_map) & set(new_map)

    def state_ok(installed):
        if len(installed) > capacity:
            return False
        dm = {}
        for m, body in keys:
            cands = [t for t in installed if t.marker == m and t.matches(body)]
            if cands:
                dec = frozenset(t.decode_parts(body) for t in cands)
                if len(dec) > 1:
                    return False
                dm[(m, body)] = dec
        return all(code in dm for code in shared_codes)

    reg = {rule_key(t): t for t in old + new}
    old_keys, new_keys = set(reg) & {rule_key(t) for t in old}, {
        rule_key(t) for t in new
    }
    shared_keys = old_keys & new_keys
    start, goal = frozenset(old_keys), frozenset(new_keys)
    if not state_ok([reg[k] for k in start]) or not state_ok([reg[k] for k in goal]):
        return None

    from collections import deque

    seen = {start}
    queue = deque([(start, 0)])
    while queue:
        state, depth = queue.popleft()
        if state == goal:
            return depth
        removable = state - shared_keys
        addable = new_keys - state
        successors = []
        for i in removable:
            for j in addable:
                successors.append((state - {i}) | {j})
        for j in addable:
            successors.append(state | {j})
        for i in removable:
            successors.append(state - {i})
        for nxt in successors:
            if nxt not in seen and state_ok([reg[k] for k in nxt]):
                seen.add(nxt)
                queue.append((nxt, depth + 1))
    return None


@pytest.mark.parametrize("seed", range(120))
def test_planner_matches_brute_force(seed):
    rng = random.Random(seed)
    old = _dedup(
        [_rand_tpl(rng, str(rng.randint(0, 9))) for _ in range(rng.randint(0, 3))]
    )
    new = _dedup(
        [_rand_tpl(rng, str(rng.randint(0, 9))) for _ in range(rng.randint(0, 3))]
    )
    capacity = rng.randint(1, 12)
    expected = _oracle(old, new, capacity)

    try:
        plan = plan_migration(old, new, capacity, old_version=1, new_version=2)
    except PlanError as exc:
        if exc.code == "RULE_LIMIT_EXCEEDED":
            return  # oracle does not apply the 8-rule input guard
        if exc.code == "DECODE_CONFLICT":
            assert expected is None, (seed, exc.code, expected)
            return
        assert exc.code == "NO_FEASIBLE_ORDER"
        assert expected is None, (seed, exc.code, expected)
        return

    assert expected is not None, (seed, "planner found a plan the oracle did not")
    assert plan["step_count"] == expected, (seed, plan["step_count"], expected)

    # Replay and verify every invariant independently.
    reg = {rule_key(t): t for t in old + new}
    bodies = _bodies_union(old + new)
    shared_codes = [
        (str(m), body)
        for m in range(10)
        for body in bodies
        if any(t.marker == str(m) and t.matches(body) for t in old)
        and any(t.marker == str(m) and t.matches(body) for t in new)
    ]
    cache = {rule_key(t) for t in old} & {rule_key(t) for t in new}
    for step in plan["steps"]:
        if step["remove"]:
            cache.discard(tuple(step["remove"]["identity"]))
        if step["install"]:
            cache.add(tuple(step["install"]["identity"]))
        installed = [reg[k] for k in cache]
        assert len(installed) <= capacity
        for i in range(len(installed)):
            for j in range(i + 1, len(installed)):
                if installed[i].marker == installed[j].marker:
                    assert not conflicts(installed[i], installed[j])
        for marker, body in shared_codes:
            assert any(
                t.marker == marker and t.matches(body) for t in installed
            ), (seed, marker, body)
    assert {rule_key(t) for t in new} == cache
