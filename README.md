# TCC-LLM

Trabalho de Conclusão de Curso em Engenharia de Computação — UTFPR.
Previsão do impacto de grandes eventos no transporte público urbano com LLMs.

A primeira etapa coleta notícias oficiais por artista e **ano de publicação**.
O coletor não usa LLM, credenciais ou serviços pagos. Requer Python 3.10 ou superior.

## Instalação e execução

No terminal, a partir da raiz do repositório:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python web-scraping.py --artista "Shakira" --ano 2026
```

O nome do arquivo solicitado foi mantido, por isso a execução usa o script diretamente.
Uma coleta pequena para desenvolvimento:

```powershell
python web-scraping.py --artista "Shakira" --ano 2026 --max-paginas 5
```

Opções adicionais: `--saida resultados/minha-coleta.json`, `--max-paginas-busca 10`,
`--intervalo 2`, `--timeout 30`, `--tentativas 3` e `--user-agent` para acrescentar
identificação/contato. `--help` apresenta as opções. O intervalo mínimo é 1 segundo;
o limite padrão da descoberta é 100 páginas de busca. O limite de desenvolvimento
conta páginas candidatas processadas, incluindo descartes e falhas, e não só notícias aceitas.

## Descoberta e regras de coleta

A busca interna observada é `https://prefeitura.rio/?s=ARTISTA`. O código extrai
URLs dos títulos dos cartões de resultados e segue os links de paginação que a
busca publica. O ano é conferido **na notícia**, depois da descoberta: datas de
eventos ou de atualização não substituem a publicação. A busca não se limita à CET-Rio.

O domínio permitido inclui `prefeitura.rio` **e seus subdomínios**, como
`comlurb.prefeitura.rio`, pois a busca da raiz também retorna esses órgãos.
Links e redirecionamentos externos são bloqueados antes do acesso. Não há busca
independente em cada órgão: a cobertura depende do índice da busca central.
URLs repetidas são eliminadas, inclusive parâmetros conhecidos de rastreamento
e URLs finais idênticas após redirecionamento. Textos republicados em URLs distintas
permanecem separados para preservar a fonte; não representam necessariamente informação independente.

Uma página candidata precisa apresentar identificação de artigo em Open Graph,
JSON-LD ou a classe WordPress `single-post`, além de título e corpo extraíveis.
Essa identificação inclui posts editoriais, não prova a relevância para um evento.
O corpo usa os contêineres observados na raiz e nos subdomínios, com suporte ao
template WordPress padrão. Menus, rodapés, compartilhamento, categorias, tags,
imagens de destaque e notícias relacionadas são excluídos. Layout não reconhecido
fica pendente de revisão. O nome do artista é comparado no título/corpo sem
distinção de caixa e acentos, com espaços normalizados e limites de palavras.
O texto salvo mantém a grafia original e normaliza apenas os espaços e as quebras.

Prioridade da publicação: `article:published_time`, `datePublished` do artigo em
JSON-LD, elementos explicitamente marcados como publicação e o rótulo visível
`Publicado em`. A atualização usa suas próprias fontes equivalentes. Uma publicação
de maior prioridade inválida fica pendente, mesmo se houver outro valor visível.
Datas sem hora permanecem `AAAA-MM-DD`; horários sem segundos mantêm precisão
de minutos; sem fuso, não recebem um fuso presumido.
O fuso informado pela fonte é preservado. `coletado_em` é o instante do acesso
registrado em UTC. Publicação ausente ou inválida nunca entra como ano confirmado.

O coletor se identifica como `TCCLLM-Coletor/0.1`, consulta
[robots.txt](https://prefeitura.rio/robots.txt) por origem e respeita proibições,
`Crawl-delay` e `Request-rate` quando fornecidos. Regras não verificáveis impedem
acesso às notícias daquela origem e geram falha. Requisições são sequenciais,
com timeout, até três tentativas por padrão para erros de rede/HTTP temporários
e espera crescente. Respostas 429/503 respeitam `Retry-After`; uma espera exigida
superior a 60 segundos é registrada como falha para retomar em outra execução.

## JSON e registros

Por padrão, os arquivos são salvos em `resultados/ARTISTA_ANO_TIMESTAMP.json`,
relativo ao diretório de execução, com UTF-8. Cada execução recebe um arquivo próprio;
`--saida` sobrescreve o caminho escolhido. O diretório de resultados é ignorado pelo Git.

O objeto contém `consulta`, horários da execução, `mecanismo_descoberta`, `resumo`,
`urls_descobertas`, `noticias` e `registros`. Cada item de `noticias` tem:

```json
{
  "artista_consultado": "Shakira",
  "ano_consultado": 2026,
  "url": "URL oficial descoberta na busca",
  "titulo": "Título original",
  "texto": "Corpo original da notícia",
  "publicado_em": "2026-05-03T10:37:17+00:00",
  "atualizado_em": null,
  "coletado_em": "Instante da coleta em ISO 8601, com fuso UTC"
}
```

O exemplo ilustra os campos, não é um resultado de coleta.
Metadados ausentes recebem `null`. `registros` guarda URL, `status`
(`descartado`, `pendente`, `falha`), `etapa` e `motivo`. Pendências de extração/data
também preservam dados extraídos e o valor bruto de publicação disponível.
Uma atualização inválida gera pendência de metadados sem impedir uma notícia com
publicação válida. As contagens de registros podem incluir descoberta e metadados,
portanto não são todas mutuamente exclusivas por notícia.

O resumo informa descobertas, páginas de busca acessadas, páginas processadas,
notícias aceitas, descartes, pendências, falhas e `parcial`, com seus motivos.
Páginas não processadas por limite são registradas individualmente. Erros de
descoberta, paginação repetida, pendências de revisão e erros por página também
tornam o resultado parcial.
O processo retorna código **2 se houver falhas registradas**, mas salva o JSON
com os resultados acessíveis. Limites e pendências sem falhas retornam 0; confira
sempre `resumo.parcial` e `registros`. Falha de acesso não significa busca vazia.

## Validação e limitações

Execute os testes locais, sem rede:

```powershell
python -m unittest discover -s tests -v
```

As fixtures são recortes do HTML observado nos dois templates e cobrem remoção
de conteúdo acessório, datas, ano de publicação, nome com acentos, publicação
ausente, descoberta/paginação, limites, robots e tentativas temporárias.

Na inspeção de 11/10/2026, a busca por Shakira retornou 24 URLs candidatas na
primeira página, incluindo subdomínios de diferentes órgãos. O link publicado
para a segunda página apontou para `reflorestario.prefeitura.rio/page/2/?s=Shakira`
e redirecionou para um site externo (ArcGIS), onde a inspeção inicial encontrou
HTTP 404. O coletor bloqueia esse redirecionamento antes de acessar o domínio
externo. As alternativas `/page/2/` e `?paged=2` na raiz repetiram os
mesmos resultados. O coletor segue o link publicado e registra a falha: **não há
evidência de cobertura completa do ano**. A disponibilidade e o HTML podem mudar.

A execução real `--artista Shakira --ano 2026 --max-paginas 6` descobriu 24 URLs,
processou seis páginas, aceitou quatro notícias, descartou duas (uma de 2025 e
uma página corporativa), registrou 18 páginas não processadas pelo limite e uma
falha de paginação. O arquivo local é `resultados/shakira_2026_validacao.json`.
Os corpos e datas dos resultados foram conferidos; também foi conferido o HTML
real de uma notícia da raiz, com publicação e atualização em UTC preservadas.
Os 18 testes locais passaram sem depender da rede.

A presença do artista é apenas um filtro de candidatura: pode haver outros assuntos
e vários eventos do mesmo artista no ano. A busca interna pode omitir publicações,
tratar acentos/nomes de modo diferente do filtro local ou apresentar paginação
defeituosa. Datas de fontes distintas podem divergir; usa-se a prioridade documentada,
sem reconciliar automaticamente conflitos. O conteúdo atual de uma notícia antiga
pode ter sido alterado; o JSON não comprova qual texto estava disponível antes de
um evento. O corte temporal para previsão, snapshots históricos, deduplicação de
textos, associação a eventos, LLM, clima e mobilidade serão etapas posteriores.
