from fastapi import FastAPI, Query
from typing import List
import requests
from bs4 import BeautifulSoup
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
import os
import json
import re

load_dotenv()
API_KEY = os.getenv("SCRAPERAPI_KEY")

app = FastAPI()

#cross origin resource sharing
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def extract_price_from_text(text):
    """Extract first number that looks like a price, e.g., 23.99 or $23.99"""
    if not text:
        return None
    #searches for "$" or one or more digits or a decimal point followed by 1-2 digits
    match = re.search(r"\$?\d+(?:\.\d{1,2})?", text)
    return match.group(0) if match else None


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

    #trying Shopify JSON endpoint first
    
    #checks if shopify page
    if "/collections/" in url:
        json_url = url.rstrip("/") + "/products.json"
        try:
            json_resp = requests.get("https://api.scraperapi.com", params={
                "api_key": API_KEY,
                "url": json_url,
                "respect_robots_txt": "true"
            })
            if json_resp.status_code == 200:
                #turn HTTP response body into Python dictionary
                data = json_resp.json()
                #check for expected structure
                if isinstance(data, dict) and "products" in data:
                    products = []
                    for p in data["products"]:
                        title = p.get("title")
                        #gets price of first variant(specific version of a product)
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
        #converts raw HTML into searchable tree of tags
        soup = BeautifulSoup(html_resp.text, "html.parser")
        #finding all product containers (add more later)
        product_cards = soup.select(
            "a[href*='/products/'], div[class*=product], li[class*=product], div[class*=item]"
        )

        products = []
        
        seen_links = set()
        for card in product_cards:
            link_tag = card.find("a", href=True) or card
            link = link_tag.get("href")
            if not link or "/products/" not in link or link in seen_links:
                continue
            seen_links.add(link)

            #if relative URL
            if link.startswith("/"):
                link = base + link

            title_tag = card.find("h1") or card.find("h2") or card.find("h3") or card.get("aria-label")
            #title might not be beautfiulsoup tag
            title = title_tag.get_text(strip=True) if hasattr(title_tag, "get_text") else str(title_tag or "").strip()

            card_text = card.get_text(" ", strip=True)
            price = extract_price_from_text(card_text)

            img_tag = card.find("img")
            image = img_tag["src"] if img_tag and img_tag.has_attr("src") else None

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

    return {"url": url, "source": "none_found", "products": [], "count": 0}
