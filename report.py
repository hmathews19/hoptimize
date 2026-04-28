"""
report.py — Generate markdown extraction reports.

The report shows what was extracted, what was defaulted, and flags things
that need deal-team review. This is critical for trust — underwriters won't
use the tool if they can't audit what the LLM pulled.
"""

from datetime import date
from pathlib import Path

from schemas import PropertyExtraction
from populator import PopulationResult


def generate_report(
    extraction: PropertyExtraction,
    population_result: PopulationResult,
    pdf_name: str,
    usage: dict = None,
    output_path: Path = None,
) -> str:
    """Generate a markdown extraction report. Returns the markdown string;
    optionally writes to output_path."""

    lines = []
    lines.append(f"# Extraction Report — {extraction.property_info.name}")
    lines.append("")
    lines.append(f"**Source:** `{pdf_name}`  ")
    lines.append(f"**Extracted:** {date.today().isoformat()}  ")
    if usage:
        lines.append(
            f"**LLM Usage:** {usage['input_tokens']:,} input + "
            f"{usage['output_tokens']:,} output tokens, "
            f"${usage['cost_usd']:.2f}, {usage['elapsed_s']:.1f}s"
        )
    lines.append("")
    lines.append("---")
    lines.append("")

    # Property Summary
    lines.append("## Property Summary")
    lines.append("")
    pi = extraction.property_info
    lines.append(f"- **Name:** {pi.name}")
    if pi.address:
        lines.append(f"- **Address:** {pi.address}, {pi.city or ''} {pi.state or ''} {pi.zip_code or ''}".strip())
    if pi.submarket:
        lines.append(f"- **Submarket:** {pi.submarket}")
    lines.append(f"- **Total SF:** {pi.total_sf:,}")
    if pi.num_buildings:
        lines.append(f"- **Buildings:** {pi.num_buildings}")
    if pi.year_built:
        lines.append(f"- **Year Built:** {pi.year_built}")
    if pi.land_acres:
        lines.append(f"- **Land:** {pi.land_acres} acres")
    lines.append("")

    # Rent Roll Summary
    lines.append("## Rent Roll Summary")
    lines.append("")
    if not extraction.tenants:
        lines.append("⚠️ **No tenants extracted.** This is unusual — review OM manually.")
    else:
        total_sf = sum(t.sf for t in extraction.tenants)
        occupied_sf = sum(t.sf for t in extraction.tenants if t.name.upper() != 'VACANT' and not t.name.upper().startswith('VACANT'))
        vacant_sf = total_sf - occupied_sf
        lines.append(f"- **Tenants extracted:** {len(extraction.tenants)}")
        lines.append(f"- **Total leased SF:** {total_sf:,}")
        lines.append(f"- **Occupied SF:** {occupied_sf:,} ({occupied_sf/pi.total_sf*100:.1f}% of property)")
        lines.append(f"- **Vacant SF:** {vacant_sf:,} ({vacant_sf/pi.total_sf*100:.1f}% of property)")

        # Check for tenants without dates (flag for review)
        tenants_no_dates = [t for t in extraction.tenants if not (t.lease_start and t.lease_end)]
        if tenants_no_dates:
            lines.append(f"- ⚠️ **{len(tenants_no_dates)} tenants missing lease dates** (will not be modeled correctly):")
            for t in tenants_no_dates[:10]:
                missing = []
                if not t.lease_start: missing.append("start")
                if not t.lease_end: missing.append("end")
                lines.append(f"  - Suite {t.suite} / {t.name}: missing {', '.join(missing)}")

        # Top 5 tenants by SF
        lines.append("")
        lines.append("### Top 5 Tenants by SF")
        lines.append("")
        lines.append("| Suite | Tenant | SF | Rent $/SF | Lease End |")
        lines.append("|---|---|---:|---:|---|")
        for t in sorted(extraction.tenants, key=lambda x: -x.sf)[:5]:
            end = t.lease_end.isoformat() if t.lease_end else "—"
            lines.append(f"| {t.suite} | {t.name} | {t.sf:,} | ${t.current_rent_psf:.2f} | {end} |")
        lines.append("")

    # Broker Assumptions
    lines.append("## Broker Assumptions")
    lines.append("")
    ba = extraction.broker_assumptions
    if ba is None:
        lines.append("⚠️ No broker assumptions section extracted. Defaults applied.")
    else:
        assumption_rows = [
            ("Analysis Start", ba.analysis_start_date),
            ("Market Rent Y1 ($/SF)", f"${ba.market_rent_y1_psf:.2f}" if ba.market_rent_y1_psf else None),
            ("TI — New ($/SF)", f"${ba.ti_new_psf:.2f}" if ba.ti_new_psf else None),
            ("TI — Renewal ($/SF)", f"${ba.ti_renewal_psf:.2f}" if ba.ti_renewal_psf else None),
            ("LC — New (%)", f"{ba.lc_new_pct*100:.1f}%" if ba.lc_new_pct else None),
            ("LC — Renewal (%)", f"{ba.lc_renewal_pct*100:.1f}%" if ba.lc_renewal_pct else None),
            ("Free Rent — New (mo)", ba.free_rent_new_months),
            ("Downtime — New (mo)", ba.downtime_new_months),
            ("Management Fee (%)", f"{ba.mgmt_fee_pct*100:.1f}%" if ba.mgmt_fee_pct else None),
            ("General Vacancy (%)", f"{ba.general_vacancy_pct*100:.1f}%" if ba.general_vacancy_pct else None),
            ("Expense Growth (%)", f"{ba.expense_growth_pct*100:.1f}%" if ba.expense_growth_pct else None),
            ("Capex Reserves ($/SF)", f"${ba.capex_reserves_psf:.2f}" if ba.capex_reserves_psf else None),
            ("CAM ($/SF)", f"${ba.cam_psf:.2f}" if ba.cam_psf else None),
            ("Insurance ($/SF)", f"${ba.insurance_psf:.2f}" if ba.insurance_psf else None),
            ("RE Taxes ($/SF)", f"${ba.real_estate_taxes_psf:.2f}" if ba.real_estate_taxes_psf else None),
        ]
        if ba.rent_growth_schedule:
            growth_str = ", ".join(f"{g*100:.1f}%" for g in ba.rent_growth_schedule)
            lines.append(f"- **Rent Growth (10yr):** {growth_str}")
        lines.append("")
        lines.append("| Field | Extracted Value |")
        lines.append("|---|---|")
        for label, val in assumption_rows:
            display = val if val is not None else "—"
            lines.append(f"| {label} | {display} |")
        lines.append("")

    # Extraction notes from the LLM
    if extraction.extraction_notes:
        lines.append("## Extraction Notes from Model")
        lines.append("")
        lines.append(f"> {extraction.extraction_notes}")
        lines.append("")

    # What was defaulted
    lines.append("## Defaulted Fields (need review)")
    lines.append("")
    if not population_result.defaulted_fields:
        lines.append("None — all fields extracted from OM.")
    else:
        lines.append("These fields were not in the OM and used standard defaults. Review on the Assumptions tab.")
        lines.append("")
        lines.append("| Field | Default Value | Reason |")
        lines.append("|---|---|---|")
        for d in population_result.defaulted_fields:
            val = d["default_value"]
            if isinstance(val, float):
                if abs(val) < 1:
                    val_str = f"{val*100:.1f}%"
                else:
                    val_str = f"{val:.2f}"
            elif isinstance(val, list):
                val_str = ", ".join(f"{x*100:.1f}%" if isinstance(x, float) else str(x) for x in val[:3]) + "..."
            else:
                val_str = str(val)
            lines.append(f"| {d['field']} | {val_str} | {d['reason']} |")
        lines.append("")

    # Warnings
    if population_result.warnings:
        lines.append("## Warnings")
        lines.append("")
        for w in population_result.warnings:
            lines.append(f"- ⚠️ {w}")
        lines.append("")

    # Completeness
    lines.append("## Completeness Check")
    lines.append("")
    comp = extraction.completeness_report()
    lines.append(f"- Property info fields populated: {sum(1 for v in comp['property_info'].values() if v)}/{len(comp['property_info'])}")
    lines.append(f"- Market data section: {'✓' if comp['has_market_data'] else '✗'}")
    lines.append(f"- Broker assumptions section: {'✓' if comp['has_broker_assumptions'] else '✗'}")
    if comp.get('broker_assumption_fields_populated') is not None:
        from schemas import BrokerAssumptions
        total_ba_fields = len(BrokerAssumptions.model_fields)
        lines.append(f"- Broker assumption fields: {comp['broker_assumption_fields_populated']}/{total_ba_fields} populated")
    lines.append(f"- Tenants extracted: {comp['num_tenants']}")
    lines.append(f"- Tenants with full lease dates: {comp['tenants_with_dates']}")
    lines.append(f"- Tenants with market rent assumption: {comp['tenants_with_market_rent']}")
    lines.append("")

    # Market Context
    market_context = _build_market_context(extraction, usage)
    if market_context:
        lines.append("")
        lines.extend(market_context)

    # Footer
    lines.append("---")
    lines.append("")
    lines.append("**Next steps:**")
    lines.append("")
    lines.append("1. Open the populated Excel model")
    lines.append("2. Review defaulted fields (highlighted on Assumptions tab) and adjust as needed")
    lines.append("3. Set Purchase Price and Debt Terms (these are always deal-team inputs)")
    lines.append("4. Review returns on Summary tab")

    markdown = "\n".join(lines)

    if output_path:
        Path(output_path).write_text(markdown, encoding="utf-8")

    return markdown


# ─────────────────────────────────────────────────────────────
# MARKET CONTEXT HELPERS
# ─────────────────────────────────────────────────────────────

def _load_market_data() -> tuple:
    """
    Load Nashville market data from om_market_data.xlsx if present next to this script,
    or from Nashville_Industrial_Market.xlsx. Returns (perf_df, txn_df) or (None, None).
    """
    try:
        import pandas as pd
    except ImportError:
        return None, None

    # Search for the file relative to this script
    search_paths = [
        Path(__file__).parent / "Nashville_Industrial_Market.xlsx",
        Path(__file__).parent / "om_market_data.xlsx",
    ]
    mkt_path = next((p for p in search_paths if p.exists()), None)
    if mkt_path is None:
        return None, None

    try:
        xl = pd.ExcelFile(mkt_path)
        perf = xl.parse("Market Performance Trends")
        txn  = xl.parse("Market Transactions")

        # Filter to real quarters and W/D sector
        q_mask = perf["Period"].isin(["Q1", "Q2", "Q3", "Q4"])
        wd_perf = perf[(perf["Sector"] == "Warehouse/Distribution") & q_mask].copy()

        q_mask_t = txn["Period"].isin(["Q1", "Q2", "Q3", "Q4"])
        wd_txn  = txn[q_mask_t].copy()

        return wd_perf, wd_txn
    except Exception:
        return None, None


def _build_market_context(extraction, usage: dict = None) -> list:
    """
    Build markdown lines for a Market Context section comparing the deal to
    recent Nashville W/D market data.
    """
    perf_df, txn_df = _load_market_data()
    if perf_df is None or txn_df is None:
        return []

    pi = extraction.property_info
    ba = extraction.broker_assumptions
    tenants = extraction.tenants

    lines = []
    lines.append("## Market Context")
    lines.append("")
    lines.append("*Based on Nashville Warehouse/Distribution market data. "
                 "Transaction comps skew toward larger assets — interpret price/SF and cap rate "
                 "comparisons with that in mind for sub-200K SF properties.*")
    lines.append("")

    # ── Last 4 quarters of performance data ──
    last4_perf = perf_df.tail(4)
    latest_perf = perf_df.iloc[-1]
    avg_asking  = last4_perf["Asking Rent/SF"].mean()
    avg_eff     = last4_perf["Effective Rent/SF"].mean()
    avg_vac     = last4_perf["Vac %"].mean()
    latest_qtr  = f"{int(latest_perf['Year'])} {latest_perf['Period']}"

    # ── Last 4 quarters of transaction data ──
    txn_valid = txn_df.dropna(subset=["Median Sales Price Per SF", "Median Transaction Cap Rate"])
    last4_txn  = txn_valid.tail(4)
    median_psf = last4_txn["Median Sales Price Per SF"].median()
    median_cap = last4_txn["Median Transaction Cap Rate"].median()
    latest_txn = txn_df.iloc[-1]
    txn_qtr    = f"{int(latest_txn['Year'])} {latest_txn['Period']}"

    # ── In-place rent ──
    occupied = [t for t in tenants if not t.name.upper().startswith("VACANT")]
    if occupied:
        total_occ_sf = sum(t.sf for t in occupied)
        in_place_wt  = sum(t.current_rent_psf * t.sf for t in occupied) / total_occ_sf
    else:
        in_place_wt = None

    # ── Rent comparison ──
    lines.append("### Rent")
    lines.append("")
    lines.append(f"| Metric | This Deal | Market (4Q avg, {latest_qtr}) | Spread |")
    lines.append("|---|---:|---:|---:|")
    if in_place_wt is not None:
        spread_inplace = in_place_wt - avg_asking
        indicator = "🟢" if spread_inplace >= 0 else "🔴"
        lines.append(f"| In-Place Rent (wtd avg) | ${in_place_wt:.2f}/SF | ${avg_asking:.2f}/SF | "
                     f"{indicator} {spread_inplace:+.2f}/SF |")
    if ba and ba.market_rent_y1_psf:
        spread_mkt = ba.market_rent_y1_psf - avg_asking
        indicator = "🟢" if spread_mkt >= 0 else "🔴"
        lines.append(f"| Broker Market Rent Assumption | ${ba.market_rent_y1_psf:.2f}/SF | "
                     f"${avg_asking:.2f}/SF | {indicator} {spread_mkt:+.2f}/SF |")
        lines.append(f"| Effective Rent (market) | — | ${avg_eff:.2f}/SF | — |")
    lines.append("")

    # ── Vacancy context ──
    prop_vac = 1.0 - (sum(t.sf for t in occupied) / pi.total_sf) if occupied else None
    lines.append("### Vacancy")
    lines.append("")
    lines.append(f"| Metric | This Property | Market (4Q avg) |")
    lines.append("|---|---:|---:|")
    if prop_vac is not None:
        vac_indicator = "🟢" if prop_vac <= avg_vac else "🔴"
        lines.append(f"| Vacancy Rate | {vac_indicator} {prop_vac*100:.1f}% | {avg_vac*100:.1f}% |")
    lines.append("")

    # ── Pricing & cap rate ──
    lines.append(f"### Pricing & Cap Rate *(4Q median as of {txn_qtr})*")
    lines.append("")
    lines.append("| Metric | Deal Input | Market Median | Note |")
    lines.append("|---|---:|---:|---|")
    if usage and usage.get("purchase_price_psf"):
        pp_psf = usage["purchase_price_psf"]
        pp_indicator = "🟢" if pp_psf <= median_psf else "🔴"
        lines.append(f"| Purchase Price/SF | {pp_indicator} ${pp_psf:.0f} | ${median_psf:.0f} | "
                     f"{'Below' if pp_psf <= median_psf else 'Above'} market median |")
    else:
        lines.append(f"| Market Median Price/SF | — | ${median_psf:.0f} | Set price in app to compare |")

    if ba and ba.exit_cap_rate:
        cap_indicator = "🟢" if ba.exit_cap_rate >= median_cap else "🔴"
        lines.append(f"| Exit Cap Rate | {cap_indicator} {ba.exit_cap_rate*100:.2f}% | "
                     f"{median_cap*100:.2f}% | "
                     f"{'Conservative (higher = safer)' if ba.exit_cap_rate >= median_cap else 'Aggressive (below market median)'} |")
    lines.append("")

    # ── Rent growth context ──
    if len(perf_df) >= 8:
        recent8  = perf_df.tail(8)
        rent_chg = (recent8["Asking Rent/SF"].iloc[-1] / recent8["Asking Rent/SF"].iloc[0] - 1)
        yoy_avg  = perf_df.tail(4)["Asking Rent % Chg"].mean()
        lines.append("### Rent Growth Context")
        lines.append("")
        lines.append(f"- Nashville W/D asking rent change over last 8 quarters: "
                     f"**{rent_chg*100:+.1f}%** "
                     f"(${recent8['Asking Rent/SF'].iloc[0]:.2f} → ${recent8['Asking Rent/SF'].iloc[-1]:.2f}/SF)")
        if not (yoy_avg != yoy_avg):  # NaN check
            lines.append(f"- Average YoY rent growth (last 4Q): **{yoy_avg*100:.1f}%**")
        if ba and ba.rent_growth_schedule:
            broker_avg = sum(ba.rent_growth_schedule[:4]) / 4
            lines.append(f"- Broker's modeled rent growth (Yr 1-4 avg): "
                         f"**{broker_avg*100:.1f}%/yr**")
        lines.append("")

    # ── Key flags ──
    flags = []
    if ba and ba.market_rent_y1_psf and in_place_wt:
        mtm_pct = (ba.market_rent_y1_psf / in_place_wt - 1) if in_place_wt > 0 else 0
        if mtm_pct > 0.25:
            flags.append(f"⚠️ **Mark-to-market upside of {mtm_pct*100:.0f}%** — "
                         f"significant NOI growth potential on rollover, but execution risk if market softens.")
        elif mtm_pct < -0.1:
            flags.append(f"⚠️ **In-place rents above market by {abs(mtm_pct)*100:.0f}%** — "
                         f"rollover risk; NOI may decline at lease expiration.")
    if extraction.extraction_notes and any(k in extraction.extraction_notes.lower()
                                           for k in ["bankruptcy", "ccaa", "distress", "chapter 15"]):
        flags.append("🔴 **Seller distress / court approval required** — "
                     "flag elevated execution risk and potential timeline uncertainty.")
    vacant_pct = sum(t.sf for t in tenants if t.name.upper().startswith("VACANT")) / pi.total_sf if pi.total_sf else 0
    if vacant_pct > 0.10:
        flags.append(f"⚠️ **{vacant_pct*100:.0f}% vacant** — lease-up assumption is a key value driver; "
                     f"stress-test downtime and market rent assumptions.")
    near_term = [t for t in occupied if t.lease_end and
                 (t.lease_end.year - date.today().year) * 12 + (t.lease_end.month - date.today().month) < 24]
    if near_term:
        near_sf = sum(t.sf for t in near_term)
        flags.append(f"⚠️ **{near_sf:,} SF ({near_sf/pi.total_sf*100:.0f}% of property) expires within 24 months** — "
                     f"near-term rollover risk; retention probability is key.")
    if flags:
        lines.append("### Deal Flags")
        lines.append("")
        for f in flags:
            lines.append(f"- {f}")
        lines.append("")

    return lines

