import argparse
import importlib.util
import json
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("dispatch.py")
SPEC = importlib.util.spec_from_file_location("event_scout_dispatch", MODULE_PATH)
dispatch = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(dispatch)

# Loading dispatch inserts its directory on sys.path, so the taxonomy helpers it
# extracted are importable here too. Internal taxonomy calls resolve names in this
# module, so tests that fake those internals must patch `taxonomy`, not `dispatch`.
import taxonomy  # noqa: E402


EVENT = {
    "source_id": "algeria-exhibitions:10",
    "summary": "Startup Fair",
    "location": "SAFEX, Algiers",
    "source_url": "https://example.test/event",
    "category": "business",
    "start_date": "2026-06-27",
    "end_date": "2026-06-28",
    "range": "27 to 28 Jun 2026",
}


def subscriber(**event_overrides):
    events = {
        "enabled": True,
        "user_id": "uid-123",
        "categories": ["business"],
        "location": "Algiers",
        "reminder_minutes": [1440, 120],
        "delivery_mode": "approval",
        "synced_event_ids": [],
        "offered_event_ids": [],
        "pending_offers": [],
    }
    events.update(event_overrides)
    return {"phone": "+213555000111", "phone_hash": "hash123", "events": events}


class PatchDispatch:
    def __init__(self, _target=None, **patches):
        self.target = _target if _target is not None else dispatch
        self.patches = patches
        self.originals = {}

    def __enter__(self):
        for name, value in self.patches.items():
            self.originals[name] = getattr(self.target, name)
            setattr(self.target, name, value)

    def __exit__(self, exc_type, exc, tb):
        for name, value in self.originals.items():
            setattr(self.target, name, value)


class EventScoutDispatchTest(unittest.TestCase):
    def test_catalog_options_include_counts(self):
        options = dispatch.catalog_options_from_listing(
            {
                1: {"category": "business", "location": "SAFEX, Algiers"},
                2: {"category": "business", "location": "SAFEX, Algiers"},
                3: {"category": "health", "location": "Oran"},
            }
        )

        self.assertEqual(options["categories"][0], {"name": "business", "count": 2})
        self.assertIn({"name": "SAFEX, Algiers", "count": 2}, options["locations"])

    def test_catalog_options_from_taxonomy_uses_canonical_matching_names(self):
        def fake_taxonomy_http_get(url):
            if "/eventcat?" in url:
                return (
                    json.dumps(
                        [
                            {"id": 1, "name": "Agri-food", "slug": "agri-food", "count": 6},
                            {"id": 2, "name": "Art", "slug": "art-en", "count": 2},
                        ]
                    ),
                    None,
                    "",
                )
            if "/location?" in url:
                return (
                    json.dumps([{"id": 3, "name": "SAFEX", "slug": "safex", "count": 83}]),
                    None,
                    "",
                )
            raise AssertionError(f"unexpected URL: {url}")

        with PatchDispatch(taxonomy, taxonomy_http_get=fake_taxonomy_http_get):
            options = dispatch.catalog_options_from_taxonomy()

        self.assertEqual(options["source"], "taxonomy")
        self.assertEqual(options["categories"], [{"name": "agri food", "count": 6}, {"name": "art en", "count": 2}])
        self.assertEqual(options["locations"], [{"name": "SAFEX, Algiers", "count": 83}])

    def test_taxonomy_fetch_retries_smaller_page_sizes(self):
        calls = []

        def fake_taxonomy_http_get(url):
            calls.append(url)
            if "per_page=50" in url:
                return (None, None, "HTTP 502: Bad Gateway")
            if "per_page=10" in url:
                return (
                    json.dumps([{"id": 1, "name": "Technology", "slug": "technology", "count": 3}]),
                    {"X-WP-TotalPages": "1"},
                    "",
                )
            raise AssertionError(f"unexpected URL: {url}")

        with PatchDispatch(taxonomy, taxonomy_http_get=fake_taxonomy_http_get):
            terms = dispatch.fetch_taxonomy_terms("eventcat")

        self.assertEqual(terms[0]["slug"], "technology")
        self.assertIn("per_page=50", calls[0])
        self.assertIn("per_page=10", calls[1])

    def test_fetch_catalog_options_does_not_fall_back_to_event_list(self):
        with PatchDispatch(
            taxonomy,
            catalog_options_from_taxonomy=lambda: (_ for _ in ()).throw(RuntimeError("taxonomy down")),
        ), PatchDispatch(
            fetch_catalog_listing=lambda: self.fail("event-list fallback should not run"),
        ):
            with self.assertRaisesRegex(RuntimeError, "taxonomy down"):
                dispatch.fetch_catalog_options()

    def test_approval_mode_offers_events_without_calendar_write(self):
        calls = []
        sent = []

        def fake_subs_cmd(*args):
            calls.append(args)
            self.assertEqual(args[0], "record-offers")
            payload = json.loads(args[args.index("--events-json") + 1])
            return {
                "events": {"pending_offers": [{**payload[0], "choice_number": 1}]},
                "added_offers": [{**payload[0], "choice_number": 1}],
            }

        with PatchDispatch(
            subs_cmd=fake_subs_cmd,
            send_offer_dm=lambda phone, offers: sent.append((phone, offers)),
            write_event=lambda *args, **kwargs: self.fail("calendar write should not run in approval mode"),
        ):
            result = dispatch.process_approval(
                subscriber(),
                [EVENT],
                {"business"},
                dry_run=False,
                send_dms=True,
            )

        self.assertEqual(result["status"], "offered")
        self.assertEqual(result["offered"], 1)
        self.assertEqual(result["added_offers"][0]["choice_number"], 1)
        self.assertEqual(calls[0][0], "record-offers")
        self.assertEqual(sent[0][0], "+213555000111")
        self.assertEqual(sent[0][1][0]["choice_number"], 1)

    def test_unknown_category_asks_for_available_category_choice(self):
        result = dispatch.process_approval(
            subscriber(categories=["tech"]),
            [EVENT],
            {"business", "health"},
            dry_run=False,
            send_dms=True,
        )

        self.assertEqual(result["status"], "needs-category-choice")
        self.assertEqual(result["unavailable_categories"], ["tech"])
        self.assertIn("business", result["available_categories"])

    def test_taxonomy_known_category_without_current_events_returns_no_matches(self):
        result = dispatch.process_subscriber(
            subscriber(categories=["technology"]),
            [EVENT],
            {"business", "technology"},
            dry_run=False,
            send_dms=True,
        )

        self.assertEqual(result["status"], "no_matches")

    def test_dispatch_does_not_mark_category_unavailable_when_taxonomy_fails(self):
        def fake_subs_cmd(*args):
            self.assertEqual(args[0], "get")
            return subscriber(categories=["technology"])

        with PatchDispatch(
            fetch_catalog_listing=lambda: {1: {"category": "business", "location": "SAFEX, Algiers"}},
            build_upcoming_events=lambda listing: [EVENT],
            subs_cmd=fake_subs_cmd,
            fetch_catalog_options=lambda: (_ for _ in ()).throw(RuntimeError("taxonomy down")),
        ):
            result = dispatch.dispatch(
                argparse.Namespace(phone="+213555000111", dry_run=False, no_dm=True)
            )

        self.assertFalse(result["category_check_verified"])
        self.assertIn("taxonomy down", result["category_verification_error"])
        self.assertEqual(result["results"][0]["status"], "category-check-unavailable")

    def test_dispatch_knows_when_taxonomy_categories_are_needed(self):
        self.assertTrue(
            dispatch.should_fetch_taxonomy_categories(
                [subscriber(categories=["technology"])],
                {"business"},
            )
        )
        self.assertFalse(
            dispatch.should_fetch_taxonomy_categories(
                [subscriber(categories=["business"])],
                {"business"},
            )
        )

    def test_save_selection_writes_selected_event_and_clears_pending(self):
        calls = []
        writes = []
        pending = {**EVENT, "choice_number": 1}

        def fake_subs_cmd(*args):
            calls.append(args)
            command = args[0]
            if command == "get":
                return {
                    "events": {
                        "enabled": True,
                        "user_id": "uid-123",
                        "reminder_minutes": [1440, 120],
                        "pending_offers": [pending],
                    }
                }
            return {"status": "ok"}

        def fake_write_event(user_id, event, reminders):
            writes.append((user_id, event["source_id"], reminders))
            return {"status": "created"}

        with PatchDispatch(
            subs_cmd=fake_subs_cmd,
            calendar_status=lambda user_id: {"calendarConnected": True},
            write_event=fake_write_event,
        ):
            result = dispatch.save_selection(
                argparse.Namespace(phone="+213555000111", select=["1"], dry_run=False)
            )

        self.assertEqual(result["status"], "saved")
        self.assertEqual(writes, [("uid-123", EVENT["source_id"], [1440, 120])])
        self.assertIn(("mark-synced", "--phone", "+213555000111", "--event-id", EVENT["source_id"]), calls)
        self.assertIn(("clear-pending", "--phone", "+213555000111", "--event-id", EVENT["source_id"]), calls)


if __name__ == "__main__":
    unittest.main()
