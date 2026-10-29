import unittest

from verify_cache_observability import (
    FAMILIES, changes, command_statistics, counters, metric_key,
    monitor_commands, wire_operations,
)


class CacheObservabilityOracles(unittest.TestCase):
    def test_request_labels_are_canonical_and_missing_series_not_zeroed(self):
        exposition = ('skipper_cache_request_total{tier="l2",result="fresh"} 2\n'
                      'skipper_cache_l2_operations_total{outcome="miss",purpose="metadata",operation="get"} 3\n')
        parsed = counters(exposition)
        self.assertEqual(parsed[metric_key(FAMILIES[0], result="fresh", tier="l2")], 2)
        self.assertNotIn(metric_key(FAMILIES[0], result="error", tier="none"), parsed)

    def test_changes_detects_new_series_and_preserves_old_ones(self):
        self.assertEqual(changes({"old": 2}, {"old": 2, "new": 1}), {"new": 1})

    def test_monitor_ignores_control_commands_and_decodes_keys(self):
        log = ('OK\n1 [0 127.0.0.1:1] "INFO" "commandstats"\n'
               '2 [0 127.0.0.1:1] "GET" "vary:key"\n'
               '3 [0 127.0.0.1:1] "SET" "key" "{\\"Payload\\":\\"body\\"}"\n'
               '4 [0 127.0.0.1:1] "DEL" "key"\n')
        self.assertEqual(monitor_commands(log),
                         [("get", "vary:key"), ("set", "key"), ("del", "key")])

    def test_wire_oracle_tracks_existence_and_metadata_independently(self):
        operations = wire_operations(
            [("get", "vary:key"), ("get", "key"), ("set", "key"),
             ("get", "key"), ("del", "key"), ("get", "key")], set())
        self.assertEqual(operations, {
            metric_key(FAMILIES[1], operation="get", purpose="metadata", outcome="miss"): 1,
            metric_key(FAMILIES[1], operation="get", purpose="response", outcome="miss"): 2,
            metric_key(FAMILIES[1], operation="get", purpose="response", outcome="success"): 1,
            metric_key(FAMILIES[1], operation="set", purpose="response", outcome="success"): 1,
            metric_key(FAMILIES[1], operation="delete", purpose="response", outcome="success"): 1,
        })

    def test_delete_absent_key_is_success_not_clean_get_miss(self):
        self.assertEqual(wire_operations([("del", "absent")], set()), {
            metric_key(FAMILIES[1], operation="delete", purpose="response", outcome="success"): 1})

    def test_commandstats_preserves_acl_rejections_separate_from_calls(self):
        self.assertEqual(command_statistics(
            '# Commandstats\ncmdstat_get:calls=5,usec=10,usec_per_call=2.00,rejected_calls=2,failed_calls=0\n'
            'cmdstat_del:calls=1,usec=1,rejected_calls=3\n'
            'cmdstat_info:calls=10,usec=1,rejected_calls=0\n'), {
                "get": {"calls": 5, "rejected": 2}, "delete": {"calls": 1, "rejected": 3}})

    def test_absence_does_not_invent_storage_counters(self):
        self.assertEqual(counters("# HELP skipper_cache_request_total description\n"), {})
