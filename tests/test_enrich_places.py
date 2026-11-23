"""Tests for the Google Places step of the enrichment pipeline (Google itself is faked)."""

import requests

from src import enrich_transactions as enrich


def _in_person(*descriptions):
    return {d: {"purchase_mode": "in_person", "merchant_name": d} for d in descriptions}


def _http_error(status_code):
    response = requests.Response()
    response.status_code = status_code
    return requests.HTTPError(f"{status_code} Client Error", response=response)


def _fake_places(monkeypatch, outcomes):
    """Each lookup returns (or raises) the next outcome; returns the merchants asked for."""
    asked = []
    outcomes = iter(outcomes)

    def search(merchant, city):
        asked.append(merchant)
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(enrich, "_search_place", search)
    monkeypatch.setattr(enrich, "_save_places_cache", lambda cache: None)
    monkeypatch.setattr(enrich.time, "sleep", lambda _seconds: None)
    return asked


def test_lookups_stop_at_the_quota_and_failures_are_not_cached(monkeypatch):
    asked = _fake_places(
        monkeypatch, [{"displayName": {"text": "Cafe A"}}, _http_error(429)]
    )

    cache = enrich.enrich_with_places(_in_person("A", "B", "C"), "Montreal, QC")

    # B hit the quota: it and C stay uncached so a later run looks them up again
    assert asked == ["A", "B"]
    assert list(cache) == ["A"]
    assert cache["A"]["place_name"] == "Cafe A"


def test_other_errors_skip_one_merchant_and_carry_on(monkeypatch):
    asked = _fake_places(
        monkeypatch, [_http_error(500), None, {"displayName": {"text": "Cafe C"}}]
    )

    cache = enrich.enrich_with_places(_in_person("A", "B", "C"), "Montreal, QC")

    assert asked == ["A", "B", "C"]
    # A failed (retried later), B genuinely has no match (remembered), C was found
    assert "A" not in cache
    assert cache["B"] == enrich._EMPTY_PLACE
    assert cache["C"]["place_name"] == "Cafe C"
