#!/usr/bin/env python3
"""Proxy TCP de captura entre o gateway CCM21 e a nuvem AirControlBase.

Gateway (Server IP apontado pra cá) -> este proxy :21880 -> nuvem 52.57.81.51:21880.
Tudo que passa nos dois sentidos é gravado em JSONL (um pacote por linha, em hex),
pra decodificar o protocolo de comando.

Porta de injeção (só localhost, default 21881): cada linha recebida é um hex que é
mandado pro gateway como se viesse da nuvem. Ex.:
    echo "aabbcc" | nc 127.0.0.1 21881
Uso: python3 acb_proxy.py [--log DIR] [--listen-port 21880] [--upstream IP:PORT]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time

UPSTREAM_DEFAULT = "52.57.81.51:21880"


class Proxy:
    def __init__(self, upstream: tuple[str, int], log_dir: str) -> None:
        self.upstream = upstream
        self.log_dir = log_dir
        self.gateways: dict[int, asyncio.StreamWriter] = {}
        self.conn_seq = 0
        os.makedirs(log_dir, exist_ok=True)

    def log(self, conn: int, direction: str, data: bytes | None = None, note: str = "") -> None:
        now = time.time()
        rec = {
            "ts": round(now, 3),
            "t": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
            "conn": conn,
            "dir": direction,
        }
        if data is not None:
            rec["len"] = len(data)
            rec["hex"] = data.hex()
        if note:
            rec["note"] = note
        path = os.path.join(self.log_dir, time.strftime("capture-%Y%m%d.jsonl"))
        with open(path, "a") as fh:
            fh.write(json.dumps(rec) + "\n")

    async def pipe(self, conn: int, direction: str, reader, writer) -> None:
        try:
            while data := await reader.read(65536):
                self.log(conn, direction, data)
                writer.write(data)
                await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError, OSError) as err:
            self.log(conn, direction, note=f"erro: {err!r}")
        finally:
            writer.close()

    async def handle_gateway(self, gw_reader, gw_writer) -> None:
        self.conn_seq += 1
        conn = self.conn_seq
        peer = gw_writer.get_extra_info("peername")
        self.log(conn, "open", note=f"gateway {peer}")
        try:
            up_reader, up_writer = await asyncio.wait_for(
                asyncio.open_connection(*self.upstream), timeout=10
            )
        except (OSError, asyncio.TimeoutError) as err:
            self.log(conn, "open", note=f"falha upstream: {err!r}")
            gw_writer.close()
            return
        self.gateways[conn] = gw_writer
        try:
            await asyncio.gather(
                self.pipe(conn, "gw>cloud", gw_reader, up_writer),
                self.pipe(conn, "cloud>gw", up_reader, gw_writer),
            )
        finally:
            self.gateways.pop(conn, None)
            self.log(conn, "close")

    async def handle_inject(self, reader, writer) -> None:
        try:
            while line := await reader.readline():
                text = line.decode(errors="replace").strip().replace(" ", "")
                if not text:
                    continue
                try:
                    data = bytes.fromhex(text)
                except ValueError:
                    writer.write(b"hex invalido\n")
                    continue
                if not self.gateways:
                    writer.write(b"nenhum gateway conectado\n")
                    continue
                for conn, gw in list(self.gateways.items()):
                    self.log(conn, "inject>gw", data)
                    gw.write(data)
                    await gw.drain()
                writer.write(f"enviado {len(data)} bytes\n".encode())
                await writer.drain()
        finally:
            writer.close()


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default="/config/acb_capture")
    ap.add_argument("--listen-port", type=int, default=21880)
    ap.add_argument("--inject-port", type=int, default=21881)
    ap.add_argument("--upstream", default=UPSTREAM_DEFAULT)
    args = ap.parse_args()
    host, port = args.upstream.rsplit(":", 1)
    proxy = Proxy((host, int(port)), args.log)
    srv = await asyncio.start_server(proxy.handle_gateway, "0.0.0.0", args.listen_port)
    inj = await asyncio.start_server(proxy.handle_inject, "127.0.0.1", args.inject_port)
    proxy.log(0, "start", note=f"listen {args.listen_port} -> {args.upstream}")
    async with srv, inj:
        await asyncio.gather(srv.serve_forever(), inj.serve_forever())


if __name__ == "__main__":
    asyncio.run(main())
