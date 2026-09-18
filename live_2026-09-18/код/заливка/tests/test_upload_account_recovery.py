"""Offline scenarios for the signed-out Google account recovery.

Drives the real module against a scripted browser and a real (fixture) private
credential store.  Nothing under test is monkeypatched: the fakes stand in for
the browser and the reviewed DOM surface only, and every assertion compares the
actual sequence of credential submissions, not a count.
"""
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
import upload_account_recovery as recovery

REVIEWED_ROOT = ROOT / 'research' / 'ww2_upload_stress_20260914'
UC = 'UClNGNGulV7gPOrMxgzEmL-w'
OTHER_UC = 'UCRKk-bT9jPAFN8NA9w7z0bA'
PROFILE = '28'
LOGIN = 'fixture.account@example.test'
PASSWORD = 'FIXTURE-PASSWORD-NOT-REAL'
TOTP = 'JBSWY3DPEHPK3PXP'


def studio_denied(uc=UC):
    """The real signed-out shape observed live: Studio keeps the channel URL but
    has no identity at all (profile 22, 14.09; profile 28, 16.09)."""
    page = studio(uc)
    page.update(app_uc='', entity=None, session_index='', denied=True)
    return page


def studio(uc=UC, denied=False, verify=False, app_uc=None):
    return {'origin': 'https://studio.youtube.com', 'host': 'studio.youtube.com',
            'path': f'/channel/{uc}/videos/upload', 'ready': 'complete',
            'app_uc': uc if app_uc is None else app_uc,
            'entity': {'type': 'CHANNEL', 'id': uc if app_uc is None else app_uc},
            'session_index': '1', 'chooser_present': False, 'chooser_has_target': False,
            'chooser_target_signed_out': False, 'denied': denied, 'lang': 'en',
            'signals': {'verify': verify, 'phone': False, 'passkey': False, 'captcha': False,
                        'wrong_password': False, 'wrong_code': False, 'rate_limit': False}}


def google(path, *, has_target=True, signed_out=True, lang='en', **signals):
    base = {'verify': False, 'phone': False, 'passkey': False, 'captcha': False,
            'wrong_password': False, 'wrong_code': False, 'rate_limit': False}
    base.update(signals)
    return {'origin': 'https://accounts.google.com', 'host': 'accounts.google.com',
            'path': path, 'ready': 'complete', 'app_uc': '', 'entity': None,
            'session_index': '', 'chooser_present': path.endswith('accountchooser'),
            'chooser_has_target': has_target, 'chooser_target_signed_out': signed_out,
            'chooser_target_authuser': ('-1' if signed_out else '0') if has_target else None,
            'chooser_entry_count': 3 if has_target else 2, 'lang': lang,
            'chooser_other_mode': False,
            'denied': False, 'signals': base}


def signin_prompt():
    return {'origin': 'https://www.youtube.com', 'host': 'www.youtube.com',
            'path': '/signin_prompt', 'ready': 'complete', 'app_uc': '', 'entity': None,
            'session_index': '', 'chooser_present': False, 'chooser_has_target': False,
            'chooser_target_signed_out': False, 'denied': False, 'lang': 'en',
            'signals': {'verify': False, 'phone': False, 'passkey': False, 'captcha': False,
                        'wrong_password': False, 'wrong_code': False, 'rate_limit': False}}


class Element:
    def __init__(self, driver, kind, value=''):
        self.driver, self.kind, self._value = driver, kind, value

    def is_displayed(self):
        return True

    def is_enabled(self):
        return True

    def get_attribute(self, name):
        return self._value if name == 'value' else ''

    @property
    def text(self):
        return 'Next' if self.kind == 'next' else ''

    def click(self):
        self.driver.events.append('click:' + self.kind)
        if self.kind == 'next':
            self.driver.advance()
        else:
            self.driver.focused = self.kind


class FakeDriver:
    """Scripted Google/Studio pages.  Records every input and click in order."""

    def __init__(self, pages):
        self.pages = list(pages)
        self.events = []
        self.inserted = []          # ordered list of stages that received text
        self.focused = ''
        self.field_value = ''

    @property
    def page(self):
        return self.pages[0]

    def advance(self):
        if len(self.pages) > 1:
            self.pages.pop(0)
            self.focused = ''
            self.field_value = ''

    @property
    def current_url(self):
        return self.page['origin'] + self.page['path']

    def get(self, url):
        self.events.append('get')
        # youtube.com/signin while signed out lands on Google's chooser, and a
        # Studio navigation after a completed sign-in lands on Studio: both are
        # modelled as "move to the next scripted page".
        if 'youtube.com/signin' in url and self.page['host'] != 'accounts.google.com':
            self.advance()
        elif 'studio.youtube.com' in url and self.page['host'] != 'studio.youtube.com':
            self.advance()

    def execute_cdp_cmd(self, method, params=None):
        if method == 'Input.insertText':
            stage = {'identifier': 'identifier', 'pwd': 'password',
                     'totp': 'totp'}.get(self._stage(), self._stage())
            self.inserted.append(stage)
            self.field_value = (params or {}).get('text', '')
            self.events.append('insert:' + stage)
        elif method == 'Input.dispatchMouseEvent' and (params or {}).get('type') == 'mouseReleased':
            self.events.append('mouse_click')
            if self._stage() in ('pwd', 'totp', 'accountchooser'):
                self.advance()
        return {}

    def _stage(self):
        return (self.page['path'] or '').rsplit('/', 1)[-1]

    def find_elements(self, by, selector):
        stage, host = self._stage(), self.page['host']
        if host != 'accounts.google.com':
            return []
        if 'identifierId' in selector and stage == 'identifier':
            return [Element(self, 'identifier', self.field_value)]
        if 'Passwd' in selector and stage == 'pwd':
            return [Element(self, 'pwd', self.field_value)]
        if 'totpPin' in selector and stage == 'totp':
            return [Element(self, 'totp', self.field_value)]
        if selector == 'button' and stage == 'identifier':
            return [Element(self, 'next')]
        return []

    def execute_script(self, script, *args):
        page, stage = self.page, self._stage()
        if 'chooser_target_signed_out' in script:
            return dict(page)
        if 'signals:{verify' in script:                      # reviewed READ_DOM
            # Faithful to research/ww2_upload_stress_20260914/auth_browser.py:208 —
            # it has no captcha key, which is why the module reads a second set.
            reviewed_keys = ('verify', 'authenticator', 'wrong_password', 'wrong_code',
                             'phone', 'passkey', 'rate_limit')
            return {'origin': page['origin'], 'path': page['path'], 'title': '',
                    'headings': [],
                    'signals': {k: bool(page['signals'].get(k)) for k in reviewed_keys},
                    'inputs': [], 'buttons': []}
        if 'captcha:' in script and 'recaptcha' in script:   # our own signal set
            # Mirrors the live rule: a recaptcha node counts only when it is
            # actually visible, or when the URL is the challenge itself.
            captcha = (bool(page['signals'].get('captcha'))
                       or bool(page.get('recaptcha_visible'))
                       or '/challenge/recaptcha' in page['path'])
            return {'lang': page.get('lang', 'en'), 'host': page['host'],
                    'path': page['path'], 'captcha': captcha,
                    **{k: bool(page['signals'].get(k)) for k in
                       ('phone', 'passkey', 'wrong_password',
                        'wrong_code', 'rate_limit')}}

        if 'elementFromPoint' in script:
            if page.get('shifted_to_remove'):
                return {'btype': '', 'identifier': False, 'text': 'remove an account'}
            if page.get('chooser_has_target') and not page.get('remove_mode'):
                return {'btype': 'multipleChoiceIdentifier', 'identifier': True, 'text': ''}
            return {'btype': '', 'identifier': False, 'text': 'use another account'}
        if 'use another account' in script:
            if stage != 'accountchooser' or page.get('remove_mode'):
                return None
            if not str(page.get('lang', 'en')).lower().startswith('en'):
                return None
            return {'x': 30, 'y': 40, 'jsname': 'rwl3qc'}
        if 'data-identifier' in script and 'accountchooser' in script:
            if (stage == 'accountchooser' and page['chooser_has_target']
                    and not page.get('remove_mode')):
                return {'x': 10, 'y': 20, 'origin': page['origin'], 'path': page['path'],
                        'account_matches': True}
            return None
        if 'String(document.documentElement.lang' in script:
            return page.get('lang', 'en')
        if 'identifierId' in script:                          # identifier freshness
            expected = json.loads(script.rsplit(',', 1)[1].rstrip(')'))
            return stage == 'identifier' and self.focused == 'identifier' and self.field_value == expected
        if 'challenge/pwd' in script or 'challenge/totp' in script:
            want = 'pwd' if 'challenge/pwd' in script else 'totp'
            if stage != want:
                return None
            return {'input': {'x': 1, 'y': 2}, 'next': {'x': 3, 'y': 4},
                    'empty': self.field_value == '', 'focused': self.focused == want,
                    'account_matches': True}
        return None


class Clock:
    """Deterministic wall clock: it only advances when the flow pauses, so the
    real 30-second TOTP-window logic runs instead of being bypassed."""

    def __init__(self, start=1789545600.0):
        self.now = float(start)

    def __call__(self):
        return self.now

    def pause(self, seconds):
        self.now += float(seconds or 0)


class Lease:
    def __init__(self, lost=False):
        self.lost = lost
        self.checks = 0

    def __call__(self):
        self.checks += 1
        if self.lost:
            raise RuntimeError('Fixture lease lost: another worker owns this profile')


class RecoveryTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Path(self.tmp.name) / 'google_accounts.json'
        self.write_store([{'profile': PROFILE, 'channel_uc': UC, 'login': LOGIN}])
        os.environ[recovery.ENABLE_ENV] = '1'
        os.environ[recovery.ROOT_ENV] = str(REVIEWED_ROOT)
        os.environ[recovery.ACCOUNTS_ENV] = str(self.store)
        # Never touch the machine's real recovery memory from a test.
        os.environ[recovery.STATE_DIR_ENV] = str(Path(self.tmp.name) / 'state')
        self.addCleanup(self.tmp.cleanup)
        for key in (recovery.ENABLE_ENV, recovery.ROOT_ENV, recovery.ACCOUNTS_ENV,
                    recovery.STATE_DIR_ENV):
            self.addCleanup(os.environ.pop, key, None)
        self.statuses = []
        # Тесты НИКОГДА не шлют настоящие сообщения: приёмник подставной.
        self.pings = []

    def notifier(self):
        def record(profile, channel_uc, reason, label=""):
            self.pings.append({'profile': profile, 'uc': channel_uc,
                               'reason': reason, 'label': label})
            return True
        return record

    def write_store(self, bindings):
        self.store.write_text(json.dumps({
            'accounts': [{'login': LOGIN, 'password': PASSWORD, 'totp': TOTP}],
            'bindings': bindings}), encoding='utf-8')
        self.store.chmod(stat.S_IRUSR | stat.S_IWUSR)

    def recover(self, pages, *, lease=None, **kwargs):
        clock = Clock()
        driver = FakeDriver(pages)
        lease = lease or Lease()
        clock = Clock()
        result = recovery.maybe_recover(driver, PROFILE, UC, lease,
                                        on_status=self.statuses.append,
                                        pause=clock.pause, wall_clock=clock, monotonic=clock,
                                        notify=self.notifier(),
                                        deadline_seconds=120, **kwargs)
        return driver, result


class SignedOutRecoveryTest(RecoveryTestBase):
    def test_fully_signed_out_account_is_restored_and_exact_channel_proved(self):
        driver, result = self.recover([
            studio_denied(),                       # what the worker really sees
            google('/v3/signin/accountchooser'),
            google('/v3/signin/challenge/pwd'),
            google('/v3/signin/challenge/totp'),
            signin_prompt(),
            studio(),
        ])
        self.assertEqual(result['recovered'], True)
        self.assertEqual(result['channel_uc'], UC)
        # Exactly one password and one code, in order, after choosing the account.
        self.assertEqual(driver.inserted, ['password', 'totp'])
        self.assertEqual(self.statuses, [recovery.STATUS_SIGNED_OUT,
                                         recovery.STATUS_RECOVERING,
                                         recovery.STATUS_AWAITING_2FA,
                                         recovery.STATUS_CHANNEL_CONFIRMED])

    def test_account_absent_from_chooser_uses_another_account_then_identifier(self):
        driver, result = self.recover([
            studio_denied(),
            google('/v3/signin/accountchooser', has_target=False),
            google('/v3/signin/identifier'),
            google('/v3/signin/challenge/pwd'),
            google('/v3/signin/challenge/totp'),
            studio(),
        ])
        self.assertEqual(result['recovered'], True)
        self.assertEqual(driver.inserted, ['identifier', 'password', 'totp'])

    def test_signin_prompt_alone_is_not_channel_access(self):
        # Google lands on /signin_prompt and Studio never reaches the exact UC.
        clock = Clock()
        driver, _ = FakeDriver([signin_prompt()]), None
        proof = recovery.verify_exact_channel(driver, UC, Lease(), timeout=0.2, pause=lambda _s: None)
        self.assertEqual(proof['confirmed'], False)


class NoLoginAttemptedTest(RecoveryTestBase):
    def test_other_signed_in_account_selected_is_left_to_the_caller(self):
        driver, result = self.recover([studio(app_uc=OTHER_UC)])
        self.assertIsNone(result)
        self.assertEqual(driver.inserted, [])
        self.assertEqual(self.statuses, [])

    def test_stale_authuser_on_exact_channel_is_not_a_signed_out_account(self):
        # The exact channel is live under a different session index: no login.
        page = studio()
        page['session_index'] = '3'
        driver, result = self.recover([page])
        self.assertIsNone(result)
        self.assertEqual(driver.inserted, [])

    def test_in_studio_verify_belongs_to_the_existing_rendezvous(self):
        driver, result = self.recover([studio(verify=True)])
        self.assertIsNone(result)
        self.assertEqual(driver.inserted, [])

    def test_disabled_flag_makes_the_module_inert(self):
        os.environ.pop(recovery.ENABLE_ENV)

        class Exploding:
            def execute_script(self, *a):
                raise AssertionError('must not read the page when disabled')

        self.assertIsNone(recovery.maybe_recover(Exploding(), PROFILE, UC, Lease()))


class OwnerHelpTest(RecoveryTestBase):
    def test_missing_binding_stops_before_any_click(self):
        self.write_store([])
        clock = Clock()
        driver = FakeDriver([google('/v3/signin/accountchooser')])
        with self.assertRaises(recovery.OwnerHelpNeeded):
            recovery.maybe_recover(driver, PROFILE, UC, Lease(),
                                   on_status=self.statuses.append, pause=clock.pause, wall_clock=clock, monotonic=clock,
                                   notify=self.notifier(), deadline_seconds=120)
        self.assertEqual(driver.events, [])
        self.assertEqual(driver.inserted, [])

    def test_ambiguous_binding_is_refused(self):
        self.write_store([{'profile': PROFILE, 'channel_uc': UC, 'login': LOGIN},
                          {'profile': PROFILE, 'channel_uc': UC, 'login': LOGIN}])
        clock = Clock()
        driver = FakeDriver([google('/v3/signin/accountchooser')])
        with self.assertRaises(recovery.OwnerHelpNeeded):
            recovery.maybe_recover(driver, PROFILE, UC, Lease(), pause=clock.pause, wall_clock=clock, monotonic=clock,
                                   notify=self.notifier(), deadline_seconds=120)
        self.assertEqual(driver.inserted, [])

    def test_world_readable_store_is_refused(self):
        self.store.chmod(0o644)
        clock = Clock()
        driver = FakeDriver([google('/v3/signin/accountchooser')])
        with self.assertRaises(recovery.OwnerHelpNeeded):
            recovery.maybe_recover(driver, PROFILE, UC, Lease(), pause=clock.pause, wall_clock=clock, monotonic=clock,
                                   notify=self.notifier(), deadline_seconds=120)
        self.assertEqual(driver.inserted, [])

    def test_rejected_password_stops_without_a_second_attempt(self):
        clock = Clock()
        driver = FakeDriver([
            studio_denied(),
            google('/v3/signin/accountchooser'),
            google('/v3/signin/challenge/pwd'),
            google('/v3/signin/challenge/pwd', wrong_password=True),
        ])
        with self.assertRaises(recovery.OwnerHelpNeeded) as error:
            recovery.maybe_recover(driver, PROFILE, UC, Lease(),
                                   on_status=self.statuses.append, pause=clock.pause, wall_clock=clock, monotonic=clock,
                                   notify=self.notifier(), deadline_seconds=120)
        self.assertEqual(driver.inserted, ['password'])
        self.assertNotIn(PASSWORD, str(error.exception))
        self.assertEqual(self.statuses[-1], recovery.STATUS_OWNER_HELP)

    def test_rejected_code_stops_without_a_second_attempt(self):
        clock = Clock()
        driver = FakeDriver([
            studio_denied(),
            google('/v3/signin/accountchooser'),
            google('/v3/signin/challenge/pwd'),
            google('/v3/signin/challenge/totp'),
            google('/v3/signin/challenge/totp', wrong_code=True),
        ])
        with self.assertRaises(recovery.OwnerHelpNeeded):
            recovery.maybe_recover(driver, PROFILE, UC, Lease(), pause=clock.pause, wall_clock=clock, monotonic=clock,
                                   notify=self.notifier(), deadline_seconds=120)
        self.assertEqual(driver.inserted, ['password', 'totp'])

    def test_phone_captcha_and_passkey_never_receive_input(self):
        for signal in ('phone', 'captcha', 'passkey'):
            with self.subTest(signal=signal):
                clock = Clock()
                driver = FakeDriver([google('/v3/signin/accountchooser', **{signal: True})])
                with self.assertRaises(recovery.OwnerHelpNeeded):
                    recovery.maybe_recover(driver, PROFILE, UC, Lease(), pause=clock.pause, wall_clock=clock, monotonic=clock,
                                   notify=self.notifier(), deadline_seconds=120)
                self.assertEqual(driver.inserted, [])

    def test_chooser_in_remove_account_mode_is_never_clicked(self):
        # The same chooser renders "Remove account" rows with the same role,
        # identifier and authuser — only data-button-type differs. Clicking one
        # would delete the account from the profile, so this must stop cold.
        page = google('/v3/signin/accountchooser')
        page.update(chooser_has_target=False, chooser_target_signed_out=False,
                    chooser_target_authuser=None, chooser_entry_count=0,
                    chooser_other_mode=True, remove_mode=True)
        clock = Clock()
        driver = FakeDriver([studio_denied(), page])
        with self.assertRaises(recovery.OwnerHelpNeeded) as error:
            recovery.maybe_recover(driver, PROFILE, UC, Lease(), pause=clock.pause,
                                   wall_clock=clock, monotonic=clock, deadline_seconds=120)
        self.assertIn('account-picking', str(error.exception))
        self.assertEqual(driver.inserted, [])
        self.assertNotIn('mouse_click', driver.events)

    def test_a_row_that_shifted_under_the_cursor_is_not_clicked(self):
        # Between measuring the row and pressing, the chooser re-renders and a
        # removal control slides under the cursor: the press must not happen.
        page = google('/v3/signin/accountchooser')
        page['shifted_to_remove'] = True
        clock = Clock()
        driver = FakeDriver([studio_denied(), page])
        with self.assertRaises(recovery.RecoveryBlocked) as error:
            recovery.maybe_recover(driver, PROFILE, UC, Lease(), pause=clock.pause,
                                   wall_clock=clock, monotonic=clock, deadline_seconds=120)
        self.assertIn('not clicking', str(error.exception))
        self.assertNotIn('mouse_click', driver.events)
        self.assertEqual(driver.inserted, [])

    def test_localized_signin_page_stops_instead_of_spinning(self):
        # The reviewed predicates and every stop signal are English-only. A
        # zh-HK page (profile 28's real capture) must be refused loudly, not
        # typed into and not quietly timed out.
        clock = Clock()
        driver = FakeDriver([studio_denied(),
                             google('/v3/signin/accountchooser', lang='zh-HK')])
        with self.assertRaises(recovery.OwnerHelpNeeded) as error:
            recovery.maybe_recover(driver, PROFILE, UC, Lease(), pause=clock.pause, wall_clock=clock, monotonic=clock,
                                   notify=self.notifier(), deadline_seconds=120)
        self.assertIn('English', str(error.exception))
        self.assertEqual(driver.inserted, [])

    def test_captcha_is_caught_even_though_the_reviewed_reader_has_no_such_signal(self):
        clock = Clock()
        driver = FakeDriver([studio_denied(),
                             google('/v3/signin/accountchooser'),
                             google('/v3/signin/challenge/pwd', captcha=True)])
        with self.assertRaises(recovery.OwnerHelpNeeded) as error:
            recovery.maybe_recover(driver, PROFILE, UC, Lease(), pause=clock.pause, wall_clock=clock, monotonic=clock,
                                   notify=self.notifier(), deadline_seconds=120)
        self.assertIn('owner', str(error.exception))
        self.assertEqual(driver.inserted, [])

    def test_signed_in_without_channel_access_asks_for_the_owner(self):
        # Studio refuses, but the chooser proves the bound account is signed in
        # (authuser 0): that is an access problem, never a password problem.
        clock = Clock()
        driver = FakeDriver([studio_denied(),
                             google('/v3/signin/accountchooser', signed_out=False)])
        with self.assertRaises(recovery.OwnerHelpNeeded) as error:
            recovery.maybe_recover(driver, PROFILE, UC, Lease(), pause=clock.pause, wall_clock=clock, monotonic=clock,
                                   notify=self.notifier(), deadline_seconds=120)
        self.assertIn('no access', str(error.exception))
        self.assertEqual(driver.inserted, [])

    def test_cookie_consent_wall_is_reported_not_clicked(self):
        consent = {'origin': 'https://consent.youtube.com', 'host': 'consent.youtube.com',
                   'path': '/m', 'ready': 'complete', 'app_uc': '', 'entity': None,
                   'session_index': '', 'chooser_present': False,
                   'chooser_has_target': False, 'chooser_target_signed_out': False,
                   'denied': False, 'lang': 'vi',
                   'signals': {'verify': False, 'phone': False, 'passkey': False,
                               'captcha': False, 'wrong_password': False,
                               'wrong_code': False, 'rate_limit': False}}
        clock = Clock()
        driver = FakeDriver([consent])
        with self.assertRaises(recovery.OwnerHelpNeeded) as error:
            recovery.maybe_recover(driver, PROFILE, UC, Lease(), pause=clock.pause, wall_clock=clock, monotonic=clock,
                                   notify=self.notifier(), deadline_seconds=120)
        self.assertIn('consent', str(error.exception))
        self.assertEqual(driver.inserted, [])

    def test_sign_in_without_exact_channel_is_not_success(self):
        clock = Clock()
        driver = FakeDriver([
            studio_denied(),
            google('/v3/signin/accountchooser'),
            google('/v3/signin/challenge/pwd'),
            google('/v3/signin/challenge/totp'),
            signin_prompt(),                      # never reaches the exact channel
        ])
        with self.assertRaises(recovery.OwnerHelpNeeded):
            recovery.maybe_recover(driver, PROFILE, UC, Lease(),
                                   on_status=self.statuses.append, pause=clock.pause, wall_clock=clock, monotonic=clock,
                                   notify=self.notifier(), deadline_seconds=120)
        self.assertEqual(self.statuses[-1], recovery.STATUS_OWNER_HELP)


class LockAndPhaseTest(RecoveryTestBase):
    def test_second_worker_on_the_same_profile_never_types(self):
        clock = Clock()
        driver = FakeDriver([
            google('/v3/signin/accountchooser'),
            google('/v3/signin/challenge/pwd'),
        ])
        with self.assertRaises(RuntimeError):
            recovery.maybe_recover(driver, PROFILE, UC, Lease(lost=True), pause=clock.pause, wall_clock=clock, monotonic=clock,
                                   notify=self.notifier(), deadline_seconds=120)
        self.assertEqual(driver.inserted, [])
        self.assertEqual(driver.events, [])

    def test_lease_lost_mid_flow_stops_before_the_next_secret(self):
        lease = Lease()
        original = lease.__call__

        class Flaky(Lease):
            def __call__(self):
                self.checks += 1
                if self.checks > 3:
                    raise RuntimeError('Fixture lease lost mid-flow')

        clock = Clock()
        driver = FakeDriver([
            studio_denied(),
            google('/v3/signin/accountchooser'),
            google('/v3/signin/challenge/pwd'),
            google('/v3/signin/challenge/totp'),
            studio(),
        ])
        with self.assertRaises(RuntimeError):
            recovery.maybe_recover(driver, PROFILE, UC, Flaky(), pause=clock.pause, wall_clock=clock, monotonic=clock,
                                   notify=self.notifier(), deadline_seconds=120)
        self.assertNotIn('totp', driver.inserted)
        del original

    def test_known_video_id_attempt_refuses_recovery(self):
        # A saved video ID means the file already reached Studio: this path may
        # never run a fresh sign-in flow that could restart an upload.
        clock = Clock()
        driver = FakeDriver([google('/v3/signin/accountchooser')])
        with self.assertRaises(recovery.RecoveryBlocked):
            recovery.maybe_recover(driver, PROFILE, UC, Lease(),
                                   phase_before_upload=False, pause=clock.pause, wall_clock=clock, monotonic=clock,
                                   notify=self.notifier(), deadline_seconds=120)
        self.assertEqual(driver.inserted, [])

    def test_transferred_file_without_id_refuses_recovery(self):
        clock = Clock()
        driver = FakeDriver([google('/v3/signin/accountchooser')])
        with self.assertRaises(recovery.RecoveryBlocked) as error:
            recovery.maybe_recover(driver, PROFILE, UC, Lease(),
                                   phase_before_upload=False, pause=clock.pause, wall_clock=clock, monotonic=clock,
                                   notify=self.notifier(), deadline_seconds=120)
        self.assertIn('before file hand-off', str(error.exception))
        self.assertEqual(driver.events, [])


class RememberedVerdictTest(RecoveryTestBase):
    def signed_out_pages(self):
        return [studio_denied(),
                google('/v3/signin/accountchooser'),
                google('/v3/signin/challenge/pwd'),
                google('/v3/signin/challenge/totp'),
                studio()]

    def test_owner_help_verdict_stops_the_next_video_without_touching_google(self):
        clock = Clock()
        driver = FakeDriver([studio_denied(),
                             google('/v3/signin/accountchooser', signed_out=False)])
        with self.assertRaises(recovery.OwnerHelpNeeded):
            recovery.maybe_recover(driver, PROFILE, UC, Lease(), pause=clock.pause, wall_clock=clock, monotonic=clock,
                                   notify=self.notifier(), deadline_seconds=120)
        # The next queued video must not repeat the whole round trip.
        clock = Clock()
        again = FakeDriver(self.signed_out_pages())
        with self.assertRaises(recovery.OwnerHelpNeeded) as error:
            recovery.maybe_recover(again, PROFILE, UC, Lease(), pause=clock.pause, wall_clock=clock, monotonic=clock,
                                   notify=self.notifier(), deadline_seconds=120)
        self.assertIn('recently', str(error.exception))
        self.assertEqual(again.events, [])
        self.assertEqual(again.inserted, [])

    def test_attempts_are_capped_within_the_window(self):
        for _ in range(recovery.MAX_ATTEMPTS_PER_WINDOW):
            clock = Clock()
            driver = FakeDriver([studio_denied(),
                                 google('/v3/signin/accountchooser'),
                                 google('/v3/signin/challenge/pwd'),
                                 google('/v3/signin/challenge/totp'),
                                 signin_prompt()])       # never proves the channel
            with self.assertRaises(recovery.OwnerHelpNeeded):
                recovery.maybe_recover(driver, PROFILE, UC, Lease(), pause=clock.pause, wall_clock=clock, monotonic=clock, deadline_seconds=120,
                                       on_status=lambda v: None)
            # clear the owner-help cooldown so the cap itself is what we measure
            state = recovery._read_state(PROFILE, UC)
            state.pop('owner_help_at', None)
            recovery._write_state(PROFILE, UC, state)
        clock = Clock()
        blocked = FakeDriver(self.signed_out_pages())
        with self.assertRaises(recovery.OwnerHelpNeeded) as error:
            recovery.maybe_recover(blocked, PROFILE, UC, Lease(), pause=clock.pause, wall_clock=clock, monotonic=clock,
                                   notify=self.notifier(), deadline_seconds=120)
        self.assertIn('too many', str(error.exception))
        self.assertEqual(blocked.inserted, [])

    def test_a_confirmed_channel_clears_the_record(self):
        clock = Clock()
        driver = FakeDriver(self.signed_out_pages())
        result = recovery.maybe_recover(driver, PROFILE, UC, Lease(), pause=clock.pause, wall_clock=clock, monotonic=clock,
                                   notify=self.notifier(), deadline_seconds=120)
        self.assertEqual(result['recovered'], True)
        state = recovery._read_state(PROFILE, UC)
        self.assertEqual(state.get('last_outcome'), recovery.STATUS_CHANNEL_CONFIRMED)
        self.assertEqual(state.get('attempts'), [])


class InvisibleRecaptchaTest(RecoveryTestBase):
    """Google keeps an invisible reCAPTCHA on almost every sign-in page for risk
    scoring. Live capture 16.09.2026 (research/dom/invisible_recaptcha_on_pwd_20260916.json):
    a normal password page carries exactly one such node, 0x0, offsetParent null.
    Treating its mere presence as a CAPTCHA stopped every sign-in before it began."""

    def test_an_invisible_recaptcha_does_not_stop_the_sign_in(self):
        pwd = google('/v3/signin/challenge/pwd')
        pwd['recaptcha_visible'] = False           # present in the DOM, 0x0
        clock = Clock()
        driver = FakeDriver([studio_denied(),
                             google('/v3/signin/accountchooser'),
                             pwd,
                             google('/v3/signin/challenge/totp'),
                             studio()])
        result = recovery.maybe_recover(driver, PROFILE, UC, Lease(), pause=clock.pause,
                                        wall_clock=clock, monotonic=clock,
                                        deadline_seconds=120)
        self.assertEqual(result['recovered'], True)
        self.assertEqual(driver.inserted, ['password', 'totp'])

    def test_a_visible_recaptcha_does_stop_it(self):
        pwd = google('/v3/signin/challenge/pwd')
        pwd['recaptcha_visible'] = True
        clock = Clock()
        driver = FakeDriver([studio_denied(),
                             google('/v3/signin/accountchooser'), pwd])
        with self.assertRaises(recovery.OwnerHelpNeeded):
            recovery.maybe_recover(driver, PROFILE, UC, Lease(), pause=clock.pause,
                                   wall_clock=clock, monotonic=clock, deadline_seconds=120)
        self.assertEqual(driver.inserted, [])

    def test_the_live_predicate_still_requires_visibility(self):
        # Guards the rule itself: whoever edits this must keep the geometry gate.
        source = recovery._CHALLENGE_SIGNALS_JS
        self.assertIn('offsetParent', source)
        self.assertIn('getBoundingClientRect', source)
        self.assertIn('/challenge/recaptcha', source)
        # the bare word must not be a text trigger: it appears in visible fine print
        self.assertNotIn('recaptcha|', source.lower().split('captcha:')[1][:200])


class TelegramPingTest(RecoveryTestBase):
    def test_owner_help_pings_once_with_a_human_reason(self):
        clock = Clock()
        driver = FakeDriver([studio_denied(),
                             google('/v3/signin/accountchooser', phone=True)])
        with self.assertRaises(recovery.OwnerHelpNeeded):
            recovery.maybe_recover(driver, PROFILE, UC, Lease(), pause=clock.pause,
                                   wall_clock=clock, monotonic=clock,
                                   notify=self.notifier(), deadline_seconds=120,
                                   channel_label='Cień Trzeciej Rzeszy')
        self.assertEqual(len(self.pings), 1)
        self.assertEqual(self.pings[0]['uc'], UC)
        self.assertEqual(self.pings[0]['label'], 'Cień Trzeciej Rzeszy')
        # причина должна быть внутренней строкой; перевод делает отправитель
        self.assertTrue(self.pings[0]['reason'])

    def test_the_cooldown_refusal_does_not_ping_again(self):
        clock = Clock()
        first = FakeDriver([studio_denied(),
                            google('/v3/signin/accountchooser', phone=True)])
        with self.assertRaises(recovery.OwnerHelpNeeded):
            recovery.maybe_recover(first, PROFILE, UC, Lease(), pause=clock.pause,
                                   wall_clock=clock, monotonic=clock,
                                   notify=self.notifier(), deadline_seconds=120)
        self.assertEqual(len(self.pings), 1)
        # следующий ролик очереди упирается в остывание — второго сообщения нет
        again = FakeDriver([studio_denied(), google('/v3/signin/accountchooser')])
        with self.assertRaises(recovery.OwnerHelpNeeded):
            recovery.maybe_recover(again, PROFILE, UC, Lease(), pause=clock.pause,
                                   wall_clock=clock, monotonic=clock,
                                   notify=self.notifier(), deadline_seconds=120)
        self.assertEqual(len(self.pings), 1)

    def test_a_broken_notifier_never_breaks_the_recovery(self):
        def explode(*a, **k):
            raise RuntimeError('телеграм недоступен')

        clock = Clock()
        driver = FakeDriver([studio_denied(),
                             google('/v3/signin/accountchooser'),
                             google('/v3/signin/challenge/pwd'),
                             google('/v3/signin/challenge/totp'),
                             studio()])
        result = recovery.maybe_recover(driver, PROFILE, UC, Lease(), pause=clock.pause,
                                        wall_clock=clock, monotonic=clock,
                                        notify=explode, deadline_seconds=120)
        self.assertEqual(result['recovered'], True)

    def test_the_human_wording_covers_every_verdict_in_the_code(self):
        import re as _re
        source = (ROOT / 'src' / 'upload_account_recovery.py').read_text(encoding='utf-8')
        raised = set(_re.findall(r'raise OwnerHelpNeeded[(]"([^"]+)"[)]', source))
        missing = sorted(r for r in raised if r not in recovery._HUMAN_REASON)
        self.assertEqual(missing, [], f'без человеческой формулировки: {missing}')


class StallTest(RecoveryTestBase):
    def test_a_page_that_never_moves_names_the_stuck_step(self):
        # The password form never renders: the flow must say where it stopped
        # instead of holding the profile for the whole budget.
        class Stuck(FakeDriver):
            def execute_script(self, script, *args):
                if 'challenge/pwd' in script:
                    return None          # form never appears
                return super().execute_script(script, *args)

        clock = Clock()
        driver = Stuck([studio_denied(),
                        google('/v3/signin/accountchooser'),
                        google('/v3/signin/challenge/pwd')])
        with self.assertRaises(recovery.RecoveryBlocked) as error:
            recovery.maybe_recover(driver, PROFILE, UC, Lease(), pause=clock.pause,
                                   wall_clock=clock, monotonic=clock, deadline_seconds=100000)
        self.assertIn('pwd', str(error.exception))
        self.assertEqual(driver.inserted, [])


class SecrecyTest(RecoveryTestBase):
    def test_no_status_or_error_ever_carries_a_secret(self):
        clock = Clock()
        driver = FakeDriver([
            studio_denied(),
            google('/v3/signin/accountchooser'),
            google('/v3/signin/challenge/pwd'),
            google('/v3/signin/challenge/totp'),
            studio(),
        ])
        result = recovery.maybe_recover(driver, PROFILE, UC, Lease(),
                                        on_status=self.statuses.append, pause=clock.pause, wall_clock=clock, monotonic=clock,
                                   notify=self.notifier(), deadline_seconds=120)
        blob = json.dumps({'statuses': self.statuses, 'result': result}, default=str)
        for secret in (PASSWORD, TOTP, LOGIN):
            self.assertNotIn(secret, blob)


if __name__ == '__main__':
    unittest.main()
