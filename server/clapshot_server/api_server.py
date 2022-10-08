"""
Websocket (Socket.IO) based API server for Clapshot.

Listens to connections from web UI, runs commands from users,
communicates with database and video ingestion pipeline.
Pushes video processing results to clients.

**Relies on reverse proxy for authentication**! Specifically,
it belives anything that it gets from the
``HTTP_X_REMOTE_USER_ID`` and ``HTTP_X_REMOTE_USER_NAME``
headers. Proxy is expected to authenticate users in some
manner (e.g. Kerberos agains AD) and set these headers.

If specified, also serves video files, but this is mainly for testing.
Nginx or Apache should be used in production for that.
"""

import asyncio
from decimal import Decimal
import hashlib
import logging
from typing import Any, Callable
from aiohttp import web
from uuid import uuid4
from pathlib import Path
import socketio
import urllib.parse
import aiofiles
import shutil
from datauri import DataURI

from .database import Database, Video, Comment, Message

sio = socketio.AsyncServer(async_mode='aiohttp', cors_allowed_origins='*')
SOCKET_IO_PATH = '/api/socket.io'

@web.middleware
async def _cors_middleware(request, handler):
    response = await handler(request)
    response.headers['Access-Control-Allow-Origin'] = '*'
    return response


class ClapshotApiServer:
    """
    Socket.io server to communicate with web client
    """

    def __init__(
            self, db: Database, 
            url_base: str,
            port: int,
            logger: logging.Logger,
            videos_dir: Path,
            upload_dir: Path,
            serve_dirs: dict[str, Path],
            ingest_callback: Callable[str, str]):
        self.db = db
        self.url_base = url_base
        self.port = port
        self.logger = logger
        self.videos_dir = videos_dir
        self.upload_dir = upload_dir
        self.serve_dirs = serve_dirs
        self.ingest_callback = ingest_callback

        self.app = web.Application(middlewares=[_cors_middleware])
        sio.attach(self.app, socketio_path=SOCKET_IO_PATH)

        self.userid_to_sid: dict[str, str] = {}


        @sio.event
        async def list_my_videos(sid, msg):
            try:
                user_id, username = await self.get_user(sid)
                self.logger.info(f"list_my_videos: user_id='{user_id}' username='{username}'")
                videos = await self.db.get_all_user_videos(user_id)
                await sio.emit('user_videos', {
                    'username': username,
                    'user_id': user_id,
                    'videos': [self.dict_for_video(v) for v in videos] 
                    }, room=sid)
            except Exception as e:
                self.logger.exception(f"Exception in list_my_videos for sid '{sid}': {e}")
                await self.push_message(dont_throw=True, msg=Message(
                    event_name='error', user_id=user_id,
                    message=f"Failed to list your videos.", details=str(e)))


        async def _emit_new_comment(c: Comment, room: str):
            """
            Helper function to send comment. Reads drawing
            from file if present and encodes into data URI.
            """
            data = c.to_dict()
            if c.drawing and not c.drawing.startswith('data:'):
                path = self.videos_dir / c.video_hash / 'drawings' / c.drawing
                if path.exists():
                    async with aiofiles.open(path, 'rb') as f:
                        data['drawing'] = DataURI.make('image/webp', charset='utf-8', base64=True, data=await f.read())
                else:
                    data['comment'] += ' [DRAWING NOT FOUND]'
            await sio.emit('new_comment', data, room=room)


        @sio.event
        async def open_video(sid, msg):
            try:
                user_id, username = await self.get_user(sid)
                video_hash = msg['video_hash']
                self.logger.info(f"open_video: user_id='{user_id}' username='{username}' video_hash='{video_hash}'")

                v = await self.db.get_video(video_hash)
                if v is None:
                    await self.push_message(Message(
                        event_name='error', user_id=user_id,
                        ref_video_hash=video_hash,
                        message=f"No such video."))
                else:
                    sio.enter_room(sid, video_hash)
                    fields = self.dict_for_video(v)
                    await sio.emit('open_video', fields, room=sid)
                    for c in await self.db.get_video_comments(video_hash):
                        self.logger.debug(f"Sending to sid='{sid}' comment {c}")
                        await _emit_new_comment(c, room=sid)

            except Exception as e:
                self.logger.exception(f"Exception in lookup_video for sid '{sid}': {e}")
                await self.push_message(dont_throw=True, msg=Message(
                    event_name='error', user_id=user_id,
                    ref_video_hash=msg.get('video_hash'),
                    message=f"Failed to lookup video.", details=str(e)))

        @sio.event
        async def del_video(sid, msg):
            try:
                user_id, username = await self.get_user(sid)
                video_hash = msg['video_hash']
                self.logger.info(f"del_video: user_id='{user_id}' video_hash='{video_hash}'")
                v = await self.db.get_video(video_hash)
                if v is None:
                    await self.push_message(Message(
                        event_name='error', user_id=user_id,
                        ref_video_hash=video_hash,
                        message=f"No such video. Cannot delete."))
                else:
                    assert user_id in (v.added_by_userid, 'admin'), f"Video '{video_hash}' not owned by you. Cannot delete."
                    await self.db.del_video_and_comments(video_hash)
                    await self.push_message(persist=True, msg=Message(
                        event_name='ok', user_id=user_id,
                        ref_video_hash=video_hash,
                        message=f"Video deleted.",
                        details=f"Added by {v.added_by_username} ({v.added_by_userid}) on {v.added_time}. Filename was '{v.orig_filename}'"))
            except Exception as e:
                self.logger.exception(f"Exception in del_video for sid '{sid}': {e}")
                await self.push_message(dont_throw=True, persist=True, msg=Message(
                    event_name='error', user_id=user_id,
                    ref_video_hash=msg.get('video_hash'),
                    message= f"Failed to delete video.", details=str(e)))



        @sio.event
        async def add_comment(sid, msg):
            try:
                user_id, username = await self.get_user(sid)
                assert user_id and username
                video_hash = msg['video_hash']
                self.logger.info(f"add_comment: user_id='{user_id}' video_hash='{video_hash}', msg='{msg.get('comment')}'")

                vid = await self.db.get_video(video_hash)
                if vid is None:
                    await self.push_message(Message(
                        event_name='error', user_id=user_id,
                        ref_video_hash=video_hash,
                        message=  f"No such video. Cannot comment."))
                    return

                # Parse drawing data if present and write to file
                if drawing := msg.get('drawing'):
                    assert drawing.startswith('data:'), f"Drawing is not a data URI."
                    img_uri = DataURI(drawing)
                    assert str(img_uri.mimetype) == 'image/webp', f"Invalid mimetype in drawing."
                    ext = str(img_uri.mimetype).split('/')[1]
                    sha256 = hashlib.sha256(img_uri.data).hexdigest()
                    fn = f"{sha256[:16]}.{ext}"
                    drawing_path = self.videos_dir / video_hash / 'drawings' / fn
                    drawing_path.parent.mkdir(parents=True, exist_ok=True)
                    async with aiofiles.open(drawing_path, 'wb') as f:
                        await f.write(img_uri.data)
                    drawing = fn

                c = Comment(
                    video_hash = video_hash,
                    parent_id = msg.get('parent_id') or None,
                    user_id = user_id,
                    username = username,
                    comment = msg.get('comment') or '',
                    timecode = msg.get('timecode') or '',
                    drawing = drawing or None)

                await self.db.add_comment(c)
                await _emit_new_comment(c, room=video_hash)

            except Exception as e:
                self.logger.exception(f"Exception in add_comment for sid '{sid}': {e}")
                await self.push_message(dont_throw=True, msg=Message(
                    event_name='error', user_id=user_id,
                    ref_video_hash=msg.get('video_hash'),
                    message=f"Failed to add comment.", details=str(e)))


        @sio.event
        async def edit_comment(sid, msg):
            try:
                user_id, username = await self.get_user(sid)
                assert user_id and username
                comment_id = msg['comment_id']
                comment = str(msg['comment'])
                self.logger.info(f"edit_comment: user_id='{user_id}' comment_id='{comment_id}', comment='{comment}'")
                
                old = await db.get_comment(comment_id)
                video_hash = old.video_hash
                assert user_id in (old.user_id, 'admin'), "You can only edit your own comments"

                await self.db.edit_comment(comment_id, comment)

                await sio.emit('del_comment', {'comment_id': comment_id}, room=video_hash)
                c = await self.db.get_comment(comment_id)
                await _emit_new_comment(c, room=video_hash)

            except Exception as e:
                self.logger.exception(f"Exception in edit_comment for sid '{sid}': {e}")
                await self.push_message(dont_throw=True, msg=Message(
                    event_name='error', user_id=user_id,
                    ref_comment_id=msg.get('comment_id'),
                    ref_video_hash=msg.get('video_hash'),
                    message=f"Failed to edit comment.", details=str(e)))


        @sio.event
        async def del_comment(sid, msg):
            try:
                user_id, username = await self.get_user(sid)
                assert user_id and username
                comment_id = msg['comment_id']
                self.logger.info(f"del_comment: user_id='{user_id}' comment_id='{comment_id}'")

                old = await self.db.get_comment(comment_id)
                video_hash = old.video_hash
                assert user_id in (old.user_id, 'admin'), "You can only delete your own comments"

                all_comm = await self.db.get_video_comments(video_hash)
                for c in all_comm:
                    if c.parent_id == comment_id:
                        raise Exception("Can't delete a comment that has replies")
            
                await self.db.del_comment(comment_id)
                await sio.emit('del_comment', {'comment_id': comment_id}, room=video_hash)

            except Exception as e:
                # self.logger.exception(f"Exception in del_comment for sid '{sid}': {e}")
                await self.push_message(dont_throw=True, msg=Message(
                    event_name='error', user_id=user_id,
                    ref_comment_id=msg.get('comment_id'),
                    message=f"Failed to delete comment.", details=str(e)))


        @sio.event
        async def list_my_messages(sid, msg):
            try:
                user_id, username = await self.get_user(sid)
                assert user_id
                self.logger.info(f"list_my_messages: user_id='{user_id}'")
                msgs = await self.db.get_user_messages(user_id)
                for m in msgs:
                    await sio.emit('message', m.to_dict(), room=sid)                
                    if not m.seen:
                        await self.db.set_message_seen(m.id, True)
            except Exception as e:
                self.logger.exception(f"Exception in list_my_messages for sid '{sid}': {e}")
                # Don't push new error messages to db, as listing them failed.
                await sio.emit("message", Message(
                        event_name='error', user_id=user_id,
                        message=f"Failed to get messages.",
                        details=str(e)
                    ).to_dict(), room=sid)

        @sio.event
        async def logout(sid):
            self.logger.info(f"logout: sid='{sid}'")
            await sio.disconnect(sid)

        @sio.event
        async def connect(sid, environ):
            # Trust headers from web server / reverse proxy on user auth
            user_id, username = self.user_from_headers(environ)
            self.logger.info(f"connect: sid='{sid}' user_id='{user_id}' username='{username}'")
            
            #for k,v in environ.items():
            #    if k != 'wsgi.input':
            #        print(f"HDR: {k}={v}")

            await sio.save_session(sid, {'user_id': user_id, 'username': username})
            self.userid_to_sid[user_id] = sid
            await sio.emit('welcome', {'username': username, 'user_id': user_id}, room=sid)
            sio.enter_room(sid, 'huoneusto')

        @sio.event
        async def disconnect(sid):
            user_id = (await sio.get_session(sid)).get('user_id')
            self.logger.info(f'Client disconnected, sid={sid}, user_id={user_id}')
            self.userid_to_sid.pop(user_id, None)


        async def post_upload_file(request):
            """
            Receive a HTTP file upload from the client.
            """
            user_id, _ = self.user_from_headers(request.headers)
            assert user_id , "No user_id for upload"            
            self.logger.info(f"post_upload_file: user_id='{user_id}'")
            
            async for field in (await request.multipart()):
                if field.name != 'fileupload':
                    logger.debug(f"post_upload_file(): Skipping UNKNOWN Multipart POST field '{field.name}'")
                else:
                    filename = field.filename
                    assert str(Path(filename).name) == filename, "Filename must not contain path"
                    dst = Path(self.upload_dir) / str(uuid4()) / Path(filename).name
                    assert not dst.exists(), f"Upload dst '{dst}' already exists, even tough it was prefixed with uuid4. Bug??"

                    try:
                        logger.info(f"post_upload_file(): Saving uploaded file '{filename}' to '{dst}'")
                        dst.parent.mkdir(parents=True, exist_ok=True)

                        async with aiofiles.open(dst, 'wb') as outf:
                            inq = asyncio.Queue(8)
                            async def reader():
                                while c := await field.read_chunk():
                                    await inq.put(c)
                                await inq.put(None)
                            async def writer():
                                while c := await inq.get():
                                    await outf.write(c)
                            tasks = [reader(), writer()]
                            try:
                                await asyncio.gather(*tasks)
                            except Exception as e:
                                for t in tasks:
                                    t.cancel()
                                logger.exception(f"post_upload_file(): Exception while saving uploaded file '{filename}' to '{dst}'")
                                raise e

                    except PermissionError as e:
                        self.logger.error(f"post_upload_file(): Permission error saving '{filename}' to '{dst}': {e}")
                        return web.Response(status=500, text="Permission error saving upload")

                    self.logger.info(f"post_upload_file(): File saved to '{dst}'. Queueing for processing.")
                    if self.ingest_callback:
                        self.ingest_callback(dst, user_id)

                    return web.Response(status=200, text="Upload OK")

                return web.Response(status=400, text="No fileupload field in POST")


        # Register HTTP routes
        self.app.router.add_post('/api/upload', post_upload_file)
        for route, path in self.serve_dirs.items():
            self.app.router.add_static(route, path)


    async def push_message(self, msg: Message, dont_throw=False, persist=False):
        """
        Push a message to the database and emit it to all clients.
        Set dont_throw if this is called from an exception handler.
        """
        if persist:
            try:
                msg = await self.db.add_message(msg) # Also sets id and timestamp
            except Exception as e:
                self.logger.error(f"Exception in push_message while persisting: {e}")
                if not dont_throw:
                    raise
        try:
            if msg.user_id in self.userid_to_sid:
                await sio.emit("message", msg.to_dict(), room=self.userid_to_sid[msg.user_id])
        except Exception as e:
            self.logger.error(f"Exception in push_message while emitting: {e}")
            if not dont_throw:
                raise

    def dict_for_video(self, v: Video) -> dict:
        video_url = self.url_base.rstrip('/') + f'/video/{v.video_hash}/' + (
            'video.mp4' if v.recompression_done else
            ('orig/'+urllib.parse.quote(v.orig_filename, safe='')))

        return {
                'orig_filename': v.orig_filename,
                'video_hash': v.video_hash,
                'video_url': video_url,
                'fps': str(round(Decimal(v.fps), 3)),  # eg. 23.976
                'added_time': str(v.added_time.isoformat()),
                'duration': v.duration,
                'username': v.added_by_username,
                'user_id': v.added_by_userid
                }


    def user_from_headers(self, headers: Any) -> tuple[str, str]:
        """
        Get user id and username from (reverse proxy's) headers.

        return: (user_id, username)
        """
        user_id = headers.get('HTTP_X_REMOTE_USER_ID') or headers.get('X-Remote-User-Id') or headers.get('X-REMOTE-USER-ID')
        if not user_id:
            self.logger.warning("No user id found in header X-REMOTE-USER-ID, using 'anonymous'")
        user_name = headers.get('HTTP_X_REMOTE_USER_NAME') or headers.get('X-Remote-User-Name') or headers.get('X-REMOTE-USER-NAME')
        return (user_id or 'anonymous', user_name or 'Anonymous')

    async def get_user(self, sid: str) -> tuple[str, str]:
        session = await sio.get_session(sid)
        return session.get('user_id'), session.get('username')



async def run_server(
        db: Database,
        logger: logging.Logger,
        url_base: str,
        push_messages: asyncio.Queue,
        videos_dir: Path,
        upload_dir: Path,
        host='127.0.0.1',
        port: int=8086,
        serve_dirs: dict[str, Path] = {},
        has_started = asyncio.Event(),
        ingest_callback: Callable[Path, str] = None,
    ) -> bool:
    """
    Run HTTP / Socket.IO API server forever (until this asyncio task is cancelled)

    Params:
        db:           Database object
        logger:       Logger instance for API server
        url_base:     Base URL for the server (e.g. https://example.com). Used e.g. to construct video URLs.
        push_messages: Queue for messages to be pushed to clients
        videos_dir:   Directory where videos are stored
        upload_dir:   Directory where uploaded files are stored
        host:         Hostname to listen on
        port:         Port to listen on
        serve_dirs:   Dict of {route: path} for static file serving
        has_started:  Event that is set when the server has started
        ingest_callback: Callback function to be called when a file is uploaded. Signature: (path: Path, user_id: str) -> None


    Returns:
        True if server was started successfully, False if not.
    """    
    try:
        async with db:
            if db.error_state:
                logger.fatal(f"DB ERROR: {db.error_state}")
                return False

            server = ClapshotApiServer(db=db, url_base=url_base, port=port, logger=logger, videos_dir=videos_dir, upload_dir=upload_dir, serve_dirs=serve_dirs, ingest_callback=ingest_callback)
            runner = web.AppRunner(server.app)
            await runner.setup()
            # bind to localhost only, no matter what url_base is (for security, use reverse proxy to expose)
            logger.info(f"Starting API server. Binding to {host}:{server.port} -- Base URL: {url_base}")
            site = web.TCPSite(runner, host, server.port)
            await site.start()
            has_started.set()

            # Wait for messages from other parts of the app,
            # and push them to clients.
            while msg := await push_messages.get():                
                await server.push_message(msg, persist=True, dont_throw=True)

    except Exception as e:
        logger.error(f"Exception in API server: {e}")
        return False

    return True
