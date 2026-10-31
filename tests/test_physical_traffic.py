"""The scoped RPC exposes two lifetime counters, never raw sync state."""
import base64
import io
import json
from pathlib import Path
import sys
import urllib.error
import urllib.request
import pytest
from mediavpn.seedbox import RPC_CODE, ALLOWED_READS


@pytest.mark.parametrize('path',['transfer/info','mediaseed/status'])
@pytest.mark.parametrize('totals',[{'alltime_ul':123,'alltime_dl':456},{'alltime_ul':0,'alltime_dl':0},{'alltime_ul':True,'alltime_dl':-1},None])
def test_only_valid_lifetime_counters_are_added_without_new_read_permissions(monkeypatch,capsys,path,totals):
    calls=[]
    class Opener:
        def open(self,request,timeout):
            endpoint=request.full_url.split('/api/v2/')[1];calls.append(endpoint)
            assert request.data is None or endpoint=='auth/login'
            if endpoint.startswith('sync/maindata'):
                if totals is None:raise urllib.error.URLError('synthetic unavailable')
                payload={'server_state':{**totals,'private_path':'secret'},'torrents':{'private':'secret'}}
            elif endpoint=='transfer/info':payload={'up_info_data':12,'dl_info_data':34,'up_info_speed':56}
            elif endpoint=='torrents/info?limit=500':payload=[]
            elif endpoint=='app/preferences':payload={}
            else:
                response=io.BytesIO(b'v5.2.0' if endpoint=='app/version' else b'Ok.');response.status=200;return response
            response=io.BytesIO(json.dumps(payload).encode());response.status=200;return response
    monkeypatch.setattr(urllib.request,'build_opener',lambda *args:Opener())
    original=Path.read_text
    monkeypatch.setattr(Path,'read_text',lambda self,*args,**kwargs:'synthetic-password' if self.as_posix()=='/secrets/qbit-password' else original(self,*args,**kwargs))
    monkeypatch.setattr(sys,'argv',['rpc',base64.b64encode(json.dumps({'path':path,'method':'GET','payload':{}}).encode()).decode()])
    exec(compile(RPC_CODE,'rpc','exec'),{})
    result=json.loads(capsys.readouterr().out);assert result['status']==200
    response=json.loads(base64.b64decode(result['body']))
    if path=='mediaseed/status':response=json.loads(base64.b64decode(response['transfer/info']['body']))
    assert response['up_info_speed']==56 and response['up_info_data']==12
    for key in ['alltime_ul','alltime_dl']:
        if totals and type(totals[key]) is int and totals[key]>=0:assert response[key]==totals[key]
        else:assert key not in response
    assert 'secret' not in json.dumps(response)
    assert calls.count('sync/maindata?rid=0')==1
    assert 'sync/maindata' not in ALLOWED_READS
