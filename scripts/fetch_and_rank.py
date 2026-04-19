#!/usr/bin/env python3
"""
Fetch top AI & Agent repos z GitHubu, spočítá weekly star gain a uloží do Supabase.
Spouštěno jednou týdně přes Vercel Cron.
"""
import os
import sys
import time
import json
import re
from datetime import datetime, timezone, timedelta
import urllib.request
import urllib.parse
import urllib.error
import subprocess
import shutil

# === KONFIGURACE ===
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
USE_GH_CLI = os.environ.get("USE_GH_CLI") == "1" or (not GITHUB_TOKEN and shutil.which("gh"))
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")  # service role, pro upsert
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")  # pro české popisky

if not GITHUB_TOKEN and not USE_GH_CLI:
    print("ERROR: GITHUB_TOKEN chybí a gh CLI není k dispozici", file=sys.stderr)
    sys.exit(1)
if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
    print("ERROR: SUPABASE_URL nebo SUPABASE_SERVICE_KEY chybí", file=sys.stderr)
    sys.exit(1)

TOP_N_PER_CATEGORY = 100

# Dotazy pro jednotlivé kategorie. Vybíráme aktivní projekty za posledních 14 dní
# (pushed) — to jsou kandidáti které pravděpodobně získali hvězdy tento týden.
CATEGORIES = {
    "ai": [
        "topic:ai stars:>500",
        "topic:artificial-intelligence stars:>500",
        "topic:llm stars:>500",
        "topic:generative-ai stars:>500",
        "topic:machine-learning stars:>1000",
    ],
    "agents": [
        "topic:ai-agent stars:>100",
        "topic:agent stars:>500",
        "topic:agents stars:>300",
        "topic:ai-agents stars:>100",
        "topic:autonomous-agents stars:>100",
        "topic:multi-agent stars:>100",
    ],
}


def gh_get(url: str) -> dict:
    """GitHub API GET s retry a rate limit awareness.

    Používá buď přímý token (produkce) nebo gh CLI (lokální/testing).
    """
    if USE_GH_CLI:
        # gh api vyžaduje path, ne plnou URL
        path = url.replace("https://api.github.com", "")
        for attempt in range(3):
            try:
                result = subprocess.run(
                    ["gh", "api", path],
                    capture_output=True, text=True, timeout=60,
                )
                if result.returncode == 0:
                    return json.loads(result.stdout)
                if "rate limit" in result.stderr.lower() or "403" in result.stderr:
                    print(f"Rate limit (gh), čekám 30s...", file=sys.stderr)
                    time.sleep(30)
                    continue
                if attempt < 2:
                    time.sleep(3)
                    continue
                raise RuntimeError(f"gh api failed: {result.stderr}")
            except subprocess.TimeoutExpired:
                if attempt < 2:
                    continue
                raise
        raise RuntimeError(f"Failed: {url}")

    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "theaiholding-bestgithub",
        },
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code in (403, 429):
                reset = int(e.headers.get("X-RateLimit-Reset", "0"))
                wait = max(10, reset - int(time.time()) + 2) if reset else 20
                print(f"Rate limit, čekám {wait}s...", file=sys.stderr)
                time.sleep(wait)
                continue
            if e.code >= 500 and attempt < 2:
                time.sleep(5 * (attempt + 1))
                continue
            raise
        except Exception:
            if attempt < 2:
                time.sleep(3)
                continue
            raise
    raise RuntimeError(f"Failed: {url}")


def search_repos(query: str, per_page: int = 100, max_pages: int = 2) -> list:
    """Vyhledá repozitáře přes /search/repositories, seřazené podle hvězd."""
    results = []
    for page in range(1, max_pages + 1):
        params = urllib.parse.urlencode({
            "q": query,
            "sort": "stars",
            "order": "desc",
            "per_page": per_page,
            "page": page,
        })
        url = f"https://api.github.com/search/repositories?{params}"
        data = gh_get(url)
        items = data.get("items", [])
        results.extend(items)
        if len(items) < per_page:
            break
        time.sleep(1)  # šetrnost k rate limitu search API (30 req/min)
    return results


def collect_category(queries: list) -> dict:
    """Vrátí dict {full_name: repo_item} pro všechny dotazy v kategorii (dedup)."""
    pool = {}
    for q in queries:
        print(f"  hledám: {q}", file=sys.stderr)
        try:
            items = search_repos(q, per_page=100, max_pages=2)
        except Exception as e:
            print(f"  WARN: {q} -> {e}", file=sys.stderr)
            continue
        for it in items:
            fn = it.get("full_name")
            if not fn:
                continue
            # Preferuj záznam s vyšším star countem (duplikáty napříč dotazy)
            if fn not in pool or it.get("stargazers_count", 0) > pool[fn].get("stargazers_count", 0):
                pool[fn] = it
    return pool


def supa(method: str, path: str, payload=None, extra_headers=None) -> list | dict:
    """Supabase REST API wrapper."""
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    headers = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }
    if extra_headers:
        headers.update(extra_headers)
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read()
            if not body:
                return []
            return json.loads(body)
    except urllib.error.HTTPError as e:
        err_body = e.read().decode(errors="replace")
        raise RuntimeError(f"Supabase {method} {path} -> {e.code}: {err_body}")


def get_previous_snapshot(repo_ids: list) -> dict:
    """Vrátí nejstarší snapshot hvězd mezi 5–10 dny zpátky pro každé repo_id."""
    if not repo_ids:
        return {}
    now = datetime.now(timezone.utc)
    date_from = (now - timedelta(days=10)).isoformat()
    date_to = (now - timedelta(days=5)).isoformat()

    # Dotaz: pro každé repo vzít nejstarší záznam v tom okně
    ids_csv = ",".join(str(i) for i in repo_ids)
    path = (
        f"bestgithub_stars_history?repo_id=in.({ids_csv})"
        f"&snapshot_at=gte.{urllib.parse.quote(date_from)}"
        f"&snapshot_at=lte.{urllib.parse.quote(date_to)}"
        f"&select=repo_id,stars_total,snapshot_at"
        f"&order=snapshot_at.asc"
    )
    rows = supa("GET", path)
    result = {}
    for r in rows:
        rid = r["repo_id"]
        if rid not in result:
            result[rid] = r["stars_total"]
    return result


def get_existing_descriptions(repo_ids: list) -> dict:
    """Vrátí {repo_id: (description_en, description_cs)} pro existující záznamy."""
    if not repo_ids:
        return {}
    ids_csv = ",".join(str(i) for i in repo_ids)
    path = f"bestgithub_repos?id=in.({ids_csv})&select=id,description,description_cs,first_seen_at"
    rows = supa("GET", path)
    return {r["id"]: r for r in rows}


def translate_to_czech_batch(items_to_translate: list) -> dict:
    """
    Přeloží description -> češtinu pro list repos.
    items_to_translate: [{'id': ..., 'name': ..., 'description': ..., 'topics': [...]}]
    Vrací: {id: czech_description}
    """
    if not items_to_translate or not OPENAI_API_KEY:
        return {}

    # Dávkujeme po 20 (kontextové okno + cena)
    result = {}
    batch_size = 20
    for i in range(0, len(items_to_translate), batch_size):
        batch = items_to_translate[i:i + batch_size]
        prompt_items = []
        for it in batch:
            topics_str = ", ".join(it.get("topics", [])[:5])
            prompt_items.append({
                "id": str(it["id"]),
                "name": it["name"],
                "description": it.get("description") or "",
                "topics": topics_str,
            })

        sys_prompt = (
            "Jsi expert na AI a open-source. Dostaneš seznam GitHub repozitářů. "
            "Ke každému napiš stručný český popis (1–2 věty, max 180 znaků), "
            "který vysvětlí CO to je a K ČEMU to slouží. Piš věcně, bez marketingu, "
            "bez uvozovek. Pokud je popis v angličtině chabý, použij název a topics pro kontext. "
            "Vrať POUZE validní JSON objekt {\"id\": \"popis\"} bez markdown bloků."
        )
        user_prompt = json.dumps(prompt_items, ensure_ascii=False)

        body = json.dumps({
            "model": "gpt-4o-mini",
            "messages": [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.3,
            "response_format": {"type": "json_object"},
        }).encode()

        req = urllib.request.Request(
            "https://api.openai.com/v1/chat/completions",
            data=body,
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read())
            content = data["choices"][0]["message"]["content"]
            parsed = json.loads(content)
            for k, v in parsed.items():
                try:
                    result[int(k)] = str(v).strip()
                except Exception:
                    continue
        except Exception as e:
            print(f"  WARN: překlad batch {i} selhal: {e}", file=sys.stderr)

        time.sleep(0.5)

    return result


def main():
    now = datetime.now(timezone.utc)
    snapshot_at = now.isoformat()
    print(f"=== Fetch started at {snapshot_at} ===", file=sys.stderr)

    all_rows = []
    all_history = []

    for category, queries in CATEGORIES.items():
        print(f"\n[kategorie: {category}]", file=sys.stderr)
        pool = collect_category(queries)
        print(f"  celkem unikátních: {len(pool)}", file=sys.stderr)

        items = list(pool.values())
        repo_ids = [it["id"] for it in items]

        # Získej předchozí snapshot pro výpočet přírůstku
        prev = get_previous_snapshot(repo_ids)
        print(f"  s historií (7d zpět): {len(prev)}", file=sys.stderr)

        # Získej existující záznamy (kvůli first_seen_at a popiskům)
        existing = get_existing_descriptions(repo_ids)

        # Spočítej přírůstek, seřaď a vezmi top N
        scored = []
        for it in items:
            rid = it["id"]
            cur_stars = it.get("stargazers_count", 0)
            prev_stars = prev.get(rid)
            if prev_stars is not None:
                gained = max(0, cur_stars - prev_stars)
            else:
                # Fallback pro repozitáře bez historie:
                # Odhad = 1% absolutních hvězd (konzervativní proxy)
                # Při druhém běhu už bude přesné.
                gained = max(1, cur_stars // 100)
            scored.append((gained, cur_stars, it))

        scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
        top = scored[:TOP_N_PER_CATEGORY]
        print(f"  top {len(top)} po seřazení", file=sys.stderr)

        # Připrav items k překladu (jen nové nebo změněné popisky)
        to_translate = []
        for gained, cur_stars, it in top:
            rid = it["id"]
            existing_rec = existing.get(rid)
            en_desc = it.get("description") or ""
            if not existing_rec or existing_rec.get("description") != en_desc or not existing_rec.get("description_cs"):
                if en_desc or it.get("name"):
                    to_translate.append({
                        "id": rid,
                        "name": it.get("name", ""),
                        "description": en_desc,
                        "topics": it.get("topics", []),
                    })

        print(f"  k překladu: {len(to_translate)}", file=sys.stderr)
        cz_translations = translate_to_czech_batch(to_translate) if to_translate else {}

        # Postav row pro upsert
        for gained, cur_stars, it in top:
            rid = it["id"]
            existing_rec = existing.get(rid)
            first_seen = existing_rec["first_seen_at"] if existing_rec else snapshot_at
            is_new = not existing_rec or (
                datetime.fromisoformat(existing_rec["first_seen_at"].replace("Z", "+00:00"))
                > now - timedelta(days=8)
            )
            description_cs = cz_translations.get(rid) or (
                existing_rec.get("description_cs") if existing_rec else None
            )
            row = {
                "id": rid,
                "full_name": it["full_name"],
                "name": it.get("name", ""),
                "owner": it["owner"]["login"] if it.get("owner") else it["full_name"].split("/")[0],
                "html_url": it["html_url"],
                "description": it.get("description"),
                "description_cs": description_cs,
                "homepage": it.get("homepage"),
                "language": it.get("language"),
                "topics": it.get("topics", []),
                "stars_total": cur_stars,
                "stars_gained_week": gained,
                "category": category,
                "first_seen_at": first_seen,
                "last_updated_at": snapshot_at,
                "snapshot_at": snapshot_at,
                "is_new": is_new,
            }
            all_rows.append(row)
            all_history.append({
                "repo_id": rid,
                "snapshot_at": snapshot_at,
                "stars_total": cur_stars,
            })

    # Upsert do bestgithub_repos
    print(f"\nupsert {len(all_rows)} řádků...", file=sys.stderr)
    for i in range(0, len(all_rows), 50):
        chunk = all_rows[i:i + 50]
        supa("POST", "bestgithub_repos", chunk,
             extra_headers={"Prefer": "resolution=merge-duplicates,return=minimal"})
    print("  hotovo.", file=sys.stderr)

    # Insert historie (append only)
    print(f"insert {len(all_history)} history záznamů...", file=sys.stderr)
    for i in range(0, len(all_history), 100):
        chunk = all_history[i:i + 100]
        try:
            supa("POST", "bestgithub_stars_history", chunk,
                 extra_headers={"Prefer": "resolution=ignore-duplicates,return=minimal"})
        except Exception as e:
            print(f"  WARN history chunk: {e}", file=sys.stderr)

    print(f"\n=== Done ===", file=sys.stderr)
    print(json.dumps({"ok": True, "rows": len(all_rows), "snapshot_at": snapshot_at}))


if __name__ == "__main__":
    main()
