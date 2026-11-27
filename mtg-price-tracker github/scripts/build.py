#!/usr/bin/env python3
"""Archivio giornaliero dei prezzi Cardmarket (via Scryfall) per l'app.

Ogni giorno:
  1. rilegge lo storico già pubblicato (meta.json + 256 file h/XX.json);
  2. scarica il bulk "default_cards" di Scryfall e prende i prezzi EUR e EUR foil di ogni edizione;
  3. aggiunge il giorno, tiene gli ultimi DAYS_KEEP giorni e riscrive i 256 file;
  4. calcola le classifiche di chi sale e chi scende (1, 7, 30 giorni) in movers.json;
  5. scrive market.json: tutte le carte da almeno 1 € con prezzo e prezzi di 1, 7 e 30 giorni prima,
     per la pagina iniziale del sito.

Uso:
  python build.py --out _site/data --prev https://utente.github.io/repo/data
  python build.py --out out --prev ./vecchi-dati --bulk default-cards.json --today 2026-11-25   (prove in locale)
"""
import argparse, datetime, json, os, sys, time, urllib.error, urllib.request

DAYS_KEEP = 120         # archivio completo (a/), letto solo da questo script
CLIENT_DAYS = 35        # quello che scarica l'app (h/): bastano 30 giorni + margine
MIN_CENTS = 100          # classifiche solo per carte da almeno 1 € (prima e dopo)
TOP = 300                # voci per classifica
MARKET_MIN = 100         # elenco "tutte le carte" (market.json): edizioni da almeno 1 €, normale o foil
CM_PREFIX = "https://www.cardmarket.com/en/Magic/Products"
FORMATS = ["commander", "duel", "modern", "legacy", "vintage", "pauper", "pioneer", "standard"]
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


def read_prev(prev):
    """Restituisce (days, {id: (e_list, f_list)}) dallo storico pubblicato, o vuoto al primo giro."""
    if not prev:
        return [], {}
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
            return [], {}
        raise
    days = meta["days"]
    hist = {}
    for sh in SHARDS:  # un errore qui ferma tutto: meglio non pubblicare che perdere lo storico
        try:
            data = load(f"a/{sh}.json")
        except (urllib.error.HTTPError, FileNotFoundError):
            data = load(f"h/{sh}.json")
        for cid, v in data.items():
            hist[cid] = (dec(v.get("e", ""), len(days)), dec(v.get("f", ""), len(days)))
    print(f"Storico precedente: {len(days)} giorni, {len(hist)} carte.")
    return days, hist


def dec(s, n):
    if not s:
        return [None] * n
    out = [int(x) if x else None for x in s.split(",")]
    return (out + [None] * n)[:n]


def enc(vals):
    return ",".join("" if v is None else str(v) for v in vals)


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
            cur[cid] = (e, fo)
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
    a = ap.parse_args()
    today = a.today or datetime.datetime.now(datetime.timezone.utc).date().isoformat()

    old_days, hist = read_prev(a.prev)
    cur, info = read_bulk(a.bulk or latest_bulk())
    if len(cur) < 1000 and not a.bulk:
        sys.exit("Troppo pochi prezzi nel bulk: qualcosa non va, non pubblico.")

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
    cdays = days[-CLIENT_DAYS:]
    cut = len(days) - len(cdays)
    shard_data = {sh: {} for sh in SHARDS}
    market, set_names = [], {}
    ids = set(hist) | set(cur)
    for cid in ids:
        oe, of = hist.get(cid, ([], []))
        ne, nf = [], []
        for d in days[:-1]:
            i = idx_old.get(d)
            ne.append(oe[i] if i is not None and i < len(oe) else None)
            nf.append(of[i] if i is not None and i < len(of) else None)
        te, tf = cur.get(cid, (None, None))
        ne.append(te)
        nf.append(tf)
        if all(v is None for v in ne) and all(v is None for v in nf):
            continue
        rec = {"e": enc(ne)}
        if any(v is not None for v in nf):
            rec["f"] = enc(nf)
        shard_data[cid[:2]][cid] = rec
        if cid in info and max(ne[-1] or 0, nf[-1] or 0) >= MARKET_MIN:
            nm, st, sn, cn, mask, cm = info[cid]
            set_names[st] = sn
            row = [cid, nm, st, cn, mask, cm[len(CM_PREFIX):] if cm.startswith(CM_PREFIX) else cm]
            for arr in (ne, nf):
                row.append(arr[-1])
                row.extend(arr[bases[w]] if bases[w] is not None else None for w in (1, 7, 30))
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

    for sh, data in shard_data.items():
        with open(os.path.join(a.out, "a", f"{sh}.json"), "w", encoding="utf-8") as f:
            json.dump(data, f, separators=(",", ":"), ensure_ascii=False)
        short = {}
        for cid, rec in data.items():
            e = dec(rec["e"], len(days))[cut:]
            fo = dec(rec.get("f", ""), len(days))[cut:]
            if all(v is None for v in e) and all(v is None for v in fo):
                continue
            sr = {"e": enc(e)}
            if any(v is not None for v in fo):
                sr["f"] = enc(fo)
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
        json.dump({"updated": today, "formats": FORMATS, "fields": ["id", "name", "set", "setName", "cn", "price", "base", "legal", "cm"], "lists": out_m},
                  f, separators=(",", ":"), ensure_ascii=False)
    market.sort(key=lambda r: -max(r[6] or 0, r[10] or 0))
    with open(os.path.join(a.out, "market.json"), "w", encoding="utf-8") as f:
        json.dump({"updated": today, "formats": FORMATS, "cmPrefix": CM_PREFIX, "sets": set_names,
                   "fields": ["id", "name", "set", "cn", "legal", "cm", "price", "base1", "base7", "base30",
                              "foil", "foilBase1", "foilBase7", "foilBase30"], "cards": market},
                  f, separators=(",", ":"), ensure_ascii=False)
    print(f"market.json: {len(market)} carte da almeno {MARKET_MIN / 100:.0f} €")
    n_cards = sum(len(d) for d in shard_data.values())
    with open(os.path.join(a.out, "meta.json"), "w", encoding="utf-8") as f:
        json.dump({"updated": today, "days": cdays, "archiveDays": len(days), "cards": n_cards, "source": "Scryfall bulk default_cards (Cardmarket trend price, EUR)"}, f)
    print(f"Fatto: {today}, {len(days)} giorni, {n_cards} carte, classifiche: " + ", ".join(f"{k}={len(v)}" for k, v in out_m.items()))


if __name__ == "__main__":
    main()
