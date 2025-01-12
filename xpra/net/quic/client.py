# This file is part of Xpra.
# Copyright (C) 2022 Antoine Martin <antoine@xpra.org>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

import socket
import ipaddress
from queue import Queue
from typing import Dict, Callable, Optional, Union, cast

from aioquic.quic.configuration import QuicConfiguration
from aioquic.quic.events import QuicEvent
from aioquic.h3.connection import H3_ALPN
from aioquic.h0.connection import H0Connection
from aioquic.h3.connection import H3Connection
from aioquic.h3.events import (
    DataReceived,
    H3Event,
    HeadersReceived,
)
from aioquic.tls import SessionTicket
from aioquic.quic.logger import QuicLogger
from aioquic.quic.connection import QuicConnection
from aioquic.asyncio.protocol import QuicConnectionProtocol

from xpra.net.socket_util import get_ssl_verify_mode, create_udp_socket
from xpra.net.quic.connection import XpraQuicConnection
from xpra.net.quic.asyncio_thread import get_threaded_loop
from xpra.net.quic.common import USER_AGENT, binary_headers
from xpra.util import ellipsizer, envbool
from xpra.os_util import memoryview_to_bytes
from xpra.log import Logger
log = Logger("quic")

HttpConnection = Union[H0Connection, H3Connection]

IPV6 = socket.has_ipv6 and envbool("XPRA_IPV6", True)

quic_logger = QuicLogger()

def save_session_ticket(ticket: SessionTicket) -> None:
    pass

WS_HEADERS = {
        ":method"   : "CONNECT",
        ":scheme"   : "https",
        ":protocol" : "websocket",
        "sec-websocket-version" : 13,
        "sec-websocket-protocol" : "xpra",
        "user-agent" : USER_AGENT,
        }


class ClientWebSocketConnection(XpraQuicConnection):

    def __init__(self, connection : HttpConnection, stream_id: int, transmit: Callable[[], None],
                 host : str, port : int, info=None, options=None) -> None:
        super().__init__(connection, stream_id, transmit, host, port, info, options)
        self.write_buffer = Queue()

    def flush_writes(self):
        #flush the buffered writes:
        while self.write_buffer.qsize()>0:
            buf = self.write_buffer.get()
            self.connection.send_data(self.stream_id, memoryview_to_bytes(buf), end_stream=False)
        self.transmit()
        self.write_buffer = None

    def write(self, buf):
        log(f"write(%s) {len(buf)} bytes", ellipsizer(buf))
        if self.write_buffer is not None:
            #buffer it until we are connected and call flush_writes()
            self.write_buffer.put(buf)
            return len(buf)
        return super().write(buf)

    def http_event_received(self, event: H3Event) -> None:
        log("http_event_received(%s)", event)
        if isinstance(event, HeadersReceived):
            for header, value in event.headers:
                if header == b"sec-websocket-protocol":
                    subprotocols = value.decode().split(",")
                    if "xpra" not in subprotocols:
                        log.warn(f"Warning: unsupported websocket subprotocols {subprotocols}")
                        self.close()
                        return
                    self.accepted = True
                    self.flush_writes()
            return
        super().http_event_received(event)


class WebSocketClient(QuicConnectionProtocol):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._http: Optional[HttpConnection] = None
        self._websockets: Dict[int, ClientWebSocketConnection] = {}
        if self._quic.configuration.alpn_protocols[0].startswith("hq-"):
            self._http = H0Connection(self._quic)
        else:
            self._http = H3Connection(self._quic)

    def open(self, host : str, port : int, path : str) -> ClientWebSocketConnection:
        log(f"open({host}, {port}, {path})")
        stream_id = self._quic.get_next_available_stream_id()
        websocket = ClientWebSocketConnection(self._http, stream_id, self.transmit,
                                              host, port)
        self._websockets[stream_id] = websocket
        headers = {
            ":authority" : host,
            ":path" : path,
            }
        headers.update(WS_HEADERS)
        log("open: sending http headers for websocket upgrade")
        self._http.send_headers(stream_id=stream_id, headers=binary_headers(headers))
        self.transmit()
        return websocket

    def quic_event_received(self, event: QuicEvent) -> None:
        for http_event in self._http.handle_event(event):
            self.http_event_received(http_event)

    def http_event_received(self, event: H3Event) -> None:
        if isinstance(event, (HeadersReceived, DataReceived)):
            stream_id = event.stream_id
            if stream_id in self._websockets:
                # websocket
                websocket : ClientWebSocketConnection = self._websockets[stream_id]
                websocket.http_event_received(event)
            else:
                log.warn(f"Warning: unexpected websocket stream id: {stream_id}")
        else:
            log.warn(f"Warning: unexpected http event type: {event}")


def quic_connect(host : str, port : int, path : str,
                 ssl_cert : str, ssl_key : str, ssl_key_password : str,
                 ssl_ca_certs, ssl_server_verify_mode : str, ssl_server_name : str):
    configuration = QuicConfiguration(is_client=True, alpn_protocols=H3_ALPN)
    configuration.verify_mode = get_ssl_verify_mode(ssl_server_verify_mode)
    if ssl_ca_certs:
        configuration.load_verify_locations(ssl_ca_certs)
    if ssl_cert:
        configuration.load_cert_chain(ssl_cert, ssl_key, ssl_key_password)
    if ssl_server_name:
        configuration.server_name = ssl_server_name
    else:
        # if host is not an IP address, use it for SNI:
        try:
            ipaddress.ip_address(host)
        except ValueError:
            configuration.server_name = host
    #configuration.max_data = args.max_data
    #configuration.max_stream_data = args.max_stream_data
    #configuration.quic_logger = QuicFileLogger(args.quic_log)
    #configuration.secrets_log_file = open(args.secrets_log, "a")
    connection = QuicConnection(configuration=configuration, session_ticket_handler=save_session_ticket)

    if IPV6:
        local_host = "::"
    else:
        local_host = "localhost"
    local_port = 0
    sock = create_udp_socket(local_host, local_port)
    log(f"create_udp_socket({local_host}:{local_port})={sock}")
    tl = get_threaded_loop()

    def create_protocol():
        return WebSocketClient(connection)

    async def connect():
        log("quic_connect: connect()")
        # lookup remote address
        infos = await tl.loop.getaddrinfo(host, port, type=socket.SOCK_DGRAM)
        log(f"getaddrinfo({host}, {port}, SOCK_DGRAM)={infos}")
        addr = infos[0][4]
        if len(addr) == 2:
            if IPV6:
                addr = ("::ffff:" + addr[0], addr[1], 0, 0)
            else:
                addr = (addr[0], addr[1])
        transport, protocol = await tl.loop.create_datagram_endpoint(create_protocol, sock=sock)
        log(f"transport={transport}, protocol={protocol}")
        protocol = cast(QuicConnectionProtocol, protocol)
        log(f"connecting to {addr}")
        protocol.connect(addr)
        try:
            await protocol.wait_connected()
        except Exception as e:
            log("connect()", exc_info=True)
            #try to get a more meaningful exception message:
            einfo = str(e)
            if not einfo:
                quic_conn = getattr(protocol, "_quic", None)
                if quic_conn:
                    close_event = getattr(quic_conn, "_close_event", None)
                    if close_event:
                        raise Exception(close_event.reason_phrase) from None
            raise
        conn = protocol.open(host, port, path)
        log(f"websocket connection {conn}")
        return conn
    #protocol.close()
    #await protocol.wait_closed()
    #transport.close()
    conn = tl.sync(connect)
    log(f"quic_connect() connect()={conn}")
    return conn
