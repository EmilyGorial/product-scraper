from fastapi import FastAPI, Query
from typing import List
import requests
import google.generativeai as genai
from bs4 import BeautifulSoup
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright
import os
import re
import asyncio
import platform
import json



# os/env setup


# ensure async works properly on windows 
if platform.system() == "Windows":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

load_dotenv()
GEMINI_API_KEY = os.getenv("GEMINI_KEY")

genai.configure(api_key=GEMINI_API_KEY)


#fastapi app + cors

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# price parser

def extract_price_from_text(text: str):
    """Extract the most likely price (not rating) from any text blob."""
    if not text:
        return None

    text = text.strip()

    # prefer explicit currency symbols
    currency_pattern = re.compile(
        r"(?:USD|US\$|CA\$|\$|£|€)\s?\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?"
    )
    match = currency_pattern.search(text)
    if match:
        return match.group(0).strip()

    # else, match plain numbers (avoid ratings)
    generic_pattern = re.compile(r"\d{1,4}(?:,\d{3})*(?:\.\d{1,2})?")
    for m in generic_pattern.finditer(text):
        value = m.group(0)
        try:
            num = float(value.replace(",", ""))
            # avoiding ratings
            if 0 < num <= 5:
                continue
            span_text = text[m.start():m.end() + 10].lower()
            if any(x in span_text for x in ["star", "rated", "/5", "/10"]):
                continue
            return f"${num:.2f}"
        except ValueError:
            continue

    return None


def fetch_with_webunlocker(url: str) -> str:
    BD_API_KEY = os.getenv("WEB_UNLOCKER_BRIGHTDATA")

    headers = {
        "Authorization": f"Bearer {BD_API_KEY}",
        "Content-Type": "application/json"
    }


    data = {
    "zone": "shopping_scraper",
    "url": url,
    "render": False,       
    "format": "json"         
}



    resp= requests.post(
        "https://api.brightdata.com/request",
        json=data,
        headers=headers
    )


    print("WebUnlocker status:", resp.status_code)

    if resp.status_code != 200:
        print("WebUnlocker error:", resp.text)
        return ""

    return resp.json()

def accept_cookies_if_present(page):
    """Best-effort cookie banner acceptor."""
    try:
        # common button texts
        texts = [
            "Accept All",
            "Accept all",
            "Accept Cookies",
            "Accept All Cookies"
            "Accept cookies",
            "I Agree",
            "I agree",
            "Allow all",
            "Got it",
            "ACCEPT"
        ]

        # try main page
        for txt in texts:
            btn = page.query_selector(f"button:has-text('{txt}')")
            if btn:
                print(f"Clicking cookie button: {txt}")
                btn.click()
                page.wait_for_timeout(500)
                return

        # try generic cookie banner containers
        possible_selectors = [
            "[id*='cookie'] button",
            "[class*='cookie'] button",
            "div[role='dialog'] button:has-text('Accept')",
        ]
        for sel in possible_selectors:
            btn = page.query_selector(sel)
            if btn:
                print(f"Clicking cookie button via selector: {sel}")
                btn.click()
                page.wait_for_timeout(500)
                return

        # try iframes 
        for frame in page.frames:
            if frame == page.main_frame:
                continue
            for txt in texts:
                btn = frame.query_selector(f"button:has-text('{txt}')")
                if btn:
                    print(f"Clicking cookie button in iframe: {txt}")
                    btn.click()
                    page.wait_for_timeout(500)
                    return

        print("No cookie banner found (or already accepted).")
    except Exception as e:
        print("Cookie accept failed (ignored):", e)



# playwright scraper
def scrape_with_playwright(url: str, max_products: int = 40):
    products = []
    seen_links = set()

    fetch_with_webunlocker(url)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context_args = {}

        context = browser.new_context(**context_args)

        page = context.new_page()

        page.goto(url, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(2000)


        # # Optional debug HTML dump
        # with open("debug_page.html", "w", encoding="utf-8") as f:
        #     f.write(page.content())

        # page.screenshot(path="full_page_screenshot.png")

        # scroll for lazy loaded products
        for _ in range(4):
            page.mouse.wheel(0, 2500)
            page.wait_for_timeout(800)
        
        accept_cookies_if_present(page)
        page.wait_for_timeout(1000)

        # debug HTML dump
        # with open("debug_page.html", "w", encoding="utf-8") as f:
        #     f.write(page.content())

        
        card_selector = (
            "article, "
            "li[class*='product'], "
            "div[class*='product'], "
            "section[class*='product'], "
            "div[data-test*='product'], "
            "li[data-test*='product'],"
            "div[class*='tile'], "
            "div[class*='card'], "
            "div.product-detail.product-wrapper"
        )
        card_elements = page.query_selector_all(card_selector)

        # UNIVERSAL_XPATH = """
        # //div[
        #     descendant::a[
        #         contains(@href, "product") or 
        #         contains(@href, "/p/") or 
        #         contains(@href, "/products/")
        #     ]
        #     and
        #     descendant::img
        #     and
        #     descendant::*[
        #         contains(text(), "$") or 
        #         contains(text(), ".")
        #     ]
        # ]
        # """

        # card_elements = page.locator(UNIVERSAL_XPATH).all()


        if not card_elements:
            browser.close()
            return []

        base_domain = url.split("/")[2]

        for card in card_elements:
            if len(products) >= max_products:
                break

            link_el = card.query_selector(
                "a[href*='product'], "
                "a[href*='/products/'], "
                "a[href*='/product/'], "
                "a[href*='item'], "
                "a[href*='shop']"
            )
            if not link_el:
                continue

            href = link_el.get_attribute("href")
            if not href:
                continue

            if href.startswith("/"):
                href = f"https://{base_domain}{href}"

            if href in seen_links:
                continue
            seen_links.add(href)

            img_el = card.query_selector("img")
            image = None
            if img_el:
                for attr in ["src", "data-src", "data-original", "data-srcset"]:
                    val = img_el.get_attribute(attr)
                    if not val:
                        continue
                    if " " in val or "," in val:
                        first = val.split(",")[0].strip()
                        image = first.split(" ")[0]
                    else:
                        image = val
                    if image:
                        image = image.split("?")[0]
                        break

            title = None

            heading = card.query_selector("h1, h2, h3, h4")
            if heading:
                t = heading.inner_text().strip()
                if t:
                    title = t

            if not title or title =="Activating this element will cause content on the page to be updated.":
                name_el = card.query_selector(
                    "[class*='title'], [class*='name'], [data-test*='title']"
                )
                if name_el:
                    t = name_el.inner_text().strip()
                    if t:
                        title = t

            if not title or  title =="Activating this element will cause content on the page to be updated.":
                for attr in ["aria-label", "title"]:
                    t = link_el.get_attribute(attr)
                    if t:
                        title = t.strip()
                        break

            if not title or  title =="Activating this element will cause content on the page to be updated.":
                t = link_el.inner_text().strip()
                if t:
                    title = t

            if not title and img_el:
                alt = img_el.get_attribute("alt")
                if alt:
                    title = alt.strip()

            if not title and href:
                slug = href.rstrip("/").split("/")[-1]
                slug = re.sub(r"[-_]+", " ", slug).title()
                title = slug

            card_text = card.inner_text() or ""
            price = extract_price_from_text(card_text)

            if not (title and price and image and href):
                continue

            if "£" in price:
                price = price.replace("£", "$")

            products.append({
                "title": title,
                "price": price,
                "link": href,
                "image": image,
            })

            print("used playwright")

        browser.close()

        print(products[:5])

        print("browser closed")

    return products



import requests
import os




# main scrape endpoint

@app.get("/scrape")
def scrape(url: str = Query(...)):
    """
    Unified scraper:
      1. Direct Shopify JSON endpoint (if applicable)
      2. Gemini block-based HTML/JSON-LD extraction (fast AI)
      3. Playwright fallback (slow but robust)
    """
    domain = url.split("/")[2]
    base = f"https://{domain}"

    # 1) shopify collection JSON (no external proxy)
    if "/collections/" in url:
        json_url = url.rstrip("/") + "/products.json"
        try:
            res = requests.get(json_url, timeout=15, headers={
                "User-Agent": "Mozilla/5.0"
            })
            if res.status_code == 200:
                data = res.json()
                if "products" in data:
                    products = []
                    for p in data["products"]:
                        title = p.get("title")
                        price_val = p.get("variants", [{}])[0].get("price")
                        price = f"${float(price_val):.2f}" if price_val else None
                        handle = p.get("handle")
                        link = f"{base}/products/{handle}" if handle else None
                        image = p.get("images", [{}])[0].get("src")

                        products.append({
                            "title": title,
                            "price": price,
                            "link": link,
                            "image": image,
                        })

                    return {
                        "url": url,
                        "source": "shopify_json",
                        "count": len(products),
                        "products": products,
                    }
        except Exception:
            pass


    # 3) playwright fallback
    try:
        products = scrape_with_playwright(url)
        if products:
            print("we have products")
            return {
                "url": url,
                "source": "playwright",
                "count": len(products),
                "products": products,
            }
    # except Exception:
    #     pass
    except Exception as e:
        print("Playwright error:", e)
        raise

    return {"url": url, "source": "none_found", "count": 0, "products": []}


# multi url endpoint

@app.post("/scrape-multi")
def scrape_multiple(urls: List[str]):
    """
    Scrape multiple URLs sequentially.
    Request body: ["url1", "url2", ...]
    """
    results = []

    for url in urls:
        try:
            data = scrape(url=url)
            products = data.get("products", [])[:40]

            results.append({
                "url": url,
                "source": data.get("source", "unknown"),
                "count": len(products),
                "products": products,
            })
        except Exception as e:
            results.append({
                "url": url,
                "error": str(e),
                "products": [],
            })

    return {"count": len(results), "results": results}
