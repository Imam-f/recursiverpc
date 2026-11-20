"""Generated server script and its shutdown protocol."""

SHUTDOWN_TOKEN = b"__recursiverpc_shutdown__\n"

SERVER_SCRIPT = r"""\
if __name__ == "__main__":
    import sys
    import os
    import atexit
    import shutil
    
    here = os.path.dirname(__file__)
    os.chdir(os.environ.get("RECURSIVERPC_WORKDIR", here))

    def rmdir():
        shutil.rmtree(here, ignore_errors = True)
    atexit.register(rmdir)

    sys.path.insert(0, here)
    from $SERVER_MODULE$ import $SERVER_CLASS$ as ServerCls
    from $SERVICE_MODULE$ import $SERVICE_CLASS$ as ServiceCls

    logger = None

    $EXTRA_SETUP$
    
    t = ServerCls(ServiceCls, hostname = "localhost", port = 0, reuse_addr = True, logger = logger)
    thd = t._start_in_thread()

    sys.stdout.write(f"{t.port}\n")
    sys.stdout.flush()

    def watch_shutdown():
        # Some SSH/uv launchers expose EOF immediately. EOF is not evidence
        # that the RPC connection has ended, so only an explicit token stops us.
        # Read the fd directly: a daemon must not hold stdin's buffered IO lock
        # when OneShotServer exits after its client disconnects.
        pending = b""
        token = b"__recursiverpc_shutdown__\n"
        try:
            while True:
                chunk = os.read(sys.stdin.fileno(), 1024)
                if not chunk:
                    return
                pending += chunk
                if token in pending:
                    t.close()
                    return
                pending = pending[-len(token):]
        except (OSError, ValueError):
            return

    import threading
    threading.Thread(target=watch_shutdown, daemon=True).start()
    try:
        # Returning from main would begin Python's threading/futures shutdown
        # even if worker or SSH threads keep the process alive.
        thd.join()
    finally:
        t.close()
        thd.join(2)
"""
