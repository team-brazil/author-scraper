# Guia comentado do script OpenAlex (Economia)

Este documento explica **em detalhes, passo a passo**, o que cada parte do seu script faz. O objetivo do
script é **coletar autores relevantes na área de Economia** a partir da API pública do **OpenAlex**, aplicar
**critérios estritos** de filtragem por relevância de conceitos, e **salvar os resultados** em um arquivo CSV, com
possibilidade de **retomada** (checkpoint via cursor) e **parada graciosa** (Ctrl+C).

## Sumário

[1. Visão geral do que o script faz](#1-visão-geral-do-que-o-script-faz)

[2. Pré-requisitos e como executar](#2-pré-requisitos-e-como-executar)

[3. Cabeçalho e codificação](#3-cabeçalho-e-codificação)

[4. Imports — bibliotecas usadas](#4-imports--bibliotecas-usadas)

[5. Configurações principais](#5-configurações-principais)

[6. Pacing adaptativo e timeouts](#6-pacing-adaptativo-e-timeouts)

[7. Filtros "estritos porém inclusivos"](#7-filtros-estritos-porém-inclusivos)

[8. Sessão HTTP e cabeçalhos](#8-sessão-http-e-cabeçalhos)

[9. Esquema do CSV (colunas)](#9-esquema-do-csv-colunas)

[10. Helpers utilitários](#10-helpers-utilitários)
  - `_cid`
  - `parse_retry_after`
  - `_get`
    
[11. Pré-carregamento dos descendentes de Economia](#11-pré-carregamento-dos-descendentes-de-economia)

[12. Contagens de trabalhos (para borderline)](#12-contagens-de-trabalhos-para-borderline)

[13. Filtro principal do autor: lógica completa](#13-filtro-principal-do-autor-lógica-completa)

[14. Utilidades de CSV e cursor](#14-utilidades-de-csv-e-cursor)

[15. Parada graciosa (Ctrl+C)](#15-parada-graciosa-ctrlc)

[16. Loop principal de coleta (fetch_economics_authors)](#16-loop-principal-de-coleta--fetch_economics_authors-)

[17. Ponto de entrada `if __name__ == "__main__"`](#17-ponto-de-entrada)

[18. Boas práticas embutidas no desenho](#18-boas-práticas-embutidas-no-desenho)

[19. Ajustes e personalizações comuns](#19-ajustes-e-personalizações-comuns)

[20. Erros comuns e como lidar](#20-erros-comuns-e-como-lidar)

## 1. Visão geral do que o script faz

- Consulta a API do OpenAlex em `/authors` para listar autores ligados ao conceito Economia (ID `C162324750` ).
- Pré-carrega todos os subconceitos de Economia (subárvore) para avaliar relevância sem precisar de chamadas extras por autor.
- Aplica um filtro estrito baseado em score absoluto, posição em top-K, força relativa e (quando necessário) proporção de trabalhos do autor em Economia.
- Grava os autores aprovados no CSV com campos padronizados.
- Gerencia limites de taxa (429) com backoff e ajusta “sleep” automaticamente.
- Permite retomar o processo de onde parou via cursor salvo em arquivo.
- Suporta parada graciosa com Ctrl+C (SIGINT), finalizando a página corrente e salvando checkpoint.

## 2. Pré-requisitos e como executar

Requisitos

- Python 3.8+ (recomendado).
- Biblioteca requests instalada.

```
  pip install requests
```

Execução

Salve o script em um arquivo, por exemplo openalex_econ.py , e execute:

```
python openalex_econ.py
```

Os resultados serão gravados em `openalex_field_outputs/economics_researchers_strict.csv` e o cursor em `openalex_field_outputs/economics_cursor.txt`.

## 3. Cabeçalho e codificação

```python
  #!/usr/bin/env python
  # coding: utf-8
```

- Shebang: usa o Python presente no PATH do ambiente (portável entre sistemas). - Codificação: define UTF-8 para permitir acentos, emojis e caracteres especiais.

## 4. Imports — bibliotecas usadas

```python
  import os, re, csv, time, signal, requests
  from datetime import datetime, timezone
  from email.utils import parsedate_to_datetime
  from functools import lru_cache
```

- `os` : arquivos, diretórios, caminhos.
- `re` : expressões regulares (sanitização de strings).
- `csv` : escrita segura de CSV (com cabeçalho e escaping corretos).
- `time` : sleep e tempo simples.
- `signal` : captura sinais do sistema (Ctrl+C) para parar com segurança.
- `requests` : HTTP para consumir a API do OpenAlex.
- `datetime` , `timezone` : datas/horas conscientes de fuso.
- `parsedate_to_datetime` : interpreta `Retry-After` no formato de data HTTP. - `lru_cache` : memoização de funções de contagem (eficiência).

## 5. Configurações principais

```python
  ECONOMICS_ID = "C162324750"
  ECONOMICS_NAME = "Economics"
```

- Conceito **raiz** (Economia) e seu **nome** legível.

```python
  OUTPUT_DIR = "openalex_field_outputs"
  os.makedirs(OUTPUT_DIR, exist_ok=True)
```

- Pasta de saída; criada se não existir.

```python
  SAFE_FIELD = re.sub(r'[^a-z0-9_-]', '', ECONOMICS_NAME.strip().replace(' ',
  '_').lower())
```

- Gera um nome de arquivo seguro (minúsculas, `_` no lugar de espaço, e remoção de caracteres especiais).

```python
  OUT_PATH = os.path.join(OUTPUT_DIR, f"{SAFE_FIELD}_researchers_strict.csv")
  CURSOR_PATH = os.path.join(OUTPUT_DIR, f"{SAFE_FIELD}_cursor.txt")
```

- Caminho do arquivo CSV final e do cursor para retomada.

```python
  PER_PAGE_AUTHORS = 200
```

- Tamanho de página para `/authors` (mais alto = menos idas/voltas).

## 6. Pacing adaptativo e timeouts

```python
  SLEEP = 0.15
  MIN_SLEEP, MAX_SLEEP = 0.05, 1.25
  BACKOFF_MULT, COOLDOWN_MULT = 1.5, 0.9
```

- `SLEEP` : pausa padrão entre páginas.
- `MIN_SLEEP` / `MAX_SLEEP` : limites inferior/superior para o sono adaptativo.
- `BACKOFF_MULT` : aumenta o “sleep” em casos de erro/limite de taxa.
- `COOLDOWN_MULT` : reduz gradualmente o “sleep” quando as respostas estão estáveis.

```
  AUTHORS_TIMEOUT = 20
  CONCEPTS_TIMEOUT = 20
  WORKS_TIMEOUT = 25
```

- Tempo máximo (segundos) para aguardar cada tipo de requisição.

## 7. Filtros “estritos porém inclusivos”

```python
  MIN_ECON_SCORE = 20                  # mínimo absoluto (0–100)
  REQUIRE_ECON_TOP_K = 5               # Economia precisa estar no top-5 conceitos
  MIN_ECON_RELATIVE = 0.6              # score de Economia >= 60% do score do conceito
  top
  BORDERLINE_SCORE = 45                # abaixo disso, exige checar proporção de
  trabalhos
  MIN_ECON_SHARE = 0.40                # se borderline: ≥40% dos trabalhos devem ser de
  Economia
  SLEEP_BETWEEN_COUNTS = 0.1           # pausa entre consultas de contagem
```

- **Score mínimo** absoluto protege contra “ruído”.
- **Top-K** garante que Economia aparece entre os principais temas do autor.
- **Força relativa** impede casos em que o autor é majoritariamente de outra área.
- **Borderline**: quando o score de Economia não é alto, a **proporção de trabalhos** em Economia precisa confirmar a relevância.

```python
  SKIP_SHARE_IF_TOP_IS_ECON = True
```

- Se o **conceito principal** do autor já for de Economia, **pula** a checagem de proporção (economia de chamadas).

## 8. Sessão HTTP e cabeçalhos

```python
  SESSION = requests.Session()
  SESSION.headers.update({"Accept-Encoding": "gzip", "User-Agent": "econ-fast/
  2.0"})
```

- Reutiliza conexões (HTTP keep-alive).
- Pede compressão gzip (menos banda).
- Define um `User-Agent` identificável (boa prática com APIs públicas).

## 9. Esquema do CSV (colunas)

```python
  CSV_FIELDS = [
    "author_id","name","orcid",
    "institution_id","affiliation","country",
    "works_count","cited_by_count",
    "fields","field_group",
    "primary_concept_id","primary_concept_name","primary_concept_score",
    "best_in_field_score","best_in_field_id","best_in_field_name",
    "is_primary_in_field"
  ]
```

- Define a **ordem** e o **conjunto** de campos persistidos no CSV.

## 10. Helpers utilitários

```python
_cid(s: str) -> str
```

Extrai o ID do final de uma URL (ex.: `https://openalex.org/C123` → `C123` ).

```python
parse_retry_after(h, default_seconds=2)
```

- Interpreta o cabeçalho `Retry-After` da API.
- Se for um **inteiro**, usa como segundos.
- Se for uma **data HTTP**, calcula a diferença para “agora”.
- Se falhar, retorna `default_seconds` .
- Garante retorno **≥ 1 segundo** quando em formato data.

```python
_get(url, params=None, timeout=30)
```

Wrapper simples para `SESSION.get` com `params` e `timeout` apropriados.

## 11. Pré-carregamento dos descendentes de Economia

```python
  def load_econ_descendants():
      base = "https://api.openalex.org/concepts"
      cursor = "*"
      ids = {ECONOMICS_ID}
       sleep_s = 0.2
       print("🔎 Preloading Economics subtree (concept IDs)...")
       while True:
           r = _get(base, params={
               "filter": f"ancestors.id:{ECONOMICS_ID}",
               "per-page": 200,
               "cursor": cursor,
               "select": "id"
           }, timeout=CONCEPTS_TIMEOUT)
           ...
```

- Percorre todas as páginas de `/concepts` com filtro `ancestors.id:<ECON_ID>` para obter **toda a subárvore** de Economia.
- Solicita somente o campo `id` para resposta mínima.
- Trata 429 (limite de taxa) respeitando `Retry-After` e faz **backoff**.
- **Trata 5xx** com backoff exponencial suave.
- Em sucesso, aplica **cooldown** (reduz levemente o `sleep` ).
- Atualiza `cursor` e pausa entre páginas para polidez.
- Retorna um `set` com todos os IDs (inclui o `ECONOMICS_ID` ).

  Benefício: depois disso, verificar se um conceito do autor pertence a Economia é O(1) (consulta a um set ), sem chamadas extras por autor.

## 12. Contagens de trabalhos (para borderline)

```python
_count_works(filter_str: str) -> int
```

- Chama `/works` com `per-page=1` e `select=id` apenas para ler `meta.count` (número total de itens que satisfazem o filtro).
- Retorna 0 em caso de falha.

```python
  _author_total_works(author_id_url: str) -> int #memoizado
```

- Conta todos os trabalhos do autor via filtro `authorships.author.id:<AID>` .
- **Pausa curta** ( `SLEEP_BETWEEN_COUNTS` ) por cortesia.
- **Memoizado** por `lru_cache` para não repetir chamadas iguais.

```python
  _author_econ_works(author_id_url: str, econ_id: str) -> int #memoizado
```

- Conta trabalhos do autor **que são de Economia** via filtro `concepts.id:<ECON_ID>` .
- Idem pausa e memoização.

```python
  econ_share_ok(author_id_url, econ_id, min_share) -> bool
```

- Calcula proporção `econ / total` e compara com `min_share` (ex.: 0.40).
- Retorna `False` se `total <= 0` .

  Essas contagens **só são usadas** quando um autor está em **zona borderline** (score de Economia < `BORDERLINE_SCORE` ) — reduzindo o custo total de API.

## 13. Filtro principal do autor: lógica completa

```python
def author_passes_field_filter_strict(author: dict, econ_desc: set):
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

    top = concepts[0]

    # 1) Economia aparece no top-K?
    if REQUIRE_ECON_TOP_K and REQUIRE_ECON_TOP_K > 0:
        if not any(c["id"] in econ_desc for c in concepts[:REQUIRE_ECON_TOP_K]):
            return False, {}

    # 2) Melhor conceito de Economia com score mínimo
    best_econ = None
    best_econ_score = 0.0
    for c in concepts:
        if c["id"] in econ_desc and c["score"] >= float(MIN_ECON_SCORE):
            if c["score"] > best_econ_score:
                best_econ = c
                best_econ_score = c["score"]
    if best_econ is None:
        return False, {}

    # 3) Força relativa: Economia forte o bastante vs. conceito top?




      if MIN_ECON_RELATIVE is not None:
          if best_econ_score < MIN_ECON_RELATIVE * float(top["score"] or 0.0):
              return False, {}

      # 4) Se borderline, verificar participação de trabalhos em Economia
      if best_econ_score < BORDERLINE_SCORE:
          if SKIP_SHARE_IF_TOP_IS_ECON and (top["id"] in econ_desc):
              pass
          else:
             if not econ_share_ok(author.get("id"), ECONOMICS_ID,
 MIN_ECON_SHARE):
                  return False, {}

      details = {
          "primary_concept_id": top["id"],
          "primary_concept_name": top["display_name"],
          "primary_concept_score": top["score"],
          "best_in_field_score": best_econ_score,
          "best_in_field_id": best_econ.get("id"),
          "best_in_field_name": best_econ.get("display_name"),
          "is_primary_in_field": top["id"] in econ_desc
      }
      return True, details
```

##### Resumo da lógica:

1. **Top-K**: Economia (ou sub) precisa estar entre os K conceitos mais fortes do autor.
2. **Score mínimo** absoluto para o melhor conceito de Economia.
3. **Força relativa**: o melhor conceito de Economia deve ser ≥ 60% do score do conceito top do autor.
4. **Borderline**: se o score de Economia não atinge `BORDERLINE_SCORE` , exige **≥ 40%** dos trabalhos em Economia, a menos que o conceito top já seja de Economia.
5. Se aprovado, retorna `True` e um dicionário de **detalhes** para preencher o CSV.

## 14. Utilidades de CSV e cursor

```python
  def init_csv(path):
      write_header = not os.path.exists(path) or os.path.getsize(path) == 0
       f = open(path, "a", newline="", encoding="utf-8")
       w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
       if write_header:
           w.writeheader()
       return f, w
```

- Abre o CSV em **append** e escreve o **cabeçalho** se necessário.

```python
  def save_cursor(next_cursor: str | None):
      if next_cursor:
          with open(CURSOR_PATH, "w", encoding="utf-8") as fh:
              fh.write(next_cursor)
```

- Persiste o **cursor** da página seguinte (checkpoint).

```python
  def load_cursor():
      if os.path.exists(CURSOR_PATH):
          s = open(CURSOR_PATH, "r", encoding="utf-8").read().strip()
          if s:
              return s
      return "*"
```

- Lê o cursor salvo; se não houver, retorna `"*"` (início da paginação).

## 15. Parada graciosa (Ctrl+C)

```python
  _SHOULD_STOP = False

  def _handle_sigint(signum, frame):
      global _SHOULD_STOP
      _SHOULD_STOP = True
      print("\n🛑 Interrupt received — finishing current page and
  checkpointing...")

  signal.signal(signal.SIGINT, _handle_sigint)
```

- Ao receber **SIGINT** (Ctrl+C), define `_SHOULD_STOP=True` .
- O loop principal verifica a flag e **para no fim da página**, salvando o cursor e fechando o CSV.

## 16. Loop principal de coleta ( `fetch_economics_authors` )

```python
  def fetch_economics_authors():
      econ_desc = load_econ_descendants()   # prefetch
      base_url = "https://api.openalex.org/authors"
      cursor = load_cursor()
      scanned = kept_total = 0




    total_candidates = None
    sleep_s = SLEEP

    print(f"📥 Starting: {ECONOMICS_NAME} (min_score={MIN_ECON_SCORE},
top_k={REQUIRE_ECON_TOP_K}, rel≥{MIN_ECON_RELATIVE},
borderline<{BORDERLINE_SCORE}→share≥{MIN_ECON_SHARE})")
    if cursor != "*":
        print("↩️ Resuming from saved cursor")

    fh, writer = init_csv(OUT_PATH)
    try:
         while True:
             params = {
                 "filter": f"x_concepts.id:{ECONOMICS_ID}",
                 "per-page": PER_PAGE_AUTHORS,
                 "cursor": cursor,
                 "select":
"id,display_name,orcid,last_known_institutions,works_count,cited_by_count,x_concepts"
             }
             r = _get(base_url, params=params, timeout=AUTHORS_TIMEOUT)

            # tratamento de 429/5xx/outros e cooldown
            ...

            data = r.json()
            if total_candidates is None:
                total_candidates = data.get("meta", {}).get("count", 0)
                print(f"🔢 Total available = {total_candidates}")

            results = data.get("results", [])
            if not results:
                break

            kept_this_page = 0
            for a in results:
                ok, det = author_passes_field_filter_strict(a, econ_desc)
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
                               "fields": "; ".join([c.get("display_name", "") for c in
  (a.get("x_concepts") or [])]),
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
              print(f"📊 Scanned {scanned} | kept {kept_total} (+
  {kept_this_page}) | sleep {sleep_s:.2f}s")

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
```

##### Destaques:

- **Filtro inicial em**   `/authors`   já   restringe   a   candidatos   ligados   a   Economia ( `x_concepts.id:<ECON_ID>` ), reduzindo ruído.
- `select` **enxuto**: só traz os campos necessários.
- **Tratamento robusto** de 429/5xx e ajuste de `sleep` (backoff/cooldown).
- **Flush periódico** do arquivo para reduzir risco de perda de dados.
- **Checkpoint** via `cursor` a cada página.

## 17. Ponto de entrada

```python
if __name__ == "__main__":
    fetch_economics_authors()
```

- Executa a função principal **somente** quando o arquivo é rodado diretamente (não em import).

## 18. Boas práticas embutidas no desenho

- **Eficiência**: sessão HTTP, `select` mínimo, paginação por cursor, e memoização de contagens.
- **Respeito à API**: implementação de `Retry-After` , backoff, cooldown e pequenas pausas entre chamadas.
- **Robustez**: retomada por cursor, fechamento garantido de arquivo ( `finally` ), parada graciosa.
- **Precisão**: combinação de critérios (top-K, absoluto, relativo, proporção) reduz falsos positivos.

## 19. Ajustes e personalizações comuns

- **Trocar de campo** (ex.: Computação):
  - Atualize `ECONOMICS_ID` e `ECONOMICS_NAME` para o conceito desejado.
  - O restante da lógica (subárvore, filtros) funciona igual.
- **Ajustar rigor** dos filtros:
  - Aumente `MIN_ECON_SCORE` e/ou `MIN_ECON_RELATIVE` para maior seletividade.
  - Reduza `REQUIRE_ECON_TOP_K` se quiser aceitar autores com Economia fora do top-5 (menos estrito).
  - Ajuste `BORDERLINE_SCORE` e `MIN_ECON_SHARE` conforme a tolerância.
- **Desempenho e limites de taxa**:
  - Ajuste `SLEEP` , `MIN_SLEEP` / `MAX_SLEEP` , `BACKOFF_MULT` , `COOLDOWN_MULT` segundo sua experiência de uso.
- **Formato de saída**:
  - Você pode trocar o CSV por Parquet/JSON facilmente, se preferir (ex.: usando pandas ).

## 20. Erros comuns e como lidar

- **429 Too Many Requests**: o script já respeita `Retry-After` e aumenta `sleep` . Se persistir, considere aumentar `SLEEP` e reduzir `PER_PAGE_AUTHORS` .
- **5xx do servidor**: são transitórios; o script faz backoff e tenta novamente.
- **Conexões instáveis**: ajuste `*_TIMEOUT` (ex.: `WORKS_TIMEOUT=35` ).
- **CSV corrompido** (queda de energia): o script faz `flush` periódico e fecha no `finally` , reduzindo
  impacto. Se necessário, remova a última linha incompleta.
- **Interrupção do usuário**: use Ctrl+C; o script finalizará a página atual, salvará o cursor e fechará o CSV.

#### Encerramento

Com este guia, você tem a documentação completa do funcionamento do script, incluindo as motivações de cada parâmetro, os pontos de robustez e caminhos de customização. Se quiser, posso preparar uma variante para outro campo (ex.: Computer Science ) ou integrar com pandas para análises adicionais depois da coleta.

## Requerimentos para RA — próximos passos

1. **Otimização de desempenho** - Revisar pontos de latência (pré-carregamento, paginação, E/S em disco) e aplicar profiling (ex.: `cProfile` ) para identificar gargalos. - Reduzir chamadas redundantes, agrupar `select` mínimos e calibrar `SLEEP` / `BACKOFF` com métricas reais. - Considerar paralelismo controlado (fila com taxa máxima) e cache persistente das contagens mais caras.
2. **Ampliar número de fields** - Generalizar o pipeline para múltiplas áreas além de Economia: **Medicina, Sociologia, Música, Finanças, Estatística, Ciência Política, Engenharias**, etc. - Parametrizar `FIELD_ID` / `FIELD_NAME` via CLI ou arquivo de configuração; executar em lote com checkpoint por área.
3. **Revisão da métrica de score** - O score atual pode **excluir** especialistas legítimos ou **incluir** generalistas por ruído (um paper fora da área principal). - Propor um modelo híbrido: peso relativo + histórico temporal + diversidade de venues + percentil dentro do field, e *downweight* de outliers ocasionais. - Introduzir limites por **proporção temporal** (últimos N anos) e por **concentração de venues** (journals/conferências core do field).
4. **Lista de artigos publicados (journals)** - Incluir, quando disponível, uma **lista de journal articles** de cada autor. - Integrar com **ORCID** para obter produções validadas pelo próprio pesquisador; cruzar com OpenAlex para metadados (DOI, journal, ano, citações). - Exportar campo adicional no CSV (ou arquivo separado) com os artigos por autor (DOI, título, ano, venue, URL).
