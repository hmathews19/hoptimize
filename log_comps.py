"""
log_comps.py — Append key OM metrics to a running comps Excel file.

Each OM processed adds one row. Over time this becomes a lightweight
proprietary database of Nashville industrial deals.
"""

from datetime import date
from pathlib import Path

import pandas as pd


COMPS_COLUMNS = [
    "date_logged",
    "source_file",
    "property_name",
    "submarket",
    "city",
    "state",
    "total_sf",
    "num_buildings",
    "year_built",
    "land_acres",
    "num_tenants",
    "occupied_sf",
    "occupancy_pct",
    "in_place_rent_psf",
    "cam_psf",
    "insurance_psf",
    "re_taxes_psf",
    "utilities_psf",
    "mgmt_fee_pct",
    "total_opex_psf",
    "analysis_start",
    "walt_years",
]


def log_om_to_comps(
    extraction,
    pdf_name: str,
    comps_path: str | Path = "om_comps.xlsx",
) -> None:
    """Append one row from this extraction to the comps workbook."""
    comps_path = Path(comps_path)
    pi = extraction.property_info
    ba = extraction.broker_assumptions

    occupied_sf = sum(
        t.sf for t in extraction.tenants
        if not t.name.upper().startswith("VACANT")
    )
    total_sf_rr = sum(t.sf for t in extraction.tenants) or pi.total_sf
    occupancy = occupied_sf / pi.total_sf if pi.total_sf else None

    # Weighted-average in-place rent (occupied suites only)
    occupied_tenants = [t for t in extraction.tenants if not t.name.upper().startswith("VACANT")]
    if occupied_tenants:
        total_occ_sf = sum(t.sf for t in occupied_tenants)
        in_place_rent = sum(t.current_rent_psf * t.sf for t in occupied_tenants) / total_occ_sf
    else:
        in_place_rent = None

    # WALT (weighted-avg lease term remaining from analysis start)
    walt = None
    if ba and ba.analysis_start_date and occupied_tenants:
        start = ba.analysis_start_date
        weighted = sum(
            max((t.lease_end - start).days / 365.25, 0) * t.sf
            for t in occupied_tenants if t.lease_end
        )
        denom = sum(t.sf for t in occupied_tenants if t.lease_end)
        walt = round(weighted / denom, 2) if denom else None

    cam = ba.cam_psf if ba else None
    ins = ba.insurance_psf if ba else None
    ret = ba.real_estate_taxes_psf if ba else None
    util = ba.utilities_psf if ba else None
    mgmt = ba.mgmt_fee_pct if ba else None
    total_opex = sum(x for x in [cam, ins, ret, util] if x is not None) or None

    new_row = {
        "date_logged": date.today().isoformat(),
        "source_file": pdf_name,
        "property_name": pi.name,
        "submarket": pi.submarket,
        "city": pi.city,
        "state": pi.state,
        "total_sf": pi.total_sf,
        "num_buildings": pi.num_buildings,
        "year_built": pi.year_built,
        "land_acres": pi.land_acres,
        "num_tenants": len(extraction.tenants),
        "occupied_sf": occupied_sf,
        "occupancy_pct": round(occupancy * 100, 1) if occupancy is not None else None,
        "in_place_rent_psf": round(in_place_rent, 2) if in_place_rent else None,
        "cam_psf": cam,
        "insurance_psf": ins,
        "re_taxes_psf": ret,
        "utilities_psf": util,
        "mgmt_fee_pct": mgmt,
        "total_opex_psf": round(total_opex, 2) if total_opex else None,
        "analysis_start": ba.analysis_start_date.isoformat() if ba and ba.analysis_start_date else None,
        "walt_years": walt,
    }

    if comps_path.exists():
        existing = pd.read_excel(comps_path)
        # Avoid duplicate: same property_name + source_file
        mask = (existing["source_file"] == pdf_name) & (existing["property_name"] == pi.name)
        if mask.any():
            existing = existing[~mask]
        df = pd.concat([existing, pd.DataFrame([new_row])], ignore_index=True)
    else:
        df = pd.DataFrame([new_row], columns=COMPS_COLUMNS)

    df.to_excel(comps_path, index=False)
