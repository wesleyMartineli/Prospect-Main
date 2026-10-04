from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import asyncio
import sys
from dataclasses import asdict

# Fix for Playwright on Windows using Uvicorn
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from prospecta import (
    scrape_maps,
    dedup_leads,
    filter_by_rating,
    analyze_all,
    load_cache,
    save_cache,
    Lead
)

app = FastAPI()

# Permitir CORS para que o Next.js possa acessar de qualquer lugar
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class ProspectRequest(BaseModel):
    query: str
    max_results: int = 100
    min_rating: float = 0.0
    headless: bool = True
    no_cache: bool = False

@app.post("/api/scrape")
async def run_scraper(req: ProspectRequest):
    try:
        # Em servidores Linux (Docker/Render), headless precisa ser sempre True
        is_headless = True if sys.platform != "win32" or os.environ.get("RENDER") else req.headless
        leads = await scrape_maps(req.query, req.max_results, is_headless)
        scraped_count = len(leads) if leads else 0
        if not leads:
            return {"error": "", "leads": [], "debug": f"scrape_maps returned 0 leads for query: {req.query}"}
        
        leads = dedup_leads(leads)
        leads = filter_by_rating(leads, req.min_rating)
        
        cache = {} if req.no_cache else load_cache(req.query)
        
        # analyze_all uses ThreadPoolExecutor internally, which is synchronous and blocking.
        # We run it in a threadpool to prevent blocking the FastAPI async event loop.
        loop = asyncio.get_event_loop()
        leads = await loop.run_in_executor(None, analyze_all, leads, cache)
        
        save_cache(req.query, leads)
        
        # Sort by priority
        leads.sort(key=lambda l: (-l.prioridade, l.nome))
        
        return {"leads": [asdict(l) for l in leads], "debug": f"scraped: {scraped_count}, final: {len(leads)}"}
    except Exception as e:
        import traceback
        return {"error": str(e) + "\n" + traceback.format_exc(), "leads": []}

@app.get("/")
def health_check():
    return {"status": "ok", "message": "Prospecta API is running"}

if __name__ == "__main__":
    import uvicorn
    import os
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
