"""Offline fault injection: real subprocess death + PostgreSQL + disk, no LLM.

Requires a dedicated DB (migrated to head), --out new directory and existing
REVIEW_AGENT_DATABASE_URL. This is the recovery boundary, not a Celery E2E run.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--child',action='store_true')
    args=parser.parse_args()
    from review_agent.harness.models import ToolCall,ReplayCategory
    from review_agent.services.task_store_postgres import PostgresTaskStore
    from review_agent.services.patch_apply import apply_patch
    from review_agent.services.recovery import reconcile_run
    url=os.environ['REVIEW_AGENT_DATABASE_URL']
    if 'resume_evidence' not in url:
        raise SystemExit('Only a dedicated resume_evidence database is permitted')
    root=args.out.resolve()
    store=PostgresTaskStore(url=url)
    if args.child:
        data=json.loads((root/'intent.json').read_text())
        store.record_tool_execution(run_id=data['run_id'],session_id=data['session_id'],
            tool_call=ToolCall(name='apply_patch',arguments={'patch':data['patch']},call_id=data['call_id']),
            replay_category=ReplayCategory.PATCH_CHECKABLE)
        applied=apply_patch(root/'workspace',data['patch'])
        assert applied.success
        # Injection point: write persisted on disk, ToolResult not persisted.
        (root/'disk-written').write_text('ready')
        while True: time.sleep(1)
    root.mkdir(parents=True,exist_ok=False)
    ws=root/'workspace'; ws.mkdir(); (ws/'readme.txt').write_text('before\n')
    subprocess.run(['git','init','-q',str(ws)],check=True)
    subprocess.run(['git','-C',str(ws),'add','readme.txt'],check=True)
    subprocess.run(['git','-C',str(ws),'-c','user.name=Evidence','-c','user.email=evidence@localhost','commit','-qm','fixture'],check=True)
    session=store.create_session('resume-recovery-evidence')
    run=store.create_run(session.session_id,'Controlled recovery boundary experiment')
    call_id='evidence-'+uuid.uuid4().hex
    patch='diff --git a/readme.txt b/readme.txt\n--- a/readme.txt\n+++ b/readme.txt\n@@ -1 +1 @@\n-before\n+after\n'
    data=dict(session_id=session.session_id,run_id=run.run_id,call_id=call_id,patch=patch)
    (root/'intent.json').write_text(json.dumps(data,indent=2))
    # Do not pass any model keys to the side-effect subprocess.
    env={k:v for k,v in os.environ.items() if not any(x in k.upper() for x in ('API_KEY','TOKEN','SECRET'))}
    child=subprocess.Popen([sys.executable,__file__,'--out',str(root),'--child'],env=env)
    try:
        deadline=time.monotonic()+20
        while not (root/'disk-written').exists():
            if child.poll() is not None: raise RuntimeError('child exited before injection point')
            if time.monotonic()>deadline: raise TimeoutError('injection point not reached')
            time.sleep(.05)
        assert store.get_tool_execution(call_id).result is None
        child.send_signal(signal.SIGKILL); exit_code=child.wait(timeout=5)
        content=(ws/'readme.txt').read_bytes(); before_hash=hashlib.sha256(content).hexdigest()
        restarted_store=PostgresTaskStore(url=url)
        decision=reconcile_run(store=restarted_store,workspace_root=ws,run_id=run.run_id)
        assert decision.status=='ok' and call_id in decision.reconciled
        record=restarted_store.get_tool_execution(call_id)
        assert record.result is not None and record.result.success
        assert (ws/'readme.txt').read_bytes()==content
        again=reconcile_run(store=restarted_store,workspace_root=ws,run_id=run.run_id)
        assert call_id in again.reused and not again.reconciled
        assert len(restarted_store.list_tool_executions(run.run_id))==1
        s2=store.create_session('resume-unknown-effect');r2=store.create_run(s2.session_id,'Unknown side effect')
        c2='unknown-'+uuid.uuid4().hex
        store.record_tool_execution(run_id=r2.run_id,session_id=s2.session_id,
            tool_call=ToolCall(name='mcp__fixture__external_write',arguments={},call_id=c2),replay_category=ReplayCategory.NON_REPLAYABLE)
        unknown=reconcile_run(store=store,workspace_root=ws,run_id=r2.run_id)
        assert unknown.status=='needs_attention' and unknown.call_id==c2
        result=dict(mode='deterministic_process_fault_injection_no_model_no_celery',child_exit_code=exit_code,
                    session_id=session.session_id,run_id=run.run_id,call_id=call_id,
                    first=decision.__dict__,second=again.__dict__,unknown_effect=unknown.__dict__,
                    disk_sha256=before_hash,disk_unchanged_after_reconcile=True,tool_record_count=1)
        (root/'result.json').write_text(json.dumps(result,indent=2))
        print(json.dumps(result,indent=2))
    finally:
        if child.poll() is None: child.kill();child.wait()

if __name__=='__main__': main()
