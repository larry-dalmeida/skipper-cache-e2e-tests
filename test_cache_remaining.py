import contextlib
import socket
import socketserver
import threading
import unittest

from verify_cache_remaining import (
    FaultRelay, finite_positive, query_values, resp_command, samples, scalar,
)


class RemainingAcceptanceOracles(unittest.TestCase):
    def test_fragmented_resp_is_not_a_wire_attempt(self):
        frame = b"*2\r\n$3\r\nGET\r\n$3\r\nkey\r\n"
        for size in range(len(frame)):
            self.assertIsNone(resp_command(frame[:size]))
        self.assertEqual(resp_command(frame), (len(frame), [b"GET", b"key"]))

    def test_pipelined_resp_preserves_the_next_command(self):
        frame = b"*2\r\n$3\r\nGET\r\n$3\r\nkey\r\n"
        size, values = resp_command(frame + frame)
        self.assertEqual(size, len(frame))
        self.assertEqual(values, [b"GET", b"key"])
        self.assertEqual(resp_command((frame + frame)[size:]), (size, values))

    def test_malformed_resp_is_explicit_failure(self):
        with self.assertRaises(AssertionError):
            resp_command(b"*1\r\n$3\r\nGETxx")

    def test_scalar_requires_observed_single_sample(self):
        with self.assertRaises(AssertionError):
            scalar("", "missing")
        with self.assertRaises(AssertionError):
            scalar('counter{key="a"} 1\ncounter{key="b"} 2\n', "counter")
        self.assertEqual(scalar('counter{key="a"} 1\n', "counter", key="a"), 1)

    def test_label_selection_does_not_replace_missing_with_zero(self):
        self.assertEqual(samples('counter{key="a"} 1\n', "counter", key="b"), [])

    def test_positive_query_must_include_exact_exercised_series(self):
        result = {"status": "success", "data": {"result": [
            {"metric": {"instance": "queue", "key": "cache.reval_dropped"}, "value": [1, "0.5"]}]}}
        self.assertEqual(len(finite_positive(result, instance="queue")), 1)
        with self.assertRaises(AssertionError):
            finite_positive(result, instance="eviction")

    def test_no_samples_zero_nan_inf_are_not_success(self):
        for value in (None, "0", "NaN", "+Inf", "-1"):
            result = {"status": "success", "data": {"result": [] if value is None else [
                {"metric": {}, "value": [1, value]}]}}
            with self.subTest(value=value), self.assertRaises(AssertionError):
                finite_positive(result)

    def test_blackhole_holds_socket_without_forwarding_and_new_connection_recovers(self):
        class Backend(socketserver.BaseRequestHandler):
            def handle(self):
                if self.request.recv(1024):
                    self.request.sendall(b"$5\r\nvalue\r\n")

        with contextlib.ExitStack() as stack:
            backend = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Backend)
            backend.daemon_threads = True
            relay = FaultRelay(backend.server_address)
            for server in (backend, relay):
                threading.Thread(target=server.serve_forever, daemon=True).start()
                stack.callback(server.server_close)
                stack.callback(server.shutdown)
            frame = b"*2\r\n$3\r\nGET\r\n$3\r\nkey\r\n"
            relay.blackholed.set()
            with socket.create_connection(relay.server_address, timeout=1) as client:
                client.settimeout(.1)
                client.sendall(frame)
                with self.assertRaises(socket.timeout):
                    client.recv(1024)
            self.assertEqual(relay.events[0]["fault"], "blackhole")
            self.assertFalse(relay.events[0]["forwarded"])
            relay.blackholed.clear()
            with socket.create_connection(relay.server_address, timeout=1) as client:
                client.sendall(frame)
                self.assertEqual(client.recv(1024), b"$5\r\nvalue\r\n")
            self.assertTrue(relay.events[1]["forwarded"])

    def test_query_vectors_compare_labels_and_values_not_iteration_order(self):
        a = {"metric": {"instance": "a"}, "value": [1, "2"]}
        b = {"metric": {"instance": "b"}, "value": [1, "3"]}
        self.assertEqual(query_values({"data": {"result": [a, b]}}),
                         query_values({"data": {"result": [b, a]}}))
        with self.assertRaises(AssertionError):
            query_values({"data": {"result": [a, a]}})
