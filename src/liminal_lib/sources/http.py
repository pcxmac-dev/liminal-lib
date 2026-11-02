"""The HTTP client every provider plugin's client builds on (plugin API 1.6): what each request is held
to whoever the provider is (its window, the request cap, the machine's daily cap, pauses, retries
with the server's Retry-After, a key never sent to another host), and its keyless form for the public
feeds (PublicClient, which identifies itself)."""
from __future__ import annotations

import email.utils
import math
import time
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import urljoin, urlparse

import requests

from ..errors import BudgetExhausted, NotEntitled, Paused, RateLimited, SourceError, Unauthorized
from ..ratelimit import RateLimiter

USER_AGENT = "liminal/1 (personal research; historical archive)"
# A server's Retry-After longer than this is not waited out inside a run: the request fails there
# and then (a 429 stops the run and kills the feed's fetching for a while), never sooner than asked.
RETRY_WAIT_MAX = 300.0
WAIT_SLICE = 2.0  # a wait before trying again is slept in slices this long, so a pause ends it
# The rate-limit headers servers send (Alpaca's X-RateLimit-*: a minute's; the IETF draft's RateLimit-*)
LIMIT_HEADERS = ("X-RateLimit-Limit", "X-RateLimit-Remaining", "X-RateLimit-Reset",
                 "RateLimit-Limit", "RateLimit-Remaining", "RateLimit-Reset", "RateLimit-Policy")


class SameHostSession(requests.Session):
    """Follows a redirect only to the scheme and host the request was sent to. requests strips the
    Authorization header (Massive's key) on a redirect to another host, but not a source's own key
    headers (Alpaca's APCA-API-KEY-ID and secret): those must never reach another host. A redirect
    elsewhere is not followed; it comes back as the answer, and is an error."""

    def get_redirect_target(self, resp):
        target = super().get_redirect_target(resp)
        if target:
            to, at = urlparse(urljoin(resp.url, target)), urlparse(resp.url)
            if (to.scheme, to.netloc) != (at.scheme, at.netloc):
                return None
        return target


class HttpClient:
    """A provider's HTTP client (plugin API 1.6: every plugin's client builds on it): its headers
    (a key's), rate limiting (its own window, or a window per asset class: `class_of`), the request
    cap, the machine's daily cap, retries with the server's Retry-After, pauses, the rate-limit
    headers seen, and links followed only on its own host."""

    def __init__(
        self,
        base_url: str,
        headers: dict[str, str] | None = None,
        rate_limiter: RateLimiter | None = None,
        session: requests.Session | None = None,
        max_retries: int = 4,
        timeout: float = 30.0,
        sleep: Callable[[float], None] = time.sleep,
        request_budget: int | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self._host = urlparse(self.base_url).netloc
        self._limiter = rate_limiter or RateLimiter(None)
        # Asset classes on another plan, with that plan's rate (class_of: which a request is; Massive's).
        self.class_limiters: dict[str, RateLimiter] = {}
        self.class_of: Callable[[str], str] | None = None
        self._session = session or SameHostSession()
        self._headers = dict(headers or {})
        # Hosts that refused the key (401) this run, with the error: nothing more is sent to them.
        self._refused: dict[str, SourceError] = {}
        self._max_retries = max_retries
        self._timeout = timeout
        self._sleep = sleep
        # Hard cap on requests this client may send, retries included.
        self.request_budget = request_budget
        self.requests_made = 0
        # Called with the running total after every request, so usage is recorded even if
        # the run is interrupted (the daily cap counts it).
        self.on_request: Callable[[int], None] | None = None
        # Called with (seconds, HTTP status) before waiting to retry a refused or failed request.
        self.on_retry: Callable[[float, int], None] | None = None
        # Asked before every request: a reason to stop (the user paused background fetching), or None.
        self.should_stop: Callable[[], str | None] | None = None
        # Asked as each request goes, counting it: False once the machine's requests on this key
        # today have reached the daily cap (every database on the machine counts: MachineLedger).
        self.take_day: Callable[[], bool] | None = None
        # The rate-limit headers its server last sent (LIMIT_HEADERS): the capability scan checks
        # the rate kept against them (Provider.run_scan).
        self.limits_seen: dict[str, str] = {}

    def tighten(self, n: int | None, seconds: float) -> bool:
        """Keep a tighter window from now on (a capability scan found its server's lower limit while
        this client runs: it went on at the old one until the next run); never a looser one. Returns
        whether it changed."""
        lim = self._limiter
        if n is None or (lim.max_calls and lim.max_calls / lim.period <= n / seconds + 1e-9 and lim.max_calls <= n):
            return False
        lim.max_calls, lim.period = n, float(seconds)
        return True

    def get(self, path_or_url: str, params: dict[str, Any] | None = None) -> dict:
        resp = self._request(path_or_url, params)
        try:
            return resp.json()
        except ValueError as e:  # an HTML error page, a cut-off body: this request's failure, not the run's
            raise SourceError(resp.status_code, f"not JSON: {e}") from e

    def get_bytes(self, url: str) -> tuple[bytes, str]:
        """A file on the API host (e.g. a company icon): (content, content type). Counts as a request."""
        resp = self._request(url, None)
        return resp.content, resp.headers.get("Content-Type", "application/octet-stream")

    def _request(self, path_or_url: str, params: dict[str, Any] | None) -> requests.Response:
        if path_or_url.startswith("http"):
            # Only follow pagination links on the API host, so the key is never sent elsewhere.
            if urlparse(path_or_url).netloc != self._host:
                raise SourceError(0, f"refusing to follow link to another host: {path_or_url}")
            url = path_or_url
        else:
            url = self.base_url + path_or_url
        host = urlparse(url).netloc
        if host in self._refused:  # the key was refused here already: every request would be, so none is sent
            was = self._refused[host]
            raise type(was)(was.status, f"{was.message} (refused earlier this run; nothing sent)")

        for attempt in range(self._max_retries + 1):
            reason = self.should_stop() if self.should_stop else None
            if reason:
                raise Paused(reason)
            if self.request_budget is not None and self.requests_made >= self.request_budget:
                raise BudgetExhausted(f"request cap of {self.request_budget} reached")
            (self.class_limiters.get(self.class_of(url), self._limiter) if self.class_of else self._limiter).acquire()
            reason = self.should_stop() if self.should_stop else None  # paused during the window's wait
            if reason:
                raise Paused(reason)
            if self.take_day is not None and not self.take_day():
                raise BudgetExhausted("the daily request cap is reached on this machine (all its databases together)")
            self.requests_made += 1  # counted before it goes: one that times out may have reached the server
            if self.on_request:
                self.on_request(self.requests_made)
            try:
                resp = self._session.get(url, params=params, headers=self._headers, timeout=self._timeout)
            except (requests.exceptions.InvalidHeader, requests.exceptions.InvalidURL):
                # A key with a space or a line break in it: nothing can be sent, ever, and the error
                # repeats the header (the key) word for word, which went to the log and the kill's
                # reason. Not tried again; the message names the trouble, never the value.
                raise Unauthorized(0, "a key or address this feed sends holds a space, a line break or another "
                                      "character a request cannot carry: check its variable's value") from None
            except requests.RequestException as e:  # no answer (network down, a timeout): tried again like a 5xx
                if attempt == self._max_retries:
                    raise SourceError(0, f"no answer: {type(e).__name__}: {e}") from e
                delay = retry_delay(None, attempt)
                if self.on_retry:
                    self.on_retry(delay, 0)
                self._wait(delay)
                continue
            sent = getattr(resp, "headers", None) or {}
            self.limits_seen.update({h: str(sent[h]) for h in LIMIT_HEADERS if sent.get(h)})
            retryable = resp.status_code == 429 or resp.status_code >= 500
            if not retryable or attempt == self._max_retries:
                break
            delay = retry_delay(resp, attempt)
            if delay > RETRY_WAIT_MAX:  # longer than a run waits: it fails now (not tried again sooner)
                break
            if self.on_retry:
                self.on_retry(delay, resp.status_code)
            self._wait(delay)

        if 300 <= resp.status_code < 400:  # a redirect to another host (SameHostSession): not followed
            where = urlparse(resp.headers.get("Location") or "").netloc or "elsewhere"
            raise SourceError(resp.status_code, f"redirected to {where}; not followed (the key stays on {host})")
        if resp.status_code >= 400:
            message = error_message(resp)
            if resp.status_code == 401:  # the key itself: the run stops and the feed is killed, not symbol by symbol
                self._refused[host] = self._unauthorized(host, message)
                raise self._refused[host]
            if resp.status_code == 403:
                raise NotEntitled(resp.status_code, message)
            if resp.status_code == 429:  # the wait the server asked for: the kill that follows lasts at least that
                raise RateLimited(resp.status_code, message,
                                  retry_delay(resp, 0) if resp.headers.get("Retry-After") else None)
            raise SourceError(resp.status_code, message)
        return resp

    def _unauthorized(self, host: str, message: str) -> SourceError:
        """The error a 401 from `host` is (and stays for the rest of the run)."""
        return Unauthorized(401, message)

    def _wait(self, seconds: float) -> None:
        """Wait before trying again. With a pause to honour (should_stop), in slices of WAIT_SLICE,
        so a pause during a long wait (a server's Retry-After) takes effect then, not after it."""
        if self.should_stop is None:
            self._sleep(seconds)
            return
        left = seconds
        while left > 0:
            self._sleep(min(WAIT_SLICE, left))
            left -= WAIT_SLICE
            reason = self.should_stop()
            if reason:
                raise Paused(reason)

class PublicClient(HttpClient):
    """A public feed's client (registry.public_client): caps, rate limiting and retries as for the
    other feeds; it identifies itself (USER_AGENT), and sends no key unless a header is given."""

    def __init__(self, base_url: str, headers: dict | None = None, rate_limiter: RateLimiter | None = None,
                 session: requests.Session | None = None, sleep: Callable[[float], None] = time.sleep,
                 request_budget: int | None = None):
        super().__init__(base_url, {"User-Agent": USER_AGENT, **(headers or {})}, rate_limiter=rate_limiter,
                         session=session, sleep=sleep, request_budget=request_budget)

    def text(self, path_or_url: str, params: dict[str, Any] | None = None) -> str:
        return self._request(path_or_url, params).text



def retry_delay(resp: requests.Response | None, attempt: int, now: datetime | None = None) -> float:
    """Seconds before trying again: the server's Retry-After (seconds, or an HTTP date), else 5 s
    doubling up to 60 s."""
    retry_after = (resp.headers.get("Retry-After") or "").strip() if resp is not None else ""
    if retry_after:
        try:
            seconds = float(retry_after)
            if math.isfinite(seconds):
                return max(0.0, seconds)
        except ValueError:
            try:
                when = email.utils.parsedate_to_datetime(retry_after)
            except (TypeError, ValueError, IndexError):
                when = None
            if when is not None:
                when = when if when.tzinfo else when.replace(tzinfo=timezone.utc)
                return max(0.0, (when - (now or datetime.now(timezone.utc))).total_seconds())
    return min(60.0, 5.0 * 2**attempt)


def error_message(resp: requests.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:200]
    if isinstance(body, dict):
        return body.get("message") or body.get("error") or str(body)[:200]
    if isinstance(body, list):  # Bitfinex: ["error", 11010, "ratelimit: error"] (a 429 read as a crash)
        return " ".join(map(str, body))[:200]
    return str(body)[:200]
