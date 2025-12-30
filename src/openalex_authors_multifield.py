#!/usr/bin/env python
# coding: utf-8

import os
import re
import csv
import time
import signal
import requests
import json
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from functools import lru_cache

# === Configurações principais ===
with open('config/campos_config.json', 'r', encoding='utf-8') as f:
    CONFIG = json.load(f)

# Variáveis globais que serão atualizadas com base no campo atual
ECONOMICS_ID = ""
ECONOMICS_NAME = ""
SAFE_FIELD = ""
OUTPUT_DIR = "openalex_field_outputs"
os.makedirs(OUTPUT_DIR, exist_ok=True)
OUT_PATH = ""
CURSOR_PATH = ""

# Configurações padrão - serão atualizadas com base na configuração do campo
PER_PAGE_AUTHORS = 200

# Pacing adaptativo e timeouts
SLEEP = 0.15
MIN_SLEEP, MAX_SLEEP = 0.05, 1.25
BACKOFF_MULT, COOLDOWN_MULT = 1.5, 0.9

AUTHORS_TIMEOUT = 20
CONCEPTS_TIMEOUT = 20
WORKS_TIMEOUT = 25

# Filtros "estritos porém inclusivos"
MIN_ECON_SCORE = 20                  # mínimo absoluto (0–100)
REQUIRE_ECON_TOP_K = 5               # Economia precisa estar no top-5 conceitos
MIN_ECON_RELATIVE = 0.6              # score de Economia >= 60% do score do conceito top
BORDERLINE_SCORE = 45                # abaixo disso, exige checar proporção de trabalhos
MIN_ECON_SHARE = 0.40                # se borderline: ≥40% dos trabalhos devem ser de Economia
SLEEP_BETWEEN_COUNTS = 0.1           # pausa entre consultas de contagem

SKIP_SHARE_IF_TOP_IS_ECON = True     # Se o conceito principal já for de Economia, pula a checagem de proporção

# Sessão HTTP e cabeçalhos
SESSION = requests.Session()
SESSION.headers.update({"Accept-Encoding": "gzip", "User-Agent": "econ-fast/2.0"})

# Esquema do CSV (colunas)
CSV_FIELDS = [
    "author_id", "name", "orcid",
    "institution_id", "affiliation", "country",
    "works_count", "cited_by_count",
    "fields", "field_group",
    "primary_concept_id", "primary_concept_name", "primary_concept_score",
    "best_in_field_score", "best_in_field_id", "best_in_field_name",
    "is_primary_in_field"
]

# Variáveis globais para controle de parada graciosa
_SHOULD_STOP = False

def _handle_sigint(signum, frame):
    global _SHOULD_STOP
    _SHOULD_STOP = True
    print("\n🛑 Interrupt received — finishing current page and checkpointing...")

signal.signal(signal.SIGINT, _handle_sigint)

def update_config_for_field(field_config):
    """Atualiza as variáveis globais com base na configuração do campo"""
    global ECONOMICS_ID, ECONOMICS_NAME, SAFE_FIELD, OUT_PATH, CURSOR_PATH
    global MIN_ECON_SCORE, REQUIRE_ECON_TOP_K, MIN_ECON_RELATIVE, BORDERLINE_SCORE, MIN_ECON_SHARE
    
    ECONOMICS_ID = field_config["id"]
    ECONOMICS_NAME = field_config["nome"]
    SAFE_FIELD = field_config["nome_seguro"]
    
    # Atualiza parâmetros de filtro com base na configuração do campo
    if "parametros_filtro" in field_config:
        filtros = field_config["parametros_filtro"]
        MIN_ECON_SCORE = filtros.get("min_score", MIN_ECON_SCORE)
        REQUIRE_ECON_TOP_K = filtros.get("top_k", REQUIRE_ECON_TOP_K)
        MIN_ECON_RELATIVE = filtros.get("min_relative", MIN_ECON_RELATIVE)
        BORDERLINE_SCORE = filtros.get("borderline_score", BORDERLINE_SCORE)
        MIN_ECON_SHARE = filtros.get("min_share", MIN_ECON_SHARE)
    
    OUT_PATH = field_config["arquivo_saida"]
    CURSOR_PATH = field_config["arquivo_cursor"]

def _cid(s: str) -> str:
    """Extrai o ID do final de uma URL (ex.: https://openalex.org/C123 → C123)"""
    if s:
        return s.split('/')[-1]
    return ""

def parse_retry_after(h, default_seconds=2):
    """Interpreta o cabeçalho Retry-After da API"""
    if not h:
        return default_seconds
    
    try:
        # Tenta interpretar como número inteiro
        return max(1, int(h))
    except ValueError:
        try:
            # Tenta interpretar como data HTTP
            dt = parsedate_to_datetime(h)
            now = datetime.now(timezone.utc)
            diff = dt - now
            return max(1, int(diff.total_seconds()))
        except:
            return default_seconds

def _get(url, params=None, timeout=30):
    """Wrapper simples para SESSION.get com params e timeout apropriados"""
    try:
        response = SESSION.get(url, params=params, timeout=timeout)
        return response
    except requests.exceptions.RequestException as e:
        print(f"⚠️ Request failed: {e}")
        return None

def load_econ_descendants():
    """Pré-carrega todos os subconceitos do campo (subárvore) para avaliar relevância sem precisar de chamadas extras por autor"""
    base = "https://api.openalex.org/concepts"
    cursor = "*"
    ids = {ECONOMICS_ID}
    sleep_s = 0.2
    print(f"🔎 Preloading {ECONOMICS_NAME} subtree (concept IDs)...")

    while True:
        params = {
            "filter": f"ancestors.id:{ECONOMICS_ID}",
            "per-page": 200,
            "cursor": cursor,
            "select": "id"
        }
        
        r = _get(base, params=params, timeout=CONCEPTS_TIMEOUT)
        if not r:
            print("⚠️ Failed to load concept descendants, continuing with empty set")
            return set()
        
        if r.status_code == 429:
            retry_after = parse_retry_after(r.headers.get('Retry-After'))
            print(f"⏳ Rate limited. Sleeping for {retry_after}s")
            time.sleep(retry_after)
            continue
        
        if r.status_code >= 500:
            print(f"⚠️ Server error {r.status_code}, backing off")
            time.sleep(2)
            continue
        
        try:
            data = r.json()
        except:
            print("⚠️ Failed to parse response as JSON")
            break
        
        new_ids = {item['id'].split('/')[-1] for item in data.get('results', [])}
        ids.update(new_ids)
        
        cursor = data.get('meta', {}).get('next_cursor')
        if not cursor:
            break
        
        time.sleep(sleep_s)
        
        # Aplica cooldown
        sleep_s = max(MIN_SLEEP, sleep_s * COOLDOWN_MULT)
    
    print(f"✅ Loaded {len(ids)} concept IDs for {ECONOMICS_NAME}")
    return ids

def init_csv(path):
    """Abre o CSV em append e escreve o cabeçalho se necessário"""
    write_header = not os.path.exists(path) or os.path.getsize(path) == 0
    f = open(path, "a", newline="", encoding="utf-8")
    w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
    if write_header:
        w.writeheader()
    return f, w

def save_cursor(next_cursor: str | None):
    """Persiste o cursor da página seguinte (checkpoint)"""
    if next_cursor:
        with open(CURSOR_PATH, "w", encoding="utf-8") as fh:
            fh.write(next_cursor)

def load_cursor():
    """Lê o cursor salvo; se não houver, retorna '*' (início da paginação)"""
    if os.path.exists(CURSOR_PATH):
        s = open(CURSOR_PATH, "r", encoding="utf-8").read().strip()
        if s:
            return s
    return "*"

@lru_cache(maxsize=1000)
def _author_total_works(author_id_url: str) -> int:
    """Conta todos os trabalhos do autor via filtro authorships.author.id:<AID>"""
    author_id = _cid(author_id_url)
    if not author_id:
        return 0
    
    filter_str = f"authorships.author.id:{author_id}"
    count = _count_works(filter_str)
    
    # Pausa curta por cortesia
    time.sleep(SLEEP_BETWEEN_COUNTS)
    return count

@lru_cache(maxsize=1000)
def _author_econ_works(author_id_url: str, econ_id: str) -> int:
    """Conta trabalhos do autor que são do campo via filtro concepts.id:<FIELD_ID>"""
    author_id = _cid(author_id_url)
    if not author_id or not econ_id:
        return 0
    
    filter_str = f"authorships.author.id:{author_id},concepts.id:{econ_id}"
    count = _count_works(filter_str)
    
    # Pausa curta por cortesia
    time.sleep(SLEEP_BETWEEN_COUNTS)
    return count

def _count_works(filter_str: str) -> int:
    """Chama /works com per-page=1 e select=id apenas para ler meta.count"""
    base_url = "https://api.openalex.org/works"
    params = {
        "filter": filter_str,
        "per-page": 1,
        "select": "id"
    }
    
    r = _get(base_url, params=params, timeout=WORKS_TIMEOUT)
    if not r or r.status_code != 200:
        return 0
    
    try:
        data = r.json()
        return data.get("meta", {}).get("count", 0)
    except:
        return 0

def econ_share_ok(author_id_url, econ_id, min_share) -> bool:
    """Calcula proporção econ / total e compara com min_share (ex.: 0.40)"""
    total = _author_total_works(author_id_url)
    if total <= 0:
        return False
    
    econ_count = _author_econ_works(author_id_url, econ_id)
    share = econ_count / total
    return share >= min_share

def author_passes_field_filter_strict(author: dict, field_desc: set):
    """Filtro principal do autor: lógica completa"""
    xcs = author.get("x_concepts") or []
    if not xcs:
        return False, {}

    # Normaliza e ordena conceitos por score decrescente
    concepts = [{
        "id": _cid(c.get("id")),
        "display_name": c.get("display_name"),
        "score": float(c.get("score") or 0.0)
    } for c in xcs]
    concepts.sort(key=lambda z: z["score"], reverse=True)

    top = concepts[0] if concepts else {"id": "", "display_name": "", "score": 0.0}

    # 1) Campo aparece no top-K?
    if REQUIRE_ECON_TOP_K and REQUIRE_ECON_TOP_K > 0:
        if not any(c["id"] in field_desc for c in concepts[:REQUIRE_ECON_TOP_K]):
            return False, {}

    # 2) Melhor conceito do campo com score mínimo
    best_field = None
    best_field_score = 0.0
    for c in concepts:
        if c["id"] in field_desc and c["score"] >= float(MIN_ECON_SCORE):
            if c["score"] > best_field_score:
                best_field = c
                best_field_score = c["score"]
    if best_field is None:
        return False, {}

    # 3) Força relativa: campo forte o bastante vs. conceito top?
    if MIN_ECON_RELATIVE is not None:
        if best_field_score < MIN_ECON_RELATIVE * float(top["score"] or 0.0):
            return False, {}

    # 4) Se borderline, verificar participação de trabalhos no campo
    if best_field_score < BORDERLINE_SCORE:
        if SKIP_SHARE_IF_TOP_IS_ECON and (top["id"] in field_desc):
            pass
        else:
            if not econ_share_ok(author.get("id"), ECONOMICS_ID, MIN_ECON_SHARE):
                return False, {}

    details = {
        "primary_concept_id": top["id"],
        "primary_concept_name": top["display_name"],
        "primary_concept_score": top["score"],
        "best_in_field_score": best_field_score,
        "best_in_field_id": best_field.get("id"),
        "best_in_field_name": best_field.get("display_name"),
        "is_primary_in_field": top["id"] in field_desc
    }
    return True, details

def fetch_authors_for_field():
    """Loop principal de coleta para o campo atual"""
    global SLEEP
    
    field_desc = load_econ_descendants()   # prefetch
    base_url = "https://api.openalex.org/authors"
    cursor = load_cursor()
    scanned = kept_total = 0

    total_candidates = None
    sleep_s = SLEEP

    print(f"📥 Starting: {ECONOMICS_NAME} (min_score={MIN_ECON_SCORE}, top_k={REQUIRE_ECON_TOP_K}, rel≥{MIN_ECON_RELATIVE}, borderline<{BORDERLINE_SCORE}→share≥{MIN_ECON_SHARE})")
    if cursor != "*":
        print("↩️ Resuming from saved cursor")

    fh, writer = init_csv(OUT_PATH)
    try:
        while True:
            params = {
                "filter": f"x_concepts.id:{ECONOMICS_ID}",
                "per-page": PER_PAGE_AUTHORS,
                "cursor": cursor,
                "select": "id,display_name,orcid,last_known_institutions,works_count,cited_by_count,x_concepts"
            }
            r = _get(base_url, params=params, timeout=AUTHORS_TIMEOUT)
            
            if not r:
                print("⚠️ Request failed, backing off...")
                sleep_s = min(MAX_SLEEP, sleep_s * BACKOFF_MULT)
                time.sleep(sleep_s)
                continue
            
            if r.status_code == 429:
                retry_after = parse_retry_after(r.headers.get('Retry-After'))
                print(f"⏳ Rate limited. Sleeping for {retry_after}s")
                time.sleep(retry_after)
                sleep_s = min(MAX_SLEEP, sleep_s * BACKOFF_MULT)
                continue
            
            if r.status_code >= 500:
                print(f"⚠️ Server error {r.status_code}, backing off")
                sleep_s = min(MAX_SLEEP, sleep_s * BACKOFF_MULT)
                time.sleep(sleep_s)
                continue
            
            # Sucesso - aplica cooldown
            sleep_s = max(MIN_SLEEP, sleep_s * COOLDOWN_MULT)

            data = r.json()
            if total_candidates is None:
                total_candidates = data.get("meta", {}).get("count", 0)
                print(f"🔢 Total available = {total_candidates}")

            results = data.get("results", [])
            if not results:
                break

            kept_this_page = 0
            for a in results:
                ok, det = author_passes_field_filter_strict(a, field_desc)
                if not ok:
                    continue

                lki = a.get("last_known_institutions") or []
                inst = lki[0] if (isinstance(lki, list) and lki) else {}

                writer.writerow({
                    "author_id": a.get("id"),
                    "name": a.get("display_name"),
                    "orcid": a.get("orcid"),
                    "institution_id": inst.get("id", "N/A"),
                    "affiliation": inst.get("display_name", "N/A"),
                    "country": inst.get("country_code", "N/A"),
                    "works_count": a.get("works_count", 0),
                    "cited_by_count": a.get("cited_by_count", 0),
                    "fields": "; ".join([c.get("display_name", "") for c in (a.get("x_concepts") or [])]),
                    "field_group": ECONOMICS_NAME,
                    "primary_concept_id": det.get("primary_concept_id"),
                    "primary_concept_name": det.get("primary_concept_name"),
                    "primary_concept_score": det.get("primary_concept_score"),
                    "best_in_field_score": det.get("best_in_field_score"),
                    "best_in_field_id": det.get("best_in_field_id"),
                    "best_in_field_name": det.get("best_in_field_name"),
                    "is_primary_in_field": det.get("is_primary_in_field"),
                })
                kept_this_page += 1
                kept_total += 1

            scanned += len(results)
            print(f"📊 Scanned {scanned} | kept {kept_total} (+{kept_this_page}) | sleep {sleep_s:.2f}s")

            next_cursor = data.get("meta", {}).get("next_cursor")
            save_cursor(next_cursor)
            cursor = next_cursor
            if not cursor:
                break

            if scanned % (PER_PAGE_AUTHORS * 5) == 0:
                fh.flush()

            if _SHOULD_STOP:
                print("🛟 Graceful stop — checkpoint saved.")
                break

            time.sleep(sleep_s)
    finally:
        fh.close()
        print(f"💾 CSV closed: {OUT_PATH}")

def process_single_field(field_config):
    """Processa um único campo com base na configuração fornecida"""
    print(f"\n{'='*60}")
    print(f"PROCESSANDO CAMPO: {field_config['nome']}")
    print(f"{'='*60}")
    
    # Atualiza as configurações com base no campo atual
    update_config_for_field(field_config)
    
    # Executa a coleta para este campo
    fetch_authors_for_field()

def main():
    """Função principal que processa todos os campos definidos na configuração"""
    print("🚀 Iniciando coleta de autores para múltiplos campos...")
    
    for i, field_config in enumerate(CONFIG["campos"]):
        print(f"\n[{i+1}/{len(CONFIG['campos'])}] Iniciando processamento para {field_config['nome']}")
        
        try:
            process_single_field(field_config)
            print(f"✅ Concluído: {field_config['nome']}")
            
            # Pequena pausa entre campos para respeitar limites de taxa
            if i < len(CONFIG["campos"]) - 1:
                print("⏳ Pausa entre campos...")
                time.sleep(5)
                
        except KeyboardInterrupt:
            print("🛑 Interrompido pelo usuário")
            break
        except Exception as e:
            print(f"❌ Erro ao processar {field_config['nome']}: {e}")
            continue
    
    print("\n🎉 Processamento concluído para todos os campos!")

if __name__ == "__main__":
    main()