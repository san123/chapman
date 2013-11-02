import re
import sys
import time
import logging
import threading
from random import randint

from paste.deploy.converters import asint

from ming.session import Session
from mongotools.pubsub import Channel

import model as M
from .context import g

log = logging.getLogger(__name__)
re_shard_qname = re.compile(r'^shard:')


class ShardWorker(object):
    '''Sharded worker'''

    def __init__(self, name, app_context):
        '''Sharded Worker - this worker reserves messages
        and then redispatches them on other ming sessions.

        It looks for things on queues starting with 'shard:',
        strips the "shard:" prefix, and redispatches to one of
        its sub-sessions.
        '''
        self._name = name
        g.app_context = app_context
        settings = app_context['registry'].settings
        snames = settings['chapmans.sessions'].split(',')
        self._sessions = [Session.by_name(sname) for sname in snames]
        self._sleep = asint(settings.get(
            'chapman.sleep', '200')) / 1000.0

        self._send_event = threading.Event()
        self._shutdown = False  # flag to indicate worker is shutting down
        self._session_id = randint(0, len(self._sessions))

    def start(self):
        M.doc_session.db.collection_names()  # force connection & auth
        self._dispatcher = threading.Thread(
            name='dispatch',
            target=self.dispatcher)
        self._dispatcher.setDaemon(True)
        self._dispatcher.start()

    def run(self):
        log.info('Entering event thread')
        conn = M.doc_session.bind.bind.conn
        conn.start_request()
        chan = M.Message.channel.new_channel()
        chan.pub('start', self._name)

        @chan.sub('ping')
        def handle_ping(chan, msg):
            data = msg['data']
            if data['worker'] in (self._name, '*'):
                data['worker'] = self._name
                chan.pub('pong', data)

        @chan.sub('kill')
        def handle_kill(chan, msg):
            if msg['data'] in (self._name, '*'):
                log.error('Received %r, exiting', msg)
                sys.exit(0)

        @chan.sub('shutdown')
        def handle_shutdown(chan, msg):
            if msg['data'] in (self._name, '*'):
                log.error('Received %r, shutting down gracefully', msg)
                self._shutdown = True
                raise StopIteration()

        @chan.sub('send')
        def handle_send(chan, msg):
            self._send_event.set()

        while True:
            try:
                chan.handle_ready(await=True, raise_errors=True)
            except StopIteration:
                break
            time.sleep(self._sleep)

        self._dispatcher.join()

    def dispatcher(self):
        log.info('Entering chapmans dispatcher thread')
        log.info('  Sub-sessions:')
        for sess in self._sessions:
            log.info('   - %s', sess.db)
        while not self._shutdown:
            msg, state = M.Message.reserve_qspec(
                self._name, re_shard_qname)
            if msg is None:
                self._send_event.clear()
                self._send_event.wait(self._sleep)
                continue
            if state is None:
                continue
            self._dispatch(msg, state)
        log.info('Exiting chapmans dispatcher thread')

    def _dispatch(self, msg, state):
        sid = self._session_id % len(self._sessions)
        self._session_id = (sid+1) % len(self._sessions)
        sess = self._sessions[sid]
        channel = Channel(sess.db, 'chapman.event')
        channel.ensure_channel()

        # Strip the queue prefix
        state.options.queue = state.options.queue.split(':', 1)[-1]
        msg.s.q = msg.s.q.split(':', 1)[-1]

        # Create the taskstate and msg in the subsession
        sess.insert(state)
        sess.insert(msg)
        channel.pub('send', msg._id)