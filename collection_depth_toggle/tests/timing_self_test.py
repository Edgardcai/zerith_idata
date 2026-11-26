"""Runs inside candidate binary, with camera/ZCM constructors denied."""
from collections import Counter, deque
import copy
import logging
from unittest.mock import patch as mock_patch


class TestEnv:
    get_observation = real_env.Real_Env.get_observation
    get_action = real_env.Real_Env.get_action
    def __init__(self, depth=False):
        self._record_depth = depth
        self.rs = NS(_lock=threading.RLock(), _timing_frames={},
                     _timing_latest={}, _timing_events=Counter())
        self.image_recorder = NS(rs=self.rs, list_all=lambda: timing.CAMERAS)
        self._timing_state_lock = threading.Lock()
        self._timing_messages = {k: deque(maxlen=256) for k in timing.ALL_FIELDS}
        self._timing_message_seq = Counter()
        self._buttons = 0
    def get_camera_matched(self):return {camera: True for camera in timing.CAMERAS}
    def has_comm_board_error(self):return False
    def has_motor_error(self):return False
    def get_vr_buttons(self):
        self._buttons += 1
        return False,False,False,self._buttons in (12,24)


def robot_messages(env, ns, value=1):
    widths={'upper_joint':14,'gripper':2,'chassis':2,'waist':3,'head':2}
    with env._timing_state_lock:
        for field in timing.ALL_FIELDS:
            part = field.rsplit('_', 1)[0]
            width = widths.get(part, 7)
            msg=NS(position=[value]*width, position_actual=[value]*width,
                   speed=[value]*width,speed_actual=[value]*width,torque_actual=[value]*width,
                   PositionData=[value]*3,RotationData=[0,0,0,1])
            env._timing_message_seq[field] += 1
            env._timing_messages[field].append((ns,env._timing_message_seq[field],msg))


def frames(env, number, ns, offsets=(0,5_000_000,-4_000_000), skip=()):
    for camera, offset in zip(timing.CAMERAS,offsets):
        if camera in skip:continue
        for kind in (('color','depth') if env._record_depth else ('color',)):
            snap=NS(last_frame=np.full((48,64,3),number%255,dtype='u1') if kind=='color'
                    else np.full((48,64),number,dtype='u2'),fmt='bgr8' if kind=='color' else 'z16',
                    timestamp=1789110000000+number*1000/30+offset/1e6,
                    _timing_epoch=0,_timing_number=number,_timing_domain='system_time',
                    _timing_receive_ns=ns+offset)
            with env.rs._lock:
                env.rs._timing_frames.setdefault((camera[3:],kind),deque(maxlen=8)).append(snap)


def test_sampler():
    env=TestEnv(); sampler=timing.Sampler(env,now_ns=0)
    robot_messages(env,99_000_000,1)
    robot_messages(env,101_000_000,99)  # Future messages must not leak backwards.
    frames(env,1,100_000_000)
    result,reason=sampler.pick(170_000_000)
    assert result and reason is None
    act,obs=result
    assert act['arm']['position']==[1]*14 and obs['state']['arm']['position']==[1]*14
    assert len(act['end']['position'])==14  # Preserve factory auxiliary poses.
    assert sampler.pick(171_000_000)[0] is None  # Cannot emit same main frame twice.
    robot_messages(env,132_000_000,2)
    frames(env,2,133_333_333,skip=('rs/cam_left_wrist',))
    assert sampler.pick(205_000_000)[0] is None
    assert sampler.stats['rejected']==1
    robot_messages(env,165_000_000,3);frames(env,3,166_666_666)
    assert sampler.pick(238_000_000)[0]
    # State source missing, bad shapes and stale values must not silently pass.
    env._timing_messages['waist_state'].clear()
    frames(env,4,200_000_000)
    assert sampler.pick(271_000_000)[0] is None
    # Clock-reset epoch cannot be crossed inside an episode.
    robot_messages(env,232_000_000,5);frames(env,5,233_333_333)
    env.rs._timing_frames[('cam_high','color')][-1]._timing_epoch=1
    assert sampler.pick(305_000_000)[1]=='camera_clock_reset'
    # Static pixels are legitimate new frames, not image-hash duplicates.
    env2=TestEnv();s2=timing.Sampler(env2,now_ns=0)
    for i in (1,2):
        ns=i*100_000_000
        robot_messages(env2,ns-1_000_000,1);frames(env2,i,ns)
        for h in env2.rs._timing_frames.values():h[-1].last_frame[:]=0
        assert s2.pick(ns+70_000_000)[0]
    return result


def test_atomic_publication():
    rec=TestEnv().rs
    actual=NS(type='color',last_frame=None,timestamp=None,fmt='bgr8')
    data=np.ones((3,3,3),dtype='u1')
    frame=NS(get_timestamp=lambda:1000.,get_frame_number=lambda:1,
             get_frame_timestamp_domain=lambda:'system_time',get_data=lambda:data)
    timing.publish_frame(rec,'cam_high',actual,frame)
    first=rec._timing_frames[('cam_high','color')][-1]
    data[:]=7
    assert np.all(first.last_frame==1) and not first.last_frame.flags.writeable
    timing.publish_frame(rec,'cam_high',actual,frame)
    assert len(rec._timing_frames[('cam_high','color')])==1
    frame.get_timestamp=lambda:1033.;frame.get_frame_number=lambda:2
    timing.publish_frame(rec,'cam_high',actual,frame)
    assert first.timestamp==1000. and np.all(first.last_frame==1)
    assert actual.timestamp==1033. and np.all(actual.last_frame==7)
    frame.get_timestamp=lambda:20.;frame.get_frame_number=lambda:1
    timing.publish_frame(rec,'cam_high',actual,frame)
    assert rec._timing_frames[('cam_high','color')][-1]._timing_epoch==1
    assert rec._timing_events['clock_or_stream_reset']==1


def test_cancel():
    env=TestEnv(); collector=NS(env=env,config={},fps=30,discard_evt=threading.Event(),
                              _timing_logger=logging.getLogger('test'))
    stopped=threading.Event();stopped.set();start=time.monotonic()
    assert timing.prepare(collector,stopped) is False
    assert time.monotonic()-start<.1


def test_callbacks():
    env=TestEnv()
    msg=NS(position_actual=[1]*14)
    real_env.Real_Env._upper_joint_state_handler(env,'channel',msg)
    msg.position_actual[0]=999
    assert env.upper_joint_state is msg  # Factory assignment untouched.
    assert env._timing_messages['upper_joint_state'][-1][2].position_actual[0]==1


def test_metadata(sample):
    action,obs=sample
    with tempfile.TemporaryDirectory() as d:
        with h5py.File(Path(d)/'x.h5','w') as f:
            timing.write_metadata(f,[(obs,action)],0)
            assert f['timing/reference_receive_ns'][0]==obs['_timing']['reference_receive_ns']
            assert f['timing/messages/upper_joint_control/receive_ns'][0]==99_000_000
            try:timing.write_metadata(f,[(obs,action)],0)
            except RuntimeError:pass
            else:raise AssertionError('duplicate metadata batch was allowed')


def test_full_capture(depth, stress=False):
    # Exercise the real patched producer, original safety/stage logic, actual
    # save-worker and JPEG/HDF5/video writer. Only sources and speech are mocked.
    env=TestEnv(depth)
    target_frames = 120 if stress else 24
    if stress:
        def stage_buttons():
            env._buttons += 1
            return False, False, False, env._buttons in (60, 120)
        env.get_vr_buttons = stage_buttons
    stopped=threading.Event()
    failure=[]
    def feeder():
        try:
            number=0;target=time.monotonic()
            while not stopped.is_set():
                ns=time.monotonic_ns()
                robot_messages(env,ns-1_000_000,number/100)
                number+=1;frames(env,number,ns,offsets=(0,0,0))
                target+=1/30;stopped.wait(max(0,target-time.monotonic()))
        except BaseException as exc:failure.append(exc)
    with tempfile.TemporaryDirectory(prefix='timing-capture-',dir=ROOT/'runtime') as d:
        c=object.__new__(collect_data.EpisodeDataCollector)
        c.env=env;c.task_id=1;c.task_name='timing_test';c.subtask_num=2;c.fps=30
        c.max_episode_length=300;c._storage_base=d;c._min_disk_gb=0;c._disk_check_mem=[0,False]
        c.config={'record_depth':depth,'task_name':c.task_name,'robot_id':'test','collector_id':0,
                  'collector_name':'offline'}
        c.discard_evt=threading.Event();c._stop_evt=threading.Event()
        c.event_queue=NS(sync_q=queue.Queue())
        c._camera_error_flag=c._motor_error_flag=c._comm_board_error_flag=False
        uid='a'*32
        thread=threading.Thread(target=feeder);thread.start()
        original_writer=collect_data._parallel_save_batch
        delayed=[];high_water=[]
        class MeasuredQueue(queue.Queue):
            def put(self,*args,**kwargs):
                result=super().put(*args,**kwargs)
                high_water.append(self.qsize())
                return result
        def slow_writer(*args,**kwargs):
            if stress and not delayed:
                delayed.append(True)
                time.sleep(1.5)
            return original_writer(*args,**kwargs)
        try:
            with mock_patch.object(collect_data,'play_text',lambda *args:None), \
                 mock_patch.object(collect_data,'_parallel_save_batch',slow_writer), \
                 mock_patch.object(collect_data,'queue',NS(Queue=MeasuredQueue,Empty=queue.Empty,Full=queue.Full)):
                c._capture_episode(uid)
                # Wait for final worker metadata, not just producer exit.
                deadline=time.monotonic()+10
                finished=None
                while time.monotonic()<deadline:
                    try:msg=json.loads(c.event_queue.sync_q.get(timeout=.2))
                    except queue.Empty:continue
                    if msg.get('event_type')=='EPISODE_END':finished=msg;break
                assert finished and finished['results']['is_success'],finished
        finally:
            stopped.set();thread.join(2)
        assert not failure,failure
        path=Path(d)/'1_timing_test'/uid
        with h5py.File(path/'episode.hdf5','r') as f:
            t=f['timestamp/t'][:]
            assert len(t)==target_frames,len(t)
            assert np.all(np.diff(t)>0)
            assert f['timestamp'].attrs['source']==timing.MASTER
            assert len(f['timing/reference_receive_ns'])==len(t)
            for cam in timing.CAMERAS:
                nums=f['timing/cameras/'+cam+'/color/frame_number'][:]
                assert np.all(np.diff(nums)>0)
                assert len(f['observation/images/'+cam+'/color'])==len(t)
            assert bool(f.attrs['depth_recorded'])==depth
            if stress:
                assert not c._timing_sampler.stats.get('rejected'),dict(c._timing_sampler.stats)
                assert max(high_water)>30,high_water
                assert np.max(np.diff(t))<50,np.diff(t)
        meta=json.loads((path/'episode_meta.json').read_text())
        # The factory retains an empty trailing stage; the existing validator
        # filters it. Preserve that behavior and check the two nonempty stages.
        steps=[s for s in meta['step_index'] if s['end_frame_id']>=s['start_frame_id']]
        assert [(s['start_frame_id'],s['end_frame_id']) for s in steps]==[(0,target_frames//2-1),(target_frames//2,target_frames-1)],steps


sample=test_sampler()
test_atomic_publication();test_cancel();test_callbacks();test_metadata(sample)
test_full_capture(False);test_full_capture(True)
test_full_capture(False,stress=True);test_full_capture(True,stress=True)
timing_results={'passed':True,'fresh_frames':True,'causal_state_and_action':True,
                'missing_camera_state_and_clock_reset_rejected':True,'atomic_camera_snapshot':True,
                'real_producer_writer_rgb_and_depth':True,'stage_indices_preserved':True,
                'robot_commands_sent':False}
(ROOT/'runtime/timing_offline_results.json').write_text(json.dumps(timing_results,indent=2)+'\n')
print('TIMING_OFFLINE_PASS',json.dumps(timing_results),flush=True)
