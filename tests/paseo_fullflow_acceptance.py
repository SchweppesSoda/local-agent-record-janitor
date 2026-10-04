"""Opt-in actual Codex/Pi/Claude deletion against owned synthetic Paseo stores.

Run with --binary ABSOLUTE_PINNED_CODEX_EXE and --scenario complete or
cold-continue. Every phase runs in a new process with isolated homes, Git
configuration and loopback-only proxies. Generated stores stay beneath
.codex-artifacts/herdr-fullflow-fixtures. Never uses a real Paseo deployment,
host session, authentication file, conversation turn or external login.
"""
import argparse
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from unittest.mock import patch

REPOSITORY = Path(__file__).resolve().parents[1]
if not (REPOSITORY/'src/local_agent_record_janitor').is_dir():
    REPOSITORY = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPOSITORY/'src'), str(REPOSITORY)]
AGENTS = ['00000000-0000-4000-8000-000000000101', '00000000-0000-4000-8000-000000000102',
          '00000000-0000-4000-8000-000000000103']
KEEPERS = ['00000000-0000-4000-8000-000000000201', '00000000-0000-4000-8000-000000000202',
           '00000000-0000-4000-8000-000000000203']
NATIVE = ['00000000-0000-4000-8000-000000000301', '00000000-0000-4000-8000-000000000302',
          '00000000-0000-4000-8000-000000000303']
NATIVE_KEEP = ['00000000-0000-4000-8000-000000000401', '00000000-0000-4000-8000-000000000402',
               '00000000-0000-4000-8000-000000000403']
CHILD = '00000000-0000-4000-8000-000000000501'
ENGINES = ('codex', 'pi', 'claude')


def metadata(path):
    info = path.stat()
    return {'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'identity': [info.st_dev, info.st_ino],
            'nlink': info.st_nlink, 'mtime_ns':info.st_mtime_ns, 'mode':info.st_mode}


def native_row(home):
    with sqlite3.connect((home/'state_5.sqlite').as_uri()+'?mode=ro', uri=True) as db:
        row = db.execute('SELECT * FROM threads WHERE id=?', (NATIVE_KEEP[0],)).fetchone()
    return hashlib.sha256(json.dumps(row, separators=(',', ':')).encode()).hexdigest()


def write(path, value):
    from local_agent_record_janitor.paseo_cleanup_files import encode
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encode(value))


def schedule(identifier, target, runs):
    return {'id': identifier, 'name': 'SYNTHETIC_SCHEDULE', 'prompt': 'SYNTHETIC_SHARED_PROMPT',
        'cadence': {'type':'every', 'everyMs':1000}, 'target':target, 'status':'paused',
        'createdAt':'t', 'updatedAt':'t', 'nextRunAt':None, 'lastRunAt':'t', 'pausedAt':'t',
        'expiresAt':None, 'maxRuns':3, 'runs':[
            {'id':'run'+str(i), 'scheduledFor':'slot'+str(i), 'startedAt':'start', 'endedAt':'end',
             'status':'succeeded', 'agentId':agent, 'workspaceId':'workspace'+str(i),
             'output':('ERASE_' if agent in AGENTS else 'KEEP_')+agent, 'error':None}
            for i, agent in enumerate(runs)]}


def seed(base, binary, scenario):
    from local_agent_record_janitor.paseo_bound_adapter import PaseoBoundAdapter, SCHEMA
    from local_agent_record_janitor.cleanup_service import CleanupService
    from local_agent_record_janitor.operation_coordinator import OperationCoordinator
    from tests.orca_native_support import create_native_schema, add_native_record
    profile, codex, pi, claude = base/'paseo', base/'codex', base/'pi', base/'claude'
    profile.mkdir(); codex.mkdir(); (pi/'sessions').mkdir(parents=True); claude.mkdir()
    (profile/'server-id').write_text('srv_acceptance\n', encoding='utf-8')
    create_native_schema(codex)
    selected_paths = [add_native_record(codex, NATIVE[0]), add_native_record(codex, CHILD, parent=NATIVE[0])]
    keeper_paths = [add_native_record(codex, NATIVE_KEEP[0])]
    index = codex/'session_index.jsonl'
    index.write_text(''.join(json.dumps({'id':value, 'thread_name':'', 'updated_at':'2026-10-03T00:00:00Z'})+'\n'
        for value in (NATIVE[0], CHILD, NATIVE_KEEP[0])), encoding='utf-8')
    for identifier, name, paths in ((NATIVE[1], 'selected', selected_paths), (NATIVE_KEEP[1], 'keeper', keeper_paths)):
        path = pi/'sessions'/(name+'.jsonl')
        path.write_text(json.dumps({'type':'session','id':identifier,'version':3,
            'timestamp':'2026-10-01T00:00:00Z','cwd':str(base)})+'\n', encoding='utf-8')
        paths.append(path)
    for identifier, name, paths in ((NATIVE[2], 'selected', selected_paths), (NATIVE_KEEP[2], 'keeper', keeper_paths)):
        path = claude/'projects'/'synthetic'/(identifier+'.jsonl')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'sessionId':identifier,'message':'SYNTHETIC_'+name})+'\n', encoding='utf-8')
        paths.append(path)
    for relative in ('debug/'+NATIVE[2]+'.txt', 'todos/'+NATIVE[2]+'-agent-main_1.json',
                     'projects/synthetic/'+NATIVE[2]+'/tool-results/result.txt'):
        path = claude/relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('SYNTHETIC_SELECTED_AUXILIARY', encoding='utf-8')
        selected_paths.append(path)
    gate = base/'synthetic-paseo.exe'
    gate.write_bytes(b'Synthetic server startup identity: never executed')
    stores = []
    selected_frontend = []
    for i, engine in enumerate(ENGINES):
        for chosen, identifier, native in ((True, AGENTS[i], NATIVE[i]), (False, KEEPERS[i], NATIVE_KEEP[i])):
            binding = {'agent_id':identifier, 'engine':engine,
                'root':str((codex, pi/'sessions', claude)[i])}
            if engine == 'codex': binding['codex_binary'] = str(binary)
            if engine == 'pi': binding['agent_dir'] = str(pi)
            stores.append(binding)
            handle = str(pi/'sessions'/('selected.jsonl' if chosen else 'keeper.jsonl')).upper() if engine == 'pi' else native
            mark = ('ERASE_' if chosen else 'KEEP_')+engine
            provider_metadata = {'provider':engine,'cwd':str(base),'systemPrompt':mark,'mcpServers':{}}
            if engine == 'pi': provider_metadata = {'cwd':str(base),'model':'SYNTHETIC_MODEL','thinkingOptionId':'high','modeId':'default'}
            if engine == 'codex': provider_metadata.update(threadId=native,modeId='default',model=None,
                thinkingOptionId=None,toolPolicy={},asyncQuestions={})
            row = {'id':identifier,'provider':engine,'cwd':str(base),'createdAt':'2026-10-05T00:00:00Z',
                'updatedAt':'2026-10-05T00:00:00Z','labels':{},'lastStatus':'closed','config':{},'title':mark,
                'persistence':{'provider':engine,'sessionId':native,'nativeHandle':handle,'metadata':provider_metadata},
                'runtimeInfo':{'provider':engine,'sessionId':native}}
            path = profile/'agents'/(identifier+'.json')
            write(path,row)
            if chosen:
                selected_frontend.append(path)
                old = profile/'agents/project'/(identifier+'.json')
                write(old,row); selected_frontend.append(old)
                atom = profile/'agents'/('.'+identifier+'.json.424242.123.'+KEEPERS[0]+'.tmp')
                write(atom,row); selected_frontend.append(atom)
            else:
                keeper_paths.append(path)
    owned = profile/'schedules/1111aaaa.json'
    write(owned, schedule('1111aaaa',{'type':'agent','agentId':AGENTS[0]},[AGENTS[0]]))
    selected_frontend.append(owned)
    ledger = schedule('2222bbbb',{'type':'new-agent','config':{'provider':'codex','cwd':str(base)}}, [*AGENTS,KEEPERS[0]])
    ledger_path = profile/'schedules/2222bbbb.json'
    write(ledger_path, ledger)
    ledger_atom = profile/'schedules'/('.2222bbbb.json.424242.123.'+KEEPERS[0]+'.tmp')
    write(ledger_atom,ledger)
    outside = base/'outside-sentinel'; outside.write_bytes(b'SYNTHETIC_OUTSIDE_SENTINEL'); keeper_paths.append(outside)
    manifest = {'schema_version':SCHEMA,'profile_root':str(profile),'server_id':'srv_acceptance',
        'runtime_binaries':[str(gate)],'native_stores':stores,'desktop_profiles':[]}
    write(base/'manifest.json',manifest)
    state = {'scenario':scenario,'selected_paths':list(map(str,selected_paths)),
        'selected_frontend':list(map(str,selected_frontend)), 'ledger_paths':list(map(str,(ledger_path,ledger_atom))),
        'ledger_before':ledger,'keepers':{str(p):metadata(p) for p in keeper_paths},'native_sentinel_row':native_row(codex)}
    write(base/'fixture-state.json',state)
    plan = OperationCoordinator(CleanupService()).plan_operation(client='paseo',record_ids=tuple(AGENTS),
        adapters=(PaseoBoundAdapter(manifest),),plan_path=base/'plan.json',operation_id='paseo-real-native-'+scenario)
    write(base/'seed-result.json',plan)
    assert plan['goal_status']=='ready', base/'seed-result.json'
    assert {a['kind'] for a in plan['actions']}=={'delete_conversation','delete_pi_session','delete_claude_session','delete_paseo_frontend'}
    return {'phase':'seed','goal_status':plan['goal_status'],'action_kinds':[a['kind'] for a in plan['actions']]}


def worker(base,binary,scenario,phase):
    base = base.resolve(strict=True)
    base.relative_to((REPOSITORY/'.codex-artifacts/herdr-fullflow-fixtures').resolve(strict=True))
    if phase=='seed': return seed(base,binary,scenario)
    from local_agent_record_janitor import paseo_cleanup, paseo_cleanup_files as files
    from local_agent_record_janitor.cleanup_service import CleanupService
    from local_agent_record_janitor.operation_coordinator import OperationCoordinator
    plan = json.loads((base/'plan.json').read_text(encoding='utf-8'))
    state = json.loads((base/'fixture-state.json').read_text(encoding='utf-8'))
    args = {'operation_id':plan['operation_id'],'plan_path':base/'plan.json','plan_sha256':plan['plan_sha256']}
    original_execute = CleanupService.execute
    def forbid_completed_native(service,context,actions,**kwargs):
        assert all(str(getattr(action.kind,'value',action.kind))=='delete_paseo_frontend' for action in actions), 'Completed native typed writer repeated'
        return original_execute(service,context,actions,**kwargs)
    with ExitStack() as stack:
        if phase=='interrupt': stack.enter_context(patch.object(paseo_cleanup,'execute',side_effect=RuntimeError('Synthetic stop before frontend mutation')))
        if phase=='resume':
            stack.enter_context(patch.object(OperationCoordinator,'_execute_manual_batch',side_effect=AssertionError('Completed native Codex writer repeated')))
            stack.enter_context(patch.object(CleanupService,'execute',forbid_completed_native))
        coordinator = OperationCoordinator(CleanupService())
        if phase=='verify':
            result = coordinator.verify_operation(operation_id=plan['operation_id'],plan_path=base/'plan.json')
        else:
            result = coordinator.apply_operation(**args,clients_closed=True)
    write(base/(phase+'-result.json'),result)
    absent = all(not Path(p).exists() for p in state['selected_paths'])
    unchanged = all(metadata(Path(p))==proof for p,proof in state['keepers'].items())
    unchanged = unchanged and native_row(base/'codex')==state['native_sentinel_row']
    assert absent and unchanged, base/(phase+'-result.json')
    if phase=='interrupt':
        assert result['goal_status'] in {'blocked','partial','unknown'}, result
        assert all(Path(p).exists() for p in state['selected_frontend']), 'Frontend changed before synthetic stop'
    else:
        assert result['goal_status']=='complete', base/(phase+'-result.json')
        assert all(not Path(p).exists() for p in state['selected_frontend'])
        expected = state['ledger_before']
        for run in expected['runs']:
            if run['agentId'] in AGENTS: run.update(agentId=None,output=None,error=None)
        assert all(json.loads(Path(p).read_text(encoding='utf-8'))==expected for p in state['ledger_paths'])
        evidence = paseo_cleanup.evidence_from_document(plan)
        assert files.remaining(evidence['files'])==0
        assert not (base/'paseo/paseo.pid').exists()
        if phase=='verify':
            status = coordinator.status_operation(operation_id=plan['operation_id'],plan_path=base/'plan.json')
            write(base/'status-result.json',status)
            assert status['goal_status']=='complete',status
    return {'phase':phase,'process_id':os.getpid(),'goal_status':result['goal_status'],'native_absent':absent,
        'sentinels_unchanged':unchanged,'result':str(base/(phase+'-result.json'))}


def wrapper(binary,scenario):
    from local_agent_record_janitor.orca_runtime import isolated_environment, PINNED_BINARY_SHA256
    assert os.name=='nt' and binary.is_absolute()
    assert metadata(binary)['sha256']==PINNED_BINARY_SHA256, 'Pinned Codex executable drift'
    artifacts = REPOSITORY/'.codex-artifacts/herdr-fullflow-fixtures'
    artifacts.mkdir(parents=True,exist_ok=True)
    base = Path(tempfile.mkdtemp(prefix='paseo-native-'+scenario+'-',dir=artifacts)).resolve(strict=True)
    empty, scratch = base/'empty-default-home',base/'environment'
    empty.mkdir(); scratch.mkdir()
    environment,cwd = isolated_environment(scratch,empty)
    phases = ('seed','apply','verify') if scenario=='complete' else ('seed','interrupt','resume','verify')
    reports=[]
    for phase in phases:
        command = [sys.executable,'-I','-B',str(Path(__file__).resolve()),'--worker','--base',str(base),
            '--binary',str(binary),'--scenario',scenario,'--phase',phase]
        process = subprocess.run(command,env=environment,cwd=cwd,capture_output=True,text=True,
            timeout=240,creationflags=subprocess.CREATE_NO_WINDOW)
        if process.returncode:
            write(base/'failed-worker.json',{'phase':phase,'stdout':process.stdout,'stderr':process.stderr,'returncode':process.returncode})
            raise RuntimeError(str(base/'failed-worker.json')+'\n'+process.stderr)
        print(process.stdout.strip(),flush=True)
        reports.append(json.loads(process.stdout))
    write(base/'acceptance-result.json',{'scenario':scenario,'binary':str(binary),
        'binary_sha256':metadata(binary)['sha256'],'reports':reports,
        'qualification':'Actual Codex/Pi/Claude writers; synthetic inert Paseo binary; independent isolated processes'})
    return base/'acceptance-result.json'


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary',type=Path,required=True)
    parser.add_argument('--scenario',choices=('complete','cold-continue'),default='complete')
    parser.add_argument('--worker',action='store_true')
    parser.add_argument('--base',type=Path)
    parser.add_argument('--phase',choices=('seed','apply','interrupt','resume','verify'))
    args=parser.parse_args()
    if args.worker and (args.base is None or args.phase is None):
        parser.error('--worker requires --base and --phase')
    if args.worker: print(json.dumps(worker(args.base,args.binary,args.scenario,args.phase)))
    else: print(wrapper(args.binary,args.scenario))
