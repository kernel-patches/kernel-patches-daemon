# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.


import asyncio
import copy
import datetime
import email.utils
import os
import re
import time
import unittest
from contextlib import contextmanager
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Callable, Dict, Iterator, List, Optional, Set, Union
from unittest.mock import patch

import aiohttp
from aioresponses import aioresponses, CallbackResult
from freezegun import freeze_time
from kernel_patches_daemon import patchwork
from kernel_patches_daemon.patchwork import (
    MAX_PAUSE_SECS,
    parse_tags,
    PatchworkUnavailable,
    RELEVANT_STATES,
    REQUEST_TIMEOUT_SECS,
    RETRY_BASE_DELAY_SECS,
    Subject,
    TTL,
    USER_AGENT,
)
from kernel_patches_daemon.status import Status
from multidict import MultiDict
from pyre_extensions import none_throws
from tests.common.patchwork_mock import (
    CHECKS_QUERY,
    DEFAULT_CHECK_CTX,
    DEFAULT_FREEZE_DATE,
    DEFAULT_TEST_RESPONSES,
    FOO_SERIES_FIRST,
    FOO_SERIES_LAST,
    get_default_pw_client,
    get_dict_key,
    init_pw_responses,
    PatchworkMock,
)
from tests.common.utils import load_test_data


@contextmanager
def fake_clock() -> Iterator[SimpleNamespace]:
    """
    Replace the clock of the Patchwork client and asyncio.sleep(). Only a
    sleep moves the clock.
    """
    clock = SimpleNamespace(now=0.0, sleeps=[])

    async def sleep(secs: float, *args: Any, **kwargs: Any) -> None:
        clock.sleeps.append(secs)
        clock.now += secs

    monotonic = SimpleNamespace(monotonic=lambda: clock.now)
    with patch("asyncio.sleep", sleep), patch.object(patchwork, "time", monotonic):
        yield clock


class PatchworkTestCase(unittest.IsolatedAsyncioTestCase):
    """
    Base TestCase class that ensure any patchwork related test cases are properly initialized.
    """

    async def asyncSetUp(self) -> None:
        self._pw = get_default_pw_client()


class TestPatchwork(PatchworkTestCase):
    @aioresponses()
    async def test_get_wrapper(self, m: aioresponses) -> None:
        """
        Simple test to ensure that GET requests are properly mocked.
        """
        pattern = re.compile(r"^.*$")
        m.get(pattern, status=200, body=b"""{"key1": "value1", "key2": 2}""")

        # pyrefly: ignore  # missing-attribute
        resp = await self._pw._Patchwork__get("object")
        m.assert_called_once()

        json_resp = await resp.json()
        self.assertEqual(json_resp["key1"], "value1")

    async def test_get_identifies_kpd(self) -> None:
        """
        GET requests carry the User-Agent, and the API token when one is set.
        """
        for token, authorization in [("mocktoken", "Token mocktoken"), (None, None)]:
            with self.subTest(token=token), aioresponses() as m:
                m.get(re.compile(r"^.*$"), status=200, body=b"{}")
                pw = get_default_pw_client(auth_token=token)
                # pyrefly: ignore  # missing-attribute
                await pw._Patchwork__get("object")
                # pyrefly: ignore  # missing-attribute
                headers = list(m.requests.values())[0][0].kwargs["headers"]
                self.assertEqual(headers["User-Agent"], USER_AGENT)
                self.assertEqual(headers.get("Authorization"), authorization)

    @aioresponses()
    async def test_get_limits_concurrent_requests(self, m: aioresponses) -> None:
        """
        At most max_concurrent_requests requests are in flight at one time.
        """
        in_flight = peak = 0

        async def respond(url, **kwargs) -> CallbackResult:
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.01)
            in_flight -= 1
            return CallbackResult(body=b"{}")

        m.get(re.compile(r"^.*$"), callback=respond, repeat=True)
        pw = get_default_pw_client(max_concurrent_requests=2, max_requests_per_sec=1000)
        # pyrefly: ignore  # missing-attribute
        get = pw._Patchwork__get
        await asyncio.gather(*[get(f"o/{i}/") for i in range(10)])
        self.assertEqual(peak, 2)

    @aioresponses()
    async def test_get_limits_request_rate(self, m: aioresponses) -> None:
        """
        At most max_requests_per_sec requests start per second.
        """
        starts = []
        pw = get_default_pw_client(max_requests_per_sec=5)
        # pyrefly: ignore  # missing-attribute
        get = pw._Patchwork__get
        with fake_clock() as clock:
            m.get(
                re.compile(r"^.*$"),
                repeat=True,
                callback=lambda url, **kwargs: starts.append(clock.now),
            )
            await asyncio.gather(*[get(f"o/{i}/") for i in range(8)])
        gaps = [b - a for a, b in zip(starts, starts[1:])]
        self.assertAlmostEqual(min(gaps), 1 / 5)

    async def test_get_backs_off(self) -> None:
        """
        A 5xx, 429, timeout or connection error pauses requests for the
        backoff delay, or for Retry-After (seconds or HTTP date) if that is
        longer. A GET tries once more after the pause, then raises
        PatchworkUnavailable.
        """
        url = "https://127.0.0.1/api/1.1/object"
        in_30s = email.utils.format_datetime(
            datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(seconds=30),
            usegmt=True,
        )
        delay = RETRY_BASE_DELAY_SECS
        timeout, refused = asyncio.TimeoutError(), aiohttp.ClientConnectionError()
        for responses, retry_after, sleeps in [
            ([503, 200], "30", [30]),
            ([429, 200], in_30s, [30]),
            ([503, 200], "0", [delay]),
            ([timeout, 200], "", [delay]),
            ([500, refused], "", [delay]),
        ]:
            with (
                self.subTest(responses=responses),
                aioresponses() as m,
                fake_clock() as clock,
            ):
                for response in responses:
                    if isinstance(response, Exception):
                        m.get(url, exception=response)
                    else:
                        headers = {"Retry-After": retry_after}
                        m.get(url, status=response, headers=headers)
                # pyrefly: ignore  # missing-attribute
                get = get_default_pw_client()._Patchwork__get("object")
                if responses[-1] == 200:
                    await get
                else:
                    with self.assertRaises(PatchworkUnavailable):
                        await get
                self.assertEqual(len(clock.sleeps), len(sleeps))
                for actual, expected in zip(clock.sleeps, sleeps):
                    self.assertAlmostEqual(actual, expected, delta=1)

    @aioresponses()
    async def test_403_pauses_all_requests(self, m: aioresponses) -> None:
        """
        A 403 is not retried, and no later request is sent before Retry-After.
        """
        url = "https://127.0.0.1/api/1.1/object"
        m.get(url, status=403, headers={"Retry-After": "60"})
        m.get(url, status=200, body=b"{}")
        # pyrefly: ignore  # missing-attribute
        get = self._pw._Patchwork__get
        with fake_clock() as clock:
            with self.assertRaises(PatchworkUnavailable):
                await get("object")
            self.assertEqual(clock.sleeps, [])
            await get("object")
        self.assertEqual(clock.sleeps, [60])

    @aioresponses()
    async def test_backoff_doubles_until_a_good_response(self, m: aioresponses) -> None:
        url = "https://127.0.0.1/api/1.1/object"
        for status in (500, 500, 200, 500, 200):
            m.get(url, status=status, body=b"{}")
        # pyrefly: ignore  # missing-attribute
        get = self._pw._Patchwork__get
        with fake_clock() as clock:
            with self.assertRaises(PatchworkUnavailable):
                await get("object")
            await get("object")
            await get("object")
        # The third request waits for the rate limit, not for a pause.
        self.assertEqual([round(s, 3) for s in clock.sleeps], [5, 10, 0.1, 5])

    @aioresponses()
    async def test_queued_requests_wait_for_the_pause(self, m: aioresponses) -> None:
        """
        Requests that wait for a slot when a 503 arrives also wait for the pause.
        """
        sent, failed_at = [], []

        async def respond(url, **kwargs) -> CallbackResult:
            first = not sent
            sent.append(time.monotonic())
            await asyncio.sleep(0.01)
            if first:
                failed_at.append(time.monotonic())
                return CallbackResult(status=503)
            return CallbackResult(body=b"{}")

        m.get(re.compile(r"^.*$"), callback=respond, repeat=True)
        # pyrefly: ignore  # missing-attribute
        get = self._pw._Patchwork__get
        with patch.object(patchwork, "RETRY_BASE_DELAY_SECS", 0.2):
            await asyncio.gather(*[get(f"o/{i}/") for i in range(6)])
        # After the 503, the 2 queued requests and the retry waited.
        self.assertEqual(len(sent), 7)
        self.assertGreaterEqual(min(sent[4:]), failed_at[0] + 0.2)

    @aioresponses()
    async def test_long_pause_raises_at_once(self, m: aioresponses) -> None:
        """
        A GET does not wait for a long pause: it raises, and sends nothing.
        The pause is not longer than MAX_PAUSE_SECS.
        """
        m.get(re.compile(r"^.*$"), status=503, headers={"Retry-After": "86400"})
        # pyrefly: ignore  # missing-attribute
        get = self._pw._Patchwork__get
        with fake_clock() as clock:
            with self.assertRaises(PatchworkUnavailable):
                await get("object")
            with self.assertRaises(PatchworkUnavailable):
                await get("object")
        self.assertEqual(self._pw.next_request_at, MAX_PAUSE_SECS)
        self.assertEqual(clock.sleeps, [])
        m.assert_called_once()

    async def test_post_failures(self) -> None:
        """
        A POST is not retried. On 5xx KPD skips it, because the next cycle
        posts it again. 403 and 429 raise.
        """
        for status, raises in [(503, False), (429, True), (403, True)]:
            with self.subTest(status=status), aioresponses() as m:
                m.post(re.compile(r"^.*$"), status=status)
                # pyrefly: ignore  # missing-attribute
                post = get_default_pw_client()._Patchwork__post("object", {})
                if raises:
                    with self.assertRaises(PatchworkUnavailable):
                        await post
                else:
                    self.assertIsNone(await post)
                m.assert_called_once()

    async def test_requests_time_out(self) -> None:
        session = await self._pw.get_http_session()
        self.assertEqual(session.timeout.total, REQUEST_TIMEOUT_SECS)

    @aioresponses()
    async def test_post_wrapper(self, m: aioresponses) -> None:
        """
        Simple test to ensure that POST requests are properly mocked.
        """
        pattern = re.compile(r"^.*$")
        m.post(pattern, status=200, body=b"""{"key1": "value1", "key2": 2}""")
        # Make sure user and token are set so the requests is actually posted.
        # pyrefly: ignore  # missing-attribute
        self._pw.pw_token = "1234567890"
        # pyrefly: ignore  # missing-attribute
        self._pw.pw_user = "somerandomuser"
        # pyrefly: ignore  # missing-attribute
        resp = await self._pw._Patchwork__post("some/random/url", "somerandomdata")
        m.assert_called_once()

        json_resp = await resp.json()
        self.assertEqual(json_resp["key1"], "value1")

    async def test_get_objects_recursive(self) -> None:
        url = "https://127.0.0.1/api/1.1/projects/"

        @dataclass
        class Response:
            body: bytes
            headers: Dict[str, str]
            url: str = "https://127.0.0.1/api/1.1/projects/"
            status_code: int = 200

        @dataclass
        class TestCase:
            name: str
            pages: List[Response]
            expected: List[Any]
            get_calls: int
            filters: Optional[Union[Dict[str, str], MultiDict]] = None

        test_cases = [
            TestCase(
                name="single page",
                pages=[Response(url=url, body=b'["a","b","c"]', headers={})],
                expected=["a", "b", "c"],
                get_calls=1,
            ),
            TestCase(
                name="Multiple pages with proper formatting",
                pages=[
                    Response(
                        url=url,
                        body=b'["a"]',
                        headers={
                            "Link": '<https://127.0.0.1/api/1.1/projects/?page=2>; rel="next"'
                        },
                    ),
                    Response(
                        url=url + "?page=2",
                        body=b'["b"]',
                        headers={
                            "Link": '<https://127.0.0.1/api/1.1/projects/?page=3>; rel="next", <https://127.0.0.1/api/1.1/projects/>; rel="prev"'
                        },
                    ),
                    Response(
                        url=url + "?page=3",
                        body=b'["c"]',
                        headers={
                            "Link": '<https://127.0.0.1/api/1.1/projects/?page=2>; rel="prev"'
                        },
                    ),
                ],
                expected=["a", "b", "c"],
                get_calls=3,
            ),
            TestCase(
                name="single page with single filters",
                pages=[
                    Response(
                        url=url + "?k1=v1&k2=v2", body=b'["a","b","c"]', headers={}
                    )
                ],
                expected=["a", "b", "c"],
                get_calls=1,
                filters={"k1": "v1", "k2": "v2"},
            ),
            TestCase(
                name="single page with list filters",
                pages=[
                    Response(
                        url=url + "?k1=v1&k2=v2&k2=v2.2",
                        body=b'["a","b","c"]',
                        headers={},
                    )
                ],
                expected=["a", "b", "c"],
                get_calls=1,
                filters=MultiDict([("k1", "v1"), ("k2", "v2"), ("k2", "v2.2")]),
            ),
        ]

        for case in test_cases:
            with self.subTest(msg=case.name):
                with aioresponses() as m:
                    for resp in case.pages:
                        m.get(
                            resp.url,
                            status=resp.status_code,
                            headers=resp.headers,
                            body=resp.body,
                        )

                    # pyrefly: ignore  # missing-attribute
                    resp = await self._pw._Patchwork__get_objects_recursive(
                        "projects", params=case.filters
                    )
                    self.assertEqual(resp, case.expected)
                    self.assertEqual(
                        # pyrefly: ignore  # missing-attribute
                        sum([len(x) for x in m.requests.values()]),
                        case.get_calls,
                    )

    @aioresponses()
    async def test_get_objects_recursive_no_param_duplication(
        self, m: aioresponses
    ) -> None:
        """Regression test: query params must not be duplicated across pages.

        The `next` link returned by Patchwork already contains all the query
        parameters. They must not be re-appended on the follow-up request, or
        every filter would be duplicated on each page, growing the URL until
        the server rejects it.
        """
        base = "https://127.0.0.1/api/1.1/projects/"
        # Page 1: filtered request; its `next` link already carries the filters.
        m.get(
            base + "?k1=v1&k2=v2",
            status=200,
            headers={"Link": f'<{base}?k1=v1&k2=v2&page=2>; rel="next"'},
            body=b'["a"]',
        )
        # Page 2: must be requested verbatim, with each filter present once.
        m.get(
            base + "?k1=v1&k2=v2&page=2",
            status=200,
            headers={},
            body=b'["b"]',
        )

        # pyrefly: ignore  # missing-attribute
        resp = await self._pw._Patchwork__get_objects_recursive(
            "projects", params={"k1": "v1", "k2": "v2"}
        )

        self.assertEqual(resp, ["a", "b"])
        # No request may carry a duplicated query parameter.
        # pyrefly: ignore  # not-iterable
        for method, req_url in m.requests:
            for param in ("k1", "k2"):
                self.assertLessEqual(
                    len(req_url.query.getall(param, [])),
                    1,
                    f"param {param!r} duplicated in {req_url}",
                )

    @aioresponses()
    async def test_try_post_nocred_nomutation(self, m: aioresponses) -> None:
        """
        When pw_token is not set or is an empty string, we will not call post.
        """
        pattern = re.compile(r"^.*$")
        m.post(pattern, status=200)
        self._pw.auth_token = None
        # pyrefly: ignore  # missing-attribute
        await self._pw._Patchwork__try_post(
            "https://127.0.0.1/some/random/url", "somerandomdata"
        )
        m.assert_not_called()

        self._pw.auth_token = ""
        # pyrefly: ignore  # missing-attribute
        await self._pw._Patchwork__try_post(
            "https://127.0.0.1/some/random/url", "somerandomdata"
        )
        m.assert_not_called()

    def test_search_patterns_need_one_project(self) -> None:
        for patterns in ([{"delegate": 1}], [{"project": 1}, {"project": 2}]):
            with self.subTest(patterns=patterns), self.assertRaises(ValueError):
                get_default_pw_client(search_patterns=patterns)

    def test_format_since(self) -> None:
        @dataclass
        class TestCase:
            name: str
            now: str
            expected: str
            lookback: int = 3

        PDT_UTC_OFFSET = -7
        test_cases = [
            TestCase(
                name="1st of Jan 00:00 UTC",
                now="2010-01-01T00:00:00",
                expected="2009-12-29T00:00:00",
            ),
            TestCase(
                name="1st of Jan 00:00 PDT",
                now=f"2010-01-01T00:00:00{PDT_UTC_OFFSET:+03}:00",
                expected="2009-12-29T00:00:00",
            ),
            TestCase(
                name="1st of Jan 23:00 UTC",
                now="2010-01-01T23:00:00",
                expected="2009-12-29T00:00:00",
            ),
        ]
        for case in test_cases:
            with self.subTest(msg=case.name):
                # Force local time to be PDT
                with freeze_time(case.now, tz_offset=PDT_UTC_OFFSET):
                    self.assertEqual(
                        self._pw.format_since(case.lookback),
                        case.expected,
                    )

    def test_parse_tags(self) -> None:
        @dataclass
        class TestCase:
            name: str
            title: str
            expected: Set[str]

        test_cases = [
            TestCase(
                name="title without tags",
                title="title",
                expected=set(),
            ),
            TestCase(
                name="title with all valid tags",
                title="[tag1, tag2]title",
                expected={"tag1", "tag2"},
            ),
            TestCase(
                name="title without ignorable tags",
                title="[tag, 1/18, v4]title",
                expected={"tag"},
            ),
            TestCase(
                name="tags with extra spaces",
                title="[tag3, tag1  , tag2]title",
                expected={"tag1", "tag2", "tag3"},
            ),
        ]
        for case in test_cases:
            with self.subTest(msg=case.name):
                self.assertEqual(
                    parse_tags(case.title),
                    case.expected,
                )

    async def _test_lookback(
        self, m: aioresponses, lookback: int, assert_func: Callable[[str], None]
    ) -> None:
        self._pw = get_default_pw_client(lookback_in_days=lookback)
        m.get(re.compile(r"^.*$"), status=200, body=b"[]")
        await self._pw.get_relevant_subjects()
        # pyrefly: ignore  # missing-attribute
        for request in m.requests.keys():
            url = str(request[1])
            assert_func(url)

    @aioresponses()
    async def test_get_objects_with_lookback(self, m: aioresponses) -> None:
        """
        If lookback is > 0, the 'since' query parameter must be present in the requests.
        """
        await self._test_lookback(m, 7, lambda url: self.assertIn("since=", url))

    @aioresponses()
    async def test_get_objects_no_lookback(self, m: aioresponses) -> None:
        """
        If lookback is <= 0, the 'since' query parameter must not be present in the requests.
        """
        await self._test_lookback(m, -1, lambda url: self.assertNotIn("since=", url))

    @aioresponses()
    async def test_lookback_moves_with_time(self, m: aioresponses) -> None:
        """
        The client is used for many cycles, so each search computes 'since' again.
        """
        self._pw = get_default_pw_client(lookback_in_days=7)
        m.get(re.compile(r"^.*$"), status=200, body=b"[]", repeat=True)
        for today, since in [
            ("2010-01-10", "2010-01-03"),
            ("2010-01-11", "2010-01-04"),
        ]:
            with freeze_time(today):
                await self._pw.get_relevant_subjects()
            # pyrefly: ignore  # missing-attribute
            self.assertIn(f"since={since}", str(list(m.requests)[-1][1]))


class TestSeries(PatchworkTestCase):
    @aioresponses()
    async def test_series_closed(self, m: aioresponses) -> None:
        """
        If one of the patch is irrelevant, the series is closed.
        """
        init_pw_responses(m, DEFAULT_TEST_RESPONSES)
        series = await self._pw.get_series_by_id(666)
        self.assertTrue(await series.is_closed())

    @aioresponses()
    async def test_series_not_closed(self, m: aioresponses) -> None:
        """
        If none of the patches are irrelevant, the series is not closed.
        """
        init_pw_responses(m, DEFAULT_TEST_RESPONSES)
        series = await self._pw.get_series_by_id(665)
        self.assertFalse(await series.is_closed())

    @aioresponses()
    async def test_series_tags(self, m: aioresponses) -> None:
        """
        Series tags are extracted from the diffs/cover letter/serie's name. We extract the content
        from the square bracket content prefixing those names and filter some out.
        """
        init_pw_responses(m, DEFAULT_TEST_RESPONSES)
        series = await self._pw.get_series_by_id(665)
        self.assertEqual(
            await series.all_tags(),
            {
                # cover letter tags
                "cover letter tag",
                "duplicate tag",
                # relevant states
                get_dict_key(RELEVANT_STATES),
                get_dict_key(RELEVANT_STATES, 1),
                # series version
                "V4",
                # Series name
                "a/b",
                # patches
                "first patch tag",
                "second patch tag",
                "some stuff with spaces",
            },
        )

    @aioresponses()
    async def test_series_visible_tags(self, m: aioresponses) -> None:
        """
        Series' visible tags are only taken from patch states and series version.
        """
        init_pw_responses(m, DEFAULT_TEST_RESPONSES)
        series = await self._pw.get_series_by_id(665)
        self.assertEqual(
            await series.visible_tags(),
            {
                # relevant states
                get_dict_key(RELEVANT_STATES),
                get_dict_key(RELEVANT_STATES, 1),
                # series version
                "V4",
            },
        )

    @aioresponses()
    async def test_series_tags_handle_missing_values(self, m: aioresponses) -> None:
        """
        Test that we handle correctly series with empty cover_letter and/or no attaches patches.
        """
        init_pw_responses(m, DEFAULT_TEST_RESPONSES)
        series = await self._pw.get_series_by_id(667)
        self.assertEqual(
            await series.all_tags(),
            {
                "V1",
            },
        )

    @aioresponses()
    async def test_series_patch_subjects(self, m: aioresponses) -> None:
        """
        Series patch subjects are extracted from all the non-cover letter patches in a series.

        Series tags are extracted from the diffs/cover letter/serie's name. We extract the content
        from the square bracket content prefixing those names and filter some out.
        """
        init_pw_responses(m, DEFAULT_TEST_RESPONSES)
        series = await self._pw.get_series_by_id(665)
        self.assertEqual(
            await series.patch_subjects(),
            [
                "foo",
                "bar",
                "foo",
            ],
        )
        # One request for the series, and one for all of its patches.
        self.assertEqual(
            # pyrefly: ignore  # not-iterable
            [str(url) for _, url in m.requests],
            [
                "https://127.0.0.1/api/1.1/series/665/",
                "https://127.0.0.1/api/1.1/patches/?project=1234&series=665",
            ],
        )

    @aioresponses()
    async def test_series_is_expired(self, m: aioresponses) -> None:
        """
        Test that when we are passed expiration date, the series is reported expired.
        """
        init_pw_responses(m, DEFAULT_TEST_RESPONSES)
        series = await self._pw.get_series_by_id(668)
        ttl = TTL[get_dict_key(TTL)]
        # take the date of the patch and move to 1 second after that.
        now = datetime.datetime.fromisoformat(DEFAULT_FREEZE_DATE) + datetime.timedelta(
            seconds=ttl + 1
        )
        with freeze_time(now):
            self.assertTrue(await series.is_expired())

    @aioresponses()
    async def test_series_is_not_expired(self, m: aioresponses) -> None:
        """
        Test that when we are not yet passed expiration date, the series is not reported expired.
        """
        init_pw_responses(m, DEFAULT_TEST_RESPONSES)
        series = await self._pw.get_series_by_id(668)
        ttl = TTL[get_dict_key(TTL)]
        # take the date of the patch and move to 1 second before that.
        now = datetime.datetime.fromisoformat(DEFAULT_FREEZE_DATE) + datetime.timedelta(
            seconds=ttl - 1
        )
        with freeze_time(now):
            self.assertFalse(await series.is_expired())

    @aioresponses()
    async def test_series_checks_update_all_diffs(self, m: aioresponses) -> None:
        """
        Test that we update all the diffs in a serie if there is either no existing check
        or checks need update.
        """
        contexts_responses = copy.deepcopy(DEFAULT_TEST_RESPONSES)
        contexts_responses.update(
            {
                # Patch with existing context
                f"https://127.0.0.1/api/1.1/patches/6651/checks/{CHECKS_QUERY}": [
                    {
                        "context": DEFAULT_CHECK_CTX,
                        "date": "2010-01-01T00:00:00",
                        "state": "pending",
                    },
                ],
                # Patches without existing context
                f"https://127.0.0.1/api/1.1/patches/6652/checks/{CHECKS_QUERY}": [],
                f"https://127.0.0.1/api/1.1/patches/6653/checks/{CHECKS_QUERY}": [],
            }
        )

        init_pw_responses(m, contexts_responses)

        post_pattern = re.compile(r"^.*$")
        m.post(post_pattern, status=200, repeat=True)

        series = await self._pw.get_series_by_id(665)
        await series.set_check(
            context=DEFAULT_CHECK_CTX,
            status=Status.PENDING,
            target_url="https://127.0.0.1/target",
        )
        # Hack... aioresponses stores the requests that were made in a dictionary whose's key is a tuple,
        # of the form (method, url).
        # https://github.com/pnuckowski/aioresponses/blob/56b843319d5d0ae8a405f188e68d7ba8c7573bc8/aioresponses/core.py#L507
        # pyrefly: ignore  # missing-attribute
        self.assertEqual(len([x for x in m.requests.keys() if x[0] == "POST"]), 3)

        # The client remembers the checks, so the same update sends nothing.
        # pyrefly: ignore  # missing-attribute
        requests = sum(len(calls) for calls in m.requests.values())
        await series.set_check(
            context=DEFAULT_CHECK_CTX,
            status=Status.PENDING,
            target_url="https://127.0.0.1/target",
        )
        # pyrefly: ignore  # missing-attribute
        self.assertEqual(sum(len(calls) for calls in m.requests.values()), requests)

    @aioresponses()
    async def test_series_checks_list_each_patch_once(self, m: aioresponses) -> None:
        """
        Checks of several contexts posted at the same time list the checks of
        each patch once.
        """
        responses = copy.deepcopy(DEFAULT_TEST_RESPONSES)
        for patch_id in (6651, 6652, 6653):
            url = f"https://127.0.0.1/api/1.1/patches/{patch_id}/checks/{CHECKS_QUERY}"
            responses[url] = []
        init_pw_responses(m, responses)
        m.post(re.compile(r"^.*$"), status=200, repeat=True)
        series = await self._pw.get_series_by_id(665)
        url = "https://127.0.0.1/target"
        await asyncio.gather(
            series.set_check(context="a", status=Status.PENDING, target_url=url),
            series.set_check(context="b", status=Status.PENDING, target_url=url),
        )
        listings = [
            len(calls)
            # pyrefly: ignore  # missing-attribute
            for (method, u), calls in m.requests.items()
            if method == "GET" and "/checks/" in str(u)
        ]
        self.assertEqual(listings, [1, 1, 1])

    @aioresponses()
    async def test_series_checks_no_update_same_state_target(
        self, m: aioresponses
    ) -> None:
        """
        Test that we don't update checks if the state and target_url have not changed.
        """
        TARGET_URL = "https://127.0.0.1/target"
        contexts_responses = copy.deepcopy(DEFAULT_TEST_RESPONSES)
        contexts_responses.update(
            {
                # Patch with existing context
                f"https://127.0.0.1/api/1.1/patches/6651/checks/{CHECKS_QUERY}": [
                    {
                        "context": DEFAULT_CHECK_CTX,
                        "date": "2010-01-01T00:00:00",
                        "state": "pending",
                        "target_url": TARGET_URL,
                    },
                ],
                # Patches without existing context
                f"https://127.0.0.1/api/1.1/patches/6652/checks/{CHECKS_QUERY}": [],
                f"https://127.0.0.1/api/1.1/patches/6653/checks/{CHECKS_QUERY}": [],
            }
        )

        init_pw_responses(m, contexts_responses)

        post_pattern = re.compile(r"^.*$")
        m.post(post_pattern, status=200, repeat=True)

        series = await self._pw.get_series_by_id(665)
        await series.set_check(
            context=DEFAULT_CHECK_CTX, status=Status.PENDING, target_url=TARGET_URL
        )
        # First patch is not updates
        self.assertEqual(
            # pyrefly: ignore  # missing-attribute
            len([x for x in m.requests.keys() if x[0] == "POST"]),
            len(series.patches) - 1,
        )

    @aioresponses()
    async def test_series_checks_update_same_state_diff_target(
        self, m: aioresponses
    ) -> None:
        """
        Test that we update checks if the state is the same, but target_url has changed.
        """
        TARGET_URL = "https://127.0.0.1/target"
        contexts_responses = copy.deepcopy(DEFAULT_TEST_RESPONSES)
        contexts_responses.update(
            {
                # Patch with existing context
                f"https://127.0.0.1/api/1.1/patches/6651/checks/{CHECKS_QUERY}": [
                    {
                        "context": DEFAULT_CHECK_CTX,
                        "date": "2010-01-01T00:00:00",
                        "state": "pending",
                        # target_url not matching
                        "target_url": TARGET_URL + "something",
                    },
                    # Older, so it does not count.
                    {
                        "context": DEFAULT_CHECK_CTX,
                        "date": "2009-01-01T00:00:00",
                        "state": "pending",
                        "target_url": TARGET_URL,
                    },
                ],
                # Patches without existing context
                f"https://127.0.0.1/api/1.1/patches/6652/checks/{CHECKS_QUERY}": [],
                f"https://127.0.0.1/api/1.1/patches/6653/checks/{CHECKS_QUERY}": [],
            }
        )

        init_pw_responses(m, contexts_responses)

        post_pattern = re.compile(r"^.*$")
        m.post(post_pattern, status=200, repeat=True)

        series = await self._pw.get_series_by_id(665)
        await series.set_check(
            context=DEFAULT_CHECK_CTX, status=Status.PENDING, target_url=TARGET_URL
        )
        # First patch is not updates
        self.assertEqual(
            # pyrefly: ignore  # missing-attribute
            len([x for x in m.requests.keys() if x[0] == "POST"]),
            len(series.patches),
        )

    @aioresponses()
    async def test_series_checks_update_diff_state_same_target(
        self, m: aioresponses
    ) -> None:
        """
        Test that we update checks if the state is not the same, but target_url is.
        """
        TARGET_URL = "https://127.0.0.1/target"
        contexts_responses = copy.deepcopy(DEFAULT_TEST_RESPONSES)
        contexts_responses.update(
            {
                # Patch with existing context
                f"https://127.0.0.1/api/1.1/patches/6651/checks/{CHECKS_QUERY}": [
                    {
                        "context": DEFAULT_CHECK_CTX,
                        "date": "2010-01-01T00:00:00",
                        "state": "pending",
                        "target_url": TARGET_URL,
                    },
                    {
                        "context": "other context",
                    },
                ],
                # Patches without existing context
                f"https://127.0.0.1/api/1.1/patches/6652/checks/{CHECKS_QUERY}": [],
                f"https://127.0.0.1/api/1.1/patches/6653/checks/{CHECKS_QUERY}": [],
            }
        )

        init_pw_responses(m, contexts_responses)

        post_pattern = re.compile(r"^.*$")
        m.post(post_pattern, status=200, repeat=True)

        series = await self._pw.get_series_by_id(665)
        # success is a conclusive (non-pending) state.
        await series.set_check(
            context=DEFAULT_CHECK_CTX, status=Status.SUCCESS, target_url=TARGET_URL
        )
        # First patch is not updates
        self.assertEqual(
            # pyrefly: ignore  # missing-attribute
            len([x for x in m.requests.keys() if x[0] == "POST"]),
            len(series.patches),
        )

    @aioresponses()
    async def test_series_checks_no_update_diff_pending_state(
        self, m: aioresponses
    ) -> None:
        """
        Test that we do not update checks if the new state is pending and we have an existing final state.
        """
        TARGET_URL = "https://127.0.0.1/target"
        contexts_responses = copy.deepcopy(DEFAULT_TEST_RESPONSES)
        contexts_responses.update(
            {
                # Patch with existing context
                f"https://127.0.0.1/api/1.1/patches/6651/checks/{CHECKS_QUERY}": [
                    {
                        "context": DEFAULT_CHECK_CTX,
                        "date": "2010-01-01T00:00:00",
                        # conclusive state.
                        "state": "success",
                        "target_url": TARGET_URL,
                    },
                ],
                # Patches without existing context
                f"https://127.0.0.1/api/1.1/patches/6652/checks/{CHECKS_QUERY}": [],
                f"https://127.0.0.1/api/1.1/patches/6653/checks/{CHECKS_QUERY}": [],
            }
        )

        init_pw_responses(m, contexts_responses)

        post_pattern = re.compile(r"^.*$")
        m.post(post_pattern, status=200, repeat=True)

        series = await self._pw.get_series_by_id(665)
        await series.set_check(
            context=DEFAULT_CHECK_CTX, status=Status.PENDING, target_url=TARGET_URL
        )
        # First patch is not updates
        self.assertEqual(
            # pyrefly: ignore  # missing-attribute
            len([x for x in m.requests.keys() if x[0] == "POST"]),
            len(series.patches) - 1,
        )


class TestSubject(PatchworkTestCase):
    @freeze_time(DEFAULT_FREEZE_DATE, tick=True)
    @aioresponses()
    async def test_relevant_series(self, m: aioresponses) -> None:
        """
        Test that we find the relevant series for a given subject.
        """
        init_pw_responses(m, DEFAULT_TEST_RESPONSES)

        s = Subject("foo", self._pw)

        # There is 2 relevant series
        relevant_series = await s.relevant_series()
        self.assertEqual(len(relevant_series), 4)

        # Series are ordered from oldest to newest
        series = relevant_series[0]
        self.assertEqual(series.id, FOO_SERIES_FIRST)
        # It has only 1 diff, diff 11
        self.assertEqual(len(await series.get_patches()), 1)
        self.assertEqual([patch["id"] for patch in await series.get_patches()], [11])

    @aioresponses()
    async def test_relevant_series_cached_per_subject(self, m: aioresponses) -> None:
        """
        Looking up the series of another subject does not evict the cached
        series of a subject.
        """
        init_pw_responses(m, DEFAULT_TEST_RESPONSES)
        foo, bar = Subject("foo", self._pw), Subject("bar", self._pw)
        for subject in (foo, bar, foo):
            await subject.relevant_series()
        # pyrefly: ignore  # missing-attribute
        searches = [calls for (_, url), calls in m.requests.items() if "q=" in str(url)]
        self.assertEqual(sum(map(len, searches)), 2)

    @freeze_time(DEFAULT_FREEZE_DATE, tick=True)
    @aioresponses()
    async def test_latest_series(self, m: aioresponses) -> None:
        """
        Test that latest_series only returns.... the latest serie.
        """
        init_pw_responses(m, DEFAULT_TEST_RESPONSES)

        s = Subject("foo", self._pw)
        latest_series = none_throws(await s.latest_series())
        # It is Series with ID FOO_SERIES_LAST
        self.assertEqual(latest_series.id, FOO_SERIES_LAST)
        # and has 3 diffs
        self.assertEqual(len(await latest_series.get_patches()), 3)

    @freeze_time(DEFAULT_FREEZE_DATE, tick=True)
    @aioresponses()
    async def test_branch_name(self, m: aioresponses) -> None:
        """
        Test that the branch name is made using the first series ID in the list of relevant series.
        """
        init_pw_responses(m, DEFAULT_TEST_RESPONSES)

        s = Subject("foo", self._pw)
        branch = await s.branch()
        # It is Series with ID 4
        self.assertEqual(branch, f"series/{FOO_SERIES_LAST}")

    @aioresponses()
    async def test_latest_series_real_sample(self, m: aioresponses) -> None:
        data_path = os.path.join(
            os.path.dirname(__file__),
            "data/test_latest_series_real_sample",
        )
        test_data = load_test_data(data_path)
        init_pw_responses(m, test_data)

        patchwork = PatchworkMock(
            server="patchwork.test",
            api_version="1.1",
            search_patterns=[{"archived": False, "project": 399, "delegate": 121173}],
            auth_token="mocktoken",
        )
        s = Subject("Support kCFI + BPF on arm64", patchwork)

        latest_series = none_throws(await s.latest_series())
        branch = none_throws(await s.branch())

        self.assertEqual(latest_series.id, 984880)
        self.assertEqual(branch, "series/984880")
