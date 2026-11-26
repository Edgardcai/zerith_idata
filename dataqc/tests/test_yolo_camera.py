import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from integrations.yolo_camera import router


def test_camera_proxy_preserves_public_origin_body_query_and_frame_headers():
    seen = []

    def upstream(request):
        seen.append(request)
        return httpx.Response(200, stream=httpx.ByteStream(b'jpeg-frame'), headers={
            'Content-Type': 'image/jpeg', 'X-Frame-Id': '41',
            'Content-Security-Policy': "frame-ancestors 'self'"})

    app = FastAPI()
    app.include_router(router)
    app.state.yolo_camera_client = httpx.AsyncClient(
        base_url='http://127.0.0.1:8787', transport=httpx.MockTransport(upstream))
    with TestClient(app, base_url='http://192.168.10.45:9990') as client:
        response = client.post('/yolo/api/camera/start?client_id=test%20one',
            json={'client_id': 'test one', 'device': 'sdk:head'},
            headers={'Origin': 'http://192.168.10.45:9990'})
        assert response.content == b'jpeg-frame'
        assert response.headers['x-frame-id'] == '41'
        assert response.headers['content-security-policy'] == "frame-ancestors 'self'"
        assert str(seen[0].url) == 'http://127.0.0.1:8787/api/camera/start?client_id=test%20one'
        assert seen[0].headers['host'] == '192.168.10.45:9990'
        assert seen[0].headers['origin'] == 'http://192.168.10.45:9990'
        assert b'sdk:head' in seen[0].content
        redirect = client.get('/yolo?embedded=1', follow_redirects=False)
        assert redirect.headers['location'] == '/yolo/?embedded=1'


@pytest.mark.parametrize('error,status', [(httpx.ConnectError, 503), (httpx.ReadTimeout, 504)])
def test_camera_proxy_reports_unavailable_service(error, status):
    def upstream(request):
        raise error('fixture unavailable', request=request)

    app = FastAPI()
    app.include_router(router)
    app.state.yolo_camera_client = httpx.AsyncClient(
        base_url='http://127.0.0.1:8787', transport=httpx.MockTransport(upstream))
    with TestClient(app) as client:
        response = client.get('/yolo/api/health')
        assert response.status_code == status
        assert response.json()['ok'] is False
