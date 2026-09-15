"""Every sync bucket must actually be visited by some cron tick.

The poll is the safety net under the webhooks — the only thing keeping
balances and holdings fresh — and tenants are spread uniformly across
buckets 0..SYNC_BUCKETS-1. If the slot the cron computes cannot reach a
bucket, every tenant hashed there silently stops polling forever. A slot of
minute//5 reaches only 0..11, so the slot is epoch-five-minute ticks, which
span every bucket.
"""

import datetime as dt
import unittest
from unittest import mock

from oikonome.jobs import worker


class SyncBucketCoverageTests(unittest.TestCase):
    def _slots_over_a_day(self, buckets: int) -> set[int]:
        """The slots sync_all would compute across a day of 5-min ticks."""
        seen = set()
        base = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
        for i in range(0, 24 * 60, 5):          # every 5 minutes for 24h
            now = base + dt.timedelta(minutes=i)
            seen.add(int(now.timestamp() // 300) % buckets)
        return seen

    def test_twelve_buckets_all_reached(self):
        with mock.patch.object(worker, "SYNC_BUCKETS", 12):
            self.assertEqual(self._slots_over_a_day(12), set(range(12)))

    def test_twenty_four_buckets_all_reached(self):
        # the documented "raise it to halve Plaid calls" knob — buckets
        # 12..23 must not be dead, as they would be under a minute//5 slot.
        with mock.patch.object(worker, "SYNC_BUCKETS", 24):
            self.assertEqual(self._slots_over_a_day(24), set(range(24)))

    def test_every_tenant_bucket_is_a_reachable_slot(self):
        for n in (1, 6, 12, 24, 48):
            with mock.patch.object(worker, "SYNC_BUCKETS", n):
                reachable = self._slots_over_a_day(n)
                # a tenant hashes to crc32 % n — every one of those must be
                # a slot some tick lands on
                self.assertEqual(reachable, set(range(n)),
                                 f"SYNC_BUCKETS={n} leaves dead buckets")


if __name__ == "__main__":
    unittest.main()
