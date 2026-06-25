import argparse
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("event_subscriptions.py")
SPEC = importlib.util.spec_from_file_location("event_subscriptions", MODULE_PATH)
event_subscriptions = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(event_subscriptions)


def namespace(**values):
    return argparse.Namespace(**values)


class EventSubscriptionsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.registry = Path(self.tmp.name) / "subscriptions.json"
        self.phone = "+213555000111"

    def tearDown(self):
        self.tmp.cleanup()

    def register(self, **overrides):
        args = {
            "registry": self.registry,
            "phone": self.phone,
            "user_id": "uid-123",
            "category": [],
            "location": "",
            "reminder_min": [],
            "delivery_mode": "approval",
        }
        args.update(overrides)
        return event_subscriptions.cmd_register(namespace(**args))

    def offer_payload(self, source_id="algeria-exhibitions:1"):
        return {
            "source_id": source_id,
            "summary": "Startup Fair",
            "location": "SAFEX, Algiers",
            "source_url": "https://example.test/event",
            "category": "business",
            "start_date": "2026-06-27",
            "end_date": "2026-06-28",
            "range": "27 to 28 Jun 2026",
        }

    def test_register_defaults_to_approval_mode(self):
        result = self.register(category=["Business"], location="Algiers")

        events = result["events"]
        self.assertEqual(events["delivery_mode"], "approval")
        self.assertEqual(events["categories"], ["business"])
        self.assertEqual(events["location"], "Algiers")
        self.assertEqual(events["reminder_minutes"], [1440, 120])
        self.assertEqual(events["pending_offers"], [])
        self.assertEqual(events["offered_event_ids"], [])

    def test_record_offers_numbers_pending_events_and_clear_pending_keeps_offer_history(self):
        self.register()
        offer = self.offer_payload()

        first = event_subscriptions.cmd_record_offers(
            namespace(
                registry=self.registry,
                phone=self.phone,
                events_json=json.dumps([offer]),
            )
        )
        self.assertEqual(first["added"], 1)
        self.assertEqual(first["added_offers"][0]["choice_number"], 1)
        self.assertEqual(first["events"]["pending_offers"][0]["choice_number"], 1)

        duplicate = event_subscriptions.cmd_record_offers(
            namespace(
                registry=self.registry,
                phone=self.phone,
                events_json=json.dumps([offer]),
            )
        )
        self.assertEqual(duplicate["added"], 0)
        self.assertEqual(len(duplicate["events"]["pending_offers"]), 1)

        cleared = event_subscriptions.cmd_clear_pending(
            namespace(
                registry=self.registry,
                phone=self.phone,
                event_id=[offer["source_id"]],
            )
        )
        self.assertEqual(cleared["removed"], 1)
        self.assertEqual(cleared["events"]["pending_offers"], [])
        self.assertEqual(cleared["events"]["offered_event_ids"], [offer["source_id"]])


if __name__ == "__main__":
    unittest.main()
