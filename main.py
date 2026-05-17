import asyncio
import re
from contextlib import asynccontextmanager
from typing import Optional, List
from fastapi import FastAPI, HTTPException
from fastapi.openapi.utils import get_openapi
from pydantic import BaseModel, Field
from cloakbrowser import launch_async
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

browser_instance = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global browser_instance
    print("[INFO] Initializing CloakBrowser Core Engine (Anti-Popup & Performance Optimizations)...")
    try:
        browser_instance = await launch_async(
            headless=True,
            humanize=True,
            human_preset="default"
        )
        print("[INFO] CloakBrowser engine initialized successfully.")
    except Exception as e:
        print(f"[CRITICAL] Failed to initialize CloakBrowser engine: {e}")
        raise e
    yield
    if browser_instance:
        print("[INFO] Shutting down CloakBrowser engine smoothly...")
        await browser_instance.close()

app = FastAPI(
    title="CloakBrowser Stream Extractor API",
    version="1.0.0",
    description="High-performance API to extract M3U8 and MP4 streaming links from heavily protected sites.",
    lifespan=lifespan
)

# --- REQUEST & RESPONSE MODELS ---
class ExtractorRequest(BaseModel):
    url: str = Field(..., description="The target streaming website URL to scan.")
    proxy: Optional[str] = Field(None, description="Optional proxy connection string (e.g., socks5://host:port).")
    timeout: Optional[int] = Field(15, description="Maximum wait time in seconds.")
    upload_pastebin: bool = Field(False, description="Upload hidden M3U8 blob contents to Pastebin.")
    fast_exit: bool = Field(True, description="Return immediately once the first stream is found.")

class BatchExtractorRequest(BaseModel):
    urls: List[str] = Field(..., description="List of target URLs to scan concurrently.")
    proxy: Optional[str] = Field(None, description="Global proxy string.")
    timeout: Optional[int] = Field(15, description="Maximum wait time per URL.")
    upload_pastebin: bool = Field(False, description="Upload hidden contents to Pastebin.")
    fast_exit: bool = Field(True, description="Return immediately upon finding streams.")

class VideoStream(BaseModel):
    type: str = Field(..., description="Type of stream (M3U8, MP4, or M3U8_HIDDEN).")
    url: str = Field(..., description="Direct media stream URL.")
    pastebin_raw: Optional[str] = Field(None, description="Pastebin raw text URL if uploaded.")
    content: Optional[str] = Field(None, description="Preview of the raw M3U8 payload.")

# --- HELPERS ---
async def upload_to_centos_paste(context, text_content: str) -> str:
    try:
        resp = await context.request.post(
            "https://paste.centos.org/",
            form={"name": "api-extractor", "title": "stream", "lang": "text", "code": text_content, "expire": "120", "submit": "submit"}
        )
        html = await resp.text()
        match = re.search(r'<a class="control" href="([^"]*?)">View Raw</a>', html)
        if match:
            raw_uri = match.group(1)
            return raw_uri if raw_uri.startswith("http") else f"https://paste.centos.org{raw_uri}"
    except Exception:
        pass
    return ""

async def force_play_videos(page):
    js_code = """() => {
        document.querySelectorAll('video, audio').forEach(v => {
            if (v.paused) {
                v.muted = true;
                const p = v.play();
                if (p !== undefined) p.catch(() => {});
            }
        });
    }"""
    try:
        await page.evaluate(js_code)
        for frame in page.frames:
            try: await frame.evaluate(js_code)
            except: pass
    except: pass

async def block_unnecessary_resources(route, request):
    if request.resource_type in ["image", "stylesheet", "font"]:
        await route.abort()
    else:
        await route.continue_()

# --- CORE ENGINE ---
async def process_single_url(url: str, proxy: Optional[str], timeout: int, upload_pastebin: bool, fast_exit: bool):
    global browser_instance
    detected_streams: List[VideoStream] = []
    
    context_options = {}
    if proxy:
        context_options["proxy"] = {"server": proxy}
        context_options["geoip"] = True

    try:
        context = await browser_instance.new_context(**context_options)
        page = await context.new_page()
        
        # Anti-Popup / Ad-blocker Engine
        async def on_new_page(new_page):
            if new_page != page:
                try:
                    await new_page.close()
                    print(f"[POPUP BLOCKED] Destroyed an intrusive ad tab.")
                except Exception:
                    pass
        context.on("page", on_new_page)

        await page.route("**/*", block_unnecessary_resources)

        async def handle_response(response):
            try:
                if response.request.method == "OPTIONS": return
                url_str, status = response.url.lower(), response.status
                
                if ".m3u8" in url_str or ".m3u" in url_str:
                    if not any(s.url == response.url for s in detected_streams):
                        detected_streams.append(VideoStream(type="M3U8", url=response.url))
                        
                elif ".mp4" in url_str:
                    if not any(s.url == response.url for s in detected_streams):
                        detected_streams.append(VideoStream(type="MP4", url=response.url))

                elif status == 200 and "mpegurl" in response.headers.get("content-type", "").lower():
                    body_bytes = await response.body()
                    text_content = body_bytes.decode("utf-8", errors="ignore")
                    if "#EXTM3U" in text_content and not any(s.content and s.content[:50] == text_content[:50] for s in detected_streams):
                        paste_link = await upload_to_centos_paste(context, text_content) if upload_pastebin else None
                        detected_streams.append(VideoStream(type="M3U8_HIDDEN", url="blob_or_hidden", pastebin_raw=paste_link, content=text_content[:150]))
            except Exception:
                pass

        page.on("response", handle_response)
        
        print(f"[PROCESS] Connecting to target: {url}")
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
        except PlaywrightTimeoutError:
            print(f"[WARN] Navigation timeout for {url}. Switching to passive sniffing...")
        except Exception as e:
            await context.close()
            return {"status": "error", "target": url, "message": str(e)}

        # Active Clickjacking Bypass Loop
        for _ in range(timeout):
            if fast_exit and len(detected_streams) > 0:
                print(f"[FAST-EXIT] Stream captured for {url}.")
                break
                
            await force_play_videos(page)
            
            viewport = page.viewport_size
            if viewport:
                cx, cy = viewport["width"] / 2, viewport["height"] / 2
                try:
                    await page.mouse.click(cx, cy)
                    await asyncio.sleep(0.1)
                    await page.mouse.click(cx, cy)
                except Exception:
                    pass
            
            await asyncio.sleep(0.9)

        await page.close()
        await context.close()

        return {"status": "success", "target": url, "total_found": len(detected_streams), "streams": detected_streams}

    except Exception as e:
        if 'context' in locals(): await context.close()
        return {"status": "error", "target": url, "message": str(e)}

# --- ENDPOINTS ---
@app.post("/api/v1/extract", tags=["Single Scraper"])
async def extract_one(payload: ExtractorRequest):
    if not browser_instance: raise HTTPException(status_code=500, detail="Browser error")
    return await process_single_url(payload.url, payload.proxy, payload.timeout, payload.upload_pastebin, payload.fast_exit)

@app.post("/api/v1/extract-batch", tags=["Batch Scraper"])
async def extract_batch(payload: BatchExtractorRequest):
    if not browser_instance: raise HTTPException(status_code=500, detail="Browser error")
    tasks = [process_single_url(u, payload.proxy, payload.timeout, payload.upload_pastebin, payload.fast_exit) for u in payload.urls]
    results = await asyncio.gather(*tasks)
    return {"status": "success", "total_urls": len(payload.urls), "results": results}

def custom_openapi():
    if app.openapi_schema: return app.openapi_schema
    app.openapi_schema = get_openapi(
        title="CloakBrowser M3U8 Extractor API", version="1.0.0",
        description="Production API Documentation for Stream Extraction", routes=app.routes,
    )
    return app.openapi_schema
app.openapi = custom_openapi