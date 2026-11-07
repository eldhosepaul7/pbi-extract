#!/usr/bin/env python3
"""
find_uc_tables.py - Find the Unity Catalog table behind Power BI tables that have no mapping.

Usage:
    python find_uc_tables.py --out out --dry-run                      # show the SQL, run nothing
    python find_uc_tables.py --out out --warehouse-id <ID> [--profile <name>] [--catalogs gold,silver] [--all]

For every Power BI table without a Databricks source (by default only the tables your base metrics
use, plus the tables they join to; --all for every table), it searches Unity Catalog by name, then
confirms candidates by comparing column names. Reads metadata only (information_schema), never data.

Writes:
    out/uc_table_candidates.csv   top candidates per table with name score and column overlap
    table_mappings_suggested.csv  best match per table; review it, save as table_mappings.csv, then
                                  python extract_bim.py model.bim --out out --mappings table_mappings.csv
    out/unmapped_tables.md        what was found, and what to do with tables that have no UC table
Needs: pip install databricks-sdk (not for --dry-run). Sign in first with: databricks auth login
"""
import argparse
import csv
import difflib
import os
import re
import sys
import time
from collections import defaultdict

NO_UC_CLASSES = {"calculated_table", "entered_data", "calculation_group"}
GUIDANCE = {
    "calculated_table": "Built in DAX inside Power BI. Date tables: use your gold date dimension instead. "
                        "Summary tables used by metrics: rebuild as a gold view.",
    "entered_data": "Typed into Power BI by hand. Selector or parameter tables: not needed. Mapping or "
                    "threshold tables used by metrics: land them in the lakehouse as a small table.",
    "calculation_group": "Power BI calculation group (often time intelligence). Becomes variant templates, "
                         "not a table.",
    "non_databricks": "Comes from another source (Excel, SharePoint, another database). Land it in the "
                      "lakehouse first if metrics need it.",
}


def norm(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


def read(path):
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def q(s):
    return "'" + s.replace("'", "''") + "'"


class Sql:
    def __init__(self, args):
        self.dry = args.dry_run
        self.log = []
        if not self.dry:
            if not args.warehouse_id:
                sys.exit("--warehouse-id is required unless you use --dry-run")
            try:
                from databricks.sdk import WorkspaceClient
            except ImportError:
                sys.exit("Install the SDK first: pip install databricks-sdk")
            self.w = WorkspaceClient(profile=args.profile) if args.profile else WorkspaceClient()
            self.wh = args.warehouse_id

    def rows(self, sql):
        """Run a read-only query and return all rows as dicts (fetches every result chunk)."""
        assert sql.lstrip().upper().startswith(("SELECT", "WITH")), "read-only queries only"
        self.log.append(sql)
        if self.dry:
            return []
        from databricks.sdk.service.sql import StatementState
        resp = self.w.statement_execution.execute_statement(warehouse_id=self.wh, statement=sql, wait_timeout="30s")
        while resp.status.state in (StatementState.PENDING, StatementState.RUNNING):
            time.sleep(3)
            resp = self.w.statement_execution.get_statement(resp.statement_id)
        if resp.status.state != StatementState.SUCCEEDED:
            raise RuntimeError(resp.status.error.message if resp.status.error else str(resp.status.state))
        cols = [c.name for c in resp.manifest.schema.columns]
        data = list((resp.result.data_array or []) if resp.result else [])
        for i in range(1, resp.manifest.total_chunk_count or 1):
            chunk = self.w.statement_execution.get_statement_result_chunk_n(resp.statement_id, i)
            data += chunk.data_array or []
        return [dict(zip(cols, r)) for r in data]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="out")
    ap.add_argument("--warehouse-id")
    ap.add_argument("--profile")
    ap.add_argument("--catalogs", help="comma-separated catalogs to search (default: all you can see)")
    ap.add_argument("--all", action="store_true", help="every unmapped table, not only those the base metrics use")
    ap.add_argument("--suggested", default="table_mappings_suggested.csv")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    out = args.out

    tables = read(os.path.join(out, "tables.csv"))
    sources = {r["table"]: r for r in read(os.path.join(out, "sources.csv"))}
    cols = read(os.path.join(out, "columns.csv"))
    rels = read(os.path.join(out, "relationships.csv"))
    fact_path = os.path.join(out, "fact_tables.csv")
    facts = {r["table"] for r in read(fact_path)} if os.path.exists(fact_path) else set()

    if args.all or not facts:
        scope = {t["table"] for t in tables}
    else:
        scope = set(facts)
        for r in rels:
            if r["from_table"] in facts:
                scope.add(r["to_table"])
            if r["to_table"] in facts:
                scope.add(r["from_table"])

    def has_source(t):
        s = sources.get(t, {})
        nt = s.get("native_sql_tables", "")
        return bool(s.get("databricks_source")) or (nt and ";" not in nt)

    targets, no_uc = [], defaultdict(list)
    for t in tables:
        name = t["table"]
        if name not in scope or has_source(name):
            continue
        cls = t.get("source_class", "")
        if cls in NO_UC_CLASSES or cls == "non_databricks":
            no_uc[cls].append(name)
        else:
            targets.append(name)

    pbi_cols = defaultdict(set)
    for c in cols:
        if c["type"] == "data":
            pbi_cols[c["table"]].add((c.get("physical_column") or c["column"]).lower())

    runner = Sql(args)
    where = "table_schema <> 'information_schema'"
    if args.catalogs:
        where += " AND table_catalog IN (" + ", ".join(q(c.strip()) for c in args.catalogs.split(",")) + ")"
    uc_tables = runner.rows(f"SELECT table_catalog, table_schema, table_name FROM system.information_schema.tables "
                            f"WHERE {where}")

    # Name candidates
    cands = {}
    for t in targets:
        nt = norm(t)
        scored = []
        for u in uc_tables:
            nu = norm(u["table_name"])
            if not nt or not nu:
                continue
            if nt == nu:
                s = 1.0
            elif nt in nu or nu in nt:
                s = 0.9 * min(len(nt), len(nu)) / max(len(nt), len(nu)) + 0.1
            else:
                s = difflib.SequenceMatcher(None, nt, nu).ratio()
            if s >= 0.5:
                scored.append((round(s, 3), f'{u["table_catalog"]}.{u["table_schema"]}.{u["table_name"]}'))
        cands[t] = sorted(scored, key=lambda x: -x[0])[:5]

    # Column overlap for the candidates
    full_names = sorted({full for c in cands.values() for _s, full in c})
    uc_cols = defaultdict(set)
    for i in range(0, len(full_names), 100):
        batch = full_names[i:i + 100]
        conds = " OR ".join(
            f"(table_catalog = {q(a)} AND table_schema = {q(b)} AND table_name = {q(c)})"
            for a, b, c in (f.split(".", 2) for f in batch))
        for r in runner.rows("SELECT table_catalog, table_schema, table_name, column_name "
                             f"FROM system.information_schema.columns WHERE {conds}"):
            uc_cols[f'{r["table_catalog"]}.{r["table_schema"]}.{r["table_name"]}'].add(r["column_name"].lower())
    if args.dry_run and not full_names:
        runner.log.append("SELECT table_catalog, table_schema, table_name, column_name FROM "
                          "system.information_schema.columns WHERE (table_catalog = '<catalog>' AND "
                          "table_schema = '<schema>' AND table_name = '<table>') OR ...")

    cand_rows, sugg_rows = [], []
    for t in targets:
        ranked = []
        for s, full in cands.get(t, []):
            pc = pbi_cols.get(t, set())
            overlap = len(pc & uc_cols.get(full, set())) / len(pc) if pc else 0.0
            combined = round(0.5 * s + 0.5 * overlap, 3)
            missing = sorted(pc - uc_cols.get(full, set()))[:5]
            ranked.append((combined, s, round(overlap, 2), full, missing))
        ranked.sort(key=lambda x: -x[0])
        for i, (comb, s, ov, full, missing) in enumerate(ranked, 1):
            cand_rows.append({"pbi_table": t, "rank": i, "uc_table": full, "name_score": s, "column_overlap": ov,
                              "combined": comb, "pbi_columns_missing_in_uc": ";".join(missing)})
        if ranked:
            best = ranked[0]
            gap = best[0] - (ranked[1][0] if len(ranked) > 1 else 0)
            confident = best[2] >= 0.8 and (gap >= 0.1 or best[1] == 1.0)
            note = "confident" if confident else (
                "CHECK: columns differ, Power Query may rename or compute columns" if best[2] < 0.8
                else "CHECK: several similar tables")
            sugg_rows.append({"pbi_table": t, "databricks_table": best[3], "confidence": note,
                              "name_score": best[1], "column_overlap": best[2]})
        else:
            sugg_rows.append({"pbi_table": t, "databricks_table": "", "confidence": "NOT FOUND: fill in by hand",
                              "name_score": "", "column_overlap": ""})

    if args.dry_run:
        print("Dry run. Queries that would run (read-only, metadata only):\n")
        print("\n\n".join(runner.log))
        print(f"\n{len(targets)} Power BI tables would be searched: " + ", ".join(targets[:20])
              + (" ..." if len(targets) > 20 else ""))
        return

    with open(os.path.join(out, "uc_table_candidates.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["pbi_table", "rank", "uc_table", "name_score", "column_overlap",
                                          "combined", "pbi_columns_missing_in_uc"])
        w.writeheader()
        w.writerows(cand_rows)
    with open(args.suggested, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["pbi_table", "databricks_table", "confidence", "name_score",
                                          "column_overlap"])
        w.writeheader()
        w.writerows(sugg_rows)

    n_conf = sum(1 for r in sugg_rows if r["confidence"] == "confident")
    L = ["# Unmapped tables", "",
         f"- Searched: {len(targets)} tables; confident matches: {n_conf}; to check: "
         f"{sum(1 for r in sugg_rows if r['confidence'].startswith('CHECK'))}; not found: "
         f"{sum(1 for r in sugg_rows if r['confidence'].startswith('NOT FOUND'))}", "",
         "Review **table_mappings_suggested.csv**, fix or fill in the rows that are not 'confident', save it as "
         "**table_mappings.csv**, then rerun extraction and analysis:", "",
         "```", "python extract_bim.py model.bim --out out --mappings table_mappings.csv",
         "python analyze_model.py --out out --base-metrics base_metrics_auto.txt", "```", "",
         "A low column overlap usually means Power Query renames or computes columns: the table is still the "
         "right source, but that logic must be recreated in a gold view.", ""]
    if no_uc:
        L += ["## Tables with no Unity Catalog table by design", ""]
        for cls, names in no_uc.items():
            L += [f"**{cls}** ({len(names)}): {', '.join(names[:15])}{' …' if len(names) > 15 else ''}", "",
                  GUIDANCE.get(cls, ""), ""]
    with open(os.path.join(out, "unmapped_tables.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    print("\n".join(L[:3]))
    print(f"Wrote {args.suggested}, {os.path.join(out, 'uc_table_candidates.csv')} and "
          f"{os.path.join(out, 'unmapped_tables.md')}")


if __name__ == "__main__":
    main()
