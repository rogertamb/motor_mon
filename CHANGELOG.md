# Changelog – Motor de Abastecimento Monitor

Todas as mudanças relevantes deste projeto são documentadas aqui.  
Formato baseado em [Keep a Changelog](https://keepachangelog.com/pt-BR/1.0.0/).

---

## [1.4.0] – 2026-05-05

### Adicionado
- **Hero card da Visão Geral no Grafana** — substitui os 4 stat panels antigos por um único painel HTML Graphics no topo do dashboard com:
  - **Semáforo grande** com efeito glow (verde/amarelo/vermelho/azul pulsante para "Em Execução")
  - Badge **Habilitado/Desabilitado** + badge **AO VIVO** + badge do **step atual** quando em execução
  - **Última mensagem** do job em destaque (preview de até 240 chars)
  - 4 métricas em cards: Duração Última (com `% vs média` e barra), Sucesso 90d (com barra), Falhas 30d, Locks ativos — cores semânticas por threshold
- Nova query `Q["overview_hero"]` consolida status + duração + média + sucesso + falhas 30d + locks num único SELECT

### Alterado
- Plugin HTML Graphics agora usa `onInit` (não `codeData`) — `codeData` permanece como JSON estático

---

## [1.3.0] – 2026-05-05

### Adicionado
- **Painel HTML Graphics no Grafana** (`gapit-htmlgraphics-panel`) — replica fielmente os cards do Flask: stripe colorido, badge `% vs média`, barra de comparação com pino azul (média), `Sucesso%`, badge da database e ícone ⚠ de anomalia
- Função `htmlgraphics()` em `grafana_provision.py` — wrapper para painéis customizados via plugin HTML Graphics
- Constantes `STEPS_HTML`, `STEPS_CSS`, `STEPS_JS` com a renderização dos cards
- A tabela detalhada com gauges foi mantida como fallback abaixo dos cards

### Pré-requisito
- Instalar o plugin no servidor Grafana: `grafana-cli plugins install gapit-htmlgraphics-panel` (ou via UI: Administration → Plugins → "HTML Graphics")

---

## [1.2.0] – 2026-05-05

### Adicionado
- **Steps – Flask**: cards repaginados com **stripe colorido** no topo, **badge `% vs média`** (verde ≤10%, amarelo 10–50% mais lento, vermelho >50%), **barra de comparação** com pino azul marcando a média histórica e **anel SVG de sucesso 90d** (verde ≥95%, amarelo ≥80%, vermelho <80%); gatilho automático de anomalia quando duração diverge >10% da média
- **Steps – Grafana**: tabela convertida em "cards visuais" com **gauges gradientes** nas colunas `Ult. Dur.`, `Media 90d`, `% vs Media` e `Sucesso%`; coluna `Anomalia` com 5 estados (`Critica`, `Lenta`, `Normal`, `Rapida`, `Sem base`) e cores próprias; threshold de anomalia alinhado ao Flask (>10% lento)

### Alterado
- `Q["steps_overview"]`: colunas de duração agora numéricas (INT) para suporte a gauge cells; nova coluna `% vs Media`; classificação de anomalia baseada em proporção (1.10× / 1.50×) em vez de desvio-padrão
- `cellHeight` do painel de steps elevado para `md` para melhor legibilidade dos gauges

---

## [1.1.0] – 2026-05-05

### Adicionado
- **Histórico de Falhas** – nova seção com tabela das últimas ocorrências de falha/cancelamento/retry do job, com classificação automática de causa provável (semáforo, deadlock, timeout, conectividade, integração CRM/ODS/Phoenix, envio de arquivos)
- **Seletor de período independente** no histórico de falhas (De / até), com atalhos rápidos 15d · 30d · 90d · Hoje; por padrão carrega a partir da data da última execução
- **Endpoint `/api/failure-history`** – consulta de falhas desacoplada do endpoint principal, aceita parâmetros `from`, `to` e `days`
- **Insights do Processo** – análise dinâmica que detecta: steps instáveis (< 95% de sucesso), anomalias de duração ativas, causa de falha recorrente nos 15 dias, steps mais lentos que a média histórica, jobs relacionados com falha e steps pulados na data selecionada
- **Grafana – painel de falhas** – stat com contador de falhas (verde/amarelo/vermelho) e tabela detalhada com classificação de causa por cor
- **Auto-refresh** – quando o job está em execução ao vivo, o dashboard atualiza automaticamente a cada 30 segundos; para automaticamente ao detectar fim da execução
- **Status no histórico de falhas** – coluna de status (Falhou / Cancelado / Retry) com badge colorido

### Corrigido
- **XSS** – aplicado `escHtml()` em todos os pontos onde dados do servidor eram inseridos em `innerHTML` sem escape: `sql_text`, `status`, `wait_type`, `db_name`, `login_name`, `job name`, `step_name`, `database_name`, `anomaly_reason`, `last_message`
- **Campo `enabled` no modo histórico** – retornava `undefined` (exibia "Não" incorretamente); agora a query faz JOIN com `sysjobs` e sempre retorna o valor correto
- **`CROSS APPLY` → `OUTER APPLY`** em `get_active_locks` – sessões bloqueadas sem `sql_handle` eram descartadas silenciosamente; agora todas as sessões ativas aparecem
- **`DATEADD` com tipo `DATE`** – `DATEADD(SECOND, ..., DATE)` não é suportado pelo SQL Server; corrigido para `DATETIME` nas queries do Grafana (`Q["job_history"]` e `Q["related_jobs"]`)
- **Histórico de falhas retornava 0** – query filtrava apenas `run_status = 0`; expandido para `IN (0, 2, 3)` para capturar também Retry e Cancelado
- **Estado `no_execution`** – modo histórico sem execução na data exibia campos vazios; agora mostra "Sem execução / Nenhuma execução registrada em YYYY-MM-DD"
- **Certificado SSL autoassinado** – adicionado `verify=False` e supressão de `InsecureRequestWarning` no `grafana_provision.py` para ambientes internos com HTTPS local
- **Datasource padrão do Grafana** – atualizado de `FORTBRS-DWBI03` para `DWBI03-SKYONE` (nome real no ambiente)

---

## [1.0.0] – 2026-05-05

### Adicionado
- Dashboard Flask com semáforo visual do job principal (`0_MAIN_FORTBRAS_MOTOR_ABASTECIMENTO`)
- Análise de steps com detecção de anomalia de duração por método IQR (90 dias de histórico)
- Painel de locks e bloqueios ativos em tempo real
- Painel de jobs relacionados (MOTOR, ABASTECIMENTO, ODS, PHOENIX, CRM, BUQUET)
- Steps de semáforo/trava com visualização do SQL de controle
- Seletor de data com modo histórico e modo ao vivo
- Navegação por data (anterior/próximo) e botão "Última execução"
- Banner de modo histórico e badge "Tempo Real"
- Modal de detalhe por step com estatísticas IQR, duração mínima/média/máxima e mensagem de erro
- Sugestões estáticas de monitoramento do processo
- Script `grafana_provision.py` para provisionamento automático do dashboard no Grafana 12 via API
- Dashboard Grafana com painéis: status do job, step em execução, duração, taxa de sucesso 90d, histórico de duração, tabela de steps com anomalia (2 desvios-padrão), duração por step ao longo do tempo, locks, jobs relacionados e steps de semáforo
