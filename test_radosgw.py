import threading
import unittest
from unittest.mock import patch
from radosgw_usage_exporter import RADOSGWCollector, MetricsSet, get_bucket_namespace

class TestGetBucketNamespace(unittest.TestCase):
    def test_namespace_extraction_from_data(self):
        # Test case based on provided Prometheus metrics data

        # First metric: namespace should be "alpha-beta"
        bucket_name_1 = "foo-bar-baz-qux-12345678-1234-5678-1234-567812345678"
        bucket_owner_1 = "obc-alpha-beta-foo-bar-baz-qux-abcdef12-3456-7890-abcd-ef1234567890"
        expected_namespace_1 = "alpha-beta"
        self.assertEqual(get_bucket_namespace(bucket_name_1, bucket_owner_1, "bucket-"), expected_namespace_1)

        # Second metric: namespace should be "gamma-delta"
        bucket_name_2 = "lorem-ipsum-dolor-sit-98765432-4321-8765-4321-876543218765"
        bucket_owner_2 = "obc-gamma-delta-bucket-lorem-ipsum-dolor-sit-1234abcd-5678-efgh-1234-abcd5678efgh"
        expected_namespace_2 = "gamma-delta"
        self.assertEqual(get_bucket_namespace(bucket_name_2, bucket_owner_2, "bucket-"), expected_namespace_2)

        # Third metric: fuzzy match to extract namespace "omega-theta"
        bucket_name_3 = "theta-omega-phi-psi"
        bucket_owner_3 = "obc-omega-theta-phi-psi-abcdefab-cdef-abcd-efab-cdefabcdefab"
        expected_namespace_3 = "omega-theta"
        self.assertEqual(get_bucket_namespace(bucket_name_3, bucket_owner_3, "bucket-"), expected_namespace_3)

    def test_explicit_bucket_name_equal_to_full_owner_string(self):
        # Regression case: deployments that set spec.bucketName to the full
        # "<namespace>-bucket-<obcName>" string instead of letting Rook
        # auto-generate "<obcName>-<uuid>". Here bucket_name == owner_no_prefix,
        # so cases 1-3 (which compare against cleaned_bucket_name) can't find a
        # boundary and the old code fell through to the fuzzy Case 4 fallback,
        # which returned "production-agent" (first hyphenated partial match)
        # instead of the correct "production-agent-places".
        bucket_name = "production-agent-places-bucket-webcontent-cache"
        bucket_owner = "obc-production-agent-places-bucket-webcontent-cache-739f1eaa-7ddf-46ea-89ec-8e50c45f84eb"
        self.assertEqual(get_bucket_namespace(bucket_name, bucket_owner, "bucket-"), "production-agent-places")

    def test_existing_fuzzy_fallback_cases_unaffected(self):
        # These already depended on the Case 4 fuzzy fallback before this fix
        # (their owner string has no literal "-bucket-" substring for the new
        # Case 3.5 to catch, so they must resolve exactly as before).
        cases = [
            (
                "cdp-production-mongodb-backup",
                "obc-production-cdp-mongodb-backup-0d9f7ba7-fec1-4c23-afb8-cbfc26da00ee",
                "production-cdp",
            ),
            (
                "fanz-production-mongodb-backup",
                "obc-production-fanz-mongodb-backup-b87dcbcc-b1a1-4d7a-98fe-229c1117cdd3",
                "production-fanz",
            ),
            (
                "pro-cxpa-ch-processing-chi-4d0a8599-4282-4234-b3bb-f3d78c0674a3",
                "obc-production-cxpa-pro-cxpa-ch-processing-chi-backup-c34f04d0-8196-4981-b39d-cdcf1a12673b",
                "production-cxpa",
            ),
            (
                "pro-cxpa-clickhouse-chi-ba-4f43923e-4ad1-4392-894f-b78a5ac7e1a6",
                "obc-production-cxpa-pro-cxpa-clickhouse-chi-backup-e4c53275-d5df-410a-ae65-b69431c18342",
                "production-cxpa",
            ),
            (
                "pro-infra-langfuse-chi-bac-1d738235-ab02-432a-80c0-e19c93ba12b1",
                "obc-production-infra-pro-infra-langfuse-chi-backup-8a4144bc-28bc-433a-b9a1-5184de7edf07",
                "production-infra",
            ),
        ]
        for bucket_name, bucket_owner, expected in cases:
            with self.subTest(bucket_owner=bucket_owner):
                self.assertEqual(get_bucket_namespace(bucket_name, bucket_owner, "bucket-"), expected)

    def test_cases_1_to_3_unaffected(self):
        # Sample of real buckets resolved via the pre-existing suffix/substring
        # matches (cases 1-3), to prove the new Case 3.5 doesn't preempt them.
        cases = [
            ("argo-workflow", "obc-argo-argo-workflow-bucket", "argo"),
            (
                "tempo-bucket",
                "obc-tempo-tempo-bucket-c644339d-9090-49c5-a1ce-73e5e69bbb40",
                "tempo",
            ),
            (
                "glitchtip-bucket",
                "obc-glitchtip-glitchtip-bucket-57613ae2-8844-49f6-b6c4-7fc22032abe7",
                "glitchtip",
            ),
            (
                "sisense-loki-boltdb",
                "obc-production-sisense-sisense-loki-boltdb-bucket-bafb0255-ea8b-40e8-972b-796cf37225bc",
                "production-sisense",
            ),
            (
                "docker-hub-cache",
                "obc-container-registry-docker-hub-cache-bucket-a89d726f-4a90-4dc2-b034-aab0035b6973",
                "container-registry",
            ),
            (
                "anti-cheating-3ec5da05-f640-4b0e-ac97-9f64171e05ff",
                "obc-production-cxpa-bucket-anti-cheating-9739d3a2-2b8b-4665-a1ce-02bf7c62df2a",
                "production-cxpa",
            ),
        ]
        for bucket_name, bucket_owner, expected in cases:
            with self.subTest(bucket_owner=bucket_owner):
                self.assertEqual(get_bucket_namespace(bucket_name, bucket_owner, "bucket-"), expected)

    def test_non_obc_owner_returns_empty_string(self):
        self.assertEqual(get_bucket_namespace("forms-e9eb30bd-d6d6-4499-b413-89c27886f4ff", "ceph-user-sn6mGpN4", "bucket-"), "")

    def test_empty_obc_name_prefix_skips_case_3_5(self):
        # With no prefix configured, Case 3.5 must no-op (not raise on the
        # empty-string delimiter) and fall through to the pre-existing Case 4
        # behavior. Real deployments always set OBC_NAME_PREFIX="bucket-", so
        # this only guards against a crash / accidental behavior change when
        # the prefix is unset - it is not asserting a "correct" namespace.
        bucket_name = "production-agent-places-bucket-webcontent-cache"
        bucket_owner = "obc-production-agent-places-bucket-webcontent-cache-739f1eaa-7ddf-46ea-89ec-8e50c45f84eb"
        self.assertEqual(get_bucket_namespace(bucket_name, bucket_owner, ""), "production-agent")

def _make_collector(enable_namespace_extraction):
    return RADOSGWCollector(
        host="http://localhost",
        admin_entry="admin",
        access_key="x",
        secret_key="y",
        store="test",
        insecure=True,
        timeout=10,
        tag_list=[],
        enable_namespace_extraction=enable_namespace_extraction,
        obc_name_prefix="bucket-",
    )


_USAGE_ENTRIES = {
    "entries": [
        {
            "owner": "obc-team-a-bucket-alpha-uuid",
            "buckets": [
                {
                    "bucket": "alpha",
                    "categories": [
                        {
                            "category": "put_obj",
                            "bytes_sent": 0,
                            "bytes_received": 100,
                            "ops": 1,
                            "successful_ops": 1,
                        }
                    ],
                }
            ],
        }
    ]
}

_BUCKET_STATS = [
    {
        "bucket": "alpha",
        "owner": "obc-team-a-bucket-alpha-uuid",
        "num_shards": 11,
        "zonegroup": "eu-central-1",
        "usage": {"rgw.main": {"size_actual": 100, "num_objects": 1}},
        "bucket_quota": {
            "max_size": 1000,
            "max_size_kb": 1,
            "max_objects": 100,
            "enabled": True,
        },
    }
]


def _fake_request_data(query, args):
    if query == "usage":
        return _USAGE_ENTRIES
    if query == "bucket":
        return _BUCKET_STATS
    return None


class TestCollectEndToEnd(unittest.TestCase):
    # Regression test for the exact bug fixed on top of PR #1 ("Split metrics
    # building from the collector"): moving state into MetricsSet without
    # threading enable_namespace_extraction/obc_name_prefix through its
    # __init__ made every collect() call raise AttributeError, unconditionally,
    # regardless of whether extraction was even enabled. Unit tests against
    # get_bucket_namespace() alone never exercise collect(), so this class of
    # bug was invisible to the existing suite - this is why it shipped.
    def test_collect_succeeds_with_namespace_extraction_enabled(self):
        collector = _make_collector(enable_namespace_extraction=True)
        with patch.object(collector, "_request_data", side_effect=_fake_request_data), \
             patch.object(collector, "_get_rgw_users", return_value=None):
            metrics = list(collector.collect())
        self.assertTrue(metrics)

    def test_collect_succeeds_with_namespace_extraction_disabled(self):
        collector = _make_collector(enable_namespace_extraction=False)
        with patch.object(collector, "_request_data", side_effect=_fake_request_data), \
             patch.object(collector, "_get_rgw_users", return_value=None):
            metrics = list(collector.collect())
        self.assertTrue(metrics)


class TestConcurrentCollectIsolation(unittest.TestCase):
    # Regression test for PR #1's actual stated purpose: two overlapping
    # collect() calls against one shared RADOSGWCollector must not share
    # mutable state. Before the PR, usage_dict/_prometheus_metrics lived on
    # the shared collector itself, so one request's in-progress state could
    # be reset or overwritten by a second request running concurrently.
    #
    # This forces a genuine interleave rather than relying on GIL-timing luck:
    # thread A runs collect() up through its own _setup_empty_prometheus_metrics
    # reset, then blocks; thread B (identical input data) runs its entire
    # collect() to completion while A is blocked; A is then released to finish.
    # With per-call state (the fix), A's result is unaffected by B running
    # concurrently. With shared state (the bug), B's reset/write would corrupt
    # or blank out what A had already started, and this test fails.
    def test_two_overlapping_collects_do_not_corrupt_each_other(self):
        collector = _make_collector(enable_namespace_extraction=False)
        real_setup = MetricsSet._setup_empty_prometheus_metrics
        call_index = {"n": 0}
        call_lock = threading.Lock()
        a_ready = threading.Event()
        b_done = threading.Event()

        def synced_setup(self, args):
            real_setup(self, args)
            with call_lock:
                call_index["n"] += 1
                is_first = call_index["n"] == 1
            if is_first:
                a_ready.set()
                if not b_done.wait(timeout=5):
                    raise AssertionError("thread B did not finish in time")
            else:
                if not a_ready.wait(timeout=5):
                    raise AssertionError("thread A did not reach sync point in time")

        results = {}
        errors = []

        def run(key):
            try:
                with patch.object(collector, "_request_data", side_effect=_fake_request_data), \
                     patch.object(collector, "_get_rgw_users", return_value=None), \
                     patch.object(MetricsSet, "_setup_empty_prometheus_metrics", synced_setup):
                    results[key] = list(collector.collect())
            except Exception as exc:  # pragma: no cover - surfaced via errors list
                errors.append(exc)
            finally:
                if key == "b":
                    b_done.set()

        thread_a = threading.Thread(target=run, args=("a",))
        thread_b = threading.Thread(target=run, args=("b",))
        thread_a.start()
        a_ready.wait(timeout=5)
        thread_b.start()
        thread_a.join(timeout=10)
        thread_b.join(timeout=10)

        self.assertFalse(errors, f"collect() raised: {errors}")
        self.assertIn("a", results)
        self.assertIn("b", results)

        for key in ("a", "b"):
            all_samples = [
                (sample.name, tuple(sorted(sample.labels.items())))
                for family in results[key]
                for sample in family.samples
            ]
            unique_samples = set(all_samples)
            alpha_bytes = [
                sample.value
                for family in results[key]
                for sample in family.samples
                if sample.name == "radosgw_usage_bucket_bytes"
            ]
            self.assertEqual(
                alpha_bytes, [100],
                f"thread {key}: expected exactly one alpha-bucket sample of 100 bytes, "
                f"got {alpha_bytes} (duplicate/missing samples indicate shared-state corruption)",
            )
            self.assertEqual(
                len(all_samples), len(unique_samples),
                f"thread {key}: duplicate (name, labels) sample pairs found - shared-state corruption",
            )


if __name__ == '__main__':
    unittest.main()

