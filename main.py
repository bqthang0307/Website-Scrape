import base64
import json
from typing import Optional, Dict, Any
import time
import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, HttpUrl

# Playwright
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

app = FastAPI(title="Web Screenshot API", version="1.0.1")


class ScrapeRequest(BaseModel):
    url: HttpUrl
    notify_api: Optional[HttpUrl] = None
    user_agent: Optional[str] = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome Safari"
    timeout_ms: Optional[int] = 30000
    full_page: Optional[bool] = True
    viewport_width: Optional[int] = 1280
    viewport_height: Optional[int] = 1080
    wait_until: Optional[str] = "networkidle"
    autoscroll: Optional[bool] = True
    autoscroll_steps: Optional[int] = 12           # kept for backward compatibility (unused by default path)
    autoscroll_delay_ms: Optional[int] = 250
    screenshot_retries: Optional[int] = 2
    screenshot_wait_ms_between_retries: Optional[int] = 1000


class SendRequest(BaseModel):
    target_api: HttpUrl
    screenshot_base64: str
    meta: Optional[Dict[str, Any]] = None


def _disable_fixed_backgrounds_and_effects(page) -> None:
    """Avoid white gaps in stitched screenshots caused by parallax/fixed backgrounds."""
    css = (
        "html, body, * {"
        " background-attachment: initial !important;"
        " background-position: 0 0 !important;"
        " scroll-behavior: auto !important;"
        "}"
    )
    try:
        page.add_style_tag(content=css)
    except Exception:
        pass


def _ensure_assets_loaded(page, *, timeout_ms: int = 5000) -> None:
    """Wait for images to decode and fonts to be ready."""
    try:
        page.evaluate(
            """
            async (timeout) => {
                const abort = new Promise((_, rej) => setTimeout(() => rej(new Error('assets-timeout')), timeout));
                const waitFonts = (typeof document.fonts !== 'undefined') ? document.fonts.ready.catch(()=>{}) : Promise.resolve();
                const imgs = Array.from(document.images || []);
                const waitImages = Promise.all(
                  imgs.map(img => (img.complete ? Promise.resolve()
                    : (img.decode ? img.decode().catch(()=>{})
                      : new Promise(r => { img.addEventListener('load', r, { once: true }); img.addEventListener('error', r, { once: true }); }))))
                );
                await Promise.race([Promise.all([waitFonts, waitImages]), abort]);
            }
            """,
            int(timeout_ms)
        )
    except Exception:
        pass


def _force_eager_loading(page) -> None:
    """Un-lazy assets so middle-of-page content renders even when offscreen."""
    try:
        page.evaluate("""
        () => {
          // Un-lazy images
          document.querySelectorAll('img[loading]').forEach(img => { img.loading = 'eager'; });
          document.querySelectorAll('img[decoding]').forEach(img => { img.decoding = 'sync'; });
          document.querySelectorAll('img[data-src]').forEach(img => { if (!img.src) img.src = img.getAttribute('data-src'); });
          document.querySelectorAll('img[data-srcset]').forEach(img => { if (!img.srcset) img.srcset = img.getAttribute('data-srcset'); });
          document.querySelectorAll('source[data-srcset]').forEach(src => { if (!src.srcset) src.srcset = src.getAttribute('data-srcset'); });

          // Un-lazy iframes/videos commonly using data-src
          document.querySelectorAll('iframe[data-src]').forEach(f => { if (!f.src) f.src = f.getAttribute('data-src'); });
          document.querySelectorAll('video[data-src]').forEach(v => { if (!v.src) v.src = v.getAttribute('data-src'); });

          // Try to trigger IntersectionObserver-based lazy blocks
          const forceInView = el => {
            el.style.minHeight = (el.offsetHeight || 1) + 'px';
            el.style.minWidth  = (el.offsetWidth  || 1) + 'px';
          };
          document.querySelectorAll('[data-lazy], .lazy, [data-infinite-scroll], [data-observe]').forEach(forceInView);
        }
        """)
    except Exception:
        pass


def _autoscroll_until_settled(page, *, max_rounds: int = 30, step_px: int = 900, delay_ms: int = 250) -> None:
    """
    Scrolls down until scroll height stops growing or max rounds reached.
    IMPORTANT: does NOT jump back to the top (avoids unloading middle content).
    """
    page.evaluate(
        """
        async ({ maxRounds, step, delay }) => {
            let lastHeight = 0;
            let stableRounds = 0;
            const wait = (ms) => new Promise(r => setTimeout(r, ms));

            for (let round = 0; round < maxRounds; round++) {
                window.scrollBy(0, step);
                await wait(delay);

                const newHeight = Math.max(
                  document.body.scrollHeight,
                  document.documentElement.scrollHeight
                );

                if (newHeight <= lastHeight) {
                    stableRounds++;
                    if (stableRounds >= 2) break; // height stable twice in a row
                } else {
                    stableRounds = 0;
                    lastHeight = newHeight;
                }
            }

            // Ensure we've actually touched the bottom once
            window.scrollTo(0, document.documentElement.scrollHeight);
            await wait(Math.max(200, delay));
        }
        """,
        {"maxRounds": int(max_rounds), "step": int(step_px), "delay": int(delay_ms)}
    )


def take_screenshot_base64(
    url: str,
    *,
    user_agent: Optional[str],
    timeout_ms: int,
    full_page: bool,
    viewport_width: int,
    viewport_height: int,
    wait_until: str,
    autoscroll: bool,
    autoscroll_steps: int,
    autoscroll_delay_ms: int,
    screenshot_retries: int = 2,
    screenshot_wait_ms_between_retries: int = 1000,
) -> Dict[str, Any]:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context(
            user_agent=user_agent,
            viewport={"width": viewport_width, "height": viewport_height},
            device_scale_factor=1.0,
        )
        page = context.new_page()

        # Block heavy media to avoid buffering stalls (keeps posters/thumbnails)
        context.route("**/*", lambda route: route.abort() if route.request.resource_type == "media" else route.continue_())

        try:
            context.set_default_timeout(timeout_ms)
            page.set_default_timeout(timeout_ms)
            page.goto(url, wait_until=wait_until, timeout=timeout_ms)
        except PWTimeoutError:
            context.close()
            browser.close()
            raise HTTPException(status_code=504, detail="Page navigation timeout")

        # Reduce motion + disable CSS animations (prevents long-running effects)
        try: page.emulate_media(reduced_motion="reduce")
        except Exception: pass
        try: page.add_style_tag(content="* { animation: none !important; transition: none !important; }")
        except Exception: pass

        _disable_fixed_backgrounds_and_effects(page)
        _force_eager_loading(page)

        if autoscroll:
            # Robust autoscroll to touch/paint all lazy/virtualized regions
            _autoscroll_until_settled(
                page,
                max_rounds=30,
                step_px=900,
                delay_ms=max(150, autoscroll_delay_ms)
            )

        # Give the browser a moment to paint everything we just revealed
        time.sleep(2)

        # Ensure fonts & images are decoded; then give network a brief idle window
        _ensure_assets_loaded(page, timeout_ms=min(8000, max(2000, timeout_ms // 4)))
        try:
            page.wait_for_load_state("networkidle", timeout=min(5000, timeout_ms))
        except Exception:
            pass

        # Pause any playing videos
        page.evaluate("() => { document.querySelectorAll('video').forEach(v => v.pause()); }")

        # Screenshot with retries; keep full_page to avoid cropping the middle
        last_error: Optional[Exception] = None
        png_bytes = None
        for attempt_index in range(max(1, int(screenshot_retries))):
            try:
                png_bytes = page.screenshot(
                    full_page=full_page,
                    type="png",
                    timeout=timeout_ms,
                )
                last_error = None
                break
            except Exception as e:
                last_error = e
                time.sleep(max(0, int(screenshot_wait_ms_between_retries)) / 1000.0)

        if png_bytes is None:
            context.close()
            browser.close()
            raise HTTPException(status_code=504, detail=f"Screenshot failed: {str(last_error) if last_error else 'Unknown error'}")

        b64 = base64.b64encode(png_bytes).decode("utf-8")
        title = page.title()
        current_url = page.url

        context.close()
        browser.close()

        return {
            "screenshot_base64": b64,
            "content_type": "image/png",
            "title": title,
            "final_url": current_url,
            "viewport": {"width": viewport_width, "height": viewport_height},
            "full_page": full_page,
        }


def send_screenshot_base64(
    target_api: str,
    screenshot_base64: str,
    meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    payload = {
        "screenshot_base64": screenshot_base64,
        "meta": meta or {},
    }
    with httpx.Client(timeout=30.0) as client:
        resp = client.post(str(target_api), json=payload)
        return {
            "target_api": target_api,
            "status_code": resp.status_code,
            "response_text": resp.text[:2000],
        }


@app.post("/scrape")
def scrape(req: ScrapeRequest):
    data = take_screenshot_base64(
        str(req.url),
        user_agent=req.user_agent,
        timeout_ms=req.timeout_ms,
        full_page=req.full_page,
        viewport_width=req.viewport_width,
        viewport_height=req.viewport_height,
        wait_until=req.wait_until,
        autoscroll=req.autoscroll,
        autoscroll_steps=req.autoscroll_steps,
        autoscroll_delay_ms=req.autoscroll_delay_ms,
        screenshot_retries=req.screenshot_retries or 2,
        screenshot_wait_ms_between_retries=req.screenshot_wait_ms_between_retries or 1000,
    )

    notify_result = None
    if req.notify_api:
        try:
            notify_result = send_screenshot_base64(
                target_api=str(req.notify_api),
                screenshot_base64=data["screenshot_base64"],
                meta={
                    "url": req.url,
                    "title": data.get("title"),
                    "final_url": data.get("final_url"),
                    "viewport": data.get("viewport"),
                },
            )
        except Exception as e:
            notify_result = {"error": str(e)}

    return {"ok": True, "data": data, "notify_result": str(notify_result)}


@app.post("/send")
def send_only(req: SendRequest):
    try:
        result = send_screenshot_base64(
            target_api=str(req.target_api),
            screenshot_base64=req.screenshot_base64,
            meta=req.meta or {},
        )
        return {"ok": True, "result": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))