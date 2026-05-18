import asyncio
import re
import random
from urllib.parse import urlparse
from contextlib import asynccontextmanager
from typing import Optional, List, Dict, Any
from fastapi import FastAPI, HTTPException
from fastapi.openapi.utils import get_openapi
from pydantic import BaseModel, Field
from cloakbrowser import launch_async
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

browser_instance = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global browser_instance
    print("[INFO] Initializing CloakBrowser Core Engine (Production Final)...")
    try:
        # Chạy ẩn không giao diện (Headless) cho Server
        browser_instance = await launch_async(headless=True, humanize=True, human_preset="default")
        print("[INFO] CloakBrowser engine initialized successfully.")
    except Exception as e:
        print(f"[CRITICAL] Failed to initialize CloakBrowser engine: {e}")
        raise e
    yield
    if browser_instance:
        print("[INFO] Shutting down CloakBrowser engine smoothly...")
        await browser_instance.close()

app = FastAPI(
    title="CloakBrowser Stream Extractor API Pro",
    version="2.0.0",
    description="Ultimate API with Auto-Detect, Iframe Wrapper, Header Spoofing, and Deep Error Inspection.",
    lifespan=lifespan,
    redoc_url=None 
)

# --- REQUEST & RESPONSE MODELS ---
class ExtractorRequest(BaseModel):
    url: str = Field(..., description="The target streaming website URL to scan.")
    proxy: Optional[str] = Field(None, description="Optional proxy string (e.g., socks5://user:pass@host:port).")
    headers: Optional[Dict[str, str]] = Field(None, description="Custom HTTP Headers to inject (Overrides default).")
    timeout: Optional[int] = Field(15, description="Maximum wait time in seconds.")
    upload_pastebin: bool = Field(False, description="Upload hidden M3U8 blob contents to Pastebin.")
    fast_exit: bool = Field(True, description="Return immediately once the first stream is found.")
    wrap_iframe: bool = Field(False, description="Bypass Framebusters: Wrap target in a fake iframe (Use only for strict CDN links).")

class BatchExtractorRequest(BaseModel):
    urls: List[str] = Field(..., description="List of target URLs to scan concurrently.")
    proxy: Optional[str] = Field(None, description="Global proxy string.")
    headers: Optional[Dict[str, str]] = Field(None, description="Custom HTTP Headers to inject.")
    timeout: Optional[int] = Field(15, description="Maximum wait time per URL.")
    upload_pastebin: bool = Field(False, description="Upload hidden contents to Pastebin.")
    fast_exit: bool = Field(True, description="Return immediately upon finding streams.")
    wrap_iframe: bool = Field(False, description="Bypass Framebusters: Wrap targets in fake iframes.")

class VideoStream(BaseModel):
    type: str = Field(..., description="Type of stream (M3U8_NETWORK, MP4_NETWORK, M3U8_HIDDEN, M3U8_DOM).")
    url: str = Field(..., description="Direct media stream URL.")
    headers: Optional[Dict[str, str]] = Field(None, description="Request headers to inject into your Video Player to bypass 403.")
    pastebin_raw: Optional[str] = Field(None, description="Pastebin raw text URL if uploaded.")
    content: Optional[str] = Field(None, description="Preview of the raw M3U8 payload or DOM.")

# --- HELPERS ---
async def upload_to_centos_paste(context, text_content: str) -> str:
    try:
        await context.request.get("https://paste.centos.org") 
        resp = await context.request.post(
            "https://paste.centos.org/",
            headers={
                "Referer": "https://paste.centos.org/",
                "Origin": "https://paste.centos.org",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Upgrade-Insecure-Requests": "1"
            },
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

def extract_player_headers(raw_headers: Dict[str, str]) -> Dict[str, str]:
    IMPORTANT_KEYS = ["referer", "user-agent", "cookie", "origin", "authorization", "x-requested-with"]
    return {k: v for k, v in raw_headers.items() if k.lower() in IMPORTANT_KEYS}

# --- CORE ENGINE ---
async def process_single_url(url: str, proxy: Optional[str], timeout: int, upload_pastebin: bool, fast_exit: bool, custom_headers: Optional[Dict[str, str]] = None, wrap_iframe: bool = False) -> Dict[str, Any]:
    global browser_instance
    detected_streams: List[VideoStream] = []
    is_timeout_triggered = False
    main_status = 0
    
    parsed_target = urlparse(url)
    base_domain = f"{parsed_target.scheme}://{parsed_target.netloc}/"
    
    # Định tuyến trang mẹ giả mạo nếu bật Iframe Wrapper
    parent_url = "https://www.google.com/"
    if custom_headers:
        for k, v in custom_headers.items():
            if k.lower() == "referer":
                parent_url = v
                break

    stealth_headers = {
        "Referer": parent_url if wrap_iframe else "https://www.google.com/", 
        "Origin": base_domain[:-1],           
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Upgrade-Insecure-Requests": "1",
        "DNT": "1"
    }

    final_headers = stealth_headers.copy()
    if custom_headers:
        normalized_custom = {k.title(): v for k, v in custom_headers.items()}
        final_headers.update(normalized_custom)

    context_options = {"extra_http_headers": final_headers}
    if proxy:
        context_options["proxy"] = {"server": proxy}
        context_options["geoip"] = True

    try:
        context = await browser_instance.new_context(**context_options)
        page = await context.new_page()
        
        # Trình diệt Popup Tab
        async def on_new_page(new_page):
            if new_page != page:
                try: await new_page.close()
                except: pass
        context.on("page", on_new_page)

        # === ROUTING ĐA LUỒNG (TRỰC TIẾP HOẶC BỌC IFRAME) ===
        if wrap_iframe:
            async def universal_route(route, request):
                req_url = request.url
                if request.resource_type in ["image", "stylesheet", "font"]:
                    await route.abort()
                    return

                if req_url == parent_url and request.resource_type == "document":
                    html_body = f"<html><body style='margin:0;overflow:hidden;'><iframe src='{url}' style='width:100vw;height:100vh;border:none;' allow='autoplay; fullscreen'></iframe></body></html>"
                    await route.fulfill(status=200, content_type="text/html", body=html_body)
                    return

                headers = request.headers.copy()
                if custom_headers:
                    for k, v in custom_headers.items(): headers[k.lower()] = v

                if request.resource_type in ["document", "sub_document"]:
                    try:
                        response = await route.fetch(headers=headers)
                        res_headers = response.headers
                        res_headers.pop("x-frame-options", None)
                        res_headers.pop("content-security-policy", None)
                        await route.fulfill(response=response, headers=res_headers)
                        return
                    except: pass
                await route.continue_(headers=headers)

            await context.route("**/*", universal_route)
            target_nav_url = parent_url 
        else:
            async def normal_route(route, request):
                if request.resource_type in ["image", "stylesheet", "font"]:
                    await route.abort()
                    return
                headers = request.headers.copy()
                if not custom_headers or not any(k.lower() == "referer" for k in custom_headers.keys()):
                    headers["Referer"] = base_domain if parsed_target.netloc in request.url else "https://www.google.com/"
                await route.continue_(headers=headers)

            await context.route("**/*", normal_route)
            target_nav_url = url

        # === TẦNG SMART AUTO-DETECT MẠNG ===
        async def handle_response(response):
            try:
                if response.request.method == "OPTIONS" or response.status not in [200, 206]: return
                if response.request.resource_type in ["image", "stylesheet", "script", "font", "document"]: return
                
                raw_url = response.url
                path = urlparse(raw_url).path.lower()
                content_type = response.headers.get("content-type", "").lower()
                captured_headers = extract_player_headers(response.request.headers)
                
                # Kiểm định MP4
                if path.endswith(".mp4") or "video/mp4" in content_type:
                    if "video/" in content_type or "application/octet-stream" in content_type:
                        if not any(s.url == raw_url for s in detected_streams):
                            detected_streams.append(VideoStream(type="MP4_NETWORK", url=raw_url, headers=captured_headers))
                
                # Kiểm định M3U8 (Chữ ký #EXTM3U)
                elif path.endswith(".m3u8") or path.endswith(".m3u") or "mpegurl" in content_type:
                    body_bytes = await response.body()
                    text_content = body_bytes.decode("utf-8", errors="ignore")
                    if "#EXTM3U" in text_content:
                        if not any(s.content and s.content[:50] == text_content[:50] for s in detected_streams) and not any(s.url == raw_url for s in detected_streams):
                            paste_link = await upload_to_centos_paste(context, text_content) if upload_pastebin else None
                            stream_type = "M3U8_NETWORK" if raw_url.startswith("http") else "M3U8_HIDDEN"
                            detected_streams.append(VideoStream(
                                type=stream_type, url=raw_url if stream_type == "M3U8_NETWORK" else "blob_or_hidden", 
                                headers=captured_headers, pastebin_raw=paste_link, content=text_content[:150]
                            ))
            except Exception:
                pass

        page.on("response", handle_response)
        
        print(f"[PROCESS] Target: {url} | Wrap: {wrap_iframe}")
        try:
            referer_to_use = final_headers.get("Referer", "https://www.google.com/")
            main_response = await page.goto(target_nav_url, referer=referer_to_use, wait_until="domcontentloaded", timeout=timeout * 1000)
            main_status = main_response.status if main_response else 0
            await asyncio.sleep(2)
        except PlaywrightTimeoutError:
            is_timeout_triggered = True
        except Exception as e:
            await context.close()
            return {"status": "error", "http_code": 404, "target": url, "message": f"Network error: {str(e)}"}

        # Vòng lặp tương tác Click Player
        for _ in range(timeout):
            if fast_exit and len(detected_streams) > 0: break
            await force_play_videos(page)
            try:
                media_elements = await page.query_selector_all("iframe, video, .play-btn, .plyr, [class*='player']")
                if media_elements:
                    for el in media_elements:
                        box = await el.bounding_box()
                        if box and box["width"] > 50 and box["height"] > 50:
                            tx = box["x"] + box["width"] / 2
                            ty = box["y"] + box["height"] / 2
                            await page.mouse.move(tx + random.randint(-50, 50), ty + random.randint(-50, 50))
                            await page.mouse.click(tx, ty)
                            await asyncio.sleep(0.1)
                            await page.mouse.click(tx, ty + random.randint(-15, 15))
                else:
                    viewport = page.viewport_size
                    if viewport:
                        cx, cy = viewport["width"] / 2, viewport["height"] / 2
                        await page.mouse.move(cx + random.randint(-100, 100), cy + random.randint(-100, 100))
                        await page.mouse.click(cx, cy)
                        await asyncio.sleep(0.1)
                        await page.mouse.click(cx, cy + random.randint(-15, 15))
            except: pass
            await asyncio.sleep(1)

        # Fallback Quét mã nguồn DOM
        if len(detected_streams) == 0 or not fast_exit:
            try:
                frames_to_scan = page.frames if wrap_iframe else [page.main_frame]
                for frame in frames_to_scan:
                    html_content = await frame.content()
                    m3u8_pattern = re.compile(r"(https?://[^\s\"\'<>]+\.m3u8[^\s\"\'<>]*)", re.IGNORECASE)
                    for match_url in m3u8_pattern.findall(html_content):
                        clean_url = match_url.replace("\\/", "/")
                        if not any(s.url == clean_url for s in detected_streams):
                            main_headers = {"User-Agent": await page.evaluate("navigator.userAgent"), "Referer": target_nav_url}
                            detected_streams.append(VideoStream(type="M3U8_DOM", url=clean_url, headers=main_headers))
            except: pass

        # === DEEP ERROR INSPECTION (Phân loại lỗi 403, 404, 504) ===
        if len(detected_streams) == 0:
            page_title = "Unknown"
            page_snippet = ""
            try:
                target_frame = page.main_frame
                if wrap_iframe:
                    for frame in page.frames:
                        if url in frame.url: target_frame = frame
                
                page_title = await target_frame.title()
                plain_text = await target_frame.evaluate("document.body.innerText")
                page_snippet = plain_text[:200].replace('\n', ' ').strip()
            except: pass

            await page.close()
            await context.close()

            if "cloudflare" in page_title.lower() or "just a moment" in page_title.lower() or "attention required" in page_title.lower():
                return {"status": "error", "http_code": 403, "target": url, "message": f"Blocked by Cloudflare/WAF. Page Title: {page_title}"}

            if main_status >= 400 and not wrap_iframe:
                 return {"status": "error", "http_code": main_status, "target": url, "message": f"Website returned HTTP {main_status}. Title: {page_title}. Snippet: {page_snippet}..."}

            if is_timeout_triggered:
                return {"status": "error", "http_code": 504, "target": url, "message": f"Website Timeout. Title seen: {page_title}"}

            return {"status": "error", "http_code": 404, "target": url, "message": f"No streams found. The bot sees this text on screen: '{page_snippet}...'. Title: '{page_title}'"}

        await page.close()
        await context.close()
        return {"status": "success", "http_code": 200, "target": url, "total_found": len(detected_streams), "streams": detected_streams}

    except Exception as e:
        if 'context' in locals(): await context.close()
        return {"status": "error", "http_code": 500, "target": url, "message": f"Execution error: {str(e)}"}

# --- ENDPOINTS ---
@app.post("/api/v1/extract", tags=["Scraper Core"])
async def extract_one(payload: ExtractorRequest):
    if not browser_instance: raise HTTPException(status_code=500, detail="Browser error")
    result = await process_single_url(payload.url, payload.proxy, payload.timeout, payload.upload_pastebin, payload.fast_exit, payload.headers, payload.wrap_iframe)
    if result["status"] == "error": raise HTTPException(status_code=result["http_code"], detail=result["message"])
    return result

@app.post("/api/v1/extract-batch", tags=["Scraper Core"])
async def extract_batch(payload: BatchExtractorRequest):
    if not browser_instance: raise HTTPException(status_code=500, detail="Browser error")
    tasks = [process_single_url(u, payload.proxy, payload.timeout, payload.upload_pastebin, payload.fast_exit, payload.headers, payload.wrap_iframe) for u in payload.urls]
    results = await asyncio.gather(*tasks)
    return {"status": "success", "total_urls": len(payload.urls), "results": results}

def custom_openapi():
    if app.openapi_schema: return app.openapi_schema
    app.openapi_schema = get_openapi(title="CloakBrowser Extractor API", version="2.0.0", routes=app.routes)
    return app.openapi_schema
app.openapi = custom_openapi
