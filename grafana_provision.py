#!/usr/bin/env python3
"""
grafana_provision.py
Provisiona no Grafana 12 (Oracle Linux) o dashboard de monitoramento
do Motor de Abastecimento.

O datasource SQL Server ja deve existir no Grafana — este script apenas
referencia o datasource existente pelo nome e envia o dashboard via API.

Uso:
    pip install requests

    # Descubra o nome exato do datasource no Grafana:
    python grafana_provision.py --url http://grafana:3000 --token glsa_xxx --list-ds

    # Envia o dashboard referenciando um datasource existente:
    python grafana_provision.py --url http://grafana:3000 --token glsa_xxx --ds "Nome do Datasource"

    # Gera o JSON para importar manualmente (Dashboards > New > Import):
    python grafana_provision.py --export-only --ds "Nome do Datasource"
"""

import argparse
import json
import sys
import requests
import urllib3

# Grafana interno com certificado autoassinado — desabilita verificação SSL
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ─── Configuração ─────────────────────────────────────────────────────────────

GRAFANA_URL   = "http://localhost:3000"
GRAFANA_TOKEN = "SEU_SERVICE_ACCOUNT_TOKEN"   # glsa_xxxxxxxxxxxxxxxx

# Nome exato do datasource SQL Server ja configurado no Grafana.
# Execute com --list-ds para listar os datasources disponiveis.
DS_NAME = "DWBI03-SKYONE"

JOB_NAME        = "0_MAIN_FORTBRAS_MOTOR_ABASTECIMENTO"
DASHBOARD_UID   = "motor-abastecimento"
DASHBOARD_TITLE = "Motor de Abastecimento - Monitor"
FOLDER_ID       = 0   # 0 = pasta General

# ─── Expressao helper: run_date + run_time -> DATETIME ────────────────────────

_DT = (
    "DATEADD(SECOND,"
    "(run_time/10000)*3600+((run_time%10000)/100)*60+(run_time%100),"
    "CAST(CAST(run_date AS CHAR(8)) AS DATETIME))"
)

# ─── SQL Queries ──────────────────────────────────────────────────────────────
# Escaping:
#   Queries sem f-string  -> ${job_name}   (variavel Grafana)
#   Queries com f-string  -> ${{job_name}} (escape do f-string)

Q = {}

Q["job_status"] = """
SELECT
    CASE
        WHEN ja.start_execution_date IS NOT NULL
         AND ja.stop_execution_date  IS NULL THEN 3
        WHEN lh.run_status = 1               THEN 1
        WHEN lh.run_status = 0               THEN 0
        ELSE 2
    END AS status_code
FROM msdb.dbo.sysjobs j
LEFT JOIN msdb.dbo.sysjobactivity ja
       ON j.job_id = ja.job_id
      AND ja.session_id = (
            SELECT MAX(session_id) FROM msdb.dbo.syssessions
            WHERE  agent_start_date IS NOT NULL)
OUTER APPLY (
    SELECT TOP 1 run_status
    FROM   msdb.dbo.sysjobhistory
    WHERE  job_id = j.job_id AND step_id = 0
    ORDER  BY run_date DESC, run_time DESC
) lh
WHERE j.name = '${job_name}'
"""

Q["current_step"] = """
SELECT
    ISNULL(js.step_name, 'Ocioso') AS step_name,
    CASE
        WHEN ja.start_execution_date IS NOT NULL
         AND ja.stop_execution_date  IS NULL
        THEN DATEDIFF(SECOND, ja.start_execution_date, GETDATE())
        ELSE 0
    END AS elapsed_s
FROM msdb.dbo.sysjobs j
LEFT JOIN msdb.dbo.sysjobactivity ja
       ON j.job_id = ja.job_id
      AND ja.session_id = (
            SELECT MAX(session_id) FROM msdb.dbo.syssessions
            WHERE  agent_start_date IS NOT NULL)
LEFT JOIN msdb.dbo.sysjobsteps js
       ON j.job_id = js.job_id
      AND js.step_id = ja.last_executed_step_id
WHERE j.name = '${job_name}'
"""

Q["last_duration"] = """
SELECT TOP 1
    (run_duration/10000)*3600
    + ((run_duration%10000)/100)*60
    + (run_duration%100) AS duracao_s
FROM  msdb.dbo.sysjobhistory
WHERE job_id = (SELECT job_id FROM msdb.dbo.sysjobs WHERE name = '${job_name}')
  AND step_id = 0
ORDER BY run_date DESC, run_time DESC
"""

Q["success_rate"] = """
SELECT
    CAST(ROUND(
        100.0 * SUM(CASE WHEN run_status = 1 THEN 1 ELSE 0 END) / COUNT(*),
    1) AS DECIMAL(5,1)) AS taxa_pct
FROM  msdb.dbo.sysjobhistory
WHERE job_id = (SELECT job_id FROM msdb.dbo.sysjobs WHERE name = '${job_name}')
  AND step_id = 0
  AND run_date >= CONVERT(int,
        CONVERT(varchar(8), DATEADD(day,-90,GETDATE()), 112))
"""

Q["job_history"] = f"""
SELECT
    exec_dt AS time,
    duracao_s,
    resultado
FROM (
    SELECT
        {_DT}                                                                      AS exec_dt,
        (run_duration/10000)*3600+((run_duration%10000)/100)*60+(run_duration%100) AS duracao_s,
        CASE run_status WHEN 1 THEN 'Sucesso' ELSE 'Falhou' END                    AS resultado
    FROM  msdb.dbo.sysjobhistory
    WHERE job_id = (SELECT job_id FROM msdb.dbo.sysjobs WHERE name = '${{job_name}}')
      AND step_id    = 0
      AND run_duration > 0
) t
WHERE $__timeFilter(exec_dt)
ORDER BY time ASC
"""

Q["steps_ts"] = f"""
SELECT
    exec_dt AS time,
    step_name,
    duracao_s
FROM (
    SELECT
        {_DT}                                                                      AS exec_dt,
        step_name,
        (run_duration/10000)*3600+((run_duration%10000)/100)*60+(run_duration%100) AS duracao_s
    FROM  msdb.dbo.sysjobhistory
    WHERE job_id = (SELECT job_id FROM msdb.dbo.sysjobs WHERE name = '${{job_name}}')
      AND step_id    > 0
      AND run_status = 1
      AND run_duration > 0
) t
WHERE $__timeFilter(exec_dt)
ORDER BY time ASC, step_name
"""

Q["steps_overview"] = f"""
WITH stats AS (
    SELECT
        step_id,
        COUNT(*)                                                                    AS total_runs,
        SUM(CASE WHEN run_status = 1 THEN 1 ELSE 0 END)                            AS ok,
        AVG(CASE WHEN run_status = 1 AND run_duration > 0
                 THEN CAST((run_duration/10000)*3600+((run_duration%10000)/100)*60
                           +(run_duration%100) AS FLOAT) ELSE NULL END)            AS avg_s,
        ISNULL(STDEV(CASE WHEN run_status = 1 AND run_duration > 0
                     THEN CAST((run_duration/10000)*3600+((run_duration%10000)/100)*60
                               +(run_duration%100) AS FLOAT) ELSE NULL END), 0)   AS std_s
    FROM  msdb.dbo.sysjobhistory
    WHERE job_id = (SELECT job_id FROM msdb.dbo.sysjobs WHERE name = '${{job_name}}')
      AND step_id > 0
      AND run_date >= CONVERT(int,CONVERT(varchar(8),DATEADD(day,-90,GETDATE()),112))
    GROUP BY step_id
),
lat AS (
    SELECT step_id, run_status, run_duration,
           ROW_NUMBER() OVER (PARTITION BY step_id
                              ORDER BY run_date DESC, run_time DESC) AS rn
    FROM  msdb.dbo.sysjobhistory
    WHERE job_id = (SELECT job_id FROM msdb.dbo.sysjobs WHERE name = '${{job_name}}')
      AND step_id > 0
)
SELECT
    js.step_id                                                         AS [#],
    js.step_name                                                       AS [Step],
    ISNULL(js.database_name,'msdb')                                   AS [Database],
    CASE l.run_status
        WHEN 1 THEN 'Sucesso' WHEN 0 THEN 'Falhou'
        WHEN 2 THEN 'Retry'   WHEN 3 THEN 'Cancelado' ELSE '-'
    END                                                                AS [Status],
    CAST(ISNULL(
        (l.run_duration/10000)*3600+((l.run_duration%10000)/100)*60+(l.run_duration%100), 0
    ) AS INT)                                                          AS [Ult. Dur.],
    CAST(ISNULL(ROUND(s.avg_s,0), 0) AS INT)                          AS [Media 90d],
    CASE
        WHEN s.avg_s IS NULL OR s.avg_s = 0 THEN 0
        ELSE CAST(ROUND(
            100.0 *
            ((l.run_duration/10000)*3600+((l.run_duration%10000)/100)*60+(l.run_duration%100))
            / s.avg_s, 0) AS INT)
    END                                                                AS [% vs Media],
    CAST(ISNULL(ROUND(100.0*s.ok/NULLIF(s.total_runs,0),1), 0) AS DECIMAL(5,1)) AS [Sucesso%],
    ISNULL(s.total_runs, 0)                                           AS [Runs 90d],
    CASE
        WHEN s.avg_s IS NULL OR s.avg_s = 0 THEN 'Sem base'
        WHEN ((l.run_duration/10000)*3600+((l.run_duration%10000)/100)*60+(l.run_duration%100))
             > s.avg_s * 1.5 THEN 'Critica'
        WHEN ((l.run_duration/10000)*3600+((l.run_duration%10000)/100)*60+(l.run_duration%100))
             > s.avg_s * 1.10 THEN 'Lenta'
        WHEN ((l.run_duration/10000)*3600+((l.run_duration%10000)/100)*60+(l.run_duration%100))
             < s.avg_s * 0.90 AND s.avg_s > 5 THEN 'Rapida'
        ELSE 'Normal'
    END                                                                AS [Anomalia]
FROM  msdb.dbo.sysjobsteps js
LEFT JOIN stats s ON js.step_id = s.step_id
LEFT JOIN lat   l ON js.step_id = l.step_id AND l.rn = 1
WHERE js.job_id = (SELECT job_id FROM msdb.dbo.sysjobs WHERE name = '${{job_name}}')
ORDER BY js.step_id
"""

Q["locks"] = """
SELECT
    r.session_id                        AS [SPID],
    ISNULL(r.blocking_session_id, 0)    AS [Bloq. por],
    r.status                            AS [Status],
    ISNULL(r.wait_type,'-')             AS [Wait Type],
    r.wait_time          / 1000         AS [Wait (s)],
    r.total_elapsed_time / 1000         AS [Elapsed (s)],
    DB_NAME(r.database_id)              AS [Database],
    s.login_name                        AS [Login],
    LEFT(t.text, 200)                   AS [SQL]
FROM  sys.dm_exec_requests r
JOIN  sys.dm_exec_sessions s ON r.session_id = s.session_id
CROSS APPLY sys.dm_exec_sql_text(r.sql_handle) t
WHERE r.session_id > 50
  AND r.session_id <> @@SPID
ORDER BY ISNULL(r.blocking_session_id,0) DESC, r.wait_time DESC
"""

Q["related_jobs"] = """
SELECT
    j.name                                                                  AS [Job],
    CASE
        WHEN ja.start_execution_date IS NOT NULL
         AND ja.stop_execution_date  IS NULL THEN 3
        WHEN lh.run_status = 1 THEN 1
        WHEN lh.run_status = 0 THEN 0
        ELSE 2
    END                                                                     AS [status_code],
    CASE
        WHEN ja.start_execution_date IS NOT NULL
         AND ja.stop_execution_date  IS NULL THEN 'Rodando'
        WHEN lh.run_status = 1 THEN 'Sucesso'
        WHEN lh.run_status = 0 THEN 'Falhou'
        ELSE '-'
    END                                                                     AS [Status],
    CONVERT(varchar(16), lh.last_run_dt, 120)                              AS [Ultima Run],
    CONCAT(lh.dur/3600,'h ',(lh.dur%3600)/60,'m ',lh.dur%60,'s')          AS [Duracao]
FROM msdb.dbo.sysjobs j
LEFT JOIN msdb.dbo.sysjobactivity ja
       ON j.job_id = ja.job_id
      AND ja.session_id = (
            SELECT MAX(session_id) FROM msdb.dbo.syssessions
            WHERE  agent_start_date IS NOT NULL)
OUTER APPLY (
    SELECT TOP 1
        run_status,
        DATEADD(SECOND,
            (run_time/10000)*3600+((run_time%10000)/100)*60+(run_time%100),
            CAST(CAST(run_date AS CHAR(8)) AS DATETIME)) AS last_run_dt,
        (run_duration/10000)*3600+((run_duration%10000)/100)*60+(run_duration%100) AS dur
    FROM  msdb.dbo.sysjobhistory
    WHERE job_id = j.job_id AND step_id = 0
    ORDER BY run_date DESC, run_time DESC
) lh
WHERE j.name LIKE '%MOTOR%'
   OR j.name LIKE '%ABASTECIMENTO%'
   OR j.name LIKE '%ODS%'
   OR j.name LIKE '%PHOENIX%'
   OR j.name LIKE '%CRM%'
   OR j.name LIKE '%BUQUET%'
ORDER BY j.name
"""

Q["failure_count"] = """
SELECT COUNT(*) AS falhas_15d
FROM msdb.dbo.sysjobhistory
WHERE job_id = (SELECT job_id FROM msdb.dbo.sysjobs WHERE name = '${job_name}')
  AND step_id    = 0
  AND run_status IN (0, 2, 3)
  AND run_date  >= CONVERT(int,
        CONVERT(varchar(8), DATEADD(day, -15, GETDATE()), 112))
"""

Q["failure_history"] = """
SELECT
    CONVERT(varchar(16),
        DATEADD(SECOND,
            (h.run_time/10000)*3600+((h.run_time%10000)/100)*60+(h.run_time%100),
            CAST(CAST(h.run_date AS CHAR(8)) AS DATETIME)
        ), 120)                                                    AS [Data/Hora],
    CONCAT(
        (h.run_duration/10000),'h ',
        ((h.run_duration%10000)/100),'m ',
        (h.run_duration%100),'s')                                 AS [Duracao],
    ISNULL('#'+CAST(fs.step_id AS VARCHAR)+' '+fs.step_name,
           '(nao identificado)')                                   AS [Step com Falha],
    CASE
        WHEN UPPER(ISNULL(fs.step_name,'')) LIKE '%SEMAFORO%'
          OR UPPER(ISNULL(fs.step_name,'')) LIKE '%TRAVA%'
          OR UPPER(ISNULL(fs.step_name,'')) LIKE '%WAIT%'
            THEN 'Semaforo / Trava'
        WHEN UPPER(ISNULL(fs.message,h.message)) LIKE '%DEADLOCK%'
            THEN 'Deadlock'
        WHEN UPPER(ISNULL(fs.message,h.message)) LIKE '%TIMEOUT%'
          OR UPPER(ISNULL(fs.message,h.message)) LIKE '%TIMED OUT%'
            THEN 'Timeout'
        WHEN UPPER(ISNULL(fs.message,h.message)) LIKE '%LOGIN%'
          OR UPPER(ISNULL(fs.message,h.message)) LIKE '%AUTHENTICATION%'
            THEN 'Autenticacao / Permissao'
        WHEN UPPER(ISNULL(fs.message,h.message)) LIKE '%NETWORK%'
          OR UPPER(ISNULL(fs.message,h.message)) LIKE '%CONNECTION%'
            THEN 'Conectividade'
        WHEN UPPER(ISNULL(fs.step_name,'')) LIKE '%BUQUET%'
          OR UPPER(ISNULL(fs.step_name,'')) LIKE '%ENVIA%'
            THEN 'Envio de Arquivos'
        WHEN UPPER(ISNULL(fs.step_name,'')) LIKE '%CRM%'
            THEN 'Integracao CRM'
        WHEN UPPER(ISNULL(fs.step_name,'')) LIKE '%ODS%'
            THEN 'Camada ODS'
        ELSE 'Investigar mensagem'
    END                                                            AS [Possivel Causa],
    LEFT(ISNULL(fs.message, h.message), 300)                      AS [Mensagem]
FROM msdb.dbo.sysjobhistory h
OUTER APPLY (
    SELECT TOP 1 sh.step_id, sh.step_name, sh.message
    FROM msdb.dbo.sysjobhistory sh
    WHERE sh.job_id   = h.job_id
      AND sh.run_date = h.run_date
      AND sh.step_id  > 0
      AND sh.run_status = 0
    ORDER BY sh.run_time DESC
) fs
WHERE h.job_id = (SELECT job_id FROM msdb.dbo.sysjobs WHERE name = '${job_name}')
  AND h.step_id    = 0
  AND h.run_status IN (0, 2, 3)
  AND h.run_date  >= CONVERT(int,
        CONVERT(varchar(8), DATEADD(day, -15, GETDATE()), 112))
ORDER BY h.run_date DESC, h.run_time DESC
"""

Q["semaphore_steps"] = """
SELECT
    js.step_id                                                              AS [#],
    js.step_name                                                            AS [Step],
    ISNULL(js.database_name,'msdb')                                        AS [Database],
    CASE lh.run_status
        WHEN 1 THEN 'Sucesso' WHEN 0 THEN 'Falhou' ELSE '-'
    END                                                                     AS [Ultimo Status],
    ISNULL(CAST(
        (lh.run_duration/10000)*3600+((lh.run_duration%10000)/100)*60+(lh.run_duration%100)
    AS VARCHAR)+' s','-')                                                  AS [Ult. Dur.]
FROM  msdb.dbo.sysjobsteps js
OUTER APPLY (
    SELECT TOP 1 run_status, run_duration
    FROM   msdb.dbo.sysjobhistory
    WHERE  job_id  = js.job_id
      AND  step_id = js.step_id
    ORDER  BY run_date DESC, run_time DESC
) lh
WHERE js.job_id = (SELECT job_id FROM msdb.dbo.sysjobs WHERE name = '${job_name}')
  AND (UPPER(js.step_name) LIKE '%SEMAFORO%'
    OR UPPER(js.step_name) LIKE '%TRAVA%'
    OR UPPER(js.step_name) LIKE '%WAIT%')
ORDER BY js.step_id
"""

# ═══════════════════════════════════════════════════════════════════════════════
# Panel builders
# ═══════════════════════════════════════════════════════════════════════════════

_pid = 0

def _next():
    global _pid
    _pid += 1
    return _pid

def _ds(uid, ds_type="mssql"):
    return {"type": ds_type, "uid": uid}

def _tgt(uid, sql, fmt="table"):
    return {
        "refId": "A",
        "datasource": _ds(uid),
        "rawSql": sql.strip(),
        "format": fmt,
        "rawQuery": True,
    }

def _row(title, y):
    return {
        "id": _next(), "type": "row", "title": title,
        "gridPos": {"h": 1, "w": 24, "x": 0, "y": y},
        "collapsed": False, "panels": [],
    }

def stat(uid, title, sql, x, y, w=6, h=4, unit="", mappings=None, thresholds=None):
    return {
        "id": _next(), "type": "stat", "title": title,
        "gridPos": {"h": h, "w": w, "x": x, "y": y},
        "datasource": _ds(uid),
        "targets": [_tgt(uid, sql)],
        "options": {
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "colorMode": "background", "graphMode": "none",
            "orientation": "auto", "textMode": "auto", "justifyMode": "center",
        },
        "fieldConfig": {
            "defaults": {
                "unit": unit,
                "mappings": mappings or [],
                "thresholds": thresholds or {
                    "mode": "absolute",
                    "steps": [{"color": "blue", "value": None}],
                },
            },
            "overrides": [],
        },
    }

def table(uid, title, sql, x, y, w=24, h=10, overrides=None):
    return {
        "id": _next(), "type": "table", "title": title,
        "gridPos": {"h": h, "w": w, "x": x, "y": y},
        "datasource": _ds(uid),
        "targets": [_tgt(uid, sql, "table")],
        "options": {
            "showHeader": True, "cellHeight": "sm",
            "footer": {"show": False, "reducer": ["sum"]},
        },
        "fieldConfig": {"defaults": {}, "overrides": overrides or []},
    }

def htmlgraphics(uid, title, sql, html, css, js, x, y, w=24, h=12):
    """Painel via plugin gapit-htmlgraphics-panel (v2+).
    - 'codeData' eh JSON (config estatica)
    - 'onInit' eh o JavaScript executado apos render, com acesso a
      'htmlNode' (DOM root) e 'data' (panelData)
    """
    return {
        "id": _next(), "type": "gapit-htmlgraphics-panel", "title": title,
        "gridPos": {"h": h, "w": w, "x": x, "y": y},
        "datasource": _ds(uid),
        "targets": [_tgt(uid, sql, "table")],
        "options": {
            "html":               html,
            "css":                css,
            "codeData":           "{}",
            "onInit":             js,
            "renderOnMount":      True,
            "centerAlignContent": False,
            "useGrandResult":     False,
            "calcsMode":          "html",
            "add100Percentage":   False,
            "dynamicHtmlGraphics": False,
            "dynamicData":        True,
            "dynamicProps":       False,
            "dynamicFontSize":    False,
            "SVGBaseFix":         True,
            "onInitOnResize":     True,
            "rootCSS":            "",
            "polygons":           [],
        },
        "fieldConfig": {"defaults": {}, "overrides": []},
    }

def timeseries(uid, title, sql, x, y, w=24, h=8, unit="s"):
    return {
        "id": _next(), "type": "timeseries", "title": title,
        "gridPos": {"h": h, "w": w, "x": x, "y": y},
        "datasource": _ds(uid),
        "targets": [_tgt(uid, sql, "time_series")],
        "options": {
            "tooltip": {"mode": "multi", "sort": "none"},
            "legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
        },
        "fieldConfig": {
            "defaults": {
                "unit": unit,
                "custom": {"lineWidth": 2, "fillOpacity": 8, "showPoints": "never"},
            },
            "overrides": [],
        },
    }

# ─── Mapeamentos de cor reutilizaveis ─────────────────────────────────────────

STATUS_MAPS = [{
    "type": "value",
    "options": {
        "0": {"text": "Falhou",      "color": "red",   "index": 0},
        "1": {"text": "Sucesso",     "color": "green", "index": 1},
        "2": {"text": "Ocioso",      "color": "gray",  "index": 2},
        "3": {"text": "Em Execucao", "color": "blue",  "index": 3},
    },
}]

DUR_THRESHOLDS = {
    "mode": "absolute",
    "steps": [
        {"color": "green",  "value": None},
        {"color": "yellow", "value": 1800},
        {"color": "red",    "value": 3600},
    ],
}

PCT_THRESHOLDS = {
    "mode": "absolute",
    "steps": [
        {"color": "red",    "value": None},
        {"color": "yellow", "value": 80},
        {"color": "green",  "value": 95},
    ],
}

def _color_ov(col, mapping_dict, mode="color-background"):
    return {
        "matcher": {"id": "byName", "options": col},
        "properties": [
            {"id": "mappings",           "value": [{"type": "value", "options": mapping_dict}]},
            {"id": "custom.displayMode", "value": mode},
        ],
    }

def _hide_ov(col):
    return {
        "matcher": {"id": "byName", "options": col},
        "properties": [{"id": "custom.hidden", "value": True}],
    }

def _gauge_ov(col, unit, thresholds, mode="gradient"):
    """mode: 'basic' (solid bar como Flask), 'gradient' (gradient), 'lcd'."""
    legacy_mode = {"basic": "basic", "gradient": "gradient-gauge", "lcd": "lcd-gauge"}[mode]
    return {
        "matcher": {"id": "byName", "options": col},
        "properties": [
            {"id": "unit",               "value": unit},
            {"id": "thresholds",         "value": thresholds},
            {"id": "custom.displayMode", "value": legacy_mode},
            {"id": "custom.cellOptions", "value": {"type": "gauge", "mode": mode}},
            {"id": "custom.align",       "value": "center"},
        ],
    }

RATIO_THRESHOLDS = {
    "mode": "absolute",
    "steps": [
        {"color": "green",  "value": None},
        {"color": "yellow", "value": 110},
        {"color": "red",    "value": 150},
    ],
}

DUR_STEP_THRESHOLDS = {
    "mode": "absolute",
    "steps": [
        {"color": "green",  "value": None},
        {"color": "yellow", "value": 600},
        {"color": "orange", "value": 1800},
        {"color": "red",    "value": 3600},
    ],
}

STEPS_OV = [
    _color_ov("Status", {
        "Sucesso":   {"color": "green",  "index": 0},
        "Falhou":    {"color": "red",    "index": 1},
        "Retry":     {"color": "yellow", "index": 2},
        "Cancelado": {"color": "gray",   "index": 3},
    }),
    _color_ov("Anomalia", {
        "Normal":   {"color": "green",     "index": 0},
        "Lenta":    {"color": "yellow",    "index": 1},
        "Critica":  {"color": "red",       "index": 2},
        "Rapida":   {"color": "blue",      "index": 3},
        "Sem base": {"color": "dark-gray", "index": 4},
    }),
    _gauge_ov("Ult. Dur.",  "s",       DUR_STEP_THRESHOLDS, mode="basic"),
    _gauge_ov("Media 90d",  "s",       DUR_STEP_THRESHOLDS, mode="basic"),
    _gauge_ov("% vs Media", "percent", RATIO_THRESHOLDS),
    _gauge_ov("Sucesso%",   "percent", PCT_THRESHOLDS),
    {
        "matcher": {"id": "byName", "options": "#"},
        "properties": [{"id": "custom.width", "value": 50}],
    },
    {
        "matcher": {"id": "byName", "options": "Step"},
        "properties": [{"id": "custom.width", "value": 260}],
    },
]

# ─── HTML Graphics: cards de steps no estilo Flask ────────────────────────────

STEPS_JS = r"""
// Roda no onInit do gapit-htmlgraphics-panel.
// Disponiveis: htmlNode (DOM), data (panelData), options, theme.
try{
  var grid = htmlNode.querySelector('#grid');
  if(!grid){ return; }
  var series=(data&&data.series&&data.series[0])||null;
  if(!series||!series.fields||!series.fields.length){grid.innerHTML='<div class="sc-empty">Sem dados.</div>';return;}
  var F={};
  series.fields.forEach(function(f){F[f.name]=(f.values&&f.values.toArray)?f.values.toArray():(f.values||[]);});
  var n=(F['#']||[]).length;
  if(!n){grid.innerHTML='<div class="sc-empty">Sem linhas.</div>';return;}
  var escH=function(s){return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');};
  var fmtSecs=function(s){s=+s||0;var h=Math.floor(s/3600),m=Math.floor((s%3600)/60),ss=s%60;return (h<10?'0':'')+h+':'+(m<10?'0':'')+m+':'+(ss<10?'0':'')+ss;};
  var html='';
  for(var i=0;i<n;i++){
    var last=+F['Ult. Dur.'][i]||0;
    var avg=+F['Media 90d'][i]||0;
    var ratio=avg>0?last/avg:0;
    var pctDiff=avg>0?Math.round((ratio-1)*100):null;
    var stat=F['Status'][i]||'-';
    var semColor=stat==='Falhou'?'red':stat==='Cancelado'?'gray':stat==='Retry'?'yellow':stat==='Sucesso'?'green':'gray';
    var ratioCls=!avg?'none':ratio>1.5?'over':pctDiff>10?'warn':'ok';
    var ratioLbl=pctDiff===null?'-':pctDiff>0?'+'+pctDiff+'%':pctDiff<0?pctDiff+'%':'=avg';
    var trackMax=Math.max(last,avg*1.6,1);
    var fillW=Math.min(100,last/trackMax*100);
    var avgMark=avg>0?Math.min(100,avg/trackMax*100):null;
    var fillCls=ratio>1.5?'over':pctDiff>10?'warn':'ok';
    var sr=+F['Sucesso%'][i]||0;
    var srCls=sr>=95?'ok':sr>=80?'warn':'bad';
    var statCls=stat==='Sucesso'?'ok':stat==='Falhou'?'bad':stat==='Retry'?'warn':'idle';
    var anom=F['Anomalia']?F['Anomalia'][i]:'';
    var anomTag=(anom==='Critica'||anom==='Lenta')?(' <span class="sc-anom" title="'+escH(anom)+'">!</span>'):'';
    var stepName=F['Step']?F['Step'][i]:'';
    var dbName=F['Database']?F['Database'][i]:'';
    var runs=F['Runs 90d']?F['Runs 90d'][i]:0;
    html+='<div class="sc-card '+semColor+'">'
      +'<div class="sc-accent '+semColor+'"></div>'
      +'<div class="sc-head">'
        +'<div style="min-width:0">'
          +'<div class="sc-id">#'+escH(F['#'][i])+anomTag+'</div>'
          +'<div class="sc-name" title="'+escH(stepName)+'">'+escH(stepName)+'</div>'
          +'<span class="sc-status '+statCls+'">'+escH(stat)+'</span>'
        +'</div>'
        +'<div style="text-align:right;flex-shrink:0">'
          +'<div class="sc-ratio '+ratioCls+'">'+ratioLbl+'</div>'
          +'<div class="sc-ratiolbl">vs media</div>'
        +'</div>'
      +'</div>'
      +'<div class="sc-times">'
        +'<span><strong>'+fmtSecs(last)+'</strong></span>'
        +'<span>avg '+fmtSecs(avg)+'</span>'
      +'</div>'
      +'<div class="sc-bar">'
        +'<div class="sc-fill '+fillCls+'" style="width:'+fillW+'%"></div>'
        +(avgMark!==null?'<div class="sc-mark" style="left:'+avgMark+'%"></div>':'')
      +'</div>'
      +'<div class="sc-foot">'
        +'<span class="sc-success '+srCls+'">'+sr.toFixed(1)+'% ok &middot; <span class="sc-runs">'+escH(runs)+'x</span></span>'
        +'<span class="sc-db">'+escH(dbName)+'</span>'
      +'</div>'
    +'</div>';
  }
  grid.innerHTML = html;
}catch(e){
  try{ htmlNode.querySelector('#grid').innerHTML =
    '<div class="sc-empty">Erro: '+(e&&e.message?e.message:e)+'</div>'; }catch(_){}
}
"""

STEPS_HTML = """
<div id="root" style="width:100%;height:100%;overflow:auto;padding:8px;
  background:#0d1117;color:#e6edf3;font-family:'Segoe UI',sans-serif;box-sizing:border-box">
  <div id="grid" style="display:grid;
       grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:10px"></div>
</div>
"""

STEPS_CSS = """
#root { box-sizing:border-box; }
#root *,
#root *::before,
#root *::after { box-sizing:border-box; }
.sc-card { background:#161b22; border:1px solid #30363d; border-radius:8px;
  position:relative; overflow:hidden; padding:10px 10px 8px; }
.sc-accent { position:absolute; top:0; left:0; right:0; height:3px; }
.sc-accent.green  { background:#00e676; }
.sc-accent.yellow { background:#ffea00; }
.sc-accent.red    { background:#ff1744; }
.sc-accent.blue   { background:#2979ff; }
.sc-accent.gray   { background:#546e7a; }
.sc-card.green  { border-color:#00e676; }
.sc-card.yellow { border-color:#ffea00; }
.sc-card.red    { border-color:#ff1744; }
.sc-card.blue   { border-color:#2979ff; }
.sc-card.gray   { border-color:#546e7a; }

.sc-head { display:flex; justify-content:space-between; align-items:flex-start; gap:6px; }
.sc-id   { color:#8b949e; font-size:.72rem; font-weight:700; }
.sc-name { font-size:.82rem; font-weight:600; line-height:1.25;
  white-space:nowrap; overflow:hidden; text-overflow:ellipsis; max-width:160px; }
.sc-status { display:inline-block; padding:2px 8px; border-radius:4px;
  font-size:.7rem; font-weight:600; margin-top:4px; }
.sc-status.ok   { background:#00e67622; color:#00e676; }
.sc-status.bad  { background:#ff174422; color:#ff1744; }
.sc-status.warn { background:#ffea0022; color:#ffea00; }
.sc-status.idle { background:#546e7a22; color:#8b949e; }

.sc-ratio { font-size:1.05rem; font-weight:700; line-height:1; text-align:right; }
.sc-ratio.ok    { color:#00e676; }
.sc-ratio.warn  { color:#ffea00; }
.sc-ratio.over  { color:#ff1744; }
.sc-ratio.none  { color:#8b949e; }
.sc-ratiolbl { color:#8b949e; font-size:.62rem; }

.sc-times { display:flex; justify-content:space-between; color:#8b949e;
  font-size:.72rem; margin-top:8px; }
.sc-times strong { color:#e6edf3; }

.sc-bar    { height:8px; background:#21262d; border-radius:4px; margin-top:4px;
  position:relative; }
.sc-fill   { height:100%; border-radius:4px; transition:width .4s; }
.sc-fill.ok   { background:#00e676; }
.sc-fill.warn { background:#ffea00; }
.sc-fill.over { background:#ff1744; }
.sc-mark   { position:absolute; top:-2px; bottom:-2px; width:2px;
  background:#58a6ff; border-radius:1px; }

.sc-foot { display:flex; justify-content:space-between; align-items:center;
  margin-top:8px; }
.sc-success { font-size:.72rem; }
.sc-success.ok   { color:#00e676; }
.sc-success.warn { color:#ffea00; }
.sc-success.bad  { color:#ff1744; }
.sc-runs  { color:#8b949e; font-size:.68rem; }
.sc-db    { background:#1f6feb22; border:1px solid #1f6feb66;
  color:#58a6ff; font-size:.68rem; padding:2px 6px; border-radius:4px; }
.sc-anom  { display:inline-block; margin-left:4px; color:#ff1744;
  font-size:.7rem; }
.sc-empty { color:#8b949e; padding:12px; }
"""

_UNUSED_OLD_STEPS_JS = r"""
// gapit-htmlgraphics-panel (codeData):
// - 'data' eh injetado pelo plugin (panelData do Grafana)
// - precisamos RETORNAR um objeto cujas chaves viram ${chave} no HTML
try {
  const series = (data && data.series && data.series[0]) || null;
  if (!series || !series.fields || !series.fields.length) {
    return { cards: '<div class="sc-empty">Sem dados.</div>' };
  }
  const F = {};
  series.fields.forEach(function (f) {
    F[f.name] = (f.values && f.values.toArray) ? f.values.toArray() : (f.values || []);
  });
  const n = (F['#'] || []).length;
  if (!n) return { cards: '<div class="sc-empty">Sem linhas.</div>' };

  const escH = function (s) {
    return String(s == null ? '' : s)
      .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
      .replace(/"/g,'&quot;');
  };
  const fmtSecs = function (s) {
    s = +s || 0;
    const h = Math.floor(s/3600), m = Math.floor((s%3600)/60), ss = s%60;
    return (h<10?'0':'')+h+':'+(m<10?'0':'')+m+':'+(ss<10?'0':'')+ss;
  };

  let html = '';
  for (let i = 0; i < n; i++) {
    const last  = +F['Ult. Dur.'][i] || 0;
    const avg   = +F['Media 90d'][i] || 0;
    const ratio = avg > 0 ? last / avg : 0;
    const pctDiff = avg > 0 ? Math.round((ratio - 1) * 100) : null;

    const stat = F['Status'][i] || '-';
    const semColor =
      stat === 'Falhou'    ? 'red'    :
      stat === 'Cancelado' ? 'gray'   :
      stat === 'Retry'     ? 'yellow' :
      stat === 'Sucesso'   ? 'green'  : 'gray';

    const ratioCls = !avg ? 'none'
      : ratio > 1.5  ? 'over'
      : pctDiff > 10 ? 'warn' : 'ok';
    const ratioLbl = pctDiff === null ? '—'
      : pctDiff > 0 ? '+'+pctDiff+'%'
      : pctDiff < 0 ? pctDiff+'%' : '=avg';

    const trackMax = Math.max(last, avg * 1.6, 1);
    const fillW   = Math.min(100, last / trackMax * 100);
    const avgMark = avg > 0 ? Math.min(100, avg / trackMax * 100) : null;
    const fillCls = ratio > 1.5 ? 'over' : pctDiff > 10 ? 'warn' : 'ok';

    const sr = +F['Sucesso%'][i] || 0;
    const srCls = sr >= 95 ? 'ok' : sr >= 80 ? 'warn' : 'bad';

    const statCls = stat === 'Sucesso' ? 'ok'
      : stat === 'Falhou' ? 'bad'
      : stat === 'Retry'  ? 'warn' : 'idle';

    const anom = F['Anomalia'] ? F['Anomalia'][i] : '';
    const anomTag = (anom === 'Critica' || anom === 'Lenta')
      ? ' <span class="sc-anom" title="'+escH(anom)+'">⚠</span>' : '';

    const stepName = F['Step'] ? F['Step'][i] : '';
    const dbName   = F['Database'] ? F['Database'][i] : '';
    const runs     = F['Runs 90d'] ? F['Runs 90d'][i] : 0;

    html +=
      '<div class="sc-card '+semColor+'">' +
        '<div class="sc-accent '+semColor+'"></div>' +
        '<div class="sc-head">' +
          '<div style="min-width:0">' +
            '<div class="sc-id">#'+escH(F['#'][i])+anomTag+'</div>' +
            '<div class="sc-name" title="'+escH(stepName)+'">'+escH(stepName)+'</div>' +
            '<span class="sc-status '+statCls+'">'+escH(stat)+'</span>' +
          '</div>' +
          '<div style="text-align:right;flex-shrink:0">' +
            '<div class="sc-ratio '+ratioCls+'">'+ratioLbl+'</div>' +
            '<div class="sc-ratiolbl">vs media</div>' +
          '</div>' +
        '</div>' +
        '<div class="sc-times">' +
          '<span>⏱ <strong>'+fmtSecs(last)+'</strong></span>' +
          '<span>avg '+fmtSecs(avg)+'</span>' +
        '</div>' +
        '<div class="sc-bar">' +
          '<div class="sc-fill '+fillCls+'" style="width:'+fillW+'%"></div>' +
          (avgMark !== null ? '<div class="sc-mark" style="left:'+avgMark+'%"></div>' : '') +
        '</div>' +
        '<div class="sc-foot">' +
          '<span class="sc-success '+srCls+'">'+sr.toFixed(1)+'% ok &middot; '+
            '<span class="sc-runs">'+escH(runs)+'x</span></span>' +
          '<span class="sc-db">'+escH(dbName)+'</span>' +
        '</div>' +
      '</div>';
  }
  return { cards: html };
} catch (e) {
  return { cards: '<div class="sc-empty">Erro: '+(e && e.message ? e.message : e)+'</div>' };
}
"""

JOBS_OV = [
    _color_ov("Status", {
        "Sucesso": {"color": "green", "index": 0},
        "Falhou":  {"color": "red",   "index": 1},
        "Rodando": {"color": "blue",  "index": 2},
    }),
    _hide_ov("status_code"),
]

LOCKS_OV = [{
    "matcher": {"id": "byName", "options": "Bloq. por"},
    "properties": [
        {"id": "thresholds", "value": {
            "mode": "absolute",
            "steps": [{"color": "green", "value": None}, {"color": "red", "value": 1}],
        }},
        {"id": "custom.displayMode", "value": "color-background"},
    ],
}]

SEM_OV = [
    _color_ov("Ultimo Status", {
        "Sucesso": {"color": "green", "index": 0},
        "Falhou":  {"color": "red",   "index": 1},
    }),
]

FAIL_OV = [
    _color_ov("Possivel Causa", {
        "Semaforo / Trava":      {"color": "orange", "index": 0},
        "Deadlock":              {"color": "red",    "index": 1},
        "Timeout":               {"color": "yellow", "index": 2},
        "Autenticacao / Permissao": {"color": "purple", "index": 3},
        "Conectividade":         {"color": "red",    "index": 4},
        "Envio de Arquivos":     {"color": "orange", "index": 5},
        "Integracao CRM":        {"color": "blue",   "index": 6},
        "Camada ODS":            {"color": "blue",   "index": 7},
        "Investigar mensagem":   {"color": "gray",   "index": 8},
    }),
]

# ═══════════════════════════════════════════════════════════════════════════════
# Dashboard builder  (recebe o UID do datasource existente no Grafana)
# ═══════════════════════════════════════════════════════════════════════════════

def build_dashboard(ds_uid: str) -> dict:
    global _pid
    _pid = 0
    panels = []
    y = 0

    # Row 1 — Visao geral
    panels += [_row("Visao Geral do Job", y)]; y += 1
    panels += [
        stat(ds_uid, "Status do Job",        Q["job_status"],    x=0,  y=y, w=5, h=4,
             mappings=STATUS_MAPS),
        stat(ds_uid, "Step em Execucao",     Q["current_step"],  x=5,  y=y, w=11, h=4,
             thresholds={"mode": "absolute", "steps": [{"color": "blue", "value": None}]}),
        stat(ds_uid, "Duracao - Ultima Run", Q["last_duration"], x=16, y=y, w=4, h=4,
             unit="s", thresholds=DUR_THRESHOLDS),
        stat(ds_uid, "Taxa de Sucesso 90d",  Q["success_rate"],  x=20, y=y, w=4, h=4,
             unit="percent", thresholds=PCT_THRESHOLDS),
    ]; y += 4

    # Row 2 — Historico do job
    panels += [_row("Historico de Execucoes", y)]; y += 1
    panels += [timeseries(ds_uid, "Duracao do Job ao Longo do Tempo",
                          Q["job_history"], x=0, y=y, w=24, h=8)]; y += 8

    # Row 3a — Steps em Cards (HTML Graphics, estilo Flask)
    panels += [_row("Steps - Cards visuais (estilo Flask)", y)]; y += 1
    panels += [htmlgraphics(ds_uid, "Steps - Ultima Execucao vs Media (cards)",
                            Q["steps_overview"],
                            html=STEPS_HTML, css=STEPS_CSS, js=STEPS_JS,
                            x=0, y=y, w=24, h=14)]; y += 14

    # Row 3b — Tabela detalhada (gauges) — opcional, mantida como fallback
    panels += [_row("Steps - Tabela detalhada (gauges)", y)]; y += 1
    p = table(ds_uid, "Steps - Cards com Duracao, % vs Media e Sucesso",
              Q["steps_overview"], x=0, y=y, w=24, h=12,
              overrides=STEPS_OV)
    p["options"]["cellHeight"] = "md"
    panels += [p]; y += 12

    # Row 4 — Duracao por step (time series)
    panels += [_row("Historico de Duracao por Step", y)]; y += 1
    panels += [timeseries(ds_uid, "Duracao por Step ao Longo do Tempo",
                          Q["steps_ts"], x=0, y=y, w=24, h=9)]; y += 9

    # Row 5 — Locks
    panels += [_row("Locks e Bloqueios - Tempo Real", y)]; y += 1
    panels += [table(ds_uid, "Sessoes Bloqueadas / Ativas",
                     Q["locks"], x=0, y=y, w=24, h=8,
                     overrides=LOCKS_OV)]; y += 8

    # Row 6 — Jobs relacionados + semaforos
    panels += [_row("Jobs Relacionados e Semaforos", y)]; y += 1
    panels += [
        table(ds_uid, "Jobs Relacionados", Q["related_jobs"],
              x=0, y=y, w=15, h=9, overrides=JOBS_OV),
        table(ds_uid, "Steps Semaforo / Trava", Q["semaphore_steps"],
              x=15, y=y, w=9, h=9, overrides=SEM_OV),
    ]; y += 9

    # Row 7 — Histórico de falhas
    panels += [_row("Historico de Falhas – Ultimos 15 Dias", y)]; y += 1
    panels += [
        stat(ds_uid, "Falhas nos Ultimos 15 Dias", Q["failure_count"],
             x=0, y=y, w=4, h=4,
             thresholds={
                 "mode": "absolute",
                 "steps": [
                     {"color": "green",  "value": None},
                     {"color": "yellow", "value": 1},
                     {"color": "red",    "value": 4},
                 ],
             }),
        table(ds_uid, "Detalhamento das Falhas", Q["failure_history"],
              x=4, y=y, w=20, h=10, overrides=FAIL_OV),
    ]

    return {
        "id":    None,
        "uid":   DASHBOARD_UID,
        "title": DASHBOARD_TITLE,
        "schemaVersion": 39,
        "version": 1,
        "refresh": "30s",
        "time":   {"from": "now-7d", "to": "now"},
        "timepicker": {},
        "timezone": "browser",
        "tags": ["motor", "abastecimento", "sql-server"],
        "editable": True,
        "graphTooltip": 1,
        "templating": {
            "list": [{
                "name":        "job_name",
                "type":        "textbox",
                "label":       "Job",
                "description": "Nome exato do SQL Server Agent Job",
                "current":     {"value": JOB_NAME, "text": JOB_NAME},
                "query":       JOB_NAME,
                "hide":        0,
                "skipUrlSync": False,
            }]
        },
        "annotations": {
            "list": [{
                "builtIn":    1,
                "datasource": {"type": "grafana", "uid": "-- Grafana --"},
                "enable":     True,
                "hide":       True,
                "iconColor":  "rgba(0,211,255,1)",
                "name":       "Annotations & Alerts",
                "type":       "dashboard",
            }]
        },
        "panels": panels,
        "links":  [],
    }

# ═══════════════════════════════════════════════════════════════════════════════
# Grafana API helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _session(token: str) -> requests.Session:
    s = requests.Session()
    s.verify = False  # certificado autoassinado
    s.headers.update({
        "Authorization": f"Bearer {token}",
        "Content-Type":  "application/json",
        "Accept":        "application/json",
    })
    return s


def list_datasources(sess: requests.Session, url: str):
    """Imprime todos os datasources cadastrados no Grafana."""
    r = sess.get(f"{url}/api/datasources")
    r.raise_for_status()
    ds_list = r.json()
    print(f"\n{'ID':<6} {'UID':<30} {'Tipo':<15} {'Nome'}")
    print("-" * 75)
    for d in sorted(ds_list, key=lambda x: x.get("name", "")):
        print(f"{d.get('id',''):<6} {d.get('uid',''):<30} {d.get('type',''):<15} {d.get('name','')}")
    print(f"\nTotal: {len(ds_list)} datasource(s)\n")


def resolve_ds_uid(sess: requests.Session, url: str, ds_name: str) -> str:
    """Retorna o UID do datasource a partir do nome configurado no Grafana."""
    r = sess.get(f"{url}/api/datasources/name/{requests.utils.quote(ds_name)}")
    if r.status_code == 404:
        print(f"ERRO: datasource '{ds_name}' nao encontrado no Grafana.")
        print("Execute com --list-ds para ver os datasources disponiveis.")
        sys.exit(1)
    r.raise_for_status()
    uid = r.json()["uid"]
    print(f"  Datasource : '{ds_name}'  (uid: {uid})")
    return uid


def push_dashboard(sess: requests.Session, url: str, ds_uid: str):
    payload = {
        "dashboard": build_dashboard(ds_uid),
        "folderId":  FOLDER_ID,
        "overwrite": True,
        "message":   "Provisionado por grafana_provision.py",
    }
    r = sess.post(f"{url}/api/dashboards/db", data=json.dumps(payload))
    if r.status_code == 200:
        resp = r.json()
        print(f"  Dashboard  : {url}{resp.get('url', '')}")
    else:
        print(f"  ERRO dashboard [{r.status_code}]: {r.text}")
        sys.exit(1)


def export_json(ds_uid: str, path: str = "grafana_dashboard.json"):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(build_dashboard(ds_uid), f, ensure_ascii=False, indent=2)
    print(f"  Exportado  -> {path}")
    print("  Importe em : Dashboards > New > Import > Upload JSON file")

# ═══════════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--url",      default=GRAFANA_URL,   help="URL do Grafana (default: %(default)s)")
    ap.add_argument("--token",    default=GRAFANA_TOKEN, help="Service Account Token (glsa_xxx)")
    ap.add_argument("--ds",       default=DS_NAME,       help="Nome do datasource no Grafana (default: %(default)s)")
    ap.add_argument("--list-ds",  action="store_true",   help="Lista os datasources disponiveis e encerra")
    ap.add_argument("--export-only", action="store_true",
                    help="Gera grafana_dashboard.json sem precisar do Grafana online")
    ap.add_argument("--ds-uid",   default=None,
                    help="UID do datasource (ignora --ds, use se o nome tiver caracteres especiais)")
    args = ap.parse_args()

    print(f"\n{'='*55}")
    print(f"  Motor de Abastecimento - Grafana 12 Provisioning")
    print(f"{'='*55}")
    print(f"  Grafana    : {args.url}")
    print(f"  Job        : {JOB_NAME}\n")

    if args.export_only:
        uid = args.ds_uid or "SUBSTITUA_PELO_UID_DO_DATASOURCE"
        print(f"  Datasource UID usado no JSON: {uid}")
        print("  (edite DS_NAME no arquivo ou passe --ds-uid para o UID correto)\n")
        export_json(uid)
        return

    if args.token == "SEU_SERVICE_ACCOUNT_TOKEN":
        print("ERRO: configure --token glsa_xxx")
        print("  Grafana > Administration > Service Accounts > Add token")
        sys.exit(1)

    sess = _session(args.token)

    if args.list_ds:
        list_datasources(sess, args.url)
        return

    # Resolve o UID do datasource existente no Grafana
    ds_uid = args.ds_uid or resolve_ds_uid(sess, args.url, args.ds)

    push_dashboard(sess, args.url, ds_uid)
    print("\nPronto!")


if __name__ == "__main__":
    main()
