import time
import logging
from datetime import datetime, timedelta

from pyramid.view import view_config
import pyramid.httpexceptions as exc
from paste.deploy.converters import asint

from chapman import model as M
from chapman import validators as V

log = logging.getLogger(__name__)

@view_config(
    route_name='chapman.1_0.queue',
    request_method='POST',
    renderer='string')
def put(request):
    metadata = V.message_schema.to_python(request.GET, request)
    data = request.json
    after = datetime.utcnow() + timedelta(metadata['delay'])
    msg = M.HTTPMessage.new(
        data=data,
        timeout=metadata['timeout'],
        after=after,
        q=request.matchdict['qname'],
        pri=metadata['priority'])
    request.response.status_int = 201
    return [request.route_url('chapman.1_0.message', message_id=msg._id)]


@view_config(
    route_name='chapman.1_0.queue',
    request_method='GET')
def get(request):
    data = V.get_schema.to_python(request.params, request)
    messages = []
    for x in range(data['count']):
        msg = M.HTTPMessage.reserve(data['client'], [request.matchdict['qname']])
        if msg is None:
            break
        messages.append(msg)
    if not messages and data['timeout'] > 0:
        return _wait_then_get(
            request,
            data['client'],
            request.matchdict['qname'],
            data['timeout'],
            asint(request.registry.settings['chapman.sleep_ms']))
    if messages:
        return dict(
            (request.route_url('chapman.1_0.message', message_id=msg._id),
             msg.data)
            for msg in messages)
    else:
        return exc.HTTPNoContent()


@view_config(
    route_name='chapman.1_0.message',
    request_method='DELETE')
def delete_message(request):
    M.HTTPMessage.m.remove(dict(_id=int(request.matchdict['message_id'])))
    return exc.HTTPNoContent()


@view_config(
    route_name='chapman.1_0.message',
    request_method='POST')
def retry_message(request):
    '''Unlocks and retries the message at a point in the future'''
    data = V.retry_schema.to_python(request.json, request)
    after = datetime.utcnow() + timedelta(seconds=data['delay'])
    M.HTTPMessage.m.update_partial(
        dict(_id=int(request.matchdict['message_id'])),
        {'s.status': 'ready',
         's.after': after})
    M.HTTPMessage.channel.pub('enqueue', int(request.matchdict['message_id']))
    return exc.HTTPNoContent()


def _wait_then_get(request, client, qname, timeout, sleep):
    chan = M.Message.channel.new_channel()
    wait_until = datetime.utcnow() + timedelta(seconds=timeout)
    msg = None

    while not msg and datetime.utcnow() < wait_until:
        cursor = chan.cursor(True)
        try:
            cursor.next()
        except StopIteration:
            time.sleep(sleep / 1e3)
        msg = M.HTTPMessage.reserve(client, [qname])
        if msg:
            return msg.__json__(request)

    return exc.HTTPNoContent()

