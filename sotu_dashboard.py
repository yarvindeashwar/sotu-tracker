"""
SOTU Email Tracker
==================
- Who got SOTU this week? Who didn't?
- Look up any chain or user
- Week-by-week lookback
"""

import streamlit as st
import pandas as pd
from google.cloud import bigquery
from google.oauth2 import service_account
from datetime import date, timedelta

st.set_page_config(page_title="SOTU Tracker", page_icon="📧", layout="wide")

st.markdown("""
<style>
    .block-container { padding-top: 1.5rem; max-width: 1200px; }
</style>
""", unsafe_allow_html=True)


# ── Week helpers ──
today = date.today()
monday = today - timedelta(days=today.weekday())
sunday = monday + timedelta(days=6)


@st.cache_data(ttl=1800, show_spinner="Loading SOTU data...")
def load_data(start_date: str, end_date: str) -> pd.DataFrame:
    credentials = service_account.Credentials.from_service_account_info(
        st.secrets["gcp_service_account"]
    )
    client = bigquery.Client(credentials=credentials, project="arboreal-vision-339901")
    query = """
    WITH chain_map AS (
        SELECT DISTINCT LOWER(email) as email, chain, name, access_level
        FROM `elt_data.weekly_email_funnel_kpi`
        WHERE notification_type = 'OPERATIONS_INTELLIGENCE_REPORT' AND email IS NOT NULL
    ),
    raw AS (
        SELECT
            COALESCE(cm.chain, 'Unknown') as chain,
            COALESCE(cm.name, '') as user_name,
            LOWER(s.email) as email,
            DATE_TRUNC(DATE(COALESCE(s.delivered_at, s.first_bounced_at, s.first_event_at)), WEEK(MONDAY)) as week_start,
            FORMAT_DATE('%A', DATE(COALESCE(s.delivered_at, s.first_bounced_at, s.first_event_at))) as send_day,
            s.delivered_at,
            s.first_opened_at,
            s.opened_count,
            s.first_bounced_at,
            CASE
                WHEN s.opened_count > 0 THEN 'Opened'
                WHEN s.delivered_at IS NOT NULL THEN 'Delivered'
                WHEN s.first_bounced_at IS NOT NULL THEN 'Bounced'
                ELSE 'Not Delivered'
            END as status,
            -- Prefer: Opened > Delivered > Not Delivered > Bounced
            ROW_NUMBER() OVER (
                PARTITION BY LOWER(s.email), DATE_TRUNC(DATE(COALESCE(s.delivered_at, s.first_bounced_at, s.first_event_at)), WEEK(MONDAY))
                ORDER BY
                    CASE
                        WHEN s.opened_count > 0 THEN 1
                        WHEN s.delivered_at IS NOT NULL AND s.first_bounced_at IS NULL THEN 2
                        WHEN s.delivered_at IS NULL AND s.first_bounced_at IS NULL THEN 3
                        ELSE 4
                    END,
                    s.delivered_at DESC NULLS LAST
            ) as rn
        FROM `sendgrid_emails.sendgrid_emails_v2_filtered` s
        LEFT JOIN chain_map cm ON LOWER(s.email) = cm.email
        WHERE s.template_name = 'operations_intelligence_report'
            AND DATE(COALESCE(s.delivered_at, s.first_bounced_at, s.first_event_at))
                BETWEEN @start_date AND @end_date
    )
    SELECT * EXCEPT(rn) FROM raw WHERE rn = 1
    ORDER BY chain, email, week_start
    """
    job_config = bigquery.QueryJobConfig(query_parameters=[
        bigquery.ScalarQueryParameter("start_date", "DATE", start_date),
        bigquery.ScalarQueryParameter("end_date", "DATE", end_date),
    ])
    df = client.query(query, job_config=job_config).to_dataframe()
    for col in ["delivered_at", "first_opened_at", "first_bounced_at"]:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce", utc=True)
            df[col] = df[col].dt.tz_convert("US/Eastern").dt.tz_localize(None)
    if "week_start" in df.columns:
        df["week_start"] = pd.to_datetime(df["week_start"]).dt.date
    return df


# Load 12 weeks of data (current + 11 prior for lookback)
lookback_start = monday - timedelta(weeks=11)
df_all = load_data(str(lookback_start), str(sunday))

if df_all.empty:
    st.warning("No SOTU emails found.")
    st.stop()

# Derive typical send day per chain from historical data
chain_send_day = (
    df_all[df_all["send_day"].notna()]
    .groupby("chain")["send_day"]
    .agg(lambda x: x.value_counts().index[0] if len(x) > 0 else "Monday")
    .to_dict()
)

# Current week slice
df_week = df_all[df_all["week_start"] == monday].copy()

# ── Header ──
st.title("SOTU Email Tracker")
st.caption(f"Week of {monday.strftime('%B %d, %Y')}")

if df_week.empty:
    st.info("No SOTU emails sent yet this week.")
else:
    # ── Top-line numbers ──
    total = len(df_week)
    delivered = df_week["status"].isin(["Opened", "Delivered"]).sum()
    opened = (df_week["status"] == "Opened").sum()
    bounced = (df_week["status"] == "Bounced").sum()
    not_delivered = (df_week["status"] == "Not Delivered").sum()
    failed = bounced + not_delivered

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Sent", f"{total:,}")
    c2.metric("Delivered", f"{delivered:,}", delta=f"{round(delivered/total*100)}%" if total else None)
    c3.metric("Opened", f"{opened:,}", delta=f"{round(opened/delivered*100)}%" if delivered else None)
    c4.metric("Failed", f"{failed:,}", delta=f"{bounced} bounced" if bounced else "0 bounced", delta_color="inverse")

st.markdown("---")

# ══════════════════════════════════════════════════
# TABS
# ══════════════════════════════════════════════════
tab_week, tab_history = st.tabs(["This Week", "Week by Week"])


# ── TAB 1: This Week ──
with tab_week:

    # Search
    search = st.text_input(
        "Look up a chain or user",
        placeholder="Type a chain name, user name, or email...",
        key="search",
    )

    if search:
        mask = (
            df_week["chain"].str.contains(search, case=False, na=False)
            | df_week["email"].str.contains(search, case=False, na=False)
            | df_week["user_name"].str.contains(search, case=False, na=False)
        )
        results = df_week[mask].copy()

        if results.empty:
            st.warning(f"No SOTU records matching **{search}** this week.")
        else:
            st.caption(f"{len(results)} result{'s' if len(results) != 1 else ''}")
            display = results[["chain", "user_name", "email", "status",
                               "delivered_at", "first_opened_at", "first_bounced_at"]].copy()
            for col in ["delivered_at", "first_opened_at", "first_bounced_at"]:
                display[col] = display[col].dt.strftime("%b %d, %I:%M %p").fillna("—")

            def color_search(row):
                if row["Status"] in ("Bounced", "Not Delivered"):
                    return ["background-color: #f8d7da"] * len(row)
                if row["Status"] == "Opened":
                    return ["background-color: #d4edda"] * len(row)
                return [""] * len(row)

            st.dataframe(
                display.rename(columns={
                    "chain": "Chain", "user_name": "Name", "email": "Email",
                    "status": "Status", "delivered_at": "Delivered",
                    "first_opened_at": "Opened", "first_bounced_at": "Bounced",
                }).style.apply(color_search, axis=1),
                use_container_width=True, hide_index=True,
            )
        st.markdown("---")

    # ── Who GOT it ──
    st.subheader("Who got SOTU this week?")

    got_df = df_week[df_week["status"].isin(["Opened", "Delivered"])].copy()

    if got_df.empty:
        st.info("No successful deliveries yet this week.")
    else:
        g1, g2 = st.columns([1, 3])
        g1.metric("Delivered", f"{len(got_df):,}")
        g2.metric("Unique Users", f"{got_df['email'].nunique():,}")

        display_got = got_df[["chain", "user_name", "email", "status",
                              "delivered_at", "first_opened_at"]].copy()
        display_got = display_got.sort_values(["chain", "email"])
        for col in ["delivered_at", "first_opened_at"]:
            display_got[col] = display_got[col].dt.strftime("%b %d, %I:%M %p").fillna("—")

        st.dataframe(
            display_got.rename(columns={
                "chain": "Chain", "user_name": "Name", "email": "Email",
                "status": "Status", "delivered_at": "Delivered At",
                "first_opened_at": "Opened At",
            }),
            use_container_width=True, hide_index=True,
        )

    st.markdown("---")

    # ── Who DIDN'T get it ──
    st.subheader("Who didn't get SOTU this week?")

    failed_df = df_week[df_week["status"].isin(["Bounced", "Not Delivered"])].copy()

    if failed_df.empty:
        st.success("Everyone got their SOTU this week.")
    else:
        st.caption(f"{len(failed_df)} failed deliveries across {failed_df['chain'].nunique()} chains")

        # Add scheduled send day
        failed_df["scheduled_day"] = failed_df["chain"].map(chain_send_day).fillna("Monday")

        display_failed = (
            failed_df[["chain", "user_name", "email", "status", "scheduled_day", "first_bounced_at"]]
            .copy()
            .sort_values(["chain", "email"])
        )
        display_failed["first_bounced_at"] = (
            display_failed["first_bounced_at"].dt.strftime("%b %d, %I:%M %p").fillna("—")
        )

        st.dataframe(
            display_failed.rename(columns={
                "chain": "Chain", "user_name": "Name", "email": "Email",
                "status": "Status", "scheduled_day": "Scheduled Day",
                "first_bounced_at": "Bounced At",
            }),
            use_container_width=True, hide_index=True,
        )

        csv = display_failed.to_csv(index=False)
        st.download_button("Download CSV", data=csv,
                           file_name=f"sotu_failed_{monday}.csv", mime="text/csv")


# ── TAB 2: Week by Week lookback ──
with tab_history:
    st.subheader("Week by Week")
    st.caption("Select a week to see that week's delivery and open status")

    weeks_available = sorted(
        [w for w in df_all["week_start"].unique() if pd.notna(w)], reverse=True
    )
    week_labels = {w: pd.Timestamp(w).strftime("%b %d, %Y") for w in weeks_available}

    selected_week = st.selectbox(
        "Select week",
        options=weeks_available,
        format_func=lambda w: f"Week of {week_labels[w]}" + (" (current)" if w == monday else ""),
        index=0,
    )

    wk_data = df_all[df_all["week_start"] == selected_week].copy()

    if wk_data.empty:
        st.info("No data for this week.")
    else:
        # KPIs for selected week
        wk_total = len(wk_data)
        wk_delivered = wk_data["status"].isin(["Opened", "Delivered"]).sum()
        wk_opened = (wk_data["status"] == "Opened").sum()
        wk_failed = wk_data["status"].isin(["Bounced", "Not Delivered"]).sum()

        h1, h2, h3, h4 = st.columns(4)
        h1.metric("Sent", f"{wk_total:,}")
        h2.metric("Delivered", f"{wk_delivered:,}", delta=f"{round(wk_delivered/wk_total*100)}%" if wk_total else None)
        h3.metric("Opened", f"{wk_opened:,}", delta=f"{round(wk_opened/wk_delivered*100)}%" if wk_delivered else None)
        h4.metric("Failed", f"{wk_failed:,}", delta_color="inverse")

        st.markdown("")

        # Full table for that week
        wk_display = wk_data[["chain", "user_name", "email", "status",
                              "delivered_at", "first_opened_at", "first_bounced_at"]].copy()
        wk_display = wk_display.sort_values(["chain", "email"])
        for col in ["delivered_at", "first_opened_at", "first_bounced_at"]:
            wk_display[col] = wk_display[col].dt.strftime("%b %d, %I:%M %p").fillna("—")

        def color_history(row):
            if row["Status"] in ("Bounced", "Not Delivered"):
                return ["background-color: #f8d7da"] * len(row)
            if row["Status"] == "Opened":
                return ["background-color: #d4edda"] * len(row)
            return [""] * len(row)

        st.dataframe(
            wk_display.rename(columns={
                "chain": "Chain", "user_name": "Name", "email": "Email",
                "status": "Status", "delivered_at": "Delivered",
                "first_opened_at": "Opened", "first_bounced_at": "Bounced",
            }).style.apply(color_history, axis=1),
            use_container_width=True, hide_index=True, height=500,
        )

        csv_hist = wk_display.to_csv(index=False)
        st.download_button("Download CSV", data=csv_hist,
                           file_name=f"sotu_week_{selected_week}.csv", mime="text/csv",
                           key="csv_history")
