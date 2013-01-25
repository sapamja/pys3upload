import os
import sys
import time
import threading
import contextlib
import Queue
from multiprocessing import pool
try:
    import cStringIO
    StringIO = cStringIO
except ImportError:
    import StringIO


class ThreadPoolError(Exception):
    def __init__(self, thread, exception):
        self.thread = thread
        self.inner_exception = exception
    def __str__(self):
        return repr(self.thread) + ' ' + repr(self.inner_exception)

class ThreadSafeCounter():
    def __init__(self):
        self.lock = threading.Lock()
        self.counter = 0

    def increment(self):
        with self.lock:
            self.counter += 1

    def decrement(self):
        with self.lock:
            self.counter -= 1

def data_collector(iterable, def_buf_size=5242880):
    ''' Buffers n bytes of data

        Args:
            iterable: could be a list, generator or string
            def_buf_size: number of bytes to buffer, default is 5mb

        Returns:
            A generator object
    '''
    buffer = ''
    for data in iterable:
        buffer += data
        if len(buffer) >= def_buf_size:
            output = buffer[:def_buf_size]
            buffer = buffer[def_buf_size:]
            yield output
    if len(buffer) > 0:
        yield buffer

def upload_part(upload_func, progress_cb, part_no, part_data):
        num_retries = 5
        def _upload_part(retries_left=num_retries):
            try:
                with contextlib.closing(StringIO.StringIO(part_data)) as f:
                    f.seek(0)
                    cb = lambda c,t:progress_cb(part_no, c, t) if progress_cb else None
                    upload_func(f, part_no, cb=cb, num_cb=100)
            except Exception, exc:
                retries_left -= 1
                if retries_left > 0:
                    return _upload_part(retries_left=retries_left)
                else:
                    return ThreadPoolError(threading.current_thread(), exc)
        return _upload_part()

def uploader(bucket, aws_access_key, aws_secret_key,
             iterable, key, progress_cb=None,
             parallelism=5, replace=False, secure=True,
             buffer=data_collector,
             connection=None):

    if not connection:
        from boto.s3 import connection

    c = connection.S3Connection(aws_access_key, aws_secret_key, is_secure=secure)
    b = c.get_bucket(bucket)

    if not replace and b.lookup(key):
        raise Exception('s3 key ' + key + ' already exists')    

    multipart_obj = b.initiate_multipart_upload(key)
    err_queue = Queue.Queue()
    in_upload = ThreadSafeCounter()

    try:
        tpool = pool.ThreadPool(processes=parallelism)

        def check_errors():
            try:
                exc = err_queue.get(block=False)
            except Queue.Empty:
                pass
            else:
                raise exc

        def waiter():
            while in_upload.counter >= parallelism:
                check_errors()
                time.sleep(0.5)

        def cb(err):
            if err: err_queue.put(err)
            in_upload.decrement()

        args = [multipart_obj.upload_part_from_file, progress_cb]

        for part_no, part in enumerate(buffer(iterable)):
            part_no += 1
            tpool.apply_async(upload_part, args + [part_no, part], callback=cb)
            in_upload.increment()
            waiter()

        tpool.close()
        tpool.join()
        # Check for thread errors before completing the upload,
        # sometimes an error can be left unchecked until we
        # get to this point.
        check_errors()
        multipart_obj.complete_upload()
    except:
        multipart_obj.cancel_upload()
        tpool.terminate()
        raise

if __name__ == '__main__':
    from optparse import OptionParser
    parser = OptionParser()
    parser.add_option('-b', '--bucket', dest='bucket',
                      help='the s3 bucket to upload to')
    parser.add_option('-k', '--key', dest='key',
                      help='the name of the key to create in the bucket')
    parser.add_option('-K', '--aws_key', dest='aws_key',
                      help='aws access key')
    parser.add_option('-s', '--aws_secret', dest='aws_secret',
                      help='aws secret key')
    parser.add_option('-d', '--data', dest='data',
                      help='the data to upload to s3 -- if left blank will be read from STDIN')
    (options, args) = parser.parse_args()

    if len(sys.argv) < 2:
        parser.print_help()
        sys.exit(0)

    if not options.bucket:
        parser.error('bucket not provided')
    if not options.key:
        parser.error('key not provided')
    if not options.aws_key:
        parser.error('aws access key not provided')
    if not options.aws_secret:
        parser.error('aws secret key not provided')

    data = sys.stdin if not options.data else [options.data]

    def cb(part_no, uploaded, total):
        print part_no, uploaded, total

    uploader(options.bucket, options.aws_key, options.aws_secret, data, options.key,
             progress_cb=cb, replace=True)