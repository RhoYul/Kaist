"""Run against TWO explicitly demo-mode servers started from config/*.json."""
import json,time
from pathlib import Path
import httpx
base=Path(__file__).resolve().parents[1]
capcfg=json.loads((base/'config/capture.json').read_text())
excfg=json.loads((base/'config/export.json').read_text())
assert capcfg['demo'] and excfg['demo'], 'Demo configuration required'
c=httpx.Client(base_url='http://127.0.0.1:'+str(capcfg['port']),timeout=120,trust_env=False)
e=httpx.Client(base_url='http://127.0.0.1:'+str(excfg['port']),timeout=120,trust_env=False)
def req(client,method,url,body=None):
    r=client.request(method,url,json=body);r.raise_for_status();return r.json()
def wait(fn,predicate,label):
    deadline=time.monotonic()+90
    while time.monotonic()<deadline:
        state=fn()
        if predicate(state):return state
        time.sleep(.25)
    raise RuntimeError(label+' timeout: '+str(state))
req(e,'POST','/api/login',dict(role='admin',password=excfg['admin_password']))
req(c,'POST','/api/login',dict(role='worker',password=capcfg['worker_password']))
task=req(e,'POST','/api/tasks',dict(name='Demo - four-camera validation',scene='2D',target=10,prompt='blue cloth',label='cloth',object_name='blue cloth',interaction='fold, roll and unfold a blue cloth on a table by hand'))
req(c,'GET','/api/state')
req(c,'POST','/api/calibration/reset')
for _ in range(5):req(c,'POST','/api/calibration/snapshot')
req(c,'POST','/api/calibration/compute',{})
req(c,'POST','/api/calibration/confirm')
ep=req(c,'POST','/api/capture/'+task['id']);key=ep['id']
getep=lambda:next(x for x in req(c,'GET','/api/state')['episodes'] if x['id']==key)
ep=wait(getep,lambda x:x['status'] in ('captured','failed'),'capture');assert ep['status']=='captured',ep
assert ep['frame_counts']=={f'cam_{n}':150 for n in range(1,5)},ep
req(c,'POST','/api/episodes/'+key+'/qc')
ep=wait(getep,lambda x:x['status']=='qc_done','qc');assert ep['qc']['demo']
req(c,'POST','/api/episodes/'+key+'/approve',dict(note='Synthetic demo; transport test only'))
req(c,'POST','/api/episodes/'+key+'/send')
ep=wait(getep,lambda x:x.get('transfer_status') in ('sent','failed'),'transfer');assert ep['transfer_status']=='sent',ep
jobs=lambda:req(e,'GET','/api/state')['jobs']
job=wait(lambda:next(x for x in jobs() if x['id']==ep['export_job']),lambda x:x['status'] in ('done','failed'),'export')
assert job['status']=='done',job
out=Path(job['output']);assert len(list(out.glob('cam_*_depth/*.npy')))==600
assert len(list(out.glob('cam_*.mp4')))==4 and (out/'rgbd_episode.json').is_file()
print(json.dumps(dict(result='PASS',episode=key,nas_folder=out.name,depth_files=600,rgb_videos=4,packing_seconds=ep.get('packing_seconds'),transfer_seconds=ep.get('transfer_seconds'),export_timings=job['timings'],note='Loopback + local synthetic NAS only; not hardware performance'),indent=2))
