import asyncio
import logging
from pathlib import Path
import queue
import threading
import docopt
from clapshot_server import database, api_server, video_processor

# TODO:
"""
# Support Ctrl+C
def signal_handler(signal, frame):
    global interrupt_flag
    if not interrupt_flag.is_set():
        logger.info("SIGINT received, exiting...")
        interrupt_flag.set()
signal.signal(signal.SIGINT, signal_handler)


...


        interrupt_flag = threading.Event()
        result_queue = queue.SimpleQueue()

        print(f"Starting monitor at {now()}...")
        p = threading.Thread(
            target=VideoProcessor.monitor_incoming_folder_loop,
            args=(vp, incoming_dir, dst_dir, interrupt_flag, result_queue, 0.1))
        p.start()
        time.sleep(1)
        
        print(f"Copying '{src}' to '{incoming_dir}' at {now()}...")
        shutil.copy(src, incoming_dir / src.name)
        shutil.copy(src_garbage, incoming_dir / src_garbage.name)
        time.sleep(1)
        
        print(f"Stopping monitor at {now()}...")
        interrupt_flag.set()
        print(f"Waiting for monitor & children to stop at {now()}...")
        p.join()

        # Check that both ok and corrupted files were processed
        res_ok = result_queue.get_nowait()
"""



def main():
    """
    Clapshot server -- backend of a video annotation tool.

    Monitors <path>/incoming for new videos, processes them, and stores them in <path>/videos.
    Then serves the annotations and comments via an asyncronous HTTP + Socket.IO API.
    Use a proxy server to serve files in /videos and to secure the API with HTTPS/WSS.

    Usage:
      clapshot-server [options] (--url-base=URL) (--data-dir=PATH)
      clapshot-server [options] [--mute TOPIC]... (--url-base=URL) (--data-dir=PATH)
      clapshot-server (-h | --help)

    Required:
     --url-base=URL       Base URL of the API server, e.g. https://example.com/clapshot/.
                          This depends on your proxy server configuration.
     --data-dir=PATH      Directory for database, /incoming, /videos and /rejected

    Options:
     -p PORT --port=PORT    Port to listen on [default: 8086]
     -H HOST --host=HOST    Host to listen on [default: localhost]
     --host-videos          Host the /videos directory [default: False]
                            (For debugging. Use Nginx or Apache with auth in production.)
     -P SEC --poll SEC      Polling interval for incoming folder [default: 3.0]
     -m TOPIC --mute TOPIC    Mute logging for a topic (can be repeated). Sets level to WARNING.
                            See logs logs for available topics.
     -l FILE --log FILE     Log to file instead of stdout
     -d --debug             Enable debug logging
     -h --help              Show this screen
    """
    args = docopt.docopt(main.__doc__)

    logging.basicConfig(
        level = (logging.DEBUG if args["--debug"] else logging.INFO),
        format='%(asctime)s %(name)-12s %(levelname)-8s %(message)s',
        datefmt='%m-%d %H:%M:%S',
        filename=args["--log"] or None,
        filemode='w' if args["--log"] else None
    )
    logger = logging.getLogger("clapshot")

    # Mute logging for some topics
    for topic in args["--mute"] or []:
        logging.getLogger(topic).setLevel(logging.WARNING)

    # Make sure data dir exists
    data_dir = Path(args["--data-dir"])
    if not (data_dir.exists() and data_dir.is_dir()):
        logger.error(f"Data directory '{data_dir}' does not exist")
        return 1

    incoming_dir = data_dir / "incoming"
    videos_dir = data_dir / "videos"
    rejected_dir = data_dir / "rejected"
    for d in (incoming_dir, videos_dir, rejected_dir):
        d.mkdir(exist_ok=True)

    url_base = args["--url-base"]
    assert url_base

    db_file = data_dir / "clapshot.sqlite"
    vp_interrupt_flag = threading.Event()

    async def go():
        vp_result_queue = queue.SimpleQueue()
        push_message_queue = asyncio.Queue()

        # Run file monitor in a thread
        vp = video_processor.VideoProcessor(db_file, logger)
        vp_thread = threading.Thread(
            target=video_processor.VideoProcessor.monitor_incoming_folder_loop,
            args=(vp, incoming_dir, videos_dir, rejected_dir, vp_interrupt_flag, vp_result_queue, float(args["--poll"])))
        vp_thread.start()

        # Run API server with asyncio forever
        async def run_api_server():
            await api_server.run_server(
                db=database.Database(
                    db_file,
                    logging.getLogger("db")),
                logger=logging.getLogger("api"),
                url_base=url_base,
                port=int(args["--port"]),
                push_messages=push_message_queue,
                serve_dirs={'/video': videos_dir} if args["--host-videos"] else {})

        async def vp_result_deliverer():
            while not vp_interrupt_flag.is_set() and vp_thread.is_alive():
                await asyncio.sleep(0.5)
                if not vp_result_queue.empty():
                    vp_res = vp_result_queue.get()  # type: video_processor.VideoProcessorResult
                    await push_message_queue.put(api_server.CustomUserMessage(
                        event_name = 'info',
                        user_id = vp_res.file_owner_id,
                        fields = {
                            'msg': vp_res.msg,
                            'orig_file': vp_res.orig_file.name,
                            'video_hash': vp_res.video_hash,
                            'severity': 'success' if vp_res.success else 'error'
                            }))
                    # TODO: Slack, too

        
        try:
            task_api = asyncio.create_task(run_api_server())
            task_msg = asyncio.create_task(vp_result_deliverer())
            while vp_thread.is_alive() and \
                  not vp_interrupt_flag.is_set() and \
                  not task_api.done() and \
                  not task_msg.done():
                await asyncio.sleep(0.2)

        except KeyboardInterrupt:
            pass
        finally:
            vp_interrupt_flag.set()
            vp_thread.join()

        logger.info("API server stopped")

    asyncio.run(go())

if __name__ == '__main__':
    main()
