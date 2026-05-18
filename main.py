import asyncio
import re
import random
from urllib.parse import urlparse
from contextlib import asynccontextmanager
from typing import Optional, List, Dict, Any
from fastapi import FastAPI, HTTPException, Body
from fastapi.openapi.utils import get_openapi
from pydantic import BaseModel, Field
from cloakbrowser import launch_async
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

# Global instance variable for sharing the browser engine across requests
browser_instance = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Lifecycle manager to initialize CloakBrowser engine on startup
    and gracefully close it on shutdown.
    """
    global browser_instance
    print("[INFO] Initializing CloakBrowser Core Engine for Headless Production Server...")
    try:
        # Initializing the source-level patched Chromium binary in headless mode
        browser_instance = await launch_async(
            headless=True, 
            humanize=True, 
            human_preset="default"
        )
        print("[INFO] CloakBrowser engine initialized successfully and ready for incoming requests.")
    except Exception as e:
        print(f"[CRITICAL] Failed to initialize CloakBrowser engine: {e}")
        raise e
    yield
    if browser_instance:
        print("[INFO] Shutting down CloakBrowser engine smoothly...")
        await browser_instance.close()

app = FastAPI(
    title="CloakBrowser Stream Extractor API Pro",
    version="1.2.5",
    description="Enterprise-grade production API to extract hidden M3U8 and MP4 media streams from highly protected websites.",
    lifespan=lifespan
)

# --- REQUEST & RESPONSE MODELS ---
class ExtractorRequest(BaseModel):
    url: str = Field(..., description="The target streaming website URL to scan.", example="https://example.com/video/player1")
    proxy: Optional[str] = Field(None, description="Optional proxy connection string (e.g., socks5://user:pass@host:port or http://host:port).", example="socks5://127.0.0.1:1080")
    timeout: Optional[int] = Field(15, description="Maximum wait time in seconds for streaming media discovery.", example=15)
    upload_pastebin: bool = Field(False, description="Whether to automatically upload hidden/blob M3U8 text contents to Pastebin.")
    fast_exit: bool = Field(True, description="Immediately return response once the first valid media stream is captured.")

class BatchExtractorRequest(BaseModel):
    urls: List[str] = Field(..., description="List of target streaming URLs to scan concurrently.")
    proxy: Optional[str] = Field(None, description="Global proxy connection string used across all parallel instances.")
    timeout: Optional[int] = Field(15, description="Maximum wait time in seconds for each separate URL scan.")
    upload_pastebin: bool = Field(False, description="Enable automatic Pastebin uploads for all captured hidden text streams.")
    fast_exit: bool = Field(True, description="Immediately complete individual scans upon finding the first valid media stream.")

class VideoStream(BaseModel):
    type: str = Field(..., description="Type of stream discovered (M3U8_NETWORK, MP4_NETWORK, M3U8_HIDDEN, or M3U8_DOM).")
    url: str = Field(..., description="Direct downloadable or viewable media stream URL.")
    pastebin_raw: Optional[str] = Field(None, description="Publicly accessible Pastebin raw text URL if upload_pastebin was activated.")
    content: Optional[str] = Field(None, description="A brief text snippet of the raw payload or method metadata.")

# --- NETWORK HELPER FUNCTIONS ---
async def upload_to_centos_paste(context, text_content: str) -> str:
    """
    Uploads raw hidden M3U8 manifests to paste.centos.org for instant extraction access.
    """
    try:
        resp = await context.request.post(
            "https://paste.centos.org/",
            form={
                "name": "stealth-api-extractor",
                "title": "stream_manifest",
                "lang": "text",
                "code": text_content,
                "expire": "120",  # Link automatically expires in 120 minutes
                "submit": "submit"
            }
        )
        html = await resp.text()
        match = re.search(r'<a class="control" href="([^"]*?)">View Raw</a>', html)
        if match:
            raw_uri = match.group(1)
            return raw_uri if raw_uri.startswith("http") else f"https://paste.centos.org{raw_uri}"
    except Exception as e:
        print(f"[ERROR] Pastebin data synchronization failed: {e}")
    return ""

async def force_play_videos(page):
    """
    Injects recursive scripts inside the main document and all sub-iframes to force video player initialization.
    """
    js_code = """() => {
        document.querySelectorAll('video, audio').forEach(v => {
            if (v.paused) {
                v.muted = true; // Muting is strictly required to bypass modern browser autoplay blocking
                const p = v.play();
                if (p !== undefined) p.catch(() => {});
            }
        });
    }"""
    try:
        await page.evaluate(js_code)
        for frame in page.frames:
            try:
                await frame.evaluate(js_code)
            except:
                pass
    except:
        pass

async def block_unnecessary_resources(route, request):
    """
    Optimizes network traffic and memory footprints by entirely dropping heavy structural design requests.
    """
    if request.resource_type in ["image", "stylesheet", "font"]:
        await route.abort()
    else:
        await route.continue_()

# --- CORE EXTRACTION ENGINE ---
async def process_single_url(url: str, proxy: Optional[str], timeout: int, upload_pastebin: bool, fast_exit: bool) -> Dict[str, Any]:
    global browser_instance
    detected_streams: List[VideoStream] = []
    
    # Static listing of known tracking providers to eliminate network parsing false positives
    TRACKING_DOMAINS = ["google-analytics.com", "yandex.ru", "doubleclick.net", "facebook.com", "googletagmanager.com", "scorecardresearch.com"]
    
    context_options = {}
    if proxy:
        context_options["proxy"] = {"server": proxy}
        context_options["geoip"] = True  # Automatically sync local browser environment details with exit IP

    try:
        # Spawning an isolated context session to prevent cookies leak cross requests
        context = await browser_instance.new_context(**context_options)
        page = await context.new_page()
        
        # Interceptive background event loop to block unwanted tab popups and ad redirects
        async def on_new_page(new_page):
            if new_page != page:
                try:
                    await new_page.close()
                    print(f"[POPUP CONTROL] Intercepted and terminated an intrusive advertisement tab for {url}")
                except Exception:
                    pass
        
        context.on("page", on_new_page)

        # Connect performance optimization route rules
        await page.route("**/*", block_unnecessary_resources)

        # Real-time packet sniffing interceptor loop
        async def handle_response(response):
            try:
                if response.request.method == "OPTIONS": 
                    return
                url_str, status = response.url.lower(), response.status
                parsed_url = urlparse(response.url)
                netloc = parsed_url.netloc.lower()
                path = parsed_url.path.lower()
                
                # Drop analytics tracking from matching rules
                if any(domain in netloc for domain in TRACKING_DOMAINS): 
                    return
                
                # Rule 1: Match static file extensions cleanly extracted from raw pathing
                if path.endswith(".m3u8"):
                    if not any(s.url == response.url for s in detected_streams):
                        detected_streams.append(VideoStream(type="M3U8_NETWORK", url=response.url))
                        print(f"[FOUND] Captured direct M3U8 stream URL: {response.url}")
                        
                elif path.endswith(".mp4"):
                    if not any(s.url == response.url for s in detected_streams):
                        detected_streams.append(VideoStream(type="MP4_NETWORK", url=response.url))
                        print(f"[FOUND] Captured direct MP4 stream URL: {response.url}")

                # Rule 2: Intercept content-type responses for obfuscated blobs and XHR text manifests
                elif status == 200 and "mpegurl" in response.headers.get("content-type", "").lower():
                    body_bytes = await response.body()
                    text_content = body_bytes.decode("utf-8", errors="ignore")
                    if "#EXTM3U" in text_content and not any(s.content and s.content[:50] == text_content[:50] for s in detected_streams):
                        print(f"[FOUND] Intercepted hidden streaming payload manifest. Syncing...")
                        paste_link = await upload_to_centos_paste(context, text_content) if upload_pastebin else None
                        detected_streams.append(VideoStream(
                            type="M3U8_HIDDEN", 
                            url="blob_or_hidden", 
                            pastebin_raw=paste_link, 
                            content=text_content[:150]
                        ))
            except Exception:
                pass

        page.on("response", handle_response)
        
        print(f"[STEALTH ACTION] Initiating engine connection to target: {url}")
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
            await asyncio.sleep(2)  # Static cool down for scripts execution and player mounting
        except PlaywrightTimeoutError:
            print(f"[WARNING] Navigation timeout reached for {url}. Switching engine to aggressive sniffing mode...")
        except Exception as e:
            await context.close()
            return {"status": "error", "target": url, "message": f"Network unreachable or link dead: {str(e)}"}

        # Targeted Behavioral Clicking Loop to pierce ad layers and force player triggers
        for _ in range(timeout):
            if fast_exit and len(detected_streams) > 0:
                print(f"[FAST-EXIT] Stream signature captured for {url}. Terminating worker sequence early.")
                break
                
            await force_play_videos(page)
            
            try:
                # Scrape coordinates for potential media player bounding blocks
                media_elements = await page.query_selector_all("iframe, video, .play-btn, .plyr, [class*='player']")
                if media_elements:
                    for el in media_elements:
                        box = await el.bounding_box()
                        if box and box["width"] > 50 and box["height"] > 50:
                            target_x = box["x"] + box["width"] / 2
                            target_y = box["y"] + box["height"] / 2
                            
                            # Move mouse randomly to clear behavioral tripwires, then click target
                            await page.mouse.move(target_x + random.randint(-50, 50), target_y + random.randint(-50, 50))
                            await page.mouse.click(target_x, target_y)
                            await asyncio.sleep(0.1)
                            await page.mouse.click(target_x, target_y + random.randint(-15, 15))
                else:
                    # Fallback coordinate center clicking sequence
                    viewport = page.viewport_size
                    if viewport:
                        cx, cy = viewport["width"] / 2, viewport["height"] / 2
                        await page.mouse.move(cx + random.randint(-100, 100), cy + random.randint(-100, 100))
                        await page.mouse.click(cx, cy)
                        await asyncio.sleep(0.1)
                        await page.mouse.click(cx, cy + random.randint(-15, 15))
            except Exception:
                pass
            
            await asyncio.sleep(1)

        # Fallback Deep DOM Scanner Layer (Mimicking Userscript string pattern analysis)
        if len(detected_streams) == 0 or not fast_exit:
            try:
                print(f"[SCANNER] Initiating Deep DOM fallback scan for {url}...")
                html_content = await page.content()
                m3u8_pattern = re.compile(r"(https?://[^\s\"\'<>]+\.m3u8[^\s\"\'<>]*)", re.IGNORECASE)
                for match_url in m3u8_pattern.findall(html_content):
                    clean_url = match_url.replace("\\/", "/")
                    if not any(s.url == clean_url for s in detected_streams):
                        detected_streams.append(VideoStream(
                            type="M3U8_DOM", 
                            url=clean_url, 
                            content="Extracted directly from HTML/JS source patterns via RegEx matching."
                        ))
                        print(f"[FOUND] Fallback DOM scanner successfully mapped stream URL: {clean_url}")
            except Exception as e:
                print(f"[WARNING] Fallback document schema scan failed: {e}")

        await page.close()
        await context.close()

        return {
            "status": "success",
            "target": url,
            "total_found": len(detected_streams),
            "streams": detected_streams
        }

    except Exception as e:
        if 'context' in locals(): 
            await context.close()
        return {"status": "error", "target": url, "message": f"Execution processing error: {str(e)}"}

# --- ROUTER ENDPOINTS ---

@app.post(
    "/api/v1/extract", 
    response_model=dict, 
    tags=["Scraper Core"],
    summary="Extract stream links from a single URL",
    description="Launches an isolated headless stealth tab session to intercept network traffic and click player elements."
)
async def extract_one(payload: ExtractorRequest):
    if not browser_instance: 
        raise HTTPException(status_code=500, detail="Browser core engine is uninitialized.")
    return await process_single_url(payload.url, payload.proxy, payload.timeout, payload.upload_pastebin, payload.fast_exit)

@app.post(
    "/api/v1/extract-batch", 
    response_model=dict, 
    tags=["Scraper Core"],
    summary="Extract stream links from multiple URLs concurrently",
    description="Processes a collection of URLs concurrently utilizing non-blocking asynchronous event-driven workers."
)
async def extract_batch(payload: BatchExtractorRequest):
    if not browser_instance: 
        raise HTTPException(status_code=500, detail="Browser core engine is uninitialized.")
    
    print(f"[BATCH PIPELINE] Executing parallel extraction threads for {len(payload.urls)} targets...")
    tasks = [
        process_single_url(u, payload.proxy, payload.timeout, payload.upload_pastebin, payload.fast_exit) 
        for u in payload.urls
    ]
    results = await asyncio.gather(*tasks)
    return {
        "status": "success",
        "total_urls": len(payload.urls),
        "results": results
    }

@app.get("/api/v1/health", tags=["System Utilities"], summary="System Performance Health Status Check")
async def health_check():
    if browser_instance:
        return {"status": "healthy", "engine": "CloakBrowser Node Engine Active"}
    return {"status": "unhealthy", "engine": "Disconnected"}

# --- OPENAPI SPEC GENERATOR ---
def custom_openapi():
    if app.openapi_schema: 
        return app.openapi_schema
    openapi_schema = get_openapi(
        title="CloakBrowser Media Extractor Service Pro",
        version="1.2.5",
        description="Enterprise automation routing gateway built with FastAPI to extract protected digital stream assets cleanly.",
        routes=app.routes,
    )
    app.openapi_schema = openapi_schema
    return app.openapi_schema

app.openapi = custom_openapi
