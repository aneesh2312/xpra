#!/usr/bin/env python3
# This file is part of Xpra.
# Copyright (C) 2016-2020 Antoine Martin <antoine@xpra.org>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

import os
import shutil
import unittest
import tempfile
from time import monotonic

from xpra.util import repr_ellipsized, envint
from xpra.os_util import load_binary_file, pollwait, OSX, POSIX
from xpra.exit_codes import EXIT_OK, EXIT_CONNECTION_FAILED, EXIT_SSL_CERTIFICATE_VERIFY_FAILURE
from xpra.net.net_util import get_free_tcp_port
from xpra.platform.dotxpra import DISPLAY_PREFIX
from unit.server_test_util import ServerTestUtil, log, estr, log_gap


CONNECT_WAIT = envint("XPRA_TEST_CONNECT_WAIT", 20)
SUBPROCESS_WAIT = envint("XPRA_TEST_SUBPROCESS_WAIT", CONNECT_WAIT*2)


class ServerSocketsTest(ServerTestUtil):

    def get_run_env(self):
        env = super().get_run_env()
        env["XPRA_CONNECT_TIMEOUT"] = str(CONNECT_WAIT)
        return env

    def start_server(self, *args):
        server_proc = self.run_xpra(["start", "--no-daemon"]+list(args))
        if pollwait(server_proc, 10) is not None:
            r = server_proc.poll()
            raise Exception(f"server failed to start with args={args}, returned {estr(r)}")
        return server_proc

    def _test_connect(self, server_args=(), auth="none", client_args=(), password=None, uri_prefix=DISPLAY_PREFIX, exit_code=0):
        display_no = self.find_free_display_no()
        display = f":{display_no}"
        log(f"starting test server on {display}")
        server = self.start_server(display, f"--auth={auth}", "--printing=no", *server_args)
        #we should always be able to get the version:
        uri = uri_prefix + str(display_no)
        start = monotonic()
        while True:
            client = self.run_xpra(["version", uri] + list(server_args or ()))
            r = pollwait(client, CONNECT_WAIT)
            if r==0:
                break
            if r is None:
                client.terminate()
            if monotonic()-start>SUBPROCESS_WAIT:
                raise Exception(f"version client failed to connect, returned {estr(r)}")
        #try to connect
        cmd = ["connect-test", uri] + [x.replace("$DISPLAY_NO", str(display_no)) for x in client_args]
        f = None
        if password:
            f = self._temp_file(password)
            cmd += [f"--password-file={f.name}"]
            cmd += [f"--challenge-handlers=file:filename={f.name}"]
        client = self.run_xpra(cmd)
        r = pollwait(client, SUBPROCESS_WAIT)
        if f:
            f.close()
        if r is None:
            client.terminate()
        server.terminate()
        if r!=exit_code:
            log.error("Exit code mismatch")
            log.error(" expected %s (%s)", estr(exit_code), exit_code)
            log.error(" got %s (%s)", estr(r), r)
            log.error(" server args=%s", server_args)
            log.error(" client args=%s", client_args)
            if r is None:
                raise Exception("expected info client to return %s but it is still running" % (estr(exit_code),))
            raise Exception("expected info client to return %s but got %s" % (estr(exit_code), estr(r)))
        pollwait(server, 10)

    def test_default_socket(self):
        self._test_connect([], "allow", [], b"hello", DISPLAY_PREFIX, EXIT_OK)

    def test_tcp_socket(self):
        port = get_free_tcp_port()
        self._test_connect([f"--bind-tcp=0.0.0.0:{port}"], "allow", [], b"hello",
						f"tcp://127.0.0.1:{port}/", EXIT_OK)
        port = get_free_tcp_port()
        self._test_connect([f"--bind-tcp=0.0.0.0:{port}"], "allow", [], b"hello",
						f"ws://127.0.0.1:{port}/", EXIT_OK)

    def test_ws_socket(self):
        port = get_free_tcp_port()
        self._test_connect([f"--bind-ws=0.0.0.0:{port}"], "allow", [], b"hello",
						f"ws://127.0.0.1:{port}/", EXIT_OK)


    def test_ssl(self):
        server = None
        display_no = self.find_free_display_no()
        display = f":{display_no}"
        tcp_port = get_free_tcp_port()
        ws_port = get_free_tcp_port()
        wss_port = get_free_tcp_port()
        ssl_port = get_free_tcp_port()
        try:
            tmpdir = tempfile.mkdtemp(suffix='ssl-xpra')
            keyfile = os.path.join(tmpdir, "key.pem")
            outfile = os.path.join(tmpdir, "out.pem")
            openssl_command = [
                "openssl", "req", "-new", "-newkey", "rsa:4096", "-days", "2", "-nodes", "-x509",
                "-subj", "/C=US/ST=Denial/L=Springfield/O=Dis/CN=localhost",
                "-keyout", keyfile, "-out", outfile,
                ]
            openssl = self.run_command(openssl_command)
            assert pollwait(openssl, 20)==0, "openssl certificate generation failed"
            #combine the two files:
            certfile = os.path.join(tmpdir, "cert.pem")
            with open(certfile, 'wb') as cert:
                for fname in (keyfile, outfile):
                    with open(fname, 'rb') as f:
                        cert.write(f.read())
            cert_data = load_binary_file(certfile)
            log("generated cert data: %s", repr_ellipsized(cert_data))
            if not cert_data:
                #cannot run openssl? (happens from rpmbuild)
                log.warn("SSL test skipped, cannot run '%s'", b" ".join(openssl_command))
                return
            server_args = [
                f"--bind-tcp=0.0.0.0:{tcp_port}",
                f"--bind-ws=0.0.0.0:{ws_port}",
                f"--bind-wss=0.0.0.0:{wss_port}",
                f"--bind-ssl=0.0.0.0:{ssl_port}",
                "--ssl=on",
                "--html=on",
                f"--ssl-cert={certfile}",
                ]

            log("starting test ssl server on %s", display)
            server = self.start_server(display, *server_args)

            #test it with openssl client:
            for port in (tcp_port, ssl_port, ws_port, wss_port):
                openssl_verify_command = (
					"openssl", "s_client", "-connect",
					"127.0.0.1:%i" % port, "-CAfile", certfile,
					)
                devnull = os.open(os.devnull, os.O_WRONLY)
                openssl = self.run_command(openssl_verify_command, stdin=devnull, shell=True)
                r = pollwait(openssl, 10)
                assert r==0, "openssl certificate verification failed, returned %s" % r

            def test_connect(uri, exit_code, *client_args):
                cmd = ["info", uri] + list(client_args)
                client = self.run_xpra(cmd)
                r = pollwait(client, CONNECT_WAIT)
                if client.poll() is None:
                    client.terminate()
                assert r==exit_code, "expected info client to return %s but got %s" % (estr(exit_code), estr(client.poll()))
            noverify = "--ssl-server-verify-mode=none"
            #connect to ssl socket:
            test_connect(f"ssl://127.0.0.1:{ssl_port}/", EXIT_OK, noverify)
            #tcp socket should upgrade to ssl:
            test_connect(f"ssl://127.0.0.1:{tcp_port}/", EXIT_OK, noverify)
            #tcp socket should upgrade to ws and ssl:
            test_connect(f"wss://127.0.0.1:{tcp_port}/", EXIT_OK, noverify)
            #ws socket should upgrade to ssl:
            test_connect(f"wss://127.0.0.1:{ws_port}/", EXIT_OK, noverify)

            #self signed cert should fail without noverify:
            test_connect(f"ssl://127.0.0.1:{ssl_port}/", EXIT_SSL_CERTIFICATE_VERIFY_FAILURE)
            test_connect(f"ssl://127.0.0.1:{tcp_port}/", EXIT_SSL_CERTIFICATE_VERIFY_FAILURE)
            test_connect(f"wss://127.0.0.1:{ws_port}/", EXIT_SSL_CERTIFICATE_VERIFY_FAILURE)
            test_connect(f"wss://127.0.0.1:{wss_port}/", EXIT_SSL_CERTIFICATE_VERIFY_FAILURE)

        finally:
            shutil.rmtree(tmpdir)
            if server:
                server.terminate()

    def test_bind_tmpdir(self):
        #remove socket dirs from default arguments temporarily:
        saved_default_xpra_args = ServerSocketsTest.default_xpra_args
        tmpsocketdir1 = tempfile.mkdtemp(suffix='xpra')
        tmpsocketdir2 = tempfile.mkdtemp(suffix='xpra')
        tmpsessionsdir = tempfile.mkdtemp(suffix='xpra')
        #hide sessions dir and use a single socket dir location:
        ServerSocketsTest.default_xpra_args = [
            x for x in saved_default_xpra_args if not x.startswith("--socket-dir")
            ] + [
                "--video-encoders=none",
                "--csc-modules=none",
                "--video-decoders=none",
                "--encodings=rgb",
                ]
        server_args = [
                "--socket-dir=%s" % tmpsocketdir1,
                "--socket-dirs=%s" % tmpsocketdir2,
                "--sessions-dir=%s" % tmpsessionsdir,
            ]
        log_gap()
        def t(client_args=(), prefix=DISPLAY_PREFIX, exit_code=EXIT_OK):
            self._test_connect(server_args, "none", client_args, None, prefix, exit_code)
        try:
            #it should not be found by default
            #since we only use hidden temporary locations
            #for both sessions-dir and socket-dir(s):
            t(exit_code=EXIT_CONNECTION_FAILED)
            #specifying the socket-dir(s) should work:
            for d in (tmpsocketdir1, tmpsocketdir2):
                t(["--socket-dir=%s" % d])
                t(["--socket-dirs=%s" % d])
        finally:
            ServerSocketsTest.default_xpra_args = saved_default_xpra_args
            for d in (tmpsocketdir1, tmpsocketdir2, tmpsessionsdir):
                shutil.rmtree(d)


def main():
    if POSIX and not OSX:
        unittest.main()


if __name__ == '__main__':
    main()
