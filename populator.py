"""
populator.py — Write extracted PropertyExtraction into the Excel template.

The template has blue cells for inputs. This module copies the template to
an output location, then writes the extracted values into specific cells.

Defaults for missing fields come from DEFAULT_ASSUMPTIONS below. The report
generator flags which fields used extracted vs. defaulted values.
"""

from datetime import date
from pathlib import Path
from typing import Optional
import shutil

from openpyxl import load_workbook

from schemas import PropertyExtraction, BrokerAssumptions


# Defaults applied when OM doesn't state a value. Based on institutional norms
# for stabilized industrial. Deal team can override on the Assumptions tab.
DEFAULT_ASSUMPTIONS = {
    "analysis_start_date": date(2024, 1, 1),
    "market_rent_y1_psf": 10.00,
    "rent_growth_schedule": [0.04, 0.035, 0.035, 0.03, 0.03, 0.03, 0.03, 0.03, 0.03, 0.03],
    "ti_new_psf": 5.00,
    "ti_renewal_psf": 1.50,
    "lc_new_pct": 0.06,
    "lc_renewal_pct": 0.03,
    "free_rent_new_months": 3,
    "free_rent_renewal_months": 0,
    "downtime_new_months": 6,
    "mgmt_fee_pct": 0.03,
    "general_vacancy_pct": 0.02,
    "expense_growth_pct": 0.03,
    "capex_reserves_psf": 0.25,
    "exit_cap_rate": 0.065,
    "cam_psf": 0.40,
    "insurance_psf": 0.18,
    "real_estate_taxes_psf": 0.95,
}


# Cell addresses on Assumptions tab — matches Industrial_UW_Model_v3.xlsx
# Keep this in sync with the template!
ASSUMPTION_CELLS = {
    "property_name": "C6",
    "address": "C7",
    "city_state_zip": "C8",
    "submarket": "C9",
    "total_sf": "C10",
    "num_buildings": "C11",
    "year_built": "C12",
    "land_acres": "C13",
    "analysis_start": "C16",
    "market_rent_y1": "C38",
    # Rent growth row 42, columns D-M (years 1-10)
    "growth_start_col": 4,  # column D
    "growth_row": 42,
    # Leasing assumptions
    "ti_new": "C47",
    "ti_ren": "C48",
    "lc_new": "C49",
    "lc_ren": "C50",
    "free_rent_new": "C51",
    "free_rent_ren": "C52",
    "downtime_new": "C53",
    "downtime_ren": "C54",    # v3: renewal downtime
    "retention_ratio": "C55", # v3: retention ratio
    "new_lease_term": "C56",  # v3: new lease term years
    # Exit
    "exit_cap": "C34",
    # Operating expenses — all shifted down 1 row vs v2
    "cam": "C59",
    "insurance": "C60",
    "ret": "C61",
    "utilities": "C62",       # v3: now official (was C66 in v2)
    "mgmt_fee": "C63",
    "expense_growth": "C64",
    "gen_vac": "C65",
    "expense_recovery": "C66", # v3: NNN recovery % (default 1.0)
    "capex_reserves": "C67",
    # Deal economics
    "purchase_price": "C18",
    "ltv": "C24",
    "interest_rate": "C25",
    "amortization": "C26",
    "io_years": "C27",
    "loan_fee": "C28",
}

# Rent Roll tab cell addresses
RENT_ROLL_FIRST_DATA_ROW = 10
RENT_ROLL_MAX_TENANTS = 50
RENT_ROLL_COLS = {
    "suite": 2, "name": 3, "sf": 4, "lease_start": 6, "lease_end": 7,
    "rent_psf": 8, "esc": 10, "recovery": 11, "market": 12, "action": 13,
}


class PopulationResult:
    """Tracks what was populated vs. defaulted, for the report."""
    def __init__(self):
        self.extracted_fields: list[str] = []
        self.defaulted_fields: list[dict] = []  # {field, default_value, reason}
        self.tenant_count: int = 0
        self.warnings: list[str] = []

    def mark_extracted(self, field: str):
        self.extracted_fields.append(field)

    def mark_defaulted(self, field: str, value, reason: str = "not in OM"):
        self.defaulted_fields.append({
            "field": field,
            "default_value": value,
            "reason": reason,
        })

    def warn(self, msg: str):
        self.warnings.append(msg)


def populate_template(
    extraction: PropertyExtraction,
    template_path: Path,
    output_path: Path,
    deal_economics: dict = None,
) -> PopulationResult:
    """
    Copy template to output_path and write extracted values into it.
    deal_economics keys: purchase_price, ltv, interest_rate, amortization,
                         io_years, loan_fee
    Returns a PopulationResult summarizing what was extracted vs. defaulted.
    """
    result = PopulationResult()

    shutil.copy(template_path, output_path)
    wb = load_workbook(output_path)

    _populate_assumptions(wb, extraction, result)
    if deal_economics:
        _populate_deal_economics(wb, deal_economics)
    _populate_rent_roll(wb, extraction, result)

    wb.save(output_path)
    return result


def _populate_deal_economics(wb, deal: dict):
    """Write purchase price and debt terms to the Assumptions tab."""
    ws = wb["Assumptions"]
    field_to_cell = {
        "purchase_price": "purchase_price",
        "ltv":            "ltv",
        "interest_rate":  "interest_rate",
        "amortization":   "amortization",
        "io_years":       "io_years",
        "loan_fee":       "loan_fee",
    }
    for key, cell_key in field_to_cell.items():
        if deal.get(key) is not None:
            ws[ASSUMPTION_CELLS[cell_key]] = deal[key]


def _populate_assumptions(wb, extraction: PropertyExtraction, result: PopulationResult):
    ws = wb["Assumptions"]
    pi = extraction.property_info
    ba = extraction.broker_assumptions or BrokerAssumptions()

    # Property info
    _set_if(ws, ASSUMPTION_CELLS["property_name"], pi.name, "property_name", result)

    # Combine address + city/state/zip
    if pi.address:
        ws[ASSUMPTION_CELLS["address"]] = pi.address
        result.mark_extracted("address")
    city_state_zip = ", ".join(filter(None, [pi.city, f"{pi.state} {pi.zip_code}".strip() if pi.state or pi.zip_code else None]))
    if city_state_zip.strip(", "):
        ws[ASSUMPTION_CELLS["city_state_zip"]] = city_state_zip.strip(", ")
        result.mark_extracted("city_state_zip")

    _set_if(ws, ASSUMPTION_CELLS["submarket"], pi.submarket, "submarket", result)
    _set_if(ws, ASSUMPTION_CELLS["total_sf"], pi.total_sf, "total_sf", result)
    _set_if(ws, ASSUMPTION_CELLS["num_buildings"], pi.num_buildings, "num_buildings", result)
    _set_if(ws, ASSUMPTION_CELLS["year_built"], pi.year_built, "year_built", result)
    _set_if(ws, ASSUMPTION_CELLS["land_acres"], pi.land_acres, "land_acres", result)

    # Analysis start date
    _set_with_default(ws, ASSUMPTION_CELLS["analysis_start"],
                      ba.analysis_start_date, "analysis_start_date", result)

    # Market rent
    _set_with_default(ws, ASSUMPTION_CELLS["market_rent_y1"],
                      ba.market_rent_y1_psf, "market_rent_y1_psf", result)

    # Rent growth schedule (10 values in row 42, columns D-M)
    growth_schedule = ba.rent_growth_schedule or DEFAULT_ASSUMPTIONS["rent_growth_schedule"]
    if ba.rent_growth_schedule:
        result.mark_extracted("rent_growth_schedule")
    else:
        result.mark_defaulted("rent_growth_schedule", DEFAULT_ASSUMPTIONS["rent_growth_schedule"])
    for i, g in enumerate(growth_schedule):
        ws.cell(row=ASSUMPTION_CELLS["growth_row"], column=ASSUMPTION_CELLS["growth_start_col"] + i).value = g

    # Leasing assumptions
    _set_with_default(ws, ASSUMPTION_CELLS["ti_new"], ba.ti_new_psf, "ti_new_psf", result)
    _set_with_default(ws, ASSUMPTION_CELLS["ti_ren"], ba.ti_renewal_psf, "ti_renewal_psf", result)
    _set_with_default(ws, ASSUMPTION_CELLS["lc_new"], ba.lc_new_pct, "lc_new_pct", result)
    _set_with_default(ws, ASSUMPTION_CELLS["lc_ren"], ba.lc_renewal_pct, "lc_renewal_pct", result)
    _set_with_default(ws, ASSUMPTION_CELLS["free_rent_new"], ba.free_rent_new_months, "free_rent_new_months", result)
    _set_with_default(ws, ASSUMPTION_CELLS["free_rent_ren"], ba.free_rent_renewal_months, "free_rent_renewal_months", result)
    _set_with_default(ws, ASSUMPTION_CELLS["downtime_new"], ba.downtime_new_months, "downtime_new_months", result)
    # v3 new leasing cells — use sensible defaults if not in OM
    ws[ASSUMPTION_CELLS["downtime_ren"]] = 0      # renewal downtime almost always 0
    ws[ASSUMPTION_CELLS["retention_ratio"]] = 0.75
    ws[ASSUMPTION_CELLS["new_lease_term"]] = 5

    # Exit cap
    _set_with_default(ws, ASSUMPTION_CELLS["exit_cap"], ba.exit_cap_rate, "exit_cap_rate", result)

    # Operating expenses
    _set_with_default(ws, ASSUMPTION_CELLS["cam"], ba.cam_psf, "cam_psf", result)
    _set_with_default(ws, ASSUMPTION_CELLS["insurance"], ba.insurance_psf, "insurance_psf", result)
    _set_with_default(ws, ASSUMPTION_CELLS["ret"], ba.real_estate_taxes_psf, "real_estate_taxes_psf", result)
    # Utilities: write extracted value if present, else write 0 (not a default that needs flagging)
    ws[ASSUMPTION_CELLS["utilities"]] = ba.utilities_psf or 0.0
    if ba.utilities_psf:
        result.mark_extracted("utilities_psf")
    _set_with_default(ws, ASSUMPTION_CELLS["mgmt_fee"], ba.mgmt_fee_pct, "mgmt_fee_pct", result)
    _set_with_default(ws, ASSUMPTION_CELLS["expense_growth"], ba.expense_growth_pct, "expense_growth_pct", result)
    _set_with_default(ws, ASSUMPTION_CELLS["gen_vac"], ba.general_vacancy_pct, "general_vacancy_pct", result)
    ws[ASSUMPTION_CELLS["expense_recovery"]] = 1.0  # full NNN recovery — always true for this asset class
    _set_with_default(ws, ASSUMPTION_CELLS["capex_reserves"], ba.capex_reserves_psf, "capex_reserves_psf", result)


def _populate_rent_roll(wb, extraction: PropertyExtraction, result: PopulationResult):
    ws = wb["Rent Roll"]

    tenants = extraction.tenants
    if not tenants:
        result.warn("No tenants extracted from OM.")
        return

    if len(tenants) > RENT_ROLL_MAX_TENANTS:
        result.warn(
            f"Extracted {len(tenants)} tenants, but template only has {RENT_ROLL_MAX_TENANTS} rows. "
            f"Only the first {RENT_ROLL_MAX_TENANTS} will be populated."
        )
        tenants = tenants[:RENT_ROLL_MAX_TENANTS]

    market_rent_fallback = (
        extraction.broker_assumptions.market_rent_y1_psf
        if extraction.broker_assumptions and extraction.broker_assumptions.market_rent_y1_psf
        else DEFAULT_ASSUMPTIONS["market_rent_y1_psf"]
    )

    for idx, tenant in enumerate(tenants):
        row = RENT_ROLL_FIRST_DATA_ROW + idx

        # Clear prior example data in this row (keep formulas in helper columns)
        # We only write to the input columns defined in RENT_ROLL_COLS
        ws.cell(row=row, column=RENT_ROLL_COLS["suite"]).value = tenant.suite
        ws.cell(row=row, column=RENT_ROLL_COLS["name"]).value = tenant.name
        ws.cell(row=row, column=RENT_ROLL_COLS["sf"]).value = tenant.sf
        if tenant.lease_start:
            ws.cell(row=row, column=RENT_ROLL_COLS["lease_start"]).value = tenant.lease_start
        if tenant.lease_end:
            ws.cell(row=row, column=RENT_ROLL_COLS["lease_end"]).value = tenant.lease_end
        ws.cell(row=row, column=RENT_ROLL_COLS["rent_psf"]).value = tenant.current_rent_psf
        ws.cell(row=row, column=RENT_ROLL_COLS["esc"]).value = tenant.escalation_pct
        ws.cell(row=row, column=RENT_ROLL_COLS["recovery"]).value = tenant.recovery_type or "NNN"
        # Market rent: per-tenant if extracted, else fallback
        ws.cell(row=row, column=RENT_ROLL_COLS["market"]).value = (
            tenant.market_rent_psf if tenant.market_rent_psf is not None else market_rent_fallback
        )
        # Action: binary derived from renewal probability for now
        # (Model will be updated to consume probability directly in next iteration)
        action = "Renew" if tenant.renewal_probability >= 0.5 else "Vacate"
        ws.cell(row=row, column=RENT_ROLL_COLS["action"]).value = action

    # Clear any template example rows beyond the extracted tenants
    for idx in range(len(tenants), RENT_ROLL_MAX_TENANTS):
        row = RENT_ROLL_FIRST_DATA_ROW + idx
        for col_key in ["suite", "name", "sf", "lease_start", "lease_end",
                        "rent_psf", "esc", "recovery", "market", "action"]:
            ws.cell(row=row, column=RENT_ROLL_COLS[col_key]).value = None

    result.tenant_count = len(tenants)


def _set_if(ws, cell: str, value, field_name: str, result: PopulationResult):
    """Set cell if value is truthy; mark extracted in result."""
    if value is not None and value != "":
        ws[cell] = value
        result.mark_extracted(field_name)


def _set_with_default(ws, cell: str, value, field_name: str, result: PopulationResult):
    """Set cell if value is present; else use default and mark defaulted."""
    if value is not None:
        ws[cell] = value
        result.mark_extracted(field_name)
    else:
        default = DEFAULT_ASSUMPTIONS.get(field_name)
        if default is not None:
            ws[cell] = default
            result.mark_defaulted(field_name, default)
