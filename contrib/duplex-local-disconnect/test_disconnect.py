# SPDX-License-Identifier: GPL-2.0-only
import contextlib
import datetime
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import urllib.error

import disconnect as d


def line(message, slot=1, second=1):
    return 'M: 2026-10-21 12:00:%02d.000 DMR Slot %d, %s\n' % (second, slot, message)


class DetectorTests(unittest.TestCase):
    def setUp(self):
        self.detector = d.Detector({'TEST1'})

    def start(self, slot=1):
        self.detector.feed(line('received network voice header from NET to TG 91', slot))

    def test_both_slots_and_late_entry(self):
        for slot in (1, 2):
            self.start(slot)
            for kind in ('voice header', 'late entry'):
                event = self.detector.feed(line('received RF %s from TEST1 to TG 4000' % kind, slot, 2))
                self.assertEqual(event[:3], (slot, 'TEST1', 91))

    def test_other_slot_and_idle_ignored(self):
        self.start(1)
        self.assertIsNone(self.detector.feed(line('received RF voice header from TEST1 to TG 4000', 2)))

    def test_private_other_group_unauthorized_ignored(self):
        self.start()
        for message in ('received RF voice header from OTHER to TG 4000',
                        'received RF voice header from TEST1 to 4000',
                        'received RF voice header from TEST1 to TG 400',
                        'received RF voice header from TEST1 to TG 91'):
            self.assertIsNone(self.detector.feed(line(message, second=2)))

    def test_end_timeout_private_data_reset(self):
        for message in (*d.END, 'received network voice header from NET to 1234567',
                        'received network data header from NET to TG 91'):
            self.start()
            self.detector.feed(line(message, second=2))
            self.assertIsNone(self.detector.feed(line('received RF voice header from TEST1 to TG 4000', second=3)))

    def test_priming_never_dispatches(self):
        self.start()
        self.assertIsNone(self.detector.feed(line('received RF voice header from TEST1 to TG 4000'), dispatch=False))

    def test_expired_and_invalid_timestamps(self):
        self.start()
        command = line('received RF voice header from TEST1 to TG 4000', second=2)
        self.assertIsNone(self.detector.feed(command.replace('12:00:', '12:11:')))
        self.assertIsNone(self.detector.feed(command.replace('12:00:', '99:00:')))


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'host').write_text('[General]\nDuplex=1\n[Log]\nFileLevel=2\n[DMR Network]\nEnable=1\nAddress=127.0.0.1\n')
        (self.root / 'gateway').write_text('[DMR Network 1]\nEnabled=1\nAddress=5051.master.brandmeister.network\nId=123456701\n')
        (self.root / 'key').write_text('[key]\napikey="test-only-never-a-real-key"\n')
        (self.root / 'settings').write_text('[Disconnect]\nDeviceId=123456701\nAllowedCallers=TEST1\nHostConfig=%s\nGatewayConfig=%s\nKeyFile=%s\n' %
                                          (self.root / 'host', self.root / 'gateway', self.root / 'key'))
        self.settings = d.Settings(self.root / 'settings')

    def test_local_validation_without_network(self):
        with patch('urllib.request.build_opener') as opener:
            self.assertEqual(self.settings.credentials(), 'test-only-never-a-real-key')
            opener.assert_not_called()

    def test_simplex_rejected(self):
        path = self.root / 'host'
        path.write_text(path.read_text().replace('Duplex=1', 'Duplex=0'))
        with self.assertRaises(ValueError):
            self.settings.credentials()

    def test_multiple_networks_rejected(self):
        path = self.root / 'gateway'
        path.write_text(path.read_text() + '[DMR Network 2]\nEnabled=1\n')
        with self.assertRaises(ValueError):
            self.settings.credentials()

    def test_wrong_device_and_host_rejected(self):
        path = self.root / 'gateway'
        original = path.read_text()
        for content in (original.replace('123456701', '765432101'),
                        original.replace('5051.master.brandmeister.network', 'other.example')):
            path.write_text(content)
            with self.assertRaises(ValueError):
                self.settings.credentials()

    def test_actions_use_same_slot_and_order(self):
        for slot in (1, 2):
            opener = Mock()
            opener.open.side_effect = lambda *a, **k: contextlib.nullcontext(Mock(status=200))
            with patch('urllib.request.build_opener', return_value=opener):
                d.drop(self.settings, slot, 'TEST1', 91)
            urls = [call.args[0].full_url for call in opener.open.call_args_list]
            self.assertEqual(urls, ['https://api.brandmeister.network/v2/device/123456701/action/%s/%s' % (a, slot)
                                    for a in ('dropDynamicGroups', 'dropCallRoute')])

    def test_first_failure_still_attempts_drop_route_no_retry(self):
        opener = Mock()
        opener.open.side_effect = [urllib.error.HTTPError('https://example', 403, 'Denied', {}, None),
                                  contextlib.nullcontext(Mock(status=200))]
        with patch('urllib.request.build_opener', return_value=opener), self.assertLogs(level='ERROR'):
            d.drop(self.settings, 1, 'TEST1', 91)
        self.assertEqual(opener.open.call_count, 2)

    def test_unauthorized_drop_never_sends(self):
        with patch('urllib.request.build_opener') as opener:
            for slot, caller in ((0, 'TEST1'), (1, 'OTHER')):
                with self.assertRaises(ValueError):
                    d.drop(self.settings, slot, caller, 91)
            opener.assert_not_called()

    def test_redirect_rejected(self):
        self.assertIsNone(d.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://other.example'))


class DispatcherTests(unittest.TestCase):
    def test_stale_duplicate_and_slot_isolation(self):
        dispatcher = d.Dispatcher(Mock())
        with patch.object(d.time, 'time', return_value=100), patch.object(d.time, 'monotonic', return_value=50), \
                patch.object(d.threading, 'Thread') as thread:
            self.assertFalse(dispatcher.submit((1, 'TEST1', 91, 94)))
            self.assertTrue(dispatcher.submit((1, 'TEST1', 91, 100)))
            self.assertFalse(dispatcher.submit((1, 'TEST1', 91, 100)))
            self.assertTrue(dispatcher.submit((2, 'TEST1', 91, 100)))
            self.assertEqual(thread.call_count, 2)

    def test_worker_failure_releases_slot(self):
        dispatcher = d.Dispatcher(Mock())
        with patch.object(d.time, 'time', return_value=100), patch.object(d, 'drop', side_effect=ValueError), \
                patch.object(d.threading, 'Thread') as thread, self.assertLogs(level='ERROR'):
            dispatcher.submit((1, 'TEST1', 91, 100))
            thread.call_args.kwargs['target']()
        self.assertFalse(dispatcher.pending)


class FollowerTests(unittest.TestCase):
    def test_startup_partial_line_and_daily_rotation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / 'MMDVM-2026-10-21.log'
            header = line('received network voice header from NET to TG 91')
            command = line('received RF voice header from TEST1 to TG 4000', second=2)
            # Complete historical commands must not replay. A partial new line
            # must stay available until the logging process finishes writing it.
            first.write_text(header + command + command[:40])
            settings = Mock(logdir=root, callers={'TEST1'})
            dispatcher = Mock()
            step = 0

            def advance(_):
                nonlocal step
                step += 1
                if step == 1:
                    dispatcher.submit.assert_not_called()
                    with first.open('a') as output:
                        output.write(command[40:])
                elif step == 2:
                    self.assertEqual(dispatcher.submit.call_count, 1)
                    (root / 'MMDVM-2026-10-22.log').write_text(header + command)
                else:
                    self.assertEqual(dispatcher.submit.call_count, 2)
                    raise KeyboardInterrupt

            with patch.object(d.time, 'sleep', side_effect=advance):
                with self.assertRaises(KeyboardInterrupt):
                    d.follow(settings, dispatcher)


if __name__ == '__main__':
    unittest.main()
