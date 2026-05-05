import pyodbc
from datetime import datetime, date as date_type
from typing import Optional, List, Dict, Any

SERVER = 'FORTBRS-DWBI03'
MAIN_JOB = '0_MAIN_FORTBRAS_MOTOR_ABASTECIMENTO'

RELATED_JOB_KEYWORDS = ['%MOTOR%', '%ABASTECIMENTO%', '%ODS%', '%PHOENIX%', '%CRM%', '%BUQUET%']

STATUS_MAP = {
    0: ('Falhou',       'danger'),
    1: ('Sucesso',      'success'),
    2: ('Retry',        'warning'),
    3: ('Cancelado',    'secondary'),
    4: ('Em Execução',  'primary'),
}


def _connect(database: str = 'msdb') -> pyodbc.Connection:
    for driver in [
        'ODBC Driver 18 for SQL Server',
        'ODBC Driver 17 for SQL Server',
        'SQL Server Native Client 11.0',
        'SQL Server',
    ]:
        try:
            return pyodbc.connect(
                f'Driver={{{driver}}};'
                f'Server={SERVER};'
                f'Database={database};'
                f'Trusted_Connection=yes;'
                f'Connect Timeout=20;'
                f'TrustServerCertificate=yes;'
            )
        except pyodbc.Error:
            continue
    raise ConnectionError(f'Sem conexão com {SERVER}. Verifique o driver ODBC e permissões AD.')


def _secs(duration: int) -> int:
    if not duration:
        return 0
    return (duration // 10000) * 3600 + ((duration % 10000) // 100) * 60 + (duration % 100)


def _hms(s: int) -> str:
    if not s or s < 0:
        return '00:00:00'
    return f'{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}'


def _to_dt(run_date, run_time) -> Optional[datetime]:
    try:
        ds = str(run_date)
        ts = str(run_time).zfill(6)
        return datetime(int(ds[:4]), int(ds[4:6]), int(ds[6:8]),
                        int(ts[:2]), int(ts[2:4]), int(ts[4:6]))
    except Exception:
        return None


def _status(code) -> Dict:
    label, css = STATUS_MAP.get(code, ('Desconhecido', 'dark'))
    return {'label': label, 'css': css, 'code': code}


def _date_to_int(date_str: str) -> Optional[int]:
    """'YYYY-MM-DD' → YYYYMMDD int usado no sysjobhistory"""
    try:
        return int(datetime.strptime(date_str, '%Y-%m-%d').strftime('%Y%m%d'))
    except Exception:
        return None


def _is_today(date_str: str) -> bool:
    return date_str == date_type.today().strftime('%Y-%m-%d')


def _iqr_bounds(values: List[float]):
    if len(values) < 4:
        return None, None
    sv = sorted(values)
    n = len(sv)
    q1 = sv[n // 4]
    q3 = sv[(3 * n) // 4]
    iqr = q3 - q1
    return max(0.0, q1 - 1.5 * iqr), q3 + 1.5 * iqr


# ─────────────────────────────────────────────────────────────────────────────
# 0. ÚLTIMA DATA DE EXECUÇÃO (para default do date picker)
# ─────────────────────────────────────────────────────────────────────────────

def get_last_execution_date() -> str:
    sql = """
    SELECT TOP 1 run_date
    FROM msdb.dbo.sysjobhistory
    WHERE job_id = (SELECT job_id FROM msdb.dbo.sysjobs WHERE name = ?)
      AND step_id = 0
      AND run_status IN (0, 1)
    ORDER BY run_date DESC, run_time DESC
    """
    try:
        with _connect() as conn:
            row = conn.execute(sql, MAIN_JOB).fetchone()
            if row and row.run_date:
                ds = str(row.run_date)
                return f'{ds[:4]}-{ds[4:6]}-{ds[6:8]}'
    except Exception:
        pass
    return date_type.today().strftime('%Y-%m-%d')


# ─────────────────────────────────────────────────────────────────────────────
# 1. STATUS ATUAL DO JOB PRINCIPAL
# ─────────────────────────────────────────────────────────────────────────────

def get_job_status(target_date: str = None) -> Dict:
    # ── modo histórico: data específica no passado ────────────────────────────
    date_int = _date_to_int(target_date) if target_date else None
    is_historical = bool(target_date and not _is_today(target_date))

    if is_historical and date_int:
        # JOIN com sysjobs para sempre obter 'enabled', mesmo sem execução na data
        hist_sql = """
        SELECT TOP 1
            j.enabled,
            h.run_status, h.run_date, h.run_time, h.run_duration, h.message,
            (SELECT COUNT(*) FROM msdb.dbo.sysjobhistory
             WHERE job_id = j.job_id AND step_id > 0 AND run_date = ?) AS steps_ran
        FROM msdb.dbo.sysjobs j
        LEFT JOIN msdb.dbo.sysjobhistory h
               ON h.job_id = j.job_id AND h.step_id = 0 AND h.run_date = ?
        WHERE j.name = ?
        ORDER BY h.run_time DESC
        """
        try:
            with _connect() as conn:
                row = conn.execute(hist_sql, date_int, date_int, MAIN_JOB).fetchone()
                if not row:
                    return {'error': f'Job "{MAIN_JOB}" não encontrado no servidor.'}

                enabled = bool(row.enabled)
                if row.run_status is None:
                    return {
                        'name':                   MAIN_JOB,
                        'enabled':                enabled,
                        'is_historical':          True,
                        'target_date':            target_date,
                        'is_running':             False,
                        'no_execution':           True,
                        'semaforo':               'gray',
                        'last_run_status':        _status(None),
                        'last_run_datetime':      None,
                        'last_run_duration_hms':  '—',
                        'last_run_duration_secs': 0,
                        'last_message':           'Sem execução registrada nesta data.',
                    }

                last_dt   = _to_dt(row.run_date, row.run_time)
                last_secs = _secs(row.run_duration)
                st        = _status(row.run_status)
                sem = ('green' if row.run_status == 1
                       else 'red' if row.run_status == 0 else 'gray')
                return {
                    'name':                   MAIN_JOB,
                    'enabled':                enabled,
                    'is_historical':          True,
                    'target_date':            target_date,
                    'is_running':             False,
                    'semaforo':               sem,
                    'steps_ran':              row.steps_ran or 0,
                    'last_run_datetime':      last_dt.isoformat() if last_dt else None,
                    'last_run_status':        st,
                    'last_run_duration_hms':  _hms(last_secs),
                    'last_run_duration_secs': last_secs,
                    'last_message':           row.message or '',
                }
        except Exception as e:
            return {'error': str(e)}

    # ── modo ao vivo (hoje ou sem data) ───────────────────────────────────────
    live_sql = """
    SELECT
        j.name,
        j.enabled,
        CASE
            WHEN ja.start_execution_date IS NOT NULL
             AND ja.stop_execution_date  IS NULL THEN 1 ELSE 0
        END AS is_running,
        ja.start_execution_date,
        ja.stop_execution_date,
        ja.last_executed_step_id,
        ISNULL(js.step_name, '') AS current_step_name,
        lh.run_status,
        lh.run_date,
        lh.run_time,
        lh.run_duration,
        lh.message
    FROM msdb.dbo.sysjobs j
    LEFT JOIN msdb.dbo.sysjobactivity ja
           ON j.job_id = ja.job_id
          AND ja.session_id = (
                SELECT MAX(session_id) FROM msdb.dbo.syssessions
                WHERE agent_start_date IS NOT NULL
              )
    LEFT JOIN msdb.dbo.sysjobsteps js
           ON j.job_id = js.job_id
          AND js.step_id = ja.last_executed_step_id
    OUTER APPLY (
        SELECT TOP 1 run_status, run_date, run_time, run_duration, message
        FROM msdb.dbo.sysjobhistory
        WHERE job_id = j.job_id AND step_id = 0
        ORDER BY run_date DESC, run_time DESC
    ) lh
    WHERE j.name = ?
    """
    try:
        with _connect() as conn:
            row = conn.execute(live_sql, MAIN_JOB).fetchone()
            if not row:
                return {'error': f'Job "{MAIN_JOB}" não encontrado no servidor.'}

            is_running = bool(row.is_running)
            last_dt    = _to_dt(row.run_date, row.run_time)
            last_secs  = _secs(row.run_duration)

            elapsed_secs = None
            if is_running and row.start_execution_date:
                elapsed_secs = int(
                    (datetime.now() - row.start_execution_date).total_seconds()
                )

            st = _status(row.run_status)
            if is_running:
                semaforo = 'blue'
            elif row.run_status == 1:
                semaforo = 'green'
            elif row.run_status == 0:
                semaforo = 'red'
            else:
                semaforo = 'gray'

            return {
                'name':                   row.name,
                'enabled':                bool(row.enabled),
                'is_running':             is_running,
                'is_historical':          False,
                'target_date':            target_date,
                'semaforo':               semaforo,
                'current_step_id':        row.last_executed_step_id,
                'current_step_name':      row.current_step_name,
                'start_execution_date':   (
                    row.start_execution_date.isoformat()
                    if row.start_execution_date else None
                ),
                'elapsed_secs':           elapsed_secs,
                'elapsed_hms':            _hms(elapsed_secs) if elapsed_secs else None,
                'last_run_datetime':      last_dt.isoformat() if last_dt else None,
                'last_run_status':        st,
                'last_run_duration_hms':  _hms(last_secs),
                'last_run_duration_secs': last_secs,
                'last_message':           row.message or '',
            }
    except Exception as e:
        return {'error': str(e)}


# ─────────────────────────────────────────────────────────────────────────────
# 2. STEPS COM HISTÓRICO E DETECÇÃO DE ANOMALIAS
# ─────────────────────────────────────────────────────────────────────────────

def get_steps_analysis(target_date: str = None) -> List[Dict]:
    steps_sql = """
    SELECT step_id, step_name, database_name, subsystem, command,
           on_success_action, on_fail_action
    FROM msdb.dbo.sysjobsteps
    WHERE job_id = (SELECT job_id FROM msdb.dbo.sysjobs WHERE name = ?)
    ORDER BY step_id
    """

    history_sql = """
    SELECT step_id, step_name, run_status, run_date, run_time,
           run_duration, message, retries_attempted
    FROM msdb.dbo.sysjobhistory
    WHERE job_id = (SELECT job_id FROM msdb.dbo.sysjobs WHERE name = ?)
      AND step_id > 0
      AND run_date >= CONVERT(int,
            CONVERT(varchar(8), DATEADD(day, -90, GETDATE()), 112))
    ORDER BY step_id, run_date DESC, run_time DESC
    """

    ACTION_MAP = {1: 'Quit sucesso', 2: 'Quit falha', 3: 'Próximo step',
                  4: 'Ir ao step...', 5: 'Quit sucesso'}

    try:
        with _connect() as conn:
            steps: Dict[int, Dict] = {}
            for r in conn.execute(steps_sql, MAIN_JOB):
                steps[r.step_id] = {
                    'step_id':        r.step_id,
                    'step_name':      r.step_name,
                    'database_name':  r.database_name or 'msdb',
                    'command':        (r.command or '')[:2000],
                    'on_success':     ACTION_MAP.get(r.on_success_action, ''),
                    'on_fail':        ACTION_MAP.get(r.on_fail_action, ''),
                    'history':        [],
                    'run_count':      0,
                    'success_rate':   0,
                    'last_status':    _status(None),
                    'last_run_datetime': None,
                    'last_duration_hms': '—',
                    'last_duration_secs': 0,
                    'avg_duration_hms': '—',
                    'avg_duration_secs': 0,
                    'min_duration_secs': 0,
                    'max_duration_secs': 0,
                    'upper_fence_secs': 0,
                    'lower_fence_secs': 0,
                    'is_anomaly':     False,
                    'anomaly_reason': '',
                    'semaforo':       'gray',
                    'last_message':   '',
                }

            # Organiza todo o histórico por step
            hist: Dict[int, list] = {}
            for r in conn.execute(history_sql, MAIN_JOB):
                hist.setdefault(r.step_id, []).append(r)

            # Determina quais linhas usar como "última execução" (display)
            date_int = _date_to_int(target_date) if target_date else None
            is_hist  = bool(target_date and not _is_today(target_date))

            for sid in steps:
                all_rows     = hist.get(sid, [])
                s            = steps[sid]
                s['run_count'] = len(all_rows)

                # Linhas a exibir como "última execução":
                # - modo histórico → apenas os runs do dia selecionado
                # - modo ao vivo   → os rows[0] (mais recente nos 90 dias)
                if is_hist and date_int is not None:
                    display_rows = [r for r in all_rows if r.run_date == date_int]
                else:
                    display_rows = all_rows

                if not display_rows:
                    # Step não executou na data selecionada
                    s['not_executed']    = True
                    s['semaforo']        = 'gray'
                    s['last_status']     = {'label': 'Não executou', 'css': 'secondary', 'code': None}
                    # ainda calcula estatísticas usando all_rows
                else:
                    s['not_executed'] = False
                    latest = display_rows[0]
                    s['last_status']        = _status(latest.run_status)
                    s['last_message']       = (latest.message or '')[:300]
                    last_dt                 = _to_dt(latest.run_date, latest.run_time)
                    s['last_run_datetime']  = last_dt.isoformat() if last_dt else None
                    last_secs               = _secs(latest.run_duration)
                    s['last_duration_secs'] = last_secs
                    s['last_duration_hms']  = _hms(last_secs)

                # Taxa de sucesso sempre sobre todos os 90 dias
                success_rows = [r for r in all_rows if r.run_status == 1]
                s['success_rate'] = (
                    round(len(success_rows) / len(all_rows) * 100, 1) if all_rows else 0
                )

                durations = [
                    _secs(r.run_duration)
                    for r in success_rows
                    if r.run_duration and _secs(r.run_duration) > 0
                ]

                if durations:
                    avg = sum(durations) / len(durations)
                    s['avg_duration_secs'] = int(avg)
                    s['avg_duration_hms']  = _hms(int(avg))
                    s['min_duration_secs'] = min(durations)
                    s['max_duration_secs'] = max(durations)

                    lo, hi = _iqr_bounds(durations)
                    if hi is not None:
                        s['upper_fence_secs'] = int(hi)
                        s['lower_fence_secs'] = int(lo)
                        last_secs = s.get('last_duration_secs', 0)
                        latest_st = (display_rows[0].run_status
                                     if display_rows else None)
                        if last_secs > 0 and latest_st == 1:
                            if last_secs > hi:
                                s['is_anomaly'] = True
                                s['anomaly_reason'] = (
                                    f'Duração {_hms(last_secs)} acima do limite '
                                    f'({_hms(int(hi))})'
                                )
                            elif lo > 0 and last_secs < lo:
                                s['is_anomaly'] = True
                                s['anomaly_reason'] = (
                                    f'Duração {_hms(last_secs)} abaixo do limite '
                                    f'({_hms(int(lo))})'
                                )

                if not s['not_executed']:
                    code = display_rows[0].run_status
                    if code == 1 and s['is_anomaly']:
                        s['semaforo'] = 'yellow'
                    elif code == 1:
                        s['semaforo'] = 'green'
                    elif code == 0:
                        s['semaforo'] = 'red'
                    elif code == 4:
                        s['semaforo'] = 'blue'
                    else:
                        s['semaforo'] = 'gray'

            return sorted(steps.values(), key=lambda x: x['step_id'])

    except Exception as e:
        return [{'error': str(e)}]


# ─────────────────────────────────────────────────────────────────────────────
# 3. LOCKS E BLOQUEIOS ATIVOS
# ─────────────────────────────────────────────────────────────────────────────

def get_active_locks() -> List[Dict]:
    sql = """
    SELECT
        r.session_id,
        r.blocking_session_id,
        r.status,
        r.wait_type,
        r.wait_time              / 1000  AS wait_secs,
        r.total_elapsed_time     / 1000  AS elapsed_secs,
        r.cpu_time,
        r.reads,
        r.writes,
        DB_NAME(r.database_id)          AS db_name,
        s.login_name,
        s.host_name,
        LEFT(s.program_name, 60)        AS program_name,
        ISNULL(LEFT(t.text, 500), '')   AS sql_text
    FROM sys.dm_exec_requests r
    JOIN  sys.dm_exec_sessions s  ON r.session_id  = s.session_id
    OUTER APPLY sys.dm_exec_sql_text(r.sql_handle) t
    WHERE r.session_id > 50
      AND r.session_id <> @@SPID
    ORDER BY
        CASE WHEN ISNULL(r.blocking_session_id,0) > 0 THEN 0 ELSE 1 END,
        r.wait_time DESC
    """
    try:
        with _connect('master') as conn:
            return [
                {
                    'session_id':         r.session_id,
                    'blocking_session_id': r.blocking_session_id or 0,
                    'is_blocked':          bool(r.blocking_session_id
                                                and r.blocking_session_id > 0),
                    'status':             r.status or '',
                    'wait_type':          r.wait_type or '—',
                    'wait_secs':          r.wait_secs or 0,
                    'elapsed_secs':       r.elapsed_secs or 0,
                    'db_name':            r.db_name or '',
                    'login_name':         r.login_name or '',
                    'host_name':          r.host_name or '',
                    'program_name':       r.program_name or '',
                    'sql_text':           (r.sql_text or '').strip(),
                }
                for r in conn.execute(sql).fetchall()
            ]
    except Exception as e:
        return [{'error': str(e)}]


# ─────────────────────────────────────────────────────────────────────────────
# 4. JOBS RELACIONADOS
# ─────────────────────────────────────────────────────────────────────────────

def get_related_jobs(target_date: str = None) -> List[Dict]:
    where_clause = ' OR '.join(["j.name LIKE ?"] * len(RELATED_JOB_KEYWORDS))
    date_int     = _date_to_int(target_date) if target_date else None
    is_hist      = bool(target_date and not _is_today(target_date))

    # Cláusula OUTER APPLY muda conforme modo (ao vivo vs histórico)
    if is_hist and date_int:
        apply_clause = f"""
    OUTER APPLY (
        SELECT TOP 1 run_status, run_date, run_time, run_duration
        FROM msdb.dbo.sysjobhistory
        WHERE job_id = j.job_id AND step_id = 0
          AND run_date = {date_int}
        ORDER BY run_time DESC
    ) lh"""
    else:
        apply_clause = """
    OUTER APPLY (
        SELECT TOP 1 run_status, run_date, run_time, run_duration
        FROM msdb.dbo.sysjobhistory
        WHERE job_id = j.job_id AND step_id = 0
        ORDER BY run_date DESC, run_time DESC
    ) lh"""

    sql = f"""
    SELECT
        j.name,
        j.enabled,
        CASE
            WHEN ja.start_execution_date IS NOT NULL
             AND ja.stop_execution_date  IS NULL THEN 1 ELSE 0
        END AS is_running,
        ja.last_executed_step_id,
        lh.run_status,
        lh.run_date,
        lh.run_time,
        lh.run_duration
    FROM msdb.dbo.sysjobs j
    LEFT JOIN msdb.dbo.sysjobactivity ja
           ON j.job_id = ja.job_id
          AND ja.session_id = (
                SELECT MAX(session_id) FROM msdb.dbo.syssessions
                WHERE agent_start_date IS NOT NULL
              )
    {apply_clause}
    WHERE {where_clause}
    ORDER BY j.name
    """
    try:
        with _connect() as conn:
            rows = conn.execute(sql, *RELATED_JOB_KEYWORDS).fetchall()
            result = []
            for r in rows:
                last_dt = _to_dt(r.run_date, r.run_time)
                last_secs = _secs(r.run_duration)
                is_running = bool(r.is_running)
                st = _status(r.run_status)

                if is_running:
                    sem = 'blue'
                elif r.run_status == 1:
                    sem = 'green'
                elif r.run_status == 0:
                    sem = 'red'
                else:
                    sem = 'gray'

                result.append({
                    'name':               r.name,
                    'enabled':            bool(r.enabled),
                    'is_running':         is_running,
                    'semaforo':           sem,
                    'last_step_id':       r.last_executed_step_id,
                    'last_status':        st,
                    'last_run_datetime':  last_dt.isoformat() if last_dt else None,
                    'last_duration_hms':  _hms(last_secs),
                })
            return result
    except Exception as e:
        return [{'error': str(e)}]


# ─────────────────────────────────────────────────────────────────────────────
# 5. HISTÓRICO DE FALHAS (últimos 15 dias) + CLASSIFICAÇÃO DE CAUSA
# ─────────────────────────────────────────────────────────────────────────────

def _classify_cause(step_name: str, message: str) -> str:
    n = (step_name or '').upper()
    m = (message  or '').upper()
    if any(k in n for k in ('SEMAFORO', 'TRAVA', 'WAIT', 'LOCK')):
        return 'Semáforo / Trava — recurso não liberado por outra execução'
    if 'DEADLOCK' in m:
        return 'Deadlock — conflito entre transações concorrentes'
    if any(k in m for k in ('TIMEOUT', 'TIMED OUT', 'QUERY TIMEOUT')):
        return 'Timeout — query ou conexão excedeu o limite de tempo'
    if any(k in m for k in ('LOGIN FAILED', 'AUTHENTICATION', 'PASSWORD', 'LOGON')):
        return 'Autenticação — falha de login ou permissão insuficiente'
    if any(k in m for k in ('NETWORK', 'TRANSPORT', 'CONNECTION', 'TCP', 'NAMED PIPE')):
        return 'Conectividade — falha de rede ou servidor indisponível'
    if any(k in m for k in ('DISK', 'FULL', 'NO SPACE', 'LOG IS FULL', 'TEMPDB')):
        return 'Espaço em disco — banco, log ou tempdb cheio'
    if any(k in n for k in ('BUQUET', 'ENVIA', 'ARQUIVO', 'FILE', 'FTP', 'SFTP')):
        return 'Envio de arquivos — falha na transferência para storage externo'
    if 'CRM' in n:
        return 'Integração CRM — falha na extração ou carga de dados CRM'
    if 'ODS' in n:
        return 'Camada ODS — falha na extração ou transformação de dados'
    if 'PHOENIX' in n:
        return 'Integração Phoenix — falha na extração ou carga Phoenix'
    if any(k in n for k in ('MOTOR', 'ABASTECIMENTO')):
        return 'Núcleo do motor — erro na lógica principal de abastecimento'
    if any(k in m for k in ('PERMISSION', 'ACCESS DENIED', 'UNAUTHORIZED')):
        return 'Permissão — usuário sem acesso ao objeto ou banco'
    return 'Causa não identificada — analise a mensagem de erro do step'


def get_failure_history(date_from: str = None, date_to: str = None,
                        days: int = 90) -> List[Dict]:
    """
    date_from / date_to: 'YYYY-MM-DD'. Se ausentes, usa os últimos `days` dias.
    """
    if date_from and date_to:
        int_from = _date_to_int(date_from)
        int_to   = _date_to_int(date_to)
        date_filter = "AND h.run_date BETWEEN ? AND ?"
        params = (MAIN_JOB, int_from, int_to)
    elif date_from:
        int_from = _date_to_int(date_from)
        date_filter = "AND h.run_date >= ?"
        params = (MAIN_JOB, int_from)
    else:
        date_filter = """AND h.run_date >= CONVERT(int,
            CONVERT(varchar(8), DATEADD(day, -?, GETDATE()), 112))"""
        params = (MAIN_JOB, days)

    sql = f"""
    SELECT
        h.run_date,
        h.run_time,
        h.run_duration,
        h.run_status,
        LEFT(h.message, 600)  AS job_message,
        fs.step_id            AS failed_step_id,
        fs.step_name          AS failed_step_name,
        LEFT(fs.message, 600) AS step_message
    FROM msdb.dbo.sysjobhistory h
    OUTER APPLY (
        SELECT TOP 1 sh.step_id, sh.step_name, sh.message
        FROM msdb.dbo.sysjobhistory sh
        WHERE sh.job_id   = h.job_id
          AND sh.run_date = h.run_date
          AND sh.step_id  > 0
          AND sh.run_status IN (0, 2)
        ORDER BY sh.run_time DESC
    ) fs
    WHERE h.job_id = (SELECT job_id FROM msdb.dbo.sysjobs WHERE name = ?)
      AND h.step_id    = 0
      AND h.run_status IN (0, 2, 3)
      {date_filter}
    ORDER BY h.run_date DESC, h.run_time DESC
    """
    try:
        with _connect() as conn:
            rows = conn.execute(sql, *params).fetchall()
            result = []
            for r in rows:
                dt = _to_dt(r.run_date, r.run_time)
                secs = _secs(r.run_duration)
                step_name = r.failed_step_name or ''
                step_msg  = r.step_message   or r.job_message or ''
                result.append({
                    'datetime':        dt.isoformat() if dt else None,
                    'duration_hms':    _hms(secs),
                    'duration_secs':   secs,
                    'job_status':      _status(r.run_status),
                    'failed_step_id':  r.failed_step_id,
                    'failed_step':     step_name or '(step não identificado)',
                    'step_message':    step_msg[:400],
                    'cause':           _classify_cause(step_name, step_msg),
                })
            return result
    except Exception as e:
        return [{'error': str(e)}]


# ─────────────────────────────────────────────────────────────────────────────
# 6. ANÁLISE DE SEMÁFOROS (extrai das commands dos steps)
# ─────────────────────────────────────────────────────────────────────────────

def get_semaphore_steps() -> List[Dict]:
    """
    Retorna os steps com 'SEMAFORO' ou 'TRAVA' no nome,
    junto com o comando SQL para inspeção manual.
    """
    sql = """
    SELECT step_id, step_name, database_name, command
    FROM msdb.dbo.sysjobsteps
    WHERE job_id = (SELECT job_id FROM msdb.dbo.sysjobs WHERE name = ?)
      AND (UPPER(step_name) LIKE '%SEMAFORO%'
        OR UPPER(step_name) LIKE '%TRAVA%'
        OR UPPER(step_name) LIKE '%WAIT%'
        OR UPPER(step_name) LIKE '%LOCK%')
    ORDER BY step_id
    """
    try:
        with _connect() as conn:
            return [
                {
                    'step_id':      r.step_id,
                    'step_name':    r.step_name,
                    'database_name': r.database_name or 'msdb',
                    'command':      (r.command or '')[:3000],
                }
                for r in conn.execute(sql, MAIN_JOB).fetchall()
            ]
    except Exception as e:
        return [{'error': str(e)}]


# ─────────────────────────────────────────────────────────────────────────────
# 7. ENDPOINT ÚNICO – agrega tudo para o dashboard
# ─────────────────────────────────────────────────────────────────────────────

def get_all_data(target_date: str = None) -> Dict:
    return {
        'server':           SERVER,
        'job_name':         MAIN_JOB,
        'refreshed_at':     datetime.now().isoformat(),
        'target_date':      target_date,
        'is_historical':    bool(target_date and not _is_today(target_date)),
        'job_status':       get_job_status(target_date),
        'steps':            get_steps_analysis(target_date),
        'locks':            get_active_locks(),
        'related_jobs':     get_related_jobs(target_date),
        'semaphore_steps':  get_semaphore_steps(),
    }
