"""
app.py — Streamlit UI for the Industrial Underwriting Pipeline.

Run with:  streamlit run app.py
"""

import io
import sys
import time
import tempfile
from datetime import date
from pathlib import Path

import pandas as pd
import streamlit as st

# ── Make sure local modules are importable when running from project root ──
sys.path.insert(0, str(Path(__file__).parent))

from extractor import get_extractor
from populator import populate_template, DEFAULT_ASSUMPTIONS
from report import generate_report
from log_comps import log_om_to_comps
from schemas import BrokerAssumptions, PropertyExtraction

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
TEMPLATE_PATH = Path(__file__).parent / "Industrial_UW_Model_v2.xlsx"
COMPS_PATH    = Path(__file__).parent / "om_comps.xlsx"
MARKET_DATA   = Path(__file__).parent / "Nashville_Industrial_Market.xlsx"

st.set_page_config(
    page_title="Industrial Underwriter",
    page_icon="🏭",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ─────────────────────────────────────────────
# SESSION STATE DEFAULTS
# ─────────────────────────────────────────────
for key, default in [
    ("step", 1),
    ("extraction", None),
    ("pdf_name", None),
    ("pdf_bytes", None),
    ("usage", None),
    ("api_key", ""),
]:
    if key not in st.session_state:
        st.session_state[key] = default


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────
def _parse_quarter_date(df: "pd.DataFrame") -> "pd.Series":
    """Convert Year + Period (Q1-Q4) columns to a proper datetime.
    Q1→Jan, Q2→Apr, Q3→Jul, Q4→Oct — real quarter start months.
    Only rows with actual quarters (Q1-Q4) are parsed; others become NaT.
    """
    import pandas as pd
    quarter_to_month = {"Q1": "01", "Q2": "04", "Q3": "07", "Q4": "10"}
    month_str = df["Period"].map(quarter_to_month)
    return pd.to_datetime(
        df["Year"].astype(str) + "-" + month_str,
        format="%Y-%m",
        errors="coerce",
    )


def _load_market_context():
    """Return last 12 quarters of asking rent & vacancy for the Nashville W/D market."""
    if not MARKET_DATA.exists():
        return None
    try:
        df = pd.read_excel(MARKET_DATA, sheet_name="Market Performance Trends")
        df = df[df["Sector"] == "Warehouse/Distribution"].copy()
        df["date"] = _parse_quarter_date(df)
        df = df.dropna(subset=["date", "Asking Rent/SF", "Vac %"])
        df = df.sort_values("date").tail(12)
        # Format date as readable quarter label for the chart
        df["quarter"] = df["date"].dt.strftime("%Y Q") + ((df["date"].dt.month - 1) // 3 + 1).astype(str)
        return df[["quarter", "Asking Rent/SF", "Vac %"]].reset_index(drop=True)
    except Exception:
        return None


def _load_transaction_context():
    """Return last 12 quarters of median price/SF and cap rate."""
    if not MARKET_DATA.exists():
        return None
    try:
        df = pd.read_excel(MARKET_DATA, sheet_name="Market Transactions")
        df["date"] = _parse_quarter_date(df)
        df = df.dropna(subset=["date"])
        df = df.sort_values("date").tail(12)
        df["quarter"] = df["date"].dt.strftime("%Y Q") + ((df["date"].dt.month - 1) // 3 + 1).astype(str)
        return df[["quarter", "Median Sales Price Per SF", "Median Transaction Cap Rate"]].reset_index(drop=True)
    except Exception:
        return None


def _get_val(ba: BrokerAssumptions | None, field: str, default_key: str):
    val = getattr(ba, field, None) if ba else None
    return val if val is not None else DEFAULT_ASSUMPTIONS.get(default_key)


def _badge(extracted: bool) -> str:
    return "✅ extracted" if extracted else "⚠️ defaulted"


def _is_extracted(extraction: PropertyExtraction, field: str) -> bool:
    """Check if a broker assumption field was actually in the OM."""
    ba = extraction.broker_assumptions
    if ba is None:
        return False
    return getattr(ba, field, None) is not None


# ─────────────────────────────────────────────
# SIDEBAR — PROGRESS
# ─────────────────────────────────────────────
def sidebar():
    with st.sidebar:
        st.title("🏭 Industrial UW")

        # ── Provider & Model ─────────────────
        st.markdown("---")
        provider = st.selectbox("LLM Provider", ["anthropic", "gemini"], index=0,
                                key="provider")
        model_options = {
            "anthropic": ["claude-sonnet-4-6", "claude-opus-4-6", "claude-haiku-4-5"],
            "gemini":    ["gemini-2.5-pro", "gemini-2.0-flash"],
        }
        st.selectbox("Model", model_options[provider], key="model")

        # ── API Key — reacts to provider ─────
        st.markdown("---")
        import os
        if provider == "anthropic":
            env_key     = os.environ.get("ANTHROPIC_API_KEY", "")
            label       = "Anthropic API Key"
            placeholder = "sk-ant-..."
            help_text   = "Get yours at console.anthropic.com"
            key_sstate  = "api_key_anthropic"
        else:
            env_key     = os.environ.get("GOOGLE_API_KEY", "")
            label       = "Google API Key"
            placeholder = "AIza..."
            help_text   = "Get yours at aistudio.google.com"
            key_sstate  = "api_key_google"

        # Initialise session state slot if needed
        if key_sstate not in st.session_state:
            st.session_state[key_sstate] = ""

        if env_key:
            st.session_state[key_sstate] = env_key
            st.success(f"🔑 {label} configured", icon="✅")
        else:
            entered = st.text_input(
                label,
                value=st.session_state[key_sstate],
                type="password",
                placeholder=placeholder,
                help=f"{help_text}. Held only in this browser session, never stored.",
            )
            st.session_state[key_sstate] = entered
            if entered:
                st.success("🔑 Key entered", icon="✅")
            else:
                st.warning(f"Enter your {label} to begin")

        # ── Progress steps ───────────────────
        st.markdown("---")
        steps = ["1 · Upload & Extract", "2 · Review Assumptions", "3 · Download Model"]
        for i, label in enumerate(steps, 1):
            if i < st.session_state.step:
                st.success(f"~~{label}~~" if False else label)
            elif i == st.session_state.step:
                st.info(f"**→ {label}**")
            else:
                st.markdown(f"&nbsp;&nbsp;&nbsp;{label}")

        if st.session_state.step > 1:
            st.markdown("---")
            if st.button("↩ Start Over", use_container_width=True):
                for k in ["step", "extraction", "pdf_name", "pdf_bytes", "usage"]:
                    st.session_state[k] = {"step": 1}.get(k)
                st.session_state.step = 1
                st.rerun()


# ─────────────────────────────────────────────
# STEP 1 — UPLOAD & EXTRACT
# ─────────────────────────────────────────────
def step_upload():
    st.header("Step 1 — Upload Offering Memorandum")

    uploaded = st.file_uploader(
        "Drop an industrial OM PDF here",
        type=["pdf"],
        help="Text-based PDFs work best. Scanned/image-only OMs may extract poorly.",
    )

    provider = st.session_state.get("provider", "anthropic")
    model    = st.session_state.get("model", "claude-sonnet-4-6")
    api_key  = st.session_state.get(
        "api_key_anthropic" if provider == "anthropic" else "api_key_google", ""
    )

    if not TEMPLATE_PATH.exists():
        st.error(f"Template not found: `{TEMPLATE_PATH}`. Place `Industrial_UW_Model_v2.xlsx` in the same folder.")
        return

    if uploaded:
        st.success(f"**{uploaded.name}** — {uploaded.size / 1024:.0f} KB")

        if not api_key:
            st.info("Enter your API key in the sidebar to continue.")
            return

        if st.button("🔍 Extract from PDF", type="primary", use_container_width=True):
            pdf_bytes = uploaded.read()
            with st.spinner("Sending to LLM — this takes 30–90 seconds for a full OM…"):
                try:
                    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
                        f.write(pdf_bytes)
                        tmp_path = Path(f.name)

                    extractor = get_extractor(provider=provider, model=model, api_key=api_key)
                    extraction = extractor.extract(tmp_path)
                    tmp_path.unlink(missing_ok=True)
                    tmp_path.unlink(missing_ok=True)

                    st.session_state.extraction = extraction
                    st.session_state.pdf_name   = uploaded.name
                    st.session_state.pdf_bytes  = pdf_bytes
                    st.session_state.usage      = extractor.last_usage
                    st.session_state.step       = 2
                    st.rerun()

                except Exception as e:
                    st.error(f"Extraction failed: {e}")

    # Market context preview
    mkt = _load_market_context()
    if mkt is not None:
        with st.expander("📊 Nashville W/D Market Context (last 12 quarters)", expanded=True):
            col_a, col_b = st.columns(2)
            with col_a:
                st.subheader("Asking Rent ($/SF NNN)")
                st.line_chart(mkt.set_index("quarter")["Asking Rent/SF"])
            with col_b:
                st.subheader("Vacancy Rate (%)")
                vac_pct = mkt[["quarter", "Vac %"]].copy()
                vac_pct["Vac %"] = vac_pct["Vac %"] * 100
                st.line_chart(vac_pct.set_index("quarter")["Vac %"])


# ─────────────────────────────────────────────
# STEP 2 — REVIEW ASSUMPTIONS
# ─────────────────────────────────────────────
def step_review():
    extraction: PropertyExtraction = st.session_state.extraction
    pi  = extraction.property_info
    ba  = extraction.broker_assumptions

    st.header(f"Step 2 — Review: {pi.name}")

    # Usage stats
    if st.session_state.usage:
        u = st.session_state.usage
        st.caption(
            f"Extracted in {u['elapsed_s']:.0f}s · "
            f"{u['input_tokens']:,} input + {u['output_tokens']:,} output tokens · "
            f"${u['cost_usd']:.2f}"
        )

    # ── Market context ────────────────────────
    txn = _load_transaction_context()
    mkt = _load_market_context()

    left_col, right_col = st.columns([1, 1], gap="large")

    # ── LEFT: Property + Rent Roll Summary ───
    with left_col:
        st.subheader("Property Summary")
        info_rows = {
            "Address":    f"{pi.address or ''}, {pi.city or ''} {pi.state or ''}".strip(", "),
            "Submarket":  pi.submarket,
            "Total SF":   f"{pi.total_sf:,}",
            "Buildings":  pi.num_buildings,
            "Year Built": pi.year_built,
            "Land":       f"{pi.land_acres} ac" if pi.land_acres else "—",
        }
        for k, v in info_rows.items():
            st.markdown(f"**{k}:** {v or '—'}")

        st.subheader("Rent Roll")
        tenants = extraction.tenants
        occupied = [t for t in tenants if not t.name.upper().startswith("VACANT")]
        vacant   = [t for t in tenants if t.name.upper().startswith("VACANT")]

        m1, m2, m3 = st.columns(3)
        m1.metric("Tenants", len(occupied))
        m2.metric("Occupancy", f"{sum(t.sf for t in occupied)/pi.total_sf*100:.1f}%")
        if occupied:
            wt_rent = sum(t.current_rent_psf * t.sf for t in occupied) / sum(t.sf for t in occupied)
            m3.metric("Avg In-Place Rent", f"${wt_rent:.2f}/SF")

        if tenants:
            rr_data = [
                {
                    "Suite": t.suite,
                    "Tenant": t.name,
                    "SF": f"{t.sf:,}",
                    "Rent $/SF": f"${t.current_rent_psf:.2f}",
                    "Expiry": t.lease_end.strftime("%m/%Y") if t.lease_end else "—",
                    "Mkt $/SF": f"${t.market_rent_psf:.2f}" if t.market_rent_psf else "—",
                }
                for t in sorted(tenants, key=lambda x: -x.sf)
            ]
            st.dataframe(rr_data, hide_index=True, use_container_width=True)

        # Flags
        defaulted_fields = [f for f in [
            "market_rent_y1_psf", "rent_growth_schedule", "ti_new_psf", "ti_renewal_psf",
            "lc_new_pct", "lc_renewal_pct", "exit_cap_rate",
        ] if not _is_extracted(extraction, f)]
        if defaulted_fields:
            st.warning(f"⚠️ {len(defaulted_fields)} assumptions defaulted — review in the panel to the right.")

        if extraction.extraction_notes:
            with st.expander("📝 Extraction Notes from Model"):
                st.info(extraction.extraction_notes)

    # ── RIGHT: Editable Assumptions ──────────
    with right_col:
        st.subheader("Assumptions")

        # ── Deal Economics ───────────────────
        with st.expander("💰 Deal Economics", expanded=True):
            default_price_psf = 100.0
            txn_ref = ""
            if txn is not None and not txn.empty:
                last = txn.dropna(subset=["Median Sales Price Per SF"]).iloc[-1]
                txn_ref = f"  *(market median: ${last['Median Sales Price Per SF']:.0f}/SF, {last['quarter']})*"

            price_psf = st.number_input(
                f"Purchase Price ($/SF){txn_ref}", min_value=10.0, max_value=500.0,
                value=default_price_psf, step=5.0,
                help="Default $100/SF. Adjust based on market comps.",
            )
            purchase_price = price_psf * pi.total_sf

            st.caption(f"→ Total: **${purchase_price:,.0f}**")

            c1, c2 = st.columns(2)
            with c1:
                ltv = st.number_input("LTV (%)", 40, 80, 65, step=5) / 100
                interest_rate = st.number_input("Interest Rate (%)", 3.0, 12.0, 6.5, step=0.25) / 100
                io_years = st.number_input("I/O Period (Years)", 0, 5, 2, step=1)
            with c2:
                amortization = st.number_input("Amortization (Years)", 15, 40, 30, step=5)
                loan_fee = st.number_input("Loan Fee (%)", 0.0, 3.0, 1.0, step=0.25) / 100
                hold_period = st.number_input("Hold Period (Years)", 3, 15, 10, step=1)

            loan_amount = purchase_price * ltv
            equity      = purchase_price * (1 - ltv)
            st.caption(
                f"Loan: **${loan_amount:,.0f}** · Equity: **${equity:,.0f}**"
            )

        # ── Market Assumptions ───────────────
        with st.expander("📈 Market & Exit", expanded=True):
            mkt_ref = ""
            if mkt is not None and not mkt.empty:
                last_rent = mkt.dropna(subset=["Asking Rent/SF"]).iloc[-1]["Asking Rent/SF"]
                mkt_ref = f"  *(market asking: ${last_rent:.2f}/SF)*"

            extracted_mkt = _is_extracted(extraction, "market_rent_y1_psf")
            market_rent = st.number_input(
                f"Market Rent Y1 ($/SF NNN){mkt_ref} {_badge(extracted_mkt)}",
                min_value=1.0, max_value=50.0,
                value=float(_get_val(ba, "market_rent_y1_psf", "market_rent_y1_psf")),
                step=0.25,
            )

            extracted_cap = _is_extracted(extraction, "exit_cap_rate")
            exit_cap = st.number_input(
                f"Exit Cap Rate (%) {_badge(extracted_cap)}",
                min_value=3.0, max_value=12.0,
                value=float(_get_val(ba, "exit_cap_rate", "exit_cap_rate")) * 100,
                step=0.25,
            ) / 100

            # Implied going-in cap (rough: NOI estimate / price)
            opex_estimate = (
                (_get_val(ba, "cam_psf", "cam_psf") or 0) +
                (_get_val(ba, "insurance_psf", "insurance_psf") or 0) +
                (_get_val(ba, "real_estate_taxes_psf", "real_estate_taxes_psf") or 0)
            )
            if occupied:
                wt_rent_val = sum(t.current_rent_psf * t.sf for t in occupied) / sum(t.sf for t in occupied)
                implied_noi = (wt_rent_val - opex_estimate) * pi.total_sf * (1 - 0.02)
                going_in_cap = implied_noi / purchase_price if purchase_price else 0
                st.caption(
                    f"Implied going-in cap: **{going_in_cap*100:.2f}%** "
                    f"(in-place NOI est. ${implied_noi:,.0f})"
                )

        # ── Rent Growth ──────────────────────
        with st.expander(f"📊 Rent Growth Schedule {_badge(_is_extracted(extraction, 'rent_growth_schedule'))}"):
            default_growth = _get_val(ba, "rent_growth_schedule", "rent_growth_schedule")
            growth_vals = []
            cols = st.columns(5)
            labels = [f"CY{2024+i}" for i in range(10)]
            for i in range(10):
                default = float(default_growth[i]) * 100 if default_growth and i < len(default_growth) else 3.0
                g = cols[i % 5].number_input(labels[i], 0.0, 20.0, default, step=0.5, key=f"gr_{i}")
                growth_vals.append(g / 100)

        # ── Leasing Assumptions ──────────────
        with st.expander("🔑 Leasing Assumptions"):
            c1, c2 = st.columns(2)
            with c1:
                ti_new   = st.number_input(f"TI — New ($/SF) {_badge(_is_extracted(extraction, 'ti_new_psf'))}", 0.0, 50.0, float(_get_val(ba, "ti_new_psf", "ti_new_psf")), step=0.5)
                lc_new   = st.number_input(f"LC — New (%) {_badge(_is_extracted(extraction, 'lc_new_pct'))}", 0.0, 15.0, float(_get_val(ba, "lc_new_pct", "lc_new_pct")) * 100, step=0.5) / 100
                fr_new   = st.number_input(f"Free Rent — New (mo) {_badge(_is_extracted(extraction, 'free_rent_new_months'))}", 0, 12, int(_get_val(ba, "free_rent_new_months", "free_rent_new_months")))
                downtime = st.number_input(f"Downtime — New (mo) {_badge(_is_extracted(extraction, 'downtime_new_months'))}", 0, 24, int(_get_val(ba, "downtime_new_months", "downtime_new_months")))
            with c2:
                ti_ren   = st.number_input(f"TI — Renewal ($/SF) {_badge(_is_extracted(extraction, 'ti_renewal_psf'))}", 0.0, 20.0, float(_get_val(ba, "ti_renewal_psf", "ti_renewal_psf")), step=0.25)
                lc_ren   = st.number_input(f"LC — Renewal (%) {_badge(_is_extracted(extraction, 'lc_renewal_pct'))}", 0.0, 10.0, float(_get_val(ba, "lc_renewal_pct", "lc_renewal_pct")) * 100, step=0.25) / 100
                fr_ren   = st.number_input(f"Free Rent — Renewal (mo) {_badge(_is_extracted(extraction, 'free_rent_renewal_months'))}", 0, 6, int(_get_val(ba, "free_rent_renewal_months", "free_rent_renewal_months")))
                retention = st.number_input("Retention Ratio (%)", 0, 100, 75, step=5) / 100

        # ── Operating Expenses ───────────────
        with st.expander("🏗️ Operating Expenses"):
            c1, c2 = st.columns(2)
            with c1:
                cam       = st.number_input(f"CAM ($/SF) {_badge(_is_extracted(extraction, 'cam_psf'))}", 0.0, 5.0, float(_get_val(ba, "cam_psf", "cam_psf")), step=0.05)
                insurance = st.number_input(f"Insurance ($/SF) {_badge(_is_extracted(extraction, 'insurance_psf'))}", 0.0, 2.0, float(_get_val(ba, "insurance_psf", "insurance_psf")), step=0.01)
                re_taxes  = st.number_input(f"RE Taxes ($/SF) {_badge(_is_extracted(extraction, 'real_estate_taxes_psf'))}", 0.0, 5.0, float(_get_val(ba, "real_estate_taxes_psf", "real_estate_taxes_psf")), step=0.05)
                utilities = st.number_input(f"Utilities ($/SF) {_badge(_is_extracted(extraction, 'utilities_psf'))}", 0.0, 2.0, float(ba.utilities_psf or 0.0) if ba else 0.0, step=0.01)
            with c2:
                mgmt_fee      = st.number_input(f"Mgmt Fee (% EGR) {_badge(_is_extracted(extraction, 'mgmt_fee_pct'))}", 0.0, 6.0, float(_get_val(ba, "mgmt_fee_pct", "mgmt_fee_pct")) * 100, step=0.25) / 100
                exp_growth    = st.number_input(f"Expense Growth (%/yr) {_badge(_is_extracted(extraction, 'expense_growth_pct'))}", 0.0, 10.0, float(_get_val(ba, "expense_growth_pct", "expense_growth_pct")) * 100, step=0.25) / 100
                gen_vacancy   = st.number_input(f"General Vacancy (%) {_badge(_is_extracted(extraction, 'general_vacancy_pct'))}", 0.0, 10.0, float(_get_val(ba, "general_vacancy_pct", "general_vacancy_pct")) * 100, step=0.25) / 100
                capex         = st.number_input(f"CapEx Reserves ($/SF) {_badge(_is_extracted(extraction, 'capex_reserves_psf'))}", 0.0, 2.0, float(_get_val(ba, "capex_reserves_psf", "capex_reserves_psf")), step=0.05)

            total_opex_display = cam + insurance + re_taxes + utilities
            st.caption(f"Total hard OpEx: **${total_opex_display:.2f}/SF** (excl. mgmt, vacancy, capex)")

        # ── Generate ─────────────────────────
        st.markdown("---")
        if st.button("⚙️ Generate Excel Model", type="primary", use_container_width=True):
            # Build override BrokerAssumptions
            overrides = BrokerAssumptions(
                analysis_start_date=ba.analysis_start_date if ba else None,
                market_rent_y1_psf=market_rent,
                rent_growth_schedule=growth_vals,
                ti_new_psf=ti_new,
                ti_renewal_psf=ti_ren,
                lc_new_pct=lc_new,
                lc_renewal_pct=lc_ren,
                free_rent_new_months=int(fr_new),
                free_rent_renewal_months=int(fr_ren),
                downtime_new_months=int(downtime),
                exit_cap_rate=exit_cap,
                cam_psf=cam,
                insurance_psf=insurance,
                real_estate_taxes_psf=re_taxes,
                utilities_psf=utilities if utilities > 0 else None,
                mgmt_fee_pct=mgmt_fee,
                expense_growth_pct=exp_growth,
                general_vacancy_pct=gen_vacancy,
                capex_reserves_psf=capex,
            )
            extraction.broker_assumptions = overrides

            deal_economics = {
                "purchase_price": purchase_price,
                "ltv":            ltv,
                "interest_rate":  interest_rate,
                "amortization":   amortization,
                "io_years":       io_years,
                "loan_fee":       loan_fee,
            }

            st.session_state["_overrides"]      = overrides
            st.session_state["_deal_economics"] = deal_economics
            st.session_state.step = 3
            st.rerun()


# ─────────────────────────────────────────────
# STEP 3 — GENERATE & DOWNLOAD
# ─────────────────────────────────────────────
def step_generate():
    extraction: PropertyExtraction = st.session_state.extraction
    deal_economics = st.session_state.get("_deal_economics", {})
    pdf_name = st.session_state.pdf_name
    pi = extraction.property_info

    st.header(f"Step 3 — Model Ready: {pi.name}")

    with st.spinner("Populating Excel model…"):
        try:
            stem = Path(pdf_name).stem
            out_dir = Path(tempfile.mkdtemp())
            model_path  = out_dir / f"{stem}_UW_Model.xlsx"
            report_path = out_dir / f"{stem}_Extraction_Report.md"

            pop_result = populate_template(
                extraction=extraction,
                template_path=TEMPLATE_PATH,
                output_path=model_path,
                deal_economics=deal_economics,
            )

            report_md = generate_report(
                extraction=extraction,
                population_result=pop_result,
                pdf_name=pdf_name,
                usage=st.session_state.usage,
                output_path=report_path,
            )

            # Log to comps
            try:
                log_om_to_comps(extraction, pdf_name, COMPS_PATH)
            except Exception as e:
                st.warning(f"Comps logging skipped: {e}")

            model_bytes  = model_path.read_bytes()
            report_bytes = report_path.read_bytes()

        except Exception as e:
            st.error(f"Model generation failed: {e}")
            import traceback; st.code(traceback.format_exc())
            return

    # ── Download buttons ──────────────────────
    st.success("✅ Model generated successfully")
    col1, col2 = st.columns(2)
    with col1:
        st.download_button(
            label="⬇️ Download Excel Model",
            data=model_bytes,
            file_name=f"{stem}_UW_Model.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True,
        )
    with col2:
        st.download_button(
            label="⬇️ Download Extraction Report",
            data=report_bytes,
            file_name=f"{stem}_Extraction_Report.md",
            mime="text/markdown",
            use_container_width=True,
        )

    # ── Key Metrics Summary ───────────────────
    st.markdown("---")
    st.subheader("Deal Snapshot")

    ba = extraction.broker_assumptions
    de = deal_economics
    purchase_price = de.get("purchase_price", 0)
    price_psf      = purchase_price / pi.total_sf if pi.total_sf else 0

    occupied = [t for t in extraction.tenants if not t.name.upper().startswith("VACANT")]
    wt_rent  = (sum(t.current_rent_psf * t.sf for t in occupied) / sum(t.sf for t in occupied)) if occupied else 0
    opex_psf = sum(x for x in [ba.cam_psf, ba.insurance_psf, ba.real_estate_taxes_psf] if x) if ba else 0
    noi_est  = (wt_rent - opex_psf) * pi.total_sf * (1 - (ba.general_vacancy_pct or 0.02)) if ba else 0
    going_in = noi_est / purchase_price if purchase_price else 0

    mc1, mc2, mc3, mc4, mc5 = st.columns(5)
    mc1.metric("Purchase Price", f"${purchase_price/1e6:.2f}M")
    mc2.metric("Price / SF",     f"${price_psf:.0f}")
    mc3.metric("Going-In Cap",   f"{going_in*100:.2f}%")
    mc4.metric("Exit Cap",       f"{(ba.exit_cap_rate or 0.065)*100:.2f}%")
    mc5.metric("LTV",            f"{de.get('ltv', 0)*100:.0f}%")

    mc6, mc7, mc8, mc9, mc10 = st.columns(5)
    mc6.metric("Total SF",          f"{pi.total_sf:,}")
    mc7.metric("In-Place Rent/SF",  f"${wt_rent:.2f}")
    mc8.metric("Market Rent/SF",    f"${ba.market_rent_y1_psf:.2f}" if ba and ba.market_rent_y1_psf else "—")
    mc9.metric("Tenants",           len(occupied))
    mc10.metric("Est. Y1 NOI",      f"${noi_est:,.0f}")

    # Extraction quality
    st.markdown("---")
    st.subheader("Extraction Quality")
    total_fields = 15
    extracted_ct = 15 - len(pop_result.defaulted_fields)
    st.progress(extracted_ct / total_fields, text=f"{extracted_ct}/{total_fields} assumption fields extracted from OM")

    if pop_result.defaulted_fields:
        with st.expander(f"⚠️ {len(pop_result.defaulted_fields)} Defaulted Fields"):
            df_defaults = pd.DataFrame(pop_result.defaulted_fields)
            st.dataframe(df_defaults, hide_index=True, use_container_width=True)

    if pop_result.warnings:
        for w in pop_result.warnings:
            st.warning(w)

    # Comps preview
    if COMPS_PATH.exists():
        with st.expander("📋 OM Comps Database", expanded=False):
            comps_df = pd.read_excel(COMPS_PATH)
            st.dataframe(comps_df, hide_index=True, use_container_width=True)
            st.download_button(
                "⬇️ Download Comps Sheet",
                data=COMPS_PATH.read_bytes(),
                file_name="om_comps.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )

    # Extraction report inline
    with st.expander("📄 Extraction Report (Markdown)", expanded=False):
        st.markdown(report_md)


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
sidebar()

if st.session_state.step == 1:
    step_upload()
elif st.session_state.step == 2:
    step_review()
elif st.session_state.step == 3:
    step_generate()
