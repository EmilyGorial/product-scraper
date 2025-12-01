from fastapi import FastAPI, Query
from typing import List
import requests
from bs4 import BeautifulSoup
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright
import os
import re
import asyncio
import platform

# ensure async works properly on windows 
if platform.system() == "Windows":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

load_dotenv()
API_KEY = os.getenv("SCRAPERAPI_KEY")

app = FastAPI()

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# helpers
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

    # else, match plain numbers but avoid ratings
    generic_pattern = re.compile(r"\d{1,4}(?:,\d{3})*(?:\.\d{1,2})?")
    for m in generic_pattern.finditer(text):
        value = m.group(0)
        try:
            num = float(value.replace(",", ""))
            # skip rating-like values
            if 0 < num <= 5:
                continue
            span_text = text[m.start():m.end() + 10].lower()
            if any(x in span_text for x in ["star", "rated", "/5", "/10"]):
                continue
            return f"${num:.2f}"
        except ValueError:
            continue

    return None


# playwright scraper 

def scrape_with_playwright(url: str, max_products: int = 40):
    products = []
    seen_links = set()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        page.goto(url, wait_until="networkidle", timeout=90000)

        # allow content to settle a bit
        page.wait_for_timeout(2000)

        # scroll to trigger lazy load on many sites
        for _ in range(4):
            page.mouse.wheel(0, 2500)
            page.wait_for_timeout(800)

        # generic product card heuristics
        card_selector = (
            "article, "
            "li[class*='product'], "
            "div[class*='product'], "
            "section[class*='product'], "
            "div[data-test*='product'], "
            "li[data-test*='product'],"
            "div[class*='tile'], "
            "div[class*='card'], "
        )
        card_elements = page.query_selector_all(card_selector)


        if not card_elements:
            browser.close()
            return []
            

        base_domain = url.split("/")[2]

        for card in card_elements:
            if len(products) >= max_products:
                break

            # link 
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

            # image
            img_el = card.query_selector("img")
            image = None
            if img_el:
                for attr in ["src", "data-src", "data-original", "data-srcset"]:
                    val = img_el.get_attribute(attr)
                    if not val:
                        continue
                    # take first URL
                    if " " in val or "," in val:
                        first_part = val.split(",")[0].strip()
                        image = first_part.split(" ")[0]
                    else:
                        image = val
                    # strip query params for cleanliness
                    if image:
                        image = image.split("?")[0]
                    if image:
                        break

            # title
            title = None

            # 1) h1–h4 inside card
            heading = card.query_selector("h1, h2, h3, h4")
            if heading:
                t = heading.inner_text().strip()
                if t:
                    title = t

            # 2) Anything with 'title' or 'name' in class
            if not title:
                name_el = card.query_selector(
                    "[class*='title'], [class*='name'], [data-test*='title']"
                )
                if name_el:
                    t = name_el.inner_text().strip()
                    if t:
                        title = t

            # 3) link aria-label or title attribute
            if not title:
                for attr in ["aria-label", "title"]:
                    t = link_el.get_attribute(attr)
                    if t:
                        title = t.strip()
                        break

            # 4) link inner text
            if not title:
                t = link_el.inner_text().strip()
                if t:
                    title = t

            # 5) image alt
            if not title and img_el:
                alt = img_el.get_attribute("alt")
                if alt:
                    title = alt.strip()

            # 6) slug from URL
            if not title and href:
                slug = href.rstrip("/").split("/")[-1]
                slug = re.sub(r"[-_]+", " ", slug)
                slug = slug.strip()
                if slug:
                    title = slug.title()

            # price 
            card_text = card.inner_text() or ""
            price = extract_price_from_text(card_text)

            if not (title and price and image and href):
                continue

            # normalize obvious currency symbols
            if "£" in price:
                price = price.replace("£", "$")

            products.append(
                {
                    "title": title,
                    "price": price,
                    "link": href,
                    "image": image,
                }
            )

        browser.close()

    return products



# multi URL endpoint

@app.post("/scrape-multi")
def scrape_multiple(urls: List[str]):
    """
    Scrape multiple URLs.
    - Expects JSON body: ["url1", "url2", ...]
    - Wraps each result in a consistent structure for the frontend.
    - Trims product lists to avoid huge payloads.
    """
    results = []

    for url in urls:
        try:
            data = scrape(url=url)

            if isinstance(data, dict):
                products = (data.get("products") or [])[:40]
                results.append(
                    {
                        "url": data.get("url", url),
                        "source": data.get("source", "unknown"),
                        "count": len(products),
                        "products": products,
                    }
                )
            else:
                products = (data or [])[:40]
                results.append(
                    {
                        "url": url,
                        "source": "unknown_raw",
                        "count": len(products),
                        "products": products,
                    }
                )
        except Exception as e:
            results.append(
                {
                    "url": url,
                    "error": str(e),
                    "products": [],
                }
            )

    return {"count": len(results), "results": results}



# single URL endpoint
@app.get("/scrape")
def scrape(url: str = Query(...)):
    """
    Scrape Shopify or static/JS collection/product pages.

    Order:
      1. Shopify /collections/.../products.json via ScraperAPI
      2. HTML via ScraperAPI
      3. Generic Playwright fallback (STRICT mode)
    """
    domain = url.split("/")[2]
    base = f"https://{domain}"

    #1) shopify JSON
    if "/collections/" in url:
        json_url = url.rstrip("/") + "/products.json"
        try:
            json_resp = requests.get(
                "https://api.scraperapi.com",
                params={
                    "api_key": API_KEY,
                    "url": json_url,
                    "respect_robots_txt": "true",
                },
                timeout=20,
            )
            if json_resp.status_code == 200:
                data = json_resp.json()
                if isinstance(data, dict) and "products" in data:
                    products = []
                    for p in data["products"]:
                        title = p.get("title")
                        price_val = p.get("variants", [{}])[0].get("price")
                        price = f"${float(price_val):.2f}" if price_val else None
                        handle = p.get("handle")
                        link = f"{base}/products/{handle}" if handle else None
                        image = p.get("images", [{}])[0].get("src")

                        if title or price or image:
                            products.append(
                                {
                                    "title": title,
                                    "price": price,
                                    "link": link,
                                    "image": image,
                                }
                            )

                    if products:
                        return {
                            "url": url,
                            "source": "collection_json",
                            "count": len(products),
                            "products": products,
                        }
        except Exception:
            pass  

    # 2)HTML via ScraperAPI
    html_products = []
    try:
        html_resp = requests.get(
            "https://api.scraperapi.com",
            params={
                "api_key": API_KEY,
                "url": url,
                "render": "true",
                "wait": "7000",
                "country_code": "us",
                "respect_robots_txt": "true",
            },
            timeout=30,
        )

        soup = BeautifulSoup(html_resp.text, "html.parser")

        product_cards = soup.select(
            "a[href*='/product'], a[href*='/shop/'], "
            "div[class*='product'], li[class*='product'], "
            "div[class*='card'], div[class*='grid'], article"
        )

        seen_links = set()
        for card in product_cards:
            link_tag = card.find("a", href=True) or card
            link = link_tag.get("href")
            if not link or link in seen_links:
                continue
            seen_links.add(link)

            if link.startswith("/"):
                link = base + link

            title_tag = (
                card.find("h1")
                or card.find("h2")
                or card.find("h3")
                or card.get("aria-label")
                or card.get("title")
            )
            title = (
                title_tag.get_text(strip=True)
                if hasattr(title_tag, "get_text")
                else str(title_tag or "").strip()
            )
            if title and title.strip().lower() == "product":
                title = None

            card_text = card.get_text(" ", strip=True)
            price = extract_price_from_text(card_text)

            image = None
            img_tag = card.find("img")
            if img_tag:
                for attr in ["src", "data-src", "data-original", "data-srcset"]:
                    if img_tag.has_attr(attr):
                        image = img_tag[attr].split()[0]
                        break

            if title or price or image:
                html_products.append(
                    {
                        "title": title,
                        "price": price,
                        "link": link,
                        "image": image,
                    }
                )

        if html_products:
            return {
                "url": url,
                "source": "collection_html",
                "count": len(html_products),
                "products": html_products,
            }
    except Exception:
        pass  


    # 3) playwright 
    try:
        products = scrape_with_playwright(url)
        if products:
            return {
                "url": url,
                "source": "playwright_generic",
                "count": len(products),
                "products": products,
            }
    except Exception:
        pass

    return {"url": url, "source": "none_found", "products": [], "count": 0}
