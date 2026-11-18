"""Guards for URLs that come from outside: staff-submitted start URLs, and every link followed.

    validate_start_url(url, config)  Raise UnsafeURLError unless url may be crawled: http(s),
                                     in config.allowed_start_domains (if set), and not resolving
                                     to a private/internal address.
    GuardedAdapter                   A requests adapter that checks the host of every request it
                                     sends -- including each redirect hop -- so a public page
                                     can't redirect the crawler to an internal service.

The point is SSRF: in AWS, a URL like http://169.254.169.254/ or http://10.0.3.7:8080/ reaches
things that must never be fetched on a user's behalf. Known gap: DNS rebinding (the name resolves
to a public address when checked and a private one when connected) -- closing that needs pinning
the checked IP for the connection, or an egress proxy/VPC rules, which is the real fix in AWS.
"""
import ipaddress
import socket
from typing import Optional
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter

from . import config as config_module
from .config import Config
from .url_classifier import is_youtube_host


class UnsafeURLError(requests.exceptions.InvalidURL):
    """A URL that must not be fetched. A RequestException (and ValueError), so callers that
    already handle bad URLs handle this too."""


def validate_start_url(url: str, config: Optional[Config] = None) -> str:
    """Return url if it may be crawled, else raise UnsafeURLError. YouTube never may: a video
    link is downloaded instead (see classify_url), and its other pages aren't read."""
    config = config or config_module.default()
    parsed = urlparse(url)
    if parsed.scheme not in ('http', 'https'):
        raise UnsafeURLError(f'Only http(s) URLs can be crawled: {url}')
    host = (parsed.hostname or '').lower().rstrip('.')
    if not host:
        raise UnsafeURLError(f'No host in URL: {url}')
    if is_youtube_host(url):  # a single video is downloaded; YouTube pages are never read
        raise UnsafeURLError(
            'YouTube pages are never crawled -- only a single video can be fetched. Give the '
            "video's own link (https://www.youtube.com/watch?v=<id>), not a playlist, channel, "
            f'search or home page: {url}')
    if config.allowed_start_domains and not domain_allowed(host, config.allowed_start_domains):
        raise UnsafeURLError(f'{host} is not an allowed domain '
                             f'(allowed: {", ".join(config.allowed_start_domains)})')
    if config.block_private_addresses:
        check_host(host)
    return url


def domain_allowed(host: str, domains: tuple[str, ...] | list[str] | set[str]) -> bool:
    """True if host is one of domains or a subdomain of one ('www.internet2.edu' for
    'internet2.edu'), ignoring a leading 'www.' on either."""
    host = host.lower().rstrip('.').removeprefix('www.')
    for domain in domains:
        domain = domain.lower().rstrip('.').removeprefix('www.')
        if host == domain or host.endswith('.' + domain):
            return True
    return False


def check_host(host: str) -> None:
    """Raise UnsafeURLError if host is, or resolves to, a non-public address. A name that
    doesn't resolve is left for the HTTP request to fail on."""
    host = host.strip('[]')
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            infos = _resolve(host)
        except (socket.gaierror, UnicodeError):
            return
        addresses = [ipaddress.ip_address(info[4][0].split('%', 1)[0]) for info in infos]
    for address in addresses:
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        if not address.is_global:
            raise UnsafeURLError(f'{host} is a private or internal address ({address}); '
                                 'refusing to connect')


def _resolve(host: str) -> list:
    return socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)


class GuardedAdapter(HTTPAdapter):
    """An HTTPAdapter that runs check_host() on every request before sending it. requests
    sends each redirect hop through the adapter, so redirects are checked too."""

    def send(self, request, *args, **kwargs):
        check_host(urlparse(request.url).hostname or '')
        return super().send(request, *args, **kwargs)
