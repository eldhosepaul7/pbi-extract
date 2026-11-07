#!/usr/bin/env python3
"""
analyze_model.py - Map every measure to a base metric and variant, and find the fact tables in scope.

Usage:
    python analyze_model.py --out out --base-metrics base_metrics.txt

Reads the files written by extract_bim.py, plus base_metrics.txt:
    one base metric per line, exactly as named in Power BI, optionally prefixed by a domain:
        pharmacy|PMPM
        medical|Admits per 1000
    Lines starting with # are ignored.

If you don't have the list yet, run without --base-metrics: the script writes
candidate_base_metrics.csv to help you build it.

Writes:
    measure_map.csv          every measure: role, base metric, variant type, status, reasons
    base_metrics_report.csv  every base metric: kind, numerator and denominator tables, cross-fact flag
    fact_tables.csv          tables the base metrics aggregate, with Databricks source and key suggestions
                             (edit the grain_key and date_column columns before running grain_checks.py)
    candidate_base_metrics.csv
    analysis.md              readable summary
Standard library only.
"""
import argparse
import csv
import json
import os
import re
from collections import Counter, defaultdict

# --------------------------------------------------------------------------------------------
# DAX lexing and light parsing
# --------------------------------------------------------------------------------------------

def strip_comments(dax):
    """Remove //, -- and /* */ comments, ignoring markers inside strings, 'table names' and [brackets]."""
    out, i, n = [], 0, len(dax)
    while i < n:
        ch = dax[i]
        two = dax[i:i + 2]
        if ch == '"':
            j = i + 1
            while j < n:
                if dax[j] == '"':
                    if j + 1 < n and dax[j + 1] == '"':
                        j += 2
                        continue
                    break
                j += 1
            out.append(dax[i:j + 1]); i = j + 1
        elif ch == "'":
            j = i + 1
            while j < n:
                if dax[j] == "'":
                    if j + 1 < n and dax[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            out.append(dax[i:j + 1]); i = j + 1
        elif ch == "[":
            j = i + 1
            while j < n:
                if dax[j] == "]":
                    if j + 1 < n and dax[j + 1] == "]":
                        j += 2
                        continue
                    break
                j += 1
            out.append(dax[i:j + 1]); i = j + 1
        elif two in ("//", "--"):
            j = dax.find("\n", i)
            i = n if j == -1 else j
        elif two == "/*":
            j = dax.find("*/", i + 2)
            i = n if j == -1 else j + 2
            out.append(" ")
        else:
            out.append(ch); i += 1
    return "".join(out)


TOKEN_RE = re.compile(r"""
    (?P<ws>\s+)
  | (?P<string>"(?:[^"]|"")*")
  | (?P<qtable>'(?:[^']|'')*')
  | (?P<bracket>\[(?:[^\]]|\]\])*\])
  | (?P<number>\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)
  | (?P<ident>[A-Za-z_][A-Za-z0-9_.]*)
  | (?P<op><=|>=|<>|==|&&|\|\||[-+*/^&=<>!])
  | (?P<punct>[(),{}])
  | (?P<other>.)
""", re.X | re.S)

KEYWORDS_NOT_CALLS = {"RETURN", "VAR", "IN", "DEFINE", "EVALUATE", "MEASURE", "ORDER", "BY"}


class Tok:
    __slots__ = ("kind", "val")

    def __init__(self, kind, val):
        self.kind, self.val = kind, val

    def __repr__(self):
        return f"{self.kind}:{self.val}"


class Call:
    __slots__ = ("name", "args")

    def __init__(self, name, args):
        self.name, self.args = name, args


class Group:
    __slots__ = ("items",)

    def __init__(self, items):
        self.items = items


def tokenize(dax):
    toks = []
    for m in TOKEN_RE.finditer(dax):
        kind = m.lastgroup
        if kind == "ws":
            continue
        toks.append(Tok(kind, m.group()))
    return toks


def build_tree(toks):
    pos = 0

    def seq(stop):
        nonlocal pos
        items = []
        while pos < len(toks):
            t = toks[pos]
            if t.kind == "punct" and t.val in stop:
                return items
            if (t.kind == "ident" and t.val.upper() not in KEYWORDS_NOT_CALLS
                    and pos + 1 < len(toks) and toks[pos + 1].val == "("):
                name = t.val.upper()
                pos += 2
                args = []
                while pos <= len(toks):
                    args.append(seq({",", ")"}))
                    if pos >= len(toks):
                        break
                    if toks[pos].val == ",":
                        pos += 1
                        continue
                    pos += 1  # ')'
                    break
                items.append(Call(name, args))
                continue
            if t.val in ("(", "{"):
                close = ")" if t.val == "(" else "}"
                pos += 1
                inner = seq({close})
                pos += 1
                items.append(Group(inner))
                continue
            if t.kind == "punct" and t.val in (")", "}", ","):
                pos += 1  # stray punctuation: skip
                continue
            items.append(t)
            pos += 1
        return items

    return seq(set())


def unq_table(tok):
    if tok.kind == "qtable":
        return tok.val[1:-1].replace("''", "'")
    return tok.val


def unq_bracket(val):
    return val[1:-1].replace("]]", "]")


AGG_COLUMN_FUNCS = {"SUM", "AVERAGE", "MIN", "MAX", "COUNT", "COUNTA", "COUNTBLANK", "DISTINCTCOUNT",
                    "DISTINCTCOUNTNOBLANK", "APPROXIMATEDISTINCTCOUNT", "MEDIAN", "PERCENTILE.INC",
                    "PERCENTILE.EXC"}
ITERATORS = {"SUMX", "AVERAGEX", "MINX", "MAXX", "COUNTX", "COUNTAX", "MEDIANX", "PRODUCTX", "GEOMEANX",
             "PERCENTILEX.INC", "PERCENTILEX.EXC", "RANKX", "CONCATENATEX"}
DISTINCT_FUNCS = {"DISTINCTCOUNT", "DISTINCTCOUNTNOBLANK", "APPROXIMATEDISTINCTCOUNT"}
TIME_PRIOR = {"SAMEPERIODLASTYEAR", "DATEADD", "PARALLELPERIOD", "PREVIOUSDAY", "PREVIOUSMONTH",
              "PREVIOUSQUARTER", "PREVIOUSYEAR"}
TIME_YTD = {"DATESYTD", "TOTALYTD"}
TIME_QTD = {"DATESQTD", "TOTALQTD"}
TIME_MTD = {"DATESMTD", "TOTALMTD"}
TIME_ROLLING = {"DATESINPERIOD"}
TIME_OTHER = {"DATESBETWEEN", "OPENINGBALANCEMONTH", "OPENINGBALANCEQUARTER", "OPENINGBALANCEYEAR",
              "CLOSINGBALANCEMONTH", "CLOSINGBALANCEQUARTER", "CLOSINGBALANCEYEAR", "LASTNONBLANK",
              "FIRSTNONBLANK", "LASTNONBLANKVALUE", "FIRSTNONBLANKVALUE", "ENDOFMONTH", "ENDOFQUARTER",
              "ENDOFYEAR", "STARTOFMONTH", "STARTOFQUARTER", "STARTOFYEAR", "LASTDATE", "FIRSTDATE"}
FILTER_MODIFIERS = {"ALL", "ALLEXCEPT", "ALLNOBLANKROW", "REMOVEFILTERS", "KEEPFILTERS", "USERELATIONSHIP",
                    "CROSSFILTER", "TREATAS"}
CONTEXT_DEPENDENT = {"ALLSELECTED", "ISINSCOPE", "ISFILTERED", "ISCROSSFILTERED", "HASONEVALUE",
                     "HASONEFILTER", "SELECTEDVALUE", "ISSELECTEDMEASURE", "SELECTEDMEASURE", "VALUES"}
SELECTOR_READERS = {"SELECTEDVALUE", "HASONEVALUE", "VALUES", "ISFILTERED", "FILTERS", "MAX", "MIN"}
HEX_COLOR_RE = re.compile(r'"#(?:[0-9A-Fa-f]{3}|[0-9A-Fa-f]{6}|[0-9A-Fa-f]{8})"')
VISUAL_NAME_RE = re.compile(r"\b(colou?r|title|subtitle|label|icon|tooltip|format|font|background|"
                            r"conditional|flag|arrow|indicator|kpi status)\b", re.I)


class Measure:
    def __init__(self, rec):
        self.table = rec["table"]
        self.name = rec["name"]
        self.raw = rec.get("expression", "")
        self.hidden = str(rec.get("is_hidden", "")).lower() == "true" or rec.get("is_hidden") is True
        self.display_folder = rec.get("display_folder", "")
        self.description = rec.get("description", "")
        self.clean = ""
        self.tree = []
        self.parse_error = ""
        self.functions = set()
        self.measure_refs = set()
        self.unresolved_refs = set()
        self.column_refs = set()      # (table, column)
        self.table_refs = set()
        self.agg_tables = set()       # tables this measure aggregates directly
        self.distinct = False
        self.var_defs = {}
        self.return_items = []
        self.string_literals = []


def walk_items(items, visit):
    """Visit every item in a sequence, with the previous item, descending into calls and groups."""
    prev = None
    for it in items:
        visit(it, prev)
        if isinstance(it, Call):
            for a in it.args:
                walk_items(a, visit)
        elif isinstance(it, Group):
            walk_items(it.items, visit)
        prev = it


def refs_in(items, ctx):
    """Collect column refs, measure refs and table refs in a sequence of items."""
    cols, meas, tabs, unresolved = set(), set(), set(), set()

    def visit(it, prev):
        if isinstance(it, Tok):
            if it.kind == "bracket":
                name = unq_bracket(it.val)
                if prev is not None and isinstance(prev, Tok) and prev.kind in ("qtable", "ident") \
                        and unq_table(prev).lower() in ctx["tables_lower"]:
                    cols.add((ctx["tables_lower"][unq_table(prev).lower()], name))
                elif name in ctx["measures"]:
                    meas.add(name)
                elif name.lower() in ctx["column_names_lower"]:
                    cols.add((None, name))
                else:
                    unresolved.add(name)
            elif it.kind in ("qtable", "ident"):
                key = unq_table(it).lower()
                if key in ctx["tables_lower"]:
                    tabs.add(ctx["tables_lower"][key])

    walk_items(items, visit)
    # A table token directly before a bracket is a column qualifier, not a table reference on its own.
    return cols, meas, tabs, unresolved


def first_table(items, ctx):
    cols, meas, tabs, _ = refs_in(items, ctx)
    found = set(tabs) | {t for t, _c in cols if t}
    return found


def split_var_return(items):
    """Split a top-level sequence into VAR definitions and the RETURN expression."""
    var_defs, ret, i = {}, items, 0
    if not any(isinstance(t, Tok) and t.kind == "ident" and t.val.upper() == "VAR" for t in items):
        return var_defs, items
    cur_name, cur_items = None, []
    ret = []
    mode = None
    while i < len(items):
        t = items[i]
        if isinstance(t, Tok) and t.kind == "ident" and t.val.upper() == "VAR":
            if cur_name:
                var_defs[cur_name] = cur_items
            cur_name = items[i + 1].val if i + 1 < len(items) and isinstance(items[i + 1], Tok) else None
            cur_items = []
            i += 3  # VAR name =
            mode = "var"
            continue
        if isinstance(t, Tok) and t.kind == "ident" and t.val.upper() == "RETURN":
            if cur_name:
                var_defs[cur_name] = cur_items
            cur_name = None
            ret = items[i + 1:]
            break
        if mode == "var":
            cur_items.append(t)
        i += 1
    return var_defs, ret


def expand_vars(items, var_defs, depth=0):
    """Replace references to VAR names with the items that define them."""
    if depth > 10:
        return items
    out = []
    for it in items:
        if isinstance(it, Tok) and it.kind == "ident" and it.val in var_defs:
            out.append(Group(expand_vars(var_defs[it.val], var_defs, depth + 1)))
        elif isinstance(it, Call):
            out.append(Call(it.name, [expand_vars(a, var_defs, depth + 1) for a in it.args]))
        elif isinstance(it, Group):
            out.append(Group(expand_vars(it.items, var_defs, depth + 1)))
        else:
            out.append(it)
    return out


def walk_calls(items):
    found = []

    def visit(it, prev):
        if isinstance(it, Call):
            found.append(it)
    walk_items(items, visit)
    return found


def unwrap(items):
    while len(items) == 1 and isinstance(items[0], Group):
        items = items[0].items
    return items


def ratio_parts(items):
    """If an expression is a ratio (DIVIDE or a single top-level '/'), return (numerator, denominator)."""
    items = unwrap(items)
    if len(items) == 1 and isinstance(items[0], Call):
        c = items[0]
        if c.name == "DIVIDE" and len(c.args) >= 2:
            return c.args[0], c.args[1]
        if c.name in ("IF", "IFERROR", "COALESCE") and c.args:
            for a in c.args[1:]:
                r = ratio_parts(a)
                if r:
                    return r
        return None
    ops = [(i, t) for i, t in enumerate(items) if isinstance(t, Tok) and t.kind == "op"]
    divs = [i for i, t in ops if t.val == "/"]
    if len(divs) == 1 and all(t.val in ("/", "*") for _, t in ops):
        i = divs[0]
        return items[:i], items[i + 1:]
    return None


def analyze_measure(m, ctx):
    try:
        m.clean = strip_comments(m.raw)
        toks = tokenize(m.clean)
        m.string_literals = [t.val for t in toks if t.kind == "string"]
        m.tree = build_tree(toks)
    except Exception as e:  # pragma: no cover
        m.parse_error = str(e)
        return
    m.var_defs, m.return_items = split_var_return(m.tree)

    def visit(it, prev):
        if isinstance(it, Call):
            m.functions.add(it.name)
            if it.name in AGG_COLUMN_FUNCS and it.args:
                m.agg_tables |= first_table(it.args[0], ctx)
                if it.name in DISTINCT_FUNCS:
                    m.distinct = True
            elif it.name == "COUNTROWS" and it.args:
                m.agg_tables |= first_table(it.args[0], ctx)
                inner = unwrap(it.args[0])
                if inner and isinstance(inner[0], Call) and inner[0].name in ("VALUES", "DISTINCT"):
                    m.distinct = True
            elif it.name in ITERATORS and it.args:
                m.agg_tables |= first_table(it.args[0], ctx)
                if len(it.args) > 1:
                    cols, _, _, _ = refs_in(it.args[1], ctx)
                    m.agg_tables |= {t for t, _c in cols if t}

    walk_items(m.tree, visit)
    cols, meas, tabs, unresolved = refs_in(m.tree, ctx)
    m.column_refs, m.measure_refs, m.table_refs = cols, meas, tabs
    var_names = set(m.var_defs)
    m.unresolved_refs = {u for u in unresolved if u not in var_names}


# --------------------------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------------------------

VARIANT_NAME_PATTERNS = [
    ("yoy", r"\byoy\b|year over year|\by/y\b"),
    ("change_pct", r"(%|\bpct\b|percent).*\b(chg|change|delta|var|variance|diff|growth)\b|"
                   r"\b(chg|change|delta|var|variance|diff|growth)\b.*(%|\bpct\b|percent)"),
    ("change", r"\b(chg|change|delta|var|variance|diff|difference|growth|trend)\b"),
    ("prior", r"\b(prior|py|ly|last year|previous|prev|pp|pm|pq|lm|lq)\b"),
    ("current", r"\b(current|curr|cur|cy|cm|cq)\b"),
    ("ytd", r"\bytd\b|year to date"),
    ("qtd", r"\bqtd\b"),
    ("mtd", r"\bmtd\b"),
    ("rolling", r"\b(r\d{1,2}m?|rolling|ttm|ltm|l\d{1,2}m|trailing)\b"),
    ("total", r"\btotal\b"),
    ("target", r"\b(target|budget|goal|benchmark)\b"),
]


def name_variants(name):
    n = re.sub(r"[_\-]+", " ", name.lower())
    found = [v for v, pat in VARIANT_NAME_PATTERNS if re.search(pat, n)]
    if "change_pct" in found and "change" in found:
        found.remove("change")
    return found


def function_variants(funcs):
    found = []
    if funcs & TIME_PRIOR:
        found.append("prior")
    if funcs & TIME_YTD:
        found.append("ytd")
    if funcs & TIME_QTD:
        found.append("qtd")
    if funcs & TIME_MTD:
        found.append("mtd")
    if funcs & TIME_ROLLING:
        found.append("rolling")
    return found


def normalize(s):
    return re.sub(r"[^a-z0-9%]+", " ", s.lower()).strip()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="out", help="folder written by extract_bim.py")
    ap.add_argument("--base-metrics", help="base_metrics.txt: one base metric per line, optional 'domain|' prefix")
    args = ap.parse_args()
    out = args.out

    with open(os.path.join(out, "measures.json"), encoding="utf-8") as f:
        recs = json.load(f)
    tables = list(csv.DictReader(open(os.path.join(out, "tables.csv"), encoding="utf-8")))
    columns = list(csv.DictReader(open(os.path.join(out, "columns.csv"), encoding="utf-8")))
    rels = list(csv.DictReader(open(os.path.join(out, "relationships.csv"), encoding="utf-8")))
    sources = {r["table"]: r for r in csv.DictReader(open(os.path.join(out, "sources.csv"), encoding="utf-8"))}
    disconnected = {r["table"] for r in csv.DictReader(open(os.path.join(out, "disconnected_tables.csv"),
                                                            encoding="utf-8"))}
    table_info = {t["table"]: t for t in tables}

    measures = {}
    for r in recs:
        measures[r["name"]] = Measure(r)
    ctx = {
        "tables_lower": {t["table"].lower(): t["table"] for t in tables},
        "measures": set(measures),
        "column_names_lower": {c["column"].lower() for c in columns},
    }
    for m in measures.values():
        analyze_measure(m, ctx)

    # Dependency closure
    closure_cache = {}

    def closure(name, stack=()):
        if name in closure_cache:
            return closure_cache[name]
        if name in stack:
            return set()
        deps = set()
        for d in measures[name].measure_refs:
            if d in measures:
                deps.add(d)
                deps |= closure(d, stack + (name,))
        closure_cache[name] = deps
        return deps

    dependents = defaultdict(set)
    for n in measures:
        for d in closure(n):
            dependents[d].add(n)

    def agg_tables_closure(name):
        ts = set(measures[name].agg_tables)
        for d in closure(name):
            ts |= measures[d].agg_tables
        return ts

    def items_tables(items, m):
        """Tables aggregated by a sub-expression (numerator or denominator), following measure refs."""
        expanded = expand_vars(items, m.var_defs)
        tmp = Measure({"table": m.table, "name": "_", "expression": ""})
        tmp.tree = expanded

        def visit(it, prev):
            if isinstance(it, Call):
                if it.name in AGG_COLUMN_FUNCS | {"COUNTROWS"} | ITERATORS and it.args:
                    tmp.agg_tables.update(first_table(it.args[0], ctx))
        walk_items(expanded, visit)
        _, meas, _, _ = refs_in(expanded, ctx)
        ts = set(tmp.agg_tables)
        for d in meas:
            if d in measures:
                ts |= agg_tables_closure(d)
        return ts, meas

    # Base metrics list
    base_domain = {}
    missing_bases = []
    if args.base_metrics:
        lookup = {normalize(n): n for n in measures}
        for line in open(args.base_metrics, encoding="utf-8-sig"):
            line = re.sub(r"\s{2,}#.*$", "", line).strip()  # drop inline "    # CHECK ..." notes
            if not line or line.startswith("#"):
                continue
            domain, _, name = line.rpartition("|")
            name = name.strip()
            match = name if name in measures else lookup.get(normalize(name))
            if match:
                base_domain[match] = domain.strip()
            else:
                missing_bases.append(line)
    bases = set(base_domain)

    # ---------------------------------------------------------------------------------------
    # Classify every measure
    # ---------------------------------------------------------------------------------------
    rows = []
    for name, m in measures.items():
        deps = closure(name)
        reasons = []
        funcs = set(m.functions)
        for d in deps:
            funcs |= measures[d].functions
        role, base, variant, status = "", "", "", "certain"
        uses_disconnected = {t for t in (m.table_refs | {t for t, _ in m.column_refs if t}) if t in disconnected}

        if m.parse_error:
            role, status = "unparsed", "uncertain"
            reasons.append("parse_error: " + m.parse_error)
        elif name in bases:
            role, base, variant = "base", name, "base"
        elif "SWITCH" in m.functions and uses_disconnected and (m.functions & SELECTOR_READERS):
            role = "selector"
            reasons.append("switches on a selector table: " + ", ".join(sorted(uses_disconnected)))
        elif any(HEX_COLOR_RE.fullmatch(s) for s in m.string_literals) or (
                VISUAL_NAME_RE.search(name) and not agg_tables_closure(name)):
            role = "visual"
            reasons.append("returns colors or formatting text, or is named like a visual helper")
        else:
            dep_bases = (deps & bases)
            if len(dep_bases) == 1:
                base = next(iter(dep_bases))
            elif len(dep_bases) > 1:
                n_name = normalize(name)
                named = [b for b in dep_bases if normalize(b) and normalize(b) in n_name]
                outer = {b for b in dep_bases if not any(b in closure(o) for o in dep_bases if o != b)}
                if len(named) == 1:
                    base = named[0]
                elif len(outer) == 1:
                    base = next(iter(outer))
                    reasons.append("depends on several base metrics; chose the outermost: " + base)
                    status = "review"
                else:
                    status = "uncertain"
                    reasons.append("depends on several base metrics: " + ", ".join(sorted(dep_bases)))
            else:
                n_name = normalize(name)
                named = sorted([b for b in bases if normalize(b) and normalize(b) in n_name],
                               key=lambda b: -len(b))
                if named:
                    base = named[0]
                    status = "review"
                    reasons.append("matched by name only, no dependency on the base metric")
            if base:
                role = "variant"
                nv = name_variants(name)
                fv = function_variants(m.functions)
                if nv:
                    variant = "+".join(nv)
                    if fv and not set(fv) & set(nv) and "change" not in variant and "change_pct" not in variant \
                            and "yoy" not in variant:
                        status = "review" if status == "certain" else status
                        reasons.append(f"name says {variant} but DAX uses {'+'.join(fv)}")
                elif fv:
                    variant = "+".join(fv)
                    status = "review" if status == "certain" else status
                    reasons.append("variant inferred from DAX time functions only")
                elif m.functions & {"CALCULATE", "CALCULATETABLE"}:
                    variant = "filtered"
                    status = "review" if status == "certain" else status
                    reasons.append("CALCULATE with a filter, check whether it is a sub-metric of the base")
                else:
                    variant = "derived"
                    status = "review" if status == "certain" else status
                    reasons.append("derived from the base metric without a recognizable variant pattern")
            if base and role == "variant":
                rp = ratio_parts(expand_vars(m.return_items or m.tree, m.var_defs))
                if rp:
                    _, num_refs = items_tables(rp[0], m)
                    _, den_refs = items_tables(rp[1], m)
                    nb = {b for b in bases if b in num_refs or any(b in closure(x) for x in num_refs if x in measures)}
                    db = {b for b in bases if b in den_refs or any(b in closure(x) for x in den_refs if x in measures)}
                    if nb and db and nb != db and not name_variants(name):
                        role = "candidate_base"
                        status = "review"
                        reasons.append("ratio of base metrics " + ", ".join(sorted(nb)) + " / "
                                       + ", ".join(sorted(db)) + ", may be a base metric itself")
                if role == "variant":
                    for c in walk_calls(m.tree):
                        if c.name in ITERATORS and len(c.args) > 1:
                            _, body_meas, _, _ = refs_in(c.args[1], ctx)
                            if body_meas:
                                role = "candidate_base"
                                status = "review"
                                reasons.append(c.name + " over a measure (fixed-grain or nested aggregation), "
                                               "may be a base metric itself")
                                break
            elif m.hidden and dependents.get(name):
                role = "helper"
                reasons.append("hidden and used by other measures")
            else:
                role = "unmapped"
                status = "uncertain" if bases else "review"
                if bases:
                    reasons.append("no base metric in its dependencies or name")

        if m.unresolved_refs:
            status = "uncertain"
            reasons.append("references not found in model: " + ", ".join(sorted(m.unresolved_refs)))
        ctx_dep = m.functions & CONTEXT_DEPENDENT - {"VALUES"}
        if ctx_dep and role not in ("selector", "visual"):
            reasons.append("context-dependent functions: " + ", ".join(sorted(ctx_dep)))
            if "ALLSELECTED" in ctx_dep or "ISINSCOPE" in ctx_dep:
                status = "uncertain" if status != "uncertain" else status
        if uses_disconnected and role not in ("selector",):
            reasons.append("reads disconnected table(s): " + ", ".join(sorted(uses_disconnected)))
        mods = m.functions & FILTER_MODIFIERS
        if mods:
            reasons.append("filter modifiers: " + ", ".join(sorted(mods)))

        agg_tabs = agg_tables_closure(name)
        rows.append({
            "measure": name, "home_table": m.table, "hidden": m.hidden, "display_folder": m.display_folder,
            "role": role, "base_metric": base, "domain": base_domain.get(base, ""), "variant_type": variant,
            "status": status, "reasons": " | ".join(reasons),
            "aggregated_tables": ";".join(sorted(agg_tabs)),
            "direct_measure_refs": ";".join(sorted(m.measure_refs)),
            "functions": ";".join(sorted(m.functions)),
            "n_dependents": len(dependents.get(name, ())),
        })

    # ---------------------------------------------------------------------------------------
    # Base metrics report
    # ---------------------------------------------------------------------------------------
    variant_counts = Counter(r["base_metric"] for r in rows if r["role"] == "variant")
    base_rows = []
    for b in sorted(bases, key=lambda x: (base_domain.get(x, ""), x.lower())):
        m = measures[b]
        ret = expand_vars(m.return_items or m.tree, m.var_defs)
        rp = ratio_parts(ret)
        kind, num_t, den_t, cross = "", set(), set(), False
        if rp:
            num_t, _ = items_tables(rp[0], m)
            den_t, _ = items_tables(rp[1], m)
            kind = "ratio"
            cross = bool(num_t and den_t and num_t != den_t)
        else:
            dist = m.distinct or any(measures[d].distinct for d in closure(b))
            ts = agg_tables_closure(b)
            num_t = ts
            if m.functions & (TIME_PRIOR | TIME_YTD | TIME_QTD | TIME_MTD | TIME_ROLLING | TIME_OTHER):
                kind = "time-based"
            elif dist:
                kind = "distinct count"
            elif m.functions & ITERATORS or any(measures[d].functions & ITERATORS for d in closure(b)):
                kind = "iterator"
            elif m.functions & (AGG_COLUMN_FUNCS | {"COUNTROWS"}) and not m.measure_refs:
                kind = "additive"
            else:
                kind = "derived"
        flags = []
        if m.functions & FILTER_MODIFIERS:
            flags.append("filter modifiers: " + ",".join(sorted(m.functions & FILTER_MODIFIERS)))
        if m.functions & CONTEXT_DEPENDENT - {"VALUES"}:
            flags.append("context-dependent: " + ",".join(sorted(m.functions & CONTEXT_DEPENDENT - {"VALUES"})))
        if "CALCULATE" in m.functions:
            flags.append("uses CALCULATE")
        base_rows.append({
            "base_metric": b, "domain": base_domain.get(b, ""), "kind": kind,
            "numerator_tables": ";".join(sorted(num_t)), "denominator_tables": ";".join(sorted(den_t)),
            "cross_fact_ratio": cross, "n_variants": variant_counts.get(b, 0),
            "flags": "; ".join(flags), "dax": m.clean.strip(),
        })

    # ---------------------------------------------------------------------------------------
    # Fact tables in scope
    # ---------------------------------------------------------------------------------------
    usage = defaultdict(set)
    for b in bases:
        for t in agg_tables_closure(b):
            usage[t].add(b)
    many_side = defaultdict(list)
    for r in rels:
        fc, tc = r["from_cardinality"], r["to_cardinality"]
        if fc == "many":
            many_side[r["from_table"]].append(r["from_column"])
        if tc == "many":
            many_side[r["to_table"]].append(r["to_column"])
    key_re = re.compile(r"(?i)(^|[\s_])(id|key|nbr|num|number|no|seq|sequence|line|ln)$|[a-z](ID|Key)$")
    date_tables = {t["table"] for t in tables if re.search(r"date|calendar|period", t["table"], re.I)}
    fact_rows = []
    for t in sorted(usage, key=lambda x: -len(usage[x])):
        info = table_info.get(t, {})
        src = sources.get(t, {})
        cols_t = [c for c in columns if c["table"] == t]
        fks = sorted(set(many_side.get(t, [])))
        keys = [c["column"] for c in cols_t if c["type"] == "data" and key_re.search(c["column"])]
        own_keys = [k for k in keys if k not in fks]
        keys = own_keys + [k for k in keys if k in fks]
        if own_keys:
            guess = own_keys[:3]
            notes_guess = "grain_key is a guess from column names: confirm it"
        else:
            guess = fks
            notes_guess = ("grain_key pre-filled with relationship columns (typical for member-month or snapshot "
                           "tables): remove any column that should not be part of the key")
        date_candidates = [r["from_column"] for r in rels if r["from_table"] == t and r["to_table"] in date_tables
                           and str(r["is_active"]).lower() == "true"]
        date_candidates += [c["column"] for c in cols_t if c["data_type"] == "dateTime" and c["type"] == "data"]
        notes = [notes_guess]
        if t in disconnected:
            notes.append("disconnected table")
        if info.get("source_class") in ("calculated_table", "entered_data", "non_databricks", "unknown"):
            notes.append(f"source is {info.get('source_class')}: no Databricks table to check")
        if info.get("source_class") == "transformed":
            notes.append("Power Query transforms this table: logic must move to gold")
        if info.get("row_cap"):
            notes.append("row cap in this file: " + info["row_cap"])
        databricks = src.get("databricks_source", "")
        if not databricks and src.get("native_sql_tables") and ";" not in src.get("native_sql_tables"):
            databricks = src["native_sql_tables"]
            notes.append("source is a native SQL query; using its table")
        fact_rows.append({
            "table": t, "databricks_source": databricks, "source_class": info.get("source_class", ""),
            "n_base_metrics": len(usage[t]), "base_metrics": ";".join(sorted(usage[t])),
            "relationship_columns": ";".join(fks),
            "candidate_key_columns": ";".join(keys[:8]),
            "grain_key": ";".join(guess), "date_column": date_candidates[0] if date_candidates else "",
            "notes": "; ".join(notes),
        })

    # ---------------------------------------------------------------------------------------
    # Candidate base metrics (to help build base_metrics.txt)
    # ---------------------------------------------------------------------------------------
    cand_rows = []
    for name, m in measures.items():
        deps = closure(name)
        direct_agg = bool(m.agg_tables)
        rp = ratio_parts(expand_vars(m.return_items or m.tree, m.var_defs))
        simple_ratio = bool(rp) and not (m.functions & (TIME_PRIOR | TIME_YTD | TIME_QTD | TIME_MTD | TIME_ROLLING))
        if not (direct_agg or simple_ratio):
            continue
        if name_variants(name):
            continue
        cand_rows.append({"measure": name, "home_table": m.table, "display_folder": m.display_folder,
                          "hidden": m.hidden, "aggregates_directly": direct_agg, "is_simple_ratio": simple_ratio,
                          "n_dependents": len(dependents.get(name, ())),
                          "in_base_list": name in bases})
    cand_rows.sort(key=lambda r: -r["n_dependents"])

    def write(path, rows_, fields):
        with open(os.path.join(out, path), "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(rows_)

    write("measure_map.csv", rows, list(rows[0].keys()) if rows else ["measure"])
    write("base_metrics_report.csv", base_rows, ["base_metric", "domain", "kind", "numerator_tables",
                                                 "denominator_tables", "cross_fact_ratio", "n_variants",
                                                 "flags", "dax"])
    write("fact_tables.csv", fact_rows, ["table", "databricks_source", "source_class", "n_base_metrics",
                                         "base_metrics", "relationship_columns", "candidate_key_columns",
                                         "grain_key", "date_column", "notes"])
    write("candidate_base_metrics.csv", cand_rows, ["measure", "home_table", "display_folder", "hidden",
                                                    "aggregates_directly", "is_simple_ratio", "n_dependents",
                                                    "in_base_list"])

    # ---------------------------------------------------------------------------------------
    # analysis.md
    # ---------------------------------------------------------------------------------------
    role_counts = Counter(r["role"] for r in rows)
    status_counts = Counter(r["status"] for r in rows)
    L = ["# Semantic model analysis", ""]
    L += ["## Summary", "",
          f"- Measures: **{len(rows)}**",
          f"- Base metrics in your list: **{len(bases)}** found" +
          (f", **{len(missing_bases)} not found in the model** (check spelling)" if missing_bases else ""),
          "- Roles: " + ", ".join(f"{k or 'none'} {v}" for k, v in role_counts.most_common()),
          "- Status: " + ", ".join(f"{k} {v}" for k, v in status_counts.most_common()),
          f"- Fact tables used by the base metrics: **{len(fact_rows)}**", ""]
    if not bases:
        L += ["> No base metric list was given. See **candidate_base_metrics.csv**: measures that aggregate "
              "columns directly or are simple ratios, sorted by how many other measures use them. Pick your "
              "base metrics from it, write them to base_metrics.txt, and rerun.", ""]
    if missing_bases:
        L += ["### Base metrics not found", ""] + [f"- {b}" for b in missing_bases] + [""]
    if base_rows:
        L += ["## Base metrics", "",
              "| Base metric | Domain | Kind | Numerator tables | Denominator tables | Cross-fact | Variants |",
              "|---|---|---|---|---|---|---|"]
        for r in base_rows:
            L.append(f"| {r['base_metric']} | {r['domain']} | {r['kind']} | {r['numerator_tables']} | "
                     f"{r['denominator_tables']} | {'**yes**' if r['cross_fact_ratio'] else ''} | {r['n_variants']} |")
        L += ["", "**Cross-fact** ratios (numerator and denominator from different fact tables, like PMPM) need a "
              "shared-grain view or a trusted query in Databricks.", ""]
    if fact_rows:
        L += ["## Fact tables in scope", "",
              "| Table | Databricks source | Base metrics using it | Relationship columns | Candidate key columns | Notes |",
              "|---|---|---|---|---|---|"]
        for r in fact_rows:
            L.append(f"| {r['table']} | {r['databricks_source'] or '—'} | {r['n_base_metrics']} | "
                     f"{r['relationship_columns']} | {r['candidate_key_columns']} | {r['notes']} |")
        L += ["", "Relationship columns are foreign keys: they show which dimensions a fact links to, not what makes "
              "a row unique. Before running grain_checks.py, check **grain_key** in fact_tables.csv (pre-filled with a guess: the columns "
              "that should identify one row, separated by ';') and check **date_column**.", ""]
    variants = Counter(r["variant_type"] for r in rows if r["role"] == "variant")
    if variants:
        L += ["## Variant types", "", "| Variant type | Measures |", "|---|---|"]
        L += [f"| {k} | {v} |" for k, v in variants.most_common()] + [""]
    reason_groups = defaultdict(list)
    for r in rows:
        if r["status"] != "certain":
            for reason in r["reasons"].split(" | "):
                key = reason.split(":")[0] if reason else "(no reason)"
                reason_groups[(r["status"], key)].append(r["measure"])
    if reason_groups:
        L += ["## Needs review", "", "Grouped by status and reason (a measure can appear under several reasons).", "",
              "| Status | Reason | Count | Examples |", "|---|---|---|---|"]
        for (st, key), ms in sorted(reason_groups.items(), key=lambda x: (x[0][0] != "uncertain", -len(x[1]))):
            ex = ", ".join(ms[:4]) + (" …" if len(ms) > 4 else "")
            L.append(f"| {st} | {key} | {len(ms)} | {ex} |")
        L += [""]
    L += ["## Files", "",
          "- measure_map.csv: every measure with role, base metric, variant type, status and reasons",
          "- base_metrics_report.csv: base metrics with kind, numerator and denominator tables, and their DAX",
          "- fact_tables.csv: fact tables in scope (fill in grain_key, check date_column)",
          "- candidate_base_metrics.csv: helps build or check base_metrics.txt", ""]
    with open(os.path.join(out, "analysis.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    print("\n".join(L))


if __name__ == "__main__":
    main()
