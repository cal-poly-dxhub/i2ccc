"""Settings for crawling and downloading, in one place.

Every function that does HTTP takes an optional `config`; without one it uses default(), which
is read from the environment once (see Config.from_env). A long-running caller -- the CLI now,
a Lambda handler later -- builds a Config once and passes it in, or calls set_default().
"""
import os
from dataclasses import dataclass, fields
from typing import Mapping, Optional

ENV_PREFIX = 'I2CC_'


@dataclass(frozen=True)
class Config:
    user_agent: str = 'Mozilla/5.0 (compatible; i2cc-scraper/1.0)'
    timeout: float = 30                 # seconds per request (connect, and each read)

    # Retries for temporary failures: connection errors, timeouts, and HTTP 429/500/502/503/504.
    # Waits retry_backoff * 2^n seconds (+ up to retry_jitter) between attempts, or as long as a
    # Retry-After header asks, capped at retry_after_max. Other errors (404, 403, ...) fail at once.
    retries: int = 3
    retry_backoff: float = 3.0
    retry_jitter: float = 1.5
    retry_after_max: float = 60
    # A download that breaks partway through is started again, up to this many times in total.
    download_attempts: int = 3

    # Crawling
    crawl_delay: float = 0.5            # seconds between page requests
    max_pages: int = 200                # pages fetched per crawl, at most
    crawl_time_limit: float = 0.0       # seconds a crawl may run before stopping cleanly
                                        # (0 = no limit); e.g. a bit under Lambda's 900

    # A recording stored without captions is checked again for them (captions are often added
    # after upload) -- no more often than every caption_recheck_hours, and only until
    # caption_recheck_days after it was downloaded. 0 days turns re-checking off.
    caption_recheck_hours: float = 24.0
    caption_recheck_days: float = 14.0

    # Which start URLs may be crawled: these domains and their subdomains (e.g. 'internet2.edu').
    # Empty means any. Applies to the URL someone submits, not to the assets it links to.
    allowed_start_domains: tuple[str, ...] = ()
    # Refuse to connect to private, loopback, link-local (e.g. 169.254.169.254) and other
    # non-public addresses -- including after redirects. Turn off only for local testing.
    block_private_addresses: bool = True

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> 'Config':
        """A Config from I2CC_<FIELD> environment variables (e.g. I2CC_TIMEOUT=60,
        I2CC_ALLOWED_START_DOMAINS=internet2.edu,incommon.org, I2CC_BLOCK_PRIVATE_ADDRESSES=0);
        unset fields keep their defaults."""
        env = os.environ if env is None else env
        values = {}
        for f in fields(cls):
            raw = env.get(ENV_PREFIX + f.name.upper())
            if raw is None:
                continue
            default = f.default
            try:
                if isinstance(default, bool):
                    if raw.strip().lower() not in ('1', 'true', 'yes', 'on', '0', 'false', 'no', 'off'):
                        raise ValueError(raw)
                    values[f.name] = raw.strip().lower() in ('1', 'true', 'yes', 'on')
                elif isinstance(default, tuple):
                    values[f.name] = tuple(d.strip().lower() for d in raw.split(',') if d.strip())
                else:  # the declared type, so I2CC_TIMEOUT=2.5 works for "timeout: float = 30"
                    convert = f.type if f.type in (int, float, str) else type(default)
                    values[f.name] = convert(raw)
            except ValueError:
                raise ValueError(f'{ENV_PREFIX}{f.name.upper()}: invalid value {raw!r}') from None
        return cls(**values)


_default: Optional[Config] = None


def default() -> Config:
    """The Config used when none is passed: set_default()'s, else Config.from_env()."""
    global _default
    if _default is None:
        _default = Config.from_env()
    return _default


def set_default(config: Optional[Config]) -> None:
    """Make config the default (None: re-read the environment next time)."""
    global _default
    _default = config
