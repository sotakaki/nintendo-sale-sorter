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
# 過去最安の「表示」フラグ。データ収集(price_history.json)は常時継続しており、
# 履歴が十分溜まったらTrueにしてロールアウトする(2026-08-14収集開始、半年〜1年後を目安)
SHOW_PRICE_HISTORY = False
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "nintendo_sale.log")
STEAM_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "steam_cache.json")
STEAM_OVERRIDES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "steam_overrides.json")
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
                    "pc": x.get("productClassCode") or "",  # HAC=Switch / BEE=Switch 2
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


REG_PRICE_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "regular_prices.json")
REG_RECHECK_DAYS = 30


def enrich_regular_prices(items):
    """実際の定価をshopper-products一括API(24件/リクエスト)で取得する。

    従来はセール価格と割引率ラベルからの逆算だったが、ラベルは整数丸めのため
    高割引帯で大きくズレる(例: 98%OFF表記の実態98.74%OFF → 逆算9,950円 vs 実定価15,795円)。
    """
    try:
        with open(REG_PRICE_CACHE, encoding="utf-8") as f:
            cache = json.load(f)
    except (OSError, ValueError):
        cache = {}
    now = time.time()
    day = 86400
    need = [it["id"] for it in items
            if it["id"] not in cache or now - cache[it["id"]].get("at", 0) > REG_RECHECK_DAYS * day]
    fetched = 0
    if need:
        try:
            token = get_guest_token()
            for i in range(0, len(need), 24):
                chunk = need[i:i + 24]
                q = urllib.parse.urlencode({"ids": ",".join(chunk), "currency": "JPY",
                                            "locale": "ja-JP", "siteId": SITE_ID})
                url = f"{API_BASE}/product/shopper-products/v1/organizations/{ORG_ID}/products?{q}"
                status, _, body = http(url, headers={"Authorization": f"Bearer {token}"})
                if status != 200:
                    log.warning("regular prices batch %d -> HTTP %s", i // 24, status)
                    continue
                for p in json.loads(body).get("data", []):
                    rp = p.get("c_original_regularPriceOnSale")
                    cache[p["id"]] = {"rp": int(rp) if rp else None, "at": now}
                fetched += len(chunk)
                time.sleep(0.3)
        except Exception:
            log.exception("regular prices fetch failed; continuing with cache")
    hit = 0
    for it in items:
        rp = (cache.get(it["id"]) or {}).get("rp")
        if rp and it.get("p") and rp > it["p"]:
            it["rp"] = rp
            hit += 1
    tmp = REG_PRICE_CACHE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False)
    os.replace(tmp, REG_PRICE_CACHE)
    log.info("regular prices: fetched=%d attached=%d/%d cached=%d", fetched, hit, len(items), len(cache))


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
IGDB_ASIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "igdb_asin.json")
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
    # 2) 日本のSwitch向けAmazon ASIN (source=20, platform=130, countries含む392)
    arows = _igdb_page_all(cid, token, "external_games", "game, uid, countries",
                           "external_game_source = 20 & platform = 130")
    asin_of = {}
    for r in arows:
        if 392 in (r.get("countries") or []) and r.get("uid"):
            asin_of.setdefault(r["game"], r["uid"])
    log.info("igdb map: jp switch asins=%d", len(asin_of))
    # 3) 関係するgame idのSteam appIDをバッチ解決 (名前マップ由来 + ASIN保有ゲーム)
    gids = sorted({g for gs in name_games.values() for g in gs} | set(asin_of))
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
    # 4) 正規化名→appid (曖昧な名前=複数ゲームで異なるappidに割れるものは捨てる)
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
    # 5) ASINマップ: 日本語名→ASIN と steam appid→ASIN の両引き
    by_name = {}
    for nm, gs in name_games.items():
        asins = {asin_of[g] for g in gs if g in asin_of}
        if len(asins) == 1:
            by_name[nm] = asins.pop()
    by_steam = {str(steam_of[g]): a for g, a in asin_of.items() if g in steam_of}
    tmp = IGDB_ASIN + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"by_name": by_name, "by_steam": by_steam}, f, ensure_ascii=False)
    os.replace(tmp, IGDB_ASIN)
    log.info("igdb map: asin by_name=%d by_steam=%d", len(by_name), len(by_steam))


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


# ---------------------------------------------------------------- Wikidata→Steam補完
# 方式: SPARQL一括クエリで「日本語ラベル/別名→Steam appID」の対応表を作り置きし、
# 日々の照合はローカルで行う(API呼び出しゼロ)。再構築は --steam-backfill 時のみ(1クエリ)。
# ※旧方式(ja.wikipedia検索の逐次クロール)は429連発で廃止。成果はsteam_cacheに残存。
WIKIDATA_MAP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wikidata_ja_map.json")


def build_wikidata_map():
    """Wikidataから日本語名→Steam appIDの対応表を1クエリで構築して保存する"""
    q = """SELECT ?name ?steam WHERE {
      ?item wdt:P1733 ?steam .
      { ?item rdfs:label ?name . FILTER(lang(?name) = "ja") }
      UNION
      { ?item skos:altLabel ?name . FILTER(lang(?name) = "ja") }
    }"""
    url = "https://query.wikidata.org/sparql?format=json&query=" + urllib.parse.quote(q)
    req = urllib.request.Request(url, headers={
        "User-Agent": "nintendo-sale-sorter/1.0 (personal tool; github.com/sotakaki/nintendo-sale-sorter)"})
    rows = json.load(urllib.request.urlopen(req, timeout=120))["results"]["bindings"]
    pairs = {}
    for r in rows:
        nm = norm_name(r["name"]["value"])
        try:
            appid = int(r["steam"]["value"])
        except (TypeError, ValueError):
            continue
        if nm:
            pairs.setdefault(nm, set()).add(appid)
    out = {nm: apps.pop() for nm, apps in pairs.items() if len(apps) == 1}  # 曖昧名は捨てる
    tmp = WIKIDATA_MAP + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    os.replace(tmp, WIKIDATA_MAP)
    log.info("wikidata map: built %d ja-name->steam entries (rows=%d)", len(out), len(rows))


def enrich_wikidata_steam(items, steam_cache, backfill=False):
    """wikidata_ja_map.json(日本語名→Steam appID)でローカル照合し、steam_cacheに注入する"""
    if backfill:
        try:
            build_wikidata_map()
        except Exception:
            log.exception("wikidata map build failed; using existing map")
    try:
        with open(WIKIDATA_MAP, encoding="utf-8") as f:
            wmap = json.load(f)
    except (OSError, ValueError):
        log.info("wikidata: no map file, skipping")
        return
    now = time.time()
    found = 0
    for it in items:
        sc = steam_cache.get(it["id"])
        if sc is None or sc.get("appid") is not None:
            continue
        appid = wmap.get(norm_name(it["n"]))
        if appid:
            found += 1
            steam_cache[it["id"]] = {"appid": appid, "checked": now,
                                     "rev": None, "rev_at": 0, "via": "wikidata"}
    log.info("wikidata-steam: map=%d found=%d", len(wmap), found)


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
            # 商品IDは「GAME」カテゴリのものだけを候補にする(先頭がDLCのconceptがあるため)
            ids = []
            for cp in cm.get("categorizedProducts") or []:
                if cp.get("topCategory") == "GAME":
                    ids += cp.get("ids") or []
            if not ids:
                for cp in cm.get("categorizedProducts") or []:
                    ids += cp.get("ids") or []
            sr = cm.get("starRating") or {}
            return {"cid": cm.get("id"), "pid": ids[0] if ids else None,
                    "pids": ids[:4],
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
    # 価格ブロックは複数ある(PS Plus会員向け「含まれます」=0円と非会員向け通常/セール価格)。
    # serviceBranding が NONE のブロック=非会員価格を優先する
    prices = re.findall(
        r'"serviceBranding":\["(\w+)"[^{}]*?"basePriceValue":(\d+),"discountedValue":(\d+),"currencyCode":"JPY"',
        html)
    out = {}
    cls = re.search(r'"storeDisplayClassification":"([A-Z_]+)"', html)
    if cls:
        out["cls"] = cls.group(1)  # FULL_GAME / ADD_ON / DEMO 等(本体判定用)
    if m:
        out["r"] = float(m.group(1))
        out["rc"] = int(m.group(2))
    if prices:
        chosen = next((p for p in prices if p[0] == "NONE"), prices[0])
        out["bp"] = int(chosen[1])
        out["pp"] = int(chosen[2])
    return out or None


PSN_BAD_CLS = {"ADD_ON", "DEMO", "VIRTUAL_CURRENCY", "SUBSCRIPTION", "THEME", "AVATAR"}


def psn_pick_full_game(candidates):
    """商品ID候補を順に照会し、ゲーム本体(FULL_GAME優先、DLC等は除外)を選ぶ → (pid, prod) or (None, None)"""
    fallback = None
    for cand in (candidates or [])[:4]:
        prod = psn_product(cand)
        if not prod:
            continue
        cls = prod.get("cls", "")
        if cls == "FULL_GAME":
            return cand, prod
        if cls not in PSN_BAD_CLS and fallback is None:
            fallback = (cand, prod)  # PREMIUM_EDITION等は本体扱いの保険候補
    return fallback or (None, None)


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
                        # DLCやデモを掴まないよう、候補から「ゲーム本体」を選んで確定する
                        pid, prod = psn_pick_full_game(hit.get("pids") or [hit["pid"]])
                        c["pid"] = pid
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
                    if prod and prod.get("cls") in PSN_BAD_CLS:
                        # 旧ロジックでDLC等を掴んでいた → エントリを破棄して再検索させる
                        del cache[it["id"]]
                        continue
                    if prod:
                        c.update(prod)
                        c["rev_at"] = now
            if c.get("r") and c.get("rc"):
                it["pv"] = c["r"]
                it["pn"] = c["rc"]
                it["pid"] = c.get("pid")
                # 非会員価格(serviceBranding=NONE)のみ採用。0/0は有効な価格ブロックなし
                # (体験版等の誤検出)とみなし価格非表示にする(★評価だけ出す)
                if c.get("pp"):
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


ASIN_OVERRIDES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "asin_overrides.json")

# ---------------------------------------------------------------- Amazon Creators API
# PA-API後継の公式アフィリエイトAPI。OAuth2(LWA) client_credentials認証。
# 束ねアカウント(レップ管理)なので通常運用は1日500リクエスト以内に抑える。
AMZ_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "amazon_cache.json")
CREATORS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".creators_api")
AMZ_TOKEN_URL = "https://api.amazon.co.jp/auth/o2/token"   # v3.3=極東リージョン
AMZ_API = "https://creatorsapi.amazon"
AMZ_MARKETPLACE = "www.amazon.co.jp"
AMZ_INTERVAL = 1.1
AMZ_SEARCH_CAP = 300           # 通常実行1回あたりの新規タイトル検索数上限
AMZ_SEARCH_CAP_BACKFILL = 1500
AMZ_NOMATCH_RECHECK_DAYS = 30
_last_amz_req = [0.0]


def creators_creds():
    cid = os.environ.get("CREATORS_CLIENT_ID", "").strip()
    sec = os.environ.get("CREATORS_CLIENT_SECRET", "").strip()
    if cid and sec:
        return cid, sec
    try:
        with open(CREATORS_FILE, encoding="utf-8") as f:
            parts = f.read().split()
        return parts[0], parts[1]
    except (OSError, IndexError):
        return None, None


def creators_token(cid, sec):
    data = urllib.parse.urlencode({
        "grant_type": "client_credentials", "client_id": cid,
        "client_secret": sec, "scope": "creatorsapi::default"}).encode()
    status, _, body = http(AMZ_TOKEN_URL, data=data,
                           headers={"Content-Type": "application/x-www-form-urlencoded"})
    if status != 200:
        log.warning("creators api: token failed %s", status)
        return None
    return json.loads(body)["access_token"]


def creators_call(token, path, body):
    wait = AMZ_INTERVAL - (time.monotonic() - _last_amz_req[0])
    if wait > 0:
        time.sleep(wait)
    _last_amz_req[0] = time.monotonic()
    req = urllib.request.Request(
        AMZ_API + path, data=json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json",
                 "x-marketplace": AMZ_MARKETPLACE, "User-Agent": UA})
    try:
        return json.load(urllib.request.urlopen(req, timeout=30))
    except (urllib.error.URLError, ValueError) as e:
        log.warning("creators api %s failed: %s", path, str(e)[:120])
        return None


AMZ_TYPE_RANK = {"pkg": 0, "dl": 1, "imp": 2}


def amz_type(title):
    """Amazon商品名から種別を推定: 国内パッケージ(pkg) / ダウンロードコード(dl) / 輸入版(imp)"""
    t = title or ""
    if "オンラインコード" in t or "ダウンロードコード" in t or "download code" in t.lower():
        return "dl"
    if re.search(r"輸入版|海外版|北米版|欧州版|アジア版|\bimport\b", t, re.IGNORECASE):
        return "imp"
    return "pkg"


def amz_search_cands(token, tag, name):
    """タイトル名でsearchItemsし、名前検証を通った候補を[(asin, 商品名), ...]で返す。

    「タイトル + Switch」で見つからなければ「タイトルのみ」でも再検索する
    (Amazon検索の並び順の癖でドンズバ商品が漏れるケースの救済)。
    パッケージ/DLコード/輸入版が併売されるため、1件でなく候補全部を集めて
    表示時にeショップ価格との比較で選び直す。
    """
    target = norm_name(name)
    if len(target) < 4:  # 超短名は誤マッチしやすいので自動検索しない(オーバーライドで対応)
        return []
    for keywords in (name[:120] + " Switch", name[:120]):
        res = creators_call(token, "/catalog/v1/searchItems", {
            "keywords": keywords,
            "searchIndex": "VideoGames",
            "itemCount": 10,
            "partnerTag": tag,
            "resources": ["itemInfo.title"],
        })
        items = ((res or {}).get("searchResult") or {}).get("items") or []
        cands = []
        for it in items:
            title = (((it.get("itemInfo") or {}).get("title") or {}).get("displayValue")) or ""
            c = norm_name(title)
            # 部分一致は8文字以上のタイトルのみ(短名は"JARS"⊂"Jar Jar's..."型の誤爆があるため全体類似のみ)
            if ((len(target) >= 8 and target in c)
                    or difflib.SequenceMatcher(None, target, c).ratio() >= 0.8):
                if it.get("asin") and it["asin"] not in [a for a, _ in cands]:
                    cands.append((it["asin"], title))
        if cands:
            return cands[:6]
    return []


def amz_get_info(token, tag, asins):
    """getItemsで最大10件ずつbuybox価格+商品名を取得 → {asin: {"p": 円|None, "t": 商品名}}"""
    out = {}
    for i in range(0, len(asins), 10):
        chunk = asins[i:i + 10]
        res = creators_call(token, "/catalog/v1/getItems", {
            "itemIds": chunk,
            "partnerTag": tag,
            "resources": ["offersV2.listings.price", "itemInfo.title"],
        })
        for it in ((res or {}).get("itemsResult") or {}).get("items") or []:
            title = (((it.get("itemInfo") or {}).get("title") or {}).get("displayValue")) or ""
            p = None
            for l in (it.get("offersV2") or {}).get("listings") or []:
                if l.get("isBuyBoxWinner"):
                    amt = ((l.get("price") or {}).get("money") or {}).get("amount")
                    if amt is not None:
                        p = int(amt)
                    break
            out[it["asin"]] = {"p": p, "t": title}
    return out


def amz_platform_ok(asin, title):
    """Switch以外のプラットフォーム専用商品(PC版/PS4版など)を候補から外す。

    商品名にSwitch表記があれば通し、PC/PlayStation/Xbox系の表記だけなら弾く。
    さらにASINは発行順なので、Switch発売(2017年)前のB05番台以前のASINは
    商品名に機種表記がなくても他機種版(例: 2007年PC版Sherlock Holmes)とみなして弾く。
    """
    t = (title or "").lower()
    if "switch" in t or "スイッチ" in t:
        return True
    if not asin.startswith("B0") or asin < "B06":
        return False
    if not t:
        return True
    return not re.search(r"\bpc\b|windows|dvd-rom|playstation|プレイステーション|\bps[345]\b|xbox", t)


def amz_rank(asin, casins, info, eshop_price):
    """候補の優先順キー(小さいほど優先)。

    1)eショップより安い国内pkg 2)安いDLコード 3)安い輸入版
    4)同額のpkg/DLコード 5)同額の輸入版 6)高いpkg/DLコード 7)高い輸入版
    価格不明の候補は最後(種別だけで順位づけ)。同順位は安いほう→pkg優先。
    """
    ty = (casins.get(asin) or {}).get("ty") or "pkg"
    tyr = AMZ_TYPE_RANK.get(ty, 0)
    ap = (info.get(asin) or {}).get("p")
    if ap is None or eshop_price is None:
        return (3, tyr, 10 ** 9, tyr)
    grp = 0 if ap < eshop_price else (1 if ap == eshop_price else 2)
    sub = tyr if grp == 0 else (1 if ty == "imp" else 0)
    return (grp, sub, ap, tyr)


def enrich_amazon(items, backfill=False):
    """Amazon直リンク+現在価格+種別(パッケージ/DLコード/輸入版)を付与(テクノエッジ版用)。

    候補の集め方: 手動オーバーライド(asin_overrides.json、タイトル名 or id:商品ID)は単独で最優先
                → IGDB由来 + Creators API検索の全候補(キャッシュ・日次キャップつき)
    価格はCreators APIのbuybox価格を毎回取得(24hより古い表示をしないため)。
    eショップのセール価格は毎日動くので、どの候補を出すかは毎ビルドamz_rankで選び直す。
    """
    try:
        with open(IGDB_ASIN, encoding="utf-8") as f:
            amap = json.load(f)
    except (OSError, ValueError):
        amap = {}
    # オーバーライドのキーは「タイトル名」または「id:商品ID」(同名でSwitch/Switch 2版が分かれる場合など)
    overrides, ov_by_id = {}, {}
    try:
        with open(ASIN_OVERRIDES, encoding="utf-8") as f:
            for k, v in json.load(f).items():
                if k.startswith("id:"):
                    ov_by_id[k[3:]] = v
                else:
                    overrides[norm_name(k)] = v
    except (OSError, ValueError):
        pass
    by_name, by_steam = amap.get("by_name", {}), amap.get("by_steam", {})
    try:
        with open(AMZ_CACHE, encoding="utf-8") as f:
            acache = json.load(f)
    except (OSError, ValueError):
        acache = {}
    # v2移行: 旧形式{id:{asin,checked}}はno-matchの記憶だけ引き継ぎ、
    # ASINあり項目は候補リスト(パッケージ/DLコード/輸入版)収集のため再検索させる
    if "_v" not in acache:
        old = acache
        acache = {"_v": 2, "items": {}, "asins": {}}
        for k, v in old.items():
            if isinstance(v, dict) and not v.get("asin"):
                acache["items"][k] = {"cands": [], "checked": v.get("checked", 0)}
    citems, casins = acache["items"], acache["asins"]
    cid, sec = creators_creds()
    token = creators_token(cid, sec) if cid else None
    tag = os.environ.get("AMAZON_TAG", "technoedge-22")
    now = time.time()
    day = 86400
    budget = AMZ_SEARCH_CAP_BACKFILL if backfill else AMZ_SEARCH_CAP
    searched = 0
    found = 0
    try:
        for it in items:
            forced = ov_by_id.get(it["id"]) or overrides.get(norm_name(it["n"]))
            cands = []
            if forced:
                cands = [forced]  # 手動オーバーライドは編集判断として常に最優先
            else:
                for a in (by_steam.get(str(it.get("appid") or "")),
                          by_name.get(norm_name(it["n"]))):
                    if a and a not in cands:
                        cands.append(a)
                ce = citems.get(it["id"])
                if ce is not None and (ce.get("cands")
                                       or now - ce.get("checked", 0) < AMZ_NOMATCH_RECHECK_DAYS * day):
                    for a in ce.get("cands") or []:
                        if a not in cands:
                            cands.append(a)
                elif token and budget > 0:
                    budget -= 1
                    searched += 1
                    hits = amz_search_cands(token, tag, it["n"])
                    citems[it["id"]] = {"cands": [a for a, _ in hits], "checked": now}
                    for a, t in hits:
                        casins[a] = {"t": t[:120], "ty": amz_type(t)}
                        if a not in cands:
                            cands.append(a)
            if cands:
                it["_azc"] = cands
                it["_azforced"] = bool(forced)
        # 全候補の現在価格(buybox)+商品名を取得し、
        # 種別×eショップ価格比較の優先順(amz_rank)で表示する1件を選ぶ
        priced = 0
        info = {}
        if token:
            asins = sorted({a for it in items for a in it.get("_azc", [])})
            info = amz_get_info(token, tag, asins)
            for a, v in info.items():
                if v.get("t"):  # getItemsの商品名で種別判定を毎日更新(オーバーライド/IGDB由来も拾う)
                    casins[a] = {"t": v["t"][:120], "ty": amz_type(v["t"])}
        for it in items:
            cands = it.pop("_azc", None)
            forced = it.pop("_azforced", False)
            if not forced and cands:
                # 手動オーバーライド以外は他機種版(PC/PS4等)を候補から除外
                cands = [a for a in cands
                         if amz_platform_ok(a, (casins.get(a) or {}).get("t"))]
            if not cands:
                continue
            best = cands[0] if forced else min(
                cands, key=lambda a: amz_rank(a, casins, info, it.get("p")))
            it["az"] = best
            found += 1
            p = (info.get(best) or {}).get("p")
            if p:
                it["azp"] = p
                priced += 1
            ty = (casins.get(best) or {}).get("ty")
            if ty in ("dl", "imp"):  # DLコード・輸入版は必ず明示(表示側でラベルにする)
                it["azt"] = ty
        log.info("amazon: direct links=%d/%d searched=%d priced=%d (overrides=%d)",
                 found, len(items), searched, priced, len(overrides))
    finally:
        tmp = AMZ_CACHE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(acache, f, ensure_ascii=False)
        os.replace(tmp, AMZ_CACHE)


KOTY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "koty.json")


def enrich_koty(items):
    """クソゲーオブザイヤー(据置Wiki)受賞作なら称号を付与。完全一致のみ(続編誤爆防止)"""
    try:
        with open(KOTY_FILE, encoding="utf-8") as f:
            koty = {norm_name(k): v for k, v in json.load(f).items()}
    except (OSError, ValueError):
        return
    found = 0
    for it in items:
        award = koty.get(norm_name(it["n"]))
        if award:
            it["kt"] = award
            found += 1
    log.info("koty: matched=%d", found)


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
    """itemsにSteamレビュー情報(sa/sp/sn)を付与。照会結果はキャッシュして差分だけ叩く

    steam_overrides.json(キー=タイトル名 or id:商品ID、値=appid or null)で
    自動マッチングの誤りを手動修正できる。null=Steamマッチ禁止
    (例:「小さな虫」がREDDEERと無関係の「Little Bug」に誤マッチしたケース)。
    """
    try:
        with open(STEAM_CACHE, encoding="utf-8") as f:
            cache = json.load(f)
    except (OSError, ValueError):
        cache = {}
    st_ov, st_ov_by_id = {}, {}
    try:
        with open(STEAM_OVERRIDES, encoding="utf-8") as f:
            for k, v in json.load(f).items():
                if k.startswith("id:"):
                    st_ov_by_id[k[3:]] = v
                else:
                    st_ov[norm_name(k)] = v
    except (OSError, ValueError):
        pass
    try:
        # Steam検索で見つからなかった日本語名タイトルをIGDBの地域別名で補完
        enrich_igdb_steam(items, cache, backfill=backfill)
    except Exception:
        log.exception("igdb-steam failed; continuing")
    try:
        # IGDBでも残った日本語名タイトルをWikidataの対応表で補完
        enrich_wikidata_steam(items, cache, backfill=backfill)
    except Exception:
        log.exception("wikidata-steam failed; continuing")
    now = time.time()
    day = 86400
    search_budget = 10**9 if backfill else STEAM_SEARCH_CAP
    review_budget = 10**9 if backfill else STEAM_REVIEW_CAP
    searched = reviewed = 0

    try:
        for it in items:
            c = cache.get(it["id"])
            # 0) 手動オーバーライド(誤マッチ修正)。適用済みなら以降の自動マッチはしない
            ov = (st_ov_by_id[it["id"]] if it["id"] in st_ov_by_id
                  else st_ov.get(norm_name(it["n"]), "-"))
            if ov != "-":
                if c is None or c.get("appid") != ov:
                    c = {"appid": ov, "checked": now, "rev": None, "rev_at": 0}
                    cache[it["id"]] = c
            # 1) appidマッチング(未照会 or マッチなしの定期再確認)
            elif c is None or (c.get("appid") is None
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
            it["appid"] = c["appid"]  # ASIN解決(enrich_amazon)用
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
        # Steam現在価格(appdetailsのprice_overviewフィルタは複数appidまとめ取り可)
        try:
            id_by_app = {}
            for it in items:
                c2 = cache.get(it["id"]) or {}
                if c2.get("appid"):
                    id_by_app.setdefault(c2["appid"], []).append(it["id"])
            stale = [a for a, iids in id_by_app.items()
                     if now - (cache.get(iids[0], {}).get("spr_at") or 0) > 0.8 * day]
            for i in range(0, len(stale), 50):
                chunk = stale[i:i + 50]
                res = steam_get("https://store.steampowered.com/api/appdetails?appids=%s"
                                "&cc=JP&l=japanese&filters=price_overview"
                                % ",".join(map(str, chunk)))
                for a in chunk:
                    e = (res or {}).get(str(a)) or {}
                    data_f = e.get("data")
                    po = data_f.get("price_overview") if isinstance(data_f, dict) else None
                    for iid in id_by_app[a]:
                        c2 = cache.get(iid)
                        if c2 is None:
                            continue
                        c2["spr_at"] = now
                        if po and po.get("final"):
                            c2["spr"] = int(po["final"] // 100)
                            c2["spd"] = po.get("discount_percent", 0)
                        else:
                            c2.pop("spr", None)
                            c2.pop("spd", None)
            priced = 0
            for it in items:
                c2 = cache.get(it["id"]) or {}
                if c2.get("spr"):
                    it["stp"] = c2["spr"]
                    if c2.get("spd"):
                        it["std"] = c2["spd"]
                    priced += 1
            log.info("steam prices: refreshed_apps=%d priced=%d", len(stale), priced)
        except Exception:
            log.exception("steam prices failed; continuing")
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
%TEMETA%%TEGA%<title>ニンテンドーストア セール 値引き率順 (%COUNT%件 / %NOW%取得)</title>
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
.hw2 { display:inline-block; border:1px solid #e60012; color:#e60012; border-radius:3px; padding:0 3px; margin-right:4px; font-size:9px; font-weight:700; }
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
.koty { font-size:9px; margin-top:2px; display:inline-block; background:#7b1113; color:#fff; border-radius:3px; padding:1px 5px; font-weight:700; cursor:pointer; }
.koty:hover { opacity:.85; }
.mk.mkbottom { margin-top:6px; padding-top:4px; border-top:1px solid var(--line); }
.mkr { font-size:10px; margin-top:1px; color:var(--sub); padding:4px 0; }
.mkr.none { font-size:9px; }
.mc { font-weight:700; cursor:pointer; display:inline-block; padding:6px 4px; margin:-6px -4px; }
.mc:hover { text-decoration:underline; }
/* 出典色はカラーユニバーサルデザイン(Okabe-Ito)配色 */
.mc.steam { color:#009E73; }
.mc.psn { color:#0072B2; }
.mc.gc { color:#B87A00; }
.mc.koty { color:#D55E00; }
@media (prefers-color-scheme: dark) {
  .mc.steam { color:#2fd6a5; } .mc.psn { color:#56B4E9; } .mc.gc { color:#E69F00; } .mc.koty { color:#ff8a4d; }
}
@media (prefers-color-scheme: dark) { .gc.good { color:#7bc67e; } .gc.mid { color:#e0a34e; } .gc.bad { color:#e57373; } }
.psn { font-size:10px; margin-top:2px; cursor:pointer; color:#0057b8; }
.psn:hover { text-decoration:underline; }
.psn .sale { color:var(--accent); font-weight:600; }
@media (prefers-color-scheme: dark) { .psn { color:#5c9ded; } }
.amz { font-size:10px; margin-top:4px; cursor:pointer; color:#b26a00; border:1px solid currentColor; border-radius:6px; padding:2px 6px; align-self:flex-start; }
.amz:hover { background:rgba(178,106,0,.08); }
.amz .prtag { font-size:8px; border:1px solid currentColor; border-radius:3px; padding:0 2px; margin-left:4px; vertical-align:1px; }
@media (prefers-color-scheme: dark) { .amz { color:#e0a34e; } }
#affnotice { font-size:11px; color:var(--sub); padding:8px 16px 0; max-width:1400px; margin:0 auto; }
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
  <label class="chk"%LOWDISP%><input type="checkbox" id="newlowonly">過去最安のみ</label>
  </div>
</header>
%TETHEME%
%AFFNOTICE%
<div id="count"></div>
<div id="grid"></div>
<footer>データはニンテンドーストアの検索APIから取得。価格・値引き率は取得時点のもの。「最大◯%OFF」はパッケージ版/DL版などで率が異なる商品。<br>Steamレビューはタイトル名の自動マッチングによる参考情報(Switch版の評価ではありません)。クリックでSteamページを開きます。<br>「過去最安」は2026-08-14からの自前トラッキングによるもので、それ以前のセール履歴は含みません。<br>判定表記は<a href="https://w.atwiki.jp/gcmatome/" target="_blank" rel="noopener">ゲームカタログ@Wiki</a>の判定(タイトル名の自動マッチング)。クリックで該当記事を開きます。「クソゲーオブザイヤー」は<a href="https://koty.wiki/" target="_blank" rel="noopener">KOTY据置Wiki</a>の受賞歴です。<br>各カード下部のメーカー名の下に、同メーカーの現セール中作品の最高評価(↑)と最低評価(↓)を表示しています。色は出典: <span style="color:#009E73;font-weight:700">Steam(〜%)</span> / <span style="color:#0072B2;font-weight:700">PS Store(★〜)</span> / <span style="color:#B87A00;font-weight:700">ゲームカタログ@Wiki(判定語)</span> / <span style="color:#D55E00;font-weight:700">クソゲーオブザイヤー(KOTY〜)</span>。表記形式でも判別できます。クリックで該当作品のページへ。<br>「PS ★」はPlayStation Store(日本)の星評価と現在価格(自動マッチング・参考情報。Switch版の評価ではありません)。クリックでPS Storeを開きます。<br><br>本サイトは個人が運営する<b>非公式サイト</b>であり、任天堂株式会社、株式会社ソニー・インタラクティブエンタテインメント、Valve Corporationその他の企業とは一切関係ありません。<br>ゲーム画像・タイトル名等の商標・著作権は各権利者に帰属します。価格・値引き率・評価は取得時点の参考情報であり、正確性を保証しません。購入の際は必ず各公式ストアで最新の価格をご確認ください。<br>掲載内容に問題がある場合は<a href="https://github.com/sotakaki/nintendo-sale-sorter/issues" target="_blank" rel="noopener">GitHubのIssue</a>からご連絡ください。速やかに対応します。</footer>
<script>
var DATA = %DATA%;
var IMG = "%IMGPREFIX%";
var AFF_TAG = "%AFFTAG%";
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
  var html = buildCards(list);
  grid.innerHTML = html;
  observeLazy();
}
function buildCards(list) {
  var cards = list.map(cardHtml);
  if (!AFF_TAG) return cards.join('');
  var out = [];
  for (var i = 0; i < cards.length; i++) {
    out.push(cards[i]);
    var rank = i + 1;
    // プリペイド枠: 10位の直後、以降は30件ごと(10, 40, 70, ...)
    if (rank >= 10 && (rank - 10) % 30 === 0) out.push(prepaidCard());
  }
  return out.join('');
}
function prepaidCard() {
  return '<a class="card pcard" href="https://www.amazon.co.jp/dp/%PREPAIDASIN%?tag=' + AFF_TAG + '" target="_blank" rel="noopener">'
    + '<span class="pc-pr">PR</span>'
    + '<img class="pc-img" src="%PREPAIDIMG%" alt="ニンテンドープリペイドカード">'
    + '<div class="pc-t">ニンテンドープリペイド</div>'
    + '<div class="pc-s">ダウンロード版の購入・<br>残高チャージに</div>'
    + '<div class="pc-btn">Amazonで購入</div></a>';
}
function cardHtml(d) {
  return (function(d) {
    var orig = d.rp || ((!d.mx && d.p != null && d.pct <= 80) ? Math.round(d.p / (1 - d.pct/100)) : null);
    return '<a class="card" href="https://store-jp.nintendo.com/item/software/D' + d.id + '" target="_blank" rel="noopener">'
      + (d.im
         ? (AFF_TAG
            ? '<img class="lz" data-src="' + IMG + d.im + '?sw=346&strip=false" alt="">'
            : '<img loading="lazy" src="' + IMG + d.im + '?sw=346&strip=false" alt="">')
         : '<div style="aspect-ratio:1;background:#ddd"></div>')
      + '<div class="b"><div class="n">' + esc(d.n) + '</div>'
      + (d.hw ? '<div class="mk"><span class="hw2">Switch 2</span></div>' : '')
      + '<div class="row"><span class="off">' + (d.mx ? '<small>最大</small>' : '') + d.pct + '<small>%OFF</small></span>'
      + '<span class="pr"><b>' + yen(d.p) + '</b>' + (d.mx ? '〜' : '') + '</span></div>'
      + (orig ? '<div class="mk">定価 ' + yen(orig) + '</div>' : '')
      + lowBadge(d)
      + kotyBadge(d)
      + gcBadge(d)
      + steamBadge(d)
      + psnBadge(d)
      + amazonBadge(d)
      + '<div class="mk mkbottom">' + esc(d.mk) + '</div>'
      + makerChips(d)
      + '</div></a>';
  })(d);
}
// iframe埋め込みだと標準のloading="lazy"やIntersectionObserverが発火しないことがあるため、
// TEモードはスクロール位置ベースの自前遅延読み込み(同一オリジンなら親のスクロールを直接監視)
var lazyList = [];
function lazyMode() {
  // same-frame: 同一ドメインiframe(親スクロール連動) / cross-frame: 別ドメインiframe(ブラウザ標準lazyに委譲) / standalone: 直接表示
  if (window.parent === window) return 'standalone';
  try { if (window.frameElement) return 'same-frame'; } catch (e) {}
  return 'cross-frame';
}
function observeLazy() {
  var imgs = grid.querySelectorAll('img.lz');
  if (lazyMode() === 'cross-frame') {
    // 別ドメインiframeでは親スクロールを参照できないため、ブラウザ標準の遅延読み込みに任せる
    imgs.forEach(function(el) {
      el.loading = 'lazy';
      el.src = el.getAttribute('data-src');
      el.classList.remove('lz');
    });
    lazyList = [];
    return;
  }
  lazyList = Array.prototype.map.call(imgs, function(el) {
    return {el: el, top: el.getBoundingClientRect().top + window.scrollY};
  });
  updateLazy();
}
function updateLazy() {
  if (!lazyList.length) return;
  var start, end, MARGIN = 900;
  var fe = null;
  try { fe = window.frameElement; } catch (e) {}
  if (fe) {
    var r = fe.getBoundingClientRect();
    var vh = window.top.innerHeight;
    start = -r.top - MARGIN;
    end = -r.top + vh + MARGIN;
  } else {
    start = window.scrollY - MARGIN;
    end = window.scrollY + window.innerHeight + MARGIN;
  }
  lazyList = lazyList.filter(function(o) {
    if (o.top >= start && o.top <= end) {
      o.el.src = o.el.getAttribute('data-src');
      o.el.classList.remove('lz');
      return false;
    }
    return true;
  });
}
(function() {
  var scrollTarget = window;
  try { if (window.frameElement) scrollTarget = window.top; } catch (e) {}
  try { scrollTarget.addEventListener('scroll', updateLazy, {passive: true}); } catch (e) {}
  try { scrollTarget.addEventListener('resize', updateLazy, {passive: true}); } catch (e) {}
  setInterval(updateLazy, 1200);  // 保険(スクロールイベントを取り逃した場合)
})();
function lowBadge(d) {
  if (d.nl === 1) return '<div class="low new">過去最安更新 (前回 ' + yen(d.hm) + ')</div>';
  if (d.nl === 2) return '<div class="low tie">過去最安 (' + d.hd.slice(2).replace(/-/g, '/') + '〜)</div>';
  if (d.hm != null && d.hm < d.p) return '<div class="low was">過去最安 ' + yen(d.hm) + ' (' + d.hd.slice(2).replace(/-/g, '/') + ')</div>';
  return '';
}
function amazonBadge(d) {
  if (!AFF_TAG) return '';
  if (d.az) {
    var tyl = d.azt === 'dl' ? 'DLコード' : (d.azt === 'imp' ? '輸入版' : '');
    var label = 'Amazonで購入' + (d.azp
      ? '（' + (tyl ? tyl + ' ' : '') + yen(d.azp) + '）'
      : (tyl ? '（' + tyl + '）' : ''));
    var cheaper = d.azp && d.p != null && d.azp < d.p;
    return '<div class="amz' + (cheaper ? ' amz-low' : '') + '" data-az="' + d.az + '">' + label
      + (cheaper ? '<span class="lowtag">eショップより安い</span>' : '') + '</div>';
  }
  return '<div class="amz" data-q="' + esc(d.n) + '">Amazonで探す</div>';
}
function psnBadge(d) {
  if (d.pv == null) return '';
  var s = 'PS ★' + d.pv.toFixed(1) + '（' + d.pn.toLocaleString('ja-JP') + '）';
  if (d.pp != null) {
    if (d.pp === 0) s += '無料';
    else {
      s += yen(d.pp);
      if (d.pb != null && d.pp < d.pb) s += '（' + Math.round((1 - d.pp / d.pb) * 100) + '%OFF）';
    }
  }
  return '<div class="psn"' + (d.pid ? ' data-pid="' + d.pid + '"' : '') + '>' + s + '</div>';
}
function kotyBadge(d) {
  if (!d.kt) return '';
  return '<div class="koty">クソゲーオブザイヤー ' + esc(d.kt) + '</div>';
}
function gcBadge(d) {
  if (!d.gv) return '';
  var cls = /^良/.test(d.gv) ? 'good' : /クソ|劣化|シリ不|不安定/.test(d.gv) ? 'bad' : /^普通/.test(d.gv) ? 'na' : 'mid';
  return '<div class="gc ' + cls + '"' + (d.gu ? ' data-gu="' + esc(d.gu) + '"' : '') + '>ゲームカタログ@Wiki: ' + esc(d.gv) + '</div>';
}
function steamBadge(d) {
  if (d.sp == null) return '';
  var cls = d.sp >= 70 ? 'g' : d.sp >= 40 ? 'y' : 'r';
  var s = 'Steam ' + d.sp + '%好評（' + d.sn.toLocaleString('ja-JP') + '件）';
  if (d.stp) s += yen(d.stp) + (d.std ? '（' + d.std + '%OFF）' : '');
  return '<div class="stm ' + cls + '" data-app="' + d.sa + '">' + s + '</div>';
}
// メーカー実績チップ: レビューが無いタイトルに、同メーカーの最高/最低評価作を出典色付きで表示
// 色=出典(凡例はフッター): steam / psn / gc(カタログ) / koty
var makerStats = (function() {
  var by = {};
  DATA.forEach(function(d) { (by[d.mk] = by[d.mk] || []).push(d); });
  function sigOf(d) {
    // その作品の代表シグナル候補: [内部スコア, 表示値, 出典クラス, リンクURL]
    var sigs = [];
    if (d.kt) sigs.push([5, 'KOTY' + (d.kt.indexOf('大賞') >= 0 ? '大賞' : '次点'), 'koty', 'https://koty.wiki/Awarded']);
    if (d.gv) {
      var s = d.gv === '良作' ? 85 : (/クソゲー|劣化|不安定|シリ不/.test(d.gv) ? 10 : 50);
      sigs.push([s, d.gv, 'gc', d.gu || 'https://w.atwiki.jp/gcmatome/']);
    }
    if (d.sp != null && (d.sn || 0) >= 20) sigs.push([d.sp, d.sp + '%', 'steam', 'https://store.steampowered.com/app/' + d.sa + '/']);
    if (d.pv != null && (d.pn || 0) >= 50) sigs.push([d.pv * 20, '★' + d.pv.toFixed(1), 'psn', d.pid ? 'https://store.playstation.com/ja-jp/product/' + d.pid : 'https://store.playstation.com/']);
    return sigs;
  }
  var out = {};
  Object.keys(by).forEach(function(mk) {
    var rated = [];
    by[mk].forEach(function(d) {
      var sigs = sigOf(d);
      if (sigs.length) {
        var avg = sigs.reduce(function(a, s){ return a + s[0]; }, 0) / sigs.length;
        rated.push({d: d, sigs: sigs, avg: avg});
      }
    });
    var st = {total: by[mk].length, rated: rated.length};
    if (rated.length) {
      rated.sort(function(a, b){ return a.avg - b.avg; });
      var worst = rated[0], best = rated[rated.length - 1];
      var bs = best.sigs.slice().sort(function(a, b){ return b[0] - a[0]; })[0];
      var ws = worst.sigs.slice().sort(function(a, b){ return a[0] - b[0]; })[0];
      st.best = {v: bs[1], c: bs[2], u: bs[3], t: best.d.n};
      st.worst = {v: ws[1], c: ws[2], u: ws[3], t: worst.d.n};
    }
    out[mk] = st;
  });
  return out;
})();
function makerChips(d) {
  var st = makerStats[d.mk];
  if (!st) return '';
  if (!st.rated) return '<div class="mkr none">メーカー評価作なし(0/' + st.total + '本)</div>';
  return '<div class="mkr">'
    + '<span class="mc up ' + st.best.c + '" data-u="' + esc(st.best.u) + '" title="' + esc(st.best.t) + '">↑' + esc(st.best.v) + '</span>'
    + '／'
    + '<span class="mc down ' + st.worst.c + '" data-u="' + esc(st.worst.u) + '" title="' + esc(st.worst.t) + '">↓' + esc(st.worst.v) + '</span>'
    + '</div>';
}
// クリック計測(テクノエッジ版のみ、GA4イベント)。リンク遷移は妨げない
function track(e) {
  if (typeof gtag !== 'function') return;
  var card = e.target.closest('.card');
  if (!card) return;
  var type = card.classList.contains('pcard') ? 'prepaid'
    : e.target.closest('.amz') ? 'amazon'
    : e.target.closest('.stm') ? 'steam'
    : e.target.closest('.psn') ? 'ps_store'
    : e.target.closest('.gc') ? 'catalog'
    : 'nintendo_store';
  var nEl = card.querySelector('.n');
  var name = card.classList.contains('pcard') ? 'ニンテンドープリペイド' : (nEl ? nEl.textContent : '');
  gtag('event', 'sale_click', {link_type: type, item_name: name.slice(0, 95)});
}
grid.addEventListener('click', function(e) {
  track(e);
  var mkr = e.target.closest('.mkr');
  if (mkr) {  // チップ行全体でカードのリンクを無効化(チップ間の誤タップでストアに飛ぶのを防ぐ)
    e.preventDefault();
    e.stopPropagation();
    var mc = e.target.closest('.mc');
    var u = mc && mc.getAttribute('data-u');
    if (u) window.open(u, '_blank', 'noopener');
    return;
  }
  var kb = e.target.closest('.koty');
  if (kb) {
    e.preventDefault();
    e.stopPropagation();
    window.open('https://koty.wiki/Awarded', '_blank', 'noopener');
    return;
  }
  var b = e.target.closest('.stm, .gc, .psn, .amz');
  if (!b) return;
  var url = b.classList.contains('stm')
    ? 'https://store.steampowered.com/app/' + b.getAttribute('data-app') + '/'
    : b.classList.contains('psn')
    ? (b.getAttribute('data-pid') ? 'https://store.playstation.com/ja-jp/product/' + b.getAttribute('data-pid') : null)
    : b.classList.contains('amz')
    ? (b.getAttribute('data-az')
       ? 'https://www.amazon.co.jp/dp/' + b.getAttribute('data-az') + '?tag=' + AFF_TAG
       : 'https://www.amazon.co.jp/s?k=' + encodeURIComponent(b.getAttribute('data-q') + ' switch') + '&tag=' + AFF_TAG)
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
%RESIZER%
</body>
</html>
"""

AFF_NOTICE_HTML = ('<div id="affnotice">【PR】本ページ内のAmazonリンクは'
                   'アフィリエイト広告です。購入により当サイト運営者に紹介料が支払われる場合があります。'
                   'Amazonボタンは国内パッケージ版を優先し、ダウンロードコード版は「DLコード」、'
                   '輸入版(海外パッケージ)は「輸入版」とボタン内に明記しています。'
                   '輸入版は言語・パッケージ仕様が国内版と異なる場合があります。</div>')

# ランキング内に挿入するプリペイドカード枠(テクノエッジ版のみ、通常カードと別デザイン+PR明記)
PREPAID_ASIN = "B09998HHSG"  # ニンテンドープリペイド番号 5000円 オンラインコード版
# Amazon商品ページの画像(暫定の直リンク。PA-API導入後は公式返却URLに差し替える)
PREPAID_IMG = "https://m.media-amazon.com/images/I/51Xu2iDZYSL._AC_SX300_.jpg"

# テクノエッジのGA4(gtag)。IDはtechno-edge.netのGTMコンテナ(GTM-MWBD5H2)内の公開値
TE_GA_ID = "G-33PLFDWM88"
TE_GA_HTML = (f'<script async src="https://www.googletagmanager.com/gtag/js?id={TE_GA_ID}"></script>\n'
              '<script>window.dataLayer=window.dataLayer||[];function gtag(){dataLayer.push(arguments);}'
              f"gtag('js',new Date());gtag('config','{TE_GA_ID}');</script>\n")

# テクノエッジのトンマナ (techno-edge.net から採取: ブランド青#0019FF, 本文#1F2346, 游ゴシック, 角丸4px, 常時ライト)
TE_THEME_CSS = """<style>
:root { --bg:#f4f5f9; --card:#fff; --text:#1f2346; --sub:#6b7089; --accent:#e60012; --line:#e2e4ef; }
body { font-family:YuGothic,"Yu Gothic Medium","Yu Gothic","Hiragino Sans",メイリオ,Meiryo,sans-serif; background:var(--bg); color:var(--text); }
header { background:#0019ff; border-bottom:none; }
header h1 { color:#fff; }
header h1 small { color:rgba(255,255,255,.75); }
.chk { color:rgba(255,255,255,.92); }
select, input[type=search] { border-radius:4px; background:#fff; color:#1f2346; border-color:#fff; }
#ftoggle { border-radius:4px; background:transparent; color:#fff; border-color:rgba(255,255,255,.65); }
#ftoggle.on { border-color:#fff; color:#fff; font-weight:700; }
.card { border-radius:4px; }
.card img { border-radius:0; }
.stm.g { color:#2e7d32; } .stm.y { color:#b26a00; } .stm.r { color:#c62828; }
.gc.good { color:#2e7d32; } .gc.mid { color:#b26a00; } .gc.bad { color:#c62828; } .gc.na { color:#6b7089; }
.low.tie { color:#2e7d32; }
.psn { color:#0057b8; }
.mc.steam { color:#009E73; } .mc.psn { color:#0072B2; } .mc.gc { color:#B87A00; } .mc.koty { color:#D55E00; }
.amz { color:#0019ff; border-color:#0019ff; border-radius:4px; }
.amz:hover { background:rgba(0,25,255,.06); }
.amz.amz-low { color:#c62828; border-color:#c62828; font-weight:700; }
.amz .lowtag { display:block; font-size:9px; font-weight:400; margin-top:1px; }
.pcard { background:#0019ff; border-color:#0019ff; color:#fff; align-items:center; justify-content:center; text-align:center; padding:16px 10px; gap:6px; position:relative; }
.pcard:hover { opacity:.92; }
.pcard .pc-icon { font-size:38px; line-height:1; }
.pcard .pc-img { width:92%; max-width:230px; border-radius:4px; aspect-ratio:auto; }
.pcard .pc-t { font-weight:700; font-size:13px; }
.pcard .pc-s { font-size:11px; opacity:.85; line-height:1.5; }
.pcard .pc-btn { margin-top:4px; background:#fff; color:#0019ff; border-radius:4px; padding:6px 14px; font-size:12px; font-weight:700; }
.pcard .pc-pr { position:absolute; top:6px; right:6px; font-size:9px; border:1px solid rgba(255,255,255,.7); border-radius:3px; padding:0 3px; }
</style>"""

RESIZER_HTML = """<script>
// iframe埋め込み時の高さ調整(テクノエッジ埋め込み用)
// 同一ドメイン設置なら window.frameElement で自分のiframeを直接リサイズできる
// (親ページへのスクリプト追加が不要)。別ドメインの場合はpostMessageで通知する。
(function() {
  if (window.parent === window) return;
  var last = 0;
  function post() {
    var h = document.body.scrollHeight;
    if (Math.abs(h - last) > 4) {
      last = h;
      try {
        if (window.frameElement) {
          window.frameElement.style.height = h + 'px';
          return;
        }
      } catch (e) {}
      window.parent.postMessage({type: 'nss-resize', height: h}, '*');
    }
  }
  new ResizeObserver(post).observe(document.body);
  window.addEventListener('load', post);
  setInterval(post, 1500);
})();
</script>"""


def build_html(items, te_mode=False, out_path=None):
    out_file = out_path or OUT_HTML
    data = []
    for it in items:
        d = {k: it[k] for k in ("id", "n", "p", "pct", "mx", "mk", "im")}
        if it.get("rp"):
            d["rp"] = it["rp"]
        # Switch 2版: 専用商品(BEE)のほか、「Nintendo Switch 2 Edition」型のアップグレード版はHACコードのため名前でも判定
        if it.get("pc") == "BEE" or "switch 2 edition" in it["n"].lower():
            d["hw"] = 1
        if it.get("sa"):
            d["sa"], d["sp"], d["sn"] = it["sa"], it["sp"], it["sn"]
            if it.get("stp"):
                d["stp"] = it["stp"]
                if it.get("std"):
                    d["std"] = it["std"]
        if SHOW_PRICE_HISTORY and it.get("hm") is not None:
            d["hm"], d["hd"] = it["hm"], it["hd"]
            if it.get("nl"):
                d["nl"] = it["nl"]
        if it.get("gv"):
            d["gv"] = it["gv"]
            if it.get("gu"):
                d["gu"] = it["gu"]
        if it.get("kt"):
            d["kt"] = it["kt"]
        if it.get("pv"):
            d["pv"], d["pn"] = it["pv"], it["pn"]
            if it.get("pid"):
                d["pid"] = it["pid"]
            if it.get("pp") is not None:
                d["pp"], d["pb"] = it["pp"], it.get("pb")
        if it.get("az"):
            d["az"] = it["az"]
            if it.get("azp"):
                d["azp"] = it["azp"]
            if it.get("azt"):
                d["azt"] = it["azt"]
        data.append(d)
    now = time.strftime("%Y-%m-%d %H:%M")
    # te_mode: Amazonアフィリエイトリンク+PR表記+テクノエッジのトンマナ+iframeリサイズを有効化
    aff_tag = os.environ.get("AMAZON_TAG", "technoedge-22") if te_mode else ""
    html = (TEMPLATE
            .replace("%DATA%", json.dumps(data, ensure_ascii=False, separators=(",", ":")))
            .replace("%IMGPREFIX%", IMG_PREFIX)
            .replace("%NOW%", now)
            .replace("%COUNT%", str(len(data)))
            .replace("%AFFTAG%", aff_tag)
            .replace("%PREPAIDASIN%", PREPAID_ASIN)
            .replace("%PREPAIDIMG%", PREPAID_IMG)
            .replace("%TEMETA%", '<meta name="robots" content="noindex">\n' if te_mode else "")
            .replace("%TEGA%", TE_GA_HTML if te_mode else "")
            .replace("%LOWDISP%", "" if SHOW_PRICE_HISTORY else ' style="display:none"')
            .replace("%AFFNOTICE%", AFF_NOTICE_HTML if te_mode else "")
            .replace("%TETHEME%", TE_THEME_CSS if te_mode else "")
            .replace("%RESIZER%", RESIZER_HTML if te_mode else ""))
    tmp = out_file + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(html)
    os.replace(tmp, out_file)
    log.info("written %s (%d items%s)", out_file, len(data), ", techno-edge" if te_mode else "")


def main():
    backfill = "--steam-backfill" in sys.argv
    try:
        items = collect()
        if len(items) < 100:
            raise RuntimeError(f"suspiciously few items: {len(items)} — keeping previous HTML")
        update_price_history(items)
        enrich_regular_prices(items)
        enrich_game_catalog(items)
        enrich_koty(items)
        try:
            enrich_steam(items, backfill=backfill)
        except Exception:
            # Steam側の障害でページ生成自体は止めない(キャッシュ済み分は付与されないだけ)
            log.exception("steam enrich failed; continuing without fresh steam data")
        try:
            enrich_psn(items, backfill=backfill)
        except Exception:
            log.exception("psn enrich failed; continuing without fresh psn data")
        te_flag = "--techno-edge" in sys.argv
        te_out = os.environ.get("NINTENDO_SALE_TE_OUT", "").strip()
        if te_flag or te_out:
            try:
                enrich_amazon(items, backfill=backfill)
            except Exception:
                log.exception("amazon enrich failed; continuing")
        build_html(items, te_mode=te_flag)
        if te_out and not te_flag:
            build_html(items, te_mode=True, out_path=te_out)
    except Exception:
        log.exception("failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
