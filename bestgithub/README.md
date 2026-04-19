# /bestgithub — AI & Agents Weekly Ranking

Týdenní žebříček nejrychleji rostoucích AI a AI Agent repozitářů na GitHubu
podle přírůstku hvězdiček za posledních 7 dní.

## Architektura

```
GitHub Search API ─┐
                   ├→ scripts/fetch_and_rank.py ──→ OpenAI (CZ popisky) ──→ Supabase
OSS Insight API   ─┘                                                           │
                                                                               ▼
                                               bestgithub/index.html ←─ čte přes REST
```

## Komponenty

- **`bestgithub/index.html`** — statická stránka, čte data přímo ze Supabase
  přes anonymní klíč (read-only RLS). "Viděno" flag se ukládá do localStorage.
- **`scripts/fetch_and_rank.py`** — fetch skript. Volá GitHub Search API, spočítá
  přírůstek hvězd vůči historii, překládá popisy do češtiny, ukládá do Supabase.
- **`api/cron-refresh.py`** — Vercel serverless endpoint. Cron trigger každé
  pondělí 06:00 UTC spustí `fetch_and_rank.py`.

## Databáze (Supabase)

- `bestgithub_repos` — aktuální stav top 100+100 repozitářů
- `bestgithub_stars_history` — týdenní snapshoty hvězd (pro výpočet weekly gainu)
- `bestgithub_seen` — rezerva pro server-side seen tracking (nyní nepoužito,
  používáme localStorage)

## Potřebné environment variables (Vercel)

| Proměnná | Popis |
|---|---|
| `GITHUB_TOKEN` | Personal Access Token, stačí `public_repo` scope |
| `SUPABASE_URL` | `https://pfanjvhwjvrkadhibbdj.supabase.co` |
| `SUPABASE_SERVICE_KEY` | Service role klíč ze Supabase → Settings → API |
| `OPENAI_API_KEY` | Pro generování českých popisků (model gpt-4o-mini) |
| `CRON_SECRET` | Náhodný string, chrání `/api/cron-refresh` endpoint |

## První spuštění

První týden nemá historii → přírůstek se spočítá jako proxy (1% absolutních
hvězd). Od druhého běhu (za týden) už bude přesný.

Pro rychlý bootstrap (okamžitý snapshot hned po nasazení):
```
curl -H "Authorization: Bearer $CRON_SECRET" \
  https://www.theaiholding.com/api/cron-refresh
```

## Frontend featury

- 2 taby: **AI** (100) / **AGENTS** (100)
- Filtr **JEN NOVINKY** — ukazuje jen repozitáře přidané tento týden
- Filtr **SKRÝT VIDĚNÉ** — kompletně skryje označené
- Kliknutím na **VIDĚNO** karta zešedne (ale zůstane), viz zadání
- Fulltext vyhledávání v názvu / popisu / topics
- Seen flags v localStorage (per-prohlížeč)
