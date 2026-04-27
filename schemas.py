"""
schemas.py — Pydantic models defining what we extract from an OM.

These classes do double duty: (1) they validate LLM output, and (2) they define
the JSON schema we send to the LLM as a contract. Any change to the model's
required fields should be reflected here.

Design notes:
- Every optional field represents something an OM might not contain. The
  extractor should populate what it can and return None for the rest. The
  pipeline will fill gaps with defaults and flag them in the report.
- Dates are ISO-8601 strings in the LLM interface (YYYY-MM-DD); we parse to
  datetime.date after validation.
- We don't try to extract purchase price — that's a deal-team input.
"""

from datetime import date
from typing import List, Optional, Literal
from pydantic import BaseModel, Field, field_validator


class PropertyInfo(BaseModel):
    """Physical/administrative facts about the asset."""
    name: str = Field(description="Property name as shown on cover of OM")
    address: Optional[str] = Field(default=None, description="Street address")
    city: Optional[str] = Field(default=None)
    state: Optional[str] = Field(default=None, description="Two-letter state code")
    zip_code: Optional[str] = Field(default=None, alias="zip")
    submarket: Optional[str] = Field(default=None, description="CRE submarket name, e.g. 'Interchange City / Southeast'")
    total_sf: int = Field(description="Total rentable square feet")
    num_buildings: Optional[int] = Field(default=None)
    year_built: Optional[int] = Field(default=None, description="Year built, or average year built for multi-building portfolios")
    land_acres: Optional[float] = Field(default=None)


class MarketData(BaseModel):
    """Submarket context — typically in the 'Market Overview' section."""
    submarket_vacancy: Optional[float] = Field(default=None, description="Submarket vacancy % as decimal, e.g. 0.043 for 4.3%")
    submarket_rent_psf: Optional[float] = Field(default=None, description="Submarket asking rent $/SF")
    rent_growth_yoy: Optional[float] = Field(default=None, description="YoY submarket rent growth as decimal")


class BrokerAssumptions(BaseModel):
    """Financial assumptions from the OM's 'Financial Assumptions' page."""
    analysis_start_date: Optional[date] = Field(default=None, description="Broker's modeled analysis start date")
    market_rent_y1_psf: Optional[float] = Field(default=None, description="Weighted-average market rent $/SF at analysis start (NNN)")
    rent_growth_schedule: Optional[List[float]] = Field(
        default=None,
        description="10-year market rent growth schedule as decimals. If broker only states a single rate, repeat it 10 times."
    )
    ti_new_psf: Optional[float] = Field(default=None, description="Tenant improvement allowance for new leases, $/SF")
    ti_renewal_psf: Optional[float] = Field(default=None)
    lc_new_pct: Optional[float] = Field(default=None, description="Leasing commission on new leases, as % of total lease value")
    lc_renewal_pct: Optional[float] = Field(default=None)
    free_rent_new_months: Optional[int] = Field(default=None)
    free_rent_renewal_months: Optional[int] = Field(default=None, description="Usually 0 for renewals")
    downtime_new_months: Optional[int] = Field(default=None, description="Months of vacancy between tenants")
    mgmt_fee_pct: Optional[float] = Field(default=None, description="Management fee as % of EGR")
    general_vacancy_pct: Optional[float] = Field(default=None)
    expense_growth_pct: Optional[float] = Field(default=None, description="Annual OpEx growth rate")
    capex_reserves_psf: Optional[float] = Field(default=None)
    exit_cap_rate: Optional[float] = Field(default=None, description="Broker-implied exit cap if stated")

    # Operating expenses (Year 1, $/SF)
    cam_psf: Optional[float] = Field(default=None, description="Common Area Maintenance, $/SF")
    insurance_psf: Optional[float] = Field(default=None)
    real_estate_taxes_psf: Optional[float] = Field(default=None)
    utilities_psf: Optional[float] = Field(default=None, description="Utilities expense, $/SF. Extract if stated in OM financials.")
    other_opex_psf: Optional[float] = Field(default=None, description="Any other operating expense not captured above, $/SF")

    @field_validator('rent_growth_schedule')
    @classmethod
    def validate_growth_length(cls, v):
        if v is not None and len(v) != 10:
            raise ValueError(f"rent_growth_schedule must have exactly 10 values, got {len(v)}")
        return v


class Tenant(BaseModel):
    """Single tenant row from the rent roll."""
    suite: str = Field(description="Suite number or identifier")
    name: str = Field(description="Tenant name, or 'VACANT' if the suite is unleased")
    sf: int = Field(description="Leased square feet")
    lease_start: Optional[date] = Field(default=None, description="Lease commencement date. If unknown, leave null.")
    lease_end: Optional[date] = Field(default=None, description="Lease expiration date. Null for vacant suites.")
    current_rent_psf: float = Field(description="Current annual base rent $/SF. Use 0 for vacant suites.")
    escalation_pct: float = Field(
        default=0.0,
        description="Annual escalation rate as decimal (e.g., 0.03 for 3%). If fixed step-ups are specified, compute the average annualized rate. 0 if not stated."
    )
    recovery_type: Optional[str] = Field(
        default="NNN",
        description="Lease type: NNN, Modified Gross, Gross, etc. Default NNN for industrial."
    )
    market_rent_psf: Optional[float] = Field(
        default=None,
        description="Broker's assumed market rent $/SF for this suite at rollover. May differ across buildings in a portfolio."
    )
    renewal_probability: float = Field(
        default=0.75,
        description="Probability tenant renews at expiration. OMs typically state 'Market - 75.00%' or 'Vacate'. Use 0.0 if broker explicitly marks as Vacate. Default 0.75 if not stated."
    )


class PropertyExtraction(BaseModel):
    """Top-level extraction result. Everything the pipeline needs from an OM."""
    property_info: PropertyInfo
    market_data: Optional[MarketData] = None
    broker_assumptions: Optional[BrokerAssumptions] = None
    tenants: List[Tenant] = Field(default_factory=list)

    # Metadata about the extraction itself
    extraction_notes: Optional[str] = Field(
        default=None,
        description="Any caveats, unusual features of the OM, or ambiguities you resolved. Free-form."
    )

    def completeness_report(self) -> dict:
        """Return a dict showing what fields were successfully populated."""
        report = {
            "property_info": {
                k: v is not None
                for k, v in self.property_info.model_dump().items()
            },
            "has_market_data": self.market_data is not None,
            "has_broker_assumptions": self.broker_assumptions is not None,
            "num_tenants": len(self.tenants),
            "tenants_with_dates": sum(1 for t in self.tenants if t.lease_start and t.lease_end),
            "tenants_with_market_rent": sum(1 for t in self.tenants if t.market_rent_psf is not None),
        }
        if self.broker_assumptions:
            report["broker_assumption_fields_populated"] = sum(
                1 for v in self.broker_assumptions.model_dump().values() if v is not None
            )
        return report
