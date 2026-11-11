# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.


import asyncio
import datetime
import email.utils
import json
import logging
import re
import time
from functools import update_wrapper
from types import SimpleNamespace
from typing import Any, AnyStr, Dict, Final, List, Optional, Sequence, Set, Tuple
from urllib.parse import urljoin

import aiohttp
import cachetools.keys
import dateutil.parser as dateparser
from cachetools import LRUCache, TTLCache
from kernel_patches_daemon.config import (
    DEFAULT_MAX_CONCURRENT_REQUESTS,
    DEFAULT_MAX_REQUESTS_PER_SEC,
    SERIES_ID_SEPARATOR,
)
from kernel_patches_daemon.status import Status
from kernel_patches_daemon.utils import gather_or_cancel
from multidict import MultiDict
from opentelemetry import metrics
from pyre_extensions import none_throws

# After failures in a row, pause requests for 5 s, then 10 s, 20 s and so on,
# or longer if Retry-After asks for it.
RETRY_BASE_DELAY_SECS: Final[int] = 5
# A request waits for a pause up to this long. A longer pause makes it raise
# PatchworkUnavailable, so that the daemon waits instead.
MAX_WAIT_SECS: Final[int] = 60
# No pause is longer than this, whatever Retry-After asks for.
MAX_PAUSE_SECS: Final[int] = 3600
REQUEST_TIMEOUT_SECS: Final[int] = 60
# For how many patches the client remembers the checks, see
# post_check_for_patch_id().
CHECK_CACHE_SIZE: Final[int] = 1024
# How many objects each cached Series and Subject method keeps results for.
# The objects compare by identity, so each one has its own entry.
OBJECT_CACHE_SIZE: Final[int] = 4096
# patchwork.kernel.org serves at most this many items per page.
MAX_PAGE_SIZE: Final[int] = 250
# How much of a response body that is not JSON to log: all of a typical
# error page.
LOGGED_BODY_BYTES: Final[int] = 512
# Tells the Patchwork admins who sends the requests and how to reach us.
USER_AGENT: Final[str] = (
    "kernel-patches-daemon/1.0 "
    "(+https://github.com/kernel-patches/kernel-patches-daemon; kernel-ci@meta.com)"
)

# when we want to push this patch through CI
RELEVANT_STATES: Dict[str, int] = {
    "new": 1,
    "under-review": 2,
    "rfc": 5,
    "changes-requested": 7,
    "queued": 13,
    "needs-ack": 15,
}
RFC_TAG: str = "RFC"
# with these tags will be closed if no updates within TTL
TTL = {"changes-requested": 3600, "rfc": 3600}

# when we are not interested in this patch anymore
IRRELEVANT_STATES: Dict[str, int] = {
    "rejected": 4,
    "accepted": 3,
    "not-applicable": 6,
    "superseded": 9,
    "awaiting-upstream": 8,
    "deferred": 10,
    "mainlined": 11,
    "handled-elsewhere": 17,
}

PW_CHECK_STATES: Dict[Status, str] = {
    Status.PENDING: "pending",
    Status.SUCCESS: "success",
    Status.SKIPPED: "success",
    Status.FAILURE: "fail",
    Status.CONFLICT: "fail",
}

SUBJECT_REGEXP: Final[re.Pattern] = re.compile(r"(?P<header>\[[^\]]*\])? *(?P<name>.+)")
IGNORE_TAGS_REGEX: Final[re.Pattern] = re.compile(
    r"([0-9]+/[0-9]+|V[0-9]+)|patch", re.IGNORECASE
)
TAG_REGEXP: Final[re.Pattern] = re.compile(r"^(\[(?P<tags>[^]]*)\])*")
PATCH_FILTERING_PROPERTIES = {"project", "delegate"}

logger: logging.Logger = logging.getLogger(__name__)
meter: metrics.Meter = metrics.get_meter("patchwork")

api_requests: metrics.Counter = meter.create_counter(name="requests.status")
api_requests_time: metrics.Histogram = meter.create_histogram(
    name="requests.duration_ms"
)
err_tag_parsing_failures: metrics.Counter = meter.create_counter(
    name="errors.tag_parsing_failures"
)
err_malformed_series: metrics.Counter = meter.create_counter(
    name="errors.malformed_series"
)


CHECK_CONTEXT_NOT_ALLOWED_CHARS_RE: Final[re.Pattern] = re.compile(r"[^-a-zA-Z0-9_]+")
MULTIPLE_UNDERSCORES_RE: Final[re.Pattern] = re.compile(r"_+")


def slugify_check_context(check_context: str) -> str:
    """
    Slugify provided string, so that it can be used as a "context" field of a patchwork check.
    See: https://github.com/getpatchwork/patchwork/blob/v3.1.3/docs/api/schemas/latest/patchwork.yaml#L1579
    """
    result = CHECK_CONTEXT_NOT_ALLOWED_CHARS_RE.sub("_", check_context)
    result = MULTIPLE_UNDERSCORES_RE.sub("_", result)
    result = result.strip("_")

    return result


## Request Tracing
class TraceContext(SimpleNamespace):
    """
    aiohttp supports tracing.
    https://docs.aiohttp.org/en/stable/client_advanced.html#aiohttp-client-tracing

    To measure the time spent on a request, we capture the start time and end time in the TraceContext class
    by using the handlers `on_request_start` and `on_request_end` respectively.
    """

    start: Optional[float] = None
    end: Optional[float] = None

    def elapsed(self) -> float:
        return none_throws(self.end) - none_throws(self.start)


async def log_response_content(
    response: aiohttp.ClientResponse, trace_ctx: TraceContext
) -> None:
    """
    Log the response content for debugging.
    This will log the content of any requests which is not `ok`, e.g status code > 400:
    https://docs.aiohttp.org/en/stable/client_reference.html#aiohttp.ClientResponse.ok
    Or any requests when in debugging mode.
    """
    log_level = logging.DEBUG if response.ok else logging.ERROR

    try:
        content = json_pprint(await response.json())
    except (json.decoder.JSONDecodeError, aiohttp.ContentTypeError):
        payload = await response.read()
        content = f"<{len(payload)} bytes> {payload[:LOGGED_BODY_BYTES]!r}"
    except Exception:
        logger.exception("Failed to handle Patchwork response")
        return

    logger.log(
        log_level,
        f"Patchwork {response.method} {response.url} "
        f"{response.status}, response: {content}",
    )


class PatchworkUnavailable(Exception):
    """Patchwork is overloaded or refuses KPD's requests."""

    def __init__(self, message: str, status: Optional[int] = None) -> None:
        super().__init__(message)
        # The HTTP status, or None for a timeout, a connection error or a pause.
        self.status = status


def retry_after_secs(response: aiohttp.ClientResponse) -> Optional[float]:
    """Return the delay in the Retry-After header (seconds or HTTP date), if any."""
    value = response.headers.get("Retry-After", "").strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        delay = email.utils.parsedate_to_datetime(value) - datetime.datetime.now(
            datetime.timezone.utc
        )
    except (TypeError, ValueError):
        return None
    return max(0.0, delay.total_seconds())


def json_pprint(obj: Any, indent: int = 2) -> str:
    """
    Return a string which is suitable to pretty print a JSON object.
    """
    return json.dumps(obj, indent=indent, sort_keys=True)


async def log_response_metrics(
    response: aiohttp.ClientResponse, trace_ctx: TraceContext
) -> None:
    """
    Log an API response metrics.
    HTTP methods like GET, POST and OPTIONS per ranges of status codes are counted,
    as well as requests elapsed time per method.
    """
    response_code_leading_num = response.status // 100

    if not response.method:
        logger.error("No http method founds in response, skipping metrics")
        return

    normalized_http_method = response.method.strip().lower()
    api_requests.add(
        1,
        {
            "http_code": f"{response_code_leading_num}xx",
            "http_method": normalized_http_method,
        },
    )
    api_requests_time.record(
        round(trace_ctx.elapsed() * 1000),  # Elapsed time in ms
        {"http_method": normalized_http_method},
    )


async def on_request_start(
    session: aiohttp.ClientSession,
    trace_ctx: TraceContext,
    params: aiohttp.TraceRequestStartParams,
) -> None:
    """
    Handler called when an HTTP request starts.
    We currently use this to capture the start time of the request.
    """
    trace_ctx.start = asyncio.get_event_loop().time()


async def on_request_end(
    session: aiohttp.ClientSession,
    trace_ctx: TraceContext,
    params: aiohttp.TraceRequestEndParams,
) -> None:
    """
    Handler called when an HTTP request finishes.
    We currently use this to capture the end time of the request and subsequently
    log the response content and metrics.
    """
    trace_ctx.end = asyncio.get_event_loop().time()
    await log_response_content(params.response, trace_ctx)
    await log_response_metrics(params.response, trace_ctx)


def time_since_secs(date: str) -> float:
    parsed_datetime = dateparser.parse(date)
    # pyrefly: ignore  # deprecated, unsupported-operation
    duration = datetime.datetime.utcnow() - parsed_datetime
    return duration.total_seconds()


def parse_tags(input: str) -> Set[str]:
    # "[tag1 ,tag2]title" -> "tag1,tags" -> ["tag1", "tag2"]
    try:
        parsed_tags = none_throws(re.match(TAG_REGEXP, input)).group("tags").split(",")
        logging.debug(f"Parsed tags from '{input}' string: {parsed_tags}")
    except Exception:
        logger.warning(f"Can't parse tags from string '{input}'")
        err_tag_parsing_failures.add(1)
        return set()

    tags = [tag.strip() for tag in parsed_tags]
    filtered_tags = {tag for tag in tags if not re.match(IGNORE_TAGS_REGEX, tag)}
    logging.debug(f"Filtered tags from '{input}' string: {filtered_tags}")
    return filtered_tags


def parse_subject(input: str) -> str:
    try:
        logging.debug(f"Parsing subject name from '{input}' patch name")
        return none_throws(re.match(SUBJECT_REGEXP, input)).group("name")
    except Exception as ex:
        logger.error(f"Failed to parse subject from patch name '{input}': {ex}")
        return ""


# An asyncio compatible cachetools.cached method.
# https://github.com/tkem/cachetools/issues/137
# We may want to use asyncache, aiocache or the likes.
def cached(cache):
    """
    Decorator for an async function: keep its results in `cache`, keyed by
    its arguments.
    """

    def decorator(func):
        async def wrapper(*args, **kwargs):
            k = cachetools.keys.hashkey(*args, **kwargs)
            try:
                return cache[k]
            except KeyError:
                pass  # key not found
            v = await func(*args, **kwargs)
            cache[k] = v
            return v

        return update_wrapper(wrapper, func)

    return decorator


class Subject:
    def __init__(self, subject: str, pw_client: "Patchwork") -> None:
        self.pw_client = pw_client
        self.subject = subject

    async def branch(self) -> Optional[str]:
        s = await self.latest_series()
        if s is None:
            return None
        return f"series{SERIES_ID_SEPARATOR}{s.id}"

    async def latest_series(self) -> Optional["Series"]:
        relevant_series = await self.relevant_series()
        if len(relevant_series) == 0:
            return None
        return relevant_series[-1]

    @cached(cache=TTLCache(maxsize=OBJECT_CACHE_SIZE, ttl=600))
    async def relevant_series(self) -> List["Series"]:
        """
        cache and return sorted list of relevant series
        where first element is first known version of same subject
        and last is the most recent
        """
        series_list = await self.pw_client.get_series(
            params={"project": str(self.pw_client.project), "q": self.subject}
        )

        logging.debug(
            f"All series for '{self.subject}' subject: {json_pprint([s.to_json() for s in series_list])}"
        )
        # we using full text search which could give ambiguous results
        # so we must filter out irrelevant results
        relevant_series = [
            series
            for series in series_list
            if series.subject == self.subject and await series.has_matching_patches()
        ]
        # sort series by age desc,  so last series is the most recent one
        sorted_series = sorted(relevant_series, key=lambda x: x.age(), reverse=True)
        logging.debug(
            f"Sorted matching series for '{self.subject}' subject: {json_pprint([s.to_json() for s in sorted_series])}"
        )
        return sorted_series

    def to_json(self) -> str:
        return json.dumps({"subject": self.subject})

    def __repr__(self) -> str:
        return f"Subject({self.to_json()})"


class Series:
    def __init__(self, pw_client: "Patchwork", data: Dict) -> None:
        self.pw_client = pw_client
        self.data = data
        self._patch_blob = None

        # We should be able to create object from a short version of series object from /patches/ endpoint
        # Docs: https://patchwork.readthedocs.io/en/latest/api/rest/schemas/v1.2/#get--api-1.2-patches-
        # {
        #     "id": 1,
        #     "url": "https://example.com",
        #     "web_url": "https://example.com",
        #     "name": "string",
        #     "date": "string",
        #     "version": 1,
        #     "mbox": "https://example.com"
        # }
        self.id = data["id"]
        self.name = data["name"]
        self.date = data["date"]
        self.url = data["url"]
        self.web_url = data["web_url"]
        self.version = data["version"]
        self._submitter_email = data["submitter"]["email"]
        self.mbox = data["mbox"]
        self.patches = data.get("patches", [])
        self.cover_letter = data.get("cover_letter")

        try:
            logging.debug(f"Parsing subject name from '{self.name}' series name")
            self.subject = none_throws(re.match(SUBJECT_REGEXP, data["name"])).group(
                "name"
            )
        except Exception as ex:
            raise ValueError(
                f"Failed to parse subject from series name '{data['name']}'"
            ) from ex

    def _is_patch_matching(self, patch: Dict[str, Any]) -> bool:
        for pattern in self.pw_client.search_patterns:
            for prop_name, expected_value in pattern.items():
                if prop_name in PATCH_FILTERING_PROPERTIES:
                    try:
                        # these values can be None so we need to filter them out first
                        if not patch[prop_name]:
                            return False
                        if patch[prop_name]["id"] != expected_value:
                            return False
                    except KeyError:
                        return False
                elif patch[prop_name] != expected_value:
                    return False
        return True

    def age(self) -> float:
        return time_since_secs(self.date)

    @cached(cache=TTLCache(maxsize=OBJECT_CACHE_SIZE, ttl=600))
    async def get_patches(self) -> List[Dict]:
        """
        Returns patches preserving original order
        for the most recent relevant series
        """
        patches = await self.pw_client.get_series_patches(self.id)
        by_id = {patch["id"]: patch for patch in patches}
        return [by_id[patch["id"]] for patch in self.patches if patch["id"] in by_id]

    async def is_closed(self) -> bool:
        """
        Series considered closed if at least one patch in this series
        is in irrelevant states
        """
        for patch in await self.get_patches():
            if patch["state"] in IRRELEVANT_STATES:
                return True
        return False

    @cached(cache=TTLCache(maxsize=OBJECT_CACHE_SIZE, ttl=120))
    async def all_tags(self) -> Set[str]:
        """
        Tags fetched from series name, diffs and cover letter
        for most relevant series
        """
        tags = {f"V{self.version}"}

        for patch in await self.get_patches():
            tags |= parse_tags(patch["name"])
            tags.add(patch["state"])

        if self.cover_letter:
            tags |= parse_tags(self.cover_letter["name"])

        tags |= parse_tags(self.name)

        return tags

    async def visible_tags(self) -> Set[str]:
        return {
            f"V{self.version}",
            *[diff["state"] for diff in await self.get_patches()],
        }

    @cached(cache=TTLCache(maxsize=OBJECT_CACHE_SIZE, ttl=120))
    async def patch_subjects(self) -> List[str]:
        """
        Returns an ordered list of all patch subjects (tags removed)
        """
        return [parse_subject(patch["name"]) for patch in await self.get_patches()]

    async def is_expired(self) -> bool:
        for diff in await self.get_patches():
            if diff["state"] in TTL:
                if time_since_secs(diff["date"]) >= TTL[diff["state"]]:
                    return True
        return False

    @cached(cache=TTLCache(maxsize=OBJECT_CACHE_SIZE, ttl=120))
    async def get_patch_binary_content(self) -> bytes:
        content = await self.pw_client.get_blob(self.mbox)
        logger.debug(
            f"Received patch mbox for series {self.id}, size: {len(content)} bytes"
        )
        return content

    async def has_matching_patches(self) -> bool:
        for patch in await self.get_patches():
            if self._is_patch_matching(patch):
                return True

        return False

    async def set_check(self, status: Status, **kwargs) -> None:
        tasks = [
            self.pw_client.post_check_for_patch_id(
                patch_id=patch["id"], status=status, check_data=kwargs
            )
            for patch in self.patches
        ]
        await gather_or_cancel(*tasks)

    def to_json(self) -> str:
        json_keys = {
            "id",
            "name",
            "date",
            "url",
            "web_url",
            "version",
            "mbox",
            "patches",
            "cover_letter",
        }
        return json.dumps(
            {k: getattr(self, k) for k in json_keys if getattr(self, k, None)}
        )

    def __repr__(self) -> str:
        return f"Series({self.to_json()})"

    @property
    def submitter_email(self):
        """Retrieve the email address of the patch series submitter."""
        return self._submitter_email

    def patch_by_subject(self, subject_str: str) -> Optional[Dict]:
        for patch in self.patches:
            if subject_str in patch["name"]:
                return patch
        return None


class Patchwork:
    def __init__(
        self,
        server: str,
        search_patterns: List[Dict[str, Any]],
        auth_token: Optional[str] = None,
        lookback_in_days: int = -1,
        api_version: str = "1.2",
        max_concurrent_requests: int = DEFAULT_MAX_CONCURRENT_REQUESTS,
        max_requests_per_sec: int = DEFAULT_MAX_REQUESTS_PER_SEC,
    ) -> None:
        self.api_url = f"https://{server}/api/{api_version}/"
        self.auth_token = auth_token
        if not auth_token:
            logger.warning("Patchwork client runs in read-only mode")
        self.search_patterns = search_patterns
        # Patchwork wants `project` in each list query. A patch must match all
        # search patterns (see Series._is_patch_matching()), so they can name
        # only one project.
        projects = {pattern.get("project") for pattern in search_patterns}
        if len(projects) != 1 or None in projects:
            raise ValueError("All search patterns must have the same `project`")
        self.project = projects.pop()
        self.lookback_in_days = lookback_in_days
        # member variable initializations
        self.known_series: Dict[int, Series] = {}
        self.known_subjects: Dict[str, Subject] = {}
        # Patch ID -> {context: latest check}. Only KPD posts checks with its
        # contexts, so it can remember them. The client lives as long as the
        # daemon, so this lasts across cycles.
        self.latest_checks: LRUCache = LRUCache(maxsize=CHECK_CACHE_SIZE)
        self.latest_checks_lock = asyncio.Lock()

        # aiohttp's ClientSession needs to be initialized within an async function.
        # We will differ this initialization to a separate function and memoize it during first call.
        self.http_session = None
        self.request_slots = asyncio.Semaphore(max_concurrent_requests)
        self.max_requests_per_sec = max_requests_per_sec
        # time.monotonic() value before which no request starts: the end of
        # the pause after a failure, or the start of the last request plus
        # 1 / max_requests_per_sec, whichever is later.
        self.next_request_at = 0.0
        # Failed requests in a row.
        self.failures = 0
        # Requests sent, for the daemon's log of each cycle.
        self.requests_sent = 0

    async def get_http_session(self) -> aiohttp.ClientSession:
        """
        Return a cached http session and initialize it if it doesn't exist.
        """
        if self.http_session is None:
            # Setup aiohttp client tracing so we can log metrics/requests
            # https://docs.aiohttp.org/en/stable/client_advanced.html#aiohttp-client-tracing
            trace_config = aiohttp.TraceConfig(trace_config_ctx_factory=TraceContext)
            # pyre-fixme[6]: In call `typing.MutableSequence.append`, for 1st positional argument, expected `_SignalCallback[TraceRequestStartParams]` but got `typing.Callable(on_request_start)[[Named(session, ClientSession), Named(trace_ctx, TraceContext), Named(params, TraceRequestStartParams)], Coroutine[typing.Any, typing.Any, None]]`.
            # pyrefly: ignore  # bad-argument-type
            trace_config.on_request_start.append(on_request_start)
            # pyre-fixme[6]: In call `typing.MutableSequence.append`, for 1st positional argument, expected `_SignalCallback[TraceRequestEndParams]` but got `typing.Callable(on_request_end)[[Named(session, ClientSession), Named(trace_ctx, TraceContext), Named(params, TraceRequestEndParams)], Coroutine[typing.Any, typing.Any, None]]`.
            # pyrefly: ignore  # bad-argument-type
            trace_config.on_request_end.append(on_request_end)
            headers = {"User-Agent": USER_AGENT}
            if self.auth_token:
                headers["Authorization"] = f"Token {self.auth_token}"
            # pyrefly: ignore  # bad-assignment
            self.http_session = aiohttp.ClientSession(
                headers=headers,
                trace_configs=[trace_config],
                # Read proxy from env var
                trust_env=True,
                timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECS),
            )
        # pyrefly: ignore  # bad-return
        return self.http_session

    def format_since(self, pw_lookback: int) -> str:
        # pyrefly: ignore  # deprecated
        today = datetime.datetime.utcnow().date()
        lookback = today - datetime.timedelta(days=pw_lookback)
        return lookback.strftime("%Y-%m-%dT%H:%M:%S")

    def __pause_after_failure(
        self,
        message: str,
        status: Optional[int] = None,
        retry_after: Optional[float] = None,
    ) -> PatchworkUnavailable:
        """
        Pause the requests of all callers after a failed request, and return
        the error to raise. The pause is 5 s, doubled for each failure in a
        row, or the Retry-After time if that is longer, up to MAX_PAUSE_SECS.
        """
        self.failures += 1
        delay = RETRY_BASE_DELAY_SECS * 2 ** min(self.failures - 1, 10)
        delay = min(max(delay, retry_after or 0), MAX_PAUSE_SECS)
        self.next_request_at = max(self.next_request_at, time.monotonic() + delay)
        logger.warning(f"{message}; pausing requests for {delay:.0f} s")
        return PatchworkUnavailable(message, status)

    async def __request(
        self, method: str, path: AnyStr, **kwargs: Any
    ) -> aiohttp.ClientResponse:
        """
        Send a request. At most max_concurrent_requests requests are in
        flight, and at most max_requests_per_sec start per second. A 5xx, 429
        or 403, a timeout or a connection error raises PatchworkUnavailable
        and pauses the requests of all callers. A request waits for a pause
        up to MAX_WAIT_SECS, and raises PatchworkUnavailable instead of
        waiting for a longer one.
        """
        http_session = await self.get_http_session()
        # pyre-ignore
        # pyrefly: ignore  # bad-argument-type
        url = urljoin(self.api_url, path)
        async with self.request_slots:
            # Check the pause after we got the slot: it can start or grow
            # while we wait for the slot.
            while (pause := self.next_request_at - time.monotonic()) > 0:
                if pause > MAX_WAIT_SECS:
                    raise PatchworkUnavailable(
                        f"Patchwork requests are paused for {pause:.0f} s"
                    )
                await asyncio.sleep(pause)
            # Nothing awaits between the loop and here, so the other requests
            # see the new value before they can start.
            self.next_request_at = max(
                self.next_request_at, time.monotonic() + 1 / self.max_requests_per_sec
            )
            self.requests_sent += 1

            # A failure starts the pause before we release the slot, so that
            # the requests that wait for the slot see it.
            try:
                resp = await http_session.request(method, url, **kwargs)
                # Read the body while we hold the slot.
                await resp.read()
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                raise self.__pause_after_failure(
                    f"Patchwork {method} {url}: {e!r}"
                ) from e
            if resp.status in (403, 429) or resp.status >= 500:
                raise self.__pause_after_failure(
                    f"Patchwork {method} {url}: {resp.status}",
                    resp.status,
                    retry_after_secs(resp),
                )
            self.failures = 0
        return resp

    async def __get(
        self, path: AnyStr, allow_redirects=True, **kwargs: Dict
    ) -> aiohttp.ClientResponse:
        try:
            return await self.__request("GET", path, **kwargs)
        except PatchworkUnavailable as e:
            if e.status == 403:
                raise
            # Try once more, after the pause.
            return await self.__request("GET", path, **kwargs)

    async def __get_object_by_id(self, object_type: str, object_id: int) -> Dict:
        resp = await self.__get(f"{object_type}/{object_id}/")
        return await resp.json()

    async def __get_objects_recursive(
        self, object_type: str, params: Optional[Dict] = None
    ) -> List[Dict]:
        items = []
        path = f"{object_type}/"
        if params is None:
            params = {}
        while True:
            response = await self.__get(path, params=params)
            j = await response.json()
            items += j

            if "next" not in response.links:
                break

            # FIXME: the returned URL is a yarl.URL that contains the full hostname, path, query parameters.
            # Here all a sudden, path is changed from a "relative path" to the full URL. Luckily, urljoin, which we use
            # in `__get` deal with this and compute the right URL.
            path = str(response.links["next"]["url"])
            # The `next` URL already carries all the query parameters, so pass an
            # empty mapping instead of the original filters: aiohttp appends
            # (rather than replaces) params, so re-passing them would duplicate
            # every filter on each page and grow the URL until the server rejects
            # the request.
            params = {}
        return items

    async def __post(
        self, path: AnyStr, data: Dict
    ) -> Optional[aiohttp.ClientResponse]:
        try:
            return await self.__request("POST", path, data=data)
        except PatchworkUnavailable as e:
            # Skip a check that got a 5xx, so that one bad patch cannot stop
            # each cycle. The next cycle posts it again, and the pause still
            # delays the next requests.
            if e.status is None or e.status < 500:
                raise
            return None

    async def __try_post(
        self, path: AnyStr, data: Dict
    ) -> Optional[aiohttp.ClientResponse]:
        if not self.auth_token:
            logger.debug(f"Patchwork POST {path}: read-only mode, request ignored")
            logger.debug(f"Patchwork POST data: {json_pprint(data)}")
            return None

        return await self.__post(path, data)

    async def get_blob(self, url: AnyStr) -> bytes:
        resp = await self.__get(url, allow_redirects=True)
        return await resp.read()

    async def __get_latest_checks(self, patch_id: int) -> Dict[str, Dict]:
        """
        Return the latest check of each context of a patch. The first call
        for a patch lists all its checks, later calls use the cache.
        """
        # The checks of all contexts of a patch are posted at the same time:
        # the lock makes them wait for one listing.
        async with self.latest_checks_lock:
            if patch_id not in self.latest_checks:
                checks = await self.__get_objects_recursive(
                    f"patches/{patch_id}/checks",
                    params={"order": "-date", "per_page": str(MAX_PAGE_SIZE)},
                )
                latest: Dict[str, Dict] = {}
                # Newest first, so the first check of a context is its latest.
                for check in checks:
                    latest.setdefault(check["context"], check)
                self.latest_checks[patch_id] = latest
            return self.latest_checks[patch_id]

    async def post_check_for_patch_id(
        self, patch_id: int, status: Status, check_data: Dict[str, Any]
    ) -> Optional[aiohttp.ClientResponse]:
        new_state = PW_CHECK_STATES[status]
        updated_check_data = {
            **check_data,
            "state": new_state,
        }
        logger.debug(
            f"Trying to update check for {patch_id} with a new content: {json_pprint(updated_check_data)}"
        )
        latest_checks = await self.__get_latest_checks(patch_id)
        check = latest_checks.get(check_data["context"])
        if check is None:
            logger.debug(f"Setting patch {patch_id} check to '{new_state}' state")
        else:
            if status == Status.PENDING and check["state"] != new_state:
                logger.info(
                    f"Not posting state update for patch {patch_id}: "
                    f"existing state '{check['state']}' can't be changed "
                    f"to pending state '{new_state}'"
                )
                return None

            if (
                check.get("state") == new_state
                and check.get("target_url") == check_data["target_url"]
            ):
                logger.debug(
                    f"Not posting state update for patch {patch_id}: previous state '{new_state}' and url are the same"
                )
                return None

            logger.debug(
                f"Updating patch {patch_id} check, current state: '{check.get('state')}', new state: '{new_state}'"
            )

        resp = await self.__try_post(
            f"patches/{patch_id}/checks/",
            data=updated_check_data,
        )
        if resp is not None and resp.ok:
            latest_checks[check_data["context"]] = updated_check_data
        return resp

    async def get_series_by_id(self, series_id: int) -> Series:
        # fetches directly only if series is not available in local scope
        if series_id not in self.known_series:
            series_json = await self.__get_object_by_id("series", series_id)
            self.known_series[series_id] = Series(self, series_json)

        return self.known_series[series_id]

    def get_subject_by_series(self, series: Series) -> Subject:
        # local cache for subjects
        if series.subject not in self.known_subjects:
            subject = Subject(series.subject, self)
            self.known_subjects[series.subject] = subject

        return self.known_subjects[series.subject]

    async def get_relevant_subjects(self) -> Sequence[Subject]:
        # The client is used for many cycles: move the lookback window along.
        since = (
            self.format_since(self.lookback_in_days)
            if self.lookback_in_days > 0
            else None
        )
        filtered_subjects = []
        self.known_series = {}
        self.known_subjects = {}

        for pattern in self.search_patterns:
            patch_filters = MultiDict(
                # pyrefly: ignore  # bad-argument-type
                [
                    ("archived", str(False)),
                    *[("state", val) for val in RELEVANT_STATES.values()],
                ]
            )
            if since is not None:
                patch_filters.add("since", since)
            patch_filters.update({k: str(v) for k, v in pattern.items()})
            logger.info(
                f"Searching for Patchwork patches that match the criteria: {patch_filters}"
            )
            all_patches = await self.__get_objects_recursive(
                "patches",
                # pyre-ignore
                # pyrefly: ignore  # bad-argument-type
                params=patch_filters,
            )

            series_ids = set()
            for patch in all_patches:
                for series_data in patch["series"]:
                    if not series_data.get("name"):
                        logger.error(f"Malformed series name in: {series_data}")
                        err_malformed_series.add(1)
                        continue

                    try:
                        series_id = int(series_data["id"])
                        series_ids.add(series_id)
                    except ValueError:
                        logger.error(f"Malformed series ID in: {series_data}")
                        err_malformed_series.add(1)
                        continue

            tasks = [self.get_series_by_id(series_id) for series_id in series_ids]
            all_series = await gather_or_cancel(*tasks)

            for series in all_series:
                if series.subject.strip() == "":
                    logger.error(f"Malformed series name: {series.name}")
                    err_malformed_series.add(1)
                    continue
                self.get_subject_by_series(series)

            logger.info(f"Total subjects found: {len(self.known_subjects)}")

            # async function used to fetch latest series for each subject concurrently
            async def fetch_latest_series(
                subject_name, subject_obj
            ) -> Tuple[str, Series, Optional[Series]]:
                return (subject_name, subject_obj, await subject_obj.latest_series())

            tasks = [fetch_latest_series(k, v) for k, v in self.known_subjects.items()]
            tasks = await gather_or_cancel(*tasks)

            for subject_name, subject_obj, latest_series in tasks:
                if not latest_series:
                    logger.error(f"Subject '{subject_name}' doesn't have any series")
                    continue

                if await latest_series.is_expired():
                    logger.info(
                        f"Subjects '{subject_name}' series {latest_series.id} is expired",
                    )
                    continue
                if await latest_series.is_closed():
                    logger.info(
                        f"Subjects '{subject_name}' series {latest_series.id} is closed",
                    )
                    continue

                logger.info(
                    f"Found relevant series {latest_series.id} for subject '{subject_name}'"
                )
                logger.info(
                    f"Adding subject '{subject_name}' into list of relevant subjects"
                )
                filtered_subjects.append(subject_obj)
        logger.info(f"Total relevant subjects found: {len(filtered_subjects)}")
        return filtered_subjects

    async def get_series_patches(self, series_id: int) -> List[Dict]:
        """
        Return the patches of a series with one list request. Unlike patch
        details, list items do not include the patch content and diff.
        """
        return await self.__get_objects_recursive(
            "patches", params={"project": str(self.project), "series": str(series_id)}
        )

    async def get_series(self, params: Optional[Dict]) -> List[Series]:
        return [
            Series(self, json)
            for json in await self.__get_objects_recursive("series", params=params)
        ]
