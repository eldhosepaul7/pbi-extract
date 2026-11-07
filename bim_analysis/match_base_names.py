#!/usr/bin/env python3
"""
match_base_names.py - Find the real Power BI measure names behind your business metric names.

Usage:
    python match_base_names.py --out out --base-metrics base_metrics.txt

Use this when analyze_model.py reports "Base metrics not found": your list uses business names
("Allowed Amount") but the model uses its own ("Total_Allowed_Amt", "_Allowed Amount").

Writes:
    out/base_name_matches.csv     top 5 candidate measures for each name in your list, with evidence
    base_metrics_suggested.txt    your list rewritten with the best match for each name;
                                  low-confidence lines are marked "# CHECK" for you to confirm

Review base_metrics_suggested.txt, fix any "# CHECK" lines using base_name_matches.csv,
save it as base_metrics.txt and rerun analyze_model.py. Standard library only.
"""
import argparse
import csv
import difflib
import json
import os
import re

SYNONYMS = {
    "amt": "amount", "amnt": "amount", "cnt": "count", "ct": "count", "avg": "average", "pct": "%",
    "percent": "%", "perc": "%", "mbr": "member", "mbrs": "member", "mem": "member", "mm": "member months",
    "rx": "script", "scripts": "script", "claims": "claim", "clm": "claim", "clms": "claim",
    "visits": "visit", "procedures": "procedure", "proc": "procedure", "admits": "admit",
    "admissions": "admit", "admission": "admit", "days": "day", "members": "member", "months": "month",
    "util": "utilization", "1000": "k", "thousand": "k", "med": "medical", "pharm": "pharmacy",
    "phar": "pharmacy", "pmpm": "pmpm", "pmpy": "pmpy", "pkpy": "pkpy", "per": "per",
}
VARIANT_WORDS = {"prior", "py", "ly", "previous", "prev", "change", "chg", "delta", "variance", "var", "diff",
                 "yoy", "ytd", "qtd", "mtd", "rolling", "r12", "ttm", "current", "cy", "cur", "growth",
                 "target", "budget", "trend", "dq"}
GENERIC = {"count", "amount", "per", "average", "rate", "number", "%", "day", "month"}
FILLER = {"the", "of", "and", "total", "sum", "measure", "value", "metric", "kpi", "m", "x"}


def split_camel(s):
    """IPANetPaidAmount -> IPA Net Paid Amount; AdmitsPerKChange -> Admits Per K Change."""
    s = re.sub(r"YoY|MoM|QoQ|WoW", lambda m: " " + m.group(0).capitalize() + " ", s)  # keep YoY as one word
    s = re.sub(r"([A-Z]{2,})(PMPM|PMPY|PKPY|ALOS)", r"\1 \2", s)  # PROFPMPMAllowed -> PROF PMPMAllowed
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", s)
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", s)
    s = re.sub(r"([A-Za-z])(\d)", r"\1 \2", s)
    return s


def tokens(name):
    s = split_camel(name).lower().replace("$", " amount ").replace("#", " count ").replace("%", " % ")
    raw = re.findall(r"[a-z0-9%]+", s)
    out = []
    for t in raw:
        t = SYNONYMS.get(t, t)
        for u in t.split():
            if len(u) > 3 and u.endswith("s") and not u.endswith("ss"):
                u = u[:-1]  # plurals: claims -> claim
            out.append(u)
    return out


def key(name):
    return " ".join(t for t in tokens(name) if t not in FILLER)


NON_BASE_WORDS = {"selector", "color", "colour", "title", "label", "flag", "icon", "tooltip"}


CURRENT_WORDS = {"current", "cur", "cy"}  # the current-window version is the best anchor when no Total_ exists


def score(base, measure, domain="", domains=()):
    all_mt = set(tokens(measure))
    qualifiers = set()
    for q in re.findall(r"\(([^)]*)\)", measure):  # e.g. (WeightedClaims): a qualifier, not a different metric
        qualifiers |= set(tokens(q))
    bt = [t for t in tokens(base) if t not in FILLER and t not in domains]
    mt = [t for t in tokens(re.sub(r"\([^)]*\)", " ", measure)) if t not in FILLER and t not in domains]
    if not bt or not mt:
        return 0.0
    ratio = difflib.SequenceMatcher(None, " ".join(bt), " ".join(mt)).ratio()
    bset, mset = set(bt), set(mt)
    weight = lambda t: 0.3 if t in GENERIC else 1.0  # "count" or "amount" matter less than "admit"
    contained = sum(weight(t) for t in bset & mset) / sum(weight(t) for t in bset)
    extra = sum(weight(t) for t in mset - bset)
    s = 0.4 * ratio + 0.6 * contained - 0.05 * extra
    if (mset & VARIANT_WORDS) - CURRENT_WORDS and not bset & VARIANT_WORDS:
        s -= 0.25  # base metrics should not be prior/change/YTD versions
    elif mset & CURRENT_WORDS:
        s -= 0.03  # Current_X: fine as an anchor, but a plain Total_X wins when both exist
    if mset & NON_BASE_WORDS:
        s -= 0.3   # selectors and formatting measures are not base metrics
    if domain:
        if domain in all_mt:
            s += 0.1   # same domain suffix, e.g. _Medical
        elif all_mt & (set(domains) - {domain}):
            s -= 0.3   # a different domain, e.g. _Pharmacy for a Medical metric
    return round(max(s, 0.0), 3)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="out")
    ap.add_argument("--base-metrics", required=True)
    ap.add_argument("--suggested", default="base_metrics_suggested.txt")
    args = ap.parse_args()

    measures = json.load(open(os.path.join(args.out, "measures.json"), encoding="utf-8"))
    mm_path = os.path.join(args.out, "measure_map.csv")
    mmap = {r["measure"]: r for r in csv.DictReader(open(mm_path, encoding="utf-8"))} if os.path.exists(mm_path) else {}
    names = [m["name"] for m in measures]
    info = {m["name"]: m for m in measures}

    lines = [l.rstrip("\n") for l in open(args.base_metrics, encoding="utf-8-sig")]
    domains = tuple(sorted({tokens(l.rpartition("|")[0])[0] for l in lines
                            if "|" in l and not l.strip().startswith("#") and tokens(l.rpartition("|")[0])}))
    rows, suggested = [], ["# domain|measure name  (generated by match_base_names.py: confirm every '# CHECK' line)"]
    for line in lines:
        line = line.rstrip("\n")
        if not line.strip() or line.strip().startswith("#"):
            continue
        domain, _, base = line.rpartition("|")
        base = base.strip()
        dom = tokens(domain)[0] if tokens(domain) else ""
        scored = sorted(((score(base, n, dom, domains), n) for n in names), key=lambda x: (-x[0], len(x[1])))[:5]
        for rank, (sc, n) in enumerate(scored, 1):
            mrow = mmap.get(n, {})
            rows.append({
                "your_name": base, "domain": domain.strip(), "rank": rank, "measure": n, "score": sc,
                "hidden": info[n].get("is_hidden", ""), "display_folder": info[n].get("display_folder", ""),
                "home_table": info[n].get("table", ""), "role_now": mrow.get("role", ""),
                "used_by_n_measures": mrow.get("n_dependents", ""),
                "aggregated_tables": mrow.get("aggregated_tables", ""),
                "dax_start": " ".join(info[n].get("expression", "").split())[:160],
            })
        best_sc, best = scored[0] if scored else (0, "")
        second = scored[1][0] if len(scored) > 1 else 0
        confident = best_sc >= 0.75 and best_sc - second >= 0.08
        prefix = f"{domain.strip()}|" if domain.strip() else ""
        if not best or best_sc < 0.45:
            near = "; ".join(n for _s, n in scored[:3])
            suggested.append(f"# CHECK: no good match for '{base}' ({domain.strip()}); closest: {near}")
        elif confident:
            suggested.append(f"{prefix}{best}")
        else:
            alts = "; ".join(n for _s, n in scored[1:3])
            suggested.append(f"{prefix}{best}    # CHECK: was '{base}', score {best_sc}; alternatives: {alts}")

    path = os.path.join(args.out, "base_name_matches.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["your_name"])
        w.writeheader()
        w.writerows(rows)
    with open(args.suggested, "w", encoding="utf-8") as f:
        f.write("\n".join(suggested) + "\n")
    n_check = sum(1 for s in suggested[1:] if "# CHECK" in s)
    print(f"Matched {len(suggested) - 1} names: {len(suggested) - 1 - n_check} confident, {n_check} to check.")
    print(f"Wrote {path} and {args.suggested}")


if __name__ == "__main__":
    main()
