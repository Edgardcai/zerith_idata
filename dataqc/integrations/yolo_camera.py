"""Embed Camera Studio and proxy it through the workbench's own origin."""
import httpx
import re
from fastapi import APIRouter, Request
from starlette.background import BackgroundTask
from starlette.responses import JSONResponse, RedirectResponse, StreamingResponse

from .legacy_ui import replace

router = APIRouter()
HOP_HEADERS = {'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
               'te', 'trailer', 'transfer-encoding', 'upgrade'}


@router.get('/yolo', include_in_schema=False)
async def camera_redirect(request: Request):
    return RedirectResponse('/yolo/' + ('?' + request.url.query if request.url.query else ''))


@router.api_route('/yolo/{path:path}', methods=['GET', 'POST', 'HEAD'])
async def camera_proxy(request: Request, path: str):
    client = request.app.state.yolo_camera_client
    # Preserve the public Host and Origin together for Camera Studio's same-origin check.
    headers = {k: v for k, v in request.headers.items()
               if k.lower() not in HOP_HEADERS | {'content-length', 'accept-encoding'}}
    url = client.base_url.copy_with(path='/' + path.lstrip('/'), query=request.url.query.encode())
    outgoing = client.build_request(request.method, url, headers=headers, content=await request.body())
    try:
        response = await client.send(outgoing, stream=True)
    except httpx.TimeoutException:
        return JSONResponse({'ok': False, 'error': '本机视觉服务响应超时，请稍后重试'}, status_code=504)
    except httpx.RequestError:
        return JSONResponse({'ok': False, 'error': '本机视觉服务暂不可用，请检查 yolo-camera-studio 服务'}, status_code=503)
    filtered = {k: v for k, v in response.headers.items()
                if k.lower() not in HOP_HEADERS | {'server', 'date'}}
    return StreamingResponse(response.aiter_raw(), status_code=response.status_code,
                             headers=filtered, background=BackgroundTask(response.aclose))


def integrate_camera(app):
    nav = re.search(r'<nav class="workspace-switch"[^>]*>.*?</nav>', app.HTML, re.S)
    if not nav:
        raise RuntimeError('Workbench navigation missing')
    app.HTML = replace(app.HTML, nav[0], nav[0].replace('</nav>',
        '      <button id="yoloWorkspaceBtn" class="workspace-btn" type="button">⑤ YOLO 实时检测</button>\n</nav>'))
    app.HTML = replace(app.HTML, '  <main id="collectionWorkspace">', '''  <div id="yoloWorkspace" class="cross-platform-workspace hidden">
    <iframe id="yoloFrame" title="YOLO 实时检测" allow="camera 'self'; fullscreen" style="height:calc(100dvh - var(--workbench-header-height, 100px));min-height:640px;border:0"></iframe>
  </div>
  <main id="collectionWorkspace">''')
    app.HTML = replace(app.HTML, '    function setWorkspace(name) {', '''    function setWorkspace(name) {
      const yolo = name === 'yolo';
      if(yolo) history.replaceState(null,'','#yolo');
      else if(location.hash === '#yolo') history.replaceState(null,'',location.pathname+location.search);
      document.getElementById('yoloWorkspace').classList.toggle('hidden', !yolo);
      document.getElementById('yoloWorkspaceBtn').classList.toggle('active', yolo);
      const yoloFrame = document.getElementById('yoloFrame');
      if(yolo && !yoloFrame.getAttribute('src')) yoloFrame.src='/yolo/?embedded=1';
      if(yoloFrame.getAttribute('src')) yoloFrame.contentWindow.postMessage({type:'yolo-workspace-visibility',visible:yolo},location.origin);''')
    app.HTML = replace(app.HTML,
        "    setWorkspace(location.hash === '#collection-dashboard' ? 'collection-dashboard' : 'collection');",
        """    document.getElementById('yoloWorkspaceBtn').onclick=()=>setWorkspace('yolo');
    setWorkspace(location.hash === '#yolo' ? 'yolo' : location.hash === '#collection-dashboard' ? 'collection-dashboard' : 'collection');""")
