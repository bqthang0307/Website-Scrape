import base64
import json
from typing import Optional, Dict, Any

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, HttpUrl

# Playwright
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError


app = FastAPI(title="Web Screenshot API", version="1.0.0")


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
    autoscroll_steps: Optional[int] = 12
    autoscroll_delay_ms: Optional[int] = 250


class SendRequest(BaseModel):
    target_api: HttpUrl
    screenshot_base64: str
    meta: Optional[Dict[str, Any]] = None


def _autoscroll(page, distance: int = 500, delay_ms: int = 250):
    """Gradual scroll to trigger lazy-loaded and animated content."""
    page.evaluate("""
        async (distance, delay) => {
            await new Promise((resolve) => {
                let totalHeight = 0;
                const scrollHeight = document.body.scrollHeight;
                const timer = setInterval(() => {
                    window.scrollBy(0, distance);
                    totalHeight += distance;

                    if (totalHeight >= scrollHeight) {
                        clearInterval(timer);
                        resolve();
                    }
                }, delay);
            });
        }
    """, distance, delay_ms)  # Pass variables as arguments here




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
) -> Dict[str, Any]:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context(
            user_agent=user_agent,
            viewport={"width": viewport_width, "height": viewport_height},
            device_scale_factor=1.0,
        )
        page = context.new_page()
        try:
            page.goto(url, wait_until=wait_until, timeout=timeout_ms)
        except PWTimeoutError:
            context.close()
            browser.close()
            raise HTTPException(status_code=504, detail="Page navigation timeout")

        # Enable autoscroll if the flag is True
        if autoscroll:
            _autoscroll(page, distance=500, delay_ms=250)  # Adjust as needed

        # Take a screenshot of the full page
        png_bytes = page.screenshot(full_page=full_page, type="png")
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