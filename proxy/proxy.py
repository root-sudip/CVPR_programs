#!/usr/bin/env python3
# coding=utf-8
#
# Author: Jianfei Wang <me@thinxer.com>
# Author: Fengyuan Chen <jeova.sanctus.unus@gmail.com>
# License: MIT

""" Proxy Server based on tornado. """

import base64
import tornado.options
import os
import re
import struct
import socket
import logging
import tornado.ioloop
import tornado.tcpserver
import tornado.iostream
import argparse
from urllib.parse import urlparse, urlunparse
from collections import OrderedDict

logging.getLogger().setLevel(logging.INFO)


def header_parser(headers):
    for header in headers.split(b'\r\n'):
        i = header.find(b':')
        if i >= 0:
            yield header[:i], header[i + 2:]


def hostport_parser(hostport, default_port):
    i = hostport.find(b':' if isinstance(hostport, bytes) else ':')
    if i >= 0:
        return hostport[:i], int(hostport[i + 1:])
    else:
        return hostport, default_port


def netloc_parser(netloc, default_port=-1):
    assert default_port
    i = netloc.rfind(b'@' if isinstance(netloc, bytes) else '@')
    if i >= 0:
        return netloc[:i], netloc[i + 1:]
    else:
        return None, netloc


def write_to(stream):
    def on_data(data):
        if data == b'':
            stream.close()
        else:
            if not stream.closed():
                stream.write(data)

    return on_data


def pipe(stream_a, stream_b):
    writer_a = write_to(stream_a)
    writer_b = write_to(stream_b)
    stream_a.read_until_close(writer_b, writer_b)
    stream_b.read_until_close(writer_a, writer_a)


def subclasses(cls, _seen=None):
    if _seen is None:
        _seen = set()
    subs = cls.__subclasses__()
    for sub in subs:
        if sub not in _seen:
            _seen.add(sub)
            yield sub
            for sub_ in subclasses(sub, _seen):
                yield sub_


class Connector:

    def __init__(self, netloc=None, path=None):
        self.netloc = netloc
        self.path = path

    @classmethod
    def accept(cls, scheme):
        raise NotImplementedError()

    def connect(self, host, port, callback):
        raise NotImplementedError()

    @classmethod
    def get(cls, url):
        parts = urlparse(url)
        for sub_cls in subclasses(cls):
            if sub_cls.accept(parts.scheme):
                return sub_cls(parts.netloc, parts.path)
        raise NotImplementedError('Unsupported scheme', parts.scheme)

    def __str__(self):
        return '%s(netloc=%s, path=%s)' % (self.__class__.__name__, repr(self.netloc), repr(self.path))


class RejectConnector(Connector):

    @classmethod
    def accept(cls, scheme):
        return scheme == 'reject'

    def connect(self, host, port, callback):
        callback(RejectConnector)

    @classmethod
    def write(cls, _):
        pass

    @classmethod
    def read_until_close(cls, req_callback, _):
        req_callback(b'HTTP/1.1 410 Gone\r\n\r\n')
        req_callback(b'')


class DirectConnector(Connector):

    @classmethod
    def accept(cls, scheme):
        return scheme == 'direct'

    def connect(self, host, port, callback):
        def on_close():
            callback(None)

        def on_connected():
            stream.set_close_callback(None)
            callback(stream)

        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM, 0)
        stream = tornado.iostream.IOStream(s)
        stream.set_close_callback(on_close)
        stream.connect((host, port), on_connected)


class SocksConnector(Connector):

    def __init__(self, netloc, path=None):
        Connector.__init__(self, netloc, path)
        self.socks_server, self.socks_port = hostport_parser(netloc, 1080)

    @classmethod
    def accept(cls, scheme):
        return scheme == 'socks'

    def connect(self, host, port, callback):

        def socks_close():
            callback(None)

        def socks_response(data):
            stream.set_close_callback(None)
            if data[1] == 0x5a:
                callback(stream)
            else:
                callback(None)

        def socks_connected():
            try:
                stream.write(b'\x04\x01' + struct.pack('>H', port)
                             + b'\x00\x00\x00\x09userid\x00' + host + b'\x00')
                stream.read_bytes(8, socks_response)
            except tornado.iostream.StreamClosedError:
                socks_close()

        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM, 0)
        stream = tornado.iostream.IOStream(s)
        stream.set_close_callback(socks_close)
        stream.connect((self.socks_server, self.socks_port), socks_connected)


class HttpConnector(Connector):

    def __init__(self, netloc, path=None):
        Connector.__init__(self, netloc, path)
        auth, host = netloc_parser(netloc)
        self.auth = base64.encodebytes(auth.encode()).strip() if auth else None
        self.http_server, self.http_port = hostport_parser(host, 3128)

    @classmethod
    def accept(cls, scheme):
        return scheme == 'http'

    def connect(self, host, port, callback):

        def http_close():
            callback(None)

        def http_response(data):
            stream.set_close_callback(None)
            code = int(data.split()[1])
            if code == 200:
                callback(stream)
            else:
                callback(None)

        def http_connected():
            try:
                stream.write(b'CONNECT ' + host + b':' +
                             str(port).encode() + b' HTTP/1.1\r\n')
                if self.auth:
                    stream.write(
                        b'Proxy-Authorization: Basic ' + self.auth + b'\r\n')
                stream.write(b'Proxy-Connection: closed\r\n')
                stream.write(b'\r\n')
                stream.read_until(b'\r\n\r\n', http_response)
            except tornado.iostream.StreamClosedError:
                http_close()

        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM, 0)
        stream = tornado.iostream.IOStream(s)
        stream.set_close_callback(http_close)
        stream.connect((self.http_server, self.http_port), http_connected)


class RulesConnector(Connector):

    def __init__(self, netloc=None, path=None):
        Connector.__init__(self, netloc, path)
        self.rules = None
        self._connectors = {}
        self._modify_time = None
        self.check_update()
        tornado.ioloop.PeriodicCallback(self.check_update, 1000).start()

    def load_rules(self):
        self.rules = []
        with open(self.path) as f:
            for l in f:
                l = l.strip()
                if not l or l.startswith('#'):
                    continue
                try:
                    rule_pattern, upstream = l.split()
                    Connector.get(upstream)
                    rule_pattern = re.compile(rule_pattern, re.I)
                except KeyboardInterrupt:
                    raise
                except:
                    logging.error('Invalid rule: %s', l)
                    continue
                self.rules.append([rule_pattern, upstream])
        self.rules.append(['.*', 'direct://'])

    def check_update(self):
        modified = os.stat(self.path).st_mtime
        if modified != self._modify_time:
            logging.info('loading %s', self.path)
            self._modify_time = modified
            self.load_rules()

    @classmethod
    def accept(cls, scheme):
        return scheme == 'rules'

    def connect(self, host, port, callback):
        s = host.decode() + ':' + str(port)
        for rule, upstream in self.rules:
            if re.match(rule, s):
                if upstream not in self._connectors:
                    self._connectors[upstream] = Connector.get(upstream)
                self._connectors[upstream].connect(host, port, callback)
                break
        else:
            raise RuntimeError('no available rule for %s' % s)


class ProxyHandler(object):

    def __init__(self, stream, connector):
        self.connector = connector

        self.incoming = stream
        self.incoming.read_until(b'\r\n', self.on_method)

        self.method = None
        self.url = None
        self.ver = None
        self.headers = None
        self.outgoing = None

    def on_method(self, method):
        self.method, self.url, self.ver = method.strip().split(b' ')
        # XXX would fail if the request doesn't have any more headers
        self.incoming.read_until(b'\r\n\r\n', self.on_headers)
        logging.debug(method.strip().decode())

    def on_connected(self, outgoing):
        if outgoing:
            try:
                path = urlunparse((b'', b'') + urlparse(self.url)[2:])
                outgoing.write(b' '.join((self.method, path, self.ver)) + b'\r\n')
                for k, v in self.headers.items():
                    outgoing.write(k + b': ' + v + b'\r\n')
                outgoing.write(b'\r\n')
                writer_in = write_to(self.incoming)
                if b'Content-Length' in self.headers:
                    self.incoming.read_bytes(
                        int(self.headers[b'Content-Length']), outgoing.write, outgoing.write)
                outgoing.read_until_close(writer_in, writer_in)
            except tornado.iostream.StreamClosedError:
                self.incoming.close()
                outgoing.close()
        else:
            self.incoming.close()

    def on_connect_connected(self, outgoing):
        if outgoing:
            try:
                self.incoming.write(b'HTTP/1.1 200 Connection Established\r\n\r\n')
            except tornado.iostream.StreamClosedError:
                self.incoming.close()
                outgoing.close()
            pipe(self.incoming, outgoing)
        else:
            self.incoming.close()

    def on_headers(self, headers_buffer):
        self.headers = OrderedDict(header_parser(headers_buffer))
        if self.method == b'CONNECT':
            host, port = hostport_parser(self.url, 443)
            self.outgoing = self.connector.connect(
                host, port, self.on_connect_connected)
        else:
            if b'Proxy-Connection' in self.headers:
                del self.headers[b'Proxy-Connection']
            self.headers[b'Connection'] = b'close'
            if b'Host' in self.headers:
                host, port = hostport_parser(self.headers[b'Host'], 80)
                self.outgoing = self.connector.connect(
                    host, port, self.on_connected)
            else:
                self.incoming.close()


class ProxyServer(tornado.tcpserver.TCPServer):

    def __init__(self, connector=None):
        tornado.tcpserver.TCPServer.__init__(self)
        self.connector = connector or DirectConnector()

    def handle_stream(self, stream, address):
        ProxyHandler(stream, self.connector)


def main():
    tornado.options.options.parse_config_file('/dev/null')

    parser = argparse.ArgumentParser(
        description='Simple proxy server based on tornado')
    parser.add_argument('-u', '--upstream', type=str,
                        help='upstream proxy like socks://localhost:1080')
    parser.add_argument('-b', '--bind', type=str, default=':8000',
                        help='bind address and port, default is :8000')
    args = parser.parse_args()

    if args.upstream:
        connector = Connector.get(args.upstream)
    else:
        connector = DirectConnector()
    logging.info('using connector: %s', connector)
    host, port = hostport_parser(args.bind, 8000)
    server = ProxyServer(connector)
    logging.info('listening on %s:%s', host, port)
    server.listen(port, host)

    tornado.ioloop.IOLoop.instance().start()


if __name__ == '__main__':
    main()