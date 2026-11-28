#!/usr/bin/env python3
"""Archivio giornaliero dei prezzi Cardmarket (via Scryfall) per l'app.

Ogni giorno:
  1. rilegge lo storico già pubblicato (meta.json + 256 file a/XX.json + 256 file w/XX.json);
  2. scarica il bulk "default_cards" di Scryfall e prende i prezzi EUR e EUR foil di ogni edizione;
  3. aggiunge il giorno, tiene gli ultimi DAYS_KEEP giorni e riscrive i 256 file;
  4. calcola le classifiche di chi sale e chi scende (1, 7, 30 giorni) in movers.json;
  4b. aggiunge dal listino pubblico di Cardmarket (price guide) la media vendite a 7 giorni
      e il prezzo più basso in vendita del giorno;
  4c. aggiunge da CardTrader (API, con il token nel segreto CARDTRADER_TOKEN) il prezzo delle copie
      EX o meglio: media delle 3 offerte in inglese più economiche;
  4d. tiene uno storico settimanale permanente (w/): un valore a settimana per ogni carta, l'ultimo della settimana;
  5. scrive market.json: tutte le carte da almeno 1 € con prezzo e prezzi di 1, 7 e 30 giorni prima,
     per la pagina iniziale del sito.

Uso:
  python build.py --out _site/data --prev https://utente.github.io/repo/data
  python build.py --out out --prev ./vecchi-dati --bulk default-cards.json --today 2026-11-25   (prove in locale)
"""
import argparse, datetime, json, os, re, sys, time, urllib.error, urllib.request

DAYS_KEEP = 120         # archivio completo (a/), letto solo da questo script
CLIENT_DAYS = 35        # quello che scarica l'app (h/): bastano 30 giorni + margine
MIN_CENTS = 100          # classifiche solo per carte da almeno 1 € (prima e dopo)
TOP = 300                # voci per classifica
MARKET_MIN = 100         # elenco "tutte le carte" (market.json): edizioni da almeno 1 €, normale o foil
CM_PREFIX = "https://www.cardmarket.com/en/Magic/Products"
FORMATS = ["commander", "duel", "modern", "legacy", "vintage", "pauper", "pioneer", "standard"]
PRICE_GUIDE = "https://downloads.s3.cardmarket.com/productCatalog/priceGuide/price_guide_{cm}.json"
# Ogni gioco è una scheda: codici su Cardmarket e CardTrader, cosa vuol dire "versione" (le due serie e/f, x/y).
# L'archivio di ogni gioco sta in data/<id>/ e il sito sceglie il gioco con il menu.
GAMES = {
    "mtg": {"name": "Magic: The Gathering", "cm": 1, "ct": 1, "variants": ["Normale", "Foil"], "formats": FORMATS},
    # One Piece: niente foil; la "versione" è la lingua (la tendenza di Cardmarket le mescola, l'EX+ di CardTrader no)
    "op": {"name": "One Piece", "cm": 18, "ct": 15, "variants": ["Inglese", "Giapponese"], "formats": [],
           "cmPath": "OnePiece", "langs": ["en", "jp"], "ctSingles": [192]},
}
GAME = dict(GAMES["mtg"], id="mtg")   # il gioco in lavorazione (lo sceglie --game)
# Serie salvate per ogni edizione: e/f = prezzo di tendenza normale/foil (Scryfall),
# s/t = media delle vendite a 7 giorni normale/foil (listino pubblico di Cardmarket).
SERIES = ("e", "f", "s", "t", "x", "y", "d", "g", "m", "n")
# x/y = prezzo "EX o meglio" su CardTrader, normale/foil: media delle 3 offerte più economiche
# in inglese, condizione Mint, Near Mint o Slightly Played (l'Excellent di Cardmarket).
# d/g = media delle vendite del giorno su Cardmarket (normale/foil): da qui la mediana su 7 giorni,
#       che ignora le giornate con una vendita fuori scala. m/n = l'offerta EX+ più economica su CardTrader (x/y).
SHORT = ("s", "t", "x", "y", "d", "g", "m", "n")   # serie tenute solo CLIENT_DAYS giorni (bastano per 1/7/30 giorni)
WEEKLY = ("e", "f")             # storico settimanale permanente: tendenza normale e foil
CT_API = "https://api.cardtrader.com/api/v2"
CT_MIN = 500             # CardTrader solo per le espansioni con carte da almeno 5 € (tendenza)
CT_GOOD = ("Mint", "Near Mint", "Slightly Played")
CT_BUDGET = 40 * 60      # secondi massimi per CardTrader per gioco: oltre, si tiene quello che c'è
HEAD = {"User-Agent": "CardmarketPriceArchive/1.0 (GitHub Actions; daily, once a day)", "Accept": "application/json"}
SHARDS = [f"{i:02x}" for i in range(256)]


def http(url, dest=None, tries=4):
    for k in range(tries):
        try:
            req = urllib.request.Request(url, headers=HEAD)
            with urllib.request.urlopen(req, timeout=300) as r:
                if dest:
                    with open(dest, "wb") as f:
                        while True:
                            b = r.read(1 << 20)
                            if not b:
                                break
                            f.write(b)
                    return dest
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise
            err = e
        except Exception as e:  # rete instabile: riprova
            err = e
        time.sleep(5 * (k + 1))
    raise err


UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def shard_of(cid):
    """File XX.json della carta: per Magic le prime due cifre dell'id Scryfall, per gli altri giochi
    il numero finale dell'id modulo 256 (lo stesso calcolo lo fa il sito)."""
    if UUID_RE.match(cid):
        return cid[:2]
    m = re.search(r"(\d+)$", cid)
    return f"{int(m.group(1)) % 256:02x}" if m else "00"


def dec_right(s, n):
    """Serie compatta (SHORT): contiene solo gli ultimi giorni, allineati alla fine."""
    if not s:
        return [None] * n
    out = [int(x) if x else None for x in s.split(",")][-n:]
    return [None] * (n - len(out)) + out


def read_series(rec, k, n):
    return dec_right(rec.get(k, ""), n) if k in SHORT else dec(rec.get(k, ""), n)


def week_of(d):
    """Il lunedì della settimana del giorno d (YYYY-MM-DD)."""
    x = datetime.date.fromisoformat(d)
    return (x - datetime.timedelta(days=x.weekday())).isoformat()


def read_prev(prev):
    """Restituisce (days, {id: {serie: lista}}, weeks, {id: {serie: lista settimanale}}) dallo storico pubblicato."""
    if not prev:
        return [], {}, [], {}
    remote = prev.startswith("http")

    def load(name):
        if remote:
            return json.loads(http(prev.rstrip("/") + "/" + name))
        with open(os.path.join(prev, name), encoding="utf-8") as f:
            return json.load(f)

    try:
        meta = load("meta.json")
    except (urllib.error.HTTPError, FileNotFoundError) as e:
        if isinstance(e, FileNotFoundError) or e.code == 404:
            print("Nessuno storico precedente: si parte da oggi.")
            return [], {}, [], {}
        raise
    # "archiveDayList" = tutti i giorni dell'archivio completo; "days" sono solo quelli per l'app (35)
    days = meta.get("archiveDayList") or meta["days"]
    hist = {}
    for sh in SHARDS:  # un errore qui ferma tutto: meglio non pubblicare che perdere lo storico
        try:
            data = load(f"a/{sh}.json")
        except (urllib.error.HTTPError, FileNotFoundError):
            data = load(f"h/{sh}.json")
        for cid, v in data.items():
            hist[cid] = {k: read_series(v, k, len(days)) for k in SERIES}
    weeks, wk = meta.get("weeks") or [], {}
    if weeks:
        for sh in SHARDS:
            for cid, v in load(f"w/{sh}.json").items():
                wk[cid] = {k: dec(v.get(k, ""), len(weeks)) for k in WEEKLY}
    print(f"Storico precedente: {len(days)} giorni, {len(hist)} carte, {len(weeks)} settimane.")
    return days, hist, weeks, wk


def dec(s, n):
    if not s:
        return [None] * n
    out = [int(x) if x else None for x in s.split(",")]
    return (out + [None] * n)[:n]


def enc(vals):
    return ",".join("" if v is None else str(v) for v in vals)


def pos(p):
    """Come cents, ma 0 (giorno senza vendite) diventa None."""
    v = cents(p)
    return v if v else None


def median_last(vals, k=7, lookback=14, need=3):
    """Mediana degli ultimi k valori presenti negli ultimi `lookback` giorni (None se ce ne sono meno di `need`)."""
    v = [x for x in vals[-lookback:] if x is not None][-k:]
    if len(v) < need:
        return None
    v = sorted(v)
    n = len(v)
    return v[n // 2] if n % 2 else round((v[n // 2 - 1] + v[n // 2]) / 2)


def cents(p):
    if p is None:
        return None
    try:
        return int(round(float(p) * 100))
    except ValueError:
        return None


def iter_cards(path):
    """Legge il bulk sia nel formato nuovo (.jsonl.gz, una carta per riga) sia in quello vecchio (.json)."""
    if path.endswith(".gz"):
        import gzip
        with gzip.open(path, "rt", encoding="utf-8") as f:
            for line in f:
                line = line.strip().rstrip(",")
                if line and line not in ("[", "]"):
                    yield json.loads(line)
    elif path.endswith(".jsonl"):
        with open(path, encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    yield json.loads(line)
    else:
        import ijson
        with open(path, "rb") as f:
            yield from ijson.items(f, "item")


def read_bulk(path):
    cur, info = {}, {}
    if True:
        for c in iter_cards(path):
            if "paper" not in (c.get("games") or []):
                continue
            pr = c.get("prices") or {}
            e, fo = cents(pr.get("eur")), cents(pr.get("eur_foil"))
            if e is None and fo is None:
                continue
            cid = c["id"]
            cur[cid] = {"e": e, "f": fo, "cm": c.get("cardmarket_id")}
            lg = c.get("legalities") or {}
            mask = sum(1 << i for i, fm in enumerate(FORMATS) if lg.get(fm) == "legal")
            cm = (c.get("purchase_uris") or {}).get("cardmarket", "")
            if "idProduct=" in cm:
                cm = cm.split("?")[0] + "?idProduct=" + cm.split("idProduct=")[1].split("&")[0]
            elif "?" in cm:
                cm = cm.split("?")[0]
            info[cid] = [c.get("name", ""), (c.get("set") or "").upper(), c.get("set_name", ""),
                         c.get("collector_number", ""), mask, cm]
    print(f"Bulk letto: {len(cur)} edizioni con prezzo EUR.")
    return cur, info


def read_price_guide():
    """Media vendite 7 giorni per idProduct, in centesimi. Se il listino non si scarica, si va avanti senza."""
    try:
        pg = json.loads(http(PRICE_GUIDE.format(cm=GAME["cm"])))
        out = {it["idProduct"]: (cents(it.get("avg7")), cents(it.get("avg7-foil")), cents(it.get("low")), cents(it.get("low-foil")),
                                  pos(it.get("avg1")), pos(it.get("avg1-foil")))
               for it in pg.get("priceGuides", []) if it.get("idProduct")}
        print(f"Listino Cardmarket del {pg.get('createdAt')}: {len(out)} prodotti.")
        return out
    except Exception as e:  # il listino è un extra: senza, l'archivio resta valido
        print("Listino Cardmarket non disponibile oggi:", e)
        return {}


def ct_get(path, token, tries=5):
    """Una chiamata all'API di CardTrader, con le attese che l'API chiede (429 e 503 con Retry-After)."""
    for k in range(tries):
        req = urllib.request.Request(CT_API + path, headers={
            "Authorization": "Bearer " + token, "Accept": "application/json",
            "User-Agent": "mtg-price-tracker (archivio giornaliero, una volta al giorno)"})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code in (429, 503):
                time.sleep(min(60, float(e.headers.get("Retry-After") or 2 * (k + 1))))
                continue
            raise
        except Exception:
            time.sleep(3 * (k + 1))
    raise RuntimeError("CardTrader non risponde: " + path)


def read_cardtrader(token, cards):
    """cards: {scryfall_id: (set, cardmarket_id, tendenza normale, tendenza foil)}.
    Restituisce {scryfall_id: {"x", "y", "xn", "yn", "ct"}} con i prezzi in centesimi."""
    if not token:
        print("CardTrader: nessun token (segreto CARDTRADER_TOKEN), salto.")
        return {}
    t0 = time.time()
    try:
        exps = ct_get("/expansions", token)
    except Exception as e:
        print("CardTrader non disponibile oggi:", e)
        return {}
    # Tutte le espansioni Magic: su CardTrader le versioni speciali (borderless, showcase...) stanno spesso
    # in espansioni a parte con un codice diverso da Scryfall. Prima quelle con lo stesso codice delle carte care.
    wanted = {st.lower() for st, _, n, f in cards.values() if max(n or 0, f or 0) >= CT_MIN}
    exps = sorted((e for e in exps if e.get("game_id") == GAME["ct"]), key=lambda e: (e.get("code") or "").lower() not in wanted)
    cm2sid = {}
    for sid, (_, cmid, _, _) in cards.items():
        if cmid:
            cm2sid.setdefault(cmid, sid)
    out, skipped, done, via_cm = {}, 0, 0, 0
    for e in exps:
        if time.time() - t0 > CT_BUDGET:
            print(f"CardTrader: tempo finito dopo {done} espansioni su {len(exps)}.")
            break
        try:
            bps = ct_get(f"/blueprints/export?expansion_id={e['id']}", token)
            prods = ct_get(f"/marketplace/products?expansion_id={e['id']}&language=en", token)
        except Exception as err:
            print("CardTrader, espansione saltata:", e.get("code"), err)
            continue
        done += 1
        bp = {b["id"]: b for b in bps}
        for bid, lst in prods.items():
            b = bp.get(int(bid))
            if not b:
                continue
            cmids = b.get("card_market_ids") or []
            sid = b.get("scryfall_id")
            if sid not in cards:   # molte versioni speciali hanno solo il codice Cardmarket: si collega con quello
                sid = next((cm2sid[c] for c in cmids if c in cm2sid), None)
                if not sid:
                    continue
                via_cm += 1
            st, cmid, tn, tf = cards[sid]
            if cmid and cmids and cmid not in cmids:   # carta diversa con lo stesso id Scryfall: meglio niente
                skipped += 1
                continue
            if sid in out:   # due versioni di CardTrader sulla stessa carta: non si sa quale sia, meglio niente
                out.pop(sid)
                skipped += 1
                continue
            rec = {"ct": b["id"]}
            for key, foil, trend in (("x", False, tn), ("y", True, tf)):
                ok = sorted((p.get("price") or {}).get("cents") for p in lst
                            if (p.get("properties_hash") or {}).get("condition") in CT_GOOD
                            and bool((p.get("properties_hash") or {}).get("mtg_foil")) == foil
                            and (p.get("properties_hash") or {}).get("mtg_language", "en") == "en"
                            and not (p.get("properties_hash") or {}).get("signed")
                            and not (p.get("properties_hash") or {}).get("altered")
                            and not p.get("graded") and (p.get("bundle_size") or 1) == 1
                            and (p.get("price") or {}).get("currency") == "EUR"
                            and (p.get("price") or {}).get("cents"))
                if not ok:
                    continue
                v = round(sum(ok[:3]) / len(ok[:3]))
                if trend and trend >= 100 and not (0.25 <= v / trend <= 15):   # quasi certamente un'altra versione
                    skipped += 1
                    continue
                rec[key], rec[key + "n"], rec[key + "m"] = v, len(ok), ok[0]
            if "x" in rec or "y" in rec:
                out[sid] = rec
    print(f"CardTrader: {done} espansioni in {time.time() - t0:.0f} s, prezzo EX+ per {len(out)} edizioni ({via_cm} collegate col codice Cardmarket), {skipped} scartate.")
    return out


CT_IMG = "https://cardtrader.com/uploads/blueprints/image/"
CATALOG, CATALOG_SETS = [], {}   # tutte le carte dei giochi senza Scryfall (per cercare e importare dal sito)


def ct_ex(lst, langkey, lang, foil=None):
    """Media delle 3 offerte più economiche in condizione Mint/Near Mint/Slightly Played (centesimi) e quante sono."""
    ok = sorted((p.get("price") or {}).get("cents") for p in lst
                if (p.get("properties_hash") or {}).get("condition") in CT_GOOD
                and (p.get("properties_hash") or {}).get(langkey, lang) == lang
                and (foil is None or bool((p.get("properties_hash") or {}).get("mtg_foil")) == foil)
                and not (p.get("properties_hash") or {}).get("signed")
                and not (p.get("properties_hash") or {}).get("altered")
                and not p.get("graded") and (p.get("bundle_size") or 1) == 1
                and (p.get("price") or {}).get("currency") == "EUR"
                and (p.get("price") or {}).get("cents"))
    if not ok:
        return None, 0, None
    return round(sum(ok[:3]) / len(ok[:3])), len(ok), ok[0]


def read_generic(token, guide):
    """Giochi senza Scryfall (One Piece...): le carte vengono da CardTrader, i prezzi di Cardmarket dal listino.
    guide: {idProduct: (tendenza, media 7 giorni, minimo)} in centesimi. Restituisce (cur, info) come read_bulk."""
    if not token:
        sys.exit("Serve il token di CardTrader (segreto CARDTRADER_TOKEN) per " + GAME["name"] + ".")
    t0 = time.time()
    exps = [e for e in ct_get("/expansions", token) if e.get("game_id") == GAME["ct"]]
    cur, info, done = {}, {}, 0
    CATALOG.clear()
    l1, l2 = GAME["langs"]
    langkey = {15: "onepiece_language"}.get(GAME["ct"], "language")
    for e in exps:
        if time.time() - t0 > CT_BUDGET:
            print(f"CardTrader: tempo finito dopo {done} espansioni su {len(exps)}.")
            break
        try:
            bps = ct_get(f"/blueprints/export?expansion_id={e['id']}", token)
            prods = ct_get(f"/marketplace/products?expansion_id={e['id']}", token)
        except Exception as err:
            print("CardTrader, espansione saltata:", e.get("code"), err)
            continue
        done += 1
        for b in bps:
            if b.get("category_id") not in GAME["ctSingles"]:
                continue
            props = b.get("fixed_properties") or b.get("properties_hash") or {}
            ver = (b.get("version") or "").strip()
            img = b.get("image_url") or ""
            # catalogo completo (anche carte senza prezzo): serve al sito per cercare e importare liste
            CATALOG.append([f"{GAME['id']}-{b['id']}", b.get("name", "") + (f" ({ver})" if ver else ""), (e.get("code") or "").upper(),
                            props.get("collector_number") or "", img[len(CT_IMG):] if img.startswith(CT_IMG) else img])
            CATALOG_SETS[(e.get("code") or "").upper()] = e.get("name", "")
            lst = prods.get(str(b["id"])) or []
            cmid = next((c for c in (b.get("card_market_ids") or []) if c in guide), None)
            tr, a7, lo, a1 = guide.get(cmid, (None,) * 4)
            x, xn, xm = ct_ex(lst, langkey, l1)
            y, yn, ym = ct_ex(lst, langkey, l2)
            if tr is None and x is None and y is None:
                continue
            cid = f"{GAME['id']}-{b['id']}"
            cur[cid] = {"e": tr, "f": tr, "s": a7, "t": a7, "x": x, "y": y, "d": a1, "g": a1, "m": xm, "n": ym, "ctn": [xn or None, yn or None, b["id"]]}
            if lo is not None:
                cur[cid]["lo"] = [lo, lo]
            info[cid] = [b.get("name", "") + (f" ({ver})" if ver else ""), (e.get("code") or "").upper(), e.get("name", ""),
                         props.get("collector_number") or "", 0,
                         f"https://www.cardmarket.com/en/{GAME['cmPath']}/Products?idProduct={cmid}" if cmid else "",
                         img[len(CT_IMG):] if img.startswith(CT_IMG) else img]
    print(f"{GAME['name']}: {done} espansioni in {time.time() - t0:.0f} s, {len(cur)} carte, "
          f"tendenza Cardmarket per {sum(1 for c in cur.values() if c['e'] is not None)}, "
          f"EX+ {l1} per {sum(1 for c in cur.values() if c['x'] is not None)}, EX+ {l2} per {sum(1 for c in cur.values() if c['y'] is not None)}.")
    return cur, info


def restore(prev_game, out):
    """Ripubblica i dati di ieri di un gioco (quando l'aggiornamento di oggi non riesce)."""
    meta = json.loads(http(prev_game + "/meta.json"))
    names = ["meta.json", "market.json", "movers.json", "catalog.json", "names.json"] + [f"{d}/{sh}.json" for d in ("a", "h") for sh in SHARDS]
    if meta.get("weeks"):
        names += [f"w/{sh}.json" for sh in SHARDS]
    for n in names:
        try:
            data = http(prev_game + "/" + n)
        except urllib.error.HTTPError:
            continue
        os.makedirs(os.path.dirname(os.path.join(out, n)) or out, exist_ok=True)
        with open(os.path.join(out, n), "wb") as f:
            f.write(data)
    print(f"Ripubblicati i dati di ieri ({meta.get('updated')}) da {prev_game}.")


def latest_bulk():
    resp = json.loads(http("https://api.scryfall.com/bulk-data"))
    items = resp.get("data", []) if isinstance(resp, dict) else resp
    item = next((i for i in items if isinstance(i, dict) and i.get("type") == "default_cards"), None)
    url = item and (item.get("jsonl_download_uri") or item.get("download_uri"))
    if not url:
        print("Risposta di Scryfall inattesa:", json.dumps(resp)[:1500])
        sys.exit("Non trovo il file default_cards nell'elenco di Scryfall.")
    dest = "default-cards" + (".jsonl.gz" if url.endswith(".jsonl.gz") else ".jsonl" if url.endswith(".jsonl") else ".json")
    print("Scarico", url, "aggiornato", item.get("updated_at"), flush=True)
    return http(url, dest=dest)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--prev", default="")
    ap.add_argument("--bulk", default="")
    ap.add_argument("--today", default="")
    ap.add_argument("--guide", default="", help="listino Cardmarket locale (prove)")
    ap.add_argument("--game", default="mtg", choices=sorted(GAMES))
    ap.add_argument("--restore", action="store_true", help="ripubblica i dati di ieri del gioco e basta")
    a = ap.parse_args()
    today = a.today or datetime.datetime.now(datetime.timezone.utc).date().isoformat()
    global GAME
    GAME = dict(GAMES[a.game], id=a.game)
    a.out = os.path.join(a.out, a.game)          # data/<gioco>/
    os.makedirs(a.out, exist_ok=True)
    prev_game = a.prev.rstrip("/") + "/" + a.game if a.prev else ""
    if a.restore:
        restore(prev_game, a.out)
        return
    old_days, hist, old_weeks, wk = read_prev(prev_game)
    if not old_days and a.prev and a.game == "mtg":
        # prima volta con l'archivio diviso per gioco: lo storico di Magic stava direttamente in data/
        print("Storico non trovato in data/mtg: lo prendo da data/ (posizione vecchia).")
        old_days, hist, old_weeks, wk = read_prev(a.prev)
    if a.game != "mtg":
        if a.guide:
            with open(a.guide, encoding="utf-8") as f:
                items = json.load(f)["priceGuides"]
        else:
            items = json.loads(http(PRICE_GUIDE.format(cm=GAME["cm"])))["priceGuides"]
        guide = {it["idProduct"]: (cents(it.get("trend")), cents(it.get("avg7")), cents(it.get("low")), pos(it.get("avg1"))) for it in items}
        print(f"Listino Cardmarket {GAME['name']}: {len(guide)} prodotti.")
        cur, info = read_generic(os.environ.get("CARDTRADER_TOKEN", "").strip(), guide)
        if len(cur) < 100:
            sys.exit("Troppo poche carte: qualcosa non va, non pubblico.")
    else:
        cur, info = read_mtg(a, cur_only=False)
    build_archive(a, today, old_days, hist, old_weeks, wk, cur, info)


def read_mtg(a, cur_only=False):
    cur, info = read_bulk(a.bulk or latest_bulk())
    if len(cur) < 1000 and not a.bulk:
        sys.exit("Troppo pochi prezzi nel bulk: qualcosa non va, non pubblico.")
    pg = {} if a.bulk else read_price_guide()
    if a.guide:
        with open(a.guide, encoding="utf-8") as f:
            pg = {it["idProduct"]: (cents(it.get("avg7")), cents(it.get("avg7-foil")), cents(it.get("low")), cents(it.get("low-foil")),
                                  pos(it.get("avg1")), pos(it.get("avg1-foil"))) for it in json.load(f)["priceGuides"]}
    ctcards = {cid: (info[cid][1], c.get("cm"), c.get("e"), c.get("f")) for cid, c in cur.items() if cid in info}
    ct = read_cardtrader(os.environ.get("CARDTRADER_TOKEN", "").strip(), ctcards)
    for cid, r in ct.items():
        cur[cid]["x"], cur[cid]["y"], cur[cid]["m"], cur[cid]["n"] = r.get("x"), r.get("y"), r.get("xm"), r.get("ym")
        cur[cid]["ctn"] = [r.get("xn"), r.get("yn"), r["ct"]]
    linked = 0
    for cid, c in cur.items():
        s7, t7, lo, lf, d1, g1 = pg.get(c.pop("cm", None), (None,) * 6)
        c["s"], c["t"], c["d"], c["g"] = s7, t7, d1, g1
        if lo is not None or lf is not None:
            c["lo"] = [lo, lf]   # prezzo più basso in vendita oggi (normale, foil): solo il valore del giorno
        linked += s7 is not None or t7 is not None
    print(f"Media vendite 7 giorni collegata a {linked} edizioni.")
    return cur, info


NO_DECK = re.compile(r"^(WC\d\d|CED|CEI|30A|PTC|OLEP|PSSC)$")   # carte non da torneo (bordo oro, Collectors' Edition...)


def write_names(out, today, cur, info):
    """names.json: per ogni nome di carta la versione più economica, per il costo di un mazzo.
    Riga: [nome, tendenza più bassa, id, set, foil, copia più economica su Cardmarket, EX+ CardTrader più economico, id CardTrader]."""
    best = {}
    for cid, c in cur.items():
        inf = info.get(cid)
        if not inf or NO_DECK.match(inf[1]) or re.search(r"Art Series|Oversized|World Championship|Token|Minigame", inf[2]):
            continue
        nm = inf[0]
        b = best.setdefault(nm, [nm, None, None, None, 0, None, None, None])
        for fo, k in ((0, "e"), (1, "f")):
            v = c.get(k)
            if v and (b[1] is None or v < b[1]):
                b[1], b[2], b[3], b[4] = v, cid, inf[1], fo
        for v in c.get("lo") or []:
            if v and (b[5] is None or v < b[5]):
                b[5] = v
        ctn = c.get("ctn") or [None, None, None]
        for k1, k2 in (("m", "x"), ("n", "y")):
            v = c.get(k1) or c.get(k2)
            if v and (b[6] is None or v < b[6]):
                b[6], b[7] = v, ctn[2]
    rows = sorted((r for r in best.values() if r[1] is not None or r[6] is not None), key=lambda r: r[0])
    with open(os.path.join(out, "names.json"), "w", encoding="utf-8") as f:
        json.dump({"updated": today, "fields": ["name", "trend", "id", "set", "foil", "cmLow", "exCT", "ctBlueprint"], "cards": rows},
                  f, separators=(",", ":"), ensure_ascii=False)
    print(f"names.json: {len(rows)} nomi di carte.")


def build_archive(a, today, old_days, hist, old_weeks, wk, cur, info):

    days = [d for d in old_days if d != today and d < today] + [today]
    days = days[-DAYS_KEEP:]
    idx_old = {d: i for i, d in enumerate(old_days)}

    def base_index(w):
        if len(days) < 2:
            return None
        if w == 1:
            return len(days) - 2
        lim = (datetime.date.fromisoformat(today) - datetime.timedelta(days=w)).isoformat()
        b = None
        for i, d in enumerate(days[:-1]):
            if d <= lim:
                b = i
        return b

    bases = {w: base_index(w) for w in (1, 7, 30)}
    movers = {f"{w}_{fin}_{dr}": [] for w in (1, 7, 30) for fin in ("n", "f") for dr in ("up", "down")}

    os.makedirs(os.path.join(a.out, "h"), exist_ok=True)
    os.makedirs(os.path.join(a.out, "a"), exist_ok=True)
    os.makedirs(os.path.join(a.out, "w"), exist_ok=True)
    cdays = days[-CLIENT_DAYS:]
    # settimane: quelle già nello storico settimanale + quelle coperte dai giorni dell'archivio
    day_week = [week_of(d) for d in days]
    weeks = sorted(set(old_weeks) | set(day_week))
    widx_old = {w: i for i, w in enumerate(old_weeks)}
    weekly = {sh: {} for sh in SHARDS}

    def week_series(cid, ser):
        """Ultimo valore di ogni settimana dai giorni; per le settimane più vecchie resta quello già salvato."""
        old, out = wk.get(cid, {}), {}
        for k in WEEKLY:
            last = {}
            for d, v in zip(day_week, ser.get(k) or []):
                if v is not None:
                    last[d] = v
            o = old.get(k, [])
            arr = []
            for w in weeks:
                v = last.get(w)
                if v is None:
                    i = widx_old.get(w)
                    v = o[i] if i is not None and i < len(o) else None
                arr.append(v)
            if any(v is not None for v in arr):
                out[k] = enc(arr)
        if out:
            weekly[shard_of(cid)][cid] = out
    cut = len(days) - len(cdays)
    shard_data = {sh: {} for sh in SHARDS}
    market, set_names = [], {}
    ids = set(hist) | set(cur)
    for cid in ids:
        old, now, ser = hist.get(cid, {}), cur.get(cid, {}), {}
        for k in SERIES:
            o = old.get(k, [])
            arr = [o[i] if i is not None and i < len(o) else None for i in (idx_old.get(d) for d in days[:-1])]
            arr.append(now.get(k))
            ser[k] = arr
        ne, nf = ser["e"], ser["f"]
        week_series(cid, ser)
        if all(v is None for v in ne) and all(v is None for v in nf):
            continue
        rec = {"e": enc(ne)}
        for k in SERIES[1:]:
            v = ser[k][-CLIENT_DAYS:] if k in SHORT else ser[k]   # le serie SHORT si salvano compatte
            if any(x is not None for x in v):
                rec[k] = enc(v)
        if now.get("lo"):
            rec["lo"] = now["lo"]
        if now.get("ctn"):
            rec["ctn"] = now["ctn"]
        shard_data[shard_of(cid)][cid] = rec
        if cid in info and max(ne[-1] or 0, nf[-1] or 0) >= MARKET_MIN:
            nm, st, sn, cn, mask, cm = info[cid][:6]
            set_names[st] = sn
            row = [cid, nm, st, cn, mask, cm[len(CM_PREFIX):] if cm.startswith(CM_PREFIX) else cm]
            for arr in (ne, nf, ser["s"], ser["t"], ser["x"], ser["y"]):
                row.append(arr[-1])
                row.extend(arr[bases[w]] if bases[w] is not None else None for w in (1, 7, 30))
            row.extend(now.get("ctn") or [None, None, None])
            row.append(info[cid][6] if len(info[cid]) > 6 else None)   # immagine (giochi senza Scryfall)
            row.extend(now.get("lo") or [None, None])   # copia più economica su Cardmarket oggi (normale, foil)
            # mediana a 7 giorni della media giornaliera di Cardmarket (normale, foil)
            row.extend(median_last(ser[k]) for k in ("d", "g"))
            # offerta EX+ più economica oggi su CardTrader, e il livello solito dei giorni prima
            # (mediana delle offerte più economiche degli ultimi giorni, ne basta uno): se oggi è molto più bassa,
            # qualcuno ha appena messo in vendita una copia a poco
            row.extend(ser[k][-1] for k in ("m", "n"))
            row.extend(median_last(ser[k][:-1], need=1) for k in ("m", "n"))
            market.append(row)
        if cid in info:
            for fin, arr in (("n", ne), ("f", nf)):
                last = arr[-1]
                if last is None or last < MIN_CENTS:
                    continue
                for w, bi in bases.items():
                    if bi is None:
                        continue
                    b = arr[bi]
                    if b is None or b < MIN_CENTS or b == last:
                        continue
                    pct = (last - b) / b * 100
                    movers[f"{w}_{fin}_{'up' if pct > 0 else 'down'}"].append((pct, cid, last, b))

    for cid in set(wk) - ids:   # carte sparite dai prezzi di oggi: lo storico settimanale resta
        week_series(cid, {})
    n_week = 0
    for sh, data in weekly.items():
        n_week += len(data)
        with open(os.path.join(a.out, "w", f"{sh}.json"), "w", encoding="utf-8") as f:
            json.dump(data, f, separators=(",", ":"), ensure_ascii=False)
    print(f"Storico settimanale: {len(weeks)} settimane, {n_week} carte.")

    for sh, data in shard_data.items():
        with open(os.path.join(a.out, "a", f"{sh}.json"), "w", encoding="utf-8") as f:
            json.dump(data, f, separators=(",", ":"), ensure_ascii=False)
        short = {}
        for cid, rec in data.items():
            sr = {}
            for k in SERIES:
                v = read_series(rec, k, len(days))[cut:]
                if any(x is not None for x in v):
                    sr[k] = enc(v)
            if "lo" in rec:
                sr["lo"] = rec["lo"]
            if "ctn" in rec:
                sr["ctn"] = rec["ctn"]
            if "e" in sr or "f" in sr:
                sr.setdefault("e", "")
                short[cid] = sr
        with open(os.path.join(a.out, "h", f"{sh}.json"), "w", encoding="utf-8") as f:
            json.dump(short, f, separators=(",", ":"), ensure_ascii=False)

    out_m = {}
    for k, lst in movers.items():
        lst.sort(key=lambda x: -abs(x[0]))
        out_m[k] = [[cid, *info[cid][:4], last, b, info[cid][4], info[cid][5]] for _, cid, last, b in lst[:TOP]]
        # stessa classifica ordinata per variazione in euro (una carta da 1000 € che sale del 5% conta piu' di una da 5 centesimi che triplica)
        by_e = sorted(lst, key=lambda x: -abs(x[2] - x[3]))
        out_m[k + "_e"] = [[cid, *info[cid][:4], last, b, info[cid][4], info[cid][5]] for _, cid, last, b in by_e[:TOP]]
    with open(os.path.join(a.out, "movers.json"), "w", encoding="utf-8") as f:
        json.dump({"updated": today, "formats": GAME["formats"], "fields": ["id", "name", "set", "setName", "cn", "price", "base", "legal", "cm"], "lists": out_m},
                  f, separators=(",", ":"), ensure_ascii=False)
    market.sort(key=lambda r: -max(r[6] or 0, r[10] or 0))
    with open(os.path.join(a.out, "market.json"), "w", encoding="utf-8") as f:
        json.dump({"updated": today, "game": GAME, "formats": GAME["formats"], "cmPrefix": CM_PREFIX, "sets": set_names,
                   "fields": ["id", "name", "set", "cn", "legal", "cm", "price", "base1", "base7", "base30",
                              "foil", "foilBase1", "foilBase7", "foilBase30",
                              "sales7", "sales7Base1", "sales7Base7", "sales7Base30",
                              "foilSales7", "foilSales7Base1", "foilSales7Base7", "foilSales7Base30",
                              "exCT", "exCTBase1", "exCTBase7", "exCTBase30",
                              "foilExCT", "foilExCTBase1", "foilExCTBase7", "foilExCTBase30",
                              "exCTOffers", "foilExCTOffers", "ctBlueprint", "image", "cmLow", "foilCmLow",
                              "sales1Med7", "foilSales1Med7", "exCTMin", "foilExCTMin", "exCTUsual", "foilExCTUsual"], "imgPrefix": CT_IMG, "cards": market},
                  f, separators=(",", ":"), ensure_ascii=False)
    print(f"market.json: {len(market)} carte da almeno {MARKET_MIN / 100:.0f} €")
    if CATALOG:
        with open(os.path.join(a.out, "catalog.json"), "w", encoding="utf-8") as f:
            json.dump({"updated": today, "game": GAME["id"], "imgPrefix": CT_IMG, "sets": CATALOG_SETS,
                       "fields": ["id", "name", "set", "cn", "image"], "cards": CATALOG},
                      f, separators=(",", ":"), ensure_ascii=False)
        print(f"catalog.json: {len(CATALOG)} carte.")
    if GAME["id"] == "mtg":
        write_names(a.out, today, cur, info)
    n_cards = sum(len(d) for d in shard_data.values())
    with open(os.path.join(a.out, "meta.json"), "w", encoding="utf-8") as f:
        json.dump({"updated": today, "days": cdays, "archiveDays": len(days), "archiveDayList": days, "weeks": weeks, "cards": n_cards, "source": "Scryfall bulk default_cards (Cardmarket trend price, EUR)"}, f)
    print(f"Fatto: {today}, {len(days)} giorni, {n_cards} carte, classifiche: " + ", ".join(f"{k}={len(v)}" for k, v in out_m.items()))


if __name__ == "__main__":
    main()
