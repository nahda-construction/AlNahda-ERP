"""No-credential policy and real socket relay tests, no external calls."""
import asyncio
import importlib.util
from pathlib import Path
import socket
import unittest
from unittest.mock import AsyncMock, patch

spec = importlib.util.spec_from_file_location("egress", Path(__file__).resolve().parents[1] / "ops/review-worker/egress_policy.py")
egress = importlib.util.module_from_spec(spec)
spec.loader.exec_module(egress)

def request(host):
    return f"CONNECT {host}:443 HTTP/1.1\r\nHost: {host}:443\r\n\r\n".encode()

class PolicyTests(unittest.TestCase):
    def test_exact_hosts(self):
        for host in egress.ALLOWED:
            self.assertEqual(egress.parse_header(request(host)), host)

    def test_block_other_hosts(self):
        for host in ("github.com", "localhost", "127.0.0.1", "169.254.169.254",
                     "host.docker.internal", "chatgpt.com.evil.test", "chatgpt.com.",
                     "CHATGPT.COM", "evil.chatgpt.com"):
            with self.subTest(host=host), self.assertRaises(ValueError):
                egress.parse_header(request(host))

    def test_block_malformed_requests(self):
        for raw in (b"GET https://chatgpt.com/ HTTP/1.1\r\n\r\n",
                    b"CONNECT chatgpt.com:80 HTTP/1.1\r\n\r\n",
                    b"CONNECT chatgpt.com:443 HTTP/1.1\r\nHost: evil.test:443\r\n\r\n",
                    b"CONNECT chatgpt.com:443 HTTP/1.1\r\nContent-Length: 1\r\n\r\n",
                    b"CONNECT chatgpt.com:443 HTTP/1.1\r\nHost: chatgpt.com:443\r\nHost: chatgpt.com:443\r\n\r\n",
                    b"CONNECT chatgpt.com:443 HTTP/1.1\r\nProxy-Authorization: token\r\n\r\n",
                    b"x"*8193):
            with self.subTest(raw=raw[:80]), self.assertRaises(ValueError):
                egress.parse_header(raw)

    def test_ip_policy(self):
        self.assertTrue(egress.public_ip("8.8.8.8"))
        for value in ("10.0.0.1", "192.168.1.1", "172.16.0.1", "127.0.0.1",
                      "169.254.169.254", "100.64.0.1", "224.0.0.1", "0.0.0.0",
                      "240.0.0.1", "::1", "::ffff:8.8.8.8"):
            self.assertFalse(egress.public_ip(value), value)

class SocketTests(unittest.IsolatedAsyncioTestCase):
    async def test_dns_rebinding_mixed_answers_denied(self):
        loop = asyncio.get_running_loop()
        infos = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))
                 for ip in ("8.8.8.8", "127.0.0.1")]
        with patch.object(loop, "getaddrinfo", new=AsyncMock(return_value=infos)), \
             patch.object(asyncio, "open_connection", new=AsyncMock()) as connect:
            with self.assertRaises(ValueError):
                await egress.connect_public("chatgpt.com")
            connect.assert_not_awaited()

    async def test_connect_uses_literal(self):
        loop = asyncio.get_running_loop()
        infos = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]
        with patch.object(loop, "getaddrinfo", new=AsyncMock(return_value=infos)), \
             patch.object(asyncio, "open_connection", new=AsyncMock(return_value=(None, None))) as connect:
            await egress.connect_public("chatgpt.com")
            connect.assert_awaited_once_with("8.8.8.8", 443, family=socket.AF_INET)

    async def test_denied_request_never_connects(self):
        connector = AsyncMock()
        server = await asyncio.start_server(lambda r,w: egress.handle(r,w,connector), "127.0.0.1", 0, limit=egress.MAX_HEADER)
        async with server:
            reader, writer = await asyncio.open_connection(*server.sockets[0].getsockname())
            writer.write(request("github.com"))
            await writer.drain()
            self.assertIn(b"403 Forbidden", await asyncio.wait_for(reader.read(), 2))
            connector.assert_not_awaited()
            writer.close()
            await writer.wait_closed()

    async def test_allowed_tunnel_relays_synthetic_bytes(self):
        async def echo(r,w):
            data = await r.readexactly(4)
            w.write(data)
            await w.drain()
            w.close()
            await w.wait_closed()
        upstream = await asyncio.start_server(echo, "127.0.0.1", 0)
        async with upstream:
            async def connector(host):
                self.assertEqual(host, "chatgpt.com")
                return await asyncio.open_connection(*upstream.sockets[0].getsockname())
            proxy = await asyncio.start_server(lambda r,w: egress.handle(r,w,connector), "127.0.0.1", 0, limit=egress.MAX_HEADER)
            async with proxy:
                reader, writer = await asyncio.open_connection(*proxy.sockets[0].getsockname())
                writer.write(request("chatgpt.com"))
                await writer.drain()
                self.assertIn(b"200 Connection Established", await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 2))
                writer.write(b"test")
                await writer.drain()
                self.assertEqual(await asyncio.wait_for(reader.readexactly(4), 2), b"test")
                writer.close()
                await writer.wait_closed()

if __name__ == "__main__":
    unittest.main()
