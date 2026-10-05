"""
Prospecta contabilidades via Google Maps.

Uso:
    python prospecta.py "contabilidade Curitiba"
    python prospecta.py "contabilidade Curitiba" --max 300 --headless --min-rating 3.5

Saida: leads_<query>.csv ordenado por prioridade.
"""

import argparse
import asyncio
import csv
import datetime
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict
from urllib.parse import quote_plus, urlparse

import requests
from playwright.async_api import async_playwright

try:
    import cloudscraper
    _scraper = cloudscraper.create_scraper(browser={"browser": "chrome", "platform": "windows", "desktop": True})
    HAS_CLOUDSCRAPER = True
except ImportError:
    _scraper = None
    HAS_CLOUDSCRAPER = False

REQUEST_TIMEOUT = 4
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
GOOGLEBOT_UA = "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
CACHE_DIR = ".cache"
CACHE_TTL_DAYS = 30
THIS_YEAR = datetime.datetime.now().year
OLD_SITE_THRESHOLD = 2020

SOCIAL_DOMAINS = {
    "instagram.com": "Instagram",
    "instagr.am": "Instagram",
    "wa.me": "WhatsApp",
    "whatsapp.com": "WhatsApp",
    "api.whatsapp.com": "WhatsApp",
    "facebook.com": "Facebook",
    "fb.com": "Facebook",
    "m.facebook.com": "Facebook",
    "linktr.ee": "Linktree",
    "linktree.com": "Linktree",
    "linkedin.com": "LinkedIn",
    "beacons.ai": "Beacons",
}

AMATEUR_DOMAINS = {
    "sites.google.com": "Google Sites",
    "business.site": "Site automatico Google",
    "negocio.site": "Site automatico Google",
    "wixsite.com": "Wix gratuito",
    "webnode.com.br": "Webnode gratuito",
    "webnode.page": "Webnode gratuito",
    "site123.me": "Site123 gratuito",
    "lovable.app": "Lovable (no-code)",
    "vitrinevirtual.net": "Vitrine Virtual (template)",
}

EMAIL_BLACKLIST = [
    "sentry.io", "wixpress", "@example", "@email.com", "@dominio",
    "@2x.png", ".png", ".jpg", ".gif", ".webp", ".svg",
    "info@mysite.com", "exemplo@", "@exemplo.", "@mysite.",
    "training.com", "@email.", "noreply@", "no-reply@",
    "contato@dominio", "seu-email@", "seuemail@",
]

PARKING_PHRASES = [
    "this domain is for sale",
    "domain is for sale",
    "buy this domain",
    "domain for sale",
    "este dominio esta a venda",
    "este domínio está à venda",
    "domínio à venda",
    "parked domain",
    "godaddy.com/domainfind",
    "hugedomains",
    "sedo.com",
]

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
COPYRIGHT_RE = re.compile(r"(?:©|&copy;|copyright)[^\d]{0,30}(\d{4})", re.I)


def detect_domain(url: str, table: dict) -> str:
    if not url:
        return ""
    host = urlparse(url).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    for domain, name in table.items():
        if host == domain or host.endswith("." + domain):
            return name
    return ""


def format_wa_numero(phone: str) -> str:
    if not phone:
        return ""
    digits = re.sub(r"\D", "", phone)
    if not digits:
        return ""
    if not digits.startswith("55"):
        digits = "55" + digits
    return digits


def extract_ig_username(url: str) -> str:
    if not url:
        return ""
    host = urlparse(url).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    if host != "instagram.com" and host != "instagr.am" and not host.endswith(".instagram.com"):
        return ""
    path = urlparse(url).path.strip("/")
    if not path:
        return ""
    first = path.split("/")[0]
    if first in ("p", "explore", "reel", "reels", "stories", "tv", "accounts", "direct"):
        return ""
    return first


def ig_link_for(nome: str, site: str) -> str:
    username = extract_ig_username(site)
    if username:
        return f"https://www.instagram.com/{username}/"
    if not nome:
        return ""
    return f"https://www.instagram.com/explore/search/keyword/?q={quote_plus(nome)}"


def fetch_wayback_year(url: str) -> int:
    try:
        r = requests.get(
            "https://archive.org/wayback/available",
            params={"url": url, "timestamp": "20000101"},
            timeout=REQUEST_TIMEOUT,
        )
        data = r.json()
        ts = data.get("archived_snapshots", {}).get("closest", {}).get("timestamp", "")
        if ts and len(ts) >= 4:
            return int(ts[:4])
    except Exception:
        pass
    return 0


def extract_copyright_year(html: str) -> int:
    years = [int(m.group(1)) for m in COPYRIGHT_RE.finditer(html)]
    # Exclui ano corrente: copyrights dinamicos (new Date().getFullYear()) sao falsos positivos
    years = [y for y in years if 1995 <= y < THIS_YEAR]
    return max(years) if years else 0


def extract_email(html: str) -> str:
    for m in EMAIL_RE.finditer(html):
        email = m.group(0)
        low = email.lower()
        if any(skip in low for skip in EMAIL_BLACKLIST):
            continue
        return email
    return ""


@dataclass
class Lead:
    nome: str = ""
    telefone: str = ""
    site: str = ""
    rating: str = ""
    reviews: str = ""
    endereco: str = ""
    categoria: str = ""
    status: str = ""
    motivo: str = ""
    prioridade: int = 0
    ano_site: int = 0
    email: str = ""
    wa_numero: str = ""
    ig_link: str = ""


# ---------- cache ----------

def _cache_path(query: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", query.lower()).strip("_")
    return os.path.join(CACHE_DIR, f"{slug}.json")


def load_cache(query: str) -> dict:
    path = _cache_path(query)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if time.time() - data.get("_ts", 0) > CACHE_TTL_DAYS * 86400:
            return {}
        return data.get("leads", {})
    except Exception:
        return {}


def save_cache(query: str, leads: list):
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = _cache_path(query)
    payload = {
        "_ts": time.time(),
        "leads": {l.nome: asdict(l) for l in leads if l.nome},
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


# ---------- scrape ----------

async def scrape_maps(query: str, max_results: int, headless: bool) -> list[Lead]:
    print(f"[1/3] Abrindo Google Maps: '{query}' (headless={headless})")
    leads: list[Lead] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=headless,
            args=["--disable-dev-shm-usage", "--no-sandbox", "--disable-gpu", "--blink-settings=imagesEnabled=false"]
        )
        ctx = await browser.new_context(
            user_agent=USER_AGENT,
            locale="pt-BR",
            viewport={"width": 1280, "height": 900},
        )
        page = await ctx.new_page()

        # Bloquear imagens e fontes para carregar 4x mais rapido e gastar muito menos memoria
        async def block_heavy_assets(route):
            try:
                if route.request.resource_type in ["image", "media", "font"]:
                    await route.abort()
                else:
                    await route.continue_()
            except Exception:
                pass

        await page.route("**/*", block_heavy_assets)

        await page.goto(
            f"https://www.google.com/maps/search/{query.replace(' ', '+')}",
            wait_until="domcontentloaded",
            timeout=45000
        )
        await page.wait_for_timeout(1500)

        try:
            buttons = page.locator('button:has-text("Aceitar tudo"), button:has-text("Accept all"), button:has-text("Concordo"), button:has-text("I agree"), form[action*="consent"] button')
            if await buttons.count() > 0:
                await buttons.first.click(timeout=1500)
                await page.wait_for_timeout(1000)
        except Exception:
            pass

        try:
            await page.wait_for_selector('[role="feed"], div[role="main"]', timeout=15000)
        except Exception:
            pass

        feed = page.locator('[role="feed"]')
        prev_count = 0
        stagnant_rounds = 0

        while True:
            cards = await feed.locator('a[href*="/maps/place/"]').all()
            count = len(cards)
            print(f"   carregados: {count}", end="\r")

            if count >= max_results:
                break
            if count == prev_count:
                stagnant_rounds += 1
                if stagnant_rounds >= 3:
                    break
            else:
                stagnant_rounds = 0
            prev_count = count

            await feed.evaluate("el => el.scrollBy(0, 2000)")
            await page.wait_for_timeout(800)

        print(f"   total coletado: {count}            ")

        cards = await feed.locator('a[href*="/maps/place/"]').all()
        cards = cards[:max_results]

        print(f"[2/3] Extraindo dados de {len(cards)} lugares")

        for i, card in enumerate(cards, 1):
            try:
                await card.click()
                await page.wait_for_timeout(600)

                lead = Lead()
                lead.nome = (await card.get_attribute("aria-label")) or ""

                # Rating: 2a linha do innerText do card pai (ex: "Nome\n5,0\n...")
                parent_text = await card.evaluate("el => el.parentElement && el.parentElement.innerText") or ""
                mr = re.search(r"(?:^|\n)\s*(\d[,\.]\d)\s*(?:\n|$)", parent_text)
                if mr:
                    lead.rating = mr.group(1).replace(",", ".")

                panel = page.locator('div[role="main"]').last

                # Reviews e Rating fallback do painel
                panel_text = (await panel.inner_text()) or ""
                if not lead.rating:
                    mr_panel = re.search(r"(\d[,\.]\d)\s*(?:estrelas|\u2605|\n|\()", panel_text, re.I)
                    if mr_panel:
                        lead.rating = mr_panel.group(1).replace(",", ".")

                mn = re.search(r"\d[,\.]\d\s*\n?\s*\(\s*([\d.,]+)\s*\)", panel_text)
                if mn:
                    lead.reviews = re.sub(r"[.,\s]", "", mn.group(1))

                addr = panel.locator('button[data-item-id="address"]')
                if await addr.count() > 0:
                    txt = await addr.first.get_attribute("aria-label") or ""
                    lead.endereco = txt.replace("Endereço: ", "").strip()

                phone = panel.locator('button[data-item-id^="phone"]')
                if await phone.count() > 0:
                    txt = await phone.first.get_attribute("aria-label") or ""
                    lead.telefone = txt.replace("Telefone: ", "").strip()

                site = panel.locator('a[data-item-id="authority"]')
                if await site.count() > 0:
                    href = await site.first.get_attribute("href") or ""
                    lead.site = href

                cat = panel.locator('button[jsaction*="category"]').first
                if await cat.count() > 0:
                    lead.categoria = (await cat.text_content() or "").strip()

                leads.append(lead)
                print(f"   [{i}/{len(cards)}] {lead.nome[:50]}", end="\r")
            except Exception as e:
                print(f"\n   ! erro no item {i}: {e}")
                continue

        print(f"\n   {len(leads)} leads extraidos                       ")
        await browser.close()

    return leads


# ---------- analise ----------

def analyze_site(lead: Lead) -> Lead:
    lead.wa_numero = format_wa_numero(lead.telefone)
    lead.ig_link = ig_link_for(lead.nome, lead.site)

    if not lead.site:
        lead.status = "SEM_SITE"
        lead.motivo = "nao tem site cadastrado no Maps"
        lead.prioridade = 100
        return lead

    social = detect_domain(lead.site, SOCIAL_DOMAINS)
    if social:
        lead.status = "LINK_SOCIAL"
        lead.motivo = f"usa {social} como site"
        lead.prioridade = 95
        return lead

    amateur = detect_domain(lead.site, AMATEUR_DOMAINS)
    if amateur:
        lead.status = "LINK_SOCIAL"
        lead.motivo = f"usa {amateur}"
        lead.prioridade = 95
        return lead

    r = None
    last_err = None
    for ua in (USER_AGENT, GOOGLEBOT_UA):
        try:
            r = requests.get(
                lead.site,
                timeout=REQUEST_TIMEOUT,
                headers={"User-Agent": ua},
                allow_redirects=True,
            )
            if r.status_code in (403, 406, 429):
                continue
            break
        except requests.RequestException as e:
            last_err = e

    # Ultimo fallback: cloudscraper para contornar Cloudflare
    if HAS_CLOUDSCRAPER and (r is None or r.status_code in (403, 406, 429, 503)):
        try:
            r2 = _scraper.get(lead.site, timeout=REQUEST_TIMEOUT * 2, allow_redirects=True)
            if r2.status_code < 400:
                r = r2
        except Exception as e:
            if r is None:
                last_err = e

    if r is None:
        lead.status = "SITE_QUEBRADO"
        lead.motivo = f"falha ao carregar: {type(last_err).__name__}"
        lead.prioridade = 90
        return lead

    if r.status_code >= 400:
        lead.status = "SITE_QUEBRADO"
        lead.motivo = f"HTTP {r.status_code}"
        lead.prioridade = 90
        return lead

    html_lower = r.text.lower()
    html_raw = r.text

    if any(p in html_lower for p in PARKING_PHRASES):
        lead.status = "DOMINIO_PARKADO"
        lead.motivo = "dominio parkado / a venda"
        lead.prioridade = 92
        return lead

    issues = []

    parsed = urlparse(r.url)
    if parsed.scheme != "https":
        issues.append("sem HTTPS")
    if "viewport" not in html_lower:
        issues.append("nao responsivo")
    if len(r.content) < 30_000:
        issues.append("pagina muito pequena")
    if any(t in html_lower for t in ["wix.com", "webnode", "site123"]):
        issues.append("template generico")
    if "<table" in html_lower and "</table>" in html_lower and html_lower.count("<div") < 20:
        issues.append("layout antigo (tabelas)")

    copyright_year = extract_copyright_year(html_raw)
    wayback_year = fetch_wayback_year(lead.site)
    ano = max(copyright_year, wayback_year)
    lead.ano_site = ano
    if ano and ano < OLD_SITE_THRESHOLD:
        issues.append(f"site antigo (~{ano})")

    lead.email = extract_email(html_raw)

    if len(issues) >= 2:
        lead.status = "SITE_RUIM"
        lead.motivo = ", ".join(issues)
        lead.prioridade = 50
    else:
        lead.status = "SITE_OK"
        lead.motivo = ", ".join(issues) if issues else "ok"
        lead.prioridade = 10

    return lead


def analyze_all(leads: list[Lead], cache: dict) -> list[Lead]:
    todo = [l for l in leads if l.nome not in cache]
    cached_results = []
    for l in leads:
        if l.nome in cache:
            data = cache[l.nome]
            l.wa_numero = format_wa_numero(l.telefone)
            l.ig_link = ig_link_for(l.nome, data.get("site", l.site))
            for k in ["site", "status", "motivo", "prioridade", "ano_site", "email"]:
                if k in data:
                    setattr(l, k, data[k])
            cached_results.append(l)

    print(f"[3/3] Analisando {len(todo)} sites ({len(cached_results)} de cache)")
    results = list(cached_results)
    if todo:
        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = {pool.submit(analyze_site, lead): lead for lead in todo}
            for i, fut in enumerate(as_completed(futures), 1):
                results.append(fut.result())
                print(f"   {i}/{len(todo)}", end="\r")
    print(f"   {len(results)} analisados            ")
    return results


# ---------- output ----------

def save_csv(leads: list[Lead], query: str) -> str:
    leads.sort(key=lambda l: (-l.prioridade, l.nome))
    slug = re.sub(r"[^a-z0-9]+", "_", query.lower()).strip("_")
    path = f"leads_{slug}.csv"
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(asdict(Lead()).keys()))
        w.writeheader()
        for lead in leads:
            w.writerow(asdict(lead))
    return path


def summary(leads: list[Lead]):
    buckets = {}
    for l in leads:
        buckets[l.status] = buckets.get(l.status, 0) + 1
    print("\nResumo:")
    for k in ["SEM_SITE", "LINK_SOCIAL", "DOMINIO_PARKADO", "SITE_QUEBRADO", "SITE_RUIM", "SITE_OK"]:
        if k in buckets:
            print(f"  {k:20s} {buckets[k]:4d}")


def filter_by_rating(leads: list[Lead], min_rating: float) -> list[Lead]:
    if min_rating <= 0:
        return leads
    out = []
    for l in leads:
        try:
            r = float((l.rating or "0").replace(",", "."))
        except ValueError:
            r = 0.0
        if r >= min_rating:
            out.append(l)
    return out


def dedup_leads(leads: list[Lead]) -> list[Lead]:
    """Remove duplicatas: mesmo nome OU (mesmo telefone + site) OU (mesmo telefone + endereco)."""
    seen_nomes = set()
    seen_pairs = set()
    seen_phone_addr = set()
    out = []
    for l in leads:
        key_nome = (l.nome or "").strip().lower()
        key_pair = (l.telefone, l.site) if l.telefone and l.site else None
        addr_norm = re.sub(r"\s+", " ", (l.endereco or "").strip().lower())
        key_pa = (l.telefone, addr_norm) if l.telefone and addr_norm else None
        if key_nome and key_nome in seen_nomes:
            continue
        if key_pair and key_pair in seen_pairs:
            continue
        if key_pa and key_pa in seen_phone_addr:
            continue
        if key_nome:
            seen_nomes.add(key_nome)
        if key_pair:
            seen_pairs.add(key_pair)
        if key_pa:
            seen_phone_addr.add(key_pa)
        out.append(l)
    return out


def main():
    parser = argparse.ArgumentParser(description="Prospecta contabilidades via Google Maps")
    parser.add_argument("query", help="ex: 'contabilidade Curitiba'")
    parser.add_argument("--max", type=int, default=200, help="maximo de resultados (default 200)")
    parser.add_argument("--headless", action="store_true", help="rodar Chromium em background")
    parser.add_argument("--min-rating", type=float, default=0.0, help="rating minimo no Maps (ex: 3.5)")
    parser.add_argument("--no-cache", action="store_true", help="ignorar cache local")
    args = parser.parse_args()

    leads = asyncio.run(scrape_maps(args.query, args.max, args.headless))
    if not leads:
        print("Nenhum lead extraido.")
        sys.exit(1)

    before = len(leads)
    leads = dedup_leads(leads)
    if len(leads) < before:
        print(f"Removidas {before - len(leads)} duplicatas: {len(leads)} leads unicos")

    leads = filter_by_rating(leads, args.min_rating)
    if args.min_rating > 0:
        print(f"Apos filtro de rating >= {args.min_rating}: {len(leads)} leads")

    cache = {} if args.no_cache else load_cache(args.query)
    leads = analyze_all(leads, cache)
    save_cache(args.query, leads)

    path = save_csv(leads, args.query)
    summary(leads)
    print(f"\nCSV salvo: {path}")


if __name__ == "__main__":
    main()
