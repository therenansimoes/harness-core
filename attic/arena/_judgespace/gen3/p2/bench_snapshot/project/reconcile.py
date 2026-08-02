"""Lead reconciliation for a B2B CRM data pipeline.

Multiple lead-gen sources feed raw contact records into this pipeline.
The same company often shows up multiple times (different people at the
same company, different capitalization, different source feeds). This
module merges those records per company and rolls up deal revenue so
sales ops has one row per company.
"""
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Lead:
    email: str
    contact_name: str
    revenue: float
    updated_at: str  # ISO date string, e.g. "2026-01-15"
    source: str


def normalize_domain(email: str) -> str:
    """Extract the company domain from an email address."""
    if "@" not in email:
        raise ValueError(f"invalid email: {email}")
    return email.split("@", 1)[1].strip()


def merge_leads(leads: list[Lead]) -> dict:
    """Group leads by company domain and roll up revenue.

    For each company domain, sums revenue across all leads and keeps the
    contact_name from whichever lead has the most recent updated_at.
    """
    companies: dict[str, dict] = {}

    for lead in leads:
        domain = normalize_domain(lead.email)

        if domain not in companies:
            companies[domain] = {
                "domain": domain,
                "revenue": 0.0,
                "contact_name": lead.contact_name,
                "updated_at": lead.updated_at,
                "sources": set(),
            }

        entry = companies[domain]
        entry["revenue"] += lead.revenue
        entry["sources"].add(lead.source)

        if datetime.fromisoformat(lead.updated_at) > datetime.fromisoformat(entry["updated_at"]):
            entry["contact_name"] = lead.contact_name
            entry["updated_at"] = lead.updated_at

    return companies


def top_companies_by_revenue(leads: list[Lead], limit: int = 10) -> list[dict]:
    merged = merge_leads(leads)
    ranked = sorted(merged.values(), key=lambda c: c["revenue"], reverse=True)
    return ranked[:limit]
