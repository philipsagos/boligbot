#!/usr/bin/env python3
"""
Boligbot — overvåger Kereby og City Apartment og sender nye boliger,
der matcher jeres kriterier, til Telegram med en færdig ansøgningstekst.

Kørsel:
  python boligbot.py          # normal kørsel (bruges af GitHub Actions)
  python boligbot.py --test   # viser hvad botten finder, sender intet, gemmer intet
  python boligbot.py --loop   # kører uendeligt hvert 2. minut (til egen computer/server)
"""
import html
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

# =====================================================================
#  KRITERIER — ret her
# =====================================================================
MAX_LEJE_GROEN = 13000      # kr./md. INKL. aconto -> grønt match
MAX_LEJE_GUL = 13500        # kr./md. INKL. aconto -> næsten-match
MIN_M2 = 50
MIN_M2_GUL = 48             # 48–49 m² giver næsten-match
MIN_VAERELSER = 2
ACONTO_SKOEN = 800          # bruges når annoncen ikke oplyser aconto

# Postnumre -> område
OMRAADER = {
    "Sydhavn": [2450],
    "Amager": [2300, 2770],                      # København S/Ørestad + Kastrup
    "Nørrebro": [2200],
    "Vesterbro": list(range(1550, 1800)),        # København V
    "Frederiksberg": list(range(1800, 2001)),    # Frederiksberg C + 2000
    "Østerbro": [2100, 2150],
    "Valby": [2500],
    "Nordvest": [2400],
    "Vanløse": [2720],
    "Indre By": list(range(1000, 1550)),         # hele København K
}

# =====================================================================
#  ANSØGNINGSTEKST — personlige oplysninger hentes fra GitHub Secrets
# =====================================================================
TELEFON = os.getenv("ANSOEGER_TELEFON", "[telefon]")
EMAIL = os.getenv("ANSOEGER_EMAIL", "[mail]")
OM_OS = os.getenv("ANSOEGER_OM_OS", "")   # fx "Jeg arbejder som X, Katrine studerer Y."

ANSOEGNING = """Hej

Min kæreste Katrine og jeg er meget interesserede i lejligheden på {adresse} og vil meget gerne komme til en fremvisning.
{om_os}
Vi søger et hjem, hvor vi kan bo sammen på længere sigt, og lejligheden passer rigtig godt til det, vi leder efter.{altan}

Vi er fleksible i forhold til både fremvisning og hurtig overtagelse.

Ring endelig på {telefon} eller skriv til {email}.

Bedste hilsner
Philip & Katrine"""

# =====================================================================
STATE_FIL = Path(__file__).with_name("state.json")
HEADERS = {"User-Agent": "Mozilla/5.0 (privat boligsøgning; kontakt via egen mail)"}
TIMEOUT = 25


def hent(url):
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return r.text


def tal(s):
    """'12.500' / '12,500' / '12 500' -> 12500"""
    if s is None:
        return None
    s = re.sub(r"[^\d]", "", s)
    return int(s) if s else None


def omraade(postnr):
    if postnr is None:
        return None
    for navn, numre in OMRAADER.items():
        if postnr in numre:
            return navn
    return None


def er_stueetage(adresse):
    """Kigger på etagedelen af adressen, fx 'Vej 1, st. th, 2200 ...'."""
    dele = [d.strip().lower() for d in adresse.split(",")]
    for d in dele[1:]:
        if re.match(r"^(st|stuen|kl|kld|kælder|parterre)\b", d):
            return True
    return False


def find_kort(element, markoer):
    """Går op fra et link til den mindste boks, der indeholder præcis én annonce."""
    node = element
    for _ in range(10):
        tekst = node.get_text(" ", strip=True)
        antal = tekst.count(markoer)
        if antal == 1:
            return node
        if antal > 1:
            return None
        if node.parent is None:
            return None
        node = node.parent
    return None


# ---------------------------------------------------------------------
#  KEREBY
# ---------------------------------------------------------------------
def kereby_liste():
    soup = BeautifulSoup(hent("https://kereby.dk/bolig/"), "html.parser")
    boliger = {}
    for a in soup.find_all("a", href=True):
        m = re.match(r"^(?:https?://(?:www\.)?kereby\.dk)?/bolig/([^/?#]+)/?$", a["href"])
        if not m:
            continue
        url = f"https://kereby.dk/bolig/{m.group(1)}/"
        if url in boliger:
            continue
        kort = find_kort(a, "kr./md")
        if kort is None:
            continue
        t = kort.get_text(" ", strip=True)
        if re.search(r"\b(Reserveret|Udlejet)\b", t):
            continue
        pn = re.search(r",\s*(\d{4})\s", t)
        boliger[url] = {
            "kilde": "Kereby",
            "url": url,
            "adresse": None,
            "postnr": int(pn.group(1)) if pn else None,
            "leje": tal(m2.group(1)) if (m2 := re.search(r"([\d.]+)\s*kr\./md", t)) else None,
            "aconto": None,
            "vaer": int(v.group(1)) if (v := re.search(r"(\d+)\s*v[æa]relser", t)) else None,
            "m2": int(k.group(1)) if (k := re.search(r"(\d+)\s*m\s*(?:2|²)", t)) else None,
            "altan": bool(re.search(r"\baltan\b", t, re.I)),
            "ledig": None,
            "tidsbegraenset": None,
        }
    return list(boliger.values())


def kereby_detaljer(b):
    """Henter selve annoncen for adresse, etage, aconto og lejeperiode."""
    soup = BeautifulSoup(hent(b["url"]), "html.parser")
    titel = soup.title.get_text(strip=True) if soup.title else ""
    adresse = titel.split("|")[0].strip()
    if adresse:
        b["adresse"] = adresse
        pn = re.search(r"\b(\d{4})\b", adresse.split(",")[-1])
        if pn:
            b["postnr"] = int(pn.group(1))
    t = soup.get_text(" ", strip=True)
    if (m := re.search(r"Leje\s*([\d.]+)\s*kr\./md", t)):
        b["leje"] = tal(m.group(1))
    if b["m2"] is None and (m := re.search(r"(\d+)\s*m\s*(?:2|²)", t)):
        b["m2"] = int(m.group(1))
    if (m := re.search(r"aconto\s*([\d.]+)\s*kr", t, re.I)):
        b["aconto"] = tal(m.group(1))
    if (m := re.search(r"(?:Overtagelse|Indflytning|Ledig)[^\d]{0,20}(\d{1,2}[./-]\d{1,2}[./-]\d{2,4})", t, re.I)):
        b["ledig"] = m.group(1)
    if re.search(r"\baltan\b", t, re.I):
        b["altan"] = True
    b["tidsbegraenset"] = bool(re.search(r"tidsbegr[æa]ns", t, re.I))
    return b


# ---------------------------------------------------------------------
#  CITY APARTMENT
# ---------------------------------------------------------------------
def cityapartment_liste():
    soup = BeautifulSoup(hent("https://cityapartment.dk/apartment-rentals-copenhagen/"), "html.parser")
    boliger = {}
    for a in soup.find_all("a", href=True):
        m = re.match(r"^(?:https?://(?:www\.)?cityapartment\.dk)?/cityapartments/([^/?#]+)/?$", a["href"])
        if not m:
            continue
        url = f"https://cityapartment.dk/cityapartments/{m.group(1)}/"
        if url in boliger:
            continue
        kort = find_kort(a, "DKK")
        if kort is None:
            continue
        t = kort.get_text(" ", strip=True)
        overskrift = kort.find(["h2", "h3", "h4"], string=re.compile(r"\d"))
        if overskrift is None:
            overskrift = next((h for h in kort.find_all(["h2", "h3", "h4"]) if re.search(r"\d", h.get_text())), None)
        adresse = overskrift.get_text(" ", strip=True).title() if overskrift else m.group(1)
        boliger[url] = {
            "kilde": "City Apartment",
            "url": url,
            "adresse": adresse,
            "postnr": int(p.group(1)) if (p := re.search(r"Zip code\s*(\d{4})", t)) else None,
            "leje": tal(l.group(1)) if (l := re.search(r"([\d.,]+)\s*DKK", t)) else None,
            "aconto": None,   # varme er ikke med i lejen -> skønnes
            "vaer": int(v.group(1)) if (v := re.search(r"(\d+)-room", t)) else None,
            "m2": int(k.group(1)) if (k := re.search(r"(\d+)\s*m²", t)) else None,
            "altan": bool(re.search(r"balcon|altan", t, re.I)),
            "ledig": d.group(1) if (d := re.search(r"Available from\s*([\d/]+)", t)) else None,
            "tidsbegraenset": False,   # City Apartment udlejer tidsubegrænset
        }
    return list(boliger.values())


# ---------------------------------------------------------------------
#  SIDER DER INDLÆSES MED JAVASCRIPT (Balder, Heimstaden)
#  Åbnes i en usynlig browser (Playwright) og læses generelt.
# ---------------------------------------------------------------------
PRIS_RE = re.compile(r"(\d{1,2}[.\s]?\d{3})\s*(?:kr|DKK)", re.I)
M2_RE = re.compile(r"(\d{2,3})\s*(?:m\s*2|m²|kvm)", re.I)
VAER_RE = re.compile(r"(\d)\s*-?\s*(?:vær|værelser|rum|room)", re.I)
POSTNR_RE = re.compile(r"\b(\d{4})\s+[A-ZÆØÅ]")
ADRESSE_RE = re.compile(r"([A-ZÆØÅ][A-Za-zÆØÅæøåé.' -]{2,40}?\s\d+\s?[A-Za-z]?(?:,\s*[^,\d]{0,6}\d*\.?\s*[a-zæøå.]{0,4})?),?\s*(\d{4})\s+[A-ZÆØÅ]")

SLUG_RE = re.compile(r"(?P<vej>[a-z-]+?)-(?P<nr>\d+(?:-[a-z])?)-(?P<etage>st|kl|\d{1,2})-"
                     r"(?P<side>[a-z]{1,2}|\d{1,2})-(?P<pn>\d{4})-(?P<by>[a-z-]+?)(?:-danmark)?/?$")


def adresse_fra_link(url):
    """'.../else-alfelts-vej-80-st-tv-2300-koebenhavn-s-danmark' -> 'Else Alfelts Vej 80, st. tv, 2300 Koebenhavn S'"""
    m = SLUG_RE.search(url.rstrip("/").rsplit("/", 1)[-1])
    if not m:
        return None
    vej = m["vej"].replace("-", " ").title()
    nr = m["nr"].replace("-", "").upper()
    return f"{vej} {nr}, {m['etage']}. {m['side']}, {m['pn']} {m['by'].replace('-', ' ').title()}"


def render_sider(urls):
    """Åbner en eller flere sider i én usynlig browser og returnerer HTML for hver."""
    from playwright.sync_api import sync_playwright
    resultat = {}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        side = browser.new_page(user_agent=HEADERS["User-Agent"], locale="da-DK")
        for url in urls:
            try:
                side.goto(url, wait_until="networkidle", timeout=60000)
                for _ in range(6):   # scroll for at få alle boliger indlæst
                    side.mouse.wheel(0, 4000)
                    side.wait_for_timeout(700)
                resultat[url] = side.content()
            except Exception as ex:  # noqa: BLE001
                print(f"  kunne ikke åbne {url}: {ex}")
        browser.close()
    return resultat


def render_side(url):
    html_ = render_sider([url]).get(url)
    if html_ is None:
        raise RuntimeError(f"kunne ikke åbne {url}")
    return html_


def generisk_liste(kilde, url, html_=None, debug=True):
    soup = BeautifulSoup(html_ if html_ is not None else render_side(url), "html.parser")
    vaert = re.match(r"https?://[^/]+", url).group(0)
    boliger = {}
    for a in soup.find_all("a", href=True):
        href = urljoin(url, a["href"]).split("#")[0]
        if not href.startswith(vaert) or href.rstrip("/") == url.rstrip("/"):
            continue
        node, kort = a, None
        for _ in range(8):   # gå op til den boks der har både pris og m²
            t = node.get_text(" ", strip=True)
            if PRIS_RE.search(t) and M2_RE.search(t):
                kort = node
                break
            if node.parent is None:
                break
            node = node.parent
        if kort is None:
            continue
        t = kort.get_text(" ", strip=True)
        if len(t) > 1500 or href in boliger:   # for stor boks = flere boliger
            continue
        if re.search(r"\b(Reserveret|Udlejet|Rented)\b", t, re.I):
            continue
        adr = ADRESSE_RE.search(t)
        pn = POSTNR_RE.search(t)
        adresse = f"{adr.group(1).strip()}, {adr.group(2)}" if adr else adresse_fra_link(href)
        boliger[href] = {
            "kilde": kilde,
            "url": href,
            "adresse": adresse,
            "postnr": int(pn.group(1)) if pn else None,
            "leje": tal(p.group(1)) if (p := PRIS_RE.search(t)) else None,
            "aconto": tal(ac.group(1)) if (ac := re.search(r"aconto[^\d]{0,15}([\d.]+)", t, re.I)) else None,
            "vaer": int(v.group(1)) if (v := VAER_RE.search(t)) else None,
            "m2": int(k.group(1)) if (k := M2_RE.search(t)) else None,
            "altan": bool(re.search(r"\baltan|balcon", t, re.I)),
            "ledig": None,
            "tidsbegraenset": bool(re.search(r"tidsbegr[æa]ns", t, re.I)),
        }
    if not boliger and debug:
        # Hjælp til fejlfinding: vis hvad siden faktisk indeholdt
        print(f"  [debug {kilde}] sidens tekst: {soup.get_text(' ', strip=True)[:1500]}")
        links = sorted({a['href'] for a in soup.find_all('a', href=True)})[:60]
        print(f"  [debug {kilde}] links: {links}")
    return list(boliger.values())


def balder_liste():
    return generisk_liste("Balder", "https://www.balder.dk/lejeboliger")


def heimstaden_liste():
    return generisk_liste("Heimstaden", "https://www.heimstaden.dk/lejeboliger/koebenhavn/")


CEJ_OVERBLIK = "https://udlejning.cej.dk/find-bolig/overblik?collection=projects&monthlyPrice=0-50000"


def cej_liste():
    """CEJ viser ejendomme først. Botten åbner hver københavnsk ejendom,
    trykker 'Se ledige boliger' og læser lejlighederne."""
    from playwright.sync_api import sync_playwright
    vaert = "https://udlejning.cej.dk"
    soup = BeautifulSoup(render_side(CEJ_OVERBLIK), "html.parser")
    ejendomme = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.startswith("/"):
            href = vaert + href
        if "/ejendomme/" in href and href not in ejendomme:
            ejendomme.append(href)
    print(f"  CEJ: {len(ejendomme)} ejendomme fundet")
    if not ejendomme:
        print(f"  [debug CEJ] sidens tekst: {soup.get_text(' ', strip=True)[:1500]}")
        return []

    boliger = {}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        side = browser.new_page(user_agent=HEADERS["User-Agent"], locale="da-DK")
        for url in ejendomme[:25]:
            try:
                side.goto(url, wait_until="networkidle", timeout=60000)
                titel = side.title()
                tekst = side.inner_text("body")
                # Byen står lige før den første "Se ledige boliger"
                hoved = titel + " " + re.split(r"Se ledige boliger", tekst, flags=re.I)[0][-300:]
                if not re.search(r"København|Frederiksberg|Valby|Kastrup", hoved):
                    print(f"  CEJ: springer over (uden for København): {titel.split('|')[0].strip()}")
                    continue
                # Find knappen/linket der faktisk er synligt (siden har en skjult mobil-udgave)
                mål = side.evaluate("""() => {
                    const els = [...document.querySelectorAll('a,button,span,div')]
                      .filter(e => /se ledige boliger/i.test(e.textContent || '') && e.children.length <= 2);
                    for (const e of els) {
                        const a = e.closest('a[href]');
                        if (a) return {href: a.href};
                    }
                    const synlig = els.find(e => e.offsetParent !== null) || els[0];
                    if (!synlig) return null;
                    (synlig.closest('button') || synlig).click();
                    return {klik: true};
                }""")
                if not mål:
                    print(f"  [debug CEJ] ingen 'Se ledige boliger'-knap på {url}")
                    continue
                if mål.get("href"):
                    side.goto(mål["href"], wait_until="networkidle", timeout=60000)
                else:
                    side.wait_for_load_state("networkidle", timeout=30000)
                side.wait_for_timeout(1500)
                for _ in range(6):
                    side.mouse.wheel(0, 4000)
                    side.wait_for_timeout(600)
                nu_url = side.url
                html_ = side.content()
            except Exception as ex:  # noqa: BLE001
                print(f"  CEJ: kunne ikke læse {url}: {ex}")
                continue
            fundet = [b for b in generisk_liste("CEJ", nu_url, html_=html_, debug=False)
                      if b["url"].rstrip("/") not in {e.rstrip("/") for e in ejendomme}]
            print(f"  CEJ: {titel.split('|')[0].strip()} -> {len(fundet)} lejligheder (side: {nu_url})")
            if not fundet:
                bs = BeautifulSoup(html_, "html.parser")
                print(f"  [debug CEJ] tekst: {bs.get_text(' ', strip=True)[:1200]}")
                print(f"  [debug CEJ] links: {sorted({a['href'] for a in bs.find_all('a', href=True)})[:40]}")
            for b in fundet:
                boliger[b["url"]] = b
        browser.close()
    return list(boliger.values())


# ---------------------------------------------------------------------
#  EDC LEJEBOLIG — nyeste boliger står øverst, så vi læser kun de første sider
# ---------------------------------------------------------------------
SETTE_URLS = set()   # udfyldes af koer() med de boliger botten allerede har set

EDC_LISTER = [
    ("https://www.edc.dk/lejebolig/lejlighed/koebenhavn/", 6),   # (adresse, maks. sider pr. kørsel)
    ("https://www.edc.dk/lejebolig/1800-1999-frederiksberg-c/", 2),
    ("https://www.edc.dk/lejebolig/2000-frederiksberg/", 2),
    ("https://www.edc.dk/lejebolig/2770-kastrup/", 1),
]
EDC_LINK_RE = re.compile(r"^(?:https?://(?:www\.)?edc\.dk)?(/leje/[a-z-]+/[^/]+/[^/]+/\d+)/?$")


def edc_side(url):
    soup = BeautifulSoup(hent(url), "html.parser")
    boliger = {}
    for a in soup.find_all("a", href=True):
        m = EDC_LINK_RE.match(a["href"])
        if not m:
            continue
        link = "https://www.edc.dk" + m.group(1) + "/"
        if link in boliger:
            continue
        kort = find_kort(a, "kr./md")
        if kort is None:
            continue
        t = kort.get_text(" ", strip=True)
        if re.search(r"\b(Reserveret|Udlejet)\b", t):
            continue
        t_adr = re.sub(r"\b(Nyhed|Åbent hus[^A-ZÆØÅ]*|[A-ZÆØÅa-zæøå]+ fra \S+)", " ", t)
        adr = re.search(r"([A-ZÆØÅ][^\d]{1,60}?\s\d+[A-Za-z]?(?:,\s*[^,]{1,15}?)?)\s+(\d{4})\s+([A-ZÆØÅ][A-Za-zÆØÅæøå. ]+?)\s+\d+\s*rum", t_adr)
        boliger[link] = {
            "kilde": "EDC",
            "url": link,
            "adresse": f"{adr.group(1).strip()}, {adr.group(2)} {adr.group(3).strip()}" if adr else adresse_fra_link(link),
            "postnr": int(adr.group(2)) if adr else (int(p.group(1)) if (p := re.search(r"/leje/[a-z-]+/(\d{4})-", link)) else None),
            "leje": tal(l.group(1)) if (l := re.search(r"([\d.]+)\s*kr\./md", t)) else None,
            "aconto": None,
            "vaer": int(v.group(1)) if (v := re.search(r"(\d+)\s*rum", t)) else None,
            "m2": int(k.group(1)) if (k := re.search(r"(\d+)\s*m²", t)) else None,
            "altan": bool(re.search(r"\baltan", t, re.I)),
            "ledig": None,
            "tidsbegraenset": bool(re.search(r"tidsbegr[æa]ns", t, re.I)),
        }
    return list(boliger.values())


def edc_liste():
    alle = {}
    for base, maks in EDC_LISTER:
        for side_nr in range(1, maks + 1):
            url = base if side_nr == 1 else f"{base}?side={side_nr}"
            fundet = edc_side(url)
            time.sleep(1)
            for b in fundet:
                alle[b["url"]] = b
            # Stop når en hel side kun indeholder boliger vi har set før
            if not fundet or all(b["url"] in SETTE_URLS for b in fundet):
                break
    return list(alle.values())


KILDER = [
    ("Kereby", kereby_liste, kereby_detaljer),
    ("City Apartment", cityapartment_liste, None),
    ("Balder", balder_liste, None),
    ("Heimstaden", heimstaden_liste, None),
    ("EDC", edc_liste, None),
    # ("CEJ", cej_liste, None),   # slået fra
]


# ---------------------------------------------------------------------
#  VURDERING
# ---------------------------------------------------------------------
def total_leje(b):
    if b["leje"] is None:
        return None
    return b["leje"] + (b["aconto"] if b["aconto"] is not None else ACONTO_SKOEN)


def hurtigt_fravalg(b):
    """Fravælger på listedata, så vi ikke henter detaljer unødigt."""
    if b["vaer"] is not None and b["vaer"] < MIN_VAERELSER and (b["m2"] is None or b["m2"] < MIN_M2):
        return True   # få værelser OG lille -> fravælg (stor bolig med "1 værelse" får lov at gå videre)
    if b["m2"] is not None and b["m2"] < MIN_M2_GUL:
        return True
    if b["leje"] is not None and b["leje"] > MAX_LEJE_GUL:
        return True
    if b["postnr"] is not None and omraade(b["postnr"]) is None:
        return True
    return False


def vurder(b):
    """Returnerer ('groen' | 'gul' | 'nej', [noter])."""
    noter = []
    gul = False

    omr = omraade(b["postnr"])
    if b["postnr"] is not None and omr is None:
        return "nej", ["forkert område"]
    if omr is None:
        gul = True
        noter.append("område ukendt")

    if b["adresse"] and er_stueetage(b["adresse"]):
        return "nej", ["stueetage"]
    if not b["adresse"]:
        gul = True
        noter.append("etage ukendt — tjek at det ikke er stuen")

    if b["vaer"] is None:
        gul = True
        noter.append("antal værelser ukendt")
    elif b["vaer"] < MIN_VAERELSER:
        if b["m2"] is not None and b["m2"] >= MIN_M2:
            # Nogle udlejere (fx City Apartment) tæller kun soveværelser
            gul = True
            noter.append(f"står som {b['vaer']} værelse, men er {b['m2']} m² — tjek antal værelser")
        else:
            return "nej", ["for få værelser"]

    if b["m2"] is None:
        gul = True
        noter.append("størrelse ukendt")
    elif b["m2"] < MIN_M2_GUL:
        return "nej", ["for lille"]
    elif b["m2"] < MIN_M2:
        gul = True
        noter.append(f"kun {b['m2']} m²")

    tot = total_leje(b)
    if tot is None:
        gul = True
        noter.append("pris ukendt")
    elif tot > MAX_LEJE_GUL:
        return "nej", ["for dyr"]
    elif tot > MAX_LEJE_GROEN:
        gul = True
        noter.append(f"{tot - MAX_LEJE_GROEN} kr. over budget")
    if b["aconto"] is None and b["leje"] is not None:
        noter.append(f"aconto ikke oplyst, skønnet {ACONTO_SKOEN} kr.")

    if b["tidsbegraenset"]:
        gul = True
        noter.append("TIDSBEGRÆNSET — tjek at det er mindst 23 mdr.")

    return ("gul" if gul else "groen"), noter


# ---------------------------------------------------------------------
#  TELEGRAM
# ---------------------------------------------------------------------
def send_telegram(tekst):
    token = os.getenv("TELEGRAM_TOKEN")
    chat = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("[ingen Telegram-opsætning]\n" + tekst + "\n")
        return
    for chat_id in [c.strip() for c in chat.split(",") if c.strip()]:   # flere modtagere: "123,456"
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": tekst, "parse_mode": "HTML", "disable_web_page_preview": False},
            timeout=TIMEOUT,
        )
        if not r.ok:
            print("Telegram-fejl:", chat_id, r.status_code, r.text)


def ansoegningstekst(b):
    return ANSOEGNING.format(
        adresse=b["adresse"].split(",")[0] if b["adresse"] else "jeres annonce",
        om_os=("\n" + OM_OS + "\n") if OM_OS else "",
        altan=" Ikke mindst altanen er et stort plus for os." if b["altan"] else "",
        telefon=TELEFON,
        email=EMAIL,
    )


# ---------------------------------------------------------------------
#  AUTOMATISK BOOKING AF FREMVISNING PÅ KEREBY
#  KEREBY_AUTOBOOK = "test"  -> udfylder formularen men sender IKKE (til afprøvning)
#  KEREBY_AUTOBOOK = "til"   -> sender forespørgslen på grønne match
#  ikke sat / andet          -> slået fra
# ---------------------------------------------------------------------
AUTOBOOK = os.getenv("KEREBY_AUTOBOOK", "").strip().lower()
NAVN = os.getenv("ANSOEGER_NAVN", "Philip og Katrine")
MAKS_BOOKINGER_PR_DOEGN = 3

# Kereby's booking (set nov. 2026): "Book fremvisning" -> vindue "Fremvisning" med kalender
# (grå dage = lukket) og tider 10:00-17:00 -> "Næste" -> Navn, Email, Telefonnummer,
# "Evt. kommentar", flueben "Jeg accepterer ..." -> "Bestil fremvisning".

LEDIG_JS = r"""(e) => {
  // Er et kalender-/tidselement synligt og muligt at vælge?
  const r = e.getBoundingClientRect();
  if (r.width === 0 || r.height === 0) return false;
  const st = getComputedStyle(e);
  if (st.visibility === 'hidden' || st.display === 'none' || st.pointerEvents === 'none' || st.cursor === 'not-allowed') return false;
  if (parseFloat(st.opacity) < 0.6) return false;
  if (e.disabled || e.getAttribute('aria-disabled') === 'true' || e.closest('[aria-disabled="true"], [disabled]')) return false;
  const kl = (e.className && e.className.baseVal !== undefined ? e.className.baseVal : e.className) + ' ' + ((e.parentElement && e.parentElement.className) || '');
  if (/disabled|unavailable|inactive|past|outside|other-month|prev-month|next-month|blocked|not-allowed|greyed|grayed/i.test(kl)) return false;
  // grå tekst = lukket dag (lysstyrke af tekstfarven)
  const m = st.color.match(/\d+(\.\d+)?/g);
  if (m) { const [rr, gg, bb] = m.map(Number); if ((0.299*rr + 0.587*gg + 0.114*bb) / 255 > 0.55) return false; }
  return true;
}"""

VAELG_FOERSTE_JS = r"""(root, moenster) => {
  // Klik på det første ledige element i 'root' hvis tekst matcher mønstret (dato eller tid)
  const ledig = __LEDIG__;
  const re = new RegExp(moenster);
  const alle = [...root.querySelectorAll('*')].filter(e => re.test((e.textContent || '').trim()));
  const blade = alle.filter(e => !alle.some(k => k !== e && e.contains(k)));   // inderste element
  for (const e of blade) {
      const mål = e.closest('button, a, td, li, [role=button], [role=gridcell], [role=option]') || e;
      if (!root.contains(mål)) continue;
      if (ledig(e) && ledig(mål)) {
          document.querySelectorAll('[data-boligbot]').forEach(x => x.removeAttribute('data-boligbot'));
          mål.setAttribute('data-boligbot', 'valg');   // Python klikker med en rigtig mus
          return e.textContent.trim();
      }
  }
  return null;
}""".replace("__LEDIG__", LEDIG_JS)

FLUEBEN_JS = r"""(root) => {
  // Sæt flueben i "Jeg accepterer ..." (også når selve boksen er skjult bag et pænt design)
  for (const cb of root.querySelectorAll('input[type=checkbox]')) {
      const ramme = cb.closest('label') || cb.parentElement;
      const tekst = ((ramme && ramme.textContent) || '') + ' ' + ((cb.id && (document.querySelector(`label[for="${cb.id}"]`) || {}).textContent) || '');
      if (!/accepter|privatpolitik|personoplys/i.test(tekst)) continue;
      if (!cb.checked) cb.click();
      if (!cb.checked) { const l = cb.id && document.querySelector(`label[for="${cb.id}"]`); if (l) l.click(); }
      return cb.checked ? 'ok' : 'kunne ikke sætte flueben';
  }
  // Ingen rigtig checkbox: klik på den tegnede boks ved teksten
  const t = [...root.querySelectorAll('*')].find(e => /jeg accepterer/i.test(e.textContent || '') && e.children.length <= 3);
  if (!t) return 'fandt ikke accept-feltet';
  const boks = t.previousElementSibling || t.parentElement.querySelector('span, div, i');
  (boks || t).click();
  return 'klikket (custom boks)';
}"""


def kereby_book(b, send=False):
    """Booker fremvisning på Kereby: første ledige dato og tid.
    send=False: udfylder alt men trykker IKKE 'Bestil fremvisning'.
    Returnerer (lykkedes, forklaring)."""
    from playwright.sync_api import sync_playwright
    trin = []

    def log(tekst):
        trin.append(tekst)
        print(f"  [booking] {tekst}")

    def fejl(tekst):
        log(f"❌ {tekst}")
        return False, " → ".join(trin)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        side = browser.new_context(user_agent=HEADERS["User-Agent"], locale="da-DK",
                                   viewport={"width": 1366, "height": 900}).new_page()
        try:
            side.goto(b["url"], wait_until="domcontentloaded", timeout=45000)
            try:
                side.wait_for_load_state("networkidle", timeout=15000)
            except Exception:  # noqa: BLE001
                pass
            for navn in ("Tillad alle", "Accepter alle", "Accepter", "Godkend alle", "OK"):   # cookie-banner
                k = side.get_by_role("button", name=re.compile(rf"^\s*{navn}", re.I))
                if k.count() and k.first.is_visible():
                    k.first.click()
                    side.wait_for_timeout(500)
                    break
            if re.search(r"\b(Reserveret|Udlejet)\b", side.inner_text("body")[:3000]):
                return fejl("boligen er allerede reserveret")

            # 1) Åbn bookingvinduet
            knapper = side.get_by_text(re.compile(r"^\s*Book fremvisning\s*$", re.I))
            synlig = next((knapper.nth(i) for i in range(knapper.count()) if knapper.nth(i).is_visible()), None)
            if synlig is None:
                return fejl("fandt ingen synlig 'Book fremvisning'-knap")
            synlig.click()
            try:
                side.get_by_text(re.compile(r"Ønsket fremvisningsdato", re.I)).first.wait_for(state="visible", timeout=10000)
            except Exception:  # noqa: BLE001
                return fejl("bookingvinduet med kalender kom ikke frem")
            # Bookingvinduet = inderste boks med både dato-kalender og tidspunkter
            vindue = side.locator("div").filter(has_text=re.compile(r"Ønsket fremvisningsdato", re.I)) \
                         .filter(has_text=re.compile(r"fremvisningstidspunkt", re.I)).last
            log("1 åbnede bookingvinduet")

            # 2) Første ledige dato. Kereby bruger kalenderen "Pikaday":
            #    td.is-disabled = lukket dag, knappen hedder button.pika-day, måned frem = button.pika-next
            dato = None
            for _ in range(3):
                dage = vindue.locator("td:not(.is-disabled):not(.is-empty) button.pika-day:not([disabled])")
                if dage.count():
                    d = dage.first
                    dato = f"{d.get_attribute('data-pika-day')}/{int(d.get_attribute('data-pika-month') or 0) + 1}"
                    d.click()
                    break
                if vindue.locator("button.pika-day").count():     # Pikaday, men ingen ledige dage: næste måned
                    vindue.locator("button.pika-next").first.click()
                    side.wait_for_timeout(800)
                    continue
                # reserve: anden kalender
                tekst = vindue.evaluate(VAELG_FOERSTE_JS, r"^\d{1,2}$")
                if tekst:
                    side.locator("[data-boligbot=valg]").first.click()
                    dato = tekst + "."
                break
            if not dato:
                return fejl("ingen ledig dato i kalenderen")
            side.wait_for_timeout(1500)
            log(f"2 valgte dato {dato}")

            # 3) Første ledige tidspunkt (Kereby: button.jorato-case-detail__booking-pill)
            piller = vindue.locator("button[class*=booking-pill]:not([disabled]):not(.is-disabled)")
            if piller.count():
                tid = piller.first.inner_text().strip()
                piller.first.click()
            else:
                tid = vindue.evaluate(VAELG_FOERSTE_JS, r"^([01]?\d|2[0-3])[:.][0-5]\d$")
                if tid:
                    side.locator("[data-boligbot=valg]").first.click()
            if not tid:
                return fejl("ingen ledige tidspunkter på den dato")
            side.wait_for_timeout(800)
            log(f"3 valgte kl. {tid}")

            # 4) Næste
            # Knappen hedder præcis "Næste" (ikke "Næste måned") og kan ligge uden for kalender-boksen
            naeste_re = re.compile(r"^\s*Næste\s*(→|->)?\s*$", re.I)
            kandidater = [side.get_by_role("button", name=naeste_re), side.get_by_text(naeste_re)]
            naeste = None
            for k in kandidater:
                for i in range(k.count()):
                    if k.nth(i).is_visible():
                        naeste = k.nth(i)
                if naeste is not None:
                    break
            if naeste is None:
                return fejl("fandt ikke 'Næste'-knappen")
            kontakt = side.get_by_text(re.compile(r"Kontaktinformation", re.I)).first
            for forsoeg in range(2):
                try:
                    naeste.click(timeout=5000)
                except Exception:  # noqa: BLE001
                    naeste.click(force=True)
                try:
                    kontakt.wait_for(state="visible", timeout=8000)
                    break
                except Exception:  # noqa: BLE001
                    if forsoeg == 0:   # vælg tiden igen og tryk Næste en gang til
                        if piller.count():
                            piller.first.click()
                            side.wait_for_timeout(800)
                        continue
                    # Fejlfinding: hvad står der i vinduet?
                    try:
                        print("  [booking-debug] Næste-knap:", naeste.evaluate("e => e.outerHTML.slice(0, 300)"))
                        print("  [booking-debug] valgte elementer:", vindue.evaluate(
                            "r => [...r.querySelectorAll('[class*=select], [class*=active], [aria-selected=true], [aria-pressed=true], :checked')]"
                            ".slice(0,8).map(e => e.outerHTML.slice(0,160))"))
                        print("  [booking-debug] tidsknap:", vindue.evaluate(
                            "r => { const e = [...r.querySelectorAll('*')].find(x => /^1[0-7]:00$/.test((x.textContent||'').trim()) && !x.children.length);"
                            " return e ? (e.closest('button,label,li,div') || e).outerHTML.slice(0, 300) : null }"))
                        print("  [booking-debug] datocelle:", vindue.evaluate(
                            "r => { const e = [...r.querySelectorAll('*')].find(x => (x.textContent||'').trim() === '23' && !x.children.length);"
                            " return e ? (e.closest('td,button,div') || e).outerHTML.slice(0, 300) : null }"))
                        print("  [booking-debug] tekst:", vindue.inner_text()[:600].replace("\n", " | "))
                    except Exception as ex:  # noqa: BLE001
                        print("  [booking-debug] kunne ikke læse vinduet:", ex)
                    return fejl("kom ikke videre efter 'Næste' (kontaktformularen kom ikke frem)")
            vindue = side.locator("div").filter(has_text=re.compile(r"Kontaktinformation", re.I)) \
                         .filter(has_text=re.compile(r"Bestil fremvisning", re.I)).last
            log("4 trykkede Næste")

            # 5) Udfyld kontaktoplysninger
            def felt(*muligheder):
                for loc in muligheder:
                    try:
                        if loc.count() and loc.first.is_visible():
                            return loc.first
                    except Exception:  # noqa: BLE001
                        pass
                return None

            navn_f = felt(vindue.get_by_label(re.compile(r"^\s*Navn", re.I)), vindue.get_by_placeholder(re.compile(r"Navn", re.I)),
                          vindue.locator("input[name*=name i]:not([type=email])"))
            mail_f = felt(vindue.locator("input[type=email]"), vindue.get_by_label(re.compile(r"e-?mail", re.I)),
                          vindue.get_by_placeholder(re.compile(r"e-?mail", re.I)), vindue.locator("input[name*=mail i]"))
            tlf_f = felt(vindue.locator("input[type=tel]"), vindue.get_by_label(re.compile(r"Telefon", re.I)),
                         vindue.get_by_placeholder(re.compile(r"Telefon", re.I)), vindue.locator("input[name*=phone i]"))
            kom_f = felt(vindue.locator("textarea"))
            mangler = [n for n, f in (("Navn", navn_f), ("Email", mail_f), ("Telefonnummer", tlf_f)) if f is None]
            if mangler:
                return fejl(f"fandt ikke feltet/felterne: {', '.join(mangler)}")
            navn_f.fill(NAVN)
            mail_f.fill(EMAIL)
            tlf_f.fill(re.sub(r"^\+?45", "", TELEFON.replace(" ", "")))
            if kom_f is not None:
                kom_f.fill(ansoegningstekst(b))
            flueben = vindue.evaluate(FLUEBEN_JS)
            log(f"5 udfyldte navn, email, telefon{', kommentar' if kom_f else ''} · flueben: {flueben}")

            bestil = vindue.get_by_role("button", name=re.compile(r"Bestil fremvisning", re.I))
            if not bestil.count():
                bestil = vindue.get_by_text(re.compile(r"^\s*Bestil fremvisning\s*$", re.I))
            if not bestil.count():
                return fejl("fandt ikke knappen 'Bestil fremvisning'")
            klar = bestil.first.is_enabled()
            if not send:
                log("6 TEST: stoppede før 'Bestil fremvisning' — intet er sendt"
                    + ("" if klar else " (OBS: knappen ser stadig grå/låst ud)"))
                return klar, f"dato {dato} kl. {tid} — " + " → ".join(trin)
            if not klar:
                return fejl("'Bestil fremvisning' er stadig låst — et felt mangler måske")

            # 6) Bestil. Knappen kan ligge under en hvid "fade" nederst i vinduet,
            #    så: rul den frem, prøv et normalt klik, ellers tving klikket igennem.
            knap = bestil.first
            try:
                knap.scroll_into_view_if_needed(timeout=5000)
            except Exception:  # noqa: BLE001
                pass
            try:
                knap.click(timeout=6000)
                log("6 trykkede 'Bestil fremvisning'")
            except Exception:  # noqa: BLE001
                try:
                    knap.click(force=True, timeout=6000)
                    log("6 trykkede 'Bestil fremvisning' (tvunget klik)")
                except Exception:  # noqa: BLE001
                    knap.evaluate("e => e.click()")
                    log("6 trykkede 'Bestil fremvisning' (via siden)")
            side.wait_for_timeout(4000)
            tekst = side.inner_text("body")
            endnu_der = bestil.first.is_visible() if bestil.count() else False
            if re.search(r"\btak\b|modtaget|bekræftet|er booket|er bestilt|bestilling", tekst, re.I) and not endnu_der:
                log("7 bestilt ✅")
                return True, f"{dato} kl. {tid}"
            if not endnu_der:
                log("6 trykkede 'Bestil fremvisning' — vinduet lukkede, men ingen tydelig bekræftelse")
                return True, f"{dato} kl. {tid} (tjek din mail for bekræftelse)"
            return fejl("trykkede 'Bestil fremvisning', men vinduet står der stadig — tjek selv")
        except Exception as ex:  # noqa: BLE001
            return fejl(f"uventet fejl: {str(ex)[:150]}")
        finally:
            browser.close()


def formater(b, status, noter):
    e = html.escape
    ikon = "🟢 MATCH" if status == "groen" else "🟡 NÆSTEN-MATCH"
    adresse = b["adresse"] or b["url"]
    linjer = [
        f"<b>{ikon} — {e(b['kilde'])}</b>",
        f"<b>{e(adresse)}</b>",
        f"{omraade(b['postnr']) or '?'} · {b['m2'] or '?'} m² · {b['vaer'] or '?'} vær.",
        f"Leje {b['leje'] or '?'} kr. + aconto {b['aconto'] if b['aconto'] is not None else '~' + str(ACONTO_SKOEN)} kr."
        f" = <b>{total_leje(b) or '?'} kr./md.</b>",
    ]
    if b["altan"]:
        linjer.append("Altan ✅")
    if b["ledig"]:
        linjer.append(f"Ledig fra {e(b['ledig'])}")
    if noter:
        linjer.append("⚠️ " + e("; ".join(noter)))
    linjer.append(f"\n👉 {e(b['url'])}")

    besked = ansoegningstekst(b)
    linjer.append("\n📋 Tryk på teksten for at kopiere:\n<code>" + e(besked) + "</code>")
    return "\n".join(linjer)


# ---------------------------------------------------------------------
#  HOVEDLØKKE
# ---------------------------------------------------------------------
def indlaes_state():
    if STATE_FIL.exists():
        try:
            return json.loads(STATE_FIL.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"set": {}, "fejl": {}}


def gem_state(state):
    STATE_FIL.write_text(json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")


# Boliger der skal vurderes én gang til (fx efter en rettelse af reglerne).
# Skift GENVURDER_VERSION når listen ændres.
GENVURDER = ["https://cityapartment.dk/cityapartments/bohmensgade-14-3-tv/"]
GENVURDER_VERSION = "2026-11-16c"


def koer(test=False):
    state = indlaes_state()
    if not test and state.get("genvurder_version") != GENVURDER_VERSION:
        for url in GENVURDER:
            state["set"].pop(url, None)
        state["genvurder_version"] = GENVURDER_VERSION
    SETTE_URLS.clear()
    SETTE_URLS.update(state["set"].keys())
    foerste_gang = not state["set"]
    nu = time.strftime("%Y-%m-%d %H:%M")
    fundet_noget = 0

    for navn, liste_fn, detalje_fn in KILDER:
        try:
            boliger = liste_fn()
        except Exception as ex:  # noqa: BLE001
            n = state["fejl"].get(navn, 0) + 1
            state["fejl"][navn] = n
            print(f"{navn}: FEJL {ex}")
            if n == 3 and not test:
                send_telegram(f"⚠️ Boligbot kan ikke hente {navn} (3 gange i træk): {html.escape(str(ex))[:200]}")
            continue

        if not boliger:
            n = state["fejl"].get(navn, 0) + 1
            state["fejl"][navn] = n
            print(f"{navn}: 0 ledige boliger fundet")
            if n == 12 and not test:   # ca. 1 time uden noget
                send_telegram(f"⚠️ Boligbot har ikke fundet en eneste ledig bolig på {navn} i en time. "
                              "Måske er siden ændret — tjek med --test.")
            continue
        state["fejl"][navn] = 0
        print(f"{navn}: {len(boliger)} ledige boliger")

        if navn == "Kereby" and AUTOBOOK == "test" and not test:
            proeve = boliger[0]
            try:
                kereby_detaljer(proeve)
            except Exception:  # noqa: BLE001
                pass
            ok, forklaring = kereby_book(proeve, send=False)
            print(f"  [booking-test på {proeve['url']}] {forklaring}")
            if not state.get("booking_test_sendt_v4"):
                send_telegram(f"🧪 <b>Booking-test på Kereby</b> ({html.escape(proeve['url'])})\n"
                              f"{'✅' if ok else '❌'} {html.escape(forklaring)}\n\nIntet er sendt til Kereby.")
                state["booking_test_sendt_v4"] = True

        for b in boliger:
            if b["url"] in state["set"] and not test:
                continue
            if hurtigt_fravalg(b):
                status, noter = "nej", ["fravalgt på listedata"]
            else:
                if detalje_fn:
                    try:
                        detalje_fn(b)
                        time.sleep(1)
                    except Exception as ex:  # noqa: BLE001
                        print(f"  kunne ikke hente {b['url']}: {ex}")
                        continue   # prøv igen næste gang
                status, noter = vurder(b)

            print(f"  [{status:5}] {b['adresse'] or b['url']} | {b['m2']} m² | {b['vaer']} vær | "
                  f"{b['leje']} + {b['aconto']} kr | postnr {b['postnr']} | {'; '.join(noter)}")

            if status in ("groen", "gul"):
                fundet_noget += 1
                booking_linje = ""
                if b["kilde"] == "Kereby" and status == "groen" and AUTOBOOK in ("til", "test") and not test:
                    idag = time.strftime("%Y-%m-%d")
                    bookinger = state.setdefault("bookinger", {})
                    if AUTOBOOK == "til" and bookinger.get(idag, 0) >= MAKS_BOOKINGER_PR_DOEGN:
                        booking_linje = "⏸ Automatisk booking sprunget over (maks. pr. døgn nået) — book selv!"
                    else:
                        ok, forklaring = kereby_book(b, send=(AUTOBOOK == "til"))
                        print(f"  [booking] {forklaring}")
                        if AUTOBOOK == "til" and ok:
                            bookinger[idag] = bookinger.get(idag, 0) + 1
                            booking_linje = f"✅ FREMVISNING BOOKET AUTOMATISK — {forklaring}"
                        elif AUTOBOOK == "til":
                            booking_linje = f"❗ Automatisk booking mislykkedes ({forklaring}) — BOOK SELV NU!"
                        else:
                            booking_linje = f"🧪 Booking-test: {forklaring}"
                if not test:
                    besked = formater(b, status, noter)
                    if booking_linje:
                        besked = f"<b>{html.escape(booking_linje)}</b>\n\n" + besked
                    send_telegram(besked)
            if not test:
                state["set"][b["url"]] = nu

    if foerste_gang and not test:
        send_telegram(f"✅ Boligbot kører. Første scanning færdig — {fundet_noget} relevante boliger lige nu. "
                      "Fremover får du kun besked om nye.")
    if not test:
        gem_state(state)


if __name__ == "__main__":
    if "--proev-booking" in sys.argv:
        url = sys.argv[sys.argv.index("--proev-booking") + 1]
        b = {"kilde": "Kereby", "url": url, "adresse": None, "altan": False}
        try:
            kereby_detaljer(b)
        except Exception as ex:  # noqa: BLE001
            print("kunne ikke læse detaljer:", ex)
        print(kereby_book(b, send=False))
    elif "--test" in sys.argv:
        koer(test=True)
    elif "--loop" in sys.argv:
        while True:
            try:
                koer()
            except Exception as ex:  # noqa: BLE001
                print("Uventet fejl:", ex)
            time.sleep(120)
    else:
        koer()
