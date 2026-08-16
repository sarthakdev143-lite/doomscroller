"""Final audit: verify source priority, platform mapping, and chopper disable."""
import httpx, json, sys
sys.path.insert(0, "D:\\Sarthak\\Coding\\My Codes\\Vibe-coding\\doomscroller")
sys.stdout.reconfigure(encoding="utf-8")
from tasks.pipeline import SOURCE_PRIORITY
from app.scrapers.yt_dlp_scraper import YtDlpScraper
from app.config import settings

print("=== SOURCE PRIORITY ===")
for src, prio in SOURCE_PRIORITY.items():
    print(f"  {src:<12} +{prio:.2f}")

scraper = YtDlpScraper()
print("\n=== PLATFORM DETECTION ===")
test_handles = [
    ("@charlidamelio", "instagram"),
    ("snapchat.com/add/test", "snapchat"),
    ("https://www.youtube.com/@test", "youtube"),
    ("https://www.reddit.com/r/videos", "reddit"),
]
for h, expected in test_handles:
    result = scraper._platform_for_handle(h)
    ok = "✓" if result == expected else f"✗ (got {result})"
    print(f"  {h:<35} -> {result:<12} {ok}")

print("\n=== CONFIG PLATFORMS ===")
print(f"  enabled: {settings.enabled_platforms_list}")
print(f"  hashtags: {settings.hashtags_list}")
print(f"  use_mock: {settings.use_mock}")

with httpx.Client(timeout=30) as client:
    r = client.get("http://127.0.0.1:8000/health")
    print(f"\n=== API HEALTH ===")
    print(f"  {r.text}")
    
    r = client.get("http://127.0.0.1:8000/videos?limit=50")
    vids = r.json()
    from collections import Counter
    src_counts = Counter(v["source"] for v in vids)
    print(f"\n=== DB SOURCES (latest {len(vids)}) ===")
    for src, cnt in src_counts.most_common():
        print(f"  {src:<12}: {cnt}")
    
    # Check pending priority
    r = client.get("http://127.0.0.1:8000/videos/pending?limit=10")
    pending = r.json()
    print(f"\n=== TOP PENDING (by velocity/engagement) ===")
    for v in pending[:5]:
        print(f"  {v['source']:<12} score={v['engagement_score']:.2f} {v['title'][:40]}")