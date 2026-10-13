"""Shared TLS trust store — HTTPS that also works on a fresh VPS.

Python verifies HTTPS against the *operating system* trust store. On Windows
that is the built-in ROOT store, so the bot works out of the box; a minimal
Debian/Ubuntu VPS often ships WITHOUT `ca-certificates`, so
`ssl.get_default_verify_paths()` points at a file that does not exist and
every call to the exchange fails with:

    NETWORK: <urlopen error [SSL: CERTIFICATE_VERIFY_FAILED] certificate
    verify failed: unable to get local issuer certificate (_ssl.c:1082)>

This module builds ONE verifier used by every outgoing HTTPS call:

1. the operating system store (an explicit ``SSL_CERT_FILE`` /
   ``SSL_CERT_DIR`` environment override is honored by ssl itself — that is
   the path for a provider that inspects TLS), then
2. the ``certifi`` bundle whenever it is installed, MERGED into the same
   context — never a replacement for it. Certifi used to load only when
   the OS store came up EMPTY, on the assumption that non-empty == working;
   a store can be non-empty and still be insufficient. Measured on this
   host (2026-10-11, after the exchange rotated the chain it serves):
   Python's Windows ROOT subset held 23 roots (DigiCert/Microsoft/
   GlobalSign/ISRG/Starfield/SSL.com/USERTrust — no TrustAsia, no Certum)
   while the server answered
   ``*.nobitex.ir <- LiteSSL Pro RSA CA 2025 <- TrustAsia TLS RSA Root CA
   <- Certum cross-sign``; ``openssl s_client`` verified it (code 0) and
   the very same handshake with the certifi bundle returned HTTP 200, yet
   every bot call failed with the error above — and because the store was
   not empty, this module never reached its own certifi fallback. Widening
   the anchor set across two trusted-root stores changes nothing about
   strictness: verification stays ``CERT_REQUIRED`` with hostname
   checking on, and an untrusted chain still fails.

Verification is never disabled: a bot that places real orders must not
accept an unverified certificate. Errors carry the exact repair command
(:func:`error_hint`) and ``python run.py --doctor`` reports the state of
this host before the engine starts.
"""
from __future__ import annotations

import os
import ssl
import threading
from pathlib import Path

ENV_BUNDLES = ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE")

FIX_HINT = ("fix on this host: sudo apt update && sudo apt install -y "
            "ca-certificates && sudo update-ca-certificates   (or: "
            "pip install certifi)   - then restart the bot")

_LOCK = threading.Lock()
_CTX: ssl.SSLContext | None = None
_SOURCE = ""


def certifi_bundle() -> str:
    """Path of the certifi bundle, or "" when certifi is not installed."""
    try:
        import certifi
    except Exception:
        return ""
    try:
        return certifi.where()
    except Exception:
        return ""


def context() -> ssl.SSLContext:
    """The shared verifying context (built once, reused by every request)."""
    global _CTX, _SOURCE
    with _LOCK:
        if _CTX is not None:
            return _CTX
        ctx = ssl.create_default_context()   # verify_mode=CERT_REQUIRED
        os_roots = len(ctx.get_ca_certs())
        bundle = certifi_bundle()
        if bundle and Path(bundle).is_file():
            # union, not replace: two trusted-root stores merged cannot
            # weaken verification, while a NON-empty-but-insufficient OS
            # store stops being fatal (see the module docstring).
            ctx.load_verify_locations(cafile=bundle)
            source = ("operating-system trust store + certifi (%s)" % bundle
                      if os_roots else
                      "certifi (%s); OS store empty" % bundle)
        elif os_roots:
            source = "operating-system trust store"
        else:
            source = "EMPTY (no CA certificates found)"
            print("[tls] WARNING: this host has no CA certificates; every "
                  "HTTPS call will fail with\n      certificate verify "
                  "failed (unable to get local issuer certificate).\n"
                  "      " + FIX_HINT)
        _CTX, _SOURCE = ctx, source
        return _CTX


def ca_report() -> dict:
    """Diagnostic view of the trust store (``--doctor`` and error hints)."""
    ctx = context()
    paths = ssl.get_default_verify_paths()
    bundle = certifi_bundle()
    env = next(((k, os.environ[k]) for k in ENV_BUNDLES
                if os.environ.get(k)), ("", ""))
    return {
        "certs": len(ctx.get_ca_certs()),
        "source": _SOURCE,
        "certifi": bool(bundle),
        "certifi_bundle": bundle,
        "os_ca_file": paths.cafile or "",
        "os_ca_dir": paths.capath or "",
        "os_ca_file_exists": bool(paths.cafile and Path(paths.cafile).is_file()),
        "env_override": "%s=%s" % env if env[0] else "",
        "ok": len(ctx.get_ca_certs()) > 0,
    }


def is_cert_error(exc: BaseException) -> bool:
    """True when a failure chain is a TLS certificate-verification problem."""
    cur: BaseException | None = exc
    seen = 0
    while cur is not None and seen < 12:
        if isinstance(cur, ssl.SSLCertVerificationError):
            return True
        if "CERTIFICATE_VERIFY_FAILED" in str(cur) or \
                "unable to get local issuer certificate" in str(cur):
            return True
        nxt = cur.__cause__ or cur.__context__
        if nxt is None:
            # urllib wraps the real failure: URLError(reason=SSLCertVerificationError)
            reason = getattr(cur, "reason", None)
            nxt = reason if isinstance(reason, BaseException) else None
        cur, seen = nxt, seen + 1
    return False


def error_hint(exc: BaseException) -> str:
    """Actionable text for a TLS failure; "" when it is something else."""
    if not is_cert_error(exc):
        return ""
    rep = ca_report()
    detail = ("this host loaded %d CA certificates (source: %s)"
              % (rep["certs"], rep["source"] or "unknown"))
    return ("TLS certificate verification failed: %s. %s"
            % (detail, FIX_HINT))


def describe_error(exc: BaseException) -> str:
    """Short human reason for a handshake/socket failure (for the doctor)."""
    cur: BaseException | None = exc
    seen, last = 0, str(exc)
    while cur is not None and seen < 12:
        last = str(cur) or last
        nxt = cur.__cause__ or cur.__context__
        if nxt is None:
            reason = getattr(cur, "reason", None)
            nxt = reason if isinstance(reason, BaseException) else None
        cur, seen = nxt, seen + 1
    return last
