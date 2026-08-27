import feedparser
import requests
from html.parser import HTMLParser
import json
from pathlib import Path
from datetime import datetime, timezone
import os

RSS_FEEDS = {
    "BBC Sport": "https://feeds.bbci.co.uk/sport/football/rss.xml",
}

DATA_FILE = Path("data/posted.json")
MAX_POSTS_PER_RUN = 5



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

    return data["choices"][0]["message"]["content"].strip()


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
                    ... on Post {
                        id
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
                    "assets": [
                        {
                            "url": image_url,
                            "type": "IMAGE"
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

        result = post_to_buffer(caption, item["image_url"])
        print("BUFFER:", result)

        posted = load_posted()
        posted.append(item)
        save_posted(posted)
