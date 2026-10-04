# prospecta

Scraper de leads via Google Maps com análise de qualidade de site. Útil pra prospecção em qualquer nicho local (contabilidade, advocacia, clínicas, etc.).

## O que faz

1. Busca no Google Maps por uma query (ex: `"contabilidade Curitiba"`).
2. Extrai nome, telefone, site, rating, endereço, categoria.
3. Analisa cada site e classifica em buckets de prioridade.
4. Salva CSV ordenado por prioridade.

## Buckets de prioridade

| Status | Quando |
|---|---|
| `SEM_SITE` (100) | sem site no Maps |
| `LINK_SOCIAL` (95) | usa Instagram/WhatsApp/Facebook ou template no-code (Wix gratuito, Lovable, business.site) |
| `DOMINIO_PARKADO` (92) | domínio à venda |
| `SITE_QUEBRADO` (90) | HTTP error, SSL, timeout |
| `SITE_RUIM` (50) | ≥2 issues (sem HTTPS, não responsivo, página pequena, layout antigo) |
| `SITE_OK` (10) | resto |

Maior prioridade = melhor lead pra abordar oferecendo desenvolvimento de site.

### Como cada bucket é detectado

**`SEM_SITE`** — campo "site" vazio no perfil do Google Maps.

**`LINK_SOCIAL`** — domínio do site bate com:
- Redes sociais: `instagram.com`, `wa.me`, `whatsapp.com`, `api.whatsapp.com`, `facebook.com`, `linktr.ee`, `linkedin.com`, `beacons.ai`.
- Plataformas amadoras / no-code: `sites.google.com`, `business.site`, `negocio.site`, `wixsite.com` (Wix gratuito), `webnode.com.br`, `site123.me`, `lovable.app`, `vitrinevirtual.net`.

**`DOMINIO_PARKADO`** — HTML contém frases tipo `"this domain is for sale"`, `"buy this domain"`, `"hugedomains"`, `"sedo.com"`, `"este domínio está à venda"`.

**`SITE_QUEBRADO`** — HTTP ≥400 (após fallback Chrome UA → Googlebot UA → cloudscraper), `ConnectionError`, `SSLError`, `ReadTimeout`.

**`SITE_RUIM`** — site abre, mas tem **≥2 sinais de baixa qualidade**:
- `sem HTTPS` — URL final não é `https://`
- `nao responsivo` — HTML não tem `<meta name="viewport">`
- `pagina muito pequena` — corpo da resposta < 30 KB (provavelmente página única, sem conteúdo real)
- `template generico` — HTML cita `wix.com`, `webnode` ou `site123` (template no domínio próprio)
- `layout antigo (tabelas)` — usa `<table>` para layout e tem menos de 20 `<div>` no total
- `site antigo (~ano)` — copyright extraído do HTML OU primeiro snapshot na Wayback Machine < 2020

**`SITE_OK`** — resto. Pode ter até 1 issue (ex: só "sem HTTPS") e ainda cair aqui. O campo `motivo` lista quais issues foram encontradas.

> Anos no copyright vindos de `new Date().getFullYear()` (data dinâmica) são filtrados — o ano corrente é ignorado pra evitar falso positivo de "site novo".

## Setup

```bash
pip install -r requirements.txt
playwright install chromium
```

## Uso

```bash
python prospecta.py "contabilidade Curitiba"
python prospecta.py "advocacia Porto Alegre" --max 300 --headless --min-rating 3.5
```

Flags:
- `--max N` — máximo de resultados (default 200)
- `--headless` — Chromium em background
- `--min-rating 3.5` — filtra por rating mínimo no Maps
- `--no-cache` — ignora cache local (TTL 30 dias em `.cache/`)

Saída: `leads_<query>.csv` ordenado por prioridade, com campos como `wa_numero` (WhatsApp pronto pra colar) e `ig_link` (perfil real quando disponível, busca por nome senão).
