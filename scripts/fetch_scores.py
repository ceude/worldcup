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

import os, sys, json, time, socket, unicodedata, urllib.request, urllib.error, urllib.parse
from datetime import datetime, timezone

# --- DNS sağlamlaştırma ---------------------------------------------------
# Bazı GitHub runner'ları belirli alan adlarını (ör. .ir) çözemiyor
# ("Name or service not known"). Çözüm: sistem DNS başarısız olursa
# Cloudflare DNS-over-HTTPS (1.1.1.1, doğrudan IP) ile A kaydını bulup
# o IP'yi kullan. SNI/Host hostname olarak kaldığı için sertifika doğrulaması bozulmaz.
_HOST_IP = {}  # hostname -> sabit IP
_real_getaddrinfo = socket.getaddrinfo
def _patched_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    if host in _HOST_IP:
        return [(socket.AF_INET, socket.SOCK_STREAM, proto, "", (_HOST_IP[host], port))]
    return _real_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)  # IPv4'e sabitle
socket.getaddrinfo = _patched_getaddrinfo

def doh_resolve(host):
    # Cloudflare (literal IP, DNS gerektirmez) -> Google yedek
    for url in (f"https://1.1.1.1/dns-query?name={host}&type=A",
                f"https://dns.google/resolve?name={host}&type=A"):
        try:
            req = urllib.request.Request(url, headers={"accept": "application/dns-json"})
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.loads(r.read().decode())
            for ans in data.get("Answer", []):
                if ans.get("type") == 1 and ans.get("data"):
                    return ans["data"]
        except Exception:
            continue
    return None

def ensure_resolvable(host):
    try:
        _real_getaddrinfo(host, 443, socket.AF_INET)
        return True
    except OSError:
        ip = doh_resolve(host)
        if ip:
            _HOST_IP[host] = ip
            print(f"  {host} sistem DNS ile çözülemedi; DoH ile bulundu -> {ip}")
            return True
        print(f"  {host} DoH ile de çözülemedi.")
        return False
# -------------------------------------------------------------------------

API_URL = os.environ.get("WC_API_URL", "https://worldcup26.ir/get/games")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SERVICE_KEY  = os.environ.get("SUPABASE_SERVICE_KEY", "")

DEBUG   = "--debug" in sys.argv
DRY_RUN = "--dry-run" in sys.argv
DIAG    = "--diag" in sys.argv

# Bu tarihten ÖNCE başlayan maçlara dokunma (elle girilmiş sonuçlar korunsun).
# 26 Haziran 2026 00:00 Almanya saati = 25 Haziran 22:00 UTC.
PROTECT_BEFORE = datetime(2026, 6, 25, 22, 0, 0, tzinfo=timezone.utc)

def parse_dt(s):
    if not s: return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None

# --- API takım adlarını bizim İngilizce anahtarlarımıza eşleyen tablo ---
# Sol: API'nin verebileceği yazımlar (küçük harf, sadeleştirilmiş). Sağ: bizim anahtar.
TEAM_ALIASES = {
    "turkey": "Türkiye", "turkiye": "Türkiye",
    "ivory coast": "Côte d'Ivoire", "cote divoire": "Côte d'Ivoire", "cote d'ivoire": "Côte d'Ivoire",
    "czech republic": "Czechia", "czechia": "Czechia",
    "cape verde": "Cabo Verde", "cabo verde": "Cabo Verde",
    "dr congo": "DR Congo", "congo dr": "DR Congo", "democratic republic of congo": "DR Congo",
    "democratic republic of the congo": "DR Congo", "congo democratic republic": "DR Congo",
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

def get_home(game):  return g(game, "home_team_name_en","home_team","home","team_home","homeTeam")
def get_away(game):  return g(game, "away_team_name_en","away_team","away","team_away","awayTeam")
def get_hs(game):    return g(game, "home_score","homeScore","score_home","home_goals")
def get_as(game):    return g(game, "away_score","awayScore","score_away","away_goals")
def get_status(game):return g(game, "time_elapsed","status","state","match_status")
def get_finished_flag(game): return g(game, "finished")

FINISHED_WORDS = {"finished","ft","ended","full time","fulltime","completed","final","aet","afterextra","penalties","pen"}
LIVE_WORDS     = {"live","inplay","in play","1h","2h","ht","first half","second half","halftime","half time","et","playing"}

def is_finished(game):
    # Önce belirgin finished bayrağı (worldcup26.ir: "TRUE"/"FALSE")
    fl = norm(get_finished_flag(game))
    if fl in ("true","1","yes"):  return True
    if fl in ("false","0","no"):  return False
    # Yoksa durum metnine bak
    return any(w in norm(get_status(game)) for w in FINISHED_WORDS)

def is_live(game):
    if is_finished(game): return False
    s = norm(get_status(game))
    # Sayısal dakika (ör. "67") da canlı sayılır
    if s and s.replace("'","").strip().isdigit(): return True
    return any(w in s for w in LIVE_WORDS)

EXTRA_WORDS = {"aet","pen","penalt","et","extra","afterextra"}
def in_extra_time(game):
    """Maç uzatmaya/penaltıya geçmiş mi? (eleme maçlarında 90 dk skorunu korumak için)"""
    s = norm(get_status(game))
    if any(w in s for w in EXTRA_WORDS): return True
    # "91", "105", "120" gibi dakika -> normal süre bitmiş
    digits = "".join(ch for ch in s.split("+")[0] if ch.isdigit())
    if digits and int(digits) > 90: return True
    return False

def http_get_json(url, headers=None, tries=4, label="API"):
    last=None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=headers or {})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError) as e:
            last=e; print(f"  {label} ağ hatası ({i+1}/{tries}): {e}; bekleniyor..."); time.sleep(3*(i+1))
    raise last

def supabase(method, path, body=None, tries=4):
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    headers = {
        "apikey": SERVICE_KEY,
        "Authorization": f"Bearer {SERVICE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }
    data = json.dumps(body).encode() if body is not None else None
    last=None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            with urllib.request.urlopen(req, timeout=30) as r:
                txt = r.read().decode("utf-8")
                return json.loads(txt) if txt else []
        except urllib.error.HTTPError:
            raise  # gerçek HTTP hatası (401/403 vb.) tekrar denenmez
        except (urllib.error.URLError, TimeoutError) as e:
            last=e; print(f"  Supabase ağ hatası ({i+1}/{tries}): {e}; bekleniyor..."); time.sleep(3*(i+1))
    raise last

def main():
    if not SUPABASE_URL or not SERVICE_KEY:
        print("HATA: SUPABASE_URL / SUPABASE_SERVICE_KEY ortam değişkenleri yok."); sys.exit(1)

    # API alan adını çözebildiğimizden emin ol (gerekirse DoH ile)
    api_host = urllib.parse.urlparse(API_URL).hostname
    if api_host:
        ensure_resolvable(api_host)

    raw = http_get_json(API_URL, label="worldcup26.ir")
    games = raw if isinstance(raw, list) else (raw.get("data") or raw.get("games") or raw.get("matches") or [])
    if DEBUG:
        print("Toplam oyun:", len(games))
        if games: print("İLK KAYIT:", json.dumps(games[0], ensure_ascii=False, indent=2))
        return

    # Bizim maçlar (takımı belli olanlar)
    rows = supabase("GET", "matches?select=id,team_home,team_away,kickoff,home_score,away_score,finished,stage")
    index = {}
    for m in rows:
        if m.get("team_home") and m.get("team_away"):
            index[(norm(m["team_home"]), norm(m["team_away"]))] = m

    updated = 0
    for game in games:
        rawh, rawa = get_home(game), get_away(game)
        h, a = to_our_team(rawh), to_our_team(rawa)
        fin0, live0 = is_finished(game), is_live(game)

        # Maçı bul: önce aynı sıra, olmazsa ters sıra (ev/deplasman kayıtta ters olabilir)
        m = None; swapped = False
        if h and a:
            m = index.get((norm(h), norm(a)))
            if not m:
                m = index.get((norm(a), norm(h)))
                if m: swapped = True

        # Teşhis
        if DIAG and (live0 or fin0):
            if not h or not a: reason = f"İSİM EŞLEŞMEDİ (api: '{rawh}' / '{rawa}')"
            elif not m:        reason = f"SUPABASE'DE MAÇ YOK ({h} vs {a})"
            else:
                k0 = parse_dt(m.get("kickoff"))
                has_score = m.get("home_score") is not None and m.get("away_score") is not None
                if k0 and k0 < PROTECT_BEFORE and has_score:
                    reason = f"KORUMA (skor dolu, korunuyor) ({m.get('kickoff')})"
                elif k0 and k0 < PROTECT_BEFORE:
                    reason = f"26 ÖNCESİ ama skor boş -> DOLDURULACAK"
                else:
                    reason = "OK" + (" [sıra ters, skor çevrildi]" if swapped else "")
            print(f"[DIAG {'CANLI' if live0 else 'BİTTİ'}] {rawh} {get_hs(game)}-{get_as(game)} {rawa} | time_elapsed={get_status(game)!r} -> {reason}")

        if not m:
            continue

        # Koruma (akıllı): 26 Haziran öncesi maçın skoru DOLUYSA üzerine yazma (elle girileni koru).
        # Skoru boş/silinmişse doldurmaya izin ver.
        k = parse_dt(m.get("kickoff"))
        if k and k < PROTECT_BEFORE and m.get("home_score") is not None and m.get("away_score") is not None:
            continue

        fin  = is_finished(game)
        live = is_live(game)
        # Sadece canlı ya da bitmiş maçlara dokun; başlamamışı atla (yoksa 0-0 "canlı" sanılır)
        if not fin and not live:
            continue

        hs, as_ = get_hs(game), get_as(game)
        if hs is None or as_ is None:
            continue
        try:
            hs, as_ = int(hs), int(as_)
        except (TypeError, ValueError):
            continue
        if swapped:
            hs, as_ = as_, hs   # ev/deplasman ters eşleşti -> skoru da çevir

        # --- Eleme turu (uzatma kuralı) ---
        # Tahminler 90 dk skoruna göre. API bitmiş skoru uzatmayı içerebilir.
        # Bu yüzden eleme maçlarını otomatik KESİNLEŞTİRME; uzatmaya geçince DOKUNMA.
        is_knockout = (m.get("stage") or "group") != "group"
        if is_knockout:
            if in_extra_time(game) or fin:
                # 90 dk + sonrası: 90 dk skoru korunsun, kesin sonucu admin girsin
                if DIAG:
                    print(f"  (eleme) {h}-{a}: uzatma/bitiş -> otomatik atlandı, 90 dk skorunu admin girecek")
                continue
            # Eleme + normal süre içinde canlı: geçici skoru yaz ama 'finished' yapma
            fin = False

        # Değişmemişse atla
        if m.get("home_score") == hs and m.get("away_score") == as_ and bool(m.get("finished")) == fin:
            continue
        patch = {"home_score": hs, "away_score": as_, "finished": fin}
        tag = "BİTTİ" if fin else "CANLI"
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
    except urllib.error.URLError as e:
        # Geçici ağ/DNS hatası: bu turu atla, 5 dk sonra tekrar denenecek (kırmızı verme)
        print("Geçici ağ hatası, bu tur atlandı:", e); sys.exit(0)
    except Exception as e:
        print("Hata:", repr(e)); sys.exit(1)
