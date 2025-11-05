from fastapi import FastAPI, Query
from typing import List
import requests
from bs4 import BeautifulSoup
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright
import os
import json
import re
import asyncio
import platform

# ensure async works properly on windows
if platform.system() == "Windows":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

load_dotenv()
API_KEY = os.getenv("SCRAPERAPI_KEY")

app = FastAPI()

# cross origin resource sharing
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def extract_price_from_text(text: str):
    """Extract the most likely price (not rating) from text."""
    if not text:
        return None

    # normalize whitespace
    text = text.strip()

    # prefer explicit currency symbols first
    currency_pattern = re.compile(
        r"(?:USD|US\$|CA\$|\$|£|€)\s?\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?"
    )
    match = currency_pattern.search(text)
    if match:
        return match.group(0).strip()

    # Otherwise, match plain numeric prices but avoid ratings
    generic_pattern = re.compile(r"\d{1,4}(?:,\d{3})*(?:\.\d{1,2})?")
    for m in generic_pattern.finditer(text):
        value = m.group(0)
        try:
            num = float(value.replace(",", ""))
            # skip typical rating-like numbers
            if 0 < num <= 5:
                continue
            # skip common patterns like "4.5 stars" or "Rated 4.9"
            span_text = text[m.start():m.end() + 10].lower()
            if any(x in span_text for x in ["star", "rated", "/5", "/10"]):
                continue
            return f"${num:.2f}"  # standardize formatting
        except ValueError:
            continue

    return None

def flatten_products(raw):
    """Normalize JSON-based product structures from different APIs."""
    flat = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                flat.append({
                    "title": item.get("title") or item.get("name"),
                    "price": extract_price_from_text(
                        str(item.get("price")) or str(item.get("priceRange", {}).get("min"))
                    ),
                    "link": item.get("handle") or item.get("url"),
                    "image": (
                        item.get("image", {}).get("src")
                        or item.get("featured_image", {}).get("url")
                        or None
                    )
                })
    return flat
def scrape_with_playwright(url, max_products=60):
    """Load Gymshark page, extract product cards, and fetch true titles/prices in parallel."""
    from playwright.sync_api import sync_playwright
    import concurrent.futures
    import time

    products = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        print(f"\n--- Navigating to: {url} ---")
        page.goto(url, wait_until="domcontentloaded", timeout=90000)

        # click cookie consent if exists
        try:
            page.wait_for_timeout(2000)
            consent = page.locator("button:has-text('Continue')")
            if consent.count() > 0:
                consent.first.click(timeout=3000)
                print("clicked cookie consent button")
        except Exception:
            print("no cookie banner found or clickable")

        # wait for product grid or cards
        try:
            page.wait_for_selector(
                "div[data-test='product-grid'] >> a[data-test='product-card-link'], \
                .product-tile, .product, a[href*='/product/'], a[href*='/p/']",
                timeout=20000
            )
            print("product grid or tiles rendered")
        except Exception:
            print("product grid not found within timeout, proceeding anyway")
            page.screenshot(path="debug_grid_missing.png", full_page=True)
            html_dump = page.content()
            with open("debug_grid_missing.html", "w", encoding="utf-8") as f:
                f.write(html_dump)
            print("saved debug_grid_missing.png and debug_grid_missing.html ")

        # proceed even if grid not found 
        page.wait_for_timeout(3000)  
        page.mouse.wheel(0, 3000)
        page.wait_for_timeout(1500)


        
        
        # scroll to trigger lazy loads
        for _ in range(6):
            page.mouse.wheel(0, 2000)
            page.wait_for_timeout(1500)

        # gather product cards
        product_cards = page.query_selector_all("a[data-test='product-card-link']")
        print(f"🛍️ Found {len(product_cards)} product cards")

        base_domain = url.split("/")[2]

        #basic data collection
        basic_products = []
        for idx, card in enumerate(product_cards[:max_products]):
            title_el = card.query_selector("[data-test='product-card-title']")
            price_el = card.query_selector("[data-test='product-card-price']")
            img_el = card.query_selector("img")

            title = title_el.inner_text().strip() if title_el else None
            price = price_el.inner_text().strip() if price_el else None
            link = card.get_attribute("href")
            image = img_el.get_attribute("src") if img_el else None

            if link and link.startswith("/"):
                link = f"https://{base_domain}{link}"

            basic_products.append({
                "title": title,
                "price": price,
                "link": link,
                "image": image
            })

        print(f"collected {len(basic_products)} base-level product cards")

        # parallel deep fetch for missing titles
        def fetch_details(prod):
            """Fetch accurate title/price from the product page."""
            if not prod["link"] or (prod["title"] and prod["title"].lower() != "product"):
                return prod
            local_page = context.new_page()
            try:
                local_page.goto(prod["link"], wait_until="domcontentloaded", timeout=20000)
                local_page.wait_for_selector("h1", timeout=5000)
                prod["title"] = local_page.locator("h1").inner_text().strip()
                prod["price"] = local_page.locator("[data-test='product-price']").inner_text().strip()
            except Exception:
                pass
            finally:
                local_page.close()
            return prod

        print("⚙️ Launching parallel detail fetches...")
        start_time = time.time()
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            products = list(executor.map(fetch_details, basic_products))

        print(f"deep fetches complete in {time.time() - start_time:.1f}s")
        print(f"extracted {len(products)} final products with accurate titles & prices")

        page.screenshot(path="gymshark_debug.png", full_page=True)
        print("📸 Saved final debug screenshot: gymshark_debug.png")

        browser.close()

    return products



@app.post("/scrape-multi")
def scrape_multiple(urls: List[str]):
    results = []
    for url in urls:
        try:
            result = scrape(url)  # reusing existing function
            results.append(result)
        except Exception as e:
            results.append({"url": url, "error": str(e)})
    return {"count": len(results), "results": results}


@app.get("/scrape")
def scrape(url: str = Query(...)):
    """Scrape Shopify or JS-rendered collection/product pages."""
    domain = url.split("/")[2]
    base = f"https://{domain}"

    # trying Shopify JSON endpoint first
    # checks if shopify page
    if "/collections/" in url:
        json_url = url.rstrip("/") + "/products.json"
        try:
            json_resp = requests.get("https://api.scraperapi.com", params={
                "api_key": API_KEY,
                "url": json_url,
                "respect_robots_txt": "true"
            })
            if json_resp.status_code == 200:
                # turn HTTP response body into Python dictionary
                data = json_resp.json()
                # check for expected structure
                if isinstance(data, dict) and "products" in data:
                    products = []
                    for p in data["products"]:
                        title = p.get("title")
                        # gets price of first variant (specific version of a product)
                        price_val = p.get("variants", [{}])[0].get("price")
                        price = f"${float(price_val):.2f}" if price_val else None
                        link = f"{base}/products/{p.get('handle')}"
                        image = p.get("images", [{}])[0].get("src")
                        products.append({
                            "title": title,
                            "price": price,
                            "link": link,
                            "image": image
                        })
                    if products:
                        return {"url": url, "source": "collection_json", "count": len(products), "products": products}
        except Exception as e:
            print("JSON scrape failed:", e)

    # fallback: HTML scrape with render=true, not working well yet
    try:
        html_resp = requests.get("https://api.scraperapi.com", params={
            "api_key": API_KEY,
            "url": url,
            "render": "true",
            "wait": "7000",
            "country_code": "us",
            "respect_robots_txt": "true"
        })
        # converts raw HTML into searchable tree of tags
        soup = BeautifulSoup(html_resp.text, "html.parser")
        # finding all product containers (add more later)

        product_cards = soup.select(
            "a[href*='/product'], a[href*='/shop/'], div[class*='product'], li[class*='product'], div[class*='card'], div[class*='grid'], article"
        )

        products = []
        seen_links = set()

        for card in product_cards:
            link_tag = card.find("a", href=True) or card
            link = link_tag.get("href")
            if not link or link in seen_links:
                continue
            seen_links.add(link)

            # if relative URL
            if link.startswith("/"):
                link = base + link

            title_tag = (card.find("h1") or card.find("h2") or card.find("h3") or card.get("aria-label") or card.get("title"))
            # title might not be BeautifulSoup tag
            title = title_tag.get_text(strip=True) if hasattr(title_tag, "get_text") else str(title_tag or "").strip()

            card_text = card.get_text(" ", strip=True)
            price = extract_price_from_text(card_text)

            img_tag = card.find("img")
            image = img_tag["src"] if img_tag and img_tag.has_attr("src") else None

            if not price:
                # check for data attributes
                for attr in ["data-price", "data-sale-price", "data-amount"]:
                    if card.has_attr(attr):
                        price = extract_price_from_text(card[attr])
                        break

            # handle lazy loaded images
            img_tag = card.find("img")
            image = None
            if img_tag:
                for attr in ["src", "data-src", "data-original", "data-srcset"]:
                    if img_tag.has_attr(attr):
                        image = img_tag[attr].split()[0]
                        break

            if title or price or image:
                products.append({
                    "title": title,
                    "price": price,
                    "link": link,
                    "image": image
                })

        if products:
            return {"url": url, "source": "collection_html", "count": len(products), "products": products}

    except Exception as e:
        print("Rendered scrape failed:", e)

    # --- Final fallback: Playwright ---
    try:
        products = scrape_with_playwright(url)
        if products:
            return {"url": url, "source": "playwright", "count": len(products), "products": products}
    except Exception as e:
        print("Playwright scrape failed:", e)

    return {"url": url, "source": "none_found", "products": [], "count": 0}
