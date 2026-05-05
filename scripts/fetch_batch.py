import os
import sys
import time
import json
import urllib.request
import urllib.parse
from datetime import datetime, timedelta
import re
import argparse
from pathlib import Path

# Add current directory to path to import to_obsidian
sys.path.append(os.path.dirname(__file__))
try:
    import to_obsidian
except ImportError:
    to_obsidian = None

# Default settings
CAMOFOX_URL = "http://localhost:9377"
DEFAULT_OUT_DIR = "exported_tweets"

def get_camofox_snapshot(url, wait_time=12):
    """Opens a URL in Camofox, waits, gets snapshot, and closes the tab."""
    try:
        # 1. Open Tab
        req = urllib.request.Request(
            f"{CAMOFOX_URL}/tabs", 
            data=json.dumps({"userId": "export", "sessionKey": "export", "url": url}).encode(),
            headers={"Content-Type": "application/json"}, method="POST"
        )
        resp = urllib.request.urlopen(req, timeout=10)
        tab_id = json.loads(resp.read().decode())["tabId"]
    except Exception as e:
        print(f"Error opening tab for {url}: {e}")
        return None

    time.sleep(wait_time)

    # 2. Snapshot
    try:
        req = urllib.request.Request(f"{CAMOFOX_URL}/tabs/{tab_id}/snapshot?userId=export")
        resp = urllib.request.urlopen(req, timeout=10)
        snapshot = json.loads(resp.read().decode())["snapshot"]
    except Exception as e:
        print(f"Error getting snapshot: {e}")
        snapshot = None

    # 3. Close Tab
    try:
        req = urllib.request.Request(f"{CAMOFOX_URL}/tabs/{tab_id}", method="DELETE")
        urllib.request.urlopen(req, timeout=5)
    except:
        pass

    return snapshot

def extract_next_cursor(snapshot):
    """Extracts the next-page cursor from a Nitter snapshot."""
    lines = snapshot.split('\n')
    for i, line in enumerate(lines):
        if 'link "Load more"' in line:
            for j in range(i + 1, min(len(lines), i + 5)):
                url_line = lines[j].strip()
                m = re.search(r'[?&]cursor=([^"&\s]+)', url_line)
                if m:
                    return urllib.parse.unquote(m.group(1))
    return None

def parse_nitter_snapshot(snapshot, username=None):
    """Basic parser for Camofox snapshot text tree."""
    tweets = []
    lines = snapshot.split('\n')
    seen_ids = set()
    current_tweet = None
    
    # If no username provided, we try to match any status ID
    status_pattern = fr'/url:\s+/{username}/status/(\d+)#m' if username else r'/url:\s+/\w+/status/(\d+)#m'
    
    for line in lines:
        line = line.strip()
        
        # Match tweet ID from URL
        m = re.search(status_pattern, line, re.IGNORECASE)
        if m:
            tid = m.group(1)
            if tid in seen_ids:
                continue
            
            if current_tweet:
                tweets.append(current_tweet)
                
            seen_ids.add(tid)
            current_tweet = {"tweet_id": tid, "text": "", "stats": "", "media": []}
            continue
            
        if current_tweet:
            if line.startswith("- text:"):
                text_val = line.replace("- text:", "").strip()
                if '' in text_val or '' in text_val: # Stats row
                    current_tweet["stats"] = text_val
                else:
                    if current_tweet["text"]:
                        current_tweet["text"] += "\n" + text_val
                    else:
                        current_tweet["text"] = text_val
            elif line.startswith("- /url: /pic/"):
                img_url = line.split("/pic/")[-1]
                current_tweet["media"].append(urllib.parse.unquote(img_url))

    if current_tweet:
        tweets.append(current_tweet)
        
    return tweets

def fetch_single_tweet_details(tweet_id, username=None):
    """Uses FxTwitter to get the exact JSON data for a single tweet."""
    # FxTwitter API doesn't actually strictly require the correct username in the path 
    # to find the status ID, but it's good practice.
    path_user = username or "i"
    url = f"https://api.fxtwitter.com/{path_user}/status/{tweet_id}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            return data.get("tweet")
    except Exception as e:
        print(f"  Error fetching details for {tweet_id}: {e}")
        return None

def main():
    parser = argparse.ArgumentParser(description="Export tweets/search results day-by-day using Camofox + Nitter.")
    parser.add_argument("--user", "-u", help="Username to export (e.g. GuarEmperor)")
    parser.add_argument("--query", "-q", help="Custom search query (e.g. '#AI' or 'keyword'). Overrides --user logic.")
    parser.add_argument("--start", "-s", default="2021-01-01", help="Start date (YYYY-MM-DD), default 2021-01-01")
    parser.add_argument("--end", "-e", help="End date (YYYY-MM-DD), default is today")
    parser.add_argument("--output", "-o", default=DEFAULT_OUT_DIR, help=f"Output directory, default: {DEFAULT_OUT_DIR}")
    parser.add_argument("--instance", default="nitter.tiekoetter.com", help="Nitter instance to use")
    parser.add_argument("--wait", type=int, default=12, help="Wait time for Camofox snapshot in seconds (default: 12)")
    parser.add_argument("--markdown", action="store_true", help="Also export to Obsidian Markdown (requires scripts/to_obsidian.py)")
    
    args = parser.parse_args()

    if not args.user and not args.query:
        print("Error: Either --user or --query must be provided.")
        parser.print_help()
        sys.exit(1)

    start_date = datetime.strptime(args.start, "%Y-%m-%d")
    if args.end:
        end_date = datetime.strptime(args.end, "%Y-%m-%d")
    else:
        end_date = datetime.now()
    
    os.makedirs(args.output, exist_ok=True)
    
    target_desc = f"query '{args.query}'" if args.query else f"user @{args.user}"
    print(f"Starting export for {target_desc} from {args.start} to {end_date.strftime('%Y-%m-%d')}")
    
    curr = start_date
    while curr <= end_date:
        next_d = curr + timedelta(days=1)
        d_str = curr.strftime("%Y-%m-%d")
        nd_str = next_d.strftime("%Y-%m-%d")
        out_file = os.path.join(args.output, f"{d_str}.json")
        
        if os.path.exists(out_file):
            print(f"Skipping {d_str} (already exists)")
            curr = next_d
            continue
            
        print(f"\n=> Fetching results for {d_str}...")
        
        all_day_tweets = []
        cursor = None
        page = 1
        
        # Build query
        if args.query:
            base_query = f"{args.query} since:{d_str} until:{nd_str}"
        else:
            base_query = f"from:{args.user} since:{d_str} until:{nd_str}"

        while True:
            params = {"f": "tweets", "q": base_query}
            if cursor:
                params["cursor"] = cursor
                
            query_str = urllib.parse.urlencode(params)
            url = f"https://{args.instance}/search?{query_str}"
            
            print(f"  Page {page}: Fetching {url}...")
            snapshot = get_camofox_snapshot(url, wait_time=args.wait)
            
            if not snapshot:
                print(f"  ❌ Failed to fetch page {page}. (Check if Camofox is running at {CAMOFOX_URL})")
                break
                
            page_tweets = parse_nitter_snapshot(snapshot, args.user)
            new_count = 0
            for tw in page_tweets:
                if not any(t['tweet_id'] == tw['tweet_id'] for t in all_day_tweets):
                    all_day_tweets.append(tw)
                    new_count += 1
            
            print(f"  Found {len(page_tweets)} tweets ({new_count} new) on page {page}.")
            
            cursor = extract_next_cursor(snapshot)
            if not cursor:
                break
                
            page += 1
            time.sleep(2)

        if all_day_tweets:
            # Enrich tweets
            enriched_tweets = []
            for i, tw in enumerate(all_day_tweets):
                print(f"  - Enriching tweet {i+1}/{len(all_day_tweets)} ({tw['tweet_id']})...")
                # We use FxTwitter to get the full JSON
                details = fetch_single_tweet_details(tw['tweet_id'], args.user)
                
                # Sleep to respect FxTwitter rate limits
                time.sleep(1) 
                
                if details:
                    enriched_tweets.append(details)
                else:
                    enriched_tweets.append(tw) # Fallback to basic text from Nitter
                    
            with open(out_file, "w", encoding="utf-8") as f:
                json.dump(enriched_tweets, f, ensure_ascii=False, indent=2)
            print(f"✅ Saved {len(enriched_tweets)} unique items for {d_str}")

            if args.markdown and to_obsidian:
                md_out_dir = Path(args.output) / "markdown" / d_str
                assets_dir = md_out_dir / "assets"
                md_out_dir.mkdir(parents=True, exist_ok=True)
                
                print(f"  - Converting to Markdown in {md_out_dir}...")
                for tw_data in enriched_tweets:
                    # to_obsidian expects a wrapper like {"tweet": ...} 
                    wrapped_data = {"tweet": tw_data, "username": tw_data.get("author", {}).get("screen_name") or args.user}
                    
                    try:
                        title, date_val, md_content = to_obsidian.json_to_markdown(wrapped_data, assets_dir)
                        safe_title = to_obsidian.sanitize_filename(title)
                        md_file = md_out_dir / f"{safe_title}.md"
                        md_file.write_text(md_content, encoding="utf-8")
                    except Exception as e:
                        print(f"    ⚠️ Failed to convert tweet {tw_data.get('tweet_id')} to markdown: {e}")
        else:
            # Create an empty marker file
            with open(out_file, "w", encoding="utf-8") as f:
                json.dump([], f)
            print(f"No results for {d_str}, saved marker.")
            
        curr = next_d
        time.sleep(3)

if __name__ == "__main__":
    main()
