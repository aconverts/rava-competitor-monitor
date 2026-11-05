#!/usr/bin/env python3
"""Compare competitor perfumes against RAVA's catalog.

Matching works on the scent name: brand names, concentrations (EDT/EDP/Parfum),
gender words and filler are stripped, typos are tolerated, then each competitor
perfume is classified as:
  HAVE    : RAVA sells the same scent as a perfume
  CHECK   : a close but not identical RAVA perfume exists (flanker / naming doubt)
  MISSING : no RAVA perfume with that scent name
Output: reports/catalog-gap.csv
"""
from __future__ import annotations

import csv
import json
import re
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"

EXCLUDE_TYPES = {"body splash", "home spray", ""}
EXCLUDE_TITLE = re.compile(r"\b(set|sets|bundle|gift box|sample|discovery|home spray|body splash)\b", re.I)

STOP = set("""
eau de du des la le les l d of the a an and et for pour by
parfums perfume perfumes edt edp edc toilette cologne spray unisex ml
""".split())

GENDER_M = {"men", "man", "him", "homme", "uomo"}
GENDER_F = {"women", "woman", "her", "femme", "donna"}

# one canonical token per brand, so "Yves Saint Laurent" == "YSL", "Dolce & Gabbana" == "D&G"
ALIASES = [
    (r"\b(eau de parfum|eau de toilette|eau de cologne)\b", " "),
    (r"\ble parfum\b", "parfum"),
    (r"\b(yves )?saint laurent\b", "ysl"), (r"\bdolce and gabbana\b|\bdolce gabbana\b|\bd and g\b", "dg"),
    (r"\blouis vuitton\b", "lv"), (r"\bjean paul gaultier\b", "jpg"), (r"\bcalvin klein\b", "ck"),
    (r"\bparfums de marly\b|\bde marly\b", "pdm"), (r"\b(maison )?francis kurkdjian\b", "mfk"),
    (r"\b(giorgio|emporio) armani\b", "armani"), (r"\bpaco rabanne\b", "rabanne"), (r"\bhugo boss\b", "boss"),
    (r"\bthierry mugler\b", "mugler"), (r"\bchristian dior\b", "dior"), (r"\bcarolina herrera\b", "herrera"),
    (r"\btom ford\b", "tomford"), (r"\bby kilian\b", "kilian"), (r"\bviktor and rolf\b", "vr"),
    (r"\bjo malone( london)?\b", "jomalone"), (r"\bnarciso rodriguez\b", "narciso"), (r"\bestee lauder\b", "lauder"),
    (r"\bbulgari\b", "bvlgari"), (r"\bmont blanc\b", "montblanc"), (r"\bmarc antoine barrois\b", "barrois"),
    (r"\bmaison crivelli\b", "crivelli"), (r"\bissey miyake\b", "issey"), (r"\belie saab\b", "eliesaab"),
    (r"\btiziana terenzi\b", "terenzi"), (r"\binitio parfums prives\b|\bparfums prives initio\b", "initio"),
    (r"\blattafa perfumes\b", "lattafa"), (r"\bbdk parfums\b", "bdk"), (r"\bmaison (martin )?margiela\b", "margiela"),
    (r"\bvan cleef and arpels\b", "vca"), (r"\bserge lutens\b", "lutens"), (r"\bmatiere premiere\b", "matierepremiere"),
    (r"\bacqua di parma\b", "adp"), (r"\bmemo paris\b", "memo"), (r"\bstephane humbert lucas\b", "shl"),
    (r"\bbillie eilish\b", "billieeilish"), (r"\broja( dove)?\b", "roja"), (r"\bnasomatto\b", "nasomatto"),
]
EXTRA_BRANDS = """
creed chanel dior tomford ysl armani prada versace gucci lancome hermes guerlain givenchy valentino burberry
boss rabanne jpg herrera azzaro montblanc bvlgari dg narciso ck kilian xerjoff nishane mancera montale mfk
pdm initio amouage byredo labo kayali lattafa armaf afnan lv jomalone vr mugler jacobs nihilo crivelli barrois
roja penhaligon penhaligons diptyque malle memo orsay parisi terenzi zara ariana grande billieeilish sol janeiro
lauder cartier chloe miu davidoff dunhill kenzo issey veneta loewe balenciaga coach wang juliette clinique
eliesaab rasasi ajmal haramain gissah bdk margiela vca lutens matierepremiere adp shl nasomatto bohoboco
chopard lacoste ferrari escada cerruti cacharel moschino lanvin rochas
""".split()
# Manual verdicts after reviewing every uncertain match by hand (Nov 2026).
# status: HAVE | MISSING | VERSION (RAVA sells the original, not this flanker/concentration)
OVERRIDES = {
    # actually in RAVA under a different name
    "1 Million": ("HAVE", "One Million Paco Rabanne"),
    "Bade'e Al Oud Amethyst Lattafa": ("HAVE", "Badee Al Oud Amethyst Men"),
    "Kayali Boujee Marshmallow": ("HAVE", "Kayali Yum Boujee Marshmallow | 81"),
    "Kayali Lovefest Burning Cherry | 48": ("HAVE", "Kayali Burning Cherry 48"),
    "Silver Scent": ("HAVE", "Silver Scent Jacques Bogart"),
    "Silver Mountain Water Creed": ("HAVE", "Creed Silver Mountain"),
    "Portrait of a Lady Frederic Malle": ("HAVE", "Portrait of a Lady"),
    "Aqua Allegoria Mandarine Basilic Guerlain": ("HAVE", "Aqua Allegoria Mandarine"),
    "Allure Sensuelle Chanel": ("HAVE", "Allure Sensual"),
    "Blooming Bouquet Parfum": ("HAVE", "Blooming Bouquet"),
    "ChaneI No. 5 Parfum": ("HAVE", "Chanel No. 5"),
    "L’Homme EDT": ("HAVE", "L’Homme Prada (Alchemi's listing is Prada L'Homme)"),
    # RAVA has the original, but not this version
    "Sauvage Parfum": ("VERSION", "RAVA has Sauvage EDT and Elixir"),
    "Sauvage Extrait": ("VERSION", "RAVA has Sauvage EDT and Elixir"),
    "Le Male Le Parfum": ("VERSION", "RAVA has Le Male, Le Male Elixir, Le Male Lover"),
    "Azzaro The Most Wanted Parfum": ("VERSION", "RAVA has Azzaro The Most Wanted"),
    "Oud Satin Mood Extrait de Parfum Maison Francis Kurkdjian": ("VERSION", "RAVA has Oud Satin Mood For Men / For Women"),
    "Baccarat Rouge Extrait de Parfum": ("VERSION", "RAVA has Baccarat Rouge 540"),
    "Y Eau de Parfum Intense": ("VERSION", "RAVA has Y EDP and Y Iced Cologne"),
    "Terre d’Hermès Eau de Parfum Intense": ("VERSION", "RAVA has Terre d'Hermès"),
    "Valaya Exclusif Parfums de Marly": ("VERSION", "RAVA has Valaya"),
    "Uomo Born In Roma Intense": ("VERSION", "RAVA has Valentino Uomo Born in Roma"),
    "Born in Roma Extradose Donna Valentino": ("VERSION", "RAVA has Born in Roma Donna + Donna Intense"),
    "Flora Gorgeous Gardenia Intense Gucci": ("VERSION", "RAVA has Flora Gorgeous Gardenia"),
    "Bleu de Chanel L'Exclusif": ("VERSION", "RAVA has Bleu de Chanel"),
    "Armani Code Elixir": ("VERSION", "RAVA has Code Profumo, Absolu, Ultimate and others"),
    "Angels' Share Paradis By Kilian": ("VERSION", "RAVA has Angels' Share"),
    "Angel's Share On The Rocks By Kilian": ("VERSION", "RAVA has Angels' Share"),
    "Si Parfum Giorgio Armani": ("VERSION", "RAVA has Si Giorgio Armani"),
    "Libre Berry Crush": ("VERSION", "RAVA has Libre, Libre Intense, Flowers & Flames, L'Absolu Platine"),
    "Boss Bottled Triumph Elixir Hugo Boss": ("VERSION", "RAVA has Boss Bottled, Absolu, Night"),
    "Invictus Victory Absolu Paco Rabanne": ("VERSION", "RAVA has Invictus Victory Elixir"),
    "Kayali Oudgasm Rose Oud | 16": ("VERSION", "RAVA has Oudgasm Vanilla Oud 36"),
    "La Belle JPG": ("VERSION", "RAVA has Le Beau (men's), not La Belle"),
    "La Belle Paradise Garden": ("VERSION", "RAVA has Le Beau Paradise Garden (men's)"),
    # not in RAVA at all, even though a similar name exists
    "Armani Code for Men": ("MISSING", "Original Armani Code men is missing; RAVA only has flankers"),
    "Yara Lattafa Perfumes": ("MISSING", "Original pink Yara missing; RAVA has Yara Elixir, Moi, Tous"),
    "ANl": ("MISSING", "Listing typo for Nishane Ani"),
    "Babycat": ("MISSING", "YSL Le Vestiaire Babycat"),
    "Reflection man": ("MISSING", "Amouage Reflection Man"),
    "Kayali Tobacco Oud | 04": ("MISSING", "RAVA has Tom Ford Tobacco Oud, not Kayali's"),
    "Flower Kenzo": ("MISSING", "Flower by Kenzo"),
    "Blue Matcha Montale": ("MISSING", ""), "Love Hibiscus": ("MISSING", ""), "Oud Voyager": ("MISSING", ""),
    "Oud Zarian": ("MISSING", ""), "Le Beau Narcisse": ("MISSING", ""), "Moon Tale": ("MISSING", "Louis Vuitton, 2025"),
}


def norm(s: str) -> str:
    s = re.sub(r"[’'`´]", " ", s)
    s = re.sub(r"\b[Nn][°º]\s*", "no ", s)       # N°5 -> no 5
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    s = s.replace("&", " and ").replace("chanei", "chanel").replace("|", " ")
    s = re.sub(r"\bno\.?\s*(\d)", r"no\1", s)   # No. 5 -> no5
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    for rx, rep in ALIASES:
        s = re.sub(rx, rep, s)
    s = re.sub(r"\b0+(\d)", r"\1", s)          # Kayali "04" -> "4"
    return re.sub(r"\s+", " ", s).strip()


def build_brand_words(rava: list[dict]) -> set[str]:
    from collections import Counter
    words = set(EXTRA_BRANDS)
    counts = Counter()
    for p in rava:
        if p["type"] != "Perfume":
            continue
        for t in p["tags"]:
            if t.lower().startswith("brand:"):
                counts[norm(t[6:])] += 1
        if p["vendor"] and p["vendor"].upper() != "RAVA":
            counts[norm(p["vendor"])] += 1
    for brand, n in counts.items():
        if n >= 3:          # real brands have several products; one-offs are mis-tagged scent names
            words |= set(brand.split())
    # never treat these scent words as brand words
    return words - {"secret", "body", "shop", "night", "man", "essence", "carmina", "adore", "king", "devotion", "rain", "water",
                    "silver", "mountain", "green", "sun", "song", "dream", "love", "flower", "tea", "oud", "rose", "black", "noir", "white", "intense", "sport", "blue", "gold", "one",
                    "light", "good", "girl", "boy", "bad", "my", "way", "the", "wanted", "most", "vanilla",
                    "parfum", "extrait", "elixir", "absolu", "men", "women", "homme", "femme", "her", "him"}


def core(title: str, brands: set[str]) -> dict:
    toks = [t for t in norm(title).split() if t not in STOP]
    gender = ("M" if any(t in GENDER_M for t in toks) else "") + ("F" if any(t in GENDER_F for t in toks) else "")
    toks = [t for t in toks if t not in GENDER_M and t not in GENDER_F]
    brand = {t for t in toks if t in brands}
    name = tuple(t for t in toks if t not in brands) or tuple(toks)
    return {"name": name, "brand": brand, "gender": gender if len(gender) == 1 else ""}


def tok_eq(a: str, b: str) -> bool:
    if a == b:
        return True
    if a.isdigit() or b.isdigit() or a[0] != b[0]:
        return False
    return len(a) >= 6 and len(b) >= 6 and SequenceMatcher(None, a, b).ratio() >= 0.86


def compare(c: dict, r: dict) -> float:
    """1.0 = same scent, 0.5-0.99 = related (flanker / doubt), 0 = different."""
    if c["gender"] and r["gender"] and c["gender"] != r["gender"]:
        return 0.0
    if c["brand"] and r["brand"] and not (c["brand"] & r["brand"]):
        return 0.0
    a, b = c["name"], r["name"]
    if not a or not b:
        return 0.0
    hit_a = sum(any(tok_eq(x, y) for y in b) for x in a)
    hit_b = sum(any(tok_eq(y, x) for x in a) for y in b)
    if hit_a == len(a) and hit_b == len(b):
        return 1.0
    return min(hit_a / len(a), hit_b / len(b)) * 0.5 + max(hit_a / len(a), hit_b / len(b)) * 0.5




def main():
    rava = json.load(open(DATA / "rava" / "catalog.json"))
    brands = build_brand_words(rava)
    rava_perf = [(p, core(p["title"], brands)) for p in rava if p["type"] == "Perfume"]
    rava_other = [(p, core(re.sub(r"(roll on|hair mist|body splash|tanning oil|home spray|\d+ml)", "",
                                  p["title"], flags=re.I), brands))
                  for p in rava if p["type"] in ("Roll On", "Hair Mist", "Body Splash", "Tanning Oil", "Home Spray")]

    rows = {}
    for site, label in (("alchemi", "Alchemi"), ("khaial", "Khaial")):
        snap = json.load(open(DATA / site / "snapshot.json"))
        for p in snap["products"].values():
            if p["type"].lower() in EXCLUDE_TYPES or EXCLUDE_TITLE.search(p["title"]):
                continue
            c = core(p["title"], brands)
            best, score = None, 0.0
            for rp, rc in rava_perf:
                s = compare(c, rc)
                if s > score:
                    best, score = rp, s
            status = "HAVE" if score >= 1.0 else "CHECK" if score >= 0.75 else "MISSING"
            note = ""
            if p["title"] in OVERRIDES:
                status, note = OVERRIDES[p["title"]]
            elif status == "CHECK":
                note = "needs review"
            other = ""
            if status != "HAVE":
                lines = sorted({rp["type"] for rp, rc in rava_other if compare(c, rc) >= 1.0})
                other = ", ".join(lines)
            prices = sorted({v["price"] for v in p["variants"].values() if v["price"]})
            key = (c['name'], c['gender'])
            row = rows.setdefault(key, {
                "scent": p["title"], "status": status, "closest_rava": best["title"] if best and score >= 0.5 else "",
                "match_score": round(score, 2), "note": note, "rava_has_in_other_lines": other,
                "alchemi": "", "khaial": "", "alchemi_url": "", "khaial_url": "",
                "type": p["type"], "in_stock": "",
            })
            row[site] = "from $" + (f"{prices[0]:.2f}" if prices else "?")
            row[f"{site}_url"] = p["url"]
            avail = any(v["available"] for v in p["variants"].values())
            row["in_stock"] = (row["in_stock"] + f" {label}:{'yes' if avail else 'SOLD OUT'}").strip()

    out = sorted(rows.values(), key=lambda r: ({"MISSING": 0, "VERSION": 1, "CHECK": 2, "HAVE": 3}[r["status"]], r["scent"].lower()))
    path = ROOT / "reports" / "catalog-gap.csv"
    path.parent.mkdir(exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(out[0].keys()))
        w.writeheader()
        w.writerows(out)
    from collections import Counter
    print(Counter(r["status"] for r in out), "unique scents:", len(out))
    return out


if __name__ == "__main__":
    main()
