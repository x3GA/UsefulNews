#!/usr/bin/env python3
"""
Сводка по RSS: читает ленты из OPML, отбирает стоящее, вытаскивает
ТЕКСТ САМОЙ СТАТЬИ (не тизер из ленты), пересказывает по-русски
и шлёт по одной новости в Telegram.

Запускать по cron раз в час. Каждый запуск = одна новость.
Каждая третья — из позитивных лент.

Установка:
    pip install feedparser requests trafilatura

Переменные окружения:
    TELEGRAM_TOKEN, TELEGRAM_CHAT, MODEL_API_KEY
"""

import os
import re
import json
import time
import html
import hashlib
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import feedparser
import requests
import trafilatura

# ── настройки ─────────────────────────────────────────────────────────

OPML_PATH = os.environ.get("OPML_PATH", "Subscriptions.opml")
STATE_PATH = os.environ.get("STATE_PATH", "newsbot_state.json")

REFRESH_HOURS = 6          # как часто обходить ленты
QUEUE_SIZE = 20            # сколько держать в очереди
MIN_SCORE = 7              # ниже этой оценки не берём вообще
MIN_ARTICLE_CHARS = 900    # короче этого — тизер, а не статья
POSITIVE_EVERY = 3         # каждая N-я новость — позитивная

POSITIVE_FEEDS = {"Positive News", "Reasons to be Cheerful"}

PROVIDER = "openrouter"            # "openrouter" или "anthropic"
MODEL = "anthropic/claude-haiku-5.5"

TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT = os.environ["TELEGRAM_CHAT"]
API_KEY = os.environ["MODEL_API_KEY"]

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 newsbot"

# ── промпты ───────────────────────────────────────────────────────────

RANK_PROMPT = """Ты отбираешь новости для человека, которому интересны
новации, прорывы, открытия и достижения — то, что реально двигает
цивилизацию. Его НЕ интересует: политика, война, скандалы, мнения,
колонки, корпоративные новости, подборки и обзоры. Ставь 0 всему, 
что является дайджестом или подборкой ссылок
(заголовки вида "The Download", "This week in", "Roundup"),
и спонсорским материалам. Ставь 0-2 всему, 
что является эссе, колонкой, призывом или рассуждением: тексты вида 
"нам нужно", "что если бы", "почему мы должны", "технология уже существует,
осталось решиться". Тебе нужно только то, что УЖЕ ПРОИЗОШЛО: сделали, 
измерили, запустили, открыли.

Оцени каждую новость от 0 до 10:
10 — настоящий прорыв или открытие
7-9 — заметное достижение, новая технология, решённая задача
4-6 — любопытно, но не принципиально
0-3 — политика, скандал, мнение, обзор, реклама

Ответь ТОЛЬКО строками вида
номер:оценка
по одной на строку, без пояснений и без markdown. Пример:
0:8
1:3
2:10"""

SUMMARY_PROMPT = """Перескажи эту статью по-русски для человека, который
хочет понять СУТЬ, а не прочитать анонс.

Жёсткие требования:
- первая строка: заголовок по-русски, по смыслу статьи
- затем 6-8 предложений ОДНИМ абзацем, без переносов строк внутри
- в них должна быть КОНКРЕТИКА: что именно произошло или что сделали,
  кто, где, как это работает, какой результат. Числа если есть —
  обязательно, но их отсутствие не делает текст пустым
- никаких "в статье рассказывается", "авторы рассуждают", "что если"
- не пересказывай вопрос, которым статья открывается — отвечай на него
- не выдумывай фактов, которых нет в тексте

Отвечай ПУСТО только если это дайджест ссылок, анонс мероприятия или
рекламный материал. Репортаж, история или разбор без цифр — не пусто.

Не пиши никаких рассуждений, разборов и черновиков. В ответе ровно
две строки: ЗАГОЛОВОК и ТЕКСТ."""

# ── состояние ─────────────────────────────────────────────────────────

def load_state():
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"seen": [], "queue": [], "sent_count": 0, "refreshed": 0}


def save_state(state):
    state["seen"] = state["seen"][-500:]
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


# ── ленты ─────────────────────────────────────────────────────────────

def read_opml(path):
    tree = ET.parse(path)
    feeds = []
    for node in tree.iter("outline"):
        url = node.get("xmlUrl")
        if url:
            feeds.append((node.get("title") or node.get("text") or url, url))
    return feeds


def strip_html(text):
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def item_id(entry):
    raw = entry.get("id") or entry.get("link") or entry.get("title", "")
    return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:16]


def collect(feeds, seen):
    items = []
    for name, url in feeds:
        try:
            parsed = feedparser.parse(url)
        except Exception as e:
            print(f"[warn] {name}: {e}")
            continue
        for entry in parsed.entries[:25]:
            iid = item_id(entry)
            if iid in seen:
                continue
            items.append({
                "id": iid,
                "feed": name,
                "title": strip_html(entry.get("title")),
                "link": entry.get("link", ""),
                "blurb": strip_html(entry.get("summary") or entry.get("description"))[:600],
                "positive": name in POSITIVE_FEEDS,
            })
    return items


def fetch_article(url):
    """Тянет страницу и вынимает основной текст. Это и есть начинка."""
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=30)
        r.raise_for_status()
        text = trafilatura.extract(
            r.text, include_comments=False, include_tables=False, favor_precision=True
        )
        return (text or "").strip()
    except Exception as e:
        print(f"[warn] не достал статью {url}: {e}")
        return ""


# ── модель ────────────────────────────────────────────────────────────

def ask(system, user, max_tokens=1200):
    if PROVIDER == "openrouter":
        r = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {API_KEY}", "content-type": "application/json"},
            json={
                "model": MODEL,
                "max_tokens": max_tokens,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            },
            timeout=120,
        )
        r.raise_for_status()
        data = r.json()
        if "choices" not in data:
            raise RuntimeError(f"API вернул: {str(data)[:300]}")
        content = data["choices"][0]["message"].get("content")
        if not content:
            raise RuntimeError(f"пустой ответ модели: {str(data)[:300]}")
        return content.strip()

    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": MODEL,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        },
        timeout=120,
    )
    r.raise_for_status()
    blocks = r.json().get("content", [])
    return "".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()


def rank(items):
    """Оценка по заголовку и тизеру. Формат построчный — его держат даже слабые модели."""
    for start in range(0, len(items), 25):
        chunk = items[start:start + 25]
        listing = "\n\n".join(
            f'{n}. [{it["feed"]}] {it["title"]}\n{it["blurb"][:300]}'
            for n, it in enumerate(chunk)
        )
        try:
            out = ask(RANK_PROMPT, listing, max_tokens=400)
            for n, s in re.findall(r"(\d+)\s*:\s*(\d+(?:\.\d+)?)", out):
                idx = int(n)
                if 0 <= idx < len(chunk):
                    chunk[idx]["score"] = float(s)
        except Exception as e:
            print(f"[warn] отбор не сработал: {e}")
        for it in chunk:
            it.setdefault("score", 0.0)   # не разобрали — считаем мусором
    return items


def summarize(article_text, title):
    out = ask(SUMMARY_PROMPT, f"{title}\n\n{article_text[:12000]}", max_tokens=1600)
    if out.strip().upper().startswith("ПУСТО"):
        return None, None
    ru_title = title
    m = re.search(r"ЗАГОЛОВОК:\s*(.+)", out)
    if m:
        ru_title = m.group(1).strip()
    m = re.search(r"ТЕКСТ:\s*(.+)", out, re.S)
    body = m.group(1).strip() if m else ""
    if body:
        # рассуждения модели идут после пустой строки — режем там
        body = re.split(r"\n\s*\n", body)[0]
        body = re.split(r"\n\s*(?:Let me|Now I|Check|Analysis|Note:|Проверк)", body)[0]
        # переносы внутри абзаца склеиваем, а не теряем
        body = re.sub(r"\s+", " ", body).strip()
    return (ru_title, body) if body else (None, None)


# ── отправка ──────────────────────────────────────────────────────────

def send(item, title, body):
    text = (
        f"<b>{html.escape(title)}</b>\n\n"
        f"{html.escape(body)}\n\n"
        f'<a href="{html.escape(item["link"])}">{html.escape(item["feed"])}</a>'
    )
    r = requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        json={"chat_id": CHAT, "text": text[:4000], "parse_mode": "HTML"},
        timeout=30,
    )
    r.raise_for_status()


def pick(queue, sent_count):
    want_positive = (sent_count + 1) % POSITIVE_EVERY == 0
    for i, it in enumerate(queue):
        if it["positive"] == want_positive:
            return queue.pop(i)
    return queue.pop(0) if queue else None


# ── главное ───────────────────────────────────────────────────────────

def main():
    state = load_state()
    seen = set(state["seen"])
    now = time.time()

    if now - state.get("refreshed", 0) > REFRESH_HOURS * 3600 or not state["queue"]:
        fresh = collect(read_opml(OPML_PATH), seen)
        print(f"[{datetime.now(timezone.utc):%H:%M}] новых записей: {len(fresh)}")
        if fresh:
            fresh = [it for it in rank(fresh) if it["score"] >= MIN_SCORE]
            fresh.sort(key=lambda it: -it["score"])
            have = {it["id"] for it in state["queue"]}
            state["queue"] += [it for it in fresh if it["id"] not in have]
            state["queue"] = state["queue"][:QUEUE_SIZE]
            print(f"прошло отбор: {len(fresh)}, в очереди: {len(state['queue'])}")
        state["refreshed"] = now

    # берём из очереди, пока не найдём настоящую статью
    for _ in range(5):
        item = pick(state["queue"], state.get("sent_count", 0))
        if not item:
            print("очередь пуста")
            break

        article = fetch_article(item["link"])
        if len(article) < MIN_ARTICLE_CHARS:
            print(f"тизер, не статья — пропускаю: {item['title']}")
            state["seen"].append(item["id"])
            continue

        try:
            title, body = summarize(article, item["title"])
        except Exception as e:
            print(f"[error] пересказ: {e}")
            state["queue"].insert(0, item)
            break

        if not body:
            print(f"сути нет — пропускаю: {item['title']}")
            state["seen"].append(item["id"])
            continue

        try:
            send(item, title, body)
            state["sent_count"] = state.get("sent_count", 0) + 1
            state["seen"].append(item["id"])
            print(f"отправлено: {title}")
        except Exception as e:
            print(f"[error] отправка: {e}")
            state["queue"].insert(0, item)
        break

    save_state(state)


if __name__ == "__main__":
    main()
