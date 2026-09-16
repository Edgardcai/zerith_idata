"""Independent old-framework workbench with a mounted automatic QC module."""
import os
from pathlib import Path
ROOT=Path(__file__).resolve().parent
RUNTIME=Path(os.environ.setdefault('DATAQC_HOME',str(ROOT/'runtime')))
os.environ['PYTHONPATH']=os.pathsep.join([str(ROOT),str(ROOT/'vendor'),os.environ.get('PYTHONPATH','')])
os.environ.setdefault('PIPELINE_DATA_SCAN_ROOT',os.path.commonpath([os.environ.get('DATAQC_REAL_ROOT','/data/zerith_data'),os.environ.get('DATAQC_SIM_ROOT','/data/sim_data')]))
os.environ.setdefault('PIPELINE_H200_DATA_SCAN_ROOT',os.environ.get('DATAQC_REAL_ROOT','/data/zerith_data'))
os.environ.setdefault('PIPELINE_H200_MCAP_SCAN_ROOT','/data')
os.environ.setdefault('PIPELINE_MANUAL_SCREENING_DATA_ROOT',str(RUNTIME/'exports'))
os.environ.setdefault('PIPELINE_MANUAL_SCREENING_STORAGE_ROOT',str(RUNTIME/'manual-screening'))
from dataqc.config import settings
os.environ.setdefault('PIPELINE_MANUAL_SCREENING_YOLO_MODEL',settings()['yolo_path'])
os.environ.setdefault('PIPELINE_MANUAL_SCREENING_YOLO_THRESHOLDS',str(RUNTIME/'config/yolo-thresholds.json'))
import sys, threading, importlib.util
sys.path.insert(0,str(ROOT/'vendor'))
from contextlib import asynccontextmanager
from http.server import ThreadingHTTPServer
import httpx
from fastapi import FastAPI,Request
from starlette.responses import StreamingResponse
from starlette.background import BackgroundTask


def load_legacy():
    path=ROOT/'legacy/scripts/embodied_data_pipeline-main/scripts/pipeline_web_app.py'
    spec=importlib.util.spec_from_file_location('unified_legacy_app',path)
    module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=module;spec.loader.exec_module(module)
    from integrations.legacy_ui import integrate
    integrate(module)
    return module


@asynccontextmanager
async def lifespan(app):
    legacy=load_legacy();server=ThreadingHTTPServer(('127.0.0.1',0),legacy.Handler)
    app.state.legacy=legacy;app.state.legacy_server=server
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    async with httpx.AsyncClient(base_url=f'http://127.0.0.1:{server.server_port}',timeout=None,trust_env=False) as client:
        app.state.client=client
        yield
    server.shutdown();server.server_close()
    for proc in legacy.REPLAY_PROCESSES.values():
        if proc.poll() is None:proc.terminate()


from dataqc.api import app as automatic_app
app=FastAPI(lifespan=lifespan)
app.mount('/auto',automatic_app)

@app.get('/healthz')
def health():
    from dataqc.motion import RULE_VERSION
    return dict(ok=True,release='20260916.3',manual_policy='portable_partial_all_grades',split_policy='direct_optional_post_qc',conversion_policy='direct_optional_post_qc',directory_policy='height_check_disabled',project=str(ROOT),framework='legacy',rules=RULE_VERSION,auto='/auto/hdf5',batch_policy='motion_batch_v1',grade_workflow='persistent_manual_notes_v2',incremental_qc='completed_reports_v1',split_layout='twohands_lefthand_v1',conversion_pool='episode_pool_v1',review_ui='review_filters_v1',replay_layout='viewport_fit_v1',grade_priority='manual_qc_collection_v1',episode_identity='collection_and_directory_v1')

@app.api_route('/{path:path}',methods=['GET','POST','PUT','PATCH','DELETE','HEAD','OPTIONS'])
async def legacy_proxy(request:Request,path:str):
    headers={k:v for k,v in request.headers.items() if k.lower() not in ('host','connection','content-length','accept-encoding')}
    target=request.url.path+('?' + request.url.query if request.url.query else '')
    req=app.state.client.build_request(request.method,target,headers=headers,content=await request.body())
    response=await app.state.client.send(req,stream=True)
    filtered={k:v for k,v in response.headers.items()if k.lower()not in ('connection','transfer-encoding','server','date')}
    return StreamingResponse(response.aiter_raw(),status_code=response.status_code,headers=filtered,background=BackgroundTask(response.aclose))

if __name__=='__main__':
    import argparse,uvicorn
    parser=argparse.ArgumentParser();parser.add_argument('--host',default='0.0.0.0');parser.add_argument('--port',type=int,default=9990)
    args=parser.parse_args();os.environ['DATAQC_PORT']=str(args.port)
    uvicorn.run('workbench:app',host=args.host,port=args.port)
