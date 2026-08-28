import feedparser
import requests
from html.parser import HTMLParser
from PIL import Image, ImageDraw
from io import BytesIO
import json
from pathlib import Path
from datetime import datetime, timezone
import os

RSS_FEEDS = {
    "BBC Sport": "https://feeds.bbci.co.uk/sport/football/rss.xml",
}

DATA_FILE = Path("data/posted.json")
LOGO_URL = "https://cdn.phototourl.com/free/2026-08-28-24699823-9ef6-4c4b-845b-5763cef36e3c.png"
MAX_POSTS_PER_RUN = 5

CLOUDINARY_CLOUD_NAME = "hli3avbk"
CLOUDINARY_UPLOAD_PRESET = "football_news24"



class OGImageParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.image = ""

    def handle_starttag(self, tag, attrs):
        if tag != "meta":
            return
        attrs = dict(attrs)
        if attrs.get("property") == "og:image":
            self.image = attrs.get("content", "").strip()


def get_article_image(url):
    try:
        response = requests.get(
            url,
            timeout=15,
            headers={"User-Agent": "Mozilla/5.0"}
        )
        response.raise_for_status()

        parser = OGImageParser()
        parser.feed(response.text)
        return parser.image
    except requests.RequestException:
        return ""



def brand_article_image(image_url):
    if not image_url:
        return ""

    try:
        image_response = requests.get(
            image_url,
            timeout=20,
            headers={"User-Agent": "Mozilla/5.0"}
        )
        image_response.raise_for_status()

        logo_response = requests.get(
            LOGO_URL,
            timeout=20,
            headers={"User-Agent": "Mozilla/5.0"}
        )
        logo_response.raise_for_status()

        image = Image.open(BytesIO(image_response.content)).convert("RGB")
        logo = Image.open(BytesIO(logo_response.content)).convert("RGBA")

        target_ratio = 16 / 9
        width, height = image.size

        if width / height > target_ratio:
            new_width = int(height * target_ratio)
            left = (width - new_width) // 2
            image = image.crop((left, 0, left + new_width, height))
        else:
            new_height = int(width / target_ratio)
            top = (height - new_height) // 2
            image = image.crop((0, top, width, top + new_height))

        image = image.resize((1200, 675), Image.Resampling.LANCZOS)

        logo.thumbnail((260, 120), Image.Resampling.LANCZOS)

        base = image.convert("RGBA")
        base.alpha_composite(logo, (24, 24))

        output = BytesIO()
        base.convert("RGB").save(
            output,
            format="JPEG",
            quality=90,
            optimize=True
        )

        upload_url = (
            f"https://api.cloudinary.com/v1_1/"
            f"{CLOUDINARY_CLOUD_NAME}/image/upload"
        )

        upload = requests.post(
            upload_url,
            data={"upload_preset": CLOUDINARY_UPLOAD_PRESET},
            files={
                "file": (
                    "football_news24.jpg",
                    output.getvalue(),
                    "image/jpeg"
                )
            },
            timeout=30,
        )
        upload.raise_for_status()

        return upload.json()["secure_url"]

    except (requests.RequestException, OSError, KeyError) as exc:
        print(f"IMAGE ERROR: {exc}")
        return image_url


def generate_caption(title, url):
    api_key = os.environ["GROQ_API_KEY"]

    prompt = f"""
Write an engaging football news post for X.

Headline: {title}
Article URL: {url}

Rules:
- Keep it concise.
- Do not invent facts.
- Make it natural and exciting.
- Include 2-3 relevant football hashtags.
- Put the article URL on the final line.
"""

    response = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        json={
            "model": "openai/gpt-oss-20b",
            "messages": [
                {"role": "user", "content": prompt}
            ],
        },
        timeout=30,
    )

    response.raise_for_status()
    data = response.json()

    generated = data["choices"][0]["message"]["content"].strip()

    # Keep the complete X post within 280 characters, including the URL.
    url_line = url.strip()
    prefix = generated.split(url_line)[0].strip() if url_line in generated else generated
    prefix = " ".join(prefix.split())
    max_prefix = 280 - len(url_line) - 1

    if max_prefix < 1:
        return url_line[:280]

    if len(prefix) > max_prefix:
        prefix = prefix[:max_prefix].rsplit(" ", 1)[0].rstrip()

    return f"{prefix}\n{url_line}"


def post_to_buffer(text, image_url):
    api_key = os.environ["BUFFER_API_KEY"]
    channel_id = os.environ["BUFFER_CHANNEL_ID"]

    response = requests.post(
        "https://api.buffer.com",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "query": """
            mutation CreatePost($input: CreatePostInput!) {
                createPost(input: $input) {
                    ... on PostActionSuccess {
                        post {
                            id
                        }
                    }
                    ... on MutationError {
                        message
                    }
                }
            }
            """,
            "variables": {
                "input": {
                    "channelId": channel_id,
                    "text": text,
                    "schedulingType": "automatic",
                    "mode": "addToQueue",
                    "assets": [
                        {
                            "image": {
                                "url": image_url
                            }
                        }
                    ]
                }
            }
        },
        timeout=30,
    )

    response.raise_for_status()
    return response.json()


def load_posted():
    if not DATA_FILE.exists():
        return []
    try:
        return json.loads(DATA_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return []


def save_posted(items):
    DATA_FILE.write_text(json.dumps(items, indent=2, ensure_ascii=False))


def is_valid_article(entry):
    url = entry.get("link", "").strip()
    title = entry.get("title", "").strip()

    if not url or not title:
        return False

    # Skip videos, podcasts, quizzes and games.
    excluded = (
        "/videos/",
        "/sounds/",
        "quiz",
        "predictor",
        "game",
        "iplayer",
    )

    return not any(word in url.lower() or word in title.lower() for word in excluded)


def fetch_news():
    posted = load_posted()
    posted_urls = {item["url"] for item in posted if "url" in item}

    new_items = []

    for source, feed_url in RSS_FEEDS.items():
        feed = feedparser.parse(feed_url)

        for entry in feed.entries:
            if not is_valid_article(entry):
                continue

            url = entry.get("link", "").strip()

            if url in posted_urls:
                continue

            item = {
                "source": source,
                "title": entry.get("title", "").strip(),
                "url": url,
                "published": entry.get("published", ""),
                "image_url": get_article_image(url),
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            }

            new_items.append(item)
            posted_urls.add(url)

            if len(new_items) >= MAX_POSTS_PER_RUN:
                return new_items

    return new_items


if __name__ == "__main__":
    items = fetch_news()

    print(f"Found {len(items)} eligible new football stories.")

    for item in items:
        print(f"\n{item['title']}")
        print(item["url"])
        print("IMAGE:", item["image_url"])

        caption = generate_caption(item["title"], item["url"])
        print("CAPTION:", caption)

        branded_image = brand_article_image(item["image_url"])
        result = post_to_buffer(caption, branded_image)
        print("BUFFER:", result)

        posted = load_posted()
        posted.append(item)
        save_posted(posted)
