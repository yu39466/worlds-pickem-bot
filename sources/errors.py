"""Typed errors for upstream data sources.

Every source module raises one of these instead of leaking exceptions from
mwclient / httpx, so callers can branch on the cause rather than on which
library happened to fail.
"""


class SourceError(Exception):
    """Base for every upstream failure."""

    def __init__(self, source, message=""):
        self.source = source
        super().__init__(message or self.__class__.__name__)


class Upstream429(SourceError):
    """Upstream asked us to slow down. Leaguepedia does this readily."""

    def __init__(self, source, retry_after=None, message=""):
        self.retry_after = retry_after
        super().__init__(source, message)


class UpstreamDown(SourceError):
    """5xx, timeout, or connection failure."""


class UpstreamAuth(SourceError):
    """Credentials missing, rejected, or expired."""


class UpstreamBadData(SourceError):
    """Reached the source, but the response was not the shape we expect."""


class ConfigError(SourceError):
    """A required environment variable is missing."""
