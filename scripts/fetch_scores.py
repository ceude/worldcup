#!/usr/bin/env python3
"""
worldcup26.ir -> Supabase skor senkronizasyonu
================================================
Canlı ve biten maç skorlarını worldcup26.ir'den çekip Supabase'deki
`matches` tablosuna yazar. GitHub Actions ile 5 dakikada bir çalışır.

Ortam değişkenleri (GitHub Secrets):
  SUPABASE_URL          -> https://XXXX.supabase.co
  SUPABASE_SERVICE_KEY  -> service_role anahtarı (RLS'i aşar, ASLA HTML'e koyma)

Kullanım:
  python fetch_scores.py            # normal çalışma
  python fetch_scores.py --debug    # API'den gelen ilk kaydı ham bas (alan adlarını görmek için)
  python fetch_scores.py --dry-run  # Supabase'e yazmadan ne yapacağını göster

NOT: worldcup26.ir'nin JSON alan adları kesin bilinmediği için script
esnek yazıldı (birkaç olası alan adını dener). İlk çalıştırmada --debug ile
gelen yapıyı gör; gerekirse aşağıdaki GETTER fonksiyonlarındaki anahtarları düzelt.
"""

import os, sys, json, unicodedata, urllib.request, urllib.error

API_URL = os.environ.get("WC_API_URL", "https://worldcup26.ir/get/games")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SERVICE_KEY  = os.environ.get("SUPABASE_SERVICE_KEY", "")

DEBUG   = "--debug" in sys.argv
DRY_RUN = "--dry-run" in sys.argv

# --- API takım adlarını bizim İngilizce anahtarlarımıza eşleyen tablo ---
# Sol: API'nin verebileceği yazımlar (küçük harf, sadeleştirilmiş). Sağ: bizim anahtar.
TEAM_ALIASES = {
    "turkey": "Türkiye", "turkiye": "Türkiye",
    "ivory coast": "Côte d'Ivoire", "cote divoire": "Côte d'Ivoire", "cote d'ivoire": "Côte d'Ivoire",
    "czech republic": "Czechia", "czechia": "Czechia",
    "cape verde": "Cabo Verde", "cabo verde": "Cabo Verde",
    "dr congo": "DR Congo", "congo dr": "DR Congo", "democratic republic of congo": "DR Congo",
    "south korea": "South Korea", "korea republic": "South Korea", "republic of korea": "South Korea",
    "usa": "United States", "united states": "United States", "united states of america": "United States",
    "iran": "Iran", "ir iran": "Iran",
    "curacao": "Curaçao",
    "bosnia and herzegovina": "Bosnia and Herzegovina", "bosnia": "Bosnia and Herzegovina",
    "south africa": "South Africa", "new zealand": "New Zealand", "saudi arabia": "Saudi Arabia",
}

# Bizim tüm takımlarımız (Supabase'deki team_home/team_away değerleri bunlardan biri)
OUR_TEAMS = [
 "Mexico","South Africa","South Korea","Czechia","Canada","Bosnia and Herzegovina","Qatar","Switzerland",
 "Brazil","Morocco","Haiti","Scotland","United States","Paraguay","Australia","Türkiye","Germany","Curaçao",
 "Côte d'Ivoire","Ecuador","Netherlands","Japan","Sweden","Tunisia","Belgium","Egypt","Iran","New Zealand",
 "Spain","Cabo Verde","Saudi Arabia","Uruguay","France","Senegal","Iraq","Norway","Argentina","Algeria",
 "Austria","Jordan","Portugal","DR Congo","Uzbekistan","Colombia","England","Croatia","Ghana","Panama"
]

def norm(s):
    if not s: return ""
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii","ignore").decode("ascii")
    return s.lower().strip().replace(".", "").replace("-", " ")

# Normalize edilmiş -> bizim anahtar
NORM_TO_KEY = {norm(t): t for t in OUR_TEAMS}
for alias, key in TEAM_ALIASES.items():
    NORM_TO_KEY[norm(alias)] = key

def to_our_team(name):
    return NORM_TO_KEY.get(norm(name))

# --- API kaydından alan çekiciler (esnek; alan adı farklıysa burayı düzelt) ---
def g(d, *keys):
    for k in keys:
        if isinstance(d, dict) and k in d and d[k] not in (None, ""):
            return d[k]
    return None

def get_home(game):  return g(game, "home_team","home","team_home","teamA","homeTeam","home_name")
def get_away(game):  return g(game, "away_team","away","team_away","teamB","awayTeam","away_name")
def get_hs(game):    return g(game, "home_score","homeScore","score_home","home_goals","homeGoals","scoreA")
def get_as(game):    return g(game, "away_score","awayScore","score_away","away_goals","awayGoals","scoreB")
def get_status(game):return g(game, "status","state","match_status","matchStatus","time_status")

FINISHED_WORDS = {"finished","ft","ended","full time","fulltime","completed","final","afterextra","penalties"}
LIVE_WORDS     = {"live","inplay","in play","1h","2h","ht","first half","second half","halftime","et","playing"}

def status_kind(status):
    s = norm(status)
    if any(w in s for w in FINISHED_WORDS): return "finished"
    if any(w in s for w in LIVE_WORDS):     return "live"
    return "other"

def http_get_json(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))

def supabase(method, path, body=None):
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    headers = {
        "apikey": SERVICE_KEY,
        "Authorization": f"Bearer {SERVICE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=30) as r:
        txt = r.read().decode("utf-8")
        return json.loads(txt) if txt else []

def main():
    if not SUPABASE_URL or not SERVICE_KEY:
        print("HATA: SUPABASE_URL / SUPABASE_SERVICE_KEY ortam değişkenleri yok."); sys.exit(1)

    raw = http_get_json(API_URL)
    games = raw if isinstance(raw, list) else (raw.get("data") or raw.get("games") or raw.get("matches") or [])
    if DEBUG:
        print("Toplam oyun:", len(games))
        if games: print("İLK KAYIT:", json.dumps(games[0], ensure_ascii=False, indent=2))
        return

    # Bizim maçlar (takımı belli olanlar)
    rows = supabase("GET", "matches?select=id,team_home,team_away,kickoff,home_score,away_score,finished")
    index = {}
    for m in rows:
        if m.get("team_home") and m.get("team_away"):
            index[(norm(m["team_home"]), norm(m["team_away"]))] = m

    updated = 0
    for game in games:
        h, a = to_our_team(get_home(game)), to_our_team(get_away(game))
        if not h or not a:
            continue
        m = index.get((norm(h), norm(a)))
        if not m:
            continue
        hs, as_ = get_hs(game), get_as(game)
        if hs is None or as_ is None:
            continue
        try:
            hs, as_ = int(hs), int(as_)
        except (TypeError, ValueError):
            continue
        kind = status_kind(get_status(game))
        finished = (kind == "finished")

        # Değişmemişse atla
        if m.get("home_score") == hs and m.get("away_score") == as_ and bool(m.get("finished")) == finished:
            continue
        patch = {"home_score": hs, "away_score": as_, "finished": finished}
        tag = "BİTTİ" if finished else ("CANLI" if kind == "live" else "?")
        print(f"[{tag}] {h} {hs}-{as_} {a}  (match id {m['id']})")
        if not DRY_RUN:
            supabase("PATCH", f"matches?id=eq.{m['id']}", patch)
        updated += 1

    print(f"Güncellenen maç: {updated}")

if __name__ == "__main__":
    try:
        main()
    except urllib.error.HTTPError as e:
        print("HTTP hata:", e.code, e.read().decode("utf-8", "ignore")); sys.exit(1)
    except Exception as e:
        print("Hata:", repr(e)); sys.exit(1)
