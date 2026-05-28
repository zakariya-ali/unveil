import os
import re
import sys
import time
import base64
import json
from datetime import datetime, timezone
import requests
from requests.auth import HTTPBasicAuth
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

def _load_api_key():
    """
    Loads Henrik API key. In development, returns from env or fallback.
    Build script (build.bat) replaces _OBF_KEY with obfuscated bytes of your real key.
    """
    if _OBF_KEY:
        xor = b'Av9KmR7pZ3'
        return bytes(_OBF_KEY[i] ^ xor[i % len(xor)] for i in range(len(_OBF_KEY))).decode()
    return os.environ.get('HENRIK_API_KEY', 'HDEV-64131666-815f-4bda-a410-33d0d1de9f87')


_OBF_KEY = b''
HENRIK_API_KEY = _load_api_key()

MATCHES_TO_FETCH = 10
KD_CHEATER_MIN = 1.5
KD_SMURF_MIN = 1.3
KD_WEAK_MAX = 0.8
GAP_BOUGHT_DAYS = 60
HS_HIGH_MIN = 30.0
WINRATE_HIGH_MIN = 60.0
ANALYZED_MODES = {'competitive'}
MIN_MATCHES_FOR_JUDGMENT = 3

SESSION = requests.Session()
SESSION.verify = False


def read_lockfile():
    path = os.path.join(
        os.environ['LOCALAPPDATA'],
        'Riot Games', 'Riot Client', 'Config', 'lockfile'
    )
    if not os.path.exists(path):
        raise FileNotFoundError("Lockfile not found — launch Valorant first.")
    with open(path) as f:
        name, pid, port, password, protocol = f.read().strip().split(':')
    return {'port': port, 'password': password, 'protocol': protocol}


def get_auth_tokens(lockfile):
    url = f"{lockfile['protocol']}://127.0.0.1:{lockfile['port']}/entitlements/v1/token"
    r = SESSION.get(url, auth=HTTPBasicAuth('riot', lockfile['password']), timeout=10)
    r.raise_for_status()
    d = r.json()
    return {
        'access_token': d['accessToken'],
        'entitlement_token': d['token'],
        'puuid': d['subject'],
    }


def _read_shooter_log():
    log_path = os.path.join(
        os.environ['LOCALAPPDATA'],
        'VALORANT', 'Saved', 'Logs', 'ShooterGame.log'
    )
    if not os.path.exists(log_path):
        return None
    with open(log_path, 'r', encoding='utf-8', errors='ignore') as f:
        return f.read()


def get_region_shard(lockfile):
    log = _read_shooter_log()
    if log:
        match = re.search(r'https?://glz-([a-z]+)-\d+\.([a-z]+)\.a\.pvp\.net', log)
        if match:
            return match.group(1), match.group(2)

    url = f"{lockfile['protocol']}://127.0.0.1:{lockfile['port']}/riotclient/region-locale"
    r = SESSION.get(url, auth=HTTPBasicAuth('riot', lockfile['password']), timeout=10)
    r.raise_for_status()
    region = r.json()['region'].lower()
    shard = 'na' if region in ('latam', 'br') else region
    return region, shard


def get_client_version():
    log = _read_shooter_log()
    if log:
        match = re.search(r'CI Server Version: (\S+)', log)
        if match:
            return match.group(1)
        match = re.search(r'Build Version: (\S+)', log)
        if match:
            return match.group(1)

    r = SESSION.get('https://valorant-api.com/v1/version', timeout=10)
    r.raise_for_status()
    return r.json()['data']['riotClientVersion']


def get_client_platform():
    platform = {
        "platformType": "PC",
        "platformOS": "Windows",
        "platformOSVersion": "10.0.19042.1.256.64bit",
        "platformChipset": "Unknown",
    }
    return base64.b64encode(json.dumps(platform).encode()).decode()


def build_headers(tokens, version, platform):
    return {
        'Authorization': f"Bearer {tokens['access_token']}",
        'X-Riot-Entitlements-JWT': tokens['entitlement_token'],
        'X-Riot-ClientVersion': version,
        'X-Riot-ClientPlatform': platform,
    }


def _region_to_shard(region):
    r = (region or '').lower()
    return 'na' if r in ('latam', 'br') else r


def _open_session(rso_session=None, region_override=None):
    """
    Build (tokens, region, shard, headers) from either:
    - lockfile (rso_session=None) — default 1PC mode
    - pre-obtained RSO tokens — 2PC mode
    """
    if rso_session is None:
        lockfile = read_lockfile()
        tokens = get_auth_tokens(lockfile)
        region, shard = get_region_shard(lockfile)
    else:
        tokens = {
            'access_token': rso_session['access_token'],
            'entitlement_token': rso_session['entitlement_token'],
            'puuid': rso_session['puuid'],
        }
        region = (
            region_override
            or rso_session.get('region_hint')
            or 'eu'
        ).lower()
        shard = _region_to_shard(region)

    version = get_client_version()
    headers = build_headers(tokens, version, get_client_platform())
    return tokens, region, shard, headers


def find_current_match(shard, region, puuid, headers):
    base = f"https://glz-{region}-1.{shard}.a.pvp.net"

    for match_type, endpoint in (('coregame', 'core-game'), ('pregame', 'pregame')):
        url = f"{base}/{endpoint}/v1/players/{puuid}"
        try:
            r = SESSION.get(url, headers=headers, timeout=10)
        except requests.RequestException as e:
            print(f"! Network error while checking {match_type}: {e}")
            continue

        if r.status_code == 200:
            return match_type, r.json()['MatchID']
        if r.status_code == 404:
            continue
        print(f"! Unexpected status {r.status_code} from endpoint {endpoint}")

    return None, None


def get_match_data(shard, region, match_id, match_type, headers):
    base = f"https://glz-{region}-1.{shard}.a.pvp.net"
    endpoint = 'pregame' if match_type == 'pregame' else 'core-game'
    url = f"{base}/{endpoint}/v1/matches/{match_id}"
    r = SESSION.get(url, headers=headers, timeout=10)
    r.raise_for_status()
    return r.json()


def fetch_match_details_riot(shard, match_id, headers):
    """Fetch match-details from Riot PVP for a finished match (used in historic analysis)."""
    url = f"https://pd.{shard}.a.pvp.net/match-details/v1/matches/{match_id}"
    try:
        r = SESSION.get(url, headers=headers, timeout=10)
        if r.status_code != 200:
            return None
        return r.json()
    except requests.RequestException:
        return None



def resolve_names_local(shard, puuids, headers):
    url = f"https://pd.{shard}.a.pvp.net/name-service/v2/players"
    r = SESSION.put(url, headers=headers, json=puuids, timeout=10)
    r.raise_for_status()
    return {p['Subject']: f"{p['GameName']}#{p['TagLine']}" for p in r.json()}


def _henrik_lookup_one(puuid, henrik_headers):
    try:
        url = f"https://api.henrikdev.xyz/valorant/v2/by-puuid/account/{puuid}"
        r = SESSION.get(url, headers=henrik_headers, timeout=12)
        if r.status_code == 200:
            data = r.json().get('data', {})
            name = data.get('name')
            tag = data.get('tag')
            if name and tag:
                return puuid, f"{name}#{tag}", None, None
            return puuid, None, None, None
        if r.status_code == 429:
            try:
                retry_after = int(r.headers.get('retry-after', '60'))
            except (ValueError, TypeError):
                retry_after = 60
            return puuid, None, 429, retry_after
        return puuid, None, r.status_code, None
    except requests.RequestException as e:
        return puuid, None, f"exception:{e}", None


def resolve_names_henrik(puuids):
    resolved = {}
    rate_limit = {'hit': False, 'retry_after': 0}
    if not puuids:
        return resolved, rate_limit

    henrik_headers = {"Authorization": HENRIK_API_KEY}
    status_counts = {}
    not_found = 0

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_henrik_lookup_one, p, henrik_headers) for p in puuids]
        for fut in as_completed(futures):
            puuid, name, err, retry_after = fut.result()
            if name:
                resolved[puuid] = name
                continue
            if err is None:
                not_found += 1
            else:
                status_counts[err] = status_counts.get(err, 0) + 1
                if err == 429 and retry_after:
                    rate_limit['hit'] = True
                    rate_limit['retry_after'] = max(rate_limit['retry_after'], retry_after)

    missed = len(puuids) - len(resolved)
    if missed:
        detail = []
        if not_found:
            detail.append(f"{not_found}x no data in Henrik (private/new account)")
        for code, count in status_counts.items():
            detail.append(f"{count}x status {code}")
        print(f"  (Henrik: {len(resolved)}/{len(puuids)} resolved; {', '.join(detail)})")

        if 403 in status_counts:
            print("  ! Error 403: HenrikDev API key is inactive or invalid.")
        if 429 in status_counts:
            print(f"  ! Rate limit hit on HenrikDev API. Retry after {rate_limit['retry_after']}s")

    return resolved, rate_limit


_PLAYER_PLACEHOLDER = re.compile(r'^Player\d*$', re.IGNORECASE)


def _looks_hidden(name):
    if not name:
        return True
    base = name.split('#', 1)[0].strip()
    if not base:
        return True
    return bool(_PLAYER_PLACEHOLDER.fullmatch(base))


def resolve_names_with_fallback(shard, puuids, headers, match_players_data, match_type):
    rate_limit = {'hit': False, 'retry_after': 0}
    try:
        names = resolve_names_local(shard, puuids, headers)
    except Exception as e:
        print(f"! Local name-service error: {e}")
        names = {}

    if match_type == 'coregame':
        incognito_map = {
            p['Subject']: p.get('IsIncognito', False) for p in match_players_data
        }
    else:
        incognito_map = {
            p['Subject']: p.get('PlayerIdentity', {}).get('Incognito', False)
            for p in match_players_data
        }

    to_reveal = [
        puuid for puuid in puuids
        if incognito_map.get(puuid) or _looks_hidden(names.get(puuid, ""))
    ]

    if to_reveal:
        print(f"--- Unmasking {len(to_reveal)} players via HenrikDev API ---")
        revealed, rate_limit = resolve_names_henrik(to_reveal)
        for puuid, real_name in revealed.items():
            names[puuid] = f"🕵️ {real_name}"
        for puuid in to_reveal:
            if puuid not in revealed:
                current = names.get(puuid, "")
                if not current or current == "#" or _looks_hidden(current):
                    names[puuid] = f"❓ hidden-{puuid[:8]}"

    return names, rate_limit


def fetch_riot_match_history(shard, puuid, headers, count=MATCHES_TO_FETCH):
    """Fetch match history list (matchIDs only) from Riot PVP. Free, no Henrik points."""
    url = f"https://pd.{shard}.a.pvp.net/match-history/v1/history/{puuid}"
    params = {"startIndex": 0, "endIndex": count}
    try:
        r = SESSION.get(url, headers=headers, params=params, timeout=10)
        if r.status_code != 200:
            return None, r.status_code
        return r.json().get('History', []), None
    except requests.RequestException as e:
        return None, f"exception:{e}"


def fetch_riot_match_details_cached(shard, match_id, headers, cache):
    """Fetch match details with an in-memory dedupe cache (match shared between players)."""
    if match_id in cache:
        return cache[match_id]
    details = fetch_match_details_riot(shard, match_id, headers)
    cache[match_id] = details
    return details


def analyze_all_players_riot(shard, puuids, headers, count=MATCHES_TO_FETCH):
    """
    Riot-only pipeline for analyzing 10 players' stats. Zero Henrik points.

    Two phases:
    1. Fetch match history for each player (10 requests, parallel)
    2. Fetch match-details for each unique match_id (dedupes across players, parallel)
    """
    results = {}
    status_counts = {}

    # Phase 1: match history per player
    histories = {}
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {
            pool.submit(fetch_riot_match_history, shard, p, headers, count): p
            for p in puuids
        }
        for fut in as_completed(futures):
            puuid = futures[fut]
            try:
                history, err = fut.result()
            except Exception as e:
                status_counts[f"history_exc:{e}"] = status_counts.get(f"history_exc:{e}", 0) + 1
                histories[puuid] = []
                continue
            if err is not None:
                status_counts[f"history_{err}"] = status_counts.get(f"history_{err}", 0) + 1
                histories[puuid] = []
            else:
                histories[puuid] = history or []

    # Collect unique match_ids (dedupe: if 5 players in same lobby, past matches are shared)
    all_match_ids = set()
    for history in histories.values():
        for entry in history:
            mid = entry.get('MatchID')
            if mid:
                all_match_ids.add(mid)

    # Phase 2: fetch each unique match-detail
    details_by_id = {}
    if all_match_ids:
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {
                pool.submit(fetch_match_details_riot, shard, mid, headers): mid
                for mid in all_match_ids
            }
            for fut in as_completed(futures):
                mid = futures[fut]
                try:
                    details_by_id[mid] = fut.result()
                except Exception:
                    details_by_id[mid] = None

    # Phase 3: compute stats per player from their matches
    for puuid, history in histories.items():
        player_matches = []
        for entry in history:
            mid = entry.get('MatchID')
            ts_ms = entry.get('GameStartTime', 0)
            details = details_by_id.get(mid) if mid else None
            if not details:
                continue
            # Augment details with the game_start_time from history (Riot details has matchInfo.gameStartMillis too, but history's GameStartTime is reliable)
            details['_history_start_ms'] = ts_ms
            details['_queue_id_from_history'] = entry.get('QueueID', '')
            player_matches.append(details)
        results[puuid] = _compute_stats_from_henrik(puuid, player_matches)

    if status_counts:
        detail = [f"{c}x {s}" for s, c in status_counts.items()]
        print(f"  (Riot: {', '.join(detail)})")

    return results


def fetch_henrik_stored_matches(region, puuid, mode='competitive', size=200):
    """
    Fetch all stored matches Henrik has for this player, filtered to a specific mode.
    Returns (matches, status, rate_meta).
    Uses 1 Henrik point per call (the endpoint is server-cached).
    """
    url = f"https://api.henrikdev.xyz/valorant/v1/by-puuid/stored-matches/{region}/{puuid}"
    headers = {"Authorization": HENRIK_API_KEY}
    params = {"mode": mode, "size": size}
    try:
        r = SESSION.get(url, headers=headers, params=params, timeout=20)
        remaining = None
        try:
            remaining = int(r.headers.get('x-ratelimit-remaining', -1))
        except (ValueError, TypeError):
            pass
        if r.status_code == 429:
            try:
                retry_after = int(r.headers.get('retry-after', '60'))
            except (ValueError, TypeError):
                retry_after = 60
            return None, 429, {'retry_after': retry_after, 'remaining': remaining}
        if r.status_code == 200:
            data = r.json().get('data', [])
            return data, None, {'remaining': remaining}
        if r.status_code == 404:
            return [], 404, {'remaining': remaining}
        # Other errors — log response body for diagnosis
        body = ''
        try:
            body = r.text[:300]
        except Exception:
            pass
        print(f"    [fetch err] {r.status_code} | URL={url}?mode={mode} | body={body}")
        return None, r.status_code, {'remaining': remaining}
    except requests.RequestException as e:
        print(f"    [fetch exc] {type(e).__name__}: {e}")
        return None, f"exception:{e}", None


def analyze_all_players_henrik_stored(region, puuids, mode='competitive'):
    """
    Fetch stored Henrik matches for all 10 players in parallel.
    Zero fallback: players without stored data get empty stats (❓ no-data tag).

    Cost: 10 Henrik points per run (1 per player).
    """
    results = {}
    rate_limit = {'hit': False, 'retry_after': 0, 'min_remaining': None}
    status_counts = {}

    print(f"\n=== DIAGNOSTIC: Henrik stored-matches for {len(puuids)} players, region={region}, mode={mode} ===")

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {
            pool.submit(fetch_henrik_stored_matches, region, p, mode): p
            for p in puuids
        }
        for fut in as_completed(futures):
            puuid = futures[fut]
            puuid_short = puuid[:8]
            try:
                matches, status, meta = fut.result()
            except Exception as e:
                print(f"  [{puuid_short}] EXCEPTION: {type(e).__name__}: {e}")
                status_counts[f"exc:{type(e).__name__}"] = status_counts.get(
                    f"exc:{type(e).__name__}", 0
                ) + 1
                results[puuid] = _compute_stats_from_henrik(puuid, [])
                continue

            remaining = (meta or {}).get('remaining')
            if remaining is not None:
                if rate_limit['min_remaining'] is None or remaining < rate_limit['min_remaining']:
                    rate_limit['min_remaining'] = remaining

            if status == 429:
                print(f"  [{puuid_short}] 429 RATE LIMIT, retry_after={meta.get('retry_after')}s, remaining={remaining}")
                rate_limit['hit'] = True
                rate_limit['retry_after'] = max(
                    rate_limit['retry_after'], (meta or {}).get('retry_after', 60)
                )
                status_counts['429'] = status_counts.get('429', 0) + 1
                results[puuid] = _compute_stats_from_henrik(puuid, [])
                continue

            if status == 404:
                print(f"  [{puuid_short}] 404 — Henrik has NO stored data for this player (remaining={remaining})")
                status_counts['404'] = status_counts.get('404', 0) + 1
                results[puuid] = _compute_stats_from_henrik(puuid, [])
                continue

            if status is not None:
                print(f"  [{puuid_short}] HTTP {status} — unexpected error (remaining={remaining})")
                status_counts[str(status)] = status_counts.get(str(status), 0) + 1
                results[puuid] = _compute_stats_from_henrik(puuid, [])
                continue

            # status == None means success (200)
            match_count = len(matches or [])
            print(f"  [{puuid_short}] OK — received {match_count} matches (remaining={remaining})")

            # DIAGNOSTIC: dump structure of first match, once per run
            if match_count > 0 and not hasattr(analyze_all_players_henrik_stored, '_dumped'):
                analyze_all_players_henrik_stored._dumped = True
                first = matches[0]
                print(f"    [RAW STRUCTURE] top-level keys: {list(first.keys())}")
                if 'metadata' in first:
                    print(f"    [RAW STRUCTURE] metadata keys: {list(first['metadata'].keys())}")
                    print(f"    [RAW STRUCTURE] metadata.mode_id: {first['metadata'].get('mode_id')!r}")
                    print(f"    [RAW STRUCTURE] metadata.queue: {first['metadata'].get('queue')!r}")
                if 'players' in first:
                    players = first['players']
                    if isinstance(players, dict):
                        print(f"    [RAW STRUCTURE] players is DICT with keys: {list(players.keys())}")
                        aps = players.get('all_players', [])
                        print(f"    [RAW STRUCTURE] players.all_players length: {len(aps)}")
                        if aps:
                            print(f"    [RAW STRUCTURE] first player keys: {list(aps[0].keys())}")
                            print(f"    [RAW STRUCTURE] first player puuid: {aps[0].get('puuid', 'NONE')[:12]}...")
                    elif isinstance(players, list):
                        print(f"    [RAW STRUCTURE] players is LIST, length: {len(players)}")
                        if players:
                            print(f"    [RAW STRUCTURE] first player keys: {list(players[0].keys())}")
                if 'teams' in first:
                    teams = first['teams']
                    if isinstance(teams, dict):
                        print(f"    [RAW STRUCTURE] teams is DICT with keys: {list(teams.keys())}")
                    elif isinstance(teams, list):
                        print(f"    [RAW STRUCTURE] teams is LIST, length: {len(teams)}")

            computed = _compute_stats_from_henrik(puuid, matches or [])
            print(f"    [PARSED] {computed['matches']} matches analyzed, KD={computed.get('kd')}, WR={computed.get('winrate')}")
            results[puuid] = computed

    print(f"=== END DIAGNOSTIC (summary: {status_counts}) ===\n")

    if status_counts:
        detail = [f"{c}x {s}" for s, c in status_counts.items()]
        print(f"  (Henrik stored: {', '.join(detail)})")

    return results, rate_limit


def fetch_henrik_matches(region, puuid, size=MATCHES_TO_FETCH):
    url = f"https://api.henrikdev.xyz/valorant/v4/by-puuid/matches/{region}/pc/{puuid}"
    headers = {"Authorization": HENRIK_API_KEY}
    params = {"size": size}
    try:
        r = SESSION.get(url, headers=headers, params=params, timeout=20)
        retry_after = None
        remaining = None
        try:
            remaining = int(r.headers.get('x-ratelimit-remaining', -1))
        except (ValueError, TypeError):
            pass
        if r.status_code == 429:
            try:
                retry_after = int(r.headers.get('retry-after', '60'))
            except (ValueError, TypeError):
                retry_after = 60
            return None, 429, {'retry_after': retry_after, 'remaining': remaining}
        if r.status_code == 200:
            return r.json().get('data', []), None, {'remaining': remaining}
        return None, r.status_code, {'remaining': remaining}
    except requests.RequestException as e:
        return None, f"exception:{e}", None


def classify_player(kd, hs_pct, winrate, winrate_sample, max_gap_days, matches):
    if matches < MIN_MATCHES_FOR_JUDGMENT:
        return "❓ new/no-data"

    tags = []

    if kd is None:
        tags.append("✅ normal")
    elif kd >= KD_CHEATER_MIN:
        tags.append("🚨 cheater?")
    elif kd >= KD_SMURF_MIN:
        tags.append("🔥 smurf?")
    elif kd < KD_WEAK_MAX:
        tags.append("🐢 weak")
    else:
        tags.append("✅ normal")

    if max_gap_days is not None and max_gap_days > GAP_BOUGHT_DAYS:
        tags.append("💰 bought?")

    if hs_pct is not None and hs_pct > HS_HIGH_MIN:
        tags.append(f"🎯 HIGH HEADSHOT RATE > {int(HS_HIGH_MIN)}")

    if winrate is not None and winrate > WINRATE_HIGH_MIN:
        tags.append(
            f"📈 SUS WINRATE > {int(WINRATE_HIGH_MIN)}% in last {winrate_sample} matches"
        )

    return " ".join(tags)


def _parse_ts_to_ms(match):
    """
    Extract timestamp in ms. Supports:
    - Riot: match.matchInfo.gameStartMillis OR match._history_start_ms (we inject it)
    - Henrik v4: metadata.started_at (ISO)
    - Henrik v3: metadata.game_start (unix seconds)
    """
    # Riot injected from history
    hist_ms = match.get('_history_start_ms')
    if hist_ms:
        return int(hist_ms)
    # Riot matchInfo
    mi = match.get('matchInfo')
    if isinstance(mi, dict):
        gsm = mi.get('gameStartMillis')
        if gsm:
            return int(gsm)
    metadata = match.get('metadata') or {}
    iso = metadata.get('started_at')
    if iso:
        try:
            dt = datetime.fromisoformat(iso.replace('Z', '+00:00'))
            return int(dt.timestamp() * 1000)
        except (ValueError, AttributeError):
            pass
    gs = metadata.get('game_start')
    if gs:
        return int(gs) * 1000
    return 0


# Riot queue id → our canonical mode name
_RIOT_QUEUE_MAP = {
    'competitive': 'competitive',
    'unrated': 'unrated',
    'deathmatch': 'deathmatch',
    'spikerush': 'spikerush',
    'swiftplay': 'swiftplay',
    'ggteam': 'escalation',
    'hurm': 'teamdeathmatch',
    'newmap': 'newmap',
    'premier': 'premier',
}


def _extract_mode(match):
    """
    Extract mode string. Supports:
    - Riot: match.matchInfo.queueId OR match._queue_id_from_history
    - Henrik v4: metadata.queue.id
    - Henrik v3: metadata.mode_id / metadata.mode
    """
    hist_q = match.get('_queue_id_from_history')
    if hist_q:
        return _RIOT_QUEUE_MAP.get(hist_q.lower(), hist_q.lower())
    mi = match.get('matchInfo')
    if isinstance(mi, dict):
        qid = mi.get('queueId') or mi.get('queueID')
        if qid:
            return _RIOT_QUEUE_MAP.get(qid.lower(), qid.lower())
    metadata = match.get('metadata') or {}
    queue = metadata.get('queue')
    if isinstance(queue, dict):
        mode = queue.get('id') or queue.get('mode_type')
        if mode:
            return mode.lower()
    return (metadata.get('mode_id') or metadata.get('mode') or '').lower()


def _find_player(match, puuid):
    """
    Find player object. Supports:
    - Riot: match.players[] with 'subject' field
    - Henrik v4: match.players[] flat with 'puuid' field
    - Henrik v3: match.players.all_players[] with 'puuid' field
    """
    players = match.get('players')
    if isinstance(players, list):
        for p in players:
            if p.get('puuid') == puuid or p.get('subject') == puuid:
                return p
    elif isinstance(players, dict):
        for p in players.get('all_players', []):
            if p.get('puuid') == puuid or p.get('subject') == puuid:
                return p
    return None


def _extract_team_id(player_data):
    """Riot: teamId. Henrik v4: team_id. Henrik v3: team."""
    return (
        player_data.get('teamId')
        or player_data.get('team_id')
        or player_data.get('team')
        or ''
    ).lower()


def _team_won(match, team_id):
    """
    teams can be:
    - Riot / Henrik v4: list of objects with teamId/team_id and won
    - Henrik v3: dict {red: {...}, blue: {...}}
    """
    teams = match.get('teams')
    if isinstance(teams, list):
        for t in teams:
            tid = (t.get('teamId') or t.get('team_id') or t.get('id') or '').lower()
            if tid == team_id:
                if t.get('won') is not None:
                    return bool(t['won'])
                if t.get('has_won') is not None:
                    return bool(t['has_won'])
        return None
    elif isinstance(teams, dict):
        t = teams.get(team_id) or teams.get(team_id.capitalize())
        if isinstance(t, dict):
            return bool(t.get('has_won') or t.get('won'))
    return None


def _extract_shots(match, player_data, puuid):
    """
    Shot breakdown per match:
    - Riot: match.roundResults[].playerStats[].damage[] with headshots/bodyshots/legshots
    - Henrik v4: player_data.stats.{headshots,bodyshots,legshots}  (pre-aggregated)
    - Henrik stored: player_data.shots.{head,body,leg}

    Returns (head, body, leg) for this single match.
    """
    stats = player_data.get('stats') or {}
    h = stats.get('headshots')
    b = stats.get('bodyshots')
    l = stats.get('legshots')
    if h is not None or b is not None or l is not None:
        return h or 0, b or 0, l or 0
    shots = player_data.get('shots') or stats.get('shots') or {}
    if shots.get('head') is not None or shots.get('body') is not None or shots.get('leg') is not None:
        return shots.get('head', 0) or 0, shots.get('body', 0) or 0, shots.get('leg', 0) or 0

    # Riot format: aggregate from roundResults
    h = b = l = 0
    for rr in match.get('roundResults', []) or []:
        for ps in rr.get('playerStats', []) or []:
            if ps.get('subject') == puuid:
                for dmg in ps.get('damage', []) or []:
                    h += dmg.get('headshots', 0) or 0
                    b += dmg.get('bodyshots', 0) or 0
                    l += dmg.get('legshots', 0) or 0
                break
    return h, b, l


# Riot competitive tier integer → name table
_RIOT_TIER_NAMES = {
    0: 'Unrated', 3: 'Iron 1', 4: 'Iron 2', 5: 'Iron 3',
    6: 'Bronze 1', 7: 'Bronze 2', 8: 'Bronze 3',
    9: 'Silver 1', 10: 'Silver 2', 11: 'Silver 3',
    12: 'Gold 1', 13: 'Gold 2', 14: 'Gold 3',
    15: 'Platinum 1', 16: 'Platinum 2', 17: 'Platinum 3',
    18: 'Diamond 1', 19: 'Diamond 2', 20: 'Diamond 3',
    21: 'Ascendant 1', 22: 'Ascendant 2', 23: 'Ascendant 3',
    24: 'Immortal 1', 25: 'Immortal 2', 26: 'Immortal 3',
    27: 'Radiant',
}


def _extract_player_tier(player_data):
    """
    Extract (tier_id, tier_name).
    - Riot: player_data.competitiveTier (int)
    - Henrik v4: player_data.tier.{id,name}
    - Henrik v3: player_data.currenttier + currenttier_patched
    """
    tier = player_data.get('tier')
    if isinstance(tier, dict):
        tid = tier.get('id')
        tname = tier.get('name')
        if tid is not None:
            return tid, tname
    # Riot format
    ct = player_data.get('competitiveTier')
    if ct is not None:
        return ct, _RIOT_TIER_NAMES.get(ct, f"Tier {ct}")
    tid = player_data.get('currenttier')
    tname = player_data.get('currenttier_patched')
    if tid is not None:
        return tid, tname
    return None, None


def _compute_stats_from_henrik(puuid, matches):
    if not matches:
        return {
            'matches': 0, 'kills': 0, 'deaths': 0, 'assists': 0,
            'kd': None, 'hs_pct': None,
            'wins': 0, 'matches_with_result': 0, 'winrate': None,
            'max_gap_days': 0.0,
            'current_tier_id': None, 'current_tier_name': None,
            'category': classify_player(None, None, None, 0, None, 0),
        }

    kills = deaths = assists = matches_analyzed = 0
    headshots = bodyshots = legshots = 0
    wins = matches_with_result = 0
    timestamps = []
    latest_tier = None
    latest_tier_ts = -1

    for m in matches:
        # stored-matches format has top-level keys 'meta', 'stats', 'teams'
        # (it's a pre-aggregated per-player match summary, not full match data)
        if 'meta' in m and 'stats' in m and 'players' not in m:
            meta = m.get('meta') or {}
            s = m.get('stats') or {}
            teams = m.get('teams') or {}

            # Mode filter (stored-matches uses meta.mode as string like "Competitive")
            mode_raw = (meta.get('mode') or '').lower()
            mode_id = (meta.get('mode_id') or '').lower()
            mode = mode_id or mode_raw
            if mode and mode not in ANALYZED_MODES:
                continue

            # Timestamp — stored-matches uses started_at ISO string
            started_at = meta.get('started_at')
            if started_at:
                try:
                    dt = datetime.fromisoformat(started_at.replace('Z', '+00:00'))
                    timestamps.append(int(dt.timestamp() * 1000))
                except (ValueError, AttributeError):
                    pass

            matches_analyzed += 1
            kills += s.get('kills', 0) or 0
            deaths += s.get('deaths', 0) or 0
            assists += s.get('assists', 0) or 0

            # Shots — stored-matches has stats.shots.{head,body,leg}
            shots = s.get('shots') or {}
            headshots += shots.get('head', 0) or 0
            bodyshots += shots.get('body', 0) or 0
            legshots += shots.get('leg', 0) or 0

            # Tier — stored-matches uses stats.tier (integer)
            tier_id = s.get('tier')
            if tier_id is not None and tier_id > 0:
                ts = timestamps[-1] if timestamps else 0
                if ts > latest_tier_ts:
                    latest_tier = (tier_id, _RIOT_TIER_NAMES.get(tier_id, f"Tier {tier_id}"))
                    latest_tier_ts = ts

            team_raw = (s.get('team') or '').lower()
            if team_raw in teams:
                team_info = teams[team_raw]
                # Sprawdzamy czy to na pewno słownik, a nie np. int (wynik)
                if isinstance(team_info, dict): 
                    won = team_info.get('has_won')
                    if won is not None:
                        matches_with_result += 1
                        if won:
                            wins += 1

            continue

        # Regular full-match format (Henrik v3/v4 or Riot PVP)
        mode = _extract_mode(m)
        if mode and mode not in ANALYZED_MODES:
            continue

        ts = _parse_ts_to_ms(m)
        if ts > 0:
            timestamps.append(ts)

        player_data = _find_player(m, puuid)
        if not player_data:
            continue

        matches_analyzed += 1
        s = player_data.get('stats', {}) or {}
        kills += s.get('kills', 0) or 0
        deaths += s.get('deaths', 0) or 0
        assists += s.get('assists', 0) or 0

        h, b, l = _extract_shots(m, player_data, puuid)
        headshots += h
        bodyshots += b
        legshots += l

        tier_id, tier_name = _extract_player_tier(player_data)
        if tier_id is not None and tier_id > 0 and ts > latest_tier_ts:
            latest_tier = (tier_id, tier_name)
            latest_tier_ts = ts

        team_id = _extract_team_id(player_data)
        if team_id:
            won = _team_won(m, team_id)
            if won is not None:
                matches_with_result += 1
                if won:
                    wins += 1

    if deaths > 0:
        kd = kills / deaths
    elif kills > 0:
        kd = float(kills)
    else:
        kd = None

    total_shots = headshots + bodyshots + legshots
    hs_pct = (headshots / total_shots * 100.0) if total_shots > 0 else None
    winrate = (wins / matches_with_result * 100.0) if matches_with_result > 0 else None

    timestamps.sort()
    max_gap_days = 0.0
    if len(timestamps) >= 2:
        gaps = [
            (timestamps[i + 1] - timestamps[i]) / 86_400_000.0
            for i in range(len(timestamps) - 1)
        ]
        max_gap_days = max(gaps)

    current_tier_id = latest_tier[0] if latest_tier else None
    current_tier_name = latest_tier[1] if latest_tier else None

    return {
        'matches': matches_analyzed,
        'kills': kills,
        'deaths': deaths,
        'assists': assists,
        'kd': kd,
        'hs_pct': hs_pct,
        'wins': wins,
        'matches_with_result': matches_with_result,
        'winrate': winrate,
        'max_gap_days': max_gap_days,
        'current_tier_id': current_tier_id,
        'current_tier_name': current_tier_name,
        'category': classify_player(
            kd, hs_pct, winrate, matches_with_result,
            max_gap_days, matches_analyzed,
        ),
    }


def analyze_all_players(region, puuids, count=MATCHES_TO_FETCH):
    results = {}
    status_counts = {}
    rate_limit = {'hit': False, 'retry_after': 0, 'min_remaining': None}

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {
            pool.submit(fetch_henrik_matches, region, p, count): p
            for p in puuids
        }
        for fut in as_completed(futures):
            puuid = futures[fut]
            try:
                matches, err, meta = fut.result()
            except Exception as e:
                status_counts[f"exception:{e}"] = status_counts.get(f"exception:{e}", 0) + 1
                results[puuid] = _compute_stats_from_henrik(puuid, [])
                continue

            if meta and meta.get('remaining') is not None and meta['remaining'] >= 0:
                if rate_limit['min_remaining'] is None or meta['remaining'] < rate_limit['min_remaining']:
                    rate_limit['min_remaining'] = meta['remaining']

            if err is not None:
                status_counts[err] = status_counts.get(err, 0) + 1
                if err == 429 and meta and meta.get('retry_after'):
                    rate_limit['hit'] = True
                    rate_limit['retry_after'] = max(rate_limit['retry_after'], meta['retry_after'])
                results[puuid] = _compute_stats_from_henrik(puuid, [])
                continue

            results[puuid] = _compute_stats_from_henrik(puuid, matches or [])

    if status_counts:
        detail = [f"{c}x status {s}" for s, c in status_counts.items()]
        print(f"  (Henrik matches: {', '.join(detail)})")
        if 403 in status_counts:
            print("  ! Error 403: HenrikDev API key is inactive or invalid.")
        if 429 in status_counts:
            print(f"  ! Rate limit hit on HenrikDev API. Retry after {rate_limit['retry_after']}s")

    return results, rate_limit


def format_kd(kd):
    if kd is None:
        return "  -  "
    return f"{kd:5.2f}"


def format_analysis(analysis):
    if not analysis:
        return f"KD: -     (0m)   ❓ no-data"
    kd = format_kd(analysis.get('kd'))
    matches = analysis.get('matches', 0)
    cat = analysis.get('category', '?')
    gap = analysis.get('max_gap_days', 0)
    hs = analysis.get('hs_pct')
    wr = analysis.get('winrate')
    hs_str = f" HS:{hs:4.1f}%" if hs is not None else " HS:  -  "
    wr_str = f" WR:{wr:4.1f}%" if wr is not None else " WR:  -  "
    gap_str = f" gap:{int(gap)}d" if gap > GAP_BOUGHT_DAYS else ""
    return f"KD:{kd}{hs_str}{wr_str} ({matches}m){gap_str}  {cat}"


_agent_cache = None
_agent_cache_lock = Lock()


def get_agent_map():
    global _agent_cache
    with _agent_cache_lock:
        if _agent_cache is None:
            r = SESSION.get('https://valorant-api.com/v1/agents?isPlayable=true', timeout=10)
            r.raise_for_status()
            _agent_cache = {
                a['uuid'].lower(): a['displayName'] for a in r.json()['data']
            }
    return _agent_cache


def agent_name(uuid, agents):
    if not uuid or uuid == '00000000-0000-0000-0000-000000000000':
        return '(not selected)'
    return agents.get(uuid.lower(), uuid)


def show_coregame(match, names, agents, analyses):
    players = match['Players']
    print(f"\n=== Match in progress — {len(players)} players ===")
    teams = {}
    for p in players:
        teams.setdefault(p['TeamID'], []).append(p)

    for team_id, team_players in teams.items():
        print(f"\n  [Team {team_id}]")
        for p in team_players:
            name = names.get(p['Subject'], '???')
            if p.get('IsIncognito') and not name.startswith("🕵️"):
                name = f"(Incognito) {name}"
            agent = agent_name(p['CharacterID'], agents)
            info = format_analysis(analyses.get(p['Subject']))
            print(f"    {name:30s}  {agent:12s}  {info}")


def show_pregame(match, names, agents, analyses):
    players = match['AllyTeam']['Players']
    print(f"\n=== Agent select — your team ({len(players)}) ===")
    for p in players:
        name = names.get(p['Subject'], '???')
        agent = agent_name(p.get('CharacterID'), agents)
        state = p.get('CharacterSelectionState') or 'selecting'
        info = format_analysis(analyses.get(p['Subject']))
        print(f"  {name:30s}  {agent:12s}  [{state:8s}]  {info}")


def print_legend():
    print("\nLegend:")
    print(f"  🚨 cheater?  KD >= {KD_CHEATER_MIN}")
    print(f"  🔥 smurf?    KD {KD_SMURF_MIN} - {KD_CHEATER_MIN}")
    print(f"  ✅ normal    KD {KD_WEAK_MAX} - {KD_SMURF_MIN}")
    print(f"  🐢 weak      KD < {KD_WEAK_MAX}")
    print(f"  💰 bought?   gap > {GAP_BOUGHT_DAYS} days in match history")
    print(f"  🎯 HIGH HS   headshot rate > {int(HS_HIGH_MIN)}%")
    print(f"  📈 SUS WR    winrate > {int(WINRATE_HIGH_MIN)}% (sample size varies)")
    print(f"  ❓ new/no-data  fewer than {MIN_MATCHES_FOR_JUDGMENT} analyzable matches")
    print("  (heuristic flags, not definitive)")


def run_analysis(progress_callback=None, rso_session=None, region_override=None):
    """
    Full analysis pipeline. Returns a dict usable by CLI or GUI.
    progress_callback: optional fn(str) called with progress messages.
    rso_session: optional RSO tokens dict for 2PC mode.
    region_override: optional region string to use in 2PC mode.
    """
    def _p(msg):
        if progress_callback:
            progress_callback(msg)

    try:
        _p("Opening session..." if rso_session else "Reading lockfile...")
        tokens, region, shard, headers = _open_session(rso_session, region_override)

        _p(f"Region: {region} | checking match state...")
        match_type, match_id = find_current_match(shard, region, tokens['puuid'], headers)
        if not match_id:
            return {
                'status': 'no_match',
                'message': 'Not currently in a match or lobby',
                'region': region,
                'shard': shard,
                'self_puuid': tokens['puuid'],
            }

        _p("Fetching match data...")
        match = get_match_data(shard, region, match_id, match_type, headers)
        agents = get_agent_map()

        players_list = (
            match['Players'] if match_type == 'coregame'
            else match['AllyTeam']['Players']
        )
        puuids = [p['Subject'] for p in players_list]

        _p(f"Resolving {len(puuids)} names...")
        names, name_rate_limit = resolve_names_with_fallback(
            shard, puuids, headers, players_list, match_type
        )

        _p(f"Analyzing {len(puuids)} players via Henrik stored-matches...")
        analyses, analyze_rate_limit = analyze_all_players_henrik_stored(
            region, puuids, mode='competitive'
        )

        combined_rate_limit = {
            'hit': name_rate_limit.get('hit', False) or analyze_rate_limit.get('hit', False),
            'retry_after': max(
                name_rate_limit.get('retry_after', 0),
                analyze_rate_limit.get('retry_after', 0),
            ),
            'min_remaining': analyze_rate_limit.get('min_remaining'),
        }

        players_out = []
        for p in players_list:
            puuid = p['Subject']
            if match_type == 'coregame':
                incog = p.get('IsIncognito', False)
                sel_state = ''
            else:
                incog = p.get('PlayerIdentity', {}).get('Incognito', False)
                sel_state = p.get('CharacterSelectionState') or 'selecting'

            name = names.get(puuid, '???')
            revealed = name.startswith("🕵️")
            hidden_fallback = name.startswith("❓")

            players_out.append({
                'puuid': puuid,
                'name': name,
                'agent_uuid': (p.get('CharacterID') or '').lower(),
                'agent_name': agent_name(p.get('CharacterID'), agents),
                'team_id': p.get('TeamID', ''),
                'is_incognito': incog,
                'is_revealed_by_henrik': revealed,
                'is_hidden_fallback': hidden_fallback,
                'selection_state': sel_state,
                'analysis': analyses.get(puuid) or {},
            })

        return {
            'status': 'ok',
            'match_type': match_type,
            'match_id': match_id,
            'region': region,
            'shard': shard,
            'self_puuid': tokens['puuid'],
            'players': players_out,
            'rate_limit': combined_rate_limit,
        }

    except FileNotFoundError as e:
        return {'status': 'error', 'error': str(e)}
    except requests.HTTPError as e:
        body = getattr(e.response, 'text', '')[:200]
        return {'status': 'error', 'error': f"HTTP error: {e} | {body}"}
    except Exception as e:
        return {'status': 'error', 'error': f"{type(e).__name__}: {e}"}


def main():
    try:
        result = run_analysis(progress_callback=print)

        if result['status'] == 'error':
            print(f"✗ {result['error']}")
            return 1
        if result['status'] == 'no_match':
            print(f"✗ {result['message']}")
            return 0

        print(f"✓ Region: {result['region']} | Shard: {result['shard']}")
        print(f"✓ PUUID: {result['self_puuid']}")

        match_type = result['match_type']
        players = result['players']

        if match_type == 'coregame':
            print(f"\n=== Match in progress — {len(players)} players ===")
            teams = {}
            for p in players:
                teams.setdefault(p['team_id'], []).append(p)
            for team_id, team_players in teams.items():
                print(f"\n  [Team {team_id}]")
                for p in team_players:
                    name = p['name']
                    if p['is_incognito'] and not p['is_revealed_by_henrik']:
                        name = f"(Incognito) {name}"
                    info = format_analysis(p['analysis'])
                    print(f"    {name:30s}  {p['agent_name']:12s}  {info}")
        else:
            print(f"\n=== Agent select — your team ({len(players)}) ===")
            for p in players:
                info = format_analysis(p['analysis'])
                print(f"  {p['name']:30s}  {p['agent_name']:12s}  [{p['selection_state']:8s}]  {info}")

        print_legend()
        return 0

    finally:
        SESSION.close()


if __name__ == '__main__':
    sys.exit(main())