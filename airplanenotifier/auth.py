"""Google Calendar OAuth and token persistence (U2 -- R1, R2, R3).

The token lives at ``~/.airplane-notifier/token.json`` as JSON rather than a
pickle: it is inspectable, and unpickling a file an attacker can write is a
code-execution hazard we have no reason to accept.

**Threading note.** :func:`get_credentials` can block for as long as the user
takes to click through Google's consent screen, because
``run_local_server`` runs its own synchronous HTTP server. Callers on the Qt
main thread must run it in a worker thread or the UI freezes (plan review P1
fix 2); this module deliberately stays synchronous and unaware of Qt.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Optional

from google.auth.exceptions import GoogleAuthError, RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from airplanenotifier import paths
from airplanenotifier.paths import credentials_path, ensure_config_dir
from airplanenotifier.log import diagnostic

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]


class TransientAuthError(Exception):
    """Authorization failed for a reason that may fix itself (usually offline).

    Distinct from a missing credentials.json or a revoked grant: the caller
    should retry on a timer rather than stopping and asking the user to act.
    """

CREDENTIALS_HELP = (
    "credentials.json was not found.\n"
    "Create a Google Cloud project, enable the Calendar API, create an "
    "OAuth client ID of type 'Desktop app', download the JSON, and save it as "
    "credentials.json next to the app."
)


def has_credentials() -> bool:
    """True when a saved token exists, without validating it."""
    return paths.TOKEN_PATH.exists()


def clear_credentials() -> None:
    """Delete the saved token. Used by the tray's Re-authorize action."""
    try:
        paths.TOKEN_PATH.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        print(f"airplane-notifier: could not remove token: {exc}", file=diagnostic)


def _load_saved() -> Optional[Credentials]:
    """Load the stored token, or None when it is absent or unreadable.

    A corrupt token must never be fatal: the worst case is that the user
    re-authorizes, which is strictly better than the app refusing to start.
    """
    if not paths.TOKEN_PATH.exists():
        return None
    try:
        return Credentials.from_authorized_user_file(str(paths.TOKEN_PATH), SCOPES)
    except (ValueError, KeyError, json.JSONDecodeError, OSError) as exc:
        print(f"airplane-notifier: ignoring unreadable token ({exc})", file=diagnostic)
        return None


def _save(creds: Credentials) -> None:
    """Write the token atomically.

    A truncating write that dies midway leaves an unparseable token and costs a
    re-authorization; state.json already uses this pattern.
    """
    ensure_config_dir()
    tmp = paths.TOKEN_PATH.with_suffix(".json.tmp")
    try:
        tmp.write_text(creds.to_json(), encoding="utf-8")
        os.replace(tmp, paths.TOKEN_PATH)
    except OSError as exc:
        print(f"airplane-notifier: could not save token: {exc}", file=diagnostic)
        try:
            tmp.unlink()
        except OSError:
            pass


def _run_flow() -> Credentials:
    """Run the browser consent flow and persist the result.

    ``port=0`` asks the OS for a free ephemeral port. Hardcoding 8080 means
    authorization fails whenever anything else already holds that port
    (plan review P1 fix 3); the library derives the redirect URI from
    whatever port it actually got.
    """
    client_secrets = credentials_path()
    if not client_secrets.exists():
        raise FileNotFoundError(CREDENTIALS_HELP)

    flow = InstalledAppFlow.from_client_secrets_file(str(client_secrets), SCOPES)
    creds = flow.run_local_server(port=0)
    _save(creds)
    return creds


def get_credentials() -> Credentials:
    """Return usable credentials, re-authorizing only when unavoidable.

    Order: a valid saved token, then a silent refresh, then the browser flow.

    Raises:
        FileNotFoundError: ``credentials.json`` is missing. The caller is
            expected to surface this as a tray notification and keep running
            rather than exiting.
    """
    creds = _load_saved()

    if creds is not None:
        if creds.valid:
            return creds

        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except RefreshError as exc:
                # Revoked, or unused past Google's six-month window. Drop it
                # and re-authorize; without this the app crashes on startup.
                print(
                    f"airplane-notifier: refresh rejected ({exc}); re-authorizing",
                    file=diagnostic,
                )
                clear_credentials()
            except GoogleAuthError as exc:
                # TransportError is a SIBLING of RefreshError, not a subclass,
                # so a network failure during refresh lands here. This is the
                # normal case at Windows login: the token expired overnight and
                # Wi-Fi is not up yet. Re-authorizing would be wrong -- the
                # token is fine -- so signal the caller to retry later.
                raise TransientAuthError(str(exc)) from exc
            else:
                _save(creds)
                return creds

    return _run_flow()
