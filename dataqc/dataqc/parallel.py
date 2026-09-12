"""Ordered episode jobs on a reusable pool; completion order never defines indices."""
from concurrent.futures import ProcessPoolExecutor, as_completed
from contextlib import contextmanager
from contextvars import ContextVar
import multiprocessing
import os

_EXECUTOR=ContextVar('conversion_executor',default=None)
THREAD_ENV=('OMP_NUM_THREADS','OPENBLAS_NUM_THREADS','MKL_NUM_THREADS','NUMEXPR_NUM_THREADS',
            'VECLIB_MAXIMUM_THREADS','BLIS_NUM_THREADS','OPENCV_FFMPEG_THREADS','DATAQC_WORKER_THREADS')


def worker_init(device):
    import cv2
    import pyarrow as pa
    from .video_encoding import _DEVICE
    cv2.setNumThreads(1)
    pa.set_cpu_count(1);pa.set_io_thread_count(1)
    _DEVICE.set(str(device))


@contextmanager
def episode_pool(workers,device='0'):
    # Called by an isolated coordinator whose environment is set before importing numpy.
    worker_init(device)
    if workers<=1:
        yield
        return
    with ProcessPoolExecutor(max_workers=workers,mp_context=multiprocessing.get_context('spawn'),
                             initializer=worker_init,initargs=(device,)) as executor:
        token=_EXECUTOR.set(executor)
        try:yield
        finally:_EXECUTOR.reset(token)


def invoke(task):
    function,payload=task
    return os.getpid(),function(payload)


def ordered_map(function,payloads,progress=lambda _:None,label='处理'):
    payloads=list(payloads)
    executor=_EXECUTOR.get()
    if executor is None:
        result=[]
        for i,payload in enumerate(payloads):
            progress(f'{label} {i+1}/{len(payloads)} · PID {os.getpid()}')
            result.append(function(payload))
        return result
    futures={executor.submit(invoke,(function,payload)):i for i,payload in enumerate(payloads)}
    result=[None]*len(payloads)
    try:
        for completed,future in enumerate(as_completed(futures),1):
            pid,value=future.result();result[futures[future]]=value
            progress(f'{label} {completed}/{len(payloads)} · PID {pid}')
    except BaseException:
        for future in futures:future.cancel()
        raise
    return result
