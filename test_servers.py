#!/usr/bin/env python3
import json, socket, ssl

addrs = [
    'bchtest:qzfxpjt3xm2rfzhxsef6f296a0zavvk6ncx7f2cnw7',
    'bchtest:qrynfuk47hxqj4sgpt62v3yzzpnjw6l2hvnc4p897k',
]

servers = [
    ('chipnet.bch.ninja', 50002),
    ('chipnet.imaginary.cash', 50002),
    ('chipnet.c3-soft.com', 64002),
]

def query(host, port, method, params):
    ctx = ssl.create_default_context()
    sock = socket.create_connection((host, port), timeout=10)
    ssock = ctx.wrap_socket(sock, server_hostname=host)
    req = json.dumps({'jsonrpc': '2.0', 'method': method, 'params': params, 'id': 1}) + '\n'
    ssock.sendall(req.encode())
    data = b''
    while b'\n' not in data:
        data += ssock.recv(65536)
    ssock.close()
    return json.loads(data.decode()).get('result')

for addr in addrs:
    print(f'\n{addr}')
    for host, port in servers:
        try:
            hist = query(host, port, 'blockchain.address.get_history', [addr])
            bal = query(host, port, 'blockchain.address.get_balance', [addr])
            utxos = query(host, port, 'blockchain.address.listunspent', [addr])
            total = bal.get('confirmed', 0) + bal.get('unconfirmed', 0)
            print(f'  {host}: {len(hist)} txs, balance={total}, {len(utxos)} utxos')
        except Exception as e:
            print(f'  {host}: error - {e}')
