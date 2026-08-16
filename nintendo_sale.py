# -*- coding: utf-8 -*-
"""ニンテンドーストアのセール商品を全件取得し、値引き率順のHTMLを生成する。

仕組み:
  1. SLAS (Salesforce Commerce Cloud) のゲスト認証(PKCE)で匿名アクセストークンを取得
  2. ストアの検索APIをソート順2種(人気順/新着順)で全ページ取得して取りこぼしを補完
  3. Steamストア検索でタイトル名マッチングし、レビュー好評率をマージ(結果はキャッシュ)
  4. 値引き率ラベル("43%OFF", "最大30%OFF")から率を抽出し、ソート/絞り込みUI付きHTMLを出力

usage:
  python nintendo_sale.py                  # 通常実行(Steam照会は差分のみ、件数キャップあり)
  python nintendo_sale.py --steam-backfill # Steam照会のキャップなし(初回の全件マッチング用)

依存: Python標準ライブラリのみ。
"""
import base64
import difflib
import hashlib
import json
import logging
import os
import re
import secrets
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

# --- ストア公開設定 (store-jp.nintendo.com のページに埋め込まれている公開値) ---
CLIENT_ID = "1ec6991a-1e8e-4c07-bc5c-8fc94a1a6127"
ORG_ID = "f_ecom_bfgj_prd"
SHORT_CODE = "zhz1np7s"
SITE_ID = "MNS"
REDIRECT_URI = "https://store-jp.nintendo.com/callback"
API_BASE = f"https://{SHORT_CODE}.api.commercecloud.salesforce.com"

OUT_HTML = os.environ.get("NINTENDO_SALE_OUT") or os.path.join(
    os.path.expanduser("~"), "Documents", "nintendo_sale_sorted.html")
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "nintendo_sale.log")
STEAM_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "steam_cache.json")
PRICE_HISTORY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "price_history.json")
GC_CATALOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gc_catalog.json")
PSN_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "psn_cache.json")
PSN_NPSSO_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".psn_npsso")

# --- PSN照会の調整値 ---
PSN_INTERVAL = 1.0
PSN_SEARCH_CAP = 150          # 通常実行1回あたりの新規検索数上限
PSN_REFRESH_CAP = 300         # 通常実行1回あたりの価格/評価再取得数上限
PSN_REFRESH_DAYS = 4          # PSはセール周期があるので価格は4日ごとに更新
PSN_NOMATCH_RECHECK_DAYS = 90
# PlayStation公式Androidアプリのclient資格情報(psn-api等コミュニティで公知の定数)
PSN_CLIENT_BASIC = "MDk1MTUxNTktNzIzNy00MzcwLTliNDAtMzgwNmU2N2MwODkxOnVjUGprYTV0bnRCMktxc1A="
PSN_REDIRECT = "com.scee.psxandroid.scecompcall://redirect"

# --- Steam照会の調整値 ---
STEAM_INTERVAL = 1.0          # リクエスト間隔(秒)。詰めすぎると429になる
STEAM_SEARCH_CAP = 200        # 通常実行1回あたりの新規タイトル検索数上限
STEAM_REVIEW_CAP = 300        # 通常実行1回あたりのレビュー再取得数上限
STEAM_REVIEW_REFRESH_DAYS = 7   # レビューをこの日数ごとに更新
STEAM_NOMATCH_RECHECK_DAYS = 90  # 「マッチなし」の再確認間隔
IMG_PREFIX = ("https://store-jp.nintendo.com/dw/image/v2/BFGJ_PRD/on/demandware.static/"
              "-/Sites-all-master-catalog/ja_JP/")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) nintendo-sale-sorter/1.0"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"), logging.StreamHandler()],
)
log = logging.getLogger(__name__)


def http(url, data=None, headers=None, follow_redirect=True):
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA, **(headers or {})})
    opener = urllib.request.build_opener()
    if not follow_redirect:
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        opener = urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(req, timeout=30) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def get_guest_token():
    """SLASゲスト認証(public client + PKCE)で匿名アクセストークンを得る"""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    q = urllib.parse.urlencode({
        "redirect_uri": REDIRECT_URI,
        "client_id": CLIENT_ID,
        "code_challenge": challenge,
        "response_type": "code",
        "hint": "guest",
    })
    url = f"{API_BASE}/shopper/auth/v1/organizations/{ORG_ID}/oauth2/authorize?{q}"
    status, headers, body = http(url, follow_redirect=False)
    if status not in (301, 302, 303):
        raise RuntimeError(f"authorize failed: {status} {body[:200]!r}")
    loc = headers.get("Location") or headers.get("location")
    params = urllib.parse.parse_qs(urllib.parse.urlparse(loc).query)
    code = params["code"][0]
    usid = params["usid"][0]

    data = urllib.parse.urlencode({
        "grant_type": "authorization_code_pkce",
        "code": code,
        "usid": usid,
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "code_verifier": verifier,
        "channel_id": SITE_ID,
    }).encode()
    status, _, body = http(
        f"{API_BASE}/shopper/auth/v1/organizations/{ORG_ID}/oauth2/token",
        data=data, headers={"Content-Type": "application/x-www-form-urlencoded"})
    if status != 200:
        raise RuntimeError(f"token failed: {status} {body[:200]!r}")
    return json.loads(body)["access_token"]


def fetch_page(token, srule, page):
    q = urllib.parse.urlencode({
        "c_cgid": "software",
        "c_prefn1": "isSale", "c_prefv1": "true",
        "c_softType": "TITLE",
        "c_srule": srule,
        "c_page": page,
        "siteId": SITE_ID,
    })
    url = f"{API_BASE}/custom/search/v1/organizations/{ORG_ID}/search?{q}"
    for attempt in range(3):
        status, _, body = http(url, headers={"Authorization": f"Bearer {token}"})
        if status == 200:
            return json.loads(body)
        log.warning("page %s (%s) -> HTTP %s (attempt %d)", page, srule, status, attempt + 1)
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"page {page} ({srule}) failed after retries")


def collect():
    token = get_guest_token()
    log.info("guest token OK")
    merged = {}
    for srule in ("most-popular", "new-arrival"):
        first = fetch_page(token, srule, 0)
        max_page = first["pagingInfo"]["maxPage"]
        total = first["pagingInfo"]["totalCount"]
        log.info("%s: total=%s maxPage=%s", srule, total, max_page)
        pages = [first] + [fetch_page(token, srule, p) for p in range(1, max_page + 1)]
        for j in pages:
            for x in j.get("resultProducts") or []:
                if x["id"] in merged:
                    continue
                img = (x.get("imageUrl") or {}).get("squareHeroBanner") or ""
                merged[x["id"]] = {
                    "id": x["id"],
                    "n": x.get("name") or "",
                    "p": x.get("salePrice"),
                    "label": x.get("saleLabel") or "",
                    "mx": 1 if x.get("isRangePrice") else 0,
                    "mk": x.get("manufacturerName") or "",
                    "im": img.replace(IMG_PREFIX, "").replace("?sw=346&strip=false", ""),
                }
        log.info("%s: merged unique=%d", srule, len(merged))
    items = []
    for it in merged.values():
        m = re.search(r"(\d+)\s*%\s*OFF", it["label"], re.I)
        if not m:
            continue
        it["pct"] = int(m.group(1))
        items.append(it)
    items.sort(key=lambda i: (-i["pct"], i["p"] if i["p"] is not None else 10**9))
    return items


def update_price_history(items):
    """セール価格の自前履歴を更新し、itemsに過去最安情報を付与する。

    履歴: {nintendo_id: {"min": 最安値, "at": "YYYY-MM-DD"}}
    付与: hm=今回より前の最安値, hd=その記録日, nl=1(最安更新) / 2(最安タイ)
    ※ トラッキング開始(2026-08-14)以前のセールは分からない点に注意
    """
    try:
        with open(PRICE_HISTORY, encoding="utf-8") as f:
            hist = json.load(f)
    except (OSError, ValueError):
        hist = {}
    today = time.strftime("%Y-%m-%d")
    for it in items:
        p = it["p"]
        if p is None:
            continue
        e = hist.get(it["id"])
        if e is None:
            hist[it["id"]] = {"min": p, "at": today}
            continue
        if e["at"] != today:  # 過去日の記録がある場合だけ「過去最安」として意味を持つ
            it["hm"] = e["min"]
            it["hd"] = e["at"]
            if p < e["min"]:
                it["nl"] = 1
            elif p == e["min"]:
                it["nl"] = 2
        if p < e["min"]:
            hist[it["id"]] = {"min": p, "at": today}
    tmp = PRICE_HISTORY + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(hist, f, ensure_ascii=False)
    os.replace(tmp, PRICE_HISTORY)
    log.info("price history: tracked=%d newlow=%d",
             len(hist), sum(1 for i in items if i.get("nl") == 1))


# ---------------------------------------------------------------- IGDB→Steam補完
# 方式: 日本語名(ja-JPローカライズ+別名)→Steam appIDの対応表をバルク構築して
# igdb_ja_map.json にコミットし、日々の照合はローカルで行う(API呼び出しゼロ)。
# 対応表の再構築は --steam-backfill 時のみ(約5分)。
IGDB_MAP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "igdb_ja_map.json")
IGDB_CREDS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".igdb_credentials")
IGDB_INTERVAL = 0.35             # IGDBは4req/秒まで許容
_last_igdb_req = [0.0]


def igdb_creds():
    cid = os.environ.get("IGDB_CLIENT_ID", "").strip()
    sec = os.environ.get("IGDB_CLIENT_SECRET", "").strip()
    if cid and sec:
        return cid, sec
    try:
        with open(IGDB_CREDS_FILE, encoding="utf-8") as f:
            parts = f.read().split()
        return parts[0], parts[1]
    except (OSError, IndexError):
        return None, None


def igdb_token(cid, sec):
    data = urllib.parse.urlencode({"client_id": cid, "client_secret": sec,
                                   "grant_type": "client_credentials"}).encode()
    status, _, body = http("https://id.twitch.tv/oauth2/token", data=data)
    if status != 200:
        log.warning("igdb: token failed %s", status)
        return None
    return json.loads(body)["access_token"]


def igdb_query(cid, token, endpoint, body):
    wait = IGDB_INTERVAL - (time.monotonic() - _last_igdb_req[0])
    if wait > 0:
        time.sleep(wait)
    _last_igdb_req[0] = time.monotonic()
    req = urllib.request.Request(
        "https://api.igdb.com/v4/" + endpoint, data=body.encode("utf-8"),
        headers={"Client-ID": cid, "Authorization": "Bearer " + token,
                 "Accept": "application/json", "User-Agent": UA})
    return json.load(urllib.request.urlopen(req, timeout=60))


def _igdb_page_all(cid, token, endpoint, fields, where=""):
    """idカーソルで全行取得"""
    rows, last_id = [], 0
    while True:
        w = f"where id > {last_id}" + (f" & ({where})" if where else "")
        batch = igdb_query(cid, token, endpoint,
                           f"fields {fields}; {w}; sort id asc; limit 500;")
        if not batch:
            return rows
        rows += batch
        last_id = batch[-1]["id"]


def build_igdb_map():
    """IGDBの日本語名→Steam appID対応表を構築して IGDB_MAP に保存する"""
    cid, sec = igdb_creds()
    if not cid:
        log.info("igdb: no credentials, skipping map build")
        return
    token = igdb_token(cid, sec)
    if not token:
        return
    # 1) 日本語名→game id 候補を収集 (ja-JPローカライズ + 日本語文字を含む別名)
    name_games = {}
    jp = re.compile(r"[ぁ-ゖァ-ヺ一-鿿]")
    rows = _igdb_page_all(cid, token, "game_localizations", "name, game", "region = 3")
    log.info("igdb map: localizations=%d", len(rows))
    alts = _igdb_page_all(cid, token, "alternative_names", "name, game")
    alts = [r for r in alts if jp.search(r.get("name") or "")]
    log.info("igdb map: ja alternative_names=%d", len(alts))
    for r in rows + alts:
        nm, g = norm_name(r.get("name") or ""), r.get("game")
        if nm and g:
            name_games.setdefault(nm, set()).add(g)
    # 2) 関係するgame idのSteam appIDをバッチ解決
    gids = sorted({g for gs in name_games.values() for g in gs})
    steam_of = {}
    for i in range(0, len(gids), 400):
        chunk = gids[i:i + 400]
        rs = igdb_query(cid, token, "external_games",
                        "fields game, uid; where game = (%s) & external_game_source = 1; limit 500;"
                        % ",".join(map(str, chunk)))
        for r in rs:
            uid = r.get("uid")
            try:
                steam_of[r["game"]] = int(uid)
            except (TypeError, ValueError):
                continue
    # 3) 正規化名→appid (曖昧な名前=複数ゲームで異なるappidに割れるものは捨てる)
    out = {}
    for nm, gs in name_games.items():
        apps = {steam_of[g] for g in gs if g in steam_of}
        if len(apps) == 1:
            out[nm] = apps.pop()
    tmp = IGDB_MAP + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    os.replace(tmp, IGDB_MAP)
    log.info("igdb map: built %d ja-name->steam entries", len(out))


def enrich_igdb_steam(items, steam_cache, backfill=False):
    """igdb_ja_map.json(日本語名→Steam appID)でローカル照合し、steam_cacheに注入する"""
    if backfill:
        try:
            build_igdb_map()
        except Exception:
            log.exception("igdb map build failed; using existing map")
    try:
        with open(IGDB_MAP, encoding="utf-8") as f:
            jamap = json.load(f)
    except (OSError, ValueError):
        log.info("igdb: no map file, skipping")
        return
    now = time.time()
    found = 0
    for it in items:
        sc = steam_cache.get(it["id"])
        if sc is None or sc.get("appid") is not None:
            continue
        appid = jamap.get(norm_name(it["n"]))
        if appid:
            found += 1
            steam_cache[it["id"]] = {"appid": appid, "checked": now,
                                     "rev": None, "rev_at": 0, "via": "igdb"}
    log.info("igdb-steam: map=%d found=%d", len(jamap), found)


# ---------------------------------------------------------------- Wikipedia→Steam補完
WIKI_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wiki_cache.json")
WIKI_INTERVAL = 1.5
WIKI_CAP = 60                    # 通常実行1回あたりの連鎖試行数上限(1試行=最大3リクエスト)
WIKI_RECHECK_DAYS = 180
WIKI_GAME_TYPES = {"Q7889", "Q116680"}  # video game, expansion pack
_last_wiki_req = [0.0]


def wiki_get(url):
    wait = WIKI_INTERVAL - (time.monotonic() - _last_wiki_req[0])
    if wait > 0:
        time.sleep(wait)
    _last_wiki_req[0] = time.monotonic()
    req = urllib.request.Request(url, headers={
        "User-Agent": "nintendo-sale-sorter/1.0 (personal tool; github.com/sotakaki/nintendo-sale-sorter)"})
    for attempt in range(2):
        try:
            return json.load(urllib.request.urlopen(req, timeout=30))
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt == 0:
                time.sleep(30)
                continue
            raise


def wiki_steam_lookup(title):
    """ja.wikipedia検索→Wikidataで検証(ビデオゲーム+ja/enラベル一致)→Steam appID or None"""
    q = urllib.parse.urlencode({"action": "query", "list": "search", "srsearch": title,
                                "srlimit": 2, "srnamespace": 0, "format": "json"})
    hits = wiki_get("https://ja.wikipedia.org/w/api.php?" + q).get("query", {}).get("search", [])
    if not hits:
        return None
    q = urllib.parse.urlencode({"action": "query", "titles": hits[0]["title"],
                                "prop": "pageprops", "ppprop": "wikibase_item",
                                "redirects": 1, "format": "json"})
    qid = None
    for p in wiki_get("https://ja.wikipedia.org/w/api.php?" + q).get("query", {}).get("pages", {}).values():
        qid = (p.get("pageprops") or {}).get("wikibase_item")
    if not qid:
        return None
    q = urllib.parse.urlencode({"action": "wbgetentities", "ids": qid,
                                "props": "claims|labels|aliases", "format": "json"})
    ent = wiki_get("https://www.wikidata.org/w/api.php?" + q).get("entities", {}).get(qid) or {}
    claims = ent.get("claims", {})
    p31 = {c["mainsnak"].get("datavalue", {}).get("value", {}).get("id")
           for c in claims.get("P31", [])}
    if not (p31 & WIKI_GAME_TYPES) or "P1733" not in claims:
        return None
    names = []
    for lang in ("ja", "en"):
        lb = ent.get("labels", {}).get(lang)
        if lb:
            names.append(lb["value"])
        names += [a["value"] for a in ent.get("aliases", {}).get(lang, [])]
    t = norm_name(title)
    if not any(difflib.SequenceMatcher(None, t, norm_name(n)).ratio() >= 0.85 for n in names if n):
        return None
    val = claims["P1733"][0]["mainsnak"].get("datavalue", {}).get("value")
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def enrich_wiki_steam(items, steam_cache, backfill=False):
    """Steam検索で見つからなかった日本語名タイトルをWikipedia経由でSteamに接続する。

    見つけたappidはsteam_cacheに注入し、レビュー取得はenrich_steamに任せる。
    """
    try:
        with open(WIKI_CACHE, encoding="utf-8") as f:
            wcache = json.load(f)
    except (OSError, ValueError):
        wcache = {}
    now = time.time()
    day = 86400
    budget = 10**9 if backfill else WIKI_CAP
    tried = found = 0
    consecutive_fails = 0
    try:
        for it in items:
            if consecutive_fails >= 5:
                log.warning("wiki: 5 consecutive failures (rate limited?), aborting this run")
                break
            sc = steam_cache.get(it["id"])
            if sc is None or sc.get("appid") is not None:
                continue  # Steam未照会 or 照会済みでマッチあり → 対象外
            if not re.search(r"[ぁ-ゖァ-ヺ一-鿿]", it["n"]):
                continue  # 英語名タイトルはSteam直接検索で十分
            w = wcache.get(it["id"])
            if w is not None and (w.get("steam") is not None
                                  or now - w.get("checked", 0) < WIKI_RECHECK_DAYS * day):
                continue
            if budget <= 0:
                continue
            budget -= 1
            tried += 1
            try:
                appid = wiki_steam_lookup(it["n"])
                consecutive_fails = 0
            except Exception as e:
                log.warning("wiki lookup failed for %r: %s", it["n"], e)
                consecutive_fails += 1
                continue
            wcache[it["id"]] = {"steam": appid, "checked": now}
            if appid:
                found += 1
                steam_cache[it["id"]] = {"appid": appid, "checked": now,
                                         "rev": None, "rev_at": 0, "via": "wiki"}
    finally:
        tmp = WIKI_CACHE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(wcache, f, ensure_ascii=False)
        os.replace(tmp, WIKI_CACHE)
        log.info("wiki-steam: tried=%d found=%d cached=%d", tried, found, len(wcache))


# ---------------------------------------------------------------- PSN
_last_psn_req = [0.0]


def psn_throttled(req):
    wait = PSN_INTERVAL - (time.monotonic() - _last_psn_req[0])
    if wait > 0:
        time.sleep(wait)
    _last_psn_req[0] = time.monotonic()
    return urllib.request.urlopen(req, timeout=30)


def kata_to_hira(s):
    return "".join(chr(ord(c) - 0x60) if "ァ" <= c <= "ヶ" else c for c in s)


def psn_get_token():
    """npsso(env PSN_NPSSO または .psn_npsso) → アプリAPI用アクセストークン"""
    npsso = os.environ.get("PSN_NPSSO", "").strip()
    if not npsso:
        try:
            with open(PSN_NPSSO_FILE, encoding="utf-8") as f:
                npsso = f.read().strip()
        except OSError:
            return None
    q = urllib.parse.urlencode({
        "access_type": "offline",
        "client_id": "09515159-7237-4370-9b40-3806e67c0891",
        "response_type": "code",
        "scope": "psn:mobile.v2.core psn:clientapp",
        "redirect_uri": PSN_REDIRECT,
    })

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None

    opener = urllib.request.build_opener(NoRedirect)
    req = urllib.request.Request(
        "https://ca.account.sony.com/api/authz/v3/oauth/authorize?" + q,
        headers={"Cookie": "npsso=" + npsso, "User-Agent": UA})
    try:
        r = opener.open(req, timeout=30)
        loc = r.headers.get("Location", "")
    except urllib.error.HTTPError as e:
        loc = e.headers.get("Location", "")
    if "code=" not in (loc or ""):
        log.warning("psn: npsso expired or invalid (no auth code)")
        return None
    code = urllib.parse.parse_qs(urllib.parse.urlparse(loc).query)["code"][0]
    data = urllib.parse.urlencode({
        "code": code, "redirect_uri": PSN_REDIRECT,
        "grant_type": "authorization_code", "token_format": "jwt"}).encode()
    status, _, body = http("https://ca.account.sony.com/api/authz/v3/oauth/token", data=data,
                           headers={"Authorization": "Basic " + PSN_CLIENT_BASIC,
                                    "Content-Type": "application/x-www-form-urlencoded"})
    if status != 200:
        log.warning("psn: token exchange failed %s", status)
        return None
    return json.loads(body)["access_token"]


def psn_search(token, term):
    body = json.dumps({
        "searchTerm": term[:100],
        "domainRequests": [{"domain": "MobileGames",
                            "pagination": {"pageSize": 5, "offset": 0}}],
        "countryCode": "jp", "languageCode": "ja", "age": 99}).encode()
    req = urllib.request.Request(
        "https://m.np.playstation.com/api/search/v1/universalSearch", data=body,
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json",
                 "Country": "JP", "Accept-Language": "ja-JP", "User-Agent": UA})
    try:
        d = json.load(psn_throttled(req))
        return (d.get("domainResponses") or [{}])[0].get("results") or []
    except (urllib.error.URLError, ValueError, KeyError) as e:
        log.warning("psn search error for %r: %s", term, e)
        return []


def psn_pick(query, results):
    """検索結果から確度の高い一致を選ぶ。名前照合(かな対応) or スコア圧勝のみ採用"""
    target = norm_name(query)
    target_hira = norm_name(kata_to_hira(query))
    short = len(target) <= 4  # 「脱出」「仲間」等の一般名詞タイトルは厳格に扱う
    for r in results[:3]:
        cm = r.get("conceptMetadata") or {}
        names = [cm.get("name") or "", cm.get("nameEn") or ""]
        names += list(((cm.get("localizedName") or {}).get("metadata") or {}).values())
        ok = False
        for nm in names:
            c = norm_name(nm)
            if c and (c == target
                      or (not short
                          and difflib.SequenceMatcher(None, target, c).ratio() >= 0.85)):
                ok = True
                break
        if not ok:
            ssn = norm_name(kata_to_hira(cm.get("searchAndSortName") or ""))
            if ssn and target_hira and (
                    ssn == target_hira
                    or (not short
                        and difflib.SequenceMatcher(None, target_hira, ssn).ratio() >= 0.85)):
                ok = True
        # 名前照合が無理でも、圧倒的な検索スコアなら採用(実測: 正解750+/ゴミ最大297)。
        # ただし短い一般名詞タイトルはスコアが高くても誤マッチしやすいので対象外
        if not ok and not short and r is results[0] and (r.get("score") or 0) >= 400:
            ok = True
        if ok:
            ids = []
            for cp in cm.get("categorizedProducts") or []:
                ids += cp.get("ids") or []
            sr = cm.get("starRating") or {}
            return {"cid": cm.get("id"), "pid": ids[0] if ids else None,
                    "nm": cm.get("name"),
                    "r": float(sr["score"]) if sr.get("score") else None,
                    "rc": int(sr["total"]) if sr.get("total") else 0}
    return None


def psn_product(pid):
    """商品ページ(認証不要)から評価と価格を取得"""
    req = urllib.request.Request(
        "https://store.playstation.com/ja-jp/product/" + pid,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
    try:
        html = psn_throttled(req).read().decode("utf-8", "replace")
    except urllib.error.URLError as e:
        log.warning("psn product fetch failed %s: %s", pid, e)
        return None
    m = re.search(r'"averageRating":([\d.]+),"totalRatingsCount":(\d+)', html)
    p = re.search(r'"basePriceValue":(\d+),"discountedValue":(\d+),"currencyCode":"JPY"', html)
    out = {}
    if m:
        out["r"] = float(m.group(1))
        out["rc"] = int(m.group(2))
    if p:
        out["bp"] = int(p.group(1))
        out["pp"] = int(p.group(2))
    return out or None


def enrich_psn(items, backfill=False):
    """PSNレビュー(★)と価格をマージ。照会結果はキャッシュして差分だけ叩く"""
    try:
        with open(PSN_CACHE, encoding="utf-8") as f:
            cache = json.load(f)
    except (OSError, ValueError):
        cache = {}
    token = psn_get_token()
    now = time.time()
    day = 86400
    search_budget = 10**9 if backfill else PSN_SEARCH_CAP
    refresh_budget = 10**9 if backfill else PSN_REFRESH_CAP
    searched = refreshed = 0
    try:
        for it in items:
            c = cache.get(it["id"])
            if c is None or (c.get("cid") is None
                             and now - c.get("checked", 0) > PSN_NOMATCH_RECHECK_DAYS * day):
                if token and search_budget > 0:
                    search_budget -= 1
                    searched += 1
                    hit = psn_pick(it["n"], psn_search(token, it["n"]))
                    c = hit or {"cid": None}
                    c["checked"] = now
                    if hit and hit.get("pid"):
                        prod = psn_product(hit["pid"])
                        if prod:
                            c.update(prod)
                        c["rev_at"] = now
                    cache[it["id"]] = c
            if not c or c.get("cid") is None:
                continue
            if c.get("pid") and now - c.get("rev_at", 0) > PSN_REFRESH_DAYS * day:
                if refresh_budget > 0:
                    refresh_budget -= 1
                    refreshed += 1
                    prod = psn_product(c["pid"])
                    if prod:
                        c.update(prod)
                        c["rev_at"] = now
            if c.get("r") and c.get("rc"):
                it["pv"] = c["r"]
                it["pn"] = c["rc"]
                it["pid"] = c.get("pid")
                # pp=0 はPS Plusゲームカタログ収録等(加入者は実質無料)なので0円のまま表示する
                if c.get("pp") is not None:
                    it["pp"] = c["pp"]
                    it["pb"] = c.get("bp")
    finally:
        tmp = PSN_CACHE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
        os.replace(tmp, PSN_CACHE)
        matched = sum(1 for i in items if i.get("pv"))
        log.info("psn: searched=%d refreshed=%d matched=%d/%d cached=%d token=%s",
                 searched, refreshed, matched, len(items), len(cache), bool(token))


GC_VERDICT_DISPLAY = {"良": "良作", "良*": "良作*", "ク": "クソゲー", "賛否": "賛否両論",
                      "なし": "普通", "なし*": "普通*"}


def enrich_game_catalog(items):
    """ゲームカタログ@Wikiの判定(gc_catalog.json、ブラウザで手動更新する静的データ)をマージ。

    atwikiはボット保護下にあるため自動取得はせず、一覧2ページをブラウザで閲覧して
    抽出したものを同梱する方式。判定の変化は緩やかなので手動更新で十分。
    """
    try:
        with open(GC_CATALOG, encoding="utf-8") as f:
            cat = json.load(f)
    except (OSError, ValueError):
        log.info("game catalog: no gc_catalog.json, skipping")
        return
    bymark = {}
    buckets = {}
    for e in cat:
        key = norm_name(e["t"])
        bymark[key] = e
        buckets.setdefault(key[:2], []).append(key)
    matched = 0
    for it in items:
        key = norm_name(it["n"])
        e = bymark.get(key)
        if e is None and len(key) >= 2:
            cands = difflib.get_close_matches(key, buckets.get(key[:2], []), n=1, cutoff=0.92)
            if cands:
                e = bymark[cands[0]]
        if e is None:
            continue
        matched += 1
        it["gv"] = GC_VERDICT_DISPLAY.get(e["v"], e["v"])
        if e.get("u"):
            it["gu"] = e["u"]
    log.info("game catalog: matched=%d/%d (catalog=%d)", matched, len(items), len(cat))


_last_steam_req = [0.0]


def steam_get(url):
    """throttle付きGET。429なら60秒待って1回だけ再試行"""
    for attempt in range(2):
        wait = STEAM_INTERVAL - (time.monotonic() - _last_steam_req[0])
        if wait > 0:
            time.sleep(wait)
        _last_steam_req[0] = time.monotonic()
        status, _, body = http(url)
        if status == 200:
            try:
                return json.loads(body)
            except ValueError:
                return None
        if status == 429 and attempt == 0:
            log.warning("steam 429, backing off 60s")
            time.sleep(60)
            continue
        return None
    return None


def norm_name(s):
    """タイトル名照合用の正規化: NFKC→小文字→記号/空白除去"""
    s = unicodedata.normalize("NFKC", s).casefold()
    s = re.sub(r"[™®©]|for nintendo switch|nintendo switch(版)?", "", s)
    return re.sub(r"[^0-9a-zひ-ゖァ-ヺー一-鿿]", "", s)


def steam_match(name):
    """Steamストア検索でタイトルを探し、確度の高い一致だけ返す → appid or None"""
    term = urllib.parse.quote(name[:100])
    d = steam_get(f"https://store.steampowered.com/api/storesearch/?term={term}&cc=JP&l=japanese")
    target = norm_name(name)
    if not target:
        return None
    for it in (d or {}).get("items", [])[:5]:
        cand = norm_name(it.get("name") or "")
        if not cand:
            continue
        if cand == target or difflib.SequenceMatcher(None, target, cand).ratio() >= 0.87:
            return it["id"]
    return None


def steam_reviews(appid):
    """レビューサマリ → {"sp": 好評率%, "sn": 総件数} or None"""
    d = steam_get(f"https://store.steampowered.com/appreviews/{appid}"
                  "?json=1&language=all&purchase_type=all&num_per_page=0")
    q = (d or {}).get("query_summary") or {}
    total = q.get("total_reviews") or 0
    if total <= 0:
        return {"sp": None, "sn": 0}
    return {"sp": round(100 * q.get("total_positive", 0) / total), "sn": total}


def enrich_steam(items, backfill=False):
    """itemsにSteamレビュー情報(sa/sp/sn)を付与。照会結果はキャッシュして差分だけ叩く"""
    try:
        with open(STEAM_CACHE, encoding="utf-8") as f:
            cache = json.load(f)
    except (OSError, ValueError):
        cache = {}
    try:
        # Steam検索で見つからなかった日本語名タイトルをIGDBの地域別名で補完
        enrich_igdb_steam(items, cache, backfill=backfill)
    except Exception:
        log.exception("igdb-steam failed; continuing")
    try:
        # IGDBでも残った日本語名タイトルをWikipedia経由で補完
        enrich_wiki_steam(items, cache, backfill=backfill)
    except Exception:
        log.exception("wiki-steam failed; continuing")
    now = time.time()
    day = 86400
    search_budget = 10**9 if backfill else STEAM_SEARCH_CAP
    review_budget = 10**9 if backfill else STEAM_REVIEW_CAP
    searched = reviewed = 0

    try:
        for it in items:
            c = cache.get(it["id"])
            # 1) appidマッチング(未照会 or マッチなしの定期再確認)
            if c is None or (c.get("appid") is None
                             and now - c.get("checked", 0) > STEAM_NOMATCH_RECHECK_DAYS * day):
                if search_budget > 0:
                    search_budget -= 1
                    searched += 1
                    appid = steam_match(it["n"])
                    c = {"appid": appid, "checked": now,
                         "rev": (c or {}).get("rev"), "rev_at": (c or {}).get("rev_at", 0)}
                    cache[it["id"]] = c
            if not c or c.get("appid") is None:
                continue
            # 2) レビュー取得/更新
            if (c.get("rev") is None or now - c.get("rev_at", 0) > STEAM_REVIEW_REFRESH_DAYS * day):
                if review_budget > 0:
                    review_budget -= 1
                    reviewed += 1
                    rev = steam_reviews(c["appid"])
                    if rev is not None:
                        c["rev"], c["rev_at"] = rev, now
            rev = c.get("rev") or {}
            if rev.get("sn"):
                it["sa"] = c["appid"]
                it["sp"] = rev.get("sp")
                it["sn"] = rev.get("sn")
    finally:
        tmp = STEAM_CACHE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
        os.replace(tmp, STEAM_CACHE)
        matched = sum(1 for i in items if i.get("sa"))
        log.info("steam: searched=%d reviewed=%d matched=%d/%d cached=%d",
                 searched, reviewed, matched, len(items), len(cache))


TEMPLATE = """<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ニンテンドーストア セール 値引き率順 (%COUNT%件 / %NOW%取得)</title>
<style>
:root { --bg:#f5f5f7; --card:#fff; --text:#1d1d1f; --sub:#6e6e73; --accent:#e60012; --line:#e3e3e6; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#161618; --card:#232326; --text:#f0f0f2; --sub:#9a9aa0; --line:#333338; }
}
* { box-sizing:border-box; margin:0; padding:0; }
body { font-family:"Hiragino Sans","Yu Gothic UI","Noto Sans JP",sans-serif; background:var(--bg); color:var(--text); }
header { position:sticky; top:0; z-index:10; background:var(--card); border-bottom:1px solid var(--line); padding:10px 16px; display:flex; flex-direction:column; gap:8px; transition:transform .2s ease; }
header.hide { transform:translateY(-100%); }
.hrow { display:flex; flex-wrap:wrap; gap:8px; align-items:center; }
#controls { display:flex; flex-wrap:wrap; gap:8px; align-items:center; }
#ftoggle { display:none; font-size:13px; padding:6px 10px; border:1px solid var(--line); border-radius:8px; background:var(--card); color:var(--text); cursor:pointer; }
#ftoggle.on { border-color:var(--accent); color:var(--accent); }
@media (max-width:640px) {
  #ftoggle { display:inline-block; margin-left:auto; }
  input[type=search] { flex:1; min-width:120px; width:auto; }
  #controls { display:none; }
  #controls.open { display:flex; }
}
header h1 { font-size:15px; margin-right:auto; }
header h1 small { color:var(--sub); font-weight:normal; margin-left:8px; }
@media (max-width:640px) { header h1 { width:100%; margin-right:0; } header h1 small { display:block; margin-left:0; font-size:10px; } }
select, input[type=search] { font-size:13px; padding:6px 8px; border:1px solid var(--line); border-radius:8px; background:var(--card); color:var(--text); }
input[type=search] { width:180px; }
#grid { display:grid; grid-template-columns:repeat(auto-fill, minmax(150px, 1fr)); gap:12px; padding:16px; max-width:1400px; margin:0 auto; }
.card { background:var(--card); border-radius:12px; overflow:hidden; text-decoration:none; color:inherit; display:flex; flex-direction:column; border:1px solid var(--line); transition:transform .1s; }
.card:hover { transform:translateY(-2px); }
.card img { width:100%; aspect-ratio:1; object-fit:cover; background:#ddd; display:block; }
.card .b { padding:8px 10px 10px; display:flex; flex-direction:column; gap:3px; flex:1; }
.card .n { font-size:12px; line-height:1.35; font-weight:600; display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden; min-height:2.7em; }
.card .mk { font-size:10px; color:var(--sub); }
.row { display:flex; align-items:baseline; gap:6px; margin-top:auto; }
.off { color:var(--accent); font-weight:700; font-size:15px; white-space:nowrap; }
.off small { font-size:10px; font-weight:600; }
.pr { font-size:12px; }
.pr b { font-size:14px; }
.chk { font-size:12px; display:flex; align-items:center; gap:4px; color:var(--sub); cursor:pointer; }
.stm { font-size:10px; margin-top:2px; cursor:pointer; }
.stm:hover { text-decoration:underline; }
.stm.g { color:#2e7d32; } .stm.y { color:#b26a00; } .stm.r { color:#c62828; }
@media (prefers-color-scheme: dark) { .stm.g { color:#7bc67e; } .stm.y { color:#e0a34e; } .stm.r { color:#e57373; } }
.low { font-size:10px; margin-top:2px; }
.low.new { color:var(--accent); font-weight:700; }
.low.tie { color:#2e7d32; }
.low.was { color:var(--sub); }
@media (prefers-color-scheme: dark) { .low.tie { color:#7bc67e; } }
.gc { font-size:10px; margin-top:2px; cursor:pointer; }
.gc:hover { text-decoration:underline; }
.gc.good { color:#2e7d32; } .gc.mid { color:#b26a00; } .gc.bad { color:#c62828; } .gc.na { color:var(--sub); }
@media (prefers-color-scheme: dark) { .gc.good { color:#7bc67e; } .gc.mid { color:#e0a34e; } .gc.bad { color:#e57373; } }
.psn { font-size:10px; margin-top:2px; cursor:pointer; color:#0057b8; }
.psn:hover { text-decoration:underline; }
.psn .sale { color:var(--accent); font-weight:600; }
@media (prefers-color-scheme: dark) { .psn { color:#5c9ded; } }
#count { font-size:12px; color:var(--sub); padding:0 16px; max-width:1400px; margin:12px auto 0; }
footer { text-align:center; color:var(--sub); font-size:11px; padding:20px; }
</style>
</head>
<body>
<header id="hd">
  <div class="hrow">
    <h1>ニンテンドーストア セール<small>値引き率順 / %NOW% 取得</small></h1>
    <input type="search" id="q" placeholder="タイトル検索">
    <button id="ftoggle" type="button">絞り込み</button>
  </div>
  <div id="controls">
  <select id="minpct">
    <option value="0">すべての値引き率</option>
    <option value="30">30%OFF以上</option>
    <option value="50">50%OFF以上</option>
    <option value="70">70%OFF以上</option>
    <option value="80">80%OFF以上</option>
    <option value="90">90%OFF以上</option>
  </select>
  <select id="maxprice">
    <option value="">価格上限なし</option>
    <option value="500">〜500円</option>
    <option value="1000">〜1,000円</option>
    <option value="2000">〜2,000円</option>
    <option value="3000">〜3,000円</option>
  </select>
  <select id="minsteam">
    <option value="0">Steam評価指定なし</option>
    <option value="70">Steam 70%以上</option>
    <option value="80">Steam 80%以上</option>
    <option value="90">Steam 90%以上</option>
  </select>
  <select id="minps">
    <option value="0">PS評価指定なし</option>
    <option value="3.5">PS ★3.5以上</option>
    <option value="4">PS ★4.0以上</option>
    <option value="4.5">PS ★4.5以上</option>
  </select>
  <select id="sort">
    <option value="pct">値引き率が高い順</option>
    <option value="plow">価格が安い順</option>
    <option value="phigh">価格が高い順</option>
    <option value="steam">Steam好評率順</option>
    <option value="ps">PS評価順</option>
  </select>
  <label class="chk"><input type="checkbox" id="steamonly">Steamレビューあり</label>
  <label class="chk"><input type="checkbox" id="gconly">カタログレビューあり</label>
  <label class="chk"><input type="checkbox" id="psnonly">PSレビューあり</label>
  <label class="chk"><input type="checkbox" id="newlowonly">過去最安のみ</label>
  </div>
</header>
<div id="count"></div>
<div id="grid"></div>
<footer>データはニンテンドーストアの検索APIから取得。価格・値引き率は取得時点のもの。「最大◯%OFF」はパッケージ版/DL版などで率が異なる商品。<br>Steamレビューはタイトル名の自動マッチングによる参考情報(Switch版の評価ではありません)。クリックでSteamページを開きます。<br>「過去最安」は2026-08-14からの自前トラッキングによるもので、それ以前のセール履歴は含みません。<br>「カタログ」は<a href="https://w.atwiki.jp/gcmatome/" target="_blank" rel="noopener">ゲームカタログ@Wiki</a>の判定(タイトル名の自動マッチング)。クリックで該当記事を開きます。<br>「PS ★」はPlayStation Store(日本)の星評価と現在価格(自動マッチング・参考情報。Switch版の評価ではありません)。クリックでPS Storeを開きます。<br><br>本サイトは個人が運営する<b>非公式サイト</b>であり、任天堂株式会社、株式会社ソニー・インタラクティブエンタテインメント、Valve Corporationその他の企業とは一切関係ありません。<br>ゲーム画像・タイトル名等の商標・著作権は各権利者に帰属します。価格・値引き率・評価は取得時点の参考情報であり、正確性を保証しません。購入の際は必ず各公式ストアで最新の価格をご確認ください。<br>掲載内容に問題がある場合は<a href="https://github.com/sotakaki/nintendo-sale-sorter/issues" target="_blank" rel="noopener">GitHubのIssue</a>からご連絡ください。速やかに対応します。</footer>
<script>
var DATA = %DATA%;
var IMG = "%IMGPREFIX%";
var grid = document.getElementById('grid'), count = document.getElementById('count');
var q = document.getElementById('q'), minpct = document.getElementById('minpct'), maxprice = document.getElementById('maxprice'), sortSel = document.getElementById('sort'), steamOnly = document.getElementById('steamonly'), gcOnly = document.getElementById('gconly'), psnOnly = document.getElementById('psnonly'), newLowOnly = document.getElementById('newlowonly'), minSteam = document.getElementById('minsteam'), minPs = document.getElementById('minps');
function yen(n) { return n == null ? '' : n.toLocaleString('ja-JP') + '円'; }
function render() {
  var kw = q.value.trim().toLowerCase();
  var mp = +minpct.value, mxp = maxprice.value ? +maxprice.value : Infinity;
  var list = DATA.filter(function(d) {
    if (d.pct < mp) return false;
    if (d.p != null && d.p > mxp) return false;
    if (steamOnly.checked && d.sp == null) return false;
    if (gcOnly.checked && !d.gv) return false;
    if (psnOnly.checked && d.pv == null) return false;
    if (newLowOnly.checked && !d.nl) return false;
    if (+minSteam.value > 0 && (d.sp == null || d.sp < +minSteam.value)) return false;
    if (+minPs.value > 0 && (d.pv == null || d.pv < +minPs.value)) return false;
    if (kw && d.n.toLowerCase().indexOf(kw) < 0 && d.mk.toLowerCase().indexOf(kw) < 0) return false;
    return true;
  });
  var s = sortSel.value;
  list.sort(s === 'plow' ? function(a,b){ return (a.p||1e9)-(b.p||1e9) || b.pct-a.pct; }
    : s === 'phigh' ? function(a,b){ return (b.p||0)-(a.p||0) || b.pct-a.pct; }
    : s === 'steam' ? function(a,b){ return (b.sp==null?-1:b.sp)-(a.sp==null?-1:a.sp) || (b.sn||0)-(a.sn||0) || b.pct-a.pct; }
    : s === 'ps' ? function(a,b){ return (b.pv==null?-1:b.pv)-(a.pv==null?-1:a.pv) || (b.pn||0)-(a.pn||0) || b.pct-a.pct; }
    : function(a,b){ return b.pct-a.pct || (a.p||1e9)-(b.p||1e9); });
  count.textContent = list.length.toLocaleString('ja-JP') + '件';
  var html = list.map(function(d) {
    var orig = (!d.mx && d.p != null && d.pct < 100) ? Math.round(d.p / (1 - d.pct/100)) : null;
    return '<a class="card" href="https://store-jp.nintendo.com/item/software/D' + d.id + '" target="_blank" rel="noopener">'
      + (d.im ? '<img loading="lazy" src="' + IMG + d.im + '?sw=346&strip=false" alt="">' : '<div style="aspect-ratio:1;background:#ddd"></div>')
      + '<div class="b"><div class="n">' + esc(d.n) + '</div><div class="mk">' + esc(d.mk) + '</div>'
      + '<div class="row"><span class="off">' + (d.mx ? '<small>最大</small>' : '') + d.pct + '<small>%OFF</small></span>'
      + '<span class="pr"><b>' + yen(d.p) + '</b>' + (d.mx ? '〜' : '') + '</span></div>'
      + (orig ? '<div class="mk">定価 ' + yen(orig) + '</div>' : '')
      + lowBadge(d)
      + gcBadge(d)
      + steamBadge(d)
      + psnBadge(d)
      + '</div></a>';
  }).join('');
  grid.innerHTML = html;
}
function lowBadge(d) {
  if (d.nl === 1) return '<div class="low new">過去最安更新 (前回 ' + yen(d.hm) + ')</div>';
  if (d.nl === 2) return '<div class="low tie">過去最安 (' + d.hd.slice(2).replace(/-/g, '/') + '〜)</div>';
  if (d.hm != null && d.hm < d.p) return '<div class="low was">過去最安 ' + yen(d.hm) + ' (' + d.hd.slice(2).replace(/-/g, '/') + ')</div>';
  return '';
}
function psnBadge(d) {
  if (d.pv == null) return '';
  var s = 'PS ★' + d.pv.toFixed(1) + '(' + d.pn.toLocaleString('ja-JP') + ')';
  if (d.pp != null) {
    s += '・' + yen(d.pp);
    if (d.pp === 0) s += ' <span class="sale">PS+カタログ?</span>';
    else if (d.pb != null && d.pp < d.pb) s += ' <span class="sale">セール中</span>';
  }
  return '<div class="psn"' + (d.pid ? ' data-pid="' + d.pid + '"' : '') + '>' + s + '</div>';
}
function gcBadge(d) {
  if (!d.gv) return '';
  var cls = /^良/.test(d.gv) ? 'good' : /クソ|劣化|シリ不|不安定/.test(d.gv) ? 'bad' : /^普通/.test(d.gv) ? 'na' : 'mid';
  return '<div class="gc ' + cls + '"' + (d.gu ? ' data-gu="' + esc(d.gu) + '"' : '') + '>カタログ: ' + esc(d.gv) + '</div>';
}
function steamBadge(d) {
  if (d.sp == null) return '';
  var cls = d.sp >= 70 ? 'g' : d.sp >= 40 ? 'y' : 'r';
  return '<div class="stm ' + cls + '" data-app="' + d.sa + '">Steam ' + d.sp + '%好評・' + d.sn.toLocaleString('ja-JP') + '件</div>';
}
grid.addEventListener('click', function(e) {
  var b = e.target.closest('.stm, .gc, .psn');
  if (!b) return;
  var url = b.classList.contains('stm')
    ? 'https://store.steampowered.com/app/' + b.getAttribute('data-app') + '/'
    : b.classList.contains('psn')
    ? (b.getAttribute('data-pid') ? 'https://store.playstation.com/ja-jp/product/' + b.getAttribute('data-pid') : null)
    : b.getAttribute('data-gu');
  if (!url) return;
  e.preventDefault();
  e.stopPropagation();
  window.open(url, '_blank', 'noopener');
});
function esc(s) { return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }
q.addEventListener('input', render);
[minpct, maxprice, sortSel, steamOnly, gcOnly, psnOnly, newLowOnly, minSteam, minPs].forEach(function(el){ el.addEventListener('change', function(){ render(); updateFtoggle(); }); });
// モバイル: 絞り込みの開閉と適用数バッジ
var ftoggle = document.getElementById('ftoggle'), controls = document.getElementById('controls');
ftoggle.addEventListener('click', function(){ controls.classList.toggle('open'); });
function updateFtoggle() {
  var n = 0;
  if (+minpct.value > 0) n++;
  if (maxprice.value) n++;
  if (+minSteam.value > 0) n++;
  if (+minPs.value > 0) n++;
  [steamOnly, gcOnly, psnOnly, newLowOnly].forEach(function(c){ if (c.checked) n++; });
  ftoggle.textContent = n ? '絞り込み(' + n + ')' : '絞り込み';
  ftoggle.classList.toggle('on', n > 0);
}
// 下スクロールでヘッダーを隠し、上スクロールで出す
var hd = document.getElementById('hd'), lastY = 0;
window.addEventListener('scroll', function(){
  var y = window.scrollY;
  if (y > lastY + 5 && y > 120) { hd.classList.add('hide'); controls.classList.remove('open'); }
  else if (y < lastY - 5) { hd.classList.remove('hide'); }
  lastY = y;
}, {passive: true});
updateFtoggle();
render();
</script>
</body>
</html>
"""


def build_html(items):
    data = []
    for it in items:
        d = {k: it[k] for k in ("id", "n", "p", "pct", "mx", "mk", "im")}
        if it.get("sa"):
            d["sa"], d["sp"], d["sn"] = it["sa"], it["sp"], it["sn"]
        if it.get("hm") is not None:
            d["hm"], d["hd"] = it["hm"], it["hd"]
            if it.get("nl"):
                d["nl"] = it["nl"]
        if it.get("gv"):
            d["gv"] = it["gv"]
            if it.get("gu"):
                d["gu"] = it["gu"]
        if it.get("pv"):
            d["pv"], d["pn"] = it["pv"], it["pn"]
            if it.get("pid"):
                d["pid"] = it["pid"]
            if it.get("pp") is not None:
                d["pp"], d["pb"] = it["pp"], it.get("pb")
        data.append(d)
    now = time.strftime("%Y-%m-%d %H:%M")
    html = (TEMPLATE
            .replace("%DATA%", json.dumps(data, ensure_ascii=False, separators=(",", ":")))
            .replace("%IMGPREFIX%", IMG_PREFIX)
            .replace("%NOW%", now)
            .replace("%COUNT%", str(len(data))))
    tmp = OUT_HTML + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(html)
    os.replace(tmp, OUT_HTML)
    log.info("written %s (%d items)", OUT_HTML, len(data))


def main():
    backfill = "--steam-backfill" in sys.argv
    try:
        items = collect()
        if len(items) < 100:
            raise RuntimeError(f"suspiciously few items: {len(items)} — keeping previous HTML")
        update_price_history(items)
        enrich_game_catalog(items)
        try:
            enrich_steam(items, backfill=backfill)
        except Exception:
            # Steam側の障害でページ生成自体は止めない(キャッシュ済み分は付与されないだけ)
            log.exception("steam enrich failed; continuing without fresh steam data")
        try:
            enrich_psn(items, backfill=backfill)
        except Exception:
            log.exception("psn enrich failed; continuing without fresh psn data")
        build_html(items)
    except Exception:
        log.exception("failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
