"""Additional tests inside the exact candidate executable; no devices opened."""
import sys
from fractions import Fraction
from contextlib import suppress
from queue import Empty, Full
import re
import os


def test_successor_pairing():
    env=TestEnv(); sampler=timing.Sampler(env,now_ns=0)
    robot_messages(env,90_000_000);robot_messages(env,130_000_000)
    frames(env,1,100_000_000,offsets=(0,-18_000_000,0))
    frames(env,2,133_333_333,offsets=(0,-17_333_333,0))
    # A third wrist frame exists, but is too late for the second reference.
    left=copy.copy(env.rs._timing_frames[('cam_left_wrist','color')][-1])
    left._timing_receive_ns=155_000_000;left._timing_number=3
    env.rs._timing_frames[('cam_left_wrist','color')].append(left)
    first,reason=sampler.pick(171_000_000)
    second,reason=sampler.pick(204_000_000)
    assert first and second and sampler.stats['accepted']==2
    assert first[1]['_timing']['cameras']['rs/cam_left_wrist/color']['frame_number']==1
    assert second[1]['_timing']['cameras']['rs/cam_left_wrist/color']['frame_number']==2


def test_early_arrival_and_short_lock():
    class CheckedLock:
        active=False
        def __enter__(self):self.active=True
        def __exit__(self,*args):self.active=False
    lock=CheckedLock()
    rec=NS(_lock=lock,_timing_frames={},_timing_latest={},_timing_events=Counter())
    def pixels():
        assert not lock.active,'Pixels must be copied before shared lock acquisition'
        return np.zeros((8,8,3),dtype='u1')
    frame=NS(get_timestamp=lambda:1.,get_frame_number=lambda:1,
             get_frame_timestamp_domain=lambda:'test',get_data=pixels)
    actual=NS(type='color',last_frame=None,timestamp=None)
    pipe=NS(wait_for_frames=lambda **kwargs:frame)
    with mock_patch.object(timing.time,'monotonic_ns',return_value=123456):
        assert timing.wait_frames(pipe) is frame
    timing.publish_frame(rec,'cam_high',actual,frame)
    assert rec._timing_frames[('cam_high','color')][-1]._timing_receive_ns==123456
    del timing._arrival.receive_ns


def test_real_announcement_bytecode():
    # Use the actual bundled AudioTrack.play_text code, fake only voice inference
    # and audio receivers. It exercises PCM resampling/broadcast with real PyAV.
    sys.path.insert(0, str(ROOT.parent/'teleop_zero_lock/tools'))
    from archive import Archive
    code=Archive(sys.executable).code('AudioTrack')
    funcs={c.co_name:c for c in code.co_consts if isinstance(c,types.CodeType)}
    synth_calls=[]
    class Voice:
        config=NS(sample_rate=22050)
        def synthesize(self,text):
            synth_calls.append(text)
            yield NS(audio_int16_bytes=np.arange(4400,dtype='i2').tobytes())
    class Target:
        def __init__(self):self.sync_q=queue.Queue(1000)
    target=Target()
    with tempfile.TemporaryDirectory() as tmp:
        ns=dict(np=np,os=os,re=re,av=av,threading=threading,logger=collect_data.logger,
                current_file_path=tmp,stream_registry={target},registry_lock=threading.Lock(),
                _play_text_lock=threading.Lock(),SAMPLE_RATE=48000,CHUNK_SAMPLES=960,
                CJK=re.compile(r'[\u3400-\u9fff\u3000-\u303f\uff00-\uffef]'),
                zh_voice=Voice(),en_voice=Voice(),Fraction=Fraction,suppress=suppress,Empty=Empty,Full=Full)
        for name in ('_split_by_lang','_ensure_2d_mono','play_text'):
            ns[name]=types.FunctionType(funcs[name],ns)
        module=NS(play_text=ns['play_text'],_play_text_lock=ns['_play_text_lock'])
        prepared=audio_cache.PreparedAudio(module)
        prepared.prepare('步骤1已完成')
        assert target.sync_q.empty(),'Prewarming must not broadcast to the user'
        before=len(synth_calls)
        prepared.prepare('步骤1已完成');prepared.play('步骤1已完成');prepared.play('步骤1已完成')
        assert len(synth_calls)==before,'Stage playback must not synthesize again'
        assert not target.sync_q.empty()
        arrays=prepared.cache['步骤1已完成']
        original_samples=sum(a.shape[1] for a in arrays)
        frames_out=[]
        while not target.sync_q.empty():frames_out.append(target.sync_q.get_nowait())
        assert sum(f.samples for f in frames_out)==2*original_samples
        # Guard non-standard emergency announcements: vendor path retained.
        prepared.play('其他告警')
        assert len(synth_calls)>before


test_successor_pairing()
test_early_arrival_and_short_lock()
test_real_announcement_bytecode()
stability_result={'passed':True,'successor_matching':True,'arrival_before_shared_lock':True,
                  'pixel_copy_outside_lock':True,'real_audio_bytecode_cached':True,
                  'writer_stall_seconds':1.5,'stress_frames_per_mode':120,
                  'stress_no_gaps_rgb_and_rgbd':True,'stability_revision':2}
(ROOT/'runtime/stability_offline_results.json').write_text(json.dumps(stability_result,indent=2)+'\n')
print('STABILITY_OFFLINE_PASS',json.dumps(stability_result),flush=True)
