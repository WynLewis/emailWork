import marimo

__generated_with = "0.21.1"
app = marimo.App(width="full", app_title="CLO Deal Extraction & Review")


@app.cell
def setup():
    import marimo as mo
    import sys, os, json, glob
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
        query_df, migrate_from_json, DB_PATH,
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
        mo, pd, os, json, glob, datetime, ROOT, conn,
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


@app.cell
def tabs(mo):
    tabs_ui = mo.ui.tabs({
        "📧 Email Review": "email_review",
        "📊 Bloomberg": "bbg_review",
        "🗄️ Database": "database",
        "🔄 SharePoint Sync": "sp_sync",
        "📈 Rules Audit": "rules_audit",
    })
    return tabs_ui,


@app.cell
def header(mo, conn, tabs_ui):
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

    mo.output.replace(mo.vstack([
        mo.md("# CLO Deal Extraction & Review"),
        stats,
        tabs_ui,
    ]))
    return


# ============================================================================
# EMAIL REVIEW TAB
# ============================================================================

@app.cell
def email_extract_button(mo, tabs_ui):
    mo.stop(tabs_ui.value != "email_review")
    run_extract = mo.ui.run_button(label="🔍 Extract All Emails", kind="success")
    return run_extract,


@app.cell
def email_extraction(
    mo, run_extract, conn, EmailQueue, get_extractor, format_tranche_pricing,
    map_extraction_to_gui, extract_from_email, log_extraction, os, json
):
    mo.stop(not run_extract.value)

    queue = EmailQueue()
    extractor = get_extractor()
    results = []

    while queue.size() > 0:
        fname = queue.peek()
        if not fname:
            break
        html = queue.read_file(fname)
        os.environ["CLO_EMAIL_FILENAME"] = fname

        try:
            result = extractor.extract(html)
            extraction = result.model_dump()
        except Exception as e:
            queue.pop()
            continue

        pricing = format_tranche_pricing(html)
        for key in ("ipt", "updated_guidance", "final_pricing"):
            if pricing.get(key):
                extraction[key] = pricing[key]

        extraction = map_extraction_to_gui(extraction)
        deal_name = extraction.get("deal_name") or extraction.get("title") or extraction.get("clo-deals__Title") or ""

        log_id = log_extraction(conn, "email", fname, deal_name, extraction)
        conn.commit()

        results.append({
            "log_id": log_id,
            "filename": fname,
            "deal_name": deal_name,
            "extraction": extraction,
            "html": html,
        })
        queue.pop()

    mo.output.replace(mo.md(f"**Extracted {len(results)} emails.** Select one below to review."))
    return results,


@app.cell
def email_selector(mo, tabs_ui, conn, json):
    mo.stop(tabs_ui.value != "email_review")
    pending = conn.execute(
        "SELECT id, source, filename, deal_name, extraction_json, status FROM extraction_log WHERE status='pending' ORDER BY id"
    ).fetchall()
    pending = [dict(r) for r in pending]

    if not pending:
        mo.output.replace(mo.callout(
            mo.md("No pending extractions. Click **Extract All Emails** to process inbox."),
            kind="info"
        ))
        return

    options = {
        f"#{r['id']} — {r['deal_name'] or r['filename'][:40]}": i
        for i, r in enumerate(pending)
    }
    selector = mo.ui.dropdown(options=options, label="Select extraction to review")
    return selector, pending


@app.cell
def email_review_pane(mo, tabs_ui, conn, json, selector, pending, accept_extraction, skip_extraction,
                       upsert_deal, upsert_manager, insert_transaction,
                       update_transaction_to_priced, save_training_pair, os, config):
    mo.stop(tabs_ui.value != "email_review")
    mo.stop(selector.value is None)

    idx = selector.value
    record = pending[idx]
    extraction = json.loads(record["extraction_json"])
    log_id = record["id"]

    # Load email HTML for preview
    email_html = ""
    try:
        email_path = os.path.join(config.INBOX_DIR, record["filename"])
        if os.path.isfile(email_path):
            with open(email_path, "r", encoding="utf-8", errors="replace") as f:
                email_html = f.read()
    except Exception:
        pass

    # Build editable form
    def _val(*keys):
        for k in keys:
            v = extraction.get(k)
            if v and str(v).strip():
                return str(v).strip()
        return ""

    form_fields = {
        "Deal Name": mo.ui.text(value=_val("deal_name", "title", "clo-deals__Title"), full_width=True),
        "Collateral Manager": mo.ui.text(value=_val("collateral_manager_legal_entity", "clo-deals__Collateral Manager"), full_width=True),
        "Manager Short": mo.ui.text(value=_val("collateral_manager_short", "clo-managers__Short Name"), full_width=True),
        "Placement Agent": mo.ui.text(value=_val("arranger", "clo-transactions__Placement Agent"), full_width=True),
        "Transaction Type": mo.ui.dropdown(
            options=["New Issue", "Reset", "Refinancing", "Re-Issue"],
            value=_val("transaction_type", "clo-transactions__Transaction Type") or "New Issue",
        ),
        "Status": mo.ui.dropdown(
            options=["Announced", "Priced", "Upcoming", "Cancelled"],
            value=_val("clo-transactions__Status", "email_type").capitalize() or "Announced",
        ),
        "Term": mo.ui.text(value=_val("term", "clo-transactions__Term"), full_width=True),
        "Collateral Type": mo.ui.dropdown(
            options=["BSL", "MM", "PC", "Infra", "EM", "Other"],
            value=_val("deal_type", "collateral_type", "clo-deals__Collateral Type") or "BSL",
        ),
        "IPT": mo.ui.text_area(value=_val("ipt", "clo-transactions__IPT"), full_width=True),
        "Final Pricing": mo.ui.text_area(value=_val("final_pricing", "clo-transactions__Final Pricing Details"), full_width=True),
    }
    form = mo.ui.dictionary(form_fields)

    accept_btn = mo.ui.run_button(label="✅ Accept & Save", kind="success")
    skip_btn = mo.ui.run_button(label="⏭️ Skip", kind="warn")

    # Layout: email left, form right
    left_panel = mo.vstack([
        mo.md(f"**📧 {record['filename'][:60]}**"),
        mo.Html(f'<div style="max-height:700px; overflow-y:auto; border:1px solid #444; padding:8px; font-size:12px;">{email_html}</div>'),
    ])

    right_panel = mo.vstack([
        mo.md("**Extraction (edit before accepting)**"),
        form,
        mo.hstack([accept_btn, skip_btn], gap=1),
    ])

    layout = mo.hstack([left_panel, right_panel], widths=[1, 1], gap=2)
    mo.output.replace(mo.vstack([selector, layout]))

    return form, accept_btn, skip_btn, log_id, email_html, record


@app.cell
def handle_accept(mo, accept_btn, form, log_id, email_html, record, conn,
                  accept_extraction, upsert_deal, upsert_manager,
                  insert_transaction, update_transaction_to_priced,
                  save_training_pair, json):
    mo.stop(not accept_btn.value)

    values = form.value
    deal_name = values["Deal Name"].strip()
    status = values["Status"]

    # Save to deals
    upsert_deal(conn, {
        "Title": deal_name,
        "Collateral Type": values["Collateral Type"],
        "Collateral Manager": values["Collateral Manager"],
    })

    # Save to managers
    if values["Collateral Manager"] and values["Manager Short"]:
        upsert_manager(conn, {
            "Name": values["Collateral Manager"],
            "Short Name": values["Manager Short"],
        })

    # Save to transactions
    txn_data = {
        "Title": deal_name,
        "Deal": deal_name,
        "Transaction Type": values["Transaction Type"],
        "Status": status,
        "Placement Agent": values["Placement Agent"],
        "Term": values["Term"],
        "Collateral Type": values["Collateral Type"],
        "Collateral Manager": values["Collateral Manager"],
        "IPT": values["IPT"],
        "Final Pricing Details": values["Final Pricing"],
    }
    if status == "Priced":
        if not update_transaction_to_priced(conn, deal_name, txn_data):
            insert_transaction(conn, txn_data)
    else:
        insert_transaction(conn, txn_data)

    # Save training pair
    extraction_dict = {k: v for k, v in values.items()}
    save_training_pair(conn, email_html, extraction_dict, "email", record["filename"])

    # Mark as accepted
    accept_extraction(conn, log_id)
    conn.commit()

    mo.output.replace(mo.callout(mo.md(f"**Saved** {deal_name}"), kind="success"))
    return


@app.cell
def handle_skip(mo, skip_btn, log_id, conn, skip_extraction):
    mo.stop(not skip_btn.value)
    skip_extraction(conn, log_id)
    conn.commit()
    mo.output.replace(mo.callout(mo.md("**Skipped.** Select next extraction."), kind="warn"))
    return


# ============================================================================
# BLOOMBERG TAB
# ============================================================================

@app.cell
def bbg_tab(mo, tabs_ui, conn, glob, ROOT, os, parse_pricing_csv, is_already_priced, json,
            log_extraction, upsert_deal, insert_transaction, map_extraction_to_gui):
    mo.stop(tabs_ui.value != "bbg_review")
    import pandas as pd

    csvs = sorted(glob.glob(os.path.join(ROOT, "pricings_*.csv")))
    if not csvs:
        mo.output.replace(mo.callout(mo.md("No `pricings_*.csv` found in project root."), kind="warn"))
        return

    csv_path = csvs[-1]
    priced = parse_pricing_csv(csv_path)
    new_deals = [d for d in priced if not is_already_priced(d["deal_name"], d["pricing_date"])]

    df = pd.DataFrame(new_deals)
    if df.empty:
        mo.output.replace(mo.callout(mo.md("All deals in the CSV are already priced."), kind="info"))
        return

    display_cols = ["deal_name", "pricing_date", "settle_date", "orig_mm", "deal_type", "lead_mgr_full", "transaction_type"]
    table = mo.ui.table(df[display_cols], selection="multi", label="Select deals to import")

    import_btn = mo.ui.run_button(label="📥 Import Selected", kind="success")

    mo.output.replace(mo.vstack([
        mo.md(f"### Bloomberg Pricing: {os.path.basename(csv_path)}"),
        mo.md(f"**{len(new_deals)}** new priced deals (of {len(priced)} total)"),
        table,
        import_btn,
    ]))
    return table, import_btn, new_deals


@app.cell
def bbg_import(mo, import_btn, table, new_deals, conn, upsert_deal, insert_transaction,
               update_transaction_to_priced, log_extraction, json):
    mo.stop(not import_btn.value)

    selected_indices = table.value.index.tolist() if hasattr(table.value, 'index') else []
    if not selected_indices:
        mo.output.replace(mo.callout(mo.md("No deals selected."), kind="warn"))
        return

    saved = 0
    for idx in selected_indices:
        deal = new_deals[idx]
        upsert_deal(conn, {
            "Title": deal["deal_name"],
            "Collateral Type": deal["deal_type"],
            "Bloomberg Deal Name": deal["deal_name"],
        })

        txn_data = {
            "Title": deal["deal_name"],
            "Deal": deal["deal_name"],
            "Transaction Type": deal["transaction_type"],
            "Status": "Priced",
            "Placement Agent": deal.get("lead_mgr_full", ""),
            "Priced Date": deal.get("pricing_date", ""),
            "Collateral Type": deal["deal_type"],
        }
        if not update_transaction_to_priced(conn, deal["deal_name"], txn_data):
            insert_transaction(conn, txn_data)
        saved += 1

    conn.commit()
    mo.output.replace(mo.callout(mo.md(f"**Imported {saved} deals** from Bloomberg."), kind="success"))
    return


# ============================================================================
# DATABASE TAB
# ============================================================================

@app.cell
def db_tab(mo, tabs_ui, conn, query_df):
    mo.stop(tabs_ui.value != "database")
    import pandas as pd

    db_subtabs = mo.ui.tabs({
        "Deals": "deals",
        "Transactions": "transactions",
        "Managers": "managers",
    })

    deals_df = pd.DataFrame(query_df(conn, "SELECT id, title, collateral_type, collateral_manager, bloomberg_deal_name FROM deals ORDER BY title LIMIT 500"))
    txns_df = pd.DataFrame(query_df(conn, """
        SELECT id, deal, status, transaction_type, placement_agent, term,
               collateral_type, announcement_date, priced_date,
               substr(ipt, 1, 80) as ipt_preview,
               substr(final_pricing_details, 1, 80) as pricing_preview
        FROM transactions ORDER BY id DESC LIMIT 500
    """))
    mgrs_df = pd.DataFrame(query_df(conn, "SELECT id, name, short_name, ultimate_parent FROM managers ORDER BY short_name LIMIT 500"))

    content = {
        "deals": mo.ui.table(deals_df, label="Deals (first 500)", page_size=20) if not deals_df.empty else mo.md("No deals"),
        "transactions": mo.ui.table(txns_df, label="Transactions (latest 500)", page_size=20) if not txns_df.empty else mo.md("No transactions"),
        "managers": mo.ui.table(mgrs_df, label="Managers (first 500)", page_size=20) if not mgrs_df.empty else mo.md("No managers"),
    }

    mo.output.replace(mo.vstack([
        mo.md("### Database Explorer"),
        db_subtabs,
        content.get(db_subtabs.value, mo.md("")),
    ]))
    return


# ============================================================================
# SHAREPOINT SYNC TAB
# ============================================================================

@app.cell
def sp_sync_tab(mo, tabs_ui, conn, query_df, os, config,
                export_deals_csv, export_transactions_csv, export_managers_csv):
    mo.stop(tabs_ui.value != "sp_sync")
    import pandas as pd

    # Load SharePoint data (the CSV files in data/csv/ are the SharePoint exports)
    csv_dir = os.path.join(config.DATA_DIR, "csv")

    sp_deals = pd.read_csv(os.path.join(csv_dir, "clo-deals.csv"), encoding="utf-8-sig") if os.path.isfile(os.path.join(csv_dir, "clo-deals.csv")) else pd.DataFrame()
    sp_txns = pd.read_csv(os.path.join(csv_dir, "clo-transactions.csv"), encoding="utf-8-sig") if os.path.isfile(os.path.join(csv_dir, "clo-transactions.csv")) else pd.DataFrame()

    # Load SQLite data
    db_deals = pd.DataFrame(query_df(conn, "SELECT title as Title, collateral_type as 'Collateral Type', collateral_manager as 'Collateral Manager', bloomberg_deal_name as 'Bloomberg Deal Name' FROM deals"))
    db_txns = pd.DataFrame(query_df(conn, """
        SELECT deal as Deal, title as Title, status as Status, transaction_type as 'Transaction Type',
               placement_agent as 'Placement Agent', term as Term, priced_date as 'Priced Date',
               final_pricing_details as 'Final Pricing Details'
        FROM transactions WHERE status = 'Priced'
    """))

    # Find new deals (in SQLite but not in SharePoint)
    new_deals = pd.DataFrame()
    new_txns = pd.DataFrame()
    if not sp_deals.empty and not db_deals.empty:
        sp_titles = set(sp_deals["Title"].str.strip().str.lower())
        new_mask = ~db_deals["Title"].str.strip().str.lower().isin(sp_titles)
        new_deals = db_deals[new_mask]
    elif not db_deals.empty:
        new_deals = db_deals

    if not sp_txns.empty and not db_txns.empty:
        # New priced transactions: in SQLite with Status=Priced but not in SP with that priced date
        sp_priced = set(
            sp_txns[sp_txns["Status"] == "Priced"]["Deal"].str.strip().str.lower()
        ) if "Status" in sp_txns.columns and "Deal" in sp_txns.columns else set()
        new_mask = ~db_txns["Deal"].str.strip().str.lower().isin(sp_priced)
        new_txns = db_txns[new_mask]
    elif not db_txns.empty:
        new_txns = db_txns

    # Export button
    export_btn = mo.ui.run_button(label="📁 Export New Records to CSV", kind="success")

    mo.output.replace(mo.vstack([
        mo.md("### SharePoint Sync"),
        mo.hstack([
            mo.stat(value=len(sp_deals), label="SP Deals", bordered=True),
            mo.stat(value=len(db_deals), label="SQLite Deals", bordered=True),
            mo.stat(value=len(new_deals), label="New Deals", bordered=True),
        ], justify="center", gap=1),
        mo.hstack([
            mo.stat(value=len(sp_txns), label="SP Transactions", bordered=True),
            mo.stat(value=len(db_txns), label="SQLite Priced Txns", bordered=True),
            mo.stat(value=len(new_txns), label="New Priced Txns", bordered=True),
        ], justify="center", gap=1),
        mo.md("#### New Deals (not in SharePoint)") if not new_deals.empty else mo.md(""),
        mo.ui.table(new_deals, label="New deals to add to SharePoint", page_size=20) if not new_deals.empty else mo.callout(mo.md("All deals are synced."), kind="success"),
        mo.md("#### New Priced Transactions") if not new_txns.empty else mo.md(""),
        mo.ui.table(new_txns, label="New priced transactions", page_size=20) if not new_txns.empty else mo.callout(mo.md("All priced transactions are synced."), kind="success"),
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
        path = os.path.join(export_dir, "new_deals.csv")
        new_deals.to_csv(path, index=False, encoding="utf-8-sig")
        files.append(f"new_deals.csv ({len(new_deals)} rows)")

    if not new_txns.empty:
        path = os.path.join(export_dir, "new_priced_transactions.csv")
        new_txns.to_csv(path, index=False, encoding="utf-8-sig")
        files.append(f"new_priced_transactions.csv ({len(new_txns)} rows)")

    if files:
        mo.output.replace(mo.callout(
            mo.md(f"**Exported to `data/export/`:**\n" + "\n".join(f"- {f}" for f in files)),
            kind="success"
        ))
    else:
        mo.output.replace(mo.callout(mo.md("Nothing to export."), kind="info"))
    return


# ============================================================================
# RULES AUDIT TAB
# ============================================================================

@app.cell
def rules_audit_tab(mo, tabs_ui, conn, query_df):
    mo.stop(tabs_ui.value != "rules_audit")
    import pandas as pd

    training_count = conn.execute("SELECT COUNT(*) FROM training_data").fetchone()[0]

    if training_count == 0:
        mo.output.replace(mo.vstack([
            mo.md("### Rules Audit"),
            mo.callout(
                mo.md("No training data yet. Accept some extractions to start building the audit trail.\n\n"
                      "**How rules improvement works:**\n"
                      "1. Every time you accept (or edit+accept) an extraction, the email + your corrections are saved\n"
                      "2. This tab analyzes which fields you correct most often\n"
                      "3. That tells you exactly which regex patterns in `my_rules.py` need work\n"
                      "4. When you're ready for LLM fine-tuning, these pairs become training data"),
                kind="info"
            ),
        ]))
        return

    # Analyze corrections
    rows = query_df(conn, "SELECT extraction_json FROM training_data")
    corrections = {}
    for row in rows:
        ext = pd.json_normalize([eval(row["extraction_json"])])
        for col in ext.columns:
            val = ext[col].iloc[0]
            if val and str(val).strip():
                corrections[col] = corrections.get(col, 0) + 1

    corrections_df = pd.DataFrame([
        {"Field": k, "Times Set": v}
        for k, v in sorted(corrections.items(), key=lambda x: -x[1])
    ])

    mo.output.replace(mo.vstack([
        mo.md("### Rules Audit"),
        mo.stat(value=training_count, label="Training Pairs Collected", bordered=True),
        mo.md("#### Field Coverage (how often each field is populated)"),
        mo.ui.table(corrections_df, page_size=30),
    ]))
    return


if __name__ == "__main__":
    app.run()
