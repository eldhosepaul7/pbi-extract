#!/usr/bin/env python3
"""
family_sources.py - Find the detail table behind each fact family, using Unity Catalog lineage.

Usage:
    python family_sources.py --out out --dry-run
    python family_sources.py --out out --warehouse-id <ID> [--profile <name>] [--depth 3]

Reads out/fact_tables.csv (fact_family and databricks_source; map missing sources first with
find_uc_tables.py). Follows system.access.table_lineage upstream from every aggregate table in a
family and ranks the upstream tables by how many of the family's tables derive from them.
The best-covered non-aggregate, non-dimension table is usually the detail fact to build the metric view on.

Writes out/fact_family_sources.csv and out/fact_family_sources.md. Reads lineage metadata only.
Lineage is only recorded for tables written in the last year by Unity Catalog-enabled compute.
"""
import argparse
import csv
import os
import re
import sys
import time
from collections import defaultdict


def read(path):
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def q(s):
    return "'" + s.replace("'", "''") + "'"


def kind(table):
    name = table.split(".")[-1]
    if re.search(r"(?i)(^|_)agg(_|$)|aggregate|summary|_sum$", name):
        return "aggregate"
    if re.match(r"(?i)dim_|d_|lkp_|ref_", name) or re.search(r"(?i)(^|_)(dim|lookup|ref)(_|$)", name):
        return "dimension"
    return "detail"


class Sql:
    def __init__(self, args):
        self.dry, self.log = args.dry_run, []
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
            data += self.w.statement_execution.get_statement_result_chunk_n(resp.statement_id, i).data_array or []
        return [dict(zip(cols, r)) for r in data]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="out")
    ap.add_argument("--warehouse-id")
    ap.add_argument("--profile")
    ap.add_argument("--depth", type=int, default=3, help="how many lineage levels to follow upstream")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    facts = read(os.path.join(args.out, "fact_tables.csv"))
    if facts and "fact_family" not in facts[0]:
        sys.exit("fact_tables.csv has no fact_family column: rerun analyze_model.py with the latest version.")
    members = defaultdict(set)
    missing = defaultdict(list)
    for r in facts:
        src = (r.get("databricks_source") or "").strip().lower()
        if src:
            members[r["fact_family"]].add(src)
        else:
            missing[r["fact_family"]].append(r["table"])

    runner = Sql(args)
    upstream = defaultdict(set)   # target -> sources
    frontier = set().union(*members.values()) if members else set()
    seen = set(frontier)
    for _level in range(args.depth):
        if not frontier:
            break
        batch_list = sorted(frontier)
        frontier = set()
        for i in range(0, len(batch_list), 200):
            batch = batch_list[i:i + 200]
            sql = ("SELECT DISTINCT lower(target_table_full_name) AS target, lower(source_table_full_name) AS source "
                   "FROM system.access.table_lineage WHERE source_table_full_name IS NOT NULL "
                   f"AND lower(target_table_full_name) IN ({', '.join(q(t) for t in batch)})")
            for r in runner.rows(sql):
                if r["source"] and r["source"] != r["target"]:
                    upstream[r["target"]].add(r["source"])
                    if r["source"] not in seen:
                        seen.add(r["source"])
                        frontier.add(r["source"])
        if args.dry_run:
            break

    if args.dry_run:
        print("Dry run. Queries that would run (read-only, lineage metadata only), first level shown:\n")
        print("\n\n".join(runner.log[:2]) + ("\n..." if len(runner.log) > 2 else ""))
        print(f"\n{sum(len(v) for v in members.values())} Databricks tables in {len(members)} families; "
              f"{sum(len(v) for v in missing.values())} fact tables have no Databricks source yet "
              "(run find_uc_tables.py first).")
        return

    rows, md = [], ["# Detail sources for each fact family", "",
                    "For each family: upstream tables found through Unity Catalog lineage, ranked by how many of the "
                    "family's aggregate tables derive from them. **coverage** 100% means every aggregate in the family "
                    "is built from that table. The top **detail** table is usually the one to build the metric view on.",
                    ""]
    for fam in sorted(members, key=lambda f: -len(members[f])):
        reach = defaultdict(lambda: [0, 99])  # upstream -> [members reaching it, min distance]
        for m in members[fam]:
            dist = {m: 0}
            stack = [m]
            while stack:
                cur = stack.pop()
                for src in upstream.get(cur, ()):
                    if src not in dist or dist[src] > dist[cur] + 1:
                        dist[src] = dist[cur] + 1
                        stack.append(src)
            for src, d in dist.items():
                if d == 0:
                    continue
                reach[src][0] += 1
                reach[src][1] = min(reach[src][1], d)
        n = len(members[fam])
        ranked = sorted(reach.items(), key=lambda x: (kind(x[0]) != "detail", -x[1][0], x[1][1]))
        md += [f"## {fam}", "", f"{n} aggregate table(s) with a Databricks source"
               + (f"; {len(missing[fam])} without one: {', '.join(missing[fam][:5])}" if missing.get(fam) else ""), ""]
        if not ranked:
            md += ["No upstream lineage found. Lineage only covers tables written in the last year by Unity "
                   "Catalog-enabled compute; check the job that builds these tables instead.", ""]
            continue
        md += ["| Upstream table | Looks like | Coverage | Levels up |", "|---|---|---|---|"]
        for src, (cnt, d) in ranked[:8]:
            rows.append({"fact_family": fam, "upstream_table": src, "looks_like": kind(src),
                         "coverage": round(cnt / n, 2), "family_tables_reaching_it": cnt, "levels_up": d})
            md.append(f"| {src} | {kind(src)} | {cnt}/{n} ({cnt / n:.0%}) | {d} |")
        md += [""]
    with open(os.path.join(args.out, "fact_family_sources.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["fact_family", "upstream_table", "looks_like", "coverage",
                                          "family_tables_reaching_it", "levels_up"])
        w.writeheader()
        w.writerows(rows)
    with open(os.path.join(args.out, "fact_family_sources.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(md) + "\n")
    print(f"Followed lineage for {sum(len(v) for v in members.values())} tables in {len(members)} families.")
    print(f"Wrote {os.path.join(args.out, 'fact_family_sources.md')} and fact_family_sources.csv")


if __name__ == "__main__":
    main()
