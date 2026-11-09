"""Task 2 - rank existing recipes against the ingredients the user has (no recipe is ever invented)."""
from __future__ import annotations

from dataclasses import dataclass, field

from . import ingredients as ing
from .store import Filter, Store, _ESC


@dataclass
class Have:
    name: str                 # normalized ingredient name
    qty: float | None = None
    unit: str | None = None
    label: str = ""           # what the user typed


@dataclass(frozen=True)
class MatchSpec:
    have: tuple = ()          # tuple[Have]
    exclude: tuple = ()       # normalized names that must be absent (hard)
    excl_mask: int = 0        # dietary groups that must be absent (hard)
    max_steps: int | None = None   # hard
    prefer: str = "balanced"  # "balanced" | "missing" (fewest extra ingredients) | "steps" (fewest steps)
    min_matched: int = 1
    assume_pantry: bool = True  # salt / pepper / water are assumed available and not scored

    def describe_constraints(self) -> str:
        parts = []
        if self.exclude:
            parts.append("excluding " + ", ".join(ing.display(x) for x in self.exclude))
        if self.excl_mask:
            parts.append("free of " + ing.describe_groups(self.excl_mask))
        if self.max_steps is not None:
            parts.append(f"at most {self.max_steps} steps")
        return "; ".join(parts) or "none"




def _rows_for(store: Store, rids: list, cols: str, chunk: int = 500):
    for i in range(0, len(rids), chunk):
        part = rids[i:i + chunk]
        q = ",".join("?" * len(part))
        yield from store.db.execute(f"SELECT {cols} FROM recipes WHERE rid IN ({q})", part)


def _qty_status(have: Have, line: dict) -> str:
    if have.qty is None:
        return "quantity not verified (none given)"
    a, b = ing.convert(have.qty, have.unit), ing.convert(line["qty"], line["unit"])
    if a is None or b is None or a[0] != b[0]:
        return "quantity not verified (units not comparable)"
    have_s = f"{have.qty:.3g}{(' ' + have.unit) if have.unit else ''}"
    need_s = f"{line['qty']:.3g}{(' ' + line['unit']) if line['unit'] else ''}"
    cooked = (" - caution: the recipe amount is of COOKED food; compare with your cooked amount"
              if "cooked" in line["raw"].lower() and "uncooked" not in line["raw"].lower() else "")
    if a[1] + 1e-9 >= b[1]:
        return f"enough (have {have_s}, recipe needs {need_s}){cooked}"
    return f"NOT enough (have {have_s}, recipe needs {need_s}){cooked}"


def _hit_sets(store: Store, spec: MatchSpec, eligible_where: str, eligible_args: list):
    """rid -> {have_index -> set(line idx)} for non-staple lines of eligible recipes."""
    db = store.db
    db.execute("DROP TABLE IF EXISTS temp.elig")
    db.execute("CREATE TEMP TABLE elig(rid INTEGER PRIMARY KEY)")
    db.execute(f"INSERT INTO temp.elig SELECT r.rid FROM recipes r WHERE {eligible_where}", eligible_args)
    hits: dict[int, dict[int, set]] = {}
    for hi, h in enumerate(spec.have):
        pats = ing.sql_like_patterns(h.name)
        cond = " OR ".join(f"alts LIKE ?{_ESC}" for _ in pats)
        sql = f"SELECT i.rid, i.idx FROM ing i JOIN temp.elig e ON e.rid = i.rid WHERE i.staple=0 AND ({cond})"
        for rid, idx in db.execute(sql, pats):
            hits.setdefault(rid, {}).setdefault(hi, set()).add(idx)
    return hits


def _rank_key(prefer: str, matched_terms: int, missing: float, steps: int):
    if prefer == "steps":
        return (-matched_terms, steps, missing)
    if prefer == "missing":
        return (-matched_terms, missing, steps)
    return (-matched_terms, missing + 0.25 * steps, steps)


def _score(spec: MatchSpec, n_core: int, n_steps: int, hit: dict) -> tuple[tuple, dict]:
    matched_terms = len(hit)
    lines = set().union(*hit.values()) if hit else set()
    matched_core = len(lines)
    missing = max(0, n_core - matched_core)
    key = _rank_key(spec.prefer, matched_terms, missing, n_steps)
    return key, {"user_cov": matched_terms / max(1, len(spec.have)), "recipe_cov": matched_core / max(1, n_core),
                 "matched_terms": matched_terms, "matched_lines": matched_core, "missing_n": missing}


def _detail(store: Store, spec: MatchSpec, rid: int, hit: dict, parts: dict) -> dict:
    rec = store.recipe(rid)
    matched_idx = set().union(*hit.values()) if hit else set()
    lines = rec["ingredients"]
    matched = []
    for hi, idxs in hit.items():
        for i in sorted(idxs):
            matched.append({"have": spec.have[hi].label or spec.have[hi].name, "line": lines[i]["raw"],
                            "qty_check": _qty_status(spec.have[hi], lines[i])})
    missing = [l["raw"] for i, l in enumerate(lines) if i not in matched_idx and not l["staple"] and l["name"]]
    staples = [l["raw"] for l in lines if l["staple"]]
    unmatched_terms = [spec.have[i].label or spec.have[i].name for i in range(len(spec.have)) if i not in hit]
    return {"rid": rid, "id": rec["id"], "title": rec["title"], "n_steps": rec["n_steps"], **parts,
            "matched": matched, "missing": missing, "staples": staples, "unused": unmatched_terms,
            "flags": rec["flags"], "uflags": rec["uflags"], "recipe": rec}


def match(store: Store, spec: MatchSpec, limit: int = 5) -> dict:
    hard = Filter(exclude=spec.exclude, excl_mask=spec.excl_mask, max_steps=spec.max_steps)
    wh, args = store.where(hard, dedupe=True)
    hits = _hit_sets(store, spec, wh, args)
    cands = [(rid, h) for rid, h in hits.items() if len(h) >= spec.min_matched]
    info = {}
    for r in _rows_for(store, [c[0] for c in cands], "rid, n_core, n_steps, id"):
        info[r["rid"]] = (r["n_core"], r["n_steps"], r["id"])
    scored = []
    for rid, h in cands:
        n_core, n_steps, rid_s = info[rid]
        sc, parts = _score(spec, n_core, n_steps, h)
        scored.append((sc, rid_s, rid, h, parts))
    scored.sort(key=lambda t: (t[0], t[1]))
    results = [_detail(store, spec, t[2], t[3], t[4]) for t in scored[:limit]]
    out = {"eligible": len(scored), "results": results, "alternatives": []}
    if not results:
        out["alternatives"] = _alternatives(store, spec, limit=3)
    return out


def _alternatives(store: Store, spec: MatchSpec, limit: int = 3) -> list[dict]:
    """Closest recipes when NOTHING passes the hard constraints; each is labelled with what it violates."""
    relaxed = MatchSpec(have=spec.have, prefer=spec.prefer, min_matched=1)
    wh, args = store.where(Filter(), dedupe=True)
    hits = _hit_sets(store, relaxed, wh, args)
    if not hits:
        return []
    ranked = []
    info = {r["rid"]: r for r in _rows_for(store, list(hits), "rid, n_core, n_steps, id, flags")}
    for rid, h in hits.items():
        r = info[rid]
        sc, parts = _score(spec, r["n_core"], r["n_steps"], h)
        viol = 0
        if spec.max_steps is not None and r["n_steps"] > spec.max_steps:
            viol += 1
        if r["flags"] & spec.excl_mask:
            viol += 1
        ranked.append((viol, sc, r["id"], rid, h, parts))
    ranked.sort(key=lambda t: t[:3])
    out = []
    for viol, _, _, rid, h, parts in ranked:
        d = _detail(store, relaxed, rid, h, parts)
        v = []
        if spec.max_steps is not None and d["n_steps"] > spec.max_steps:
            v.append(f"has {d['n_steps']} steps (limit {spec.max_steps})")
        bad = d["flags"] & spec.excl_mask
        if bad:
            v.append("contains " + ing.describe_groups(bad))
        for ex in spec.exclude:
            for l in d["recipe"]["ingredients"]:
                if any(ing.name_matches(ex, a) for a in l["alts"]):
                    v.append(f"contains excluded '{ing.display(ex)}' ({l['raw']})")
                    break
        if not v:
            continue  # it satisfies everything -> would have been an eligible result
        d["violations"] = v
        out.append(d)
        if len(out) >= limit:
            break
    return out
