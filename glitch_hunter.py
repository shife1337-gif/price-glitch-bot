import os
import json
import time
import re
import xml.etree.ElementTree as ET
import requests

# ==============================================================================
# CONFIGURATION & SECRETS (Loaded automatically from GitHub Secrets)
# ==============================================================================
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
BESTBUY_API_KEY = os.environ.get("BESTBUY_API_KEY", "").strip()
KEEPA_RSS_URL = os.environ.get("KEEPA_RSS_URL", "").strip()

MIN_DISCOUNT_PCT = int(os.environ.get("MIN_DISCOUNT_PCT", "75"))
SEEN_FILE = "seen_deals.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}

# Store-specific embed colors
STORE_COLORS = {
    "Best Buy": 0x0046BE,   # Best Buy Blue
    "Target": 0xCC0000,     # Target Red
    "Walmart": 0x0071CE,    # Walmart Blue
    "Amazon": 0xFF9900,     # Amazon Orange
    "Multi-Store Glitch": 0xFF0000, # Bright Red Alert
}

# ==============================================================================
# DUPLICATE TRACKER (Prevents spamming the same deal twice)
# ==============================================================================
def load_seen_deals():
    if os.path.exists(SEEN_FILE):
        try:
            with open(SEEN_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return set(data[-2000:])  # Keep the most recent 2,000 IDs
        except Exception:
            return set()
    return set()

def save_seen_deals(seen_set):
    try:
        with open(SEEN_FILE, "w", encoding="utf-8") as f:
            json.dump(list(seen_set)[-2000:], f, indent=2)
    except Exception as e:
        print(f"Error saving seen deals: {e}")

# ==============================================================================
# DISCORD WEBHOOK SENDER
# ==============================================================================
def send_discord_alert(store, title, old_price, new_price, discount_pct, url, image_url="", category="General"):
    if not DISCORD_WEBHOOK_URL:
        print(f"[DRY RUN - NO WEBHOOK] [{store}] {discount_pct}% OFF: {title} (${new_price}) -> {url}")
        return

    is_extreme = discount_pct >= 80
    headline = (
        f"@everyone 🚨 **EXTREME {discount_pct}% OFF PRICE GLITCH ALERT!** 🚨"
        if is_extreme
        else f"🔥 **{discount_pct}% OFF Deal Alert ({store})**"
    )

    fields = [
        {"name": "🏬 Store", "value": f"**{store}**", "inline": True},
        {"name": "🏷️ Category", "value": category, "inline": True},
        {"name": "📉 Discount", "value": f"**{discount_pct}% OFF**", "inline": True},
    ]

    if old_price and new_price is not None:
        fields.extend([
            {"name": "❌ Regular Price", "value": f"~~${old_price:.2f}~~", "inline": True},
            {"name": "✅ Glitch Price", "value": f"**${new_price:.2f}**", "inline": True},
            {"name": "💰 Total Savings", "value": f"${(old_price - new_price):.2f}", "inline": True},
        ])

    embed = {
        "title": f"[{discount_pct}% OFF] {title[:220]}",
        "url": url,
        "color": 0xFF0000 if is_extreme else STORE_COLORS.get(store, 0x00FF00),
        "fields": fields,
        "footer": {
            "text": "Cloud Glitch Hunter • Checkout immediately — price errors can be canceled anytime."
        },
    }

    if image_url and image_url.startswith("http"):
        embed["thumbnail"] = {"url": image_url}

    payload = {
        "username": "Captain Hook • Glitch Hunter",
        "content": headline,
        "embeds": [embed],
    }

    try:
        resp = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=15)
        if resp.status_code in (200, 204):
            print(f"[SENT] {store}: {title[:60]} ({discount_pct}% OFF)")
        else:
            print(f"[DISCORD ERROR] Status {resp.status_code}: {resp.text}")
        time.sleep(1.5)  # Respect Discord rate limits
    except Exception as e:
        print(f"[WEBHOOK EXCEPTION] {e}")

# ==============================================================================
# ENGINE 1: BEST BUY DIRECT CATALOG API (Tech, Electronics, Appliances)
# ==============================================================================
def scan_bestbuy_api(seen_deals):
    if not BESTBUY_API_KEY:
        print("[Best Buy] No BESTBUY_API_KEY set. Skipping direct API scan (still covered by multi-store scanner).")
        return

    print("[Best Buy] Scanning live API for >= 75% off markdowns & clearance...")
    url = (
        f"https://api.bestbuy.com/v1/products(onSale=true&percentSavings>={MIN_DISCOUNT_PCT}&regularPrice>=15)"
        f"?format=json&show=sku,name,regularPrice,salePrice,percentSavings,url,image,categoryPath.name"
        f"&pageSize=50&sort=percentSavings.desc&apiKey={BESTBUY_API_KEY}"
    )

    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        if resp.status_code != 200:
            print(f"[Best Buy] API returned status {resp.status_code}")
            return
        data = resp.json()
        for item in data.get("products", []):
            sku = f"bby_{item.get('sku')}"
            if sku in seen_deals:
                continue

            reg_price = float(item.get("regularPrice", 0))
            sale_price = float(item.get("salePrice", 0))
            if reg_price <= 0 or sale_price <= 0:
                continue

            pct = round(((reg_price - sale_price) / reg_price) * 100)
            if pct >= MIN_DISCOUNT_PCT:
                seen_deals.add(sku)
                cats = item.get("categoryPath", [])
                cat_name = cats[1]["name"] if len(cats) > 1 else "Tech & Electronics"
                send_discord_alert(
                    store="Best Buy",
                    title=item.get("name", "Best Buy Deal"),
                    old_price=reg_price,
                    new_price=sale_price,
                    discount_pct=pct,
                    url=item.get("url", "https://www.bestbuy.com"),
                    image_url=item.get("image", ""),
                    category=cat_name,
                )
    except Exception as e:
        print(f"[Best Buy] Error: {e}")

# ==============================================================================
# ENGINE 2: TARGET REDSKY PUBLIC API (Tech, Clothing, Home & Clearance)
# ==============================================================================
def scan_target_redsky(seen_deals):
    print("[Target] Scanning Target RedSky API for 75%+ clearance & price drops...")
    # Public visitor key used by Target.com frontend
    visitor_key = "9f36aeafbe60771e321a7cc95a78140772ab3e96"
    search_queries = [
        ("electronics clearance", "Tech & Electronics"),
        ("mens womens clothing clearance", "Clothing & Apparel"),
        ("home kitchen clearance", "Home & Kitchen"),
        ("video games clearance", "Gaming"),
        ("apple sony samsung bose", "Tech & Audio"),
    ]

    for keyword, category_label in search_queries:
        endpoint = (
            "https://redsky.target.com/redsky_aggregations/v1/web/plp_search_v2"
            f"?key={visitor_key}&channel=WEB&count=24&default_purchasability_filter=true"
            f"&include_sponsored=false&keyword={requests.utils.quote(keyword)}"
            "&offset=0&page=%2Fs%2Fclearance&platform=desktop&pricing_store_id=3991"
        )
        try:
            resp = requests.get(endpoint, headers=HEADERS, timeout=15)
            if resp.status_code != 200:
                continue
            data = resp.json()
            products = data.get("data", {}).get("search", {}).get("products", [])
            for prod in products:
                tcin = f"tgt_{prod.get('tcin')}"
                if tcin in seen_deals:
                    continue

                price_info = prod.get("price", {})
                current_price = price_info.get("current_retail") or price_info.get("current_retail_min")
                reg_price = price_info.get("reg_retail") or price_info.get("reg_retail_max")

                if not current_price or not reg_price or reg_price <= current_price:
                    continue

                pct = round(((reg_price - current_price) / reg_price) * 100)
                if pct >= MIN_DISCOUNT_PCT:
                    seen_deals.add(tcin)
                    item_info = prod.get("item", {})
                    title = item_info.get("product_description", {}).get("title", "Target Clearance Item")
                    url = item_info.get("enrichment", {}).get("buy_url", f"https://www.target.com/p/-/A-{prod.get('tcin')}")
                    image = item_info.get("enrichment", {}).get("images", {}).get("primary_image_url", "")
                    send_discord_alert(
                        store="Target",
                        title=title,
                        old_price=float(reg_price),
                        new_price=float(current_price),
                        discount_pct=pct,
                        url=url,
                        image_url=image,
                        category=category_label,
                    )
        except Exception as e:
            print(f"[Target] Query '{keyword}' note: {e}")

# ==============================================================================
# ENGINE 3: KEEPA AMAZON 75%-99% DROP FEED (If Configured)
# ==============================================================================
def scan_keepa_amazon_rss(seen_deals):
    if not KEEPA_RSS_URL:
        return
    print("[Amazon Keepa] Checking custom Keepa 75%+ drop RSS feed...")
    try:
        resp = requests.get(KEEPA_RSS_URL, headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            return
        root = ET.fromstring(resp.content)
        for item in root.findall(".//item"):
            title = item.findtext("title", "")
            link = item.findtext("link", "")
            guid = f"keepa_{item.findtext('guid', link)}"
            if guid in seen_deals:
                continue

            seen_deals.add(guid)
            # Extract percentage if present in Keepa title
            pct_match = re.search(r"(\d{2})%\s*(?:off|drop|-)", title, re.IGNORECASE)
            pct = int(pct_match.group(1)) if pct_match else 80
            send_discord_alert(
                store="Amazon",
                title=title,
                old_price=None,
                new_price=None,
                discount_pct=pct,
                url=link,
                category="Amazon Instant Drop",
            )
    except Exception as e:
        print(f"[Amazon Keepa] Error: {e}")

# ==============================================================================
# ENGINE 4: MULTI-STORE REAL-TIME GLITCH AGGREGATOR
# (Catches Walmart, Amazon, Best Buy, Target & Clothing 75%+ Glitches Instantly)
# ==============================================================================
def parse_discount_and_prices(text):
    """Detects explicit percentage drops (e.g., 80% off) OR calculates math from 'was $100 now $20'."""
    text_clean = text.replace(",", "")

    # 1. Check explicit percentage mentions (e.g., "80% off", "85% discount", "-75%")
    pct_matches = re.findall(r"(\d{2,3})%\s*(?:off|discount|drop|savings)", text_clean, re.IGNORECASE)
    for m in pct_matches:
        val = int(m)
        if MIN_DISCOUNT_PCT <= val <= 99:
            return val, None, None

    # 2. Check price comparison patterns (e.g., "$19.99 (was $120)" or "Reg $200 Now $35")
    prices = [float(p) for p in re.findall(r"\$(\d+(?:\.\d{1,2})?)", text_clean)]
    if len(prices) >= 2:
        low, high = min(prices), max(prices)
        if high >= 15 and low > 0 and high > low:
            calc_pct = round(((high - low) / high) * 100)
            if MIN_DISCOUNT_PCT <= calc_pct <= 99:
                # Only treat two-price comparison as a true discount if comparison words exist
                if re.search(r"(was|reg|orig|retail|list|down from|instead of|save|off|glitch|error|clearance)", text_clean, re.IGNORECASE):
                    return calc_pct, high, low

    # 3. Check explicit "price error" / "glitch" keywords
    if re.search(r"(price error|price glitch|pricing error|insane glitch|free after)", text_clean, re.IGNORECASE):
        return 85, None, None

    return 0, None, None

def detect_store_and_category(title, link):
    combined = f"{title} {link}".lower()
    store = "Multi-Store Glitch"
    if "walmart" in combined:
        store = "Walmart"
    elif "amazon" in combined or "amzn.to" in combined or "a.co" in combined:
        store = "Amazon"
    elif "bestbuy" in combined or "best buy" in combined:
        store = "Best Buy"
    elif "target" in combined:
        store = "Target"

    category = "Everything 75%+ Off"
    if any(k in combined for k in ["shirt", "pants", "jacket", "hoodie", "shoes", "sneaker", "apparel", "clothing", "nike", "adidas", "levi"]):
        category = "Clothing & Apparel"
    elif any(k in combined for k in ["tv", "laptop", "monitor", "gpu", "ssd", "headphones", "earbuds", "apple", "ipad", "gaming", "keyboard", "mouse", "camera"]):
        category = "Tech & Electronics"

    return store, category

def scan_multistore_glitch_feeds(seen_deals):
    print("[Multi-Store Scanner] Scanning Walmart, Amazon, Best Buy, Target & Clothing feeds for 75%+ drops...")
    feeds = [
        # Combined Reddit Deal Subreddits (Tech, Clothing, All-Store Deals, Console Deals)
        "https://www.reddit.com/r/deals+buildapcsales+frugalmalefashion+frugalfemalefashion+ConsoleDeals+walmart/new/.rss",
        # Slickdeals Hot & Frontpage Deals
        "https://slickdeals.net/newsearch.php?mode=popdeals&searcharea=deals&searchin=first&rss=1",
        # DealNews Instant RSS
        "https://www.dealnews.com/c142/Electronics/?rss=1",
        "https://www.dealnews.com/c202/Clothing-Accessories/?rss=1",
    ]

    for feed_url in feeds:
        try:
            resp = requests.get(feed_url, headers=HEADERS, timeout=15)
            if resp.status_code != 200:
                continue

            root = ET.fromstring(resp.content)
            # Handle both RSS (<item>) and Atom (<entry>) feeds
            entries = root.findall(".//item") + root.findall(".//{http://www.w3.org/2005/Atom}entry")

            for entry in entries[:35]:
                title = (
                    entry.findtext("title")
                    or entry.findtext("{http://www.w3.org/2005/Atom}title")
                    or ""
                )
                link = entry.findtext("link") or ""
                if not link:
                    link_elem = entry.find("{http://www.w3.org/2005/Atom}link")
                    if link_elem is not None:
                        link = link_elem.attrib.get("href", "")

                item_id = f"feed_{hash(title[:80])}"
                if item_id in seen_deals or not title:
                    continue

                pct, old_p, new_p = parse_discount_and_prices(title)
                if pct >= MIN_DISCOUNT_PCT:
                    seen_deals.add(item_id)
                    store, category = detect_store_and_category(title, link)
                    send_discord_alert(
                        store=store,
                        title=title,
                        old_price=old_p,
                        new_price=new_p,
                        discount_pct=pct,
                        url=link,
                        category=category,
                    )
        except Exception as e:
            print(f"[Multi-Store Scanner] Feed check note: {e}")

# ==============================================================================
# MAIN EXECUTION
# ==============================================================================
if __name__ == "__main__":
    print("=== STARTING 24/7 CLOUD GLITCH HUNTER ===")
    seen = load_seen_deals()
    initial_count = len(seen)

    scan_bestbuy_api(seen)
    scan_target_redsky(seen)
    scan_keepa_amazon_rss(seen)
    scan_multistore_glitch_feeds(seen)

    save_seen_deals(seen)
    print(f"=== SCAN COMPLETE: {len(seen) - initial_count} new 75%+ deals alerted ===")
