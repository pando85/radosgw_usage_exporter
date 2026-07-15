#!/usr/bin/python
# -*- coding: utf-8 -*-

import time
import threading
import requests
import warnings
import logging
import json
import argparse
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlencode, quote
from awsauth import S3Auth
from requests.adapters import HTTPAdapter

# urllib3 renamed the Retry "methods" kwarg across versions; import defensively.
try:
    from urllib3.util.retry import Retry
except ImportError:  # pragma: no cover - very old urllib3 bundled in requests
    from requests.packages.urllib3.util.retry import Retry

from prometheus_client import start_http_server
from collections import defaultdict, Counter
from prometheus_client.core import GaugeMetricFamily, CounterMetricFamily, REGISTRY


# The four counter fields we track per (owner, bucket, category) from the usage log.
USAGE_FIELDS = ("ops", "successful_ops", "bytes_sent", "bytes_received")


def _make_retry(total):
    """
    Build a urllib3 Retry that works across versions (the kwarg for which HTTP
    methods are retryable was renamed allowed_methods <- method_whitelist).
    """
    common = dict(
        total=total,
        connect=total,
        read=total,
        backoff_factor=0.5,
        status_forcelist=(500, 502, 503, 504),
        raise_on_status=False,
    )
    try:
        return Retry(allowed_methods=frozenset(["GET"]), **common)
    except TypeError:  # older urllib3
        return Retry(method_whitelist=frozenset(["GET"]), **common)


class RADOSGWCollector(object):
    """RADOSGWCollector gathers bucket level usage data for all buckets from
    the specified RADOSGW and presents it in a format suitable for pulling via
    a Prometheus server.

    Design (why this is not a naive per-scrape collector):

    * A background poller thread refreshes the data on its own schedule and
      builds an immutable metric *snapshot*.  ``collect()`` only serves that
      snapshot, so Prometheus scrapes are instant and can never time out or
      block on RADOSGW (the old failure mode: slow/huge usage reads on the
      scrape path -> scrape timeout -> "dormant" gaps -> giant catch-up spikes).

    * Usage is fetched *incrementally*.  RADOSGW returns usage as per-time-bin
      entries; we remember the last-seen value of each open bin and only add the
      positive delta to an in-memory monotonic cumulative counter.  Each poll
      therefore costs O(recent activity) instead of O(entire usage log), and the
      exported counter stays monotonic even if the usage log is trimmed.

    * Work is fanned out across a worker pool (one task per user for usage +
      user-info, one task per bucket for bucket stats) so a poll finishes in
      roughly wall-time = slowest single request rather than the sum of all.

    * I/O is hardened: pooled session with automatic retries/backoff, every
      request wrapped so a failure returns None instead of aborting the poll,
      and gauges are served from a last-good cache so a partial failure yields
      stale values rather than gaps.

    NOTE: By default RADOSGW Servers do not gather usage data and it must be
    enabled by 'rgw enable usage log = true' in the appropriate section
    of ceph.conf see Ceph documentation for details"""

    def __init__(
        self, host, admin_entry, access_key, secret_key, store, insecure,
        timeout, tag_list, enable_namespace_extraction, obc_name_prefix,
        poll_interval, workers, usage_window, seed_full, retries,
    ):
        super(RADOSGWCollector, self).__init__()
        self.host = host
        self.access_key = access_key
        self.secret_key = secret_key
        self.store = store
        self.insecure = insecure
        self.timeout = timeout
        self.tag_list = tag_list
        self.enable_namespace_extraction = enable_namespace_extraction
        self.obc_name_prefix = obc_name_prefix

        # New behavioural knobs.
        self.poll_interval = poll_interval          # seconds between background polls
        self.workers = max(1, int(workers))         # parallel request fan-out
        self.usage_window = max(0, int(usage_window))  # incremental look-back (s)
        self.seed_full = seed_full                  # read full history on first poll
        self.retries = max(0, int(retries))

        # helpers for default schema
        if not self.host.startswith("http"):
            self.host = "http://{0}".format(self.host)
        # and for request_uri
        if not self.host.endswith("/"):
            self.host = "{0}/".format(self.host)

        self.url = "{0}{1}/".format(self.host, admin_entry)

        # --- persistent state shared between the poller and collect() ---
        # The immutable snapshot served to Prometheus (list of metric families).
        self._snapshot = None
        self._snapshot_lock = threading.Lock()

        # Incremental usage accounting (only touched by the single poller thread).
        # (owner, bucket, category) -> Counter of cumulative totals
        self._cumulative = defaultdict(Counter)
        # (owner, bucket, category, bin_id) -> Counter of last-seen bin values
        self._bin_seen = {}
        self._usage_seeded = False

        # Last-good raw JSON for gauges so a partial fetch failure keeps values.
        self._bucket_cache = {}   # bucket_name -> bucket stats dict
        self._user_cache = {}     # uid -> user info dict

        # Poller health, exported for observability.
        self._last_poll_ok = 0.0
        self._last_poll_duration = 0.0
        self._poll_errors = 0
        self._last_poll_success = False

        # Prepare Requests Session
        self._session()

    def start(self):
        """Launch the background poller. Called once from main()."""
        thread = threading.Thread(
            target=self._poll_loop, name="rgw-poller", daemon=True
        )
        thread.start()

    # ------------------------------------------------------------------ #
    # Scrape path - serves the cached snapshot only, never touches RADOSGW
    # ------------------------------------------------------------------ #
    def collect(self):
        with self._snapshot_lock:
            snapshot = self._snapshot

        if not snapshot:
            # Poller has not produced data yet (or first poll still running).
            # Expose exporter_up=0 so the state is observable in Prometheus.
            up = GaugeMetricFamily(
                "radosgw_usage_exporter_up",
                "1 if the exporter has a fresh snapshot from the last poll",
                labels=[],
            )
            up.add_metric([], 0)
            yield up
            return

        for metric in snapshot:
            yield metric

    # ------------------------------------------------------------------ #
    # Background poller
    # ------------------------------------------------------------------ #
    def _poll_loop(self):
        cycle = 0
        while True:
            cycle += 1
            start = time.time()
            summary = None
            try:
                summary = self._poll_once()
                self._last_poll_ok = time.time()
                self._last_poll_success = True
            except Exception:
                self._poll_errors += 1
                self._last_poll_success = False
                logging.exception("RGW poll cycle %d failed", cycle)
            finally:
                self._last_poll_duration = time.time() - start
                # Rebuild the snapshot even on partial failure so the health
                # metrics (and any data we did gather) stay current.
                try:
                    self._build_snapshot()
                except Exception:
                    logging.exception("Failed to build metric snapshot")
            if summary is not None:
                logging.info(
                    "poll #%d ok in %.2fs: %d users, %d buckets, %d usage series, "
                    "%d bins tracked (errors so far: %d)",
                    cycle, self._last_poll_duration, summary[0], summary[1],
                    len(self._cumulative), len(self._bin_seen), self._poll_errors,
                )
            time.sleep(self.poll_interval)

    def _poll_once(self):
        """One refresh cycle: discover users/buckets, fan the fetches out across
        the worker pool, fold the results into persistent state.
        Returns (num_users, num_buckets) for logging."""
        if not self._usage_seeded and self.seed_full:
            logging.info("first poll: seeding usage counters from full history")

        users = self._get_rgw_users() or []
        bucket_names = self._list_buckets()

        logging.info(
            "poll start: %d users, %d buckets, %s mode, %d workers",
            len(users), len(bucket_names),
            "seed" if not self._usage_seeded else "incremental", self.workers,
        )

        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            usage_futures = {
                pool.submit(self._fetch_user_usage, uid): uid for uid in users
            }
            info_futures = {
                pool.submit(self._fetch_user_info, uid): uid for uid in users
            }
            bucket_futures = {
                pool.submit(self._fetch_bucket_stats, name): name
                for name in bucket_names
            }

            # Usage deltas are applied here in the poller thread (single writer),
            # so no locking is needed around _cumulative / _bin_seen.
            for fut in as_completed(usage_futures):
                uid = usage_futures[fut]
                try:
                    data = fut.result()
                    if data:
                        self._apply_usage(data)
                except Exception as e:
                    logging.warning("usage fetch failed for uid %r: %s", uid, e)

            for fut in as_completed(info_futures):
                uid = info_futures[fut]
                try:
                    info = fut.result()
                    if info:
                        self._user_cache[uid] = info
                except Exception as e:
                    logging.warning("user-info fetch failed for uid %r: %s", uid, e)

            for fut in as_completed(bucket_futures):
                name = bucket_futures[fut]
                try:
                    data = fut.result()
                    if isinstance(data, dict):
                        self._bucket_cache[name] = data
                except Exception as e:
                    logging.warning("bucket stats fetch failed for %r: %s", name, e)

        # After the first successful pass we switch usage to incremental mode.
        self._usage_seeded = True
        self._prune_bins()
        return len(users), len(bucket_names)

    # ------------------------------------------------------------------ #
    # Requests / session
    # ------------------------------------------------------------------ #
    def _session(self):
        """Setup a pooled Requests session with automatic retries/backoff."""
        self.session = requests.Session()
        # Size the pool to the fan-out so concurrent workers reuse connections.
        pool_size = max(10, self.workers * 2)
        adapter = HTTPAdapter(
            pool_connections=pool_size,
            pool_maxsize=pool_size,
            max_retries=_make_retry(self.retries),
        )
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

        # Inversion of condition, when '--insecure' is defined we disable
        # requests warning about certificate hostname mismatch.
        if not self.insecure:
            warnings.filterwarnings("ignore", message="Unverified HTTPS request")

    def _request_data(self, query, params=None):
        """
        Requests data from RGW. Returns parsed JSON on success, or None on any
        error (non-200, timeout, connection failure). Never raises to the caller
        so one bad request cannot abort a whole poll.
        """
        q = {"format": "json"}
        if params:
            q.update(params)
        # quote (not quote_plus) so spaces in dates become %20, which RGW expects.
        url = "{0}{1}/?{2}".format(self.url, query, urlencode(q, quote_via=quote))

        try:
            response = self.session.get(
                url,
                verify=self.insecure,
                timeout=float(self.timeout),
                auth=S3Auth(self.access_key, self.secret_key, self.host),
            )
            if response.status_code == requests.codes.ok:
                return response.json()
            logging.error(
                "Request error [%s] on %s: %s",
                response.status_code,
                query,
                response.content[:256].decode("utf-8", "replace"),
            )
            return None
        # DNS, connection errors, timeouts, etc.
        except requests.exceptions.RequestException as e:
            logging.info("Request error on %s: %s", query, e)
            return None
        except ValueError as e:  # malformed JSON body
            logging.warning("Bad JSON on %s: %s", query, e)
            return None

    # ------------------------------------------------------------------ #
    # Fetch helpers (run inside worker threads)
    # ------------------------------------------------------------------ #
    def _fetch_user_usage(self, uid):
        """Fetch usage for a single user. Uses a bounded look-back window once
        seeded so each request stays small; the first pass optionally reads full
        history to seed the cumulative counters at their true absolute value."""
        params = {"show-summary": "False", "show-entries": "True", "uid": uid}
        if self._usage_seeded or not self.seed_full:
            since = time.gmtime(time.time() - self.usage_window)
            params["start"] = time.strftime("%Y-%m-%d %H:%M:%S", since)
        return self._request_data("usage", params)

    def _fetch_user_info(self, uid):
        return self._request_data("user", {"uid": uid, "stats": "True"})

    def _fetch_bucket_stats(self, name):
        data = self._request_data("bucket", {"bucket": name, "stats": "True"})
        # Most releases return a dict for a single bucket; a few wrap it in a
        # one-element list. Normalise to the dict.
        if isinstance(data, list):
            data = next((x for x in data if isinstance(x, dict)), None)
        return data

    def _list_buckets(self):
        """Return the list of bucket names (cheap call, no per-bucket stats)."""
        data = self._request_data("bucket", {})
        if isinstance(data, list):
            return data
        return []

    def _get_rgw_users(self):
        """API request to get users."""
        rgw_users = self._request_data("user", {"list": ""})
        if rgw_users and "keys" in rgw_users:
            return rgw_users["keys"]
        # Compat with old Ceph versions (pre 12.2.13/13.2.9)
        return self._request_data("metadata/user", {})

    # ------------------------------------------------------------------ #
    # Incremental usage accounting
    # ------------------------------------------------------------------ #
    def _apply_usage(self, data):
        """Fold a usage response into the cumulative counters using per-bin
        deltas so counters stay monotonic and each poll only adds new activity.
        Runs single-threaded in the poller."""
        for entry in data.get("entries", []):
            owner = entry.get("owner") or entry.get("user")
            if not owner:
                continue
            for bucket in entry.get("buckets", []):
                try:
                    if not isinstance(bucket, dict):
                        continue  # Hammer junk
                    name = bucket.get("bucket") or "bucket_root"
                    # Per-time-bin id so the current (open) bin is tracked
                    # separately from closed ones. Prefer numeric epoch.
                    bin_id = bucket.get("epoch") or bucket.get("time") or 0
                    for category in bucket.get("categories", []):
                        cname = category.get("category")
                        new = Counter(
                            {f: category.get(f, 0) for f in USAGE_FIELDS}
                        )
                        key = (owner, name, cname, bin_id)
                        prev = self._bin_seen.get(key)
                        if prev is None:
                            delta = new
                        else:
                            # Only positive movement: a bin can never lose ops,
                            # so a decrease means a reset/trim -> ignore it.
                            delta = Counter()
                            for f in USAGE_FIELDS:
                                d = new[f] - prev[f]
                                if d > 0:
                                    delta[f] = d
                        self._bin_seen[key] = new
                        if delta:
                            self._cumulative[(owner, name, cname)].update(delta)
                except (KeyError, TypeError) as e:
                    logging.warning(
                        "Skipping malformed usage entry for bucket %r: %s",
                        bucket.get("bucket", "?") if isinstance(bucket, dict) else "?",
                        e,
                    )
                    continue

    def _prune_bins(self):
        """Drop tracking state for closed bins older than the look-back window;
        they are immutable and already folded into the cumulative totals, so
        keeping them would leak memory over long runs."""
        if not self._bin_seen:
            return
        cutoff = time.time() - max(self.usage_window * 2, 7200)
        stale = [
            key for key in self._bin_seen
            if isinstance(key[3], (int, float)) and key[3] and key[3] < cutoff
        ]
        for key in stale:
            del self._bin_seen[key]

    # ------------------------------------------------------------------ #
    # Snapshot building
    # ------------------------------------------------------------------ #
    def _build_snapshot(self):
        """Build a fresh set of metric families from persistent state and swap
        it in atomically for collect() to serve."""
        metrics = self._setup_empty_prometheus_metrics()

        self._populate_usage_metrics(metrics)

        for bucket in list(self._bucket_cache.values()):
            self._populate_bucket_usage(metrics, bucket)

        for uid, info in list(self._user_cache.items()):
            self._populate_user_info(metrics, uid, info)

        # Poll/health metrics.
        metrics["scrape_duration_seconds"].add_metric([], self._last_poll_duration)

        extras = []
        up = GaugeMetricFamily(
            "radosgw_usage_exporter_up",
            "1 if the exporter has a fresh snapshot from the last poll",
            labels=[],
        )
        up.add_metric([], 1 if self._last_poll_success else 0)
        extras.append(up)

        last_ok = GaugeMetricFamily(
            "radosgw_usage_exporter_last_success_timestamp_seconds",
            "Unix timestamp of the last successful poll",
            labels=[],
        )
        last_ok.add_metric([], self._last_poll_ok)
        extras.append(last_ok)

        errors = CounterMetricFamily(
            "radosgw_usage_exporter_poll_errors_total",
            "Total number of failed poll cycles",
            labels=[],
        )
        errors.add_metric([], self._poll_errors)
        extras.append(errors)

        snapshot = list(metrics.values()) + extras
        with self._snapshot_lock:
            self._snapshot = snapshot

    def _setup_empty_prometheus_metrics(self):
        """
        The metrics we want to export. Returns a fresh dict each call so the
        snapshot is immutable once built.
        """
        b_labels = ["bucket", "owner", "category", "store"]
        if self.tag_list:
            b_labels = b_labels + self.tag_list.split(",")
        if self.enable_namespace_extraction:
            b_labels.append("namespace")

        return {
            "ops": CounterMetricFamily(
                "radosgw_usage_ops_total",
                "Number of operations",
                labels=b_labels,
            ),
            "successful_ops": CounterMetricFamily(
                "radosgw_usage_successful_ops_total",
                "Number of successful operations",
                labels=b_labels,
            ),
            "bytes_sent": CounterMetricFamily(
                "radosgw_usage_sent_bytes_total",
                "Bytes sent by the RADOSGW",
                labels=b_labels,
            ),
            "bytes_received": CounterMetricFamily(
                "radosgw_usage_received_bytes_total",
                "Bytes received by the RADOSGW",
                labels=b_labels,
            ),
            "bucket_usage_bytes": GaugeMetricFamily(
                "radosgw_usage_bucket_bytes",
                "Bucket used bytes",
                labels=b_labels,
            ),
            "bucket_utilized_bytes": GaugeMetricFamily(
                "radosgw_usage_bucket_utilized_bytes",
                "Bucket utilized bytes",
                labels=b_labels,
            ),
            "bucket_usage_objects": GaugeMetricFamily(
                "radosgw_usage_bucket_objects",
                "Number of objects in bucket",
                labels=b_labels,
            ),
            "bucket_quota_enabled": GaugeMetricFamily(
                "radosgw_usage_bucket_quota_enabled",
                "Quota enabled for bucket",
                labels=b_labels,
            ),
            "bucket_quota_max_size": GaugeMetricFamily(
                "radosgw_usage_bucket_quota_size",
                "Maximum allowed bucket size",
                labels=b_labels,
            ),
            "bucket_quota_max_size_bytes": GaugeMetricFamily(
                "radosgw_usage_bucket_quota_size_bytes",
                "Maximum allowed bucket size in bytes",
                labels=b_labels,
            ),
            "bucket_quota_max_objects": GaugeMetricFamily(
                "radosgw_usage_bucket_quota_size_objects",
                "Maximum allowed bucket size in number of objects",
                labels=b_labels,
            ),
            "bucket_shards": GaugeMetricFamily(
                "radosgw_usage_bucket_shards",
                "Number ob shards in bucket",
                labels=b_labels,
            ),
            "user_metadata": GaugeMetricFamily(
                "radosgw_user_metadata",
                "User metadata",
                labels=["user", "display_name", "email", "storage_class", "store"],
            ),
            "user_quota_enabled": GaugeMetricFamily(
                "radosgw_usage_user_quota_enabled",
                "User quota enabled",
                labels=["user", "store"],
            ),
            "user_quota_max_size": GaugeMetricFamily(
                "radosgw_usage_user_quota_size",
                "Maximum allowed size for user",
                labels=["user", "store"],
            ),
            "user_quota_max_size_bytes": GaugeMetricFamily(
                "radosgw_usage_user_quota_size_bytes",
                "Maximum allowed size in bytes for user",
                labels=["user", "store"],
            ),
            "user_quota_max_objects": GaugeMetricFamily(
                "radosgw_usage_user_quota_size_objects",
                "Maximum allowed number of objects across all user buckets",
                labels=["user", "store"],
            ),
            "user_bucket_quota_enabled": GaugeMetricFamily(
                "radosgw_usage_user_bucket_quota_enabled",
                "User per-bucket-quota enabled",
                labels=["user", "store"],
            ),
            "user_bucket_quota_max_size": GaugeMetricFamily(
                "radosgw_usage_user_bucket_quota_size",
                "Maximum allowed size for each bucket of user",
                labels=["user", "store"],
            ),
            "user_bucket_quota_max_size_bytes": GaugeMetricFamily(
                "radosgw_usage_user_bucket_quota_size_bytes",
                "Maximum allowed size bytes size for each bucket of user",
                labels=["user", "store"],
            ),
            "user_bucket_quota_max_objects": GaugeMetricFamily(
                "radosgw_usage_user_bucket_quota_size_objects",
                "Maximum allowed number of objects in each user bucket",
                labels=["user", "store"],
            ),
            "user_total_objects": GaugeMetricFamily(
                "radosgw_usage_user_total_objects",
                "Usage of objects by user",
                labels=["user", "store"],
            ),
            "user_total_bytes": GaugeMetricFamily(
                "radosgw_usage_user_total_bytes",
                "Usage of bytes by user",
                labels=["user", "store"],
            ),
            "scrape_duration_seconds": GaugeMetricFamily(
                "radosgw_usage_scrape_duration_seconds",
                "Amount of time the last background poll took",
                labels=[],
            ),
        }

    def _populate_usage_metrics(self, metrics):
        """Populate the usage counter families from the cumulative totals."""
        tag_count = len(self.tag_list.split(",")) if self.tag_list else 0
        for (bucket_owner, bucket_name, category), data in list(
            self._cumulative.items()
        ):
            # Build metrics labels to match the label schema
            u_metrics = [bucket_name, bucket_owner, category, self.store]
            # Usage API doesn't provide tags -> empty placeholders
            if tag_count:
                u_metrics = u_metrics + [""] * tag_count
            if self.enable_namespace_extraction:
                u_metrics.append(
                    get_bucket_namespace(bucket_name, bucket_owner, self.obc_name_prefix)
                )

            metrics["ops"].add_metric(u_metrics, data.get("ops", 0))
            metrics["successful_ops"].add_metric(u_metrics, data.get("successful_ops", 0))
            metrics["bytes_sent"].add_metric(u_metrics, data.get("bytes_sent", 0))
            metrics["bytes_received"].add_metric(u_metrics, data.get("bytes_received", 0))

    def _populate_bucket_usage(self, metrics, bucket):
        """
        Populate bucket gauge families from a single bucket-stats dict.
        Some skips and adjustments for various Ceph releases.
        """
        if type(bucket) is not dict:
            # Hammer junk, just skip it
            return

        bucket_name = bucket["bucket"]
        bucket_owner = bucket["owner"]
        bucket_shards = bucket["num_shards"]
        bucket_usage_bytes = 0
        bucket_utilized_bytes = 0
        bucket_usage_objects = 0
        if self.enable_namespace_extraction:
            bucket_namespace = get_bucket_namespace(
                bucket_name, bucket_owner, self.obc_name_prefix
            )

        if bucket["usage"] and "rgw.main" in bucket["usage"]:
            # Prefer bytes, instead kbytes
            if "size_actual" in bucket["usage"]["rgw.main"]:
                bucket_usage_bytes = bucket["usage"]["rgw.main"]["size_actual"]
            # Hammer don't have bytes field
            elif "size_kb_actual" in bucket["usage"]["rgw.main"]:
                usage_kb = bucket["usage"]["rgw.main"]["size_kb_actual"]
                bucket_usage_bytes = usage_kb * 1024

            # Compressed buckets, since Kraken
            if "size_utilized" in bucket["usage"]["rgw.main"]:
                bucket_utilized_bytes = bucket["usage"]["rgw.main"]["size_utilized"]

            # Get number of objects in bucket
            if "num_objects" in bucket["usage"]["rgw.main"]:
                bucket_usage_objects = bucket["usage"]["rgw.main"]["num_objects"]

        if "zonegroup" in bucket:
            bucket_zonegroup = bucket["zonegroup"]
        # Hammer
        else:
            bucket_zonegroup = "0"

        taglist = []
        if self.tag_list:
            bucket_tagset = bucket.get("tagset", {})
            for k in self.tag_list.split(","):
                taglist.append(bucket_tagset.get(k, ""))

        b_metrics = [bucket_name, bucket_owner, bucket_zonegroup, self.store]
        if taglist:
            b_metrics = b_metrics + taglist
        if self.enable_namespace_extraction:
            b_metrics.append(bucket_namespace)

        metrics["bucket_usage_bytes"].add_metric(b_metrics, bucket_usage_bytes)
        metrics["bucket_utilized_bytes"].add_metric(b_metrics, bucket_utilized_bytes)
        metrics["bucket_usage_objects"].add_metric(b_metrics, bucket_usage_objects)

        if "bucket_quota" in bucket:
            metrics["bucket_quota_enabled"].add_metric(
                b_metrics, bucket["bucket_quota"]["enabled"]
            )
            metrics["bucket_quota_max_size"].add_metric(
                b_metrics, bucket["bucket_quota"]["max_size"]
            )
            metrics["bucket_quota_max_size_bytes"].add_metric(
                b_metrics, bucket["bucket_quota"]["max_size_kb"] * 1024
            )
            metrics["bucket_quota_max_objects"].add_metric(
                b_metrics, bucket["bucket_quota"]["max_objects"]
            )

        metrics["bucket_shards"].add_metric(b_metrics, bucket_shards)

    def _populate_user_info(self, metrics, user, user_info):
        """Populate user gauge families from a pre-fetched user-info dict."""
        if not isinstance(user_info, dict):
            return

        user_display_name = user_info.get("display_name", "")
        user_email = user_info.get("email", "")
        # Nautilus+
        user_storage_class = user_info.get("default_storage_class", "")

        metrics["user_metadata"].add_metric(
            [user, user_display_name, user_email, user_storage_class, self.store], 1
        )

        if "stats" in user_info:
            metrics["user_total_bytes"].add_metric(
                [user, self.store], user_info["stats"]["size_actual"]
            )
            metrics["user_total_objects"].add_metric(
                [user, self.store], user_info["stats"]["num_objects"]
            )

        if "user_quota" in user_info:
            quota = user_info["user_quota"]
            metrics["user_quota_enabled"].add_metric([user, self.store], quota["enabled"])
            metrics["user_quota_max_size"].add_metric([user, self.store], quota["max_size"])
            metrics["user_quota_max_size_bytes"].add_metric(
                [user, self.store], quota["max_size_kb"] * 1024
            )
            metrics["user_quota_max_objects"].add_metric(
                [user, self.store], quota["max_objects"]
            )

        if "bucket_quota" in user_info:
            quota = user_info["bucket_quota"]
            metrics["user_bucket_quota_enabled"].add_metric(
                [user, self.store], quota["enabled"]
            )
            metrics["user_bucket_quota_max_size"].add_metric(
                [user, self.store], quota["max_size"]
            )
            metrics["user_bucket_quota_max_size_bytes"].add_metric(
                [user, self.store], quota["max_size_kb"] * 1024
            )
            metrics["user_bucket_quota_max_objects"].add_metric(
                [user, self.store], quota["max_objects"]
            )


def parse_args():
    parser = argparse.ArgumentParser(
        description="RADOSGW address and local binding port as well as \
        S3 access_key and secret_key"
    )
    parser.add_argument(
        "-H",
        "--host",
        required=False,
        help="Server URL for the RADOSGW api (example: http://objects.dreamhost.com/)",
        default=os.environ.get("RADOSGW_SERVER", "http://radosgw:80"),
    )
    parser.add_argument(
        "-e",
        "--admin-entry",
        required=False,
        help="The entry point for an admin request URL [default is '%(default)s']",
        default=os.environ.get("ADMIN_ENTRY", "admin"),
    )
    parser.add_argument(
        "-a",
        "--access-key",
        required=False,
        help="S3 access key",
        default=os.environ.get("ACCESS_KEY", "NA"),
    )
    parser.add_argument(
        "-s",
        "--secret-key",
        required=False,
        help="S3 secret key",
        default=os.environ.get("SECRET_KEY", "NA"),
    )
    parser.add_argument(
        "-k",
        "--insecure",
        help="Allow insecure server connections when using SSL",
        action="store_false",
    )
    parser.add_argument(
        "-p",
        "--port",
        required=False,
        type=int,
        help="Port to listen",
        default=int(os.environ.get("VIRTUAL_PORT", "9242")),
    )
    parser.add_argument(
        "-S",
        "--store",
        required=False,
        help="Store name added to metrics",
        default=os.environ.get("STORE", "us-east-1"),
    )
    parser.add_argument(
        "-t",
        "--timeout",
        required=False,
        help="Per-request timeout in seconds when talking to RADOSGW",
        default=os.environ.get("TIMEOUT", "60"),
    )
    parser.add_argument(
        "-l",
        "--log-level",
        required=False,
        help="Provide logging level: DEBUG, INFO, WARNING, ERROR or CRITICAL",
        default=os.environ.get("LOG_LEVEL", "INFO"),
    )
    parser.add_argument(
        "-T",
        "--tag-list",
        required=False,
        help="Add bucket tags as label (example: 'tag1,tag2,tag3') ",
        default=os.environ.get("TAG_LIST", ""),
    )
    parser.add_argument(
        "-N",
        "--enable-namespace-extraction",
        required=False,
        help="Enable extraction of namespace from bucket owner and bucket name based on Rook user name generation.",
        default=os.environ.get("ENABLE_NAMESPACE_EXTRACTION", "false").lower() == "true",
    )
    parser.add_argument(
        "--obc-name-prefix",
        required=False,
        help="Prefix that may appear before obc.Name inside the bucket name when extracting namespace",
        default=os.environ.get("OBC_NAME_PREFIX", ""),
    )
    parser.add_argument(
        "-i",
        "--poll-interval",
        required=False,
        type=int,
        help="Seconds between background refreshes of RADOSGW data [default %(default)s]",
        default=int(os.environ.get("POLL_INTERVAL", "60")),
    )
    parser.add_argument(
        "-w",
        "--workers",
        required=False,
        type=int,
        help="Number of parallel workers for per-user / per-bucket requests [default %(default)s]",
        default=int(os.environ.get("WORKERS", "16")),
    )
    parser.add_argument(
        "-W",
        "--usage-window",
        required=False,
        type=int,
        help="Incremental usage look-back window in seconds; each poll only "
             "re-reads bins newer than this [default %(default)s]",
        default=int(os.environ.get("USAGE_WINDOW", "10800")),
    )
    parser.add_argument(
        "--no-seed-full",
        dest="seed_full",
        action="store_false",
        help="Do not read the full usage history on the first poll; start "
             "cumulative counters from the look-back window instead (faster "
             "startup, but the absolute counter value restarts from ~0).",
        default=os.environ.get("SEED_FULL", "true").lower() == "true",
    )
    parser.add_argument(
        "-r",
        "--retries",
        required=False,
        type=int,
        help="HTTP retry attempts per request (with backoff) [default %(default)s]",
        default=int(os.environ.get("RETRIES", "3")),
    )
    return parser.parse_args()

def get_bucket_namespace(bucket_name, bucket_owner, obc_name_prefix):
    """
    Extract a Kubernetes namespace from a Rook-generated RGW user/bucket name.

    Rook generates user names like:
        obc-<namespace>-<obcName>-<uuid>

    However, some deployments prepend a prefix to <obcName>, so:
        obc-<namespace>-<prefix><obcName>-<uuid>

    Parameters:
        bucket_name (str): The bucket name, which typically equals <obcName>.
        bucket_owner (str): The RGW user name, typically "obc-<ns>-<name>-<uuid>".
        obc_name_prefix (str): Optional prefix added before obcName.

    Returns:
        str: The extracted namespace, or empty string if it cannot be determined.
    """

    def _strip_uuid_suffix(value):
        """
        Remove trailing '-<uuid>' from strings where <uuid> is a standard 36-char UUID.
        """
        if not value:
            return value

        uuid_len = 36
        if len(value) <= uuid_len:
            return value

        possible_uuid = value[-uuid_len:]
        if re.match(r"^[0-9a-f-]{36}$", possible_uuid, re.IGNORECASE):
            # If there's a dash just before the UUID, remove it too
            cut_index = -uuid_len - 1 if value[-uuid_len - 1] == "-" else -uuid_len
            return value[:cut_index]

        return value

    # Ensure this is a Rook OBC-style user
    if not bucket_owner.startswith("obc-"):
        return ""

    # Remove the "obc-" prefix
    owner_no_prefix = _strip_uuid_suffix(bucket_owner[len("obc-"):])
    cleaned_bucket_name = _strip_uuid_suffix(bucket_name)

    # Construct possible suffix patterns
    # Example:
    #   "-mycustomprefixbucketname"
    #   "-bucketname"
    prefixed_bucket_suffix = f"-{obc_name_prefix}{cleaned_bucket_name}"
    bucket_suffix = f"-{cleaned_bucket_name}"

    # Case 1: Owner ends with prefix+bucket
    if owner_no_prefix.endswith(prefixed_bucket_suffix):
        return owner_no_prefix[: -len(prefixed_bucket_suffix)]

    # Case 2: Owner ends with bucket (no prefix)
    if owner_no_prefix.endswith(bucket_suffix):
        return owner_no_prefix[: -len(bucket_suffix)]

    # Case 3: Bucket occurs somewhere in the owner name
    # Look for prefixed version first
    idx = owner_no_prefix.find(prefixed_bucket_suffix)
    if idx != -1:
        return owner_no_prefix[:idx]

    # Fallback: plain bucket name
    idx = owner_no_prefix.find(bucket_suffix)
    if idx != -1:
        return owner_no_prefix[:idx]

    # Case 4: Fuzzy matching - look for bucket name components
    # Split bucket name by hyphens and try to find where these parts appear in owner
    bucket_parts = cleaned_bucket_name.split("-")

    # Try to find a sequence of bucket parts within the owner string
    # This helps when bucket name is "cdp-staging-mongodb-backup"
    # and owner is "obc-staging-cdp-mongodb-backup-<uuid>"
    for i in range(len(bucket_parts)):
        for j in range(i + 1, len(bucket_parts) + 1):
            # Try progressively longer substrings from the bucket name
            bucket_substring = "-".join(bucket_parts[i:j])
            search_pattern = f"-{bucket_substring}"

            idx = owner_no_prefix.find(search_pattern)
            if idx != -1:
                # Found a match - return everything before this match
                namespace = owner_no_prefix[:idx]
                # Make sure we have a valid namespace (at least one hyphen, meaning multiple parts)
                if namespace and "-" in namespace:
                    return namespace

    return ""

def main():
    try:
        args = parse_args()
        logging.basicConfig(level=args.log_level.upper())
        collector = RADOSGWCollector(
            args.host,
            args.admin_entry,
            args.access_key,
            args.secret_key,
            args.store,
            args.insecure,
            args.timeout,
            args.tag_list,
            args.enable_namespace_extraction,
            args.obc_name_prefix,
            args.poll_interval,
            args.workers,
            args.usage_window,
            args.seed_full,
            args.retries,
        )
        # Start the background poller before serving so scrapes are cache-only.
        collector.start()
        REGISTRY.register(collector)
        start_http_server(args.port, addr="::")
        logging.info(
            "Polling %s every %ss with %s workers. Serving at port: %s",
            args.host, args.poll_interval, args.workers, args.port,
        )
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logging.info("\nInterrupted")
        exit(0)


if __name__ == "__main__":
    main()
