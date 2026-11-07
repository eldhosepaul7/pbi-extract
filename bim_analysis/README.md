# Power BI model extraction and analysis

Three scripts that turn a Power BI model file (`model.bim`) into the information you need to rebuild its metrics in Databricks: which measures are base metrics and which are variants, which fact tables they use, and whether those tables have the grain the model assumes.

No Power BI experience is needed. Nothing here reads data from the .pbix, and nothing writes to Databricks.

| Step | Script | Needs | Output you read |
|---|---|---|---|
| 1 | `extract_bim.py` | `model.bim` | `out/extract_summary.md` |
| 2 | `list_metrics.py` | step 1 output | `base_metrics_auto.txt` (delete what you don't need) |
| 3 | `analyze_model.py` | step 1 output + `base_metrics.txt` | `out/analysis.md` |
| 3b | `find_uc_tables.py` | Databricks CLI sign-in | `table_mappings.csv` for tables without a source |
| 4 | (you) | | edit `out/fact_tables.csv` |
| 5 | `grain_checks.py` | Databricks CLI sign-in, a SQL warehouse | `out/grain_checks.md` |
| 6 | (you + Copilot) | | `out/measure_map.csv` reviewed |

## Before you start

- Python 3.10 or later. Steps 1 and 3 use only the standard library.
- For step 5 only: `pip install databricks-sdk`, and sign in once with `databricks auth login --host <your workspace URL>`. Find a SQL warehouse ID with `databricks warehouses list`; a small warehouse is fine.
- Put `model.bim` in this folder.

To try everything on a small example first (about a minute), run the commands below with `example/model.bim` and `example/base_metrics.txt`.

## Step 1: Extract

```
python extract_bim.py model.bim --out out
```

Open `out/extract_summary.md` and check:

- **Tables kept vs excluded.** Power BI's Auto date/time feature silently creates a hidden date table and relationship for every date column. These are excluded automatically and listed in `excluded_objects.csv`. Expect the table and relationship counts to drop a lot.
- **Tables by source.** `direct` means a Databricks table with only renames or type changes. `transformed` means Power Query filters, adds columns or merges, and that logic must move into gold. `calculated_table`, `entered_data` and `non_databricks` have no Databricks table behind them. `unknown` means the source could not be parsed: check those few by hand in `sources.csv`.
- **Row caps.** Tables that load only part of the data (for example "Table.FirstN (10000)"). This matters for parity testing, not for this analysis.

DAX is saved exactly as written, with line breaks, in `measures.json`. Do not open and re-save it in Excel.

## Step 2: Build the base metric list

The easiest way needs no Power BI knowledge. It builds the list from the measure names themselves:

```
python list_metrics.py --out out
```

It groups measures by their core name, so `Total_AllowedAmount_Medical`, `Prior_AllowedAmount_Medical` and `Allowed%Change_Medical` become one metric, "Allowed Amount" (Medical). For each metric it picks the base measure: `Total_X` if one exists, otherwise `Current_X`. It writes:

- `base_metrics_auto.txt`: one line per metric, ready to use. Delete the lines for metrics business users don't need. Lines starting with `#` are set aside: helpers, data-quality (`_DQ`) measures, and families that have only variants.
- `out/metric_inventory.csv`: every metric with its base measure, its variants and all its measures. This is useful for reviewing the list with the business.

Selector and formatting measures (`MetricSelector_*`, `ShowSlicer_*`, colors) are left out automatically.

The file format, if you edit by hand, is one base metric per line, spelled exactly as in Power BI, with an optional domain in front:

```
# domain|measure name
Medical|Total_AllowedAmount_Medical
Pharmacy|Current_AllowedPerScript(WeightedClaims)_Pharmacy
```

**If you start from a business list instead** (names like "Allowed Amount"), `match_base_names.py` finds the closest real measure for each name:

```
python match_base_names.py --out out --base-metrics my_business_list.txt
```

It writes `base_metrics_suggested.txt`. Resolve the lines marked `# CHECK` using `out/base_name_matches.csv`, which shows the top 5 candidates per name with their DAX.

## Step 3: Analyze

```
python analyze_model.py --out out --base-metrics base_metrics_auto.txt
```

Open `out/analysis.md`. Its sections:

- **Summary.** If "Base metrics not found" appears, a name in your list doesn't match the model. Fix the spelling and rerun.
- **Base metrics.** Each metric's kind (additive, distinct count, ratio and so on), the tables in its numerator and denominator, and whether it is a **cross-fact** ratio, like PMPM (claims ÷ eligibility). Cross-fact ratios need a shared-grain view or a trusted query in Databricks.
- **Fact tables in scope.** Only the tables your base metrics actually aggregate, with their Databricks source. Expect a handful, not dozens.
- **Variant types.** How many prior, change, YTD, rolling and other variants exist.
- **Needs review.** Measures the script could not classify with certainty, grouped by reason.

`out/measure_map.csv` has one row per measure. The **role** column:

| Role | Meaning | What to do |
|---|---|---|
| base | In your base list | Convert carefully, one by one |
| variant | Prior, change, YTD, etc. of a base metric | Covered by variant templates |
| candidate_base | Built from base metrics in a way that is really a new metric (e.g. Paid ÷ Scripts, or an average per member) | Decide: add it to `base_metrics.txt`, or treat as a variant |
| selector | Switches between measures based on a slicer | Not migrating: the chat question picks the metric |
| visual | Returns colors or labels | Not migrating |
| helper | Hidden building block used by other measures | Usually not migrating on its own |
| unmapped | No link to any base metric | Check: a missing base metric, or not needed |

The **status** column is `certain`, `review` or `uncertain`, with the reasons alongside.

## Step 3b: Map tables that have no Databricks source

Some tables show `—` as their Databricks source in "Fact tables in scope", with source class `unknown`. That usually means Power Query loads them in a way the parser can't read, such as through a custom function. Let Unity Catalog find them:

```
python find_uc_tables.py --out out --dry-run
python find_uc_tables.py --out out --warehouse-id <WAREHOUSE_ID> --profile <PROFILE> --catalogs gold
```

It searches Unity Catalog by table name, then confirms each candidate by comparing column names. It reads metadata only (`information_schema`), never data. By default it only looks at the tables your base metrics use and the tables they join to; add `--all` for every table. `--catalogs` limits the search, which makes it faster.

Open `table_mappings_suggested.csv`:

- **confident**: the name and columns match. Keep it.
- **CHECK: columns differ**: probably the right table, with Power Query renaming or computing columns. That logic must be recreated in a gold view.
- **CHECK: several similar tables**: pick the right one from `out/uc_table_candidates.csv`.
- **NOT FOUND**: fill in `catalog.schema.table` by hand, or ask whoever owns the pipeline.

Save it as `table_mappings.csv`, then rerun:

```
python extract_bim.py model.bim --out out --mappings table_mappings.csv
python analyze_model.py --out out --base-metrics base_metrics_auto.txt
```

Some tables have no Unity Catalog table by design. `out/unmapped_tables.md` lists them with what to do:

| Kind | What to do |
|---|---|
| Calculated (DAX) date table | Use your gold date dimension |
| Calculated summary table used by metrics | Rebuild as a gold view |
| Selector or parameter table (entered data) | Not needed: the chat question replaces it |
| Mapping or threshold table (entered data) | Land it in the lakehouse as a small table |
| Excel, SharePoint or another database | Land it in the lakehouse if metrics need it |

**`_Agg` tables are pre-aggregated.** Map them so parity can be checked against them, but build the metric views on the detail tables they are computed from. Unity Catalog lineage shows those:

```sql
SELECT DISTINCT target_table_full_name, source_table_full_name
FROM system.access.table_lineage
WHERE lower(target_table_full_name) LIKE '%agg'
```

## Step 4: Confirm the grain key of each fact table

Open `out/fact_tables.csv`. For each table, check two columns:

- **grain_key**: the columns that should identify exactly one row, separated by `;`. It is pre-filled with a guess. Claim lines are usually claim ID + line number. A claim header is the claim ID alone. Eligibility is usually member + month. For `_Agg` tables it is every column the table was grouped by.
- **date_column**: the main date, used to limit scans to recent months.

**Relationship columns** are foreign keys: they show which dimensions a fact links to, not what makes a row unique.

## Step 5: Check grain in Databricks

First a dry run, which writes the SQL without running anything:

```
python grain_checks.py --out out --dry-run
```

Read `out/grain_checks.sql`. Every statement is a `SELECT` returning counts or dates; no member, patient or claim identifiers. Then run it:

```
python grain_checks.py --out out --warehouse-id <WAREHOUSE_ID> --profile <PROFILE>
```

`--months 12` (the default) limits fact scans to the last 12 months to keep cost down. Use `--only "Table Name"` to rerun a single table.

Open `out/grain_checks.md`:

| Result | Meaning |
|---|---|
| fact grain **fail** | The key doesn't identify one row: the real grain is finer, or there are duplicates. Measures on this table can double-count |
| one-side duplicates **fail** | A dimension key repeats, so joins multiply fact rows (fan-out). A common cause of parity mismatches |
| orphan fact rows **review** | Fact rows with no matching dimension. Power BI shows them under a blank member |
| cross-fact **review** | The denominator, e.g. member months, repeats per member per month, so PMPM-style ratios need a proper shared-grain view |
| **error** "column not found" | Power Query renamed or computed that column. Fix `physical_column` for it in `out/columns.csv` and rerun |

## Step 6: Review the uncertain measures with Copilot

Copilot is useful here, on a small set, with the deterministic output as its starting point. In VS Code Agent mode:

```
Read out/measure_map.csv and out/measures.json. For every row whose status is "review" or
"uncertain", read the measure's DAX and the reasons column, then propose role, base_metric and
variant_type with a one-line justification. Do not change rows whose status is "certain".
Write out/measure_map_reviewed.csv with two extra columns, proposed_change and justification.
List any measure you still can't classify, and say what information would resolve it.
```

Take anything still unclear to the Power BI developer who built the model.

## What to send for the next step

From `analysis.md`: the Base metrics table, Fact tables in scope and Needs review. Also send `grain_checks.md`. Remove server hostnames and warehouse IDs before sharing outside your organization.

## Troubleshooting

- **"No model found in the file"**: the file isn't a .bim. A `.pbit` contains a `DataModelSchema` file in the same format: rename the .pbit to .zip, extract it, and pass `DataModelSchema` instead.
- **Sources show `{param:Something}`**: a Power Query parameter couldn't be resolved. Its value is in `parameters.csv`.
- **Many measures `unmapped`**: usually base metric names that don't match. Check "Base metrics not found" in analysis.md.
- **Many `references not found in model`**: the measure refers to something outside this model, or a name was renamed. Check one example in `measures.json`.
