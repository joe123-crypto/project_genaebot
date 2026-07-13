#!/usr/bin/env python3

from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import urllib.error

import job_scout
import jobscout.api
import jobscout.constants
import jobscout.delivery
import jobscout.dispatch_store
import jobscout.group_feed
import jobscout.subscriber

import jobscout.commands_group_feed
import jobscout.commands_setup
import jobscout.dispatch_runner

class ZwGatingTests(unittest.TestCase):
  def test_zw_targeted_by_country_code(self) -> None:
    self.assertTrue(job_scout.zw_targeted({"country": "zw"}))
    self.assertTrue(job_scout.zw_targeted({"country": "Zimbabwe"}))

  def test_zw_targeted_by_location(self) -> None:
    self.assertTrue(job_scout.zw_targeted({"country": "dz", "locations": ["Harare"]}))
    self.assertTrue(job_scout.zw_targeted({"locations": ["Bulawayo CBD"]}))

  def test_not_zw_targeted_for_algeria(self) -> None:
    self.assertFalse(job_scout.zw_targeted({"country": "dz", "locations": ["Algiers"]}))

  def test_fetch_scraped_candidates_skips_non_zw(self) -> None:
    with mock.patch("scrapers.fetch_scraped_candidates") as inner:
      out = job_scout.fetch_scraped_candidates({"country": "dz", "locations": ["Oran"]}, 5)
    self.assertEqual(out, [])
    inner.assert_not_called()

  def test_fetch_scraped_candidates_runs_for_zw(self) -> None:
    sentinel = [{"source": "vacancymail", "company": "A", "role": "Driver"}]
    with mock.patch("scrapers.fetch_scraped_candidates", return_value=sentinel) as inner:
      out = job_scout.fetch_scraped_candidates({"country": "zw", "targetRoles": ["Driver"]}, 5)
    self.assertEqual(out, sentinel)
    inner.assert_called_once()

  def test_fetch_scraped_candidates_swallows_errors(self) -> None:
    with mock.patch("scrapers.fetch_scraped_candidates", side_effect=RuntimeError("boom")):
      out = job_scout.fetch_scraped_candidates({"country": "zw", "targetRoles": ["Driver"]}, 5)
    self.assertEqual(out, [])


class ScraperBaseTests(unittest.TestCase):
  def test_build_candidate_matches_normalized_schema_keys(self) -> None:
    from scrapers import base

    normalized = job_scout.normalized_candidate(
      {"job_title": "Driver", "employer_name": "Acme", "job_apply_link": "https://x/apply"},
      "Driver",
    )
    candidate = base.build_candidate(
      "vacancymail", role="Driver", company="Acme", url="https://x/job",
      description="Drive", location="Harare", emails=["hr@acme.co.zw"],
    )
    self.assertEqual(set(candidate.keys()), set(normalized.keys()))
    self.assertEqual(candidate["source"], "vacancymail")
    self.assertEqual(candidate["applicationEmail"], "hr@acme.co.zw")
    self.assertIn("https://x/job", candidate["applyLinks"])

  def test_role_matcher_requires_all_significant_words(self) -> None:
    from scrapers import base

    match = base.build_role_matcher(["Delivery Driver", "Security Guard"])
    self.assertTrue(match("Delivery Driver", "vans in Harare"))
    self.assertTrue(match("Guard", "security guard for site"))
    self.assertFalse(match("Accountant", "manage ledgers"))

  def test_empty_roles_match_everything(self) -> None:
    from scrapers import base

    self.assertTrue(base.build_role_matcher([])("Anything", "text"))

  def test_waitstaff_family_accepts_hospitality_roles_and_rejects_false_positives(self) -> None:
    from scrapers import base

    match = base.build_candidate_matcher(["Waitress"])
    self.assertTrue(match("Waiter", "Serve diners"))
    self.assertTrue(match("Server", "Restaurant and hospitality role"))
    self.assertTrue(match("Food and Beverage Attendant", "Hotel"))
    self.assertFalse(match("Group Executive Chef", "Lead restaurant kitchens"))
    self.assertFalse(match("Server Administrator", "Maintain Linux servers"))
    self.assertTrue(base.build_candidate_matcher(["Any"])("Accountant", "Finance role"))

  def test_source_search_terms_expand_waitstaff_without_repeating_other_roles(self) -> None:
    from scrapers import base

    self.assertEqual(
      base.source_search_terms(["Waitress", "Waiter", "Delivery Driver", "delivery driver"]),
      ["waitress", "waiter", "Delivery Driver"],
    )
    self.assertEqual(base.source_search_terms(["Any"]), [""])

  def test_pagination_max_reads_query_and_path_links(self) -> None:
    from scrapers import base

    html = '<a href="?page=4">4</a><a href="/2026/07/page/9/">9</a>'
    self.assertEqual(base.pagination_max(html), 9)

  def test_month_lookback_starts_at_oldest_included_month(self) -> None:
    from scrapers import base

    self.assertEqual(base.month_lookback_start(3, date(2026, 1, 15)), date(2025, 11, 1))


class BoardParserTests(unittest.TestCase):
  VM_LIST = (
    '<a href="/jobs/driver-101/" class="job-listing">'
    '<div class="job-listing-description">'
    '<h3 class="job-listing-title">Delivery Driver</h3>'
    '<h4 class="job-listing-company">Acme Logistics</h4>'
    '<p class="job-listing-text">Driver needed for deliveries in Harare.</p>'
    '</div>'
    '<div class="job-listing-footer"><ul>'
    '<li><i class="icon-material-outline-location-on"></i> Harare</li>'
    '<li><i class="icon-material-outline-access-time"></i> Expires 30 Jun 2026</li>'
    '</ul></div></a>'
  )
  VM_DETAIL = (
    '<div class="col-xl-8 col-lg-8 content-right-offset">'
    '<div class="single-page-section"><h3>Job Description</h3><p>Drive vans across Harare.</p></div>'
    '<div class="single-page-section"><h3>How to Apply</h3><p>Email: jobs@acme.co.zw before 30 June.</p></div>'
    '</div>'
    '<div class="col-xl-4"><a href="mailto:footer@vacancymail.co.zw">noise</a></div>'
  )
  IH_LIST = (
    '<div class="company-list-logo"><a href="/job/nurse-200/"><img src="x" alt="City Hospital"></a></div>'
    '<div class="company-list-details">'
    '<h3><a href="/job/nurse-200/">Registered Nurse</a></h3>'
    '<p class="company-state"><i class="fa fa-map-marker"></i> Bulawayo</p>'
    '<p class="open-icon"><i class="fa fa-clock-o"></i> Expires 25 Jun 2026</p>'
    '<p class="company-state"><i class="fa fa-building"></i> City Hospital</p>'
    '</div>'
  )
  IH_DETAIL = (
    '<div class="single-candidate-bottom-left"><h3>job Description</h3>'
    '<p>Care for patients. Apply via hr@cityhospital.co.zw</p>'
    '<a href="https://apply.cityhospital.co.zw/n">Apply Now</a></div>'
    '<div class="single-candidate-bottom-right">related jobs</div>'
  )
  VB_LIST = (
    '<li class="post-9 job_listing type-job_listing"><a href="https://vacancybox.co.zw/job/cashier-9/">'
    '<div class="position"><h3>Cashier</h3><div class="company"><strong>Acme Retail</strong></div></div>'
    '<div class="location">Mutare</div><ul class="meta"><li class="date">Today</li></ul></a></li>'
    '<li class="post-10 job_listing type-job_listing"><a href="https://vacancybox.co.zw/job/welder-1/">'
    '<div class="position"><h3>Welder</h3><div class="company"><strong>Other</strong></div></div>'
    '<div class="location">Harare</div></a></li>'
  )
  VB_DETAIL = '<script type="application/ld+json">' + json.dumps({
    "@context": "https://schema.org", "@type": "JobPosting", "title": "Cashier",
    "hiringOrganization": {"@type": "Organization", "name": "VacancyBox"},
    "jobLocation": {"@type": "Place", "address": {"addressCountry": "ZW"}},
  }) + '</script><script type="application/ld+json">' + json.dumps({
    "@context": "https://schema.org",
    "@type": "JobPosting",
    "title": "Cashier",
    "description": "<p>DUE: 25 JUN 2026 Cashier at Acme. Email hr@acme.co.zw</p>",
    "hiringOrganization": {"@type": "Organization", "name": "Acme Retail"},
    "jobLocation": {"@type": "Place", "address": "Mutare"},
  }) + '</script>'
  AN_LIST = (
    '<div class="p-wrap p-list p-list-2" data-pid="101">'
    '<a class="p-flink" href="https://applynow.co.zw/2026/07/04/city-of-kadoma/">'
    '<img width="300" class="featured-img wp-post-image" alt="City of Kadoma"></a>'
    '<h2 class="entry-title"> <a class="p-url" href="https://applynow.co.zw/2026/07/04/city-of-kadoma/" rel="bookmark"> City of Kadoma is hiring: Accountant | Apply by 22 July 2026 </a></h2>'
    '<time class="updated" datetime="2026-07-04T08:09:50+02:00">a day ago</time></div>'
    '<div class="p-wrap p-list p-list-2" data-pid="102">'
    '<a class="p-flink" href="https://applynow.co.zw/2026/07/03/blue-carbon/">'
    '<img width="300" class="featured-img wp-post-image" alt="Blue Carbon"></a>'
    '<h2 class="entry-title"> <a class="p-url" href="https://applynow.co.zw/2026/07/03/blue-carbon/" rel="bookmark"> Free Blue Carbon Finance Course 2026 </a></h2>'
    '<time class="updated" datetime="2026-07-03T17:51:18+02:00">2 days ago</time></div>'
  )
  AN_DETAIL = (
    '<div class="entry-content rbct clearfix"><h2>Accountant</h2>'
    '<p>Prepare municipal accounts. Closing Date: 22 July 2026.</p>'
    '<p>Send your CV to hr@kadoma.co.zw or apply at '
    '<a href="https://apply.kadoma.co.zw/n">the portal</a>.</p>'
    '<a href="https://facebook.com/applynow">Follow us</a></div>'
    '<div class="single-related"><h2 class="entry-title"><a href="/other/">Other Job</a></h2>'
    '<p>noise@other.co.zw</p></div>'
  )
  AN_POST = {
    "id": 101,
    "date": "2026-07-04T08:09:50",
    "link": "https://applynow.co.zw/2026/07/04/city-of-kadoma/",
    "title": {"rendered": "City of Kadoma is hiring: Accountant | Apply by 22 July 2026"},
    "content": {"rendered": AN_DETAIL},
  }

  def test_vacancymail_parses_card_and_detail(self) -> None:
    from scrapers import vacancymail as vm

    cards = vm.parse_cards(self.VM_LIST)
    self.assertEqual(len(cards), 1)
    self.assertEqual(cards[0]["role"], "Delivery Driver")
    self.assertEqual(cards[0]["company"], "Acme Logistics")
    self.assertEqual(cards[0]["location"], "Harare")
    self.assertEqual(cards[0]["closing"], "30 Jun 2026")
    self.assertEqual(cards[0]["url"], "https://vacancymail.co.zw/jobs/driver-101/")
    desc, emails = vm.parse_detail(self.VM_DETAIL)
    self.assertIn("Drive vans", desc)
    self.assertEqual(emails, ["jobs@acme.co.zw"])  # footer e-mail excluded

  def test_vacancymail_fetch_with_match(self) -> None:
    from scrapers import vacancymail as vm

    calls: list[str] = []
    def fake_get(url, timeout=15, attempts=2):
      calls.append(url)
      return self.VM_LIST if url.startswith(f"{vm.LIST_URL}?") else self.VM_DETAIL

    with mock.patch("scrapers.base.http_get", side_effect=fake_get):
      out = vm.fetch(
        limit=5,
        match=lambda role, text: "driver" in f"{role} {text}".lower(),
        roles=["Driver"],
      )
    self.assertEqual(len(out), 1)
    self.assertEqual(out[0]["source"], "vacancymail")
    self.assertEqual(out[0]["applicationEmail"], "jobs@acme.co.zw")
    self.assertIn("search=Driver", calls[0])

  def test_vacancymail_fetch_follows_search_pagination(self) -> None:
    from scrapers import vacancymail as vm

    listing_calls: list[str] = []
    page_one = self.VM_LIST + '<a href="?page=2">2</a>'
    def fake_get(url, timeout=15, attempts=2):
      if url.startswith(f"{vm.LIST_URL}?"):
        listing_calls.append(url)
        return "" if "page=2" in url else page_one
      return self.VM_DETAIL

    with mock.patch("scrapers.base.http_get", side_effect=fake_get):
      vm.fetch(limit=5, match=lambda role, text: True, roles=["Driver"])
    self.assertEqual(len(listing_calls), 2)
    self.assertIn("page=2", listing_calls[1])

  def test_vacancymail_keeps_listing_when_detail_times_out(self) -> None:
    from scrapers import vacancymail as vm

    def fake_get(url, timeout=15, attempts=2):
      return self.VM_LIST if url.startswith(f"{vm.LIST_URL}?") else None

    with mock.patch("scrapers.base.http_get", side_effect=fake_get):
      out = vm.fetch(limit=5, match=lambda role, text: True, roles=["Driver"])
    self.assertEqual(len(out), 1)
    self.assertIn("Driver needed", out[0]["description"])

  def test_iharare_parses_card_and_detail(self) -> None:
    from scrapers import iharare as ih

    cards = ih.parse_cards(self.IH_LIST)
    self.assertEqual(len(cards), 1)
    self.assertEqual(cards[0]["role"], "Registered Nurse")
    self.assertEqual(cards[0]["company"], "City Hospital")
    self.assertEqual(cards[0]["location"], "Bulawayo")
    desc, emails, links = ih.parse_detail(self.IH_DETAIL)
    self.assertIn("Care for patients", desc)
    self.assertEqual(emails, ["hr@cityhospital.co.zw"])
    self.assertEqual(links, ["https://apply.cityhospital.co.zw/n"])

  def test_iharare_fetch_uses_server_search(self) -> None:
    from scrapers import iharare as ih

    calls: list[str] = []
    def fake_get(url, timeout=15, attempts=2):
      calls.append(url)
      return self.IH_LIST if url.startswith(f"{ih.LIST_URL}?") else self.IH_DETAIL

    with mock.patch("scrapers.base.http_get", side_effect=fake_get):
      out = ih.fetch(
        limit=5,
        match=lambda role, text: "nurse" in f"{role} {text}".lower(),
        roles=["Nurse"],
      )
    self.assertEqual([candidate["role"] for candidate in out], ["Registered Nurse"])
    self.assertIn("search=Nurse", calls[0])

  def test_iharare_fetch_follows_search_pagination(self) -> None:
    from scrapers import iharare as ih

    listing_calls: list[str] = []
    page_one = self.IH_LIST + '<a href="?page=2">2</a>'
    def fake_get(url, timeout=15, attempts=2):
      if url.startswith(f"{ih.LIST_URL}?"):
        listing_calls.append(url)
        return "" if "page=2" in url else page_one
      return self.IH_DETAIL

    with mock.patch("scrapers.base.http_get", side_effect=fake_get):
      ih.fetch(limit=5, match=lambda role, text: True, roles=["Nurse"])
    self.assertEqual(len(listing_calls), 2)
    self.assertIn("page=2", listing_calls[1])

  def test_vacancybox_card_and_detail(self) -> None:
    from scrapers import vacancybox as vb

    cards = vb.parse_cards(self.VB_LIST)
    self.assertEqual([card["role"] for card in cards], ["Cashier", "Welder"])
    with mock.patch("scrapers.base.http_get", return_value=self.VB_DETAIL):
      candidate = vb.candidate_from_card(cards[0], timeout=5, fetch_detail=True)
    self.assertEqual(candidate["role"], "Cashier")
    self.assertEqual(candidate["applicationEmail"], "hr@acme.co.zw")
    self.assertEqual(candidate["closing"], "25 JUN 2026")
    detail = vb.parse_detail(self.VB_DETAIL)
    self.assertEqual(detail["company"], "Acme Retail")
    self.assertEqual(detail["location"], "Mutare")

  def test_vacancybox_fetch_filters_by_role(self) -> None:
    from scrapers import vacancybox as vb

    with mock.patch("scrapers.base.http_post_form_json", return_value={"html": self.VB_LIST, "max_num_pages": 1}) as listing, \
         mock.patch("scrapers.base.http_get", return_value=self.VB_DETAIL):
      out = vb.fetch(
        limit=5,
        match=lambda role, text: "cashier" in f"{role} {text}".lower(),
        roles=["Cashier"],
      )
    self.assertEqual([c["role"] for c in out], ["Cashier"])
    self.assertEqual(out[0]["company"], "Acme Retail")
    self.assertEqual(listing.call_args.args[1]["search_keywords"], "Cashier")

  def test_vacancybox_listing_failure_is_reported(self) -> None:
    from scrapers import vacancybox as vb

    with mock.patch("scrapers.base.http_post_form_json", return_value=None):
      with self.assertRaisesRegex(RuntimeError, "listing page 1 request failed"):
        vb.fetch(limit=5)

  def test_vacancybox_fetch_follows_reported_pagination(self) -> None:
    from scrapers import vacancybox as vb

    responses = [
      {"html": self.VB_LIST, "max_num_pages": 2},
      {"html": "", "max_num_pages": 2},
    ]
    with mock.patch("scrapers.base.http_post_form_json", side_effect=responses) as listing, \
         mock.patch("scrapers.base.http_get", return_value=self.VB_DETAIL):
      vb.fetch(limit=5, match=lambda role, text: True, roles=["Cashier"])
    self.assertEqual(listing.call_count, 2)
    self.assertEqual(listing.call_args_list[1].args[1]["page"], 2)

  def test_vacancybox_stops_before_detail_fetch_for_old_results(self) -> None:
    from scrapers import vacancybox as vb

    old_list = self.VB_LIST.replace(
      '<li class="date">Today</li>',
      '<li class="date"><time datetime="2026-04-22">Posted on April 22, 2026</time></li>',
      1,
    )
    with mock.patch("scrapers.base.http_post_form_json", return_value={"html": old_list, "max_num_pages": 5}), \
         mock.patch("scrapers.base.month_lookback_start", return_value=date(2026, 5, 1)), \
         mock.patch("scrapers.base.http_get") as detail:
      out = vb.fetch(limit=5, match=lambda role, text: True, roles=["Cashier"])
    self.assertEqual(out, [])
    detail.assert_not_called()

  def test_applynow_parses_card_and_detail(self) -> None:
    from scrapers import applynow as an

    cards = an.parse_cards(self.AN_LIST)
    self.assertEqual(len(cards), 2)
    self.assertEqual(cards[0]["role"], "City of Kadoma is hiring: Accountant | Apply by 22 July 2026")
    self.assertEqual(cards[0]["company"], "City of Kadoma")
    self.assertEqual(cards[0]["closing"], "22 July 2026")
    self.assertEqual(cards[0]["url"], "https://applynow.co.zw/2026/07/04/city-of-kadoma/")
    desc, emails, links = an.parse_detail(self.AN_DETAIL)
    self.assertIn("Prepare municipal accounts", desc)
    self.assertEqual(emails, ["hr@kadoma.co.zw"])  # related-post e-mail excluded
    self.assertEqual(links, ["https://apply.kadoma.co.zw/n"])  # social link excluded

  def test_applynow_fetch_filters_by_role(self) -> None:
    from scrapers import applynow as an

    calls: list[str] = []
    def fake_get_json(url, timeout=15, attempts=2):
      calls.append(url)
      return [self.AN_POST]

    with mock.patch("scrapers.base.http_get_json", side_effect=fake_get_json):
      out = an.fetch(
        limit=5,
        match=lambda role, text: "accountant" in f"{role} {text}".lower(),
        roles=["Accountant"],
      )
    self.assertEqual(len(out), 1)
    self.assertEqual(out[0]["source"], "applynow")
    self.assertEqual(out[0]["company"], "City of Kadoma")
    self.assertEqual(out[0]["applicationEmail"], "hr@kadoma.co.zw")
    self.assertEqual(out[0]["closing"], "22 July 2026")
    self.assertIn("search=Accountant", calls[0])
    self.assertIn("after=", calls[0])

  def test_applynow_search_paginates_until_an_empty_page(self) -> None:
    from scrapers import applynow as an

    with mock.patch.object(an, "POSTS_PER_PAGE", 1), \
         mock.patch("scrapers.base.http_get_json", side_effect=[[self.AN_POST], []]) as request:
      out = an.fetch(limit=5, match=lambda role, text: True, roles=["Accountant"])
    self.assertEqual(len(out), 1)
    self.assertEqual(request.call_count, 2)
    self.assertIn("page=2", request.call_args_list[1].args[0])

  def test_applynow_search_stops_when_page_past_end_returns_none(self) -> None:
    # A full last page (exactly POSTS_PER_PAGE) makes fetch request one page past
    # the end, which WordPress answers with HTTP 400 -> None. Having already read a
    # page, fetch should end the term rather than raise and drop what it collected.
    from scrapers import applynow as an

    with mock.patch.object(an, "POSTS_PER_PAGE", 1), \
         mock.patch("scrapers.base.http_get_json", side_effect=[[self.AN_POST], None]) as request:
      out = an.fetch(limit=5, match=lambda role, text: True, roles=["Accountant"])
    self.assertEqual(len(out), 1)
    self.assertEqual(request.call_count, 2)

  def test_applynow_listing_failure_is_reported(self) -> None:
    from scrapers import applynow as an

    with mock.patch("scrapers.base.http_get_json", return_value=None):
      with self.assertRaisesRegex(RuntimeError, "search page 1 request failed"):
        an.fetch(limit=5)


class ScraperIntegrationTests(unittest.TestCase):
  def test_matches_roles_uses_role_and_description(self) -> None:
    import scrapers

    self.assertTrue(scrapers.matches_roles(
      {"role": "Cleaner", "description": "office cleaning"}, {"targetRoles": ["Cleaner"]}))
    self.assertFalse(scrapers.matches_roles(
      {"role": "Welder", "description": "metal work"}, {"targetRoles": ["Cleaner"]}))

  def test_parallel_registry_passes_roles_and_isolates_source_failure(self) -> None:
    import scrapers

    healthy = mock.Mock()
    healthy.fetch.return_value = [{"source": "healthy", "role": "Waiter"}]
    broken = mock.Mock()
    broken.fetch.side_effect = RuntimeError("temporary outage")
    with mock.patch.dict(scrapers.SCRAPERS, {"healthy": healthy, "broken": broken}, clear=True):
      candidates, results = scrapers.fetch_scraped_candidates_with_results(
        {"targetRoles": ["Waitress"]},
        5,
      )
    self.assertEqual(candidates, [{"source": "healthy", "role": "Waiter"}])
    self.assertEqual(results["healthy"]["status"], "ok")
    self.assertEqual(results["broken"]["status"], "unavailable")
    self.assertEqual(healthy.fetch.call_args.kwargs["roles"], ["Waitress"])

  def test_merge_dedups_jsearch_and_board_duplicate(self) -> None:
    from scrapers import base

    jsearch = job_scout.normalized_candidate(
      {"job_title": "Driver", "employer_name": "Acme", "job_apply_link": "https://acme/apply"}, "Driver")
    board = base.build_candidate("vacancymail", role="Driver", company="Acme",
                                 url="https://vacancymail.co.zw/jobs/driver-1/", description="drive")
    unique = base.build_candidate("iharare", role="Welder", company="Other",
                                  url="https://ihararejobs.com/job/welder-1/", description="weld")
    merged = job_scout.filter_candidates([jsearch, board, unique], {}, [], 10)
    keys = {job_scout.application_key(c) for c in merged}
    # Acme/Driver collapses to one entry; Other/Welder stays.
    self.assertEqual(len(merged), 2)
    self.assertIn(("acme", "driver"), keys)

  def test_record_application_propagates_board_source(self) -> None:
    candidate = {"company": "Acme", "role": "Driver", "applicationEmail": "hr@acme.co.zw",
                 "sourceUrl": "https://vacancymail.co.zw/jobs/driver-1/", "source": "vacancymail"}
    with mock.patch("jobscout.api.request_json", return_value={"ok": True}) as req:
      job_scout.record_candidate_application("user-1", candidate, "applied")
    payload = req.call_args.args[2]
    self.assertEqual(payload["source"], "vacancymail")

if __name__ == "__main__":
  unittest.main()
