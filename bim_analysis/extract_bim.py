#!/usr/bin/env python3
"""
extract_bim.py - Extract a Power BI semantic model (.bim, TMSL JSON) into reviewable files.

Usage:
    python extract_bim.py model.bim --out out

Writes to the output folder:
    tables.csv              one row per table, with source class and row-cap flags
    columns.csv             one row per column, including the physical (Databricks) column name
    measures.json           every measure with its DAX exactly as written (line breaks kept)
    measures.csv            the same measures without expressions, for quick browsing
    relationships.csv       relationships with Power BI defaults filled in
    sources.csv             Databricks catalog/schema/table per table, parsed from Power Query (M)
    parameters.csv          Power Query parameters (e.g. catalog name, row limit)
    disconnected_tables.csv tables with no relationships (often selectors or parameters)
    rls.csv                 row-level security role filters
    excluded_objects.csv    auto-generated date tables and relationships that were excluded
    extract_summary.md      counts and warnings

Standard library only. No data is read, only model metadata.
"""
import argparse
import csv
import json
import os
import re
import sys
from collections import defaultdict

AUTO_DATE_PREFIXES = ("LocalDateTable_", "DateTableTemplate_")
AUTO_DATE_ANNOTATIONS = {"__PBI_LocalDateTable", "__PBI_TemplateDateTable"}

# Power Query functions that only rename, retype or select columns (source stays "direct")
M_LIGHT = {"Table.RenameColumns", "Table.TransformColumnTypes", "Table.SelectColumns",
           "Table.RemoveColumns", "Table.ReorderColumns", "Table.PromoteHeaders"}
M_ROWCAP = {"Table.FirstN", "Table.Range", "Table.Skip", "Table.LastN"}
M_NON_DATABRICKS = ("Excel.Workbook", "SharePoint.", "Sql.Database", "Csv.Document", "Web.Contents",
                    "Odbc.", "OleDb.", "Snowflake.", "GoogleBigQuery.", "Oracle.", "File.Contents",
                    "Folder.Files", "AzureStorage.", "PowerPlatform.", "Dataverse.", "Salesforce.")
M_ENTER_DATA = "Binary.Decompress(Binary.FromText("
DATABRICKS_SOURCE_RE = re.compile(
    r'\b(?:Databricks|DatabricksMultiCloud|AzureDatabricks)\.(?:Catalogs|Query|Contents)\s*\(\s*"([^"]*)"\s*,\s*"([^"]*)"')
NAV_RE = re.compile(
    r'\[\s*(?:Name\s*=\s*(?P<n1>"(?:[^"]|"")*"|[A-Za-z_#][\w."# ]*?)\s*,\s*Kind\s*=\s*"(?P<k1>\w+)"'
    r'|Kind\s*=\s*"(?P<k2>\w+)"\s*,\s*Name\s*=\s*(?P<n2>"(?:[^"]|"")*"|[A-Za-z_#][\w."# ]*?))\s*\]')
NATIVE_QUERY_RE = re.compile(r'Value\.NativeQuery\s*\((?:[^"]|"(?:[^"]|"")*")*?,\s*"((?:[^"]|"")*)"', re.S)
RENAME_CALL_RE = re.compile(r'Table\.RenameColumns\s*\(')
PAIR_RE = re.compile(r'\{\s*"((?:[^"]|"")*)"\s*,\s*"((?:[^"]|"")*)"\s*\}')
M_FUNC_RE = re.compile(r'\b(Table\.[A-Za-z]+)\s*\(')
SQL_TABLE_RE = re.compile(r'\b(?:FROM|JOIN)\s+([`"\w.]+)', re.I)
SQL_LIMIT_RE = re.compile(r'\b(?:LIMIT|TOP)\s+(\d+)', re.I)
PARAM_RE = re.compile(r'^\s*(.+?)\s+meta\s*\[\s*IsParameterQuery\s*=\s*true', re.S | re.I)
SHARED_REF_RE = re.compile(r'#"((?:[^"]|"")+)"|\b([A-Za-z_][A-Za-z0-9_]*)\b')


def text(expr):
    """TMSL stores expressions as a string or a list of lines."""
    if expr is None:
        return ""
    if isinstance(expr, list):
        return "\n".join(str(x) for x in expr)
    return str(expr)


def load_bim(path):
    for enc in ("utf-8-sig", "utf-16", "utf-8"):
        try:
            with open(path, encoding=enc) as f:
                return json.load(f)
        except (UnicodeError, json.JSONDecodeError):
            continue
    sys.exit(f"Could not read {path} as JSON (tried UTF-8 and UTF-16).")


def find_model(doc):
    """Accept a plain .bim, a database object, or a TMSL createOrReplace script."""
    if isinstance(doc, dict):
        if "tables" in doc and ("relationships" in doc or "culture" in doc or "expressions" in doc):
            return doc
        if "model" in doc and isinstance(doc["model"], dict):
            return doc["model"]
        for key in ("createOrReplace", "create", "alter", "database", "object"):
            if key in doc:
                found = find_model(doc[key])
                if found:
                    return found
    return None


def is_auto_date(table):
    if table.get("name", "").startswith(AUTO_DATE_PREFIXES):
        return True
    names = {a.get("name") for a in table.get("annotations", []) if isinstance(a, dict)}
    return bool(names & AUTO_DATE_ANNOTATIONS)


def unquote_m(s):
    s = s.strip()
    if s.startswith('"') and s.endswith('"'):
        return s[1:-1].replace('""', '"'), True
    return s, False


def parse_parameters(expressions):
    params = {}
    rows = []
    for e in expressions:
        body = text(e.get("expression"))
        m = PARAM_RE.match(body)
        if m:
            raw = m.group(1).strip()
            val, quoted = unquote_m(raw)
            params[e["name"]] = val
            rows.append({"name": e["name"], "value": val, "is_parameter": True,
                         "looks_like_row_limit": bool(re.search(r"row|limit|top|sample", e["name"], re.I))})
        else:
            rows.append({"name": e["name"], "value": "", "is_parameter": False, "looks_like_row_limit": False})
    return params, rows


def resolve_shared(m_code, shared, depth=0):
    """Append the code of shared queries this M references, so navigation inside them is found."""
    if depth > 3 or not shared:
        return m_code
    extra = []
    for q, plain in SHARED_REF_RE.findall(m_code):
        name = (q or plain).replace('""', '"')
        if name in shared:
            extra.append(resolve_shared(shared[name], shared, depth + 1))
    return m_code + ("\n" + "\n".join(extra) if extra else "")


def parse_source(m_code, params, shared):
    """Work out where a table's data comes from and what Power Query does to it."""
    full = resolve_shared(m_code, shared)
    info = {"source_class": "", "host": "", "http_path": "", "catalog": "", "schema": "", "table": "",
            "databricks_source": "", "native_sql": "", "native_sql_tables": "", "row_cap": "",
            "m_functions": "", "renames": []}
    if not full.strip():
        info["source_class"] = "unknown"
        return info
    m = DATABRICKS_SOURCE_RE.search(full)
    if m:
        info["host"], info["http_path"] = m.group(1), m.group(2)
    nav = {}
    for nm in NAV_RE.finditer(full):
        name = nm.group("n1") or nm.group("n2")
        kind = (nm.group("k1") or nm.group("k2") or "").lower()
        val, quoted = unquote_m(name)
        if not quoted:
            val = params.get(val.strip('#"'), "{param:" + val + "}")
        nav.setdefault(kind, val)  # first occurrence of each level
    info["catalog"] = nav.get("database", "")
    info["schema"] = nav.get("schema", "")
    info["table"] = nav.get("table", nav.get("view", ""))
    if info["catalog"] and info["schema"] and info["table"]:
        info["databricks_source"] = f'{info["catalog"]}.{info["schema"]}.{info["table"]}'
    nq = NATIVE_QUERY_RE.search(full)
    if nq:
        sql = nq.group(1).replace('""', '"')
        info["native_sql"] = sql
        info["native_sql_tables"] = ";".join(sorted({t.strip('`"') for t in SQL_TABLE_RE.findall(sql)}))
        lim = SQL_LIMIT_RE.search(sql)
        if lim:
            info["row_cap"] = f"SQL LIMIT/TOP {lim.group(1)}"
    funcs = M_FUNC_RE.findall(m_code)
    info["m_functions"] = ";".join(sorted(set(funcs)))
    caps = [f for f in funcs if f in M_ROWCAP]
    if caps:
        cap_args = re.findall(r'Table\.(?:FirstN|Range|LastN)\s*\([^,]+,\s*([^)\s,]+)', m_code)
        cap_vals = [params.get(a, a) for a in cap_args]
        info["row_cap"] = "; ".join(filter(None, [info["row_cap"], f"{','.join(caps)} ({','.join(cap_vals)})"]))
    for call in RENAME_CALL_RE.finditer(m_code):
        block = call_arguments(m_code, call.end() - 1)
        for old, new in PAIR_RE.findall(block):
            info["renames"].append((old.replace('""', '"'), new.replace('""', '"')))
    heavy = [f for f in funcs if f not in M_LIGHT and f not in M_ROWCAP]
    if nq:
        info["source_class"] = "native_query"
    elif M_ENTER_DATA in full:
        info["source_class"] = "entered_data"
    elif any(tok in full for tok in M_NON_DATABRICKS) and not info["host"]:
        info["source_class"] = "non_databricks"
    elif info["databricks_source"]:
        info["source_class"] = "transformed" if heavy else "direct"
    else:
        info["source_class"] = "unknown"
    return info


def call_arguments(code, open_paren_index):
    """Return the text between a '(' and its matching ')', ignoring parentheses inside M strings."""
    depth, i, in_str = 0, open_paren_index, False
    while i < len(code):
        ch = code[i]
        if in_str:
            if ch == '"':
                if i + 1 < len(code) and code[i + 1] == '"':
                    i += 1
                else:
                    in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return code[open_paren_index + 1:i]
        i += 1
    return code[open_paren_index + 1:]


def write_csv(path, rows, fields):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("bim", help="path to model.bim")
    ap.add_argument("--out", default="out", help="output folder (default: out)")
    ap.add_argument("--mappings", help="CSV with columns pbi_table, databricks_table (catalog.schema.table) "
                                       "for tables whose source could not be parsed")
    args = ap.parse_args()
    mappings = {}
    if args.mappings:
        with open(args.mappings, encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                if r.get("pbi_table") and (r.get("databricks_table") or "").strip():
                    mappings[r["pbi_table"].strip()] = r["databricks_table"].strip()
    os.makedirs(args.out, exist_ok=True)

    model = find_model(load_bim(args.bim))
    if not model:
        sys.exit("No model found in the file. Is this a Power BI / Analysis Services .bim?")

    params, param_rows = parse_parameters(model.get("expressions", []))
    shared = {e["name"]: text(e.get("expression")) for e in model.get("expressions", [])}

    all_tables = model.get("tables", [])
    auto = {t["name"] for t in all_tables if is_auto_date(t)}
    tables = [t for t in all_tables if t["name"] not in auto]
    excluded = [{"object": "table", "name": n, "reason": "Power BI auto date/time table"} for n in sorted(auto)]

    rels_raw = model.get("relationships", [])
    rels, seen = [], set()
    for r in rels_raw:
        if r.get("fromTable") in auto or r.get("toTable") in auto:
            excluded.append({"object": "relationship", "name": r.get("name", ""),
                             "reason": f'touches auto date table ({r.get("fromTable")} -> {r.get("toTable")})'})
            continue
        row = {
            "name": r.get("name", ""),
            "from_table": r.get("fromTable"), "from_column": r.get("fromColumn"),
            "to_table": r.get("toTable"), "to_column": r.get("toColumn"),
            "from_cardinality": r.get("fromCardinality", "many"),
            "to_cardinality": r.get("toCardinality", "one"),
            "cross_filtering_behavior": r.get("crossFilteringBehavior", "oneDirection"),
            "is_active": r.get("isActive", True),
        }
        key = (row["from_table"], row["from_column"], row["to_table"], row["to_column"])
        row["is_duplicate"] = key in seen
        seen.add(key)
        rels.append(row)
    rel_count = defaultdict(int)
    for r in rels:
        rel_count[r["from_table"]] += 1
        rel_count[r["to_table"]] += 1

    table_rows, col_rows, measures, source_rows, disc_rows = [], [], [], [], []
    for t in tables:
        name = t["name"]
        parts = t.get("partitions", [])
        part = parts[0] if parts else {}
        src = part.get("source", {}) if part else {}
        src_type = src.get("type", "")
        if src_type == "calculated":
            info = {"source_class": "calculated_table", "renames": []}
            source_expr = text(src.get("expression"))
        else:
            m_code = text(src.get("expression")) or text(src.get("query"))
            if not m_code and t.get("refreshPolicy"):
                m_code = text(t["refreshPolicy"].get("sourceExpression"))
            info = parse_source(m_code, params, shared)
            source_expr = m_code
        if name in mappings:
            full = mappings[name]
            parts = full.split(".")
            info["databricks_source"] = full
            if len(parts) == 3:
                info["catalog"], info["schema"], info["table"] = parts
            if info.get("source_class") in ("unknown", "", "calculated_table", "entered_data", "non_databricks"):
                info["source_class"] = "mapped"
        is_calc_group = "calculationGroup" in t
        if is_calc_group:
            info["source_class"] = "calculation_group"
        rename_back = {new: old for old, new in info.get("renames", [])}

        n_cols = 0
        for c in t.get("columns", []):
            ctype = c.get("type", "data")
            if ctype == "rowNumber":
                continue
            n_cols += 1
            source_col = c.get("sourceColumn", "")
            col_rows.append({
                "table": name, "column": c.get("name"), "data_type": c.get("dataType", ""),
                "type": ctype, "is_hidden": c.get("isHidden", False), "is_key": c.get("isKey", False),
                "source_column": source_col,
                "physical_column": rename_back.get(source_col, source_col) if ctype == "data" else "",
                "expression": text(c.get("expression")).replace("\n", " ") if ctype == "calculated" else "",
            })
        for ms in t.get("measures", []):
            measures.append({
                "table": name, "name": ms.get("name"),
                "expression": text(ms.get("expression")),
                "format_string": ms.get("formatString", ""),
                "format_string_expression": text((ms.get("formatStringDefinition") or {}).get("expression")),
                "display_folder": ms.get("displayFolder", ""),
                "description": text(ms.get("description")),
                "is_hidden": ms.get("isHidden", False),
            })
        calc_items = []
        if is_calc_group:
            for item in t["calculationGroup"].get("calculationItems", []):
                calc_items.append({"name": item.get("name"), "expression": text(item.get("expression"))})
        table_rows.append({
            "table": name, "is_hidden": t.get("isHidden", False),
            "is_calculated_table": src_type == "calculated", "is_calculation_group": is_calc_group,
            "partition_mode": part.get("mode", "default") if part else "",
            "source_class": info.get("source_class", ""),
            "databricks_source": info.get("databricks_source", ""),
            "row_cap": info.get("row_cap", ""),
            "n_columns": n_cols, "n_measures": len(t.get("measures", [])),
            "n_relationships": rel_count.get(name, 0),
            "calculation_items": json.dumps(calc_items) if calc_items else "",
        })
        source_rows.append({
            "table": name, "source_class": info.get("source_class", ""),
            "databricks_source": info.get("databricks_source", ""),
            "catalog": info.get("catalog", ""), "schema": info.get("schema", ""), "source_table": info.get("table", ""),
            "host": info.get("host", ""), "http_path": info.get("http_path", ""),
            "native_sql_tables": info.get("native_sql_tables", ""), "native_sql": info.get("native_sql", ""),
            "row_cap": info.get("row_cap", ""), "m_functions": info.get("m_functions", ""),
            "renames": json.dumps(info.get("renames", [])), "source_expression": source_expr,
        })
        if rel_count.get(name, 0) == 0:
            disc_rows.append({"table": name, "source_class": info.get("source_class", ""),
                              "n_columns": n_cols, "n_measures": len(t.get("measures", [])),
                              "columns": ";".join(c.get("name", "") for c in t.get("columns", [])
                                                  if c.get("type") != "rowNumber"),
                              "source_expression": source_expr.replace("\n", " ")[:500]})

    rls_rows = []
    for role in model.get("roles", []):
        for tp in role.get("tablePermissions", []):
            rls_rows.append({"role": role.get("name"), "table": tp.get("name"),
                             "filter_expression": text(tp.get("filterExpression"))})

    out = args.out
    write_csv(os.path.join(out, "tables.csv"), table_rows, list(table_rows[0].keys()) if table_rows else ["table"])
    write_csv(os.path.join(out, "columns.csv"), col_rows,
              ["table", "column", "data_type", "type", "is_hidden", "is_key", "source_column", "physical_column", "expression"])
    with open(os.path.join(out, "measures.json"), "w", encoding="utf-8") as f:
        json.dump(measures, f, indent=2, ensure_ascii=False)
    write_csv(os.path.join(out, "measures.csv"), measures,
              ["table", "name", "display_folder", "is_hidden", "format_string", "description"])
    write_csv(os.path.join(out, "relationships.csv"), rels,
              ["name", "from_table", "from_column", "to_table", "to_column", "from_cardinality",
               "to_cardinality", "cross_filtering_behavior", "is_active", "is_duplicate"])
    write_csv(os.path.join(out, "sources.csv"), source_rows, list(source_rows[0].keys()) if source_rows else ["table"])
    write_csv(os.path.join(out, "parameters.csv"), param_rows, ["name", "value", "is_parameter", "looks_like_row_limit"])
    write_csv(os.path.join(out, "disconnected_tables.csv"), disc_rows,
              ["table", "source_class", "n_columns", "n_measures", "columns", "source_expression"])
    write_csv(os.path.join(out, "rls.csv"), rls_rows, ["role", "table", "filter_expression"])
    write_csv(os.path.join(out, "excluded_objects.csv"), excluded, ["object", "name", "reason"])

    # Summary
    by_class = defaultdict(int)
    for r in table_rows:
        by_class[r["source_class"] or "unknown"] += 1
    capped = [r for r in table_rows if r["row_cap"]]
    dupes = sum(1 for r in rels if r["is_duplicate"])
    inactive = sum(1 for r in rels if not r["is_active"])
    m2m = sum(1 for r in rels if r["from_cardinality"] == "many" and r["to_cardinality"] == "many")
    both = sum(1 for r in rels if r["cross_filtering_behavior"] == "bothDirections")
    lines = [
        "# Extraction summary", "",
        f"- Tables: {len(all_tables)} in file, {len(auto)} auto date/time tables excluded, **{len(tables)} kept**",
        f"- Relationships: {len(rels_raw)} in file, {len(rels_raw) - len(rels)} excluded with auto date tables, "
        f"**{len(rels)} kept** ({inactive} inactive, {m2m} many-to-many, {both} bidirectional, {dupes} duplicates)",
        f"- Columns: {len(col_rows)}",
        f"- Measures: **{len(measures)}** (expressions kept exactly as written in measures.json)",
        f"- Disconnected tables: {len(disc_rows)}",
        f"- Tables mapped from --mappings: {sum(1 for r in table_rows if r['table'] in mappings)}"
        + (f" (not found in the model: {', '.join(sorted(set(mappings) - {r['table'] for r in table_rows}))})"
           if set(mappings) - {r['table'] for r in table_rows} else ""),
        f"- Row-level security filters: {len(rls_rows)}",
        "", "## Tables by source", "",
        "| Source class | Tables |", "|---|---|",
    ] + [f"| {k} | {v} |" for k, v in sorted(by_class.items(), key=lambda x: -x[1])] + [
        "", "Source classes: **direct** = Databricks table, only renames or type changes in Power Query; "
        "**transformed** = Power Query filters, adds columns or merges (that logic must move to gold); "
        "**native_query** = SQL written in Power Query; **calculated_table** = built in DAX; "
        "**entered_data** = typed into Power BI by hand; **non_databricks** = another source; "
        "**mapped** = source set by hand in --mappings; "
        "**unknown** = could not parse the source.", "",
    ]
    if capped:
        lines += ["## Row caps found", "", "These tables load only part of the data in this file:", ""]
        lines += [f"- {r['table']}: {r['row_cap']}" for r in capped]
        lines += [""]
    rl = [p for p in param_rows if p["looks_like_row_limit"]]
    if rl:
        lines += ["## Parameters that look like row limits", ""]
        lines += [f"- {p['name']} = {p['value']}" for p in rl] + [""]
    unresolved = [r for r in source_rows if "{param:" in r.get("databricks_source", "")]
    if unresolved:
        lines += ["## Sources using parameters that could not be resolved", ""]
        lines += [f"- {r['table']}: {r['databricks_source']}" for r in unresolved] + [""]
    with open(os.path.join(out, "extract_summary.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print("\n".join(lines))
    print(f"\nFiles written to {os.path.abspath(out)}")


if __name__ == "__main__":
    main()
