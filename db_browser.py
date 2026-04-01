import marimo

__generated_with = "0.21.1"
app = marimo.App(width="full", app_title="CLO Database Browser")


@app.cell
def setup():
    import marimo as mo
    import sys, os
    import pandas as pd
    import sqlite3

    ROOT = os.path.dirname(os.path.abspath("__file__"))
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)

    from backend import config
    DB_PATH = os.path.join(config.DATA_DIR, "clo.db")
    conn = sqlite3.connect(DB_PATH)

    return mo, pd, conn, DB_PATH


@app.cell
def header(mo, conn):
    counts = {}
    for table in ["deals", "transactions", "managers", "training_data", "extraction_log"]:
        counts[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    tabs = mo.ui.tabs({
        "Browse Tables": "browse",
        "SQL Query": "sql",
    })

    mo.output.replace(mo.vstack([
        mo.md("# CLO Database Browser"),
        mo.hstack([
            mo.stat(value=counts["deals"], label="Deals", bordered=True),
            mo.stat(value=counts["transactions"], label="Transactions", bordered=True),
            mo.stat(value=counts["managers"], label="Managers", bordered=True),
            mo.stat(value=counts["training_data"], label="Training Pairs", bordered=True),
        ], justify="center", gap=1),
        tabs,
    ]))
    return tabs,


# ============================================================================
# BROWSE TAB
# ============================================================================

@app.cell
def browse_controls(mo, tabs):
    mo.stop(tabs.value != "browse")

    table_select = mo.ui.dropdown(
        options=["deals", "transactions", "managers", "training_data", "extraction_log"],
        value="deals",
        label="Table",
    )
    search_text = mo.ui.text(value="", label="Search", placeholder="Filter rows...", full_width=True)

    return table_select, search_text


@app.cell
def browse_view(mo, tabs, table_select, search_text, conn, pd):
    mo.stop(tabs.value != "browse")

    table = table_select.value
    search = search_text.value.strip()

    # Get columns
    cols_df = pd.read_sql(f"PRAGMA table_info({table})", conn)
    columns = cols_df["name"].tolist()

    # Schema info
    schema_rows = []
    for _, row in cols_df.iterrows():
        pk = "PK" if row["pk"] else ""
        tags = pk
        schema_rows.append(f"| `{row['name']}` | `{row['type']}` | {tags} |")

    rels = {"deals": "- `title` ← transactions.deal\n- `collateral_manager` → managers.name",
            "transactions": "- `deal` → deals.title\n- `collateral_manager` → managers.name",
            "managers": "- `name` ← deals.collateral_manager"}.get(table, "")

    schema = mo.md(f"| Column | Type | |\n|---|---|---|\n" + "\n".join(schema_rows)
                   + ("\n\n" + rels if rels else ""))

    # Query
    if search:
        conditions = " OR ".join(f"CAST({c} AS TEXT) LIKE ?" for c in columns)
        params = tuple(f"%{search}%" for _ in columns)
        df = pd.read_sql(f"SELECT * FROM {table} WHERE {conditions} LIMIT 500", conn, params=params)
        info = f"**{len(df)}** rows matching `{search}`"
    else:
        df = pd.read_sql(f"SELECT * FROM {table} LIMIT 500", conn)
        total = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        info = f"**{len(df)}** of **{total:,}** rows"

    # Truncate long text for display
    for col in df.columns:
        df[col] = df[col].apply(lambda x: (str(x)[:80] + "...") if x and len(str(x)) > 80 else x)

    data_table = mo.ui.table(df, page_size=25, selection="single", label=table)

    mo.output.replace(mo.vstack([
        mo.hstack([table_select, search_text], widths=[1, 3], gap=1),
        schema,
        mo.md(info),
        data_table,
    ]))
    return data_table,


@app.cell
def row_detail(mo, tabs, data_table, table_select, conn, pd):
    mo.stop(tabs.value != "browse")
    if data_table.value is None or data_table.value.empty:
        return

    row = data_table.value.iloc[0].to_dict()
    table = table_select.value

    # Full values (untruncated) — re-fetch from DB
    row_id = row.get("id")
    if row_id:
        full = pd.read_sql(f"SELECT * FROM {table} WHERE id = ?", conn, params=(row_id,))
        if not full.empty:
            row = full.iloc[0].to_dict()

    detail = []
    for k, v in row.items():
        val = str(v) if v is not None else ""
        if len(val) > 120:
            detail.append(f"**{k}:**\n```\n{val}\n```")
        else:
            detail.append(f"**{k}:** `{val}`")

    # Related records
    related = mo.md("")
    if table == "deals" and row.get("title"):
        txns = pd.read_sql(
            "SELECT id, deal, status, transaction_type, placement_agent, term, priced_date "
            "FROM transactions WHERE deal = ? LIMIT 20", conn, params=(row["title"],))
        if not txns.empty:
            related = mo.vstack([
                mo.md(f"#### Transactions for this deal ({len(txns)})"),
                mo.ui.table(txns, page_size=10),
            ])

    if table == "transactions" and row.get("deal"):
        deal = pd.read_sql("SELECT * FROM deals WHERE title = ?", conn, params=(row["deal"],))
        if not deal.empty:
            related = mo.vstack([mo.md("#### Parent Deal"), mo.ui.table(deal)])

    if table == "managers" and row.get("name"):
        deals = pd.read_sql(
            "SELECT id, title, collateral_type, bloomberg_deal_name "
            "FROM deals WHERE collateral_manager = ? LIMIT 20", conn, params=(row["name"],))
        if not deals.empty:
            related = mo.vstack([
                mo.md(f"#### Deals managed ({len(deals)})"),
                mo.ui.table(deals, page_size=10),
            ])

    mo.output.replace(mo.vstack([
        mo.md("### Row Detail"),
        mo.md("\n\n".join(detail)),
        related,
    ]))
    return


# ============================================================================
# SQL QUERY TAB
# ============================================================================

@app.cell
def sql_tab(mo, tabs):
    mo.stop(tabs.value != "sql")

    examples = mo.ui.dropdown(
        options={
            "Pick an example...": "",
            "All priced deals with pricing": "SELECT d.title, t.placement_agent, t.term, t.priced_date,\n       substr(t.final_pricing_details, 1, 100) as pricing\nFROM transactions t\nJOIN deals d ON t.deal = d.title\nWHERE t.status = 'Priced'\nORDER BY t.priced_date DESC\nLIMIT 50",
            "Deals by manager": "SELECT m.short_name, m.name, COUNT(d.id) as deal_count\nFROM managers m\nJOIN deals d ON d.collateral_manager = m.name\nGROUP BY m.short_name\nORDER BY deal_count DESC\nLIMIT 30",
            "Deals by placement agent": "SELECT t.placement_agent, COUNT(*) as count,\n       SUM(CASE WHEN t.status='Priced' THEN 1 ELSE 0 END) as priced,\n       SUM(CASE WHEN t.status='Announced' THEN 1 ELSE 0 END) as announced\nFROM transactions t\nWHERE t.placement_agent != ''\nGROUP BY t.placement_agent\nORDER BY count DESC",
            "Deals by collateral type": "SELECT collateral_type, COUNT(*) as count\nFROM deals\nWHERE collateral_type != ''\nGROUP BY collateral_type\nORDER BY count DESC",
            "Term distribution": "SELECT term, COUNT(*) as count\nFROM transactions\nWHERE term != ''\nGROUP BY term\nORDER BY count DESC\nLIMIT 30",
            "Recent transactions": "SELECT deal, status, transaction_type, placement_agent, term, priced_date\nFROM transactions\nORDER BY id DESC\nLIMIT 30",
            "Managers with most deals": "SELECT m.short_name, m.ultimate_parent, COUNT(d.id) as deals\nFROM managers m\nLEFT JOIN deals d ON d.collateral_manager = m.name\nGROUP BY m.name\nORDER BY deals DESC\nLIMIT 20",
            "Training data summary": "SELECT source, COUNT(*) as pairs,\n       MIN(accepted_at) as earliest,\n       MAX(accepted_at) as latest\nFROM training_data\nGROUP BY source",
        },
        value="",
        label="Examples",
    )
    return examples,


@app.cell
def sql_input(mo, tabs, examples):
    mo.stop(tabs.value != "sql")

    default_sql = examples.value if examples.value else "SELECT * FROM deals LIMIT 20"

    sql_query = mo.ui.text_area(
        value=default_sql,
        label="SQL",
        full_width=True,
    )
    run_btn = mo.ui.run_button(label="Run", kind="success")

    mo.output.replace(mo.vstack([examples, sql_query, run_btn]))
    return sql_query, run_btn


@app.cell
def sql_results(mo, tabs, run_btn, sql_query, conn, pd):
    mo.stop(tabs.value != "sql")
    mo.stop(not run_btn.value)

    query = sql_query.value.strip()
    if not query:
        return

    try:
        df = pd.read_sql(query, conn)
        mo.output.replace(mo.vstack([
            mo.md(f"**{len(df)}** rows"),
            mo.ui.table(df, page_size=50, label="Results"),
        ]))
    except Exception as e:
        mo.output.replace(mo.callout(mo.md(f"**Error:** `{e}`"), kind="danger"))
    return


if __name__ == "__main__":
    app.run()
