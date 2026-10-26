"""HTTP GET with timeouts and exponential-backoff retries on transient failures."""

import requests
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from core.config import settings
from core.log import get_logger

log = get_logger("http")

RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_cfg = settings()["http"]


def _is_transient(exc: BaseException) -> bool:
    if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
        return True
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        return exc.response.status_code in RETRYABLE_STATUS
    return False


@retry(
    retry=retry_if_exception(_is_transient),
    stop=stop_after_attempt(_cfg["max_attempts"]),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    before_sleep=lambda rs: log.warning(
        "transient error (%s), retry %d", rs.outcome.exception(), rs.attempt_number
    ),
    reraise=True,
)
def get(url: str, params: dict | None = None) -> requests.Response:
    resp = requests.get(url, params=params, timeout=_cfg["timeout_seconds"])
    resp.raise_for_status()
    return resp
