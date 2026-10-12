import asyncio
import hashlib
import importlib.util
import json
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, AsyncMock

import pytest
from agent import jev_shadow as shadow

BRIDGE = Path('/Users/nickgeorge-studio/Projects/jev-task-router')
sys.path.insert(0, str(BRIDGE))
import shadow_bridge as bridge
import router


def event(locked=False):
    return dict(task_hash=hashlib.sha256(b'PRIVATE TASK').hexdigest(), kind='delegate',
                actual_model='gpt-6-astra', actual_provider='openai-codex', locked=locked,
                mode='SHADOW_ONLY', executed=False, timestamp=time.time())


def test_privacy_budget_and_holds(tmp_path, monkeypatch):
    monkeypatch.setattr(router, 'live_transport', Mock(side_effect=AssertionError('network forbidden')))
    e = event()
    assert bridge.process(tmp_path, e, {})['status'] == 'privacy_skipped'
    assert bridge.process(tmp_path, event(True), {})['status'] == 'locked'
    task = dict(task_id='public', brief='Extract the author from a public sentence.', intended_route='luna',
                kind='extraction', approved_external=True, requirements={})
    bridge.register(tmp_path, e['task_hash'], task)
    a = dict(source=router.MODELS_URL, verified_at=0, models={})
    catalog = tmp_path/'availability.json'; catalog.write_text(json.dumps(a))
    cfg = dict(availability=str(catalog), allow_external=True, max_calls_per_day=1)
    assert bridge.process(tmp_path, e, cfg)['status'] == 'availability_hold'
    a['verified_at'] = time.time(); catalog.write_text(json.dumps(a))
    response = {'answers':{k:dict(type='choice', choice='low', confidence=1, probabilities={'low':1,'medium':0,'high':0}) for k in router.CRITERIA}}
    transport = Mock(return_value=response)
    r = bridge.process(tmp_path, e, cfg, transport)
    assert r['status'] == 'recommendation_hold' and not r['decision']['eligible']
    assert transport.call_args.args[0]['state'] == {'task_brief':task['brief']}
    assert 'PRIVATE TASK' not in ''.join(p.read_text() for p in tmp_path.rglob('*.json'))
    with pytest.raises(ValueError): bridge.reserve_daily(tmp_path/'jev-shadow', 1)
    router.live_transport.assert_not_called()


def test_disabled_and_real_sidecar(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    (tmp_path/'config.yaml').write_text('extra: {}')
    shadow.observe('SECRET CONTEXT',kind='delegate',actual_model='gpt-6-astra')
    assert not (tmp_path/'jev-shadow').exists()
    (tmp_path/'config.yaml').write_text(json.dumps({'extra':{'jev_shadow':{'enabled':True,'bridge':str(BRIDGE/'shadow_bridge.py')}}}))
    shadow.observe('SECRET CONTEXT',kind='delegate',actual_model='gpt-6-astra')
    assert shadow._slot.acquire(timeout=10); shadow._slot.release()
    files = list((tmp_path/'jev-shadow/events').glob('*.json'))
    assert len(files) == 1
    assert json.loads(files[0].read_text())['status'] == 'privacy_skipped'
    assert 'SECRET CONTEXT' not in files[0].read_text()


@pytest.mark.parametrize('script,status', [('import time; time.sleep(10)', 'hook_timeout'), ('raise RuntimeError()', 'hook_error')])
def test_worker_failure_records_and_opens_circuit(tmp_path, monkeypatch, script, status):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setattr(shadow, '_cooldown_until', 0)
    sidecar = tmp_path/'sidecar.py'; sidecar.write_text(script)
    cfg = dict(enabled=True, bridge=str(sidecar), timeout_seconds=1)
    (tmp_path/'config.yaml').write_text(json.dumps({'extra':{'jev_shadow':cfg}}))
    shadow.observe('PRIVATE',kind='background',actual_model='gpt-6-astra')
    assert shadow._slot.acquire(timeout=5); shadow._slot.release()
    shadow.observe('PRIVATE',kind='background',actual_model='gpt-6-astra')
    records = [json.loads(p.read_text()) for p in (tmp_path/'jev-shadow/events').glob('*.json')]
    assert {r['status'] for r in records} == {status, 'circuit_open'}
    assert all('PRIVATE' not in json.dumps(r) for r in records)


@pytest.mark.parametrize('failure', [RuntimeError('sidecar'), subprocess.TimeoutExpired('sidecar',1)])
def test_delegate_dispatch_unchanged(monkeypatch, failure):
    from tools import delegate_tool as d
    parent = SimpleNamespace(_delegate_depth=0)
    creds = dict(model='gpt-6-astra', provider='openai-codex', api_key='SECRET')
    for name, value in [('is_spawn_paused',False),('_get_max_spawn_depth',2),('_load_config',{'model':'gpt-6-astra'}),
                        ('_get_max_concurrent_children',3),('_resolve_delegation_credentials',creds),
                        ('_coerce_task_schemas',([None],None)),('_build_children',([],None)),('_capture_origin',(None,None,None))]:
        monkeypatch.setattr(d,name,Mock(return_value=value))
    monkeypatch.setattr(d,'_announce_batch',Mock())
    monkeypatch.setattr(d,'_Batch',Mock())
    monkeypatch.setattr(d,'_run_batch',Mock(return_value='done'))
    monkeypatch.setattr('tools.delegation_live_log.create_live_transcripts',Mock(return_value=('id',[],[])))
    observer = Mock(side_effect=failure); monkeypatch.setattr(shadow,'observe',observer)
    assert d.delegate_task(goal='task',context='PRIVATE CONTEXT',parent_agent=parent) == 'done'
    d._build_children.assert_called_once()
    assert d._build_children.call_args.args[2] == creds
    assert observer.call_args.kwargs['locked'] is True
    assert 'PRIVATE CONTEXT' not in str(observer.call_args)
    d._run_batch.assert_called_once()


@pytest.mark.parametrize('fail', [False, True])
def test_background_constructor_unchanged(monkeypatch, fail):
    from gateway.run_turn import GatewayTurnMixin
    import gateway.run as run
    import run_agent
    adapter = SimpleNamespace(send=AsyncMock(),extract_media=lambda x:([],x),extract_images=lambda x:([],x))
    runtime = dict(provider='openai-codex',api_key='SECRET',api_mode='codex_responses')
    async def execute(fn): return fn()
    runner = SimpleNamespace(
        config=SimpleNamespace(), _adapter_for_source=lambda s:adapter, _thread_metadata_for_source=lambda *a:{},
        _resolve_session_agent_runtime=lambda **kw:('gpt-6-astra',dict(runtime)),
        _resolve_turn_toolsets=lambda *a:([],None),_provider_routing={},
        _resolve_session_reasoning_config=lambda **kw:{'effort':'high'},_resolve_session_service_tier=lambda **kw:None,
        _resolve_turn_agent_config=lambda *a:{'model':'gpt-6-astra','runtime':dict(runtime)},
        _resolve_session_key_or_none=lambda *a:'session',
        _peek_session_state=lambda *a:SimpleNamespace(conversation=SimpleNamespace(model_override={'model':'gpt-6-astra'})),
        _session_db=None,_refresh_fallback_model=lambda:None,_cleanup_agent_resources=Mock(),_run_in_executor_with_context=execute)
    source = SimpleNamespace(platform='telegram',**{k:None for k in ('user_id','user_id_alt','user_name','chat_id','chat_name','chat_type','thread_id')})
    monkeypatch.setattr(run,'_load_gateway_config',lambda:{})
    monkeypatch.setattr(run,'_checkpoint_agent_kwargs',lambda cfg:{})
    monkeypatch.setattr(run,'_current_max_iterations',lambda:3)
    monkeypatch.setattr(run,'_platform_config_key',lambda p:p)
    monkeypatch.setattr(run,'_get_channel_override',lambda *a,**kw:None)
    constructor=Mock(return_value=SimpleNamespace(run_conversation=Mock(return_value={'final_response':''})))
    monkeypatch.setattr(run_agent,'AIAgent',constructor)
    observer=Mock(side_effect=RuntimeError('failed') if fail else None); monkeypatch.setattr(shadow,'observe',observer)
    asyncio.run(GatewayTurnMixin._run_background_task_inner(runner,'PUBLIC TASK',source,'task-id'))
    constructor.assert_called_once()
    kw=constructor.call_args.kwargs
    assert kw['model']=='gpt-6-astra' and all(kw[k]==v for k,v in runtime.items())
    assert kw['reasoning_config']=={'effort':'high'}
    constructor.return_value.run_conversation.assert_called_once_with(user_message='PUBLIC TASK',task_id='task-id')
    observer.assert_called_once()
    assert observer.call_args.kwargs['locked'] is True
