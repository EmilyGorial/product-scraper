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
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

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


# playwright scraper

def scrape_with_playwright(url: str, max_products: int = 40):
    products = []
    seen_links = set()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        page.goto(url, wait_until="networkidle", timeout=90000)

        # wait to settle content
        page.wait_for_timeout(2000)

        # scroll a bit to trigger lazy loading
        for _ in range(4):
            page.mouse.wheel(0, 2500)
            page.wait_for_timeout(800)

        card_selector = (
            "article, "
            "li[class*='product'], "
            "div[class*='product'], "
            "section[class*='product'], "
            "div[data-test*='product'], "
            "li[data-test*='product'],"
            "div[class*='tile'], "
            "div[class*='card']"
        )
        card_elements = page.query_selector_all(card_selector)

        if not card_elements:
            browser.close()
            return []

        base_domain = url.split("/")[2]

        for card in card_elements:
            if len(products) >= max_products:
                break

            # product link
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
                    if " " in val or "," in val:
                        first = val.split(",")[0].strip()
                        image = first.split(" ")[0]
                    else:
                        image = val
                    if image:
                        image = image.split("?")[0]
                        break

            # title
            title = None

            heading = card.query_selector("h1, h2, h3, h4")
            if heading:
                t = heading.inner_text().strip()
                if t:
                    title = t

            if not title:
                name_el = card.query_selector(
                    "[class*='title'], [class*='name'], [data-test*='title']"
                )
                if name_el:
                    t = name_el.inner_text().strip()
                    if t:
                        title = t

            if not title:
                for attr in ["aria-label", "title"]:
                    t = link_el.get_attribute(attr)
                    if t:
                        title = t.strip()
                        break

            if not title:
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



# gemini block based scrape

def extract_product_blocks(url: str):
    """
    Return a list of small HTML or JSON-LD snippets that likely represent products.
    Each item is a tuple: ("html" | "json-ld", content).
    """
    resp = requests.get(url, timeout=15, headers={
        "User-Agent": "Mozilla/5.0"
    })
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    blocks = []

    # 1) JSON-LD product schema blocks
    for script in soup.find_all("script", type="application/ld+json"):
        if not script.string:
            continue
        try:
            data = json.loads(script.string)
        except Exception:
            continue

        def handle_json_ld(obj):
            if not isinstance(obj, dict):
                return
            t = obj.get("@type")
            if t in ["Product", "Offer"]:
                blocks.append(("json-ld", obj))

        if isinstance(data, list):
            for item in data:
                handle_json_ld(item)
        else:
            handle_json_ld(data)

    # 2) HTML block candidates via class names
    candidate_selectors = [
        "[class*=product]",
        "[class*=grid]",
        "[class*=item]",
        "[class*=tile]",
        "[class*=card]",
        "article",
        "li",
    ]

    for sel in candidate_selectors:
        for el in soup.select(sel):
            text = el.get_text(" ", strip=True)
            # heuristic: only keep blocks that look price-y
            if any(word in text.lower() for word in ["$", "price", "sale", "now", "was"]):
                blocks.append(("html", str(el)))

    # 3) Anchor-based candidates
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if any(x in href for x in ["/product", "/products", "/shop", "/p/"]):
            parent_html = str(a.parent)
            blocks.append(("html", parent_html))

    return blocks


def gemini_extract_from_block(block, url: str):
    """
    Convert one product block into structured product info via Gemini.
    block is a tuple: ("html" | "json-ld", content)
    """
    block_type, content = block

    # JSON-LD: already structured, just normalize
    if block_type == "json-ld":
        name = content.get("name")
        image = content.get("image")
        if isinstance(image, list):
            image = image[0] if image else None

        offers = content.get("offers") or {}
        if isinstance(offers, list):
            offers = offers[0] if offers else {}

        price_raw = offers.get("price") or offers.get("priceSpecification", {}).get("price")
        price = extract_price_from_text(str(price_raw)) if price_raw else None

        return {
            "title": name,
            "price": price,
            "link": url,  # JSON-LD often doesn't have deep links; we fall back to page URL
            "image": image,
        }

    # HTML: ask Gemini to interpret
    prompt = f"""
Extract a single product from the HTML below.

Return ONLY this JSON:
{{
  "title": "...",
  "price": "...",
  "link": "...",
  "image": "..."
}}

Rules:
- If there is a product link, include it as "link".
- If the link is relative, resolve it against: {url}
- "price" should include a currency symbol if present in the HTML.
- If some field is missing, use null for that field.
- Do NOT invent products that are not in the HTML.

HTML:
{content}
"""

    model = genai.GenerativeModel(
        "gemini-2.0-flash-lite",
        generation_config={"response_mime_type": "application/json"},
    )

    try:
        out = model.generate_content(prompt).text.strip()
        data = json.loads(out)
    except Exception:
        return None

    # Resolve relative link
    link = data.get("link") or ""
    if link.startswith("/"):
        base = url.split("/")[0] + "//" + url.split("/")[2]
        link = base + link

    # Normalize price
    price = data.get("price")
    if price:
        price = extract_price_from_text(str(price))

    return {
        "title": data.get("title"),
        "price": price,
        "link": link or url,
        "image": data.get("image"),
    }


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

    # 2) gemini scrape
    try:
        blocks = extract_product_blocks(url)
        ai_products = []
        seen_links = set()

        for block in blocks[:20]:  # limit blocks to control cost
            p = gemini_extract_from_block(block, url)
            if not p:
                continue
            if not p.get("title"):
                continue

            link = p.get("link")
            # Deduplicate by link if present
            if link and link in seen_links:
                continue
            if link:
                seen_links.add(link)

            ai_products.append(p)

        if ai_products:
            return {
                "url": url,
                "source": "gemini_blocks",
                "count": len(ai_products),
                "products": ai_products[:40],
            }
    except Exception:
        pass

    # 3) playwright fallback
    try:
        products = scrape_with_playwright(url)
        if products:
            return {
                "url": url,
                "source": "playwright",
                "count": len(products),
                "products": products,
            }
    except Exception:
        pass

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
