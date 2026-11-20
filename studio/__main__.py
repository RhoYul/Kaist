import argparse, json, re, secrets, socket
from pathlib import Path
from .core import DEFAULTS, load_config, save_json

def main():
    p=argparse.ArgumentParser(description='PhysicalAI KAIST Platform: capture PC + export server + control PC')
    sub=p.add_subparsers(dest='command',required=True)
    init=sub.add_parser('init',help='촬영 PC 설정 (capture.json · export.json) 만들기')
    init.add_argument('--demo',action='store_true');init.add_argument('--directory',default='config')
    init.add_argument('--station',default=None,help='촬영 PC 이름 (기본: 컴퓨터 이름)')
    init.add_argument('--nas-root',default='',help='NAS 저장 폴더 (예: \\\\10.20.32.24\\kaist-data\\rgbd_raw)')
    init.add_argument('--data-dir',default=None,help='원본 · DB 를 둘 로컬 폴더 (기본: runtime)')
    init.add_argument('--remote-access',action='store_true',help='export 서버(8766)를 사내망에 연다 (관제 PC 가 붙을 수 있게)')
    ctl=sub.add_parser('init-control',help='관제 PC 설정 (control.json) 만들기');ctl.add_argument('--directory',default='control-config')
    serve=sub.add_parser('serve');serve.add_argument('--config',required=True)
    args=p.parse_args()
    if args.command=='init':
        root=Path(args.directory);root.mkdir(parents=True,exist_ok=True)
        token=secrets.token_urlsafe(32);admin=secrets.token_urlsafe(15);worker=secrets.token_urlsafe(15)
        for role in ('capture','export'):
            if (root/(role+'.json')).exists(): raise SystemExit(str(root/(role+'.json'))+' already exists; will not overwrite.')
        # 촬영 PC 이름: 영문 · 숫자 · - · _ 만 (컴퓨터 이름에 한글이 있으면 그 글자는 뺀다)
        station=args.station or ''.join(ch if (ch.isascii() and ch.isalnum()) or ch in '-_' else '-' for ch in socket.gethostname().lower()).strip('-')[:60] or 'station-01'
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,60}',station): raise SystemExit("촬영 PC 이름 '"+station+"' 은 쓸 수 없습니다. 영문 · 숫자 · - · _ 만 (예: capture-01)")
        base=Path(args.data_dir) if args.data_dir else Path('runtime')
        for role,port in [('capture',8765),('export',8766)]:
            cfg=dict(DEFAULTS,role=role,port=port,demo=args.demo,data_dir=str(base/role),station=station,transfer_token=token,admin_password=admin,worker_password=worker,
                     export_url='http://127.0.0.1:8766')
            if role=='export' and args.remote_access and not args.demo: cfg['host']='0.0.0.0'
            if role=='export': cfg['nas_root']=args.nas_root
            if args.demo:
                nas=Path('runtime/demo-nas');nas.mkdir(parents=True,exist_ok=True);cfg['nas_root']=str(nas.resolve())
            save_json(root/(role+'.json'),cfg)
        print('Created configuration in',root,'(station:',station+')')
        print('Admin password:',admin);print('Worker password:',worker)
        print('Server token (관제 PC 등록용):',token)
    elif args.command=='init-control':
        root=Path(args.directory);root.mkdir(parents=True,exist_ok=True);dest=root/'control.json'
        if dest.exists(): raise SystemExit(str(dest)+' already exists; will not overwrite.')
        admin=secrets.token_urlsafe(15)
        save_json(dest,dict(role='control',host='127.0.0.1',port=8767,data_dir=str(Path('runtime-control')),admin_password=admin))
        print('Created',dest);print('Control password:',admin)
    else:
        import uvicorn
        cfg=load_config(args.config)
        if cfg['role']=='control':
            from .control import create_control_app
            uvicorn.run(create_control_app(cfg),host=cfg['host'],port=cfg['port'],workers=1); return
        from .app import create_app
        if cfg['demo'] and cfg['host'] not in ('127.0.0.1','localhost','::1'): raise SystemExit('Demo must bind loopback only')
        uvicorn.run(create_app(cfg),host=cfg['host'],port=cfg['port'],workers=1)

if __name__=='__main__': main()
