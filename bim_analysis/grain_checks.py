#!/usr/bin/env python3
"""
grain_checks.py - Check fact-table grain and relationship safety in Databricks. Read-only, counts only.

Usage:
    python grain_checks.py --out out --dry-run                 # write the SQL to out/grain_checks.sql, run nothing
    python grain_checks.py --out out --warehouse-id <ID>       # run the checks
    options: --profile <cli profile>  --months 12  --only "Pharmacy Claims"

Reads fact_tables.csv (edit grain_key and date_column first), columns.csv, relationships.csv, sources.csv.
Writes grain_checks.csv and grain_checks.md.

Checks:
    1. Fact grain: rows vs distinct grain_key values, rows with a NULL key, date range.
    2. Relationships from in-scope facts: duplicate keys on the "one" side (causes fan-out),
       and fact rows whose key has no match (shown under a blank member in Power BI).
    3. Cross-fact ratios (e.g. PMPM): is the denominator table unique on the columns it shares
       with the numerator table?

Every query is a SELECT that returns counts or dates. No identifiers are returned.
Needs: pip install databricks-sdk (not needed for --dry-run). Sign in first with: databricks auth login
"""
import argparse
import csv
import os
import sys
import time


def q(ident):
    return "`" + ident.replace("`", "``") + "`"


def qtable(full):
    return ".".join(q(p) for p in full.split("."))


def read(path):
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


class Runner:
    def __init__(self, args):
        self.dry = args.dry_run
        self.wh = args.warehouse_id
        self.w = None
        if not self.dry:
            if not self.wh:
                sys.exit("--warehouse-id is required unless you use --dry-run")
            try:
                from databricks.sdk import WorkspaceClient
            except ImportError:
                sys.exit("Install the SDK first: pip install databricks-sdk")
            self.w = WorkspaceClient(profile=args.profile) if args.profile else WorkspaceClient()

    def run(self, sql):
        head = sql.lstrip().upper()
        if not (head.startswith("SELECT") or head.startswith("WITH")):
            raise RuntimeError("refusing to run a statement that is not a SELECT")
        if self.dry:
            return None
        from databricks.sdk.service.sql import StatementState
        resp = self.w.statement_execution.execute_statement(
            warehouse_id=self.wh, statement=sql, wait_timeout="30s")
        while resp.status.state in (StatementState.PENDING, StatementState.RUNNING):
            time.sleep(3)
            resp = self.w.statement_execution.get_statement(resp.statement_id)
        if resp.status.state != StatementState.SUCCEEDED:
            msg = resp.status.error.message if resp.status.error else str(resp.status.state)
            raise RuntimeError(msg)
        cols = [c.name for c in resp.manifest.schema.columns]
        data = (resp.result.data_array or [[]]) if resp.result else [[]]
        return dict(zip(cols, data[0])) if data and data[0] else {}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="out")
    ap.add_argument("--warehouse-id")
    ap.add_argument("--profile", help="Databricks CLI profile name (default: DEFAULT)")
    ap.add_argument("--months", type=int, default=12, help="limit fact scans to the last N months (0 = no limit)")
    ap.add_argument("--only", help="check only this Power BI table")
    ap.add_argument("--dry-run", action="store_true", help="write the SQL without running it")
    args = ap.parse_args()
    out = args.out

    facts = read(os.path.join(out, "fact_tables.csv"))
    cols = read(os.path.join(out, "columns.csv"))
    rels = read(os.path.join(out, "relationships.csv"))
    sources = {r["table"]: r for r in read(os.path.join(out, "sources.csv"))}
    base_report_path = os.path.join(out, "base_metrics_report.csv")
    base_report = read(base_report_path) if os.path.exists(base_report_path) else []

    phys = {(c["table"], c["column"]): (c["physical_column"] or c["column"]) for c in cols}

    def dbx_source(table):
        s = sources.get(table, {})
        if s.get("databricks_source"):
            return s["databricks_source"]
        nt = s.get("native_sql_tables", "")
        return nt if nt and ";" not in nt else ""

    def pcol(table, column):
        return q(phys.get((table, column), column))

    runner = Runner(args)
    results, sql_log = [], []

    def check(table, name, sql, interpret):
        sql_log.append(f"-- {table}: {name}\n{sql};\n")
        row = {"table": table, "check": name, "result": "", "status": "", "sql": " ".join(sql.split())}
        if args.dry_run:
            row["status"] = "not run (dry run)"
        else:
            try:
                res = runner.run(sql)
                row["result"], row["status"] = interpret(res)
            except Exception as e:  # keep going on errors
                row["status"] = "error"
                row["result"] = str(e)[:300]
        results.append(row)
        print(f"[{row['status']}] {table}: {name} {row['result']}")

    fact_names = set()
    for f in facts:
        t = f["table"]
        if args.only and t != args.only:
            continue
        src = f.get("databricks_source") or dbx_source(t)
        if not src:
            results.append({"table": t, "check": "fact grain", "result": "no Databricks source", "status": "skipped",
                            "sql": ""})
            continue
        fact_names.add(t)
        date_col = f.get("date_column", "").strip()
        where = (f" WHERE {pcol(t, date_col)} >= add_months(current_date(), -{args.months})"
                 if date_col and args.months > 0 else "")
        keys = [k.strip() for k in f.get("grain_key", "").split(";") if k.strip()]
        sel = ["COUNT(*) AS row_count"]
        if keys:
            kp = ", ".join(pcol(t, k) for k in keys)
            nulls = " OR ".join(f"{pcol(t, k)} IS NULL" for k in keys)
            sel += [f"COUNT(DISTINCT {kp}) AS distinct_keys",
                    f"SUM(CASE WHEN {nulls} THEN 1 ELSE 0 END) AS null_key_rows"]
        if date_col:
            sel += [f"MIN({pcol(t, date_col)}) AS min_date", f"MAX({pcol(t, date_col)}) AS max_date"]
        sql = f"SELECT {', '.join(sel)} FROM {qtable(src)}{where}"

        def interp_fact(r, keys=keys):
            if not r:
                return "no result", "error"
            rc = int(r.get("row_count") or 0)
            msg = f"rows={rc}"
            if "min_date" in r:
                msg += f", dates {r.get('min_date')} to {r.get('max_date')}"
            if not keys:
                return msg + ", no grain_key set", "incomplete"
            dk, nk = int(r.get("distinct_keys") or 0), int(r.get("null_key_rows") or 0)
            msg += f", distinct keys={dk}, rows with NULL key={nk}"
            ok = (dk + nk == rc) and nk == 0
            return msg, "pass" if ok else "fail"
        check(t, "fact grain (" + (";".join(keys) or "no key") + ")", sql, interp_fact)

        # Relationships where this fact is on the many side
        for r in rels:
            if r["from_table"] == t and r["from_cardinality"] == "many":
                fk, other, ok_col, other_card = r["from_column"], r["to_table"], r["to_column"], r["to_cardinality"]
            elif r["to_table"] == t and r["to_cardinality"] == "many":
                fk, other, ok_col, other_card = r["to_column"], r["from_table"], r["from_column"], r["from_cardinality"]
            else:
                continue
            osrc = dbx_source(other)
            label = f"{t}[{fk}] -> {other}[{ok_col}]"
            if not osrc:
                results.append({"table": t, "check": "relationship " + label,
                                "result": f"{other} has no Databricks source "
                                          f"({sources.get(other, {}).get('source_class', '?')})",
                                "status": "skipped", "sql": ""})
                continue
            note = ""
            if sources.get(other, {}).get("source_class") in ("transformed", "native_query"):
                note = " (Power Query changes this table, so Databricks may differ from Power BI)"
            if other_card == "one":
                dup_sql = (f"SELECT COUNT(*) AS duplicate_keys FROM (SELECT {pcol(other, ok_col)} AS k "
                           f"FROM {qtable(osrc)} GROUP BY {pcol(other, ok_col)} HAVING COUNT(*) > 1)")
                check(t, "one-side duplicates " + label, dup_sql,
                      lambda r, note=note: (f"duplicate keys={r.get('duplicate_keys')}{note}",
                                            "pass" if int(r.get("duplicate_keys") or 0) == 0 else "fail"))
            else:
                results.append({"table": t, "check": "relationship " + label,
                                "result": "many-to-many relationship: agree the join with the business",
                                "status": "review", "sql": ""})
            orphan_sql = (f"SELECT COUNT(*) AS orphan_rows FROM (SELECT {pcol(t, fk)} AS k FROM {qtable(src)}{where}) f "
                          f"LEFT ANTI JOIN {qtable(osrc)} d ON f.k = d.{pcol(other, ok_col)} WHERE f.k IS NOT NULL")
            check(t, "orphan fact rows " + label, orphan_sql,
                  lambda r, note=note: (f"orphan rows={r.get('orphan_rows')}{note}",
                                        "pass" if int(r.get("orphan_rows") or 0) == 0 else "review"))

    # Cross-fact ratios: denominator unique on shared columns?
    for b in base_report:
        if str(b.get("cross_fact_ratio")).lower() != "true":
            continue
        nums = [x for x in b["numerator_tables"].split(";") if x]
        dens = [x for x in b["denominator_tables"].split(";") if x]
        for nt in nums:
            for dt in dens:
                dsrc = dbx_source(dt)
                if not dsrc:
                    continue
                ncols = {phys.get((nt, c["column"]), c["column"]) for c in cols if c["table"] == nt}
                dcols = {phys.get((dt, c["column"]), c["column"]) for c in cols if c["table"] == dt}
                shared = sorted((ncols & dcols) - {""})
                if not shared:
                    results.append({"table": dt, "check": f"cross-fact {b['base_metric']}",
                                    "result": f"no shared columns between {nt} and {dt}", "status": "review",
                                    "sql": ""})
                    continue
                dfact = next((x for x in facts if x["table"] == dt), {})
                ddate = dfact.get("date_column", "").strip()
                key_cols = [q(c) for c in shared]
                label_cols = list(shared)
                dwhere = ""
                if ddate:
                    dphys = phys.get((dt, ddate), ddate)
                    if dphys not in shared:
                        key_cols.append(f"date_trunc('MONTH', {q(dphys)})")
                        label_cols.append(f"month of {dphys}")
                    if args.months > 0:
                        dwhere = f" WHERE {q(dphys)} >= add_months(current_date(), -{args.months})"
                kp = ", ".join(key_cols)
                sql = (f"SELECT COUNT(*) AS row_count, COUNT(DISTINCT {kp}) AS distinct_shared "
                       f"FROM {qtable(dsrc)}{dwhere}")
                check(dt, f"cross-fact {b['base_metric']}: {dt} unique on ({', '.join(label_cols)})", sql,
                      lambda r: (f"rows={r.get('row_count')}, distinct on shared columns={r.get('distinct_shared')}",
                                 "pass" if r.get("row_count") == r.get("distinct_shared") else "review"))

    with open(os.path.join(out, "grain_checks.sql"), "w", encoding="utf-8") as f:
        f.write("\n".join(sql_log))
    with open(os.path.join(out, "grain_checks.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["table", "check", "result", "status", "sql"])
        w.writeheader()
        w.writerows(results)
    bad = [r for r in results if r["status"] in ("fail", "error", "review", "incomplete", "skipped")]
    lines = ["# Grain checks", "",
             f"- Checks: {len(results)}; passed: {sum(1 for r in results if r['status'] == 'pass')}; "
             f"needing attention: {len(bad)}", "",
             "| Table | Check | Status | Result |", "|---|---|---|---|"]
    lines += [f"| {r['table']} | {r['check']} | {r['status']} | {r['result']} |" for r in results]
    lines += ["", "How to read this:",
              "- **fact grain fail**: the key does not identify one row. The real grain is finer, or there are "
              "duplicates. Measures on this table can double-count.",
              "- **one-side duplicates fail**: the dimension key repeats, so joining to it multiplies fact rows "
              "(fan-out). This is a common cause of parity mismatches.",
              "- **orphan fact rows**: rows with no matching dimension. Power BI shows them under a blank member.",
              "- **cross-fact review**: the denominator (e.g. member months) repeats on the shared columns, so "
              "ratios like PMPM need a proper shared-grain view.", ""]
    with open(os.path.join(out, "grain_checks.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"\nWrote {os.path.join(out, 'grain_checks.md')}, grain_checks.csv and grain_checks.sql")


if __name__ == "__main__":
    main()
