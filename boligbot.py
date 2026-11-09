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
    if b["vaer"] is not None and b["vaer"] < MIN_VAERELSER:
        return True
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

FYLD_FORMULAR_JS = r"""(data) => {
  const synlig = e => { const r = e.getBoundingClientRect(); const st = getComputedStyle(e);
      return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none'; };
  const etiket = e => {
      let t = (e.name || '') + ' ' + (e.id || '') + ' ' + (e.placeholder || '') + ' ' + (e.getAttribute('aria-label') || '');
      if (e.id) { const l = document.querySelector(`label[for="${e.id}"]`); if (l) t += ' ' + l.textContent; }
      const l2 = e.closest('label'); if (l2) t += ' ' + l2.textContent;
      const p = e.parentElement;   // brug kun forælderens tekst hvis den er kort (ellers er det hele formularen)
      if (t.trim().length < 3 && p && p.textContent.length < 120) t += ' ' + p.textContent;
      return t.toLowerCase(); };
  const saet = (e, v) => { const proto = e.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      Object.getOwnPropertyDescriptor(proto, 'value').set.call(e, v);
      e.dispatchEvent(new Event('input', {bubbles: true})); e.dispatchEvent(new Event('change', {bubbles: true})); };
  const rapport = [];
  for (const e of document.querySelectorAll('input, textarea, select')) {
      if (!synlig(e)) continue;
      const t = etiket(e), type = (e.type || '').toLowerCase();
      const r = e.getBoundingClientRect();
      if (r.right < 0 || r.bottom < 0) continue;   // gemt uden for skærmen = fælde
      if (/udfyld ikke|honeypot|leave.*empty/.test(t) || type === 'hidden' || type === 'submit' || type === 'search') continue;
      let v = null;
      if (type === 'checkbox') {
          if (/accept|privat|samtyk|vilkår|betingelse|gdpr|persondata/.test(t)) { if (!e.checked) e.click(); rapport.push('☑ ' + t.slice(0, 60)); }
          else rapport.push('☐ (rørt ikke) ' + t.slice(0, 60));
          continue;
      }
      if (type === 'email' || /mail/.test(t)) v = data.email;
      else if (type === 'tel' || /telefon|phone|mobil|tlf/.test(t)) v = data.telefon;
      else if (e.tagName === 'TEXTAREA' || /besked|message|kommentar/.test(t)) v = data.besked;
      else if (/efternavn|last.?name/.test(t)) v = data.efternavn;
      else if (/fornavn|first.?name/.test(t)) v = data.fornavn;
      else if (/navn|name/.test(t)) v = data.navn;
      if (v !== null) { saet(e, v); rapport.push('✎ ' + t.slice(0, 50).replace(/\s+/g, ' ') + ' = ' + v.slice(0, 30)); }
      else rapport.push('? ukendt felt: ' + type + ' ' + t.slice(0, 60).replace(/\s+/g, ' '));
  }
  return rapport;
}"""

KLIK_TEKST_JS = r"""(regex) => {
  const re = new RegExp(regex, 'i');
  const synlig = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0 && getComputedStyle(e).visibility !== 'hidden'; };
  const kandidater = [...document.querySelectorAll('a, button, input[type=submit], [role=button]')]
      .filter(e => re.test((e.textContent || e.value || '').trim()) && synlig(e));
  if (!kandidater.length) return null;
  const e = kandidater[kandidater.length - 1];
  e.scrollIntoView({block: 'center'}); e.click();
  return (e.textContent || e.value || '').trim().slice(0, 40);
}"""

VAELG_TID_JS = r"""(vaelg) => {
  // Finder synlige, klikbare tidspunkter (fx "16:30", "kl. 17.00") og vælger det første ledige
  const synlig = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0 && getComputedStyle(e).visibility !== 'hidden'; };
  const tid = /\b([01]?\d|2[0-3])[:.][0-5]\d\b/;
  const kandidater = [...document.querySelectorAll('button, a, [role=button], [role=option], label, li, td, div')]
      .filter(e => synlig(e) && e.children.length <= 3 && tid.test(e.textContent || '') && (e.textContent || '').trim().length < 60)
      .filter(e => !e.disabled && e.getAttribute('aria-disabled') !== 'true' && !/disabled|optaget|booked|full|udsolgt/i.test(e.className + ' ' + e.textContent));
  // fjern forældre der blot indeholder et andet tidspunkt
  const blade = kandidater.filter(e => !kandidater.some(k => k !== e && e.contains(k)));
  const tekster = blade.map(e => e.textContent.trim().replace(/\s+/g, ' ')).slice(0, 10);
  if (vaelg && blade.length) {
      const e = blade[0]; e.scrollIntoView({block: 'center'});
      (e.closest('button, a, [role=button], [role=option], label') || e).click();
      const radio = e.querySelector('input[type=radio]') || (e.closest('label') && e.closest('label').querySelector('input[type=radio]'));
      if (radio && !radio.checked) radio.click();
      return {valgt: tekster[0], alle: tekster};
  }
  return {valgt: null, alle: tekster};
}"""


HAR_EMAIL_JS = r"""() => [...document.querySelectorAll('input')].some(e => {
    const r = e.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && (e.type === 'email' || /mail/i.test((e.name||'') + (e.id||'') + (e.placeholder||'')));
})"""
HAR_TIDER_JS = r"""() => /\b([01]?\d|2[0-3])[:.][0-5]\d\b/.test(document.body ? document.body.innerText : '')"""


def _find_formular_ramme(side, sekunder=10):
    """Leder i siden og alle indlejrede rammer (iframes) efter formularen/kalenderen."""
    for _ in range(sekunder * 2):
        for ramme in side.frames:
            try:
                if ramme.evaluate(HAR_EMAIL_JS):
                    return ramme
            except Exception:  # noqa: BLE001
                pass
        side.wait_for_timeout(500)
    # ingen e-mail-felt: tag en ramme med tidspunkter (kalender), ellers hovedsiden
    for ramme in side.frames:
        try:
            if ramme != side.main_frame and ramme.evaluate(HAR_TIDER_JS):
                return ramme
        except Exception:  # noqa: BLE001
            pass
    return side.main_frame


def _beskriv(side):
    """Kort beskrivelse af hvad der er på skærmen — til fejlfinding."""
    dele = []
    try:
        rammer = [f.url[:80] for f in side.frames if f != side.main_frame and f.url not in ("about:blank", "")]
        if rammer:
            dele.append(f"rammer: {rammer[:4]}")
        knapper = side.evaluate(r"""() => [...document.querySelectorAll('a, button, [role=button]')]
            .filter(e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0 && r.top < innerHeight && r.bottom > 0; })
            .map(e => (e.textContent||'').trim().replace(/\s+/g,' ')).filter(t => t && t.length < 40).slice(0, 15)""")
        dele.append(f"synlige knapper: {knapper}")
        dele.append(f"adresse nu: {side.url[:100]}")
    except Exception as ex:  # noqa: BLE001
        dele.append(f"(kunne ikke beskrive siden: {ex})")
    return " | ".join(dele)


def kereby_book(b, send=False):
    """Åbner boligen, trykker 'Book fremvisning', vælger evt. tidspunkt og udfylder formularen.
    Returnerer (lykkedes: bool, forklaring: str)."""
    from playwright.sync_api import sync_playwright
    fornavn, _, efternavn = NAVN.partition(" ")
    data = {"navn": NAVN, "fornavn": fornavn, "efternavn": efternavn or fornavn,
            "email": EMAIL, "telefon": TELEFON, "besked": ansoegningstekst(b)}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        kontekst = browser.new_context(user_agent=HEADERS["User-Agent"], locale="da-DK",
                                       viewport={"width": 1366, "height": 900})
        side = kontekst.new_page()
        try:
            side.goto(b["url"], wait_until="networkidle", timeout=45000)
            side.evaluate(KLIK_TEKST_JS, r"^(accepter|tillad|godkend|ok)( alle)?")   # cookie-banner
            side.wait_for_timeout(800)
            if re.search(r"\b(Reserveret|Udlejet)\b", side.inner_text("body")[:3000]):
                return False, "boligen er allerede reserveret"
            antal_sider = len(kontekst.pages)
            if not side.evaluate(KLIK_TEKST_JS, r"book fremvisning"):
                return False, "fandt ingen 'Book fremvisning'-knap. " + _beskriv(side)
            side.wait_for_timeout(2500)
            if len(kontekst.pages) > antal_sider:       # åbnede i nyt vindue/fane
                side = kontekst.pages[-1]
                side.wait_for_load_state("networkidle", timeout=30000)
                print(f"  [booking] åbnede nyt vindue: {side.url}")
            try:
                side.wait_for_load_state("networkidle", timeout=10000)
            except Exception:  # noqa: BLE001
                pass

            ramme = _find_formular_ramme(side)
            # Ingen formular/kalender endnu? Tryk "Book fremvisning" igen inde i den nye ramme/det nye vindue
            if not ramme.evaluate(HAR_EMAIL_JS) and not ramme.evaluate(HAR_TIDER_JS):
                for f in [f for f in side.frames if f != side.main_frame] + [side.main_frame]:
                    try:
                        if f.evaluate(KLIK_TEKST_JS, r"book fremvisning|book en tid|vælg tid|se ledige tider"):
                            print(f"  [booking] trykkede igen i {f.url[:80]}")
                            side.wait_for_timeout(2500)
                            break
                    except Exception:  # noqa: BLE001
                        pass
                ramme = _find_formular_ramme(side, sekunder=6)
            if ramme != side.main_frame:
                print(f"  [booking] formularen ligger i en indlejret ramme: {ramme.url[:100]}")

            # Kalender? Vælg første ledige tidspunkt (at vælge en tid sender intet — det gør kun "bekræft")
            tider = ramme.evaluate(VAELG_TID_JS, True)
            valgt_tid = tider.get("valgt")
            if tider.get("alle"):
                print(f"  [booking] tidspunkter set: {tider['alle']}" + (f" -> valgte {valgt_tid}" if valgt_tid else ""))
                if valgt_tid:
                    side.wait_for_timeout(1500)
                    ramme.evaluate(KLIK_TEKST_JS, r"^(næste|videre|fortsæt|vælg|continue|next)$")
                    side.wait_for_timeout(1500)
                    ramme = _find_formular_ramme(side, sekunder=6)

            rapport = ramme.evaluate(FYLD_FORMULAR_JS, data)
            print("  [booking] felter:\n    " + "\n    ".join(rapport or ["(ingen synlige felter)"]))
            if not any(r.startswith("✎") and "@" in r for r in rapport):
                beskrivelse = _beskriv(side)
                print(f"  [booking] skærmen efter klik: {side.inner_text('body')[:1000]!r}")
                return False, "fandt ikke formularen (intet e-mail-felt). " + beskrivelse
            if not send:
                knap = ramme.evaluate(r"""() => [...document.querySelectorAll('button, input[type=submit]')]
                    .filter(e => e.getBoundingClientRect().width > 0).map(e => (e.textContent||e.value).trim()).filter(Boolean).slice(-6)""")
                tidstekst = f" Ledige tidspunkter: {tider['alle'][:5]}." if tider.get("alle") else " Ingen kalender fundet."
                return True, f"TEST: formularen er udfyldt men IKKE sendt.{tidstekst} Synlige knapper: {knap}"
            klikket = ramme.evaluate(KLIK_TEKST_JS, r"^(send|book|bekræft|anmod|indsend|tilmeld)")
            if not klikket:
                ramme.evaluate("() => { const f = [...document.querySelectorAll('form')].find(f => f.querySelector('input[type=email]')); if (f) f.requestSubmit(); }")
                klikket = "(formularen sendt)"
            side.wait_for_timeout(4000)
            tekst = " ".join(f.evaluate("() => document.body ? document.body.innerText : ''") for f in side.frames)
            if re.search(r"tak for|modtaget|bekræft|booket|vi kontakter|vi vender tilbage|sendt", tekst, re.I):
                return True, (f"tid: {valgt_tid}" if valgt_tid else f"forespørgsel sendt ('{klikket}')")
            return False, f"trykkede '{klikket}', men så ingen bekræftelse — tjek selv"
        except Exception as ex:  # noqa: BLE001
            return False, f"fejl: {str(ex)[:150]}"
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


def koer(test=False):
    state = indlaes_state()
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
            if not state.get("booking_test_sendt_v2"):
                send_telegram(f"🧪 <b>Booking-test på Kereby</b> ({html.escape(proeve['url'])})\n"
                              f"{'✅' if ok else '❌'} {html.escape(forklaring)}\n\nIntet er sendt til Kereby.")
                state["booking_test_sendt_v2"] = True

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
