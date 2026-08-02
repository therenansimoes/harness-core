from reconcile import Lead, merge_leads, top_companies_by_revenue


def test_merge_sums_revenue_for_same_company():
    leads = [
        Lead("alice@acme.com", "Alice", 1000.0, "2026-01-01", "webform"),
        Lead("bob@acme.com", "Bob", 2500.0, "2026-01-05", "trade_show"),
    ]
    merged = merge_leads(leads)
    assert len(merged) == 1
    assert merged["acme.com"]["revenue"] == 3500.0


def test_merge_is_case_insensitive_on_domain():
    # Same company, but the lead-gen sources disagree on capitalization
    # (webform lowercases, trade show badge scanner titlecases).
    leads = [
        Lead("alice@Acme.com", "Alice", 1000.0, "2026-01-01", "webform"),
        Lead("bob@acme.com", "Bob", 2500.0, "2026-01-05", "trade_show"),
    ]
    merged = merge_leads(leads)
    assert len(merged) == 1, f"expected 1 merged company, got {len(merged)}: {list(merged.keys())}"
    total_revenue = sum(c["revenue"] for c in merged.values())
    assert total_revenue == 3500.0


def test_merge_keeps_most_recent_contact_name():
    leads = [
        Lead("a@corp.io", "Old Contact", 100.0, "2025-06-01", "webform"),
        Lead("b@corp.io", "New Contact", 200.0, "2026-02-01", "referral"),
    ]
    merged = merge_leads(leads)
    assert merged["corp.io"]["contact_name"] == "New Contact"


def test_top_companies_by_revenue_ranks_correctly():
    leads = [
        Lead("a@small.io", "A", 100.0, "2026-01-01", "webform"),
        Lead("b@big.io", "B", 9000.0, "2026-01-01", "webform"),
        Lead("c@BIG.io", "C", 1000.0, "2026-01-02", "trade_show"),
    ]
    top = top_companies_by_revenue(leads, limit=1)
    assert top[0]["domain"] == "big.io"
    assert top[0]["revenue"] == 10000.0
