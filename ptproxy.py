#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import time
import json
import shlex
import select
import threading
import subprocess
import socketserver

import socks

# Default config
# If config file is not specified on the command line, this is used instead.

CFG = {
    # Role: client|server
    "role": "server",
    # Where to store PT state files
    "state": ".",
    # For server, which address to forward
    # For client, which address to listen
    "local": "127.0.0.1:1080",
    # For server, which address to listen
    # For client, which address to connect
    "server": "0.0.0.0:23456",
    # The PT command line
    "ptexec": "obfs4proxy -logLevel=ERROR -enableLogging=true",
    # The PT name, must be only one
    "ptname": "obfs4",
    # [Client] PT arguments
    "ptargs": "cert=AAAAAAAAAAAAAAAAAAAAAAAAAAAAA+AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA;iat-mode=0",
    # [Optional][Server] PT options
    # <key>=<value> [;<key>=<value> ...]
    "ptserveropt": "",
    # [Optional][Client] Which outgoing proxy must PT use
    # <proxy_type>://[<user_name>][:<password>][@]<ip>:<port>
    "ptproxy": ""
}

# End

TRANSPORT_VERSIONS = ('1',)

logtime = lambda: time.strftime('%Y-%m-%d %H:%M:%S')

class PTConnectFailed(Exception):
    pass


class ThreadedTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True


class ThreadedTCPRequestHandler(socketserver.BaseRequestHandler):

    def handle(self):
        ptsock = socks.socksocket()
        ptsock.set_proxy(*CFG['_ptcli'])
        host, port = CFG['server'].split(':')
        ptsock.connect((host, int(port)))
        run = 1
        while run:
            rl, wl, xl = select.select([self.request, ptsock], [], [], 300)
            if not rl:
                break
            run = 0
            for s in rl:
                try:
                    data = s.recv(1024)
                except Exception as ex:
                    print(logtime(), ex)
                    continue
                if data:
                    run += 1
                else:
                    continue
                if s is self.request:
                    ptsock.sendall(data)
                elif s is ptsock:
                    self.request.sendall(data)


def ptenv():
    env = os.environ.copy()
    env['TOR_PT_STATE_LOCATION'] = CFG['state']
    env['TOR_PT_MANAGED_TRANSPORT_VER'] = ','.join(TRANSPORT_VERSIONS)
    if CFG["role"] == "client":
        env['TOR_PT_CLIENT_TRANSPORTS'] = CFG['ptname']
        if CFG.get('ptproxy'):
            env['TOR_PT_PROXY'] = CFG['ptproxy']
    elif CFG["role"] == "server":
        env['TOR_PT_SERVER_TRANSPORTS'] = CFG['ptname']
        env['TOR_PT_SERVER_BINDADDR'] = '%s-%s' % (
            CFG['ptname'], CFG['server'])
        env['TOR_PT_ORPORT'] = CFG['local']
        env['TOR_PT_EXTENDED_SERVER_PORT'] = ''
        if CFG.get('ptserveropt'):
            env['TOR_PT_SERVER_TRANSPORT_OPTIONS'] = ';'.join(
                '%s:%s' % (CFG['ptname'], kv) for kv in CFG['ptserveropt'].split(';'))
    else:
        raise ValueError('"role" must be either "server" or "client"')
    return env


def checkproc():
    global PT_PROC
    if PT_PROC is None or PT_PROC.poll() is not None:
        PT_PROC = subprocess.Popen(shlex.split(
            CFG['ptexec']), stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=ptenv())
    return PT_PROC


def parseptline(iterable):
    global CFG
    for ln in iterable:
        ln = ln.decode('utf_8').rstrip('\n')
        sp = ln.split(' ', 1)
        kw = sp[0]
        if kw in ('ENV-ERROR', 'VERSION-ERROR', 'PROXY-ERROR',
                  'CMETHOD-ERROR', 'SMETHOD-ERROR'):
            raise PTConnectFailed(ln)
        elif kw == 'VERSION':
            if sp[1] not in TRANSPORT_VERSIONS:
                raise PTConnectFailed('PT returned invalid version: ' + sp[1])
        elif kw == 'PROXY':
            if sp[1] != 'DONE':
                raise PTConnectFailed('PT returned invalid info: ' + ln)
        elif kw == 'CMETHOD':
            vals = sp[1].split(' ')
            if vals[0] == CFG['ptname']:
                host, port = vals[2].split(':')
                CFG['_ptcli'] = (
                    socks.PROXY_TYPES[vals[1].upper()], host, int(port),
                    True, CFG['ptargs'][:255], CFG['ptargs'][255:] or '\0')
        elif kw == 'SMETHOD':
            vals = sp[1].split(' ')
            if vals[0] == CFG['ptname']:
                print('===== Server information =====')
                print('"server": "%s",' % vals[1])
                print('"ptname": "%s",' % vals[0])
                for opt in vals[2:]:
                    if opt.startswith('ARGS:'):
                        print('"ptargs": "%s",' % opt[5:].replace(',', ';'))
                print('==============================')
        elif kw in ('CMETHODS', 'SMETHODS') and sp[1] == 'DONE':
            print(logtime(), 'PT started successfully.')
            return


def runpt():
    global CFG, PTREADY
    while CFG['_run']:
        print(logtime(), 'Starting PT...')
        proc = checkproc()
        # If error then die
        parseptline(proc.stdout)
        PTREADY.set()
        # Use this to block
        try:
            while proc.stdout.readline():
                pass
        except BrokenPipeError:
            pass
        PTREADY.clear()
        print(logtime(), 'PT died.')


try:
    if len(sys.argv) == 1:
        pass
    elif len(sys.argv) == 2:
        if sys.argv[1] in ('-h', '--help'):
            print('usage: python3 %s [-c|-s] [config.json]' % __file__)
            sys.exit(0)
        else:
            CFG = json.load(open(sys.argv[1], 'r'))
    elif len(sys.argv) == 3:
        CFG = json.load(open(sys.argv[2], 'r'))
        if sys.argv[1] == '-c':
            CFG['role'] = 'client'
        elif sys.argv[1] == '-s':
            CFG['role'] = 'server'
except Exception as ex:
    print(ex)
    print('usage: python3 %s [-c|-s] [config.json]' % sys.argv[0])
    sys.exit(1)

PT_PROC = None
PTREADY = threading.Event()

try:
    CFG['_run'] = True
    if CFG['role'] == 'client':
        ptthr = threading.Thread(target=runpt)
        ptthr.daemon = True
        ptthr.start()
        PTREADY.wait()
        host, port = CFG['local'].split(':')
        server = ThreadedTCPServer(
            (host, int(port)), ThreadedTCPRequestHandler)
        server.serve_forever()
    else:
        runpt()
finally:
    CFG['_run'] = False
    if PT_PROC:
        PT_PROC.kill()
