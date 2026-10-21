#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Opt-in WPSD companion: RF TG4000 during a duplex downlink -> BM API."""
import argparse
import configparser
import datetime
import logging
from pathlib import Path
import re
import threading
import time
import urllib.error
import urllib.request

LINE = re.compile(r'^.: (\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d+) DMR Slot ([12]), (.*)$')
RF = re.compile(r'^received RF (?:voice header|late entry) from (\S+) to TG 4000$')
NET = re.compile(r'^received network (?:voice header|late entry) from .* to TG (\d+)$')
END = ('received network end of voice transmission',
       'network watchdog has expired', 'network user has timed out')


def read_ini(path):
    conf = configparser.ConfigParser(interpolation=None, strict=False)
    with open(path) as source:
        conf.read_file(source)
    return conf


class Settings:
    def __init__(self, path):
        conf = read_ini(path)['Disconnect']
        self.device = conf['DeviceId'].strip()
        if not re.fullmatch(r'[1-9][0-9]{6,8}', self.device):
            raise ValueError('DeviceId must be a 7 to 9 digit DMR device ID')
        self.callers = frozenset(conf['AllowedCallers'].upper().split())
        if not self.callers or not all(re.fullmatch(r'[A-Z0-9/]+', x) for x in self.callers):
            raise ValueError('AllowedCallers must contain explicit log callsigns or IDs')
        self.logdir = Path(conf.get('LogDirectory', '/var/log/pi-star'))
        self.host = conf.get('HostConfig', '/etc/mmdvmhost')
        self.gateway = conf.get('GatewayConfig', '/etc/dmrgateway')
        self.key = conf.get('KeyFile', '/etc/bmapi.key')
        self.network = conf.get('NetworkSection', 'DMR Network 1')

    def credentials(self):
        # Fail closed after a dashboard configuration change. Logs do not identify
        # the source network, so multiple enabled gateway networks are unsupported.
        host = read_ini(self.host)
        gateway = read_ini(self.gateway)
        if not host.getboolean('General', 'Duplex'):
            raise ValueError('This workaround requires duplex mode')
        if host.getint('Log', 'FileLevel') not in (1, 2):
            raise ValueError('MMDVMHost file logging must include message-level events')
        if not host.getboolean('DMR Network', 'Enable') or host.get('DMR Network', 'Address') not in ('127.0.0.1', 'localhost'):
            raise ValueError('MMDVMHost must use the local DMRGateway')
        enabled = [s for s in gateway.sections() if re.fullmatch(r'DMR Network [1-9][0-9]*', s)
                   and gateway.getboolean(s, 'Enabled', fallback=False)]
        if enabled != [self.network]:
            raise ValueError('Exactly the configured BrandMeister network must be enabled')
        net = gateway[self.network]
        address = net['Address'].strip().lower()
        if not address.endswith('.brandmeister.network') or net['Id'].strip() != self.device:
            raise ValueError('BrandMeister gateway address or device ID does not match')
        token = read_ini(self.key).get('key', 'apikey').strip().strip('"')
        if not token or '\n' in token or '\r' in token:
            raise ValueError('A BrandMeister API key is required')
        return token


class Detector:
    def __init__(self, callers):
        self.callers = callers
        self.active = {1: None, 2: None}

    def feed(self, line, dispatch=True):
        match = LINE.match(line.strip())
        if not match:
            return None
        stamp, slot, message = match.groups()
        try:
            stamp = datetime.datetime.strptime(stamp, '%Y-%m-%d %H:%M:%S.%f').replace(
                tzinfo=datetime.timezone.utc).timestamp()
        except ValueError:
            return None
        slot = int(slot)
        network = NET.match(message)
        if network:
            self.active[slot] = (stamp, int(network[1]))
            return None
        if message.startswith(END) or message.startswith(('received network voice header',
                                                        'received network late entry',
                                                        'received network data header')):
            self.active[slot] = None
            return None
        rf = RF.match(message)
        active = self.active[slot]
        if (dispatch and rf and rf[1].upper() in self.callers and active
                and active[1] != 4000 and 0 <= stamp - active[0] < 600):
            return slot, rf[1], active[1], stamp
        return None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def drop(settings, slot, caller, group):
    if slot not in (1, 2) or caller.upper() not in settings.callers:
        raise ValueError('Unauthorized caller or slot')
    token = settings.credentials()
    opener = urllib.request.build_opener(NoRedirect())
    logging.info('TG4000 from %s: disconnect slot %s active TG%s', caller, slot, group)
    # Each action is independent: still try to stop the QSO if clearing the
    # subscriptions fails. Do not retry later against a potentially new call.
    for action in ('dropDynamicGroups', 'dropCallRoute'):
        request = urllib.request.Request(
            'https://api.brandmeister.network/v2/device/%s/action/%s/%s' %
            (settings.device, action, slot),
            headers={'Authorization': 'Bearer ' + token,
                     'User-Agent': 'MMDVMHost-duplex-local-disconnect'})
        try:
            with opener.open(request, timeout=5) as response:
                response.read(4096)
                if response.status != 200:
                    raise RuntimeError('Unexpected HTTP status')
            logging.info('%s slot %s: HTTP 200 (verify downlink stops)', action, slot)
        except urllib.error.HTTPError as exc:
            logging.error('%s slot %s: HTTP %s', action, slot, exc.code)
        except Exception as exc:
            # Never log request headers, tokens or response bodies.
            logging.error('%s slot %s failed: %s', action, slot, type(exc).__name__)


class Dispatcher:
    def __init__(self, settings):
        self.settings = settings
        self.pending = set()
        self.last = {}
        self.lock = threading.Lock()

    def submit(self, event):
        slot, caller, group, stamp = event
        if abs(time.time() - stamp) > 5:
            return False
        with self.lock:
            if slot in self.pending or time.monotonic() - self.last.get(slot, -10) < 3:
                return False
            self.pending.add(slot)
            self.last[slot] = time.monotonic()

        def worker():
            try:
                drop(self.settings, slot, caller, group)
            except Exception as exc:
                logging.error('Disconnect rejected: %s', type(exc).__name__)
            finally:
                with self.lock:
                    self.pending.discard(slot)

        threading.Thread(target=worker, daemon=True).start()
        return True


def follow(settings, dispatcher):
    handle, identity = None, None
    detector = Detector(settings.callers)
    try:
        while True:
            files = sorted(settings.logdir.glob('MMDVM-????-??-??.log'))
            if not files:
                time.sleep(0.25)
                continue
            path = files[-1]
            try:
                stat = path.stat()
                current = (path, stat.st_ino)
                if current != identity or (handle and stat.st_size < handle.tell()):
                    first_open = handle is None
                    if handle:
                        handle.close()
                    handle = path.open(errors='replace')
                    identity = current
                    detector = Detector(settings.callers)
                    # Prime active-call state, but never replay commands at start.
                    if first_open:
                        while True:
                            position = handle.tell()
                            line = handle.readline()
                            if not line.endswith('\n'):
                                handle.seek(position)
                                break
                            detector.feed(line, dispatch=False)
                position = handle.tell()
                line = handle.readline()
                if not line.endswith('\n'):
                    handle.seek(position)
                    time.sleep(0.05)
                    continue
                event = detector.feed(line)
                if event:
                    dispatcher.submit(event)
            except FileNotFoundError:
                time.sleep(0.25)
    finally:
        if handle:
            handle.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default='/etc/tg4000-local-disconnect.ini')
    parser.add_argument('--check', action='store_true', help='Validate local settings; no API requests')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    settings = Settings(args.config)
    settings.credentials()
    if args.check:
        logging.info('Configuration valid; no API requests made')
        return
    logging.info('Local TG4000 API disconnect enabled: device %s, callers %s',
                 settings.device, ','.join(sorted(settings.callers)))
    follow(settings, Dispatcher(settings))


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        # Config-parser exceptions can contain a malformed secret line.
        logging.error('Stopped: %s; check configuration files and permissions', type(exc).__name__)
        raise SystemExit(1)
