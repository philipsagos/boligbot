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


def render_side(url):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch()
        side = browser.new_page(user_agent=HEADERS["User-Agent"], locale="da-DK")
        side.goto(url, wait_until="networkidle", timeout=60000)
        for _ in range(6):   # scroll for at få alle boliger indlæst
            side.mouse.wheel(0, 4000)
            side.wait_for_timeout(700)
        indhold = side.content()
        browser.close()
    return indhold


def generisk_liste(kilde, url):
    soup = BeautifulSoup(render_side(url), "html.parser")
    vaert = re.match(r"https?://[^/]+", url).group(0)
    boliger = {}
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.startswith("/"):
            href = vaert + href
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
    if not boliger:
        # Hjælp til fejlfinding: vis hvad siden faktisk indeholdt
        print(f"  [debug {kilde}] sidens tekst: {soup.get_text(' ', strip=True)[:1500]}")
        links = sorted({a['href'] for a in soup.find_all('a', href=True)})[:60]
        print(f"  [debug {kilde}] links: {links}")
    return list(boliger.values())


def balder_liste():
    return generisk_liste("Balder", "https://www.balder.dk/lejeboliger")


def heimstaden_liste():
    return generisk_liste("Heimstaden", "https://www.heimstaden.dk/lejeboliger/koebenhavn/")


KILDER = [
    ("Kereby", kereby_liste, kereby_detaljer),
    ("City Apartment", cityapartment_liste, None),
    ("Balder", balder_liste, None),
    ("Heimstaden", heimstaden_liste, None),
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

    besked = ANSOEGNING.format(
        adresse=adresse.split(",")[0] if b["adresse"] else "jeres annonce",
        om_os=("\n" + OM_OS + "\n") if OM_OS else "",
        altan=" Ikke mindst altanen er et stort plus for os." if b["altan"] else "",
        telefon=TELEFON,
        email=EMAIL,
    )
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
                if not test:
                    send_telegram(formater(b, status, noter))
            if not test:
                state["set"][b["url"]] = nu

    if foerste_gang and not test:
        send_telegram(f"✅ Boligbot kører. Første scanning færdig — {fundet_noget} relevante boliger lige nu. "
                      "Fremover får du kun besked om nye.")
    if not test:
        gem_state(state)


if __name__ == "__main__":
    if "--test" in sys.argv:
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
