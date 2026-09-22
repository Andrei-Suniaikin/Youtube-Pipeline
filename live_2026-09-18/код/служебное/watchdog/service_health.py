"""Bounded read-only probes for the three production services and render node."""
import json
from pathlib import Path
import sqlite3
import subprocess
import time
import urllib.request


def _result(ok, summary, **data):
    return dict(ok=bool(ok), summary=summary, **data)


def _default_open(db_path):
    """Прежнее открытие: read-only URI без повторов. Под read-only sandbox WAL-база
    без -shm так не читается — поэтому сторож передаёт свой opener (open_db)."""
    return sqlite3.connect(Path(db_path).resolve().as_uri()+'?mode=ro', uri=True, timeout=5), 'read_only'


def _validate_ignore(ignore_components):
    ignore = list(ignore_components)
    if any(not isinstance(item, str) or not item for item in ignore):
        raise ValueError('ignore_components must be a list of component names')
    return ignore


def check_report(path, max_age, now, ignore_components=()):
    """ignore_components: блоки отчёта, чей провал не валит проверку (по умолчанию нет).
    Игнорируемые, но красные блоки остаются видны в data (ignored_failing) и в summary."""
    try:
        ignore = _validate_ignore(ignore_components)
        data = json.loads(Path(path).read_text())
        stamp = float(data['generated_at'])
        fresh = -60 <= now-stamp <= max_age
        components = data.get('components', {})
        if not isinstance(components,dict) or not components or any(not isinstance(v,dict) or type(v.get('ok')) is not bool for v in components.values()):
            raise ValueError('invalid report components')
        if type(data.get('ok')) is not bool or data['ok'] and not all(v['ok'] for v in components.values()):
            raise ValueError('inconsistent report summary')
        failed = [str(k) for k, v in components.items() if v['ok'] is False and k not in ignore]
        ignored_failing = [k for k in ignore if k in components and components[k]['ok'] is False]
        # Верхний ok=False, который не объясняется ни одним красным блоком (у backup так
        # пишется offsite_manifest_error) — ошибка вне блоков; ignore_components её не покрывает.
        unexplained = data['ok'] is False and not failed and not ignored_failing
        # Без ignore тождественно прежнему: fresh и data.ok is True.
        ok = fresh and not failed and not unexplained
        problems = []
        if not fresh:
            problems.append(f'отчёт устарел (возраст {round((now-stamp)/60)} мин)')
        if failed:
            problems.append('сбой блоков отчёта: ' + ', '.join(failed))
        if unexplained:
            problems.append('отчёт сообщает ошибку вне блоков')
        summary = '; '.join(problems) if problems else 'проверка свежая и успешная'
        if ignored_failing:
            summary += ' (без учёта: ' + ', '.join(ignored_failing) + ')'
        return _result(ok, summary,
                       age_seconds=round(now-stamp), component_ok={str(k): bool(v.get('ok')) for k,v in components.items() if isinstance(v,dict)},
                       failed_components=failed, ignored_failing=ignored_failing)
    except (OSError, ValueError, KeyError, TypeError):
        return _result(False, 'отчёт недоступен или повреждён')


def check_neighbor(cfg, now, open_db=None):
    """Never emit raw job data: it can contain credentials or user text."""
    name = cfg['name']
    try:
        state = subprocess.run(['systemctl','is-active',cfg['unit']], stdin=subprocess.DEVNULL,
                               capture_output=True, text=True, timeout=10)
        if state.returncode or state.stdout.strip() != 'active':
            return _result(False, name+': служба не активна', category='service')
        with urllib.request.urlopen(cfg['health_url'], timeout=10) as response:
            if response.status != 200:
                return _result(False, name+': health не подтверждён', category='service')
        conn, _mode = (open_db or _default_open)(str(Path(cfg['db']).resolve()))
        conn.execute('PRAGMA query_only=ON')
        try:
            columns={r[1] for r in conn.execute('PRAGMA table_info(jobs)')}
            waitcol='not_before' if 'not_before' in columns else '0'
            rows=conn.execute(f"SELECT status,created_at,data,{waitcol} FROM jobs WHERE status IN ('pending','running','resetting','failed','partial','interrupted','completed_partial') ORDER BY created_at DESC LIMIT 10000").fetchall()
        finally:
            conn.close()
        counts={}; stuck=0; waiting=0; internal=0; external=0; partial=0
        for status,created,raw,not_before in rows:
            counts[status]=counts.get(status,0)+1
            value=json.loads(raw)
            if not isinstance(value,dict):
                raise ValueError('bad row')
            eligible = float(not_before or 0)<=now
            acknowledged = str(value.get('provider') or value.get('generation_provider') or '') in cfg.get('acknowledged_providers',[])
            if not eligible or acknowledged:
                waiting += 1
                continue
            if status in ('pending','running','resetting'):
                start=float(value.get('started_at') or created)
                age=now-(float(created) if status=='pending' else start)
                limit=float(cfg.get('queued_max_seconds',21600) if status=='pending' else cfg.get('running_max_seconds',14400))
                stuck += age>limit
            elif float(value.get('finished_at') or created)>=now-21600:
                error=str(value.get('error') or '').lower()
                is_external=any(marker in error for marker in ('rate limit','quota exceeded','insufficient quota','http 429','http 503','provider unavailable','external_provider'))
                if status in ('partial','completed_partial'):
                    partial+=1
                if is_external: external+=1
                else: internal+=1
        ok=stuck==0 and internal<int(cfg.get('min_errors',3))
        return _result(ok,name+f': зависших {stuck}, ошибок сервиса {internal}, отказов провайдера {external}, частичных {partial}, ожидают окна/паузы {waiting}',
                       category='queue' if stuck else 'internal_error' if not ok else 'external_provider' if external else 'idle' if not any(counts.get(x,0) for x in ('pending','running','resetting')) else 'working',
                       counts=counts,stuck=stuck,internal_errors=internal,external_errors=external,partial=partial,waiting=waiting)
    except Exception as exc:
        # Текст ошибки — только в отчёт прогона (не в Telegram), чтобы причина была видна.
        return _result(False,name+': проверка недоступна ('+type(exc).__name__+')',category='monitor',error=str(exc)[:120])


MINSK_PROBE = r'''
import json,time,pathlib,subprocess,shutil
now=time.time();root=pathlib.Path('/opt/video_agent/render_jobs')
p=subprocess.run(['systemctl','is-active','vsrender-puller.service'],capture_output=True,text=True,timeout=10)
counts={};stuck=0;retained=0;invalid=0
for f in (root/'outbox').glob('*.json'):
 try:
  d=json.loads(f.read_text());phase=str(d.get('phase','invalid'));counts[phase]=counts.get(phase,0)+1
  deadline=float(d.get('deadline_at') or 0)
  stamp=float(d.get('created_at') or f.stat().st_mtime)
  if phase in ('ready','error') and ((deadline and deadline<now) or now-stamp>14400):stuck+=1
  out=d.get('output') or ''
  if phase!='acked':retained+=int(d.get('retained_bytes') or d.get('size') or (pathlib.Path(out).stat().st_size if out and pathlib.Path(out).is_file() else 0))
 except Exception:invalid+=1
free=shutil.disk_usage(root if root.exists() else root.parent).free
attention=invalid+counts.get('quarantined',0)+counts.get('invalid',0)
print(json.dumps(dict(active=p.returncode==0 and p.stdout.strip()=='active',counts=counts,stuck=stuck,attention=attention,free_bytes=free,retained_bytes=retained)))
'''


def check_minsk(host):
    try:
        done=subprocess.run(['ssh','-o','BatchMode=yes','-o','ConnectTimeout=8','-o','ServerAliveInterval=5','-o','ServerAliveCountMax=2',host,'python3','-'],input=MINSK_PROBE,text=True,capture_output=True,timeout=35)
        if done.returncode:
            raise RuntimeError('ssh')
        value=json.loads(done.stdout)
        ok=value['active'] and not value['stuck'] and not value['attention'] and value['free_bytes']>=32*1024**3 and value['retained_bytes']<=64*1024**3
        return _result(ok,'Минск: '+('нода доступна, outbox без проблем' if ok else 'нода/диск/outbox требует внимания'),**value)
    except Exception as exc:
        return _result(False,'Минск: read-only проверка недоступна ('+type(exc).__name__+')')


def check_render_heartbeat(db, now, max_age=180, open_db=None):
    try:
        conn, _mode = (open_db or _default_open)(str(Path(db).resolve()))
        try:
            row = conn.execute("SELECT v FROM admin_state WHERE k='render_node_health'").fetchone()
        finally:
            conn.close()
        data = json.loads(row[0])
        fresh = -60 <= now-float(data["generated_at"]) <= max_age
        return _result(fresh and data.get("ok") is True,
                       "Минск: " + ("heartbeat свежий, диск/outbox в норме" if fresh and data.get("ok") is True else "heartbeat устарел или нода сообщает проблему"),
                       age_seconds=round(now-float(data["generated_at"])),
                       metrics={name:data.get(name) for name in ("free_bytes","outbox_retained_bytes","outbox_attention","outbox_stuck","active_jobs","puller_sha256","worker_sha256")})
    except Exception as exc:
        return _result(False, "Минск: heartbeat недоступен ("+type(exc).__name__+")", error=str(exc)[:120])


def check_all(cfg, now=None, open_db=None):
    """open_db(path) -> (connection, mode): как открывать WAL-базы соседей и heartbeat;
    None = прежнее открытие без повторов."""
    now=time.time() if now is None else now
    out={}
    for kind in ("services", "reports"):
        entries=cfg.get(kind, [])
        if not isinstance(entries,list):
            out["invalid_"+kind]=_result(False, "некорректная настройка проверок")
            continue
        for index, entry in enumerate(entries):
            name=str(entry.get("name") or index) if isinstance(entry,dict) else str(index)
            key=("service_" if kind=="services" else "report_")+name
            try:
                if kind=="services":
                    out[key]=check_neighbor(entry,now,open_db=open_db)
                else:
                    ignore=entry.get("ignore_components", []) if isinstance(entry,dict) else []
                    if not isinstance(ignore, list):
                        raise ValueError("ignore_components must be a list of component names")
                    out[key]=check_report(entry["path"],float(entry["max_age_seconds"]),now,
                                          ignore_components=_validate_ignore(ignore))
            except Exception as exc:
                out[key]=_result(False,"проверка недоступна ("+type(exc).__name__+")")
    if cfg.get('render_heartbeat_db'):
        out['minsk']=check_render_heartbeat(cfg['render_heartbeat_db'],now,open_db=open_db)
    elif cfg.get('minsk_host'):
        out['minsk']=check_minsk(cfg['minsk_host'])
    return out
