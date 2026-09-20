"""RED regression for the accepted Jev routing catalog contract."""

import unittest

from lib.agent_analysis import AGENT_CHAIN


class JevRoutingCatalogRegressionTest(unittest.TestCase):
    def test_agent_catalog_records_availability_provenance_and_freshness(self):
        self.assertTrue(AGENT_CHAIN)

        for candidate in AGENT_CHAIN:
            with self.subTest(candidate=candidate.name):
                availability = getattr(candidate, "availability", None)
                self.assertIsInstance(availability, dict)
                self.assertIn(
                    availability.get("status"),
                    {"available", "unavailable", "unknown"},
                )
                self.assertIn(
                    availability.get("provenance"),
                    {"read_only_discovery", "operator"},
                )
                self.assertIsInstance(availability.get("checked_at"), str)
                self.assertTrue(availability["checked_at"])


if __name__ == "__main__":
    unittest.main()
