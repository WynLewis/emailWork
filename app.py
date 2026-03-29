import marimo

__generated_with = "0.21.1"
app = marimo.App(width="full", app_title="CLO Deal Extraction & Review")


@app.cell
def setup():
    import marimo as mo
    import sys, os, json, glob, re
    import pandas as pd
    from datetime import datetime

    ROOT = os.path.dirname(os.path.abspath("__file__"))
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)

    os.environ.setdefault("CLO_EXTRACTOR", "rules")
    os.environ.setdefault("CLO_RULES_MODULE", "my_rules.py")

    from backend.store_db import (
        get_db, init_db, upsert_deal, upsert_manager,
        insert_transaction, update_transaction_to_priced,
        save_training_pair, log_extraction, accept_extraction,
        skip_extraction, get_pending_extractions, get_all_deals,
        get_all_transactions, get_all_managers, get_training_count,
        export_deals_csv, export_transactions_csv, export_managers_csv,
        query_df, DB_PATH,
    )
    from backend.watcher import EmailQueue
    from backend.extractor import get_extractor, format_tranche_pricing
    from backend.field_mapping import map_extraction_to_gui, load_mapping
    from backend.bbg_pricing import parse_pricing_csv, is_already_priced
    from backend import config
    from my_rules import extract_from_email

    conn = get_db()
    init_db(conn)

    return (
        mo, pd, os, json, glob, re, datetime, ROOT, conn,
        get_db, init_db, upsert_deal, upsert_manager,
        insert_transaction, update_transaction_to_priced,
        save_training_pair, log_extraction, accept_extraction,
        skip_extraction, get_pending_extractions, get_all_deals,
        get_all_transactions, get_all_managers, get_training_count,
        export_deals_csv, export_transactions_csv, export_managers_csv,
        query_df, DB_PATH,
        EmailQueue, get_extractor, format_tranche_pricing,
        map_extraction_to_gui, load_mapping,
        parse_pricing_csv, is_already_priced, config,
        extract_from_email,
    )


# ============================================================================
# HEADER — Stats + Tab Navigation
# ============================================================================

@app.cell
def header(mo, conn):
    deal_count = conn.execute("SELECT COUNT(*) FROM deals").fetchone()[0]
    txn_count = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    mgr_count = conn.execute("SELECT COUNT(*) FROM managers").fetchone()[0]
    training_count = conn.execute("SELECT COUNT(*) FROM training_data").fetchone()[0]
    pending_count = conn.execute("SELECT COUNT(*) FROM extraction_log WHERE status='pending'").fetchone()[0]

    stats = mo.hstack([
        mo.stat(value=deal_count, label="Deals", bordered=True),
        mo.stat(value=txn_count, label="Transactions", bordered=True),
        mo.stat(value=mgr_count, label="Managers", bordered=True),
        mo.stat(value=training_count, label="Training Pairs", bordered=True),
        mo.stat(value=pending_count, label="Pending Review", bordered=True),
    ], justify="center", gap=1)

    tabs = mo.ui.tabs({
        "📧 Email Review": "email_review",
        "📊 Bloomberg": "bbg_review",
        "🗄️ Database": "database",
        "🔄 SharePoint Sync": "sp_sync",
        "📈 Rules Audit": "rules_audit",
    })

    mo.output.replace(mo.vstack([
        mo.md("# CLO Deal Extraction & Review"),
        stats,
        tabs,
    ]))
    return tabs,


# ============================================================================
# EMAIL REVIEW TAB — Split pane with confidence indicators
# ============================================================================

@app.cell
def email_controls(mo, tabs):
    mo.stop(tabs.value != "email_review")
    run_extract = mo.ui.run_button(label="Extract All Emails", kind="success")
    return run_extract,


@app.cell
def email_extraction(
    mo, run_extract, conn, EmailQueue, get_extractor, format_tranche_pricing,
    map_extraction_to_gui, os, json, log_extraction
):
    mo.stop(not run_extract.value)

    queue = EmailQueue()
    extractor = get_extractor()
    count = 0

    while queue.size() > 0:
        fname = queue.peek()
        if not fname:
            break
        html = queue.read_file(fname)
        os.environ["CLO_EMAIL_FILENAME"] = fname

        try:
            result = extractor.extract(html)
            extraction = result.model_dump()
        except Exception:
            queue.pop()
            continue

        pricing = format_tranche_pricing(html)
        for key in ("ipt", "updated_guidance", "final_pricing"):
            if pricing.get(key):
                extraction[key] = pricing[key]

        extraction = map_extraction_to_gui(extraction)
        deal_name = extraction.get("deal_name") or extraction.get("title") or ""

        log_extraction(conn, "email", fname, deal_name, extraction)
        conn.commit()
        count += 1
        queue.pop()

    return count,


@app.cell
def email_selector(mo, tabs, conn, json, count):
    mo.stop(tabs.value != "email_review")
    pending = conn.execute(
        "SELECT id, filename, deal_name, extraction_json, status "
        "FROM extraction_log WHERE status='pending' ORDER BY id"
    ).fetchall()
    pending = [dict(r) for r in pending]

    if not pending:
        mo.output.replace(mo.callout(
            mo.md("No pending extractions. Click **Extract All Emails** to process inbox."),
            kind="info"
        ))
        return

    selector = mo.ui.dropdown(
        options={f"#{r['id']}  {r['deal_name'] or r['filename'][:40]}": i for i, r in enumerate(pending)},
        label="Select extraction",
    )
    return selector, pending


@app.cell
def _conf_badge(mo):
    """Helper to render confidence badges."""
    def conf_badge(level: str) -> str:
        colors = {
            "high": "🟢",
            "medium": "🟡",
            "low": "🔴",
            "missing": "⚫",
        }
        return colors.get(level, "⚪")
    return conf_badge,


@app.cell
def email_review_pane(
    mo, tabs, conn, json, selector, pending, conf_badge,
    accept_extraction, skip_extraction,
    upsert_deal, upsert_manager, insert_transaction,
    update_transaction_to_priced, save_training_pair, os, config, query_df
):
    mo.stop(tabs.value != "email_review")
    mo.stop(selector.value is None)

    idx = selector.value
    record = pending[idx]
    extraction = json.loads(record["extraction_json"])
    log_id = record["id"]
    confidence = extraction.get("_confidence", {})

    # Load email HTML
    email_html = ""
    try:
        path = os.path.join(config.INBOX_DIR, record["filename"])
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                email_html = f.read()
    except Exception:
        pass

    def _val(*keys):
        for k in keys:
            v = extraction.get(k)
            if v and str(v).strip():
                return str(v).strip()
        return ""

    def _conf(field):
        return conf_badge(confidence.get(field, "missing"))

    # --- Check if deal already exists in DB (for diff view) ---
    deal_title = _val("deal_name", "title", "clo-deals__Title")
    existing_deal = None
    existing_txn = None
    if deal_title:
        rows = query_df(conn, "SELECT * FROM deals WHERE title = ?", (deal_title.strip(),))
        if rows:
            existing_deal = rows[0]
        rows = query_df(conn, "SELECT * FROM transactions WHERE deal = ? ORDER BY id DESC LIMIT 1", (deal_title.strip(),))
        if rows:
            existing_txn = rows[0]

    # --- Build the editable form with confidence badges ---
    # Each row shows: confidence emoji | label | input field
    # TABLE 1: deals columns
    deal_fields = mo.md(f"""### Writes to: `deals` table
| | Field | Value | Confidence |
|---|---|---|---|
| {_conf('deal_name')} | **Title** (PK, links to transactions.deal) | `{_val('deal_name', 'title')}` | {confidence.get('deal_name', 'missing')} |
| {_conf('collateral_type')} | **Collateral Type** | `{_val('deal_type', 'collateral_type')}` | {confidence.get('collateral_type', 'missing')} |
| {_conf('collateral_manager_legal_entity')} | **Collateral Manager** (links to managers.name) | `{_val('collateral_manager_legal_entity')}` | {confidence.get('collateral_manager_legal_entity', 'missing')} |
""")

    # TABLE 2: transactions columns
    txn_fields = mo.md(f"""### Writes to: `transactions` table
| | Field | Value | Confidence |
|---|---|---|---|
| {_conf('deal_name')} | **Deal** (FK → deals.title) | `{_val('deal_name', 'title')}` | {confidence.get('deal_name', 'missing')} |
| {_conf('transaction_type')} | **Transaction Type** | `{_val('transaction_type')}` | {confidence.get('transaction_type', 'missing')} |
| {_conf('status')} | **Status** | `{_val('clo-transactions__Status', 'email_type')}` | {confidence.get('status', 'missing')} |
| {_conf('arranger')} | **Placement Agent** | `{_val('arranger')}` | {confidence.get('arranger', 'missing')} |
| {_conf('term')} | **Term** | `{_val('term')}` | {confidence.get('term', 'missing')} |
| | **IPT** | `{_val('ipt')[:60]}{'...' if len(_val('ipt')) > 60 else ''}` | — |
| | **Final Pricing** | `{_val('final_pricing')[:60]}{'...' if len(_val('final_pricing')) > 60 else ''}` | — |
""")

    # TABLE 3: managers columns
    mgr_fields = mo.md(f"""### Writes to: `managers` table
| | Field | Value | Confidence |
|---|---|---|---|
| {_conf('collateral_manager_legal_entity')} | **Name** (PK) | `{_val('collateral_manager_legal_entity')}` | {confidence.get('collateral_manager_legal_entity', 'missing')} |
| {_conf('collateral_manager_short')} | **Short Name** | `{_val('collateral_manager_short')}` | {confidence.get('collateral_manager_short', 'missing')} |
""")

    # --- Diff view (if record exists) ---
    diff_section = mo.md("")
    if existing_deal or existing_txn:
        diff_rows = []
        if existing_deal:
            for col, ext_key in [("title", "deal_name"), ("collateral_type", "deal_type"), ("collateral_manager", "collateral_manager_legal_entity")]:
                old = existing_deal.get(col, "")
                new = _val(ext_key)
                if new and old != new:
                    diff_rows.append(f"| deals.{col} | `{old[:40]}` | `{new[:40]}` |")
        if existing_txn:
            for col, ext_key in [("placement_agent", "arranger"), ("term", "term"), ("status", "email_type")]:
                old = existing_txn.get(col, "")
                new = _val(ext_key)
                if new and old.lower() != new.lower():
                    diff_rows.append(f"| transactions.{col} | `{old[:40]}` | `{new[:40]}` |")

        if diff_rows:
            diff_section = mo.callout(mo.md(
                "### Diff: Existing vs New\n"
                "| Field | Current (DB) | New (Extraction) |\n"
                "|---|---|---|\n" + "\n".join(diff_rows)
            ), kind="warn")
        else:
            diff_section = mo.callout(mo.md("Deal exists in DB — no field changes detected."), kind="info")

    # --- Editable form ---
    form_fields = {
        "Deal Name": mo.ui.text(value=_val("deal_name", "title", "clo-deals__Title"), full_width=True),
        "Collateral Manager": mo.ui.text(value=_val("collateral_manager_legal_entity", "clo-deals__Collateral Manager"), full_width=True),
        "Manager Short": mo.ui.text(value=_val("collateral_manager_short", "clo-managers__Short Name"), full_width=True),
        "Placement Agent": mo.ui.text(value=_val("arranger", "clo-transactions__Placement Agent"), full_width=True),
        "Transaction Type": mo.ui.dropdown(options=["New Issue", "Reset", "Refinancing", "Re-Issue"],
            value=_val("transaction_type", "clo-transactions__Transaction Type") or "New Issue"),
        "Status": mo.ui.dropdown(options=["Announced", "Priced", "Upcoming", "Cancelled"],
            value=(_val("clo-transactions__Status") or _val("email_type").capitalize()) or "Announced"),
        "Term": mo.ui.text(value=_val("term", "clo-transactions__Term"), full_width=True),
        "Collateral Type": mo.ui.dropdown(options=["BSL", "MM", "PC", "Infra", "EM", "Other"],
            value=_val("deal_type", "collateral_type", "clo-deals__Collateral Type") or "BSL"),
        "IPT": mo.ui.text_area(value=_val("ipt", "clo-transactions__IPT"), full_width=True),
        "Final Pricing": mo.ui.text_area(value=_val("final_pricing", "clo-transactions__Final Pricing Details"), full_width=True),
    }
    form = mo.ui.dictionary(form_fields)

    accept_btn = mo.ui.run_button(label="Accept & Save", kind="success")
    skip_btn = mo.ui.run_button(label="Skip", kind="warn")

    # --- Layout ---
    left = mo.vstack([
        mo.md(f"**{record['filename'][:60]}**"),
        mo.Html(f'<iframe srcdoc="{email_html.replace(chr(34), "&quot;")}" '
                f'style="width:100%; height:700px; border:1px solid #444;"></iframe>'),
    ])

    right = mo.vstack([
        mo.md("## Edit Extraction"),
        diff_section,
        deal_fields,
        txn_fields,
        mgr_fields,
        mo.md("---"),
        mo.md("### Edit Fields"),
        form,
        mo.hstack([accept_btn, skip_btn], gap=1),
    ])

    layout = mo.hstack([left, right], widths=[1, 1], gap=2)
    mo.output.replace(mo.vstack([selector, layout]))
    return form, accept_btn, skip_btn, log_id, email_html, record, extraction


@app.cell
def handle_accept(mo, accept_btn, form, log_id, email_html, record, extraction, conn,
                  accept_extraction, upsert_deal, upsert_manager,
                  insert_transaction, update_transaction_to_priced,
                  save_training_pair, json):
    mo.stop(not accept_btn.value)

    v = form.value
    deal_name = v["Deal Name"].strip()
    status = v["Status"]

    upsert_deal(conn, {"Title": deal_name, "Collateral Type": v["Collateral Type"],
                       "Collateral Manager": v["Collateral Manager"]})

    if v["Collateral Manager"] and v["Manager Short"]:
        upsert_manager(conn, {"Name": v["Collateral Manager"], "Short Name": v["Manager Short"]})

    txn = {"Title": deal_name, "Deal": deal_name, "Transaction Type": v["Transaction Type"],
           "Status": status, "Placement Agent": v["Placement Agent"], "Term": v["Term"],
           "Collateral Type": v["Collateral Type"], "Collateral Manager": v["Collateral Manager"],
           "IPT": v["IPT"], "Final Pricing Details": v["Final Pricing"]}

    if status == "Priced":
        if not update_transaction_to_priced(conn, deal_name, txn):
            insert_transaction(conn, txn)
    else:
        insert_transaction(conn, txn)

    save_training_pair(conn, email_html, {k: v for k, v in form.value.items()},
                       "email", record["filename"])
    accept_extraction(conn, log_id)
    conn.commit()

    mo.output.replace(mo.callout(mo.md(f"**Saved** {deal_name}"), kind="success"))
    return


@app.cell
def handle_skip(mo, skip_btn, log_id, conn, skip_extraction):
    mo.stop(not skip_btn.value)
    skip_extraction(conn, log_id)
    conn.commit()
    mo.output.replace(mo.callout(mo.md("**Skipped**"), kind="warn"))
    return


# ============================================================================
# BLOOMBERG TAB
# ============================================================================

@app.cell
def bbg_tab(mo, tabs, conn, glob, ROOT, os, pd, parse_pricing_csv, is_already_priced,
            upsert_deal, insert_transaction, update_transaction_to_priced):
    mo.stop(tabs.value != "bbg_review")

    csvs = sorted(glob.glob(os.path.join(ROOT, "pricings_*.csv")))
    if not csvs:
        mo.output.replace(mo.callout(mo.md("No `pricings_*.csv` found."), kind="warn"))
        return

    csv_path = csvs[-1]
    priced = parse_pricing_csv(csv_path)
    new_deals = [d for d in priced if not is_already_priced(d["deal_name"], d["pricing_date"])]

    if not new_deals:
        mo.output.replace(mo.callout(mo.md("All deals already priced."), kind="info"))
        return

    df = pd.DataFrame(new_deals)
    cols = ["deal_name", "pricing_date", "settle_date", "orig_mm", "deal_type", "lead_mgr_full", "transaction_type"]
    table = mo.ui.table(df[cols], selection="multi", label="Select deals to import")
    import_btn = mo.ui.run_button(label="Import Selected", kind="success")

    mo.output.replace(mo.vstack([
        mo.md(f"### Bloomberg: {os.path.basename(csv_path)}"),
        mo.md(f"**{len(new_deals)}** new priced deals"),
        table, import_btn,
    ]))
    return table, import_btn, new_deals


@app.cell
def bbg_import(mo, import_btn, table, new_deals, conn, upsert_deal,
               insert_transaction, update_transaction_to_priced):
    mo.stop(not import_btn.value)
    indices = table.value.index.tolist() if hasattr(table.value, 'index') else []
    if not indices:
        mo.output.replace(mo.callout(mo.md("No deals selected."), kind="warn"))
        return

    for idx in indices:
        d = new_deals[idx]
        upsert_deal(conn, {"Title": d["deal_name"], "Collateral Type": d["deal_type"],
                           "Bloomberg Deal Name": d["deal_name"]})
        txn = {"Title": d["deal_name"], "Deal": d["deal_name"], "Transaction Type": d["transaction_type"],
               "Status": "Priced", "Placement Agent": d.get("lead_mgr_full", ""),
               "Priced Date": d.get("pricing_date", ""), "Collateral Type": d["deal_type"]}
        if not update_transaction_to_priced(conn, d["deal_name"], txn):
            insert_transaction(conn, txn)

    conn.commit()
    mo.output.replace(mo.callout(mo.md(f"**Imported {len(indices)} deals.**"), kind="success"))
    return


# ============================================================================
# DATABASE TAB — Table-by-table with schema + relationships
# ============================================================================

@app.cell
def db_tab(mo, tabs, conn, query_df, pd):
    mo.stop(tabs.value != "database")

    db_tabs = mo.ui.tabs({
        "deals": "deals",
        "transactions": "transactions",
        "managers": "managers",
        "Schema & Relationships": "schema",
    })
    return db_tabs,


@app.cell
def db_content(mo, tabs, db_tabs, conn, query_df, pd):
    mo.stop(tabs.value != "database")

    if db_tabs.value == "schema":
        schema = mo.md("""### Table Relationships

```
┌──────────────────────┐     ┌──────────────────────────────────┐
│       managers       │     │             deals                │
├──────────────────────┤     ├──────────────────────────────────┤
│ name (PK, UNIQUE)    │◄────│ collateral_manager → managers.name│
│ short_name           │     │ title (PK, UNIQUE)               │
│ ultimate_parent      │     │ collateral_type                  │
│ crd_number           │     │ bloomberg_deal_name              │
│ sec_number           │     │ intex_deal                       │
│ website              │     │ intex_preprice                   │
└──────────────────────┘     │ deal_documents                   │
                             └───────────────┬──────────────────┘
                                             │
                                             │ deals.title
                                             ▼
                             ┌──────────────────────────────────┐
                             │         transactions             │
                             ├──────────────────────────────────┤
                             │ deal (FK → deals.title)          │
                             │ title                            │
                             │ collateral_manager → managers.name│
                             │ transaction_type                 │
                             │ status                           │
                             │ placement_agent                  │
                             │ term                             │
                             │ ipt                              │
                             │ final_pricing_details            │
                             │ announcement_date                │
                             │ priced_date                      │
                             │ engaged / executed               │
                             └──────────────────────────────────┘
```

**Key relationships:**
- `transactions.deal` → `deals.title` (every transaction belongs to a deal)
- `deals.collateral_manager` → `managers.name` (every deal has a manager)
- `transactions.collateral_manager` → `managers.name` (denormalized for convenience)

**Placement Agent** is a constrained text field (24 allowed values from SharePoint).
""")
        mo.output.replace(mo.vstack([db_tabs, schema]))
        return

    if db_tabs.value == "deals":
        df = pd.DataFrame(query_df(conn,
            "SELECT id, title, collateral_type, collateral_manager, bloomberg_deal_name "
            "FROM deals ORDER BY title LIMIT 500"))
        label = f"deals ({conn.execute('SELECT COUNT(*) FROM deals').fetchone()[0]} total)"
    elif db_tabs.value == "transactions":
        df = pd.DataFrame(query_df(conn, """
            SELECT id, deal, status, transaction_type, placement_agent, term,
                   collateral_type, announcement_date, priced_date,
                   substr(ipt, 1, 60) as ipt_preview,
                   substr(final_pricing_details, 1, 60) as pricing_preview
            FROM transactions ORDER BY id DESC LIMIT 500
        """))
        label = f"transactions ({conn.execute('SELECT COUNT(*) FROM transactions').fetchone()[0]} total)"
    elif db_tabs.value == "managers":
        df = pd.DataFrame(query_df(conn,
            "SELECT id, name, short_name, ultimate_parent FROM managers ORDER BY short_name LIMIT 500"))
        label = f"managers ({conn.execute('SELECT COUNT(*) FROM managers').fetchone()[0]} total)"
    else:
        return

    if df.empty:
        mo.output.replace(mo.vstack([db_tabs, mo.md("No data.")]))
        return

    table = mo.ui.table(df, label=label, page_size=25)
    mo.output.replace(mo.vstack([db_tabs, table]))
    return


# ============================================================================
# SHAREPOINT SYNC TAB
# ============================================================================

@app.cell
def sp_tab(mo, tabs, conn, query_df, os, config, pd,
           export_deals_csv, export_transactions_csv, export_managers_csv):
    mo.stop(tabs.value != "sp_sync")

    csv_dir = os.path.join(config.DATA_DIR, "csv")

    sp_deals = pd.read_csv(os.path.join(csv_dir, "clo-deals.csv"), encoding="utf-8-sig") if os.path.isfile(os.path.join(csv_dir, "clo-deals.csv")) else pd.DataFrame()
    sp_txns = pd.read_csv(os.path.join(csv_dir, "clo-transactions.csv"), encoding="utf-8-sig") if os.path.isfile(os.path.join(csv_dir, "clo-transactions.csv")) else pd.DataFrame()

    db_deals = pd.DataFrame(query_df(conn,
        "SELECT title as Title, collateral_type as 'Collateral Type', "
        "collateral_manager as 'Collateral Manager', "
        "bloomberg_deal_name as 'Bloomberg Deal Name' FROM deals"))
    db_txns = pd.DataFrame(query_df(conn, """
        SELECT deal as Deal, title as Title, status as Status,
               transaction_type as 'Transaction Type',
               placement_agent as 'Placement Agent', term as Term,
               priced_date as 'Priced Date',
               substr(final_pricing_details, 1, 100) as 'Final Pricing Details'
        FROM transactions WHERE status = 'Priced'
    """))

    # New deals
    new_deals = pd.DataFrame()
    if not sp_deals.empty and not db_deals.empty:
        sp_titles = set(sp_deals["Title"].str.strip().str.lower())
        new_deals = db_deals[~db_deals["Title"].str.strip().str.lower().isin(sp_titles)]
    elif not db_deals.empty:
        new_deals = db_deals

    # New priced transactions
    new_txns = pd.DataFrame()
    if not sp_txns.empty and not db_txns.empty:
        sp_priced = set()
        if "Status" in sp_txns.columns and "Deal" in sp_txns.columns:
            sp_priced = set(sp_txns[sp_txns["Status"] == "Priced"]["Deal"].str.strip().str.lower())
        new_txns = db_txns[~db_txns["Deal"].str.strip().str.lower().isin(sp_priced)]
    elif not db_txns.empty:
        new_txns = db_txns

    export_btn = mo.ui.run_button(label="Export New Records to CSV", kind="success")

    mo.output.replace(mo.vstack([
        mo.md("### SharePoint Sync"),
        mo.hstack([
            mo.stat(value=len(sp_deals), label="SP Deals", bordered=True),
            mo.stat(value=len(db_deals), label="DB Deals", bordered=True),
            mo.stat(value=len(new_deals), label="New Deals", bordered=True),
        ], justify="center", gap=1),
        mo.hstack([
            mo.stat(value=len(sp_txns), label="SP Transactions", bordered=True),
            mo.stat(value=len(db_txns), label="DB Priced", bordered=True),
            mo.stat(value=len(new_txns), label="New Priced", bordered=True),
        ], justify="center", gap=1),
        mo.md("#### New Deals") if not new_deals.empty else mo.md(""),
        mo.ui.table(new_deals, page_size=20) if not new_deals.empty else mo.callout(mo.md("All synced."), kind="success"),
        mo.md("#### New Priced Transactions") if not new_txns.empty else mo.md(""),
        mo.ui.table(new_txns, page_size=20) if not new_txns.empty else mo.callout(mo.md("All synced."), kind="success"),
        export_btn,
    ]))
    return export_btn, new_deals, new_txns


@app.cell
def sp_export(mo, export_btn, new_deals, new_txns, os, config):
    mo.stop(not export_btn.value)
    export_dir = os.path.join(config.DATA_DIR, "export")
    os.makedirs(export_dir, exist_ok=True)
    files = []
    if not new_deals.empty:
        p = os.path.join(export_dir, "new_deals.csv")
        new_deals.to_csv(p, index=False, encoding="utf-8-sig")
        files.append(f"new_deals.csv ({len(new_deals)} rows)")
    if not new_txns.empty:
        p = os.path.join(export_dir, "new_priced_transactions.csv")
        new_txns.to_csv(p, index=False, encoding="utf-8-sig")
        files.append(f"new_priced_transactions.csv ({len(new_txns)} rows)")
    msg = "**Exported:**\n" + "\n".join(f"- {f}" for f in files) if files else "Nothing to export."
    mo.output.replace(mo.callout(mo.md(msg), kind="success" if files else "info"))
    return


# ============================================================================
# RULES AUDIT TAB
# ============================================================================

@app.cell
def rules_tab(mo, tabs, conn, query_df, pd, json):
    mo.stop(tabs.value != "rules_audit")

    training_count = conn.execute("SELECT COUNT(*) FROM training_data").fetchone()[0]

    if training_count == 0:
        mo.output.replace(mo.vstack([
            mo.md("### Rules Audit"),
            mo.callout(mo.md(
                "No training data yet. Accept extractions to start building data.\n\n"
                "**Rules improvement loop:**\n"
                "1. Accept/edit extractions → saves training pairs\n"
                "2. This tab shows which fields you correct most\n"
                "3. Fix those patterns in `my_rules.py`\n"
                "4. Training pairs become LLM fine-tuning data later"
            ), kind="info"),
        ]))
        return

    rows = query_df(conn, "SELECT extraction_json FROM training_data")
    field_counts = {}
    for row in rows:
        try:
            ext = json.loads(row["extraction_json"])
        except Exception:
            continue
        for field, val in ext.items():
            if val and str(val).strip():
                field_counts[field] = field_counts.get(field, 0) + 1

    df = pd.DataFrame([{"Field": k, "Times Set": v, "Coverage": f"{v/training_count*100:.0f}%"}
                       for k, v in sorted(field_counts.items(), key=lambda x: -x[1])])

    mo.output.replace(mo.vstack([
        mo.md("### Rules Audit"),
        mo.stat(value=training_count, label="Training Pairs", bordered=True),
        mo.md("#### Field Coverage"),
        mo.ui.table(df, page_size=30),
    ]))
    return


if __name__ == "__main__":
    app.run()
