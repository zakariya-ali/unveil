"""
Riot Sign-On (RSO) login via the user's system browser + manual URL paste.

The only redirect_uri Riot accepts for play-valorant-web-prod is
https://playvalorant.com/opt_in. Riot redirects there with tokens in the
URL fragment after login. The page shows a 404 (not meant to be visited),
but the URL bar contains the tokens.

Flow:
  1. open_login_page() opens system browser to Riot login
  2. User logs in
  3. User is redirected to playvalorant.com/opt_in#access_token=...
  4. User copies the full URL and pastes back to the GUI
  5. finalize_rso_login(url) parses token + fetches entitlements + userinfo

No pywebview, no pythonnet, no localhost server.
"""
import webbrowser
import urllib.parse

import requests


_COUNTRY_TO_REGION = {
    'pl': 'eu', 'de': 'eu', 'fr': 'eu', 'gb': 'eu', 'es': 'eu', 'it': 'eu',
    'tr': 'eu', 'ru': 'eu', 'ua': 'eu', 'cz': 'eu', 'nl': 'eu', 'se': 'eu',
    'us': 'na', 'ca': 'na', 'mx': 'na',
    'br': 'br',
    'kr': 'kr',
    'jp': 'ap', 'sg': 'ap', 'au': 'ap', 'hk': 'ap', 'tw': 'ap',
    'ar': 'latam', 'cl': 'latam', 'co': 'latam', 'pe': 'latam',
}


RIOT_AUTH_URL = (
    "https://auth.riotgames.com/authorize"
    "?client_id=play-valorant-web-prod"
    "&nonce=1"
    "&redirect_uri=https%3A%2F%2Fplayvalorant.com%2Fopt_in"
    "&response_type=token%20id_token"
    "&scope=account%20openid"
)


def country_to_region(country):
    if not country:
        return None
    country = country.lower()
    for prefix, region in [
        ('eu', 'eu'), ('na', 'na'), ('la', 'latam'), ('br', 'br'),
        ('kr', 'kr'), ('jp', 'ap'), ('ap', 'ap'), ('tr', 'eu'), ('ru', 'eu'),
    ]:
        if country.startswith(prefix):
            return region
    return _COUNTRY_TO_REGION.get(country)


class LoginCancelled(Exception):
    pass


class LoginFailed(Exception):
    pass


def open_login_page():
    try:
        webbrowser.open(RIOT_AUTH_URL)
        return True
    except Exception:
        return False


def parse_callback_url(url):
    """
    Parse a pasted playvalorant.com/opt_in URL and extract tokens from fragment.
    Returns dict {access_token, id_token, expires_in} or raises.
    """
    if not url:
        raise LoginFailed("Empty URL")

    url = url.strip().strip('"').strip("'")

    if 'access_token=' not in url:
        raise LoginFailed(
            "Pasted URL does not contain an access_token. "
            "Make sure you paste the full URL after successful login."
        )

    if '#' in url and 'access_token=' in url.split('#', 1)[1]:
        tail = url.split('#', 1)[1]
    elif '?' in url and 'access_token=' in url.split('?', 1)[1]:
        tail = url.split('?', 1)[1]
    else:
        raise LoginFailed("Could not locate access_token in URL")

    params = {}
    for item in tail.split('&'):
        if '=' in item:
            k, v = item.split('=', 1)
            params[k] = urllib.parse.unquote(v)

    if not params.get('access_token'):
        raise LoginFailed("access_token is empty in pasted URL")

    try:
        expires_in = int(params.get('expires_in', 3600))
    except (ValueError, TypeError):
        expires_in = 3600

    return {
        'access_token': params['access_token'],
        'id_token': params.get('id_token'),
        'expires_in': expires_in,
    }


def fetch_entitlements(access_token, session=None):
    s = session or requests
    r = s.post(
        'https://entitlements.auth.riotgames.com/api/token/v1',
        headers={'Authorization': f'Bearer {access_token}'},
        json={},
        timeout=10,
    )
    r.raise_for_status()
    return r.json()['entitlements_token']


def fetch_userinfo(access_token, session=None):
    s = session or requests
    r = s.post(
        'https://auth.riotgames.com/userinfo',
        headers={'Authorization': f'Bearer {access_token}'},
        timeout=10,
    )
    r.raise_for_status()
    data = r.json()
    acct = data.get('acct') or {}
    return {
        'puuid': data.get('sub'),
        'country': data.get('country'),
        'name': acct.get('game_name'),
        'tag': acct.get('tag_line'),
    }


def finalize_rso_login(pasted_url, http_session=None):
    """Parse the pasted URL, then call Riot to get entitlements + userinfo."""
    tokens = parse_callback_url(pasted_url)
    access = tokens['access_token']

    entitlements = fetch_entitlements(access, session=http_session)
    userinfo = fetch_userinfo(access, session=http_session)

    if not userinfo.get('puuid'):
        raise LoginFailed("Could not retrieve PUUID from userinfo")

    region = country_to_region(userinfo.get('country'))

    return {
        'access_token': access,
        'entitlement_token': entitlements,
        'puuid': userinfo['puuid'],
        'name': userinfo.get('name'),
        'tag': userinfo.get('tag'),
        'region_hint': region,
    }
