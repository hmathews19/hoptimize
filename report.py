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
    if comp.get('broker_assumption_fields_populated'):
        lines.append(f"- Broker assumption fields: {comp['broker_assumption_fields_populated']}/15 populated")
    lines.append(f"- Tenants extracted: {comp['num_tenants']}")
    lines.append(f"- Tenants with full lease dates: {comp['tenants_with_dates']}")
    lines.append(f"- Tenants with market rent assumption: {comp['tenants_with_market_rent']}")
    lines.append("")

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
