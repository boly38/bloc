"""Optional, unauthenticated Opper API reachability check; never requests inference."""

import urllib.error
import urllib.request

from . import bridge_runtime


URL = "https://api.opper.ai/v3/compat/models"
TIMEOUT_SECONDS = 5


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def health_status():
    """Check the API response headers only, without credentials, redirects, or retries."""
    bridge_runtime.check_shutdown()
    request = urllib.request.Request(URL, method="GET")
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
            status = response.status
    except urllib.error.HTTPError as error:
        status = error.code
        error.close()
    except (TimeoutError, urllib.error.URLError, OSError) as error:
        reason = error.reason if isinstance(error, urllib.error.URLError) else error
        if isinstance(reason, TimeoutError):
            return "Opper API: reachability check timed out"
        return "Opper API: unreachable from the bridge (check network, DNS, and TLS)"
    finally:
        bridge_runtime.check_shutdown()

    if status in (200, 401):
        return "Opper API: reachable (authentication and inference not checked)"
    if status == 429:
        return "Opper API: rate limited (HTTP 429; inference not checked)"
    return f"Opper API: reachability check failed (HTTP {status}; inference not checked)"
