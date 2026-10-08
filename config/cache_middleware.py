"""ETag + default Cache-Control middleware.

Sits after RequestTimingMiddleware. For 200 GETs with a renderable body we hash the
body to a strong ETag (handy for debugging and log correlation) and set
``Cache-Control: private, no-store``.

We deliberately never answer 304 and never let a client keep a copy. The iPhone
app's URL cache was found serving a days-old body for a plan endpoint after a 304
whose ETag matched the CURRENT body (2026-10-08: a project showed "0 of 0 steps" —
its state before steps were added — until pulled to refresh twice). A cached copy
of live account data is not worth that; every GET returns the real body.

We skip non-GET, non-200, and streaming responses. A view that sets its own
Cache-Control keeps it.
"""

from __future__ import annotations

import hashlib

_DEFAULT_CACHE_CONTROL = "private, no-store"


class ETagMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)

        if request.method != "GET" or response.status_code != 200:
            return response
        if getattr(response, "streaming", False):
            return response
        # DRF may defer rendering; force it so we can hash content.
        if hasattr(response, "accepted_renderer") and not getattr(response, "_is_rendered", False):
            response.render()
        if not hasattr(response, "content"):
            return response

        etag = '"' + hashlib.md5(response.content, usedforsecurity=False).hexdigest() + '"'
        response["ETag"] = etag

        if "Cache-Control" not in response:
            response["Cache-Control"] = _DEFAULT_CACHE_CONTROL
        # Auth-bearing responses must vary on Authorization; otherwise a CDN
        # or proxy could leak tenant A's body to tenant B.
        existing_vary = response.get("Vary", "")
        if "Authorization" not in existing_vary:
            response["Vary"] = f"{existing_vary}, Authorization" if existing_vary else "Authorization"

        return response
